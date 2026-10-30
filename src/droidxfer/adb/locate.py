"""Locate the adb binary and make sure the adb server is running."""

from __future__ import annotations

import asyncio
import os
import shutil
from pathlib import Path

from .client import AdbClient
from .protocol import AdbConnectionError

_CANDIDATES = (
    "/opt/homebrew/bin/adb",
    "/usr/local/bin/adb",
    "~/Library/Android/sdk/platform-tools/adb",
)


def _executable(path: str | os.PathLike[str]) -> str | None:
    p = Path(path).expanduser()
    return str(p) if p.is_file() and os.access(p, os.X_OK) else None


def find_adb(override: str | None = None) -> str | None:
    if override:
        return _executable(override)
    for env in ("ANDROID_HOME", "ANDROID_SDK_ROOT"):
        if root := os.environ.get(env):
            if found := _executable(Path(root) / "platform-tools" / "adb"):
                return found
    if found := shutil.which("adb"):
        return found
    for candidate in _CANDIDATES:
        if found := _executable(candidate):
            return found
    return None


async def ensure_server(client: AdbClient, adb_path: str | None) -> int:
    """Return the adb server version, starting the server with `adb start-server` if needed."""
    try:
        return await client.version()
    except (AdbConnectionError, asyncio.TimeoutError):
        if not adb_path:
            raise AdbConnectionError(
                "adb server is not running and the adb binary was not found. "
                "Install it with: brew install --cask android-platform-tools"
            ) from None
    proc = await asyncio.create_subprocess_exec(
        adb_path, "start-server", stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    _, err = await asyncio.wait_for(proc.communicate(), 30)
    if proc.returncode != 0:
        raise AdbConnectionError(f"adb start-server failed: {err.decode(errors='replace').strip()}")
    return await client.version()
