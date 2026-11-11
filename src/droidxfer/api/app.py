"""FastAPI application factory."""

from __future__ import annotations

import asyncio
import hashlib
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from ..adb import AdbError
from ..core.context import AppContext
from ..core.devices import DeviceNotFound, DeviceUnavailable
from ..core.paths import PathError
from ..core.remotefs import RemoteOpError
from ..core.wireless import WirelessError
from .routes import router
from .security import SecurityMiddleware

WEB_DIR = Path(__file__).resolve().parent.parent / "web"

_STATUS: list[tuple[type[BaseException], int]] = [
    (PathError, 400),
    (ValueError, 400),
    (NotADirectoryError, 400),
    (IsADirectoryError, 400),
    (WirelessError, 400),
    (RemoteOpError, 400),
    (PermissionError, 403),
    (FileNotFoundError, 404),
    (KeyError, 404),
    (DeviceNotFound, 404),
    (FileExistsError, 409),
    (DeviceUnavailable, 409),
    (AdbError, 502),
    (asyncio.TimeoutError, 504),
    (OSError, 500),
]


def _handler(status: int):
    async def handle(_: Request, exc: Exception) -> JSONResponse:
        detail = exc.args[0] if isinstance(exc, KeyError) and exc.args else str(exc)
        return JSONResponse({"detail": detail or exc.__class__.__name__}, status_code=status)

    return handle


class RevalidatingStaticFiles(StaticFiles):
    """Static assets that the browser must revalidate (ETag -> 304) on every load.

    Without a Cache-Control header browsers cache files heuristically based on their age, so after an
    update an old stylesheet could be reused next to new scripts and break the UI.
    """

    def file_response(self, *args, **kwargs) -> Response:
        response = super().file_response(*args, **kwargs)
        response.headers["Cache-Control"] = "no-cache"
        return response


def asset_version(web_dir: Path = WEB_DIR) -> str:
    """Short hash of the web assets' names, sizes and mtimes; changes whenever any asset changes."""
    digest = hashlib.sha256()
    for path in sorted(p for p in web_dir.rglob("*") if p.is_file()):
        st = path.stat()
        digest.update(f"{path.relative_to(web_dir)}|{st.st_size}|{st.st_mtime_ns}\n".encode())
    return digest.hexdigest()[:12]


def create_app(*, token: str, port: int, context: AppContext | None = None, track: bool = True) -> FastAPI:
    ctx = context or AppContext()

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        await ctx.start(track=track)
        try:
            yield
        finally:
            await ctx.stop()

    app = FastAPI(title="droidxfer", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.ctx = ctx
    for exc_type, status in _STATUS:
        app.add_exception_handler(exc_type, _handler(status))
    app.include_router(router, prefix="/api")
    # Assets live under a versioned prefix, so after an update the browser can't mix cached old files
    # with new ones (relative imports between the JS modules inherit the prefix).
    version = asset_version()
    static_prefix = f"/static/{version}"
    app.mount(static_prefix, RevalidatingStaticFiles(directory=WEB_DIR), name="static")
    index_html = (WEB_DIR / "index.html").read_text().replace('"/static/', f'"{static_prefix}/')

    @app.get("/", include_in_schema=False)
    async def index() -> HTMLResponse:
        return HTMLResponse(index_html, headers={"Cache-Control": "no-store"})

    app.add_middleware(SecurityMiddleware, token=token, port=port)
    return app
