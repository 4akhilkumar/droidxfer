"""ADB file sync protocol (the `sync:` service): LIST/LIS2, STAT/STA2/LST2, SEND, RECV."""

from __future__ import annotations

import asyncio
import errno
import os
import stat as statmod
import struct
from dataclasses import dataclass
from typing import AsyncIterator, Awaitable, BinaryIO, Callable

from .protocol import AdbCommandError, AdbError, AdbStream

SYNC_DATA_MAX = 64 * 1024
IO_CHUNK = 1024 * 1024
MAX_PATH = 1024
FS_ENCODING_ERRORS = "surrogateescape"

ProgressFn = Callable[[int], None]
CheckpointFn = Callable[[], Awaitable[None]]

_HDR = struct.Struct("<4sI")
_STAT_V1 = struct.Struct("<4sIII")  # id, mode, size, mtime
_DENT_V1 = struct.Struct("<4sIIII")  # id, mode, size, mtime, namelen
_STAT_V2 = struct.Struct("<4sIQQIIIIQqqq")  # id, error, dev, ino, mode, nlink, uid, gid, size, atime, mtime, ctime
_DENT_V2 = struct.Struct("<4sIQQIIIIQqqqI")  # stat_v2 + namelen

_MISSING_ERRNOS = {errno.ENOENT, errno.ENOTDIR}


def encode_path(path: str) -> bytes:
    return path.encode("utf-8", FS_ENCODING_ERRORS)


def decode_name(raw: bytes) -> str:
    return raw.decode("utf-8", FS_ENCODING_ERRORS)


@dataclass(slots=True)
class RemoteStat:
    mode: int
    size: int
    mtime: int

    @property
    def is_dir(self) -> bool:
        return statmod.S_ISDIR(self.mode)

    @property
    def is_file(self) -> bool:
        return statmod.S_ISREG(self.mode)

    @property
    def is_link(self) -> bool:
        return statmod.S_ISLNK(self.mode)


@dataclass(slots=True)
class RemoteEntry(RemoteStat):
    name: str


