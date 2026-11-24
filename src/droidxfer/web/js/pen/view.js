// The Mac side: watch and draw on the notebook live, browse every version, convert handwriting,
// and control tablet mode.

import { InkCanvas } from './canvas.js';
import { el, put, svg, toast } from './icons.js';
import { applyOp, newId } from './model.js';
import { PenSocket } from './net.js';
import { drawPage, unionBounds } from './render.js';

const $ = (id) => document.getElementById(id);
const top = $('top');
const pagesNav = $('pages');
const stage = $('stage');
const side = $('side');
const main = $('main');

async function api(method, url, body, raw) {
  const opts = { method, credentials: 'same-origin', headers: {} };
  if (raw) {
    opts.body = raw;
  } else if (body !== undefined) {
    opts.headers['Content-Type'] = 'application/json';
    opts.body = JSON.stringify(body);
  }
  const res = await fetch(url, opts);
  if (!res.ok) {
    let detail = res.statusText;
    try {
      detail = (await res.json()).detail ?? detail;
    } catch {
      /* not json */
    }
    if (typeof detail !== 'string') detail = JSON.stringify(detail);
    const err = new Error(detail);
    err.status = res.status;
    throw err;
  }
  return res.json();
}
const get = (url) => api('GET', url);
const post = (url, body = {}) => api('POST', url, body);
const qs = (o) => new URLSearchParams(Object.entries(o).filter(([, v]) => v !== undefined && v !== null)).toString();
const nbUrl = (suffix = '') => `/api/pen/notebooks/${encodeURIComponent(state.notebook.id)}${suffix}`;
const fmtTime = (t) => new Date(t * 1000).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
const fmtDay = (t) => {
  const d = new Date(t * 1000);
  const today = new Date();
  const yesterday = new Date(Date.now() - 86400000);
  if (d.toDateString() === today.toDateString()) return 'Today';
  if (d.toDateString() === yesterday.toDateString()) return 'Yesterday';
  return d.toLocaleDateString([], { weekday: 'short', day: 'numeric', month: 'short', year: d.getFullYear() === today.getFullYear() ? undefined : 'numeric' });
};

function pref(key, fallback) {
  try {
    const v = localStorage.getItem(`droidxfer.pen.${key}`);
    return v === null ? fallback : JSON.parse(v);
  } catch {
    return fallback;
  }
}
function setPref(key, value) {
  try {
    localStorage.setItem(`droidxfer.pen.${key}`, JSON.stringify(value));
  } catch {
    /* ignore */
  }
}

const sock = new PenSocket('/api/pen/ws');
let state = null; // {notebook, pages, seq, can_undo, can_redo, current, status}
let status = { phone: null, pad_online: false };
let notebooks = [];
let devices = [];
let settings = null;
let selection = [];
let preview = null; // {seq, entry, state, ink, mode}
const prefs = { tool: pref('tool', 'pen'), color: pref('color', '#1d1d1f'), size: pref('size', 3), eraser: pref('eraser', 'stroke') };

// ------------------------------------------------------------------ live canvas
const liveHost = el('div', { class: 'ink-host' });
const selbar = el('div', { class: 'selbar', hidden: true });
liveHost.append(selbar);
stage.append(liveHost);
const assetUrl = (name) => nbUrl(`/asset/${encodeURIComponent(name)}`);
const ink = new InkCanvas(liveHost, {
  role: 'mac',
  send: (msg) => sock.send(msg),
  commit: (op) => sock.sendOp(op),
  assetUrl,
  onSelection: (ids, rect) => showSelection(ids, rect),
  onViewChange: () => selection.length && placeSelbar(),
});

// ------------------------------------------------------------------ top bar
const nbSelect = el('select', { title: 'Notebook', onchange: () => sock.action('open', { notebook: nbSelect.value }).catch((e) => toast(e.message, 'error')) });
const deviceSelect = el('select', { title: 'Phone' });
const phoneDot = el('span', { class: 'dot off' });
const phoneText = el('span', { class: 'muted' }, 'Phone not connected');
const connectBtn = el('button', { class: 'btn small primary', type: 'button', onclick: () => connectPhone() }, 'Open pad on phone');
const disconnectBtn = el('button', { class: 'btn small', type: 'button', hidden: true, onclick: () => disconnectPhone() }, 'Close pad');
const toolButtons = {};
const toolBtn = (tool, title, key) => {
  const b = el('button', { class: 'icon-btn', type: 'button', title: `${title} (${key})`, onclick: () => chooseTool(tool) }, svg(tool, 20));
  toolButtons[tool] = b;
  return b;
};
const colorInput = el('input', { type: 'color', value: prefs.color, title: 'Colour', style: { width: '34px', height: '30px', border: '0', background: 'none', padding: '0' } });
colorInput.addEventListener('input', () => {
  prefs.color = colorInput.value;
  setPref('color', prefs.color);
  if (['eraser', 'lasso', 'laser', 'pan'].includes(prefs.tool)) prefs.tool = 'pen';
  applyTool();
});
const sizeInput = el('input', { type: 'range', min: '0.5', max: '24', step: '0.5', value: String(prefs.size), title: 'Size', style: { width: '90px' } });
sizeInput.addEventListener('input', () => {
  prefs.size = Number(sizeInput.value);
  setPref('size', prefs.size);
  applyTool();
});
const undoBtn = el('button', { class: 'icon-btn', type: 'button', title: 'Undo (⌘Z)', onclick: () => sock.send({ t: 'undo' }) }, svg('undo', 20));
const redoBtn = el('button', { class: 'icon-btn', type: 'button', title: 'Redo (⇧⌘Z)', onclick: () => sock.send({ t: 'redo' }) }, svg('redo', 20));
const fileInput = el('input', { type: 'file', accept: 'image/*', hidden: true });
fileInput.addEventListener('change', () => {
  const file = fileInput.files[0];
  if (file) uploadPicture(file, fileInput.dataset.mode || 'insert');
  fileInput.value = '';
});
const padStatus = el('span', { class: 'muted', title: 'What the phone is using' });

top.append(
  el('div', { class: 'brand' }, 'droidxfer ', el('span', {}, 'S Pen')),
  nbSelect,
  el('button', { class: 'icon-btn', type: 'button', title: 'Notebook menu', onclick: (e) => notebookMenu(e.currentTarget) }, svg('menu', 20)),
  el('span', { class: 'sep' }),
  el('div', { class: 'group' }, toolBtn('pen', 'Pen', 'P'), toolBtn('pencil', 'Pencil', 'N'), toolBtn('highlighter', 'Highlighter', 'H'),
    toolBtn('eraser', 'Eraser; click again to switch whole strokes / partial', 'E'), toolBtn('lasso', 'Lasso', 'L'), toolBtn('laser', 'Laser pointer', 'K'),
    toolBtn('pan', 'Move around (or hold Space)', 'Space'), colorInput, sizeInput),
  el('span', { class: 'sep' }),
  undoBtn, redoBtn,
  el('span', { class: 'sep' }),
  el('button', { class: 'icon-btn', type: 'button', title: 'Annotate a Mac screenshot', onclick: () => screenshot() }, svg('camera', 20)),
  el('button', { class: 'icon-btn', type: 'button', title: 'Insert a picture (or drop / paste one; Shift-drop opens it as a new page)', onclick: () => { fileInput.dataset.mode = 'insert'; fileInput.click(); } }, svg('image', 20)),
  el('button', { class: 'icon-btn', type: 'button', title: 'Export', onclick: (e) => exportMenu(e.currentTarget) }, svg('save', 20)),
  el('button', { class: 'icon-btn', type: 'button', title: 'Present full screen (laser pointer)', onclick: () => present() }, svg('present', 20)),
  fileInput,
  el('div', { class: 'spacer' }),
  padStatus,
  el('div', { class: 'phone-box' }, svg('phone', 18), phoneDot, phoneText, deviceSelect, connectBtn, disconnectBtn),
  el('button', { class: 'icon-btn', type: 'button', title: 'Show or hide the side panel', onclick: () => { main.classList.toggle('no-side'); side.hidden = main.classList.contains('no-side'); } }, svg('history', 20)),
);

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
  colorInput.value = prefs.color;
  ink.setTool({ tool: prefs.tool, color: prefs.color, size: prefs.size, eraser: prefs.eraser, snap: true });
  liveHost.querySelector('.ink-overlay').style.cursor = prefs.tool === 'pan' ? 'grab' : 'crosshair';
}

