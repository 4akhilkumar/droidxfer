"""macOS-only helpers: clipboard, screenshots, Apple Vision text recognition and pen/tablet events.

Everything degrades gracefully: on a Mac without pyobjc (or in tests) the functions raise MacBridgeError
with a plain-words reason instead of crashing.
"""

from __future__ import annotations

import ctypes
import logging
import os
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)


class MacBridgeError(RuntimeError):
    """A macOS feature isn't available or failed (missing permission, cancelled, not a Mac...)."""


# dyld search-path overrides. With e.g. DYLD_LIBRARY_PATH=/opt/homebrew/lib, ImageIO's private libPng/libGIF/
# libJPEG/libTIFF resolve (case-insensitively) to Homebrew's libpng/libgif/libjpeg/libtiff instead, and the first
# PNG that Vision or AppKit decodes kills the process with SIGBUS (EXC_ARM_DA_ALIGN at 0xbad4007).
DYLD_OVERRIDES = ("DYLD_LIBRARY_PATH", "DYLD_FRAMEWORK_PATH")


def dyld_overrides() -> list[str]:
    """The dyld override variables set in this process's environment (they only take effect at launch)."""
    return [name for name in DYLD_OVERRIDES if os.environ.get(name)]


def _quartz():
    try:
        import Quartz  # type: ignore[import-not-found]
    except ImportError as exc:  # pragma: no cover - always installed on macOS
        raise MacBridgeError("this needs macOS with pyobjc installed (uv sync)") from exc
    return Quartz


# ------------------------------------------------------------------ clipboard
def copy_png(png: bytes) -> None:
    """Put a PNG image on the Mac clipboard (as PNG and TIFF, so every app can paste it)."""
    try:
        import AppKit  # type: ignore[import-not-found]
    except ImportError as exc:  # pragma: no cover
        raise MacBridgeError("this needs macOS with pyobjc installed (uv sync)") from exc
    data = AppKit.NSData.dataWithBytes_length_(png, len(png))
    rep = AppKit.NSBitmapImageRep.imageRepWithData_(data)
    board = AppKit.NSPasteboard.generalPasteboard()
    board.clearContents()
    board.declareTypes_owner_([AppKit.NSPasteboardTypePNG, AppKit.NSPasteboardTypeTIFF], None)
    board.setData_forType_(data, AppKit.NSPasteboardTypePNG)
    if rep is not None:
        board.setData_forType_(rep.TIFFRepresentation(), AppKit.NSPasteboardTypeTIFF)


def copy_text(text: str) -> None:
    try:
        subprocess.run(["/usr/bin/pbcopy"], input=text.encode("utf-8"), check=True, timeout=10)
    except (OSError, subprocess.SubprocessError) as exc:
        raise MacBridgeError(f"couldn't copy to the clipboard: {exc}") from exc


# ------------------------------------------------------------------ screenshots
def screen_capture_allowed() -> bool | None:
    try:
        return bool(_quartz().CGPreflightScreenCaptureAccess())
    except Exception:
        return None


def interactive_screenshot(timeout: float = 180.0) -> bytes:
    """Let the user drag a box on the Mac's screen (or press Space for a window); returns PNG bytes."""
    if sys.platform != "darwin":
        raise MacBridgeError("screenshots need macOS")
    if screen_capture_allowed() is False:
        try:
            _quartz().CGRequestScreenCaptureAccess()
        except Exception:
            pass
        raise MacBridgeError(
            "macOS hasn't allowed screenshots yet: open System Settings › Privacy & Security › "
            "Screen & System Audio Recording, turn on your terminal app, then restart droidxfer")
    fd, tmp = tempfile.mkstemp(suffix=".png", prefix="droidxfer-shot-")
    os.close(fd)
    os.unlink(tmp)
    try:
        subprocess.run(["/usr/sbin/screencapture", "-i", "-x", "-o", tmp], timeout=timeout, check=False)
        path = Path(tmp)
        if not path.exists() or path.stat().st_size == 0:
            raise MacBridgeError("screenshot cancelled")
        return path.read_bytes()
    except subprocess.TimeoutExpired as exc:
        raise MacBridgeError("screenshot timed out") from exc
    finally:
        Path(tmp).unlink(missing_ok=True)


# ------------------------------------------------------------------ Apple Vision
@dataclass
class TextLine:
    text: str
    confidence: float
    box: tuple[float, float, float, float]  # x, y, w, h in 0..1, origin top-left


