// "Logs" window with two tabs, both using the matrix_sols-style LogViewer:
//   * droidxfer activity - droidxfer's own log file (transfers, connections, Wi-Fi, app changes, errors)
//   * Phone log          - the phone's system log (logcat), with ready-made filters and plain-words notes

import { api, qs } from './api.js';
import { confirmDialog } from './dialogs.js';
import { LogViewer } from './logviewer.js';
import { el, formatBytes, toast } from './util.js';

const LEVEL_TEXT = { DEBUG: 'Detailed', INFO: 'Normal', WARNING: 'Problems only', ERROR: 'Errors only' };

// ----------------------------------------------------------------- droidxfer's own log
function activitySource(fileLine) {
  const url = (params) => `/api/logs/app?${qs(params)}`;
  return {
    key: 'app',
    groupLabel: 'All areas',
    streamLabel: 'All source files',
    groupHint: 'Log groups: one per area of droidxfer (transfer, devices, wireless, apps, api…)',
    streamHint: 'Log streams: the source file that wrote the entry',
    streamNoun: 'this file',
    emptyText: 'No activity matches this query. droidxfer records what it does here: transfers, phones connecting, Wi-Fi, app changes and errors.',
    async meta() {
      const meta = await api.get(url({ action: 'meta' }));
      this.onFile(meta.file, meta.log_level);
      return meta;
    },
    onFile(file, level) {
      if (level) this.level = level;
      fileLine.replaceChildren(
        file
          ? `${file.path} · ${formatBytes(file.size)} · updated ${file.modified}`
          : 'No log file yet: it is created when droidxfer first records something.',
        this.level ? ` · recording: ${LEVEL_TEXT[this.level] || this.level} (change in Settings)` : '',
      );
    },
    search: (query, cursor) => api.get(url({ ...query, action: 'search', cursor: cursor || undefined })),
    histogram: (query) => api.get(url({ ...query, action: 'histogram' })),
    downloadUrl: (query) => url({ ...query, action: 'download' }),
    tail(query, h) {
      let offset = null;
      let inode = null;
      let busy = false;
      let stopped = false;
      const poll = async () => {
        if (stopped || busy || document.hidden) return;
        busy = true;
        const initial = offset === null;
        try {
          const data = await api.get(url({ ...query, action: 'tail', offset: initial ? undefined : offset, inode: initial ? undefined : inode }));
          if (stopped) return;
          offset = data.offset;
          inode = data.inode;
          if (data.rotated) h.notice('The log file was rotated: following the new file.');
          if (data.skipped_bytes) h.notice(`The log is growing fast: skipped ${formatBytes(data.skipped_bytes)} to keep up.`);
          h.entries(data.entries, initial, data);
          h.status(`● Live · updated ${new Date().toLocaleTimeString()}`);
        } catch (err) {
          h.status(`Error: ${err.message}`);
        } finally {
          busy = false;
        }
      };
      poll();
      const timer = setInterval(poll, 2000);
      return () => { stopped = true; clearInterval(timer); };
    },
  };
}

// ----------------------------------------------------------------- the phone's log
function phoneSource(getDevice, controls) {
  const base = () => `/api/devices/${encodeURIComponent(getDevice().id)}/logs`;
  const source = {
    key: 'phone',
    groupLabel: 'All apps',
    streamLabel: 'All tags',
    groupHint: 'Log groups: the app or system process that wrote the line',
    streamHint: 'Log streams: the log tag',
    streamNoun: 'this tag',
    emptyText: 'No lines match. The phone keeps only roughly the last 15–30 minutes: try a ready-made filter, "All" or fewer filters.',
    qid: null,
    meta: () => api.get('/api/logs/phone/meta'),
    extraQuery() {
      return {
        preset: controls.preset.value || undefined,
        app: controls.app.value.trim() || undefined,
        mask: controls.mask.checked ? '1' : '0',
        notes: controls.notes.checked ? '1' : undefined,
      };
    },
    applyExtra(q) {
      controls.preset.value = q.preset || '';
      controls.app.value = q.app || '';
      controls.mask.checked = q.mask !== '0';
      controls.notes.checked = q.notes === '1';
      controls.describe();
    },
    resetExtra() {
      this.applyExtra({});
    },
    async search(query, cursor) {
      const data = await api.get(`${base()}?${qs({ ...query, action: 'search', cursor: cursor || undefined })}`);
      source.qid = data.qid;
      return data;
    },
    downloadUrl: (query) => `${base()}?${qs({ ...query, action: 'download', qid: source.qid || undefined })}`,
    tail(query, h) {
      const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
      const socket = new WebSocket(`${proto}//${location.host}${base()}/live?${qs(query)}`);
      let echo = null;
      let first = true;
      socket.addEventListener('message', (e) => {
        const msg = JSON.parse(e.data);
        if (msg.type === 'started') {
          echo = msg;
          h.status('● Live · waiting for new lines (do the thing you want to check now)');
        } else if (msg.type === 'entries') {
          h.entries(msg.entries, false, first ? echo : null);
          first = false;
          h.status(`● Live · updated ${new Date().toLocaleTimeString()}`);
        } else if (msg.type === 'error') {
          h.error(msg.message);
        }
      });
      socket.addEventListener('close', () => h.status('Disconnected'));
      return () => { if (socket.readyState <= WebSocket.OPEN) socket.close(); };
    },
  };
  return source;
}

