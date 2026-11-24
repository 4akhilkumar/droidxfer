"""S Pen pad: the phone's browser becomes a drawing pad for the Mac.

How it connects: `adb reverse tcp:<port> tcp:<port>` makes http://localhost:<port> on the phone reach this
server (which still only listens on 127.0.0.1), then the phone's browser is opened on /pad with a one-time
token that only unlocks the pad. Works over USB and over Wi-Fi debugging; when droidxfer switches the phone
between them the tunnel is re-created and the pad reconnects by itself.

Every browser window (the phone pad and any Mac "Pen" tab) is a *peer* on one WebSocket. They all show the
same notebook and page; changes go through `Notebook.apply` and are broadcast to every peer, while
in-progress strokes, hover and the laser pointer are only relayed (never saved).
"""

from __future__ import annotations

import asyncio
import io
import json
import logging
import os
import re
import secrets
import shlex
import shutil
import tempfile
import time
from pathlib import Path
from typing import Any

from PIL import Image

from ..adb import AdbClient, AdbError
from . import handwriting, macbridge
from .config import app_dir, cache_dir
from .devices import DeviceManager, DeviceUnavailable
from .events import EventBus
from .notebooks import TEMPLATES, NoteError, Notebook, NotebookStore, blank_page, new_id, valid_name
from .penrender import PageRenderer, flatten, obj_bounds, to_png

log = logging.getLogger(__name__)

PAD_COOKIE = "droidxfer_pad"
INDEX_IDLE = 4.0  # seconds after the last change before a page's handwriting is (re)indexed
MAX_PICTURE = 60 * 1024 * 1024

DEFAULT_SHORTCUTS: dict[str, list[list[str]]] = {
    "Any app": [["Undo", "cmd+z"], ["Redo", "cmd+shift+z"], ["Copy", "cmd+c"], ["Paste", "cmd+v"],
                ["Esc", "escape"], ["Delete", "delete"]],
    "Adobe Photoshop": [["Undo", "cmd+z"], ["Redo", "cmd+shift+z"], ["Brush", "b"], ["Eraser", "e"],
                        ["Smaller", "["], ["Bigger", "]"], ["Eyedropper", "i"], ["Swap colours", "x"],
                        ["Zoom in", "cmd+="], ["Zoom out", "cmd+-"]],
    "krita": [["Undo", "cmd+z"], ["Redo", "cmd+shift+z"], ["Brush", "b"], ["Eraser", "e"], ["Smaller", "["],
              ["Bigger", "]"], ["Swap colours", "x"], ["Zoom in", "="], ["Zoom out", "-"]],
    "Preview": [["Undo", "cmd+z"], ["Redo", "cmd+shift+z"], ["Markup", "cmd+shift+a"], ["Zoom in", "cmd+="],
                ["Zoom out", "cmd+-"]],
    "Figma": [["Undo", "cmd+z"], ["Redo", "cmd+shift+z"], ["Pencil", "shift+p"], ["Move", "v"],
              ["Zoom in", "cmd+="], ["Zoom out", "cmd+-"]],
}


class PenSettings:
    """pen.json: handwriting engine, browser, tablet mapping, shortcut sets, last notebook."""

    DEFAULTS: dict[str, Any] = {
        "hw_mode": "auto", "gemma_model": "gemma4:12b", "browser": "", "last_notebook": "", "last_page": "",
        "tablet_target": {"kind": "display", "id": None}, "shortcuts": DEFAULT_SHORTCUTS,
    }

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or app_dir() / "pen.json"
        self.data = dict(self.DEFAULTS)
        try:
            raw = json.loads(self.path.read_text())
            if isinstance(raw, dict):
                for key, default in self.DEFAULTS.items():
                    if key in raw and type(raw[key]) is type(default):
                        self.data[key] = raw[key]
        except (OSError, ValueError):
            pass

    def __getitem__(self, key: str) -> Any:
        return self.data[key]

    def update(self, **changes: Any) -> None:
        if "hw_mode" in changes and changes["hw_mode"] not in handwriting.MODES:
            raise ValueError(f"hw_mode must be one of {handwriting.MODES}")
        if "gemma_model" in changes and (not isinstance(changes["gemma_model"], str) or not changes["gemma_model"]):
            raise ValueError("gemma_model must be a model name")
        if "browser" in changes and not re.fullmatch(r"[A-Za-z0-9_.]*", changes["browser"] or ""):
            raise ValueError("browser must be an Android package name")
        if "shortcuts" in changes:
            _check_shortcuts(changes["shortcuts"])
        self.data.update(changes)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=".pen-")
        with os.fdopen(fd, "w") as fh:
            json.dump(self.data, fh, indent=2)
        os.replace(tmp, self.path)

    def public(self) -> dict:
        return {k: self.data[k] for k in ("hw_mode", "gemma_model", "browser", "tablet_target", "shortcuts")}


