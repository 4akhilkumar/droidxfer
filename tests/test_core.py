import asyncio
import os

import pytest

from droidxfer.core.localfs import LocalFS
from droidxfer.core.paths import PathError, check_remote_mutable, normalize_remote, unique_name
from droidxfer.core.transfer import Direction, JobStatus


# paths ------------------------------------------------------------------------------
def test_normalize_remote():
    assert normalize_remote("/sdcard//DCIM/../Download/") == "/sdcard/Download"
    assert normalize_remote("//sdcard") == "/sdcard"
    for bad in ("", "sdcard", "/sdcard/a\nb", "/x\x00"):
        with pytest.raises(PathError):
            normalize_remote(bad)


def test_protected_paths():
    for p in ("/", "/sdcard", "/sdcard/", "/storage/emulated/0", "/data", "/foo"):
        with pytest.raises(PathError):
            check_remote_mutable(p)
    assert check_remote_mutable("/sdcard/Download/x.jpg") == "/sdcard/Download/x.jpg"


def test_unique_name():
    taken = {"a.txt", "a (1).txt", "dir"}
    assert unique_name("a.txt", taken.__contains__) == "a (2).txt"
    assert unique_name("dir", taken.__contains__, is_dir=True) == "dir (1)"
    assert unique_name("new.txt", taken.__contains__) == "new.txt"


# local fs ---------------------------------------------------------------------------
def test_localfs_confinement(tmp_path):
    home = tmp_path / "home"
    (home / "docs").mkdir(parents=True)
    outside = tmp_path / "secret"
    outside.mkdir()
    (home / "escape").symlink_to(outside)
    fs = LocalFS(home)
    assert fs.resolve("~/docs") == (home / "docs").resolve()
    for bad in (str(outside), "~/../secret", "~/escape"):
        with pytest.raises(PermissionError):
            fs.resolve(bad)
    with pytest.raises(PathError):
        fs.mkdir(str(home), "../evil")


