"""File-system view of an MTP session using the same shapes as the adb code (RemoteStat/RemoteEntry)."""

from __future__ import annotations

import asyncio
import os
import posixpath
import stat as statmod
from typing import AsyncIterator, BinaryIO

from ..adb.sync import CheckpointFn, ProgressFn, RemoteEntry, RemoteStat
from .session import MtpSession, Node

DIR_MODE = statmod.S_IFDIR | 0o775
FILE_MODE = statmod.S_IFREG | 0o664


def _stat(node: Node) -> RemoteStat:
    return RemoteStat(mode=DIR_MODE if node.is_dir else FILE_MODE, size=0 if node.is_dir else node.size, mtime=node.mtime)


def _entry(node: Node) -> RemoteEntry:
    return RemoteEntry(
        mode=DIR_MODE if node.is_dir else FILE_MODE, size=0 if node.is_dir else node.size,
        mtime=node.mtime, name=node.path.rsplit("/", 1)[1],
    )


class MtpFS:
    def __init__(self, session: MtpSession) -> None:
        self.session = session

    # reads -----------------------------------------------------------------------------
    async def stat(self, path: str) -> RemoteStat | None:
        node = await self.session.resolve(path)
        return _stat(node) if node else None

    async def list_entries(self, path: str) -> list[RemoteEntry]:
        return [_entry(n) for n in await self.session.list_nodes(path)]

    async def listing(self, path: str, *, show_hidden: bool) -> dict:
        canonical = self.session._canonical(path)
        nodes = await self.session.list_nodes(canonical)
        entries = []
        for n in nodes:
            name = n.path.rsplit("/", 1)[1]
            if name.startswith(".") and not show_hidden:
                continue
            entries.append({
                "name": name, "path": n.path, "is_dir": n.is_dir, "is_link": False,
                "size": 0 if n.is_dir else n.size, "mtime": n.mtime, "hidden": name.startswith("."),
            })
        entries.sort(key=lambda x: (not x["is_dir"], x["name"].casefold()))
        parent = posixpath.dirname(canonical) if canonical != "/" else None
        return {"path": canonical, "parent": parent, "root": "/", "entries": entries}

    async def _file(self, path: str) -> Node:
        node = await self.session.resolve(path)
        if node is None:
            raise FileNotFoundError(path)
        if node.is_dir:
            raise IsADirectoryError(path)
        return node

    async def iter_range(self, path: str, start: int = 0, length: int | None = None,
                         checkpoint: CheckpointFn | None = None) -> AsyncIterator[bytes]:
        node = await self._file(path)
        async for chunk in self.session.iter_read(node, start, length, checkpoint):
            yield chunk

    async def read_bytes(self, path: str, limit: int) -> bytes:
        node = await self._file(path)
        if node.size > limit:
            raise ValueError(f"{path} is larger than {limit} bytes")
        return b"".join([c async for c in self.session.iter_read(node)])

    async def storage(self, path: str) -> dict:
        st = await self.session.storage_for(path)
        return {"total": st.capacity, "free": st.free}

    # writes ----------------------------------------------------------------------------
    async def _dir(self, path: str) -> Node:
        node = await self.session.resolve(path)
        if node is None:
            raise FileNotFoundError(path)
        if not node.is_dir:
            raise NotADirectoryError(path)
        return node

    async def mkdir(self, parent: str, name: str) -> str:
        parent_node = await self._dir(parent)
        if any(n.path.rsplit("/", 1)[1] == name for n in await self.session.list_nodes(parent_node.path)):
            raise FileExistsError(posixpath.join(parent_node.path, name))
        return (await self.session.mkdir(parent_node, name)).path

    async def mkdirs(self, paths: list[str]) -> None:
        for path in sorted(set(paths), key=lambda x: x.count("/")):
            if await self.session.resolve(path) is not None:
                continue
            parent, name = posixpath.split(self.session._canonical(path))
            await self.mkdirs([parent])
            await self.session.mkdir(await self._dir(parent), name)

    async def rename(self, path: str, new_name: str) -> str:
        node = await self.session.resolve(path)
        if node is None:
            raise FileNotFoundError(path)
        await self.session.rename(node, new_name)
        return posixpath.join(posixpath.dirname(node.path), new_name)

    async def delete(self, paths: list[str]) -> None:
        for path in paths:
            node = await self.session.resolve(path)
            if node is None:
                raise FileNotFoundError(path)
            await self.session.delete(node)

    async def remove_quietly(self, path: str) -> None:
        try:
            await self.delete([path])
        except Exception:
            pass

    async def push(self, src: BinaryIO, dst: str, *, size: int, mtime: int,
                   progress: ProgressFn | None = None) -> None:
        parent_path, name = posixpath.split(self.session._canonical(dst))
        parent = await self._dir(parent_path)
        existing = await self.session.resolve(dst)
        if existing is not None:
            # MTP can't overwrite in place: replace the old file.
            if existing.is_dir:
                raise IsADirectoryError(dst)
            await self.session.delete(existing)
        await self.session.upload(parent, name, size, mtime, src.read, progress)


class MtpSync:
    """Stands in for adb's SyncConnection inside the transfer engine and RemoteFS.walk."""

    stat_v2 = ls_v2 = True

    def __init__(self, fs: MtpFS) -> None:
        self.fs = fs
        self.broken = False

    async def __aenter__(self) -> "MtpSync":
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()

    async def close(self) -> None:
        pass  # the session is shared and closed by the manager when idle

    async def stat(self, path: str, *, follow_links: bool = True) -> RemoteStat | None:
        return await self.fs.stat(path)

    async def list(self, path: str) -> list[RemoteEntry]:
        # adb's sync LIST answers an empty list for a missing folder; the engine relies on that
        # (it creates the destination folder afterwards).
        try:
            return await self.fs.list_entries(path)
        except FileNotFoundError:
            return []

    async def push(self, src: BinaryIO, dst: str, *, mtime: int, mode: int = 0o644,
                   progress: ProgressFn | None = None, checkpoint: CheckpointFn | None = None) -> None:
        if checkpoint is not None:
            await checkpoint()  # an upload is one MTP transaction: pausing takes effect between files
        size = (await asyncio.to_thread(os.fstat, src.fileno())).st_size
        await self.fs.push(src, dst, size=size, mtime=mtime, progress=progress)

    async def iter_pull(self, src: str, *, checkpoint: CheckpointFn | None = None) -> AsyncIterator[bytes]:
        async for chunk in self.fs.iter_range(src, checkpoint=checkpoint):
            yield chunk

    async def pull(self, src: str, dst: BinaryIO, *, progress: ProgressFn | None = None,
                   checkpoint: CheckpointFn | None = None) -> int:
        total = 0
        async for chunk in self.fs.iter_range(src, checkpoint=checkpoint):
            await asyncio.to_thread(dst.write, chunk)
            total += len(chunk)
            if progress:
                progress(len(chunk))
        return total

    async def read_bytes(self, src: str, limit: int) -> bytes:
        return await self.fs.read_bytes(src, limit)
