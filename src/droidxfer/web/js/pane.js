// A file browser pane used for both the Mac and the Android side.

import { confirmDialog, promptText } from './dialogs.js';
import { el, fileIcon, formatBytes, formatDate, icon, isThumbable, storage, store, toast } from './util.js';

let activeDrag = null; // { pane, paths } while dragging rows between panes

const COLUMNS = [
  { key: 'name', label: 'Name' },
  { key: 'size', label: 'Size' },
  { key: 'mtime', label: 'Modified' },
];

export class FilePane {
  constructor(root, source, handlers) {
    this.root = root;
    this.source = source;
    this.handlers = handlers;
    this.path = null;
    this.parent = null;
    this.entries = [];
    this.visible = [];
    this.selected = new Set();
    this.anchor = null;
    this.filter = '';
    this.showHidden = false;
    this.sort = storage(`sort.${source.kind}`, { key: 'name', dir: 1 });
    this.view = storage(`view.${source.kind}`, 'list');
    this.build();
  }

  // ---------------------------------------------------------------- DOM setup
  build() {
    const btn = (name, title, onclick) =>
      el('button', { class: 'icon-btn', type: 'button', title, 'aria-label': title, onclick }, icon(name));

    this.titleEl = el('span', { class: 'pane-title' }, this.source.title);
    this.subEl = el('span', { class: 'pane-sub' });
    this.placesEl = el('select', { class: 'places', title: 'Go to a common folder', onchange: () => {
      const target = this.placesEl.value;
      this.placesEl.selectedIndex = 0;
      if (target) this.load(target);
    } });

    this.upBtn = btn('up', 'Enclosing folder (Cmd+Up)', () => this.goUp());
    this.crumbs = el('div', { class: 'crumbs', title: 'Click empty space to type a path' });
    this.crumbs.addEventListener('click', (e) => {
      if (e.target === this.crumbs) this.editPath();
    });
    this.pathInput = el('input', { class: 'path-input', hidden: true, spellcheck: 'false', autocomplete: 'off' });
    this.pathInput.addEventListener('keydown', (e) => {
      if (e.key === 'Enter') {
        this.pathInput.hidden = true;
        this.crumbs.hidden = false;
        this.load(this.pathInput.value.trim());
      } else if (e.key === 'Escape') {
        this.pathInput.hidden = true;
        this.crumbs.hidden = false;
      }
    });
    this.pathInput.addEventListener('blur', () => {
      this.pathInput.hidden = true;
      this.crumbs.hidden = false;
    });

    this.filterInput = el('input', { type: 'search', class: 'filter', placeholder: 'Filter', 'aria-label': 'Filter by name' });
    this.filterInput.addEventListener('input', () => {
      this.filter = this.filterInput.value.trim().toLowerCase();
      this.render();
    });
    this.renameBtn = btn('edit', 'Rename (F2)', () => this.rename());
    this.deleteBtn = btn('trash', this.source.kind === 'mac' ? 'Move to Trash' : 'Delete from phone', () => this.remove());
    this.viewBtn = btn(this.view === 'list' ? 'grid' : 'list', 'Toggle list / thumbnails', () => this.toggleView());
    this.hiddenBox = el('input', { type: 'checkbox' });
    this.hiddenBox.addEventListener('change', () => {
      this.showHidden = this.hiddenBox.checked;
      this.refresh();
    });

    this.body = el('div', { class: 'pane-body', tabindex: '0' });
    this.message = el('div', { class: 'pane-message', hidden: true });
    this.status = el('div', { class: 'statusbar' });

    this.root.append(
      el('div', { class: 'pane-head' }, el('div', { class: 'pane-titles' }, this.titleEl, this.subEl), this.placesEl),
      el('div', { class: 'nav' }, this.upBtn, btn('refresh', 'Refresh', () => this.refresh()), this.crumbs, this.pathInput),
      el(
        'div',
        { class: 'tools' },
        this.filterInput,
        this.newBtn = btn('plus', 'New folder', () => this.newFolder()),
        this.renameBtn,
        this.deleteBtn,
        this.viewBtn,
        el('label', { class: 'check', title: 'Show hidden files' }, this.hiddenBox, 'Hidden'),
      ),
      this.body,
      this.message,
      this.status,
    );

    this.body.addEventListener('click', (e) => this.onClick(e));
    this.body.addEventListener('dblclick', (e) => this.onDblClick(e));
    this.body.addEventListener('keydown', (e) => this.onKey(e));
    this.body.addEventListener('dragstart', (e) => this.onDragStart(e));
    this.body.addEventListener('dragend', () => this.onDragEnd());
    this.body.addEventListener('dragover', (e) => this.onDragOver(e));
    this.body.addEventListener('dragleave', (e) => this.onDragLeave(e));
    this.body.addEventListener('drop', (e) => this.onDrop(e));
    this.renderPlaces();
  }

