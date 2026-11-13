// "Apps" dialog: disable, uninstall (for the main user) and restore preinstalled apps, with safety ratings.

import { api } from './api.js';
import { el, formatDate, toast } from './util.js';

const RATING = {
  safe: ['Safe', 'Removing it only removes this app.'],
  probably_safe: ['Probably safe', 'You lose the feature described.'],
  advanced: ['Advanced', 'Other apps or core features depend on it.'],
  unsafe: ["Don't remove", 'Can break the phone. Only changeable in expert mode.'],
  unknown: ['Unknown', 'Not rated. Be careful: search for the package name before removing it.'],
};
const STATE = { enabled: 'Enabled', disabled: 'Disabled', uninstalled: 'Uninstalled' };
const ACTION = {
  disable: { verb: 'Disable', done: 'disabled' },
  uninstall: { verb: 'Uninstall for user', done: 'uninstalled' },
  restore: { verb: 'Restore', done: 'restored' },
};
const groupByAction = (steps) => steps.reduce((acc, s) => ((acc[s.action] ??= []).push(s), acc), {});
const IN_USE = { home: 'home screen', sms: 'default SMS app', dialer: 'default phone app', ime: 'current keyboard' };

export class AppsDialog {
  constructor() {
    this.deviceId = null;
    this.apps = [];
    this.selected = new Set();
    this.busy = false;

    const select = (name, options) =>
      el('select', { name, onchange: () => this.render() }, options.map(([v, label]) => el('option', { value: v }, label)));
    this.search = el('input', { type: 'search', placeholder: 'Search name or package', oninput: () => this.render() });
    this.kind = select('kind', [['system', 'Preinstalled apps'], ['user', 'Apps you installed'], ['all', 'All apps']]);
    this.rating = select('rating', [['', 'Any rating'], ...Object.entries(RATING).map(([k, [label]]) => [k, label])]);
    this.state = select('state', [['', 'Any state'], ['enabled', 'Enabled'], ['disabled', 'Disabled'], ['uninstalled', 'Uninstalled'], ['changed', 'Changed by droidxfer']]);
    this.expert = el('input', { type: 'checkbox', onchange: () => this.render() });
    this.catalogLine = el('div', { class: 'apps-catalog' });
    this.listEl = el('div', { class: 'apps-list' });
    this.countEl = el('span', { class: 'apps-count' });
    this.titleEl = el('h3', {}, 'Apps');
    this.importInput = el('input', { type: 'file', accept: 'application/json,.json', hidden: true, onchange: (e) => this.importFile(e) });
    this.buttons = {
      disable: el('button', { class: 'btn primary', type: 'button', onclick: () => this.run('disable') }, 'Disable'),
      uninstall: el('button', { class: 'btn', type: 'button', onclick: () => this.run('uninstall') }, 'Uninstall for user'),
      restore: el('button', { class: 'btn', type: 'button', onclick: () => this.run('restore') }, 'Restore'),
    };

    this.dialog = el(
      'dialog',
      { id: 'dlg-apps', class: 'apps' },
      el('div', { class: 'dlg-head' }, this.titleEl,
        el('div', { class: 'info-actions' },
          el('button', { class: 'btn small', type: 'button', onclick: () => this.restoreAll(), title: 'Undo every change droidxfer made on this phone' }, 'Restore all'),
          el('button', { class: 'btn small', type: 'button', onclick: () => this.exportList(), title: "Save this phone's changes to reuse on another phone" }, 'Export'),
          el('button', { class: 'btn small', type: 'button', onclick: () => this.importInput.click(), title: 'Apply a saved list of changes' }, 'Import'),
          this.importInput,
          el('button', { class: 'btn small', type: 'button', onclick: () => this.dialog.close() }, 'Close'))),
      el('p', { class: 'apps-hint' },
        'Disabling is fully reversible. "Uninstall for user" removes the app and its data for you but keeps the original in the system, so Restore (or a factory reset) brings it back. Apps you installed yourself are removed completely.'),
      this.catalogLine,
      el('div', { class: 'apps-filters' }, this.search, this.kind, this.rating, this.state,
        el('label', { class: 'check', title: "Allow changing apps rated \"Don't remove\". Core Android parts and apps you're using stay protected." }, this.expert, 'Expert mode')),
      this.listEl,
      el('div', { class: 'apps-actions' }, this.countEl, el('div', { class: 'spacer' }), this.buttons.restore, this.buttons.uninstall, this.buttons.disable),
    );
    this.confirm = el('dialog', { id: 'dlg-apps-confirm', class: 'wide' });
    document.body.append(this.dialog, this.confirm);
  }

  open(device) {
    this.deviceId = device.id;
    this.titleEl.textContent = `Apps on ${device.name}`;
    this.selected.clear();
    if (!this.dialog.open) this.dialog.showModal();
    this.load();
  }

  url(suffix = '') {
    return `/api/devices/${encodeURIComponent(this.deviceId)}/apps${suffix}`;
  }

