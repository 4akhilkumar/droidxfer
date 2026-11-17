"""Read the phone's system log (logcat) over adb, filter it and explain common events in plain words.

Everything here is read-only except `clear()`, which empties the phone's log buffers (`logcat -c`).

Two ways to read:
- dump():  what is already stored on the phone (`logcat -d`), for "what just happened?" after the fact.
           Filters run on the phone first (`grep`), because a busy phone logs hundreds of lines a second.
- stream(): new lines as they are written (`logcat` without -d), filtered here on the Mac.

Presets are ready-made filters for common problems (headphone buttons, Bluetooth, crashes, audio).
Lines they recognise get a short `note` in plain words, e.g. "Headphones (Bluetooth) sent Pause".
"""

from __future__ import annotations

import asyncio
import re
import shlex
import time
from dataclasses import dataclass
from typing import AsyncIterator, Callable

from .apps import PACKAGE_RE
from .remotefs import RemoteFS, RemoteOpError

LEVELS = "VDIWEF"
LEVEL_NAMES = {"V": "Verbose", "D": "Debug", "I": "Info", "W": "Warning", "E": "Error", "F": "Fatal"}
WINDOWS = (1, 5, 10, 30, 60)  # minutes offered in the UI; 0 means everything stored on the phone
DEFAULT_WINDOW = 10
DEFAULT_LIMIT = 5000
MAX_LIMIT = 50000
MAX_QUERY = 200

_LINE = re.compile(r"^\s*(\d+\.\d+)\s+(\d+)\s+(\d+)\s+([VDIWEFA])\s+(.*?)\s*: (.*)$")
_MAC = re.compile(r"(?<![0-9A-Fa-f:])(?:[0-9A-Fa-f]{2}:){4}([0-9A-Fa-f]{2}:[0-9A-Fa-f]{2})(?![0-9A-Fa-f:])")


@dataclass(slots=True)
class LogLine:
    time: float
    pid: int
    tid: int
    level: str
    tag: str
    message: str
    note: str | None = None

    def to_dict(self) -> dict:
        return {
            "time": self.time, "pid": self.pid, "tid": self.tid, "level": self.level,
            "tag": self.tag, "message": self.message, "note": self.note,
        }

    def to_text(self) -> str:
        stamp = time.strftime("%m-%d %H:%M:%S", time.localtime(self.time)) + f".{int(self.time * 1000) % 1000:03d}"
        text = f"{stamp} {self.pid:>5} {self.tid:>5} {self.level} {self.tag}: {self.message}"
        return f"{text}    <-- {self.note}" if self.note else text


def parse_line(raw: str) -> LogLine | None:
    """Parse one `logcat -v threadtime -v epoch` line; separators like '--------- beginning of main' give None."""
    m = _LINE.match(raw)
    if not m:
        return None
    level = "F" if m.group(4) == "A" else m.group(4)
    return LogLine(float(m.group(1)), int(m.group(2)), int(m.group(3)), level, m.group(5), m.group(6))


def mask_addresses(text: str) -> str:
    """Hide all but the last two bytes of Bluetooth/Wi-Fi hardware addresses (AA:BB:CC:DD:EE:FF)."""
    return _MAC.sub(lambda m: "XX:XX:XX:XX:" + m.group(1), text)


# plain-word explanations ---------------------------------------------------------------
SENDERS = {
    "com.android.bluetooth": "Headphones (Bluetooth)",
    "com.google.android.googlequicksearchbox": "Google app (Assistant)",
    "com.android.systemui": "Phone's media controls",
    "com.google.android.bluetooth": "Headphones (Bluetooth)",
}
KEYS = {
    "PLAY": "Play", "PAUSE": "Pause", "PLAY_PAUSE": "a Play/Pause toggle", "STOP": "Stop",
    "NEXT": "Next track", "PREVIOUS": "Previous track", "FAST_FORWARD": "Fast forward", "REWIND": "Rewind",
    "HEADSETHOOK": "the headset button",
}
AVRCP_STATUS = {0: "stopped", 1: "playing", 2: "paused", 3: "seeking forward", 4: "seeking back", 255: "error"}
PROFILES = {
    "A2DP": "Music audio", "HEADSET": "Calls/microphone", "LE_AUDIO": "LE Audio",
    "HEARING_AID": "Hearing aid", "HID": "Buttons/controls (HID)",
}
STATES = {
    "STATE_CONNECTED": "connected", "STATE_DISCONNECTED": "disconnected",
    "STATE_CONNECTING": "connecting", "STATE_DISCONNECTING": "disconnecting",
}
USAGES = {
    "USAGE_MEDIA": "started playing media",
    "USAGE_GAME": "started game audio",
    "USAGE_NOTIFICATION": "played a notification sound, interrupting other audio",
    "USAGE_NOTIFICATION_RINGTONE": "is ringing (incoming call)",
    "USAGE_VOICE_COMMUNICATION": "started a call",
    "USAGE_ALARM": "started an alarm",
    "USAGE_ASSISTANT": "Assistant is listening or speaking",
    "USAGE_ASSISTANCE_NAVIGATION_GUIDANCE": "gave a navigation prompt",
}

