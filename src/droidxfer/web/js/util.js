// Small DOM and formatting helpers. All user-provided text goes through textContent.

export function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs)) {
    if (value === undefined || value === null || value === false) continue;
    if (key === 'class') node.className = value;
    else if (key === 'text') node.textContent = value;
    else if (key === 'dataset') Object.assign(node.dataset, value);
    else if (key.startsWith('on') && typeof value === 'function') node.addEventListener(key.slice(2), value);
    else if (value === true) node.setAttribute(key, '');
    else node.setAttribute(key, String(value));
  }
  for (const child of children.flat()) {
    if (child === null || child === undefined || child === false) continue;
    node.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return node;
}

// Trusted, static SVG markup only.
const ICONS = {
  folder: '<path d="M3 6.5A1.5 1.5 0 0 1 4.5 5h4l2 2h9A1.5 1.5 0 0 1 21 8.5v9a1.5 1.5 0 0 1-1.5 1.5h-15A1.5 1.5 0 0 1 3 17.5z" fill="currentColor" opacity=".9"/>',
  file: '<path d="M6 3h8l4 4v14H6z" fill="none" stroke="currentColor" stroke-width="1.6"/><path d="M14 3v4h4" fill="none" stroke="currentColor" stroke-width="1.6"/>',
  image: '<rect x="3.5" y="4.5" width="17" height="15" rx="2" fill="none" stroke="currentColor" stroke-width="1.6"/><circle cx="9" cy="10" r="1.8" fill="currentColor"/><path d="M4 18l5-5 4 4 3-3 4 4" fill="none" stroke="currentColor" stroke-width="1.6"/>',
  video: '<rect x="3.5" y="5.5" width="13" height="13" rx="2" fill="none" stroke="currentColor" stroke-width="1.6"/><path d="M16.5 10l4-2.5v9l-4-2.5z" fill="currentColor"/>',
  audio: '<path d="M9 17V5l10-2v12" fill="none" stroke="currentColor" stroke-width="1.6"/><circle cx="7" cy="17" r="2.5" fill="currentColor"/><circle cx="17" cy="15" r="2.5" fill="currentColor"/>',
  archive: '<rect x="4" y="3.5" width="16" height="17" rx="2" fill="none" stroke="currentColor" stroke-width="1.6"/><path d="M12 4v2m0 2v2m0 2v2" stroke="currentColor" stroke-width="2"/>',
  up: '<path d="M12 19V5M5 12l7-7 7 7" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>',
  refresh: '<path d="M20 11a8 8 0 1 0-2.3 5.7M20 4v7h-7" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>',
  plus: '<path d="M12 5v14M5 12h14" stroke="currentColor" stroke-width="2" stroke-linecap="round"/>',
  edit: '<path d="M4 20h4L19 9l-4-4L4 16z" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linejoin="round"/>',
  trash: '<path d="M4 7h16M9 7V4h6v3M6 7l1 13h10l1-13" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linejoin="round"/>',
  grid: '<path d="M4 4h7v7H4zM13 4h7v7h-7zM4 13h7v7H4zM13 13h7v7h-7z" fill="none" stroke="currentColor" stroke-width="1.8"/>',
  list: '<path d="M4 6h16M4 12h16M4 18h16" stroke="currentColor" stroke-width="2" stroke-linecap="round"/>',
  pause: '<path d="M8 5v14M16 5v14" stroke="currentColor" stroke-width="2.4" stroke-linecap="round"/>',
  play: '<path d="M7 5l12 7-12 7z" fill="currentColor"/>',
  close: '<path d="M6 6l12 12M18 6L6 18" stroke="currentColor" stroke-width="2" stroke-linecap="round"/>',
  retry: '<path d="M4 12a8 8 0 1 0 2.3-5.7M4 4v5h5" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>',
  link: '<path d="M10 14a4 4 0 0 0 5.7 0l3-3a4 4 0 0 0-5.7-5.7l-1 1M14 10a4 4 0 0 0-5.7 0l-3 3a4 4 0 0 0 5.7 5.7l1-1" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"/>',
};

