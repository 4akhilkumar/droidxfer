"""An in-process fake adb server (host protocol + sync + shell v2) backed by a local directory."""

from __future__ import annotations

import asyncio
import errno
import hashlib
import os
import shlex
import shutil
import stat as statmod
import struct
import threading
from pathlib import Path

from droidxfer.adb.sync import _DENT_V1, _DENT_V2, _HDR, _STAT_V1, _STAT_V2

_SHELL_HDR = struct.Struct("<BI")


def lp(text: str) -> bytes:
    data = text.encode()
    return b"%04x" % len(data) + data


class FakeAdb:
    def __init__(
        self,
        root: Path,
        serial: str = "FAKE123",
        features: tuple[str, ...] = ("shell_v2", "stat_v2", "ls_v2", "cmd"),
    ) -> None:
        self.root = root
        self.serial = serial
        self.features = features
        self.port = 0
        self.shell_log: list[str] = []
        self.mdns_text = ""
        self.fail_recv_after: int | None = None  # drop connection after N bytes of RECV data
        fp = "google/cheetah/cheetah:15/AP4A.250105.002/12701944:user/release-keys"
        self.props: dict[str, str] = {
            "ro.product.brand": "google",
            "ro.product.manufacturer": "Google",
            "ro.product.model": "Pixel 7 Pro",
            "ro.product.device": "cheetah",
            "ro.product.name": "cheetah",
            "ro.product.first_api_level": "33",
            "ro.product.cpu.abi": "arm64-v8a",
            "ro.soc.manufacturer": "Google",
            "ro.soc.model": "GS201",
            "ro.hardware": "cheetah",
            "ro.boot.hardware.sku": "GP4BC",
            "ro.build.version.release": "15",
            "ro.build.version.sdk": "35",
            "ro.build.version.security_patch": "2025-01-05",
            "ro.build.display.id": "AP4A.250105.002",
            "ro.build.type": "user",
            "ro.build.tags": "release-keys",
            "ro.build.fingerprint": fp,
            "ro.vendor.build.fingerprint": fp,
            "ro.bootimage.build.fingerprint": fp,
            "ro.system.build.fingerprint": "google/generic_system_google/generic:15/AP4A.250105.002/12701944:user/release-keys",
            "ro.bootloader": "cloudripper-15.0-12345",
            "gsm.version.baseband": "g5300q-1,g5300q-1",
            "ro.boot.verifiedbootstate": "green",
            "ro.boot.flash.locked": "1",
            "ro.boot.vbmeta.device_state": "locked",
            "ro.secure": "1",
            "ro.debuggable": "0",
            "ro.crypto.state": "encrypted",
        }
        self.battery_sysfs: dict[str, str] = {
            "cycle_count": "494",
            "charge_full": "4436000",
            "charge_full_design": "5002000",
            "manufacturing_date": "1709942400",
            "first_usage_date": "0",
            "serial_number": "BATTSERIAL1",
        }
        self.su_paths: list[str] = []
        # package manager state: package -> flags
        self.packages: dict[str, dict] = {
            "com.google.android.apps.tips": {"system": True, "updated": True},
            "com.google.android.youtube": {"system": True, "updated": True},
            "com.google.android.apps.nexuslauncher": {"system": True},
            "com.android.systemui": {"system": True},
            "com.google.android.gms": {"system": True, "protected": True},
            "com.google.android.inputmethod.latin": {"system": True},
            "com.google.android.apps.messaging": {"system": True},
            "com.google.android.dialer": {"system": True},
            "com.facebook.appmanager": {"system": True},
            "com.example.unrated": {"system": True},
            "com.example.game": {"system": False},
            "com.samsung.android.app.tips": {"system": False, "installer": "com.samsung.android.app.omcagent"},
        }
        for flags in self.packages.values():
            flags.setdefault("enabled", True)
            flags.setdefault("installed", True)
        self.roles = {
            "home": "com.google.android.apps.nexuslauncher",
            "sms": "com.google.android.apps.messaging",
            "dialer": "com.google.android.dialer",
            "ime": "com.google.android.inputmethod.latin/com.android.inputmethod.latin.LatinIME",
        }
        self.selinux = "Enforcing"
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._server: asyncio.base_events.Server | None = None

    @property
    def devices_text(self) -> str:
        return f"{self.serial}               device usb:1-1 product:cheetah model:Pixel_7_Pro device:cheetah transport_id:1\n"

    # lifecycle -------------------------------------------------------------------
    def start(self) -> "FakeAdb":
        ready = threading.Event()

        def run() -> None:
            self._loop = asyncio.new_event_loop()
            self._server = self._loop.run_until_complete(asyncio.start_server(self._handle, "127.0.0.1", 0))
            self.port = self._server.sockets[0].getsockname()[1]
            ready.set()
            self._loop.run_forever()

        self._thread = threading.Thread(target=run, daemon=True)
        self._thread.start()
        ready.wait(5)
        return self

    def stop(self) -> None:
        if self._loop:
            self._loop.call_soon_threadsafe(self._loop.stop)
        if self._thread:
            self._thread.join(5)

    def path(self, device_path: str) -> Path:
        return self.root / device_path.lstrip("/")

    # protocol --------------------------------------------------------------------
    @staticmethod
    async def _read_request(r: asyncio.StreamReader) -> str:
        length = int(await r.readexactly(4), 16)
        return (await r.readexactly(length)).decode()

    async def _handle(self, r: asyncio.StreamReader, w: asyncio.StreamWriter) -> None:
        try:
            req = await self._read_request(r)
            if req == "host:version":
                w.write(b"OKAY" + lp("0029"))
            elif req in ("host:devices-l", "host:devices"):
                w.write(b"OKAY" + lp(self.devices_text))
            elif req == "host:track-devices-l":
                w.write(b"OKAY" + lp(self.devices_text))
                await w.drain()
                await r.read()
            elif req.startswith("host-serial:") and req.endswith(":features"):
                w.write(b"OKAY" + lp(",".join(self.features)))
            elif req.startswith("host:connect:"):
                w.write(b"OKAY" + lp(f"connected to {req[len('host:connect:'):]}"))
            elif req.startswith("host:disconnect:"):
                w.write(b"OKAY" + lp(f"disconnected {req[len('host:disconnect:'):]}"))
            elif req.startswith("host:pair:"):
                w.write(b"OKAY" + lp(f"Successfully paired to 192.168.1.50:37000 [guid=adb-{self.serial}-abcdef]"))
            elif req == "host:mdns:services":
                w.write(b"OKAY" + lp(self.mdns_text))
            elif req.startswith("host:transport:"):
                if req[len("host:transport:"):] != self.serial:
                    w.write(b"FAIL" + lp("device not found"))
                    return
                w.write(b"OKAY")
                svc = await self._read_request(r)
                if svc == "sync:":
                    w.write(b"OKAY")
                    await self._sync(r, w)
                elif svc.startswith("shell,v2,raw:"):
                    w.write(b"OKAY")
                    await self._shell(svc[len("shell,v2,raw:"):], w)
                elif svc.startswith("tcpip:"):
                    w.write(b"OKAY" + f"restarting in TCP mode port: {svc[6:]}\n".encode())
                else:
                    w.write(b"FAIL" + lp(f"unsupported service {svc}"))
            else:
                w.write(b"FAIL" + lp(f"unknown request {req}"))
            await w.drain()
        except (asyncio.IncompleteReadError, ConnectionError):
            pass
        finally:
            w.close()

    # shell -----------------------------------------------------------------------
    async def _shell(self, command: str, w: asyncio.StreamWriter) -> None:
        self.shell_log.append(command)
        out, code = self._run_shell(command)
        if out:
            w.write(_SHELL_HDR.pack(1, len(out)) + out)
        w.write(_SHELL_HDR.pack(3, 1) + bytes([code]))
        await w.drain()

    def _info_output(self) -> bytes:
        props = "\n".join(f"[{k}]: [{v}]" for k, v in sorted(self.props.items()))
        battery = (
            "Current Battery Service state:\n  AC powered: false\n  USB powered: true\n  Wireless powered: false\n"
            "  status: 2\n  health: 2\n  present: true\n  level: 57\n  scale: 100\n  voltage: 4171\n"
            "  temperature: 363\n  technology: Li-ion\n"
        )
        sysbat = "\n".join(f"{k}={v}" for k, v in self.battery_sysfs.items())
        parts = [
            ("props", props), ("battery", battery), ("sysbat", sysbat),
            ("meminfo", "MemTotal:       11743512 kB\nMemAvailable:    5000000 kB"), ("cpus", "8"),
            ("wm", "Physical size: 1440x3120\nPhysical density: 560"),
            ("df", "Filesystem 1K-blocks Used Available Use% Mounted on\n/dev/block/dm-65 115249236 68075864 47042300 60% /data"),
            ("kernel", "6.1.99-android14"), ("selinux", self.selinux), ("su", "\n".join(self.su_paths)),
            ("uptime", "43948.59 153377.77"), ("gms", "    versionName=26.43.30 (260400-1006944126)"),
        ]
        return ("\n".join(f"@@{name}\n{body}" for name, body in parts) + "\n@@end\n").encode()

    def _apps_output(self) -> bytes:
        def line(pkg: str, flags: dict, detailed: bool) -> str:
            if not detailed:
                return f"package:{pkg}"
            where = f"/data/app/~~x==/{pkg}-y==/base.apk" if flags.get("updated") or not flags["system"] else f"/system/app/{pkg}/{pkg}.apk"
            return f"package:{where}={pkg} versionCode:1 uid:10{len(pkg)}"
        pk = self.packages
        parts = [
            ("all", [line(p, f, True) for p, f in pk.items() if f["installed"] or f["system"]]),
            ("installed", [line(p, f, False) for p, f in pk.items() if f["installed"]]),
            ("system", [line(p, f, False) for p, f in pk.items() if f["system"]]),
            ("disabled", [line(p, f, False) for p, f in pk.items() if f["installed"] and not f["enabled"]]),
            ("installers", [f"package:{p}  installer={f.get('installer', 'com.android.vending')}"
                            for p, f in pk.items() if f["installed"] and not f["system"]]),
            ("home", [self.roles["home"]]), ("sms", [self.roles["sms"]]), ("dialer", [self.roles["dialer"]]),
            ("ime", [self.roles["ime"]]),
        ]
        return ("\n".join(f"@@{name}\n" + "\n".join(lines) for name, lines in parts) + "\n@@end\n").encode()

    def _pm(self, toks: list[str]) -> tuple[bytes, int]:
        verb, pkg = toks[1] if toks[0] == "pm" else toks[2], toks[-1]
        flags = self.packages.get(pkg)
        if flags is None:
            return f"Error: Unknown package: {pkg}\n".encode(), 1
        if verb in ("disable-user", "enable", "uninstall") and flags.get("protected"):
            return (f"Exception occurred while executing '{verb}':\njava.lang.SecurityException: Shell cannot change "
                    f"component state for {pkg}\n").encode(), 255
        if verb == "disable-user":
            flags["enabled"] = False
            return f"Package {pkg} new state: disabled-user\n".encode(), 0
        if verb == "enable":
            flags["enabled"] = True
            return f"Package {pkg} new state: enabled\n".encode(), 0
        if verb == "uninstall":
            flags["installed"] = False
            if not flags["system"]:
                del self.packages[pkg]  # a user app is gone completely
            return b"Success\n", 0
        if verb == "install-existing":
            if not flags["system"]:
                return f"Package {pkg} doesn't exist\n".encode(), 1
            flags["installed"] = True
            return f"Package {pkg} installed for user: 0\n".encode(), 0
        return f"Unknown command: {verb}\n".encode(), 1

    def _run_shell(self, command: str) -> tuple[bytes, int]:
        if command.startswith("echo @@all"):
            return self._apps_output(), 0
        if command.startswith(("pm ", "cmd package ")):
            return self._pm(shlex.split(command))
        if command.startswith("echo @@props"):
            return self._info_output(), 0
        if command.startswith("getprop"):
            return f"{self.serial}\nGoogle\nPixel 7 Pro\n15\n35\n".encode(), 0
        lexer = shlex.shlex(command, posix=True, punctuation_chars=True)
        lexer.whitespace_split = True
        groups: list[list[str]] = [[]]
        for tok in lexer:
            if tok == ";":
                groups.append([])
            else:
                groups[-1].append(tok)
        out = b""
        for toks in filter(None, groups):
            name, flags = toks[0], [t for t in toks[1:] if t.startswith("-") and t != "--"]
            args = [t for t in toks[1:] if not t.startswith("-")]
            if name == "mkdir":
                for a in args:
                    p = self.path(a)
                    if "-p" in flags:
                        p.mkdir(parents=True, exist_ok=True)
                    elif p.exists():
                        return f"mkdir: '{a}': File exists\n".encode(), 1
                    else:
                        p.mkdir()
            elif name == "rm":
                for a in args:
                    p = self.path(a)
                    if p.is_dir() and not p.is_symlink():
                        shutil.rmtree(p)
                    elif p.exists() or p.is_symlink():
                        p.unlink()
            elif name == "mv":
                os.rename(self.path(args[0]), self.path(args[1]))
            elif name == "md5sum":
                digest = hashlib.md5(self.path(args[0]).read_bytes()).hexdigest()
                out += f"{digest}  {args[0]}\n".encode()
            elif name == "dd":
                opts = dict(t.split("=", 1) for t in toks[1:] if "=" in t)
                with open(self.path(opts["if"]), "rb") as fh:
                    fh.seek(int(opts.get("skip", 0)))
                    out += fh.read(int(opts["count"])) if "count" in opts else fh.read()
            elif name == "df":
                usage = shutil.disk_usage(self.path(args[0]))
                out += b"Filesystem 1K-blocks Used Available Use% Mounted on\n"
                out += f"/dev/fuse {usage.total // 1024} {usage.used // 1024} {usage.free // 1024} 1% /storage/emulated\n".encode()
            elif name == "ip":
                out += b"3: wlan0: <UP>\n    inet 192.168.1.50/24 brd 192.168.1.255 scope global wlan0\n"
        return out, 0

    # sync ------------------------------------------------------------------------
    async def _sync(self, r: asyncio.StreamReader, w: asyncio.StreamWriter) -> None:
        while True:
            ident, length = _HDR.unpack(await r.readexactly(8))
            arg = (await r.readexactly(length)).decode("utf-8", "surrogateescape")
            if ident == b"QUIT":
                return
            if ident in (b"STA2", b"LST2"):
                w.write(self._stat_v2(ident, arg))
            elif ident == b"STAT":
                try:
                    st = os.lstat(self.path(arg))
                    w.write(_STAT_V1.pack(b"STAT", st.st_mode, st.st_size, int(st.st_mtime)))
                except OSError:
                    w.write(_STAT_V1.pack(b"STAT", 0, 0, 0))
            elif ident in (b"LIS2", b"LIST"):
                self._list(ident, arg, w)
            elif ident == b"SEND":
                if not await self._recv_file(arg, r, w):
                    return
            elif ident == b"RECV":
                if not await self._send_file(arg, w):
                    return
            await w.drain()

    def _stat_v2(self, ident: bytes, arg: str) -> bytes:
        try:
            p = self.path(arg)
            st = os.stat(p) if ident == b"STA2" else os.lstat(p)
        except OSError as exc:
            return _STAT_V2.pack(ident, exc.errno or errno.ENOENT, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0)
        mtime = int(st.st_mtime)
        return _STAT_V2.pack(ident, 0, 0, st.st_ino, st.st_mode, 1, 0, 0, st.st_size, mtime, mtime, mtime)

    def _list(self, ident: bytes, arg: str, w: asyncio.StreamWriter) -> None:
        directory = self.path(arg)
        entries = []
        if directory.is_dir():
            entries = [(".", os.lstat(directory)), *[(e.name, e.stat(follow_symlinks=False)) for e in os.scandir(directory)]]
        for name, st in entries:
            raw = name.encode("utf-8", "surrogateescape")
            mtime = int(st.st_mtime)
            if ident == b"LIS2":
                w.write(_DENT_V2.pack(b"DNT2", 0, 0, st.st_ino, st.st_mode, 1, 0, 0, st.st_size, mtime, mtime, mtime, len(raw)) + raw)
            else:
                w.write(_DENT_V1.pack(b"DENT", st.st_mode, st.st_size, mtime, len(raw)) + raw)
        if ident == b"LIS2":
            w.write(_DENT_V2.pack(b"DONE", 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0))
        else:
            w.write(_DENT_V1.pack(b"DONE", 0, 0, 0, 0))

    async def _recv_file(self, spec: str, r: asyncio.StreamReader, w: asyncio.StreamWriter) -> bool:
        path, _, mode = spec.rpartition(",")
        target = self.path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        ok = not target.is_dir()
        with open(os.devnull if not ok else target, "wb") as fh:
            while True:
                ident, length = _HDR.unpack(await r.readexactly(8))
                if ident == b"DATA":
                    fh.write(await r.readexactly(length))
                elif ident == b"DONE":
                    mtime = length
                    break
                else:
                    return False
        if not ok:
            msg = f"{path}: Is a directory".encode()
            w.write(_HDR.pack(b"FAIL", len(msg)) + msg)
            await w.drain()
            return False
        os.utime(target, (mtime, mtime))
        assert statmod.S_ISREG(int(mode))
        w.write(_HDR.pack(b"OKAY", 0))
        return True

    async def _send_file(self, path: str, w: asyncio.StreamWriter) -> bool:
        target = self.path(path)
        if not target.is_file():
            msg = f"remote object '{path}' does not exist".encode()
            w.write(_HDR.pack(b"FAIL", len(msg)) + msg)
            await w.drain()
            return False
        sent = 0
        with open(target, "rb") as fh:
            while chunk := fh.read(64 * 1024):
                if self.fail_recv_after is not None and sent >= self.fail_recv_after:
                    return False
                w.write(_HDR.pack(b"DATA", len(chunk)) + chunk)
                sent += len(chunk)
                await w.drain()
        w.write(_HDR.pack(b"DONE", 0))
        return True
