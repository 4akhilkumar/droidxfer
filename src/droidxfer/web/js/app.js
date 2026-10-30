// App entry point: wires panes, devices, transfers, dialogs and live events together.

import { api, connectEvents, qs } from './api.js';
import { settingsDialog, transferOptions } from './dialogs.js';
import { FilePane } from './pane.js';
import { TransfersPanel } from './transfers.js';
import { collectDropped, uploadAll } from './upload.js';
import { basename, formatBytes, storage, store, toast } from './util.js';
import { WirelessDialog } from './wireless.js';

const $ = (sel) => document.querySelector(sel);

const state = {
  home: null,
  settings: null,
  devices: [],
  adb: { ok: true, error: null },
  deviceId: storage('device', null),
};

let macPane;
let androidPane;
let transfers;
let wireless;

const currentDevice = () => state.devices.find((d) => d.id === state.deviceId) || null;
const deviceReady = () => currentDevice()?.state === 'device';
const devUrl = (suffix) => `/api/devices/${encodeURIComponent(state.deviceId)}/${suffix}`;

const macSource = {
  kind: 'mac',
  title: 'Mac',
  available: () => true,
  unavailableMessage: () => '',
  list: (path, hidden) => api.get(`/api/local/ls?${qs({ path, hidden })}`),
  mkdir: (parent, name) => api.post('/api/local/mkdir', { parent, name }),
  rename: (path, name) => api.post('/api/local/rename', { path, name }),
  remove: (paths) => api.post('/api/local/delete', { paths }),
  thumbUrl: (path) => `/api/local/thumb?${qs({ path })}`,
  fileUrl: (path) => `/api/local/file?${qs({ path })}`,
  home: () => state.home,
  places: () =>
    [['Home', ''], ['Desktop', '/Desktop'], ['Documents', '/Documents'], ['Downloads', '/Downloads'],
      ['Pictures', '/Pictures'], ['Movies', '/Movies'], ['Music', '/Music']].map(([label, sub]) => ({ label, path: state.home + sub })),
  crumbBase: (path) => (path === state.home || path.startsWith(`${state.home}/`) ? { label: 'Home', path: state.home } : { label: '/', path: '/' }),
  remember: (path) => store('macPath', path),
  initialPath: () => storage('macPath', state.home),
};

const androidSource = {
  kind: 'android',
  title: 'Android',
  available: deviceReady,
  unavailableMessage() {
    if (!state.adb.ok) return `ADB is not available: ${state.adb.error || 'unknown error'}.\nInstall it with: brew install --cask android-platform-tools`;
    const device = currentDevice();
    if (!device) {
      return 'No Android device connected.\n\nPlug in your phone with USB debugging enabled (Settings > System > Developer options), or click "Wi-Fi" to connect wirelessly.';
    }
    if (device.state === 'unauthorized') return 'Unlock your phone and tap "Allow" on the USB debugging prompt.';
    return `The phone is ${device.state}. Waiting for it to become ready\u2026`;
  },
  list: (path, hidden) => api.get(`${devUrl('ls')}?${qs({ path, hidden })}`),
  mkdir: (parent, name) => api.post(devUrl('mkdir'), { parent, name }),
  rename: (path, name) => api.post(devUrl('rename'), { path, name }),
  remove: (paths) => api.post(devUrl('delete'), { paths }),
  thumbUrl: (path) => `${devUrl('thumb')}?${qs({ path })}`,
  fileUrl: (path) => `${devUrl('file')}?${qs({ path })}`,
  home: () => '/sdcard',
  places: () =>
    [['Internal storage', '/sdcard'], ['Camera', '/sdcard/DCIM/Camera'], ['Pictures', '/sdcard/Pictures'],
      ['Screenshots', '/sdcard/Pictures/Screenshots'], ['Download', '/sdcard/Download'], ['Documents', '/sdcard/Documents'],
      ['Movies', '/sdcard/Movies'], ['Music', '/sdcard/Music'], ['Root (/)', '/']].map(([label, path]) => ({ label, path })),
  crumbBase(path) {
    for (const base of ['/sdcard', '/storage/emulated/0']) {
      if (path === base || path.startsWith(`${base}/`)) return { label: 'Internal storage', path: base };
    }
    return { label: '/', path: '/' };
  },
  remember: (path) => store(`androidPath.${state.deviceId}`, path),
  initialPath: () => storage(`androidPath.${state.deviceId}`, '/sdcard'),
};

