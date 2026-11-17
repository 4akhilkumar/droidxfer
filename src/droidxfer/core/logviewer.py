"""CloudWatch-Logs-like querying, ported from matrix_sols (ms_apps/security/log_viewer.py).

Shared by both tabs of the Logs window:
* droidxfer activity - droidxfer's own log file (see applog.py), read here exactly like matrix_sols reads
                       its application log: backwards in chunks, bounded per request, byte-offset cursors,
                       rotation-aware live tail.
* Phone log          - the phone's logcat (see logs.py), which builds entries from a dump and reuses the
                       query, pattern and histogram code below.

Features: log groups / streams, filter patterns, time ranges, newest-first paging, a per-level histogram,
live tail and download.

Only droidxfer's own log file is ever opened; nothing from a request can choose another file.

Log line format (see applog.FORMAT):
    17-Nov-26 20:41:07 [ERROR] droidxfer/core/transfer.py:412:_run - message
Lines that do not start with a timestamp header (e.g. tracebacks) belong to the preceding entry.
"""

from __future__ import annotations

import os
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Callable, Iterable, Iterator, Mapping

from . import applog

# ----------------------------------------------------------------------
# Limits
# ----------------------------------------------------------------------
CHUNK_SIZE = 256 * 1024                 # Bytes read per backward step
MAX_SCAN_BYTES = 64 * 1024 * 1024       # Max bytes scanned per search / histogram request
MAX_TAIL_BYTES = 2 * 1024 * 1024        # Max bytes read per live-tail poll
MAX_ENTRY_CHARS = 20000                 # Entry text is truncated beyond this
MAX_PAGE_SIZE = 500
DEFAULT_PAGE_SIZE = 100
MAX_DOWNLOAD_ENTRIES = 10000
MAX_PATTERN_LENGTH = 500
TAIL_INITIAL_ENTRIES = 50
TIME_ORDER_TOLERANCE = timedelta(minutes=5)  # Several processes write the file; lines can be slightly out of order
META_CACHE_TTL = 30                     # Seconds

