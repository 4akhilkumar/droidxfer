import io
import os

import pytest

from droidxfer.adb import AdbClient, AdbCommandError
from droidxfer.adb.client import parse_device_list, parse_mdns_services
from fake_adb import FakeAdb


def test_parse_device_list_long_format():
    text = (
        "28111FDH2000XB         device usb:336592896X product:cheetah model:Pixel_7_Pro device:cheetah transport_id:3\n"
        "192.168.1.50:5555      device product:cheetah model:Pixel_7_Pro device:cheetah transport_id:4\n"
        "ZY223                  unauthorized usb:1-1 transport_id:5\n"
        "adb-28111FDH2000XB-AbCdEf._adb-tls-connect._tcp device product:cheetah model:Pixel_7_Pro device:cheetah transport_id:6\n"
    )
    usb, tcp, unauth, mdns = parse_device_list(text)
    assert (usb.serial, usb.state, usb.model, usb.is_wireless) == ("28111FDH2000XB", "device", "Pixel_7_Pro", False)
    assert tcp.is_wireless and tcp.transport_id == "4"
    assert unauth.state == "unauthorized"
    assert mdns.is_wireless


def test_parse_mdns_services():
    text = (
        "adb-28111FDH2000XB-AbCdEf\t_adb-tls-connect._tcp\t192.168.1.50:41235\n"
        "droidxfer-abc123\t_adb-tls-pairing._tcp\t192.168.1.50:37001\n"
    )
    connect, pairing = parse_mdns_services(text)
    assert connect.is_connect and connect.port == 41235
    assert pairing.is_pairing and pairing.name == "droidxfer-abc123"


async def test_host_services(fake_adb):
    client = AdbClient(port=fake_adb.port)
    assert await client.version() == 0x29
    [dev] = await client.devices()
    assert dev.serial == "FAKE123" and dev.state == "device"
    assert "stat_v2" in await client.features("FAKE123")
    assert (await client.connect("192.168.1.50", 5555)).startswith("connected to")


async def test_unknown_transport_fails(fake_adb):
    client = AdbClient(port=fake_adb.port)
    with pytest.raises(AdbCommandError, match="device not found"):
        await client.shell("NOPE", "echo hi")


async def test_shell_v2_exit_code(fake_adb, device_root):
    client = AdbClient(port=fake_adb.port)
    (device_root / "sdcard" / "a").mkdir()
    res = await client.shell("FAKE123", "mkdir -- /sdcard/a")
    assert res.exit_code == 1 and "File exists" in res.text
    res = await client.shell("FAKE123", "mkdir -p -- /sdcard/b/c")
    assert res.ok and (device_root / "sdcard" / "b" / "c").is_dir()


@pytest.mark.parametrize("features", [("shell_v2", "stat_v2", "ls_v2"), ("shell_v2",)])
async def test_sync_roundtrip(tmp_path, features):
    root = tmp_path / "dev"
    (root / "sdcard").mkdir(parents=True)
    server = FakeAdb(root, features=features).start()
    try:
        client = AdbClient(port=server.port)
        payload = os.urandom(300_000)
        seen = []
        async with await client.sync("FAKE123", set(features)) as sc:
            await sc.push(io.BytesIO(payload), "/sdcard/x/data.bin", mtime=1_700_000_000, progress=seen.append)
            st = await sc.stat("/sdcard/x/data.bin")
            assert st and st.is_file and st.size == len(payload) and st.mtime == 1_700_000_000
            assert await sc.stat("/sdcard/missing") is None
            names = {e.name for e in await sc.list("/sdcard/x")}
            assert names == {"data.bin"}
            out = io.BytesIO()
            assert await sc.pull("/sdcard/x/data.bin", out) == len(payload)
            assert out.getvalue() == payload
        assert sum(seen) == len(payload)
    finally:
        server.stop()


async def test_pull_missing_file_raises(fake_adb):
    client = AdbClient(port=fake_adb.port)
    sc = await client.sync("FAKE123", {"stat_v2", "ls_v2"})
    with pytest.raises(AdbCommandError, match="does not exist"):
        await sc.pull("/sdcard/nope.txt", io.BytesIO())
    assert sc.broken
    await sc.close()
