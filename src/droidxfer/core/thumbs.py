"""Image thumbnails for Mac and Android files, cached on disk."""

from __future__ import annotations

import asyncio
import hashlib
import io
import logging
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

log = logging.getLogger(__name__)

try:  # HEIC/HEIF photos (Samsung, iPhone); optional so a broken wheel never breaks the app
    from pillow_heif import register_heif_opener

    register_heif_opener()
    HEIF_EXTS = frozenset({".heic", ".heif"})
except Exception as _exc:  # pragma: no cover - depends on the platform wheel
    log.info("HEIC thumbnails unavailable: %s", _exc)
    HEIF_EXTS = frozenset()

IMAGE_EXTS = frozenset(".jpg .jpeg .png .gif .webp .bmp .tif .tiff".split()) | HEIF_EXTS
THUMB_SIZE = 256
PREVIEW_SIZE = 2048  # full-screen previews of formats browsers can't show (HEIC, TIFF)
MAX_SOURCE_BYTES = 64 * 1024 * 1024
CACHE_MAX_BYTES = 200 * 1024 * 1024  # prune the oldest thumbnails beyond this...
CACHE_TARGET_BYTES = 150 * 1024 * 1024  # ...down to this
PRUNE_EVERY = 50  # check the cache size after this many new thumbnails


def is_image(name: str) -> bool:
    return posixpath.splitext(name)[1].lower() in IMAGE_EXTS


def render_thumbnail(data: bytes, size: int = THUMB_SIZE) -> bytes:
    with Image.open(io.BytesIO(data)) as original:
        original.draft("RGB", (size * 2, size * 2))  # fast JPEG decode at reduced scale
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
        img.save(out, "JPEG", quality=80 if size <= THUMB_SIZE else 88)
        return out.getvalue()


class ThumbnailService:
    def __init__(self, local: LocalFS, remote: RemoteFS, devices: DeviceManager, root: Path | None = None) -> None:
        self.local = local
        self.remote = remote
        self.devices = devices
        self.dir = (root or cache_dir()) / "thumbs"
        self._sem = asyncio.Semaphore(3)
        self._stored = 0

    async def for_local(self, path: str, size: int = THUMB_SIZE) -> bytes:
        p = self.local.resolve(path)
        if not is_image(p.name):
            raise ValueError("not an image")
        st = p.stat()
        if st.st_size > MAX_SOURCE_BYTES:
            raise ValueError("image too large for a thumbnail")
        key = f"L|{p}|{st.st_mtime_ns}|{st.st_size}" + (f"|{size}" if size != THUMB_SIZE else "")
        return await self._get(key, lambda: asyncio.to_thread(p.read_bytes), size)

    async def for_remote(self, device_id: str, path: str, size: int = THUMB_SIZE) -> bytes:
        path = normalize_remote(path)
        if not is_image(path):
            raise ValueError("not an image")
        st = await self.remote.stat(device_id, path)
        if st is None:
            raise FileNotFoundError(path)
        if st.size > MAX_SOURCE_BYTES:
            raise ValueError("image too large for a thumbnail")
        device = self.devices.get(device_id)
        key = f"R|{device.id}|{path}|{st.mtime}|{st.size}" + (f"|{size}" if size != THUMB_SIZE else "")
        return await self._get(key, lambda: self.remote.read_bytes(device_id, path, MAX_SOURCE_BYTES), size)

    async def _get(self, key: str, loader, size: int = THUMB_SIZE) -> bytes:
        cached = self.dir / (hashlib.sha256(key.encode("utf-8", "surrogateescape")).hexdigest()[:40] + ".jpg")
        if cached.exists():
            return await asyncio.to_thread(self._read_hit, cached)
        async with self._sem:
            data = await loader()
            thumb = await asyncio.to_thread(render_thumbnail, data, size)
        await asyncio.to_thread(self._store, cached, thumb)
        self._stored += 1
        if self._stored % PRUNE_EVERY == 1:
            await asyncio.to_thread(self.prune)
        return thumb

    @staticmethod
    def _read_hit(path: Path) -> bytes:
        data = path.read_bytes()
        try:
            os.utime(path)  # mark as recently used, so pruning keeps it
        except OSError:
            pass
        return data

    def prune(self, max_bytes: int = CACHE_MAX_BYTES, target_bytes: int = CACHE_TARGET_BYTES) -> int:
        """Delete least recently used thumbnails when the cache grows past `max_bytes`."""
        try:
            files = [(e.stat().st_mtime, e.stat().st_size, e.path) for e in os.scandir(self.dir) if e.is_file()]
        except OSError:
            return 0
        total = sum(size for _, size, _ in files)
        if total <= max_bytes:
            return 0
        removed = 0
        for _, size, path in sorted(files):
            if total <= target_bytes:
                break
            try:
                os.unlink(path)
            except OSError:
                continue
            total -= size
            removed += 1
        return removed

    def _store(self, target: Path, data: bytes) -> None:
        target.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=target.parent, suffix=".tmp")
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        os.replace(tmp, target)
