"""MTP (File transfer mode) support: protocol, session, device detection and app integration."""

import asyncio
import io
import os

import pytest

from droidxfer.core import mtpdevices
from droidxfer.core.context import AppContext
from droidxfer.core.devices import DeviceUnavailable
from droidxfer.core.transfer import JobStatus
from droidxfer.mtp import MtpFS, MtpSession
from droidxfer.mtp import protocol as p
from droidxfer.mtp import session as session_mod
from fake_mtp import FakeMtpDevice

MTP_ID = "MTPONLY1"


@pytest.fixture
def phone_root(tmp_path):
    root = tmp_path / "mtp-phone"
    (root / "DCIM" / "Camera").mkdir(parents=True)
    (root / "Download").mkdir()
    (root / "DCIM" / "Camera" / "IMG_1.jpg").write_bytes(b"jpeg" * 1000)
    os.utime(root / "DCIM" / "Camera" / "IMG_1.jpg", (1_700_000_000, 1_700_000_000))
    (root / ".hidden").write_text("x")
    return root


@pytest.fixture
def fake_phone(phone_root):
    return FakeMtpDevice(phone_root)


async def _session(device: FakeMtpDevice) -> MtpSession:
    s = MtpSession(device.link(), name="fake")
    await s.open()
    return s


# protocol ----------------------------------------------------------------------------
def test_strings_dates_and_object_info():
    for text in ("", "a", "Ünïcødé 📷.jpg"):
        assert p.read_string(p.pack_string(text), 0)[0] == text
    assert p.parse_date(p.format_date(1_700_000_000)) == 1_700_000_000
    assert p.parse_date("garbage") == 0
    packed = p.pack_object_info(0x10001, 5, "x.bin", 123, folder=False, mtime=1_700_000_000)
    info = p.parse_object_info(9, packed)
    assert (info.name, info.size, info.parent, info.mtime, info.is_dir) == ("x.bin", 123, 5, 1_700_000_000, False)


# session -----------------------------------------------------------------------------
async def test_listing_paths_and_aliases(fake_phone):
    s = await _session(fake_phone)
    fs = MtpFS(s)
    assert [e["name"] for e in (await fs.listing("/", show_hidden=False))["entries"]] == ["sdcard"]
    root = await fs.listing("/sdcard", show_hidden=False)
    assert [e["name"] for e in root["entries"]] == ["DCIM", "Download"]
    assert ".hidden" in [e["name"] for e in (await fs.listing("/sdcard", show_hidden=True))["entries"]]
    camera = await fs.listing("/storage/emulated/0/DCIM/Camera", show_hidden=False)
    assert camera["path"] == "/sdcard/DCIM/Camera" and camera["parent"] == "/sdcard/DCIM"
    entry = camera["entries"][0]
    assert (entry["name"], entry["size"], entry["mtime"]) == ("IMG_1.jpg", 4000, 1_700_000_000)
    assert p.GET_OBJECT_INFO not in fake_phone.log  # listed with GetObjectPropList, not file by file
    await s.close()


async def test_listing_without_prop_list(phone_root):
    phone = FakeMtpDevice(phone_root, ops=[op for op in FakeMtpDevice(phone_root).ops if op != p.GET_OBJECT_PROP_LIST])
    s = await _session(phone)
    names = [e.name for e in await MtpFS(s).list_entries("/sdcard/DCIM/Camera")]
    assert names == ["IMG_1.jpg"] and p.GET_OBJECT_INFO in phone.log
    await s.close()


async def test_stale_session_is_reopened(fake_phone):
    fake_phone.session_open = True  # left open by another program
    s = await _session(fake_phone)
    assert fake_phone.session_open and fake_phone.log.count(p.CLOSE_SESSION) == 1
    await s.close()


@pytest.mark.parametrize("size", [0, 1, 500, 512 - 12, 1024 * 1024 * 4 - 12, 1024 * 1024 * 4 + 3, 9_000_123])
async def test_upload_download_roundtrip(fake_phone, phone_root, size):
    """Covers packet-boundary sizes: every write but the last must be whole packets."""
    s = await _session(fake_phone)
    fs = MtpFS(s)
    data = os.urandom(size)
    progress = []
    await fs.push(io.BytesIO(data), "/sdcard/Download/f.bin", size=size, mtime=1_600_000_000,
                  progress=progress.append)
    stored = phone_root / "Download" / "f.bin"
    assert stored.read_bytes() == data and int(stored.stat().st_mtime) == 1_600_000_000
    assert sum(progress) == size
    assert b"".join([c async for c in fs.iter_range("/sdcard/Download/f.bin")]) == data
    if size > 10:
        assert b"".join([c async for c in fs.iter_range("/sdcard/Download/f.bin", 3, 7)]) == data[3:10]
    await s.close()


