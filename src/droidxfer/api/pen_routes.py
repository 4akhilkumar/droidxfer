"""REST + WebSocket endpoints for the S Pen pad, notebooks and their history (mounted at /api/pen)."""

from __future__ import annotations

import asyncio
import io
import logging
import shutil
import subprocess
import tempfile
import time
from collections import OrderedDict
from pathlib import Path
from typing import Any, Literal
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, WebSocket
from PIL import Image
from pydantic import BaseModel, Field

from ..core import handwriting, macbridge
from ..core.context import AppContext
from ..core.notebooks import NoteError, Notebook
from ..core.penrender import flatten, page_to_svg, pages_to_pdf, to_png

router = APIRouter()
log = logging.getLogger(__name__)
MAX_UPLOAD = 60 * 1024 * 1024


def get_ctx(request: Request) -> AppContext:
    return request.app.state.ctx


Ctx = Depends(get_ctx)


class ConnectBody(BaseModel):
    device_id: str
    open_browser: bool = True


class NotebookBody(BaseModel):
    name: str
    template: Literal["blank", "ruled", "grid", "dot"] = "blank"


class NotebookPatch(BaseModel):
    name: str | None = None
    exclude_search: bool | None = None


class CheckpointBody(BaseModel):
    name: str = Field(min_length=1, max_length=80)


class RestoreBody(BaseModel):
    seq: int
    page: str | None = None


class AnnotateBody(BaseModel):
    source: Literal["mac", "android"]
    path: str
    device_id: str | None = None


class PenSettingsBody(BaseModel):
    hw_mode: Literal["auto", "vision", "gemma"] | None = None
    gemma_model: str | None = None
    browser: str | None = None
    shortcuts: dict[str, list[list[str]]] | None = None


class ActionBody(BaseModel):
    name: str
    args: dict[str, Any] = {}


class CompactBody(BaseModel):
    older_than_days: float = Field(gt=0)


def _download(data: bytes, name: str, media_type: str) -> Response:
    return Response(data, media_type=media_type, headers={
        "Content-Disposition": f"attachment; filename*=UTF-8''{quote(name, safe='')}", "Cache-Control": "no-store"})


# ------------------------------------------------------------------ pad tunnel
@router.get("/status")
async def status(ctx: AppContext = Ctx) -> dict:
    return ctx.pen.status()


@router.post("/connect")
async def connect(body: ConnectBody, ctx: AppContext = Ctx) -> dict:
    return await ctx.pen.connect_phone(body.device_id, body.open_browser)


@router.post("/reopen")
async def reopen(ctx: AppContext = Ctx) -> dict:
    if not ctx.pen.phone:
        raise HTTPException(409, "the pen pad isn't connected")
    await ctx.pen.open_pad(ctx.pen.phone["device_id"])
    return ctx.pen.status()


@router.post("/disconnect")
async def disconnect(ctx: AppContext = Ctx) -> dict:
    await ctx.pen.disconnect_phone()
    return ctx.pen.status()


@router.post("/action")
async def action(body: ActionBody, ctx: AppContext = Ctx) -> Any:
    try:
        return {"result": await ctx.pen.run_action(body.name, body.args)}
    except RuntimeError as exc:
        raise HTTPException(409, str(exc)) from None


# ------------------------------------------------------------------ settings
@router.get("/settings")
async def pen_settings(ctx: AppContext = Ctx) -> dict:
    models = await asyncio.to_thread(handwriting.gemma_models)
    return {**ctx.pen.settings.public(), "vision": macbridge.vision_available(), "gemma_models": models,
            "notes_dir": str(ctx.pen.store.root)}


@router.put("/settings")
async def update_pen_settings(body: PenSettingsBody, ctx: AppContext = Ctx) -> dict:
    changes = body.model_dump(exclude_none=True)
    try:
        ctx.pen.settings.update(**changes)
    except macbridge.MacBridgeError as exc:
        raise HTTPException(400, str(exc)) from None
    return await pen_settings(ctx)


# ------------------------------------------------------------------ notebooks
def _nb(ctx: AppContext, notebook_id: str) -> Notebook:
    nb = ctx.pen.store.get(notebook_id)
    ctx.pen._attach(nb)
    return nb


@router.get("/notebooks")
async def notebooks(ctx: AppContext = Ctx) -> list[dict]:
    return ctx.pen.store.list()


@router.post("/notebooks")
async def create_notebook(body: NotebookBody, ctx: AppContext = Ctx) -> dict:
    nb = ctx.pen.store.create(body.name, body.template)
    ctx.pen.switch(nb.id)
    return nb.info()


