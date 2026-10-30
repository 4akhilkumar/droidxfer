# droidxfer — Progress Log

Mac <-> Android file transfer app (Python, ADB-based). This file is the hand-off document:
if a session runs out of context, share this file + the repo in a new session to continue.

## Agreed requirements / decisions
- Approach A: ADB only (developer options + USB debugging). No Android companion app.
- Wired USB preferred; Wi-Fi fallback (Android 11+ wireless debugging: pair code / QR, `adb connect`;
  also "enable Wi-Fi from USB" via `tcpip:5555`). Same phone on USB+Wi-Fi is grouped by `ro.serialno`, USB preferred.
- Target phone: Pixel 7 Pro, Android 15+ (sync v2 STA2/LIS2 used; v1 fallback kept).
- Python 3.11.16, managed with **uv**. Package name `droidxfer`, src layout.
- Own ADB client (asyncio sockets to adb server 127.0.0.1:5037) — no adbutils/ppadb.
- UI: localhost web UI (primary) + Typer CLI. Frontend: native HTML/CSS/JS only (no frameworks).
- Mac access: whole home directory (paths confined to `~`; Mac deletes go to `~/.Trash`).
- v1 features: multi-device, delete/rename/mkdir on device, thumbnails (Pillow), checksum verify (md5), QR pairing (segno SVG).

## Environment notes
- Installed via Homebrew: `uv` (/opt/homebrew/bin/uv), `android-platform-tools` cask (adb 37.0.1).
- Python 3.11.16 available via uv (`~/.local/bin/python3.11`).
- Run: `uv run droidxfer serve` (opens browser with token URL). Tests: `uv run pytest`.

## Architecture
```
src/droidxfer/
  adb/        protocol.py (host conn, framing, errors) · client.py (AdbClient: host services, transport)
              sync.py (SyncConnection: LIST/LIS2/STAT/STA2/SEND/RECV) · shell.py (shell v2) · locate.py (find/start adb)
  core/       config.py · models.py · events.py (EventBus) · devices.py (DeviceManager, track-devices)
              wireless.py (pair/connect/QR/tcpip) · localfs.py (home-confined Mac FS) · remotefs.py (Android FS ops)
              transfer.py (TransferEngine) · thumbs.py (Pillow thumbnail cache) · paths.py (validation)
  api/        app.py (FastAPI factory) · security.py (token cookie, Host/Origin/Sec-Fetch checks) · routes.py · ws.py
  web/        index.html · styles.css · js/*.js (ES modules)
  cli.py      Typer: serve, devices, ls, push, pull, pair, connect, disconnect, tcpip, mdns
tests/        fake_adb.py (in-process fake adb server) + tests
```

### Security model
- Bind 127.0.0.1 only. Random token per run -> `/?token=...` sets HttpOnly SameSite=Strict cookie, redirects to `/`.
- Every /api request + WS needs cookie; Host header must be localhost/127.0.0.1:<port>;
  `Sec-Fetch-Site` must be same-origin/none; Origin (if present) must match.
- Mac paths resolved (realpath) and confined to home. Android paths normalised, no NUL/newline; shell args `shlex.quote`d.

## Milestones / status
- [x] Plan approved
- [x] M1 scaffold + adb locate + device list
- [x] M2 ADB protocol + sync + CLI ls/push/pull
- [x] M3 TransferEngine (queue, folders, cancel/pause/retry, conflicts, mtime, verify, media scan)
- [x] M4 Web API + dual-pane UI + drag/drop + progress
- [x] M5 Wireless (pair code, QR, connect, tcpip from USB, auto USB/Wi-Fi switching)
- [x] M6 Security hardening, tests (34 passing), README
- [x] Live test on the real Pixel 7 Pro over USB (Android 17, serial 35091FDH3000EJ) — all passed (session 2)
- [ ] Live test over Wi-Fi (QR / pairing code / tcpip) and USB->Wi-Fi failover by unplugging

## Key behaviours / gotchas (for future sessions)
- Starlette TestClient WebSockets ignore base_url host and cookies -> tests pass `host`/`cookie` headers explicitly.
- Uploads (Finder drops) are staged in `cache_dir()/uploads/<id>` (inside home so LocalFS allows them),
  pushed via a normal job with `cleanup=[staging]`; staging is kept for FAILED jobs so retry works.
- Transport drop mid-file => engine waits up to 30 s for the device on any transport (USB/Wi-Fi), retries the file (3 attempts).
- Pull writes to `.<rand>.droidxfer-part` then `os.replace`; push partials are `rm -f`'d on file errors/cancel.
- `DROIDXFER_HOME` env var relocates config + cache (used by tests).

- Session 1: DONE tests/ (fake_adb.py, test_adb.py, test_core.py, test_api.py) -> 34 passed.
  Smoke-tested `droidxfer serve` in browser (Mac pane, empty-device state, QR dialog OK) and CLI (`devices`, `ls`).
  README written. Remaining: live test with the Pixel.
- Session 2: live USB tests on Pixel 7 Pro: CLI push/pull of nested folder (unicode names, empty dir, 300 MB)
  with --verify -> byte-identical round trip, mtimes kept. Speed ~33-40 MB/s, same as `adb push/pull` on random data
  (adb is only faster on compressible data because it uses sendrecv_v2 compression; we don't yet).
  Web API: browse, mkdir/rename/delete, protected delete refused, conflict rename/skip, thumbnails, preview,
  Finder-style upload, folder pull, pause/resume (+verify), cancel push/pull (no partial files left) -> all OK.
  Web UI: drag Mac->Android, arrow-button Android->Mac, live progress/speed/ETA, thumbnail grid -> OK.
  Test artifacts were deleted afterwards (user approved).
- Session 2: README now has "Setup on a new Mac" (brew/uv/adb, uv sync, phone prep, serve, `uv tool install .`,
  Wi-Fi, troubleshooting). Verified wheel includes web/ assets.
## Possible next steps / ideas
- Live verification on device; tune throughput (IO_CHUNK, SYNC_DATA_MAX) if USB speed is low.
- HEIC thumbnails (pillow-heif), video thumbnails, HTTP Range support for remote previews.
- Zeroconf fallback if `adb mdns` discovery is unavailable.

## Log
- Session 1: env set up (uv + adb installed). Plan approved. Starting implementation.
- Session 1: DONE adb/ (protocol.py, sync.py, client.py [incl. shell v2, pair/connect/mdns/tcpip], locate.py),
  core/ (config, events, paths, localfs, devices, remotefs, wireless [QR via segno], transfer, thumbs, context).
  `uv sync` OK, imports OK. NEXT: cli.py, api/, web/, tests/, README.
- Session 1: DONE cli.py (serve/devices/ls/push/pull/pair/qr/connect/disconnect/tcpip/mdns),
  api/security.py (ASGI middleware), api/routes.py (REST+WS+uploads), api/app.py. NEXT: web/ UI, tests, README.
- Session 1: DONE web/ (index.html, styles.css, js/{util,api,dialogs,pane,transfers,upload,wireless,app}.js).
  NEXT: tests/ (fake adb server), run pytest, smoke-test `droidxfer serve`, README.
