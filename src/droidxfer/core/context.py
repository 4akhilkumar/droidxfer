"""Wires the core services together for the web server and the CLI."""

from __future__ import annotations

import shutil
from pathlib import Path

from ..adb import AdbClient
from ..adb.protocol import DEFAULT_HOST, DEFAULT_PORT
from .config import ConfigStore, cache_dir
from .devices import DeviceManager
from .events import EventBus
from .localfs import LocalFS
from .remotefs import RemoteFS
from .thumbs import ThumbnailService
from .transfer import TransferEngine
from .wireless import WirelessManager


class AppContext:
    def __init__(
        self,
        *,
        adb_host: str = DEFAULT_HOST,
        adb_port: int = DEFAULT_PORT,
        local_root: Path | None = None,
        config_path: Path | None = None,
    ) -> None:
        self.config = ConfigStore(config_path)
        self.bus = EventBus()
        self.client = AdbClient(adb_host, adb_port)
        self.devices = DeviceManager(self.client, self.bus, self.config)
        self.local = LocalFS(local_root)
        self.remote = RemoteFS(self.client, self.devices)
        self.engine = TransferEngine(self.devices, self.remote, self.local, self.bus, self.config)
        self.wireless = WirelessManager(self.client, self.devices, self.config, self.bus)
        self.thumbs = ThumbnailService(self.local, self.remote, self.devices)
        self.uploads_dir = cache_dir() / "uploads"

    async def start(self, *, track: bool = True) -> None:
        shutil.rmtree(self.uploads_dir, ignore_errors=True)
        if track:
            await self.devices.start()
        else:
            await self.devices.ensure_adb()
            await self.devices.refresh()

    async def stop(self) -> None:
        await self.engine.shutdown()
        await self.wireless.stop()
        await self.devices.stop()
