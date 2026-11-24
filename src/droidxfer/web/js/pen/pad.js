// The phone side: a full-screen drawing pad for the S Pen. Fingers pan/zoom, only the pen draws.

import { InkCanvas } from './canvas.js';
import { el, put, svg, toast } from './icons.js';
import { applyOp, COLORS, newId } from './model.js';
import { PenSocket } from './net.js';
import { unionBounds } from './render.js';

const $app = document.getElementById('app');
const sock = new PenSocket('/api/pen/pad/ws', { persistKey: 'droidxfer.pad.outbox' });

function pref(key, fallback) {
  try {
    const v = localStorage.getItem(`droidxfer.pad.${key}`);
    return v === null ? fallback : JSON.parse(v);
  } catch {
    return fallback;
  }
}
function setPref(key, value) {
  try {
    localStorage.setItem(`droidxfer.pad.${key}`, JSON.stringify(value));
  } catch {
    /* ignore */
  }
}

const sizes = pref('sizes', { pen: 3, pencil: 4, highlighter: 8 });
const prefs = { tool: pref('tool', 'pen'), color: pref('color', '#1d1d1f'), eraser: pref('eraser', 'stroke'), snap: pref('snap', true) };

let state = null; // {notebook, pages, seq, can_undo, can_redo}
let connected = false;
let waiting = 0;
let pendingPage = null;
let selection = { ids: [], rect: null };

// ------------------------------------------------------------------ layout
const toolButtons = {};
const toolBtn = (tool, title) => {
  const b = el('button', { class: 'icon-btn', type: 'button', title, 'aria-label': title, onclick: () => chooseTool(tool) }, svg(tool));
  toolButtons[tool] = b;
  return b;
};
const colorDot = el('span', { class: 'color-btn' });
const undoBtn = el('button', { class: 'icon-btn', type: 'button', title: 'Undo', onclick: () => sock.send({ t: 'undo' }) }, svg('undo'));
const redoBtn = el('button', { class: 'icon-btn', type: 'button', title: 'Redo', onclick: () => sock.send({ t: 'redo' }) }, svg('redo'));
const bar = el('div', { class: 'pad-bar' },
  el('button', { class: 'icon-btn', type: 'button', title: 'Menu', onclick: () => openMenu() }, svg('menu')),
  el('span', { class: 'sep' }),
  toolBtn('pen', 'Pen'), toolBtn('pencil', 'Pencil'), toolBtn('highlighter', 'Highlighter'),
  toolBtn('eraser', 'Eraser (or click the S Pen button while hovering)'), toolBtn('lasso', 'Lasso: select, move, copy, convert'),
  toolBtn('laser', 'Laser pointer'),
  el('span', { class: 'sep' }),
  el('button', { class: 'icon-btn', type: 'button', title: 'Colour and size', onclick: () => openPalette() }, colorDot));

const pageNo = el('span', { class: 'page-no' }, '–');
const statusDot = el('span', { class: 'dot wait' });
const statusText = el('span', {}, 'Connecting…');
const bottom = el('div', { class: 'pad-bottom' },
  el('button', { class: 'icon-btn', type: 'button', title: 'Previous page', onclick: () => stepPage(-1) }, svg('prev')),
  pageNo,
  el('button', { class: 'icon-btn', type: 'button', title: 'Next page', onclick: () => stepPage(1) }, svg('next')),
  el('button', { class: 'icon-btn', type: 'button', title: 'New page after this one', onclick: () => addPage() }, svg('plus')),
  undoBtn, redoBtn,
  el('span', { class: 'status' }, statusDot, statusText));

const host = el('div', { class: 'ink-host' });
const selbar = el('div', { class: 'selbar', hidden: true });
host.append(selbar);
$app.append(bar, host, bottom);

const ink = new InkCanvas(host, {
  role: 'pad',
  send: (msg) => sock.send(msg),
  commit: (op) => sock.sendOp(op),
  assetUrl: (name) => `/api/pen/pad/asset/${encodeURIComponent(state?.notebook.id || '')}/${encodeURIComponent(name)}`,
  onSelection: (ids, rect) => showSelection(ids, rect),
  onViewChange: () => selection.ids.length && placeSelbar(),
});