def test_localfs_list_and_trash(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    (home / "b.txt").write_text("b")
    (home / ".hidden").write_text("h")
    (home / "A").mkdir()
    fs = LocalFS(home)
    names = [e["name"] for e in fs.list(str(home))["entries"]]
    assert names == ["A", "b.txt"]
    assert ".hidden" in [e["name"] for e in fs.list(str(home), show_hidden=True)["entries"]]
    fs.trash([str(home / "b.txt")])
    assert not (home / "b.txt").exists() and (home / ".Trash" / "b.txt").exists()
    with pytest.raises(PathError):
        fs.trash([str(home)])


# devices ----------------------------------------------------------------------------
async def test_device_resolution(ctx):
    [device] = ctx.devices.snapshot()["devices"]
    assert device["id"] == "FAKE123"
    assert device["connection"] == "usb"
    assert device["model"] == "Pixel 7 Pro" and device["android_version"] == "15"
    assert ctx.devices.transport("FAKE123").serial == "FAKE123"


# transfers --------------------------------------------------------------------------
def _make_tree(home):
    src = home / "Photos"
    (src / "trip" / "empty").mkdir(parents=True)
    (src / "a.jpg").write_bytes(os.urandom(200_000))
    (src / "trip" / "b.txt").write_text("hello")
    (src / ".DS_Store").write_text("junk")
    return src


async def test_push_folder(ctx, mac_home, device_root, fake_adb):
    src = _make_tree(mac_home)
    job = ctx.engine.submit("FAKE123", Direction.PUSH, [str(src)], "/sdcard/Download", conflict="rename", verify=True)
    await asyncio.wait_for(ctx.engine.wait(job.id), 10)
    assert job.status == JobStatus.COMPLETED, job.to_dict()
    dest = device_root / "sdcard" / "Download" / "Photos"
    assert (dest / "a.jpg").read_bytes() == (src / "a.jpg").read_bytes()
    assert (dest / "trip" / "b.txt").read_text() == "hello"
    assert (dest / "trip" / "empty").is_dir()
    assert not (dest / ".DS_Store").exists()
    assert int((dest / "a.jpg").stat().st_mtime) == int((src / "a.jpg").stat().st_mtime)
    assert any("MEDIA_SCANNER_SCAN_FILE" in c for c in fake_adb.shell_log)

    again = ctx.engine.submit("FAKE123", Direction.PUSH, [str(src)], "/sdcard/Download", conflict="rename")
    await asyncio.wait_for(ctx.engine.wait(again.id), 10)
    assert (device_root / "sdcard" / "Download" / "Photos (1)" / "a.jpg").exists()


@pytest.mark.parametrize("policy", ["skip", "overwrite", "rename"])
async def test_push_file_conflicts(ctx, mac_home, device_root, policy):
    (mac_home / "note.txt").write_text("new")
    existing = device_root / "sdcard" / "Download" / "note.txt"
    existing.write_text("old")
    job = ctx.engine.submit("FAKE123", "push", [str(mac_home / "note.txt")], "/sdcard/Download", conflict=policy)
    await asyncio.wait_for(ctx.engine.wait(job.id), 10)
    assert job.status == JobStatus.COMPLETED
    if policy == "skip":
        assert existing.read_text() == "old" and job.count("skipped") == 1
    elif policy == "overwrite":
        assert existing.read_text() == "new"
    else:
        assert existing.read_text() == "old"
        assert (existing.parent / "note (1).txt").read_text() == "new"


async def test_pull_folder_and_conflict(ctx, mac_home, device_root):
    camera = device_root / "sdcard" / "DCIM" / "Camera"
    camera.mkdir(parents=True)
    (camera / "IMG_1.jpg").write_bytes(b"x" * 1000)
    (camera / "sub").mkdir()
    (camera / "sub" / "IMG_2.jpg").write_bytes(b"y" * 10)
    dest = mac_home / "Downloads"
    job = ctx.engine.submit("FAKE123", "pull", ["/sdcard/DCIM/Camera"], str(dest), verify=True)
    await asyncio.wait_for(ctx.engine.wait(job.id), 10)
    assert job.status == JobStatus.COMPLETED, job.to_dict()
    assert (dest / "Camera" / "IMG_1.jpg").read_bytes() == b"x" * 1000
    assert (dest / "Camera" / "sub" / "IMG_2.jpg").read_bytes() == b"y" * 10
    assert not list(dest.rglob("*.droidxfer-part"))

    single = ctx.engine.submit("FAKE123", "pull", ["/sdcard/DCIM/Camera/IMG_1.jpg"], str(dest / "Camera"), conflict="rename")
    await asyncio.wait_for(ctx.engine.wait(single.id), 10)
    assert (dest / "Camera" / "IMG_1 (1).jpg").exists()


async def test_pull_missing_source_fails_and_retry(ctx, mac_home, device_root):
    job = ctx.engine.submit("FAKE123", "pull", ["/sdcard/nope.txt"], str(mac_home))
    await asyncio.wait_for(ctx.engine.wait(job.id), 10)
    assert job.status == JobStatus.FAILED and job.count("failed") == 1


async def test_pull_connection_drop_marks_failed(ctx, mac_home, device_root, fake_adb):
    (device_root / "sdcard" / "big.bin").write_bytes(os.urandom(400_000))
    fake_adb.fail_recv_after = 128 * 1024
    job = ctx.engine.submit("FAKE123", "pull", ["/sdcard/big.bin"], str(mac_home))
    await asyncio.wait_for(ctx.engine.wait(job.id), 20)
    assert job.status == JobStatus.FAILED
    assert not (mac_home / "big.bin").exists()
    assert not list(mac_home.glob("*.droidxfer-part"))

    fake_adb.fail_recv_after = None
    retry = ctx.engine.retry(job.id)
    await asyncio.wait_for(ctx.engine.wait(retry.id), 10)
    assert retry.status == JobStatus.COMPLETED
    assert (mac_home / "big.bin").stat().st_size == 400_000


async def test_cancel_running_job(ctx, mac_home, device_root):
    (mac_home / "f.bin").write_bytes(os.urandom(100_000))
    job = ctx.engine.submit("FAKE123", "push", [str(mac_home / "f.bin")], "/sdcard/Download")
    ctx.engine.pause(job.id)
    await asyncio.sleep(0.2)
    ctx.engine.cancel(job.id)
    await asyncio.wait_for(ctx.engine.wait(job.id), 10)
    assert job.status == JobStatus.CANCELLED


async def test_push_outside_home_rejected(ctx, tmp_path):
    outside = tmp_path / "outside.txt"
    outside.write_text("x")
    job = ctx.engine.submit("FAKE123", "push", [str(outside)], "/sdcard/Download")
    await asyncio.wait_for(ctx.engine.wait(job.id), 10)
    assert job.status == JobStatus.FAILED and "outside" in job.error


async def test_remote_ops(ctx, device_root):
    created = await ctx.remote.mkdir("FAKE123", "/sdcard/Download", "New Folder")
    assert (device_root / "sdcard" / "Download" / "New Folder").is_dir()
    renamed = await ctx.remote.rename("FAKE123", created, "Renamed")
    assert renamed == "/sdcard/Download/Renamed"
    listing = await ctx.remote.list("FAKE123", "/sdcard/Download")
    assert [e["name"] for e in listing["entries"]] == ["Renamed"]
    await ctx.remote.delete("FAKE123", [renamed])
    assert not (device_root / "sdcard" / "Download" / "Renamed").exists()
    with pytest.raises(PathError):
        await ctx.remote.delete("FAKE123", ["/sdcard"])
