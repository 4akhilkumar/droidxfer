"""An in-process fake Android MTP device backed by a local folder (the phone's shared storage)."""

from __future__ import annotations

import os
import shutil
import struct
from collections import deque
from pathlib import Path

from droidxfer.mtp import protocol as p
from droidxfer.mtp.usblink import Candidate

STORAGE = 0x10001
SUPPORTED_OPS = [
    p.GET_DEVICE_INFO, p.OPEN_SESSION, p.CLOSE_SESSION, p.GET_STORAGE_IDS, p.GET_STORAGE_INFO,
    p.GET_OBJECT_HANDLES, p.GET_OBJECT_INFO, p.GET_OBJECT, p.DELETE_OBJECT, p.SEND_OBJECT_INFO,
    p.SEND_OBJECT, p.GET_DEVICE_PROP_VALUE, p.GET_PARTIAL_OBJECT, p.GET_OBJECT_PROP_VALUE,
    p.SET_OBJECT_PROP_VALUE, p.GET_OBJECT_PROP_LIST, p.GET_PARTIAL_OBJECT_64,
]
_DATA_OUT = {p.SEND_OBJECT_INFO, p.SEND_OBJECT, p.SET_OBJECT_PROP_VALUE}


def _u32_array(values) -> bytes:
    values = list(values)
    return struct.pack(f"<I{len(values)}I", len(values), *values)


def _u16_array(values) -> bytes:
    values = list(values)
    return struct.pack(f"<I{len(values)}H", len(values), *values)


