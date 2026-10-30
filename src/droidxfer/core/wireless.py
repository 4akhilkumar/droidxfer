"""Wi-Fi connectivity: wireless-debugging pairing (code or QR), connect/disconnect, tcpip from USB."""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import re
import secrets
import string
from dataclasses import dataclass

import segno

from ..adb import AdbClient, AdbError
from .config import ConfigStore
from .devices import DeviceManager
from .events import EventBus

log = logging.getLogger(__name__)

_HOSTNAME = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9.-]{0,251}[A-Za-z0-9])?$")
_CODE = re.compile(r"^[A-Za-z0-9]{4,32}$")
_GUID = re.compile(r"guid=([^\]\s]+)")
_WLAN_IP = re.compile(r"inet (\d{1,3}(?:\.\d{1,3}){3})/")
QR_TIMEOUT = 180


class WirelessError(Exception):
    pass


def validate_host(host: str) -> str:
    host = (host or "").strip().strip("[]")
    try:
        return str(ipaddress.ip_address(host))
    except ValueError:
        if _HOSTNAME.match(host):
            return host
    raise WirelessError(f"invalid host: {host!r}")


def validate_port(port: int) -> int:
    if not isinstance(port, int) or not 1 <= port <= 65535:
        raise WirelessError(f"invalid port: {port!r}")
    return port


@dataclass
class QrSession:
    name: str
    password: str
    state: str = "waiting"
    message: str = "Waiting for the phone to scan the QR code"
    task: asyncio.Task | None = None

    def to_dict(self) -> dict:
        return {"name": self.name, "state": self.state, "message": self.message}


class WirelessManager:
    def __init__(self, client: AdbClient, devices: DeviceManager, config: ConfigStore, bus: EventBus) -> None:
        self.client = client
        self.devices = devices
        self.config = config
        self.bus = bus
        self._qr: dict[str, QrSession] = {}

    async def pair(self, host: str, port: int, code: str) -> str:
        host, port = validate_host(host), validate_port(port)
        if not _CODE.match(code or ""):
            raise WirelessError("pairing code must be 4-32 letters/digits")
        msg = (await self.client.pair(host, port, code)).strip()
        if not msg.lower().startswith("successfully paired"):
            raise WirelessError(msg or "pairing failed")
        return msg

    async def connect(self, host: str, port: int) -> str:
        host, port = validate_host(host), validate_port(port)
        msg = (await self.client.connect(host, port)).strip()
        if not (msg.startswith("connected to") or msg.startswith("already connected")):
            raise WirelessError(msg or "connect failed")
        self.config.remember_endpoint(host, port)
        return msg

    async def disconnect(self, serial: str) -> str:
        return (await self.client.disconnect(serial)).strip()

    async def discover(self) -> list[dict]:
        services = await self.client.mdns_services()
        return [
            {
                "name": s.name,
                "type": "pairing" if s.is_pairing else "connect" if s.is_connect else s.service_type,
                "host": s.host,
                "port": s.port,
            }
            for s in services
        ]

    def known(self) -> list[dict]:
        return [
            {"host": e.host, "port": e.port, "name": e.name, "serialno": e.serialno, "last_seen": e.last_seen}
            for e in self.config.settings.known_endpoints
        ]

    def forget(self, host: str, port: int) -> None:
        self.config.forget_endpoint(host, port)

    async def enable_tcpip(self, device_id: str, port: int = 5555) -> str:
        """Switch a USB-connected phone to classic adb-over-TCP and connect to it."""
        port = validate_port(port)
        t = self.devices.transport(device_id, kind="usb")
        res = await self.client.shell(t.serial, "ip -f inet addr show wlan0", v2="shell_v2" in t.features)
        match = _WLAN_IP.search(res.text)
        if not match:
            raise WirelessError("the phone does not seem to be connected to Wi-Fi (no wlan0 address)")
        ip = match.group(1)
        await self.client.tcpip(t.serial, port)
        last_error = ""
        for _ in range(10):
            await asyncio.sleep(1.5)
            try:
                return await self.connect(ip, port)
            except (WirelessError, AdbError, asyncio.TimeoutError) as exc:
                last_error = str(exc)
        raise WirelessError(f"phone switched to TCP mode but connecting to {ip}:{port} failed: {last_error}")

    # QR pairing ----------------------------------------------------------------
    def start_qr(self) -> dict:
        name = "droidxfer-" + secrets.token_hex(3)
        alphabet = string.ascii_letters + string.digits
        password = "".join(secrets.choice(alphabet) for _ in range(12))
        payload = f"WIFI:T:ADB;S:{name};P:{password};;"
        svg = segno.make(payload, error="m").svg_inline(scale=6, border=2, dark="#000000", light="#ffffff")
        session = QrSession(name, password)
        session.task = asyncio.create_task(self._run_qr(session), name=f"qr-{name}")
        self._qr[name] = session
        return {**session.to_dict(), "svg": svg, "payload": payload, "timeout": QR_TIMEOUT}

    def qr_status(self, name: str) -> dict:
        session = self._qr.get(name)
        if session is None:
            raise KeyError(name)
        return session.to_dict()

    def cancel_qr(self, name: str) -> None:
        session = self._qr.pop(name, None)
        if session and session.task:
            session.task.cancel()

    async def stop(self) -> None:
        for name in list(self._qr):
            self.cancel_qr(name)

    def _update(self, s: QrSession, state: str, message: str) -> None:
        s.state, s.message = state, message
        self.bus.publish("qr_status", s.to_dict())

    async def _services(self) -> list:
        try:
            return await self.client.mdns_services()
        except (AdbError, asyncio.TimeoutError) as exc:
            log.debug("mdns query failed: %s", exc)
            return []

    async def _run_qr(self, s: QrSession) -> None:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + QR_TIMEOUT
        try:
            while loop.time() < deadline:
                pairing = next((x for x in await self._services() if x.is_pairing and x.name == s.name), None)
                if pairing is None:
                    await asyncio.sleep(1)
                    continue
                self._update(s, "pairing", f"Pairing with {pairing.host}:{pairing.port}...")
                msg = await self.pair(pairing.host, pairing.port, s.password)
                self._update(s, "paired", msg)
                guid = m.group(1) if (m := _GUID.search(msg)) else None
                for _ in range(30):
                    conn = next(
                        (
                            x for x in await self._services()
                            if x.is_connect and (x.name == guid or x.host == pairing.host)
                        ),
                        None,
                    )
                    if conn:
                        self._update(s, "connected", await self.connect(conn.host, conn.port))
                        return
                    await asyncio.sleep(1)
                self._update(s, "paired", "Paired. The phone will connect when Wireless debugging is on.")
                return
            self._update(s, "expired", "QR code expired. Generate a new one.")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._update(s, "failed", str(exc) or exc.__class__.__name__)