@router.patch("/notebooks/{notebook_id}")
async def patch_notebook(notebook_id: str, body: NotebookPatch, ctx: AppContext = Ctx) -> dict:
    nb = _nb(ctx, notebook_id)
    if body.exclude_search is not None:
        nb.set_meta(exclude_search=body.exclude_search)
        if body.exclude_search:
            shutil.rmtree(nb.folder / "text", ignore_errors=True)
    if body.name is not None and body.name != nb.id:
        was_current = ctx.pen.current.get("notebook") == nb.id
        nb = ctx.pen.store.rename(nb.id, body.name)
        if was_current:
            ctx.pen.switch(nb.id, ctx.pen.current.get("page"))
    return nb.info()


@router.delete("/notebooks/{notebook_id}")
async def delete_notebook(notebook_id: str, ctx: AppContext = Ctx) -> dict:
    was_current = ctx.pen.current.get("notebook") == notebook_id
    ctx.pen.store.delete(notebook_id)
    if was_current:
        ctx.pen.current = {}
        nb = ctx.pen.notebook()
        ctx.pen.switch(nb.id)
    return {"ok": True}


@router.get("/notebooks/{notebook_id}")
async def notebook(notebook_id: str, ctx: AppContext = Ctx) -> dict:
    return _nb(ctx, notebook_id).snapshot()


@router.get("/notebooks/{notebook_id}/history")
async def history(notebook_id: str, before: int | None = None, limit: int = Query(300, ge=1, le=2000),
                  ctx: AppContext = Ctx) -> dict:
    nb = _nb(ctx, notebook_id)
    return {**nb.history(before, limit), "checkpoints": nb.checkpoints()}


@router.get("/notebooks/{notebook_id}/state")
async def state_at(notebook_id: str, seq: int, ctx: AppContext = Ctx) -> dict:
    nb = _nb(ctx, notebook_id)
    state = await asyncio.to_thread(nb.state_at, seq)
    return {"seq": min(seq, nb.seq), "pages": state["pages"]}


@router.get("/notebooks/{notebook_id}/ops")
async def ops(notebook_id: str, start: int = 0, end: int | None = None, page: str | None = None,
              ctx: AppContext = Ctx) -> dict:
    nb = _nb(ctx, notebook_id)
    return {"ops": nb.ops_between(start, nb.seq if end is None else end, page)}


@router.post("/notebooks/{notebook_id}/checkpoint")
async def checkpoint(notebook_id: str, body: CheckpointBody, ctx: AppContext = Ctx) -> dict:
    nb = _nb(ctx, notebook_id)
    entry = nb.checkpoint(body.name, "mac")
    ctx.pen._broadcast_entry(nb, entry)
    return entry.summary()


@router.post("/notebooks/{notebook_id}/restore")
async def restore(notebook_id: str, body: RestoreBody, ctx: AppContext = Ctx) -> dict:
    nb = _nb(ctx, notebook_id)
    entry = nb.restore(body.seq, body.page, "mac")
    if ctx.pen.current.get("notebook") == nb.id:
        ctx.pen._broadcast_entry(nb, entry)
    return entry.summary()


@router.post("/notebooks/{notebook_id}/compact")
async def compact(notebook_id: str, body: CompactBody, ctx: AppContext = Ctx) -> dict:
    nb = _nb(ctx, notebook_id)
    return {"removed": nb.compact(body.older_than_days)}


_THUMBS: OrderedDict[tuple, bytes] = OrderedDict()


@router.get("/notebooks/{notebook_id}/thumb")
async def thumb(notebook_id: str, page: str, seq: int | None = None, w: int = Query(220, ge=40, le=1200),
                ctx: AppContext = Ctx) -> Response:
    nb = _nb(ctx, notebook_id)
    seq = nb.seq if seq is None else min(seq, nb.seq)
    key = (str(nb.folder), page, seq, w)
    data = _THUMBS.get(key)
    if data is None:
        def render() -> bytes:
            state = nb.state_at(seq)
            pg = ctx.pen.find_page(nb, page, state)
            img = ctx.pen.renderer_for(nb).render(pg, scale=w / pg["w"])
            out = io.BytesIO()
            flatten(img).save(out, "PNG")
            return out.getvalue()

        data = await asyncio.to_thread(render)
        _THUMBS[key] = data
        while len(_THUMBS) > 400:
            _THUMBS.popitem(last=False)
    return Response(data, media_type="image/png", headers={"Cache-Control": "private, max-age=3600"})