class FakeMtpDevice:
    def __init__(self, root: Path, *, serial: str = "MTPONLY1", max_packet: int = 512,
                 ops: list[int] | None = None) -> None:
        self.root = root  # what the phone calls "Internal shared storage"
        self.serial = serial
        self.max_packet = max_packet
        self.ops = list(SUPPORTED_OPS if ops is None else ops)
        self.locked = False
        self.battery = 77
        self.session_open = False
        self.opened_links = 0
        self.log: list[int] = []
        self._handles: dict[int, Path] = {}
        self._by_path: dict[Path, int] = {}
        self._next = 1
        self._pending: dict | None = None

    def candidate(self, mode: str = "mtp", key: str | None = None) -> Candidate:
        return Candidate(key or self.serial, self.serial, "Google", "Pixel 7 Pro", 0x18D1, 0x4EE1, mode,
                         self.link if mode == "mtp" else None)

    def link(self) -> "FakeLink":
        self.opened_links += 1
        return FakeLink(self)

    # objects -----------------------------------------------------------------------
    def handle(self, path: Path) -> int:
        if path not in self._by_path:
            self._by_path[path] = self._next
            self._handles[self._next] = path
            self._next += 1
        return self._by_path[path]

    def path(self, handle: int) -> Path:
        path = self._handles.get(handle)
        if path is None or not (path.exists() or path.is_symlink()):
            raise p.MtpError(p.INVALID_OBJECT_HANDLE)
        return path

    def children(self, parent: int) -> list[Path]:
        folder = self.root if parent in (p.ROOT_PARENT, 0) else self.path(parent)
        return sorted(folder.iterdir())

    def parent_handle(self, path: Path) -> int:
        return p.ROOT_PARENT if path.parent == self.root else self.handle(path.parent)

    def object_info(self, path: Path) -> bytes:
        st = path.stat()
        size = 0 if path.is_dir() else st.st_size
        date = p.format_date(st.st_mtime)
        fields = (STORAGE, p.FORMAT_FOLDER if path.is_dir() else p.FORMAT_UNDEFINED, 0, min(size, 0xFFFFFFFF),
                  0, 0, 0, 0, 0, 0, 0, self.parent_handle(path), 1 if path.is_dir() else 0, 0, 0)
        return (struct.pack("<IHHIHIIIIIIIHII", *fields) + p.pack_string(path.name)
                + p.pack_string(date) + p.pack_string(date) + p.pack_string(""))

    def prop_value(self, path: Path, code: int) -> bytes:
        st = path.stat()
        if code == p.PROP_FILE_NAME:
            return struct.pack("<H", 0xFFFF) + p.pack_string(path.name)
        if code == p.PROP_FORMAT:
            return struct.pack("<HH", 0x0004, p.FORMAT_FOLDER if path.is_dir() else p.FORMAT_UNDEFINED)
        if code == p.PROP_SIZE:
            return struct.pack("<HQ", 0x0008, 0 if path.is_dir() else st.st_size)
        if code == p.PROP_DATE_MODIFIED:
            return struct.pack("<H", 0xFFFF) + p.pack_string(p.format_date(st.st_mtime))
        if code == p.PROP_STORAGE_ID:
            return struct.pack("<HI", 0x0006, STORAGE)
        raise p.MtpError(0xA801)

    # operations ----------------------------------------------------------------------
    def handle_op(self, code: int, params: tuple[int, ...], data: bytes | None) -> tuple[bytes | None, tuple[int, ...]]:
        self.log.append(code)
        if code not in self.ops:
            raise p.MtpError(0x2005)
        if code == p.GET_DEVICE_INFO:
            body = struct.pack("<HIH", 100, 6, 100) + p.pack_string("microsoft.com: 1.0; android.com: 1.0;")
            body += struct.pack("<H", 0) + _u16_array(self.ops) + _u16_array([]) + _u16_array([p.DEVICE_PROP_BATTERY])
            body += _u16_array([]) + _u16_array([])
            body += p.pack_string("Google") + p.pack_string("Pixel 7 Pro") + p.pack_string("1.0") + p.pack_string("abc")
            return body, ()
        if code == p.OPEN_SESSION:
            if self.session_open:
                raise p.MtpError(p.SESSION_ALREADY_OPEN)
            self.session_open = True
            return None, ()
        if code == p.CLOSE_SESSION:
            self.session_open = False
            return None, ()
        if not self.session_open:
            raise p.MtpError(p.SESSION_NOT_OPEN)
        if code == p.GET_STORAGE_IDS:
            return _u32_array([] if self.locked else [STORAGE]), ()
        if code == p.GET_STORAGE_INFO:
            usage = shutil.disk_usage(self.root)
            body = struct.pack("<HHHQQI", 3, 2, 0, usage.total, usage.free, 0xFFFFFFFF)
            return body + p.pack_string("Internal shared storage") + p.pack_string(""), ()
        if code == p.GET_OBJECT_HANDLES:
            return _u32_array(self.handle(c) for c in self.children(params[2])), ()
        if code == p.GET_OBJECT_INFO:
            return self.object_info(self.path(params[0])), ()
        if code == p.GET_OBJECT_PROP_LIST:
            parent, _fmt, prop, _group, _depth = params
            if prop == p.ALL:
                raise p.MtpError(0xA801)  # like the Pixel: one property at a time
            paths = self.children(parent)
            if parent not in (0, p.ROOT_PARENT):
                paths = [self.path(parent), *paths]  # Android includes the folder itself
            body = struct.pack("<I", len(paths))
            for path in paths:
                body += struct.pack("<IH", self.handle(path), prop) + self.prop_value(path, prop)
            return body, ()
        if code == p.GET_OBJECT_PROP_VALUE:
            return self.prop_value(self.path(params[0]), params[1])[2:], ()
        if code == p.GET_OBJECT:
            return self.path(params[0]).read_bytes(), ()
        if code in (p.GET_PARTIAL_OBJECT_64, p.GET_PARTIAL_OBJECT):
            if code == p.GET_PARTIAL_OBJECT_64:
                offset, length = params[1] | (params[2] << 32), params[3]
            else:
                offset, length = params[1], params[2]
            with open(self.path(params[0]), "rb") as fh:
                fh.seek(offset)
                return fh.read(length), ()
        if code == p.SEND_OBJECT_INFO:
            info = p.parse_object_info(0, data)
            folder = self.root if params[1] in (p.ROOT_PARENT, 0) else self.path(params[1])
            target = folder / info.name
            if info.is_dir:
                target.mkdir()
                self._pending = None
            else:
                self._pending = {"path": target, "mtime": info.mtime}
            return None, (STORAGE, params[1], self.handle(target))
        if code == p.SEND_OBJECT:
            pending, self._pending = self._pending, None
            if pending is None:
                raise p.MtpError(0x2002)
            pending["path"].write_bytes(data)
            if pending["mtime"]:
                os.utime(pending["path"], (pending["mtime"], pending["mtime"]))
            return None, ()
        if code == p.DELETE_OBJECT:
            target = self.path(params[0])
            shutil.rmtree(target) if target.is_dir() else target.unlink()
            return None, ()
        if code == p.SET_OBJECT_PROP_VALUE:
            target = self.path(params[0])
            new_name, _ = p.read_string(data, 0)
            renamed = target.with_name(new_name)
            target.rename(renamed)
            del self._by_path[target]
            self._by_path[renamed] = params[0]
            self._handles[params[0]] = renamed
            return None, ()
        if code == p.GET_DEVICE_PROP_VALUE:
            return bytes([self.battery]), ()
        raise p.MtpError(0x2005)


