"""Phone info report and authenticity checks."""

import pytest

from droidxfer.core import deviceinfo
from droidxfer.core.deviceinfo import _parse_date, split_sections

from test_api import authed, client  # noqa: F401  (fixtures)

NOW = 1_736_500_000.0  # 2025-01-10


def _rows(report, section):
    return {r["label"]: r["value"] for s in report["sections"] if s["id"] == section for r in s["rows"]}


def _checks(report):
    return {c["id"]: c["status"] for c in report["checks"]}


async def _report(ctx, fake_adb):
    report = await deviceinfo.collect(ctx.remote, "FAKE123")
    sections = split_sections(fake_adb._info_output().decode())
    return deviceinfo.build_report(sections, serial="FAKE123", connection="usb", now=NOW) | {"live": report}


async def test_genuine_phone_passes(ctx, fake_adb):
    report = await _report(ctx, fake_adb)
    assert report["verdict"] == "pass", report["checks"]
    assert set(_checks(report).values()) == {"pass"}
    device = _rows(report, "device")
    assert device["Name"] == "Google Pixel 7 Pro"
    assert device["Chip"] == "Google GS201"
    assert device["CPU"] == "arm64-v8a, 8 cores"
    assert device["RAM"] == "11.2 GB"
    assert device["Screen"] == "1440 × 3120 at 560 dpi"
    assert device["Launched with"] == "Android 13 (API 33)"
    battery = _rows(report, "battery")
    assert battery["Level"] == "57%"
    assert battery["Status"] == "Charging (USB)"
    assert battery["Capacity"] == "4,436 of 5,002 mAh design (89%)"
    assert battery["Charge cycles"] == "494"
    assert battery["Temperature"] == "36.3 °C"
    assert battery["Manufactured"] == "2024-03-09"
    assert battery["First used"] == "Not available"
    software = _rows(report, "software")
    assert software["Security patch"] == "2025-01-05 (5 days old)"
    assert software["Baseband"] == "g5300q-1"
    assert software["Google Play services"] == "26.43.30"
    assert report["live"]["name"] == "Google Pixel 7 Pro"  # the real adb round trip parsed too


@pytest.mark.parametrize(
    "change, check, status",
    [
        ({"ro.boot.verifiedbootstate": "orange"}, "verified_boot", "fail"),
        ({"ro.boot.verifiedbootstate": "yellow"}, "verified_boot", "warn"),
        ({"ro.boot.flash.locked": "0", "ro.boot.vbmeta.device_state": "unlocked"}, "bootloader", "fail"),
        ({"ro.build.tags": "test-keys"}, "official_build", "fail"),
        ({"ro.build.type": "userdebug"}, "official_build", "fail"),
        ({"ro.vendor.build.fingerprint": "google/cheetah/cheetah:14/UQ1A.240105.004/111:user/release-keys"},
         "consistency", "warn"),
        ({"ro.product.brand": "samsung"}, "consistency", "warn"),
        ({"ro.kernel.qemu": "1"}, "real_hardware", "fail"),
        ({"ro.debuggable": "1"}, "root", "fail"),
        ({"ro.crypto.state": "unencrypted"}, "encryption", "warn"),
        ({"ro.build.version.security_patch": "2024-01-05"}, "security_patch", "warn"),
    ],
)
async def test_tampering_is_flagged(ctx, fake_adb, change, check, status):
    fake_adb.props.update(change)
    report = await _report(ctx, fake_adb)
    assert _checks(report)[check] == status
    assert report["verdict"] == ("fail" if status == "fail" else "warn")


async def test_root_and_selinux(ctx, fake_adb):
    fake_adb.su_paths = ["/system/xbin/su"]
    fake_adb.selinux = "Permissive"
    report = await _report(ctx, fake_adb)
    assert _checks(report)["root"] == "fail" and _checks(report)["selinux"] == "fail"
    assert "/system/xbin/su" in next(c["detail"] for c in report["checks"] if c["id"] == "root")


async def test_missing_values_are_not_guessed(ctx, fake_adb):
    fake_adb.battery_sysfs = {}
    for key in ("ro.boot.verifiedbootstate", "ro.boot.flash.locked", "ro.boot.vbmeta.device_state", "ro.soc.model",
                "ro.soc.manufacturer"):
        fake_adb.props.pop(key)
    report = await _report(ctx, fake_adb)
    battery = _rows(report, "battery")
    assert battery["Capacity"] == battery["Charge cycles"] == battery["Battery serial"] == "Not available"
    assert _rows(report, "device")["Chip"] == "cheetah"  # falls back to ro.hardware
    assert _checks(report)["verified_boot"] == "unknown" and _checks(report)["bootloader"] == "unknown"
    assert report["verdict"] == "pass"  # unknown isn't treated as a failure


def test_parse_date():
    assert str(_parse_date("1709942400")) == "2024-03-09"
    assert str(_parse_date("20240309")) == "2024-03-09"
    assert str(_parse_date("2024-03-09")) == "2024-03-09"
    assert _parse_date("0") is None and _parse_date("") is None and _parse_date("garbage") is None
    assert _parse_date("20241399") is None


def test_info_endpoint(authed):  # noqa: F811
    r = authed.get("/api/devices/FAKE123/info")
    assert r.status_code == 200
    body = r.json()
    assert body["name"] == "Google Pixel 7 Pro" and body["checks"] and body["notes"]
    assert authed.get("/api/devices/NOPE/info").status_code == 404


def test_preview_endpoint_converts_heic(authed, mac_home, device_root):  # noqa: F811
    pillow_heif = pytest.importorskip("pillow_heif")
    import io

    from PIL import Image

    buf = io.BytesIO()
    pillow_heif.from_pillow(Image.new("RGB", (3000, 2000), (10, 200, 10))).save(buf)
    (device_root / "sdcard" / "photo.heic").write_bytes(buf.getvalue())
    (mac_home / "photo.heic").write_bytes(buf.getvalue())
    for url, params in (("/api/devices/FAKE123/preview", {"path": "/sdcard/photo.heic"}),
                        ("/api/local/preview", {"path": str(mac_home / "photo.heic")})):
        r = authed.get(url, params=params)
        assert r.status_code == 200 and r.headers["content-type"] == "image/jpeg"
        assert Image.open(io.BytesIO(r.content)).size == (2048, 1365)
    assert authed.get("/api/local/preview", params={"path": str(mac_home)}).status_code in (400, 415)


def test_static_files_are_versioned_and_revalidated(authed):  # noqa: F811
    import re

    page = authed.get("/").text
    css = re.search(r'href="(/static/[0-9a-f]{12}/styles\.css)"', page).group(1)
    assert re.search(r'src="/static/[0-9a-f]{12}/js/app\.js"', page)
    assert authed.get(css.replace(css.split("/")[2], "000000000000")).status_code == 404  # old versions are gone
    r = authed.get(css)
    assert r.status_code == 200 and r.headers["cache-control"] == "no-cache" and r.headers.get("etag")
    again = authed.get(css, headers={"if-none-match": r.headers["etag"]})
    assert again.status_code == 304
