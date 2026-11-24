// Trusted, static SVG icons for the pen pages.
const P = {
  pen: '<path d="M4 20l1.2-4.4L15.8 5a2 2 0 0 1 2.8 0l.4.4a2 2 0 0 1 0 2.8L8.4 18.8z" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linejoin="round"/><path d="M13.5 7.3l3.2 3.2" stroke="currentColor" stroke-width="1.7"/>',
  pencil: '<path d="M5 19l1-4L16 5l3 3L9 18z" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linejoin="round"/><path d="M6 15l3 3" stroke="currentColor" stroke-width="1.7"/><path d="M5 19l1.4-.4-1-1z" fill="currentColor"/>',
  highlighter: '<path d="M9 14l7.5-9.5 3 2.5L12 16.5z" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linejoin="round"/><path d="M9 14l-1.5 3.5 2 1 2.5-2" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linejoin="round"/><path d="M4 21h9" stroke="currentColor" stroke-width="3" opacity=".45" stroke-linecap="round"/>',
  eraser: '<path d="M8.5 19.5h11M4.6 14.6l8.8-8.8a2 2 0 0 1 2.8 0l2.4 2.4a2 2 0 0 1 0 2.8l-7.7 7.7a2 2 0 0 1-1.4.6H8.3a2 2 0 0 1-1.4-.6l-2.3-2.3a1.3 1.3 0 0 1 0-1.8z" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linejoin="round"/><path d="M9 10.3l5.6 5.6" stroke="currentColor" stroke-width="1.7"/>',
  lasso: '<path d="M7.5 15.5C4.6 14.4 3 12.6 3 10.5 3 6.9 7 4 12 4s9 2.9 9 6.5-4 6.5-9 6.5c-.9 0-1.8-.1-2.6-.3" fill="none" stroke="currentColor" stroke-width="1.7" stroke-dasharray="2.6 2.4" stroke-linecap="round"/><circle cx="8.5" cy="17" r="1.8" fill="none" stroke="currentColor" stroke-width="1.6"/><path d="M8 18.8c-.3 1.2-1.2 2-2.5 2.2" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round"/>',
  laser: '<circle cx="12" cy="12" r="3.2" fill="currentColor"/><circle cx="12" cy="12" r="7" fill="none" stroke="currentColor" stroke-width="1.5" opacity=".45"/>',
  pan: '<path d="M8 13V6.5a1.5 1.5 0 0 1 3 0V12m0-6.5v-1a1.5 1.5 0 0 1 3 0V12m0-6a1.5 1.5 0 0 1 3 0V13m0-4.5a1.5 1.5 0 0 1 3 0V15a6 6 0 0 1-6 6h-1.4a6 6 0 0 1-4.5-2.1L4.4 15.8a1.6 1.6 0 0 1 2.4-2.1L8 15" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"/>',
  grid: '<path d="M4 4h16v16H4zM4 10h16M4 15h16M10 4v16M15 4v16" fill="none" stroke="currentColor" stroke-width="1.5"/>',
  undo: '<path d="M9 14L4 9l5-5" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round"/><path d="M4 9h10.5a5.5 5.5 0 0 1 0 11H11" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round"/>',
  redo: '<path d="M15 14l5-5-5-5" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round"/><path d="M20 9H9.5a5.5 5.5 0 0 0 0 11H13" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round"/>',
  menu: '<path d="M4 7h16M4 12h16M4 17h16" stroke="currentColor" stroke-width="1.9" stroke-linecap="round"/>',
  prev: '<path d="M15 5l-7 7 7 7" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>',
  next: '<path d="M9 5l7 7-7 7" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>',
  plus: '<path d="M12 5v14M5 12h14" stroke="currentColor" stroke-width="2" stroke-linecap="round"/>',
  close: '<path d="M6 6l12 12M18 6L6 18" stroke="currentColor" stroke-width="2" stroke-linecap="round"/>',
  trash: '<path d="M4 7h16M9 7V4h6v3M6 7l1 13h10l1-13" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linejoin="round"/>',
  copy: '<rect x="8" y="8" width="12" height="12" rx="2" fill="none" stroke="currentColor" stroke-width="1.7"/><path d="M16 8V6a2 2 0 0 0-2-2H6a2 2 0 0 0-2 2v8a2 2 0 0 0 2 2h2" fill="none" stroke="currentColor" stroke-width="1.7"/>',
  text: '<path d="M5 6V4h14v2M12 4v16M9 20h6" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"/>',
  palette: '<circle cx="12" cy="12" r="8.5" fill="none" stroke="currentColor" stroke-width="1.7"/>',
  tablet: '<rect x="3" y="5" width="18" height="12" rx="2" fill="none" stroke="currentColor" stroke-width="1.7"/><path d="M8 21h8M12 17v4" stroke="currentColor" stroke-width="1.7"/><path d="M14 8l-4.5 4.5L9 14l1.5-.5L15 9z" fill="currentColor"/>',
  history: '<path d="M4 12a8 8 0 1 0 2.3-5.7M4 4v4h4" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"/><path d="M12 8v4l3 2" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"/>',
  phone: '<rect x="7" y="3" width="10" height="18" rx="2" fill="none" stroke="currentColor" stroke-width="1.7"/><path d="M11 18h2" stroke="currentColor" stroke-width="1.7" stroke-linecap="round"/>',
  mac: '<rect x="3" y="4" width="18" height="12" rx="1.5" fill="none" stroke="currentColor" stroke-width="1.7"/><path d="M2 19h20" stroke="currentColor" stroke-width="1.7" stroke-linecap="round"/>',
  camera: '<path d="M4 8h3l2-2.5h6L17 8h3v11H4z" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linejoin="round"/><circle cx="12" cy="13" r="3.5" fill="none" stroke="currentColor" stroke-width="1.7"/>',
  image: '<rect x="3.5" y="4.5" width="17" height="15" rx="2" fill="none" stroke="currentColor" stroke-width="1.6"/><circle cx="9" cy="10" r="1.8" fill="currentColor"/><path d="M4 18l5-5 4 4 3-3 4 4" fill="none" stroke="currentColor" stroke-width="1.6"/>',
  sign: '<path d="M3 17c3-1 4-9 6-9s-1 9 1 9 3-5 4-5 0 4 2 4 3-2 5-2" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round"/><path d="M3 21h18" stroke="currentColor" stroke-width="1.5" opacity=".5"/>',
  expand: '<path d="M4 9V4h5M20 9V4h-5M4 15v5h5M20 15v5h-5" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"/>',
  play: '<path d="M7 5l12 7-12 7z" fill="currentColor"/>',
  pause: '<path d="M8 5v14M16 5v14" stroke="currentColor" stroke-width="2.4" stroke-linecap="round"/>',
  save: '<path d="M5 4h11l3 3v13H5z" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linejoin="round"/><path d="M8 4v5h7V4M8 20v-6h8v6" fill="none" stroke="currentColor" stroke-width="1.7"/>',
  search: '<circle cx="11" cy="11" r="6.5" fill="none" stroke="currentColor" stroke-width="1.8"/><path d="M16 16l4.5 4.5" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"/>',
  duplicate: '<rect x="4" y="4" width="11" height="11" rx="2" fill="none" stroke="currentColor" stroke-width="1.7"/><rect x="9" y="9" width="11" height="11" rx="2" fill="none" stroke="currentColor" stroke-width="1.7"/>',
  send: '<path d="M4 12l16-8-6 16-2.5-6.5z" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linejoin="round"/>',
  present: '<rect x="3" y="4" width="18" height="12" rx="1.5" fill="none" stroke="currentColor" stroke-width="1.7"/><circle cx="14" cy="10" r="2" fill="#ff2d2d"/><path d="M9 20l3-4 3 4" fill="none" stroke="currentColor" stroke-width="1.7"/>',
  compare: '<rect x="3" y="5" width="8" height="14" rx="1.5" fill="none" stroke="currentColor" stroke-width="1.7"/><rect x="13" y="5" width="8" height="14" rx="1.5" fill="none" stroke="currentColor" stroke-width="1.7"/>',
};

