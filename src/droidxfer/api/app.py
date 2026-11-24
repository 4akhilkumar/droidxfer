"""FastAPI application factory."""

from __future__ import annotations

import asyncio
import hashlib
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from .. import __version__
from ..adb import AdbError
from ..core import applog
from ..mtp import LinkError, MtpError
from ..core.apps import AppActionError
from ..core.context import AppContext
from ..core.devices import DeviceNotFound, DeviceUnavailable
from ..core.macbridge import MacBridgeError
from ..core.notebooks import NoteError
from ..core.paths import PathError
from ..core.remotefs import RemoteOpError
from ..core.wireless import WirelessError
from .pen_routes import router as pen_router
from .routes import router
from .security import SecurityMiddleware

WEB_DIR = Path(__file__).resolve().parent.parent / "web"

_STATUS: list[tuple[type[BaseException], int]] = [
    (NoteError, 400),
    (MacBridgeError, 409),
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
    (MtpError, 502),
    (AppActionError, 502),
    (LinkError, 503),
    (asyncio.TimeoutError, 504),
    (OSError, 500),
]


log = logging.getLogger(__name__)


def _handler(status: int):
    async def handle(request: Request, exc: Exception) -> JSONResponse:
        detail = exc.args[0] if isinstance(exc, KeyError) and exc.args else str(exc)
        # Path only (no query string): queries can carry file names the user didn't mean to log.
        level = logging.ERROR if status >= 500 else logging.INFO if status in (404, 409) else logging.WARNING
        log.log(level, "%s %s -> %d %s: %s", request.method, request.url.path, status, exc.__class__.__name__,
                detail or "-")
        return JSONResponse({"detail": detail or exc.__class__.__name__}, status_code=status)

    return handle


async def _unexpected(request: Request, exc: Exception) -> JSONResponse:
    log.error("%s %s failed unexpectedly", request.method, request.url.path, exc_info=exc)
    return JSONResponse({"detail": f"unexpected error: {exc.__class__.__name__}: {exc}"}, status_code=500)


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
    applog.register_secret(token)
    applog.setup(ctx.config.settings.log_level)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        log.info("droidxfer %s web server starting on 127.0.0.1:%d", __version__, port)
        await ctx.start(track=track)
        try:
            yield
        finally:
            await ctx.stop()
            log.info("web server stopped")

    app = FastAPI(title="droidxfer", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.ctx = ctx
    for exc_type, status in _STATUS:
        app.add_exception_handler(exc_type, _handler(status))
    app.add_exception_handler(Exception, _unexpected)
    app.include_router(router, prefix="/api")
    app.include_router(pen_router, prefix="/api/pen")
    ctx.pen.port = port
    # Assets live under a versioned prefix, so after an update the browser can't mix cached old files
    # with new ones (relative imports between the JS modules inherit the prefix).
    version = asset_version()
    static_prefix = f"/static/{version}"
    app.mount(static_prefix, RevalidatingStaticFiles(directory=WEB_DIR), name="static")
    pages = {name: (WEB_DIR / f"{name}.html").read_text().replace('"/static/', f'"{static_prefix}/')
             for name in ("index", "pen", "pad")}

    @app.get("/", include_in_schema=False)
    async def index() -> HTMLResponse:
        return HTMLResponse(pages["index"], headers={"Cache-Control": "no-store"})

    @app.get("/pen", include_in_schema=False)
    async def pen_page() -> HTMLResponse:
        return HTMLResponse(pages["pen"], headers={"Cache-Control": "no-store"})

    @app.get("/pad", include_in_schema=False)
    async def pad_page() -> HTMLResponse:
        return HTMLResponse(pages["pad"], headers={"Cache-Control": "no-store"})

    app.add_middleware(SecurityMiddleware, token=token, port=port, pad_check=ctx.pen.check_pad_token)
    return app