  renderPlaces() {
    this.placesEl.replaceChildren(el('option', { value: '' }, 'Go to\u2026'));
    for (const place of this.source.places()) {
      this.placesEl.append(el('option', { value: place.path }, place.label));
    }
  }

  setSubtitle(text) {
    this.subEl.textContent = text || '';
  }

  setShowHidden(value) {
    this.showHidden = value;
    this.hiddenBox.checked = value;
  }

  // -------------------------------------------------------------- navigation
  async load(path, { keepSelection = false, select = null } = {}) {
    if (!this.source.available()) {
      this.showMessage(this.source.unavailableMessage());
      return;
    }
    this.root.classList.add('loading');
    try {
      const res = await this.source.list(path, this.showHidden);
      const changed = res.path !== this.path;
      this.path = res.path;
      this.parent = res.parent;
      this.entries = res.entries;
      if (changed) {
        this.filter = '';
        this.filterInput.value = '';
      }
      if (select) this.selected = new Set([select].flat());
      else if (!keepSelection || changed) this.selected = new Set();
      else {
        const existing = new Set(res.entries.map((e) => e.path));
        this.selected = new Set([...this.selected].filter((p) => existing.has(p)));
      }
      this.anchor = null;
      this.source.remember(this.path);
      this.hideMessage();
      this.render();
      if (select) this.scrollToSelection();
    } catch (err) {
      toast(err.message, 'error');
      const home = this.source.home();
      if (this.path === null && path !== home) await this.load(home);
      else if (this.path === null) this.showMessage(err.message);
    } finally {
      this.root.classList.remove('loading');
    }
  }

  refresh() {
    if (this.path) return this.load(this.path, { keepSelection: true });
    return this.load(this.source.home());
  }

  goUp() {
    if (this.parent) this.load(this.parent, { select: this.path });
  }

  editPath() {
    this.crumbs.hidden = true;
    this.pathInput.hidden = false;
    this.pathInput.value = this.path || '';
    this.pathInput.focus();
    this.pathInput.select();
  }

  open(entry) {
    if (entry.is_dir) this.load(entry.path);
    else window.open(this.source.fileUrl(entry.path), '_blank', 'noopener');
  }

  showMessage(text) {
    this.path = null;
    this.parent = null;
    this.entries = [];
    this.visible = [];
    this.selected.clear();
    this.body.replaceChildren();
    this.body.hidden = true;
    this.message.hidden = false;
    this.message.textContent = text;
    this.crumbs.replaceChildren();
    this.status.textContent = '';
    this.updateButtons();
  }

  hideMessage() {
    this.body.hidden = false;
    this.message.hidden = true;
  }

  // --------------------------------------------------------------- rendering
  computeVisible() {
    const { key, dir } = this.sort;
    const list = this.filter ? this.entries.filter((e) => e.name.toLowerCase().includes(this.filter)) : [...this.entries];
    list.sort((a, b) => {
      if (a.is_dir !== b.is_dir) return a.is_dir ? -1 : 1;
      let cmp = 0;
      if (key === 'size') cmp = a.size - b.size;
      else if (key === 'mtime') cmp = a.mtime - b.mtime;
      if (cmp === 0) cmp = a.name.localeCompare(b.name, undefined, { numeric: true, sensitivity: 'base' });
      return cmp * dir;
    });
    this.visible = list;
  }