def vision_available() -> bool:
    try:
        import Vision  # type: ignore[import-not-found]  # noqa: F401
    except ImportError:
        return False
    return sys.platform == "darwin" and not dyld_overrides()


def vision_recognize(png: bytes, languages: list[str] | None = None) -> list[TextLine]:
    """Recognize printed and handwritten text with Apple's on-device Vision framework."""
    try:
        import Vision  # type: ignore[import-not-found]
        from Foundation import NSData  # type: ignore[import-not-found]
    except ImportError as exc:
        raise MacBridgeError("Apple Vision isn't available (needs macOS and pyobjc-framework-Vision)") from exc
    if found := dyld_overrides():  # Vision would crash the whole process, not raise
        raise MacBridgeError(f"Apple Vision can't run with {', '.join(found)} set: unset it and restart droidxfer")
    data =NSData.dataWithBytes_length_(png, len(png))
    handler = Vision.VNImageRequestHandler.alloc().initWithData_options_(data, None)
    request = Vision.VNRecognizeTextRequest.alloc().init()
    request.setRecognitionLevel_(Vision.VNRequestTextRecognitionLevelAccurate)
    request.setUsesLanguageCorrection_(True)
    if languages:
        request.setRecognitionLanguages_(languages)
    elif hasattr(request, "setAutomaticallyDetectsLanguage_"):
        request.setAutomaticallyDetectsLanguage_(True)
    ok, error = handler.performRequests_error_([request], None)
    if not ok:
        raise MacBridgeError(f"Vision failed: {error}")
    lines: list[TextLine] = []
    for obs in request.results() or []:
        candidates = obs.topCandidates_(1)
        if not candidates:
            continue
        best = candidates[0]
        box = obs.boundingBox()
        x, y, w, h = box.origin.x, box.origin.y, box.size.width, box.size.height
        lines.append(TextLine(str(best.string()), float(best.confidence()), (x, 1 - y - h, w, h)))
    return order_lines(lines)


def order_lines(lines: list[TextLine]) -> list[TextLine]:
    """Reading order: top to bottom; boxes on the same row (overlapping vertically) left to right."""
    rows: list[list[TextLine]] = []
    for line in sorted(lines, key=lambda l: l.box[1]):
        mid = line.box[1] + line.box[3] / 2
        for row in rows:
            ref = row[0]
            if ref.box[1] <= mid <= ref.box[1] + ref.box[3]:
                row.append(line)
                break
        else:
            rows.append([line])
    return [l for row in rows for l in sorted(row, key=lambda l: l.box[0])]


# ------------------------------------------------------------------ permissions
def accessibility_allowed() -> bool | None:
    """Whether this process may post mouse/keyboard events (System Settings > Privacy > Accessibility)."""
    if sys.platform != "darwin":
        return None
    try:
        lib = ctypes.cdll.LoadLibrary("/System/Library/Frameworks/ApplicationServices.framework/ApplicationServices")
        lib.AXIsProcessTrusted.restype = ctypes.c_bool
        return bool(lib.AXIsProcessTrusted())
    except OSError:
        return None


def open_privacy_pane(kind: str) -> None:
    anchors = {"accessibility": "Privacy_Accessibility", "screen": "Privacy_ScreenCapture"}
    url = f"x-apple.systempreferences:com.apple.preference.security?{anchors.get(kind, 'Privacy')}"
    subprocess.run(["/usr/bin/open", url], check=False, timeout=10)


def host_app_name() -> str:
    """The app macOS asks permission for (the terminal that started droidxfer), for instructions."""
    term = os.environ.get("TERM_PROGRAM", "")
    return {"Apple_Terminal": "Terminal", "iTerm.app": "iTerm", "vscode": "Visual Studio Code",
            "WarpTerminal": "Warp", "ghostty": "Ghostty"}.get(term, term or "your terminal app")


# ------------------------------------------------------------------ displays & windows
def displays() -> list[dict]:
    Q = _quartz()
    err, ids, count = Q.CGGetActiveDisplayList(16, None, None)
    if err:
        raise MacBridgeError("couldn't list displays")
    main = Q.CGMainDisplayID()
    names: dict[int, str] = {}
    try:
        import AppKit  # type: ignore[import-not-found]

        for screen in AppKit.NSScreen.screens():
            num = screen.deviceDescription().get("NSScreenNumber")
            if num is not None:
                names[int(num)] = str(screen.localizedName())
    except Exception:
        pass
    out = []
    for i, did in enumerate(ids[:count]):
        b = Q.CGDisplayBounds(did)
        out.append({"id": int(did), "name": names.get(int(did), f"Display {i + 1}"), "main": did == main,
                    "x": b.origin.x, "y": b.origin.y, "w": b.size.width, "h": b.size.height})
    out.sort(key=lambda d: (not d["main"], d["x"]))
    return out


