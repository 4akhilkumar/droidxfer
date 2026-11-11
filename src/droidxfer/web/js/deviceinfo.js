// "Phone info" dialog: hardware, software and battery details plus firmware authenticity checks.

import { api } from './api.js';
import { el, toast } from './util.js';

const MARK = { pass: '✓', warn: '!', fail: '✕', unknown: '?' };
const VERDICT = { pass: 'Looks genuine', warn: 'Needs attention', fail: 'Possible tampering', unknown: 'Not checked' };

export class DeviceInfoDialog {
  constructor() {
    this.report = null;
    this.deviceId = null;
    this.titleEl = el('h3');
    this.body = el('div', { class: 'info-body' });
    this.refreshBtn = el('button', { class: 'btn small', type: 'button', onclick: () => this.load() }, 'Refresh');
    this.copyBtn = el('button', { class: 'btn small', type: 'button', onclick: () => this.copy(), disabled: true }, 'Copy as text');
    this.dialog = el(
      'dialog',
      { id: 'dlg-info', class: 'wide info' },
      el(
        'div',
        { class: 'dlg-head' },
        this.titleEl,
        el('div', { class: 'info-actions' }, this.copyBtn, this.refreshBtn,
          el('button', { class: 'btn small', type: 'button', onclick: () => this.dialog.close() }, 'Close')),
      ),
      this.body,
    );
    document.body.append(this.dialog);
  }

  open(device) {
    this.deviceId = device.id;
    this.titleEl.textContent = device.name || 'Phone info';
    if (!this.dialog.open) this.dialog.showModal();
    this.load();
  }

  async load() {
    const deviceId = this.deviceId;
    this.copyBtn.disabled = true;
    this.refreshBtn.disabled = true;
    this.body.replaceChildren(el('div', { class: 'info-loading' }, 'Reading phone details…'));
    try {
      const report = await api.get(`/api/devices/${encodeURIComponent(deviceId)}/info`);
      if (deviceId !== this.deviceId) return;
      this.report = report;
      this.render(report);
      this.copyBtn.disabled = false;
    } catch (err) {
      this.body.replaceChildren(el('div', { class: 'info-error' }, `Could not read phone details: ${err.message}`));
    } finally {
      this.refreshBtn.disabled = false;
    }
  }

  render(report) {
    this.titleEl.textContent = report.name;
    const banner = el(
      'div',
      { class: `info-verdict ${report.verdict}` },
      el('span', { class: 'info-mark' }, MARK[report.verdict]),
      el('div', {}, el('strong', {}, VERDICT[report.verdict]), el('div', {}, report.message)),
    );
    const checks = el(
      'ul',
      { class: 'info-checks' },
      report.checks.map((c) =>
        el(
          'li',
          { class: c.status },
          el('span', { class: 'info-mark', title: c.status }, MARK[c.status]),
          el('div', {}, el('div', { class: 'info-check-head' }, el('strong', {}, c.label), el('span', {}, c.value)),
            el('div', { class: 'info-detail' }, c.detail)),
        ),
      ),
    );
    const sections = report.sections.map((s) =>
      el(
        'section',
        { class: 'info-section' },
        el('h4', {}, s.title),
        el(
          'dl',
          {},
          s.rows.flatMap((r) => [
            el('dt', { title: r.hint || null }, r.label),
            el('dd', { class: r.value === 'Not available' ? 'na' : null, title: r.hint || null }, r.value),
          ]),
        ),
      ),
    );
    const notes = el('ul', { class: 'info-notes' }, report.notes.map((n) => el('li', {}, n)));
    this.body.replaceChildren(
      banner,
      ...(report.checks.length ? [el('h4', {}, 'Authenticity'), checks] : []),
      el('div', { class: 'info-grid' }, sections),
      notes,
    );
  }

  async copy() {
    const r = this.report;
    if (!r) return;
    const lines = [r.name, `${VERDICT[r.verdict]}: ${r.message}`, '', 'Authenticity'];
    for (const c of r.checks) lines.push(`  [${c.status}] ${c.label}: ${c.value}`);
    for (const s of r.sections) {
      lines.push('', s.title);
      for (const row of s.rows) lines.push(`  ${row.label}: ${row.value}`);
    }
    try {
      await navigator.clipboard.writeText(lines.join('\n'));
      toast('Phone details copied', 'success');
    } catch (err) {
      toast(`Could not copy: ${err.message}`, 'error');
    }
  }
}