// ----------------------------------------------------------------- the window
export class LogsDialog {
  constructor() {
    this.device = null;
    this.viewers = {};
    this.tab = 'app';

    this.fileLine = el('div', { class: 'lv-file' });
    this.activity = new LogViewer(activitySource(this.fileLine), { tailMax: 1000 });

    const preset = el('select', { title: 'Ready-made filters for common problems; matching lines get a plain-words note' },
      el('option', { value: '' }, 'No ready-made filter'));
    const app = el('input', { type: 'search', class: 'lv-app', placeholder: 'One app (package)', spellcheck: 'false',
      title: 'Only one installed app, including its earlier runs, e.g. org.videolan.vlc' });
    const mask = el('input', { type: 'checkbox', checked: true });
    const notes = el('input', { type: 'checkbox' });
    const presetHint = el('div', { class: 'lv-preset-hint' });
    this.phoneControls = {
      preset, app, mask, notes,
      describe: () => {
        const p = this.presets?.find((x) => x.id === preset.value);
        presetHint.textContent = p ? p.description : '';
      },
    };
    this.phone = new LogViewer(phoneSource(() => this.device, this.phoneControls), {
      tailMax: 2000,
      extraControls: [
        preset, app,
        el('label', { class: 'check', title: 'Show only lines with a plain-words explanation' }, notes, 'Explained only'),
        el('label', { class: 'check', title: 'Keep only the last two bytes of Bluetooth and Wi-Fi addresses' }, mask, 'Hide addresses'),
      ],
    });
    const rerunPhone = () => { this.phoneControls.describe(); this.phone.rerun(); };
    preset.addEventListener('change', rerunPhone);
    app.addEventListener('change', rerunPhone);
    mask.addEventListener('change', rerunPhone);
    notes.addEventListener('change', rerunPhone);

    this.phoneBody = el('div', { class: 'lv-tab-body' },
      el('div', { class: 'lv-tab-intro' },
        el('span', {}, 'Read-only: nothing on the phone changes. Reproduce the problem, then search. The phone keeps roughly the last 15–30 minutes.'),
        el('div', { class: 'spacer' }),
        el('button', { type: 'button', class: 'btn small', onclick: () => this.clearPhone(), title: "Empty the phone's log (asks first)" }, 'Clear phone log')),
      presetHint,
      this.phone.root);
    this.phoneMessage = el('div', { class: 'lv-empty', hidden: true });
    this.activityBody = el('div', { class: 'lv-tab-body' },
      el('div', { class: 'lv-tab-intro' }, this.fileLine),
      this.activity.root);

    this.tabBtns = {
      app: el('button', { type: 'button', onclick: () => this.show('app') }, 'droidxfer activity'),
      phone: el('button', { type: 'button', onclick: () => this.show('phone') }, 'Phone log'),
    };
    this.dialog = el('dialog', { id: 'dlg-logs', class: 'logs', onclose: () => { this.activity.close(); this.phone.close(); } },
      el('div', { class: 'dlg-head' },
        el('h3', {}, 'Logs'),
        el('nav', { class: 'tabs lv-tabs' }, this.tabBtns.app, this.tabBtns.phone),
        el('button', { type: 'button', class: 'btn small', onclick: () => this.dialog.close() }, 'Close')),
      this.activityBody, this.phoneBody, this.phoneMessage);
    document.body.append(this.dialog);
    this.started = { app: false, phone: false };
  }

  phoneUsable() {
    return this.device && this.device.state === 'device' && this.device.capabilities?.adb !== false;
  }

  open(device) {
    const changed = this.device?.id !== device?.id;
    this.device = device;
    this.tabBtns.phone.textContent = device ? `Phone log · ${device.name}` : 'Phone log';
    if (changed && this.started.phone) {
      this.phone.close();
      this.started.phone = false;
    }
    if (!this.dialog.open) this.dialog.showModal();
    this.show(this.tab);
  }

  async show(tab) {
    this.tab = tab;
    for (const [k, btn] of Object.entries(this.tabBtns)) btn.classList.toggle('active', k === tab);
    this.activityBody.hidden = tab !== 'app';
    const phoneOk = this.phoneUsable();
    this.phoneBody.hidden = tab !== 'phone' || !phoneOk;
    this.phoneMessage.hidden = tab !== 'phone' || phoneOk;
    if (tab === 'phone' && !phoneOk) {
      this.phoneMessage.textContent = !this.device
        ? 'Connect a phone to read its log.'
        : 'Reading the phone’s log needs USB debugging. This phone is connected in File transfer (MTP) mode or isn’t ready yet.';
      return;
    }
    if (this.started[tab]) return;
    this.started[tab] = true;
    if (tab === 'phone' && !this.presets) {
      try {
        const meta = await api.get('/api/logs/phone/meta');
        this.presets = meta.presets;
        this.phoneControls.preset.append(...meta.presets.map((p) => el('option', { value: p.id, title: p.description }, p.label)));
      } catch { /* the viewer reports errors itself */ }
    }
    (tab === 'app' ? this.activity : this.phone).init();
  }

  async clearPhone() {
    const ok = await confirmDialog({
      title: 'Clear the phone’s log?',
      message: 'This empties the log stored on the phone. It can’t be undone, and searches will only find what happens from now on. Nothing else on the phone changes.',
      okLabel: 'Clear phone log',
      danger: true,
    });
    if (!ok) return;
    try {
      await api.post(`/api/devices/${encodeURIComponent(this.device.id)}/logs/clear`);
      toast('Phone log cleared', 'success');
      this.phone.rerun();
    } catch (err) {
      toast(err.message, 'error', 8000);
    }
  }
}