_SKIP_OWNERS = {"Window Server", "Dock", "Control Center", "Notification Center", "SystemUIServer", "Spotlight",
                "WindowManager", "Wallpaper", "TextInputMenuAgent"}


def front_window() -> dict | None:
    """The frontmost normal window (owner app name + bounds), without needing Screen Recording permission."""
    Q = _quartz()
    info = Q.CGWindowListCopyWindowInfo(Q.kCGWindowListOptionOnScreenOnly | Q.kCGWindowListExcludeDesktopElements,
                                        Q.kCGNullWindowID) or []
    for win in info:
        if win.get("kCGWindowLayer", 1) != 0:
            continue
        owner = str(win.get("kCGWindowOwnerName", ""))
        if owner in _SKIP_OWNERS or owner.lower().startswith("droidxfer"):
            continue
        b = win.get("kCGWindowBounds") or {}
        if b.get("Width", 0) < 200 or b.get("Height", 0) < 200:  # skip popovers and toolbars
            continue
        return {"app": owner, "pid": int(win.get("kCGWindowOwnerPID", 0)),
                "x": float(b["X"]), "y": float(b["Y"]), "w": float(b["Width"]), "h": float(b["Height"])}
    return None


# ------------------------------------------------------------------ keyboard shortcuts
# US-ANSI virtual key codes
KEYCODES = {
    "a": 0, "s": 1, "d": 2, "f": 3, "h": 4, "g": 5, "z": 6, "x": 7, "c": 8, "v": 9, "b": 11, "q": 12, "w": 13,
    "e": 14, "r": 15, "y": 16, "t": 17, "1": 18, "2": 19, "3": 20, "4": 21, "6": 22, "5": 23, "=": 24, "9": 25,
    "7": 26, "-": 27, "8": 28, "0": 29, "]": 30, "o": 31, "u": 32, "[": 33, "i": 34, "p": 35, "return": 36,
    "l": 37, "j": 38, "'": 39, "k": 40, ";": 41, "\\": 42, ",": 43, "/": 44, "n": 45, "m": 46, ".": 47,
    "tab": 48, "space": 49, "`": 50, "delete": 51, "escape": 53, "left": 123, "right": 124, "down": 125, "up": 126,
}
_MODS = {"cmd": "kCGEventFlagMaskCommand", "shift": "kCGEventFlagMaskShift", "alt": "kCGEventFlagMaskAlternate",
         "opt": "kCGEventFlagMaskAlternate", "ctrl": "kCGEventFlagMaskControl"}


def parse_combo(combo: str) -> tuple[list[str], str]:
    parts = [p.strip().lower() for p in combo.split("+") if p.strip()]
    if not parts:
        raise MacBridgeError("empty shortcut")
    key = parts[-1]
    mods = parts[:-1]
    if key not in KEYCODES or any(m not in _MODS for m in mods):
        raise MacBridgeError(f"unknown shortcut {combo!r}")
    return mods, key


def send_shortcut(combo: str) -> None:
    Q = _quartz()
    mods, key = parse_combo(combo)
    flags = 0
    for m in mods:
        flags |= getattr(Q, _MODS[m])
    code = KEYCODES[key]
    for down in (True, False):
        ev = Q.CGEventCreateKeyboardEvent(None, code, down)
        Q.CGEventSetFlags(ev, flags)
        Q.CGEventPost(Q.kCGHIDEventTap, ev)


