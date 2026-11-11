# droidxfer — Progress Log

Mac <-> Android file transfer app (Python, ADB-based). This file is the hand-off document:
if a session runs out of context, share this file + the repo in a new session to continue.

## Agreed requirements / decisions
- Approach A: ADB only (developer options + USB debugging). No Android companion app.
- Wired USB preferred; Wi-Fi fallback (Android 11+ wireless debugging: pair code / QR, `adb connect`;
  also "enable Wi-Fi from USB" via `tcpip:5555`). Same phone on USB+Wi-Fi is grouped by `ro.serialno`, USB preferred.
- Target phone: Pixel 7 Pro, Android 15+ (sync v2 STA2/LIS2 used; v1 fallback kept).
- Python 3.12.6 (requires >= 3.11), managed with **uv**. Package name `droidxfer`, src layout.
- Own ADB client (asyncio sockets to adb server 127.0.0.1:5037) — no adbutils/ppadb.
- UI: localhost web UI (primary) + Typer CLI. Frontend: native HTML/CSS/JS only (no frameworks).
- Mac access: whole home directory plus `/Volumes/*` drives (setting `external_drives`, default on). Mac deletes use
  `/usr/bin/trash` (macOS 15+, system Trash) with a fallback of moving into `~/.Trash`.
- v1 features: multi-device, delete/rename/mkdir on device, thumbnails (Pillow), checksum verify (md5), QR pairing (segno SVG).

## Environment notes
- Installed via Homebrew: `uv` (/opt/homebrew/bin/uv), `android-platform-tools` cask (adb 37.0.1).
- Python 3.11.16 available via uv (`~/.local/bin/python3.11`).
- Run: `uv run droidxfer serve` (opens browser with token URL). Tests: `uv run pytest`.