_KEY = re.compile(r"dispatchMediaKeyEvent, pkg=([^\s,]+).*?action=ACTION_DOWN, keyCode=KEYCODE_(?:MEDIA_)?(\w+)")
_PLAY_STATUS = re.compile(r"Update PlaybackStatus (\d+) -> (\d+)")
_CONN = re.compile(r"\b(A2DP|HEADSET|LE_AUDIO|HEARING_AID|HID)\w* connection state changed: .*?(STATE_[A-Z]+) -> (STATE_[A-Z]+)")
_ACTIVE = re.compile(r"Active Device Changed: (\S+) for profile: (\w+)")
_FOCUS = re.compile(r"(requestAudioFocus|abandonAudioFocus)\(\) from .*?callingPack=([^\s,]+)")
_USAGE = re.compile(r"\bAA=(USAGE_\w+)")
_ANR = re.compile(r"ANR in (\S+)")
_DIED = re.compile(r"Process (\S+) \(pid \d+\) has died")
_CRASH_PROC = re.compile(r"^Process: (\S+?),")


def _sender(pkg: str) -> str:
    return SENDERS.get(pkg, pkg)


def _audio_owner(pkg: str) -> str:
    return "System sounds" if pkg == "com.android.systemui" else SENDERS.get(pkg, pkg)


def explain(line: LogLine) -> str | None:
    """A short plain-words note for events the presets look for, or None."""
    msg = line.message
    if "dispatchMediaKeyEvent" in msg and (m := _KEY.search(msg)):
        pkg, key = m.groups()
        note = f"{_sender(pkg)} sent {KEYS.get(key, key.replace('_', ' ').title())}"
        if key == "PLAY_PAUSE":
            note += ": it flips whatever is happening, so a missed one inverts later ones"
        return note
    if "PlaybackStatus" in msg and (m := _PLAY_STATUS.search(msg)):
        before, after = (AVRCP_STATUS.get(int(g), g) for g in m.groups())
        return f"Playback {before} → {after} (as reported to the headphones)"
    if "opcode=Opcode::PASS_THROUGH" in msg:
        return "Headphones sent a button signal over Bluetooth"
    if "connection state changed" in msg and (m := _CONN.search(msg)):
        profile, _, after = m.groups()
        return f"{PROFILES.get(profile, profile)} {STATES.get(after, after.lower())}"
    if "Active Device Changed" in msg and (m := _ACTIVE.search(msg)):
        device, profile = m.groups()
        what = PROFILES.get(profile, profile)
        return f"{what}: no active device" if device == "null" else f"{what} now goes to this device"
    if "AudioFocus()" in msg and (m := _FOCUS.search(msg)):
        kind, pkg = m.groups()
        if kind == "abandonAudioFocus":
            return f"{_audio_owner(pkg)} finished using audio"
        usage = m2.group(1) if (m2 := _USAGE.search(msg)) else ""
        return f"{_audio_owner(pkg)} {USAGES.get(usage, 'asked for audio')}"
    if "FATAL EXCEPTION" in msg:
        return "An app crashed (the lines below show where)"
    if line.tag == "AndroidRuntime" and (m := _CRASH_PROC.match(msg)):
        return f"Crashed app: {m.group(1)}"
    if "ANR in" in msg and (m := _ANR.search(msg)):
        return f"{m.group(1)} stopped responding (ANR)"
    if "Fatal signal" in msg:
        return "A native crash (the app or service was killed)"
    if "has died" in msg and (m := _DIED.search(msg)):
        return f"{m.group(1)} was stopped by Android"
    return None


