"""MTP/PTP wire format: containers, operation codes and the datasets droidxfer needs.

Reference: USB Media Transfer Protocol 1.1 and Android's MtpServer (which adds the 64-bit partial read).
"""

from __future__ import annotations

import struct
import time
from dataclasses import dataclass
from datetime import datetime

# container types
CMD, DATA, RESPONSE, EVENT = 1, 2, 3, 4
HEADER = struct.Struct("<IHHI")  # length, type, code, transaction id

# operations
GET_DEVICE_INFO = 0x1001
OPEN_SESSION = 0x1002
CLOSE_SESSION = 0x1003
GET_STORAGE_IDS = 0x1004
GET_STORAGE_INFO = 0x1005
GET_OBJECT_HANDLES = 0x1007
GET_OBJECT_INFO = 0x1008
GET_OBJECT = 0x1009
DELETE_OBJECT = 0x100B
SEND_OBJECT_INFO = 0x100C
SEND_OBJECT = 0x100D
GET_DEVICE_PROP_VALUE = 0x1015
GET_PARTIAL_OBJECT = 0x101B
GET_OBJECT_PROP_VALUE = 0x9803
SET_OBJECT_PROP_VALUE = 0x9804
GET_OBJECT_PROP_LIST = 0x9805
GET_PARTIAL_OBJECT_64 = 0x95C1  # Android extension

# responses
OK = 0x2001
SESSION_NOT_OPEN = 0x2003
INVALID_OBJECT_HANDLE = 0x2009
STORE_FULL = 0x200C
ACCESS_DENIED = 0x200F
DEVICE_BUSY = 0x2019
SESSION_ALREADY_OPEN = 0x201E
TRANSACTION_CANCELLED = 0x201F

RESPONSE_NAMES = {
    0x2002: "general error",
    SESSION_NOT_OPEN: "session not open",
    0x2005: "operation not supported",
    0x2006: "parameter not supported",
    0x2008: "invalid storage",
    INVALID_OBJECT_HANDLE: "no such file",
    0x200B: "unsupported format",
    STORE_FULL: "phone storage is full",
    0x200D: "object is write-protected",
    0x200E: "storage is read-only",
    ACCESS_DENIED: "access denied",
    DEVICE_BUSY: "phone is busy",
    0x201A: "invalid parent folder",
    0x201D: "invalid parameter",
    SESSION_ALREADY_OPEN: "session already open",
    TRANSACTION_CANCELLED: "cancelled",
    0xA801: "invalid property",
}

# properties
PROP_STORAGE_ID = 0xDC01
PROP_FORMAT = 0xDC02
PROP_SIZE = 0xDC04
PROP_FILE_NAME = 0xDC07
PROP_DATE_MODIFIED = 0xDC09
DEVICE_PROP_BATTERY = 0x5001

FORMAT_UNDEFINED = 0x3000
FORMAT_FOLDER = 0x3001
ROOT_PARENT = 0xFFFFFFFF  # "storage root" in GetObjectHandles / SendObjectInfo
ALL = 0xFFFFFFFF

# class-specific control requests (still image class)
REQ_CANCEL = 0x64
REQ_DEVICE_RESET = 0x66
REQ_GET_DEVICE_STATUS = 0x67
CANCEL_EVENT = 0x4001

# data type codes used in property lists
_FIXED = {
    0x0001: "<b", 0x0002: "<B", 0x0003: "<h", 0x0004: "<H", 0x0005: "<i",
    0x0006: "<I", 0x0007: "<q", 0x0008: "<Q",
}
TYPE_UINT128, TYPE_INT128, TYPE_STRING = 0x000A, 0x0009, 0xFFFF


class MtpError(Exception):
    def __init__(self, code: int, operation: int | None = None, message: str | None = None) -> None:
        self.code = code
        self.operation = operation
        text = message or RESPONSE_NAMES.get(code, f"MTP error {code:#06x}")
        super().__init__(text)


def pack_container(kind: int, code: int, tid: int, payload: bytes = b"") -> bytes:
    length = HEADER.size + len(payload)
    return HEADER.pack(min(length, 0xFFFFFFFF), kind, code, tid) + payload


def pack_params(*params: int) -> bytes:
    return struct.pack(f"<{len(params)}I", *params)


# strings -----------------------------------------------------------------------------
def pack_string(text: str) -> bytes:
    if not text:
        return b"\x00"
    encoded = (text + "\x00").encode("utf-16-le", "surrogatepass")
    chars = len(encoded) // 2
    if chars > 255:
        raise ValueError("MTP strings are limited to 254 characters")
    return bytes([chars]) + encoded


def read_string(buf: bytes, off: int) -> tuple[str, int]:
    n = buf[off]
    off += 1
    raw = buf[off : off + n * 2]
    return raw.decode("utf-16-le", "replace").rstrip("\x00"), off + n * 2


