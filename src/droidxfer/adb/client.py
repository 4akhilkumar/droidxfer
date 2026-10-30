"""Async client for the local adb server (host services, transports, shell v2, sync)."""

from __future__ import annotations

import asyncio
import re
import struct
from dataclasses import dataclass
from typing import AsyncIterator

from .protocol import DEFAULT_HOST, DEFAULT_PORT, AdbCommandError, AdbError, AdbStream
from .sync import SyncConnection

_KV = re.compile(r"\s(usb|product|model|device|transport_id):(\S+)")
_SHELL_HDR = struct.Struct("<BI")
_SHELL_STDOUT, _SHELL_STDERR, _SHELL_EXIT, _SHELL_CLOSE_STDIN = 1, 2, 3, 4


def is_wireless_serial(serial: str) -> bool:
    return "_adb-tls-connect" in serial or ":" in serial


@dataclass(slots=True)
class DeviceEntry:
    serial: str
    state: str
    usb: str | None = None
    product: str | None = None
    model: str | None = None
    device: str | None = None
    transport_id: str | None = None

    @property
    def is_wireless(self) -> bool:
        return is_wireless_serial(self.serial)


@dataclass(slots=True)
class MdnsService:
    name: str
    service_type: str
    host: str
    port: int

    @property
    def is_pairing(self) -> bool:
        return "adb-tls-pairing" in self.service_type

    @property
    def is_connect(self) -> bool:
        return "adb-tls-connect" in self.service_type or self.service_type.startswith("_adb._tcp")


@dataclass(slots=True)
class ShellResult:
    stdout: bytes
    stderr: bytes
    exit_code: int | None

    @property
    def text(self) -> str:
        return self.stdout.decode("utf-8", "replace")

    @property
    def ok(self) -> bool:
        return self.exit_code in (0, None)


def parse_device_list(text: str) -> list[DeviceEntry]:
    entries: list[DeviceEntry] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("*") or line.startswith("List of"):
            continue
        parts = line.split(None, 1)
        rest = " " + parts[1] if len(parts) > 1 else ""
        match = _KV.search(rest)
        state = (rest[: match.start()] if match else rest).strip() or "unknown"
        kv = dict(_KV.findall(rest))
        entries.append(DeviceEntry(serial=parts[0], state=state, **kv))
    return entries


def parse_mdns_services(text: str) -> list[MdnsService]:
    services: list[MdnsService] = []
    for raw in text.splitlines():
        parts = raw.strip().split()
        if len(parts) < 3 or ":" not in parts[-1]:
            continue
        host, _, port = parts[-1].rpartition(":")
        if not port.isdigit():
            continue
        services.append(
            MdnsService(name=parts[0], service_type=parts[1], host=host.strip("[]"), port=int(port))
        )
    return services


def format_hostport(host: str, port: int) -> str:
    return f"[{host}]:{port}" if ":" in host else f"{host}:{port}"


class AdbClient:
    def __init__(self, host: str = DEFAULT_HOST, port: int = DEFAULT_PORT) -> None:
        self.host = host
        self.port = port

    async def _open(self) -> AdbStream:
        return await AdbStream.open(self.host, self.port)

    async def query(self, service: str, timeout: float | None = 15.0) -> str:
        """Run a host service that answers OKAY + length-prefixed string."""

        async def run() -> str:
            async with await self._open() as s:
                await s.send_request(service)
                await s.read_status()
                return await s.read_length_prefixed()

        return await asyncio.wait_for(run(), timeout)

    async def version(self) -> int:
        return int(await self.query("host:version", timeout=5.0), 16)

    async def devices(self) -> list[DeviceEntry]:
        return parse_device_list(await self.query("host:devices-l"))

    async def track_devices(self) -> AsyncIterator[list[DeviceEntry]]:
        """Yield the full device list each time the adb server reports a change."""
        s = await self._open()
        try:
            await s.send_request("host:track-devices-l")
            await s.read_status()
            while True:
                yield parse_device_list(await s.read_length_prefixed())
        finally:
            await s.close()

    async def features(self, serial: str) -> set[str]:
        text = await self.query(f"host-serial:{serial}:features", timeout=5.0)
        return {f.strip() for f in text.split(",") if f.strip()}

    async def connect(self, host: str, port: int) -> str:
        return await self.query(f"host:connect:{format_hostport(host, port)}", timeout=30.0)

    async def disconnect(self, target: str) -> str:
        return await self.query(f"host:disconnect:{target}", timeout=10.0)

    async def pair(self, host: str, port: int, code: str) -> str:
        return await self.query(f"host:pair:{code}:{format_hostport(host, port)}", timeout=30.0)

    async def mdns_services(self) -> list[MdnsService]:
        return parse_mdns_services(await self.query("host:mdns:services", timeout=5.0))

    async def open_service(self, serial: str, service: str) -> AdbStream:
        s = await self._open()
        try:
            await s.send_request(f"host:transport:{serial}")
            await s.read_status()
            await s.send_request(service)
            await s.read_status()
        except BaseException:
            await s.close()
            raise
        return s

    async def shell(
        self, serial: str, command: str, *, v2: bool = True, timeout: float | None = 60.0
    ) -> ShellResult:
        """Run a shell command. Callers must quote untrusted arguments with shlex.quote."""

        async def run() -> ShellResult:
            if not v2:
                async with await self.open_service(serial, f"shell:{command}") as s:
                    return ShellResult(await s.read_all(), b"", None)
            async with await self.open_service(serial, f"shell,v2,raw:{command}") as s:
                await s.write(_SHELL_HDR.pack(_SHELL_CLOSE_STDIN, 0))
                out, err = bytearray(), bytearray()
                while True:
                    try:
                        ident, length = _SHELL_HDR.unpack(await s.read_exactly(_SHELL_HDR.size))
                    except AdbError:
                        return ShellResult(bytes(out), bytes(err), None)
                    data = await s.read_exactly(length)
                    if ident == _SHELL_STDOUT:
                        out += data
                    elif ident == _SHELL_STDERR:
                        err += data
                    elif ident == _SHELL_EXIT:
                        return ShellResult(bytes(out), bytes(err), data[0] if data else None)

        return await asyncio.wait_for(run(), timeout)

    async def sync(self, serial: str, features: set[str] | frozenset[str] = frozenset()) -> SyncConnection:
        return SyncConnection(await self.open_service(serial, "sync:"), features)

    async def tcpip(self, serial: str, port: int = 5555) -> str:
        async with await self.open_service(serial, f"tcpip:{port}") as s:
            return (await s.read_all()).decode("utf-8", "replace").strip()


__all__ = [
    "AdbClient",
    "AdbCommandError",
    "DeviceEntry",
    "MdnsService",
    "ShellResult",
    "is_wireless_serial",
    "parse_device_list",
    "parse_mdns_services",
]
