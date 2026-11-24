// The drawing surface used by both the phone pad and the Mac view.
// Two stacked canvases: `base` (the page, redrawn only when it changes) and `overlay` (strokes being drawn,
// hover cursors, lasso, laser), redrawn every animation frame while something moves.

import { newId } from './model.js';
import {
  AssetCache, bounds, drawObj, drawPage, hitObj, inLasso, splitStroke, toPage, toScreen, traceStroke, unionBounds,
} from './render.js';
import { recognizeShape } from './shapes.js';

const SNAP_HOLD_MS = 550;
const LASER_FADE_MS = 900;
const MARGIN = 16;

function smooth(pts) {
  if (pts.length < 4) return pts;
  const out = [pts[0]];
  for (let i = 1; i < pts.length - 1; i += 1) {
    const [a, b, c] = [pts[i - 1], pts[i], pts[i + 1]];
    const p = [(a[0] + 2 * b[0] + c[0]) / 4, (a[1] + 2 * b[1] + c[1]) / 4, b[2]];
    if (b.length > 3) p.push(b[3]);
    out.push(p);
  }
  out.push(pts[pts.length - 1]);
  return out;
}

const round = (v, d = 2) => Math.round(v * 10 ** d) / 10 ** d;
const cleanPt = (p) => (p.length > 3 ? [round(p[0]), round(p[1]), round(p[2], 3), round(p[3], 3)] : [round(p[0]), round(p[1]), round(p[2], 3)]);

export class InkCanvas {
  constructor(host, opts) {
    this.host = host;
    this.role = opts.role; // 'pad' | 'mac'
    this.send = opts.send || (() => {}); // (msg) => void, for live/hover/laser
    this.commit = opts.commit || (() => {}); // (op) => void, for saved changes
    this.onSelection = opts.onSelection || (() => {});
    this.onViewChange = opts.onViewChange || (() => {});
    this.readOnly = !!opts.readOnly;
    this.assets = new AssetCache(opts.assetUrl, () => this.invalidate());
    this.state = { pages: [] };
    this.pageId = null;
    this.tool = { tool: 'pen', color: '#1d1d1f', size: 3, eraser: 'stroke', snap: true };
    this.view = { s: 1, ox: MARGIN, oy: MARGIN };
    this.fitted = false;
    this.highlight = null; // Map id -> color (compare mode)
    this.hiddenExtra = new Set();

    this.base = document.createElement('canvas');
    this.overlay = document.createElement('canvas');
    this.base.className = 'ink-base';
    this.overlay.className = 'ink-overlay';
    host.append(this.base, this.overlay);

    this.drawing = null; // own stroke in progress
    this.pending = new Map(); // obj id -> stroke committed but not yet echoed by the server
    this.remote = new Map(); // peer stroke id -> live stroke
    this.hovers = new Map(); // peer id -> {x, y, page, t}
    this.myHover = null;
    this.erasing = null; // {ids:Set, pieces:Map}
    this.lasso = null; // [[x,y],...] while drawing a lasso
    this.selection = new Set();
    this.moving = null; // {start:[x,y], dx, dy}
    this.lasers = new Map(); // peer id ('me' for own) -> [{x,y,t}]
    this.touches = new Map();
    this.gesture = null;
    this.dirty = true;
    this.overlayDirty = true;
    this.sentLive = 0;

    this.resizeObserver = new ResizeObserver(() => this.resize());
    this.resizeObserver.observe(host);
    this.bindInput();
    this.loop = this.loop.bind(this);
    requestAnimationFrame(this.loop);
  }

  // ---------------------------------------------------------------- data
  get page() {
    return this.state.pages.find((p) => p.id === this.pageId) || this.state.pages[0] || null;
  }

  setState(state, pageId) {
    this.state = state;
    if (pageId) this.pageId = pageId;
    if (!this.page) this.pageId = state.pages[0]?.id || null;
    this.pruneSelection();
    this.invalidate();
  }

