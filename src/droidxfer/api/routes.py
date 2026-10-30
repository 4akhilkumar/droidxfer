"""REST + WebSocket endpoints for the web UI."""

from __future__ import annotations

import asyncio
import mimetypes
import os
import re
import secrets
import shutil
from pathlib import PurePosixPath
from typing import Any, Literal
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, StreamingResponse
from PIL import UnidentifiedImageError
from pydantic import BaseModel, Field

from .. import __version__
from ..core.context import AppContext
from ..core.paths import validate_name
from ..core.transfer import Direction

router = APIRouter()
Conflict = Literal["rename", "overwrite", "skip"]
_UPLOAD_ID = re.compile(r"^[0-9a-f]{16}$")
_PREVIEW_CSP = "default-src 'none'; img-src 'self'; media-src 'self'; style-src 'unsafe-inline'; sandbox"


def get_ctx(request: Request) -> AppContext:
    return request.app.state.ctx


Ctx = Depends(get_ctx)


# request bodies ------------------------------------------------------------------
class MkdirBody(BaseModel):
    parent: str
    name: str


class RenameBody(BaseModel):
    path: str
    name: str


class PathsBody(BaseModel):
    paths: list[str] = Field(min_length=1)


class TransferBody(BaseModel):
    device_id: str
    direction: Literal["push", "pull"]
    sources: list[str] = Field(min_length=1)
    dest_dir: str
    conflict: Conflict | None = None
    verify: bool | None = None


class CommitBody(BaseModel):
    target: Literal["mac", "android"]
    dest_dir: str
    device_id: str | None = None
    conflict: Conflict | None = None
    verify: bool | None = None


class PairBody(BaseModel):
    host: str
    port: int
    code: str


class HostPortBody(BaseModel):
    host: str
    port: int


class SerialBody(BaseModel):
    serial: str


class TcpipBody(BaseModel):
    port: int = 5555


class SettingsBody(BaseModel):
    adb_path: str | None = None
    download_dir: str | None = None
    default_conflict: Conflict | None = None
    verify_checksum: bool | None = None
    media_scan: bool | None = None
    show_hidden: bool | None = None
    prefer_usb: bool | None = None
    ask_before_transfer: bool | None = None


# helpers --------------------------------------------------------------------------
def _file_headers(name: str) -> tuple[str, dict[str, str]]:
    media_type = mimetypes.guess_type(name)[0] or "application/octet-stream"
    inline = (
        media_type.startswith(("image/", "video/", "audio/")) and media_type != "image/svg+xml"
    ) or media_type == "text/plain"
    if not inline:
        media_type = "application/octet-stream"
    disposition = "inline" if inline else "attachment"
    return media_type, {
        "Content-Disposition": f"{disposition}; filename*=UTF-8''{quote(name, safe='')}",
        "Content-Security-Policy": _PREVIEW_CSP,
        "Cache-Control": "no-store",
    }


async def _thumb(load) -> Response:
    try:
        data = await load
    except (UnidentifiedImageError, ValueError) as exc:
        raise HTTPException(415, f"no thumbnail: {exc}") from None
    return Response(data, media_type="image/jpeg", headers={"Cache-Control": "private, max-age=86400"})


# session / settings ---------------------------------------------------------------
@router.get("/session")
async def session(ctx: AppContext = Ctx) -> dict[str, Any]:
    return {
        "version": __version__,
        "home": str(ctx.local.root),
        "settings": ctx.config.settings.public(),
        "devices": ctx.devices.snapshot(),
        "jobs": ctx.engine.list(),
    }


@router.get("/settings")
async def get_settings(ctx: AppContext = Ctx) -> dict[str, Any]:
    return ctx.config.settings.public()


@router.put("/settings")
async def put_settings(body: SettingsBody, ctx: AppContext = Ctx) -> dict[str, Any]:
    return ctx.config.update(body.model_dump(exclude_unset=True)).public()


# Android device -------------------------------------------------------------------
@router.get("/devices")
async def devices(ctx: AppContext = Ctx) -> dict[str, Any]:
    return ctx.devices.snapshot()


@router.get("/devices/{device_id}/ls")
async def device_ls(device_id: str, path: str = "/sdcard", hidden: bool = False, ctx: AppContext = Ctx) -> dict:
    return await ctx.remote.list(device_id, path, show_hidden=hidden)


@router.get("/devices/{device_id}/thumb")
async def device_thumb(device_id: str, path: str, ctx: AppContext = Ctx) -> Response:
    return await _thumb(ctx.thumbs.for_remote(device_id, path))