  async load() {
    this.listEl.replaceChildren(el('div', { class: 'info-loading' }, 'Reading the app list…'));
    try {
      const res = await api.get(this.url());
      this.apps = res.apps;
      this.renderCatalog(res.catalog);
      const known = new Set(this.apps.map((a) => a.package));
      this.selected = new Set([...this.selected].filter((p) => known.has(p)));
      this.render();
    } catch (err) {
      this.listEl.replaceChildren(el('div', { class: 'info-error' }, `Could not read the app list: ${err.message}`));
    }
  }

  renderCatalog(status) {
    const community = status.community_fetched_at
      ? `community list: ${status.community_count.toLocaleString()} packages, downloaded ${formatDate(status.community_fetched_at)}`
      : 'community list: not downloaded';
    const button = el('button', { class: 'btn small', type: 'button', onclick: () => this.fetchCommunity(button) },
      status.community_fetched_at ? 'Update' : 'Download community list');
    button.title = `Downloads the Universal Android Debloater (UAD-ng) list from GitHub (${status.community_url}). droidxfer's own ratings take priority.`;
    this.catalogLine.replaceChildren(`Ratings: ${status.curated_count} from droidxfer · ${community} `, button);
  }

  async fetchCommunity(button) {
    button.disabled = true;
    button.textContent = 'Downloading…';
    try {
      const status = await api.post('/api/apps/catalog/fetch');
      toast(`Community list downloaded: ${status.community_count.toLocaleString()} packages`, 'success');
      await this.load();
    } catch (err) {
      toast(err.message, 'error', 8000);
      button.disabled = false;
      button.textContent = 'Download community list';
    }
  }

  visible() {
    const q = this.search.value.trim().toLowerCase();
    return this.apps.filter((a) => {
      const preinstalled = a.system || !!a.preloaded_by;
      if (this.kind.value === 'system' && !preinstalled) return false;
      if (this.kind.value === 'user' && preinstalled) return false;
      if (this.rating.value && a.rating !== this.rating.value) return false;
      if (this.state.value === 'changed' ? !a.changed_by_droidxfer : this.state.value && a.state !== this.state.value) return false;
      return !q || a.package.toLowerCase().includes(q) || (a.name || '').toLowerCase().includes(q);
    });
  }

  locked(app) {
    if (app.state !== 'enabled') return null; // anything changed can always be restored
    if (app.core) return app.in_use ? `In use as your ${IN_USE[app.in_use]}` : 'Core part of Android';
    if (app.rating === 'unsafe' && !this.expert.checked) return "Rated \"Don't remove\" (turn on expert mode)";
    return null;
  }

  render() {
    const rows = this.visible();
    const order = { unsafe: 0, advanced: 1, probably_safe: 2, safe: 3, unknown: 4 };
    rows.sort((a, b) => (a.name || a.package).localeCompare(b.name || b.package));
    rows.sort((a, b) => (a.state === 'enabled') - (b.state === 'enabled') || order[b.rating] - order[a.rating]);
    const list = rows.map((app) => {
      const lock = this.locked(app);
      if (lock) this.selected.delete(app.package);
      const box = el('input', { type: 'checkbox', disabled: !!lock, title: lock || null });
      box.checked = this.selected.has(app.package);
      box.addEventListener('change', () => {
        if (box.checked) this.selected.add(app.package);
        else this.selected.delete(app.package);
        this.updateActions();
      });
      const [ratingLabel, ratingHint] = RATING[app.rating] || RATING.unknown;
      const source = app.rating_source === 'community' ? ` (community list: ${app.community_rating})` : '';
      return el(
        'label',
        { class: `apps-row ${lock ? 'is-locked' : ''}` },
        box,
        el('div', { class: 'apps-main' },
          el('div', { class: 'apps-name' }, app.name || app.package,
            app.in_use ? el('span', { class: 'apps-tag' }, `your ${IN_USE[app.in_use]}`) : null,
            app.preloaded_by ? el('span', { class: 'apps-tag', title: 'Not part of the system: uninstalling removes it completely.' }, `preloaded by ${app.preloaded_by}`)
              : !app.system ? el('span', { class: 'apps-tag' }, 'installed by you') : null),
          app.name ? el('div', { class: 'apps-pkg' }, app.package) : null,
          app.description ? el('div', { class: 'apps-desc' }, app.description) : null),
        el('span', { class: `apps-rating ${app.rating}`, title: ratingHint + source }, ratingLabel),
        el('span', { class: `apps-state ${app.state}` }, STATE[app.state]),
      );
    });
    this.listEl.replaceChildren(...(list.length ? list : [el('div', { class: 'info-loading' }, 'No apps match the filters.')]));
    this.updateActions(rows.length);
  }