// ------------------------------------------------------------------ tools
let beforeEraser = null;
ink.onSideClick = () => {
  if (prefs.tool === 'eraser') {
    prefs.tool = beforeEraser || 'pen';
    toast(`S Pen button: back to ${prefs.tool}`, 'info', 1200);
  } else {
    beforeEraser = prefs.tool;
    prefs.tool = 'eraser';
    toast('S Pen button: eraser (click again to switch back)', 'info', 1500);
  }
  setPref('tool', prefs.tool);
  applyTool();
};

function chooseTool(tool) {
  if (tool === 'eraser' && prefs.tool === 'eraser') {
    prefs.eraser = prefs.eraser === 'stroke' ? 'partial' : 'stroke';
    setPref('eraser', prefs.eraser);
    toast(prefs.eraser === 'stroke' ? 'Eraser: whole strokes' : 'Eraser: only what you touch');
  }
  prefs.tool = tool;
  setPref('tool', tool);
  applyTool();
}

function applyTool() {
  for (const [tool, b] of Object.entries(toolButtons)) b.classList.toggle('on', tool === prefs.tool);
  colorDot.style.background = prefs.color;
  const size = sizes[prefs.tool] ?? sizes.pen;
  ink.setTool({ tool: prefs.tool, color: prefs.color, size, eraser: prefs.eraser, snap: prefs.snap });
  sock.send({ t: 'tool', tool: prefs.tool, color: prefs.color, size });
}

// ------------------------------------------------------------------ sheets
let openSheet = null;
function closeSheet() {
  openSheet?.forEach((n) => n.remove());
  openSheet = null;
}
function sheet(title, ...children) {
  closeSheet();
  const back = el('div', { class: 'sheet-back', onclick: closeSheet });
  const box = el('div', { class: 'sheet pad-sheet', role: 'dialog' },
    el('div', { class: 'row' }, el('h3', {}, title), el('span', { style: { flex: '1' } }),
      el('button', { class: 'icon-btn', type: 'button', title: 'Close', onclick: closeSheet }, svg('close'))),
    ...children);
  document.body.append(back, box);
  openSheet = [back, box];
  return box;
}

function openPalette() {
  const sw = COLORS.map((c) => el('button', {
    class: `swatch${c === prefs.color ? ' on' : ''}`, type: 'button', 'aria-label': c, style: { background: c },
    onclick: () => {
      prefs.color = c;
      setPref('color', c);
      if (['eraser', 'lasso', 'laser'].includes(prefs.tool)) prefs.tool = 'pen';
      applyTool();
      closeSheet();
    },
  }));
  const tool = ['pen', 'pencil', 'highlighter'].includes(prefs.tool) ? prefs.tool : 'pen';
  const sizeLabel = el('span', { class: 'muted' }, `${sizes[tool]}`);
  const range = el('input', { type: 'range', min: '0.5', max: '24', step: '0.5', value: String(sizes[tool]) });
  range.addEventListener('input', () => {
    sizes[tool] = Number(range.value);
    sizeLabel.textContent = range.value;
    setPref('sizes', sizes);
    applyTool();
  });
  const seg = (options, current, onPick) => el('div', { class: 'seg' }, ...options.map(([value, label]) => el('button', {
    type: 'button', class: value === current ? 'on' : '', onclick: (e) => {
      onPick(value);
      for (const b of e.target.parentElement.children) b.classList.toggle('on', b === e.target);
    },
  }, label)));
  sheet('Colour and size',
    el('div', { class: 'swatches' }, sw),
    el('h4', {}, `${tool[0].toUpperCase()}${tool.slice(1)} size`), el('div', { class: 'row' }, range, sizeLabel),
    el('h4', {}, 'Eraser'),
    seg([['stroke', 'Whole strokes'], ['partial', 'Only what I touch']], prefs.eraser, (v) => { prefs.eraser = v; setPref('eraser', v); applyTool(); }),
    el('h4', {}, 'Shapes'),
    seg([[true, 'Straighten when I hold still'], [false, 'Off']], prefs.snap, (v) => { prefs.snap = v; setPref('snap', v); applyTool(); }),
    el('p', { class: 'hint' }, 'Hold the S Pen still at the end of a stroke to turn it into a straight line, rectangle or ellipse. Click the S Pen button while hovering to switch to the eraser and back. If that opens Samsung\u2019s Air command menu instead, turn Air command off in Settings › Advanced features › S Pen.'));
}

