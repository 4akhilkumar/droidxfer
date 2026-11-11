"""Finds phones in MTP ("File transfer") mode and manages one MTP session per phone.

Scanning only reads USB descriptors. A session (which claims the phone's MTP interface and so locks out
other apps such as Image Capture) is opened when droidxfer actually needs the phone, and released after
it has been idle for a while.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Callable

from ..mtp import Candidate, LinkError, MtpFS, MtpSession, scan_usb
from .config import ConfigStore
from .devices import DeviceManager, DeviceUnavailable, Transport

log = logging.getLogger(__name__)

SCAN_INTERVAL = 2.0
IDLE_CLOSE = 30.0

Scanner = Callable[[], list[Candidate]]


class MtpManager:
    def __init__(self, devices: DeviceManager, config: ConfigStore, scanner: Scanner | None = None) -> None:
        self.devices = devices
        self.config = config
        self.scanner: Scanner = scanner or (lambda: scan_usb())  # looked up at call time (tests patch it)
        self._candidates: dict[str, Candidate] = {}
        self._sessions: dict[str, MtpSession] = {}
        self._opening: dict[str, asyncio.Lock] = {}
        self._task: asyncio.Task | None = None

    @property
    def enabled(self) -> bool:
        return self.config.settings.mtp

    # lifecycle -----------------------------------------------------------------------
    async def start(self, *, poll: bool = True) -> None:
        await self.scan()
        if poll:
            self._task = asyncio.create_task(self._loop(), name="mtp-scanner")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
        for key in list(self._sessions):
            await self._close(key)

    async def _loop(self) -> None:
        while True:
            await asyncio.sleep(SCAN_INTERVAL)
            try:
                await self.scan()
                await self._close_idle()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # keep scanning across USB hiccups
                log.info("MTP scan failed: %s", exc)

    # discovery -----------------------------------------------------------------------
    async def scan(self) -> None:
        found = await asyncio.to_thread(self.scanner) if self.enabled else []
        current = {c.key: c for c in found}
        for key in [k for k in self._sessions if k not in current]:
            await self._close(key)  # unplugged
        self._candidates = current
        transports = []
        for c in current.values():
            state = {"mtp": "device", "ptp": "ptp"}.get(c.mode, "no_file_transfer")
            transports.append(Transport(
                serial=f"mtp:{c.key}", kind="mtp", state=state, model_hint=c.product,
                serialno=c.serial or None, manufacturer=c.manufacturer, model=c.product,
                resolved=state == "device",
            ))
        self.devices.set_mtp_transports(transports)

    # sessions ------------------------------------------------------------------------
    def _key(self, transport: Transport) -> str:
        return transport.serial.removeprefix("mtp:")

    async def fs(self, transport: Transport) -> MtpFS:
        return MtpFS(await self.session(transport))

    async def session(self, transport: Transport) -> MtpSession:
        key = self._key(transport)
        lock = self._opening.setdefault(key, asyncio.Lock())
        async with lock:
            existing = self._sessions.get(key)
            if existing is not None and not existing.broken:
                existing.last_used = time.monotonic()
                return existing
            if existing is not None:
                await self._close(key)
            candidate = self._candidates.get(key)
            if candidate is None:
                raise DeviceUnavailable("the phone is no longer connected over USB")
            try:
                link = await asyncio.to_thread(candidate.open)
                session = MtpSession(link, name=key)
                try:
                    await session.open()
                except BaseException:
                    await session.close()
                    raise
            except LinkError as exc:
                raise DeviceUnavailable(str(exc)) from exc
            self._sessions[key] = session
            return session

    async def _close(self, key: str) -> None:
        session = self._sessions.pop(key, None)
        if session is not None:
            await session.close()

    async def _close_idle(self) -> None:
        now = time.monotonic()
        for key, session in list(self._sessions.items()):
            if session.broken or (now - session.last_used > IDLE_CLOSE and not session._lock.locked()):
                await self._close(key)