class FakeLink:
    """Implements droidxfer.mtp.usblink.Link with USB-like packet rules."""

    def __init__(self, device: FakeMtpDevice) -> None:
        self.device = device
        self.max_packet = device.max_packet
        self._out: deque[bytes] = deque()
        self._cmd: tuple[int, int, tuple[int, ...]] | None = None
        self._data = bytearray()
        self._data_len = 0
        self.closed = False
        self.cancelled: list[int] = []

    def write(self, data: bytes, timeout: int) -> None:
        if not data:
            return  # zero-length packet
        if self._cmd is not None and self._cmd[0] in _DATA_OUT:
            self._data += data
            if not self._data_len:
                self._data_len = p.HEADER.unpack_from(self._data)[0]
            if len(self._data) < self._data_len:
                if len(data) % self.max_packet:
                    # A short packet ends the transfer early: a real phone stalls and the host times out.
                    self._cmd = None
                    raise TimeoutError("short packet in the middle of a data phase")
                return
            code, tid, params = self._cmd
            self._cmd = None
            payload = bytes(self._data[p.HEADER.size : self._data_len])
            self._data.clear()
            self._data_len = 0
            self._respond(code, tid, params, payload)
            return
        length, kind, code, tid = p.HEADER.unpack_from(data)
        assert kind == p.CMD, "expected a command container"
        params = struct.unpack(f"<{(length - 12) // 4}I", data[12:length])
        if code in _DATA_OUT:
            self._cmd = (code, tid, params)
            return
        self._respond(code, tid, params, None)

    def _respond(self, code: int, tid: int, params, payload) -> None:
        try:
            data, rparams = self.device.handle_op(code, params, payload)
            rcode = p.OK
        except p.MtpError as exc:
            data, rparams, rcode = None, (), exc.code
        if data is not None:
            self._queue(p.pack_container(p.DATA, code, tid, data))
        self._queue(p.pack_container(p.RESPONSE, rcode, tid, p.pack_params(*rparams)))

    def _queue(self, container: bytes) -> None:
        self._out.append(container)
        if len(container) % self.max_packet == 0:
            self._out.append(b"")  # zero-length packet after a full-packet transfer

    def read(self, size: int, timeout: int) -> bytes:
        if not self._out:
            raise TimeoutError("nothing to read")
        transfer = self._out.popleft()
        if len(transfer) > size:
            self._out.appendleft(transfer[size:])
            return transfer[:size]
        return transfer

    def cancel(self, tid: int) -> None:
        self.cancelled.append(tid)
        self._cmd = None
        self._data.clear()
        self._data_len = 0
        self._out.clear()

    def device_status(self) -> int:
        return p.OK

    def clear_halts(self) -> None:
        pass

    def reset(self) -> None:
        self.device.session_open = False

    def close(self) -> None:
        self.closed = True