const page = () => ink.page;
const pageIndex = () => (state ? state.pages.findIndex((p) => p.id === ink.pageId) : -1);

async function act(name, args = {}, { timeout = 60000, ok } = {}) {
  try {
    const result = await sock.action(name, args, timeout);
    if (ok) toast(typeof ok === 'function' ? ok(result) : ok, 'success');
    return result;
  } catch (err) {
    toast(err.message, 'error', 6000);
    return null;
  }
}

function menuItem(icon, label, onclick, sub = '') {
  return el('button', { class: 'menu-item', type: 'button', onclick: () => { closeSheet(); onclick(); } },
    svg(icon), el('span', {}, label), sub ? el('span', { class: 'sub' }, sub) : null);
}

function openMenu() {
  if (!state) return;
  const p = page();
  sheet(state.notebook.name,
    el('div', { class: 'menu-list' },
      menuItem('history', 'Switch notebook…', openNotebooks),
      menuItem('plus', 'New notebook…', newNotebook),
      menuItem('save', 'Save this version…', saveVersion),
      el('h4', {}, 'This page'),
      menuItem('grid', 'Paper', openPaper, p?.template || ''),
      menuItem('copy', 'Copy page to the Mac clipboard', () => act('clipboard', { page: p.id }, { ok: 'Copied. Paste it on the Mac with ⌘V.' })),
      menuItem('text', 'Turn handwriting into text', () => toText(null)),
      menuItem('send', 'Save page to this phone’s gallery', () => act('send_to_phone', { page: p.id }, { ok: 'Saved to Pictures › droidxfer Notes' })),
      menuItem('save', 'Save annotated copy next to the original', () => act('save_copy', { page: p.id }, { ok: (r) => (r.where === 'mac' ? 'Saved on the Mac' : 'Saved on the phone') })),
      menuItem('trash', 'Delete page', deletePage),
      el('h4', {}, 'From the Mac'),
      menuItem('camera', 'Annotate a Mac screenshot', screenshot),
      menuItem('sign', 'Insert signature or sticker…', openStickers),
      el('h4', {}, 'More'),
      menuItem('tablet', 'Use the S Pen as a Mac tablet', startTablet),
      menuItem('expand', 'Full screen', () => document.documentElement.requestFullscreen?.({ navigationUI: 'hide' }).catch(() => {})),
    ));
}

async function openNotebooks() {
  const list = await act('list_notebooks');
  if (!list) return;
  sheet('Notebooks', el('div', { class: 'menu-list' }, list.map((nb) => el('button', {
    class: `menu-item${nb.id === state.notebook.id ? ' on' : ''}`, type: 'button',
    onclick: () => { closeSheet(); act('open', { notebook: nb.id }); },
  }, svg('history'), el('span', {}, nb.name), el('span', { class: 'sub' }, `${nb.pages} page${nb.pages === 1 ? '' : 's'}`)))));
}

function askText(title, label, value = '') {
  return new Promise((resolve) => {
    const input = el('input', { class: 'input', value, autocomplete: 'off' });
    const form = el('form', {}, el('label', { class: 'field' }, el('span', {}, label), input),
      el('div', { class: 'row' }, el('span', { style: { flex: '1' } }),
        el('button', { class: 'btn', type: 'button', onclick: () => { closeSheet(); resolve(null); } }, 'Cancel'),
        el('button', { class: 'btn primary', type: 'submit' }, 'OK')));
    form.addEventListener('submit', (e) => {
      e.preventDefault();
      closeSheet();
      resolve(input.value.trim() || null);
    });
    sheet(title, form);
    setTimeout(() => input.focus(), 50);
  });
}

async function newNotebook() {
  const name = await askText('New notebook', 'Name');
  if (name) act('new_notebook', { name, template: page()?.template || 'blank' });
}

async function saveVersion() {
  const name = await askText('Save this version', 'Name it so you can find it in the history', new Date().toLocaleString());
  if (name) act('checkpoint', { name }, { ok: 'Version saved' });
}