// ------------------------------------------------------------------ popup menus
let popup = null;
function closePopup() {
  popup?.remove();
  popup = null;
}
document.addEventListener('pointerdown', (e) => {
  if (popup && !popup.contains(e.target)) closePopup();
});
function menu(anchor, items) {
  closePopup();
  const r = anchor.getBoundingClientRect();
  popup = el('div', { class: 'menu-pop', style: { left: `${Math.min(r.left, innerWidth - 260)}px`, top: `${r.bottom + 4}px` } },
    items.filter(Boolean).map(([label, fn, extra]) => (label === '-' ? el('div', { style: { height: '1px', background: 'var(--border)', margin: '4px 0' } })
      : el('button', { class: 'menu-item', type: 'button', onclick: () => { closePopup(); fn(); } }, el('span', {}, label), extra ? el('span', { class: 'sub' }, extra) : null))));
  document.body.append(popup);
}

function ask(title, label, value = '') {
  return new Promise((resolve) => {
    const dlg = el('dialog', { class: 'sheet', style: { position: 'fixed', inset: '0', margin: 'auto', width: '380px', height: 'fit-content' } });
    const input = el('input', { class: 'input', value, style: { width: '100%' } });
    const form = el('form', { method: 'dialog' }, el('h3', {}, title), el('label', { class: 'field' }, el('span', {}, label), input),
      el('div', { class: 'row', style: { justifyContent: 'flex-end' } },
        el('button', { class: 'btn', type: 'button', onclick: () => { dlg.close(); resolve(null); } }, 'Cancel'),
        el('button', { class: 'btn primary', type: 'submit' }, 'OK')));
    form.addEventListener('submit', (e) => {
      e.preventDefault();
      dlg.close();
      resolve(input.value.trim() || null);
    });
    dlg.append(form);
    dlg.addEventListener('close', () => { dlg.remove(); resolve(null); });
    document.body.append(dlg);
    dlg.showModal();
    input.select();
  });
}

async function notebookMenu(anchor) {
  const nb = state.notebook;
  menu(anchor, [
    ['New notebook…', async () => {
      const name = await ask('New notebook', 'Name');
      if (name) sock.action('new_notebook', { name, template: ink.page?.template || 'blank' }).catch((e) => toast(e.message, 'error'));
    }],
    ['Rename…', async () => {
      const name = await ask('Rename notebook', 'Name', nb.name);
      if (name && name !== nb.name) api('PATCH', nbUrl(), { name }).then(loadNotebooks).catch((e) => toast(e.message, 'error'));
    }],
    ['Save this version…', () => saveVersion()],
    [nb.exclude_search ? 'Include in search' : 'Exclude from search', () => api('PATCH', nbUrl(), { exclude_search: !nb.exclude_search }).then((info) => {
      state.notebook = info;
      toast(info.exclude_search ? 'This notebook won’t appear in search.' : 'This notebook will be searchable.');
    }).catch((e) => toast(e.message, 'error'))],
    ['-'],
    ['Move notebook to the Trash…', async () => {
      if (!window.confirm(`Move "${nb.name}" to the Trash? You can put it back from the Trash in Finder.`)) return;
      api('DELETE', nbUrl()).then(loadNotebooks).catch((e) => toast(e.message, 'error'));
    }],
  ]);
}

function exportMenu(anchor) {
  const page = ink.page;
  const href = (format, extra = {}) => `${nbUrl('/export')}?${qs({ format, page: page?.id, ...extra })}`;
  const download = (url) => {
    const a = el('a', { href: url, download: '' });
    document.body.append(a);
    a.click();
    a.remove();
  };
  menu(anchor, [
    ['Copy page to the clipboard', () => post('/api/pen/action', { name: 'clipboard', args: { page: page.id } }).then(() => toast('Copied', 'success')).catch((e) => toast(e.message, 'error'))],
    ['-'],
    ['Page as PNG', () => download(href('png'))],
    ['Page as SVG', () => download(href('svg'))],
    ['Whole notebook as PDF', () => download(href('pdf'))],
    ['-'],
    ['Timelapse of this page (GIF)', () => { toast('Making the timelapse…'); download(href('gif')); }],
    ['Timelapse of this page (MP4)', () => { toast('Making the timelapse…'); download(href('mp4')); }],
    ['-'],
    ['Save page to the phone’s gallery', () => post('/api/pen/action', { name: 'send_to_phone', args: { page: page.id } }).then(() => toast('Sending to Pictures › droidxfer Notes', 'success')).catch((e) => toast(e.message, 'error'))],
    ['Save annotated copy next to the original', () => post('/api/pen/action', { name: 'save_copy', args: { page: page.id } }).then((r) => toast(r.result.where === 'mac' ? `Saved: ${r.result.paths?.[0] || ''}` : 'Saved on the phone', 'success')).catch((e) => toast(e.message, 'error'))],
  ]);
}

// ------------------------------------------------------------------ phone connection
async function loadDevices() {
  try {
    devices = (await get('/api/devices')).devices.filter((d) => d.state === 'device');
  } catch {
    devices = [];
  }
  const current = deviceSelect.value;
  put(deviceSelect, ...(devices.length ? devices.map((d) => new Option(`${d.name} (${d.connection === 'mtp' ? 'MTP' : d.connection})`, d.id)) : [new Option('No phone', '')]));
  if (devices.some((d) => d.id === current)) deviceSelect.value = current;
  renderPhone();
}

function renderPhone() {
  const up = !!status.phone;
  phoneDot.className = `dot ${up ? (status.pad_online ? 'on' : 'wait') : 'off'}`;
  phoneText.textContent = up ? (status.pad_online ? 'Pad connected' : 'Waiting for the phone…') : 'Phone not connected';
  deviceSelect.hidden = up;
  connectBtn.hidden = false;
  connectBtn.textContent = up ? 'Open pad again' : 'Open pad on phone';
  connectBtn.disabled = !up && !devices.length;
  disconnectBtn.hidden = !up;
}

async function connectPhone() {
  try {
    if (status.phone) status = await post('/api/pen/reopen');
    else {
      const device = devices.find((d) => d.id === deviceSelect.value) || devices[0];
      if (!device) return;
      if (device.connection === 'mtp') {
        toast('The pen pad needs USB debugging; this phone is in File transfer (MTP) mode.', 'error', 7000);
        return;
      }
      status = await post('/api/pen/connect', { device_id: device.id });
      toast('Opening the pad on the phone. Unlock it if it’s locked.', 'success', 5000);
    }
  } catch (err) {
    toast(err.message, 'error', 8000);
  }
  renderPhone();
}

async function disconnectPhone() {
  try {
    status = await post('/api/pen/disconnect');
  } catch (err) {
    toast(err.message, 'error');
  }
  renderPhone();
}

// ------------------------------------------------------------------ notebooks & pages
async function loadNotebooks() {
  try {
    notebooks = await get('/api/pen/notebooks');
  } catch {
    return;
  }
  put(nbSelect, ...notebooks.map((n) => new Option(n.name, n.id)));
  if (state) nbSelect.value = state.notebook.id;
}

const thumbCache = new Map();
let thumbTimer = null;
const dirtyThumbs = new Set();

