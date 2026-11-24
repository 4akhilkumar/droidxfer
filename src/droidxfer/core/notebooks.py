"""S Pen notebooks: pages of strokes, stored as an append-only history of changes.

Every change is one entry in `history.jsonl` (an *op* plus its *inverse*), so any earlier version can be
rebuilt, previewed, compared or restored, and undo/redo is just "apply the inverse / apply it again" as a
new entry: nothing is ever rewritten or lost. A full copy of the notebook is saved every
`SNAPSHOT_EVERY` changes so opening an old version never replays more than that many entries.

Folder layout (one folder per notebook under the notes folder):
    notebook.json          name, created time, options
    history.jsonl          one JSON entry per line
    snapshots/<seq>.json   state after entry <seq>
    assets/<sha>.<ext>     pictures used as backgrounds or inserted images
    text/<page>.json       handwriting turned into text, for search
"""

from __future__ import annotations

import copy
import hashlib
import json
import logging
import math
import os
import re
import secrets
import shutil
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from .config import app_dir

log = logging.getLogger(__name__)

SNAPSHOT_EVERY = 500
SESSION_GAP = 20 * 60  # seconds without changes that start a new session in the history
MAX_POINTS = 20000
MAX_OBJS_PER_OP = 5000
TOOLS = ("pen", "pencil", "highlighter")
TEMPLATES = ("blank", "ruled", "grid", "dot")
DEFAULT_PAGE = (1000, 1414)  # A4 portrait, in page units
_ID = re.compile(r"^[A-Za-z0-9_-]{1,40}$")
_COLOR = re.compile(r"^#[0-9a-fA-F]{6}$")
_ASSET = re.compile(r"^[0-9a-f]{40}\.(png|jpg)$")
_BAD_NAME = re.compile(r'[/\\:\x00-\x1f]')


class NoteError(ValueError):
    """A change or request that can't be applied (bad data, missing page...)."""


def notes_root() -> Path:
    if override := os.environ.get("DROIDXFER_NOTES"):
        return Path(override)
    if home := os.environ.get("DROIDXFER_HOME"):  # tests relocate everything
        return Path(home) / "Notes"
    return Path.home() / "Documents" / "droidxfer Notes"


def new_id() -> str:
    return secrets.token_urlsafe(9).replace("-", "a").replace("_", "b")


# ------------------------------------------------------------------ validation
def _num(value: Any, lo: float, hi: float, what: str, digits: int = 2) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise NoteError(f"{what} must be a number")
    if not lo <= value <= hi:
        raise NoteError(f"{what} out of range")
    return round(float(value), digits)


def _ident(value: Any, what: str = "id") -> str:
    if not isinstance(value, str) or not _ID.match(value):
        raise NoteError(f"invalid {what}")
    return value


def _ids(value: Any) -> list[str]:
    if not isinstance(value, list) or len(value) > MAX_OBJS_PER_OP or not all(isinstance(i, str) for i in value):
        raise NoteError("ids must be a list of object ids")
    return value


def _color(value: Any) -> str:
    if not isinstance(value, str) or not _COLOR.match(value):
        raise NoteError("color must look like #rrggbb")
    return value.lower()


def clean_obj(raw: Any) -> dict:
    if not isinstance(raw, dict):
        raise NoteError("object must be a JSON object")
    kind = raw.get("type")
    oid = _ident(raw.get("id"))
    if kind == "stroke":
        tool = raw.get("tool")
        if tool not in TOOLS:
            raise NoteError("unknown tool")
        pts = raw.get("pts")
        if not isinstance(pts, list) or not 1 <= len(pts) <= MAX_POINTS:
            raise NoteError("a stroke needs 1 to 20000 points")
        clean = []
        for p in pts:
            if not isinstance(p, list) or len(p) not in (3, 4):
                raise NoteError("each point is [x, y, pressure] or [x, y, pressure, tilt]")
            point = [_num(p[0], -1e5, 1e5, "x"), _num(p[1], -1e5, 1e5, "y"), _num(p[2], 0, 1, "pressure", 3)]
            if len(p) == 4:
                point.append(_num(p[3], 0, 1, "tilt", 3))
            clean.append(point)
        return {"id": oid, "type": "stroke", "tool": tool, "color": _color(raw.get("color")),
                "size": _num(raw.get("size"), 0.2, 80, "size"), "pts": clean}
    if kind == "image":
        asset = raw.get("asset")
        if not isinstance(asset, str) or not _ASSET.match(asset):
            raise NoteError("invalid image asset")
        return {"id": oid, "type": "image", "asset": asset,
                "x": _num(raw.get("x"), -1e5, 1e5, "x"), "y": _num(raw.get("y"), -1e5, 1e5, "y"),
                "w": _num(raw.get("w"), 1, 1e5, "w"), "h": _num(raw.get("h"), 1, 1e5, "h")}
    if kind == "text":
        text = raw.get("text")
        if not isinstance(text, str) or len(text) > 20000:
            raise NoteError("invalid text")
        return {"id": oid, "type": "text", "text": text, "color": _color(raw.get("color", "#1d1d1f")),
                "x": _num(raw.get("x"), -1e5, 1e5, "x"), "y": _num(raw.get("y"), -1e5, 1e5, "y"),
                "size": _num(raw.get("size", 28), 4, 400, "size"),
                "w": _num(raw.get("w", 600), 10, 1e5, "w")}
    raise NoteError("unknown object type")