export function svg(name, size = 22) {
  const span = document.createElement('span');
  span.className = 'ico';
  span.innerHTML = `<svg viewBox="0 0 24 24" width="${size}" height="${size}" aria-hidden="true">${P[name] || ''}</svg>`;
  return span;
}

export function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs)) {
    if (value === undefined || value === null || value === false) continue;
    if (key === 'class') node.className = value;
    else if (key === 'text') node.textContent = value;
    else if (key === 'dataset') Object.assign(node.dataset, value);
    else if (key === 'style' && typeof value === 'object') Object.assign(node.style, value);
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

// replaceChildren that skips null/false/undefined (which it would otherwise print as text).
export function put(node, ...children) {
  node.replaceChildren(...children.flat().filter((c) => c !== null && c !== undefined && c !== false));
}

export function toast(message, kind = 'info', timeout = 3500) {
  let host = document.getElementById('toasts');
  if (!host) {
    host = el('div', { id: 'toasts', class: 'toasts', 'aria-live': 'polite' });
    document.body.append(host);
  }
  const node = el('div', { class: `toast ${kind}`, role: kind === 'error' ? 'alert' : 'status' }, message);
  host.append(node);
  setTimeout(() => node.classList.add('leaving'), timeout);
  setTimeout(() => node.remove(), timeout + 400);
}