async def test_overwrite_mkdir_rename_delete(fake_phone, phone_root):
    s = await _session(fake_phone)
    fs = MtpFS(s)
    await fs.mkdirs(["/sdcard/Download/a/b/c"])
    assert (phone_root / "Download" / "a" / "b" / "c").is_dir()
    await fs.push(io.BytesIO(b"old"), "/sdcard/Download/a/x.txt", size=3, mtime=1)
    await fs.push(io.BytesIO(b"newer"), "/sdcard/Download/a/x.txt", size=5, mtime=1)
    assert (phone_root / "Download" / "a" / "x.txt").read_bytes() == b"newer"
    assert await fs.rename("/sdcard/Download/a/x.txt", "y.txt") == "/sdcard/Download/a/y.txt"
    assert (await fs.stat("/sdcard/Download/a/y.txt")).size == 5
    with pytest.raises(FileExistsError):
        await fs.mkdir("/sdcard/Download/a", "b")
    await fs.delete(["/sdcard/Download/a"])
    assert not (phone_root / "Download" / "a").exists() and await fs.stat("/sdcard/Download/a") is None
    assert await s.battery_level() == 77
    info = await fs.storage("/sdcard")
    assert info["total"] > 0 and info["free"] > 0
    await s.close()


async def test_locked_phone_explains(fake_phone):
    fake_phone.locked = True
    s = await _session(fake_phone)
    with pytest.raises(PermissionError, match="Unlock"):
        await MtpFS(s).listing("/sdcard", show_hidden=False)
    await s.close()


async def test_short_packet_regression(fake_phone, monkeypatch):
    """The original bug: a non-final write that wasn't whole packets made the phone stall."""
    s = await _session(fake_phone)
    monkeypatch.setattr(session_mod, "READ_CHUNK", 1000)  # 4 * 1000 isn't a multiple of 512
    data = os.urandom(50_000)
    await MtpFS(s).push(io.BytesIO(data), "/sdcard/Download/g.bin", size=len(data), mtime=1)
    assert (fake_phone.root / "Download" / "g.bin").read_bytes() == data
    await s.close()


