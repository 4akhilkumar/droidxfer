"""Transfer queue: plans and executes push (Mac -> Android) and pull (Android -> Mac) jobs."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import posixpath
import secrets
import shutil
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

from ..adb import AdbCommandError, AdbConnectionError, AdbError, SyncConnection
from .config import CONFLICT_POLICIES, ConfigStore
from .devices import DeviceError, DeviceManager
from .events import EventBus
from .localfs import LocalFS
from .paths import PathError, normalize_remote, remote_join, unique_name
from .remotefs import RemoteFS, RemoteOpError

log = logging.getLogger(__name__)

MAX_HISTORY = 200
RECONNECT_TIMEOUT = 30.0
MAX_ATTEMPTS = 3
PROGRESS_INTERVAL = 0.3
PART_SUFFIX = ".droidxfer-part"


class Direction(str, Enum):
    PUSH = "push"  # Mac -> Android
    PULL = "pull"  # Android -> Mac


class JobStatus(str, Enum):
    QUEUED = "queued"
    PLANNING = "planning"
    RUNNING = "running"
    PAUSED = "paused"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


FINISHED = frozenset({JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.CANCELLED})
_TRANSPORT_ERRORS = (AdbConnectionError, DeviceError, asyncio.TimeoutError, ConnectionError)
_FILE_ERRORS = (AdbError, RemoteOpError, OSError, PathError)


@dataclass
class TransferItem:
    src: str
    rel: str
    dst: str
    size: int
    mtime: int
    mode: int = 0o644
    status: str = "pending"  # pending | done | skipped | failed
    error: str | None = None
    transferred: int = 0


@dataclass
class TransferJob:
    id: str
    device_id: str
    direction: Direction
    sources: list[str]
    dest_dir: str
    conflict: str
    verify: bool
    media_scan: bool = True
    status: JobStatus = JobStatus.QUEUED
    error: str | None = None
    items: list[TransferItem] = field(default_factory=list)
    dirs: list[str] = field(default_factory=list)
    preplanned: bool = False
    cleanup: list[str] = field(default_factory=list)
    retry_of: str | None = None
    total_bytes: int = 0
    bytes_done: int = 0
    current: str | None = None
    speed: float = 0.0
    created_at: float = field(default_factory=time.time)
    started_at: float | None = None
    finished_at: float | None = None
    resume_event: asyncio.Event = field(default_factory=asyncio.Event, repr=False)
    task: asyncio.Task | None = field(default=None, repr=False)
    _emit_at: float = 0.0
    _emit_bytes: int = 0

    def __post_init__(self) -> None:
        self.resume_event.set()

    def count(self, status: str) -> int:
        return sum(1 for i in self.items if i.status == status)

    @property
    def title(self) -> str:
        first = posixpath.basename(self.sources[0].rstrip("/")) if self.sources else "transfer"
        return first if len(self.sources) == 1 else f"{first} and {len(self.sources) - 1} more"

    def to_dict(self) -> dict:
        remaining = max(self.total_bytes - self.bytes_done, 0)
        failures = [{"rel": i.rel, "error": i.error} for i in self.items if i.status == "failed"]
        return {
            "id": self.id,
            "device_id": self.device_id,
            "direction": self.direction.value,
            "title": self.title,
            "sources": self.sources[:5],
            "source_count": len(self.sources),
            "dest_dir": self.dest_dir,
            "conflict": self.conflict,
            "verify": self.verify,
            "status": self.status.value,
            "error": self.error,
            "total_files": len(self.items),
            "done_files": self.count("done"),
            "skipped_files": self.count("skipped"),
            "failed_files": len(failures),
            "failures": failures[:20],
            "total_bytes": self.total_bytes,
            "bytes_done": self.bytes_done,
            "current": self.current,
            "speed": round(self.speed),
            "eta": round(remaining / self.speed) if self.speed > 1 and self.status == JobStatus.RUNNING else None,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "can_retry": self.status in (JobStatus.FAILED, JobStatus.CANCELLED),
        }


def md5_file(path: str | Path) -> str:
    digest = hashlib.md5(usedforsecurity=False)
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


class TransferEngine:
    def __init__(
        self,
        devices: DeviceManager,
        remote: RemoteFS,
        local: LocalFS,
        bus: EventBus,
        config: ConfigStore,
    ) -> None:
        self.devices = devices
        self.remote = remote
        self.local = local
        self.bus = bus
        self.config = config
        self.jobs: dict[str, TransferJob] = {}
        self._queues: dict[str, asyncio.Queue[TransferJob]] = {}
        self._workers: dict[str, asyncio.Task] = {}

    # public API ----------------------------------------------------------------
    def submit(
        self,
        device_id: str,
        direction: Direction | str,
        sources: list[str],
        dest_dir: str,
        *,
        conflict: str | None = None,
        verify: bool | None = None,
        cleanup: list[str] | None = None,
    ) -> TransferJob:
        settings = self.config.settings
        conflict = conflict or settings.default_conflict
        if conflict not in CONFLICT_POLICIES:
            raise ValueError(f"conflict must be one of {CONFLICT_POLICIES}")
        if not sources:
            raise ValueError("no sources selected")
        device = self.devices.get(device_id)
        job = TransferJob(
            id=secrets.token_hex(6),
            device_id=device.id,
            direction=Direction(direction),
            sources=list(sources),
            dest_dir=dest_dir,
            conflict=conflict,
            verify=settings.verify_checksum if verify is None else verify,
            media_scan=settings.media_scan,
            cleanup=list(cleanup or []),
        )
        self._enqueue(job)
        return job

    def pause(self, job_id: str) -> TransferJob:
        job = self._get(job_id)
        if job.status not in FINISHED:
            job.resume_event.clear()
            job.status = JobStatus.PAUSED
            self._emit(job)
        return job

    def resume(self, job_id: str) -> TransferJob:
        job = self._get(job_id)
        if job.status == JobStatus.PAUSED:
            job.status = JobStatus.RUNNING if job.started_at else JobStatus.QUEUED
            job.resume_event.set()
            self._emit(job)
        return job

    def cancel(self, job_id: str) -> TransferJob:
        job = self._get(job_id)
        if job.status in FINISHED:
            return job
        if job.task and not job.task.done():
            job.task.cancel()
            job.resume_event.set()
        else:
            self._finish(job, JobStatus.CANCELLED)
        return job

    def retry(self, job_id: str) -> TransferJob:
        old = self._get(job_id)
        if old.status not in (JobStatus.FAILED, JobStatus.CANCELLED):
            raise ValueError("only failed or cancelled transfers can be retried")
        job = TransferJob(
            id=secrets.token_hex(6),
            device_id=old.device_id,
            direction=old.direction,
            sources=old.sources,
            dest_dir=old.dest_dir,
            conflict=old.conflict,
            verify=old.verify,
            media_scan=old.media_scan,
            cleanup=old.cleanup,
            retry_of=old.id,
        )
        old.cleanup = []
        pending = [i for i in old.items if i.status in ("failed", "pending")]
        if old.items and pending:
            job.preplanned = True
            job.dirs = list(old.dirs)
            job.items = [
                TransferItem(i.src, i.rel, i.dst, i.size, i.mtime, i.mode) for i in pending
            ]
        self._enqueue(job)
        return job

    def clear_finished(self) -> list[str]:
        removed = [jid for jid, job in self.jobs.items() if job.status in FINISHED]
        for jid in removed:
            self._cleanup(self.jobs.pop(jid), force=True)
        self.bus.publish("jobs_removed", removed)
        return removed

    def list(self) -> list[dict]:
        return [job.to_dict() for job in self.jobs.values()]

    async def wait(self, job_id: str) -> TransferJob:
        job = self._get(job_id)
        while job.status not in FINISHED:
            if job.task:
                await asyncio.wait([job.task])
            else:
                await asyncio.sleep(0.1)
        return job

    async def shutdown(self) -> None:
        for job in self.jobs.values():
            if job.task and not job.task.done():
                job.task.cancel()
        workers = list(self._workers.values())
        for w in workers:
            w.cancel()
        await asyncio.gather(*workers, *(j.task for j in self.jobs.values() if j.task), return_exceptions=True)

    # internals -----------------------------------------------------------------
    def _get(self, job_id: str) -> TransferJob:
        try:
            return self.jobs[job_id]
        except KeyError:
            raise KeyError(f"unknown transfer {job_id}") from None

    def _enqueue(self, job: TransferJob) -> None:
        self.jobs[job.id] = job
        while len(self.jobs) > MAX_HISTORY:
            oldest = next((j for j in self.jobs.values() if j.status in FINISHED), None)
            if oldest is None:
                break
            self._cleanup(self.jobs.pop(oldest.id), force=True)
        queue = self._queues.get(job.device_id)
        if queue is None:
            queue = self._queues[job.device_id] = asyncio.Queue()
            self._workers[job.device_id] = asyncio.create_task(
                self._worker(queue), name=f"transfers-{job.device_id}"
            )
        queue.put_nowait(job)
        self._emit(job)

    async def _worker(self, queue: asyncio.Queue[TransferJob]) -> None:
        while True:
            job = await queue.get()
            if job.status in FINISHED:
                continue
            job.task = asyncio.create_task(self._run(job), name=f"job-{job.id}")
            await asyncio.wait([job.task])

    def _emit(self, job: TransferJob) -> None:
        self.bus.publish("job", job.to_dict())

    def _finish(self, job: TransferJob, status: JobStatus, error: str | None = None) -> None:
        job.status = status
        job.error = error
        job.finished_at = time.time()
        job.current = None
        job.speed = 0.0
        self._cleanup(job)
        self._emit(job)

    def _cleanup(self, job: TransferJob, force: bool = False) -> None:
        # Staged uploads are kept for failed jobs so they can be retried.
        if not job.cleanup or (job.status == JobStatus.FAILED and not force):
            return
        for path in job.cleanup:
            shutil.rmtree(path, ignore_errors=True)
        job.cleanup = []

    async def _checkpoint(self, job: TransferJob) -> None:
        if not job.resume_event.is_set():
            await job.resume_event.wait()
            job.status = JobStatus.RUNNING
            job._emit_at = time.monotonic()
            job._emit_bytes = job.bytes_done
            self._emit(job)

    def _progress(self, job: TransferJob, item: TransferItem, n: int) -> None:
        job.bytes_done += n
        item.transferred += n
        now = time.monotonic()
        elapsed = now - job._emit_at
        if elapsed >= PROGRESS_INTERVAL:
            rate = (job.bytes_done - job._emit_bytes) / elapsed
            job.speed = rate if job.speed == 0 else 0.6 * job.speed + 0.4 * rate
            job._emit_at, job._emit_bytes = now, job.bytes_done
            self._emit(job)

    async def _run(self, job: TransferJob) -> None:
        try:
            await self._checkpoint(job)
            if not job.preplanned:
                job.status = JobStatus.PLANNING
                self._emit(job)
                if job.direction == Direction.PUSH:
                    await self._plan_push(job)
                else:
                    await self._plan_pull(job)
            job.total_bytes = sum(i.size for i in job.items if i.status == "pending")
            job.status = JobStatus.RUNNING
            job.started_at = time.time()
            job._emit_at, job._emit_bytes = time.monotonic(), 0
            self._emit(job)
            await self._create_dirs(job)
            await self._execute(job)
            if job.direction == Direction.PUSH and job.media_scan:
                done = [i.dst for i in job.items if i.status == "done"]
                try:
                    await self.remote.media_scan(job.device_id, done)
                except Exception as exc:
                    log.info("media scan failed: %s", exc)
            failed = job.count("failed")
            if failed:
                self._finish(job, JobStatus.FAILED, f"{failed} of {len(job.items)} files failed")
            else:
                self._finish(job, JobStatus.COMPLETED)
        except asyncio.CancelledError:
            self._finish(job, JobStatus.CANCELLED)
        except Exception as exc:
            log.exception("transfer %s failed", job.id)
            self._finish(job, JobStatus.FAILED, str(exc) or exc.__class__.__name__)

    # planning ------------------------------------------------------------------
    async def _plan_push(self, job: TransferJob) -> None:
        dest = normalize_remote(job.dest_dir)
        job.dest_dir = dest
        dest_names = await self._remote_names(job.device_id, dest)
        job.dirs.append(dest)
        for raw in job.sources:
            src = self.local.resolve(raw)
            if src.is_dir():
                top_name = self._top_name(src.name, dest_names, job.conflict, is_dir=True)
                top = remote_join(dest, top_name)
                job.dirs.append(top)
                dirs, files = await asyncio.to_thread(self.local.walk, src)
                job.dirs.extend(posixpath.join(top, d) for d in dirs)
                for f in files:
                    job.items.append(
                        TransferItem(str(f.path), f"{top_name}/{f.rel}", posixpath.join(top, f.rel),
                                     f.size, f.mtime, f.mode & 0o777)
                    )
            else:
                f = self.local.file_entry(src)
                if f is None:
                    job.items.append(TransferItem(str(src), src.name, "", 0, 0, status="failed",
                                                  error="not a regular file"))
                    continue
                job.items.append(TransferItem(str(f.path), src.name, remote_join(dest, src.name),
                                              f.size, f.mtime, f.mode & 0o777))

    async def _plan_pull(self, job: TransferJob) -> None:
        dest = self.local.resolve(job.dest_dir)
        await asyncio.to_thread(dest.mkdir, parents=True, exist_ok=True)
        job.dest_dir = str(dest)
        dest_names = set(await asyncio.to_thread(os.listdir, dest))
        async with await self.remote.sync(job.device_id) as sc:
            for raw in job.sources:
                src = normalize_remote(raw)
                name = posixpath.basename(src) or "root"
                st = await sc.stat(src)
                if st is None:
                    job.items.append(TransferItem(src, name, "", 0, 0, status="failed", error="not found"))
                    continue
                if st.is_dir:
                    top_name = self._top_name(name, dest_names, job.conflict, is_dir=True)
                    top = dest / top_name
                    job.dirs.append(str(top))
                    dirs, files = await self.remote.walk(job.device_id, src, sc)
                    job.dirs.extend(str(top / d) for d in dirs)
                    for f in files:
                        job.items.append(TransferItem(f.path, f"{top_name}/{f.rel}", str(top / f.rel),
                                                      f.size, f.mtime))
                elif st.is_file:
                    job.items.append(TransferItem(src, name, str(dest / name), st.size, st.mtime))
                else:
                    job.items.append(TransferItem(src, name, "", 0, 0, status="failed",
                                                  error="not a regular file"))

    @staticmethod
    def _top_name(name: str, existing: set[str], conflict: str, *, is_dir: bool) -> str:
        """Top-level folders get a fresh name under 'rename'; otherwise they are merged."""
        if conflict == "rename" and name in existing:
            name = unique_name(name, existing.__contains__, is_dir=is_dir)
        existing.add(name)
        return name

    async def _remote_names(self, device_id: str, directory: str) -> set[str]:
        async with await self.remote.sync(device_id) as sc:
            return {e.name for e in await sc.list(directory)}

    async def _create_dirs(self, job: TransferJob) -> None:
        if not job.dirs:
            return
        if job.direction == Direction.PUSH:
            await self.remote.mkdirs(job.device_id, job.dirs)
        else:
            for d in job.dirs:
                await asyncio.to_thread(os.makedirs, self.local.resolve(d), exist_ok=True)

    # execution -----------------------------------------------------------------
    async def _execute(self, job: TransferJob) -> None:
        sc: SyncConnection | None = None
        name_cache: dict[str, set[str]] = {}
        try:
            for item in job.items:
                if item.status != "pending":
                    continue
                await self._checkpoint(job)
                job.current = item.rel
                attempt = 0
                while True:
                    attempt += 1
                    try:
                        if sc is None or sc.broken:
                            if sc is not None:
                                await sc.close()
                            sc = None
                            sc = await self.remote.sync(job.device_id)
                        await self._transfer_one(job, item, sc, name_cache)
                        break
                    except _TRANSPORT_ERRORS as exc:
                        job.bytes_done -= item.transferred
                        item.transferred = 0
                        if sc is not None:
                            sc.broken = True
                        if attempt >= MAX_ATTEMPTS:
                            self._fail_item(job, item, exc)
                            break
                        log.info("transport lost during %s (%s); waiting for device", item.rel, exc)
                        try:
                            await self.devices.wait_for(job.device_id, RECONNECT_TIMEOUT)
                        except DeviceError as wait_exc:
                            for rest in job.items:
                                if rest.status == "pending":
                                    self._fail_item(job, rest, wait_exc)
                            return
                    except _FILE_ERRORS as exc:
                        self._fail_item(job, item, exc)
                        break
                self._progress(job, item, 0)
        finally:
            if sc is not None:
                await sc.close()

    def _fail_item(self, job: TransferJob, item: TransferItem, exc: BaseException) -> None:
        item.status = "failed"
        item.error = str(exc) or exc.__class__.__name__
        job.total_bytes -= item.size - item.transferred

    def _skip_item(self, job: TransferJob, item: TransferItem) -> None:
        item.status = "skipped"
        job.total_bytes -= item.size

    async def _transfer_one(
        self, job: TransferJob, item: TransferItem, sc: SyncConnection, name_cache: dict[str, set[str]]
    ) -> None:
        if job.direction == Direction.PUSH:
            await self._push_one(job, item, sc, name_cache)
        else:
            await self._pull_one(job, item, sc)
        if item.status == "pending":
            item.status = "done"

    async def _push_one(
        self, job: TransferJob, item: TransferItem, sc: SyncConnection, name_cache: dict[str, set[str]]
    ) -> None:
        parent, name = posixpath.split(item.dst)
        if parent not in name_cache:
            name_cache[parent] = {e.name for e in await sc.list(parent)}
        names = name_cache[parent]
        if name in names:
            if job.conflict == "skip":
                self._skip_item(job, item)
                return
            if job.conflict == "rename":
                name = unique_name(name, names.__contains__)
                item.dst = posixpath.join(parent, name)
        names.add(name)
        try:
            with open(item.src, "rb") as fh:
                await sc.push(
                    fh,
                    item.dst,
                    mtime=item.mtime,
                    mode=item.mode,
                    progress=lambda n: self._progress(job, item, n),
                    checkpoint=lambda: self._checkpoint(job),
                )
            if job.verify:
                local, remote = await asyncio.gather(
                    asyncio.to_thread(md5_file, item.src), self.remote.md5(job.device_id, item.dst)
                )
                if local != remote:
                    raise RemoteOpError(f"checksum mismatch (mac {local}, android {remote})")
        except BaseException as exc:
            if not isinstance(exc, _TRANSPORT_ERRORS):
                await asyncio.shield(self.remote.remove_quietly(job.device_id, item.dst))
            names.discard(name)
            raise

    async def _pull_one(self, job: TransferJob, item: TransferItem, sc: SyncConnection) -> None:
        dst = self.local.resolve(item.dst, follow=False)
        if os.path.lexists(dst):
            if job.conflict == "skip":
                self._skip_item(job, item)
                return
            if job.conflict == "rename":
                dst = dst.parent / unique_name(dst.name, lambda n: os.path.lexists(dst.parent / n))
                item.dst = str(dst)
        part = dst.parent / f".{secrets.token_hex(6)}{PART_SUFFIX}"
        try:
            with open(part, "wb") as fh:
                await sc.pull(
                    item.src,
                    fh,
                    progress=lambda n: self._progress(job, item, n),
                    checkpoint=lambda: self._checkpoint(job),
                )
            os.utime(part, (item.mtime, item.mtime))
            if job.verify:
                local, remote = await asyncio.gather(
                    asyncio.to_thread(md5_file, part), self.remote.md5(job.device_id, item.src)
                )
                if local != remote:
                    raise RemoteOpError(f"checksum mismatch (android {remote}, mac {local})")
            os.replace(part, dst)
        finally:
            part.unlink(missing_ok=True)


__all__ = ["Direction", "JobStatus", "TransferEngine", "TransferJob", "AdbCommandError"]
