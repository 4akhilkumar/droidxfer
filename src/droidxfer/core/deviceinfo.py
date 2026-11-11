"""Detailed phone information (hardware, software, battery) and firmware authenticity checks.

Everything is read with one read-only shell command over adb. Values a phone doesn't expose (many OEMs
hide battery cycle count or design capacity) are reported as "Not available" rather than guessed.
"""

from __future__ import annotations

import re
import time
from datetime import date, datetime, timezone

from .remotefs import RemoteFS, RemoteOpError

_BATTERY_FILES = (
    "cycle_count charge_full charge_full_design manufacturing_date manufacture_date first_usage_date "
    "serial_number health_index"
).split()
_SU_DIRS = "/system/bin /system/xbin /sbin /su/bin /vendor/bin /system/sbin /data/local/xbin /data/local/bin /data/adb"

# One round trip; sections are delimited by "@@name" marker lines.
INFO_COMMAND = "; ".join(
    [
        "echo @@props; getprop",
        "echo @@battery; dumpsys battery 2>/dev/null",
        "echo @@sysbat; for f in " + " ".join(_BATTERY_FILES) + "; do "
        "p=/sys/class/power_supply/battery/$f; [ -r $p ] && echo \"$f=$(cat $p 2>/dev/null)\"; done",
        "echo @@meminfo; grep -E '^(MemTotal|MemAvailable):' /proc/meminfo",
        "echo @@cpus; grep -c ^processor /proc/cpuinfo",
        "echo @@wm; wm size 2>/dev/null; wm density 2>/dev/null",
        "echo @@df; df -k /data 2>/dev/null",
        "echo @@kernel; uname -r",
        "echo @@selinux; getenforce 2>/dev/null",
        f"echo @@su; for d in {_SU_DIRS}; do [ -e $d/su ] && echo $d/su; done",
        "echo @@uptime; cat /proc/uptime",
        "echo @@gms; dumpsys package com.google.android.gms 2>/dev/null | grep -m1 versionName",
        "echo @@end",
    ]
)

ANDROID_FOR_SDK = {
    21: "5.0", 22: "5.1", 23: "6", 24: "7.0", 25: "7.1", 26: "8.0", 27: "8.1", 28: "9", 29: "10",
    30: "11", 31: "12", 32: "12L", 33: "13", 34: "14", 35: "15", 36: "16", 37: "17",
}
BATTERY_STATUS = {1: "Unknown", 2: "Charging", 3: "Discharging", 4: "Not charging", 5: "Full"}
BATTERY_HEALTH = {
    1: "Unknown", 2: "Good", 3: "Overheating", 4: "Dead", 5: "Over voltage", 6: "Failure", 7: "Cold",
}
NA = "Not available"
PATCH_WARN_DAYS = 120

_PROP = re.compile(r"^\[(.+?)\]: \[(.*)\]$")
_KV = re.compile(r"^\s*([^:]+):\s*(.*)$")


# parsing -----------------------------------------------------------------------------
def split_sections(text: str) -> dict[str, str]:
    sections: dict[str, list[str]] = {}
    current: list[str] | None = None
    for line in text.splitlines():
        if line.startswith("@@"):
            current = sections.setdefault(line[2:].strip(), [])
        elif current is not None:
            current.append(line)
    return {k: "\n".join(v).strip() for k, v in sections.items()}


def parse_props(text: str) -> dict[str, str]:
    return {m.group(1): m.group(2) for line in text.splitlines() if (m := _PROP.match(line))}


def parse_colon_pairs(text: str) -> dict[str, str]:
    """`key: value` lines (dumpsys battery, /proc/meminfo); keys lower-cased."""
    return {m.group(1).strip().lower(): m.group(2).strip() for line in text.splitlines() if (m := _KV.match(line))}


def parse_equals(text: str) -> dict[str, str]:
    return dict(line.split("=", 1) for line in text.splitlines() if "=" in line)


