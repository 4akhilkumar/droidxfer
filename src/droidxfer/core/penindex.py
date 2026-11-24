"""Background handwriting reading for "Search all notebooks".

Two passes per page, so search is quick *and* accurate:

1. **Vision** (Apple Vision, a fraction of a second) a few seconds after you stop writing, so new writing is
   searchable almost at once.
2. **Gemma** (Ollama, several seconds a page) when the engine setting asks for it: always with "gemma", and
   with "auto" when Vision wasn't confident. It runs one page at a time, only after the page has been left
   alone for a while, only while the Mac is plugged in, and steps aside while you read a page yourself.

Each page's results live in ``<notebook>/text/<page>.json`` with a fingerprint of the handwriting they were
read from, so unchanged pages are never read twice. Search looks in both texts.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import time
import unicodedata
from pathlib import Path
from typing import TYPE_CHECKING, Any

from . import handwriting, macbridge
from .notebooks import NoteError, Notebook

if TYPE_CHECKING:
    from .penpad import PenHub

log = logging.getLogger(__name__)

VISION_IDLE = 4.0  # seconds after the last change before Vision reads a page
GEMMA_IDLE = 20.0  # seconds a page must be left alone before Gemma reads it
OLLAMA_RETRY = 300.0  # seconds to wait after Ollama wasn't reachable
POWER_CHECK = 60.0  # seconds between power adapter checks


def norm(text: str) -> str:
    """For matching: the same letters (Telugu, Hindi...) can be typed as different code point sequences."""
    return unicodedata.normalize("NFC", text).casefold()


def ink_fingerprint(page: dict) -> str:
    """Identifies the handwriting on a page (what the readers see); text boxes and pictures don't count."""
    strokes = [[i, page["objs"][i]] for i in page["order"] if i in page["objs"]
               and page["objs"][i]["type"] == "stroke" and page["objs"][i].get("tool") != "highlighter"]
    return hashlib.sha1(json.dumps(strokes, sort_keys=True).encode()).hexdigest()[:20]


def needs_gemma(record: dict, fp: str, mode: str, model: str) -> bool:
    if mode == "vision":
        return False
    vision = record.get("vision") or {}
    if vision.get("fp") == fp and vision.get("empty"):
        return False  # nothing handwritten on the page
    if mode == "auto" and vision.get("fp") == fp and vision.get("text") \
            and (vision.get("confidence") or 0) >= handwriting.CONFIDENCE_FLOOR:
        return False  # Vision was sure enough
    gemma = record.get("gemma") or {}
    return not (gemma.get("fp") == fp and gemma.get("model") == model)


