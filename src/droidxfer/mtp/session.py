"""An MTP session with one phone: blocking USB transactions on a worker thread, async API on top.

MTP allows one transaction at a time per session, so every operation takes `self._lock`. Long downloads
are split into partial reads so browsing, thumbnails and pausing still work between chunks; an upload
is a single transaction, so pausing it takes effect when the file is done.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import logging
import posixpath
import struct
import threading
import time
from dataclasses import dataclass
from typing import AsyncIterator, Callable

from . import protocol as p
from .usblink import Link, LinkError

log = logging.getLogger(__name__)

READ_CHUNK = 1024 * 1024  # bytes per USB read request (multiple of every max packet size)
PARTIAL_CHUNK = 8 * 1024 * 1024  # bytes per GetPartialObject call when downloading
CMD_TIMEOUT = 10_000
DATA_TIMEOUT = 30_000
SLOW_TIMEOUT = 120_000  # deleting a big folder can take a while
LIST_TTL = 30.0  # seconds a cached folder listing is trusted for path lookups

PRIMARY = "/sdcard"
_PRIMARY_ALIASES = ("/sdcard", "/storage/emulated/0", "/storage/self/primary", "/mnt/sdcard")


class Aborted(Exception):
    pass


@dataclass(slots=True)
class Node:
    """A resolved virtual path."""

    path: str
    storage_id: int  # 0 for virtual folders ("/", "/storage")
    handle: int  # p.ROOT_PARENT for a storage root
    is_dir: bool
    size: int = 0
    mtime: int = 0

    @property
    def is_storage_root(self) -> bool:
        return self.storage_id != 0 and self.handle == p.ROOT_PARENT


class MtpSession:
    def __init__(self, link: Link, *, name: str = "phone") -> None:
        self.link = link
        self.name = name
        self.info: p.DeviceInfo | None = None
        self.broken = False
        self.last_used = time.monotonic()
        self._lock = asyncio.Lock()
        self._executor = concurrent.futures.ThreadPoolExecutor(max_workers=1, thread_name_prefix=f"mtp-{name}")
        self._tid = 0
        self._listings: dict[str, tuple[float, list[Node]]] = {}
        self._storages: list[p.StorageInfo] = []
        self._mounts: dict[str, p.StorageInfo] = {}

    # lifecycle ---------------------------------------------------------------------
    async def open(self) -> None:
        self.info = p.parse_device_info((await self._run(p.GET_DEVICE_INFO))[0])
        if "microsoft.com" not in self.info.extension:
            raise LinkError("this device speaks PTP (photos only), not MTP. Choose File transfer on the phone.")
        try:
            await self._run(p.OPEN_SESSION, 1)
        except p.MtpError as exc:
            if exc.code != p.SESSION_ALREADY_OPEN:
                raise
            # A previous program (or a crashed run of ours) left a session open on the phone.
            await self._run(p.CLOSE_SESSION)
            await self._run(p.OPEN_SESSION, 1)
        await self.refresh_storages()

    async def close(self) -> None:
        if not self.broken:
            try:
                async with self._lock:
                    await self._call(self._transact, p.CLOSE_SESSION, (), timeout=2000)
            except Exception:
                pass
        await asyncio.get_running_loop().run_in_executor(self._executor, self.link.close)
        self._executor.shutdown(wait=False)

    def supports(self, op: int) -> bool:
        return bool(self.info and op in self.info.operations)

    # blocking transaction core (worker thread) -------------------------------------
    def _next_tid(self) -> int:
        self._tid = self._tid % 0xFFFFFFFE + 1
        return self._tid

    def _write_container(self, kind: int, code: int, tid: int, payload: bytes) -> None:
        data = p.pack_container(kind, code, tid, payload)
        self.link.write(data, CMD_TIMEOUT)
        if len(data) % self.link.max_packet == 0:
            self.link.write(b"", CMD_TIMEOUT)

    def _read(self, timeout: int) -> bytes:
        while True:
            chunk = self.link.read(READ_CHUNK, timeout)
            if chunk:  # skip zero-length packets that end a data phase
                return chunk

    def _transact(
        self,
        code: int,
        params: tuple[int, ...],
        *,
        data_out: bytes | None = None,
        producer: tuple[int, Callable[[int], bytes]] | None = None,
        sink: Callable[[bytes], None] | None = None,
        abort: threading.Event | None = None,
        timeout: int = DATA_TIMEOUT,
    ) -> tuple[bytes, tuple[int, ...]]:
        tid = self._next_tid()
        try:
            self._write_container(p.CMD, code, tid, p.pack_params(*params))
            if data_out is not None:
                self._write_container(p.DATA, code, tid, data_out)
            elif producer is not None:
                self._send_stream(code, tid, *producer, abort)
            data = bytearray()
            while True:
                first = self._read(timeout)
                length, kind, rcode, _rtid = p.HEADER.unpack_from(first)
                payload = first[p.HEADER.size :]
                if kind == p.DATA:
                    self._receive_data(length, payload, data, sink, abort)
                elif kind == p.RESPONSE:
                    rparams = struct.unpack(f"<{len(payload) // 4}I", payload[: len(payload) // 4 * 4])
                    if rcode != p.OK:
                        raise p.MtpError(rcode, code)
                    return bytes(data), rparams
        except Aborted:
            self._recover(tid)
            raise
        except TimeoutError:
            self._recover(tid)
            raise

    def _receive_data(self, length: int, payload: bytes, data: bytearray, sink, abort) -> None:
        unknown_length = length == 0xFFFFFFFF  # data phases over 4 GB
        remaining = None if unknown_length else length - p.HEADER.size - len(payload)
        last = len(payload) + p.HEADER.size
        while True:
            if payload:
                if sink is not None:
                    sink(payload)
                else:
                    data += payload
            if abort is not None and abort.is_set():
                raise Aborted()
            if remaining is not None and remaining <= 0:
                return
            if remaining is None and last < READ_CHUNK:
                return  # short transfer ends an unknown-length data phase
            payload = self.link.read(READ_CHUNK, DATA_TIMEOUT)
            last = len(payload)
            if remaining is not None:
                remaining -= len(payload)

    def _send_stream(self, code: int, tid: int, total: int, read: Callable[[int], bytes], abort) -> None:
        # A short USB packet ends the data phase, so every write except the last must be a whole number of
        # max-size packets; the remainder is carried into the next write.
        mps = self.link.max_packet
        buf = bytearray(p.HEADER.pack(min(p.HEADER.size + total, 0xFFFFFFFF), p.DATA, code, tid))
        sent = 0
        while sent < total:
            if abort is not None and abort.is_set():
                raise Aborted()
            chunk = read(min(READ_CHUNK * 4, total - sent))
            if not chunk:
                raise LinkError("the file got shorter while it was being sent")
            sent += len(chunk)
            buf += chunk
            if sent < total:
                cut = len(buf) - len(buf) % mps
                if cut:
                    self.link.write(bytes(buf[:cut]), DATA_TIMEOUT)
                    del buf[:cut]
        if buf:
            self.link.write(bytes(buf), DATA_TIMEOUT)
        if (p.HEADER.size + total) % mps == 0:
            self.link.write(b"", DATA_TIMEOUT)

    def _recover(self, tid: int) -> None:
        """Abort the transaction in progress (MTP cancel request), or mark the session unusable."""
        try:
            self.link.cancel(tid)
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline:
                try:
                    self.link.read(READ_CHUNK, 200)  # drain whatever the phone still sends
                except TimeoutError:
                    break
            self.link.clear_halts()
            while time.monotonic() < deadline:
                if self.link.device_status() == p.OK:
                    return
                time.sleep(0.05)
        except Exception as exc:
            log.info("MTP cancel failed: %s", exc)
        # The phone didn't accept the cancel and may be stuck mid-transfer: reset the USB port (like
        # re-plugging the cable). The manager opens a fresh session on the next request.
        log.warning("MTP transaction could not be cancelled; resetting the USB connection")
        self.link.reset()
        self.broken = True

    # async plumbing ----------------------------------------------------------------
    async def _call(self, fn, *args, **kwargs):
        if self.broken:
            raise LinkError("the MTP connection was reset; it will reconnect")
        loop = asyncio.get_running_loop()
        future = loop.run_in_executor(self._executor, lambda: fn(*args, **kwargs))
        try:
            return await asyncio.shield(future)
        except asyncio.CancelledError:
            abort = kwargs.get("abort")
            if abort is not None:
                abort.set()
            try:
                await future  # the worker stops at the next chunk and cancels the transaction
            except Exception:
                pass
            raise
        except LinkError:
            self.broken = True
            raise
        finally:
            self.last_used = time.monotonic()

    async def _run(self, code: int, *params: int, **kwargs) -> tuple[bytes, tuple[int, ...]]:
        async with self._lock:
            return await self._call(self._transact, code, params, **kwargs)

    # storages & virtual paths --------------------------------------------------------
    async def refresh_storages(self) -> list[p.StorageInfo]:
        ids = p.read_u32_array((await self._run(p.GET_STORAGE_IDS))[0])
        storages = []
        for sid in ids:
            data, _ = await self._run(p.GET_STORAGE_INFO, sid)
            storages.append(p.parse_storage_info(sid, data))
        self._storages = storages
        mounts: dict[str, p.StorageInfo] = {}
        for i, st in enumerate(storages):
            if i == 0:
                mounts[PRIMARY] = st
            else:
                label = (st.description or st.label or f"storage-{st.storage_id:x}").replace("/", "-")
                mounts[f"/storage/{label}"] = st
        self._mounts = mounts
        return storages

    def _require_unlocked(self) -> None:
        if not self._storages:
            raise PermissionError(
                "the phone shows no storage over MTP. Unlock it; if it's unlocked, choose File transfer again "
                "in the phone's USB notification."
            )

    @staticmethod
    def _canonical(path: str) -> str:
        path = posixpath.normpath("/" + path.lstrip("/"))
        for alias in _PRIMARY_ALIASES:
            if path == alias or path.startswith(alias + "/"):
                return PRIMARY + path[len(alias) :]
        return path

    def _mount_of(self, path: str) -> tuple[str, p.StorageInfo] | None:
        for mount, st in self._mounts.items():
            if path == mount or path.startswith(mount + "/"):
                return mount, st
        return None

    def _virtual_children(self, path: str) -> list[Node] | None:
        """Folders that exist only to reach the storages: "/" and "/storage"."""
        if path == "/":
            names = {m.split("/")[1] for m in self._mounts}
            return [Node(f"/{n}", 0, 0, True) for n in sorted(names)]
        if path == "/storage":
            extra = [m for m in self._mounts if m.startswith("/storage/")]
            return [Node(m, 0, 0, True) for m in extra] if extra else None
        return None

    async def list_nodes(self, path: str, *, fresh: bool = True) -> list[Node]:
        path = self._canonical(path)
        if not self._storages:
            await self.refresh_storages()
        self._require_unlocked()
        virtual = self._virtual_children(path)
        if virtual is not None:
            return virtual
        if not fresh and (cached := self._listings.get(path)) and time.monotonic() - cached[0] < LIST_TTL:
            return cached[1]
        node = await self.resolve(path)
        if node is None:
            raise FileNotFoundError(path)
        if not node.is_dir:
            raise NotADirectoryError(path)
        children = await self._fetch_children(node)
        self._listings[path] = (time.monotonic(), children)
        return children

    async def _fetch_children(self, node: Node) -> list[Node]:
        parent = node.handle
        if self.supports(p.GET_OBJECT_PROP_LIST):
            try:
                return await self._children_by_props(node)
            except p.MtpError as exc:
                log.info("GetObjectPropList failed (%s); listing object by object", exc)
        handles = p.read_u32_array((await self._run(p.GET_OBJECT_HANDLES, node.storage_id, 0, parent))[0])
        nodes = []
        for h in handles:
            info = await self.object_info(h)
            nodes.append(Node(posixpath.join(node.path, info.name), info.storage_id, h, info.is_dir,
                              info.size, info.mtime))
        return nodes

    async def _children_by_props(self, node: Node) -> list[Node]:
        # One request per property for all children: ~40x faster than one ObjectInfo per file.
        parent = 0 if node.is_storage_root else node.handle
        props: dict[int, dict[int, object]] = {}
        for code in (p.PROP_FILE_NAME, p.PROP_FORMAT, p.PROP_SIZE, p.PROP_DATE_MODIFIED, p.PROP_STORAGE_ID):
            data, _ = await self._run(p.GET_OBJECT_PROP_LIST, parent, 0, code, 0, 1)
            for handle, prop, value in p.parse_prop_list(data):
                props.setdefault(handle, {})[prop] = value
        nodes = []
        for handle, values in props.items():
            if handle == node.handle or values.get(p.PROP_STORAGE_ID, node.storage_id) != node.storage_id:
                continue
            name = str(values.get(p.PROP_FILE_NAME, ""))
            if not name:
                continue
            nodes.append(Node(
                posixpath.join(node.path, name), node.storage_id, handle,
                values.get(p.PROP_FORMAT) == p.FORMAT_FOLDER,
                int(values.get(p.PROP_SIZE, 0) or 0),
                p.parse_date(str(values.get(p.PROP_DATE_MODIFIED, ""))),
            ))
        return nodes

    async def resolve(self, path: str) -> Node | None:
        path = self._canonical(path)
        if not self._storages:
            await self.refresh_storages()
        self._require_unlocked()
        if path in ("/", "/storage"):
            return Node(path, 0, 0, True)
        found = self._mount_of(path)
        if found is None:
            return None
        mount, st = found
        node = Node(mount, st.storage_id, p.ROOT_PARENT, True)
        for part in [x for x in path[len(mount) :].split("/") if x]:
            children = await self.list_nodes(node.path, fresh=False)
            match = next((c for c in children if c.path.rsplit("/", 1)[1] == part), None)
            if match is None:
                # Maybe it was created on the phone after we cached the listing.
                children = await self.list_nodes(node.path, fresh=True)
                match = next((c for c in children if c.path.rsplit("/", 1)[1] == part), None)
            if match is None:
                return None
            node = match
        return node

    def invalidate(self, directory: str) -> None:
        directory = self._canonical(directory)
        for key in [k for k in self._listings if k == directory or k.startswith(directory.rstrip("/") + "/")]:
            del self._listings[key]

    # object operations ---------------------------------------------------------------
    async def object_info(self, handle: int) -> p.ObjectInfo:
        info = p.parse_object_info(handle, (await self._run(p.GET_OBJECT_INFO, handle))[0])
        if info.size == 0xFFFFFFFF:  # over 4 GB: ask for the 64-bit size property
            data, _ = await self._run(p.GET_OBJECT_PROP_VALUE, handle, p.PROP_SIZE)
            info.size = struct.unpack_from("<Q", data)[0]
        return info

    async def read_partial(self, node: Node, offset: int, length: int) -> bytes:
        if self.supports(p.GET_PARTIAL_OBJECT_64):
            data, _ = await self._run(p.GET_PARTIAL_OBJECT_64, node.handle, offset & 0xFFFFFFFF, offset >> 32, length)
            return data
        if self.supports(p.GET_PARTIAL_OBJECT) and offset + length <= 0xFFFFFFFF:
            data, _ = await self._run(p.GET_PARTIAL_OBJECT, node.handle, offset, length)
            return data
        raise p.MtpError(0x2005, p.GET_PARTIAL_OBJECT, "this phone can't read part of a file over MTP")

    async def iter_read(self, node: Node, start: int = 0, length: int | None = None,
                        checkpoint: Callable[[], object] | None = None) -> AsyncIterator[bytes]:
        """Read a file in chunks; the session is free for other requests between chunks."""
        end = node.size if length is None else min(node.size, start + length)
        if not (self.supports(p.GET_PARTIAL_OBJECT_64) or self.supports(p.GET_PARTIAL_OBJECT)):
            data, _ = await self._run(p.GET_OBJECT, node.handle)  # whole file in one go
            yield data[start:end]
            return
        offset = start
        while offset < end:
            if checkpoint is not None:
                await checkpoint()
            chunk = await self.read_partial(node, offset, min(PARTIAL_CHUNK, end - offset))
            if not chunk:
                raise LinkError("the phone returned less data than the file size")
            offset += len(chunk)
            yield chunk

    async def upload(self, parent: Node, name: str, size: int, mtime: float, read: Callable[[int], bytes],
                     progress: Callable[[int], None] | None = None) -> Node:
        if not parent.is_dir or parent.storage_id == 0:
            raise PermissionError(f"can't create files in {parent.path}")
        info = p.pack_object_info(parent.storage_id, parent.handle, name, size, folder=False, mtime=mtime)
        loop = asyncio.get_running_loop()

        def counted(n: int) -> bytes:
            chunk = read(n)
            if progress is not None and chunk:
                loop.call_soon_threadsafe(progress, len(chunk))
            return chunk

        abort = threading.Event()
        async with self._lock:
            _, params = await self._call(self._transact, p.SEND_OBJECT_INFO, (parent.storage_id, parent.handle),
                                         data_out=info)
            handle = params[2]
            try:
                await self._call(self._transact, p.SEND_OBJECT, (), producer=(size, counted), abort=abort,
                                 timeout=SLOW_TIMEOUT)
            except BaseException:
                # Don't leave a half-written file behind.
                if not self.broken:
                    try:
                        await self._call(self._transact, p.DELETE_OBJECT, (handle,))
                    except Exception:
                        pass
                raise
            finally:
                self.invalidate(parent.path)
        return Node(posixpath.join(parent.path, name), parent.storage_id, handle, False, size, int(mtime))

    async def mkdir(self, parent: Node, name: str) -> Node:
        if not parent.is_dir or parent.storage_id == 0:
            raise PermissionError(f"can't create folders in {parent.path}")
        info = p.pack_object_info(parent.storage_id, parent.handle, name, 0, folder=True, mtime=time.time())
        _, params = await self._run(p.SEND_OBJECT_INFO, parent.storage_id, parent.handle, data_out=info)
        self.invalidate(parent.path)
        return Node(posixpath.join(parent.path, name), parent.storage_id, params[2], True, 0, int(time.time()))

    async def rename(self, node: Node, new_name: str) -> None:
        await self._run(p.SET_OBJECT_PROP_VALUE, node.handle, p.PROP_FILE_NAME, data_out=p.pack_string(new_name))
        self.invalidate(posixpath.dirname(node.path))
        self.invalidate(node.path)

    async def delete(self, node: Node) -> None:
        await self._run(p.DELETE_OBJECT, node.handle, timeout=SLOW_TIMEOUT)
        self.invalidate(posixpath.dirname(node.path))
        self.invalidate(node.path)

    async def battery_level(self) -> int | None:
        if not self.info or p.DEVICE_PROP_BATTERY not in self.info.device_props:
            return None
        data, _ = await self._run(p.GET_DEVICE_PROP_VALUE, p.DEVICE_PROP_BATTERY)
        return data[0] if data else None

    async def storage_for(self, path: str) -> p.StorageInfo:
        await self.refresh_storages()
        self._require_unlocked()
        found = self._mount_of(self._canonical(path))
        return found[1] if found else self._storages[0]