def _int(value: str | None) -> int | None:
    try:
        return int(str(value).strip().split()[0])
    except (TypeError, ValueError, IndexError):
        return None


def _gb(n_bytes: float) -> str:
    return f"{n_bytes / 1024**3:.1f} GB"


def _parse_date(value: str | None) -> date | None:
    """Battery dates come as epoch seconds (Pixel) or YYYYMMDD / YYYY-MM-DD on other phones."""
    raw = (value or "").strip()
    if not raw or raw in ("0", "-1"):
        return None
    try:
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", raw):
            return date.fromisoformat(raw)
        if re.fullmatch(r"(19|20)\d{6}", raw):
            return date(int(raw[:4]), int(raw[4:6]), int(raw[6:]))
        if raw.isdigit() and int(raw) > 100_000_000:
            return datetime.fromtimestamp(int(raw), timezone.utc).date()
    except ValueError:
        return None
    return None


def _duration(seconds: float) -> str:
    days, rest = divmod(int(seconds), 86400)
    hours, rest = divmod(rest, 3600)
    minutes = rest // 60
    if days:
        return f"{days} d {hours} h"
    return f"{hours} h {minutes} min" if hours else f"{minutes} min"


# report ------------------------------------------------------------------------------
def _row(label: str, value: object, hint: str | None = None) -> dict:
    text = NA if value in (None, "") else str(value)
    row = {"label": label, "value": text}
    if hint:
        row["hint"] = hint
    return row


def _check(cid: str, label: str, status: str, value: str, detail: str) -> dict:
    return {"id": cid, "label": label, "status": status, "value": value, "detail": detail}


def _fingerprint_parts(fp: str) -> tuple[str, str, str] | None:
    m = re.match(r"^([^/]+)/([^/]+)/([^:]+):", fp or "")
    return (m.group(1), m.group(2), m.group(3)) if m else None