function renderThumb(page, canvas) {
  const w = 146;
  const s = w / page.w;
  const dpr = window.devicePixelRatio || 1;
  canvas.width = Math.round(w * dpr);
  canvas.height = Math.round(page.h * s * dpr);
  const ctx = canvas.getContext('2d');
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  drawPage(ctx, page, { s, ox: 0, oy: 0 }, { assets: ink.assets, paper: true });
}

function renderPages() {
  if (!state) return;
  const nodes = state.pages.map((page, i) => {
    let entry = thumbCache.get(page.id);
    if (!entry) {
      const canvas = el('canvas');
      const btn = el('button', { class: 'page-thumb', type: 'button', title: `Page ${i + 1}` }, canvas, el('span', { class: 'no' }), el('span', { class: 'pad-here' }));
      btn.addEventListener('click', () => goToPage(page.id));
      btn.addEventListener('contextmenu', (e) => { e.preventDefault(); pageMenu(btn, page.id); });
      entry = { btn, canvas, drawn: false };
      thumbCache.set(page.id, entry);
    }
    entry.btn.classList.toggle('on', page.id === ink.pageId);
    entry.btn.querySelector('.no').textContent = String(i + 1);
    put(entry.btn.querySelector('.pad-here'), page.id === state.current?.page && status.pad_online ? svg('phone', 14) : '');
    if (!entry.drawn || dirtyThumbs.has(page.id)) {
      renderThumb(page, entry.canvas);
      entry.drawn = true;
    }
    return entry.btn;
  });
  dirtyThumbs.clear();
  for (const id of thumbCache.keys()) if (!state.pages.some((p) => p.id === id)) thumbCache.delete(id);
  put(pagesNav, ...nodes, el('button', { class: 'btn add', type: 'button', onclick: () => addPage() }, svg('plus', 18), 'Page'));
}

function scheduleThumbs(ids) {
  for (const id of ids) dirtyThumbs.add(id);
  clearTimeout(thumbTimer);
  thumbTimer = setTimeout(renderPages, 250);
}

function pageMenu(anchor, id) {
  const i = state.pages.findIndex((p) => p.id === id);
  const page = state.pages[i];
  menu(anchor, [
    ['Move up', () => sock.sendOp({ op: 'page_move', id, index: i - 1 }), i === 0 ? '' : ''],
    ['Move down', () => sock.sendOp({ op: 'page_move', id, index: i + 1 })],
    ['-'],
    ...[['blank', 'Blank paper'], ['ruled', 'Ruled paper'], ['grid', 'Grid paper'], ['dot', 'Dotted paper']].map(([t, label]) => [label, () => sock.sendOp({ op: 'page_update', id, template: t }), page.template === t ? '✓' : '']),
    ['-'],
    ['Delete page', () => deletePage(id)],
  ]);
}

const PAPERS = [['blank', 'Blank'], ['ruled', 'Ruled'], ['grid', 'Grid'], ['dot', 'Dots']];

// Deleting the only page: ask which paper the fresh page should have, then replace it.
function choosePaper(current) {
  return new Promise((resolve) => {
    let picked = null;
    const dlg = el('dialog', { class: 'sheet', style: { position: 'fixed', inset: '0', margin: 'auto', width: '400px', height: 'fit-content' } });
    const choices = el('div', { class: 'paper-choices' }, PAPERS.map(([v, label]) => el('button', {
      class: `btn${v === current ? ' primary' : ''}`, type: 'button', onclick: () => { picked = v; dlg.close(); },
    }, label)));
    dlg.append(
      el('h3', {}, 'Delete the last page?'),
      el('p', { class: 'hint' }, 'This is the only page, so a new empty page takes its place. Choose its paper. Undo brings the deleted page back.'),
      choices,
      el('div', { class: 'row', style: { justifyContent: 'flex-end', marginTop: '14px' } },
        el('button', { class: 'btn', type: 'button', onclick: () => dlg.close() }, 'Cancel')),
    );
    dlg.addEventListener('close', () => { dlg.remove(); resolve(picked); });
    document.body.append(dlg);
    dlg.showModal();
    choices.querySelector('.primary')?.focus();
  });
}

async function deletePage(id) {
  const old = state.pages.find((p) => p.id === id);
  if (!old) return;
  if (state.pages.length > 1) {
    sock.sendOp({ op: 'page_remove', id });
    return;
  }
  const template = await choosePaper(old.template);
  if (!template || !state.pages.some((p) => p.id === id)) return;
  const fresh = { id: newId(), w: old.bg ? 1000 : old.w, h: old.bg ? 1414 : old.h, template, bg: null, objs: {}, order: [] };
  pendingPage = fresh.id;
  // One batch, so a single undo restores the old page: add the new page first, then remove the old one.
  sock.sendOp({ op: 'batch', ops: [{ op: 'page_add', page: fresh, index: 1 }, { op: 'page_remove', id }] });
}

let pendingPage = null;
function addPage() {
  const cur = ink.page;
  const fresh = { id: newId(), w: cur && !cur.bg ? cur.w : 1000, h: cur && !cur.bg ? cur.h : 1414, template: cur?.template || 'blank', bg: null, objs: {}, order: [] };
  pendingPage = fresh.id;
  const index = state.pages.findIndex((p) => p.id === ink.pageId) + 1;
  sock.sendOp({ op: 'page_add', page: fresh, index });
}

function goToPage(id, tell = true) {
  if (!state.pages.some((p) => p.id === id)) return;
  ink.setPage(id);
  state.current = { ...state.current, page: id };
  if (tell) sock.send({ t: 'page', page: id });
  renderPages();
  if (sideTab === 'history') historyPanel.refreshSoon();
}

// ------------------------------------------------------------------ selection bar
function placeSelbar() {
  const r = ink.selectionBounds() && selectionRect;
  if (!r) return;
  const w = selbar.offsetWidth || 260;
  let x = Math.max(6, Math.min(liveHost.clientWidth - w - 6, r.x + r.w / 2 - w / 2));
  let y = r.y - 56;
  if (y < 6) y = Math.min(liveHost.clientHeight - 52, r.y + r.h + 10);
  selbar.style.left = `${x}px`;
  selbar.style.top = `${y}px`;
}
let selectionRect = null;
function showSelection(ids, rect) {
  selection = ids;
  selectionRect = rect;
  if (!ids.length) {
    selbar.hidden = true;
    textPanel.renderButtons?.();
    return;
  }
  const page = ink.page;
  const b = (icon, title, fn) => el('button', { class: 'icon-btn', type: 'button', title, onclick: fn }, svg(icon, 20));
  put(selbar, 
    b('trash', 'Delete (⌫)', () => deleteSelection()),
    b('palette', 'Use the current colour', () => {
      const changes = {};
      for (const id of ids) if (page.objs[id] && page.objs[id].type !== 'image') changes[id] = { color: prefs.color };
      if (Object.keys(changes).length) sock.sendOp({ op: 'update', page: page.id, changes });
    }),
    b('duplicate', 'Duplicate', () => duplicate(ids)),
    b('copy', 'Copy as picture (clipboard)', () => post('/api/pen/action', { name: 'clipboard', args: { page: page.id, ids } }).then(() => toast('Copied', 'success')).catch((e) => toast(e.message, 'error'))),
    b('text', 'Turn into text', () => { openTab('text'); textPanel.read(ids); }),
    b('sign', 'Save as signature / sticker', async () => {
      const name = await ask('Save as signature or sticker', 'Name', 'Signature');
      if (name) post('/api/pen/action', { name: 'sticker_save', args: { name, page: page.id, ids } }).then(() => toast(`Saved “${name}”`, 'success')).catch((e) => toast(e.message, 'error'));
    }),
  );
  selbar.hidden = false;
  placeSelbar();
  textPanel.renderButtons?.();
}

function deleteSelection() {
  if (!selection.length) return;
  sock.sendOp({ op: 'remove', page: ink.page.id, ids: selection });
  ink.clearSelection();
}

