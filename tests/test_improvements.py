"""Tests for the bug fixes and features added after v0.1.0."""

import asyncio
import io
import json
import os
import time

import pytest
from PIL import Image

from droidxfer.api.routes import parse_range
from droidxfer.core import transfer as transfer_mod
from droidxfer.core.config import ConfigStore
from droidxfer.core.localfs import LocalFS
from droidxfer.core.paths import PathError
from droidxfer.core.thumbs import IMAGE_EXTS, ThumbnailService, render_thumbnail
from droidxfer.core.transfer import Direction, JobStatus

from test_api import authed, client, wait_job  # noqa: F401  (fixtures)


# transfer engine ---------------------------------------------------------------------
async def test_pause_during_planning_stays_paused(ctx, mac_home, device_root):
    (mac_home / "f.bin").write_bytes(os.urandom(50_000))
    started, release = asyncio.Event(), asyncio.Event()
    original = ctx.engine._plan_push

    async def slow_plan(job):
        started.set()
        await release.wait()
        await original(job)

    ctx.engine._plan_push = slow_plan
    job = ctx.engine.submit("FAKE123", Direction.PUSH, [str(mac_home / "f.bin")], "/sdcard/Download")
    await asyncio.wait_for(started.wait(), 5)
    assert job.status == JobStatus.PLANNING
    ctx.engine.pause(job.id)
    release.set()
    await asyncio.sleep(0.3)
    assert job.status == JobStatus.PAUSED
    assert job.bytes_done == 0
    ctx.engine.resume(job.id)
    await asyncio.wait_for(ctx.engine.wait(job.id), 10)
    assert job.status == JobStatus.COMPLETED
    assert (device_root / "sdcard" / "Download" / "f.bin").stat().st_size == 50_000


async def test_resume_during_planning_shows_planning(ctx, mac_home):
    (mac_home / "f.bin").write_bytes(b"x")
    started, release = asyncio.Event(), asyncio.Event()
    original = ctx.engine._plan_push

    async def slow_plan(job):
        started.set()
        await release.wait()
        await original(job)

    ctx.engine._plan_push = slow_plan
    job = ctx.engine.submit("FAKE123", Direction.PUSH, [str(mac_home / "f.bin")], "/sdcard/Download")
    await asyncio.wait_for(started.wait(), 5)
    ctx.engine.pause(job.id)
    ctx.engine.resume(job.id)
    assert job.status == JobStatus.PLANNING
    release.set()
    await asyncio.wait_for(ctx.engine.wait(job.id), 10)
    assert job.status == JobStatus.COMPLETED


async def test_retry_only_once(ctx, mac_home):
    job = ctx.engine.submit("FAKE123", "pull", ["/sdcard/nope.txt"], str(mac_home))
    await asyncio.wait_for(ctx.engine.wait(job.id), 10)
    assert job.to_dict()["can_retry"]
    retry = ctx.engine.retry(job.id)
    assert job.retried_by == retry.id and not job.to_dict()["can_retry"]
    with pytest.raises(ValueError):
        ctx.engine.retry(job.id)
    await asyncio.wait_for(ctx.engine.wait(retry.id), 10)


async def test_history_eviction_is_published(ctx, mac_home, monkeypatch):
    monkeypatch.setattr(transfer_mod, "MAX_HISTORY", 2)
    queue = ctx.bus.subscribe()
    ids = []
    for _ in range(3):
        job = ctx.engine.submit("FAKE123", "pull", ["/sdcard/nope.txt"], str(mac_home))
        await asyncio.wait_for(ctx.engine.wait(job.id), 10)
        ids.append(job.id)
    removed = []
    while not queue.empty():
        event = queue.get_nowait()
        if event["type"] == "jobs_removed":
            removed += event["data"]
    assert removed == [ids[0]]
    assert ids[0] not in ctx.engine.jobs


async def test_push_skip_identical(ctx, mac_home, device_root):
    dest = device_root / "sdcard" / "Download"
    for name, text in (("same.txt", "same"), ("changed.txt", "new"), ("fresh.txt", "fresh")):
        (mac_home / name).write_text(text)
        os.utime(mac_home / name, (1_700_000_000, 1_700_000_000))
    (dest / "same.txt").write_text("same")
    os.utime(dest / "same.txt", (1_700_000_000, 1_700_000_000))
    (dest / "changed.txt").write_text("old")
    sources = [str(mac_home / n) for n in ("same.txt", "changed.txt", "fresh.txt")]
    job = ctx.engine.submit("FAKE123", "push", sources, "/sdcard/Download", conflict="skip_identical")
    await asyncio.wait_for(ctx.engine.wait(job.id), 10)
    assert job.status == JobStatus.COMPLETED, job.to_dict()
    assert job.count("skipped") == 1 and job.count("done") == 2
    assert (dest / "changed.txt").read_text() == "new"
    assert (dest / "fresh.txt").read_text() == "fresh"
    assert not (dest / "same (1).txt").exists()


