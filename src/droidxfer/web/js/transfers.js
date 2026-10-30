// The transfer queue panel at the bottom of the window.

import { api } from './api.js';
import { el, formatBytes, formatDuration, icon, toast } from './util.js';

const FINISHED = new Set(['completed', 'failed', 'cancelled']);

export class TransfersPanel {
  constructor(root, { onFinished }) {
    this.root = root;
    this.list = root.querySelector('#transfers-list');
    this.countEl = root.querySelector('#transfers-count');
    this.onFinished = onFinished;
    this.jobs = new Map();
    this.rows = new Map();
    root.querySelector('#transfers-toggle').addEventListener('click', () => root.classList.toggle('collapsed'));
    root.querySelector('#btn-clear').addEventListener('click', async () => {
      try {
        const { removed } = await api.post('/api/transfers/clear');
        this.remove(removed);
      } catch (err) {
        toast(err.message, 'error');
      }
    });
    this.empty = el('div', { class: 'empty small' }, 'No transfers yet. Drag files between the panes or use the arrow buttons.');
    this.list.append(this.empty);
  }

  expand() {
    this.root.classList.remove('collapsed');
  }

  setAll(jobs) {
    for (const row of this.rows.values()) row.root.remove();
    this.rows.clear();
    this.jobs.clear();
    for (const job of jobs) this.upsert(job, { silent: true });
    this.updateCount();
  }

  upsert(job, { silent = false } = {}) {
    const previous = this.jobs.get(job.id);
    this.jobs.set(job.id, job);
    let row = this.rows.get(job.id);
    if (!row) {
      row = this.createRow(job);
      this.rows.set(job.id, row);
      this.list.prepend(row.root);
    }
    this.updateRow(row, job);
    if (!silent && previous && previous.status !== job.status && FINISHED.has(job.status)) this.onFinished(job);
    this.updateCount();
  }

  remove(ids) {
    for (const id of ids) {
      this.rows.get(id)?.root.remove();
      this.rows.delete(id);
      this.jobs.delete(id);
    }
    this.updateCount();
  }

  updateCount() {
    const active = [...this.jobs.values()].filter((j) => !FINISHED.has(j.status)).length;
    this.countEl.textContent = active ? `(${active} active)` : this.jobs.size ? `(${this.jobs.size})` : '';
    this.empty.hidden = this.jobs.size > 0;
  }

  createRow(job) {
    const row = {
      title: el('div', { class: 'job-title' }),
      dest: el('div', { class: 'job-dest' }),
      fill: el('div', { class: 'fill' }),
      stats: el('div', { class: 'job-stats' }),
      actions: el('div', { class: 'job-actions' }),
      failures: el('details', { class: 'job-failures', hidden: true }),
      status: null,
    };
    row.bar = el('div', { class: 'bar' }, row.fill);
    row.root = el(
      'div',
      { class: 'job', dataset: { id: job.id } },
      el('div', { class: 'job-main' }, row.title, row.dest, row.bar, row.stats, row.failures),
      row.actions,
    );
    return row;
  }

  updateRow(row, job) {
    const arrow = job.direction === 'push' ? 'Mac \u2192 Android' : 'Android \u2192 Mac';
    row.title.replaceChildren(el('span', { class: `dir ${job.direction}` }, arrow), ' ', job.title);
    row.dest.textContent = `to ${job.dest_dir}`;
    row.dest.title = job.dest_dir;
    row.root.className = `job ${job.status}`;

    const finished = FINISHED.has(job.status);
    const pct = job.total_bytes ? job.bytes_done / job.total_bytes : finished && job.status === 'completed' ? 1 : 0;
    row.fill.style.width = `${Math.min(100, Math.max(0, pct * 100)).toFixed(1)}%`;
    row.bar.classList.toggle('indeterminate', job.status === 'planning');

    row.stats.textContent = this.statsText(job);
    if (row.status !== job.status) {
      row.status = job.status;
      row.actions.replaceChildren(...this.actionButtons(job));
    }
    this.renderFailures(row, job);
  }

  statsText(job) {
    const files = `${job.done_files}/${job.total_files} files`;
    const bytes = `${formatBytes(job.bytes_done)} of ${formatBytes(job.total_bytes)}`;
    switch (job.status) {
      case 'queued':
        return 'Waiting\u2026';
      case 'planning':
        return 'Scanning files\u2026';
      case 'running': {
        const parts = [bytes, files];
        if (job.speed) parts.push(`${formatBytes(job.speed)}/s`);
        if (job.eta !== null && job.eta !== undefined) parts.push(`${formatDuration(job.eta)} left`);
        if (job.current) parts.push(job.current);
        return parts.join(' \u00B7 ');
      }
      case 'paused':
        return `Paused \u00B7 ${bytes} \u00B7 ${files}`;
      case 'completed': {
        const skipped = job.skipped_files ? `, ${job.skipped_files} skipped` : '';
        return `Done \u00B7 ${job.done_files} copied${skipped} (${formatBytes(job.bytes_done)})`;
      }
      case 'failed':
        return `Failed \u00B7 ${job.error || 'error'} \u00B7 ${job.done_files} copied`;
      case 'cancelled':
        return `Cancelled \u00B7 ${job.done_files} copied`;
      default:
        return job.status;
    }
  }

  actionButtons(job) {
    const btn = (name, title, action) =>
      el('button', {
        class: 'icon-btn',
        type: 'button',
        title,
        'aria-label': title,
        onclick: async () => {
          try {
            this.upsert(await api.post(`/api/transfers/${job.id}/${action}`));
          } catch (err) {
            toast(err.message, 'error');
          }
        },
      }, icon(name));
    const buttons = [];
    if (['queued', 'planning', 'running'].includes(job.status)) buttons.push(btn('pause', 'Pause', 'pause'));
    if (job.status === 'paused') buttons.push(btn('play', 'Resume', 'resume'));
    if (!FINISHED.has(job.status)) buttons.push(btn('close', 'Cancel', 'cancel'));
    if (job.can_retry) buttons.push(btn('retry', 'Retry failed files', 'retry'));
    return buttons;
  }

  renderFailures(row, job) {
    const failures = job.failures || [];
    row.failures.hidden = failures.length === 0;
    if (!failures.length) return;
    const list = el('ul', {}, failures.map((f) => el('li', {}, el('strong', {}, f.rel), `: ${f.error}`)));
    const more = job.failed_files > failures.length ? el('div', {}, `\u2026and ${job.failed_files - failures.length} more`) : null;
    const open = row.failures.open;
    row.failures.replaceChildren(el('summary', {}, `${job.failed_files} file(s) failed`), list, more);
    row.failures.open = open;
  }
}
