"""Tracks adb transports and groups them into logical devices (same phone over USB + Wi-Fi)."""

from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass, field

from ..adb import AdbClient, DeviceEntry
from ..adb.locate import ensure_server, find_adb
from .config import ConfigStore
from .events import EventBus

log = logging.getLogger(__name__)

_MDNS_SERIAL = re.compile(r"^adb-(.+)-[A-Za-z0-9]{6}\._adb-tls-connect")
_PROPS = (
    "getprop ro.serialno; getprop ro.product.manufacturer; getprop ro.product.model; "
    "getprop ro.build.version.release; getprop ro.build.version.sdk"
)
_STATE_RANK = {"device": 0, "unauthorized": 1, "authorizing": 2, "connecting": 3, "offline": 4}
MAX_RESOLVE_ATTEMPTS = 5


class DeviceError(Exception):
    pass


class DeviceNotFound(DeviceError):
    pass


class DeviceUnavailable(DeviceError):
    pass


@dataclass
class Transport:
    serial: str
    kind: str  # "usb" | "wifi"
    state: str = "unknown"
    model_hint: str | None = None
    serialno: str | None = None
    manufacturer: str = ""
    model: str = ""
    android_version: str = ""
    sdk: str = ""
    features: frozenset[str] = field(default_factory=frozenset)
    resolved: bool = False
    attempts: int = 0

    @property
    def group_key(self) -> str:
        if self.serialno:
            return self.serialno
        if m := _MDNS_SERIAL.match(self.serial):
            return m.group(1)
        return self.serial

    def to_dict(self) -> dict:
        return {"serial": self.serial, "kind": self.kind, "state": self.state}


@dataclass
class Device:
    id: str
    transports: list[Transport]

    def active(self, prefer_usb: bool = True) -> Transport | None:
        ready = [t for t in self.transports if t.state == "device" and t.resolved]
        if not ready:
            return None
        ready.sort(key=lambda t: (t.kind != "usb") if prefer_usb else (t.kind == "usb"))
        return ready[0]

    def _info(self) -> Transport | None:
        return next((t for t in self.transports if t.resolved), None)

    def to_dict(self, prefer_usb: bool = True) -> dict:
        info = self._info()
        active = self.active(prefer_usb)
        hint = next((t.model_hint for t in self.transports if t.model_hint), None)
        model = info.model if info else (hint or "").replace("_", " ")
        manufacturer = info.manufacturer if info else ""
        state = "device" if active else min(
            (t.state for t in self.transports), key=lambda s: _STATE_RANK.get(s, 9)
        )
        return {
            "id": self.id,
            "name": f"{manufacturer} {model}".strip() or self.id,
            "model": model,
            "manufacturer": manufacturer,
            "android_version": info.android_version if info else "",
            "sdk": info.sdk if info else "",
            "state": state,
            "connection": active.kind if active else None,
            "active_serial": active.serial if active else None,
            "transports": [t.to_dict() for t in self.transports],
        }