// ------------------------------------------------------------------ transfers
async function askOptions(summary) {
  const defaults = { conflict: state.settings.default_conflict, verify: state.settings.verify_checksum };
  if (!state.settings.ask_before_transfer) return defaults;
  const choice = await transferOptions({ summary, ...defaults });
  if (!choice) return null;
  if (choice.remember) {
    try {
      state.settings = await api.put('/api/settings', {
        default_conflict: choice.conflict,
        verify_checksum: choice.verify,
        ask_before_transfer: false,
      });
      toast('Saved as default. You can change this in Settings.');
    } catch (err) {
      toast(err.message, 'error');
    }
  }
  return choice;
}

function describe(sources, direction, destDir) {
  const what = sources.length === 1 ? `"${basename(sources[0])}"` : `${sources.length} items`;
  return `Copy ${what} ${direction === 'push' ? 'to the phone' : 'to the Mac'}, into ${destDir}`;
}

async function startTransfer(direction, sources, destDir) {
  if (!deviceReady()) {
    toast('Connect and select an Android device first.', 'error');
    return;
  }
  if (!sources.length || !destDir) {
    toast('Select files or folders to copy first.');
    return;
  }
  const options = await askOptions(describe(sources, direction, destDir));
  if (!options) return;
  try {
    const job = await api.post('/api/transfers', {
      device_id: state.deviceId,
      direction,
      sources,
      dest_dir: destDir,
      conflict: options.conflict,
      verify: options.verify,
    });
    transfers.upsert(job);
    transfers.expand();
  } catch (err) {
    toast(err.message, 'error');
  }
}

async function handleFinderDrop(pane, dataTransfer, destDir) {
  const pending = collectDropped(dataTransfer);
  const toAndroid = pane.source.kind === 'android';
  if (toAndroid && !deviceReady()) {
    toast('Connect and select an Android device first.', 'error');
    return;
  }
  let files;
  try {
    files = await pending;
  } catch (err) {
    toast(`Could not read dropped files: ${err.message}`, 'error');
    return;
  }
  if (!files.length) {
    toast('Nothing to copy (empty folders are skipped).');
    return;
  }
  const options = await askOptions(
    `Copy ${files.length} dropped file${files.length === 1 ? '' : 's'} ${toAndroid ? 'to the phone' : 'to the Mac'}, into ${destDir}`,
  );
  if (!options) return;
  const banner = $('#upload-banner');
  banner.hidden = false;
  banner.textContent = `Preparing ${files.length} file(s)\u2026`;
  try {
    const uploadId = await uploadAll(files, (done, total, bytes, totalBytes) => {
      banner.textContent = `Reading dropped files: ${done}/${total} (${formatBytes(bytes)} of ${formatBytes(totalBytes)})`;
    });
    const res = await api.post(`/api/uploads/${uploadId}/commit`, {
      target: toAndroid ? 'android' : 'mac',
      device_id: toAndroid ? state.deviceId : null,
      dest_dir: destDir,
      conflict: options.conflict,
      verify: options.verify,
    });
    if (res.job) {
      transfers.upsert(res.job);
      transfers.expand();
    } else {
      toast(`Copied ${res.placed.length} item(s)`, 'success');
      macPane.refresh();
    }
  } catch (err) {
    toast(err.message, 'error');
  } finally {
    banner.hidden = true;
  }
}

function covers(pane, dir) {
  return pane.path && (dir === pane.path || dir.startsWith(`${pane.path.replace(/\/$/, '')}/`));
}

function onJobFinished(job) {
  if (job.status === 'completed') toast(`Finished: ${job.title}`, 'success');
  else if (job.status === 'failed') toast(`Transfer failed: ${job.error || job.title}`, 'error', 8000);
  const target = job.direction === 'push' ? androidPane : macPane;
  if (covers(target, job.dest_dir)) target.refresh();
}

function updateMiddleButtons() {
  const ready = deviceReady();
  $('#btn-to-android').disabled = !ready || !macPane.selected.size || !androidPane.path;
  $('#btn-to-mac').disabled = !ready || !androidPane.selected.size || !macPane.path;
}

// -------------------------------------------------------------------- devices
function renderDevices() {
  const select = $('#device-select');
  select.replaceChildren();
  if (!state.devices.length) {
    select.append(new Option('No device', ''));
    select.disabled = true;
  } else {
    select.disabled = false;
    for (const d of state.devices) {
      const how = d.connection ? (d.connection === 'usb' ? 'USB' : 'Wi-Fi') : d.state;
      select.append(new Option(`${d.name} (${how})`, d.id));
    }
    select.value = state.deviceId ?? '';
  }
  const device = currentDevice();
  const badge = $('#device-badge');
  if (device?.connection) {
    badge.textContent = device.connection === 'usb' ? 'USB' : 'Wi-Fi';
    badge.className = `badge ${device.connection}`;
  } else {
    badge.textContent = device ? device.state : '';
    badge.className = device ? 'badge warn' : 'badge';
  }
  const adb = $('#adb-status');
  adb.hidden = state.adb.ok;
  adb.textContent = 'ADB unavailable';
  adb.title = state.adb.error || '';
}