export function icon(name, cls = '') {
  const span = document.createElement('span');
  span.className = `icon ${cls}`.trim();
  span.innerHTML = `<svg viewBox="0 0 24 24" aria-hidden="true">${ICONS[name] || ICONS.file}</svg>`;
  return span;
}

const IMAGE_EXT = new Set(['jpg', 'jpeg', 'png', 'gif', 'webp', 'bmp', 'tif', 'tiff']);
const PHOTO_EXT = new Set([...IMAGE_EXT, 'heic', 'heif', 'dng', 'svg']);
const VIDEO_EXT = new Set(['mp4', 'mkv', 'mov', '3gp', 'webm', 'avi', 'm4v']);
const AUDIO_EXT = new Set(['mp3', 'm4a', 'aac', 'flac', 'ogg', 'opus', 'wav', 'amr']);
const ARCHIVE_EXT = new Set(['zip', 'tar', 'gz', 'tgz', '7z', 'rar', 'apk', 'dmg', 'xz', 'bz2']);

export function extension(name) {
  const i = name.lastIndexOf('.');
  return i > 0 ? name.slice(i + 1).toLowerCase() : '';
}

export const isThumbable = (name) => IMAGE_EXT.has(extension(name));

export function fileIcon(entry) {
  if (entry.is_dir) return 'folder';
  const ext = extension(entry.name);
  if (PHOTO_EXT.has(ext)) return 'image';
  if (VIDEO_EXT.has(ext)) return 'video';
  if (AUDIO_EXT.has(ext)) return 'audio';
  if (ARCHIVE_EXT.has(ext)) return 'archive';
  return 'file';
}

export function formatBytes(n) {
  if (n === null || n === undefined) return '';
  const units = ['B', 'KB', 'MB', 'GB', 'TB'];
  let i = 0;
  let value = n;
  while (value >= 1024 && i < units.length - 1) {
    value /= 1024;
    i += 1;
  }
  return i === 0 ? `${value} B` : `${value.toFixed(value < 10 ? 1 : 0)} ${units[i]}`;
}

export function formatDate(seconds) {
  if (!seconds) return '';
  const d = new Date(seconds * 1000);
  return d.toLocaleString(undefined, { year: 'numeric', month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' });
}

export function formatDuration(seconds) {
  if (seconds === null || seconds === undefined || !Number.isFinite(seconds)) return '';
  const s = Math.max(0, Math.round(seconds));
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  const sec = String(s % 60).padStart(2, '0');
  return h ? `${h}:${String(m).padStart(2, '0')}:${sec}` : `${m}:${sec}`;
}

export function basename(path) {
  const trimmed = path.replace(/\/+$/, '');
  return trimmed.slice(trimmed.lastIndexOf('/') + 1) || '/';
}

export function joinPath(dir, name) {
  return dir.endsWith('/') ? dir + name : `${dir}/${name}`;
}

export function storage(key, fallback = null) {
  try {
    const raw = localStorage.getItem(`droidxfer.${key}`);
    return raw === null ? fallback : JSON.parse(raw);
  } catch {
    return fallback;
  }
}

export function store(key, value) {
  try {
    localStorage.setItem(`droidxfer.${key}`, JSON.stringify(value));
  } catch {
    /* storage may be unavailable */
  }
}

function raiseToasts(host) {
  if (typeof host.showPopover !== 'function') return;   // older browsers: normal layer
  try {
    if (!host.matches(':popover-open')) {
      host.showPopover();
    } else if (document.querySelector('dialog[open]')) {
      host.hidePopover();      // re-adding moves it to the top of the top layer,
      host.showPopover();      // above any dialog opened since
    }
  } catch {
    /* fall back to the normal layer */
  }
}

export function toast(message, kind = 'info', timeout = 4500) {
  const host = document.getElementById('toasts');
  const node = el('div', { class: `toast ${kind}`, role: kind === 'error' ? 'alert' : 'status' }, message);
  host.append(node);
  raiseToasts(host);
  setTimeout(() => node.classList.add('leaving'), timeout);
  setTimeout(() => node.remove(), timeout + 400);
}