function openPaper() {
  const p = page();
  sheet('Paper', el('div', { class: 'menu-list' }, [['blank', 'Blank'], ['ruled', 'Ruled'], ['grid', 'Grid'], ['dot', 'Dots']].map(([v, label]) =>
    el('button', { class: `menu-item${p.template === v ? ' on' : ''}`, type: 'button', onclick: () => {
      closeSheet();
      sock.sendOp({ op: 'page_update', id: p.id, template: v });
    } }, label))));
}

function deletePage() {
  const old = page();
  if (!old) return;
  if (state.pages.length <= 1) {
    // The only page: ask which paper the new empty page gets, then swap them in one undoable change.
    sheet('Delete the last page?',
      el('p', { class: 'hint' }, 'This is the only page, so a new empty page takes its place. Choose its paper. Undo brings the deleted page back.'),
      el('div', { class: 'menu-list' }, [['blank', 'Blank'], ['ruled', 'Ruled'], ['grid', 'Grid'], ['dot', 'Dots']].map(([v, label]) =>
        el('button', { class: `menu-item${old.template === v ? ' on' : ''}`, type: 'button', onclick: () => {
          closeSheet();
          if (!state.pages.some((p) => p.id === old.id)) return;
          const fresh = { id: newId(), w: old.bg ? 1000 : old.w, h: old.bg ? 1414 : old.h, template: v, bg: null, objs: {}, order: [] };
          pendingPage = fresh.id;
          sock.sendOp({ op: 'batch', ops: [{ op: 'page_add', page: fresh, index: 1 }, { op: 'page_remove', id: old.id }] });
          toast('Page deleted. Undo brings it back.');
        } }, label))));
    return;
  }
  sock.sendOp({ op: 'page_remove', id: old.id });
  toast('Page deleted. Undo brings it back.');
}

async function screenshot() {
  toast('On the Mac: drag a box around what you want to annotate (Esc cancels).', 'info', 6000);
  await act('screenshot', {}, { timeout: 200000 });
}

async function openStickers() {
  const list = await act('sticker_list');
  if (!list) return;
  const body = list.length
    ? el('div', { class: 'menu-list' }, list.map((s) => el('button', {
      class: 'menu-item', type: 'button', onclick: () => { closeSheet(); act('sticker_insert', { name: s.name, page: page().id }); },
    }, el('img', { src: `/api/pen/pad/sticker/${encodeURIComponent(s.name)}.png?v=${s.mtime}`, alt: '', style: { height: '36px', maxWidth: '120px', background: '#fff', borderRadius: '6px' } }), el('span', {}, s.name))))
    : el('p', { class: 'hint' }, 'Nothing saved yet. Write your signature, select it with the lasso and choose "Save as signature".');
  sheet('Signatures & stickers', body);
}

// ------------------------------------------------------------------ handwriting
async function toText(ids) {
  const p = page();
  toast('Reading your handwriting…', 'info', 2500);
  const result = await act('to_text', { page: p.id, ids }, { timeout: 300000 });
  if (!result) return;
  if (!result.text) {
    toast('No text found.');
    return;
  }
  const engine = result.engine?.startsWith('gemma') ? `Gemma (${result.engine.slice(6)})` : 'Apple Vision';
  const conf = result.confidence != null ? ` · ${Math.round(result.confidence * 100)}% sure` : '';
  const area = unionBounds((ids || p.order).map((id) => p.objs[id]).filter((o) => o?.type === 'stroke')) || [40, 40, 400, 100];
  const textObj = { id: newId(), type: 'text', text: result.text, color: '#1d1d1f', size: 28, x: area[0], w: Math.max(200, area[2] - area[0]) };
  sheet('Text',
    el('div', { class: 'result-text' }, result.text),
    el('p', { class: 'hint' }, `${engine}${conf} · ${(result.ms / 1000).toFixed(1)} s`),
    el('div', { class: 'row' },
      el('button', { class: 'btn', type: 'button', onclick: () => { closeSheet(); act('copy_text', { text: result.text }, { ok: 'Copied on the Mac' }); } }, 'Copy on the Mac'),
      el('button', { class: 'btn', type: 'button', onclick: () => {
        closeSheet();
        sock.sendOp({ op: 'add', page: p.id, objs: [{ ...textObj, y: area[3] + 10 }] });
      } }, 'Insert below'),
      el('button', { class: 'btn primary', type: 'button', onclick: () => {
        closeSheet();
        const strokes = (ids || p.order).filter((id) => p.objs[id]?.type === 'stroke');
        sock.sendOp({ op: 'batch', ops: [{ op: 'remove', page: p.id, ids: strokes }, { op: 'add', page: p.id, objs: [{ ...textObj, y: area[1] }] }] });
      } }, 'Replace handwriting')));
}