# presets -------------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class Preset:
    id: str
    label: str
    description: str
    grep: str  # POSIX extended regex, run on the phone (toybox grep -E) and again here
    buffers: str = "main,system,crash"
    exclude: str | None = None  # Python regex: matching lines are dropped (noise)

    def to_dict(self) -> dict:
        return {"id": self.id, "label": self.label, "description": self.description}


PRESETS: dict[str, Preset] = {
    p.id: p
    for p in (
        Preset(
            "headphones",
            "Headphones & media buttons",
            "What the headphones sent (play, pause, toggle), which app passed it on, and when playback changed.",
            r"opcode=Opcode::PASS_THROUGH|Update PlaybackStatus|dispatchMediaKeyEvent|connection state changed|"
            r"Active Device Changed",
            exclude=r"action=ACTION_UP",
        ),
        Preset(
            "bluetooth",
            "Bluetooth connections",
            "Connects, disconnects and drops for headphones, speakers, watches and cars.",
            r"connection state changed|Active Device Changed|[Bb]ond[ _]?[Ss]tate|ACL (dis)?connected",
        ),
        Preset(
            "crashes",
            "App crashes & freezes",
            "Apps that crashed or stopped responding (\"isn't responding\").",
            r"FATAL EXCEPTION|AndroidRuntime|ANR in |Fatal signal|has died",
            exclude=r"has died: (cch|prev|svcb|home|lst|bfgs|bg)",  # Android freeing memory: routine, not a crash
        ),
        Preset(
            "audio",
            "Audio interruptions",
            "When notifications, calls, alarms or the Assistant take over the sound.",
            r"requestAudioFocus\(\) from|abandonAudioFocus\(\) from",
        ),
    )
}


@dataclass(slots=True)
class LogFilter:
    preset: str | None = None
    min_level: str = "V"
    query: str = ""
    app: str | None = None
    mask: bool = True

    def __post_init__(self) -> None:
        if self.preset == "":
            self.preset = None
        if self.preset is not None and self.preset not in PRESETS:
            raise ValueError(f"unknown preset {self.preset!r}")
        self.min_level = (self.min_level or "V").upper()
        if self.min_level not in LEVELS:
            raise ValueError(f"unknown level {self.min_level!r}; use one of {', '.join(LEVELS)}")
        self.query = (self.query or "").strip()
        if len(self.query) > MAX_QUERY:
            raise ValueError("search text is too long")
        if "\n" in self.query or "\r" in self.query:
            raise ValueError("search text can't contain line breaks")
        self.app = (self.app or "").strip() or None
        if self.app is not None and not PACKAGE_RE.match(self.app):
            raise ValueError(f"not a package name: {self.app!r}")

    @property
    def _preset(self) -> Preset | None:
        return PRESETS[self.preset] if self.preset else None

    def logcat_command(self, uid: int | None, *, dump: bool, since: str) -> str:
        """The logcat command line: format, buffers, app (uid), start time, then the level filterspec.

        `since` is a shell expression for logcat -T (epoch seconds), or "" for everything stored.
        """
        preset = self._preset
        args = ["-v", "threadtime", "-v", "epoch", "-b", preset.buffers if preset else "main,system,crash"]
        if uid is not None:
            args.append(f"--uid={uid}")
        parts = ["logcat", "-d" if dump else "", " ".join(shlex.quote(a) for a in args)]
        if since:
            parts.append(f'-T "{since}"')
        if self.min_level != "V":
            parts.append(shlex.quote(f"*:{self.min_level}"))
        return " ".join(p for p in parts if p)

    def phone_grep(self) -> str:
        """`| grep ...` stages that cut the dump down on the phone before it is sent over USB."""
        stages = []
        if preset := self._preset:
            stages.append(f"grep -E {shlex.quote(preset.grep)}")
        if self.query:
            stages.append(f"grep -iF -e {shlex.quote(self.query)}")
        return "".join(f" | {s}" for s in stages)

    def make_matcher(self) -> Callable[[LogLine], bool]:
        preset = self._preset
        include = re.compile(preset.grep) if preset else None
        exclude = re.compile(preset.exclude) if preset and preset.exclude else None
        query = self.query.lower()
        min_rank = LEVELS.index(self.min_level)

        def match(line: LogLine) -> bool:
            if LEVELS.index(line.level) < min_rank:
                return False
            if line.tag == "adbd" and "logcat" in line.message:
                return False  # adbd logging droidxfer's own logcat/grep command
            whole = f"{line.tag}: {line.message}"
            if include and not include.search(whole):
                return False
            if exclude and exclude.search(whole):
                return False
            return not query or query in whole.lower()

        return match

    def finish(self, line: LogLine) -> LogLine:
        if self.mask:
            line.message = mask_addresses(line.message)
        line.note = explain(line)
        return line