  setPage(id, { fit = true } = {}) {
    if (id === this.pageId) return;
    this.pageId = id;
    this.clearSelection();
    if (fit) this.fitted = false;
    this.invalidate();
  }

  // Called after the server applied a change (already applied to this.state by the owner).
  changed(op, touched) {
    if (op.op === 'add') for (const o of op.objs) this.pending.delete(o.id), this.dropLive(o.id);
    if (op.op === 'batch') for (const sub of op.ops) if (sub.op === 'add') for (const o of sub.objs) this.pending.delete(o.id);
    this.pruneSelection();
    if (!this.page || touched.includes(this.page.id)) this.invalidate();
  }

  dropLive(id) {
    for (const [key, s] of this.remote) if (s.id === id) this.remote.delete(key);
  }

  setTool(changes) {
    Object.assign(this.tool, changes);
    if (changes.tool && changes.tool !== 'lasso') this.clearSelection();
    this.overlayDirty = true;
  }

  invalidate() {
    this.dirty = true;
    this.overlayDirty = true;
  }

  // ---------------------------------------------------------------- view
  resize() {
    const dpr = window.devicePixelRatio || 1;
    const { width, height } = this.host.getBoundingClientRect();
    for (const c of [this.base, this.overlay]) {
      c.width = Math.max(1, Math.round(width * dpr));
      c.height = Math.max(1, Math.round(height * dpr));
      c.style.width = `${width}px`;
      c.style.height = `${height}px`;
    }
    this.cssW = width;
    this.cssH = height;
    if (!this.fitted) this.fit();
    this.invalidate();
  }

  fit(mode = 'width') {
    const page = this.page;
    if (!page || !this.cssW) return;
    const sw = (this.cssW - MARGIN * 2) / page.w;
    const sh = (this.cssH - MARGIN * 2) / page.h;
    const s = mode === 'page' ? Math.min(sw, sh) : sw;
    this.view = { s, ox: (this.cssW - page.w * s) / 2, oy: MARGIN };
    if (mode === 'page') this.view.oy = (this.cssH - page.h * s) / 2;
    this.fitted = true;
    this.invalidate();
    this.onViewChange(this.view);
  }

  zoomAt(factor, cx, cy) {
    const s = Math.min(Math.max(this.view.s * factor, 0.05), 12);
    const k = s / this.view.s;
    this.view = { s, ox: cx - (cx - this.view.ox) * k, oy: cy - (cy - this.view.oy) * k };
    this.invalidate();
    this.onViewChange(this.view);
  }

  panBy(dx, dy) {
    this.view.ox += dx;
    this.view.oy += dy;
    this.invalidate();
    this.onViewChange(this.view);
  }

  // ---------------------------------------------------------------- input
  bindInput() {
    const el = this.overlay;
    el.style.touchAction = 'none';
    el.addEventListener('pointerdown', (e) => this.onDown(e));
    el.addEventListener('pointermove', (e) => this.onMove(e));
    el.addEventListener('pointerup', (e) => this.onUp(e));
    el.addEventListener('pointercancel', (e) => this.onUp(e, true));
    el.addEventListener('pointerleave', (e) => {
      if (e.pointerType === 'pen' || e.pointerType === 'mouse') this.hoverEnd();
    });
    el.addEventListener('contextmenu', (e) => e.preventDefault());
    el.addEventListener('wheel', (e) => {
      e.preventDefault();
      const r = el.getBoundingClientRect();
      if (e.ctrlKey || e.metaKey) this.zoomAt(Math.exp(-e.deltaY * 0.01), e.clientX - r.left, e.clientY - r.top);
      else this.panBy(-e.deltaX, -e.deltaY);
    }, { passive: false });
  }

  capture(id) {
    try {
      this.overlay.setPointerCapture(id);
    } catch {
      /* the pointer is already gone */
    }
  }

  local(e) {
    const r = this.overlay.getBoundingClientRect();
    return [e.clientX - r.left, e.clientY - r.top];
  }