  render() {
    this.computeVisible();
    this.renderCrumbs();
    this.body.replaceChildren(this.view === 'grid' ? this.renderGrid() : this.renderList());
    if (!this.visible.length) {
      this.body.append(el('div', { class: 'empty' }, this.filter ? 'No matching items' : 'This folder is empty'));
    }
    this.updateSelection();
  }

  renderCrumbs() {
    this.crumbs.replaceChildren();
    if (!this.path) return;
    const base = this.source.crumbBase(this.path);
    const parts = [{ label: base.label, path: base.path }];
    let acc = base.path;
    for (const segment of this.path.slice(base.path.length).split('/').filter(Boolean)) {
      acc = acc.endsWith('/') ? acc + segment : `${acc}/${segment}`;
      parts.push({ label: segment, path: acc });
    }
    parts.forEach((part, i) => {
      if (i) this.crumbs.append(el('span', { class: 'sep' }, '\u203A'));
      const crumb = el('button', { type: 'button', class: 'crumb', title: part.path }, part.label);
      crumb.addEventListener('click', () => this.load(part.path));
      this.crumbs.append(crumb);
    });
    this.upBtn.disabled = !this.parent;
  }

  renderList() {
    const head = el('tr');
    for (const col of COLUMNS) {
      const arrow = this.sort.key === col.key ? (this.sort.dir > 0 ? ' \u25B2' : ' \u25BC') : '';
      const th = el('th', { class: `col-${col.key}`, scope: 'col' }, col.label + arrow);
      th.addEventListener('click', () => this.setSort(col.key));
      head.append(th);
    }
    const tbody = el('tbody');
    this.visible.forEach((entry, idx) => {
      tbody.append(
        el(
          'tr',
          { draggable: 'true', dataset: { idx: String(idx) } },
          el(
            'td',
            { class: 'col-name' },
            el(
              'div',
              { class: 'cell-name' },
              icon(fileIcon(entry), entry.is_dir ? 'folder' : ''),
              el('span', { class: 'name', title: entry.name }, entry.name),
              entry.is_link ? icon('link', 'link') : null,
            ),
          ),
          el('td', { class: 'col-size' }, entry.is_dir ? '\u2014' : formatBytes(entry.size)),
          el('td', { class: 'col-mtime' }, formatDate(entry.mtime)),
        ),
      );
    });
    return el('table', { class: 'files' }, el('thead', {}, head), tbody);
  }

  renderGrid() {
    const grid = el('div', { class: 'grid' });
    this.visible.forEach((entry, idx) => {
      let thumb;
      if (!entry.is_dir && isThumbable(entry.name)) {
        thumb = el('img', { class: 'thumb', loading: 'lazy', alt: '', draggable: 'false', src: this.source.thumbUrl(entry.path) });
        thumb.addEventListener('error', () => thumb.replaceWith(icon(fileIcon(entry), 'big')), { once: true });
      } else {
        thumb = icon(fileIcon(entry), `big ${entry.is_dir ? 'folder' : ''}`);
      }
      grid.append(
        el(
          'div',
          { class: 'tile', draggable: 'true', dataset: { idx: String(idx) }, title: entry.name },
          el('div', { class: 'tile-thumb' }, thumb),
          el('div', { class: 'tile-name' }, entry.name),
        ),
      );
    });
    return grid;
  }

  setSort(key) {
    this.sort = { key, dir: this.sort.key === key ? -this.sort.dir : 1 };
    store(`sort.${this.source.kind}`, this.sort);
    this.render();
  }

  toggleView() {
    this.view = this.view === 'list' ? 'grid' : 'list';
    store(`view.${this.source.kind}`, this.view);
    this.viewBtn.replaceChildren(icon(this.view === 'list' ? 'grid' : 'list'));
    this.render();
  }

  // --------------------------------------------------------------- selection
  rows() {
    return this.body.querySelectorAll('[data-idx]');
  }