def _asset_response(nb: Notebook, name: str) -> Response:
    path = nb.asset_path(name)
    media = "image/png" if name.endswith(".png") else "image/jpeg"
    return Response(path.read_bytes(), media_type=media, headers={"Cache-Control": "private, max-age=31536000, immutable"})


@router.get("/notebooks/{notebook_id}/asset/{name}")
async def asset(notebook_id: str, name: str, ctx: AppContext = Ctx) -> Response:
    return _asset_response(_nb(ctx, notebook_id), name)


async def _body(request: Request) -> bytes:
    data = bytearray()
    async for chunk in request.stream():
        data += chunk
        if len(data) > MAX_UPLOAD:
            raise HTTPException(413, "pictures can be at most 60 MB")
    if not data:
        raise HTTPException(400, "empty upload")
    return bytes(data)


@router.post("/notebooks/{notebook_id}/pictures")
async def add_picture(notebook_id: str, request: Request, mode: Literal["page", "insert"] = "insert",
                      page: str | None = None, ctx: AppContext = Ctx) -> dict:
    data = await _body(request)
    nb = _nb(ctx, notebook_id)
    if ctx.pen.current.get("notebook") != nb.id:
        ctx.pen.switch(nb.id)
    if mode == "page":
        return await asyncio.to_thread(ctx.pen.add_picture_page, nb, data, "mac", None, "Added a picture to annotate")
    return await asyncio.to_thread(ctx.pen.insert_picture, nb, data, page, "mac")


@router.post("/annotate")
async def annotate(body: AnnotateBody, ctx: AppContext = Ctx) -> dict:
    """Open a picture from the Mac or the phone as a new page to draw on."""
    if body.source == "mac":
        path = ctx.local.resolve(body.path)
        if path.stat().st_size > MAX_UPLOAD:
            raise HTTPException(413, "pictures can be at most 60 MB")
        data = await asyncio.to_thread(path.read_bytes)
        origin = {"kind": "mac", "path": str(path)}
    else:
        if not body.device_id:
            raise HTTPException(400, "device_id is required")
        data = await ctx.remote.read_bytes(body.device_id, body.path, MAX_UPLOAD)
        origin = {"kind": "android", "device_id": body.device_id, "path": body.path}
    nb = ctx.pen.notebook()
    result = await asyncio.to_thread(ctx.pen.add_picture_page, nb, data, "mac", origin,
                                     f"Opened {Path(body.path).name} to annotate")
    return {**result, "notebook": nb.id}


# ------------------------------------------------------------------ export
@router.get("/notebooks/{notebook_id}/export")
async def export(notebook_id: str, format: Literal["png", "svg", "pdf", "gif", "mp4"] = "png",  # noqa: A002
                 page: str | None = None, seq: int | None = None, ctx: AppContext = Ctx) -> Response:
    nb = _nb(ctx, notebook_id)
    state = await asyncio.to_thread(nb.state_at, nb.seq if seq is None else seq)
    stamp = time.strftime("%Y-%m-%d")
    if format == "pdf":
        data = await asyncio.to_thread(pages_to_pdf, ctx.pen.renderer_for(nb), state["pages"])
        return _download(data, f"{nb.name} {stamp}.pdf", "application/pdf")
    pg = ctx.pen.find_page(nb, page, state)
    title = f"{nb.name} - page {state['pages'].index(pg) + 1}"
    if format == "png":
        data = await asyncio.to_thread(ctx.pen.render_png, nb, pg["id"], None, state=state)
        return _download(data, f"{title}.png", "image/png")
    if format == "svg":
        def href(name: str) -> str:
            import base64

            path = nb.asset_path(name)
            mime = "image/png" if name.endswith(".png") else "image/jpeg"
            return f"data:{mime};base64,{base64.b64encode(path.read_bytes()).decode()}"

        return _download(page_to_svg(pg, href).encode(), f"{title}.svg", "image/svg+xml")
    data = await asyncio.to_thread(_timelapse, ctx, nb, pg["id"], format)
    return _download(data, f"{title} timelapse.{format}", "image/gif" if format == "gif" else "video/mp4")