  isInk(e) {
    // On the phone only the S Pen draws (palm rejection); fingers pan and zoom.
    if (e.pointerType === 'touch') return false;
    return true;
  }

  sample(e) {
    const [sx, sy] = this.local(e);
    const [x, y] = toPage(this.view, sx, sy);
    let p = e.pressure;
    if (e.pointerType === 'mouse' || !p) p = 0.5;
    else if (e.pointerType === 'pen') {
      // Chrome on the S25 Ultra tops out around 0.56: stretch to the highest pressure seen so far,
      // so a firm press reaches full width on any pen.
      this.penMax = Math.max(this.penMax || 0.6, p);
      p = Math.min(1, (p / this.penMax) ** 0.8);
    }
    let tilt = 0;
    if (typeof e.altitudeAngle === 'number' && e.pointerType === 'pen') tilt = Math.max(0, 1 - e.altitudeAngle / (Math.PI / 2));
    else if (e.tiltX || e.tiltY) tilt = Math.min(1, Math.hypot(e.tiltX, e.tiltY) / 90);
    return this.tool.tool === 'pencil' ? [x, y, p, tilt] : [x, y, p];
  }

  sideButton(e) {
    // S Pen side button (barrel) or an eraser end: erase while held
    return e.pointerType === 'pen' && ((e.buttons & 2) || (e.buttons & 32) || e.button === 5);
  }

  onDown(e) {
    if (!this.page) return;
    if (!this.isInk(e)) {
      this.touches.set(e.pointerId, this.local(e));
      this.capture(e.pointerId);
      this.startGesture();
      return;
    }
    if (e.pointerType === 'mouse' && e.button === 1) return; // middle button: ignore
    if (e.pointerType === 'mouse' && (e.button === 2 || e.altKey || this.spaceDown)) {
      this.dragPan = this.local(e);
      this.capture(e.pointerId);
      return;
    }
    this.capture(e.pointerId);
    this.hoverEnd();
    let tool = this.tool.tool;
    if (this.sideButton(e)) tool = 'eraser';
    if (this.readOnly && tool !== 'lasso' && tool !== 'pan' && tool !== 'laser') tool = 'lasso';
    const pt = this.sample(e);
    this.active = { tool, id: e.pointerId };
    if (tool === 'pan') {
      this.dragPan = this.local(e);
    } else if (tool === 'eraser') {
      this.erasing = { ids: new Set(), pieces: new Map() };
      this.eraseAt(pt[0], pt[1]);
    } else if (tool === 'lasso') {
      const b = this.selectionBounds();
      if (b && pt[0] >= b[0] && pt[0] <= b[2] && pt[1] >= b[1] && pt[1] <= b[3] && !this.readOnly) {
        this.moving = { start: [pt[0], pt[1]], dx: 0, dy: 0 };
      } else {
        this.clearSelection();
        this.lasso = [[pt[0], pt[1]]];
      }
    } else if (tool === 'laser') {
      this.addLaser('me', pt[0], pt[1]);
    } else {
      this.drawing = { id: newId(), type: 'stroke', tool, color: this.tool.color, size: this.tool.size, pts: [pt],
        page: this.page.id, lastMove: performance.now(), snapped: false };
      this.sentLive = 0;
    }
    this.overlayDirty = true;
  }

  onMove(e) {
    if (this.touches.has(e.pointerId)) {
      this.touches.set(e.pointerId, this.local(e));
      this.moveGesture();
      return;
    }
    if (this.dragPan) {
      const [x, y] = this.local(e);
      this.panBy(x - this.dragPan[0], y - this.dragPan[1]);
      this.dragPan = [x, y];
      return;
    }
    if (!this.active) {
      if (e.pointerType === 'pen') this.watchSideButton(e);
      if ((e.pointerType === 'pen' || e.pointerType === 'mouse') && this.page) this.hover(e);
      return;
    }
    const events = e.getCoalescedEvents ? e.getCoalescedEvents() : [e];
    for (const ev of events.length ? events : [e]) this.moveSample(this.sample(ev));
    this.overlayDirty = true;
  }