def read_u16_array(buf: bytes, off: int) -> tuple[list[int], int]:
    n = struct.unpack_from("<I", buf, off)[0]
    return list(struct.unpack_from(f"<{n}H", buf, off + 4)), off + 4 + 2 * n


def read_u32_array(buf: bytes, off: int = 0) -> list[int]:
    n = struct.unpack_from("<I", buf, off)[0]
    return list(struct.unpack_from(f"<{n}I", buf, off + 4))


# dates: "YYYYMMDDThhmmss[.s][Z|±hhmm]" in the phone's local time ---------------------
def format_date(epoch: float) -> str:
    return time.strftime("%Y%m%dT%H%M%S", time.localtime(epoch))


def parse_date(text: str) -> int:
    if len(text) < 15:
        return 0
    try:
        stamp = datetime.strptime(text[:15], "%Y%m%dT%H%M%S")
    except ValueError:
        return 0
    return int(stamp.timestamp())  # naive -> interpreted in the Mac's local time zone


# datasets ----------------------------------------------------------------------------
@dataclass(slots=True)
class DeviceInfo:
    extension: str
    operations: frozenset[int]
    device_props: frozenset[int]
    manufacturer: str
    model: str
    version: str
    serial: str

    @property
    def is_android(self) -> bool:
        return "android.com" in self.extension


def parse_device_info(buf: bytes) -> DeviceInfo:
    ext, off = read_string(buf, 8)
    off += 2  # functional mode
    ops, off = read_u16_array(buf, off)
    _events, off = read_u16_array(buf, off)
    props, off = read_u16_array(buf, off)
    _capture, off = read_u16_array(buf, off)
    _playback, off = read_u16_array(buf, off)
    manufacturer, off = read_string(buf, off)
    model, off = read_string(buf, off)
    version, off = read_string(buf, off)
    serial, off = read_string(buf, off)
    return DeviceInfo(ext, frozenset(ops), frozenset(props), manufacturer, model, version, serial)


@dataclass(slots=True)
class StorageInfo:
    storage_id: int
    storage_type: int
    capacity: int
    free: int
    description: str
    label: str


def parse_storage_info(storage_id: int, buf: bytes) -> StorageInfo:
    storage_type, _fs, _access, capacity, free, _free_objects = struct.unpack_from("<HHHQQI", buf, 0)
    description, off = read_string(buf, 26)
    label, _ = read_string(buf, off)
    return StorageInfo(storage_id, storage_type, capacity, free, description, label)


@dataclass(slots=True)
class ObjectInfo:
    handle: int
    storage_id: int
    format: int
    size: int
    parent: int
    name: str
    mtime: int

    @property
    def is_dir(self) -> bool:
        return self.format == FORMAT_FOLDER


_OBJECT_INFO = struct.Struct("<IHHIHIIIIIIIHII")


def parse_object_info(handle: int, buf: bytes) -> ObjectInfo:
    f = _OBJECT_INFO.unpack_from(buf, 0)
    name, off = read_string(buf, _OBJECT_INFO.size)
    _created, off = read_string(buf, off)
    modified, off = read_string(buf, off)
    return ObjectInfo(handle, f[0], f[1], f[3], f[11], name, parse_date(modified))


def pack_object_info(storage_id: int, parent: int, name: str, size: int, *, folder: bool, mtime: float) -> bytes:
    fields = (
        storage_id,
        FORMAT_FOLDER if folder else FORMAT_UNDEFINED,
        0,  # protection
        min(size, 0xFFFFFFFF),
        0, 0, 0, 0, 0, 0, 0,  # thumbnail / image fields
        parent,
        1 if folder else 0,  # association type: generic folder
        0,
        0,
    )
    date = format_date(mtime)
    return _OBJECT_INFO.pack(*fields) + pack_string(name) + pack_string(date) + pack_string(date) + pack_string("")


def parse_prop_list(buf: bytes) -> list[tuple[int, int, object]]:
    """GetObjectPropList data: [(handle, property code, value)]."""
    count = struct.unpack_from("<I", buf, 0)[0]
    off = 4
    out: list[tuple[int, int, object]] = []
    for _ in range(count):
        handle, code, dtype = struct.unpack_from("<IHH", buf, off)
        off += 8
        if dtype == TYPE_STRING:
            value, off = read_string(buf, off)
        elif dtype in _FIXED:
            fmt = _FIXED[dtype]
            value = struct.unpack_from(fmt, buf, off)[0]
            off += struct.calcsize(fmt)
        elif dtype in (TYPE_UINT128, TYPE_INT128):
            lo, hi = struct.unpack_from("<QQ", buf, off)
            value = lo | (hi << 64)
            off += 16
        else:
            raise MtpError(0x2002, GET_OBJECT_PROP_LIST, f"unsupported property data type {dtype:#x}")
        out.append((handle, code, value))
    return out