  updateActions(shown = this.visible().length) {
    const chosen = this.apps.filter((a) => this.selected.has(a.package));
    this.countEl.textContent = `${shown} shown · ${chosen.length} selected`;
    this.buttons.disable.disabled = this.busy || !chosen.some((a) => a.state === 'enabled');
    this.buttons.uninstall.disabled = this.busy || !chosen.some((a) => a.state !== 'uninstalled');
    this.buttons.restore.disabled = this.busy || !chosen.some((a) => a.state !== 'enabled');
  }

  async run(action) {
    const packages = [...this.selected];
    if (!packages.length) return;
    await this.planAndApply(action, packages);
  }

  async planAndApply(action, packages, steps = null) {
    try {
      steps ??= (await api.post(this.url('/plan'), { action, packages, expert: this.expert.checked })).steps;
    } catch (err) {
      toast(err.message, 'error');
      return;
    }
    const groups = groupByAction(steps);
    if (!(await this.confirmSteps(groups))) return;
    this.busy = true;
    this.updateActions();
    let ok = 0;
    let failed = [];
    try {
      for (const [act, items] of Object.entries(groups)) {
        const todo = items.filter((s) => !s.skip).map((s) => s.package);
        if (!todo.length) continue;
        const { results } = await api.post(this.url('/apply'), { action: act, packages: todo, expert: this.expert.checked });
        ok += results.filter((r) => r.ok).length;
        failed = failed.concat(results.filter((r) => r.ok === false));
      }
      if (ok) toast(`${ok} app${ok === 1 ? '' : 's'} changed`, 'success');
      for (const f of failed.slice(0, 3)) toast(`${f.package}: ${f.message}`, 'error', 9000);
    } catch (err) {
      toast(err.message, 'error');
    } finally {
      this.busy = false;
      this.selected.clear();
      await this.load();
    }
  }

  confirmSteps(groups) {
    const sections = [];
    let actionable = 0;
    for (const [action, steps] of Object.entries(groups)) {
      const todo = steps.filter((s) => !s.skip);
      const skipped = steps.filter((s) => s.skip);
      actionable += todo.length;
      const name = (pkg) => this.apps.find((a) => a.package === pkg)?.name || pkg;
      if (todo.length) {
        sections.push(el('h4', {}, `${ACTION[action].verb}: ${todo.length} app${todo.length === 1 ? '' : 's'}`),
          el('ul', { class: 'apps-confirm-list' }, todo.map((s) =>
            el('li', {}, el('strong', {}, name(s.package)), ` — ${s.package}`,
              s.warning ? el('div', { class: 'apps-warning' }, s.warning) : null))));
      }
      if (skipped.length) {
        sections.push(el('h4', {}, `Skipped: ${skipped.length}`),
          el('ul', { class: 'apps-confirm-list muted' }, skipped.map((s) => el('li', {}, `${name(s.package)}: ${s.skip}`))));
      }
      if (action === 'uninstall' && todo.length) {
        sections.push(el('p', { class: 'apps-warning' }, "Uninstalling deletes these apps' data on the phone. Restore brings preinstalled apps back, but not their data."));
      }
    }
    const okBtn = el('button', { class: 'btn primary', type: 'button', disabled: !actionable }, actionable ? 'Apply' : 'Nothing to do');
    this.confirm.replaceChildren(el('h3', {}, 'Review changes'), ...sections,
      el('div', { class: 'actions' },
        el('button', { class: 'btn', type: 'button', onclick: () => this.confirm.close('cancel') }, 'Cancel'), okBtn));
    okBtn.addEventListener('click', () => this.confirm.close('ok'));
    this.confirm.returnValue = '';
    this.confirm.showModal();
    return new Promise((resolve) => {
      this.confirm.addEventListener('close', () => resolve(this.confirm.returnValue === 'ok'), { once: true });
    });
  }

  async restoreAll() {
    const changed = this.apps.filter((a) => a.changed_by_droidxfer).map((a) => a.package);
    if (!changed.length) {
      toast('Nothing to restore: droidxfer has no active changes on this phone.');
      return;
    }
    await this.planAndApply('restore', changed);
  }

  async exportList() {
    try {
      const doc = await api.get(this.url('/export'));
      if (!doc.packages.length) {
        toast('Nothing to export: droidxfer has no active changes on this phone.');
        return;
      }
      const blob = new Blob([JSON.stringify(doc, null, 2)], { type: 'application/json' });
      const link = el('a', { href: URL.createObjectURL(blob), download: `droidxfer-apps-${(doc.device.model || 'phone').replace(/\W+/g, '-')}.json` });
      document.body.append(link);
      link.click();
      link.remove();
      setTimeout(() => URL.revokeObjectURL(link.href), 1000);
    } catch (err) {
      toast(err.message, 'error');
    }
  }

  async importFile(event) {
    const file = event.target.files[0];
    event.target.value = '';
    if (!file) return;
    try {
      const doc = JSON.parse(await file.text());
      const { steps } = await api.post(this.url('/import-plan'), { document: doc, expert: this.expert.checked });
      await this.planAndApply(null, null, steps);
    } catch (err) {
      toast(`Could not import: ${err.message}`, 'error');
    }
  }
}