def authenticity_checks(props: dict[str, str], su_paths: list[str], selinux: str, today: date) -> list[dict]:
    checks: list[dict] = []
    p = props.get

    vb = p("ro.boot.verifiedbootstate", "").lower()
    if vb == "green":
        checks.append(_check("verified_boot", "Verified boot", "pass", "Green",
                             "The phone booted firmware signed by its manufacturer, checked by the hardware."))
    elif vb == "yellow":
        checks.append(_check("verified_boot", "Verified boot", "warn", "Yellow",
                             "The firmware is signed with a custom key, not the manufacturer's."))
    elif vb in ("orange", "red"):
        checks.append(_check("verified_boot", "Verified boot", "fail", vb.capitalize(),
                             "The bootloader is unlocked or the firmware failed verification, so it may be modified."))
    else:
        checks.append(_check("verified_boot", "Verified boot", "unknown", NA, "This phone doesn't report its boot state."))

    locked, state = p("ro.boot.flash.locked", ""), p("ro.boot.vbmeta.device_state", "").lower()
    if locked == "1" or state == "locked":
        checks.append(_check("bootloader", "Bootloader", "pass", "Locked",
                             "Only manufacturer-signed firmware can be installed."))
    elif locked == "0" or state == "unlocked":
        checks.append(_check("bootloader", "Bootloader", "fail", "Unlocked",
                             "Unofficial firmware can be installed. Sellers sometimes unlock and re-flash phones."))
    else:
        checks.append(_check("bootloader", "Bootloader", "unknown", NA, "This phone doesn't report its lock state."))

    build_type, tags = p("ro.build.type", ""), p("ro.build.tags", "")
    value = f"{build_type or '?'} / {tags or '?'}"
    if build_type == "user" and tags == "release-keys":
        checks.append(_check("official_build", "Official build", "pass", value,
                             "A normal consumer build signed with release keys."))
    else:
        checks.append(_check("official_build", "Official build", "fail", value,
                             "Retail phones ship 'user' builds signed with 'release-keys'. This is a test or developer build."))

    # Every firmware image (system, vendor, boot, odm, ...) reports a fingerprint
    # "brand/product/device:release/id/incremental:type/tags". The brand/product part legitimately differs
    # for the shared generic system image, so compare only the build part after the first colon.
    fps = {k: v for k, v in props.items() if k.endswith("build.fingerprint") and v}
    builds = {k: v.split(":", 1)[1] for k, v in fps.items() if ":" in v}
    main = _fingerprint_parts(p("ro.build.fingerprint", ""))
    brand, product = p("ro.product.brand", "").lower(), p("ro.product.name", "")
    problems = []
    if main and brand and main[0].lower() != brand:
        problems.append(f"fingerprint brand '{main[0]}' differs from product brand '{p('ro.product.brand')}'")
    if main and product and main[1] != product:
        problems.append(f"fingerprint product '{main[1]}' differs from '{product}'")
    if len(set(builds.values())) > 1:
        odd = sorted(k.removeprefix("ro.").removesuffix(".build.fingerprint") or "build" for k, v in builds.items()
                     if v != builds.get("ro.build.fingerprint"))
        problems.append(f"firmware images report different builds ({', '.join(odd)})")
    if not fps:
        checks.append(_check("consistency", "Firmware consistency", "unknown", NA, "No build fingerprints reported."))
    elif problems:
        checks.append(_check("consistency", "Firmware consistency", "warn", "Mismatch",
                             "; ".join(problems).capitalize() + ". This can happen after partial updates, "
                             "but also on phones with edited system properties."))
    else:
        checks.append(_check("consistency", "Firmware consistency", "pass", f"{len(fps)} images match",
                             "All firmware parts report the same build, matching the phone's brand and model."))

    hardware = (p("ro.hardware", "") + " " + p("ro.board.platform", "")).lower()
    if p("ro.kernel.qemu") == "1" or any(x in hardware for x in ("goldfish", "ranchu", "vbox")):
        checks.append(_check("real_hardware", "Real hardware", "fail", "Emulator",
                             "This is an Android emulator, not a physical phone."))
    else:
        checks.append(_check("real_hardware", "Real hardware", "pass", "Yes", "No emulator markers found."))

    insecure = []
    if su_paths:
        insecure.append("su binary at " + ", ".join(su_paths))
    if p("ro.secure") == "0":
        insecure.append("ro.secure=0")
    if p("ro.debuggable") == "1":
        insecure.append("ro.debuggable=1")
    if insecure:
        checks.append(_check("root", "Root access", "fail", "Possible root",
                             "Found " + "; ".join(insecure) + "."))
    else:
        checks.append(_check("root", "Root access", "pass", "Not rooted",
                             "No su binary and the system isn't debuggable."))

    mode = selinux.strip().capitalize()
    if mode == "Enforcing":
        checks.append(_check("selinux", "SELinux", "pass", mode, "Android's security policy is enforced."))
    elif mode:
        checks.append(_check("selinux", "SELinux", "fail", mode, "Android's security policy is not enforced."))
    else:
        checks.append(_check("selinux", "SELinux", "unknown", NA, "Could not read the SELinux mode."))

    if p("ro.crypto.state") == "encrypted":
        checks.append(_check("encryption", "Storage encryption", "pass", "Encrypted", "User data is encrypted."))
    else:
        checks.append(_check("encryption", "Storage encryption", "warn", p("ro.crypto.state") or NA,
                             "User data doesn't report as encrypted."))

    patch = p("ro.build.version.security_patch", "")
    try:
        age = (today - date.fromisoformat(patch)).days
    except ValueError:
        checks.append(_check("security_patch", "Security updates", "unknown", NA, "No security patch date reported."))
    else:
        status = "pass" if age <= PATCH_WARN_DAYS else "warn"
        detail = f"Patch level {patch} ({max(age, 0)} days old)."
        if status == "warn":
            detail += " Check Settings > System > Software updates."
        checks.append(_check("security_patch", "Security updates", status, patch, detail))
    return checks