async def test_cancelled_upload_leaves_no_file(fake_phone, phone_root):
    s = await _session(fake_phone)
    started = asyncio.Event()
    loop = asyncio.get_running_loop()

    class SlowFile(io.BytesIO):
        def read(self, n=-1):
            loop.call_soon_threadsafe(started.set)  # runs on the session's worker thread
            import time
            time.sleep(0.05)
            return super().read(min(n, 4096))

    task = asyncio.create_task(MtpFS(s).push(SlowFile(os.urandom(400_000)), "/sdcard/Download/c.bin",
                                             size=400_000, mtime=1))
    await started.wait()
    await asyncio.sleep(0.1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not (phone_root / "Download" / "c.bin").exists()
    assert not s.broken  # the cancel request worked, the session is still usable
    assert [e.name for e in await MtpFS(s).list_entries("/sdcard/Download")] == []
    await s.close()


# device detection and app integration -----------------------------------------------
@pytest.fixture
async def mtp_ctx(fake_adb, mac_home, fake_phone):
    found = [fake_phone.candidate()]
    context = AppContext(adb_port=fake_adb.port, local_root=mac_home, mtp_scanner=lambda: list(found))
    context.found = found
    await context.start(track=False)
    yield context
    await context.stop()


async def test_mtp_phone_listed_and_browsable(mtp_ctx):
    snapshot = mtp_ctx.devices.snapshot()["devices"]
    phone = next(d for d in snapshot if d["id"] == MTP_ID)
    assert phone["connection"] == "mtp" and phone["name"] == "Google Pixel 7 Pro"
    assert phone["capabilities"] == {"adb": False, "verify": False, "full_info": False}
    listing = await mtp_ctx.remote.list(MTP_ID, "/sdcard/DCIM/Camera")
    assert [e["name"] for e in listing["entries"]] == ["IMG_1.jpg"]
    with pytest.raises(Exception, match="USB debugging"):
        await mtp_ctx.remote.md5(MTP_ID, "/sdcard/DCIM/Camera/IMG_1.jpg")


async def test_transfers_over_mtp(mtp_ctx, mac_home, phone_root):
    src = mac_home / "Trip"
    (src / "day1").mkdir(parents=True)
    (src / "day1" / "a.jpg").write_bytes(os.urandom(70_000))
    (src / "b.txt").write_text("hello")
    os.utime(src / "b.txt", (1_650_000_000, 1_650_000_000))
    job = mtp_ctx.engine.submit(MTP_ID, "push", [str(src)], "/sdcard/Download", verify=True)
    # the engine creates a destination folder that doesn't exist yet, like over adb
    new_dest = mtp_ctx.engine.submit(MTP_ID, "push", [str(src / "b.txt")], "/sdcard/Download/New/Deeper")
    await asyncio.wait_for(mtp_ctx.engine.wait(new_dest.id), 10)
    assert new_dest.status == JobStatus.COMPLETED, new_dest.to_dict()
    assert (phone_root / "Download" / "New" / "Deeper" / "b.txt").read_text() == "hello"
    await asyncio.wait_for(mtp_ctx.engine.wait(job.id), 10)
    assert job.status == JobStatus.COMPLETED, job.to_dict()
    assert job.verify is False and "USB debugging" in job.notice
    dest = phone_root / "Download" / "Trip"
    assert (dest / "day1" / "a.jpg").read_bytes() == (src / "day1" / "a.jpg").read_bytes()
    assert int((dest / "b.txt").stat().st_mtime) == 1_650_000_000

    again = mtp_ctx.engine.submit(MTP_ID, "push", [str(src)], "/sdcard/Download", conflict="skip_identical")
    await asyncio.wait_for(mtp_ctx.engine.wait(again.id), 10)
    assert again.count("skipped") == 2 and again.count("done") == 0  # dates survive MTP

    back = mtp_ctx.engine.submit(MTP_ID, "pull", ["/sdcard/Download/Trip"], str(mac_home / "Back"))
    await asyncio.wait_for(mtp_ctx.engine.wait(back.id), 10)
    assert back.status == JobStatus.COMPLETED
    assert (mac_home / "Back" / "Trip" / "day1" / "a.jpg").read_bytes() == (src / "day1" / "a.jpg").read_bytes()
    assert int((mac_home / "Back" / "Trip" / "b.txt").stat().st_mtime) == 1_650_000_000

    deleted = await mtp_ctx.remote.delete(MTP_ID, ["/sdcard/Download/Trip"])
    assert deleted == ["/sdcard/Download/Trip"] and not dest.exists()


async def test_phone_info_over_mtp(mtp_ctx):
    from droidxfer.core import deviceinfo

    report = await deviceinfo.collect(mtp_ctx.remote, MTP_ID)
    assert report["verdict"] == "unknown" and report["checks"] == []
    rows = {r["label"]: r["value"] for s in report["sections"] for r in s["rows"]}
    assert rows["Level"] == "77%" and rows["Connected by"] == "USB, File transfer (MTP)"
    assert "Internal shared storage" in rows


async def test_adb_preferred_when_both(fake_adb, mac_home, phone_root):
    phone = FakeMtpDevice(phone_root, serial="FAKE123")  # same serial as the fake adb phone
    context = AppContext(adb_port=fake_adb.port, local_root=mac_home, mtp_scanner=lambda: [phone.candidate()])
    await context.start(track=False)
    try:
        devices = context.devices.snapshot()["devices"]
        assert len(devices) == 1 and devices[0]["connection"] == "usb"
        assert sorted(t["kind"] for t in devices[0]["transports"]) == ["mtp", "usb"]
        assert phone.opened_links == 0  # no MTP session while adb is used
    finally:
        await context.stop()


async def test_phone_not_in_file_transfer_mode(mtp_ctx, fake_phone):
    mtp_ctx.found[:] = [fake_phone.candidate(mode="none")]
    await mtp_ctx.mtp.scan()
    phone = next(d for d in mtp_ctx.devices.snapshot()["devices"] if d["id"] == MTP_ID)
    assert phone["state"] == "no_file_transfer" and phone["connection"] is None
    with pytest.raises(DeviceUnavailable, match="File transfer"):
        mtp_ctx.devices.transport(MTP_ID)


async def test_unplug_closes_session_and_idle_release(mtp_ctx, fake_phone, monkeypatch):
    await mtp_ctx.remote.list(MTP_ID, "/sdcard")
    session = next(iter(mtp_ctx.mtp._sessions.values()))
    monkeypatch.setattr(mtpdevices, "IDLE_CLOSE", 0)
    await mtp_ctx.mtp._close_idle()
    assert not mtp_ctx.mtp._sessions and session.link.closed  # released for other apps
    await mtp_ctx.remote.list(MTP_ID, "/sdcard")  # reopens on demand
    mtp_ctx.found.clear()
    await mtp_ctx.mtp.scan()
    assert not mtp_ctx.mtp._sessions
    assert MTP_ID not in [d["id"] for d in mtp_ctx.devices.snapshot()["devices"]]


async def test_mtp_setting_off(mtp_ctx):
    mtp_ctx.config.update({"mtp": False})
    await mtp_ctx.mtp.scan()
    assert MTP_ID not in [d["id"] for d in mtp_ctx.devices.snapshot()["devices"]]


def test_cli_info_over_mtp(monkeypatch, fake_adb, mac_home, fake_phone):
    from typer.testing import CliRunner

    from droidxfer import cli

    monkeypatch.setattr("droidxfer.core.mtpdevices.scan_usb", lambda: [fake_phone.candidate()])
    monkeypatch.setattr("droidxfer.core.context.DEFAULT_PORT", fake_adb.port, raising=False)
    original = cli.AppContext
    monkeypatch.setattr(cli, "AppContext", lambda: original(adb_port=fake_adb.port, local_root=mac_home))
    result = CliRunner().invoke(cli.app, ["info", "--device", MTP_ID])
    assert result.exit_code == 0, result.output
    assert "File transfer (MTP)" in result.output and "Authenticity checks need USB debugging" in result.output