@router.get("/devices/{device_id}/file")
async def device_file(device_id: str, path: str, ctx: AppContext = Ctx) -> StreamingResponse:
    st = await ctx.remote.stat(device_id, path)
    if st is None or not st.is_file:
        raise HTTPException(404, "file not found")
    media_type, headers = _file_headers(PurePosixPath(path).name)
    headers["Content-Length"] = str(st.size)
    return StreamingResponse(ctx.remote.iter_file(device_id, path), media_type=media_type, headers=headers)


@router.post("/devices/{device_id}/mkdir")
async def device_mkdir(device_id: str, body: MkdirBody, ctx: AppContext = Ctx) -> dict:
    return {"path": await ctx.remote.mkdir(device_id, body.parent, body.name)}


@router.post("/devices/{device_id}/rename")
async def device_rename(device_id: str, body: RenameBody, ctx: AppContext = Ctx) -> dict:
    return {"path": await ctx.remote.rename(device_id, body.path, body.name)}


@router.post("/devices/{device_id}/delete")
async def device_delete(device_id: str, body: PathsBody, ctx: AppContext = Ctx) -> dict:
    return {"deleted": await ctx.remote.delete(device_id, body.paths)}


@router.post("/devices/{device_id}/tcpip")
async def device_tcpip(device_id: str, body: TcpipBody, ctx: AppContext = Ctx) -> dict:
    return {"message": await ctx.wireless.enable_tcpip(device_id, body.port)}


# Mac ------------------------------------------------------------------------------
@router.get("/local/ls")
async def local_ls(path: str = "~", hidden: bool = False, ctx: AppContext = Ctx) -> dict:
    return await asyncio.to_thread(ctx.local.list, path, show_hidden=hidden)


@router.get("/local/thumb")
async def local_thumb(path: str, ctx: AppContext = Ctx) -> Response:
    return await _thumb(ctx.thumbs.for_local(path))


@router.get("/local/file")
async def local_file(path: str, ctx: AppContext = Ctx) -> FileResponse:
    p = ctx.local.resolve(path)
    if not p.is_file():
        raise HTTPException(404, "file not found")
    media_type, headers = _file_headers(p.name)
    return FileResponse(p, media_type=media_type, headers=headers)


@router.post("/local/mkdir")
async def local_mkdir(body: MkdirBody, ctx: AppContext = Ctx) -> dict:
    return {"path": await asyncio.to_thread(ctx.local.mkdir, body.parent, body.name)}


@router.post("/local/rename")
async def local_rename(body: RenameBody, ctx: AppContext = Ctx) -> dict:
    return {"path": await asyncio.to_thread(ctx.local.rename, body.path, body.name)}


@router.post("/local/delete")
async def local_delete(body: PathsBody, ctx: AppContext = Ctx) -> dict:
    return {"trashed": await asyncio.to_thread(ctx.local.trash, body.paths)}


# transfers ------------------------------------------------------------------------
@router.get("/transfers")
async def transfers(ctx: AppContext = Ctx) -> list[dict]:
    return ctx.engine.list()


@router.post("/transfers")
async def create_transfer(body: TransferBody, ctx: AppContext = Ctx) -> dict:
    job = ctx.engine.submit(
        body.device_id, Direction(body.direction), body.sources, body.dest_dir,
        conflict=body.conflict, verify=body.verify,
    )
    return job.to_dict()


@router.post("/transfers/clear")
async def clear_transfers(ctx: AppContext = Ctx) -> dict:
    return {"removed": ctx.engine.clear_finished()}


@router.post("/transfers/{job_id}/{action}")
async def transfer_action(
    job_id: str, action: Literal["pause", "resume", "cancel", "retry"], ctx: AppContext = Ctx
) -> dict:
    return getattr(ctx.engine, action)(job_id).to_dict()


# uploads (files dropped from Finder) -----------------------------------------------
def _upload_dir(ctx: AppContext, upload_id: str):
    path = ctx.uploads_dir / upload_id
    if not _UPLOAD_ID.match(upload_id) or not path.is_dir():
        raise HTTPException(404, "unknown upload")
    return path


@router.post("/uploads")
async def create_upload(ctx: AppContext = Ctx) -> dict:
    upload_id = secrets.token_hex(8)
    (ctx.uploads_dir / upload_id).mkdir(parents=True)
    return {"id": upload_id}