class DeviceManager:
    def __init__(self, client: AdbClient, bus: EventBus, config: ConfigStore) -> None:
        self.client = client
        self.bus = bus
        self.config = config
        self.adb_error: str | None = None
        self._transports: dict[str, Transport] = {}
        self._lock = asyncio.Lock()
        self._task: asyncio.Task | None = None
        self._last_snapshot = ""
        self._background: set[asyncio.Task] = set()

    # lifecycle -----------------------------------------------------------------
    async def start(self) -> None:
        self._task = asyncio.create_task(self._track_loop(), name="device-tracker")

    async def stop(self) -> None:
        tasks = [t for t in (self._task, *self._background) if t]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    @property
    def tracking(self) -> bool:
        return self._task is not None and not self._task.done()

    async def ensure_adb(self) -> int:
        return await ensure_server(self.client, find_adb(self.config.settings.adb_path))

    async def refresh(self) -> None:
        await self._apply(await self.client.devices())

    async def _track_loop(self) -> None:
        while True:
            try:
                await self.ensure_adb()
                self.adb_error = None
                async for entries in self.client.track_devices():
                    await self._apply(entries)
                raise ConnectionError("adb server closed the device tracker")
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # keep tracking alive across adb restarts
                log.warning("device tracking error: %s", exc)
                self.adb_error = str(exc) or exc.__class__.__name__
                await self._apply([])
                await asyncio.sleep(2)

    # state ---------------------------------------------------------------------
    async def _apply(self, entries: list[DeviceEntry]) -> None:
        async with self._lock:
            current: dict[str, Transport] = {}
            for e in entries:
                kind = "wifi" if e.is_wireless else "usb"
                t = self._transports.get(e.serial)
                if t is None or t.kind != kind:
                    t = Transport(serial=e.serial, kind=kind)
                if e.state != "device":
                    t.resolved = False
                    t.attempts = 0
                t.state = e.state
                t.model_hint = e.model or t.model_hint
                current[e.serial] = t
            self._transports = current
            pending = [t for t in current.values() if t.state == "device" and not t.resolved]
            if pending:
                await asyncio.gather(*(self._resolve(t) for t in pending))
        self._publish()

    async def _resolve(self, t: Transport) -> None:
        t.attempts += 1
        try:
            t.features = frozenset(await self.client.features(t.serial))
            res = await self.client.shell(t.serial, _PROPS, v2="shell_v2" in t.features, timeout=10)
            lines = [line.strip() for line in res.text.splitlines()] + [""] * 5
            t.serialno = lines[0] or t.serial
            t.manufacturer, t.model, t.android_version, t.sdk = lines[1:5]
            t.resolved = True
        except Exception as exc:
            log.info("could not query %s (attempt %d): %s", t.serial, t.attempts, exc)
            if t.attempts < MAX_RESOLVE_ATTEMPTS:
                task = asyncio.create_task(self._retry_resolve(t.serial))
                self._background.add(task)
                task.add_done_callback(self._background.discard)
            return
        if t.kind == "wifi" and "_adb-tls" not in t.serial:
            host, _, port = t.serial.rpartition(":")
            if port.isdigit():
                self.config.remember_endpoint(host.strip("[]"), int(port), t.model, t.serialno or "")

    async def _retry_resolve(self, serial: str) -> None:
        await asyncio.sleep(3)
        async with self._lock:
            t = self._transports.get(serial)
            if t is None or t.resolved or t.state != "device":
                return
            await self._resolve(t)
        self._publish()

    def _publish(self) -> None:
        snapshot = self.snapshot()
        encoded = json.dumps(snapshot, sort_keys=True)
        if encoded != self._last_snapshot:
            self._last_snapshot = encoded
            self.bus.publish("devices", snapshot)

    # queries -------------------------------------------------------------------
    def devices(self) -> list[Device]:
        groups: dict[str, list[Transport]] = {}
        for t in self._transports.values():
            groups.setdefault(t.group_key, []).append(t)
        return [Device(key, ts) for key, ts in sorted(groups.items())]

    def snapshot(self) -> dict:
        prefer_usb = self.config.settings.prefer_usb
        return {
            "adb": {"ok": self.adb_error is None, "error": self.adb_error},
            "devices": [d.to_dict(prefer_usb) for d in self.devices()],
        }

    def get(self, device_id: str) -> Device:
        for d in self.devices():
            if d.id == device_id or any(t.serial == device_id for t in d.transports):
                return d
        raise DeviceNotFound(f"device {device_id!r} is not connected")

    def transport(self, device_id: str, kind: str | None = None) -> Transport:
        """Pick the transport to use now (USB preferred unless configured otherwise)."""
        device = self.get(device_id)
        if kind:
            t = next((t for t in device.transports if t.kind == kind and t.state == "device" and t.resolved), None)
        else:
            t = device.active(self.config.settings.prefer_usb)
        if t is None:
            states = ", ".join(f"{x.kind}:{x.state}" for x in device.transports)
            hint = " Unlock the phone and accept the USB debugging prompt." if "unauthorized" in states else ""
            raise DeviceUnavailable(f"device {device_id} is not ready ({states}).{hint}")
        return t

    async def wait_for(self, device_id: str, timeout: float) -> Transport:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while True:
            try:
                return self.transport(device_id)
            except DeviceError:
                if loop.time() >= deadline:
                    raise
            if not self.tracking:
                try:
                    await self.refresh()
                except Exception:
                    pass
            await asyncio.sleep(0.5)