// ------------------------------------------------------------------ selection bar
function placeSelbar() {
  const r = selection.rect;
  if (!r) return;
  const w = selbar.offsetWidth || 300;
  const hostW = host.clientWidth;
  let x = r.x + r.w / 2 - w / 2;
  x = Math.max(6, Math.min(hostW - w - 6, x));
  let y = r.y - 58;
  if (y < 6) y = Math.min(host.clientHeight - 54, r.y + r.h + 12);
  selbar.style.left = `${x}px`;
  selbar.style.top = `${y}px`;
}

function showSelection(ids, rect) {
  selection = { ids, rect };
  if (!ids.length) {
    selbar.hidden = true;
    return;
  }
  const p = page();
  const b = (icon, title, fn) => el('button', { class: 'icon-btn', type: 'button', title, 'aria-label': title, onclick: fn }, svg(icon));
  put(selbar, 
    b('trash', 'Delete', () => { sock.sendOp({ op: 'remove', page: p.id, ids }); ink.clearSelection(); }),
    b('palette', `Make it ${prefs.color}`, () => {
      const changes = {};
      for (const id of ids) if (p.objs[id] && p.objs[id].type !== 'image') changes[id] = { color: prefs.color };
      if (Object.keys(changes).length) sock.sendOp({ op: 'update', page: p.id, changes });
    }),
    b('duplicate', 'Duplicate', () => {
      const copies = ids.map((id) => structuredClone(p.objs[id])).filter(Boolean).map((o) => {
        o.id = newId();
        if (o.type === 'stroke') o.pts = o.pts.map((pt) => [pt[0] + 24, pt[1] + 24, ...pt.slice(2)]);
        else { o.x += 24; o.y += 24; }
        return o;
      });
      sock.sendOp({ op: 'add', page: p.id, objs: copies });
      ink.selectIds(copies.map((o) => o.id));
    }),
    b('copy', 'Copy to the Mac clipboard', () => act('clipboard', { page: p.id, ids }, { ok: 'Copied. Paste on the Mac with ⌘V.' })),
    b('text', 'Turn into text', () => toText(ids)),
    b('sign', 'Save as signature / sticker', async () => {
      const name = await askText('Save as signature', 'Name', 'Signature');
      if (name) act('sticker_save', { name, page: p.id, ids }, { ok: `Saved “${name}”` });
    }),
  );
  selbar.hidden = false;
  placeSelbar();
}

// ------------------------------------------------------------------ pages
function updatePageUi() {
  if (!state) return;
  const i = pageIndex();
  pageNo.textContent = `${i + 1} / ${state.pages.length}`;
  undoBtn.disabled = !state.can_undo;
  redoBtn.disabled = !state.can_redo;
}

function goToPage(id, tell = true) {
  if (!state || !state.pages.some((p) => p.id === id)) return;
  ink.setPage(id);
  if (tell) sock.send({ t: 'page', page: id });
  updatePageUi();
}

function stepPage(delta) {
  const i = pageIndex();
  const next = state?.pages[i + delta];
  if (next) goToPage(next.id);
}

function addPage() {
  const cur = page();
  const fresh = { id: newId(), w: cur && !cur.bg ? cur.w : 1000, h: cur && !cur.bg ? cur.h : 1414, template: cur?.template || 'blank', bg: null, objs: {}, order: [] };
  pendingPage = fresh.id;
  sock.sendOp({ op: 'page_add', page: fresh, index: pageIndex() + 1 });
}

// ------------------------------------------------------------------ status
function renderStatus() {
  statusDot.className = `dot ${connected ? (waiting ? 'wait' : 'on') : 'off'}`;
  if (!connected) statusText.textContent = waiting ? `Offline · ${waiting} change${waiting === 1 ? '' : 's'} kept` : 'Reconnecting…';
  else statusText.textContent = waiting ? `Saving ${waiting}…` : (state ? state.notebook.name : 'Connected');
}