function duplicate(ids) {
  const page = ink.page;
  const copies = ids.map((id) => structuredClone(page.objs[id])).filter(Boolean).map((o) => {
    o.id = newId();
    if (o.type === 'stroke') o.pts = o.pts.map((p) => [p[0] + 24, p[1] + 24, ...p.slice(2)]);
    else { o.x += 24; o.y += 24; }
    return o;
  });
  sock.sendOp({ op: 'add', page: page.id, objs: copies });
  ink.selectIds(copies.map((o) => o.id));
}

// ------------------------------------------------------------------ pictures
async function uploadPicture(file, mode) {
  if (!state) return;
  toast('Adding the picture…', 'info', 2000);
  try {
    await api('POST', `${nbUrl('/pictures')}?${qs({ mode, page: ink.pageId })}`, undefined, file);
  } catch (err) {
    toast(err.message, 'error', 6000);
  }
}

let dropHint = null;
stage.addEventListener('dragover', (e) => {
  if (![...e.dataTransfer.types].includes('Files')) return;
  e.preventDefault();
  if (!dropHint) {
    dropHint = el('div', { class: 'drop-hint' }, 'Drop to insert the picture · hold Shift to open it as a new page to annotate');
    stage.append(dropHint);
  }
});
stage.addEventListener('dragleave', (e) => {
  if (e.target === stage || !stage.contains(e.relatedTarget)) { dropHint?.remove(); dropHint = null; }
});
stage.addEventListener('drop', (e) => {
  e.preventDefault();
  dropHint?.remove();
  dropHint = null;
  const file = [...e.dataTransfer.files].find((f) => f.type.startsWith('image/') || /\.(heic|heif)$/i.test(f.name));
  if (file) uploadPicture(file, e.shiftKey ? 'page' : 'insert');
  else toast('Drop a picture (PNG, JPEG, HEIC…).');
});
document.addEventListener('paste', (e) => {
  if (e.target instanceof HTMLInputElement || e.target instanceof HTMLTextAreaElement) return;
  const item = [...(e.clipboardData?.items || [])].find((i) => i.type.startsWith('image/'));
  if (item) {
    e.preventDefault();
    uploadPicture(item.getAsFile(), 'insert');
  }
});

async function screenshot() {
  toast('Drag a box around what you want to annotate (Space picks a window, Esc cancels).', 'info', 6000);
  try {
    await post('/api/pen/action', { name: 'screenshot', args: {} });
  } catch (err) {
    if (!/cancelled/.test(err.message)) toast(err.message, 'error', 9000);
  }
}

async function saveVersion() {
  const name = await ask('Save this version', 'Name it so you can find it later', new Date().toLocaleString());
  if (!name) return;
  try {
    await post(nbUrl('/checkpoint'), { name });
    toast('Version saved', 'success');
  } catch (err) {
    toast(err.message, 'error');
  }
}

// ------------------------------------------------------------------ present mode
function present() {
  const restoreTool = prefs.tool;
  chooseTool('laser');
  stage.requestFullscreen?.().then(() => {
    stage.classList.add('presenting');
    setTimeout(() => ink.fit('page'), 120);
  }).catch(() => toast('Full screen isn’t available here.'));
  const onExit = () => {
    if (document.fullscreenElement) return;
    document.removeEventListener('fullscreenchange', onExit);
    stage.classList.remove('presenting');
    chooseTool(restoreTool);
    setTimeout(() => ink.fit('width'), 120);
  };
  document.addEventListener('fullscreenchange', onExit);
}

// ------------------------------------------------------------------ side panel
let sideTab = pref('tab', 'history');
const tabs = el('div', { class: 'side-tabs' });
const sideBody = el('div', { class: 'side-body' });
side.append(tabs, sideBody);

function openTab(name) {
  sideTab = name;
  setPref('tab', name);
  for (const b of tabs.children) b.classList.toggle('on', b.dataset.tab === name);
  if (main.classList.contains('no-side')) {
    main.classList.remove('no-side');
    side.hidden = false;
  }
  ({ history: historyPanel, text: textPanel, tablet: tabletPanel, settings: settingsPanel })[name].render();
}
for (const [id, label] of [['history', 'History'], ['text', 'Text'], ['tablet', 'Tablet'], ['settings', 'Settings']]) {
  tabs.append(el('button', { type: 'button', dataset: { tab: id }, onclick: () => openTab(id) }, label));
}

// ---------------- history (version control)
const historyPanel = {
  data: null,
  expanded: new Set(),
  timer: null,
  refreshSoon() {
    clearTimeout(this.timer);
    this.timer = setTimeout(() => sideTab === 'history' && this.render(), 500);
  },
  async render() {
    if (!state) return;
    try {
      this.data = await get(`${nbUrl('/history')}?limit=1500`);
    } catch (err) {
      put(sideBody, el('p', { class: 'hint' }, err.message));
      return;
    }
    const { entries, checkpoints, more } = this.data;
    const sessions = [];
    for (const e of entries) {
      const last = sessions[sessions.length - 1];
      if (last && last.id === e.session) last.entries.push(e);
      else sessions.push({ id: e.session, entries: [e] });
    }
    if (!this.expanded.size && sessions[0]) this.expanded.add(sessions[0].id);
    const pageId = ink.pageId;
    const nodes = [
      el('div', { class: 'row' },
        el('button', { class: 'btn small primary', type: 'button', onclick: () => saveVersion() }, svg('save', 16), 'Save version'),
        el('button', { class: 'btn small', type: 'button', onclick: () => startReplay() }, svg('play', 14), 'Replay page'),
      ),
      el('p', { class: 'hint' }, 'Every change is kept. Click any entry to see the notebook as it was, then restore it, compare it with now, or copy parts of it back.'),
    ];
    if (checkpoints.length) {
      nodes.push(el('h4', {}, 'Saved versions'));
      nodes.push(el('div', { class: 'entries' }, checkpoints.slice(0, 20).map((c) => this.entryNode(c, true))));
    }
    nodes.push(el('h4', {}, 'Sessions'));
    for (const s of sessions) {
      const first = s.entries[s.entries.length - 1];
      const last = s.entries[0];
      const thumbPage = last.pages.find((p) => state.pages.some((x) => x.id === p)) || pageId;
      const open = this.expanded.has(s.id);
      const head = el('div', { class: 'session-head', onclick: () => { if (open) this.expanded.delete(s.id); else this.expanded.add(s.id); this.render(); } },
        el('img', { alt: '', loading: 'lazy', src: `${nbUrl('/thumb')}?${qs({ page: thumbPage, seq: last.seq, w: 104 })}` }),
        el('div', {}, el('div', { class: 't' }, `${fmtDay(first.t)} ${fmtTime(first.t)}–${fmtTime(last.t)}`),
          el('div', { class: 'm' }, `${s.entries.length} change${s.entries.length === 1 ? '' : 's'} · ${open ? 'hide' : 'show'}`)));
      nodes.push(head);
      if (open) nodes.push(el('div', { class: 'entries' }, s.entries.map((e) => this.entryNode(e))));
    }
    if (more) nodes.push(el('p', { class: 'hint' }, 'Older changes are kept too; export or compact them in Settings.'));
    if (!entries.length) nodes.push(el('p', { class: 'hint' }, 'Nothing yet. Start drawing!'));
    put(sideBody, ...nodes);
  },
  entryNode(e, checkpoint = false) {
    const icon = e.actor === 'pad' ? 'phone' : e.actor === 'mac' ? 'mac' : 'history';
    return el('button', {
      class: `entry kind-${e.kind}${preview?.seq === e.seq ? ' on' : ''}`, type: 'button', title: `Version #${e.seq}`,
      onclick: () => enterPreview(e),
    }, svg(icon, 15), el('span', { class: 'what' }, checkpoint ? `★ ${e.name || e.label}` : e.label), el('span', { class: 'when' }, checkpoint ? `${fmtDay(e.t)} ${fmtTime(e.t)}` : fmtTime(e.t)));
  },
};