def build_report(sections: dict[str, str], *, serial: str, connection: str | None, now: float | None = None) -> dict:
    now = time.time() if now is None else now
    today = datetime.fromtimestamp(now, timezone.utc).date()
    props = parse_props(sections.get("props", ""))
    p = props.get
    battery = parse_colon_pairs(sections.get("battery", ""))
    sysbat = parse_equals(sections.get("sysbat", ""))
    meminfo = parse_colon_pairs(sections.get("meminfo", ""))
    su_paths = [line.strip() for line in sections.get("su", "").splitlines() if line.strip()]

    manufacturer = p("ro.product.manufacturer", "")
    model = p("ro.product.model", "")
    name = f"{manufacturer} {model}".strip() or serial

    # Device -------------------------------------------------------------------------
    mem_kb = _int(meminfo.get("memtotal"))
    storage_total = storage_free = None
    for line in reversed(sections.get("df", "").splitlines()):
        nums = [f for f in line.split()[1:4] if f.isdigit()]
        if len(nums) == 3:
            storage_total, storage_free = int(nums[0]) * 1024, int(nums[2]) * 1024
            break
    wm = sections.get("wm", "")
    size = re.search(r"Override size:\s*(\S+)", wm) or re.search(r"Physical size:\s*(\S+)", wm)
    density = re.search(r"Override density:\s*(\d+)", wm) or re.search(r"Physical density:\s*(\d+)", wm)
    chip = " ".join(x for x in (p("ro.soc.manufacturer"), p("ro.soc.model")) if x) or p("ro.board.platform") or p("ro.hardware")
    cores = _int(sections.get("cpus"))
    first_api = _int(p("ro.product.first_api_level"))
    device_rows = [
        _row("Name", name),
        _row("Brand", p("ro.product.brand")),
        _row("Model", model),
        _row("Codename", p("ro.product.device")),
        _row("Hardware SKU", p("ro.boot.hardware.sku") or p("ro.boot.product.hardware.sku")),
        _row("Hardware revision", p("ro.boot.hardware.revision") or p("ro.revision")),
        _row("Chip", chip),
        _row("CPU", f"{p('ro.product.cpu.abi', '?')}" + (f", {cores} cores" if cores else "")),
        _row("RAM", _gb(mem_kb * 1024) if mem_kb else None,
             "Usable memory; it's a little below the advertised size because some is reserved."),
        _row("Storage", f"{_gb(storage_total)} ({_gb(storage_free)} free)" if storage_total else None,
             "Usable space; the advertised size is a little larger."),
        _row("Screen", (size.group(1).replace("x", " × ") if size else "") + (f" at {density.group(1)} dpi" if density else "")),
        _row("Launched with", f"Android {ANDROID_FOR_SDK.get(first_api, '?')} (API {first_api})" if first_api else None),
        _row("adb serial", serial),
        _row("Connected by", {"usb": "USB", "wifi": "Wi-Fi"}.get(connection or "", connection)),
    ]

    # Software -----------------------------------------------------------------------
    patch = p("ro.build.version.security_patch", "")
    try:
        patch_text = f"{patch} ({max((today - date.fromisoformat(patch)).days, 0)} days old)"
    except ValueError:
        patch_text = patch
    baseband = ", ".join(dict.fromkeys(x.strip() for x in p("gsm.version.baseband", "").split(",") if x.strip()))
    gms = re.search(r"versionName=(\S+)", sections.get("gms", ""))
    uptime = _int((sections.get("uptime") or "").split(".")[0])
    software_rows = [
        _row("Android", f"{p('ro.build.version.release', '?')} (API {p('ro.build.version.sdk', '?')})"),
        _row("Security patch", patch_text),
        _row("Build number", p("ro.build.display.id") or p("ro.build.id")),
        _row("Build date", p("ro.build.date")),
        _row("Build fingerprint", p("ro.build.fingerprint")),
        _row("Kernel", sections.get("kernel")),
        _row("Bootloader", p("ro.bootloader") or p("ro.boot.bootloader")),
        _row("Baseband", baseband),
        _row("Google Play services", gms.group(1) if gms else None),
        _row("Uptime", _duration(uptime) if uptime is not None else None),
    ]

    # Battery ------------------------------------------------------------------------
    level, scale = _int(battery.get("level")), _int(battery.get("scale")) or 100
    status = BATTERY_STATUS.get(_int(battery.get("status")) or 0)
    sources = [s for s, key in (("USB", "usb powered"), ("charger", "ac powered"), ("wireless", "wireless powered"),
                                ("dock", "dock powered")) if battery.get(key) == "true"]
    if status in ("Charging", "Full") and sources:
        status = f"{status} ({', '.join(sources)})"
    temp = _int(battery.get("temperature"))
    volt = _int(battery.get("voltage"))
    full, design = _int(sysbat.get("charge_full")), _int(sysbat.get("charge_full_design"))
    capacity = health_pct = None
    if full and design and full > 0 and design > 0:
        health_pct = round(full / design * 100)
        capacity = f"{full // 1000:,} of {design // 1000:,} mAh design ({health_pct}%)"
    cycles = _int(sysbat.get("cycle_count"))
    made = _parse_date(sysbat.get("manufacturing_date") or sysbat.get("manufacture_date"))
    first_use = _parse_date(sysbat.get("first_usage_date"))
    battery_rows = [
        _row("Level", f"{round(level * 100 / scale)}%" if level is not None else None),
        _row("Status", status),
        _row("Health", BATTERY_HEALTH.get(_int(battery.get("health")) or 0)),
        _row("Capacity", capacity, "Current full charge compared with the design capacity. Below about 80% the battery is worn."),
        _row("Charge cycles", f"{cycles:,}" if cycles is not None and cycles >= 0 else None,
             "Most phone batteries are rated for 800-1,000 cycles to 80% capacity."),
        _row("Temperature", f"{temp / 10:.1f} °C" if temp is not None else None),
        _row("Voltage", f"{volt / 1000:.2f} V" if volt else None),
        _row("Technology", battery.get("technology") or sysbat.get("technology")),
        _row("Manufactured", made.isoformat() if made else None),
        _row("First used", first_use.isoformat() if first_use else None),
        _row("Battery serial", sysbat.get("serial_number")),
    ]

    checks = authenticity_checks(props, su_paths, sections.get("selinux", ""), today)
    statuses = {c["status"] for c in checks}
    if "fail" in statuses:
        verdict, message = "fail", "Signs of modified, rooted or unofficial firmware. See the failed checks."
    elif "warn" in statuses:
        verdict, message = "warn", "No signs of tampering, but some checks need attention."
    else:
        verdict, message = "pass", "Official, unmodified firmware with a locked bootloader."

    return {
        "name": name,
        "verdict": verdict,
        "message": message,
        "battery_health_percent": health_pct,
        "checks": checks,
        "sections": [
            {"id": "device", "title": "Device", "rows": device_rows},
            {"id": "software", "title": "Software", "rows": software_rows},
            {"id": "battery", "title": "Battery", "rows": battery_rows},
        ],
        "notes": [
            "IMEI can't be read over adb on Android 10 and later. Dial *#06# on the phone and compare it "
            "with the box, the receipt and the SIM tray label.",
            "These checks catch modified, rooted or unofficial firmware, but a determined counterfeit can fake "
            "system properties. Also compare the chip, RAM and storage above with the model's published specs.",
        ],
        "collected_at": now,
    }


async def collect(remote: RemoteFS, device_id: str) -> dict:
    t = remote.devices.transport(device_id)
    res = await remote.shell(device_id, INFO_COMMAND, timeout=30)
    sections = split_sections(res.text)
    if "props" not in sections:
        raise RemoteOpError("the phone returned no information")
    return build_report(sections, serial=t.serial, connection=t.kind)