  moveSample(pt) {
    const tool = this.active.tool;
    if (tool === 'eraser') this.eraseAt(pt[0], pt[1]);
    else if (tool === 'laser') this.addLaser('me', pt[0], pt[1]);
    else if (tool === 'lasso') {
      if (this.moving) {
        this.moving.dx = pt[0] - this.moving.start[0];
        this.moving.dy = pt[1] - this.moving.start[1];
        this.dirty = true;
      } else if (this.lasso) this.lasso.push([pt[0], pt[1]]);
    } else if (this.drawing && !this.drawing.snapped) {
      const last = this.drawing.pts[this.drawing.pts.length - 1];
      const d = Math.hypot(pt[0] - last[0], pt[1] - last[1]);
      if (d * this.view.s < 0.75) {
        last[2] = Math.max(last[2], pt[2]);
        return;
      }
      // "holding still" is measured from where the hold began, so small pen jitter doesn't restart it
      const anchor = this.drawing.anchor || last;
      if (Math.hypot(pt[0] - anchor[0], pt[1] - anchor[1]) * this.view.s > 7) {
        this.drawing.lastMove = performance.now();
        this.drawing.anchor = pt;
      }
      this.drawing.pts.push(pt);
    }
  }

  // Samsung keeps "S Pen button + touch" for itself, so the browser never sees it. A quick click of the
  // button while hovering does reach us (buttons goes 1 -> 0 with pressure 0): use it as a toggle.
  watchSideButton(e) {
    const held = (e.buttons & 3) !== 0 && !e.pressure;
    if (held && !this.sideDown) this.sideDown = performance.now();
    else if (!held && this.sideDown) {
      const ms = performance.now() - this.sideDown;
      this.sideDown = 0;
      if (ms < 800) this.onSideClick?.();
    }
  }

  onUp(e, cancelled = false) {
    if (this.touches.has(e.pointerId)) {
      this.touches.delete(e.pointerId);
      this.startGesture();
      if (!this.touches.size) this.endGesture();
      return;
    }
    if (this.dragPan) {
      this.dragPan = null;
      if (!this.active) return;
    }
    if (!this.active || this.active.id !== e.pointerId) return;
    const tool = this.active.tool;
    this.active = null;
    if (tool === 'eraser') this.finishErase(cancelled);
    else if (tool === 'lasso') this.finishLasso(cancelled);
    else if (tool === 'laser') { /* fades by itself */ } else this.finishStroke(cancelled);
    this.overlayDirty = true;
  }

  // ---------------------------------------------------------------- strokes
  finishStroke(cancelled) {
    const stroke = this.drawing;
    this.drawing = null;
    if (!stroke) return;
    if (cancelled && stroke.pts.length < 3) {
      this.send({ t: 'live', id: stroke.id, end: true, cancel: true });
      return;
    }
    const pts = (stroke.snapped ? stroke.pts : smooth(stroke.pts)).map(cleanPt);
    const obj = { id: stroke.id, type: 'stroke', tool: stroke.tool, color: stroke.color, size: stroke.size, pts };
    this.pending.set(obj.id, { ...obj, page: stroke.page });
    this.send({ t: 'live', id: stroke.id, end: true });
    this.commit({ op: 'add', page: stroke.page, objs: [obj] });
    setTimeout(() => {
      if (this.pending.delete(obj.id)) this.overlayDirty = true;
    }, 8000);
  }

  pushLive() {
    const s = this.drawing;
    if (!s) return;
    if (s.resetLive) {
      this.send({ t: 'live', id: s.id, page: s.page, tool: s.tool, color: s.color, size: s.size, pts: s.pts, reset: true });
      s.resetLive = false;
      this.sentLive = s.pts.length;
      return;
    }
    if (s.pts.length <= this.sentLive) return;
    const fresh = s.pts.slice(this.sentLive);
    this.send({ t: 'live', id: s.id, page: s.page, tool: s.tool, color: s.color, size: s.size, pts: fresh });
    this.sentLive = s.pts.length;
  }