async def test_pull_skip_identical_is_incremental(ctx, mac_home, device_root):
    camera = device_root / "sdcard" / "DCIM" / "Camera"
    camera.mkdir(parents=True)
    (camera / "a.jpg").write_bytes(b"a" * 100)
    first = ctx.engine.submit("FAKE123", "pull", ["/sdcard/DCIM/Camera"], str(mac_home), conflict="skip_identical")
    await asyncio.wait_for(ctx.engine.wait(first.id), 10)
    assert first.count("done") == 1
    (camera / "b.jpg").write_bytes(b"b" * 100)
    second = ctx.engine.submit("FAKE123", "pull", ["/sdcard/DCIM/Camera"], str(mac_home), conflict="skip_identical")
    await asyncio.wait_for(ctx.engine.wait(second.id), 10)
    assert second.status == JobStatus.COMPLETED
    assert second.count("skipped") == 1 and second.count("done") == 1
    assert (mac_home / "Camera" / "b.jpg").read_bytes() == b"b" * 100


async def test_push_fails_early_without_space(ctx, mac_home, device_root, monkeypatch):
    (mac_home / "big.bin").write_bytes(b"x" * 10_000)

    async def tiny(device_id, path):
        return {"total": 100_000, "free": 1_000}

    monkeypatch.setattr(ctx.remote, "storage", tiny)
    job = ctx.engine.submit("FAKE123", "push", [str(mac_home / "big.bin")], "/sdcard/Download", conflict="rename")
    await asyncio.wait_for(ctx.engine.wait(job.id), 10)
    assert job.status == JobStatus.FAILED and "not enough space" in job.error
    assert not (device_root / "sdcard" / "Download" / "big.bin").exists()


async def test_remote_storage_and_range(ctx, device_root):
    info = await ctx.remote.storage("FAKE123", "/sdcard")
    assert info["total"] > 0 and 0 <= info["free"] <= info["total"]
    data = os.urandom(300_000)
    (device_root / "sdcard" / "v.mp4").write_bytes(data)
    head = b"".join([c async for c in ctx.remote.iter_range("FAKE123", "/sdcard/v.mp4", 0, 2)])
    assert head == data[:2]
    middle = b"".join([c async for c in ctx.remote.iter_range("FAKE123", "/sdcard/v.mp4", 150_000, 70_000)])
    assert middle == data[150_000:220_000]


# config --------------------------------------------------------------------------------
def test_config_survives_bad_values(tmp_path):
    path = tmp_path / "config.json"
    path.write_text(json.dumps({
        "show_hidden": "yes",
        "verify_checksum": True,
        "media_scan": 1,
        "default_conflict": "explode",
        "adb_path": 42,
        "download_dir": "~/Downloads",  # removed setting from v0.1.0
        "known_endpoints": [
            {"host": "192.168.1.5", "port": 5555, "unexpected": True},
            {"host": "x", "port": "not-a-port"},
            "garbage",
        ],
    }))
    settings = ConfigStore(path).settings
    assert settings.show_hidden is False and settings.verify_checksum is True and settings.media_scan is True
    assert settings.default_conflict == "rename" and settings.adb_path is None
    assert [(e.host, e.port) for e in settings.known_endpoints] == [("192.168.1.5", 5555)]


def test_config_not_an_object(tmp_path):
    path = tmp_path / "config.json"
    path.write_text("[1, 2]")
    assert ConfigStore(path).settings.default_conflict == "rename"


