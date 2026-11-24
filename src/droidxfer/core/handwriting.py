"""Handwriting to text: Apple Vision first (fast, offline), Gemma 4 through the local Ollama as a fallback.

Modes: "auto" (Vision, then Gemma when Vision finds nothing or isn't confident), "vision", "gemma".
Nothing leaves the Mac: Ollama runs on 127.0.0.1.
"""

from __future__ import annotations

import asyncio
import base64
import io
import json
import logging
import re
import time
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass, field

from PIL import Image

from . import macbridge

log = logging.getLogger(__name__)

OLLAMA_URL = "http://127.0.0.1:11434"
MODES = ("auto", "vision", "gemma")
OLLAMA_UNREACHABLE = "isn't reachable"  # part of the error when Ollama isn't running
# Below this average confidence Auto asks Gemma. Measured on real S Pen handwriting: Vision said 0.73 while
# getting 8 of ~30 words wrong, and Gemma 4 12b got them all; printed text scores 1.0.
CONFIDENCE_FLOOR = 0.85
GEMMA_PROMPT = (
    "Transcribe the handwritten text in this image exactly as written, one line of output per written line. "
    "Leave out words that are crossed out. Do not correct spelling, translate, describe drawings or add "
    "anything else. Write plain text only: no Markdown, no LaTeX, no blank lines between lines; write an "
    "arrow as ->. If there is no text, output nothing."
)


@dataclass
class Attempt:
    engine: str
    text: str = ""
    confidence: float | None = None
    ms: int = 0
    error: str | None = None


@dataclass
class Result:
    text: str
    engine: str | None
    confidence: float | None
    ms: int
    attempts: list[Attempt] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


def prepare(png: bytes, max_side: int = 2048) -> bytes:
    """Flatten onto white and limit the size (both engines read dark-on-white best)."""
    with Image.open(io.BytesIO(png)) as im:
        im = im.convert("RGBA")
        bg = Image.new("RGB", im.size, (255, 255, 255))
        bg.paste(im, mask=im.getchannel("A"))
        if max(bg.size) > max_side:
            bg.thumbnail((max_side, max_side), Image.LANCZOS)
        out = io.BytesIO()
        bg.save(out, "PNG")
        return out.getvalue()


def _vision(png: bytes) -> Attempt:
    start = time.perf_counter()
    try:
        lines = macbridge.vision_recognize(png)
    except Exception as exc:
        return Attempt("vision", error=str(exc), ms=round((time.perf_counter() - start) * 1000))
    text = "\n".join(l.text for l in lines)
    conf = sum(l.confidence for l in lines) / len(lines) if lines else None
    return Attempt("vision", text, conf, round((time.perf_counter() - start) * 1000))


def _post(path: str, body: dict, timeout: float) -> dict:
    req = urllib.request.Request(f"{OLLAMA_URL}{path}", data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as res:  # noqa: S310 - fixed localhost URL
        return json.loads(res.read())


def gemma_models(timeout: float = 2.0) -> list[str]:
    """Ollama models that can read images (empty if Ollama isn't running)."""
    try:
        with urllib.request.urlopen(f"{OLLAMA_URL}/api/tags", timeout=timeout) as res:  # noqa: S310
            data = json.loads(res.read())
    except (OSError, ValueError):
        return []
    names = []
    for m in data.get("models", []):
        caps = m.get("capabilities")
        if caps is None or "vision" in caps:
            names.append(m.get("name", ""))
    return [n for n in names if n]


def _gemma(png: bytes, model: str, timeout: float = 240.0) -> Attempt:
    start = time.perf_counter()
    body = {"model": model, "prompt": GEMMA_PROMPT, "images": [base64.b64encode(png).decode()],
            "stream": False, "think": False, "keep_alive": "10m", "options": {"temperature": 0}}
    try:
        data = _post("/api/generate", body, timeout)
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:200]
        if exc.code == 400 and "think" in detail:  # older Ollama without the think switch
            body.pop("think")
            try:
                data = _post("/api/generate", body, timeout)
            except Exception as again:
                return Attempt("gemma", error=str(again), ms=round((time.perf_counter() - start) * 1000))
        else:
            return Attempt("gemma", error=f"Ollama said {exc.code}: {detail}", ms=round((time.perf_counter() - start) * 1000))
    except (OSError, ValueError) as exc:
        return Attempt("gemma", error=f"Ollama {OLLAMA_UNREACHABLE} on {OLLAMA_URL} ({exc})",
                       ms=round((time.perf_counter() - start) * 1000))
    text = clean_model_text(data.get("response") or "")
    return Attempt(f"gemma:{model}", text, None, round((time.perf_counter() - start) * 1000))


_LATEX = {r"\rightarrow": "->", r"\to": "->", r"\leftarrow": "<-", r"\times": "x", r"\%": "%"}


def clean_model_text(text: str) -> str:
    """Tidy a model's answer into plain text (it sometimes adds Markdown/LaTeX despite the prompt)."""
    text = text.strip()
    if text.startswith("```"):
        text = text.strip("`").split("\n", 1)[-1].strip()
    text = re.sub(r"~~.*?~~", "", text)  # crossed-out words
    text = re.sub(r"\$\s*(\\[a-zA-Z%]+)\s*\$", lambda m: _LATEX.get(m.group(1), ""), text)
    for k, v in _LATEX.items():
        text = text.replace(k, v)
    text = re.sub(r"\*\*(.+?)\*\*", r"\1", text)
    lines = [re.sub(r"[ \t]+", " ", line).strip() for line in text.splitlines()]
    return "\n".join(line for line in lines if line)


def recognize_sync(png: bytes, mode: str = "auto", model: str = "gemma4:12b") -> Result:
    if mode not in MODES:
        raise ValueError(f"mode must be one of {MODES}")
    png = prepare(png)
    attempts: list[Attempt] = []
    chosen: Attempt | None = None
    if mode in ("auto", "vision"):
        v = _vision(png)
        attempts.append(v)
        if v.error is None and v.text and (mode == "vision" or (v.confidence or 0) >= CONFIDENCE_FLOOR):
            chosen = v
    if chosen is None and mode in ("auto", "gemma"):
        g = _gemma(png, model)
        attempts.append(g)
        if g.error is None and g.text:
            chosen = g
    if chosen is None:  # Auto: keep a weak Vision answer rather than nothing
        chosen = next((a for a in attempts if a.error is None and a.text), None)
    total = sum(a.ms for a in attempts)
    if chosen is None:
        errors = [a.error for a in attempts if a.error]
        if errors and len(errors) == len(attempts):
            raise RuntimeError("; ".join(errors))
        return Result("", None, None, total, attempts)
    return Result(chosen.text, chosen.engine, chosen.confidence, total, attempts)


def read_vision(png: bytes) -> Attempt:
    """One Vision pass (for the background search index)."""
    return _vision(prepare(png))


def read_gemma(png: bytes, model: str) -> Attempt:
    """One Gemma pass (for the background search index)."""
    return _gemma(prepare(png), model)


async def recognize(png: bytes, mode: str = "auto", model: str = "gemma4:12b") -> Result:
    return await asyncio.to_thread(recognize_sync, png, mode, model)


def compare_sync(png: bytes, model: str) -> dict:
    """Run both engines on the same image (for choosing the defaults)."""
    png = prepare(png)
    return {"vision": asdict(_vision(png)), "gemma": asdict(_gemma(png, model))}
