"""Low-level framing for the ADB host protocol (client <-> adb server on tcp:5037)."""

from __future__ import annotations

import asyncio

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 5037
STREAM_LIMIT = 4 * 1024 * 1024


class AdbError(Exception):
    """Base class for ADB failures."""


class AdbConnectionError(AdbError):
    """The adb server or the device connection is unreachable / closed."""


class AdbCommandError(AdbError):
    """The adb server or device answered with FAIL."""


class AdbStream:
    """A single TCP connection to the adb server speaking the host protocol."""

    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self.reader = reader
        self.writer = writer
        self._closed = False

    @classmethod
    async def open(cls, host: str = DEFAULT_HOST, port: int = DEFAULT_PORT, timeout: float = 5.0) -> "AdbStream":
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(host, port, limit=STREAM_LIMIT), timeout
            )
        except (OSError, asyncio.TimeoutError) as exc:
            raise AdbConnectionError(f"cannot reach adb server at {host}:{port}: {exc}") from exc
        return cls(reader, writer)

    async def __aenter__(self) -> "AdbStream":
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()

    async def send_request(self, payload: str) -> None:
        data = payload.encode("utf-8")
        if len(data) > 0xFFFF:
            raise AdbError("request too long")
        await self.write(b"%04x" % len(data) + data)

    async def write(self, data: bytes) -> None:
        self.write_nowait(data)
        await self.drain()

    def write_nowait(self, data: bytes) -> None:
        try:
            self.writer.write(data)
        except (ConnectionError, OSError, RuntimeError) as exc:
            raise AdbConnectionError(f"connection lost while writing: {exc}") from exc

    async def drain(self) -> None:
        try:
            await self.writer.drain()
        except (ConnectionError, OSError) as exc:
            raise AdbConnectionError(f"connection lost while writing: {exc}") from exc

    async def read_exactly(self, n: int) -> bytes:
        try:
            return await self.reader.readexactly(n)
        except asyncio.IncompleteReadError as exc:
            raise AdbConnectionError("connection closed by adb") from exc
        except (ConnectionError, OSError) as exc:
            raise AdbConnectionError(f"connection lost while reading: {exc}") from exc

    async def read_status(self) -> None:
        status = await self.read_exactly(4)
        if status == b"OKAY":
            return
        if status == b"FAIL":
            raise AdbCommandError(await self.read_length_prefixed())
        raise AdbError(f"unexpected adb status {status!r}")

    async def read_length_prefixed(self) -> str:
        raw_len = await self.read_exactly(4)
        try:
            length = int(raw_len, 16)
        except ValueError as exc:
            raise AdbError(f"bad length prefix {raw_len!r}") from exc
        return (await self.read_exactly(length)).decode("utf-8", "replace")

    async def read_all(self) -> bytes:
        chunks = []
        try:
            while chunk := await self.reader.read(65536):
                chunks.append(chunk)
        except (ConnectionError, OSError) as exc:
            raise AdbConnectionError(f"connection lost while reading: {exc}") from exc
        return b"".join(chunks)

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self.writer.close()
        try:
            await self.writer.wait_closed()
        except (ConnectionError, OSError):
            pass
