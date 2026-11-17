// CloudWatch-like log viewer, ported from matrix_sols (static/js/application_logs.js) to a reusable component.
// One instance per tab of the Logs window; a "source" adapter supplies the data:
//
//   source = {
//     key,                                   // saved queries are stored per source
//     meta()                     -> {levels:[{id,label,severity}], ranges, default_range, groups?, file?}
//     search(query, cursor)      -> {entries, next_cursor, partial, cursor_reset, highlight, case_sensitive,
//                                    histogram?, groups?, scanned_bytes?, matched?, kept?}
//     histogram?(query)          -> {buckets, step_seconds, total, by_level, partial, range, elapsed_ms}
//     tail(query, handlers)      -> stop()   handlers: {entries(list, initial), status(text), notice(text), error(text)}
//     downloadUrl(query)         -> url
//     extraQuery?()              -> extra params (merged into every request)
//     emptyText?                 -> text when nothing matches
//   }

import { promptText, confirmDialog } from './dialogs.js';
import { el, storage, store, toast, formatBytes } from './util.js';

const PAGE_SIZE = 100;
const RANGE_LABELS = {
  '1m': '1m', '5m': '5m', '15m': '15m', '30m': '30m', '1h': '1h', '3h': '3h', '12h': '12h', '1d': '1d', '3d': '3d', '1w': '1w',
};
const formatNumber = (n) => Number(n || 0).toLocaleString();
const pad = (n) => String(n).padStart(2, '0');
const toInput = (d) => `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
const parseStamp = (s) => new Date(s.replace(' ', 'T'));
const escapeRegex = (s) => s.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');

// ------------------------------------------------------------------ multi-select (groups / streams)
function createMultiSelect(placeholder, onChange) {
  const selected = new Set();
  let options = [];
  const valueEl = el('span', { class: 'lv-ms-value is-placeholder' }, placeholder);
  const trigger = el('button', { type: 'button', class: 'lv-ms-trigger' }, valueEl, el('span', { class: 'lv-caret-down', 'aria-hidden': 'true' }, '▾'));
  const search = el('input', { type: 'search', class: 'lv-ms-search', placeholder: 'Search…' });
  const list = el('div', { class: 'lv-ms-options' });
  const selectShown = el('button', { type: 'button', class: 'link' }, 'Select shown');
  const clear = el('button', { type: 'button', class: 'link' }, 'Clear');
  const panel = el('div', { class: 'lv-ms-panel', hidden: true }, search, list, el('div', { class: 'lv-ms-footer' }, selectShown, clear));
  const root = el('div', { class: 'lv-ms' }, trigger, panel);

  const renderValue = () => {
    const values = [...selected];
    valueEl.textContent = !values.length ? placeholder : values.length <= 2 ? values.join(', ') : `${values.length} selected`;
    valueEl.classList.toggle('is-placeholder', !values.length);
    trigger.title = values.join('\n');
  };
  const visible = () => {
    const term = search.value.trim().toLowerCase();
    return options.filter((o) => !term || o.value.toLowerCase().includes(term));
  };
  const renderList = () => {
    const shown = visible();
    if (!shown.length) {
      list.replaceChildren(el('div', { class: 'lv-ms-empty' }, options.length ? 'No matches' : 'Nothing to choose yet'));
      return;
    }
    let section = null;
    const rows = [];
    for (const opt of shown) {
      if (opt.section && opt.section !== section) {
        section = opt.section;
        rows.push(el('div', { class: 'lv-ms-group-label' }, section));
      }
      const box = el('input', { type: 'checkbox' });
      box.checked = selected.has(opt.value);
      box.addEventListener('change', () => {
        if (box.checked) selected.add(opt.value);
        else selected.delete(opt.value);
        renderValue();
        onChange();
      });
      rows.push(el('label', { class: 'lv-ms-option' }, box, el('span', { class: 'lv-ms-name', title: opt.value }, opt.label || opt.value),
        el('span', { class: 'lv-ms-count' }, formatNumber(opt.count))));
    }
    list.replaceChildren(...rows);
  };
  trigger.addEventListener('click', () => {
    const open = panel.hidden;
    document.querySelectorAll('.lv-ms-panel').forEach((p) => { p.hidden = true; });
    panel.hidden = !open;
    if (open) { renderList(); search.focus(); }
  });
  search.addEventListener('input', renderList);
  selectShown.addEventListener('click', () => { visible().forEach((o) => selected.add(o.value)); renderValue(); renderList(); onChange(); });
  clear.addEventListener('click', () => { selected.clear(); renderValue(); renderList(); onChange(); });
  document.addEventListener('click', (e) => { if (!root.contains(e.target)) panel.hidden = true; });

  return {
    root,
    setOptions(next) { options = next; if (!panel.hidden) renderList(); },
    getValues: () => [...selected],
    setValues(values) { selected.clear(); (values || []).forEach((v) => selected.add(v)); renderValue(); if (!panel.hidden) renderList(); },
  };
}

// ------------------------------------------------------------------------------------- viewer
export class LogViewer {
  constructor(source, { extraControls = [], tailMax = 1000 } = {}) {
    this.source = source;
    this.tailMax = tailMax;
    this.meta = null;
    this.state = { range: '1h', nextCursor: null, loaded: 0, requestId: 0, highlight: null, groups: [], tail: null };
    this.savedKey = `logs.saved.${source.key}`;

    // query bar
    this.pattern = el('input', { type: 'search', class: 'lv-pattern', placeholder: 'Filter pattern: error timeout   "exact phrase"   -exclude   ?any ?of   /regex/', spellcheck: 'false' });
    this.caseBox = el('input', { type: 'checkbox' });
    this.help = el('div', { class: 'lv-help', hidden: true },
      el('table', {},
        el('tr', {}, el('td', {}, el('code', {}, 'error timeout')), el('td', {}, 'both terms must appear')),
        el('tr', {}, el('td', {}, el('code', {}, '"exact phrase"')), el('td', {}, 'the phrase must appear')),
        el('tr', {}, el('td', {}, el('code', {}, '-debug  -"a b"')), el('td', {}, 'must NOT appear')),
        el('tr', {}, el('td', {}, el('code', {}, '?error ?warning')), el('td', {}, 'at least one of the ? terms')),
        el('tr', {}, el('td', {}, el('code', {}, '/pid \\d+/')), el('td', {}, 'regular expression (can also take - or ?)'))),
      el('div', { class: 'lv-help-note' }, 'Terms match anywhere in the entry, ignoring case unless "Aa" is ticked.'));
    this.runBtn = el('button', { type: 'submit', class: 'btn primary small' }, 'Run');
    const form = el('form', { class: 'lv-query' },
      this.pattern,
      el('button', { type: 'button', class: 'btn small', title: 'Pattern syntax', onclick: () => { this.help.hidden = !this.help.hidden; } }, '?'),
      el('label', { class: 'check lv-case', title: 'Case-sensitive' }, this.caseBox, 'Aa'),
      this.runBtn);
    form.addEventListener('submit', (e) => { e.preventDefault(); this.rerun(); });

    // range
    this.rangeBox = el('div', { class: 'segmented lv-ranges', role: 'group' });
    this.start = el('input', { type: 'datetime-local', step: '1' });
    this.end = el('input', { type: 'datetime-local', step: '1' });
    this.custom = el('div', { class: 'lv-custom', hidden: true }, 'From', this.start, 'to', this.end,
      el('button', { type: 'button', class: 'btn small', onclick: () => this.rerun() }, 'Apply'));

    // levels / groups / streams
    this.levelsBox = el('div', { class: 'lv-levels' });
    this.groups = createMultiSelect(source.groupLabel || 'All groups', () => this.refreshStreams());
    this.streams = createMultiSelect(source.streamLabel || 'All streams', () => {});
    this.groups.root.title = source.groupHint || 'Log groups';
    this.streams.root.title = source.streamHint || 'Log streams';

    // saved queries & actions
    this.saved = el('select', { class: 'lv-saved', onchange: () => this.applySaved() });
    this.deleteSavedBtn = el('button', { type: 'button', class: 'btn small', disabled: true, onclick: () => this.deleteSaved() }, 'Delete');
    this.tailBtn = el('button', { type: 'button', class: 'btn small', onclick: () => this.toggleTail() }, '● Start live tail');

    // histogram
    this.summary = el('div', { class: 'lv-summary' });
    this.legend = el('div', { class: 'lv-legend' });
    this.bars = el('div', { class: 'lv-histogram', role: 'img' });
    this.axis = el('div', { class: 'lv-axis' });
    this.histCard = el('div', { class: 'lv-hist-card' }, el('div', { class: 'lv-hist-head' }, this.summary, this.legend), this.bars, this.axis);

    // results
    this.resultsTitle = el('strong', {}, 'Log events');
    this.tailStatus = el('span', { class: 'lv-tail-status', hidden: true });
    this.pauseBtn = el('button', { type: 'button', class: 'btn small', hidden: true, onclick: () => this.togglePause() }, 'Pause');
    this.clearTailBtn = el('button', { type: 'button', class: 'btn small', hidden: true, onclick: () => this.clearRows() }, 'Clear');
    this.wrapBox = el('input', { type: 'checkbox', onchange: () => { store('logs.wrap', this.wrapBox.checked); this.applyWrap(); } });
    this.expandBtn = el('button', { type: 'button', class: 'btn small', onclick: () => this.toggleExpandAll() }, 'Expand all');
    this.notice = el('div', { class: 'lv-notice', hidden: true });
    this.rows = el('div', { class: 'lv-rows', role: 'log' });
    this.empty = el('div', { class: 'lv-empty', hidden: true });
    this.loading = el('div', { class: 'lv-empty', hidden: true }, 'Loading…');
    this.tableWrap = el('div', { class: 'lv-table' }, this.rows, this.empty, this.loading);
    this.footerInfo = el('span', { class: 'lv-footer-info' });
    this.loadMoreBtn = el('button', { type: 'button', class: 'btn small', hidden: true, onclick: () => this.fetchPage(true) }, 'Load more');

    this.root = el('div', { class: 'lv' },
      form, this.help,
      el('div', { class: 'lv-row' }, this.rangeBox, this.custom),
      el('div', { class: 'lv-row' }, this.levelsBox, this.groups.root, this.streams.root, ...extraControls),
      el('div', { class: 'lv-row' },
        this.saved,
        el('button', { type: 'button', class: 'btn small', onclick: () => this.saveQuery() }, 'Save query'),
        this.deleteSavedBtn,
        el('button', { type: 'button', class: 'btn small', onclick: () => this.reset() }, 'Reset'),
        el('div', { class: 'spacer' }),
        el('button', { type: 'button', class: 'btn small', onclick: () => this.download(), title: 'Download the matching entries (newest first, up to 10,000)' }, 'Download'),
        this.tailBtn),
      this.histCard,
      el('div', { class: 'lv-results-head' }, this.resultsTitle, this.tailStatus, this.pauseBtn, this.clearTailBtn,
        el('div', { class: 'spacer' }),
        el('label', { class: 'check' }, this.wrapBox, 'Wrap lines'),
        this.expandBtn),
      this.notice,
      this.tableWrap,
      el('div', { class: 'lv-footer' }, this.footerInfo, el('div', { class: 'spacer' }), this.loadMoreBtn),
    );
    this.wrapBox.checked = !!storage('logs.wrap', false);
    this.applyWrap();
    this.renderSaved();
  }

  // ------------------------------------------------------------------- setup
  async init() {
    try {
      this.meta = await this.source.meta();
    } catch (err) {
      this.showNotice(err.message, true);
      return;
    }
    const ranges = [...this.meta.ranges, 'all', 'custom'];
    this.rangeBox.replaceChildren(...ranges.map((r) => el('button', {
      type: 'button', dataset: { range: r },
      onclick: () => { this.setRange(r); if (r === 'custom') { if (!this.start.value) this.start.focus(); return; } if (!this.state.tail) this.runQuery(); },
    }, RANGE_LABELS[r] || (r === 'all' ? 'All' : 'Custom'))));
    this.levelsBox.replaceChildren(...this.meta.levels.map((lv) => {
      const box = el('input', { type: 'checkbox', value: lv.id, onchange: () => this.rerun() });
      return el('label', { class: `lv-level-check sev-${lv.severity}`, title: lv.label }, box, lv.id.length === 1 ? lv.label : lv.id);
    }));
    this.setRange(this.meta.default_range);
    this.setGroups(this.meta.groups || []);
    this.runQuery();
  }

  setGroups(groups) {
    this.state.groups = groups;
    this.groups.setOptions(groups.map((g) => ({ value: g.name, count: g.count })));
    this.refreshStreams();
  }

  refreshStreams() {
    const chosen = this.groups.getValues();
    const options = [];
    for (const g of this.state.groups) {
      if (chosen.length && !chosen.includes(g.name)) continue;
      for (const s of g.streams) options.push({ value: s.name, count: s.count, section: g.name });
    }
    this.streams.setOptions(options);
  }

  setRange(range) {
    this.state.range = range;
    this.rangeBox.querySelectorAll('button').forEach((b) => b.classList.toggle('active', b.dataset.range === range));
    this.custom.hidden = range !== 'custom';
  }

  levels() {
    return [...this.levelsBox.querySelectorAll('input:checked')].map((i) => i.value);
  }

  collectQuery() {
    const q = { range: this.state.range };
    if (this.state.range === 'custom') {
      if (this.start.value) q.start = this.start.value;
      if (this.end.value) q.end = this.end.value;
    }
    const levels = this.levels();
    if (levels.length) q.levels = levels.join(',');
    const groups = this.groups.getValues();
    if (groups.length) q.groups = groups.join(',');
    const streams = this.streams.getValues();
    if (streams.length) q.streams = streams.join(',');
    const pattern = this.pattern.value.trim();
    if (pattern) q.q = pattern;
    if (this.caseBox.checked) q.case = '1';
    return { ...q, ...(this.source.extraQuery?.() || {}) };
  }

  applyQuery(q) {
    this.setRange(q.range || this.meta?.default_range || '1h');
    this.start.value = q.start || '';
    this.end.value = q.end || '';
    const levels = (q.levels || '').split(',').filter(Boolean);
    this.levelsBox.querySelectorAll('input').forEach((i) => { i.checked = levels.includes(i.value); });
    this.groups.setValues((q.groups || '').split(',').filter(Boolean));
    this.refreshStreams();
    this.streams.setValues((q.streams || '').split(',').filter(Boolean));
    this.pattern.value = q.q || '';
    this.caseBox.checked = q.case === '1';
    this.source.applyExtra?.(q);
  }

  // --------------------------------------------------------------------- rendering
  buildHighlighter(highlight, caseSensitive) {
    if (!highlight || !highlight.length) return null;
    const parts = highlight.map((h) => (h.type === 'regex' ? `(?:${h.value})` : escapeRegex(h.value)));
    try {
      return new RegExp(parts.join('|'), caseSensitive ? 'g' : 'gi');
    } catch {
      const plain = highlight.filter((h) => h.type === 'text').map((h) => escapeRegex(h.value));
      return plain.length ? new RegExp(plain.join('|'), caseSensitive ? 'g' : 'gi') : null;
    }
  }

  appendHighlighted(target, text) {
    const regex = this.state.highlight;
    if (!regex || !text) { target.append(text || ''); return; }
    regex.lastIndex = 0;
    let last = 0;
    let match;
    let guard = 0;
    while ((match = regex.exec(text)) !== null && guard < 500) {
      guard += 1;
      if (match[0].length === 0) { regex.lastIndex += 1; continue; }
      if (match.index > last) target.append(text.slice(last, match.index));
      target.append(el('mark', { class: 'lv-hl' }, match[0]));
      last = match.index + match[0].length;
    }
    if (last < text.length) target.append(text.slice(last));
  }

  buildEntry(entry, isNew = false) {
    const msg = el('span', { class: 'lv-msg' });
    if (entry.note) msg.append(el('span', { class: 'lv-note' }, entry.note));
    this.appendHighlighted(msg, entry.message);
    if (entry.extra) {
      const lines = entry.extra.split('\n').length;
      msg.append(el('span', { class: 'lv-has-extra' }, `+${lines} line${lines === 1 ? '' : 's'}`));
    }
    const main = el('div', { class: 'lv-row-main' },
      el('span', { class: 'lv-caret', 'aria-hidden': 'true' }, '›'),
      el('span', { class: 'lv-ts', title: entry.timestamp }, entry.timestamp),
      el('span', { class: `lv-lvl sev-${entry.severity}` }, entry.level),
      el('span', { class: 'lv-src', title: entry.source_title }, el('bdi', {}, entry.source)),
      msg);
    const detail = el('div', { class: 'lv-detail' });
    const wrapper = el('div', { class: `lv-entry${isNew ? ' is-new' : ''}${entry.note ? ' has-note' : ''}` }, main, detail);
    let built = false;
    const build = () => {
      if (built) return;
      built = true;
      const pre = el('pre');
      this.appendHighlighted(pre, entry.extra ? `${entry.message}\n${entry.extra}` : entry.message);
      if (entry.truncated) pre.append('\n… [truncated]');
      detail.append(...[
        el('div', { class: 'lv-detail-meta' }, entry.details.map(([k, v]) => el('span', {}, el('b', {}, `${k}: `), String(v)))),
        entry.note ? el('div', { class: 'lv-detail-note' }, entry.note) : null,
        pre,
        el('div', { class: 'lv-detail-actions' },
          el('button', { type: 'button', class: 'btn small', onclick: (e) => { e.stopPropagation(); this.copy(entry.raw); } }, 'Copy'),
          el('button', { type: 'button', class: 'btn small', onclick: (e) => { e.stopPropagation(); this.onlyStream(entry); } }, `Only ${this.source.streamNoun || 'this stream'}`),
          el('button', { type: 'button', class: 'btn small', onclick: (e) => { e.stopPropagation(); this.around(entry); } }, '±1 min around')),
      ].filter(Boolean));
    };
    main.addEventListener('click', () => { build(); wrapper.classList.toggle('is-open'); });
    wrapper._open = () => { build(); wrapper.classList.add('is-open'); };
    return wrapper;
  }

  async copy(text) {
    try {
      await navigator.clipboard.writeText(text);
      toast('Copied to the clipboard', 'success');
    } catch {
      toast('Could not copy', 'error');
    }
  }

  onlyStream(entry) {
    this.groups.setValues([entry.group]);
    this.refreshStreams();
    this.streams.setValues([entry.stream]);
    this.rerun();
  }

  around(entry) {
    if (this.state.tail) this.stopTail();
    const center = parseStamp(entry.timestamp);
    this.setRange('custom');
    this.start.value = toInput(new Date(center.getTime() - 60000));
    this.end.value = toInput(new Date(center.getTime() + 60000));
    this.pattern.value = '';
    this.runQuery();
  }

  setEmpty() {
    const hasRows = this.rows.children.length > 0;
    this.empty.hidden = hasRows || !this.loading.hidden;
    this.empty.textContent = this.state.tail
      ? 'Waiting for new log events…'
      : (this.source.emptyText || 'No log events match this query.');
  }

  setLoading(loading) {
    this.loading.hidden = !loading;
    this.runBtn.disabled = loading;
    this.loadMoreBtn.disabled = loading;
    if (loading) this.empty.hidden = true;
  }

  showNotice(message, isError = false) {
    this.notice.hidden = !message;
    this.notice.textContent = message || '';
    this.notice.classList.toggle('is-error', isError);
  }

  updateFooter(extra = '') {
    const tail = this.state.tail;
    this.footerInfo.textContent = tail
      ? `${formatNumber(this.rows.children.length)} events in view (max ${formatNumber(this.tailMax)})`
      : `${formatNumber(this.state.loaded)} events loaded${extra}`;
    this.loadMoreBtn.hidden = !!tail || !this.state.nextCursor;
  }

  // --------------------------------------------------------------------- histogram
  renderHistogram(data) {
    const levels = this.meta.levels;
    const buckets = data.buckets || [];
    const totals = buckets.map((b) => Object.values(b.counts).reduce((a, c) => a + c, 0));
    const max = Math.max(1, ...totals);
    this.bars.replaceChildren(...buckets.map((bucket, i) => {
      const breakdown = levels.filter((l) => bucket.counts[l.id]).map((l) => `${l.label}: ${formatNumber(bucket.counts[l.id])}`);
      const bar = el('div', {
        class: 'lv-bar',
        title: `${bucket.start}\n${formatNumber(totals[i])} events${breakdown.length ? `\n${breakdown.join('\n')}` : ''}\nClick to zoom in`,
      }, levels.filter((l) => bucket.counts[l.id]).map((l) => {
        const seg = el('div', { class: `lv-bar-seg sev-${l.severity}` });
        seg.style.height = `${(bucket.counts[l.id] / max) * 100}%`;
        return seg;
      }));
      bar.addEventListener('click', () => {
        const start = parseStamp(bucket.start);
        const end = new Date(start.getTime() + data.step_seconds * 1000 - 1000);
        this.setRange('custom');
        this.start.value = toInput(start);
        this.end.value = toInput(end);
        this.runQuery();
      });
      return bar;
    }));
    const ticks = buckets.length ? [...new Set([0, Math.floor(buckets.length / 2), buckets.length - 1])] : [];
    this.axis.replaceChildren(...ticks.map((i) => el('span', {}, buckets[i].start.slice(5, 16))));
    this.summary.replaceChildren(el('strong', {}, formatNumber(data.total)),
      ` matching events · ${data.range.start || 'beginning'} → ${data.range.end}${data.elapsed_ms != null ? ` · ${data.elapsed_ms} ms` : ''}`,
      data.partial ? ' · partial (scan limit reached)' : '');
    this.legend.replaceChildren(...levels.map((l) => el('span', { class: 'lv-legend-item' },
      el('span', { class: `lv-swatch sev-${l.severity}` }), `${l.id.length === 1 ? l.label : l.id} ${formatNumber((data.by_level || {})[l.id])}`)));
  }

  // --------------------------------------------------------------------- search
  async fetchPage(append) {
    const query = this.collectQuery();
    const requestId = ++this.state.requestId;
    const cursor = append ? this.state.nextCursor : null;
    this.setLoading(true);
    if (!append) this.summary.textContent = 'Counting events…';
    try {
      const data = await this.source.search({ ...query, limit: PAGE_SIZE }, cursor);
      if (requestId !== this.state.requestId) return;
      if (!append) this.rows.replaceChildren();
      this.state.highlight = this.buildHighlighter(data.highlight, data.case_sensitive);
      const fragment = document.createDocumentFragment();
      data.entries.forEach((entry) => fragment.append(this.buildEntry(entry)));
      this.rows.append(fragment);
      this.state.loaded = this.rows.children.length;
      this.state.nextCursor = data.next_cursor;
      if (data.histogram) this.renderHistogram(data.histogram);
      if (data.groups) this.setGroups(data.groups);
      if (data.file !== undefined) this.source.onFile?.(data.file);
      if (data.cursor_reset) this.showNotice('The log changed since the first page; results restarted from the newest entries.');
      else if (data.partial && data.scanned_bytes != null) this.showNotice(`Scan limit reached after ${formatBytes(data.scanned_bytes)} without filling the page. Use "Load more" to keep searching older entries.`);
      else if (data.partial) this.showNotice(`Only the newest ${formatNumber(data.kept)} of ${formatNumber(data.matched)} matching lines are kept. Narrow the query to see older ones.`);
      else this.showNotice('');
      if (!append && data.scroll !== false) this.rows.scrollTop = 0;
    } catch (err) {
      if (requestId !== this.state.requestId) return;
      this.rows.replaceChildren();
      this.state.nextCursor = null;
      this.state.loaded = 0;
      this.showNotice(err.message, true);
      if (!append) this.summary.textContent = '';
    } finally {
      if (requestId === this.state.requestId) {
        this.setLoading(false);
        this.setEmpty();
        this.updateFooter();
      }
    }
  }

  async fetchHistogram() {
    if (!this.source.histogram) return;
    this.summary.textContent = 'Counting events…';
    try {
      this.renderHistogram(await this.source.histogram(this.collectQuery()));
    } catch (err) {
      this.summary.textContent = err.message;
    }
  }

  runQuery() {
    if (this.state.tail) { this.startTail(); return; }
    if (this.state.range === 'custom' && !this.start.value) {
      toast('Pick a start date/time for the custom range.', 'error');
      return;
    }
    this.state.nextCursor = null;
    this.state.loaded = 0;
    this.rows.replaceChildren();
    this.fetchPage(false);
    this.fetchHistogram();
  }

  rerun() {
    if (this.state.tail) this.startTail();
    else this.runQuery();
  }

  // --------------------------------------------------------------------- live tail
  appendTail(entries, initial) {
    if (!entries.length) return;
    const stick = initial || this.rows.scrollHeight - this.rows.scrollTop - this.rows.clientHeight < 60;
    const fragment = document.createDocumentFragment();
    entries.forEach((entry) => fragment.append(this.buildEntry(entry, !initial)));
    this.rows.append(fragment);
    while (this.rows.children.length > this.tailMax) this.rows.firstElementChild.remove();
    if (stick) this.rows.scrollTop = this.rows.scrollHeight;
    this.setEmpty();
    this.updateFooter();
  }

  startTail() {
    this.stopTail(true);
    this.state.requestId += 1; // cancel in-flight searches
    this.setLoading(false);
    this.rows.replaceChildren();
    this.showNotice('');
    const tail = { paused: false, buffer: [], stop: null };
    this.state.tail = tail;
    this.histCard.hidden = true;
    this.resultsTitle.textContent = 'Live tail';
    this.tailBtn.textContent = '■ Stop live tail';
    this.tailBtn.classList.add('is-live');
    this.tailStatus.hidden = false;
    this.pauseBtn.hidden = false;
    this.clearTailBtn.hidden = false;
    this.pauseBtn.textContent = 'Pause';
    this.setTailStatus('Connecting…');
    const query = this.collectQuery();
    delete query.range; delete query.start; delete query.end;
    tail.stop = this.source.tail(query, {
      entries: (list, initial, highlight) => {
        if (this.state.tail !== tail) return;
        if (highlight) this.state.highlight = this.buildHighlighter(highlight.highlight, highlight.case_sensitive);
        if (tail.paused) { tail.buffer.push(...list); tail.buffer.splice(0, Math.max(0, tail.buffer.length - this.tailMax)); return; }
        this.appendTail(list, initial);
      },
      status: (text) => { if (this.state.tail === tail && !tail.paused) this.setTailStatus(text); },
      notice: (text) => { if (this.state.tail === tail) this.showNotice(text); },
      error: (text) => {
        if (this.state.tail !== tail) return;
        this.setTailStatus(`Stopped: ${text}`);
        this.showNotice(text, true);
      },
    });
    this.setEmpty();
    this.updateFooter();
  }

  setTailStatus(text) {
    this.tailStatus.textContent = text;
    this.tailStatus.classList.toggle('is-paused', !!this.state.tail?.paused);
  }

  togglePause() {
    const tail = this.state.tail;
    if (!tail) return;
    tail.paused = !tail.paused;
    this.pauseBtn.textContent = tail.paused ? 'Resume' : 'Pause';
    this.setTailStatus(tail.paused ? 'Paused' : 'Resuming…');
    if (!tail.paused && tail.buffer.length) {
      const pending = tail.buffer.splice(0);
      this.appendTail(pending, false);
    }
  }

  stopTail(silent = false) {
    const tail = this.state.tail;
    if (!tail) return;
    this.state.tail = null;
    tail.stop?.();
    if (silent) return;
    this.histCard.hidden = false;
    this.resultsTitle.textContent = 'Log events';
    this.tailBtn.textContent = '● Start live tail';
    this.tailBtn.classList.remove('is-live');
    this.tailStatus.hidden = true;
    this.pauseBtn.hidden = true;
    this.clearTailBtn.hidden = true;
  }

  toggleTail() {
    if (this.state.tail) {
      this.stopTail();
      this.runQuery();
    } else {
      this.startTail();
    }
  }

  clearRows() {
    this.rows.replaceChildren();
    this.setEmpty();
    this.updateFooter();
  }

  // --------------------------------------------------------------------- saved queries (per browser)
  loadSaved() {
    return storage(this.savedKey, []);
  }

  renderSaved(selected = '') {
    const saved = this.loadSaved();
    this.saved.replaceChildren(el('option', { value: '' }, saved.length ? 'Saved queries…' : 'No saved queries'),
      ...saved.map((item) => el('option', { value: item.name }, item.name)));
    this.saved.value = selected;
    this.deleteSavedBtn.disabled = !selected;
  }

  async saveQuery() {
    const name = await promptText({ title: 'Save query', label: 'Name', value: this.saved.value || '', okLabel: 'Save' });
    if (!name) return;
    const saved = this.loadSaved().filter((item) => item.name !== name);
    saved.push({ name, query: this.collectQuery(), savedAt: new Date().toISOString() });
    saved.sort((a, b) => a.name.localeCompare(b.name));
    store(this.savedKey, saved);
    this.renderSaved(name);
    toast(`Saved query "${name}" in this browser`, 'success');
  }

  applySaved() {
    const name = this.saved.value;
    this.deleteSavedBtn.disabled = !name;
    const item = name && this.loadSaved().find((q) => q.name === name);
    if (!item) return;
    this.applyQuery(item.query);
    this.rerun();
  }

  async deleteSaved() {
    const name = this.saved.value;
    if (!name) return;
    const ok = await confirmDialog({ title: 'Delete saved query?', message: `"${name}" will be removed from this browser.`, okLabel: 'Delete', danger: true });
    if (!ok) return;
    store(this.savedKey, this.loadSaved().filter((item) => item.name !== name));
    this.renderSaved();
  }

  reset() {
    this.source.resetExtra?.();
    this.applyQuery({ range: this.meta?.default_range });
    this.renderSaved();
    this.rerun();
  }

  // --------------------------------------------------------------------- misc
  download() {
    if (this.state.range === 'custom' && !this.start.value) {
      toast('Pick a start date/time for the custom range.', 'error');
      return;
    }
    const link = el('a', { href: this.source.downloadUrl(this.collectQuery()), download: '' });
    document.body.append(link);
    link.click();
    link.remove();
  }

  applyWrap() {
    this.tableWrap.classList.toggle('lv-wrap', this.wrapBox.checked);
  }

  toggleExpandAll() {
    const entries = [...this.rows.children];
    const anyClosed = entries.some((e) => !e.classList.contains('is-open'));
    entries.forEach((e) => (anyClosed ? e._open() : e.classList.remove('is-open')));
    this.expandBtn.textContent = anyClosed ? 'Collapse all' : 'Expand all';
  }

  close() {
    this.stopTail();
  }
}