  trySnap() {
    const s = this.drawing;
    if (!s || s.snapped || !this.tool.snap) return;
    if (performance.now() - s.lastMove < SNAP_HOLD_MS || s.pts.length < 6) return;
    const shape = recognizeShape(s.pts);
    s.lastMove = Infinity; // only try once per hold
    if (!shape) return;
    s.pts = shape.pts;
    s.snapped = true;
    s.resetLive = true;
    this.overlayDirty = true;
    if (navigator.vibrate) navigator.vibrate(12);
  }

  // ---------------------------------------------------------------- erasing
  eraseAt(x, y) {
    const page = this.page;
    const r = (this.tool.eraser === 'partial' ? 9 : 6) / this.view.s;
    const er = this.erasing;
    if (this.tool.eraser === 'partial') {
      for (const id of page.order) {
        const obj = page.objs[id];
        if (!obj || obj.type !== 'stroke') continue;
        const current = er.pieces.get(id) || [obj.pts];
        if (!current.some((pts) => hitObj({ ...obj, pts }, x, y, r))) continue;
        const next = [];
        for (const pts of current) next.push(...splitStroke({ ...obj, pts }, x, y, r));
        er.pieces.set(id, next);
        er.ids.add(id);
      }
    } else {
      for (const id of page.order) {
        const obj = page.objs[id];
        if (obj && !er.ids.has(id) && obj.type === 'stroke' && hitObj(obj, x, y, r)) er.ids.add(id);
      }
    }
    this.eraserPos = [x, y, r];
    this.dirty = true;
  }

  finishErase(cancelled) {
    const er = this.erasing;
    this.erasing = null;
    this.eraserPos = null;
    this.dirty = true;
    if (!er || cancelled || !er.ids.size) return;
    const page = this.page;
    const ids = [...er.ids];
    if (this.tool.eraser !== 'partial') {
      this.commit({ op: 'remove', page: page.id, ids });
      return;
    }
    const added = [];
    for (const id of ids) {
      const obj = page.objs[id];
      for (const pts of er.pieces.get(id) || []) added.push({ ...obj, id: newId(), pts: pts.map(cleanPt) });
    }
    const ops = [{ op: 'remove', page: page.id, ids }];
    if (added.length) ops.push({ op: 'add', page: page.id, objs: added });
    this.hiddenExtra = new Set(ids);
    for (const obj of added) this.pending.set(obj.id, { ...obj, page: page.id });
    this.commit(ops.length === 1 ? ops[0] : { op: 'batch', ops });
    setTimeout(() => { this.hiddenExtra = new Set(); this.dirty = true; }, 4000);
  }

  // ---------------------------------------------------------------- lasso & selection
  finishLasso(cancelled) {
    if (this.moving) {
      const { dx, dy } = this.moving;
      this.moving = null;
      this.dirty = true;
      if (!cancelled && (Math.abs(dx) > 0.5 || Math.abs(dy) > 0.5)) {
        this.commit({ op: 'move', page: this.page.id, ids: [...this.selection], dx: round(dx), dy: round(dy) });
      }
      this.emitSelection();
      return;
    }
    const poly = this.lasso;
    this.lasso = null;
    if (!poly || cancelled || poly.length < 3) return;
    const page = this.page;
    this.selection = new Set(page.order.filter((id) => page.objs[id] && inLasso(page.objs[id], poly)));
    this.emitSelection();
  }

  selectIds(ids) {
    this.selection = new Set(ids);
    this.emitSelection();
    this.overlayDirty = true;
  }

  clearSelection() {
    if (!this.selection.size) return;
    this.selection = new Set();
    this.emitSelection();
    this.overlayDirty = true;
  }

  pruneSelection() {
    const page = this.page;
    if (!page || !this.selection.size) return;
    const before = this.selection.size;
    this.selection = new Set([...this.selection].filter((id) => page.objs[id]));
    if (this.selection.size !== before) this.emitSelection();
  }