def _clean_bg(raw: Any) -> dict | None:
    if raw is None:
        return None
    if not isinstance(raw, dict) or not isinstance(raw.get("asset"), str) or not _ASSET.match(raw["asset"]):
        raise NoteError("invalid background")
    return {"asset": raw["asset"]}


def clean_page(raw: Any) -> dict:
    if not isinstance(raw, dict):
        raise NoteError("page must be a JSON object")
    template = raw.get("template", "blank")
    if template not in TEMPLATES:
        raise NoteError("unknown page template")
    objs = raw.get("objs", {})
    order = raw.get("order", [])
    if not isinstance(objs, dict) or not isinstance(order, list):
        raise NoteError("invalid page contents")
    clean_objs = {}
    for oid in order:
        if oid not in objs:
            raise NoteError("page order lists a missing object")
        clean_objs[oid] = clean_obj(objs[oid])
    return {"id": _ident(raw.get("id")), "w": _num(raw.get("w", DEFAULT_PAGE[0]), 100, 20000, "w"),
            "h": _num(raw.get("h", DEFAULT_PAGE[1]), 100, 40000, "h"), "template": template,
            "bg": _clean_bg(raw.get("bg")), "objs": clean_objs, "order": list(clean_objs)}


def blank_page(template: str = "blank", w: float = DEFAULT_PAGE[0], h: float = DEFAULT_PAGE[1]) -> dict:
    return {"id": new_id(), "w": w, "h": h, "template": template, "bg": None, "objs": {}, "order": []}


# ------------------------------------------------------------------ applying ops
def _page(state: dict, page_id: Any) -> dict:
    for page in state["pages"]:
        if page["id"] == page_id:
            return page
    raise NoteError("that page no longer exists")


def _page_index(state: dict, page_id: str) -> int:
    for i, page in enumerate(state["pages"]):
        if page["id"] == page_id:
            return i
    raise NoteError("that page no longer exists")


def _shift(obj: dict, dx: float, dy: float) -> None:
    if obj["type"] == "stroke":
        for p in obj["pts"]:
            p[0] = round(p[0] + dx, 2)
            p[1] = round(p[1] + dy, 2)
    else:
        obj["x"] = round(obj["x"] + dx, 2)
        obj["y"] = round(obj["y"] + dy, 2)


def _text(value: Any) -> str:
    if not isinstance(value, str) or len(value) > 20000:
        raise NoteError("invalid text")
    return value


_UPDATABLE = {"color": _color, "size": lambda v: _num(v, 0.2, 400, "size"), "text": _text,
              "w": lambda v: _num(v, 1, 1e5, "w"), "h": lambda v: _num(v, 1, 1e5, "h")}


