"""Reading the words on the screen with Windows' own text recognition (Windows.Media.Ocr, through
the pywinrt packages). It sees what is drawn, so it also finds the buttons of games and other
programs that Windows accessibility can't see into (a Roblox menu's SELL ALL, a game's Start).
"""

from __future__ import annotations

import sys
import threading
from dataclasses import dataclass, field

PACKAGES = ["winrt-runtime", "winrt-Windows.Media.Ocr", "winrt-Windows.Graphics.Imaging",
            "winrt-Windows.Storage.Streams", "winrt-Windows.Foundation", "winrt-Windows.Foundation.Collections",
            "winrt-Windows.Globalization"]


@dataclass
class Line:
    text: str
    left: int          # pixels of the picture it was read from
    top: int
    right: int
    bottom: int
    words: list = field(default_factory=list)    # (text, left, top, right, bottom)

    @property
    def center(self) -> tuple[int, int]:
        return (self.left + self.right) // 2, (self.top + self.bottom) // 2


_engine = None
_lock = threading.Lock()
_apartments = threading.local()


def missing() -> str | None:
    """Why text reading can't work here, or None when it can."""
    if sys.platform != "win32":
        return "Reading text on the screen uses Windows' own text recognition, so it works on Windows only."
    import importlib.util

    try:
        if importlib.util.find_spec("winrt.windows.media.ocr") is None:
            raise ImportError
    except (ImportError, ValueError):
        return ("Reading text on the screen needs the winrt add-on, which isn't installed. Turn Computer use off "
                "and on again in the app to install it.")
    return None


def _apartment():
    """WinRT needs COM set up on each thread that uses it (multi-threaded: the tool threads don't
    run a window). A thread where it is already set up another way is fine as it is."""
    if getattr(_apartments, "done", False):
        return
    _apartments.done = True
    try:
        from winrt.runtime import ApartmentType, init_apartment

        init_apartment(ApartmentType.MULTI_THREADED)
    except Exception:  # noqa: BLE001 - already initialised (e.g. single-threaded by Qt)
        pass


def _get_engine():
    global _engine
    if _engine is None:
        from winrt.windows.media.ocr import OcrEngine

        engine = OcrEngine.try_create_from_user_profile_languages()
        if engine is None:      # no language of the user's has a recognizer: English usually does
            from winrt.windows.globalization import Language

            engine = OcrEngine.try_create_from_language(Language("en-US"))
        if engine is None:
            raise RuntimeError("Windows has no text recognition language installed (Settings > Time & language > "
                               "Language: add English with its optional features).")
        _engine = engine
    return _engine


def read(img) -> list[Line]:
    """The lines of text in a PIL image, top to bottom, in that image's pixels."""
    why = missing()
    if why:
        raise RuntimeError(why)
    from winrt.windows.graphics.imaging import BitmapAlphaMode, BitmapPixelFormat, SoftwareBitmap
    from winrt.windows.media.ocr import OcrEngine

    with _lock:
        _apartment()
        engine = _get_engine()
        scale = 1.0
        most = int(OcrEngine.max_image_dimension or 10000)
        if max(img.size) > most:
            from PIL import Image

            scale = most / max(img.size)
            img = img.resize((int(img.width * scale), int(img.height * scale)), Image.LANCZOS)
        rgb = img.convert("RGB")
        bitmap = SoftwareBitmap.create_copy_from_buffer(rgb.tobytes("raw", "BGRX"), BitmapPixelFormat.BGRA8,
                                                        rgb.width, rgb.height, BitmapAlphaMode.IGNORE)
        result = engine.recognize_async(bitmap).get()
    lines = []
    for ln in result.lines:
        words = []
        for w in ln.words:
            r = w.bounding_rect
            words.append((w.text, int(r.x / scale), int(r.y / scale), int((r.x + r.width) / scale),
                          int((r.y + r.height) / scale)))
        if not words:
            continue
        lines.append(Line(" ".join(t for t, *_ in words), min(w[1] for w in words), min(w[2] for w in words),
                          max(w[3] for w in words), max(w[4] for w in words), words))
    lines.sort(key=lambda ln: (ln.top // 8, ln.left))
    return lines


def split_wide(lines: list[Line], gap: float = 1.6) -> list[Line]:
    """Windows reads a row of separate buttons (WORKSHOP  UPGRADE  SELL) as one line. Split a line
    where the space between two words is much wider than its letters are tall: those are separate
    things to click."""
    out = []
    for ln in lines:
        part = [ln.words[0]]
        for w in ln.words[1:]:
            prev = part[-1]
            height = max(4, max(prev[4] - prev[2], w[4] - w[2]))
            if w[1] - prev[3] > gap * height:
                out.append(_line(part))
                part = []
            part.append(w)
        out.append(_line(part))
    return out


def _line(words) -> Line:
    return Line(" ".join(t for t, *_ in words), min(w[1] for w in words), min(w[2] for w in words),
                max(w[3] for w in words), max(w[4] for w in words), list(words))


def _norm(s: str) -> str:
    return " ".join("".join(c.lower() if c.isalnum() else " " for c in s).split())


def find(lines: list[Line], want: str) -> list[tuple[float, Line]]:
    """Lines (or the run of words inside a line) that say `want`, best first, as (score, line):
    1.0 = the same words, less for words inside a longer line or a close misreading (OCR reads
    l as I, 0 as O...)."""
    import difflib

    target = _norm(want)
    if not target:
        return []
    n = len(target.split())
    found = []
    for ln in lines:
        words = ln.words
        best = None
        # every run of as many words as wanted: "SELL" inside "SELL ALL ORES" is its own button part
        for i in range(len(words)):
            for j in range(i + 1, min(len(words), i + n + 1) + 1):
                run = words[i:j]
                text = _norm(" ".join(w[0] for w in run))
                if not text:
                    continue
                if text == target:
                    score = 1.0 if j - i == len(words) else 0.95
                else:
                    ratio = difflib.SequenceMatcher(None, text, target).ratio()
                    score = ratio * 0.9 if ratio >= 0.8 else 0
                if score and (best is None or score > best[0]):
                    best = (score, _line(run))
        if best is None and target in _norm(ln.text):
            best = (0.7, ln)
        if best:
            found.append(best)
    found.sort(key=lambda f: (-f[0], f[1].top, f[1].left))
    return found
