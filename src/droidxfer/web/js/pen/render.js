// Drawing pages on a <canvas>. Width/opacity rules match core/penrender.py so exports look like the screen.

export const GRID = 40;
const TEMPLATE_COLOR = 'rgb(170,190,215)';

export function strokeWidth(tool, size, p, t = 0) {
  if (tool === 'highlighter') return size * 2.5;
  if (tool === 'pencil') return size * (0.5 + 0.5 * p) * (1 + 1.5 * t);
  return size * (0.3 + 0.7 * p);
}

export function strokeAlpha(stroke) {
  if (stroke.tool === 'highlighter') return 0.35;
  if (stroke.tool === 'pencil') {
    let sum = 0;
    for (const p of stroke.pts) sum += p[2];
    return 0.25 + 0.6 * (sum / stroke.pts.length);
  }
  return 1;
}

export function bounds(obj) {
  if (obj.type === 'stroke') {
    let x0 = Infinity;
    let y0 = Infinity;
    let x1 = -Infinity;
    let y1 = -Infinity;
    for (const p of obj.pts) {
      if (p[0] < x0) x0 = p[0];
      if (p[1] < y0) y0 = p[1];
      if (p[0] > x1) x1 = p[0];
      if (p[1] > y1) y1 = p[1];
    }
    const pad = strokeWidth(obj.tool, obj.size, 1, 1) / 2 + 1;
    return [x0 - pad, y0 - pad, x1 + pad, y1 + pad];
  }
  if (obj.type === 'text') {
    const lines = Math.max(1, textLines(obj).length);
    return [obj.x, obj.y, obj.x + obj.w, obj.y + lines * obj.size * 1.3];
  }
  return [obj.x, obj.y, obj.x + obj.w, obj.y + obj.h];
}

export function unionBounds(objs) {
  let b = null;
  for (const o of objs) {
    const r = bounds(o);
    b = b ? [Math.min(b[0], r[0]), Math.min(b[1], r[1]), Math.max(b[2], r[2]), Math.max(b[3], r[3])] : r;
  }
  return b;
}

let measureCtx = null;
function textLines(obj) {
  measureCtx ??= document.createElement('canvas').getContext('2d');
  measureCtx.font = `${obj.size}px -apple-system, Helvetica, Arial, sans-serif`;
  const out = [];
  for (const para of obj.text.split('\n')) {
    let line = '';
    for (const word of para.split(' ')) {
      const trial = line ? `${line} ${word}` : word;
      if (line && measureCtx.measureText(trial).width > obj.w) {
        out.push(line);
        line = word;
      } else line = trial;
    }
    out.push(line);
  }
  return out;
}

// view: { s: css px per page unit, ox, oy: css px of the page's top-left }
export const toScreen = (view, x, y) => [view.ox + x * view.s, view.oy + y * view.s];
export const toPage = (view, x, y) => [(x - view.ox) / view.s, (y - view.oy) / view.s];

export class AssetCache {
  constructor(urlFor, onLoad) {
    this.urlFor = urlFor;
    this.onLoad = onLoad;
    this.images = new Map();
  }

  get(name) {
    let img = this.images.get(name);
    if (!img) {
      img = new Image();
      img.decoding = 'async';
      img.addEventListener('load', () => this.onLoad?.());
      img.src = this.urlFor(name);
      this.images.set(name, img);
    }
    return img.complete && img.naturalWidth ? img : null;
  }
}

