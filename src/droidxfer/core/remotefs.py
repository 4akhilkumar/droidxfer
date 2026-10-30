"""File operations on the Android device (browse, mkdir, rename, delete, md5, media scan)."""

from __future__ import annotations

import posixpath
import shlex
import stat as statmod
from dataclasses import dataclass
from typing import AsyncIterator
from urllib.parse import quote as url_quote

from ..adb import AdbClient, RemoteStat, ShellResult, SyncConnection
from ..adb.protocol import AdbCommandError
from .devices import DeviceManager
from .paths import PathError, check_remote_mutable, normalize_remote, remote_join

MAX_SHELL_CMD = 32 * 1024
MEDIA_EXTS = frozenset(
    ".jpg .jpeg .png .gif .webp .heic .heif .bmp .dng .mp4 .mkv .mov .3gp .webm .avi "
    ".mp3 .m4a .aac .flac .ogg .opus .wav .amr".split()
)
SHARED_STORAGE_PREFIXES = ("/sdcard/", "/storage/")


class RemoteOpError(Exception):
    pass


@dataclass(slots=True)
class RemoteFile:
    path: str
    rel: str
    size: int
    mtime: int


def _q(path: str) -> str:
    return shlex.quote(path)


def _batches(prefix: str, args: list[str]) -> list[str]:
    commands, current = [], prefix
    for arg in args:
        piece = " " + _q(arg)
        if len(current) + len(piece) > MAX_SHELL_CMD and current != prefix:
            commands.append(current)
            current = prefix
        current += piece
    if current != prefix:
        commands.append(current)
    return commands


