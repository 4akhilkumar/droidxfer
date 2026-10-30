// "Connect over Wi-Fi" dialog: QR pairing, code pairing, connect, and tcpip-from-USB.

import { api } from './api.js';
import { el, formatDate, toast } from './util.js';

const $ = (sel, root = document) => root.querySelector(sel);

export class WirelessDialog {
  constructor(getState) {
    this.getState = getState;
    this.dialog = $('#dlg-wireless');
    this.qrName = null;

    for (const tab of this.dialog.querySelectorAll('[data-tab]')) {
      tab.addEventListener('click', () => this.showTab(tab.dataset.tab));
    }
    $('[data-close]', this.dialog).addEventListener('click', () => this.dialog.close());
    this.dialog.addEventListener('close', () => this.stopQr());

    $('#qr-start').addEventListener('click', () => this.startQr());
    $('#form-pair').addEventListener('submit', (e) => this.pair(e));
    $('#form-connect').addEventListener('submit', (e) => this.connect(e));
    $('#btn-discover').addEventListener('click', () => this.discover());
    $('#btn-tcpip').addEventListener('click', () => this.tcpip());
  }

  open(tab = 'qr') {
    this.showTab(tab);
    this.dialog.showModal();
  }

  showTab(name) {
    for (const tab of this.dialog.querySelectorAll('[data-tab]')) tab.classList.toggle('active', tab.dataset.tab === name);
    for (const panel of this.dialog.querySelectorAll('[data-panel]')) panel.hidden = panel.dataset.panel !== name;
    if (name === 'connect') {
      this.renderKnown();
      this.renderConnected();
      this.discover();
    }
    if (name === 'usb') this.renderUsb();
  }

  busy(button, on) {
    button.disabled = on;
    button.classList.toggle('busy', on);
  }

  // ------------------------------------------------------------------- QR
  async startQr() {
    const button = $('#qr-start');
    this.busy(button, true);
    await this.stopQr();
    try {
      const session = await api.post('/api/wireless/qr');
      this.qrName = session.name;
      // The SVG is generated server-side by segno from a random name/password, not user input.
      $('#qr-box').innerHTML = session.svg;
      this.setQrStatus(session);
    } catch (err) {
      toast(err.message, 'error');
    } finally {
      this.busy(button, false);
    }
  }

  async stopQr() {
    if (!this.qrName) return;
    const name = this.qrName;
    this.qrName = null;
    $('#qr-box').replaceChildren();
    $('#qr-status').textContent = '';
    try {
      await api.del(`/api/wireless/qr/${encodeURIComponent(name)}`);
    } catch {
      /* already finished */
    }
  }

  onQrStatus(data) {
    if (data.name !== this.qrName) return;
    this.setQrStatus(data);
    if (data.state === 'connected') {
      toast('Connected over Wi-Fi', 'success');
      $('#qr-box').replaceChildren();
      this.qrName = null;
    }
  }

  setQrStatus(data) {
    const line = $('#qr-status');
    line.className = `status-line ${data.state}`;
    line.textContent = data.message;
  }

  // ---------------------------------------------------------- pair / connect
  async pair(e) {
    e.preventDefault();
    const form = e.target;
    const button = $('button', form);
    this.busy(button, true);
    try {
      const { message } = await api.post('/api/wireless/pair', {
        host: form.elements.host.value.trim(),
        port: Number(form.elements.port.value),
        code: form.elements.code.value.trim(),
      });
      toast(message, 'success', 7000);
      $('#form-connect').elements.host.value = form.elements.host.value.trim();
      form.elements.code.value = '';
      this.showTab('connect');
    } catch (err) {
      toast(err.message, 'error', 8000);
    } finally {
      this.busy(button, false);
    }
  }

  async connect(e) {
    e.preventDefault();
    const form = e.target;
    await this.connectTo(form.elements.host.value.trim(), Number(form.elements.port.value), $('button', form));
  }

  async connectTo(host, port, button) {
    if (button) this.busy(button, true);
    try {
      const { message } = await api.post('/api/wireless/connect', { host, port });
      toast(message, 'success');
      this.renderKnown();
    } catch (err) {
      toast(err.message, 'error', 8000);
    } finally {
      if (button) this.busy(button, false);
    }
  }

