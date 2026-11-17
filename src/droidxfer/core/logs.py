"""The phone's system log (logcat) over adb: the "Phone log" tab of the Logs window.

Queried like droidxfer's own log (logviewer.py, ported from matrix_sols): time range, levels, log groups
(= app / process) and streams (= log tag), a filter pattern, a histogram and newest-first paging, plus
a live tail. Everything is read-only except `clear()`, which empties the phone's log (`logcat -c`).

- search():   what the phone has already stored (`logcat -d`). A busy phone logs ~30,000 lines a minute,
              so the time range, level, app, ready-made filter and plain search terms are applied on the
              phone first (logcat options + grep); the rest of the query runs here. The result is cached
              briefly so "Load more" and Download don't read the phone again.
- stream():   new lines as the phone writes them (live tail).

Ready-made filters (presets) target common problems (headphone buttons, Bluetooth, crashes, audio) and
lines they recognise get a short `note` in plain words, e.g. "Headphones (Bluetooth) sent Pause".
"""

from __future__ import annotations

import asyncio
import logging
import re
import secrets
import shlex
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import AsyncIterator, Callable, Iterator

from .apps import PACKAGE_RE
from .logviewer import (
    MAX_DOWNLOAD_ENTRIES, HistogramBuilder, LogQuery, LogQueryError, Params, fmt_time, group_tree, level_options,
)
from .remotefs import RemoteFS, RemoteOpError

log = logging.getLogger(__name__)

LEVELS = "VDIWEF"
LEVEL_NAMES = {"V": "Verbose", "D": "Debug", "I": "Info", "W": "Warning", "E": "Error", "F": "Fatal"}
SEVERITY = {"V": "debug", "D": "debug", "I": "info", "W": "warning", "E": "error", "F": "critical"}
RANGES = {
    "1m": timedelta(minutes=1),
    "5m": timedelta(minutes=5),
    "15m": timedelta(minutes=15),
    "30m": timedelta(minutes=30),
    "1h": timedelta(hours=1),
    "3h": timedelta(hours=3),
    "12h": timedelta(hours=12),
}
DEFAULT_RANGE = "15m"
MAX_KEEP = 20000        # matching lines kept per search (newest); the histogram counts all of them
CACHE_SIZE = 4          # recent search results kept for "Load more" / Download
CACHE_TTL = 300         # seconds
PS_REFRESH = 3.0        # live tail: re-read the process list at most this often for unknown pids

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

    def to_text(self, full_date: bool = False) -> str:
        fmt = "%Y-%m-%d %H:%M:%S" if full_date else "%m-%d %H:%M:%S"
        stamp = time.strftime(fmt, time.localtime(self.time)) + f".{int(self.time * 1000) % 1000:03d}"
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
    grep: str  # regular expression applied here (Python re)
    terms: tuple[str, ...] = ()  # fixed strings for the phone-side pre-filter (grep -F is ~30x faster than -E)
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
            terms=("opcode=Opcode::PASS_THROUGH", "Update PlaybackStatus", "dispatchMediaKeyEvent",
                   "connection state changed", "Active Device Changed"),
            exclude=r"action=ACTION_UP",
        ),
        Preset(
            "bluetooth",
            "Bluetooth connections",
            "Connects, disconnects and drops for headphones, speakers, watches and cars.",
            r"connection state changed|Active Device Changed|[Bb]ond[ _]?[Ss]tate|ACL (dis)?connected",
            terms=("connection state changed", "Active Device Changed", "ond state", "ond State", "ondState",
                   "ondstate", "ond_state", "ond_State", "ACL connected", "ACL disconnected"),
        ),
        Preset(
            "crashes",
            "App crashes & freezes",
            "Apps that crashed or stopped responding (\"isn't responding\").",
            r"FATAL EXCEPTION|AndroidRuntime|ANR in |Fatal signal|has died",
            terms=("FATAL EXCEPTION", "AndroidRuntime", "ANR in ", "Fatal signal", "has died"),
            exclude=r"has died: (cch|prev|svcb|home|lst|bfgs|bg)",  # Android freeing memory: routine, not a crash
        ),
        Preset(
            "audio",
            "Audio interruptions",
            "When notifications, calls, alarms or the Assistant take over the sound.",
            r"requestAudioFocus\(\) from|abandonAudioFocus\(\) from",
            terms=("requestAudioFocus() from", "abandonAudioFocus() from"),
        ),
    )
}