// preview / compare / restore
const banner = el('div', { class: 'banner', hidden: true });
stage.append(banner);

async function enterPreview(entry) {
  let data;
  try {
    data = await get(`${nbUrl('/state')}?seq=${entry.seq}`);
  } catch (err) {
    toast(err.message, 'error');
    return;
  }
  exitPreview(false);
  const pageId = data.pages.some((p) => p.id === ink.pageId) ? ink.pageId : (entry.pages.find((id) => data.pages.some((p) => p.id === id)) || data.pages[0].id);
  preview = { seq: entry.seq, entry, state: data, pageId, mode: 'view', sel: [] };
  liveHost.hidden = true;
  showPreview();
  historyPanel.render();
}

function previewCanvas(pages, pageId, opts = {}) {
  const host = el('div', { class: 'ink-host' });
  const c = new InkCanvas(host, { role: 'mac', readOnly: true, assetUrl, onSelection: opts.onSelection });
  c.setState({ pages }, pageId);
  c.setTool({ tool: 'lasso' });
  return { host, c };
}

function showPreview() {
  const p = preview;
  p.views?.forEach((v) => v.c.destroy());
  p.wrap?.remove();
  p.views = [];
  const nowState = { pages: state.pages };
  const nowPage = nowState.pages.find((x) => x.id === p.pageId);
  if (p.mode === 'side') {
    const old = previewCanvas(p.state.pages, p.pageId);
    const now = previewCanvas(nowState.pages, nowPage ? p.pageId : ink.pageId);
    old.host.append(el('span', { class: 'tag' }, `Version #${p.seq}`));
    now.host.append(el('span', { class: 'tag' }, 'Now'));
    p.wrap = el('div', { class: 'split' }, old.host, now.host);
    p.views = [old, now];
  } else if (p.mode === 'overlay') {
    // everything now, plus what was removed since; added = green, removed = red
    const oldPage = p.state.pages.find((x) => x.id === p.pageId);
    const merged = structuredClone(nowPage || oldPage);
    const highlight = new Map();
    if (oldPage && nowPage) {
      for (const id of nowPage.order) if (!oldPage.objs[id]) highlight.set(id, '#2f9e44');
      for (const id of oldPage.order) {
        if (!nowPage.objs[id]) {
          merged.objs[id] = oldPage.objs[id];
          merged.order.push(id);
          highlight.set(id, '#e5484d');
        }
      }
    }
    const view = previewCanvas([merged], merged.id);
    view.c.highlight = highlight;
    view.c.invalidate();
    p.wrap = el('div', { class: 'ink-host', style: { position: 'absolute', inset: '0' } }, view.host);
    view.host.style.position = 'absolute';
    view.host.style.inset = '0';
    p.views = [view];
  } else {
    const view = previewCanvas(p.state.pages, p.pageId, { onSelection: (ids) => { p.sel = ids; renderBanner(); } });
    view.host.style.position = 'absolute';
    view.host.style.inset = '0';
    p.wrap = view.host;
    p.views = [view];
  }
  stage.insertBefore(p.wrap, banner);
  renderBanner();
}

function renderBanner() {
  const p = preview;
  if (!p) {
    banner.hidden = true;
    return;
  }
  const when = `${fmtDay(p.entry.t)} ${fmtTime(p.entry.t)}`;
  const pageExistsNow = state.pages.some((x) => x.id === p.pageId);
  const pageNo = p.state.pages.findIndex((x) => x.id === p.pageId) + 1;
  const pager = p.state.pages.length > 1 ? el('select', { class: 'input', onchange: (e) => { p.pageId = e.target.value; showPreview(); } },
    p.state.pages.map((pg, i) => { const o = new Option(`Page ${i + 1}`, pg.id); o.selected = pg.id === p.pageId; return o; })) : null;
  put(banner, 
    el('span', {}, el('b', {}, `Version #${p.seq}`), ` · ${when} · ${p.entry.label}`),
    pager,
    el('div', { class: 'seg' }, ...[['view', 'Then'], ['overlay', 'Changes'], ['side', 'Side by side']].map(([m, label]) =>
      el('button', { type: 'button', class: p.mode === m ? 'on' : '', onclick: () => { p.mode = m; p.sel = []; showPreview(); } }, label))),
    p.mode === 'view' ? el('button', { class: 'btn small', type: 'button', disabled: !p.sel.length || !pageExistsNow && !state.pages.length, title: 'Lasso parts of the old version, then copy them into the page as it is now',
      onclick: () => copyFromPast() }, `Copy ${p.sel.length || ''} selected to now`) : null,
    el('button', { class: 'btn small', type: 'button', onclick: () => restore(true) }, `Restore page ${pageNo}`),
    el('button', { class: 'btn small', type: 'button', onclick: () => restore(false) }, 'Restore whole notebook'),
    el('button', { class: 'btn small primary', type: 'button', onclick: () => exitPreview() }, 'Back to now'),
  );
  banner.hidden = false;
}

function copyFromPast() {
  const p = preview;
  const oldPage = p.state.pages.find((x) => x.id === p.pageId);
  const target = state.pages.some((x) => x.id === p.pageId) ? p.pageId : ink.pageId;
  const objs = p.sel.map((id) => structuredClone(oldPage.objs[id])).filter(Boolean).map((o) => ({ ...o, id: newId() }));
  if (!objs.length) return;
  sock.sendOp({ op: 'add', page: target, objs }, { copy: true });
  toast(`Copied ${objs.length} item${objs.length === 1 ? '' : 's'} into the current page. Undo removes them.`, 'success');
  exitPreview();
  goToPage(target);
}

async function restore(pageOnly) {
  const p = preview;
  const what = pageOnly ? 'this page' : 'the whole notebook';
  if (!window.confirm(`Restore ${what} to version #${p.seq}? The current version stays in the history, so you can undo this or restore it later.`)) return;
  try {
    await post(nbUrl('/restore'), { seq: p.seq, page: pageOnly ? p.pageId : null });
    toast(`Restored ${what}.`, 'success');
    const target = p.pageId;
    exitPreview();
    setTimeout(() => goToPage(target), 150);
  } catch (err) {
    toast(err.message, 'error');
  }
}

function exitPreview(render = true) {
  if (!preview) return;
  preview.views?.forEach((v) => v.c.destroy());
  preview.wrap?.remove();
  preview.timer && clearInterval(preview.timer);
  preview = null;
  banner.hidden = true;
  liveHost.hidden = false;
  ink.invalidate();
  if (render && sideTab === 'history') historyPanel.render();
}

// replay: rebuild the page change by change
async function startReplay() {
  const pageId = ink.pageId;
  let ops;
  try {
    ops = (await get(`${nbUrl('/ops')}?${qs({ start: 0, page: pageId })}`)).ops;
  } catch (err) {
    toast(err.message, 'error');
    return;
  }
  if (!ops.length) {
    toast('This page has no history to replay yet.');
    return;
  }
  let base;
  try {
    base = await get(`${nbUrl('/state')}?seq=${ops[0].seq - 1}`);
  } catch (err) {
    toast(err.message, 'error');
    return;
  }
  exitPreview(false);
  const allOps = (await get(`${nbUrl('/ops')}?${qs({ start: ops[0].seq - 1, end: ops[ops.length - 1].seq })}`)).ops;
  const work = structuredClone(base);
  const view = previewCanvas(work.pages, pageId);
  view.host.style.position = 'absolute';
  view.host.style.inset = '0';
  liveHost.hidden = true;
  preview = { seq: null, replay: true, wrap: view.host, views: [view] };
  stage.insertBefore(view.host, banner);
  let i = 0;
  let speed = 1;
  let playing = true;
  const status = el('span', {});
  const step = () => {
    if (!playing || i >= allOps.length) return;
    const n = Math.max(1, Math.round(speed));
    for (let k = 0; k < n && i < allOps.length; k += 1, i += 1) applyOp(work, allOps[i].op);
    view.c.setState(work, pageId);
    status.textContent = `${i} / ${allOps.length} · ${fmtDay(allOps[Math.max(i - 1, 0)].t)} ${fmtTime(allOps[Math.max(i - 1, 0)].t)}`;
    if (i >= allOps.length) playing = false;
  };
  preview.timer = setInterval(step, 120);
  const playBtn = el('button', { class: 'btn small', type: 'button', onclick: () => { if (i >= allOps.length) { i = 0; Object.assign(work, structuredClone(base)); } playing = !playing; } }, 'Play / pause');
  const speedSel = el('select', { class: 'input', onchange: (e) => { speed = Number(e.target.value); } }, [1, 4, 16, 64].map((v) => new Option(`${v}×`, String(v))));
  put(banner, el('b', {}, 'Replay'), status, playBtn, speedSel,
    el('a', { class: 'btn small', href: `${nbUrl('/export')}?${qs({ format: 'gif', page: pageId })}`, download: '' }, 'GIF'),
    el('a', { class: 'btn small', href: `${nbUrl('/export')}?${qs({ format: 'mp4', page: pageId })}`, download: '' }, 'MP4'),
    el('button', { class: 'btn small primary', type: 'button', onclick: () => exitPreview() }, 'Done'));
  banner.hidden = false;
}