class RemoteFS:
    def __init__(self, client: AdbClient, devices: DeviceManager) -> None:
        self.client = client
        self.devices = devices

    async def sync(self, device_id: str) -> SyncConnection:
        t = self.devices.transport(device_id)
        return await self.client.sync(t.serial, t.features)

    async def shell(self, device_id: str, command: str, timeout: float = 120) -> ShellResult:
        t = self.devices.transport(device_id)
        return await self.client.shell(t.serial, command, v2="shell_v2" in t.features, timeout=timeout)

    async def _checked(self, device_id: str, command: str) -> ShellResult:
        res = await self.shell(device_id, command)
        if not res.ok:
            msg = (res.stderr or res.stdout).decode("utf-8", "replace").strip()
            raise RemoteOpError(msg or f"command failed with exit code {res.exit_code}")
        return res

    async def stat(self, device_id: str, path: str) -> RemoteStat | None:
        async with await self.sync(device_id) as sc:
            return await sc.stat(normalize_remote(path))

    async def list(self, device_id: str, path: str, *, show_hidden: bool = False) -> dict:
        path = normalize_remote(path)
        entries = []
        async with await self.sync(device_id) as sc:
            st = await sc.stat(path)
            if st is None:
                raise FileNotFoundError(path)
            if not st.is_dir:
                raise NotADirectoryError(path)
            for e in await sc.list(path):
                if e.name.startswith(".") and not show_hidden:
                    continue
                full = posixpath.join(path, e.name)
                is_dir, size, mtime = e.is_dir, e.size, e.mtime
                if e.is_link:
                    try:
                        target = await sc.stat(full)
                    except AdbCommandError:
                        target = None
                    if target:
                        is_dir, size, mtime = target.is_dir, target.size, target.mtime
                entries.append(
                    {
                        "name": e.name,
                        "path": full,
                        "is_dir": is_dir,
                        "is_link": e.is_link,
                        "size": 0 if is_dir else size,
                        "mtime": mtime,
                        "hidden": e.name.startswith("."),
                    }
                )
        entries.sort(key=lambda x: (not x["is_dir"], x["name"].casefold()))
        parent = posixpath.dirname(path) if path != "/" else None
        return {"path": path, "parent": parent, "root": "/", "entries": entries}

    async def mkdir(self, device_id: str, parent: str, name: str) -> str:
        target = remote_join(parent, name)
        await self._checked(device_id, f"mkdir -- {_q(target)}")
        return target

    async def mkdirs(self, device_id: str, paths: list[str]) -> None:
        for command in _batches("mkdir -p --", [normalize_remote(p) for p in paths]):
            await self._checked(device_id, command)

    async def rename(self, device_id: str, path: str, new_name: str) -> str:
        src = check_remote_mutable(path)
        dst = remote_join(posixpath.dirname(src), new_name)
        if await self.stat(device_id, dst) is not None:
            raise FileExistsError(dst)
        await self._checked(device_id, f"mv -- {_q(src)} {_q(dst)}")
        return dst

    async def delete(self, device_id: str, paths: list[str]) -> list[str]:
        targets = [check_remote_mutable(p) for p in paths]
        if not targets:
            raise PathError("nothing to delete")
        for command in _batches("rm -rf --", targets):
            await self._checked(device_id, command)
        return targets

    async def remove_quietly(self, device_id: str, path: str) -> None:
        try:
            await self.shell(device_id, f"rm -f -- {_q(normalize_remote(path))}", timeout=15)
        except Exception:
            pass

    async def md5(self, device_id: str, path: str) -> str:
        res = await self._checked(device_id, f"md5sum -- {_q(normalize_remote(path))}")
        digest = res.text.split(maxsplit=1)[0] if res.text.strip() else ""
        if len(digest) != 32:
            raise RemoteOpError(f"unexpected md5sum output: {res.text.strip()!r}")
        return digest.lower()

    async def media_scan(self, device_id: str, paths: list[str]) -> None:
        """Ask MediaStore to index pushed media so it appears in Gallery/Photos right away."""
        targets = [
            p for p in paths
            if p.startswith(SHARED_STORAGE_PREFIXES) and posixpath.splitext(p)[1].lower() in MEDIA_EXTS
        ]
        commands, current = [], []
        for p in targets:
            uri = "file://" + url_quote(p)
            current.append(
                f"am broadcast -a android.intent.action.MEDIA_SCANNER_SCAN_FILE -d {_q(uri)} >/dev/null 2>&1"
            )
            if sum(len(c) for c in current) > MAX_SHELL_CMD:
                commands.append("; ".join(current))
                current = []
        if current:
            commands.append("; ".join(current))
        for command in commands:
            await self.shell(device_id, command, timeout=120)

    async def walk(self, device_id: str, top: str, sc: SyncConnection) -> tuple[list[str], list[RemoteFile]]:
        """Recursively list `top` (a directory). Symlinked directories are not followed."""
        dirs: list[str] = []
        files: list[RemoteFile] = []
        stack = [""]
        while stack:
            rel_dir = stack.pop()
            abs_dir = posixpath.join(top, rel_dir) if rel_dir else top
            for e in await sc.list(abs_dir):
                rel = posixpath.join(rel_dir, e.name) if rel_dir else e.name
                full = posixpath.join(abs_dir, e.name)
                mode, size, mtime = e.mode, e.size, e.mtime
                if e.is_link:
                    target = await sc.stat(full)
                    if target is None or not target.is_file:
                        continue
                    mode, size, mtime = target.mode, target.size, target.mtime
                if statmod.S_ISDIR(mode):
                    dirs.append(rel)
                    stack.append(rel)
                elif statmod.S_ISREG(mode):
                    files.append(RemoteFile(full, rel, size, mtime))
        return dirs, files

    async def iter_file(self, device_id: str, path: str) -> AsyncIterator[bytes]:
        path = normalize_remote(path)
        sc = await self.sync(device_id)
        try:
            async for chunk in sc.iter_pull(path):
                yield chunk
        finally:
            sc.broken = True
            await sc.close()

    async def read_bytes(self, device_id: str, path: str, limit: int) -> bytes:
        async with await self.sync(device_id) as sc:
            return await sc.read_bytes(normalize_remote(path), limit)