// ------------------------------------------------------------------ socket events
sock.on('connection', (on) => {
  connected = on;
  renderStatus();
});
sock.on('outbox', (n) => {
  waiting = n;
  renderStatus();
});
sock.on('state', (msg) => {
  const first = !state || state.notebook.id !== msg.notebook.id;
  state = msg;
  ink.setState({ pages: msg.pages }, msg.current.page);
  if (first) ink.fitted = false;
  ink.invalidate();
  updatePageUi();
  renderStatus();
  applyTool();
});
sock.on('applied', (msg) => {
  if (!state || msg.notebook !== state.notebook.id) return;
  const touched = applyOp(ink.state, msg.entry.op);
  state.pages = ink.state.pages;
  state.can_undo = msg.can_undo;
  state.can_redo = msg.can_redo;
  ink.changed(msg.entry.op, touched);
  if (pendingPage && state.pages.some((p) => p.id === pendingPage)) {
    goToPage(pendingPage);
    pendingPage = null;
  } else if (!state.pages.some((p) => p.id === ink.pageId)) {
    goToPage(msg.current?.page || state.pages[0].id, false);
  }
  if (msg.entry.kind === 'undo' || msg.entry.kind === 'redo') toast(msg.entry.label, 'info', 1600);
  updatePageUi();
});
sock.on('page', (msg) => goToPage(msg.page, false));
for (const t of ['live', 'hover', 'hover_end', 'laser']) sock.on(t, (msg) => ink.remoteMessage(msg));
sock.on('rejected', (msg) => {
  if (!/already exists/.test(msg.error)) toast(`Not saved: ${msg.error}`, 'error');
});
sock.on('notice', (msg) => toast(msg.text));
sock.on('tablet', (msg) => (msg.info.on && !tablet.node ? tablet.open(msg.info) : tablet.update(msg.info)));
sock.on('bye', (msg) => cover('Pen pad closed', msg.reason, false));
sock.on('expired', () => cover('This pad link has expired', 'On the Mac, open droidxfer › S Pen and press "Open pad on phone" again.', false));

// ------------------------------------------------------------------ start screen, wake lock
let wakeLock = null;
async function keepAwake() {
  try {
    wakeLock = await navigator.wakeLock?.request('screen');
  } catch {
    wakeLock = null;
  }
}
document.addEventListener('visibilitychange', () => {
  if (document.visibilityState === 'visible') keepAwake();
});

function cover(title, text, startButton) {
  const node = el('div', { class: 'cover' }, el('div', {},
    el('h2', {}, title), el('p', {}, text),
    startButton ? el('button', { class: 'btn primary', type: 'button', onclick: () => {
      document.documentElement.requestFullscreen?.({ navigationUI: 'hide' }).catch(() => {});
      keepAwake();
      node.remove();
    } }, 'Start drawing') : null));
  document.body.append(node);
}

cover('S Pen pad', 'Draw with the S Pen; what you draw appears on the Mac. Use one finger to scroll and two to zoom. Double-tap with a finger to fit the page.', true);