class SyncConnection:
    """One `sync:` session on a device. Not safe for concurrent use; reuse sequentially."""

    def __init__(self, stream: AdbStream, features: set[str] | frozenset[str] = frozenset()) -> None:
        self._s = stream
        self.stat_v2 = "stat_v2" in features
        self.ls_v2 = "ls_v2" in features
        # After FAIL, cancellation or a dropped connection the session cannot be reused.
        self.broken = False

    async def __aenter__(self) -> "SyncConnection":
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()

    async def close(self) -> None:
        if not self.broken:
            try:
                await self._s.write(_HDR.pack(b"QUIT", 0))
            except AdbError:
                pass
        await self._s.close()

    async def _request(self, cmd: bytes, path: str) -> None:
        raw = encode_path(path)
        if len(raw) > MAX_PATH:
            raise AdbError(f"remote path too long ({len(raw)} bytes)")
        await self._s.write(_HDR.pack(cmd, len(raw)) + raw)

    async def _fail_message(self, length: int) -> str:
        self.broken = True
        return (await self._s.read_exactly(length)).decode("utf-8", "replace")

    async def stat(self, path: str, *, follow_links: bool = True) -> RemoteStat | None:
        """Return file info, or None when the path does not exist."""
        try:
            if self.stat_v2:
                cmd = b"STA2" if follow_links else b"LST2"
                await self._request(cmd, path)
                fields = _STAT_V2.unpack(await self._s.read_exactly(_STAT_V2.size))
                if fields[0] != cmd:
                    raise AdbError(f"unexpected stat reply {fields[0]!r}")
                error = fields[1]
                if error in _MISSING_ERRNOS:
                    return None
                if error:
                    raise AdbCommandError(f"{path}: {os.strerror(error)}")
                return RemoteStat(mode=fields[4], size=fields[8], mtime=fields[10])

            await self._request(b"STAT", path)
            ident, mode, size, mtime = _STAT_V1.unpack(await self._s.read_exactly(_STAT_V1.size))
            if ident != b"STAT":
                raise AdbError(f"unexpected stat reply {ident!r}")
            return None if mode == 0 else RemoteStat(mode=mode, size=size, mtime=mtime)
        except AdbCommandError:
            raise
        except BaseException:
            self.broken = True
            raise

    async def list(self, path: str) -> list[RemoteEntry]:
        entries: list[RemoteEntry] = []
        try:
            if self.ls_v2:
                await self._request(b"LIS2", path)
                while True:
                    f = _DENT_V2.unpack(await self._s.read_exactly(_DENT_V2.size))
                    if f[0] == b"DONE":
                        break
                    if f[0] != b"DNT2":
                        raise AdbError(f"unexpected list reply {f[0]!r}")
                    name = decode_name(await self._s.read_exactly(f[12]))
                    entries.append(RemoteEntry(mode=f[4], size=f[8], mtime=f[10], name=name))
            else:
                await self._request(b"LIST", path)
                while True:
                    ident, mode, size, mtime, namelen = _DENT_V1.unpack(
                        await self._s.read_exactly(_DENT_V1.size)
                    )
                    if ident == b"DONE":
                        break
                    if ident != b"DENT":
                        raise AdbError(f"unexpected list reply {ident!r}")
                    name = decode_name(await self._s.read_exactly(namelen))
                    entries.append(RemoteEntry(mode=mode, size=size, mtime=mtime, name=name))
        except BaseException:
            self.broken = True
            raise
        return [e for e in entries if e.name not in (".", "..")]

    async def push(
        self,
        src: BinaryIO,
        dst: str,
        *,
        mtime: int,
        mode: int = 0o644,
        progress: ProgressFn | None = None,
        checkpoint: CheckpointFn | None = None,
    ) -> None:
        await self._request(b"SEND", f"{dst},{statmod.S_IFREG | (mode & 0o777)}")
        try:
            while True:
                if checkpoint:
                    await checkpoint()
                buf = await asyncio.to_thread(src.read, IO_CHUNK)
                if not buf:
                    break
                view = memoryview(buf)
                for off in range(0, len(buf), SYNC_DATA_MAX):
                    piece = view[off : off + SYNC_DATA_MAX]
                    self._s.write_nowait(_HDR.pack(b"DATA", len(piece)))
                    self._s.write_nowait(piece)
                await self._s.drain()
                if progress:
                    progress(len(buf))
            await self._s.write(_HDR.pack(b"DONE", mtime & 0xFFFFFFFF))
            ident, length = _HDR.unpack(await self._s.read_exactly(_HDR.size))
        except BaseException:
            self.broken = True
            raise
        if ident == b"OKAY":
            return
        if ident == b"FAIL":
            raise AdbCommandError(await self._fail_message(length))
        self.broken = True
        raise AdbError(f"unexpected send reply {ident!r}")

    async def iter_pull(self, src: str, *, checkpoint: CheckpointFn | None = None) -> AsyncIterator[bytes]:
        await self._request(b"RECV", src)
        try:
            while True:
                if checkpoint:
                    await checkpoint()
                ident, length = _HDR.unpack(await self._s.read_exactly(_HDR.size))
                if ident == b"DATA":
                    yield await self._s.read_exactly(length)
                elif ident == b"DONE":
                    return
                elif ident == b"FAIL":
                    raise AdbCommandError(await self._fail_message(length))
                else:
                    raise AdbError(f"unexpected recv reply {ident!r}")
        except AdbCommandError:
            raise
        except BaseException:
            self.broken = True
            raise

    async def pull(
        self,
        src: str,
        dst: BinaryIO,
        *,
        progress: ProgressFn | None = None,
        checkpoint: CheckpointFn | None = None,
    ) -> int:
        total = 0
        pending = bytearray()
        try:
            async for chunk in self.iter_pull(src, checkpoint=checkpoint):
                pending += chunk
                total += len(chunk)
                if progress:
                    progress(len(chunk))
                if len(pending) >= IO_CHUNK:
                    await asyncio.to_thread(dst.write, bytes(pending))
                    pending.clear()
        except BaseException:
            self.broken = True
            raise
        if pending:
            await asyncio.to_thread(dst.write, bytes(pending))
        return total

    async def read_bytes(self, src: str, limit: int) -> bytes:
        """Read a whole remote file into memory, refusing files larger than `limit`."""
        buf = bytearray()
        async for chunk in self.iter_pull(src):
            buf += chunk
            if len(buf) > limit:
                self.broken = True
                raise AdbError(f"{src} is larger than {limit} bytes")
        return bytes(buf)
