"""Path validation helpers for Android (POSIX) paths and file names."""

from __future__ import annotations

import posixpath
from itertools import count
from typing import Callable

_FORBIDDEN_CHARS = ("\x00", "\n", "\r")

PROTECTED_REMOTE = frozenset(
    {
        "/",
        "/sdcard",
        "/sdcard/Android",
        "/storage",
        "/storage/emulated",
        "/storage/emulated/0",
        "/storage/emulated/0/Android",
        "/storage/self",
        "/storage/self/primary",
        "/data",
        "/system",
        "/vendor",
        "/product",
        "/apex",
        "/proc",
        "/sys",
        "/dev",
        "/mnt",
    }
)


class PathError(ValueError):
    """Invalid or disallowed path."""


def normalize_remote(path: str) -> str:
    if not isinstance(path, str) or not path:
        raise PathError("empty path")
    if any(c in path for c in _FORBIDDEN_CHARS):
        raise PathError("path contains forbidden characters")
    if not path.startswith("/"):
        raise PathError(f"remote path must be absolute: {path!r}")
    normalized = posixpath.normpath(path)
    if normalized.startswith("//"):
        normalized = "/" + normalized.lstrip("/")
    return normalized


def remote_join(directory: str, name: str) -> str:
    return normalize_remote(posixpath.join(normalize_remote(directory), validate_name(name)))


def check_remote_mutable(path: str) -> str:
    """Refuse to delete/rename top-level or system-critical locations on the device."""
    normalized = normalize_remote(path)
    if normalized in PROTECTED_REMOTE or normalized.count("/") < 2:
        raise PathError(f"refusing to modify protected location {normalized}")
    return normalized


def validate_name(name: str) -> str:
    if not name or name in (".", "..") or "/" in name or any(c in name for c in _FORBIDDEN_CHARS):
        raise PathError(f"invalid file name: {name!r}")
    if len(name.encode("utf-8", "surrogateescape")) > 255:
        raise PathError("file name too long")
    return name


def unique_name(name: str, exists: Callable[[str], bool], *, is_dir: bool = False) -> str:
    """Return `name`, or `name (N).ext` for the first N that does not exist."""
    if not exists(name):
        return name
    stem, ext = (name, "") if is_dir else posixpath.splitext(name)
    for i in count(1):
        candidate = f"{stem} ({i}){ext}"
        if not exists(candidate):
            return candidate
    raise AssertionError("unreachable")