def apply_op(state: dict, op: dict, *, lenient: bool = False) -> dict:
    """Apply `op` to `state` in place and return its inverse. Raises NoteError if it doesn't fit.

    `lenient` (used for undo/redo/restore) skips parts that no longer apply, e.g. erasing a stroke
    that was already erased, instead of failing the whole change.
    """
    kind = op.get("op") if isinstance(op, dict) else None
    if kind == "add":
        page = _page(state, op.get("page"))
        objs = op.get("objs")
        if not isinstance(objs, list) or not 1 <= len(objs) <= MAX_OBJS_PER_OP:
            raise NoteError("nothing to add")
        at = op.get("at")
        clean = [clean_obj(raw) for raw in objs]  # validate everything before changing anything
        if len({o["id"] for o in clean}) != len(clean):
            raise NoteError("duplicate object ids")
        if not lenient and any(o["id"] in page["objs"] for o in clean):
            raise NoteError("object already exists")
        added = []
        for i, obj in enumerate(clean):
            if obj["id"] in page["objs"]:
                continue
            page["objs"][obj["id"]] = obj
            pos = at[i] if isinstance(at, list) and i < len(at) and isinstance(at[i], int) else len(page["order"])
            page["order"].insert(min(max(pos, 0), len(page["order"])), obj["id"])
            added.append(obj["id"])
        return {"op": "remove", "page": page["id"], "ids": added}
    if kind == "remove":
        page = _page(state, op.get("page"))
        ids = _ids(op.get("ids"))
        indexed = sorted(((page["order"].index(i), i) for i in ids if i in page["objs"]))
        if len(indexed) != len(set(ids)) and not lenient:
            raise NoteError("some objects no longer exist")
        removed = [page["objs"][i] for _, i in indexed]
        for _, i in indexed:
            del page["objs"][i]
            page["order"].remove(i)
        return {"op": "add", "page": page["id"], "objs": removed, "at": [pos for pos, _ in indexed]}
    if kind == "move":
        page = _page(state, op.get("page"))
        dx, dy = _num(op.get("dx"), -1e5, 1e5, "dx"), _num(op.get("dy"), -1e5, 1e5, "dy")
        wanted = _ids(op.get("ids"))
        ids = [i for i in dict.fromkeys(wanted) if i in page["objs"]]
        if len(ids) != len(set(wanted)) and not lenient:
            raise NoteError("some objects no longer exist")
        for i in ids:
            _shift(page["objs"][i], dx, dy)
        return {"op": "move", "page": page["id"], "ids": ids, "dx": -dx, "dy": -dy}
    if kind == "update":
        page = _page(state, op.get("page"))
        changes = op.get("changes")
        if not isinstance(changes, dict):
            raise NoteError("changes must be an object")
        planned: list[tuple[dict, str, Any]] = []
        for oid, fields in changes.items():
            obj = page["objs"].get(oid)
            if obj is None or not isinstance(fields, dict):
                if lenient:
                    continue
                raise NoteError("some objects no longer exist")
            for key, value in fields.items():
                if key not in _UPDATABLE or key not in obj:
                    raise NoteError(f"can't change {key}")
                planned.append((obj, key, _UPDATABLE[key](value)))
        before: dict[str, dict] = {}
        for obj, key, value in planned:
            before.setdefault(obj["id"], {})[key] = obj[key]
            obj[key] = value
        return {"op": "update", "page": page["id"], "changes": before}
    if kind == "page_add":
        page = clean_page(op.get("page"))
        if any(p["id"] == page["id"] for p in state["pages"]):
            raise NoteError("page already exists")
        index = op.get("index", len(state["pages"]))
        index = min(max(index if isinstance(index, int) else len(state["pages"]), 0), len(state["pages"]))
        state["pages"].insert(index, page)
        return {"op": "page_remove", "id": page["id"]}
    if kind == "page_remove":
        index = _page_index(state, op.get("id"))
        if len(state["pages"]) == 1:
            raise NoteError("a notebook needs at least one page")
        page = state["pages"].pop(index)
        return {"op": "page_add", "page": page, "index": index}
    if kind == "page_move":
        index = _page_index(state, op.get("id"))
        to = op.get("index")
        if not isinstance(to, int):
            raise NoteError("index must be a number")
        page = state["pages"].pop(index)
        state["pages"].insert(min(max(to, 0), len(state["pages"])), page)
        return {"op": "page_move", "id": page["id"], "index": index}
    if kind == "page_update":
        page = _page(state, op.get("id"))
        values: dict[str, Any] = {}
        for key in ("template", "bg", "w", "h"):
            if key not in op:
                continue
            value = op[key]
            if key == "template" and value not in TEMPLATES:
                raise NoteError("unknown page template")
            if key == "bg":
                value = _clean_bg(value)
            if key in ("w", "h"):
                value = _num(value, 100, 40000, key)
            values[key] = value
        prev = {key: page[key] for key in values}
        page.update(values)
        return {"op": "page_update", "id": page["id"], **prev}
    if kind == "page_replace":
        page = clean_page(op.get("page"))
        try:
            index = _page_index(state, page["id"])
        except NoteError:  # restoring a page that was deleted since: put it back
            index = min(op.get("index", len(state["pages"])) if isinstance(op.get("index"), int) else len(state["pages"]), len(state["pages"]))
            state["pages"].insert(index, page)
            return {"op": "page_remove", "id": page["id"]}
        old = state["pages"][index]
        state["pages"][index] = page
        return {"op": "page_replace", "page": old, "index": index}
    if kind == "pages_replace":
        pages = op.get("pages")
        if not isinstance(pages, list) or not pages:
            raise NoteError("a notebook needs at least one page")
        new_pages = [clean_page(p) for p in pages]
        old = state["pages"]
        state["pages"] = new_pages
        return {"op": "pages_replace", "pages": old}
    if kind == "batch":
        ops = op.get("ops")
        if not isinstance(ops, list) or not ops or len(ops) > 200:
            raise NoteError("a batch needs 1 to 200 changes")
        inverses = []
        try:
            for sub in ops:
                if not isinstance(sub, dict) or sub.get("op") in ("batch", "checkpoint"):
                    raise NoteError("invalid change in a batch")
                inverses.append(apply_op(state, sub, lenient=lenient))
        except Exception:
            for inv in reversed(inverses):  # roll back the parts already applied
                apply_op(state, inv, lenient=True)
            raise
        return {"op": "batch", "ops": inverses[::-1]}
    if kind == "checkpoint":
        return {"op": "checkpoint"}
    raise NoteError("unknown change")