  selectionBounds() {
    const page = this.page;
    if (!page || !this.selection.size) return null;
    return unionBounds([...this.selection].map((id) => page.objs[id]).filter(Boolean));
  }

  emitSelection() {
    const b = this.selectionBounds();
    let rect = null;
    if (b) {
      const [x0, y0] = toScreen(this.view, b[0], b[1]);
      const [x1, y1] = toScreen(this.view, b[2], b[3]);
      rect = { x: x0, y: y0, w: x1 - x0, h: y1 - y0 };
    }
    this.onSelection([...this.selection], rect);
  }

  // ---------------------------------------------------------------- laser & hover
  addLaser(who, x, y) {
    let trail = this.lasers.get(who);
    if (!trail) this.lasers.set(who, (trail = []));
    trail.push({ x, y, t: performance.now() });
    if (who === 'me') (this.laserOut ??= []).push([round(x), round(y)]);
    this.overlayDirty = true;
  }

  hover(e) {
    const [sx, sy] = this.local(e);
    const [x, y] = toPage(this.view, sx, sy);
    this.myHover = { x, y };
    this.hoverOut = true;
    this.overlayDirty = true;
  }

  hoverEnd() {
    if (!this.myHover) return;
    this.myHover = null;
    this.send({ t: 'hover_end' });
    this.overlayDirty = true;
  }

  // Messages relayed from the other device.
  remoteMessage(msg) {
    if (msg.t === 'live') {
      if (msg.end) {
        const s = this.remote.get(msg.id);
        if (s) {
          if (msg.cancel) this.remote.delete(msg.id);
          else setTimeout(() => { this.remote.delete(msg.id); this.overlayDirty = true; }, 5000);
        }
      } else {
        let s = this.remote.get(msg.id);
        if (!s || msg.reset) {
          // type is needed: drawObj() only draws objects whose type it knows
          s = { id: msg.id, type: 'stroke', page: msg.page, tool: msg.tool, color: msg.color, size: msg.size, pts: [] };
          this.remote.set(msg.id, s);
        }
        s.pts.push(...msg.pts);
      }
    } else if (msg.t === 'hover') {
      this.hovers.set(msg.from, { x: msg.x, y: msg.y, page: msg.page, t: performance.now() });
    } else if (msg.t === 'hover_end') {
      this.hovers.delete(msg.from);
    } else if (msg.t === 'laser') {
      if (msg.page === this.page?.id) for (const [x, y] of msg.pts) this.addLaser(msg.from, x, y);
    }
    this.overlayDirty = true;
  }

  // ---------------------------------------------------------------- touch gestures (pan / pinch)
  startGesture() {
    const pts = [...this.touches.values()];
    if (!pts.length) return;
    const cx = pts.reduce((a, p) => a + p[0], 0) / pts.length;
    const cy = pts.reduce((a, p) => a + p[1], 0) / pts.length;
    const spread = pts.length > 1 ? Math.hypot(pts[0][0] - pts[1][0], pts[0][1] - pts[1][1]) : 0;
    this.gesture = { cx, cy, spread, count: pts.length, startedAt: this.gesture?.startedAt ?? performance.now(), moved: this.gesture?.moved ?? false };
  }

  moveGesture() {
    const g = this.gesture;
    const pts = [...this.touches.values()];
    if (!g || !pts.length) return;
    const cx = pts.reduce((a, p) => a + p[0], 0) / pts.length;
    const cy = pts.reduce((a, p) => a + p[1], 0) / pts.length;
    if (Math.hypot(cx - g.cx, cy - g.cy) > 3) g.moved = true;
    this.panBy(cx - g.cx, cy - g.cy);
    if (pts.length > 1 && g.spread) {
      const spread = Math.hypot(pts[0][0] - pts[1][0], pts[0][1] - pts[1][1]);
      this.zoomAt(spread / g.spread, cx, cy);
      g.spread = spread;
      g.moved = true;
    }
    g.cx = cx;
    g.cy = cy;
  }