def _check_shortcuts(value: Any) -> None:
    if not isinstance(value, dict) or len(value) > 50:
        raise ValueError("shortcuts must be an object of app name -> [[label, keys], ...]")
    for app, items in value.items():
        if not isinstance(app, str) or not isinstance(items, list) or len(items) > 24:
            raise ValueError("each app needs a list of up to 24 shortcuts")
        for item in items:
            if not (isinstance(item, list) and len(item) == 2 and all(isinstance(x, str) for x in item)):
                raise ValueError("each shortcut is [label, keys]")
            macbridge.parse_combo(item[1])


class Peer:
    def __init__(self, ws, role: str) -> None:
        self.ws = ws
        self.role = role  # "pad" (the phone) | "mac" (a Pen tab on the Mac)
        self.id = new_id()
        self.queue: asyncio.Queue[dict] = asyncio.Queue(4000)

    def send(self, msg: dict) -> None:
        try:
            self.queue.put_nowait(msg)
        except asyncio.QueueFull:
            pass  # a stuck browser: drop rather than block everyone


class PenHub:
    def __init__(self, client: AdbClient, devices: DeviceManager, bus: EventBus, store: NotebookStore | None = None,
                 settings: PenSettings | None = None, *, engine=None, remote=None, local=None) -> None:
        self.client = client
        self.engine = engine  # TransferEngine, RemoteFS, LocalFS: for "send to phone" / "save annotated copy"
        self.remote = remote
        self.local = local
        self.devices = devices
        self.bus = bus
        self.store = store or NotebookStore()
        self.settings = settings or PenSettings()
        self.renderer = PageRenderer()
        self.port: int | None = None
        self.pad_token: str | None = None
        self.phone: dict | None = None  # {"device_id", "serial"} while the pad tunnel is up
        self.peers: dict[str, Peer] = {}
        self.current: dict[str, str] = {}  # {"notebook", "page"}
        self.tool: dict = {}
        self.cids: dict[str, int] = {}  # recent client change ids -> seq (dedupes resends after reconnects)
        self.tablet: macbridge.TabletInjector | None = None
        self.tablet_on = False
        self.tablet_rect: dict | None = None
        self._index_dirty: dict[tuple[str, str], float] = {}
        self._tasks: list[asyncio.Task] = []
        self._watch_queue: asyncio.Queue | None = None
        self.indexer_enabled = True

    # ------------------------------------------------------------------ lifecycle
    async def start(self) -> None:
        self._watch_queue = self.bus.subscribe()
        self._tasks.append(asyncio.create_task(self._watch_devices()))
        if self.indexer_enabled:
            self._tasks.append(asyncio.create_task(self._index_loop()))

    async def stop(self) -> None:
        try:
            await self.disconnect_phone()
        except Exception:
            pass
        for t in self._tasks:
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        if self._watch_queue:
            self.bus.unsubscribe(self._watch_queue)

    # ------------------------------------------------------------------ auth
    def check_pad_token(self, token: str) -> bool:
        return bool(self.pad_token) and secrets.compare_digest(token.encode(), self.pad_token.encode())

    # ------------------------------------------------------------------ phone tunnel
    def status(self) -> dict:
        return {"phone": self.phone, "pad_online": any(p.role == "pad" for p in self.peers.values()),
                "mac_views": sum(p.role == "mac" for p in self.peers.values()), "current": self.current,
                "tablet": self.tablet_on}

    async def connect_phone(self, device_id: str, open_browser: bool = True) -> dict:
        if self.port is None:
            raise RuntimeError("the pen pad needs the web server (droidxfer serve)")
        transport = self.devices.transport(device_id)
        if transport.kind == "mtp":
            raise DeviceUnavailable("the S Pen pad needs USB debugging (it uses adb). File transfer mode can't do it.")
        await self.client.reverse(transport.serial, f"tcp:{self.port}", f"tcp:{self.port}")
        self.pad_token = secrets.token_urlsafe(24)
        self.phone = {"device_id": self.devices.get(device_id).id, "serial": transport.serial}
        log.info("pen pad: tunnel up on %s (%s), port %d", transport.serial, transport.kind, self.port)
        if open_browser:
            await self.open_pad(device_id)
        self._publish_status()
        return self.status()

    async def open_pad(self, device_id: str) -> None:
        transport = self.devices.transport(device_id)
        url = f"http://localhost:{self.port}/pad?k={self.pad_token}"
        await self.client.shell(transport.serial, "input keyevent KEYCODE_WAKEUP", timeout=10)
        browser = self.settings["browser"]
        cmd = f"am start -a android.intent.action.VIEW -d {shlex.quote(url)}"
        if browser:
            cmd += f" -p {shlex.quote(browser)}"
        result = await self.client.shell(transport.serial, cmd, timeout=15)
        if "Error" in result.text:
            raise RuntimeError(f"couldn't open the phone's browser: {result.text.strip()[:200]}")

    async def disconnect_phone(self) -> None:
        phone, self.phone = self.phone, None
        self.pad_token = None
        for peer in list(self.peers.values()):
            if peer.role == "pad":
                peer.send({"t": "bye", "reason": "The Mac closed the pen pad."})
        if phone and self.port:
            try:
                await self.client.reverse_remove(phone["serial"], f"tcp:{self.port}")
            except (AdbError, OSError, asyncio.TimeoutError):
                pass
        self._publish_status()

    async def _watch_devices(self) -> None:
        """Re-create the tunnel when the phone moves between USB and Wi-Fi."""
        assert self._watch_queue is not None
        while True:
            event = await self._watch_queue.get()
            if event.get("type") != "devices" or not self.phone:
                continue
            try:
                transport = self.devices.transport(self.phone["device_id"])
            except Exception:
                continue  # gone for now; the pad keeps its changes and retries
            if transport.kind == "mtp" or transport.serial == self.phone["serial"]:
                continue
            try:
                await self.client.reverse(transport.serial, f"tcp:{self.port}", f"tcp:{self.port}")
                self.phone["serial"] = transport.serial
                log.info("pen pad: tunnel moved to %s (%s)", transport.serial, transport.kind)
                self._publish_status()
            except Exception as exc:
                log.warning("pen pad: couldn't move the tunnel to %s: %s", transport.serial, exc)

    def _publish_status(self) -> None:
        status = self.status()
        self.bus.publish("pen", status)
        self.broadcast({"t": "status", **status})

    # ------------------------------------------------------------------ peers
    def broadcast(self, msg: dict, exclude: Peer | None = None) -> None:
        for peer in list(self.peers.values()):
            if peer is not exclude:
                peer.send(msg)

    def notebook(self) -> Notebook:
        nb_id = self.current.get("notebook") or self.settings["last_notebook"]
        try:
            nb = self.store.get(nb_id) if nb_id else self.store.default()
        except (KeyError, NoteError):
            nb = self.store.default()
        self._attach(nb)
        if self.current.get("notebook") != nb.id:
            self.current = {"notebook": nb.id, "page": self.settings["last_page"]}
        if not any(p["id"] == self.current.get("page") for p in nb.state["pages"]):
            self.current["page"] = nb.state["pages"][0]["id"]
        return nb

    def _attach(self, nb: Notebook) -> None:
        if self._on_entry not in nb.listeners:
            nb.listeners.append(self._on_entry)

    def _on_entry(self, entry) -> None:
        if self.current.get("notebook"):
            for page in entry.pages:
                self._index_dirty[(self.current["notebook"], page)] = time.monotonic()

    def _state_msg(self, nb: Notebook) -> dict:
        return {"t": "state", **nb.snapshot(), "current": self.current, "tool": self.tool, "status": self.status(),
                "settings": {"hw_mode": self.settings["hw_mode"]}}

    def _remember(self) -> None:
        if self.current:
            try:
                self.settings.update(last_notebook=self.current["notebook"], last_page=self.current["page"])
            except Exception:
                pass

    async def serve(self, ws, role: str) -> None:
        peer = Peer(ws, role)
        self.peers[peer.id] = peer
        writer = asyncio.create_task(self._writer(peer))
        log.info("pen: %s connected", "phone pad" if role == "pad" else "Mac pen view")
        try:
            nb = self.notebook()
            peer.send(self._state_msg(nb))
            self._publish_status()
            while True:
                try:
                    msg = await ws.receive_json()
                except ValueError:
                    continue
                if not isinstance(msg, dict):
                    continue
                await self._handle(peer, msg)
        except Exception as exc:  # WebSocketDisconnect and friends
            if exc.__class__.__name__ not in ("WebSocketDisconnect", "ClientDisconnected"):
                log.debug("pen peer %s closed: %r", role, exc)
        finally:
            self.peers.pop(peer.id, None)
            writer.cancel()
            if role == "pad" and self.tablet_on and not any(p.role == "pad" for p in self.peers.values()):
                self._tablet_leave()
            self.broadcast({"t": "hover_end", "from": peer.id})
            self._publish_status()

    async def _writer(self, peer: Peer) -> None:
        try:
            while True:
                msg = await peer.queue.get()
                await peer.ws.send_json(msg)
        except Exception:
            return

    async def _handle(self, peer: Peer, msg: dict) -> None:
        t = msg.get("t")
        if t in ("live", "hover", "hover_end", "laser", "select"):
            msg["from"] = peer.id
            self.broadcast(msg, exclude=peer)
        elif t == "tool":
            self.tool = {k: msg.get(k) for k in ("tool", "color", "size") if k in msg}
            self.broadcast({"t": "tool", **self.tool, "from": peer.id}, exclude=peer)
        elif t == "op":
            self._apply(peer, msg)
        elif t in ("undo", "redo"):
            nb = self.notebook()
            entry = nb.undo(peer.role) if t == "undo" else nb.redo(peer.role)
            if entry is None:
                peer.send({"t": "notice", "text": f"Nothing to {t}."})
            else:
                self._broadcast_entry(nb, entry)
        elif t == "page":
            nb = self.notebook()
            if any(p["id"] == msg.get("page") for p in nb.state["pages"]):
                self.current["page"] = msg["page"]
                self.broadcast({"t": "page", "page": msg["page"], "from": peer.id}, exclude=peer)
                self._remember()
        elif t == "tablet":
            self._tablet_samples(msg.get("events") or [])
        elif t == "action":
            await self._action(peer, msg)
        elif t == "ping":
            peer.send({"t": "pong", "ts": msg.get("ts")})

    def _apply(self, peer: Peer, msg: dict) -> None:
        cid = msg.get("cid")
        nb = self.notebook()
        if isinstance(cid, str) and cid in self.cids:
            peer.send({"t": "ack", "cid": cid, "seq": self.cids[cid]})
            return
        try:
            op = msg.get("op")
            if not isinstance(op, dict) or op.get("op") == "checkpoint":
                raise NoteError("invalid change")
            entry = nb.apply(op, actor=peer.role, kind="copy" if msg.get("copy") else "edit")
        except NoteError as exc:
            peer.send({"t": "rejected", "cid": cid, "error": str(exc)})
            return
        if isinstance(cid, str) and len(cid) <= 64:
            self.cids[cid] = entry.seq
            if len(self.cids) > 5000:
                for key in list(self.cids)[:1000]:
                    del self.cids[key]
        self._broadcast_entry(nb, entry, cid)

    def _broadcast_entry(self, nb: Notebook, entry, cid: str | None = None) -> None:
        msg = {"t": "applied", "notebook": nb.id, "entry": {k: v for k, v in entry.to_json().items() if k != "inv"},
               "can_undo": bool(nb.undo_stack), "can_redo": bool(nb.redo_stack)}
        if cid:
            msg["cid"] = cid
        # Any change (a page delete, a batch that swaps the last page, an undo...) can remove the current page.
        if not any(p["id"] == self.current.get("page") for p in nb.state["pages"]):
            self.current["page"] = nb.state["pages"][0]["id"]
        msg["current"] = self.current
        self.broadcast(msg)

    def switch(self, notebook_id: str, page: str | None = None) -> Notebook:
        nb = self.store.get(notebook_id)
        self._attach(nb)
        pages = [p["id"] for p in nb.state["pages"]]
        self.current = {"notebook": nb.id, "page": page if page in pages else pages[0]}
        self._remember()
        for peer in list(self.peers.values()):
            peer.send(self._state_msg(nb))
        return nb

    # ------------------------------------------------------------------ actions
    async def _action(self, peer: Peer, msg: dict) -> None:
        name, rid, args = msg.get("name"), msg.get("id"), msg.get("args") or {}
        try:
            result = await self.run_action(name, args, peer)
            peer.send({"t": "result", "id": rid, "ok": True, "result": result})
        except Exception as exc:
            if not isinstance(exc, (NoteError, ValueError, KeyError, macbridge.MacBridgeError, RuntimeError,
                                    FileExistsError, DeviceUnavailable)):
                log.exception("pen action %s failed", name)
            text = exc.args[0] if isinstance(exc, KeyError) and exc.args else str(exc)
            peer.send({"t": "result", "id": rid, "ok": False, "error": text or exc.__class__.__name__})

    async def run_action(self, name: str, args: dict, peer: Peer | None = None) -> Any:
        role = peer.role if peer else "mac"
        if name == "list_notebooks":
            return self.store.list()
        if name == "open":
            nb = self.switch(args["notebook"], args.get("page"))
            return nb.info()
        if name == "new_notebook":
            nb = self.store.create(args.get("name") or self.store.unique_name("Notes"), args.get("template", "blank"))
            self.switch(nb.id)
            return nb.info()
        if name == "checkpoint":
            nb = self.notebook()
            entry = nb.checkpoint(args.get("name", ""), role)
            self._broadcast_entry(nb, entry)
            return entry.summary()
        if name == "clipboard":
            png = self.render_png(self.notebook(), args.get("page"), args.get("ids"), transparent=bool(args.get("ids")))
            await asyncio.to_thread(macbridge.copy_png, png)
            return {"bytes": len(png)}
        if name == "to_text":
            return await self.to_text(args.get("page"), args.get("ids"), args.get("mode"), insert=args.get("insert"),
                                      actor=role)
        if name == "copy_text":
            await asyncio.to_thread(macbridge.copy_text, str(args.get("text", ""))[:100000])
            return {"ok": True}
        if name == "screenshot":
            return await self.screenshot_page(role)
        if name == "send_to_phone":
            return await self.send_to_phone(args.get("page"))
        if name == "save_copy":
            return await self.save_annotated(args.get("page"))
        if name == "sticker_save":
            return self.save_sticker(args.get("name", ""), args.get("page"), args.get("ids"))
        if name == "sticker_list":
            return self.stickers()
        if name == "sticker_insert":
            return self.insert_sticker(args.get("name", ""), args.get("page"), role, args.get("x"), args.get("y"))
        if name == "tablet":
            return self.set_tablet(bool(args.get("on")), args.get("target"))
        if name == "tablet_info":
            return self.tablet_info()
        if name == "shortcut":
            if not self.tablet_on:
                raise RuntimeError("turn on tablet mode first")
            await asyncio.to_thread(macbridge.send_shortcut, str(args.get("keys", "")))
            return {"ok": True}
        if name == "open_privacy":
            macbridge.open_privacy_pane(str(args.get("kind", "")))
            return {"ok": True}
        raise ValueError(f"unknown action {name!r}")

    # ------------------------------------------------------------------ rendering helpers
    def renderer_for(self, nb: Notebook) -> PageRenderer:
        return PageRenderer(nb.asset_path)

    def find_page(self, nb: Notebook, page_id: str | None, state: dict | None = None) -> dict:
        pages = (state or nb.state)["pages"]
        page_id = page_id or self.current.get("page")
        for p in pages:
            if p["id"] == page_id:
                return p
        if page_id:
            raise NoteError("that page doesn't exist")
        return pages[0]

    def render_png(self, nb: Notebook, page_id: str | None, ids: list[str] | None = None, *, transparent: bool = False,
                   scale: float = 2.0, state: dict | None = None) -> bytes:
        page = self.find_page(nb, page_id, state)
        renderer = self.renderer_for(nb)
        if ids:
            wanted = [i for i in ids if i in page["objs"]]
            if not wanted:
                raise NoteError("nothing selected")
            boxes = [obj_bounds(page["objs"][i]) for i in wanted]
            crop = (min(b[0] for b in boxes) - 12, min(b[1] for b in boxes) - 12,
                    max(b[2] for b in boxes) + 12, max(b[3] for b in boxes) + 12)
            img = renderer.render(page, scale=scale, background=not transparent, objs=wanted, crop=crop)
        else:
            img = renderer.render(page, scale=scale)
        return to_png(img if transparent else flatten(img))

    # ------------------------------------------------------------------ handwriting
    def _ink_png(self, nb: Notebook, page: dict, ids: list[str] | None) -> tuple[bytes, tuple[float, float, float, float]]:
        wanted = [i for i in (ids or page["order"]) if i in page["objs"] and page["objs"][i]["type"] == "stroke"
                  and page["objs"][i]["tool"] != "highlighter"]
        if not wanted:
            raise NoteError("there's no handwriting to read")
        boxes = [obj_bounds(page["objs"][i]) for i in wanted]
        crop = (min(b[0] for b in boxes) - 20, min(b[1] for b in boxes) - 20,
                max(b[2] for b in boxes) + 20, max(b[3] for b in boxes) + 20)
        bare = dict(page, bg=None)  # backgrounds would confuse the reader
        img = self.renderer_for(nb).render(bare, scale=1.5, template=False, objs=wanted, crop=crop)
        return to_png(flatten(img)), crop

    async def to_text(self, page_id: str | None, ids: list[str] | None, mode: str | None = None, *,
                      insert: str | None = None, actor: str = "mac") -> dict:
        nb = self.notebook()
        page = self.find_page(nb, page_id)
        png, crop = self._ink_png(nb, page, ids)
        result = await handwriting.recognize(png, mode or self.settings["hw_mode"], self.settings["gemma_model"])
        out = result.to_dict()
        if insert in ("below", "replace") and result.text:
            text_obj = {"id": new_id(), "type": "text", "text": result.text, "color": "#1d1d1f",
                        "x": crop[0] + 20, "y": crop[3] + 10 if insert == "below" else crop[1] + 20,
                        "size": 28, "w": max(200, min(page["w"] - crop[0], crop[2] - crop[0]))}
            ops: list[dict] = [{"op": "add", "page": page["id"], "objs": [text_obj]}]
            if insert == "replace":
                strokes = [i for i in (ids or page["order"]) if page["objs"].get(i, {}).get("type") == "stroke"]
                ops.insert(0, {"op": "remove", "page": page["id"], "ids": strokes})
            op = ops[0] if len(ops) == 1 else {"op": "batch", "ops": ops}
            entry = nb.apply(op, actor=actor, label="Turned handwriting into text")
            self._broadcast_entry(nb, entry)
        return out

    # ------------------------------------------------------------------ pictures
    @staticmethod
    def normalize_picture(data: bytes) -> tuple[bytes, str, int, int]:
        """Any picture Pillow can read (incl. HEIC) -> PNG (if it has transparency) or JPEG."""
        from . import thumbs  # noqa: F401  (registers the HEIC opener)
        try:
            with Image.open(io.BytesIO(data)) as im:
                im.load()
                from PIL import ImageOps
                im = ImageOps.exif_transpose(im)
                if max(im.size) > 6000:
                    im.thumbnail((6000, 6000), Image.LANCZOS)
                out = io.BytesIO()
                if im.mode in ("RGBA", "LA", "P") and (im.mode != "P" or "transparency" in im.info):
                    im.convert("RGBA").save(out, "PNG")
                    ext = "png"
                else:
                    im.convert("RGB").save(out, "JPEG", quality=92)
                    ext = "jpg"
                return out.getvalue(), ext, im.size[0], im.size[1]
        except Exception as exc:
            raise NoteError(f"that file isn't a picture droidxfer can read ({exc.__class__.__name__})") from None

    def add_picture_page(self, nb: Notebook, data: bytes, actor: str, origin: dict | None = None,
                         label: str = "Added a picture to annotate") -> dict:
        blob, ext, w, h = self.normalize_picture(data)
        asset = nb.add_asset(blob, ext)
        width = 1000.0
        height = max(100.0, min(40000.0, round(width * h / w, 2)))
        page = blank_page("blank", width, height)
        page["bg"] = {"asset": asset}
        current = self.current.get("page")
        index = next((i + 1 for i, p in enumerate(nb.state["pages"]) if p["id"] == current), len(nb.state["pages"]))
        entry = nb.apply({"op": "page_add", "page": page, "index": index}, actor=actor, label=label)
        if origin:
            origins = dict(nb.meta.get("origins", {}))
            origins[page["id"]] = origin
            nb.set_meta(origins=origins)
        self._broadcast_entry(nb, entry)
        self.current["page"] = page["id"]
        self.broadcast({"t": "page", "page": page["id"]})
        self._remember()
        return {"page": page["id"], "asset": asset}

    def insert_picture(self, nb: Notebook, data: bytes, page_id: str | None, actor: str) -> dict:
        blob, ext, w, h = self.normalize_picture(data)
        asset = nb.add_asset(blob, ext)
        page = self.find_page(nb, page_id)
        scale = min(1.0, page["w"] * 0.6 / w, page["h"] * 0.6 / h)
        obj = {"id": new_id(), "type": "image", "asset": asset, "w": round(w * scale, 2), "h": round(h * scale, 2),
               "x": round((page["w"] - w * scale) / 2, 2), "y": round((page["h"] - h * scale) / 2, 2)}
        entry = nb.apply({"op": "add", "page": page["id"], "objs": [obj]}, actor=actor)
        self._broadcast_entry(nb, entry)
        return {"id": obj["id"], "asset": asset}

    async def screenshot_page(self, actor: str) -> dict:
        png = await asyncio.to_thread(macbridge.interactive_screenshot)
        nb = self.notebook()
        return self.add_picture_page(nb, png, actor, {"kind": "screenshot", "time": time.time()},
                                     label="Added a Mac screenshot to annotate")

    # ------------------------------------------------------------------ saving copies
    def _staging(self) -> Path:
        folder = cache_dir() / "uploads" / secrets.token_hex(8)
        folder.mkdir(parents=True, exist_ok=True)
        return folder

    def _page_title(self, nb: Notebook, page_id: str) -> str:
        index = next(i for i, p in enumerate(nb.state["pages"]) if p["id"] == page_id)
        return f"{nb.name} - page {index + 1}"

    async def send_to_phone(self, page_id: str | None) -> dict:
        engine, remote = self.engine, self.remote
        if not self.phone:
            raise RuntimeError("connect the phone first")
        nb = self.notebook()
        page = self.find_page(nb, page_id)
        folder = self._staging()
        target = folder / f"{_safe_file(self._page_title(nb, page['id']))}.png"
        target.write_bytes(self.render_png(nb, page["id"]))
        dest = "/sdcard/Pictures/droidxfer Notes"
        await remote.mkdirs(self.phone["device_id"], [dest])
        job = engine.submit(self.phone["device_id"], "push", [str(target)], dest, conflict="rename", cleanup=[str(folder)])
        return {"job": job.id, "dest": dest, "name": target.name}

    async def save_annotated(self, page_id: str | None) -> dict:
        nb = self.notebook()
        page = self.find_page(nb, page_id)
        origin = nb.meta.get("origins", {}).get(page["id"])
        folder = self._staging()
        png = self.render_png(nb, page["id"])
        if not origin or origin.get("kind") not in ("mac", "android"):
            dest = self.local.root / "Pictures" / "droidxfer Notes"
            dest.mkdir(parents=True, exist_ok=True)
            origin = {"kind": "mac", "path": str(dest / f"{_safe_file(self._page_title(nb, page['id']))}.png")}
        stem = Path(origin["path"]).stem
        target = folder / f"{_safe_file(stem)} (annotated).png"
        target.write_bytes(png)
        parent = str(Path(origin["path"]).parent) if origin["kind"] == "mac" else origin["path"].rsplit("/", 1)[0] or "/"
        if origin["kind"] == "mac":
            placed = await asyncio.to_thread(self.local.move_into, [target], parent, "rename")
            shutil.rmtree(folder, ignore_errors=True)
            return {"where": "mac", "paths": placed}
        job = self.engine.submit(origin["device_id"], "push", [str(target)], parent, conflict="rename", cleanup=[str(folder)])
        return {"where": "android", "job": job.id, "dest": parent}

    # ------------------------------------------------------------------ stickers (signatures etc.)
    def _sticker_dir(self) -> Path:
        return self.store.root / ".stickers"

    def save_sticker(self, name: str, page_id: str | None, ids: list[str] | None) -> dict:
        name = valid_name(name)
        if not ids:
            raise NoteError("select what to save first (lasso)")
        png = self.render_png(self.notebook(), page_id, ids, transparent=True)
        folder = self._sticker_dir()
        folder.mkdir(parents=True, exist_ok=True)
        (folder / f"{name}.png").write_bytes(png)
        return {"name": name}

    def stickers(self) -> list[dict]:
        folder = self._sticker_dir()
        if not folder.is_dir():
            return []
        return [{"name": f.stem, "mtime": f.stat().st_mtime} for f in sorted(folder.glob("*.png"))]

    def sticker_path(self, name: str) -> Path:
        path = self._sticker_dir() / f"{valid_name(name)}.png"
        if not path.is_file():
            raise FileNotFoundError("no such signature or sticker")
        return path

    def insert_sticker(self, name: str, page_id: str | None, actor: str, x: Any = None, y: Any = None) -> dict:
        data = self.sticker_path(name).read_bytes()
        nb = self.notebook()
        with Image.open(io.BytesIO(data)) as im:
            w, h = im.size
        asset = nb.add_asset(data, "png")
        page = self.find_page(nb, page_id)
        scale = min(0.5, page["w"] * 0.5 / w)  # stickers are saved at 2x: half size is their real size
        ow, oh = w * scale, h * scale
        ox = x if isinstance(x, (int, float)) else (page["w"] - ow) / 2
        oy = y if isinstance(y, (int, float)) else (page["h"] - oh) / 2
        obj = {"id": new_id(), "type": "image", "asset": asset, "x": round(ox, 2), "y": round(oy, 2),
               "w": round(ow, 2), "h": round(oh, 2)}
        entry = nb.apply({"op": "add", "page": page["id"], "objs": [obj]}, actor=actor, label=f"Inserted “{name}”")
        self._broadcast_entry(nb, entry)
        return {"id": obj["id"]}

    # ------------------------------------------------------------------ search index
    def _text_file(self, nb: Notebook, page_id: str) -> Path:
        return nb.folder / "text" / f"{page_id}.json"

    async def index_page(self, nb: Notebook, page_id: str) -> str:
        page = self.find_page(nb, page_id)
        try:
            png, _ = self._ink_png(nb, page, None)
            lines = await asyncio.to_thread(macbridge.vision_recognize, png)
            text = "\n".join(l.text for l in lines)
        except NoteError:
            text = ""
        path = self._text_file(nb, page_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"seq": nb.seq, "text": text}))
        return text

    async def _index_loop(self) -> None:
        if not macbridge.vision_available():
            return
        await asyncio.sleep(5)
        try:  # catch up on pages changed while droidxfer wasn't running
            for info in self.store.list():
                if info.get("exclude_search"):
                    continue
                nb = self.store.get(info["id"])
                for page in nb.state["pages"]:
                    path = self._text_file(nb, page["id"])
                    if not path.exists() or (nb.entries and path.stat().st_mtime < nb.entries[-1].t):
                        self._index_dirty[(nb.id, page["id"])] = 0.0
        except Exception:
            log.exception("search index scan failed")
        while True:
            await asyncio.sleep(1.5)
            now = time.monotonic()
            ready = [k for k, t in self._index_dirty.items() if now - t >= INDEX_IDLE]
            for key in ready[:5]:
                self._index_dirty.pop(key, None)
                try:
                    nb = self.store.get(key[0])
                    if nb.meta.get("exclude_search"):
                        continue
                    await self.index_page(nb, key[1])
                except (KeyError, NoteError):
                    continue
                except Exception as exc:
                    log.warning("couldn't index handwriting on %s: %s", key, exc)

    def search(self, query: str, limit: int = 50) -> list[dict]:
        words = [w.lower() for w in query.split() if w.strip()]
        if not words:
            return []
        hits = []
        for info in self.store.list():
            if info.get("exclude_search"):
                continue
            nb = self.store.get(info["id"])
            for index, page in enumerate(nb.state["pages"]):
                texts = []
                path = self._text_file(nb, page["id"])
                if path.exists():
                    try:
                        texts.append(json.loads(path.read_text()).get("text", ""))
                    except ValueError:
                        pass
                texts += [o["text"] for o in page["objs"].values() if o["type"] == "text"]
                haystack = "\n".join(texts)
                low = haystack.lower()
                if all(w in low for w in words):
                    at = low.find(words[0])
                    start = max(0, at - 60)
                    snippet = haystack[start: at + 100].replace("\n", " ")
                    hits.append({"notebook": nb.id, "name": nb.name, "page": page["id"], "page_no": index + 1,
                                 "snippet": ("…" if start else "") + snippet})
                    if len(hits) >= limit:
                        return hits
        return hits

    # ------------------------------------------------------------------ tablet mode
    def tablet_info(self) -> dict:
        info: dict[str, Any] = {"on": self.tablet_on, "allowed": macbridge.accessibility_allowed(),
                                "host_app": macbridge.host_app_name(), "target": self.settings["tablet_target"],
                                "shortcuts": self.settings["shortcuts"]}
        try:
            info["displays"] = macbridge.displays()
            info["front"] = macbridge.front_window()
        except macbridge.MacBridgeError as exc:
            info["error"] = str(exc)
        info["rect"] = self._tablet_rect()
        info["profile"] = self._profile(info.get("front"))
        return info

    def _profile(self, front: dict | None) -> str:
        app = (front or {}).get("app", "")
        for name in self.settings["shortcuts"]:
            if name != "Any app" and app.lower().startswith(name.lower()):
                return name
        return "Any app"

    def _tablet_rect(self) -> dict | None:
        target = self.settings["tablet_target"] or {}
        try:
            if target.get("kind") == "window":
                win = macbridge.front_window()
                if win:
                    return {"x": win["x"], "y": win["y"], "w": win["w"], "h": win["h"], "label": win["app"]}
            screens = macbridge.displays()
        except macbridge.MacBridgeError:
            return None
        chosen = next((d for d in screens if d["id"] == target.get("id")), screens[0] if screens else None)
        if not chosen:
            return None
        return {"x": chosen["x"], "y": chosen["y"], "w": chosen["w"], "h": chosen["h"], "label": chosen["name"]}

    def set_tablet(self, on: bool, target: dict | None = None) -> dict:
        if target is not None:
            if not isinstance(target, dict) or target.get("kind") not in ("display", "window"):
                raise ValueError("target must be a display or the front window")
            self.settings.update(tablet_target={"kind": target["kind"], "id": target.get("id")})
        if on:
            if macbridge.accessibility_allowed() is False:
                raise RuntimeError(
                    f"macOS needs to allow {macbridge.host_app_name()} to control the computer: System Settings "
                    "› Privacy & Security › Accessibility. Turn it on, then restart droidxfer.")
            self.tablet = self.tablet or macbridge.TabletInjector()
        elif self.tablet_on:
            self._tablet_leave()
        self.tablet_on = on
        self.tablet_rect = self._tablet_rect() if on else None
        info = self.tablet_info()
        self.broadcast({"t": "tablet", "info": info})
        return info

    def _tablet_leave(self) -> None:
        if self.tablet:
            try:
                self.tablet.sample("leave", 0, 0)
            except Exception:
                pass

    def _tablet_samples(self, events: list) -> None:
        if not self.tablet_on or not self.tablet or not self.tablet_rect:
            return
        rect = self.tablet_rect
        for ev in events[:500]:
            if not isinstance(ev, dict):
                continue
            kind = ev.get("k")
            if kind not in ("hover", "down", "move", "up", "leave"):
                continue
            try:
                nx = min(max(float(ev.get("x", 0)), 0.0), 1.0)
                ny = min(max(float(ev.get("y", 0)), 0.0), 1.0)
                p = min(max(float(ev.get("p", 0)), 0.0), 1.0)
                tilt = (min(max(float(ev.get("tx", 0)), -1.0), 1.0), min(max(float(ev.get("ty", 0)), -1.0), 1.0))
            except (TypeError, ValueError):
                continue
            x, y = rect["x"] + nx * rect["w"], rect["y"] + ny * rect["h"]
            try:
                self.tablet.sample(kind, x, y, p, tilt, bool(ev.get("side")))
            except Exception as exc:
                log.warning("tablet event failed: %s", exc)
                return


def _safe_file(name: str) -> str:
    return re.sub(r'[/\\:\x00-\x1f]', "-", name).strip(". ") or "page"
