// Promise-based wrappers around the <dialog> elements declared in index.html.

const $ = (sel, root = document) => root.querySelector(sel);

function waitClose(dialog) {
  return new Promise((resolve) => {
    dialog.addEventListener('close', () => resolve(dialog.returnValue), { once: true });
  });
}

function wire(dialog) {
  if (dialog.dataset.wired) return;
  dialog.dataset.wired = '1';
  const form = $('form', dialog);
  form.addEventListener('submit', (e) => {
    e.preventDefault();
    dialog.close('ok');
  });
  for (const btn of dialog.querySelectorAll('[data-cancel]')) {
    btn.addEventListener('click', () => dialog.close('cancel'));
  }
}

export async function promptText({ title, label, value = '', okLabel = 'OK' }) {
  const dialog = $('#dlg-prompt');
  wire(dialog);
  $('[data-title]', dialog).textContent = title;
  $('[data-label]', dialog).textContent = label;
  $('[data-ok]', dialog).textContent = okLabel;
  const input = $('input', dialog);
  input.value = value;
  dialog.returnValue = '';
  dialog.showModal();
  input.focus();
  const dot = value.lastIndexOf('.');
  input.setSelectionRange(0, dot > 0 ? dot : value.length);
  const result = await waitClose(dialog);
  const text = input.value.trim();
  return result === 'ok' && text ? text : null;
}

export async function confirmDialog({ title, message, okLabel = 'OK', danger = false }) {
  const dialog = $('#dlg-confirm');
  wire(dialog);
  $('[data-title]', dialog).textContent = title;
  $('[data-message]', dialog).textContent = message;
  const ok = $('[data-ok]', dialog);
  ok.textContent = okLabel;
  ok.classList.toggle('danger', danger);
  dialog.returnValue = '';
  dialog.showModal();
  ok.focus();
  return (await waitClose(dialog)) === 'ok';
}

export async function transferOptions({ summary, conflict, verify }) {
  const dialog = $('#dlg-transfer');
  wire(dialog);
  $('[data-summary]', dialog).textContent = summary;
  const form = $('form', dialog);
  form.elements.conflict.value = conflict;
  form.elements.verify.checked = verify;
  form.elements.remember.checked = false;
  dialog.returnValue = '';
  dialog.showModal();
  $('[data-ok]', dialog).focus();
  if ((await waitClose(dialog)) !== 'ok') return null;
  return {
    conflict: form.elements.conflict.value,
    verify: form.elements.verify.checked,
    remember: form.elements.remember.checked,
  };
}

export async function settingsDialog(settings) {
  const dialog = $('#dlg-settings');
  wire(dialog);
  const f = $('form', dialog).elements;
  f.default_conflict.value = settings.default_conflict;
  f.verify_checksum.checked = settings.verify_checksum;
  f.media_scan.checked = settings.media_scan;
  f.show_hidden.checked = settings.show_hidden;
  f.prefer_usb.checked = settings.prefer_usb;
  f.ask_before_transfer.checked = settings.ask_before_transfer;
  f.adb_path.value = settings.adb_path || '';
  dialog.returnValue = '';
  dialog.showModal();
  if ((await waitClose(dialog)) !== 'ok') return null;
  return {
    default_conflict: f.default_conflict.value,
    verify_checksum: f.verify_checksum.checked,
    media_scan: f.media_scan.checked,
    show_hidden: f.show_hidden.checked,
    prefer_usb: f.prefer_usb.checked,
    ask_before_transfer: f.ask_before_transfer.checked,
    adb_path: f.adb_path.value.trim() || null,
  };
}
