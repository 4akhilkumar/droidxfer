"""droidxfer's own activity log: one text file, same line format as matrix_sols.

    17-Nov-26 20:41:07 [INFO] droidxfer/core/transfer.py:402:_run - pull finished: 12 files, 48.2 MB in 3.1 s
    17-Nov-26 20:41:09 [ERROR] droidxfer/api/app.py:57:handle - GET /api/devices/X/ls failed: ...
    Traceback (most recent call last):      <- extra lines belong to the entry above
      ...

The web server and every CLI command append to the same file, possibly at the same time, so each record
is written with an exclusive lock (fcntl.flock) and the size-based rotation happens under that lock too
(the matrix_sols notes on in-process rotation racing between processes apply here as well).

Secrets never reach the file: the web session token, Wi-Fi pairing codes and QR passwords are registered
with `register_secret()` and replaced by ******, `token=...` query values are masked, and the home folder
is shortened to `~`.
"""

from __future__ import annotations

import fcntl
import logging
import os
import re
import threading
from pathlib import Path

from .config import app_dir

LOG_NAME = "droidxfer.log"
MAX_BYTES = 5 * 1024 * 1024
BACKUPS = 3
LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")
DEFAULT_LEVEL = "INFO"
FORMAT = "{asctime} [{levelname}] {pathname}:{lineno}:{funcName} - {message}"
DATEFMT = "%d-%b-%y %H:%M:%S"

# Third-party loggers that are noisy at INFO; they still reach the file at WARNING and above.
QUIET_LOGGERS = ("uvicorn.access", "asyncio", "PIL", "multipart", "python_multipart", "httpx", "usb")

_PACKAGE_ROOT = str(Path(__file__).resolve().parent.parent.parent)  # folder that contains droidxfer/
_TOKEN_RE = re.compile(r"(token=)[^&\s\"']+")
_secrets: set[str] = set()
_secrets_lock = threading.Lock()


def log_dir() -> Path:
    if os.environ.get("DROIDXFER_HOME"):
        return app_dir() / "logs"
    return Path.home() / "Library" / "Logs" / "droidxfer"


def log_path() -> Path:
    return log_dir() / LOG_NAME


def register_secret(value: str | None) -> None:
    """Never write `value` to the log (e.g. a session token or pairing code)."""
    if value and len(value) >= 4:
        with _secrets_lock:
            _secrets.add(value)


def mask(text: str) -> str:
    with _secrets_lock:
        values = sorted(_secrets, key=len, reverse=True)
    for value in values:
        if value in text:
            text = text.replace(value, "******")
    text = _TOKEN_RE.sub(r"\1******", text)
    home = str(Path.home())
    if len(home) > 1:
        text = text.replace(home, "~")
    return text


def relative_source(pathname: str) -> str:
    """droidxfer/core/transfer.py for our files, uvicorn/server.py for libraries, else the file name."""
    normalized = pathname.replace("\\", "/")
    root = _PACKAGE_ROOT.replace("\\", "/") + "/"
    if normalized.startswith(root):
        return normalized[len(root):]
    if "site-packages/" in normalized:
        return normalized.rsplit("site-packages/", 1)[1]
    if "/lib/python" in normalized:  # standard library
        return "python/" + normalized.rsplit("/", 1)[-1]
    return normalized.rsplit("/", 1)[-1]


class MaskingFormatter(logging.Formatter):
    """matrix_sols' `custom_format` with relative source paths; masks secrets in the whole entry,
    including tracebacks (a filter on record.msg alone would miss %-args and exception text)."""

    def __init__(self) -> None:
        super().__init__(FORMAT, datefmt=DATEFMT, style="{")

    def format(self, record: logging.LogRecord) -> str:
        original = record.pathname
        record.pathname = relative_source(original)
        try:
            text = super().format(record)
        finally:
            record.pathname = original
        return mask(text)


class LockedRotatingFileHandler(logging.Handler):
    """Append-only file handler that is safe with several droidxfer processes writing at once."""

    def __init__(self, path: Path, max_bytes: int = MAX_BYTES, backups: int = BACKUPS) -> None:
        super().__init__()
        self.path = path
        self.lock_path = path.with_name(path.name + ".lock")
        self.max_bytes = max_bytes
        self.backups = backups

    def _rotate(self) -> None:
        for i in range(self.backups, 0, -1):
            src = self.path if i == 1 else self.path.with_name(f"{self.path.name}.{i - 1}")
            if src.exists():
                os.replace(src, self.path.with_name(f"{self.path.name}.{i}"))

    def emit(self, record: logging.LogRecord) -> None:
        try:
            data = (self.format(record) + "\n").encode("utf-8", "replace")
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.lock_path, "a") as lock:
                fcntl.flock(lock, fcntl.LOCK_EX)
                try:
                    try:
                        size = self.path.stat().st_size
                    except FileNotFoundError:
                        size = 0
                    if size and size + len(data) > self.max_bytes:
                        self._rotate()
                    fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
                    try:
                        os.write(fd, data)
                    finally:
                        os.close(fd)
                finally:
                    fcntl.flock(lock, fcntl.LOCK_UN)
        except Exception:
            self.handleError(record)


_handler: LockedRotatingFileHandler | None = None


def normalize_level(level: str | None) -> str:
    level = (level or DEFAULT_LEVEL).upper()
    return level if level in LEVELS else DEFAULT_LEVEL


def setup(level: str | None = None, *, path: Path | None = None) -> LockedRotatingFileHandler:
    """Start writing droidxfer's log file (idempotent; a second call only changes the level/path)."""
    global _handler
    root = logging.getLogger()
    target = path or log_path()
    if _handler is not None and _handler.path != target:
        root.removeHandler(_handler)
        _handler = None
    if _handler is None:
        _handler = LockedRotatingFileHandler(target)
        _handler.setFormatter(MaskingFormatter())
        root.addHandler(_handler)
        for name in QUIET_LOGGERS:
            logging.getLogger(name).setLevel(logging.WARNING)
    set_level(level)
    return _handler


def set_level(level: str | None) -> None:
    if _handler is None:
        return
    value = getattr(logging, normalize_level(level))
    _handler.setLevel(value)
    root = logging.getLogger()
    others = [h.level for h in root.handlers if h is not _handler and h.level]
    root.setLevel(min([value, *others]) if others else value)


def teardown() -> None:
    """Stop writing the file (tests)."""
    global _handler
    if _handler is not None:
        logging.getLogger().removeHandler(_handler)
        _handler = None
