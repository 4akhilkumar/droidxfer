# droidxfer

Transfer files between your Mac and an Android phone over ADB, using USB or Wi-Fi. It comes with a local web UI that has two panes and supports drag and drop, plus a command-line tool.

## Setup on a new Mac

### 1. Install the tools
Install [Homebrew](https://brew.sh) if you don't have it:
```bash
/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
```
Then install uv (manages Python and dependencies) and adb (Android platform tools):
```bash
brew install uv
brew install --cask android-platform-tools
uv --version && adb version
```
You don't need to install Python yourself. `uv` downloads Python 3.11.16, the version pinned in `.python-version`.

### 2. Get the code
Copy the project folder to the new Mac (for example `git clone <your-repo-url> mac_android`, or copy/unzip the folder). Then run:
```bash
cd mac_android
uv sync                 # creates .venv with Python 3.11.16 and all dependencies
uv run pytest           # optional: runs the test suite against a fake phone, no device needed
```

### 3. Prepare the phone (one time)
1. **Settings > About phone**, then tap **Build number** 7 times to enable Developer options.
2. **Settings > System > Developer options**, then turn on **USB debugging**.
3. Connect the phone with a USB **data** cable. Unlock it and tap **Allow** on the "Allow USB debugging?" prompt. Tick **Always allow from this computer**.
4. Check the connection:
   ```bash
   adb devices -l               # should show your phone with state "device"
   uv run droidxfer devices
   ```

### 4. Start the app
```bash
uv run droidxfer serve                 # opens the web UI in your browser
uv run droidxfer serve --port 9000     # use a different port
uv run droidxfer serve --no-browser    # just print the URL
```
The printed URL contains a one-time access token, so open it exactly as printed. Stop the server with `Ctrl+C`.
The first time the app opens Desktop, Documents or Downloads, macOS asks to allow access for your terminal app (Terminal, iTerm or VS Code). Click **Allow**.

### 5. Optional: install the `droidxfer` command globally
To run `droidxfer` from any folder without `uv run`:
```bash
uv tool install .            # run inside the project folder
droidxfer serve
uv tool install --reinstall .   # after updating the code
uv tool uninstall droidxfer     # to remove it
```

### 6. Optional: Wi-Fi
The phone and the Mac must be on the same Wi-Fi network. On the phone, turn on **Developer options > Wireless debugging**. Then in the app click **Wi-Fi…** and either:
- **QR code**: on the phone, tap **Pair device with QR code** and scan it.
- **Pairing code**: on the phone, tap **Pair device with pairing code**, enter the IP, port and code, then **Connect** using the IP & port shown on the main Wireless debugging screen.
- **From USB**: with the cable plugged in, switch to adb over Wi-Fi on port 5555. This lasts until the phone restarts.

### Troubleshooting
| Problem | Fix |
|---|---|
| `adb devices` shows nothing | Use a data cable (not charge-only), try another port, and check that USB debugging is on. Run `adb kill-server && adb start-server`. |
| Device is `unauthorized` | Unlock the phone and accept the prompt. If no prompt appears, go to **Developer options > Revoke USB debugging authorizations** and reconnect. |
| "ADB unavailable" in the UI | Check `which adb`. Set the full path in **Settings > adb path** (e.g. `/opt/homebrew/bin/adb`). |
| Android Studio is also installed | Two different adb versions will keep restarting each other's server. Use one adb (e.g. put Homebrew's first in `PATH`) or set the same path in Settings. |
| `address already in use` | Another server is already on that port. Use `--port 9000` or stop the other one. |
| "Session expired" in the browser | The server was restarted. Open the new URL printed in the terminal. |
| Wi-Fi device not found | Keep the Wireless debugging screen open on the phone, use the same network, and check that the router doesn't isolate clients (guest networks often do). |

Settings and saved Wi-Fi endpoints are stored in `~/Library/Application Support/droidxfer/`. Thumbnails are cached in `~/Library/Caches/droidxfer/`.

### Web UI
- Left pane: Mac (your home folder). Right pane: Android (starts at `/sdcard`).
- Drag files between the panes, or select them and use the arrow buttons. Dropping on a folder row copies into that folder.
- You can also drag files from Finder onto either pane.
- Select files with click, Cmd-click and Shift-click. Keyboard shortcuts: Cmd+A, Enter, Backspace (go up), F2 (rename), Delete.
- Toolbar actions: new folder, rename, delete (Mac items go to the Trash; deletes on the phone are permanent), thumbnail view, show hidden files.
- Transfer queue: shows progress, speed and time left, with pause, resume, cancel and retry.
- **Wi-Fi...** opens the wireless options: QR pairing, pairing code, connect, or "From USB" (`adb tcpip 5555`).
- If the phone is connected by both USB and Wi-Fi, USB is used. If the cable is unplugged during a transfer, it continues over Wi-Fi.

### CLI
```bash
uv run droidxfer devices
uv run droidxfer ls /sdcard/DCIM/Camera
uv run droidxfer push ~/Pictures/trip /sdcard/Pictures --conflict rename
uv run droidxfer pull /sdcard/DCIM/Camera ~/Downloads --verify
uv run droidxfer qr                       # pair over Wi-Fi by scanning a QR code
uv run droidxfer pair 192.168.1.20:37099 123456
uv run droidxfer connect 192.168.1.20:41235
uv run droidxfer tcpip                    # switch a USB phone to Wi-Fi
```

## Tests
```bash
uv run pytest
```
The tests use an in-process fake adb server (`tests/fake_adb.py`), so no phone is needed.

## Security
The server listens only on `127.0.0.1`. Access requires a random token for each run, stored in an HttpOnly, SameSite=Strict cookie. Requests with an unexpected Host, Origin or a cross-site `Sec-Fetch-Site` header are rejected. Mac paths are limited to your home folder. Protected locations on the phone (`/`, `/sdcard`, `/data`, ...) can't be deleted or renamed.