// ---------------- text (handwriting) & search
const textPanel = {
  result: null,
  ids: null,
  busy: false,
  render() {
    const modeSel = el('select', { class: 'input' }, [['', `Default (${settings?.hw_mode || 'auto'})`], ['auto', 'Auto: Vision, then Gemma'], ['vision', 'Apple Vision only'], ['gemma', 'Gemma only']].map(([v, l]) => new Option(l, v)));
    this.modeSel = modeSel;
    this.buttons = el('div', { class: 'row' });
    this.out = el('div', { class: 'stack' });
    const search = el('input', { class: 'input', type: 'search', placeholder: 'Search your handwriting…', style: { width: '100%' } });
    const hits = el('div', {});
    let t = null;
    search.addEventListener('input', () => {
      clearTimeout(t);
      t = setTimeout(async () => {
        const q = search.value.trim();
        if (!q) { put(hits, ); return; }
        try {
          const res = await get(`/api/pen/search?${qs({ q })}`);
          put(hits, ...(res.length ? res.map((h) => el('button', { class: 'search-hit', type: 'button', onclick: () => openHit(h) },
            el('div', {}, el('b', {}, h.name), ` · page ${h.page_no}`), el('div', { class: 's' }, h.via === 'translation' ? `English translation: ${h.snippet}` : h.snippet))) : [el('p', { class: 'hint' }, 'No matches.')]));
        } catch (err) {
          put(hits, el('p', { class: 'hint' }, err.message));
        }
      }, 250);
    });
    put(sideBody, 
      el('h4', {}, 'Handwriting to text'),
      el('label', { class: 'field' }, el('span', {}, 'Engine'), modeSel),
      this.buttons,
      this.out,
      el('h4', {}, 'Search all notebooks'),
      search, hits,
      this.indexLine = el('p', { class: 'hint' }),
      el('p', { class: 'hint' }, 'Handwriting is read in the background so you can search it: by Apple Vision a few seconds after you stop writing, then by Gemma when the engine is Gemma, or Auto and Vision wasn’t sure. Gemma reads one page at a time, only while the Mac is plugged in. Everything stays on this Mac. Notebooks can be excluded from the notebook menu.'),
      el('h4', {}, 'Which engine is better for my handwriting?'),
      el('button', { class: 'btn small', type: 'button', onclick: () => this.compare() }, 'Compare Vision and Gemma on this page'),
    );
    this.renderButtons();
    this.renderResult();
    this.refreshIndex();
  },
  async refreshIndex() {
    clearTimeout(this.indexTimer);
    if (sideTab !== 'text' || !this.indexLine?.isConnected) return;
    try {
      const s = await get('/api/pen/search/status');
      const n = s.gemma_pending;
      const pages = `${n} page${n === 1 ? '' : 's'}`;
      this.indexLine.textContent = s.mode === 'vision' ? ''
        : s.paused === 'battery' && n ? `Improving search with Gemma: ${pages} waiting until the Mac is plugged in.`
        : s.paused === 'ollama' && n ? `Gemma isn’t reachable (start the Ollama app), so ${pages} wait. Search uses Apple Vision’s reading meanwhile.`
        : n ? `Improving search: ${pages} left to read with Gemma.`
        : 'Search uses Gemma’s reading where it helps.';
    } catch {
      this.indexLine.textContent = '';
    }
    this.indexTimer = setTimeout(() => this.refreshIndex(), 5000);
  },
  renderButtons() {
    if (!this.buttons || sideTab !== 'text') return;
    put(this.buttons, 
      el('button', { class: 'btn small primary', type: 'button', disabled: this.busy, onclick: () => this.read(null) }, 'Read this page'),
      el('button', { class: 'btn small', type: 'button', disabled: this.busy || !selection.length, onclick: () => this.read(selection) }, `Read selection${selection.length ? ` (${selection.length})` : ''}`),
    );
  },
  async read(ids) {
    if (sideTab !== 'text') openTab('text');
    this.busy = true;
    this.ids = ids;
    this.result = null;
    this.renderButtons();
    put(this.out, el('p', { class: 'hint' }, 'Reading… (Gemma can take a few seconds the first time it loads)'));
    try {
      const r = await post('/api/pen/action', { name: 'to_text', args: { page: ink.pageId, ids, mode: this.modeSel?.value || null } });
      this.result = r.result;
    } catch (err) {
      put(this.out, el('p', { class: 'hint' }, err.message));
    }
    this.busy = false;
    this.renderButtons();
    this.renderResult();
  },
  renderResult() {
    const r = this.result;
    if (!r || !this.out) return;
    const engine = (name) => (name?.startsWith('gemma') ? `Gemma (${name.slice(6)})` : 'Apple Vision');
    const page = ink.page;
    const area = unionBounds((this.ids || page.order).map((id) => page.objs[id]).filter((o) => o?.type === 'stroke')) || [40, 40, 440, 100];
    const textObj = (value) => ({ id: newId(), type: 'text', text: value, color: '#1d1d1f', size: 28, x: area[0], w: Math.max(200, area[2] - area[0]) });
    const box = (value) => {
      const t = el('textarea', { class: 'result-text', style: { width: '100%', minHeight: '120px' } });
      t.value = value;
      return t;
    };
    // Copy and Insert below for any text box (the reading or a translation)
    const actions = (t, extra = []) => el('div', { class: 'row' },
      el('button', { class: 'btn small', type: 'button', onclick: () => navigator.clipboard.writeText(t.value).then(() => toast('Copied', 'success')) }, 'Copy'),
      el('button', { class: 'btn small', type: 'button', onclick: () => sock.sendOp({ op: 'add', page: page.id, objs: [{ ...textObj(t.value), y: area[3] + 10 }] }) }, 'Insert below'),
      ...extra);
    const text = box(r.text);
    const nodes = [
      r.text ? text : el('p', { class: 'hint' }, 'No text found.'),
      el('p', { class: 'hint' }, r.engine ? `${engine(r.engine)}${r.confidence != null ? ` · ${Math.round(r.confidence * 100)}% sure` : ''} · ${(r.ms / 1000).toFixed(1)} s` : ''),
      r.attempts.length > 1 ? el('p', { class: 'hint' }, `Tried: ${r.attempts.map((a) => `${engine(a.engine)} (${a.error ? 'failed' : `${(a.ms / 1000).toFixed(1)} s`})`).join(', ')}`) : null,
    ];
    if (r.text) {
      nodes.push(actions(text, [el('button', { class: 'btn small', type: 'button', onclick: () => {
        const strokes = (this.ids || page.order).filter((id) => page.objs[id]?.type === 'stroke');
        sock.sendOp({ op: 'batch', ops: [{ op: 'remove', page: page.id, ids: strokes }, { op: 'add', page: page.id, objs: [{ ...textObj(text.value), y: area[1] }] }] });
      } }, 'Replace handwriting')]));
      if (r.translation) {
        const tr = box(r.translation);
        nodes.push(el('h4', {}, 'English translation'), tr, actions(tr));
      }
      // other languages on demand (one quick Gemma request, no image)
      const target = el('select', { class: 'input' }, ['English', 'Malayalam', 'Tamil', 'Japanese', 'Hindi'].map((l) => new Option(l, l)));
      const translated = el('div', { class: 'stack' });
      const go = el('button', { class: 'btn small', type: 'button', onclick: async () => {
        go.disabled = true;
        put(translated, el('p', { class: 'hint' }, `Translating into ${target.value}…`));
        try {
          const res = await post('/api/pen/translate', { text: text.value, target: target.value });
          const t = box(res.text);
          put(translated, el('p', { class: 'hint' }, `${res.target} · ${(res.ms / 1000).toFixed(1)} s`), t, actions(t));
        } catch (err) {
          put(translated, el('p', { class: 'hint' }, err.message));
        }
        go.disabled = false;
      } }, 'Translate');
      nodes.push(el('h4', {}, 'Translate to'), el('div', { class: 'row' }, target, go), translated);
    }
    put(this.out, ...nodes);
  },
  async compare() {
    put(this.out, el('p', { class: 'hint' }, 'Running both engines…'));
    try {
      const res = await post(`${nbUrl('/hw-compare')}?${qs({ page: ink.pageId })}`);
      const card = (title, a) => el('div', { class: 'attempt' },
        el('div', { class: 'h' }, title, el('span', { class: 'badge' }, a.error ? 'failed' : `${(a.ms / 1000).toFixed(2)} s`),
          a.confidence != null ? el('span', { class: 'badge' }, `${Math.round(a.confidence * 100)}% sure`) : null),
        el('div', { class: 'result-text' }, a.error || a.text || '(nothing found)'));
      put(this.out, card('Apple Vision', res.vision), card(`Gemma (${settings?.gemma_model})`, res.gemma),
        el('p', { class: 'hint' }, 'Pick the better one as the default in Settings.'));
    } catch (err) {
      put(this.out, el('p', { class: 'hint' }, err.message));
    }
  },
};

