// "Logs" dialog: read the phone's system log (logcat), live or what's already stored, with ready-made filters
// that explain common events in plain words (headphone buttons, Bluetooth, crashes, audio interruptions).

import { api, qs } from './api.js';
import { confirmDialog } from './dialogs.js';
import { el, toast } from './util.js';

const MAX_LIVE = 5000; // lines kept on screen in live mode (oldest dropped)
const LEVEL_OPTIONS = [['V', 'All levels'], ['I', 'Info and above'], ['W', 'Warnings and errors'], ['E', 'Errors only']];

const pad = (n, w = 2) => String(n).padStart(w, '0');
function clock(seconds) {
  const d = new Date(seconds * 1000);
  return `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}.${pad(d.getMilliseconds(), 3)}`;
}
function stamp(seconds) {
  const d = new Date(seconds * 1000);
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${clock(seconds)}`;
}

export class LogsDialog {
  constructor() {
    this.device = null;
    this.lines = [];
    this.mode = 'recent';
    this.socket = null;
    this.loadSeq = 0;
    this.presetsInfo = null;

    const select = (options, onchange) =>
      el('select', { onchange }, options.map(([v, label]) => el('option', { value: v }, label)));
    this.modeBtns = {
      recent: el('button', { type: 'button', onclick: () => this.setMode('recent'), title: 'Logs already stored on the phone' }, 'What just happened'),
      live: el('button', { type: 'button', onclick: () => this.setMode('live'), title: 'New lines as the phone writes them' }, 'Live'),
    };
    this.preset = select([['', 'All logs']], () => this.changed());
    this.window = select([], () => this.changed());
    this.level = select(LEVEL_OPTIONS, () => this.changed());
    this.app = el('input', { type: 'search', placeholder: 'One app (package name)', spellcheck: 'false', onchange: () => this.changed() });
    this.search = el('input', { type: 'search', placeholder: 'Search text', spellcheck: 'false', onchange: () => this.changed() });
    this.mask = el('input', { type: 'checkbox', checked: true, onchange: () => this.changed() });
    this.notesOnly = el('input', { type: 'checkbox', onchange: () => this.render() });
    this.presetHint = el('div', { class: 'logs-preset-hint' });
    this.listEl = el('div', { class: 'logs-list', role: 'log' });
    this.statusEl = el('span', { class: 'logs-status' });
    this.actionBtn = el('button', { class: 'btn primary', type: 'button', onclick: () => this.primaryAction() }, 'Reload');
    this.titleEl = el('h3', {}, 'Logs');

    this.dialog = el(
      'dialog',
      { id: 'dlg-logs', class: 'logs', onclose: () => this.stopLive() },
      el('div', { class: 'dlg-head' }, this.titleEl,
        el('div', { class: 'info-actions' },
          el('button', { class: 'btn small', type: 'button', onclick: () => this.save(), title: 'Save the lines shown below to a text file on this Mac' }, 'Save…'),
          el('button', { class: 'btn small', type: 'button', onclick: () => this.clearPhone(), title: "Empty the phone's log (asks first)" }, 'Clear phone log'),
          el('button', { class: 'btn small', type: 'button', onclick: () => this.dialog.close() }, 'Close'))),
      el('p', { class: 'apps-hint' },
        'Read-only: this never changes anything on the phone. Tip: reproduce the problem, then open "What just happened" with a ready-made filter. Logs can contain personal details (device names, account IDs), so check a saved file before sharing it.'),
      el('div', { class: 'logs-controls' },
        el('div', { class: 'segmented', role: 'group' }, this.modeBtns.recent, this.modeBtns.live),
        this.preset, this.window, this.level, this.app, this.search),
      el('div', { class: 'logs-controls' },
        this.presetHint, el('div', { class: 'spacer' }),
        el('label', { class: 'check', title: 'Show only lines with a plain-words explanation' }, this.notesOnly, 'Explained lines only'),
        el('label', { class: 'check', title: 'Show only the last two bytes of Bluetooth and Wi-Fi addresses' }, this.mask, 'Hide device addresses')),
      this.listEl,
      el('div', { class: 'apps-actions' }, this.statusEl, el('div', { class: 'spacer' }),
        el('button', { class: 'btn', type: 'button', onclick: () => this.clearView(), title: 'Empty this view (the phone keeps its log)' }, 'Clear view'),
        this.actionBtn),
    );
    document.body.append(this.dialog);
  }

  async open(device) {
    this.device = device;
    this.titleEl.textContent = `Logs on ${device.name}`;
    if (!this.dialog.open) this.dialog.showModal();
    if (!this.presetsInfo) {
      try {
        this.presetsInfo = await api.get('/api/logs/presets');
        this.preset.append(...this.presetsInfo.presets.map((p) => el('option', { value: p.id, title: p.description }, p.label)));
        this.window.append(
          ...this.presetsInfo.windows.map((m) => el('option', { value: m }, `Last ${m} min`)),
          el('option', { value: 0 }, 'Everything stored'),
        );
        this.window.value = String(this.presetsInfo.default_window);
      } catch (err) {
        this.listEl.replaceChildren(el('div', { class: 'info-error' }, `Could not load filters: ${err.message}`));
        return;
      }
    }
    this.setMode(this.mode);
  }

  url(suffix = '') {
    return `/api/devices/${encodeURIComponent(this.device.id)}/logs${suffix}`;
  }

  params() {
    return {
      preset: this.preset.value || undefined,
      level: this.level.value,
      q: this.search.value.trim() || undefined,
      app: this.app.value.trim() || undefined,
      mask: this.mask.checked,
    };
  }

  setMode(mode) {
    this.mode = mode;
    for (const [m, btn] of Object.entries(this.modeBtns)) btn.classList.toggle('active', m === mode);
    this.window.hidden = mode === 'live';
    this.stopLive();
    this.clearView();
    if (mode === 'recent') this.load();
    else this.startLive();
  }

  changed() {
    const preset = this.presetsInfo?.presets.find((p) => p.id === this.preset.value);
    this.presetHint.textContent = preset ? preset.description : '';
    if (this.mode === 'recent') this.load();
    else if (this.socket) this.startLive();
    else this.render();
  }

  primaryAction() {
    if (this.mode === 'recent') this.load();
    else if (this.socket) this.stopLive();
    else this.startLive();
  }

  // ------------------------------------------------------------- what just happened
  async load() {
    const seq = ++this.loadSeq;
    this.actionBtn.disabled = true;
    this.statusEl.textContent = '';
    this.listEl.replaceChildren(el('div', { class: 'info-loading' }, 'Reading the phone’s log…'));
    try {
      const res = await api.get(`${this.url()}?${qs({ ...this.params(), minutes: this.window.value })}`);
      if (seq !== this.loadSeq) return;
      this.lines = res.lines;
      this.result = res;
      this.render();
    } catch (err) {
      if (seq !== this.loadSeq) return;
      this.lines = [];
      this.listEl.replaceChildren(el('div', { class: 'info-error' }, `Could not read the log: ${err.message}`));
    } finally {
      if (seq === this.loadSeq) this.actionBtn.disabled = false;
    }
  }

  // ------------------------------------------------------------------------- live
  startLive() {
    this.stopLive();
    this.result = null;
    const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
    const query = qs(this.params());
    const socket = new WebSocket(`${proto}//${location.host}${this.url('/live')}${query ? `?${query}` : ''}`);
    this.socket = socket;
    this.actionBtn.textContent = 'Stop';
    this.actionBtn.disabled = false;
    if (!this.lines.length) this.listEl.replaceChildren(el('div', { class: 'info-loading' }, 'Waiting for new lines… (do the thing you want to check now)'));
    this.updateStatus();
    socket.addEventListener('message', (e) => {
      if (socket !== this.socket) return;
      const msg = JSON.parse(e.data);
      if (msg.type === 'lines') this.append(msg.lines);
      else if (msg.type === 'error') {
        toast(`Live log stopped: ${msg.message}`, 'error', 8000);
        this.stopLive();
      }
    });
    socket.addEventListener('close', () => {
      if (socket === this.socket) this.stopLive();
    });
  }

  stopLive() {
    const socket = this.socket;
    this.socket = null;
    if (socket && socket.readyState <= WebSocket.OPEN) socket.close();
    if (this.mode === 'live') {
      this.actionBtn.textContent = 'Start';
      this.updateStatus();
    } else {
      this.actionBtn.textContent = 'Reload';
    }
  }

  append(lines) {
    const atBottom = this.listEl.scrollHeight - this.listEl.scrollTop - this.listEl.clientHeight < 40;
    if (!this.lines.length) this.listEl.replaceChildren();
    this.lines.push(...lines);
    let dropped = 0;
    if (this.lines.length > MAX_LIVE) {
      dropped = this.lines.length - MAX_LIVE;
      this.lines.splice(0, dropped);
    }
    if (this.notesOnly.checked) {
      this.render(); // rows on screen don't map 1:1 to this.lines; rebuild (few rows have notes)
      return;
    }
    for (let i = 0; i < dropped && this.listEl.firstChild; i++) this.listEl.firstChild.remove();
    this.listEl.append(...lines.map((l) => this.row(l)));
    if (atBottom) this.listEl.scrollTop = this.listEl.scrollHeight;
    this.updateStatus();
  }

  // ------------------------------------------------------------------------ view
  row(line) {
    return el(
      'div',
      { class: `logs-row lvl-${line.level}${line.note ? ' has-note' : ''}` },
      el('span', { class: 'logs-time', title: stamp(line.time) }, clock(line.time)),
      el('span', { class: 'logs-level', title: `pid ${line.pid}, thread ${line.tid}` }, line.level),
      el('span', { class: 'logs-tag', title: line.tag }, line.tag),
      el('span', { class: 'logs-msg' }, line.message, line.note ? el('span', { class: 'logs-note' }, line.note) : null),
    );
  }

  visibleLines() {
    return this.notesOnly.checked ? this.lines.filter((l) => l.note) : this.lines;
  }

  render() {
    const lines = this.visibleLines();
    if (!lines.length) {
      const empty = this.mode === 'live'
        ? (this.socket ? 'Waiting for new lines… (do the thing you want to check now)' : 'Press Start to follow the log live.')
        : 'No matching lines. Try a longer time range, a different filter or "All levels".';
      this.listEl.replaceChildren(el('div', { class: 'info-loading' }, empty));
    } else {
      this.listEl.replaceChildren(...lines.map((l) => this.row(l)));
      this.listEl.scrollTop = this.listEl.scrollHeight;
    }
    this.updateStatus();
  }

  updateStatus() {
    const shown = this.visibleLines().length.toLocaleString();
    if (this.mode === 'live') {
      this.statusEl.textContent = `${this.socket ? 'Following live' : 'Stopped'} · ${shown} line${shown === '1' ? '' : 's'}`
        + (this.lines.length >= MAX_LIVE ? ` (only the newest ${MAX_LIVE.toLocaleString()} kept)` : '');
    } else if (this.result) {
      const { matched, truncated, limit } = this.result;
      this.statusEl.textContent = truncated
        ? `Showing the newest ${limit.toLocaleString()} of ${matched.toLocaleString()} matching lines. Narrow the filter to see older ones.`
        : `${shown} line${shown === '1' ? '' : 's'}`;
    } else {
      this.statusEl.textContent = '';
    }
  }

  clearView() {
    this.lines = [];
    this.result = null;
    this.render();
  }

  // --------------------------------------------------------------------- actions
  save() {
    const lines = this.visibleLines();
    if (!lines.length) {
      toast('Nothing to save: no lines are shown.');
      return;
    }
    const filters = [
      this.preset.value && `filter: ${this.preset.selectedOptions[0].textContent}`,
      this.mode === 'recent' && `range: ${this.window.selectedOptions[0].textContent}`,
      this.level.value !== 'V' && `level: ${this.level.selectedOptions[0].textContent}`,
      this.app.value.trim() && `app: ${this.app.value.trim()}`,
      this.search.value.trim() && `search: "${this.search.value.trim()}"`,
      this.notesOnly.checked && 'explained lines only',
    ].filter(Boolean);
    const header = [
      `droidxfer log · ${this.device.name} · saved ${stamp(Date.now() / 1000)}`,
      filters.length ? filters.join(' · ') : 'all logs',
      '',
    ];
    const body = lines.map((l) => {
      const text = `${stamp(l.time)} ${String(l.pid).padStart(5)} ${String(l.tid).padStart(5)} ${l.level} ${l.tag}: ${l.message}`;
      return l.note ? `${text}    <-- ${l.note}` : text;
    });
    const blob = new Blob([[...header, ...body, ''].join('\n')], { type: 'text/plain' });
    const d = new Date();
    const name = `droidxfer-log-${(this.device.model || this.device.name || 'phone').replace(/\W+/g, '-')}-${d.getFullYear()}${pad(d.getMonth() + 1)}${pad(d.getDate())}-${pad(d.getHours())}${pad(d.getMinutes())}.txt`;
    const link = el('a', { href: URL.createObjectURL(blob), download: name });
    document.body.append(link);
    link.click();
    link.remove();
    setTimeout(() => URL.revokeObjectURL(link.href), 1000);
  }

  async clearPhone() {
    const ok = await confirmDialog({
      title: 'Clear the phone’s log?',
      message: 'This empties the log stored on the phone. It can’t be undone, and "What just happened" will only show what happens from now on. Nothing else on the phone changes.',
      okLabel: 'Clear phone log',
      danger: true,
    });
    if (!ok) return;
    try {
      await api.post(this.url('/clear'));
      toast('Phone log cleared', 'success');
      if (this.mode === 'recent') this.load();
    } catch (err) {
      toast(err.message, 'error', 8000);
    }
  }
}
