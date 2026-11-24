// WebSocket link to the Mac. Saved changes go through an outbox that survives disconnects (and, on the
// phone, page reloads): they are resent until the server confirms them, and the server ignores repeats.

import { newId } from './model.js';

export class PenSocket {
  constructor(path, { persistKey = null } = {}) {
    this.path = path;
    this.persistKey = persistKey;
    this.handlers = new Map();
    this.outbox = [];
    this.pendingActions = new Map();
    this.delay = 400;
    this.open = false;
    this.stopped = false;
    if (persistKey) {
      try {
        this.outbox = JSON.parse(localStorage.getItem(persistKey) || '[]');
      } catch {
        this.outbox = [];
      }
    }
    this.connect();
    setInterval(() => this.open && this.raw({ t: 'ping', ts: Date.now() }), 15000);
  }

  on(type, fn) {
    if (!this.handlers.has(type)) this.handlers.set(type, []);
    this.handlers.get(type).push(fn);
  }

  emit(type, data) {
    for (const fn of this.handlers.get(type) || []) fn(data);
  }

  connect() {
    if (this.stopped) return;
    const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
    const ws = new WebSocket(`${proto}//${location.host}${this.path}`);
    this.ws = ws;
    ws.addEventListener('open', () => {
      this.open = true;
      this.delay = 400;
      this.emit('connection', true);
    });
    ws.addEventListener('message', (e) => {
      let msg;
      try {
        msg = JSON.parse(e.data);
      } catch {
        return;
      }
      if (msg.t === 'state') this.flush();
      if ((msg.t === 'applied' || msg.t === 'ack' || msg.t === 'rejected') && msg.cid) this.confirm(msg.cid);
      if (msg.t === 'result') {
        const pending = this.pendingActions.get(msg.id);
        if (pending) {
          this.pendingActions.delete(msg.id);
          clearTimeout(pending.timer);
          if (msg.ok) pending.resolve(msg.result);
          else pending.reject(new Error(msg.error));
        }
        return;
      }
      this.emit(msg.t, msg);
    });
    ws.addEventListener('close', (e) => {
      this.open = false;
      this.emit('connection', false);
      for (const [id, pending] of this.pendingActions) {
        clearTimeout(pending.timer);
        pending.reject(new Error('Lost the connection to the Mac'));
        this.pendingActions.delete(id);
      }
      if (e.code === 4401 || e.code === 4403) {
        this.stopped = true;
        this.emit('expired', e.code);
        return;
      }
      setTimeout(() => this.connect(), this.delay);
      this.delay = Math.min(this.delay * 1.6, 5000);
    });
  }

  raw(msg) {
    if (!this.open) return false;
    try {
      this.ws.send(JSON.stringify(msg));
      return true;
    } catch {
      return false;
    }
  }

  // Fire-and-forget (live strokes, hover, laser): dropped while offline.
  send(msg) {
    this.raw(msg);
  }

  persist() {
    if (!this.persistKey) return;
    try {
      localStorage.setItem(this.persistKey, JSON.stringify(this.outbox.slice(-500)));
    } catch {
      /* storage full or unavailable */
    }
  }

  // A saved change: kept until the Mac confirms it.
  sendOp(op, extra = {}) {
    const msg = { t: 'op', cid: newId(), op, ...extra };
    this.outbox.push(msg);
    this.persist();
    this.raw(msg);
    this.emit('outbox', this.outbox.length);
    return msg.cid;
  }

  confirm(cid) {
    const before = this.outbox.length;
    this.outbox = this.outbox.filter((m) => m.cid !== cid);
    if (this.outbox.length !== before) {
      this.persist();
      this.emit('outbox', this.outbox.length);
    }
  }

  flush() {
    for (const msg of this.outbox) this.raw(msg);
  }

  action(name, args = {}, timeout = 60000) {
    return new Promise((resolve, reject) => {
      if (!this.open) {
        reject(new Error('Not connected to the Mac'));
        return;
      }
      const id = newId();
      const timer = setTimeout(() => {
        this.pendingActions.delete(id);
        reject(new Error('The Mac took too long to answer'));
      }, timeout);
      this.pendingActions.set(id, { resolve, reject, timer });
      this.raw({ t: 'action', id, name, args });
    });
  }
}