  updateSelection() {
    for (const row of this.rows()) {
      const entry = this.visible[Number(row.dataset.idx)];
      row.classList.toggle('selected', this.selected.has(entry.path));
    }
    const chosen = this.selectedEntries();
    const bytes = chosen.reduce((sum, e) => sum + (e.is_dir ? 0 : e.size), 0);
    this.status.textContent =
      `${this.visible.length} item${this.visible.length === 1 ? '' : 's'}` +
      (chosen.length ? ` \u00B7 ${chosen.length} selected${bytes ? ` (${formatBytes(bytes)})` : ''}` : '');
    this.updateButtons();
    this.handlers.onSelection?.(this);
  }

  updateButtons() {
    const n = this.selected.size;
    this.renameBtn.disabled = n !== 1;
    this.deleteBtn.disabled = n === 0;
    this.newBtn.disabled = !this.path;
    this.upBtn.disabled = !this.path || !this.parent;
  }

  selectedEntries() {
    return this.visible.filter((e) => this.selected.has(e.path));
  }

  selectedPaths() {
    return this.selectedEntries().map((e) => e.path);
  }

  selectIndex(idx, { shift = false, toggle = false } = {}) {
    const entry = this.visible[idx];
    if (!entry) return;
    if (shift && this.anchor !== null) {
      const [a, b] = [Math.min(this.anchor, idx), Math.max(this.anchor, idx)];
      if (!toggle) this.selected.clear();
      for (let i = a; i <= b; i += 1) this.selected.add(this.visible[i].path);
    } else if (toggle) {
      if (this.selected.has(entry.path)) this.selected.delete(entry.path);
      else this.selected.add(entry.path);
      this.anchor = idx;
    } else {
      this.selected = new Set([entry.path]);
      this.anchor = idx;
    }
    this.cursor = idx;
    this.updateSelection();
  }

  scrollToSelection() {
    const row = [...this.rows()].find((r) => r.classList.contains('selected'));
    row?.scrollIntoView({ block: 'nearest' });
  }

  indexFromEvent(e) {
    const row = e.target.closest('[data-idx]');
    return row && this.body.contains(row) ? Number(row.dataset.idx) : null;
  }

  onClick(e) {
    this.body.focus({ preventScroll: true });
    const idx = this.indexFromEvent(e);
    if (idx === null) {
      if (e.target.closest('th')) return;
      this.selected.clear();
      this.anchor = null;
      this.updateSelection();
      return;
    }
    this.selectIndex(idx, { shift: e.shiftKey, toggle: e.metaKey || e.ctrlKey });
  }

  onDblClick(e) {
    const idx = this.indexFromEvent(e);
    if (idx !== null) this.open(this.visible[idx]);
  }

  onKey(e) {
    if (e.target !== this.body) return;
    const meta = e.metaKey || e.ctrlKey;
    const current = this.cursor ?? this.anchor ?? -1;
    switch (e.key) {
      case 'ArrowDown':
      case 'ArrowUp': {
        if (meta && e.key === 'ArrowUp') {
          this.goUp();
        } else if (meta && e.key === 'ArrowDown') {
          const [only] = this.selectedEntries();
          if (only) this.open(only);
        } else {
          const step = e.key === 'ArrowDown' ? 1 : -1;
          const next = Math.min(Math.max(current + step, 0), this.visible.length - 1);
          if (e.shiftKey && this.anchor === null) this.anchor = Math.max(current, 0);
          this.selectIndex(next, { shift: e.shiftKey });
          this.scrollToSelection();
        }
        break;
      }
      case 'Enter': {
        const chosen = this.selectedEntries();
        if (chosen.length === 1) this.open(chosen[0]);
        break;
      }
      case 'Backspace':
        if (meta) this.remove();
        else this.goUp();
        break;
      case 'Delete':
        this.remove();
        break;
      case 'F2':
        this.rename();
        break;
      case 'Escape':
        this.selected.clear();
        this.updateSelection();
        break;
      case 'a':
        if (!meta) return;
        this.selected = new Set(this.visible.map((v) => v.path));
        this.updateSelection();
        break;
      default:
        return;
    }
    e.preventDefault();
  }