def pages_touched(op: dict) -> list[str]:
    kind = op.get("op")
    if kind in ("add", "remove", "move", "update"):
        return [op["page"]]
    if kind in ("page_remove", "page_move", "page_update"):
        return [op["id"]]
    if kind in ("page_add", "page_replace"):
        return [op["page"]["id"]]
    if kind == "pages_replace":
        return [p["id"] for p in op["pages"]]
    if kind == "batch":
        seen: list[str] = []
        for sub in op["ops"]:
            seen += [p for p in pages_touched(sub) if p not in seen]
        return seen
    return []


def _count(n: int, word: str) -> str:
    return f"{n} {word}{'' if n == 1 else 's'}"


def describe(op: dict, state: dict) -> str:
    """A short human summary of a change, e.g. 'Drew a stroke on page 2'."""

    def page_no(page_id: str) -> str:
        for i, p in enumerate(state["pages"]):
            if p["id"] == page_id:
                return f"page {i + 1}"
        return "a page"

    kind = op.get("op")
    if kind == "add":
        objs = op["objs"]
        kinds = {o["type"] for o in objs}
        if kinds == {"stroke"}:
            tools = {o["tool"] for o in objs}
            what = "highlighted" if tools == {"highlighter"} else "drew"
            noun = "a stroke" if len(objs) == 1 else _count(len(objs), "stroke")
            return f"{what.capitalize()} {noun} on {page_no(op['page'])}"
        if kinds == {"image"}:
            return f"Inserted {'a picture' if len(objs) == 1 else _count(len(objs), 'picture')} on {page_no(op['page'])}"
        if kinds == {"text"}:
            return f"Added text on {page_no(op['page'])}"
        return f"Added {_count(len(objs), 'item')} on {page_no(op['page'])}"
    if kind == "remove":
        return f"Erased {_count(len(op['ids']), 'item')} on {page_no(op['page'])}"
    if kind == "move":
        return f"Moved {_count(len(op['ids']), 'item')} on {page_no(op['page'])}"
    if kind == "update":
        keys = {k for fields in op["changes"].values() for k in fields}
        verb = "Recoloured" if keys == {"color"} else "Resized" if keys <= {"size", "w", "h"} else "Edited"
        return f"{verb} {_count(len(op['changes']), 'item')} on {page_no(op['page'])}"
    if kind == "page_add":
        return f"Added {page_no(op['page']['id'])}"
    if kind == "page_remove":
        return f"Deleted {page_no(op['id'])}"
    if kind == "page_move":
        return f"Moved {page_no(op['id'])} to position {op['index'] + 1}"
    if kind == "page_update":
        if "bg" in op:
            return f"Changed the picture behind {page_no(op['id'])}"
        return f"Changed the paper of {page_no(op['id'])}"
    if kind == "page_replace":
        return f"Replaced {page_no(op['page']['id'])}"
    if kind == "pages_replace":
        return "Replaced every page"
    if kind == "batch":
        subs = op["ops"]
        kinds = {s.get("op") for s in subs}
        if kinds == {"page_add", "page_remove"} and len(subs) == 2:  # the only page swapped for a new empty one
            removed = next(s for s in subs if s["op"] == "page_remove")["id"]
            added = next(s for s in subs if s["op"] == "page_add")["page"]
            paper = {"blank": "blank", "ruled": "ruled", "grid": "grid", "dot": "dotted"}.get(added.get("template"), "blank")
            return f"Deleted {page_no(removed)} and started a new {paper} page"
        if kinds <= {"add", "remove"} and len(subs) >= 2:
            page = subs[0].get("page")
            return f"Erased part of {_count(sum(len(s['ids']) for s in subs if s['op'] == 'remove'), 'stroke')} on {page_no(page)}"
        return f"{_count(len(subs), 'change')}"
    if kind == "checkpoint":
        return "Saved version"
    return "Changed the notebook"