# ------------------------------------------------------------------ pen as a Mac tablet
class TabletInjector:
    """Turns pad pen samples into macOS tablet events (pressure + tilt), like a graphics tablet would."""

    DEVICE_ID = 0x5E9  # any stable number; ties proximity and point events together

    def __init__(self) -> None:
        self.Q = _quartz()
        self.in_proximity = False
        self.down: str | None = None  # "left" | "right"
        self.last = (0.0, 0.0)

    def _proximity(self, enter: bool) -> None:
        Q = self.Q
        ev = Q.CGEventCreate(None)
        Q.CGEventSetType(ev, Q.kCGEventTabletProximity)
        Q.CGEventSetIntegerValueField(ev, Q.kCGTabletProximityEventVendorID, 0x056A)
        Q.CGEventSetIntegerValueField(ev, Q.kCGTabletProximityEventTabletID, 1)
        Q.CGEventSetIntegerValueField(ev, Q.kCGTabletProximityEventPointerID, 1)
        Q.CGEventSetIntegerValueField(ev, Q.kCGTabletProximityEventDeviceID, self.DEVICE_ID)
        Q.CGEventSetIntegerValueField(ev, Q.kCGTabletProximityEventSystemTabletID, 1)
        Q.CGEventSetIntegerValueField(ev, Q.kCGTabletProximityEventVendorPointerType, 0x0802)
        Q.CGEventSetIntegerValueField(ev, Q.kCGTabletProximityEventPointerType, 1)  # pen
        # x, y, buttons, pressure, tilt x/y
        Q.CGEventSetIntegerValueField(ev, Q.kCGTabletProximityEventCapabilityMask, 0x0001 | 0x0002 | 0x0040 | 0x0400 | 0x0080 | 0x0100)
        Q.CGEventSetIntegerValueField(ev, Q.kCGTabletProximityEventEnterProximity, 1 if enter else 0)
        Q.CGEventPost(Q.kCGHIDEventTap, ev)
        self.in_proximity = enter

    def _mouse(self, etype: int, x: float, y: float, button: int, pressure: float, tilt: tuple[float, float]) -> None:
        Q = self.Q
        ev = Q.CGEventCreateMouseEvent(None, etype, (x, y), button)
        Q.CGEventSetIntegerValueField(ev, Q.kCGMouseEventSubtype, Q.kCGEventMouseSubtypeTabletPoint)
        Q.CGEventSetDoubleValueField(ev, Q.kCGMouseEventPressure, pressure)
        Q.CGEventSetIntegerValueField(ev, Q.kCGTabletEventPointX, int(x * 10))
        Q.CGEventSetIntegerValueField(ev, Q.kCGTabletEventPointY, int(y * 10))
        Q.CGEventSetDoubleValueField(ev, Q.kCGTabletEventPointPressure, pressure)
        Q.CGEventSetDoubleValueField(ev, Q.kCGTabletEventTiltX, tilt[0])
        Q.CGEventSetDoubleValueField(ev, Q.kCGTabletEventTiltY, tilt[1])
        Q.CGEventSetIntegerValueField(ev, Q.kCGTabletEventDeviceID, self.DEVICE_ID)
        if etype in (Q.kCGEventLeftMouseDown, Q.kCGEventLeftMouseUp, Q.kCGEventRightMouseDown, Q.kCGEventRightMouseUp):
            Q.CGEventSetIntegerValueField(ev, Q.kCGMouseEventClickState, 1)
        Q.CGEventPost(Q.kCGHIDEventTap, ev)

    def sample(self, kind: str, x: float, y: float, pressure: float = 0.0, tilt: tuple[float, float] = (0, 0),
               side: bool = False) -> None:
        """kind: hover | down | move | up | leave. x/y are global screen points."""
        Q = self.Q
        if kind == "leave":
            if self.down:
                self.sample("up", *self.last)
            if self.in_proximity:
                self._proximity(False)
            return
        if not self.in_proximity:
            self._proximity(True)
        self.last = (x, y)
        if kind == "hover":
            self._mouse(Q.kCGEventMouseMoved, x, y, Q.kCGMouseButtonLeft, 0.0, tilt)
        elif kind == "down":
            self.down = "right" if side else "left"
            etype = Q.kCGEventRightMouseDown if side else Q.kCGEventLeftMouseDown
            button = Q.kCGMouseButtonRight if side else Q.kCGMouseButtonLeft
            self._mouse(etype, x, y, button, max(pressure, 0.01), tilt)
        elif kind == "move":
            if self.down == "right":
                self._mouse(Q.kCGEventRightMouseDragged, x, y, Q.kCGMouseButtonRight, pressure, tilt)
            elif self.down == "left":
                self._mouse(Q.kCGEventLeftMouseDragged, x, y, Q.kCGMouseButtonLeft, pressure, tilt)
            else:
                self._mouse(Q.kCGEventMouseMoved, x, y, Q.kCGMouseButtonLeft, 0.0, tilt)
        elif kind == "up":
            if self.down == "right":
                self._mouse(Q.kCGEventRightMouseUp, x, y, Q.kCGMouseButtonRight, 0.0, tilt)
            elif self.down == "left":
                self._mouse(Q.kCGEventLeftMouseUp, x, y, Q.kCGMouseButtonLeft, 0.0, tilt)
            self.down = None


def now_ms() -> float:
    return time.perf_counter() * 1000