function drawTemplate(ctx, page, view) {
  if (!page.template || page.template === 'blank') return;
  ctx.save();
  ctx.strokeStyle = TEMPLATE_COLOR;
  ctx.fillStyle = TEMPLATE_COLOR;
  ctx.lineWidth = Math.max(0.5, 1.2 * view.s);
  const [left, top] = toScreen(view, 0, 0);
  const [right, bottom] = toScreen(view, page.w, page.h);
  ctx.beginPath();
  if (page.template === 'ruled' || page.template === 'grid') {
    for (let y = page.template === 'ruled' ? GRID * 2 : GRID; y < page.h; y += GRID) {
      const sy = view.oy + y * view.s;
      ctx.moveTo(left, sy);
      ctx.lineTo(right, sy);
    }
  }
  if (page.template === 'grid') {
    for (let x = GRID; x < page.w; x += GRID) {
      const sx = view.ox + x * view.s;
      ctx.moveTo(sx, top);
      ctx.lineTo(sx, bottom);
    }
  }
  ctx.stroke();
  if (page.template === 'dot') {
    const r = Math.max(0.8, 1.8 * view.s);
    for (let y = GRID; y < page.h; y += GRID) {
      for (let x = GRID; x < page.w; x += GRID) {
        ctx.beginPath();
        ctx.arc(view.ox + x * view.s, view.oy + y * view.s, r, 0, Math.PI * 2);
        ctx.fill();
      }
    }
  }
  ctx.restore();
}

// One filled outline per stroke: a circle at every point plus a quad between neighbours, all wound the
// same way, filled once. No double-darkening at joints, and translucent tools blend exactly once.
export function strokePath(stroke, view) {
  const path = new Path2D();
  const pts = stroke.pts;
  const pos = (p) => [view.ox + p[0] * view.s, view.oy + p[1] * view.s];
  const rad = (p) => Math.max(strokeWidth(stroke.tool, stroke.size, p[2], p[3] || 0) * view.s, 0.7) / 2;
  let prev = null;
  for (const p of pts) {
    const [x, y] = pos(p);
    const r = rad(p);
    path.moveTo(x + r, y);
    path.arc(x, y, r, 0, Math.PI * 2, false);
    if (prev) {
      const [px, py, pr] = prev;
      const dx = x - px;
      const dy = y - py;
      const len = Math.hypot(dx, dy);
      if (len > 0.01) {
        const nx = -dy / len;
        const ny = dx / len;
        // same winding as the arcs (positive signed area in screen coordinates), so the union has no holes
        path.moveTo(px + nx * pr, py + ny * pr);
        path.lineTo(px - nx * pr, py - ny * pr);
        path.lineTo(x - nx * r, y - ny * r);
        path.lineTo(x + nx * r, y + ny * r);
        path.closePath();
      }
    }
    prev = [x, y, r];
  }
  return path;
}

export function traceStroke(ctx, stroke, view) {
  ctx.fillStyle = stroke.color;
  ctx.fill(strokePath(stroke, view), 'nonzero');
}

export function drawObj(ctx, obj, view, assets) {
  if (obj.type === 'stroke') {
    const alpha = strokeAlpha(obj);
    if (alpha >= 1) traceStroke(ctx, obj, view);
    else {
      ctx.save();
      ctx.globalAlpha = alpha;
      if (obj.tool === 'highlighter') ctx.globalCompositeOperation = 'multiply';
      traceStroke(ctx, obj, view);
      ctx.restore();
    }
  } else if (obj.type === 'image') {
    const img = assets?.get(obj.asset);
    const [x, y] = toScreen(view, obj.x, obj.y);
    if (img) ctx.drawImage(img, x, y, obj.w * view.s, obj.h * view.s);
    else {
      ctx.save();
      ctx.fillStyle = 'rgba(128,128,128,.15)';
      ctx.fillRect(x, y, obj.w * view.s, obj.h * view.s);
      ctx.restore();
    }
  } else if (obj.type === 'text') {
    ctx.save();
    ctx.fillStyle = obj.color;
    ctx.textBaseline = 'top';
    ctx.font = `${obj.size * view.s}px -apple-system, Helvetica, Arial, sans-serif`;
    textLines(obj).forEach((line, i) => {
      const [x, y] = toScreen(view, obj.x, obj.y + i * obj.size * 1.3);
      ctx.fillText(line, x, y);
    });
    ctx.restore();
  }
}