# ------------------------------------------------------------------ store
def _atomic_write(path: Path, data: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tmp-")
    with os.fdopen(fd, "w") as fh:
        fh.write(data)
    os.replace(tmp, path)


def valid_name(name: Any) -> str:
    if not isinstance(name, str):
        raise NoteError("name must be text")
    name = " ".join(name.split())
    if not name or len(name) > 80 or name.startswith(".") or _BAD_NAME.search(name):
        raise NoteError("use a name of 1 to 80 characters without / \\ : or a leading dot")
    return name


@dataclass
class Entry:
    seq: int
    t: float
    actor: str
    session: str
    kind: str  # edit | undo | redo | restore | checkpoint | copy
    label: str
    op: dict
    inv: dict
    of: int | None = None
    pages: list[str] = field(default_factory=list)

    def to_json(self) -> dict:
        data = {"seq": self.seq, "t": self.t, "actor": self.actor, "session": self.session, "kind": self.kind,
                "label": self.label, "op": self.op, "inv": self.inv, "pages": self.pages}
        if self.of is not None:
            data["of"] = self.of
        return data

    def summary(self) -> dict:
        data = self.to_json()
        del data["op"], data["inv"]
        if self.kind == "checkpoint":
            data["name"] = self.op.get("name", "")
        return data


class Notebook:
    """One open notebook: current state in memory, history on disk."""

    def __init__(self, folder: Path) -> None:
        self.folder = folder
        self.id = folder.name
        self.meta: dict = json.loads((folder / "notebook.json").read_text())
        self.entries: list[Entry] = []
        self.base_seq = 0  # history before this was compacted away
        self.state: dict = {"pages": []}
        self.undo_stack: list[int] = []
        self.redo_stack: list[int] = []
        self.listeners: list[Callable[[Entry], None]] = []
        self._load()

    # -- loading
    def _load(self) -> None:
        base = self.folder / "snapshots" / "base.json"
        if base.exists():
            data = json.loads(base.read_text())
            self.base_seq, self.state = data["seq"], data["state"]
        history = self.folder / "history.jsonl"
        if history.exists():
            with history.open() as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        raw = json.loads(line)
                    except ValueError:
                        log.warning("notebook %s: skipping a damaged history line", self.id)
                        continue
                    self.entries.append(Entry(raw["seq"], raw["t"], raw.get("actor", "?"), raw.get("session", ""),
                                              raw.get("kind", "edit"), raw.get("label", ""), raw["op"], raw["inv"],
                                              raw.get("of"), raw.get("pages", [])))
        latest = self._latest_snapshot(None)
        if latest is not None and latest[0] > self.base_seq:
            seq, self.state = latest
        else:
            seq = self.base_seq
        for e in self.entries:
            if e.seq > seq:
                apply_op(self.state, e.op, lenient=True)
        if not self.state["pages"]:
            self.state["pages"].append(blank_page(self.meta.get("template", "blank")))
            self._persist_base()

    def _persist_base(self) -> None:
        _atomic_write(self.folder / "snapshots" / "base.json", json.dumps({"seq": self.base_seq, "state": self.state}))

    def _latest_snapshot(self, upto: int | None) -> tuple[int, dict] | None:
        snaps = self.folder / "snapshots"
        best = None
        if snaps.is_dir():
            for f in snaps.glob("*.json"):
                if f.stem.isdigit() and (upto is None or int(f.stem) <= upto) and (best is None or int(f.stem) > best):
                    best = int(f.stem)
        if best is None:
            return None
        return best, json.loads((snaps / f"{best}.json").read_text())

    # -- info
    @property
    def seq(self) -> int:
        return self.entries[-1].seq if self.entries else self.base_seq

    @property
    def name(self) -> str:
        return self.meta.get("name", self.id)

    def info(self) -> dict:
        return {"id": self.id, "name": self.name, "created": self.meta.get("created"),
                "updated": self.entries[-1].t if self.entries else self.meta.get("created"),
                "pages": len(self.state["pages"]), "seq": self.seq,
                "exclude_search": bool(self.meta.get("exclude_search")),
                "first_page": self.state["pages"][0]["id"] if self.state["pages"] else None}

    def snapshot(self) -> dict:
        return {"notebook": self.info(), "pages": self.state["pages"], "seq": self.seq,
                "can_undo": bool(self.undo_stack), "can_redo": bool(self.redo_stack)}

    def set_meta(self, **changes: Any) -> None:
        self.meta.update(changes)
        _atomic_write(self.folder / "notebook.json", json.dumps(self.meta, indent=2))

    # -- changes
    def _append(self, entry: Entry) -> None:
        self.folder.mkdir(parents=True, exist_ok=True)
        with (self.folder / "history.jsonl").open("a") as fh:
            fh.write(json.dumps(entry.to_json(), separators=(",", ":")) + "\n")
            fh.flush()
            os.fsync(fh.fileno())
        self.entries.append(entry)
        if entry.seq % SNAPSHOT_EVERY == 0:
            _atomic_write(self.folder / "snapshots" / f"{entry.seq}.json", json.dumps(self.state))
        for listener in list(self.listeners):
            try:
                listener(entry)
            except Exception:  # a listener must never break saving
                log.exception("notebook listener failed")

    def _session_for(self, actor_session: str) -> str:
        last = self.entries[-1] if self.entries else None
        if last and time.time() - last.t < SESSION_GAP:
            return last.session
        return actor_session or new_id()

    def apply(self, op: dict, *, actor: str, session: str = "", kind: str = "edit", of: int | None = None,
              lenient: bool = False, label: str | None = None, track_undo: bool = True) -> Entry:
        try:
            # describe() needs the page numbers before e.g. a page is deleted
            summary = label or describe(op, self.state)
        except Exception:
            summary = "Changed the notebook"
        try:
            inv = apply_op(self.state, op, lenient=lenient)
        except NoteError:
            raise
        except Exception as exc:  # malformed op that slipped past validation: never half-apply
            log.warning("rejected a malformed change: %s", exc)
            raise NoteError(f"invalid change: {exc.__class__.__name__}") from None
        stored = _stored_op(op, self.state)
        entry = Entry(self.seq + 1, round(time.time(), 3), actor, self._session_for(session), kind, summary,
                      stored, inv, of, pages_touched(stored) or pages_touched(inv))
        self._append(entry)
        if track_undo and kind in ("edit", "restore", "copy") and op.get("op") != "checkpoint":
            self.undo_stack.append(entry.seq)
            del self.undo_stack[:-500]
            self.redo_stack.clear()
        return entry

    def entry(self, seq: int) -> Entry:
        lo, hi = 0, len(self.entries) - 1
        while lo <= hi:
            mid = (lo + hi) // 2
            s = self.entries[mid].seq
            if s == seq:
                return self.entries[mid]
            if s < seq:
                lo = mid + 1
            else:
                hi = mid - 1
        raise NoteError("that version is not in the history")

    def undo(self, actor: str, session: str = "") -> Entry | None:
        while self.undo_stack:
            seq = self.undo_stack.pop()
            target = self.entry(seq)
            try:
                entry = self.apply(target.inv, actor=actor, session=session, kind="undo", of=seq, lenient=True,
                                   label=f"Undo: {target.label}", track_undo=False)
            except NoteError:
                continue  # the change was overtaken (e.g. its page was deleted): skip to the one before
            self.redo_stack.append(seq)
            return entry
        return None

    def redo(self, actor: str, session: str = "") -> Entry | None:
        while self.redo_stack:
            seq = self.redo_stack.pop()
            target = self.entry(seq)
            try:
                entry = self.apply(target.op, actor=actor, session=session, kind="redo", of=seq, lenient=True,
                                   label=f"Redo: {target.label}", track_undo=False)
            except NoteError:
                continue
            self.undo_stack.append(entry.seq)
            return entry
        return None

    # -- history
    def state_at(self, seq: int) -> dict:
        if seq < self.base_seq:
            raise NoteError("that version was removed when the history was compacted")
        if seq >= self.seq:
            return copy.deepcopy(self.state)
        snap = self._latest_snapshot(seq)
        if snap is not None and snap[0] >= self.base_seq:
            start, state = snap
        else:
            base = self.folder / "snapshots" / "base.json"
            if base.exists():
                data = json.loads(base.read_text())
                start, state = data["seq"], data["state"]
            else:
                start, state = 0, {"pages": []}
        for e in self.entries:
            if start < e.seq <= seq:
                apply_op(state, e.op, lenient=True)
        if not state["pages"]:  # before the first page was created
            state["pages"] = [blank_page()]
        return state

    def history(self, before: int | None = None, limit: int = 200) -> dict:
        items = [e for e in self.entries if before is None or e.seq < before]
        page = items[-limit:]
        return {"entries": [e.summary() for e in reversed(page)], "more": len(items) > len(page),
                "base_seq": self.base_seq, "seq": self.seq}

    def checkpoints(self) -> list[dict]:
        return [e.summary() for e in reversed(self.entries) if e.kind == "checkpoint"]

    def checkpoint(self, name: str, actor: str, session: str = "") -> Entry:
        name = valid_name(name)
        return self.apply({"op": "checkpoint", "name": name}, actor=actor, session=session, kind="checkpoint",
                          label=f"Saved version “{name}”", track_undo=False)

    def restore(self, seq: int, page_id: str | None, actor: str, session: str = "") -> Entry:
        old = self.state_at(seq)
        when = time.strftime("%d %b %H:%M", time.localtime(self.entry(seq).t)) if seq > self.base_seq else "the start"
        if page_id:
            page = next((p for p in old["pages"] if p["id"] == page_id), None)
            if page is None:
                raise NoteError("that page didn't exist in that version")
            index = next(i for i, p in enumerate(old["pages"]) if p["id"] == page_id)
            label = f"Restored a page to the version from {when} (#{seq})"
            return self.apply({"op": "page_replace", "page": page, "index": index}, actor=actor, session=session,
                              kind="restore", of=seq, label=label)
        return self.apply({"op": "pages_replace", "pages": old["pages"]}, actor=actor, session=session,
                          kind="restore", of=seq, label=f"Restored the notebook to the version from {when} (#{seq})")

    def ops_between(self, start: int, end: int, page_id: str | None = None) -> list[dict]:
        out = []
        for e in self.entries:
            if start < e.seq <= end and (page_id is None or page_id in e.pages):
                out.append({"seq": e.seq, "t": e.t, "op": e.op, "label": e.label})
        return out

    def compact(self, older_than_days: float) -> int:
        """Forget history entries older than N days (the notebook's content is kept). Returns entries removed."""
        cutoff = time.time() - older_than_days * 86400
        old = [e for e in self.entries if e.t < cutoff]
        if not old:
            return 0
        keep_from = old[-1].seq
        state = self.state_at(keep_from)
        self.base_seq = keep_from
        _atomic_write(self.folder / "snapshots" / "base.json", json.dumps({"seq": keep_from, "state": state}))
        self.entries = [e for e in self.entries if e.seq > keep_from]
        _atomic_write(self.folder / "history.jsonl",
                      "".join(json.dumps(e.to_json(), separators=(",", ":")) + "\n" for e in self.entries))
        for f in (self.folder / "snapshots").glob("*.json"):
            if f.stem.isdigit() and int(f.stem) <= keep_from:
                f.unlink()
        self.undo_stack = [s for s in self.undo_stack if s > keep_from]
        self.redo_stack = [s for s in self.redo_stack if s > keep_from]
        return len(old)

    # -- assets
    def add_asset(self, data: bytes, ext: str) -> str:
        if ext not in ("png", "jpg"):
            raise NoteError("pictures are stored as png or jpg")
        name = f"{hashlib.sha1(data).hexdigest()}.{ext}"
        path = self.folder / "assets" / name
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        return name

    def asset_path(self, name: str) -> Path:
        if not _ASSET.match(name):
            raise NoteError("invalid asset name")
        path = self.folder / "assets" / name
        if not path.is_file():
            raise FileNotFoundError("picture not found")
        return path


def _stored_op(op: dict, state: dict) -> dict:
    """The op as saved: objects/pages re-read from the (validated) state so the log never holds raw input."""
    kind = op.get("op")
    if kind == "add":
        page = _page(state, op["page"])
        ids = [o.get("id") for o in op["objs"] if isinstance(o, dict)]
        stored = {"op": "add", "page": op["page"], "objs": [copy.deepcopy(page["objs"][i]) for i in ids if i in page["objs"]]}
        if isinstance(op.get("at"), list):
            stored["at"] = [a for a in op["at"] if isinstance(a, int)]
        return stored
    if kind in ("page_add", "page_replace"):
        page = next((p for p in state["pages"] if p["id"] == op["page"].get("id")), None)
        stored = {"op": kind, "page": copy.deepcopy(page) if page else op["page"]}
        if isinstance(op.get("index"), int):
            stored["index"] = op["index"]
        return stored
    if kind == "pages_replace":
        return {"op": kind, "pages": copy.deepcopy(state["pages"])}
    if kind == "batch":
        return {"op": "batch", "ops": [_stored_simple(s) for s in op["ops"]]}
    return _stored_simple(op)


def _stored_simple(op: dict) -> dict:
    allowed = {
        "add": ("page", "objs", "at"), "remove": ("page", "ids"), "move": ("page", "ids", "dx", "dy"),
        "update": ("page", "changes"), "page_add": ("page", "index"), "page_remove": ("id",),
        "page_move": ("id", "index"), "page_update": ("id", "template", "bg", "w", "h"),
        "page_replace": ("page", "index"), "pages_replace": ("pages",), "checkpoint": ("name",),
    }
    keys = allowed.get(op.get("op"), ())
    data = {"op": op["op"], **{k: copy.deepcopy(op[k]) for k in keys if k in op}}
    if data["op"] == "add":
        data["objs"] = [clean_obj(o) for o in data["objs"]]
    return data


class NotebookStore:
    def __init__(self, root: Path | None = None) -> None:
        self._root = root
        self.open: dict[str, Notebook] = {}

    @property
    def root(self) -> Path:
        return self._root or notes_root()

    def _folder(self, notebook_id: str) -> Path:
        notebook_id = valid_name(notebook_id)
        return self.root / notebook_id

    def list(self) -> list[dict]:
        out = []
        if self.root.is_dir():
            for folder in self.root.iterdir():
                if folder.is_dir() and (folder / "notebook.json").is_file():
                    try:
                        out.append(self.get(folder.name).info())
                    except Exception as exc:
                        log.warning("can't open notebook %s: %s", folder.name, exc)
        out.sort(key=lambda n: n.get("updated") or 0, reverse=True)
        return out

    def get(self, notebook_id: str) -> Notebook:
        if notebook_id in self.open:
            return self.open[notebook_id]
        folder = self._folder(notebook_id)
        if not (folder / "notebook.json").is_file():
            raise KeyError(f"notebook {notebook_id!r} not found")
        nb = Notebook(folder)
        self.open[notebook_id] = nb
        return nb

    def create(self, name: str, template: str = "blank") -> Notebook:
        name = valid_name(name)
        if template not in TEMPLATES:
            raise NoteError("unknown page template")
        folder = self.root / name
        if folder.exists():
            raise FileExistsError(f"a notebook called {name!r} already exists")
        folder.mkdir(parents=True)
        _atomic_write(folder / "notebook.json", json.dumps({"name": name, "created": time.time(), "template": template,
                                                             "format": 1}, indent=2))
        return self.get(name)

    def unique_name(self, base: str) -> str:
        name, n = base, 2
        while (self.root / name).exists():
            name, n = f"{base} {n}", n + 1
        return name

    def rename(self, notebook_id: str, new_name: str) -> Notebook:
        new_name = valid_name(new_name)
        nb = self.get(notebook_id)
        if new_name == nb.id:
            return nb
        target = self.root / new_name
        if target.exists():
            raise FileExistsError(f"a notebook called {new_name!r} already exists")
        nb.folder.rename(target)
        del self.open[notebook_id]
        renamed = Notebook(target)
        renamed.undo_stack, renamed.redo_stack, renamed.listeners = nb.undo_stack, nb.redo_stack, nb.listeners
        renamed.set_meta(name=new_name)
        self.open[new_name] = renamed
        return renamed

    def delete(self, notebook_id: str) -> None:
        """Move a notebook folder to the Trash (or ~/.Trash), never a permanent delete."""
        nb = self.get(notebook_id)
        self.open.pop(notebook_id, None)
        trash = Path.home() / ".Trash"
        if os.environ.get("DROIDXFER_HOME"):
            trash = Path(os.environ["DROIDXFER_HOME"]) / "Trash"
        trash.mkdir(parents=True, exist_ok=True)
        dest = trash / nb.folder.name
        n = 2
        while dest.exists():
            dest = trash / f"{nb.folder.name} {n}"
            n += 1
        shutil.move(str(nb.folder), dest)

    def default(self) -> Notebook:
        existing = self.list()
        if existing:
            return self.get(existing[0]["id"])
        return self.create("My notes", "blank")


def settings_path() -> Path:
    return app_dir() / "pen.json"
