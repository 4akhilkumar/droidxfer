"""Draws notebook pages with Pillow (PNG, PDF, thumbnails, timelapse frames, handwriting input) and as SVG.

The width/opacity rules match web/js/pen/render.js so exports look like the screen.
"""

from __future__ import annotations

import html
import io
import math
from functools import lru_cache
from pathlib import Path
from typing import Callable, Iterable

from PIL import Image, ImageDraw, ImageFont

GRID = 40.0
TEMPLATE_COLOR = (170, 190, 215)
SUPERSAMPLE = 2


def stroke_width(tool: str, size: float, pressure: float, tilt: float = 0.0) -> float:
    if tool == "highlighter":
        return size * 2.5
    if tool == "pencil":
        return size * (0.5 + 0.5 * pressure) * (1 + 1.5 * tilt)
    return size * (0.3 + 0.7 * pressure)


def stroke_alpha(stroke: dict) -> float:
    if stroke["tool"] == "highlighter":
        return 0.35
    if stroke["tool"] == "pencil":
        pts = stroke["pts"]
        avg = sum(p[2] for p in pts) / len(pts)
        return 0.25 + 0.6 * avg
    return 1.0


def _rgb(color: str) -> tuple[int, int, int]:
    return int(color[1:3], 16), int(color[3:5], 16), int(color[5:7], 16)