# Mac file system -----------------------------------------------------------------------
def _volumes(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    volumes = tmp_path / "Volumes"
    (volumes / "USB").mkdir(parents=True)
    (volumes / "USB" / "photo.jpg").write_bytes(b"jpg")
    (volumes / "Macintosh HD").symlink_to("/")
    return home, volumes


def test_external_drives(tmp_path):
    home, volumes = _volumes(tmp_path)
    enabled = {"on": True}
    fs = LocalFS(home, extra_roots=[volumes], extras_enabled=lambda: enabled["on"])
    usb = volumes / "USB"
    assert fs.volumes() == [{"name": "USB", "path": str(usb.resolve())}]
    listing = fs.list(str(usb))
    assert [e["name"] for e in listing["entries"]] == ["photo.jpg"]
    assert listing["parent"] == str(volumes.resolve()) or listing["parent"] == str(volumes)
    with pytest.raises(PermissionError):
        fs.resolve(str(volumes / "Macintosh HD" / "etc"))
    with pytest.raises(PathError):
        fs.trash([str(usb)])
    with pytest.raises(PathError):
        fs.rename(str(usb), "Other")
    enabled["on"] = False
    assert fs.volumes() == []
    with pytest.raises(PermissionError):
        fs.list(str(usb))


def test_local_storage(tmp_path):
    fs = LocalFS(tmp_path)
    info = fs.storage(str(tmp_path))
    assert info["total"] > 0 and 0 <= info["free"] <= info["total"]


def test_system_trash_is_used(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    (home / "doc.txt").write_text("x")
    log = tmp_path / "trash.log"
    tool = tmp_path / "trash"
    tool.write_text(f'#!/bin/sh\necho "$1" >> "{log}"\nrm -rf "$1"\n')
    tool.chmod(0o755)
    monkeypatch.setattr("droidxfer.core.localfs.TRASH_TOOL", str(tool))
    fs = LocalFS(home)
    fs.trash([str(home / "doc.txt")])
    assert not (home / "doc.txt").exists()
    assert log.read_text().strip() == str((home / "doc.txt").resolve())
    assert not (home / ".Trash").exists()


def test_system_trash_failure_falls_back(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    (home / "doc.txt").write_text("x")
    tool = tmp_path / "trash"
    tool.write_text("#!/bin/sh\nexit 1\n")
    tool.chmod(0o755)
    monkeypatch.setattr("droidxfer.core.localfs.TRASH_TOOL", str(tool))
    LocalFS(home).trash([str(home / "doc.txt")])
    assert (home / ".Trash" / "doc.txt").exists()


# thumbnails ----------------------------------------------------------------------------
def test_heic_thumbnail():
    pillow_heif = pytest.importorskip("pillow_heif")
    assert ".heic" in IMAGE_EXTS
    buf = io.BytesIO()
    pillow_heif.from_pillow(Image.new("RGB", (640, 480), (200, 30, 30))).save(buf, quality=80)
    thumb = Image.open(io.BytesIO(render_thumbnail(buf.getvalue())))
    assert thumb.format == "JPEG" and max(thumb.size) == 256


def test_thumbnail_cache_prune(tmp_path):
    service = ThumbnailService(None, None, None, root=tmp_path)
    service.dir.mkdir(parents=True)
    now = time.time()
    for i in range(10):
        f = service.dir / f"{i}.jpg"
        f.write_bytes(b"x" * 1000)
        os.utime(f, (now - 100 + i, now - 100 + i))  # 0.jpg is the least recently used
    assert service.prune(max_bytes=20_000, target_bytes=5_000) == 0
    assert service.prune(max_bytes=8_000, target_bytes=5_000) == 5
    assert sorted(p.name for p in service.dir.iterdir()) == [f"{i}.jpg" for i in range(5, 10)]


# API -----------------------------------------------------------------------------------
def test_parse_range():
    assert parse_range("bytes=0-1", 100) == (0, 1)
    assert parse_range("bytes=10-", 100) == (10, 99)
    assert parse_range("bytes=-10", 100) == (90, 99)
    assert parse_range("bytes=50-500", 100) == (50, 99)
    assert parse_range("bytes=200-", 100) is None
    assert parse_range("bytes=5-1", 100) is None
    assert parse_range("bytes=0-1,5-6", 100) is None
    assert parse_range("bytes=-", 100) is None
    assert parse_range("bytes=0-0", 0) is None


def test_device_file_ranges(authed, device_root):  # noqa: F811
    data = os.urandom(200_000)
    (device_root / "sdcard" / "clip.mp4").write_bytes(data)
    url, params = "/api/devices/FAKE123/file", {"path": "/sdcard/clip.mp4"}
    full = authed.get(url, params=params)
    assert full.status_code == 200 and full.headers["accept-ranges"] == "bytes" and full.content == data
    probe = authed.get(url, params=params, headers={"range": "bytes=0-1"})
    assert probe.status_code == 206 and probe.content == data[:2]
    assert probe.headers["content-range"] == "bytes 0-1/200000"
    tail = authed.get(url, params=params, headers={"range": "bytes=100000-"})
    assert tail.status_code == 206 and tail.content == data[100_000:]
    assert int(tail.headers["content-length"]) == 100_000
    bad = authed.get(url, params=params, headers={"range": "bytes=999999-"})
    assert bad.status_code == 416 and bad.headers["content-range"] == "bytes */200000"


def test_storage_and_volume_endpoints(authed, mac_home):  # noqa: F811
    device = authed.get("/api/devices/FAKE123/storage", params={"path": "/sdcard"}).json()
    assert device["free"] > 0
    local = authed.get("/api/local/storage", params={"path": str(mac_home)}).json()
    assert local["free"] > 0
    assert isinstance(authed.get("/api/local/volumes").json(), list)
    authed.put("/api/settings", json={"external_drives": False})
    assert authed.get("/api/local/volumes").json() == []


def test_settings_republish_devices(authed):  # noqa: F811
    assert authed.put("/api/settings", json={"prefer_usb": False}).json()["prefer_usb"] is False
    assert "download_dir" not in authed.get("/api/settings").json()


def test_skip_identical_via_api(authed, mac_home, device_root):  # noqa: F811
    (mac_home / "a.txt").write_text("a")
    body = {"device_id": "FAKE123", "direction": "push", "sources": [str(mac_home / "a.txt")],
            "dest_dir": "/sdcard/Download", "conflict": "skip_identical"}
    first = wait_job(authed, authed.post("/api/transfers", json=body).json()["id"])
    second = wait_job(authed, authed.post("/api/transfers", json=body).json()["id"])
    assert first["done_files"] == 1 and second["skipped_files"] == 1