  endGesture() {
    const g = this.gesture;
    this.gesture = null;
    if (!g) return;
    const quick = performance.now() - g.startedAt < 250;
    if (quick && !g.moved && g.count === 2) this.onTwoFingerTap?.();
    if (quick && !g.moved && g.count === 1) {
      const now = performance.now();
      if (this.lastTap && now - this.lastTap < 320) this.fit(this.view.s > ((this.cssW - 2 * MARGIN) / this.page.w) * 1.05 ? 'width' : 'page');
      this.lastTap = now;
    }
  }

  // ---------------------------------------------------------------- render loop
  loop(now) {
    if (this.dead) return;
    requestAnimationFrame(this.loop);
    if (this.drawing) {
      this.trySnap();
      this.pushLive();
    }
    if (this.laserOut?.length) {
      this.send({ t: 'laser', page: this.page?.id, pts: this.laserOut });
      this.laserOut = [];
    }
    if (this.hoverOut && this.myHover) {
      this.send({ t: 'hover', page: this.page?.id, x: round(this.myHover.x), y: round(this.myHover.y) });
      this.hoverOut = false;
    }
    let animating = false;
    for (const [who, trail] of this.lasers) {
      while (trail.length && now - trail[0].t > LASER_FADE_MS) trail.shift();
      if (!trail.length) this.lasers.delete(who);
      else animating = true;
    }
    for (const [who, h] of this.hovers) if (now - h.t > 3000) this.hovers.delete(who);
    if (this.dirty) this.renderBase();
    if (this.overlayDirty || animating || this.drawing) this.renderOverlay(now);
  }

  hiddenIds() {
    const hidden = new Set(this.hiddenExtra);
    if (this.erasing) for (const id of this.erasing.ids) hidden.add(id);
    if (this.moving) for (const id of this.selection) hidden.add(id);
    return hidden;
  }

  renderBase() {
    this.dirty = false;
    const ctx = this.base.getContext('2d');
    const dpr = window.devicePixelRatio || 1;
    ctx.setTransform(1, 0, 0, 1, 0, 0);
    ctx.clearRect(0, 0, this.base.width, this.base.height);
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    const page = this.page;
    if (!page) return;
    if (!this.fitted) this.fit();
    drawPage(ctx, page, this.view, { assets: this.assets, hidden: this.hiddenIds(), highlight: this.highlight });
  }