@router.put("/uploads/{upload_id}")
async def upload_file(
    upload_id: str, request: Request, rel: str = Query(min_length=1), mtime: int | None = None,
    ctx: AppContext = Ctx,
) -> dict:
    base = _upload_dir(ctx, upload_id)
    parts = PurePosixPath(rel).parts
    if not parts or rel.startswith("/"):
        raise HTTPException(400, "invalid relative path")
    target = base.joinpath(*(validate_name(p) for p in parts))
    target.parent.mkdir(parents=True, exist_ok=True)
    size = 0
    with open(target, "wb") as fh:
        async for chunk in request.stream():
            fh.write(chunk)
            size += len(chunk)
    if mtime:
        os.utime(target, (mtime / 1000, mtime / 1000))
    return {"size": size}


@router.post("/uploads/{upload_id}/commit")
async def commit_upload(upload_id: str, body: CommitBody, ctx: AppContext = Ctx) -> dict:
    base = _upload_dir(ctx, upload_id)
    items = sorted(base.iterdir())
    if not items:
        raise HTTPException(400, "upload is empty")
    if body.target == "android":
        if not body.device_id:
            raise HTTPException(400, "device_id is required")
        job = ctx.engine.submit(
            body.device_id, Direction.PUSH, [str(p) for p in items], body.dest_dir,
            conflict=body.conflict, verify=body.verify, cleanup=[str(base)],
        )
        return {"job": job.to_dict()}
    placed = await asyncio.to_thread(
        ctx.local.move_into, items, body.dest_dir, body.conflict or ctx.config.settings.default_conflict
    )
    shutil.rmtree(base, ignore_errors=True)
    return {"placed": placed}


@router.delete("/uploads/{upload_id}")
async def abort_upload(upload_id: str, ctx: AppContext = Ctx) -> dict:
    shutil.rmtree(_upload_dir(ctx, upload_id), ignore_errors=True)
    return {"ok": True}


# wireless -------------------------------------------------------------------------
@router.get("/wireless/discover")
async def wireless_discover(ctx: AppContext = Ctx) -> list[dict]:
    return await ctx.wireless.discover()


@router.get("/wireless/known")
async def wireless_known(ctx: AppContext = Ctx) -> list[dict]:
    return ctx.wireless.known()


@router.post("/wireless/pair")
async def wireless_pair(body: PairBody, ctx: AppContext = Ctx) -> dict:
    return {"message": await ctx.wireless.pair(body.host, body.port, body.code)}


@router.post("/wireless/connect")
async def wireless_connect(body: HostPortBody, ctx: AppContext = Ctx) -> dict:
    return {"message": await ctx.wireless.connect(body.host, body.port)}


@router.post("/wireless/disconnect")
async def wireless_disconnect(body: SerialBody, ctx: AppContext = Ctx) -> dict:
    return {"message": await ctx.wireless.disconnect(body.serial)}


@router.post("/wireless/forget")
async def wireless_forget(body: HostPortBody, ctx: AppContext = Ctx) -> dict:
    ctx.wireless.forget(body.host, body.port)
    return {"ok": True}


@router.post("/wireless/qr")
async def wireless_qr(ctx: AppContext = Ctx) -> dict:
    session = ctx.wireless.start_qr()
    session.pop("payload")
    return session


@router.get("/wireless/qr/{name}")
async def wireless_qr_status(name: str, ctx: AppContext = Ctx) -> dict:
    return ctx.wireless.qr_status(name)


@router.delete("/wireless/qr/{name}")
async def wireless_qr_cancel(name: str, ctx: AppContext = Ctx) -> dict:
    ctx.wireless.cancel_qr(name)
    return {"ok": True}


# live events ----------------------------------------------------------------------
@router.websocket("/events")
async def events(ws: WebSocket) -> None:
    ctx: AppContext = ws.app.state.ctx
    await ws.accept()
    queue = ctx.bus.subscribe()

    async def drain() -> None:
        try:
            while True:
                await ws.receive_text()
        except WebSocketDisconnect:
            return

    receiver = asyncio.create_task(drain())
    try:
        await ws.send_json({"type": "snapshot", "data": {"devices": ctx.devices.snapshot(), "jobs": ctx.engine.list()}})
        while True:
            getter = asyncio.create_task(queue.get())
            done, _ = await asyncio.wait({getter, receiver}, return_when=asyncio.FIRST_COMPLETED)
            if receiver in done:
                getter.cancel()
                break
            await ws.send_json(getter.result())
    except (WebSocketDisconnect, RuntimeError):
        pass
    finally:
        ctx.bus.unsubscribe(queue)
        receiver.cancel()