class HandwritingIndex:
    def __init__(self, hub: PenHub) -> None:
        self.hub = hub
        self.vision_queue: dict[tuple[str, str], float] = {}  # (notebook, page) -> time of last change
        self.gemma_queue: dict[tuple[str, str], float] = {}
        self.user_reads = 0
        self.paused: str | None = None  # "battery" | "ollama" while the Gemma pass is waiting
        self._ollama_retry_at = 0.0
        self._power: tuple[float, bool] = (-POWER_CHECK, True)
        self._rescan = True

    def _wants_gemma(self, record: dict, fp: str) -> bool:
        st = self.hub.settings
        return needs_gemma(record, fp, st["hw_mode"], st["gemma_model"])

    # ------------------------------------------------------------------ records
    @staticmethod
    def _file(nb: Notebook, page_id: str) -> Path:
        return nb.folder / "text" / f"{page_id}.json"

    def load(self, nb: Notebook, page_id: str) -> dict:
        try:
            raw = json.loads(self._file(nb, page_id).read_text())
        except (OSError, ValueError):
            return {}
        if not isinstance(raw, dict):
            return {}
        if "vision" not in raw and "gemma" not in raw:  # first version: Vision text only, no fingerprint
            return {"vision": {"fp": None, "text": raw.get("text", "")}}
        return raw

    def _save(self, nb: Notebook, page_id: str, engine: str, data: dict) -> None:
        record = self.load(nb, page_id)
        record[engine] = data
        path = self._file(nb, page_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(record))

    # ------------------------------------------------------------------ queueing
    def changed(self, notebook_id: str, page_ids: list[str]) -> None:
        now = time.monotonic()
        for page in page_ids:
            self.vision_queue[(notebook_id, page)] = now
            if (notebook_id, page) in self.gemma_queue:
                self.gemma_queue[(notebook_id, page)] = now  # keep waiting until the page is left alone

    def rescan_soon(self) -> None:
        """After the engine or model setting changes: check every page again."""
        self._rescan = True

    def _scan(self) -> None:
        current = self.hub.current.get("notebook")
        infos = sorted(self.hub.store.list(), key=lambda i: i["id"] != current)  # the open notebook first
        for info in infos:
            if info.get("exclude_search"):
                continue
            nb = self.hub.store.get(info["id"])
            for page in nb.state["pages"]:
                key = (nb.id, page["id"])
                fp = ink_fingerprint(page)
                record = self.load(nb, page["id"])
                if (record.get("vision") or {}).get("fp") != fp:
                    self.vision_queue.setdefault(key, 0.0)
                elif self._wants_gemma(record, fp):
                    self.gemma_queue.setdefault(key, 0.0)
                else:
                    self.gemma_queue.pop(key, None)

    # ------------------------------------------------------------------ reading
    def _page(self, key: tuple[str, str]) -> tuple[Notebook, dict] | None:
        try:
            nb = self.hub.store.get(key[0])
        except (KeyError, NoteError):
            return None
        if nb.meta.get("exclude_search"):
            return None
        page = next((p for p in nb.state["pages"] if p["id"] == key[1]), None)
        return (nb, page) if page else None

    async def read_vision(self, nb: Notebook, page: dict) -> dict:
        fp = ink_fingerprint(page)
        known = self.load(nb, page["id"]).get("vision") or {}
        data: dict | None = None
        try:
            if known.get("fp") == fp and not known.get("error"):
                data = known  # only text boxes or pictures changed: the handwriting reads the same
            else:
                png, _ = self.hub._ink_png(nb, page, None)
        except NoteError:
            data = {"fp": fp, "text": "", "empty": True}
        else:
            if data is None:
                attempt = await asyncio.to_thread(handwriting.read_vision, png)
                data = {"fp": fp, "text": attempt.text, "confidence": attempt.confidence}
                if attempt.error:
                    data["error"] = attempt.error
        self._save(nb, page["id"], "vision", data)
        key = (nb.id, page["id"])
        if self._wants_gemma(self.load(nb, page["id"]), fp):
            self.gemma_queue[key] = time.monotonic()
        else:
            self.gemma_queue.pop(key, None)
        return data

    async def read_gemma(self, nb: Notebook, page: dict) -> bool:
        """False when Ollama couldn't be reached (the page stays queued)."""
        model = self.hub.settings["gemma_model"]
        fp = ink_fingerprint(page)
        try:
            png, _ = self.hub._ink_png(nb, page, None)
        except NoteError:
            return True
        attempt = await asyncio.to_thread(handwriting.read_gemma, png, model)
        if attempt.error and handwriting.OLLAMA_UNREACHABLE in attempt.error:
            return False
        data = {"fp": fp, "model": model, "text": attempt.text}
        if attempt.error:  # e.g. the model isn't installed: don't retry until the page or setting changes
            data["error"] = attempt.error
            log.warning("Gemma couldn't read %s/%s: %s", nb.id, page["id"], attempt.error)
        elif handwriting.needs_translation(attempt.text):  # so an English search finds Telugu, Hindi... writing
            tr = await asyncio.to_thread(handwriting.translate_sync, attempt.text, "English", model)
            if tr.error and handwriting.OLLAMA_UNREACHABLE in tr.error:
                return False
            if tr.error:
                log.warning("Gemma couldn't translate %s/%s: %s", nb.id, page["id"], tr.error)
            else:
                data["translation"] = tr.text
        if ink_fingerprint(page) == fp:  # the page didn't change while Gemma was reading
            self._save(nb, page["id"], "gemma", data)
        return True

    def remember(self, nb: Notebook, page: dict, result: handwriting.Result) -> None:
        """Keep the result of a "Read this page" the user asked for, so the background needn't read it again."""
        fp = ink_fingerprint(page)
        model = self.hub.settings["gemma_model"]
        for attempt in result.attempts:
            if attempt.error:
                continue
            if attempt.engine == "vision":
                self._save(nb, page["id"], "vision", {"fp": fp, "text": attempt.text, "confidence": attempt.confidence})
            elif attempt.engine == f"gemma:{model}":
                data = {"fp": fp, "model": model, "text": attempt.text}
                if handwriting.needs_translation(attempt.text):
                    if result.translation is None:
                        continue  # the translation failed: let the background try again
                    data["translation"] = result.translation
                self._save(nb, page["id"], "gemma", data)
        if not self._wants_gemma(self.load(nb, page["id"]), fp):
            self.gemma_queue.pop((nb.id, page["id"]), None)

    @contextlib.contextmanager
    def user_reading(self):
        """The Gemma pass waits while the user reads a page (Ollama does one thing at a time)."""
        self.user_reads += 1
        try:
            yield
        finally:
            self.user_reads -= 1

    def _on_power(self) -> bool:
        checked, on = self._power
        if time.monotonic() - checked >= POWER_CHECK:
            on = macbridge.on_ac_power()
            self._power = (time.monotonic(), on)
        return on

    # ------------------------------------------------------------------ loop
    async def step(self) -> None:
        """One round of background work (the loop calls this every couple of seconds)."""
        if self._rescan:
            self._rescan = False
            try:
                self._scan()
            except Exception:
                log.exception("handwriting index scan failed")
        now = time.monotonic()
        for key in [k for k, t in self.vision_queue.items() if now - t >= VISION_IDLE][:5]:
            self.vision_queue.pop(key, None)
            found = self._page(key)
            if found:
                try:
                    await self.read_vision(*found)
                except Exception as exc:
                    log.warning("couldn't read handwriting on %s: %s", key, exc)
        await self._gemma_step()

    async def _gemma_step(self) -> None:
        if self.hub.settings["hw_mode"] == "vision":
            self.gemma_queue.clear()
            self.paused = None
            return
        if not self.gemma_queue:
            self.paused = None
            return
        if not self._on_power():
            self.paused = "battery"
            return
        now = time.monotonic()
        if now < self._ollama_retry_at:
            self.paused = "ollama"
            return
        self.paused = None
        if self.user_reads:
            return
        ready = [k for k, t in self.gemma_queue.items() if now - t >= GEMMA_IDLE and k not in self.vision_queue]
        if not ready:
            return
        key = ready[0]
        found = self._page(key)
        if not found:
            self.gemma_queue.pop(key, None)
            return
        nb, page = found
        if not self._wants_gemma(self.load(nb, page["id"]), ink_fingerprint(page)):
            self.gemma_queue.pop(key, None)
            return
        try:
            reached = await self.read_gemma(nb, page)
        except Exception as exc:
            log.warning("couldn't read handwriting with Gemma on %s: %s", key, exc)
            reached = True
        if reached:
            if self.gemma_queue.get(key, 0.0) <= now:  # not changed again while Gemma was reading
                self.gemma_queue.pop(key, None)
        else:
            self._ollama_retry_at = time.monotonic() + OLLAMA_RETRY
            self.paused = "ollama"

    async def run(self) -> None:
        await asyncio.sleep(5)
        while True:
            await self.step()
            await asyncio.sleep(2)

    def status(self) -> dict:
        return {"mode": self.hub.settings["hw_mode"], "model": self.hub.settings["gemma_model"],
                "gemma_pending": len(self.gemma_queue),
                "vision_pending": len(self.vision_queue), "paused": self.paused}

    # ------------------------------------------------------------------ search
    def search(self, query: str, limit: int = 50) -> list[dict]:
        words = [norm(w) for w in query.split() if w.strip()]
        if not words:
            return []
        hits = []
        for info in self.hub.store.list():
            if info.get("exclude_search"):
                continue
            nb = self.hub.store.get(info["id"])
            for index, page in enumerate(nb.state["pages"]):
                record = self.load(nb, page["id"])
                gemma = record.get("gemma") or {}
                current = gemma.get("fp") == ink_fingerprint(page)  # old text could hold erased words
                written = [t for t in (
                    gemma.get("text") if current else None,
                    (record.get("vision") or {}).get("text"),
                    *[o["text"] for o in page["objs"].values() if o["type"] == "text"],
                ) if t]
                translation = gemma.get("translation") if current else None
                texts = [(norm(t), t) for t in written] + ([(norm(translation), translation)] if translation else [])
                if not all(any(w in low for low, _ in texts) for w in words):
                    continue
                # the snippet comes from the first (best) text that has the first word; the translation is last
                index_hit = next(i for i, (low, _) in enumerate(texts) if words[0] in low)
                low, text = texts[index_hit]
                via = "translation" if index_hit >= len(written) else "writing"
                at = low.find(words[0])
                start = max(0, at - 60)
                snippet = text[start: at + 100].replace("\n", " ")
                hits.append({"notebook": nb.id, "name": nb.name, "page": page["id"], "page_no": index + 1,
                             "snippet": ("…" if start else "") + snippet, "via": via})
                if len(hits) >= limit:
                    return hits
        return hits