function loadAndroid() {
  const device = currentDevice();
  androidPane.setSubtitle(device ? `${device.name}${device.android_version ? ` \u00B7 Android ${device.android_version}` : ''}` : '');
  if (deviceReady()) androidPane.load(androidSource.initialPath());
  else androidPane.showMessage(androidSource.unavailableMessage());
  updateMiddleButtons();
}

function onDevices(snapshot) {
  const before = currentDevice();
  const wasReady = deviceReady();
  state.devices = snapshot.devices;
  state.adb = snapshot.adb;
  if (!currentDevice()) {
    const pick = state.devices.find((d) => d.state === 'device') || state.devices[0];
    state.deviceId = pick ? pick.id : null;
    store('device', state.deviceId);
  }
  renderDevices();
  const now = currentDevice();
  if (before?.id !== now?.id || wasReady !== deviceReady()) {
    loadAndroid();
  } else if (now && before?.connection && now.connection && before.connection !== now.connection) {
    toast(`Now using ${now.connection === 'usb' ? 'USB' : 'Wi-Fi'} for ${now.name}`);
    androidPane.setSubtitle(`${now.name}${now.android_version ? ` \u00B7 Android ${now.android_version}` : ''}`);
  } else if (!deviceReady()) {
    androidPane.showMessage(androidSource.unavailableMessage());
  }
  wireless.onDevices();
  updateMiddleButtons();
}

// ----------------------------------------------------------------------- init
async function init() {
  let session;
  try {
    session = await api.get('/api/session');
  } catch (err) {
    if (err.status === 401) $('#locked').hidden = false;
    else toast(err.message, 'error', 15000);
    return;
  }
  state.home = session.home;
  state.settings = session.settings;

  const handlers = {
    onTransfer: (from, to, paths, destDir) => startTransfer(from.source.kind === 'mac' ? 'push' : 'pull', paths, destDir),
    onFinderDrop: handleFinderDrop,
    onSelection: () => macPane && androidPane && updateMiddleButtons(),
  };
  macPane = new FilePane($('#pane-mac'), macSource, handlers);
  androidPane = new FilePane($('#pane-android'), androidSource, handlers);
  macPane.setShowHidden(state.settings.show_hidden);
  androidPane.setShowHidden(state.settings.show_hidden);
  macPane.setSubtitle('This Mac');
  transfers = new TransfersPanel($('#transfers'), { onFinished: onJobFinished });
  wireless = new WirelessDialog(() => ({ devices: state.devices, deviceId: state.deviceId }));

  $('#device-select').addEventListener('change', (e) => {
    state.deviceId = e.target.value || null;
    store('device', state.deviceId);
    renderDevices();
    loadAndroid();
  });
  $('#btn-wireless').addEventListener('click', () => wireless.open(deviceReady() && currentDevice().connection === 'usb' ? 'usb' : 'qr'));
  $('#btn-settings').addEventListener('click', async () => {
    const changes = await settingsDialog(state.settings);
    if (!changes) return;
    try {
      state.settings = await api.put('/api/settings', changes);
      macPane.setShowHidden(state.settings.show_hidden);
      androidPane.setShowHidden(state.settings.show_hidden);
      macPane.refresh();
      if (deviceReady()) androidPane.refresh();
      toast('Settings saved', 'success');
    } catch (err) {
      toast(err.message, 'error');
    }
  });
  $('#btn-to-android').addEventListener('click', () => startTransfer('push', macPane.selectedPaths(), androidPane.path));
  $('#btn-to-mac').addEventListener('click', () => startTransfer('pull', androidPane.selectedPaths(), macPane.path));

  // Dropping files outside the panes must not navigate the page away.
  window.addEventListener('dragover', (e) => e.preventDefault());
  window.addEventListener('drop', (e) => e.preventDefault());

  onDevices(session.devices);
  transfers.setAll(session.jobs);
  await macPane.load(macSource.initialPath());

  connectEvents(
    (event) => {
      switch (event.type) {
        case 'snapshot':
          onDevices(event.data.devices);
          transfers.setAll(event.data.jobs);
          break;
        case 'devices':
          onDevices(event.data);
          break;
        case 'job':
          transfers.upsert(event.data);
          break;
        case 'jobs_removed':
          transfers.remove(event.data);
          break;
        case 'qr_status':
          wireless.onQrStatus(event.data);
          break;
        default:
          break;
      }
    },
    (connected) => {
      $('#conn-lost').hidden = connected;
      if (!connected) {
        api.get('/api/session').catch((err) => {
          if (err.status === 401) $('#locked').hidden = false;
        });
      }
    },
  );
}

init();