// ------------------------------------------------------------------ tablet mode
const tablet = {
  node: null,
  info: null,
  queue: [],
  timer: null,
  update(info) {
    this.info = info;
    if (!this.node) return;
    if (!info.on) {
      this.close(false);
      return;
    }
    this.layout();
  },
  layout() {
    const info = this.info;
    if (!this.node || !info) return;
    const rect = info.rect || { w: 16, h: 10, label: 'Mac' };
    this.label.textContent = `Drawing on: ${rect.label}${info.front && info.target?.kind !== 'window' ? ` · ${info.front.app}` : ''}`;
    const area = this.area.getBoundingClientRect();
    const k = Math.min((area.width - 16) / rect.w, (area.height - 16) / rect.h);
    Object.assign(this.rect.style, { width: `${rect.w * k}px`, height: `${rect.h * k}px` });
    const keys = info.shortcuts?.[info.profile] || info.shortcuts?.['Any app'] || [];
    if (this.profile !== info.profile || this.keys.childElementCount === 0) {
      this.profile = info.profile;
      put(this.keys, el('span', { class: 'muted', style: { alignSelf: 'center', color: '#888' } }, info.profile),
        ...keys.map(([label, combo]) => el('button', { type: 'button', onclick: () => act('shortcut', { keys: combo }) }, label)));
    }
  },
  push(e, k) {
    const r = this.rect.getBoundingClientRect();
    const x = (e.clientX - r.left) / r.width;
    const y = (e.clientY - r.top) / r.height;
    const side = e.pointerType === 'pen' && ((e.buttons & 2) || (e.buttons & 32));
    this.queue.push({ k, x: Math.round(x * 10000) / 10000, y: Math.round(y * 10000) / 10000,
      p: e.pointerType === 'mouse' ? (e.buttons ? 0.5 : 0) : e.pressure, tx: (e.tiltX || 0) / 90, ty: (e.tiltY || 0) / 90, side: !!side });
    this.cursor.style.left = `${x * r.width}px`;
    this.cursor.style.top = `${y * r.height}px`;
    this.cursor.hidden = k === 'leave';
    this.cursor.classList.toggle('down', k === 'down' || (k === 'move' && this.down));
    if (!this.timer) this.timer = requestAnimationFrame(() => this.flush());
  },
  flush() {
    this.timer = null;
    if (this.queue.length) sock.send({ t: 'tablet', events: this.queue });
    this.queue = [];
  },
  open(info) {
    this.info = info;
    this.label = el('span', { class: 'label' });
    this.cursor = el('div', { class: 'cursor', hidden: true });
    this.rect = el('div', { class: 'tablet-rect' }, this.cursor);
    this.area = el('div', { class: 'tablet-area' }, this.rect);
    this.keys = el('div', { class: 'tablet-keys' });
    const targets = [...(info.displays || []).map((d) => ({ kind: 'display', id: d.id, label: d.name })), { kind: 'window', label: 'Front window' }];
    const targetBtn = el('button', { class: 'btn small', type: 'button', onclick: async () => {
      const cur = targets.findIndex((t) => t.kind === this.info.target?.kind && (t.kind === 'window' || t.id === this.info.target?.id));
      const next = targets[(cur + 1) % targets.length];
      const res = await act('tablet', { on: true, target: { kind: next.kind, id: next.id ?? null } });
      if (res) this.update(res);
    } }, 'Change area');
    this.node = el('div', { class: 'tablet-mode' },
      el('div', { class: 'tablet-head' },
        el('button', { class: 'icon-btn', type: 'button', title: 'Back to notes', onclick: () => this.close(true) }, svg('close')),
        this.label, el('span', { style: { flex: '1' } }), targetBtn),
      this.area, this.keys);
    const r = this.rect;
    r.addEventListener('pointerdown', (e) => {
      if (e.pointerType === 'touch') return;
      r.setPointerCapture(e.pointerId);
      this.down = true;
      this.push(e, 'down');
    });
    r.addEventListener('pointermove', (e) => {
      if (e.pointerType === 'touch') return;
      const events = this.down && e.getCoalescedEvents ? e.getCoalescedEvents() : [e];
      for (const ev of events.length ? events : [e]) this.push(ev, this.down ? 'move' : 'hover');
    });
    const up = (e) => {
      if (e.pointerType === 'touch' || !this.down) return;
      this.down = false;
      this.push(e, 'up');
    };
    r.addEventListener('pointerup', up);
    r.addEventListener('pointercancel', up);
    r.addEventListener('pointerleave', (e) => {
      if (e.pointerType !== 'touch' && !this.down) this.push(e, 'leave');
    });
    r.addEventListener('contextmenu', (e) => e.preventDefault());
    document.body.append(this.node);
    this.layout();
    this.resize = () => this.layout();
    window.addEventListener('resize', this.resize);
    this.poll = setInterval(async () => {
      try {
        this.update(await sock.action('tablet_info'));
      } catch {
        /* offline */
      }
    }, 2500);
  },
  close(tell) {
    if (!this.node) return;
    clearInterval(this.poll);
    window.removeEventListener('resize', this.resize);
    this.node.remove();
    this.node = null;
    this.keys = null;
    if (tell) act('tablet', { on: false });
  },
};

async function startTablet() {
  const info = await act('tablet', { on: true }, { timeout: 15000 });
  if (info) tablet.open(info);
}

window.addEventListener('resize', () => tablet.node && tablet.layout());
applyTool();
renderStatus();