def _timelapse(ctx: AppContext, nb: Notebook, page_id: str, fmt: str, max_frames: int = 180) -> bytes:
    from ..core.notebooks import apply_op

    touching = [e for e in nb.entries if page_id in e.pages]
    if not touching:
        raise NoteError("this page has no history to replay")
    state = nb.state_at(touching[0].seq - 1)
    renderer = ctx.pen.renderer_for(nb)
    frames: list[Image.Image] = []
    n = len(touching)
    if n > max_frames:  # spread the frames evenly over the page's history
        pick = {touching[round(i * (n - 1) / (max_frames - 1))].seq for i in range(max_frames)}
    else:
        pick = {e.seq for e in touching}
    entries = [e for e in nb.entries if touching[0].seq <= e.seq <= touching[-1].seq]
    width = 720
    for e in entries:
        apply_op(state, e.op, lenient=True)
        if e.seq not in pick:
            continue
        pg = next((p for p in state["pages"] if p["id"] == page_id), None)
        if pg is None:
            continue
        frames.append(flatten(renderer.render(pg, scale=width / pg["w"])))
    if not frames:
        raise NoteError("nothing to replay")
    frames += [frames[-1]] * 12  # hold the finished page for a moment
    if fmt == "gif":
        out = io.BytesIO()
        frames[0].save(out, "GIF", save_all=True, append_images=frames[1:], duration=90, loop=0, optimize=True)
        return out.getvalue()
    ffmpeg = shutil.which("ffmpeg") or ("/opt/homebrew/bin/ffmpeg" if Path("/opt/homebrew/bin/ffmpeg").exists() else None)
    if not ffmpeg:
        raise NoteError("MP4 needs ffmpeg (brew install ffmpeg); GIF works without it")
    with tempfile.TemporaryDirectory(prefix="droidxfer-replay-") as tmp:
        for i, frame in enumerate(frames):
            frame.save(Path(tmp) / f"{i:05d}.png")
        out_path = Path(tmp) / "out.mp4"
        proc = subprocess.run([ffmpeg, "-loglevel", "error", "-y", "-framerate", "12", "-i", str(Path(tmp) / "%05d.png"),
                               "-vf", "scale=trunc(iw/2)*2:trunc(ih/2)*2", "-pix_fmt", "yuv420p", "-c:v", "libx264",
                               str(out_path)], capture_output=True, timeout=300)
        if proc.returncode != 0:
            raise NoteError(f"ffmpeg failed: {proc.stderr.decode('utf-8', 'replace')[:200]}")
        return out_path.read_bytes()


# ------------------------------------------------------------------ handwriting & search
@router.get("/search")
async def search(q: str = Query(min_length=1, max_length=200), ctx: AppContext = Ctx) -> list[dict]:
    return await asyncio.to_thread(ctx.pen.search, q)


@router.post("/notebooks/{notebook_id}/hw-compare")
async def hw_compare(notebook_id: str, page: str | None = None, ctx: AppContext = Ctx) -> dict:
    nb = _nb(ctx, notebook_id)
    pg = ctx.pen.find_page(nb, page)
    png, _ = ctx.pen._ink_png(nb, pg, None)
    return await asyncio.to_thread(handwriting.compare_sync, png, ctx.pen.settings["gemma_model"])


# ------------------------------------------------------------------ stickers
@router.get("/stickers")
async def stickers(ctx: AppContext = Ctx) -> list[dict]:
    return ctx.pen.stickers()


@router.get("/stickers/{name}.png")
async def sticker(name: str, ctx: AppContext = Ctx) -> Response:
    return Response(ctx.pen.sticker_path(name).read_bytes(), media_type="image/png",
                    headers={"Cache-Control": "no-cache"})


@router.delete("/stickers/{name}")
async def delete_sticker(name: str, ctx: AppContext = Ctx) -> dict:
    ctx.pen.sticker_path(name).unlink()
    return {"ok": True}


# ------------------------------------------------------------------ the phone pad (pad token is enough)
@router.get("/pad/asset/{notebook_id}/{name}")
async def pad_asset(notebook_id: str, name: str, ctx: AppContext = Ctx) -> Response:
    return _asset_response(_nb(ctx, notebook_id), name)


@router.get("/pad/sticker/{name}.png")
async def pad_sticker(name: str, ctx: AppContext = Ctx) -> Response:
    return await sticker(name, ctx)


@router.websocket("/pad/ws")
async def pad_ws(ws: WebSocket) -> None:
    await ws.accept()
    await ws.app.state.ctx.pen.serve(ws, "pad")


@router.websocket("/ws")
async def mac_ws(ws: WebSocket) -> None:
    await ws.accept()
    await ws.app.state.ctx.pen.serve(ws, "mac")
