"""Persistent settings stored as JSON in ~/Library/Application Support/droidxfer."""

from __future__ import annotations

import json
import os
import tempfile
import time
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path

CONFLICT_POLICIES = ("rename", "overwrite", "skip")


def app_dir() -> Path:
    if override := os.environ.get("DROIDXFER_HOME"):
        return Path(override)
    return Path.home() / "Library" / "Application Support" / "droidxfer"


def cache_dir() -> Path:
    if override := os.environ.get("DROIDXFER_HOME"):
        return Path(override) / "cache"
    return Path.home() / "Library" / "Caches" / "droidxfer"


@dataclass
class KnownEndpoint:
    host: str
    port: int
    name: str = ""
    serialno: str = ""
    last_seen: float = 0.0


@dataclass
class Settings:
    adb_path: str | None = None
    download_dir: str = "~/Downloads"
    default_conflict: str = "rename"
    verify_checksum: bool = False
    media_scan: bool = True
    show_hidden: bool = False
    prefer_usb: bool = True
    ask_before_transfer: bool = True
    known_endpoints: list[KnownEndpoint] = field(default_factory=list)

    def public(self) -> dict:
        data = asdict(self)
        data.pop("known_endpoints")
        return data


EDITABLE_FIELDS = {
    "adb_path",
    "download_dir",
    "default_conflict",
    "verify_checksum",
    "media_scan",
    "show_hidden",
    "prefer_usb",
    "ask_before_transfer",
}


class ConfigStore:
    def __init__(self, path: Path | None = None) -> None:
        self.path = path or app_dir() / "config.json"
        self.settings = self._load()

    def _load(self) -> Settings:
        try:
            raw = json.loads(self.path.read_text())
        except (OSError, ValueError):
            return Settings()
        known = [KnownEndpoint(**k) for k in raw.pop("known_endpoints", []) if isinstance(k, dict)]
        allowed = {f.name for f in fields(Settings)}
        settings = Settings(**{k: v for k, v in raw.items() if k in allowed})
        settings.known_endpoints = known
        if settings.default_conflict not in CONFLICT_POLICIES:
            settings.default_conflict = "rename"
        return settings

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=".config-")
        with os.fdopen(fd, "w") as fh:
            json.dump(asdict(self.settings), fh, indent=2)
        os.replace(tmp, self.path)

    def update(self, changes: dict) -> Settings:
        for key, value in changes.items():
            if key not in EDITABLE_FIELDS:
                raise ValueError(f"unknown setting: {key}")
            if key == "default_conflict" and value not in CONFLICT_POLICIES:
                raise ValueError(f"default_conflict must be one of {CONFLICT_POLICIES}")
            setattr(self.settings, key, value)
        self.save()
        return self.settings

    def remember_endpoint(self, host: str, port: int, name: str = "", serialno: str = "") -> None:
        known = self.settings.known_endpoints
        for ep in known:
            if ep.host == host and ep.port == port:
                ep.name = name or ep.name
                ep.serialno = serialno or ep.serialno
                ep.last_seen = time.time()
                break
        else:
            known.append(KnownEndpoint(host, port, name, serialno, time.time()))
        known.sort(key=lambda e: e.last_seen, reverse=True)
        del known[20:]
        self.save()

    def forget_endpoint(self, host: str, port: int) -> None:
        self.settings.known_endpoints = [
            e for e in self.settings.known_endpoints if not (e.host == host and e.port == port)
        ]
        self.save()