@dataclass(slots=True)
class PhoneOptions:
    """Phone-only filters on top of the shared query: ready-made filter, one app, address masking."""

    preset: str | None = None
    app: str | None = None
    mask: bool = True
    notes_only: bool = False

    def __post_init__(self) -> None:
        self.preset = self.preset or None
        if self.preset is not None and self.preset not in PRESETS:
            raise LogQueryError(f"unknown ready-made filter {self.preset!r}")
        self.app = (self.app or "").strip() or None
        if self.app is not None and not PACKAGE_RE.match(self.app):
            raise LogQueryError(f"not a package name: {self.app!r}")

    @property
    def preset_obj(self) -> Preset | None:
        return PRESETS[self.preset] if self.preset else None

    def logcat_command(self, uid: int | None, *, dump: bool, since: str, levels: list[str]) -> str:
        """logcat options (format, buffers, app, start time), then the level filterspec (must come last)."""
        preset = self.preset_obj
        args = ["-v", "threadtime", "-v", "epoch", "-b", preset.buffers if preset else "main,system,crash"]
        if uid is not None:
            args.append(f"--uid={uid}")
        parts = ["logcat", "-d" if dump else "", " ".join(shlex.quote(a) for a in args)]
        if since:
            parts.append(f'-T "{since}"')
        lowest = min((LEVELS.index(lv) for lv in levels), default=0)
        if lowest:
            parts.append(shlex.quote(f"*:{LEVELS[lowest]}"))
        return " ".join(p for p in parts if p)

    def phone_grep(self, terms: list[str], case_sensitive: bool) -> str:
        """`| grep ...` stages that cut a dump down on the phone before it is sent over USB."""
        stages = []
        if preset := self.preset_obj:
            stages.append("grep -F " + " ".join(f"-e {shlex.quote(t)}" for t in preset.terms))
        flag = "-F" if case_sensitive else "-iF"
        for term in terms[:3]:
            if "\n" not in term and "\r" not in term:
                stages.append(f"grep {flag} -e {shlex.quote(term)}")
        return "".join(f" | {s}" for s in stages)

    def make_matcher(self) -> Callable[[LogLine], bool]:
        preset = self.preset_obj
        include = re.compile(preset.grep) if preset else None
        exclude = re.compile(preset.exclude) if preset and preset.exclude else None

        notes_only = self.notes_only

        def match(line: LogLine) -> bool:
            if line.tag == "adbd" and "logcat" in line.message:
                return False  # adbd logging droidxfer's own logcat/grep command
            if notes_only and explain(line) is None:
                return False
            if include is None:
                return True
            whole = f"{line.tag}: {line.message}"
            return bool(include.search(whole)) and not (exclude and exclude.search(whole))

        return match

    def finish(self, line: LogLine) -> LogLine:
        if self.mask:
            line.message = mask_addresses(line.message)
        line.note = explain(line)
        return line


def presets() -> list[dict]:
    return [p.to_dict() for p in PRESETS.values()]


def query_from_params(params: Params, *, require_range: bool = True) -> LogQuery:
    return LogQuery.from_params(params, require_range, levels=LEVELS, ranges=RANGES, default_range=DEFAULT_RANGE)


def line_text(line: LogLine) -> str:
    """What the search pattern is matched against (the note is not part of the log)."""
    return f"{line.tag}: {line.message}"


def to_entry(line: LogLine, group: str, ident: int) -> dict:
    stamp = datetime.fromtimestamp(line.time)
    return {
        "id": ident,
        "timestamp": fmt_time(stamp),
        "level": line.level,
        "severity": SEVERITY.get(line.level, "info"),
        "group": group,
        "stream": line.tag,
        "source": line.tag,
        "source_title": f"{group} (pid {line.pid}, thread {line.tid})",
        "details": [["App / process", group], ["Tag", line.tag], ["Process ID", line.pid], ["Thread ID", line.tid]],
        "message": line.message,
        "extra": "",
        "truncated": False,
        "note": line.note,
        "raw": line.to_text(full_date=True),
    }


def parse_ps(text: str) -> dict[int, str]:
    names: dict[int, str] = {}
    for raw in text.splitlines():
        parts = raw.split(None, 1)
        if len(parts) == 2 and parts[0].isdigit():
            names[int(parts[0])] = parts[1].strip()
    return names


def split_sections(text: str) -> dict[str, str]:
    """Sections of one shell round trip, each introduced by an "@@name" line (log lines never start with @@)."""
    out: dict[str, list[str]] = {}
    current: list[str] | None = None
    for raw in text.split("\n"):
        if raw.startswith("@@"):
            name, _, rest = raw[2:].partition(" ")
            current = out.setdefault(name, [])
            if rest:
                current.append(rest)
        elif current is not None:
            current.append(raw)
    return {k: "\n".join(v) for k, v in out.items()}


