// Fetch wrapper and the live-event WebSocket.

export class ApiError extends Error {
  constructor(status, detail) {
    super(detail);
    this.status = status;
  }
}

async function request(method, url, body) {
  const options = { method, credentials: 'same-origin', headers: {} };
  if (body !== undefined) {
    options.headers['Content-Type'] = 'application/json';
    options.body = JSON.stringify(body);
  }
  const res = await fetch(url, options);
  if (!res.ok) {
    let detail = res.statusText;
    try {
      const payload = await res.json();
      detail = payload.detail ?? detail;
    } catch {
      /* not JSON */
    }
    if (typeof detail !== 'string') {
      detail = Array.isArray(detail) ? detail.map((d) => d.msg || JSON.stringify(d)).join('; ') : JSON.stringify(detail);
    }
    throw new ApiError(res.status, detail);
  }
  return res.status === 204 ? null : res.json();
}

export const api = {
  get: (url) => request('GET', url),
  post: (url, body = {}) => request('POST', url, body),
  put: (url, body = {}) => request('PUT', url, body),
  del: (url) => request('DELETE', url),
};

export function qs(params) {
  const search = new URLSearchParams();
  for (const [k, v] of Object.entries(params)) {
    if (v !== undefined && v !== null) search.set(k, String(v));
  }
  return search.toString();
}

export async function uploadFile(uploadId, rel, file) {
  const url = `/api/uploads/${uploadId}?${qs({ rel, mtime: file.lastModified })}`;
  const res = await fetch(url, { method: 'PUT', body: file, credentials: 'same-origin' });
  if (!res.ok) {
    let detail = res.statusText;
    try {
      detail = (await res.json()).detail ?? detail;
    } catch {
      /* ignore */
    }
    throw new ApiError(res.status, String(detail));
  }
}

export function connectEvents(onEvent, onStatus) {
  let delay = 500;
  let socket;
  const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
  const open = () => {
    socket = new WebSocket(`${proto}//${location.host}/api/events`);
    socket.addEventListener('open', () => {
      delay = 500;
      onStatus(true);
    });
    socket.addEventListener('message', (e) => {
      try {
        onEvent(JSON.parse(e.data));
      } catch (err) {
        console.error('bad event', err);
      }
    });
    socket.addEventListener('close', () => {
      onStatus(false);
      setTimeout(open, delay);
      delay = Math.min(delay * 2, 8000);
    });
  };
  open();
  return () => socket && socket.close();
}