  renderOverlay(now) {
    this.overlayDirty = false;
    const ctx = this.overlay.getContext('2d');
    const dpr = window.devicePixelRatio || 1;
    ctx.setTransform(1, 0, 0, 1, 0, 0);
    ctx.clearRect(0, 0, this.overlay.width, this.overlay.height);
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    const page = this.page;
    if (!page) return;
    const view = this.view;
    // committed strokes waiting for the server's echo, remote live strokes, own live stroke
    for (const s of this.pending.values()) if (s.page === page.id && !page.objs[s.id]) drawObj(ctx, s, view, this.assets);
    for (const s of this.remote.values()) if (s.page === page.id && s.pts.length && !page.objs[s.id]) drawObj(ctx, s, view, this.assets);
    if (this.drawing && this.drawing.page === page.id) drawObj(ctx, this.drawing, view, this.assets);
    // partial-eraser pieces
    if (this.erasing) {
      for (const [id, pieces] of this.erasing.pieces) {
        const obj = page.objs[id];
        if (obj) for (const pts of pieces) drawObj(ctx, { ...obj, pts }, view, this.assets);
      }
    }
    // selection being moved
    if (this.moving) {
      const moved = { ox: view.ox + this.moving.dx * view.s, oy: view.oy + this.moving.dy * view.s, s: view.s };
      for (const id of this.selection) if (page.objs[id]) drawObj(ctx, page.objs[id], moved, this.assets);
    }
    // selection box
    const b = this.selectionBounds();
    if (b) {
      const dx = this.moving ? this.moving.dx : 0;
      const dy = this.moving ? this.moving.dy : 0;
      const [x0, y0] = toScreen(view, b[0] + dx, b[1] + dy);
      const [x1, y1] = toScreen(view, b[2] + dx, b[3] + dy);
      ctx.save();
      ctx.strokeStyle = '#0a84ff';
      ctx.fillStyle = 'rgba(10,132,255,.06)';
      ctx.setLineDash([6, 4]);
      ctx.lineWidth = 1.5;
      ctx.fillRect(x0 - 4, y0 - 4, x1 - x0 + 8, y1 - y0 + 8);
      ctx.strokeRect(x0 - 4, y0 - 4, x1 - x0 + 8, y1 - y0 + 8);
      ctx.restore();
    }
    if (this.lasso && this.lasso.length > 1) {
      ctx.save();
      ctx.strokeStyle = '#0a84ff';
      ctx.setLineDash([5, 5]);
      ctx.lineWidth = 1.5;
      ctx.beginPath();
      this.lasso.forEach(([x, y], i) => {
        const [sx, sy] = toScreen(view, x, y);
        if (i) ctx.lineTo(sx, sy);
        else ctx.moveTo(sx, sy);
      });
      ctx.stroke();
      ctx.restore();
    }
    if (this.eraserPos) {
      const [x, y, r] = this.eraserPos;
      const [sx, sy] = toScreen(view, x, y);
      ctx.save();
      ctx.strokeStyle = 'rgba(120,120,128,.9)';
      ctx.lineWidth = 1;
      ctx.beginPath();
      ctx.arc(sx, sy, r * view.s, 0, Math.PI * 2);
      ctx.stroke();
      ctx.restore();
    }
    // laser trails
    for (const trail of this.lasers.values()) {
      for (let i = 1; i < trail.length; i += 1) {
        const a = trail[i - 1];
        const c = trail[i];
        const life = 1 - (now - c.t) / LASER_FADE_MS;
        if (life <= 0) continue;
        const [ax, ay] = toScreen(view, a.x, a.y);
        const [cx, cy] = toScreen(view, c.x, c.y);
        ctx.strokeStyle = `rgba(255,45,45,${(life * 0.9).toFixed(3)})`;
        ctx.lineWidth = 2 + 6 * life;
        ctx.lineCap = 'round';
        ctx.beginPath();
        ctx.moveTo(ax, ay);
        ctx.lineTo(cx, cy);
        ctx.stroke();
      }
      const head = trail[trail.length - 1];
      if (head) {
        const [hx, hy] = toScreen(view, head.x, head.y);
        ctx.fillStyle = 'rgba(255,45,45,.95)';
        ctx.shadowColor = 'rgba(255,45,45,.8)';
        ctx.shadowBlur = 14;
        ctx.beginPath();
        ctx.arc(hx, hy, 6, 0, Math.PI * 2);
        ctx.fill();
        ctx.shadowBlur = 0;
      }
    }
    // the other device's pen hovering
    for (const h of this.hovers.values()) {
      if (h.page !== page.id) continue;
      const [sx, sy] = toScreen(view, h.x, h.y);
      ctx.save();
      ctx.strokeStyle = this.tool.color === '#ffffff' ? '#1d1d1f' : '#0a84ff';
      ctx.fillStyle = 'rgba(10,132,255,.15)';
      ctx.lineWidth = 1.5;
      ctx.beginPath();
      ctx.arc(sx, sy, 7, 0, Math.PI * 2);
      ctx.fill();
      ctx.stroke();
      ctx.restore();
    }
  }

  // Screen rectangle of the page (for positioning toolbars).
  pageRect() {
    const page = this.page;
    if (!page) return null;
    const [x0, y0] = toScreen(this.view, 0, 0);
    return { x: x0, y: y0, w: page.w * this.view.s, h: page.h * this.view.s };
  }

  destroy() {
    this.resizeObserver.disconnect();
    this.base.remove();
    this.overlay.remove();
    this.dead = true;
  }
}

export { bounds, traceStroke };