def obj_bounds(obj: dict) -> tuple[float, float, float, float]:
    if obj["type"] == "stroke":
        xs = [p[0] for p in obj["pts"]]
        ys = [p[1] for p in obj["pts"]]
        pad = stroke_width(obj["tool"], obj["size"], 1.0, 1.0) / 2 + 1
        return min(xs) - pad, min(ys) - pad, max(xs) + pad, max(ys) + pad
    if obj["type"] == "text":
        lines = max(1, obj["text"].count("\n") + 1 + len(obj["text"]) * obj["size"] * 0.55 // max(obj["w"], 1))
        return obj["x"], obj["y"], obj["x"] + obj["w"], obj["y"] + lines * obj["size"] * 1.3
    return obj["x"], obj["y"], obj["x"] + obj["w"], obj["y"] + obj["h"]


@lru_cache(maxsize=8)
def _font(px: int) -> ImageFont.ImageFont:
    for candidate in ("/System/Library/Fonts/Helvetica.ttc", "/System/Library/Fonts/Supplemental/Arial.ttf",
                      "/Library/Fonts/Arial.ttf"):
        try:
            return ImageFont.truetype(candidate, px)
        except OSError:
            continue
    return ImageFont.load_default()


def _wrap(text: str, font: ImageFont.ImageFont, width: float) -> list[str]:
    lines: list[str] = []
    for para in text.split("\n"):
        words = para.split(" ")
        line = ""
        for word in words:
            trial = f"{line} {word}".strip()
            if line and font.getlength(trial) > width:
                lines.append(line)
                line = word
            else:
                line = trial
        lines.append(line)
    return lines


class PageRenderer:
    def __init__(self, asset_loader: Callable[[str], Path] | None = None) -> None:
        self.asset_loader = asset_loader

    def _asset(self, name: str) -> Image.Image | None:
        if not self.asset_loader:
            return None
        try:
            with Image.open(self.asset_loader(name)) as im:
                return im.convert("RGBA")
        except Exception:
            return None

    def render(self, page: dict, *, scale: float = 1.0, background: bool = True, template: bool = True,
               objs: Iterable[str] | None = None, crop: tuple[float, float, float, float] | None = None,
               max_pixels: int = 60_000_000) -> Image.Image:
        """Render a page (or only `objs`, or only the `crop` box) to an RGBA image."""
        x0, y0, x1, y1 = crop or (0.0, 0.0, page["w"], page["h"])
        w, h = max(x1 - x0, 1), max(y1 - y0, 1)
        if w * h * scale * scale * SUPERSAMPLE * SUPERSAMPLE > max_pixels:
            scale = math.sqrt(max_pixels / (w * h)) / SUPERSAMPLE
        k = scale * SUPERSAMPLE
        size = (max(1, round(w * k)), max(1, round(h * k)))
        img = Image.new("RGBA", size, (255, 255, 255, 255) if background else (255, 255, 255, 0))

        def tx(x: float, y: float) -> tuple[float, float]:
            return (x - x0) * k, (y - y0) * k

        if background and page.get("bg"):
            bg = self._asset(page["bg"]["asset"])
            if bg is not None:
                left, top = tx(0, 0)
                _composite_clipped(img, bg.resize((max(1, round(page["w"] * k)), max(1, round(page["h"] * k))),
                                                  Image.LANCZOS), round(left), round(top))
        if template and background and page.get("template", "blank") != "blank":
            self._template(img, page, tx, k)
        wanted = set(objs) if objs is not None else None
        for oid in page["order"]:
            if wanted is not None and oid not in wanted:
                continue
            obj = page["objs"][oid]
            if obj["type"] == "stroke":
                self._stroke(img, obj, tx, k)
            elif obj["type"] == "image":
                pic = self._asset(obj["asset"])
                if pic is not None:
                    left, top = tx(obj["x"], obj["y"])
                    pic = pic.resize((max(1, round(obj["w"] * k)), max(1, round(obj["h"] * k))), Image.LANCZOS)
                    _composite_clipped(img, pic, round(left), round(top))
            elif obj["type"] == "text":
                font = _font(max(4, round(obj["size"] * k)))
                draw = ImageDraw.Draw(img)
                left, top = tx(obj["x"], obj["y"])
                for i, line in enumerate(_wrap(obj["text"], font, obj["w"] * k)):
                    draw.text((left, top + i * obj["size"] * 1.3 * k), line, fill=_rgb(obj["color"]) + (255,), font=font)
        return img.resize((max(1, round(size[0] / SUPERSAMPLE)), max(1, round(size[1] / SUPERSAMPLE))), Image.LANCZOS)

    @staticmethod
    def _template(img: Image.Image, page: dict, tx, k: float) -> None:
        draw = ImageDraw.Draw(img)
        kind = page["template"]
        width = max(1, round(1.2 * k))
        if kind in ("ruled", "grid"):
            y = GRID * 2 if kind == "ruled" else GRID
            while y < page["h"]:
                draw.line([tx(0, y), tx(page["w"], y)], fill=TEMPLATE_COLOR + (255,), width=width)
                y += GRID
        if kind == "grid":
            x = GRID
            while x < page["w"]:
                draw.line([tx(x, 0), tx(x, page["h"])], fill=TEMPLATE_COLOR + (255,), width=width)
                x += GRID
        if kind == "dot":
            r = 1.8 * k
            y = GRID
            while y < page["h"]:
                x = GRID
                while x < page["w"]:
                    cx, cy = tx(x, y)
                    draw.ellipse([cx - r, cy - r, cx + r, cy + r], fill=TEMPLATE_COLOR + (255,))
                    x += GRID
                y += GRID

    @staticmethod
    def _stroke(img: Image.Image, stroke: dict, tx, k: float) -> None:
        pts = stroke["pts"]
        bx0, by0, bx1, by1 = obj_bounds(stroke)
        ox, oy = tx(bx0, by0)
        ox, oy = math.floor(ox), math.floor(oy)
        ex, ey = tx(bx1, by1)
        mw, mh = max(1, math.ceil(ex) - ox + 2), max(1, math.ceil(ey) - oy + 2)
        mask = Image.new("L", (mw, mh), 0)
        draw = ImageDraw.Draw(mask)
        prev = None
        for p in pts:
            x, y = tx(p[0], p[1])
            x, y = x - ox, y - oy
            wpx = max(stroke_width(stroke["tool"], stroke["size"], p[2], p[3] if len(p) > 3 else 0.0) * k, 1.0)
            r = wpx / 2
            draw.ellipse([x - r, y - r, x + r, y + r], fill=255)
            if prev is not None:
                draw.line([prev, (x, y)], fill=255, width=max(1, round(wpx)))
            prev = (x, y)
        alpha = stroke_alpha(stroke)
        if alpha < 1:
            mask = mask.point(lambda v: round(v * alpha))
        layer = Image.new("RGBA", (mw, mh), _rgb(stroke["color"]) + (0,))
        layer.putalpha(mask)
        _composite_clipped(img, layer, ox, oy)


def _composite_clipped(img: Image.Image, layer: Image.Image, ox: int, oy: int) -> None:
    """alpha_composite that accepts layers hanging off any edge of `img`."""
    cx, cy = max(0, -ox), max(0, -oy)
    right, bottom = min(layer.width, img.width - ox), min(layer.height, img.height - oy)
    if cx >= right or cy >= bottom:
        return
    part = layer.crop((cx, cy, right, bottom))
    img.alpha_composite(part, (max(ox, 0), max(oy, 0)))


def to_png(img: Image.Image) -> bytes:
    out = io.BytesIO()
    img.save(out, "PNG", optimize=False)
    return out.getvalue()


def flatten(img: Image.Image) -> Image.Image:
    bg = Image.new("RGB", img.size, (255, 255, 255))
    bg.paste(img, mask=img.getchannel("A"))
    return bg


def pages_to_pdf(renderer: PageRenderer, pages: list[dict], scale: float = 2.0) -> bytes:
    images = [flatten(renderer.render(p, scale=scale)) for p in pages]
    out = io.BytesIO()
    images[0].save(out, "PDF", save_all=True, append_images=images[1:], resolution=72 * scale)
    return out.getvalue()


def page_to_svg(page: dict, asset_href: Callable[[str], str] | None = None) -> str:
    w, h = page["w"], page["h"]
    parts = [f'<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink" '
             f'viewBox="0 0 {w:g} {h:g}" width="{w:g}" height="{h:g}">',
             f'<rect width="{w:g}" height="{h:g}" fill="#ffffff"/>']
    if page.get("bg") and asset_href:
        parts.append(f'<image href="{html.escape(asset_href(page["bg"]["asset"]))}" x="0" y="0" '
                     f'width="{w:g}" height="{h:g}" preserveAspectRatio="none"/>')
    for oid in page["order"]:
        obj = page["objs"][oid]
        if obj["type"] == "stroke":
            parts.append(_stroke_svg(obj))
        elif obj["type"] == "image" and asset_href:
            parts.append(f'<image href="{html.escape(asset_href(obj["asset"]))}" x="{obj["x"]:g}" y="{obj["y"]:g}" '
                         f'width="{obj["w"]:g}" height="{obj["h"]:g}" preserveAspectRatio="none"/>')
        elif obj["type"] == "text":
            lines = obj["text"].split("\n")
            tspans = "".join(f'<tspan x="{obj["x"]:g}" dy="{(0 if i == 0 else obj["size"] * 1.3):g}">'
                             f'{html.escape(line)}</tspan>' for i, line in enumerate(lines))
            parts.append(f'<text x="{obj["x"]:g}" y="{obj["y"] + obj["size"]:g}" font-family="Helvetica, Arial, sans-serif" '
                         f'font-size="{obj["size"]:g}" fill="{obj["color"]}">{tspans}</text>')
    parts.append("</svg>")
    return "\n".join(parts)


def _stroke_svg(stroke: dict) -> str:
    pts = stroke["pts"]
    color = stroke["color"]
    alpha = stroke_alpha(stroke)
    if len(pts) == 1:
        p = pts[0]
        r = stroke_width(stroke["tool"], stroke["size"], p[2], p[3] if len(p) > 3 else 0) / 2
        return f'<circle cx="{p[0]:g}" cy="{p[1]:g}" r="{r:.2f}" fill="{color}" fill-opacity="{alpha:.2f}"/>'
    # consecutive segments with (nearly) the same width share one path
    paths: list[tuple[float, list[str]]] = []
    for a, b in zip(pts, pts[1:]):
        wa = stroke_width(stroke["tool"], stroke["size"], a[2], a[3] if len(a) > 3 else 0)
        wb = stroke_width(stroke["tool"], stroke["size"], b[2], b[3] if len(b) > 3 else 0)
        width = round((wa + wb) / 2, 1)
        if paths and abs(paths[-1][0] - width) < 0.35:
            paths[-1][1].append(f"L{b[0]:g} {b[1]:g}")
        else:
            paths.append((width, [f"M{a[0]:g} {a[1]:g}", f"L{b[0]:g} {b[1]:g}"]))
    body = "".join(f'<path d="{"".join(d)}" stroke-width="{wd:g}"/>' for wd, d in paths)
    return (f'<g fill="none" stroke="{color}" stroke-linecap="round" stroke-linejoin="round" '
            f'opacity="{alpha:.2f}">{body}</g>')