LEVELS = ["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]
LEVEL_SEVERITY = {"DEBUG": "debug", "INFO": "info", "WARNING": "warning", "ERROR": "error", "CRITICAL": "critical"}

RELATIVE_RANGES = {
    "5m": timedelta(minutes=5),
    "15m": timedelta(minutes=15),
    "30m": timedelta(minutes=30),
    "1h": timedelta(hours=1),
    "3h": timedelta(hours=3),
    "12h": timedelta(hours=12),
    "1d": timedelta(days=1),
    "3d": timedelta(days=3),
    "1w": timedelta(weeks=1),
}
DEFAULT_RANGE = "1h"

HEADER_RE = re.compile(
    rb"^(\d{2})-([A-Z][a-z]{2})-(\d{2}) (\d{2}):(\d{2}):(\d{2}) \[([A-Z]+)\] (.*?):(\d+):([^\s]*) - ?(.*)$"
)

MONTHS = {
    b"Jan": 1, b"Feb": 2, b"Mar": 3, b"Apr": 4, b"May": 5, b"Jun": 6,
    b"Jul": 7, b"Aug": 8, b"Sep": 9, b"Oct": 10, b"Nov": 11, b"Dec": 12,
}

HISTOGRAM_STEPS = [
    timedelta(seconds=1), timedelta(seconds=5),
    timedelta(seconds=10), timedelta(seconds=30), timedelta(minutes=1), timedelta(minutes=5),
    timedelta(minutes=10), timedelta(minutes=30), timedelta(hours=1), timedelta(hours=3),
    timedelta(hours=6), timedelta(hours=12), timedelta(days=1),
]
HISTOGRAM_TARGET_BUCKETS = 60

Params = Mapping[str, str]


class LogQueryError(ValueError):
    """Raised for invalid user supplied query parameters."""


# ----------------------------------------------------------------------
# Log source
# ----------------------------------------------------------------------
def get_log_file_path() -> str:
    return str(applog.log_path())


def get_log_file_info() -> dict | None:
    """Basic information about the log file (or ``None`` when it does not exist yet)."""
    path = get_log_file_path()
    try:
        stat = os.stat(path)
    except OSError:
        return None
    return {
        "name": os.path.basename(path),
        "path": applog.mask(path),
        "size": stat.st_size,
        "inode": str(stat.st_ino),
        "modified": datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M:%S"),
    }


def now_local() -> datetime:
    """Current local time without microseconds (matches the log timestamps)."""
    return datetime.now().replace(microsecond=0)


def fmt_time(value: datetime) -> str:
    if value.microsecond:
        return value.strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
    return value.strftime("%Y-%m-%d %H:%M:%S")


# ----------------------------------------------------------------------
# Entry parsing
# ----------------------------------------------------------------------
@dataclass
class LogEntry:
    """A single (possibly multi-line) entry of droidxfer's own log."""

    offset: int
    timestamp: datetime
    level: str
    path: str
    lineno: int
    func: str
    message: str
    extra_lines: list = field(default_factory=list)

    _text: str | None = None

    @property
    def group(self) -> str:
        return derive_group_and_stream(self.path)[0]

    @property
    def stream(self) -> str:
        return derive_group_and_stream(self.path)[1]

    @property
    def text(self) -> str:
        """Full raw text of the entry, used for pattern matching and download."""
        if self._text is None:
            header = (
                f"{self.timestamp.strftime('%d-%b-%y %H:%M:%S')} [{self.level}] "
                f"{self.path}:{self.lineno}:{self.func} - {self.message}"
            )
            self._text = "\n".join([header, *self.extra_lines]) if self.extra_lines else header
        return self._text

    def to_dict(self) -> dict:
        message = self.message
        extra = "\n".join(self.extra_lines)
        truncated = False
        if len(message) + len(extra) > MAX_ENTRY_CHARS:
            truncated = True
            message = message[:MAX_ENTRY_CHARS]
            extra = extra[: max(0, MAX_ENTRY_CHARS - len(message))]
        group, stream = derive_group_and_stream(self.path)
        return {
            "id": self.offset,
            "timestamp": fmt_time(self.timestamp),
            "level": self.level,
            "severity": LEVEL_SEVERITY.get(self.level, "info"),
            "group": group,
            "stream": stream,
            "source": f"{stream}:{self.lineno}",
            "source_title": f"{self.path}:{self.lineno} ({self.func})",
            "details": [["Group", group], ["Stream", stream], ["Line", self.lineno], ["Function", self.func]],
            "message": message,
            "extra": extra,
            "truncated": truncated,
            "note": None,
            "raw": self.text,
        }


_group_cache: dict[str, tuple[str, str]] = {}
_SUBPACKAGES = {"adb", "mtp", "api", "web"}


def derive_group_and_stream(path: str) -> tuple[str, str]:
    """
    Map a source path to a (log group, log stream) pair.

    droidxfer/core/transfer.py -> ('transfer', 'core/transfer.py')
    droidxfer/adb/client.py    -> ('adb', 'adb/client.py')
    droidxfer/cli.py           -> ('cli', 'cli.py')
    uvicorn/protocols/http.py  -> ('uvicorn', 'uvicorn/protocols/http.py')
    """
    cached = _group_cache.get(path)
    if cached:
        return cached
    normalized = path.replace("\\", "/")
    if normalized.startswith("droidxfer/"):
        stream = normalized[len("droidxfer/"):]
        parts = stream.split("/")
        if len(parts) == 1:
            group = parts[0].removesuffix(".py")
        elif parts[0] in _SUBPACKAGES:
            group = parts[0]
        else:  # core/<module>.py: one group per feature (transfer, devices, wireless, apps...)
            group = parts[-1].removesuffix(".py")
    else:
        stream = normalized.lstrip("./")
        group = stream.split("/", 1)[0] if "/" in stream else "other"
    if len(_group_cache) < 5000:
        _group_cache[path] = (group, stream)
    return group, stream


def _parse_header(line: bytes) -> LogEntry | None:
    match = HEADER_RE.match(line)
    if not match:
        return None
    day, mon, year, hour, minute, second, level, path, lineno, func, message = match.groups()
    month = MONTHS.get(mon)
    if not month:
        return None
    try:
        timestamp = datetime(2000 + int(year), month, int(day), int(hour), int(minute), int(second))
    except ValueError:
        return None
    return LogEntry(
        offset=0,
        timestamp=timestamp,
        level=level.decode("ascii", "replace"),
        path=path.decode("utf-8", "replace"),
        lineno=int(lineno),
        func=func.decode("utf-8", "replace"),
        message=message.decode("utf-8", "replace").rstrip("\r"),
    )


def _iter_lines_reverse(fh, end: int, start: int = 0) -> Iterator[tuple[int, bytes]]:
    """Yield ``(offset, line_bytes)`` from ``end`` backwards down to ``start``."""
    pos = end
    buffer = b""
    while pos > start:
        read_size = min(CHUNK_SIZE, pos - start)
        pos -= read_size
        fh.seek(pos)
        buffer = fh.read(read_size) + buffer
        parts = buffer.split(b"\n")
        offsets = []
        cursor = pos
        for part in parts:
            offsets.append(cursor)
            cursor += len(part) + 1
        for index in range(len(parts) - 1, 0, -1):
            yield offsets[index], parts[index]
        buffer = parts[0]
    if buffer or end > start:
        yield start, buffer


def iter_entries_reverse(fh, end: int, start: int = 0) -> Iterator[LogEntry]:
    """Complete entries from newest to oldest within [start, end)."""
    continuation: list[str] = []
    for offset, raw in _iter_lines_reverse(fh, end, start):
        if not raw:
            continue
        entry = _parse_header(raw)
        if entry is None:
            continuation.append(raw.decode("utf-8", "replace").rstrip("\r"))
            continue
        entry.offset = offset
        if continuation:
            continuation.reverse()
            entry.extra_lines = continuation
            continuation = []
        yield entry


def iter_entries_forward(data: bytes, base_offset: int) -> Iterator[LogEntry]:
    current = None
    offset = base_offset
    for raw in data.split(b"\n"):
        line_offset = offset
        offset += len(raw) + 1
        if not raw:
            continue
        entry = _parse_header(raw)
        if entry is None:
            if current is not None:
                current.extra_lines.append(raw.decode("utf-8", "replace").rstrip("\r"))
            continue
        if current is not None:
            yield current
        entry.offset = line_offset
        current = entry
    if current is not None:
        yield current


# ----------------------------------------------------------------------
# Filter pattern (CloudWatch-like)
# ----------------------------------------------------------------------
PATTERN_TOKEN_RE = re.compile(r'''([-?]?)(?:"((?:[^"\\]|\\.)*)"|/((?:[^/\\]|\\.)+)/|(\S+))''')


class FilterPattern:
    """
    Simple CloudWatch-style filter pattern.

        error timeout       -> both terms must appear (AND)
        "exact phrase"      -> phrase must appear
        -debug  -"a b"      -> term / phrase must NOT appear
        ?error ?warning     -> at least one of the ? terms must appear (OR)
        /order id: \\w+/    -> regular expression (may also be prefixed with - or ?)
    """

    def __init__(self, pattern: str | None, case_sensitive: bool = False) -> None:
        self.raw = (pattern or "").strip()
        if len(self.raw) > MAX_PATTERN_LENGTH:
            raise LogQueryError(f"Filter pattern is too long (max {MAX_PATTERN_LENGTH} characters).")
        self.case_sensitive = case_sensitive
        self.required: list[Callable[[str], bool]] = []
        self.excluded: list[Callable[[str], bool]] = []
        self.optional: list[Callable[[str], bool]] = []
        self.highlight: list[dict] = []
        self.required_terms: list[str] = []  # plain required terms, for pre-filtering on the phone

        flags = 0 if case_sensitive else re.IGNORECASE
        for match in PATTERN_TOKEN_RE.finditer(self.raw):
            prefix, phrase, regex, word = match.groups()
            if regex is not None:
                try:
                    compiled = re.compile(regex, flags)
                except re.error as exc:
                    raise LogQueryError(f"Invalid regular expression /{regex}/: {exc}") from exc
                matcher = compiled.search
                if prefix != "-":
                    self.highlight.append({"type": "regex", "value": regex})
            else:
                term = phrase.replace('\\"', '"') if phrase is not None else word
                if not term:
                    continue
                needle = term if case_sensitive else term.lower()
                matcher = self._make_contains(needle)
                if prefix != "-":
                    self.highlight.append({"type": "text", "value": term})
                if not prefix:
                    self.required_terms.append(term)

            if prefix == "-":
                self.excluded.append(matcher)
            elif prefix == "?":
                self.optional.append(matcher)
            else:
                self.required.append(matcher)

    @staticmethod
    def _make_contains(needle: str) -> Callable[[str], bool]:
        return lambda haystack: needle in haystack

    @property
    def is_empty(self) -> bool:
        return not (self.required or self.excluded or self.optional)

    def matches(self, text: str) -> bool:
        if self.is_empty:
            return True
        subject = text if self.case_sensitive else text.lower()
        if any(m(subject) for m in self.excluded):
            return False
        if not all(m(subject) for m in self.required):
            return False
        if self.optional and not any(m(subject) for m in self.optional):
            return False
        return True


# ----------------------------------------------------------------------
# Query
# ----------------------------------------------------------------------
def _parse_datetime(value: str | None, name: str) -> datetime | None:
    if not value:
        return None
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            return datetime.strptime(value, fmt)
        except ValueError:
            continue
    raise LogQueryError(f"Invalid {name} date/time.")


def _parse_list(value: str | None, allowed: Iterable[str] | None = None) -> list[str]:
    if not value:
        return []
    items = [item.strip() for item in value.split(",") if item.strip()]
    if allowed is not None:
        allowed = set(allowed)
        items = [item for item in items if item in allowed]
    return items


@dataclass
class LogQuery:
    """Validated query parameters."""

    start: datetime | None = None
    end: datetime | None = None
    levels: list = field(default_factory=list)
    groups: list = field(default_factory=list)
    streams: list = field(default_factory=list)
    pattern: FilterPattern = field(default_factory=lambda: FilterPattern(""))
    page_size: int = DEFAULT_PAGE_SIZE
    range_key: str = DEFAULT_RANGE

    @classmethod
    def from_params(
        cls,
        params: Params,
        require_range: bool = True,
        *,
        levels: Iterable[str] = LEVELS,
        ranges: Mapping[str, timedelta] = RELATIVE_RANGES,
        default_range: str = DEFAULT_RANGE,
    ) -> "LogQuery":
        range_key = params.get("range") or default_range
        end = None
        if range_key == "custom":
            start = _parse_datetime(params.get("start"), "start")
            end = _parse_datetime(params.get("end"), "end")
            if not start:
                raise LogQueryError("Start date/time is required for a custom range.")
            if end and end < start:
                raise LogQueryError("End date/time must be after the start date/time.")
        elif range_key == "all":
            start = None
        elif range_key in ranges:
            start = now_local() - ranges[range_key]
        elif require_range:
            raise LogQueryError("Invalid time range.")
        else:
            start = None

        try:
            page_size = int(params.get("limit") or DEFAULT_PAGE_SIZE)
        except ValueError as exc:
            raise LogQueryError("Invalid page size.") from exc
        page_size = max(1, min(page_size, MAX_PAGE_SIZE))

        return cls(
            start=start,
            end=end,
            levels=_parse_list(params.get("levels"), allowed=levels),
            groups=_parse_list(params.get("groups")),
            streams=_parse_list(params.get("streams")),
            pattern=FilterPattern(params.get("q", ""), params.get("case") == "1"),
            page_size=page_size,
            range_key=range_key,
        )

    def matches_fields(self, timestamp: datetime, level: str, group: str, stream: str, text: Callable[[], str],
                       check_time: bool = True) -> bool:
        if check_time:
            if self.start and timestamp < self.start:
                return False
            if self.end and timestamp > self.end:
                return False
        if self.levels and level not in self.levels:
            return False
        if self.groups and group not in self.groups:
            return False
        if self.streams and stream not in self.streams:
            return False
        return self.pattern.is_empty or self.pattern.matches(text())

    def matches(self, entry: LogEntry, check_time: bool = True) -> bool:
        group, stream = derive_group_and_stream(entry.path)
        return self.matches_fields(entry.timestamp, entry.level, group, stream, lambda: entry.text, check_time)

    def is_older_than_range(self, entry: LogEntry) -> bool:
        return bool(self.start) and entry.timestamp < self.start - TIME_ORDER_TOLERANCE

    def range_dict(self) -> dict:
        return {
            "start": fmt_time(self.start) if self.start else None,
            "end": fmt_time(self.end or now_local()),
        }

    def echo(self) -> dict:
        return {"highlight": self.pattern.highlight, "case_sensitive": self.pattern.case_sensitive}


# ----------------------------------------------------------------------
# Histogram (shared)
# ----------------------------------------------------------------------
def _histogram_step(start: datetime, end: datetime) -> timedelta:
    span = end - start
    for step in HISTOGRAM_STEPS:
        if span / step <= HISTOGRAM_TARGET_BUCKETS:
            return step
    return HISTOGRAM_STEPS[-1] * max(1, int(span / (HISTOGRAM_STEPS[-1] * HISTOGRAM_TARGET_BUCKETS)) + 1)


def _floor_time(value: datetime, step: timedelta) -> datetime:
    seconds = int(step.total_seconds())
    epoch = datetime(2000, 1, 1)
    elapsed = int((value - epoch).total_seconds())
    return epoch + timedelta(seconds=elapsed - (elapsed % seconds))


class HistogramBuilder:
    """Collects (timestamp, level) pairs and lays them out in ~60 time buckets."""

    def __init__(self, levels: Iterable[str]) -> None:
        self.levels = list(levels)
        self.counts: dict[datetime, dict[str, int]] = {}
        self.by_level = {level: 0 for level in self.levels}
        self.total = 0
        self.oldest: datetime | None = None
        self.newest: datetime | None = None

    def add(self, timestamp: datetime, level: str) -> None:
        self.total += 1
        self.by_level[level] = self.by_level.get(level, 0) + 1
        second = timestamp.replace(microsecond=0)
        bucket = self.counts.setdefault(second, {})
        bucket[level] = bucket.get(level, 0) + 1
        self.newest = second if self.newest is None or second > self.newest else self.newest
        self.oldest = second if self.oldest is None or second < self.oldest else self.oldest

    def build(self, query: LogQuery, *, partial: bool, started: float) -> dict:
        range_end = query.end or now_local()
        range_start = query.start or self.oldest or range_end - timedelta(hours=1)
        if self.newest and self.newest > range_end:
            range_end = self.newest
        if range_start >= range_end:
            range_start = range_end - timedelta(minutes=1)

        step = _histogram_step(range_start, range_end)
        buckets: dict[datetime, dict[str, int]] = {}
        cursor = _floor_time(range_start, step)
        while cursor <= range_end:
            buckets[cursor] = {}
            cursor += step
        for timestamp, level_counts in self.counts.items():
            target = buckets.setdefault(_floor_time(timestamp, step), {})
            for level, count in level_counts.items():
                target[level] = target.get(level, 0) + count

        return {
            "buckets": [{"start": fmt_time(key), "counts": value} for key, value in sorted(buckets.items())],
            "step_seconds": int(step.total_seconds()),
            "total": self.total,
            "by_level": self.by_level,
            "partial": partial,
            "elapsed_ms": int((time.monotonic() - started) * 1000),
            "range": query.range_dict(),
        }


# ----------------------------------------------------------------------
# droidxfer activity log: search / histogram / tail / download / meta
# ----------------------------------------------------------------------
def _parse_cursor(params: Params, file_info: dict) -> tuple[int, bool]:
    cursor = params.get("cursor")
    if not cursor:
        return file_info["size"], False
    try:
        inode, raw_offset = cursor.split(":", 1)
        offset = int(raw_offset)
    except ValueError as exc:
        raise LogQueryError("Invalid cursor.") from exc
    if inode != file_info["inode"] or offset < 0 or offset > file_info["size"]:
        return file_info["size"], True  # rotated / truncated since the first page
    return offset, False


def search(params: Params) -> dict:
    """One page of matching entries, newest first."""
    query = LogQuery.from_params(params)
    file_info = get_log_file_info()
    if not file_info:
        return {"entries": [], "file": None, "next_cursor": None, "partial": False, "scanned_bytes": 0,
                "cursor_reset": False, "range": query.range_dict(), **query.echo()}

    end_offset, cursor_reset = _parse_cursor(params, file_info)
    scan_floor = max(0, end_offset - MAX_SCAN_BYTES)
    entries: list[LogEntry] = []
    last_offset = end_offset
    reached_start_of_range = False
    started = time.monotonic()

    with open(get_log_file_path(), "rb") as fh:
        for entry in iter_entries_reverse(fh, end_offset, scan_floor):
            last_offset = entry.offset
            if query.is_older_than_range(entry):
                reached_start_of_range = True
                break
            if query.matches(entry):
                entries.append(entry)
                if len(entries) >= query.page_size:
                    break

    page_full = len(entries) >= query.page_size
    hit_scan_limit = not page_full and not reached_start_of_range and scan_floor > 0
    has_more = (page_full or hit_scan_limit) and last_offset > 0
    return {
        "entries": [entry.to_dict() for entry in entries],
        "next_cursor": f"{file_info['inode']}:{last_offset}" if has_more else None,
        "partial": hit_scan_limit,
        "cursor_reset": cursor_reset,
        "scanned_bytes": end_offset - last_offset,
        "elapsed_ms": int((time.monotonic() - started) * 1000),
        "range": query.range_dict(),
        "file": file_info,
        **query.echo(),
    }


def histogram(params: Params) -> dict:
    query = LogQuery.from_params(params)
    builder = HistogramBuilder(LEVELS)
    started = time.monotonic()
    file_info = get_log_file_info()
    if not file_info:
        return builder.build(query, partial=False, started=started) | {"file": None}
    end_offset = file_info["size"]
    scan_floor = max(0, end_offset - MAX_SCAN_BYTES)
    reached_start_of_range = False
    with open(get_log_file_path(), "rb") as fh:
        for entry in iter_entries_reverse(fh, end_offset, scan_floor):
            if query.is_older_than_range(entry):
                reached_start_of_range = True
                break
            if query.matches(entry):
                builder.add(entry.timestamp, entry.level)
    partial = not reached_start_of_range and scan_floor > 0
    return builder.build(query, partial=partial, started=started) | {"file": file_info}


def tail(params: Params) -> dict:
    """
    Live tail. Without ``offset``: the latest matching entries and the end-of-file offset. With ``offset``
    (and ``inode``): entries appended since then. Rotation / truncation restarts from the new file.
    """
    query = LogQuery.from_params(params, require_range=False)
    file_info = get_log_file_info()
    if not file_info:
        return {"entries": [], "offset": 0, "inode": None, "rotated": False, "skipped_bytes": 0, "file": None,
                **query.echo()}
    size = file_info["size"]
    path = get_log_file_path()

    raw_offset = params.get("offset")
    if raw_offset in (None, ""):
        entries: list[LogEntry] = []
        with open(path, "rb") as fh:
            for entry in iter_entries_reverse(fh, size, max(0, size - MAX_SCAN_BYTES)):
                if query.matches(entry, check_time=False):
                    entries.append(entry)
                    if len(entries) >= TAIL_INITIAL_ENTRIES:
                        break
        entries.reverse()
        return {"entries": [e.to_dict() for e in entries], "offset": size, "inode": file_info["inode"],
                "rotated": False, "skipped_bytes": 0, "file": file_info, **query.echo()}

    try:
        offset = int(raw_offset)
    except ValueError as exc:
        raise LogQueryError("Invalid offset.") from exc

    rotated = params.get("inode") != file_info["inode"] or offset > size or offset < 0
    if rotated:
        offset = 0
    skipped = 0
    if size - offset > MAX_TAIL_BYTES:
        skipped = size - offset - MAX_TAIL_BYTES
        offset = size - MAX_TAIL_BYTES

    data = b""
    if size > offset:
        with open(path, "rb") as fh:
            fh.seek(offset)
            data = fh.read(size - offset)

    # Only consume up to the last complete line; the rest is picked up on the next poll.
    last_newline = data.rfind(b"\n")
    consumed = b"" if last_newline == -1 else data[: last_newline + 1]
    if skipped:
        first_newline = consumed.find(b"\n")
        consumed_start = first_newline + 1 if first_newline != -1 else len(consumed)
        consumed = consumed[consumed_start:]
        base = offset + consumed_start
    else:
        base = offset

    entries = [e for e in iter_entries_forward(consumed, base) if query.matches(e, check_time=False)]
    return {
        "entries": [e.to_dict() for e in entries],
        "offset": base + len(consumed),
        "inode": file_info["inode"],
        "rotated": rotated,
        "skipped_bytes": skipped,
        "file": file_info,
        **query.echo(),
    }


def iter_download(params: Params) -> Iterator[str]:
    """Raw text of matching entries (newest first), at most MAX_DOWNLOAD_ENTRIES."""
    query = LogQuery.from_params(params)
    file_info = get_log_file_info()
    if not file_info:
        return
    size = file_info["size"]
    count = 0
    with open(get_log_file_path(), "rb") as fh:
        for entry in iter_entries_reverse(fh, size, max(0, size - MAX_SCAN_BYTES)):
            if query.is_older_than_range(entry):
                break
            if query.matches(entry):
                yield entry.text + "\n"
                count += 1
                if count >= MAX_DOWNLOAD_ENTRIES:
                    break


_meta_cache: dict = {}


def level_options(levels: Iterable[str], labels: Mapping[str, str] | None = None,
                  severity: Mapping[str, str] | None = None) -> list[dict]:
    labels = labels or {}
    severity = severity or LEVEL_SEVERITY
    return [{"id": lv, "label": labels.get(lv, lv), "severity": severity.get(lv, "info")} for lv in levels]


def group_tree(streams: Mapping[tuple[str, str], dict]) -> list[dict]:
    """[{name, count, streams: [{name, count, last}]}] sorted by name."""
    groups: dict[str, dict] = {}
    for (group, stream), info in streams.items():
        group_info = groups.setdefault(group, {"name": group, "count": 0, "streams": []})
        group_info["count"] += info["count"]
        group_info["streams"].append({"name": stream, "count": info["count"], "last": fmt_time(info["last"])})
    for group_info in groups.values():
        group_info["streams"].sort(key=lambda item: item["name"])
    return sorted(groups.values(), key=lambda item: item["name"].lower())


def meta() -> dict:
    """Log groups / streams in the file (cached briefly), plus file info."""
    base = {
        "levels": level_options(LEVELS),
        "ranges": list(RELATIVE_RANGES),
        "default_range": DEFAULT_RANGE,
        "log_level": None,
    }
    file_info = get_log_file_info()
    if not file_info:
        return base | {"file": None, "groups": [], "oldest": None, "newest": None, "partial": False}

    cached = _meta_cache.get("value")
    if cached and cached["inode"] == file_info["inode"] and cached["size"] == file_info["size"] \
            and time.monotonic() - cached["at"] < META_CACHE_TTL:
        return base | cached["data"] | {"file": file_info}

    size = file_info["size"]
    scan_floor = max(0, size - MAX_SCAN_BYTES)
    streams: dict[tuple[str, str], dict] = {}
    oldest = newest = None
    with open(get_log_file_path(), "rb") as fh:
        for entry in iter_entries_reverse(fh, size, scan_floor):
            key = derive_group_and_stream(entry.path)
            info = streams.setdefault(key, {"count": 0, "last": entry.timestamp})
            info["count"] += 1
            newest = newest or entry.timestamp
            oldest = entry.timestamp

    data = {
        "groups": group_tree(streams),
        "oldest": fmt_time(oldest) if oldest else None,
        "newest": fmt_time(newest) if newest else None,
        "partial": scan_floor > 0,
    }
    _meta_cache["value"] = {"inode": file_info["inode"], "size": size, "at": time.monotonic(), "data": data}
    return base | data | {"file": file_info}
