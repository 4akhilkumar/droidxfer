// Uploading files dropped from Finder into a pane.

import { api, uploadFile } from './api.js';

const SKIP = new Set(['.DS_Store']);

async function walk(entry, prefix, out) {
  if (SKIP.has(entry.name)) return;
  const rel = prefix ? `${prefix}/${entry.name}` : entry.name;
  if (entry.isFile) {
    const file = await new Promise((resolve, reject) => entry.file(resolve, reject));
    out.push({ file, rel });
  } else if (entry.isDirectory) {
    const reader = entry.createReader();
    for (;;) {
      const batch = await new Promise((resolve, reject) => reader.readEntries(resolve, reject));
      if (!batch.length) break;
      for (const child of batch) await walk(child, rel, out);
    }
  }
}

// Must be called synchronously from the drop handler: DataTransfer items expire afterwards.
export function collectDropped(dataTransfer) {
  const entries = [];
  for (const item of dataTransfer.items) {
    if (item.kind !== 'file') continue;
    const entry = item.webkitGetAsEntry?.();
    if (entry) entries.push(entry);
  }
  const plainFiles = entries.length ? [] : [...dataTransfer.files];
  return (async () => {
    if (!entries.length) return plainFiles.filter((f) => !SKIP.has(f.name)).map((file) => ({ file, rel: file.name }));
    const out = [];
    for (const entry of entries) await walk(entry, '', out);
    return out;
  })();
}

export async function uploadAll(files, onProgress) {
  const { id } = await api.post('/api/uploads');
  const total = files.reduce((sum, f) => sum + f.file.size, 0);
  const queue = [...files];
  let done = 0;
  let bytes = 0;
  const worker = async () => {
    while (queue.length) {
      const item = queue.shift();
      await uploadFile(id, item.rel, item.file);
      done += 1;
      bytes += item.file.size;
      onProgress(done, files.length, bytes, total);
    }
  };
  try {
    await Promise.all([worker(), worker(), worker()]);
  } catch (err) {
    api.del(`/api/uploads/${id}`).catch(() => {});
    throw err;
  }
  return id;
}