async function openHit(hit) {
  if (hit.notebook !== state.notebook.id) {
    try {
      await sock.action('open', { notebook: hit.notebook, page: hit.page });
    } catch (err) {
      toast(err.message, 'error');
    }
  } else goToPage(hit.page);
}

// ---------------- tablet mode
const tabletPanel = {
  info: null,
  profile: null,
  async render() {
    try {
      this.info = (await post('/api/pen/action', { name: 'tablet_info' })).result;
    } catch (err) {
      put(sideBody, el('p', { class: 'hint' }, err.message));
      return;
    }
    const info = this.info;
    const allowed = info.allowed;
    const targets = [...(info.displays || []).map((d) => [`display:${d.id}`, `${d.name} (${Math.round(d.w)}×${Math.round(d.h)})`]), ['window', 'The front window (follows the app you use)']];
    const cur = info.target?.kind === 'window' ? 'window' : `display:${info.target?.id ?? info.displays?.[0]?.id}`;
    const targetSel = el('select', { class: 'input', onchange: () => this.set(info.on, targetSel.value) }, targets.map(([v, l]) => { const o = new Option(l, v); o.selected = v === cur; return o; }));
    this.profile ??= info.profile;
    put(sideBody, 
      el('h4', {}, 'S Pen as a Mac tablet'),
      el('p', { class: 'hint' }, 'The S Pen moves the Mac’s pointer and draws in any app (Preview, Photoshop, Krita, Figma…) with pressure and tilt. Hovering moves the pointer; the S Pen button right-clicks.'),
      allowed === false ? el('div', { class: 'attempt' },
        el('p', {}, `macOS must allow ${info.host_app} to control the computer: System Settings › Privacy & Security › Accessibility. Turn it on, then restart droidxfer.`),
        el('button', { class: 'btn small', type: 'button', onclick: () => post('/api/pen/action', { name: 'open_privacy', args: { kind: 'accessibility' } }) }, 'Open Accessibility settings')) : null,
      el('div', { class: 'row' },
        el('button', { class: `btn small ${info.on ? '' : 'primary'}`, type: 'button', onclick: () => this.set(!info.on, targetSel.value) }, info.on ? 'Turn tablet mode off' : 'Turn tablet mode on'),
        el('span', { class: 'muted' }, info.on ? 'On: draw on the phone' : 'Off')),
      el('label', { class: 'field' }, el('span', {}, 'The phone screen maps to'), targetSel),
      info.front ? el('p', { class: 'hint' }, `Front app now: ${info.front.app} → shortcut set “${info.profile}”`) : null,
      el('h4', {}, 'Shortcut buttons on the phone'),
      this.editor(),
    );
  },
  async set(on, targetValue) {
    const target = targetValue === 'window' ? { kind: 'window' } : { kind: 'display', id: Number(String(targetValue).split(':')[1]) };
    try {
      await post('/api/pen/action', { name: 'tablet', args: { on, target } });
    } catch (err) {
      toast(err.message, 'error', 9000);
    }
    this.render();
  },
  editor() {
    const all = structuredClone(this.info.shortcuts);
    const names = Object.keys(all);
    if (!names.includes(this.profile)) this.profile = names[0];
    const rows = el('div', {});
    const draw = () => put(rows, ...all[this.profile].map((item, i) => el('div', { class: 'shortcut-row' },
      el('input', { class: 'input', value: item[0], oninput: (e) => { item[0] = e.target.value; } }),
      el('input', { class: 'input', value: item[1], title: 'Like cmd+shift+z, [, b, escape', oninput: (e) => { item[1] = e.target.value; } }),
      el('button', { class: 'icon-btn', type: 'button', title: 'Remove', onclick: () => { all[this.profile].splice(i, 1); draw(); } }, svg('close', 16)))));
    const profileSel = el('select', { class: 'input', onchange: () => { this.profile = profileSel.value; draw(); } }, names.map((n) => { const o = new Option(n, n); o.selected = n === this.profile; return o; }));
    draw();
    return el('div', { class: 'stack' },
      el('div', { class: 'row' }, profileSel,
        el('button', { class: 'btn small', type: 'button', onclick: async () => {
          const name = await ask('Shortcut set for an app', 'App name as macOS shows it (e.g. Pixelmator Pro)');
          if (name && !all[name]) { all[name] = [['Undo', 'cmd+z']]; this.info.shortcuts = all; this.profile = name; this.render(); }
        } }, 'Add app')),
      rows,
      el('div', { class: 'row' },
        el('button', { class: 'btn small', type: 'button', onclick: () => { all[this.profile].push(['New', 'cmd+z']); draw(); } }, 'Add shortcut'),
        el('button', { class: 'btn small primary', type: 'button', onclick: async () => {
          try {
            await api('PUT', '/api/pen/settings', { shortcuts: all });
            toast('Shortcuts saved', 'success');
            this.render();
          } catch (err) {
            toast(err.message, 'error', 7000);
          }
        } }, 'Save')),
      el('p', { class: 'hint' }, 'Keys: cmd, shift, alt, ctrl + a letter, number, [ ] - = , . / ; \' ` or escape, return, tab, space, delete, arrows. The set is chosen by the front app’s name.'));
  },
};