@dataclass
class SearchResult:
    qid: str
    created: float
    lines: list[tuple[LogLine, str]]  # newest first
    matched: int
    histogram: dict
    groups: list[dict]
    echo: dict
    range: dict
    phone_time: float | None
    elapsed_ms: int
    filter: dict = field(default_factory=dict)

    def page(self, offset: int, size: int) -> dict:
        chunk = self.lines[offset: offset + size]
        end = offset + len(chunk)
        return {
            "qid": self.qid,
            "entries": [to_entry(line, group, offset + i) for i, (line, group) in enumerate(chunk)],
            "next_cursor": f"{self.qid}:{end}" if end < len(self.lines) else None,
            "partial": self.matched > len(self.lines) and end >= len(self.lines),
            "cursor_reset": False,
            "kept": len(self.lines),
            "matched": self.matched,
            "range": self.range,
            "phone_time": self.phone_time,
            "elapsed_ms": self.elapsed_ms,
            **self.echo,
        }


class LogReader:
    def __init__(self, remote: RemoteFS) -> None:
        self.remote = remote
        self._cache: OrderedDict[str, SearchResult] = OrderedDict()

    # helpers -----------------------------------------------------------------------------
    async def _uid(self, device_id: str, package: str) -> int:
        res = await self.remote.shell(device_id, f"pm list packages --user 0 -U {shlex.quote(package)}", timeout=20)
        for raw in res.text.splitlines():
            parts = raw.strip().split()
            if len(parts) == 2 and parts[0] == f"package:{package}" and parts[1].startswith("uid:"):
                return int(parts[1][4:])
        raise RemoteOpError(f"{package} is not installed on this phone")

    async def _uid_for(self, device_id: str, opts: PhoneOptions) -> int | None:
        return await self._uid(device_id, opts.app) if opts.app else None

    def _remember(self, result: SearchResult) -> None:
        now = time.monotonic()
        for key in [k for k, v in self._cache.items() if now - v.created > CACHE_TTL]:
            del self._cache[key]
        self._cache[result.qid] = result
        while len(self._cache) > CACHE_SIZE:
            self._cache.popitem(last=False)

    def cached(self, qid: str | None) -> SearchResult | None:
        result = self._cache.get(qid or "")
        if result and time.monotonic() - result.created <= CACHE_TTL:
            return result
        return None

    # what the phone has stored -----------------------------------------------------------
    async def run(self, device_id: str, params: Params, opts: PhoneOptions) -> SearchResult:
        """Read the stored log once, filter it and keep the newest MAX_KEEP matching lines."""
        query = query_from_params(params)
        started = time.monotonic()
        uid = await self._uid_for(device_id, opts)
        since = f"{query.start.timestamp():.3f}" if query.start else ""
        logcat = opts.logcat_command(uid, dump=True, since=since, levels=query.levels)
        grep = opts.phone_grep(query.pattern.required_terms, query.pattern.case_sensitive)
        command = f"echo @@now $(date +%s); echo @@ps; ps -A -o PID,NAME; echo @@log; {logcat}{grep}"
        res = await self.remote.shell(device_id, command, timeout=120)
        sections = split_sections(res.text)
        try:
            phone_now = float(sections.get("now", "").split()[0])
        except (IndexError, ValueError):
            phone_now = None
        names = parse_ps(sections.get("ps", ""))
        text = sections.get("log", "")
        result = await asyncio.to_thread(self._filter, text, names, query, opts)
        result.phone_time = phone_now
        result.elapsed_ms = int((time.monotonic() - started) * 1000)
        result.histogram["elapsed_ms"] = result.elapsed_ms
        result.filter = {"preset": opts.preset, "app": opts.app}
        self._remember(result)
        return result

    @staticmethod
    def _filter(text: str, names: dict[int, str], query: LogQuery, opts: PhoneOptions) -> SearchResult:
        preset_match = opts.make_matcher()
        end_before = query.end + timedelta(seconds=1) if query.end else None
        histogram = HistogramBuilder(LEVELS)
        streams: dict[tuple[str, str], dict] = {}
        kept: list[tuple[LogLine, str]] = []
        matched = 0
        for raw in text.splitlines():
            line = parse_line(raw)
            if line is None or not preset_match(line):
                continue
            stamp = datetime.fromtimestamp(line.time)
            # The end is inclusive to the whole second (custom ranges have no milliseconds).
            if (query.start and stamp < query.start) or (end_before and stamp >= end_before):
                continue
            if query.levels and line.level not in query.levels:
                continue
            group = names.get(line.pid) or f"pid {line.pid}"
            info = streams.setdefault((group, line.tag), {"count": 0, "last": stamp})
            info["count"] += 1
            info["last"] = max(info["last"], stamp)
            if not query.matches_fields(stamp, line.level, group, line.tag, lambda: line_text(line), check_time=False):
                continue
            matched += 1
            histogram.add(stamp, line.level)
            kept.append((line, group))
            if len(kept) > MAX_KEEP * 2:
                del kept[: len(kept) - MAX_KEEP]
        kept = kept[-MAX_KEEP:]
        kept.reverse()  # newest first, like the activity log
        for line, _ in kept:
            opts.finish(line)
        return SearchResult(
            qid=secrets.token_hex(6),
            created=time.monotonic(),
            lines=kept,
            matched=matched,
            histogram=histogram.build(query, partial=False, started=time.monotonic()),
            groups=group_tree(streams),
            echo=query.echo(),
            range=query.range_dict(),
            phone_time=None,
            elapsed_ms=0,
        )

    async def search(self, device_id: str, params: Params, opts: PhoneOptions) -> dict:
        """First page plus histogram and groups; with `cursor` ("qid:offset"), the next page of that search."""
        size = query_from_params(params).page_size
        cursor = params.get("cursor")
        if cursor:
            qid, _, raw_offset = cursor.partition(":")
            result = self.cached(qid)
            if result is not None and raw_offset.isdigit():
                return result.page(int(raw_offset), size)
            result = await self.run(device_id, params, opts)
            return result.page(0, size) | {"cursor_reset": True}
        result = await self.run(device_id, params, opts)
        return result.page(0, size) | {"histogram": result.histogram, "groups": result.groups}

    async def download(self, device_id: str, params: Params, opts: PhoneOptions) -> Iterator[str]:
        result = self.cached(params.get("qid")) or await self.run(device_id, params, opts)
        return (line.to_text(full_date=True) + "\n" for line, _ in result.lines[:MAX_DOWNLOAD_ENTRIES])

    # live --------------------------------------------------------------------------------
    async def stream(self, device_id: str, params: Params, opts: PhoneOptions) -> AsyncIterator[dict]:
        """New lines as the phone writes them, as entry dicts. Runs until the caller stops iterating."""
        query = query_from_params(params, require_range=False)
        t = self.remote.devices.transport(device_id)
        if t.kind == "mtp":
            raise RemoteOpError("reading logs needs USB debugging; the phone is in File transfer (MTP) mode")
        uid = await self._uid_for(device_id, opts)
        names = parse_ps((await self.remote.shell(device_id, "ps -A -o PID,NAME", timeout=20)).text)
        names_at = time.monotonic()
        command = opts.logcat_command(uid, dump=False, since="$(date +%s).000", levels=query.levels)
        preset_match = opts.make_matcher()
        pending = b""
        counter = 0
        stream = self.remote.client.shell_stream(t.serial, command, v2="shell_v2" in t.features)
        try:
            async for chunk in stream:
                pending += chunk
                *complete, pending = pending.split(b"\n")
                for raw in complete:
                    line = parse_line(raw.decode("utf-8", "replace").rstrip("\r"))
                    if line is None or not preset_match(line):
                        continue
                    if line.pid not in names and time.monotonic() - names_at > PS_REFRESH:
                        res = await self.remote.shell(device_id, "ps -A -o PID,NAME", timeout=20)
                        names, names_at = parse_ps(res.text), time.monotonic()
                    group = names.get(line.pid) or f"pid {line.pid}"
                    stamp = datetime.fromtimestamp(line.time)
                    if not query.matches_fields(stamp, line.level, group, line.tag, lambda: line_text(line),
                                                check_time=False):
                        continue
                    counter += 1
                    yield to_entry(opts.finish(line), group, counter)
        finally:
            await stream.aclose()

    async def clear(self, device_id: str) -> None:
        res = await self.remote.shell(device_id, "logcat -c", timeout=20)
        log.info("phone %s: cleared the phone's log (%s)", device_id, "ok" if res.ok else "failed")
        if not res.ok:
            raise RemoteOpError(res.stderr.decode("utf-8", "replace").strip() or "could not clear the log")


def meta() -> dict:
    return {
        "levels": level_options(LEVELS, LEVEL_NAMES, SEVERITY),
        "ranges": list(RANGES),
        "default_range": DEFAULT_RANGE,
        "presets": presets(),
    }
