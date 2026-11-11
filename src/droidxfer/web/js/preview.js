// In-app viewer for photos, videos and audio from either pane.
//
// Media is shown inside this page (same origin, session cookie), never by opening the raw file URL in a
// new tab: that response carries a sandboxing CSP, which gives the tab an opaque origin, so the browser's
// follow-up Range requests for video/audio would go out without the cookie and be rejected.

import { el, extension, formatBytes, formatDate, icon } from './util.js';

const NATIVE_IMAGE = new Set(['jpg', 'jpeg', 'png', 'gif', 'webp', 'bmp', 'avif']);
const CONVERTED_IMAGE = new Set(['heic', 'heif', 'tif', 'tiff']); // shown via a server-made JPEG
const VIDEO = new Set(['mp4', 'm4v', 'mov', 'webm', 'mkv', '3gp']);
const AUDIO = new Set(['mp3', 'm4a', 'aac', 'flac', 'ogg', 'opus', 'wav', 'amr']);

export function previewKind(name) {
  const ext = extension(name);
  if (NATIVE_IMAGE.has(ext)) return 'image';
  if (CONVERTED_IMAGE.has(ext)) return 'converted';
  if (VIDEO.has(ext)) return 'video';
  if (AUDIO.has(ext)) return 'audio';
  return null;
}

export const canPreview = (entry) => !entry.is_dir && previewKind(entry.name) !== null;

class PreviewViewer {
  constructor() {
    this.items = [];
    this.index = 0;
    this.source = null;
    this.media = null;

    const btn = (name, title, onclick) =>
      el('button', { class: 'icon-btn', type: 'button', title, 'aria-label': title, onclick }, icon(name));
    this.titleEl = el('div', { class: 'preview-title' });
    this.metaEl = el('div', { class: 'preview-meta' });
    this.prevBtn = btn('prev', 'Previous (←)', () => this.step(-1));
    this.nextBtn = btn('next', 'Next (→)', () => this.step(1));
    this.downloadLink = el('a', { class: 'btn small', title: 'Download this file' }, 'Download');
    this.stage = el('div', { class: 'preview-stage' });
    this.dialog = el(
      'dialog',
      { id: 'dlg-preview', class: 'preview', 'aria-label': 'Preview' },
      el(
        'div',
        { class: 'preview-head' },
        el('div', { class: 'preview-titles' }, this.titleEl, this.metaEl),
        this.prevBtn,
        this.nextBtn,
        this.downloadLink,
        btn('close', 'Close (Esc)', () => this.dialog.close()),
      ),
      this.stage,
    );
    document.body.append(this.dialog);

    this.dialog.addEventListener('close', () => this.clear());
    this.dialog.addEventListener('keydown', (e) => {
      if (e.target instanceof HTMLInputElement) return;
      if (e.key === 'ArrowLeft') this.step(-1);
      else if (e.key === 'ArrowRight') this.step(1);
      else return;
      e.preventDefault();
    });
    // Clicking the dimmed backdrop closes the viewer.
    this.dialog.addEventListener('click', (e) => {
      if (e.target === this.dialog) this.dialog.close();
    });
  }

  open(source, entries, entry) {
    this.source = source;
    this.items = entries.filter(canPreview);
    this.index = Math.max(0, this.items.findIndex((e) => e.path === entry.path));
    if (!this.dialog.open) this.dialog.showModal();
    this.show();
  }

  step(delta) {
    const next = this.index + delta;
    if (next < 0 || next >= this.items.length) return;
    this.index = next;
    this.show();
  }

  clear() {
    if (this.media) {
      // Stop playback and release the connection to the phone.
      if (this.media.pause) this.media.pause();
      this.media.removeAttribute('src');
      if (this.media.load) this.media.load();
      this.media = null;
    }
    this.stage.replaceChildren();
  }

  show() {
    this.clear();
    const entry = this.items[this.index];
    if (!entry) return;
    const kind = previewKind(entry.name);
    const url = this.source.fileUrl(entry.path);

    this.titleEl.textContent = entry.name;
    this.titleEl.title = entry.path;
    this.metaEl.textContent = [
      `${this.index + 1} of ${this.items.length}`,
      formatBytes(entry.size),
      formatDate(entry.mtime),
    ].filter(Boolean).join(' · ');
    this.prevBtn.disabled = this.index === 0;
    this.nextBtn.disabled = this.index === this.items.length - 1;
    this.downloadLink.href = url;
    this.downloadLink.download = entry.name;

    const failed = (message) => {
      this.stage.replaceChildren(el('div', { class: 'preview-error' }, message));
    };
    let media;
    if (kind === 'image' || kind === 'converted') {
      media = el('img', { alt: entry.name, draggable: 'false' });
      media.addEventListener('error', () => failed('This image could not be shown. Use Download to open it on the Mac.'), { once: true });
      media.src = kind === 'image' ? url : this.source.previewUrl(entry.path);
    } else {
      media = el(kind, { controls: true, autoplay: true, preload: 'metadata' });
      media.addEventListener(
        'error',
        () => failed(`This browser can't play this ${kind} format. Use Download to open it with another app.`),
        { once: true },
      );
      media.src = url;
    }
    this.media = media;
    this.stage.replaceChildren(media);
    this.dialog.focus();
  }
}

let viewer = null;

export function openPreview(source, entries, entry) {
  viewer ??= new PreviewViewer();
  viewer.open(source, entries, entry);
}