// ---------------- settings
const settingsPanel = {
  async render() {
    try {
      settings = await get('/api/pen/settings');
    } catch (err) {
      put(sideBody, el('p', { class: 'hint' }, err.message));
      return;
    }
    const save = async (changes) => {
      try {
        settings = await api('PUT', '/api/pen/settings', changes);
        toast('Saved', 'success');
      } catch (err) {
        toast(err.message, 'error');
      }
    };
    const mode = el('select', { class: 'input', onchange: () => save({ hw_mode: mode.value }) },
      [['auto', 'Auto: Apple Vision, then Gemma if unsure'], ['vision', 'Apple Vision only (fast)'], ['gemma', 'Gemma only (slower, often better on messy writing)']].map(([v, l]) => { const o = new Option(l, v); o.selected = v === settings.hw_mode; return o; }));
    const models = settings.gemma_models.length ? settings.gemma_models : [settings.gemma_model];
    const model = el('select', { class: 'input', onchange: () => save({ gemma_model: model.value }) }, models.map((m) => { const o = new Option(m, m); o.selected = m === settings.gemma_model; return o; }));
    const browsers = [['', 'The phone’s default browser'], ['com.android.chrome', 'Chrome'], ['com.sec.android.app.sbrowser', 'Samsung Internet']];
    const browser = el('select', { class: 'input', onchange: () => save({ browser: browser.value }) }, browsers.map(([v, l]) => { const o = new Option(l, v); o.selected = v === settings.browser; return o; }));
    const days = el('input', { class: 'input', type: 'number', min: '1', value: '90', style: { width: '80px' } });
    put(sideBody, 
      el('h4', {}, 'Handwriting to text'),
      el('label', { class: 'field' }, el('span', {}, 'Engine'), mode),
      el('label', { class: 'field' }, el('span', {}, 'Gemma model (Ollama on this Mac)'), model),
      el('p', { class: 'hint' }, 'Any language, even mixed on a page: Apple Vision reads English, and when it isn’t sure (as with Telugu, which it can’t read), Gemma reads every word in the script it’s written in. Writing that isn’t in English letters also gets an English translation, so searching “weather” finds “వాతావరణం”. For Telugu, gemma4:31b read every word right in a test; smaller models missed some.'),
      el('p', { class: 'hint' }, `Apple Vision: ${settings.vision ? 'available' : 'not available'} · Ollama: ${settings.gemma_models.length ? `${settings.gemma_models.length} model(s) that read images` : 'not running (start the Ollama app)'}. Nothing leaves this Mac.`),
      el('h4', {}, 'Phone'),
      el('label', { class: 'field' }, el('span', {}, 'Open the pad in'), browser),
      el('h4', {}, 'This notebook'),
      el('p', { class: 'hint' }, `Saved in ${settings.notes_dir}/${state?.notebook.name}`),
      el('div', { class: 'row' }, el('span', {}, 'Forget history older than'), days, el('span', {}, 'days'),
        el('button', { class: 'btn small', type: 'button', onclick: async () => {
          if (!window.confirm(`Forget the history of "${state.notebook.name}" older than ${days.value} days? The notebook itself is kept, but you can no longer go back to those versions.`)) return;
          try {
            const r = await post(nbUrl('/compact'), { older_than_days: Number(days.value) });
            toast(r.removed ? `Removed ${r.removed} old changes.` : 'Nothing that old.', 'success');
          } catch (err) {
            toast(err.message, 'error');
          }
        } }, 'Compact')),
      el('p', { class: 'hint' }, 'By default everything is kept forever (strokes are small).'),
    );
  },
};

// ------------------------------------------------------------------ socket events
function renderAll() {
  nbSelect.value = state.notebook.id;
  undoBtn.disabled = !state.can_undo;
  redoBtn.disabled = !state.can_redo;
  renderPages();
}

sock.on('state', (msg) => {
  const switched = !state || state.notebook.id !== msg.notebook.id;
  state = msg;
  status = msg.status;
  if (switched) {
    thumbCache.clear();
    ink.assets.images.clear();
    exitPreview(false);
    ink.fitted = false;
    loadNotebooks();
  }
  ink.setState({ pages: msg.pages }, msg.current.page);
  if (msg.tool?.tool) padStatus.textContent = `Phone: ${msg.tool.tool}`;
  renderAll();
  renderPhone();
  openTab(sideTab);
});
sock.on('applied', (msg) => {
  if (!state || msg.notebook !== state.notebook.id) return;
  const touched = applyOp(ink.state, msg.entry.op);
  state.pages = ink.state.pages;
  state.can_undo = msg.can_undo;
  state.can_redo = msg.can_redo;
  state.notebook.pages = state.pages.length;
  ink.changed(msg.entry.op, touched);
  if (pendingPage && state.pages.some((p) => p.id === pendingPage)) {
    goToPage(pendingPage);
    pendingPage = null;
  } else if (!state.pages.some((p) => p.id === ink.pageId)) {
    goToPage(msg.current?.page || state.pages[0].id, false);
  }
  undoBtn.disabled = !state.can_undo;
  redoBtn.disabled = !state.can_redo;
  scheduleThumbs(touched);
  if (sideTab === 'history' && !preview) historyPanel.refreshSoon();
});
sock.on('page', (msg) => goToPage(msg.page, false));
for (const t of ['live', 'hover', 'hover_end', 'laser']) sock.on(t, (msg) => ink.remoteMessage(msg));
sock.on('tool', (msg) => { padStatus.textContent = `Phone: ${msg.tool}`; });
sock.on('status', (msg) => {
  status = msg;
  renderPhone();
  renderPages();
});
sock.on('rejected', (msg) => {
  if (!/already exists/.test(msg.error)) toast(`Not saved: ${msg.error}`, 'error');
});
sock.on('notice', (msg) => toast(msg.text));
sock.on('tablet', () => sideTab === 'tablet' && tabletPanel.render());
sock.on('connection', (on) => {
  if (!on) toast('Lost the connection to droidxfer. Reconnecting…', 'error', 2500);
});
sock.on('expired', () => {
  put(document.body, el('div', { class: 'cover' }, el('div', {}, el('h2', {}, 'Session expired'), el('p', {}, 'Open the link printed by droidxfer serve again.'))));
});

// ------------------------------------------------------------------ keyboard
document.addEventListener('keydown', (e) => {
  if (e.target instanceof HTMLInputElement || e.target instanceof HTMLTextAreaElement || e.target instanceof HTMLSelectElement) return;
  const mod = e.metaKey || e.ctrlKey;
  if (mod && e.key.toLowerCase() === 'z') {
    e.preventDefault();
    sock.send({ t: e.shiftKey ? 'redo' : 'undo' });
  } else if ((e.key === 'Backspace' || e.key === 'Delete') && selection.length) {
    e.preventDefault();
    deleteSelection();
  } else if (e.key === 'Escape') {
    if (preview) exitPreview();
    else ink.clearSelection();
  } else if (e.key === 'ArrowRight' || e.key === 'ArrowDown' || e.key === 'PageDown') {
    const i = state.pages.findIndex((p) => p.id === ink.pageId);
    if (state.pages[i + 1]) goToPage(state.pages[i + 1].id);
  } else if (e.key === 'ArrowLeft' || e.key === 'ArrowUp' || e.key === 'PageUp') {
    const i = state.pages.findIndex((p) => p.id === ink.pageId);
    if (state.pages[i - 1]) goToPage(state.pages[i - 1].id);
  } else if (e.key === ' ') {
    ink.spaceDown = true;
    e.preventDefault();
  } else if (!mod && e.key.length === 1) {
    const tool = { p: 'pen', n: 'pencil', h: 'highlighter', e: 'eraser', l: 'lasso', k: 'laser' }[e.key.toLowerCase()];
    if (tool) chooseTool(tool);
    if (e.key === '0') ink.fit('width');
    if (e.key === '9') ink.fit('page');
  }
});
document.addEventListener('keyup', (e) => {
  if (e.key === ' ') ink.spaceDown = false;
});

// ------------------------------------------------------------------ start
applyTool();
loadDevices();
setInterval(() => !status.phone && loadDevices(), 5000);
window.addEventListener('focus', loadDevices);
get('/api/pen/settings').then((s) => { settings = s; }).catch(() => {});

