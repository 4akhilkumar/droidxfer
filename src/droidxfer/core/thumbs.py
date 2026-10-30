"""Image thumbnails for Mac and Android files, cached on disk."""

from __future__ import annotations

import asyncio
import hashlib
import io
import os
import posixpath
import tempfile
from pathlib import Path

from PIL import Image, ImageOps

from .config import cache_dir
from .devices import DeviceManager
from .localfs import LocalFS
from .paths import normalize_remote
from .remotefs import RemoteFS

IMAGE_EXTS = frozenset(".jpg .jpeg .png .gif .webp .bmp .tif .tiff".split())
THUMB_SIZE = 256
MAX_SOURCE_BYTES = 64 * 1024 * 1024


def is_image(name: str) -> bool:
    return posixpath.splitext(name)[1].lower() in IMAGE_EXTS


def render_thumbnail(data: bytes, size: int = THUMB_SIZE) -> bytes:
    with Image.open(io.BytesIO(data)) as original:
        original.draft("RGB", (size * 2, size * 2))
        img = ImageOps.exif_transpose(original)
        img.thumbnail((size, size))
        if img.mode in ("RGBA", "LA", "P"):
            img = img.convert("RGBA")
            background = Image.new("RGB", img.size, (255, 255, 255))
            background.paste(img, mask=img.getchannel("A"))
            img = background
        elif img.mode != "RGB":
            img = img.convert("RGB")
        out = io.BytesIO()
        img.save(out, "JPEG", quality=80)
        return out.getvalue()


class ThumbnailService:
    def __init__(self, local: LocalFS, remote: RemoteFS, devices: DeviceManager, root: Path | None = None) -> None:
        self.local = local
        self.remote = remote
        self.devices = devices
        self.dir = (root or cache_dir()) / "thumbs"
        self._sem = asyncio.Semaphore(3)

    async def for_local(self, path: str) -> bytes:
        p = self.local.resolve(path)
        if not is_image(p.name):
            raise ValueError("not an image")
        st = p.stat()
        if st.st_size > MAX_SOURCE_BYTES:
            raise ValueError("image too large for a thumbnail")
        return await self._get(f"L|{p}|{st.st_mtime_ns}|{st.st_size}", lambda: asyncio.to_thread(p.read_bytes))

    async def for_remote(self, device_id: str, path: str) -> bytes:
        path = normalize_remote(path)
        if not is_image(path):
            raise ValueError("not an image")
        st = await self.remote.stat(device_id, path)
        if st is None:
            raise FileNotFoundError(path)
        if st.size > MAX_SOURCE_BYTES:
            raise ValueError("image too large for a thumbnail")
        device = self.devices.get(device_id)
        key = f"R|{device.id}|{path}|{st.mtime}|{st.size}"
        return await self._get(key, lambda: self.remote.read_bytes(device_id, path, MAX_SOURCE_BYTES))

    async def _get(self, key: str, loader) -> bytes:
        cached = self.dir / (hashlib.sha256(key.encode("utf-8", "surrogateescape")).hexdigest()[:40] + ".jpg")
        if cached.exists():
            return await asyncio.to_thread(cached.read_bytes)
        async with self._sem:
            data = await loader()
            thumb = await asyncio.to_thread(render_thumbnail, data)
        await asyncio.to_thread(self._store, cached, thumb)
        return thumb

    def _store(self, target: Path, data: bytes) -> None:
        target.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=target.parent, suffix=".tmp")
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        os.replace(tmp, target)