  async discover() {
    const list = $('#discover-list');
    list.replaceChildren(el('li', { class: 'muted' }, 'Scanning\u2026'));
    try {
      const services = await api.get('/api/wireless/discover');
      list.replaceChildren();
      if (!services.length) {
        list.append(el('li', { class: 'muted' }, 'Nothing found. Turn on Wireless debugging on the phone and keep that screen open.'));
      }
      for (const s of services) {
        const action =
          s.type === 'pairing'
            ? el('button', { class: 'btn small', type: 'button', onclick: () => {
                const form = $('#form-pair');
                form.elements.host.value = s.host;
                form.elements.port.value = s.port;
                this.showTab('code');
                form.elements.code.focus();
              } }, 'Pair\u2026')
            : el('button', { class: 'btn small', type: 'button', onclick: (ev) => this.connectTo(s.host, s.port, ev.currentTarget) }, 'Connect');
        list.append(el('li', {}, el('span', {}, el('strong', {}, s.name), ` ${s.host}:${s.port} (${s.type})`), action));
      }
    } catch (err) {
      list.replaceChildren(el('li', { class: 'muted' }, `Discovery failed: ${err.message}`));
    }
  }

  async renderKnown() {
    const list = $('#known-list');
    try {
      const known = await api.get('/api/wireless/known');
      list.replaceChildren();
      if (!known.length) list.append(el('li', { class: 'muted' }, 'No saved endpoints yet.'));
      for (const k of known) {
        const label = `${k.name ? `${k.name} \u2014 ` : ''}${k.host}:${k.port}`;
        list.append(
          el(
            'li',
            {},
            el('span', { title: k.last_seen ? `Last used ${formatDate(k.last_seen)}` : '' }, label),
            el('span', { class: 'row-actions' },
              el('button', { class: 'btn small', type: 'button', onclick: (ev) => this.connectTo(k.host, k.port, ev.currentTarget) }, 'Connect'),
              el('button', { class: 'btn small', type: 'button', onclick: async () => {
                await api.post('/api/wireless/forget', { host: k.host, port: k.port }).catch((err) => toast(err.message, 'error'));
                this.renderKnown();
              } }, 'Forget'),
            ),
          ),
        );
      }
    } catch (err) {
      list.replaceChildren(el('li', { class: 'muted' }, err.message));
    }
  }

  renderConnected() {
    const list = $('#wifi-list');
    list.replaceChildren();
    const transports = this.getState().devices.flatMap((d) =>
      d.transports.filter((t) => t.kind === 'wifi').map((t) => ({ ...t, name: d.name })),
    );
    if (!transports.length) list.append(el('li', { class: 'muted' }, 'No Wi-Fi connections.'));
    for (const t of transports) {
      list.append(
        el('li', {}, el('span', {}, `${t.name} \u2014 ${t.serial} (${t.state})`),
          el('button', { class: 'btn small', type: 'button', onclick: async () => {
            try {
              toast((await api.post('/api/wireless/disconnect', { serial: t.serial })).message);
            } catch (err) {
              toast(err.message, 'error');
            }
          } }, 'Disconnect')),
      );
    }
  }

  onDevices() {
    if (!this.dialog.open) return;
    const active = $('[data-tab].active', this.dialog)?.dataset.tab;
    if (active === 'connect') this.renderConnected();
    if (active === 'usb') this.renderUsb();
  }

  // ------------------------------------------------------------------ USB
  usbDevice() {
    const { devices, deviceId } = this.getState();
    const device = devices.find((d) => d.id === deviceId);
    const usb = device?.transports.find((t) => t.kind === 'usb' && t.state === 'device');
    return usb ? device : null;
  }

  renderUsb() {
    const device = this.usbDevice();
    $('#usb-device').textContent = device
      ? `Selected phone: ${device.name} (USB)`
      : 'Select a phone that is connected by USB first.';
    $('#btn-tcpip').disabled = !device;
  }

  async tcpip() {
    const device = this.usbDevice();
    if (!device) return;
    const button = $('#btn-tcpip');
    this.busy(button, true);
    try {
      const { message } = await api.post(`/api/devices/${encodeURIComponent(device.id)}/tcpip`, { port: 5555 });
      toast(`${message}. You can unplug the cable now.`, 'success', 8000);
    } catch (err) {
      toast(err.message, 'error', 8000);
    } finally {
      this.busy(button, false);
    }
  }
}