  // ------------------------------------------------------------ drag & drop
  onDragStart(e) {
    const idx = this.indexFromEvent(e);
    if (idx === null) return;
    const entry = this.visible[idx];
    if (!this.selected.has(entry.path)) this.selectIndex(idx);
    activeDrag = { pane: this, paths: this.selectedPaths() };
    e.dataTransfer.effectAllowed = 'copy';
    e.dataTransfer.setData('text/plain', activeDrag.paths.join('\n'));
  }

  onDragEnd() {
    activeDrag = null;
    this.clearDropHighlight();
  }

  dropTarget(e) {
    const idx = this.indexFromEvent(e);
    const entry = idx === null ? null : this.visible[idx];
    if (entry && entry.is_dir) return { dir: entry.path, row: e.target.closest('[data-idx]') };
    return { dir: this.path, row: null };
  }

  acceptsDrop(e) {
    if (!this.path || !this.source.available()) return false;
    if (activeDrag) return activeDrag.pane !== this;
    return [...e.dataTransfer.types].includes('Files');
  }

  onDragOver(e) {
    if (!this.acceptsDrop(e)) return;
    e.preventDefault();
    e.dataTransfer.dropEffect = 'copy';
    const { row } = this.dropTarget(e);
    this.body.classList.add('drop-target');
    for (const r of this.body.querySelectorAll('.drop-row')) if (r !== row) r.classList.remove('drop-row');
    row?.classList.add('drop-row');
  }

  onDragLeave(e) {
    if (!this.body.contains(e.relatedTarget)) this.clearDropHighlight();
  }

  clearDropHighlight() {
    this.body.classList.remove('drop-target');
    for (const r of this.body.querySelectorAll('.drop-row')) r.classList.remove('drop-row');
  }

  onDrop(e) {
    if (!this.acceptsDrop(e)) return;
    e.preventDefault();
    const { dir } = this.dropTarget(e);
    this.clearDropHighlight();
    if (activeDrag) {
      const { pane, paths } = activeDrag;
      activeDrag = null;
      this.handlers.onTransfer(pane, this, paths, dir);
    } else {
      this.handlers.onFinderDrop(this, e.dataTransfer, dir);
    }
  }

  // ------------------------------------------------------------------ actions
  async newFolder() {
    if (!this.path) return;
    const name = await promptText({ title: 'New folder', label: 'Folder name', value: 'New Folder', okLabel: 'Create' });
    if (!name) return;
    try {
      const res = await this.source.mkdir(this.path, name);
      await this.load(this.path, { select: res.path });
    } catch (err) {
      toast(err.message, 'error');
    }
  }

  async rename() {
    const [entry] = this.selectedEntries();
    if (!entry || this.selected.size !== 1) return;
    const name = await promptText({ title: 'Rename', label: 'New name', value: entry.name, okLabel: 'Rename' });
    if (!name || name === entry.name) return;
    try {
      const res = await this.source.rename(entry.path, name);
      await this.load(this.path, { select: res.path });
    } catch (err) {
      toast(err.message, 'error');
    }
  }

  async remove() {
    const paths = this.selectedPaths();
    if (!paths.length) return;
    const what = paths.length === 1 ? `"${paths[0].split('/').pop()}"` : `${paths.length} items`;
    const mac = this.source.kind === 'mac';
    const ok = await confirmDialog({
      title: mac ? 'Move to Trash' : 'Delete from phone',
      message: mac ? `Move ${what} to the Trash?` : `Permanently delete ${what} from the phone? This cannot be undone.`,
      okLabel: mac ? 'Move to Trash' : 'Delete',
      danger: !mac,
    });
    if (!ok) return;
    try {
      await this.source.remove(paths);
      toast(mac ? `Moved ${what} to the Trash` : `Deleted ${what}`);
    } catch (err) {
      toast(err.message, 'error');
    }
    await this.refresh();
  }
}