def presets() -> list[dict]:
    return [p.to_dict() for p in PRESETS.values()]


# reading from the phone ------------------------------------------------------------------
class LogReader:
    def __init__(self, remote: RemoteFS) -> None:
        self.remote = remote

    async def _uid(self, device_id: str, package: str) -> int:
        res = await self.remote.shell(device_id, f"pm list packages --user 0 -U {shlex.quote(package)}", timeout=20)
        for raw in res.text.splitlines():
            parts = raw.strip().split()
            if len(parts) == 2 and parts[0] == f"package:{package}" and parts[1].startswith("uid:"):
                return int(parts[1][4:])
        raise RemoteOpError(f"{package} is not installed on this phone")

    async def _uid_for(self, device_id: str, flt: LogFilter) -> int | None:
        return await self._uid(device_id, flt.app) if flt.app else None

    async def dump(
        self, device_id: str, flt: LogFilter, *, minutes: int = DEFAULT_WINDOW, limit: int = DEFAULT_LIMIT
    ) -> dict:
        """Lines already stored on the phone from the last `minutes` (0 = all), newest `limit` kept."""
        if minutes < 0 or minutes > 24 * 60:
            raise ValueError("minutes must be between 0 and 1440")
        limit = max(1, min(limit, MAX_LIMIT))
        uid = await self._uid_for(device_id, flt)
        since = f"$(($(date +%s)-{minutes * 60})).000" if minutes else ""
        command = f"echo @@now $(date +%s); {flt.logcat_command(uid, dump=True, since=since)}{flt.phone_grep()}"
        res = await self.remote.shell(device_id, command, timeout=90)
        text = res.text
        phone_now: float | None = None
        if text.startswith("@@now "):
            head, _, text = text.partition("\n")
            try:
                phone_now = float(head.split()[1])
            except (IndexError, ValueError):
                phone_now = None
        lines, matched = await asyncio.to_thread(_filter_text, text, flt, limit)
        return {
            "lines": [line.to_dict() for line in lines],
            "matched": matched,
            "truncated": matched > len(lines),
            "limit": limit,
            "minutes": minutes,
            "phone_time": phone_now,
            "filter": {"preset": flt.preset, "level": flt.min_level, "query": flt.query, "app": flt.app},
        }

    async def stream(self, device_id: str, flt: LogFilter) -> AsyncIterator[LogLine]:
        """New lines as the phone writes them, filtered and explained. Runs until the caller stops iterating."""
        uid = await self._uid_for(device_id, flt)
        t = self.remote.devices.transport(device_id)
        if t.kind == "mtp":
            raise RemoteOpError("reading logs needs USB debugging; the phone is in File transfer (MTP) mode")
        command = flt.logcat_command(uid, dump=False, since="$(date +%s).000")
        match = flt.make_matcher()
        pending = b""
        stream = self.remote.client.shell_stream(t.serial, command, v2="shell_v2" in t.features)
        try:
            async for chunk in stream:
                pending += chunk
                *complete, pending = pending.split(b"\n")
                for raw in complete:
                    line = parse_line(raw.decode("utf-8", "replace").rstrip("\r"))
                    if line is not None and match(line):
                        yield flt.finish(line)
        finally:
            await stream.aclose()

    async def clear(self, device_id: str) -> None:
        res = await self.remote.shell(device_id, "logcat -c", timeout=20)
        if not res.ok:
            raise RemoteOpError(res.stderr.decode("utf-8", "replace").strip() or "could not clear the log")


def _filter_text(text: str, flt: LogFilter, limit: int) -> tuple[list[LogLine], int]:
    match = flt.make_matcher()
    kept: list[LogLine] = []
    matched = 0
    for raw in text.splitlines():
        line = parse_line(raw)
        if line is None or not match(line):
            continue
        matched += 1
        kept.append(line)
        if len(kept) > limit * 2:  # keep memory bounded; only the newest `limit` survive
            del kept[: len(kept) - limit]
    kept = kept[-limit:]
    return [flt.finish(line) for line in kept], matched


def to_text(lines: list[LogLine], header: str = "") -> str:
    body = "\n".join(line.to_text() for line in lines)
    return f"{header}\n{body}\n" if header else body + "\n"