## Architecture
```
src/droidxfer/
  adb/        protocol.py (host conn, framing, errors) · client.py (AdbClient: host services, transport, shell v2 + streaming)
              sync.py (SyncConnection: LIST/LIS2/STAT/STA2/SEND/RECV) · locate.py (find/start adb)
  core/       config.py · context.py (AppContext) · events.py (EventBus) · devices.py (DeviceManager, track-devices)
              wireless.py (pair/connect/QR/tcpip) · localfs.py (home-confined Mac FS) · remotefs.py (Android FS ops)
              transfer.py (TransferEngine) · thumbs.py (Pillow thumbnail cache) · paths.py (validation)
  api/        app.py (FastAPI factory) · security.py (token cookie, Host/Origin/Sec-Fetch checks) · routes.py (REST + WS)
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
- Session 3 (2026-11-10): code review + fixes on branch `improvements/bugfixes-and-features` (55 tests passing).
  Bugs: pause during planning was overwritten to "running"; a job could be retried twice (dupes + lost upload staging);
  no HTTP Range for phone files (Safari video, seeking); upload writes blocked the event loop; "prefer USB" didn't
  republish devices; history eviction wasn't sent to the UI; malformed config.json crashed startup; QR sessions leaked;
  `download_dir` setting was dead (removed). Features: `skip_identical` conflict policy (size + mtime), free space in
  pane status bars + early "not enough space" failure (rename policy only), HEIC thumbnails (pillow-heif), LRU-capped
  thumbnail cache (~200 MB), external drives (/Volumes), system Trash.
  Range reads at offset > 0 use `dd ... iflag=skip_bytes,count_bytes` (toybox) over a streamed shell; offset 0 uses sync RECV.
  Verified live on the Pixel 7 Pro over USB (toybox 0.8.13): `df -k` free space, dd Range reads byte-identical at 5 offsets
  (<0.1 s each, dd seeks), browser video seek on a 58 MB mp4 (206s, no server errors, no leftover dd), skip_identical
  push/pull incremental runs, HEIC thumbnail from the phone, Mac delete via /usr/bin/trash. Test files removed afterwards.

- Session 3, part 2 (2026-11-10): double-click on phone videos showed an empty player. Cause: /file responses carry
  CSP `sandbox` (opaque origin), so the browser's follow-up Range requests from the media document went without the
  SameSite cookie (401) / were blocked. Fix: in-app viewer (web/js/preview.js) plays media inside the app page; raw
  /file keeps the sandbox for anything opened in a tab. /preview endpoints render HEIC/TIFF as 2048 px JPEG.
  Also found: static assets had no Cache-Control, so browsers reused a weeks-old styles.css next to new JS after an
  update. Assets are now served under /static/<hash of web files>/ with `no-cache`, index.html rewritten at startup.
  New: phone info (core/deviceinfo.py, GET /api/devices/{id}/info, `droidxfer info`, web/js/deviceinfo.js): one shell
  round trip (getprop, dumpsys battery, /sys/class/power_supply/battery/*, meminfo, wm, df, uname, getenforce, su
  scan, uptime, GMS version). Firmware consistency compares only the build part of every *.build.fingerprint (the
  generic system image legitimately has product "generic_system"). Verified on the Pixel 7 Pro: all checks pass,
  494 cycles, 4,436/5,002 mAh (89%), battery made 2024-03-09. 73 tests passing.

- Session 4 (2026-11-11): MTP ("File transfer" mode, no USB debugging). Pure Python: pyusb + libusb-package (pip
  wheels bundle libusb; no Homebrew). New package `src/droidxfer/mtp/`: protocol.py (containers, datasets, prop
  lists), usblink.py (pyusb link + scan; never claims the interface while scanning; "PTP"-labelled interfaces are
  photo-only), session.py (worker-thread transactions, cancel/recover with USB port reset fallback, virtual paths:
  first storage = /sdcard plus /storage/emulated/0 aliases, others /storage/<name>), fs.py (MtpFS + MtpSync, which
  stands in for adb's SyncConnection in the transfer engine). core/mtpdevices.py polls USB every 2 s, opens a
  session lazily, closes it after 30 s idle or on unplug. Transport kind "mtp", grouped with adb by USB serial;
  adb preferred. Live-verified on the Pixel 7 Pro with USB debugging OFF: CLI push/ls/pull round trip identical
  with dates kept, skip_identical incremental, info (reduced), web UI browse/thumbnails/viewer seek (0.1 s),
  copy from the UI, delete. Raw speed (USB 3): ~200 MB/s up, ~230 MB/s down (8 MB GetPartialObject64 chunks).
  Gotchas found live: (1) every non-final bulk write in a data phase must be whole max-packet multiples, else the
  phone stalls (needs USB reset); (2) GetObjectInfo per file is ~21 ms -> list folders with one
  GetObjectPropList per property (Pixel rejects 0xFFFFFFFF "all props" with 0xA801); prop list with depth 1
  includes the folder itself; storage root = parent 0; (3) a crashed client leaves the phone's session open
  (0x201E) -> CloseSession + OpenSession; (4) MTP keeps the ObjectInfo modification date on disk (local time).
  96 tests passing (fake MTP device in tests/fake_mtp.py enforces the short-packet rule).

## Possible next steps / ideas
- Option to prefer MTP for transfers even when adb is available (about 5x faster on USB 3).
- Tune throughput (IO_CHUNK, SYNC_DATA_MAX); check Range/HEIC on a Samsung phone and in Safari.
- sendrecv_v2 compression; pipelining many small files; stream Finder drops straight to the phone instead of staging.
- Optional spec table per model (expected chip/RAM/storage) to strengthen the authenticity verdict.
- Video thumbnails; Zeroconf fallback if `adb mdns` discovery is unavailable.

## Log
- Session 1: env set up (uv + adb installed). Plan approved. Starting implementation.
- Session 1: DONE adb/ (protocol.py, sync.py, client.py [incl. shell v2, pair/connect/mdns/tcpip], locate.py),
  core/ (config, events, paths, localfs, devices, remotefs, wireless [QR via segno], transfer, thumbs, context).
  `uv sync` OK, imports OK. NEXT: cli.py, api/, web/, tests/, README.
- Session 1: DONE cli.py (serve/devices/ls/push/pull/pair/qr/connect/disconnect/tcpip/mdns),
  api/security.py (ASGI middleware), api/routes.py (REST+WS+uploads), api/app.py. NEXT: web/ UI, tests, README.
- Session 1: DONE web/ (index.html, styles.css, js/{util,api,dialogs,pane,transfers,upload,wireless,app}.js).
  NEXT: tests/ (fake adb server), run pytest, smoke-test `droidxfer serve`, README.