// Paper, picture behind, template and every object (except `hidden` ids).
export function drawPage(ctx, page, view, { assets, hidden, paper = true, highlight } = {}) {
  const [x, y] = toScreen(view, 0, 0);
  if (paper) {
    ctx.save();
    ctx.shadowColor = 'rgba(0,0,0,.18)';
    ctx.shadowBlur = 12;
    ctx.fillStyle = '#ffffff';
    ctx.fillRect(x, y, page.w * view.s, page.h * view.s);
    ctx.restore();
  }
  if (page.bg) {
    const img = assets?.get(page.bg.asset);
    if (img) ctx.drawImage(img, x, y, page.w * view.s, page.h * view.s);
  }
  drawTemplate(ctx, page, view);
  for (const id of page.order) {
    if (hidden?.has(id)) continue;
    const obj = page.objs[id];
    if (!obj) continue;
    if (highlight?.has(id)) {
      ctx.save();
      ctx.shadowColor = highlight.get(id);
      ctx.shadowBlur = 0;
      drawObj(ctx, { ...obj, color: highlight.get(id) }, view, assets);
      ctx.restore();
    } else drawObj(ctx, obj, view, assets);
  }
}

// ------------------------------------------------------------------ hit testing
function segDist2(px, py, ax, ay, bx, by) {
  const dx = bx - ax;
  const dy = by - ay;
  const len = dx * dx + dy * dy;
  let t = len ? ((px - ax) * dx + (py - ay) * dy) / len : 0;
  t = Math.max(0, Math.min(1, t));
  const x = ax + t * dx - px;
  const y = ay + t * dy - py;
  return x * x + y * y;
}

export function hitObj(obj, x, y, r) {
  if (obj.type === 'stroke') {
    const pts = obj.pts;
    const reach = r + strokeWidth(obj.tool, obj.size, 0.6) / 2;
    const r2 = reach * reach;
    if (pts.length === 1) return (pts[0][0] - x) ** 2 + (pts[0][1] - y) ** 2 <= r2;
    for (let i = 1; i < pts.length; i += 1) {
      if (segDist2(x, y, pts[i - 1][0], pts[i - 1][1], pts[i][0], pts[i][1]) <= r2) return true;
    }
    return false;
  }
  const [x0, y0, x1, y1] = bounds(obj);
  return x >= x0 - r && x <= x1 + r && y >= y0 - r && y <= y1 + r;
}

export function insidePolygon(poly, x, y) {
  let inside = false;
  for (let i = 0, j = poly.length - 1; i < poly.length; j = i, i += 1) {
    const [xi, yi] = poly[i];
    const [xj, yj] = poly[j];
    if ((yi > y) !== (yj > y) && x < ((xj - xi) * (y - yi)) / (yj - yi) + xi) inside = !inside;
  }
  return inside;
}

export function inLasso(obj, poly) {
  if (obj.type === 'stroke') {
    let n = 0;
    for (const p of obj.pts) if (insidePolygon(poly, p[0], p[1])) n += 1;
    return n / obj.pts.length >= 0.6;
  }
  const [x0, y0, x1, y1] = bounds(obj);
  return insidePolygon(poly, (x0 + x1) / 2, (y0 + y1) / 2);
}

// Split a stroke where the eraser circle touches it; returns the surviving pieces (point arrays).
export function splitStroke(stroke, x, y, r) {
  const r2 = (r + strokeWidth(stroke.tool, stroke.size, 0.5) / 2) ** 2;
  const pieces = [];
  let current = [];
  for (const p of stroke.pts) {
    if ((p[0] - x) ** 2 + (p[1] - y) ** 2 <= r2) {
      if (current.length) pieces.push(current);
      current = [];
    } else current.push(p);
  }
  if (current.length) pieces.push(current);
  return pieces.filter((piece) => piece.length >= 2 || stroke.pts.length === 1);
}
