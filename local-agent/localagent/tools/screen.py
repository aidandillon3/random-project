"""Seeing the screen and using the mouse and keyboard ("Computer use"; only offered while the
toggle next to the message box is on).

Coordinates: the model works in the coordinates of the last screenshot it saw. Depending on
the model that is either the screenshot's own pixels or a 0-1000 grid over the screen (Qwen
vision models are trained on the 0-1000 grid). Everything here converts to real screen
pixels before touching the mouse.
"""

from __future__ import annotations

import ctypes
import io
import shutil
import subprocess
import sys
import threading
import time

from . import ocr, winput
from .base import CONTROL, RUN, ToolContext, ToolError, ToolResult, tool

WINDOWS = sys.platform == "win32"

_dpi_done = False


def _dpi_aware():
    """Real pixels everywhere, so screenshots and clicks agree on high-DPI screens. Only once the
    screen is used: the app window (Qt) has usually made itself DPI-aware by then, and this does
    nothing."""
    global _dpi_done
    if _dpi_done or not WINDOWS:
        return
    _dpi_done = True
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass


# ------------------------------------------------------------------ coordinates
def coord_mode(ctx: ToolContext) -> str:
    mode = getattr(ctx.settings, "coord_mode", "auto")
    if mode in ("pixels", "normalized"):
        return mode
    name = (ctx.screen.get("model_name") or getattr(ctx.settings, "model", "") or "").lower()
    return "normalized" if "qwen3" in name or "qwen-3" in name else "pixels"


def coord_help(ctx: ToolContext) -> str:
    if coord_mode(ctx) == "normalized":
        return ("Coordinates are on a 0-1000 grid over the screenshot: x=0 is the left edge, x=1000 the "
                "right edge, y=0 the top, y=1000 the bottom.")
    return ("Coordinates are pixels of the last screenshot you were shown (x from the left, y from the top), "
            "not real screen pixels; they are converted for you.")


def to_screen(ctx: ToolContext, x: float, y: float, in_zoom: bool = False) -> tuple[int, int]:
    """A position the model gave, in real screen pixels. in_zoom: it is a position in the last
    zoomed view, which is much easier to aim at small things in than the whole screenshot."""
    s = ctx.screen
    if "img_w" not in s:       # no screenshot yet (it may hold only the model's name)
        _shot(ctx)  # take one so we know the geometry
        s = ctx.screen
    screen = s
    if in_zoom:
        if not s.get("zoom"):
            raise ToolError("There is no zoomed view yet: use zoom first, or leave out in_zoom.")
        s = s["zoom"]
    lim_x, lim_y = _limits(ctx, s)
    x, y = float(x), float(y)
    if not (-5 <= x <= lim_x + 5 and -5 <= y <= lim_y + 5):
        # a silent clamp to the edge would click somewhere else; say what the numbers mean instead
        if coord_mode(ctx) == "normalized":
            raise ToolError(f"({x:g}, {y:g}) is off the 0-1000 grid. Positions go from 0 to 1000 across and down "
                            f"the screenshot, whatever its size (it is {s['img_w']}x{s['img_h']} pixels): "
                            f"grid x = pixel x * 1000 / {s['img_w']}, grid y = pixel y * 1000 / {s['img_h']}.")
        raise ToolError(f"({x:g}, {y:g}) is outside the {'zoomed view' if in_zoom else 'screenshot'}, which is "
                        f"{s['img_w']}x{s['img_h']} pixels.")
    fx, fy = x / lim_x, y / lim_y
    px, py = int(s["left"] + fx * s["width"]), int(s["top"] + fy * s["height"])
    # Stay 1 pixel off the edges: a corner is pyautogui's emergency stop, and x=0 / x=1000 would land on one.
    return (min(max(px, screen["left"] + 1), screen["left"] + screen["width"] - 2),
            min(max(py, screen["top"] + 1), screen["top"] + screen["height"] - 2))


def _limits(ctx: ToolContext, s: dict | None = None) -> tuple[float, float]:
    """The right and bottom edges in the coordinates the model uses (of the screenshot, or of s)."""
    s = ctx.screen if s is None else s
    return (1000.0, 1000.0) if coord_mode(ctx) == "normalized" else (float(s["img_w"]), float(s["img_h"]))


def from_screen(ctx: ToolContext, px: int, py: int) -> tuple[int, int]:
    s = ctx.screen
    fx, fy = (px - s["left"]) / s["width"], (py - s["top"]) / s["height"]
    if coord_mode(ctx) == "normalized":
        return round(fx * 1000), round(fy * 1000)
    return round(fx * s["img_w"]), round(fy * s["img_h"])


# ------------------------------------------------------------------ capture
def _mss():
    import mss

    _dpi_aware()
    return getattr(mss, "MSS", None) or mss.mss    # mss 10.2 renamed mss.mss to mss.MSS


def _grab(monitor: int, region=None):
    from PIL import Image

    with _mss()() as sct:
        mons = sct.monitors
        if monitor < 0 or monitor >= len(mons):
            monitor = 1 if len(mons) > 1 else 0
        mon = mons[monitor]
        box = region or mon
        raw = sct.grab(box)
        img = Image.frombytes("RGB", raw.size, raw.rgb)
    return img, mon, monitor


def monitors() -> list[dict]:
    """The screens, numbered like the monitor setting (1 = the first one mss lists). Empty when
    the screen can't be read (no mss, no desktop)."""
    try:
        with _mss()() as sct:
            return [dict(m) for m in sct.monitors[1:]]
    except Exception:  # noqa: BLE001 - used for labels and notes only
        return []


def monitor_size(m: dict) -> str:
    """'2560x1440 (main)': Windows puts the main screen's top-left corner at 0, 0."""
    main = m.get("is_primary") or (m["left"] == 0 and m["top"] == 0)
    return f"{m['width']}x{m['height']}" + (" (main)" if main else "")


def monitors_text(mons: list[dict] | None = None) -> str:
    mons = monitors() if mons is None else mons
    return ", ".join(f"{i} = {monitor_size(m)}" for i, m in enumerate(mons, 1))


def _cursor():
    try:
        import pyautogui

        return pyautogui.position()
    except (Exception, SystemExit):     # it exits on import when a part of it is missing
        return None


def _png(img) -> bytes:
    buf = io.BytesIO()
    img.save(buf, "PNG", optimize=False, compress_level=1)     # fastest: the model server unpacks it anyway
    return buf.getvalue()


def _shot(ctx: ToolContext, monitor: int | None = None, marks: bool = False) -> bytes:
    from PIL import Image

    monitor = ctx.settings.monitor if monitor is None else monitor
    img, mon, monitor = _grab(int(monitor or 1))
    real_w, real_h = img.size
    found = _targets(ctx, img, mon) if marks else None     # read before the pointer is drawn over it
    cur = _cursor()
    size = fit_size(ctx, real_w, real_h)
    if size != img.size:
        img = img.resize(size, Image.LANCZOS)
    ctx.screen.update(left=mon["left"], top=mon["top"], width=real_w, height=real_h,
                      img_w=img.size[0], img_h=img.size[1], monitor=monitor)
    ctx.screen.pop("pointer", None)
    if cur and mon["left"] <= cur[0] < mon["left"] + real_w and mon["top"] <= cur[1] < mon["top"] + real_h:
        # drawn after shrinking, so it is as easy to see at any size: a magenta ring around the exact
        # point with ticks outside it (the point itself stays visible). Games draw their own pointer,
        # often not where the real one is, so this is the one to aim with.
        cx = (cur[0] - mon["left"]) * img.width / real_w
        cy = (cur[1] - mon["top"]) * img.height / real_h
        ctx.screen["pointer"] = (cx, cy)
        _draw_pointer(img, cx, cy)
    if ctx.settings.grid_overlay:
        _grid(ctx, img)
    if found is not None:
        ctx.screen["marks"] = found
        _draw_marks(img, found, mon, img.width / real_w)
    return _png(img)


def _draw_pointer(img, cx: float, cy: float):
    from PIL import ImageDraw

    draw = ImageDraw.Draw(img)
    r = 7
    draw.ellipse([cx - r - 1, cy - r - 1, cx + r + 1, cy + r + 1], outline=(0, 0, 0), width=4)
    draw.ellipse([cx - r, cy - r, cx + r, cy + r], outline=(255, 0, 255), width=2)
    for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
        draw.line([cx + dx * (r + 2), cy + dy * (r + 2), cx + dx * (r + 7), cy + dy * (r + 7)],
                  fill=(255, 0, 255), width=2)


# ------------------------------------------------------------------ numbered marks
MAX_MARKS = 60


def _targets(ctx: ToolContext, img, mon: dict) -> list[dict]:
    """Things to click on this screen, in real screen pixels: the buttons, fields and links that
    Windows accessibility knows in the window in front, and short pieces of text read off the
    picture itself (game menus, which accessibility can't see). Long text lines are left out:
    they are reading, not buttons."""
    found: list[dict] = []
    if WINDOWS:
        try:
            for ctype, name, (l, t, r, b) in _uia_quick(80, 1.5):
                if r <= mon["left"] or l >= mon["left"] + mon["width"] or b <= mon["top"] or t >= mon["top"] + mon["height"]:
                    continue
                found.append(dict(label=f"{ctype}: {name}" if name else ctype, box=(l, t, r, b), kind="ui"))
        except Exception:  # noqa: BLE001 - text marks still work without it
            pass
    try:
        lines = ocr.split_wide(ocr.read(img)) if not ocr.missing() else []
    except Exception:  # noqa: BLE001 - accessibility marks still work without it
        lines = []
    for ln in lines:
        if len(ln.text) > 40 or len(ln.words) > 5:
            continue
        box = (ln.left + mon["left"], ln.top + mon["top"], ln.right + mon["left"], ln.bottom + mon["top"])
        cx, cy = (box[0] + box[2]) / 2, (box[1] + box[3]) / 2
        # the same thing as an accessibility mark already has
        if any(m["kind"] == "ui" and m["box"][0] <= cx <= m["box"][2] and m["box"][1] <= cy <= m["box"][3]
               and ocr._norm(ln.text) in ocr._norm(m["label"]) for m in found):
            continue
        found.append(dict(label=ln.text, box=box, kind="text"))
    found = found[:MAX_MARKS]
    for n, m in enumerate(found, 1):
        m["n"] = n
    return found


def _draw_marks(img, found: list[dict], mon: dict, scale: float):
    from PIL import ImageDraw

    d = ImageDraw.Draw(img)
    for m in found:
        l, t, r, b = ((m["box"][0] - mon["left"]) * scale, (m["box"][1] - mon["top"]) * scale,
                      (m["box"][2] - mon["left"]) * scale, (m["box"][3] - mon["top"]) * scale)
        color = (0, 160, 255) if m["kind"] == "ui" else (255, 140, 0)
        d.rectangle([l, t, r, b], outline=color, width=1)
        tag = str(m["n"])
        tw = 7 * len(tag) + 4
        x, y = max(0, l - 1), max(0, t - 13)
        d.rectangle([x, y, x + tw, y + 12], fill=color)
        d.text((x + 2, y), tag, fill=(0, 0, 0))


def _marks_text(found: list[dict]) -> str:
    if not found:
        return "No marks: nothing clickable or short text was found on this screen."
    return ("Marks (click with mark=N): " + "; ".join(f"{m['n']} {m['label'][:40]}" for m in found) + ".")


def _mark_point(ctx: ToolContext, n) -> tuple[int, int]:
    marks = ctx.screen.get("marks") or []
    for m in marks:
        if m["n"] == int(n):
            l, t, r, b = m["box"]
            return (l + r) // 2, (t + b) // 2
    raise ToolError(f"There is no mark {n}: take a screenshot with marks=true first." if not marks else
                    f"There is no mark {n} on the last marked screenshot (1 to {len(marks)}).")


def _point(ctx: ToolContext, a: dict, x: str = "x", y: str = "y", mark: str = "mark") -> tuple[int, int]:
    """Where a call points: a mark's middle, or x, y of the screenshot (or of the zoomed view)."""
    if a.get(mark) is not None:
        return _mark_point(ctx, a[mark])
    if a.get(x) is None or a.get(y) is None:
        raise ToolError(f"Give {x} and {y}, or {mark}.")
    return to_screen(ctx, a[x], a[y], bool(a.get("in_zoom")))


# Qwen vision (llama.cpp's mtmd, which Strata uses too) cuts a picture into 32x32-pixel squares and, past a
# limit, shrinks it first: Strata's limit is 1024 squares (about 1 megapixel), llama.cpp's 4096. A picture it
# shrinks is seen at another size than the one the model is told, and its positions come back at that other
# scale (at 2560x1440 they were half of what they should be). So pictures are made at a size it sees as it
# is: whole squares, under the smaller limit.
VISION_SQUARE = 32
VISION_SQUARES = 1024
# Automatic screenshots use about half that: in the computer use test (2026-10-29) 960x512 shots did all 8
# tasks like 1344x768 ones, with 0.8 s instead of 1.4 s of reading per picture. Zooms keep the whole limit,
# so aiming at small things stays sharp.
AUTO_SQUARES = 512


def fit_size(ctx: ToolContext, w: int, h: int, most: float = 1.0, zoom: bool = False) -> tuple[int, int]:
    """The size to show a w x h picture at: at most `most` times bigger, under the vision limit, in whole
    32-pixel squares so nothing is resized again. A screenshot also stays under the size picked in Settings,
    or about AUTO_SQUARES squares when that is 0 (automatic); a zoom uses the whole limit."""
    squares = int(ctx.settings.extra.get("vision_max_tokens") or VISION_SQUARES) if hasattr(ctx.settings, "extra") \
        else VISION_SQUARES
    scale = min(most, (squares * VISION_SQUARE * VISION_SQUARE / (w * h)) ** 0.5)
    if not zoom:
        side = int(getattr(ctx.settings, "screen_max_side", 0) or 0)
        if side > 0:
            scale = min(scale, side / max(w, h))
        else:
            scale = min(scale, (min(squares, AUTO_SQUARES) * VISION_SQUARE * VISION_SQUARE / (w * h)) ** 0.5)
    return (max(VISION_SQUARE, int(w * scale) // VISION_SQUARE * VISION_SQUARE),
            max(VISION_SQUARE, int(h * scale) // VISION_SQUARE * VISION_SQUARE))


def _grid(ctx: ToolContext, img):
    from PIL import ImageDraw

    d = ImageDraw.Draw(img)
    w, h = img.size
    norm = coord_mode(ctx) == "normalized"
    step = 100
    n = 1000 if norm else max(w, h)
    for v in range(step, n, step):
        x = v * w / 1000 if norm else v
        y = v * h / 1000 if norm else v
        if x < w:
            d.line([(x, 0), (x, h)], fill=(255, 0, 255), width=1)
            d.text((x + 2, 2), str(v), fill=(255, 0, 255))
        if y < h:
            d.line([(0, y), (w, y)], fill=(255, 0, 255), width=1)
            d.text((2, y + 2), str(v), fill=(255, 0, 255))


def _geometry_note(ctx: ToolContext) -> str:
    """The whole explanation once, then only what it needs (fewer tokens per screenshot), unless the
    screen, its size or the coordinate style changed since."""
    s = ctx.screen
    key = (s["monitor"], s["width"], s["height"], s["img_w"], s["img_h"], coord_mode(ctx))
    if s.get("explained") == key:
        return f"Screen {s['monitor']}, image {s['img_w']}x{s['img_h']}.{_pointer_note(ctx)}"
    s["explained"] = key
    note = (f"Screen {s['monitor']}: {s['width']}x{s['height']} real pixels, image shown at "
            f"{s['img_w']}x{s['img_h']}; positions read off an earlier screenshot of another size don't apply. "
            f"{coord_help(ctx)}")
    mons = monitors()
    if len(mons) > 1:
        note += f" Screens: {monitors_text(mons)}; screenshot(monitor=N) looks at another one."
    return note + _pointer_note(ctx)


def _pointer_note(ctx: ToolContext) -> str:
    """Where the mouse is, in the coordinates the model gives: with the ring on the picture it can see
    how far the pointer is from what it meant to hit, and correct by that much."""
    s = ctx.screen
    if "pointer" not in s:
        return ""
    x, y = s["pointer"]
    if coord_mode(ctx) == "normalized":
        x, y = x * 1000 / s["img_w"], y * 1000 / s["img_h"]
    return f" Mouse pointer (magenta ring): {x:.0f}, {y:.0f}."


def _settle(monitor, least: float = 0.12, most: float = 0.6):
    """Wait for the screen to stop changing after an action (a menu opening, a page loading), but no
    longer than `most`: a game that never stops moving gets the same wait as before, and a still
    window is looked at sooner. Compared on small copies, which take a few milliseconds."""
    from PIL import ImageChops, ImageStat

    start = time.time()
    time.sleep(least)
    prev = None
    while time.time() - start < most:
        try:
            img, _, _ = _grab(int(monitor or 1))
        except Exception:  # noqa: BLE001 - the screenshot after this says what's wrong
            return
        small = img.reduce(max(1, img.width // 160))
        if prev is not None and prev.size == small.size and \
                sum(ImageStat.Stat(ImageChops.difference(prev, small)).mean) < 3:
            return
        prev = small
        time.sleep(0.04)


def after_action(ctx: ToolContext, text: str) -> ToolResult:
    # capture_after is False for all but the last control action of a step: one fresh screenshot
    # at the end instead of several stale ones.
    if not ctx.settings.screenshot_after_action or not getattr(ctx, "capture_after", True):
        return ToolResult(text)
    _settle(ctx.screen.get("monitor", ctx.settings.monitor))
    try:
        png = _shot(ctx, ctx.screen.get("monitor"))   # the screen the model is working on
    except Exception as e:  # never fail the action because the follow-up shot failed
        return ToolResult(f"{text}\n(could not take a follow-up screenshot: {e})")
    # with its size note: after a restart or a size change the model must not go on with the old size
    return ToolResult(f"{text}\nScreenshot after the action is attached. {_geometry_note(ctx)}", images=[png])


@tool("screenshot", """
Take a screenshot so you can see the screen. The magenta ring is the mouse pointer. Use the
coordinates of this image with click, drag, scroll and zoom. marks=true numbers the buttons,
fields and short text on it, so you can click one with mark=N instead of aiming.""",
      {"monitor": {"type": "integer", "description": "Screen number: 1, 2, 3... Default: the one picked in the app"},
       "marks": {"type": "boolean", "description": "Number the things to click (takes a moment longer)"}},
      group="control")
def screenshot(ctx: ToolContext, a: dict):
    marks = bool(a.get("marks", getattr(ctx.settings, "screen_marks", False)))
    png = _shot(ctx, a.get("monitor"), marks)
    note = _geometry_note(ctx)
    return ToolResult(note + ("\n" + _marks_text(ctx.screen["marks"]) if marks else ""), images=[png])


@tool("zoom", """
Look closer at part of the screen at full resolution, to read small text. Give the box in
the same coordinates as the last screenshot.""",
      {"x": {"type": "number"}, "y": {"type": "number"},
       "width": {"type": "number"}, "height": {"type": "number"}},
      required=["x", "y", "width", "height"], group="control")
def zoom(ctx: ToolContext, a: dict):
    from PIL import Image

    x1, y1 = to_screen(ctx, a["x"], a["y"])
    lim_x, lim_y = _limits(ctx)     # a box that runs past the edge stops at it
    x2, y2 = to_screen(ctx, min(lim_x, float(a["x"]) + float(a["width"])),
                       min(lim_y, float(a["y"]) + float(a["height"])))
    if x2 - x1 < 4 or y2 - y1 < 4:
        raise ToolError("That box is too small: give its top-left corner as x, y and its size as width, height.")
    box = {"left": x1, "top": y1, "width": x2 - x1, "height": y2 - y1}
    img, _, _ = _grab(ctx.screen.get("monitor", ctx.settings.monitor), box)
    img = img.resize(fit_size(ctx, *img.size, most=3.0, zoom=True), Image.LANCZOS)
    ctx.screen["zoom"] = dict(box, img_w=img.width, img_h=img.height)
    norm = coord_mode(ctx) == "normalized"
    where = ("on a 0-1000 grid over this zoomed image" if norm else
             f"in pixels of this zoomed image ({img.width}x{img.height})")
    pointer = ""
    cur = _cursor()
    if cur and x1 <= cur[0] < x2 and y1 <= cur[1] < y2:
        px, py = (cur[0] - x1) * img.width / (x2 - x1), (cur[1] - y1) * img.height / (y2 - y1)
        _draw_pointer(img, px, py)
        if norm:
            px, py = px * 1000 / img.width, py * 1000 / img.height
        pointer = f" The mouse pointer is in it (magenta ring) at {px:.0f}, {py:.0f} of this zoomed view."
    return ToolResult(f"Zoomed view attached. To click or point at something you see in it, give its position "
                      f"{where} and in_zoom=true: that aims much more precisely. Without in_zoom, positions are "
                      f"on the full screenshot.{pointer}", images=[_png(img)])


# ------------------------------------------------------------------ mouse and keyboard
def _gui():
    _dpi_aware()
    try:
        import pyautogui
    except (Exception, SystemExit) as e:  # pragma: no cover - depends on the desktop
        raise ToolError(f"Mouse and keyboard control is not available: {e}")
    pyautogui.FAILSAFE = True   # slam the mouse into a screen corner to stop it
    pyautogui.PAUSE = 0.03
    return pyautogui


def _where(a: dict) -> str:
    return f"mark {a['mark']}" if a.get("mark") is not None else f"({a.get('x')}, {a.get('y')})"


MAX_HOLD = 10.0     # seconds a key or button may be held down in one call


def _seconds(a: dict, name: str = "hold_seconds") -> float:
    try:
        return max(0.0, min(MAX_HOLD, float(a.get(name) or 0)))
    except (TypeError, ValueError):
        raise ToolError(f"{name} must be a number of seconds.")


def _key(g, k: str, up: bool):
    if WINDOWS:
        winput.key(k, up)
    else:
        (g.keyUp if up else g.keyDown)(k)


def _button(g, b: str, up: bool):
    if WINDOWS:
        winput.button(b, up)
    else:
        (g.mouseUp if up else g.mouseDown)(button=b)


def _wait_held(ctx: ToolContext, g, seconds: float):
    """Keep what is pressed down for a while; the corner and Stop still end it."""
    end = time.time() + seconds
    while True:
        check = getattr(g, "failSafeCheck", None)
        if check:
            check()
        left = end - time.time()
        if left <= 0 or ctx.stop.is_set():
            return
        time.sleep(min(0.05, left))


def _press(ctx: ToolContext, g, keys: list[str], hold: float):
    """Down in order, held (a few frames at least: games miss shorter taps), up in reverse."""
    down = []
    try:
        for k in keys:
            _key(g, k, False)
            down.append(k)
        _wait_held(ctx, g, max(0.05, hold))
    finally:
        for k in reversed(down):
            _key(g, k, True)
        time.sleep(0.03)


def _click_here(ctx: ToolContext, g, b: str, clicks: int, hold: float):
    for i in range(clicks):
        if i:
            time.sleep(0.08)
        _button(g, b, False)
        try:
            _wait_held(ctx, g, max(0.05, hold))
        finally:
            _button(g, b, True)


def _move_to(g, px: int, py: int, seconds: float = 0.05):
    if WINDOWS:
        winput.move_to(px, py, seconds)
        # then a 1-pixel wiggle on the spot: some games (Roblox) only take a click on a button once the
        # pointer has moved over it in a frame of their own; a pointer that just arrived doesn't count
        time.sleep(0.02)
        winput.move_to(px + 1, py)
        time.sleep(0.02)
        winput.move_to(px, py)
    else:
        g.moveTo(px, py, duration=seconds)
    time.sleep(0.05)      # a game frame or two to see where the pointer is before a click


def _to_front(px: int, py: int) -> str:
    """Clicks go to the window under the point, but a game that isn't in front often eats the first
    click to come to the front (or doesn't come at all): bring it there first."""
    if not WINDOWS:
        return ""
    try:
        root, front, title = winput.window_at(px, py)
        if not root or root == front:
            return ""
        winput.bring_to_front(root)
        time.sleep(0.3)
        return f" ('{title}' was brought to the front first.)" if title else ""
    except Exception:  # noqa: BLE001 - clicking still works without it
        return ""


@tool("click", """
Click at a point on the screen (coordinates of the last screenshot), or on a mark of the last
marked screenshot. The window there comes to the front first. In a game, hold_seconds keeps the
button down (e.g. 1.5 to keep firing).""",
      {"x": {"type": "number"}, "y": {"type": "number"},
       "mark": {"type": "integer", "description": "A number from the last screenshot with marks=true, instead of x, y"},
       "button": {"type": "string", "enum": ["left", "right", "middle"]},
       "clicks": {"type": "integer", "description": "2 for double-click"},
       "hold_seconds": {"type": "number", "description": "Keep the button down this long (up to 10)"},
       "in_zoom": {"type": "boolean", "description": "x, y are in the last zoomed view, not the screenshot"}},
      risk=CONTROL, group="control",
      describe=lambda a: f"{a.get('button', 'left')} click x{a.get('clicks', 1)} at {_where(a)}"
      + (f", held {a['hold_seconds']}s" if a.get("hold_seconds") else ""))
def click(ctx: ToolContext, a: dict):
    g = _gui()
    px, py = _point(ctx, a)
    hold = _seconds(a)
    _move_to(g, px, py)
    note = _to_front(px, py)
    _click_here(ctx, g, a.get("button") or "left", max(1, min(5, int(a.get("clicks") or 1))), hold)
    return after_action(ctx, f"Clicked at {_where(a)}" + (f", held {hold:g}s" if hold else "") + "." + note)


def find_on_screen(ctx: ToolContext, text: str, monitor: int | None = None) -> tuple[list, list, dict]:
    """Where `text` is on a screen: (matches as (score, ocr.Line) in real screen pixels, best first;
    every line read, for when there is no match; the screen). Reads the words off the picture,
    so it works in games; in normal windows accessibility names are searched too."""
    from types import SimpleNamespace

    why = ocr.missing()
    if why and not WINDOWS:
        raise ToolError(why)
    img, mon, monitor = _grab(int(monitor or ctx.screen.get("monitor") or ctx.settings.monitor or 1))
    lines = []
    if not why:
        try:
            lines = ocr.split_wide(ocr.read(img))
        except Exception as e:  # noqa: BLE001 - said to the model below if nothing else finds it
            why = f"Reading the screen failed: {e}"
    dx, dy = mon["left"], mon["top"]     # picture pixels to screen pixels
    lines = [ocr._line([(w[0], w[1] + dx, w[2] + dy, w[3] + dx, w[4] + dy) for w in ln.words]) for ln in lines]
    matches = ocr.find(lines, text)
    if (not matches or matches[0][0] < 0.9) and WINDOWS:
        try:     # a button that shows an icon, or text Windows reads wrong, may still have its name
            want = ocr._norm(text)
            for ctype, name, (l, t, r, b) in _uia_quick(300, 2):
                if want and want == ocr._norm(name) and mon["left"] <= (l + r) // 2 < mon["left"] + mon["width"]:
                    matches.insert(0, (1.0, ocr.Line(name, l, t, r, b, [])))
                    break
        except Exception:  # noqa: BLE001 - the text read off the screen is still there
            pass
    if not matches and why and not lines:
        raise ToolError(why)
    return matches, lines, SimpleNamespace(**mon, number=monitor)


@tool("click_text", """
Click on words shown on the screen, such as a button's label ("SELL ALL", "Play", "File",
"Save"). It reads the screen to find them (also in games) and clicks their middle: no aiming
needed. When the words are there more than once, nth picks which (1 = top-most).""",
      {"text": {"type": "string"}, "nth": {"type": "integer"},
       "button": {"type": "string", "enum": ["left", "right", "middle"]},
       "clicks": {"type": "integer", "description": "2 for double-click"},
       "hold_seconds": {"type": "number"},
       "monitor": {"type": "integer", "description": "Screen number; default: the one it is working on"}},
      required=["text"], risk=CONTROL, group="control",
      describe=lambda a: f"Click the text '{a.get('text')}'")
def click_text(ctx: ToolContext, a: dict):
    g = _gui()
    want = str(a.get("text") or "").strip()
    if not want:
        raise ToolError("Say which words to click.")
    matches, lines, mon = find_on_screen(ctx, want, a.get("monitor"))
    if not matches:
        seen = [ln.text for ln in lines if len(ln.text) <= 40][:40]
        raise ToolError(f"'{want}' isn't on screen {mon.number}" + (f". Text it can read there: {'; '.join(seen)}."
                                                                     if seen else " (no text could be read there)."))
    top = matches[0][0]
    same = sorted((ln for s, ln in matches if s >= top - 0.05), key=lambda ln: (ln.top // 8, ln.left))
    nth = max(1, int(a.get("nth") or 1))
    if nth > len(same):
        raise ToolError(f"'{want}' is on the screen {len(same)} time(s), so there is no number {nth}.")
    ln = same[nth - 1]
    px, py = ln.center
    hold = _seconds(a)
    _move_to(g, px, py)
    note = _to_front(px, py)
    _click_here(ctx, g, a.get("button") or "left", max(1, min(5, int(a.get("clicks") or 1))), hold)
    ctx.screen["monitor"] = mon.number
    more = f" ({len(same)} matches; this was number {nth}, top to bottom.)" if len(same) > 1 else ""
    said = f"'{ln.text}'" + (f" (for '{want}')" if ocr._norm(ln.text) != ocr._norm(want) else "")
    return after_action(ctx, f"Clicked {said} on screen {mon.number}." + more + note)


@tool("move_mouse", "Move the mouse pointer without clicking (for hover menus and tooltips).",
      {"x": {"type": "number"}, "y": {"type": "number"},
       "mark": {"type": "integer", "description": "A number from the last screenshot with marks=true, instead of x, y"},
       "in_zoom": {"type": "boolean", "description": "x, y are in the last zoomed view, not the screenshot"}},
      risk=CONTROL, group="control", describe=lambda a: f"Move mouse to {_where(a)}")
def move_mouse(ctx: ToolContext, a: dict):
    g = _gui()
    _move_to(g, *_point(ctx, a), 0.15)
    return after_action(ctx, f"Mouse moved to {_where(a)}.")


@tool("move_mouse_by", """
Move the mouse by an amount instead of to a point. In games that hide the pointer, this is what
turns the camera (move_mouse and click positions don't): dx > 0 turns right, dy > 0 looks down.
The amount is in mouse counts, not screen coordinates; how far that turns depends on the game's
sensitivity, so start with about 100-300 and look at the result.""",
      {"dx": {"type": "integer"}, "dy": {"type": "integer"},
       "seconds": {"type": "number", "description": "Spread the move over this long (default 0.2)"}},
      risk=CONTROL, group="control", describe=lambda a: f"Move mouse by ({a.get('dx', 0)}, {a.get('dy', 0)})")
def move_mouse_by(ctx: ToolContext, a: dict):
    g = _gui()
    try:
        dx, dy = int(a.get("dx") or 0), int(a.get("dy") or 0)
    except (TypeError, ValueError):
        raise ToolError("dx and dy must be whole numbers.")
    seconds = _seconds(a, "seconds") if a.get("seconds") is not None else 0.2
    steps = max(1, min(60, int(seconds / 0.01)))     # small steps: one big jump looks like a glitch to some games
    done_x = done_y = 0
    for i in range(1, steps + 1):
        if ctx.stop.is_set():
            break
        sx, sy = round(dx * i / steps) - done_x, round(dy * i / steps) - done_y
        if WINDOWS:
            winput.move_by(sx, sy)
        else:
            g.moveRel(sx, sy)
        done_x, done_y = done_x + sx, done_y + sy
        time.sleep(seconds / steps)
    return after_action(ctx, f"Moved the mouse by ({done_x}, {done_y}).")


@tool("drag", "Drag with the left mouse button from one point to another (or from one mark to another).",
      {"from_x": {"type": "number"}, "from_y": {"type": "number"},
       "to_x": {"type": "number"}, "to_y": {"type": "number"},
       "from_mark": {"type": "integer"}, "to_mark": {"type": "integer"}},
      risk=CONTROL, group="control",
      describe=lambda a: f"Drag from ({a.get('from_x')}, {a.get('from_y')}) to ({a.get('to_x')}, {a.get('to_y')})")
def drag(ctx: ToolContext, a: dict):
    g = _gui()
    x1, y1 = _point(ctx, {**a, "in_zoom": False}, "from_x", "from_y", "from_mark")
    x2, y2 = _point(ctx, {**a, "in_zoom": False}, "to_x", "to_y", "to_mark")
    _move_to(g, x1, y1, 0.1)
    _button(g, "left", False)
    try:
        time.sleep(0.05)
        _move_to(g, x2, y2, 0.4)
    finally:
        _button(g, "left", True)
    return after_action(ctx, "Dragged.")


@tool("scroll", "Scroll the mouse wheel. Positive amount scrolls down, negative scrolls up. Optional point to scroll over.",
      {"amount": {"type": "integer", "description": "Wheel notches, e.g. 5 or -5"},
       "x": {"type": "number"}, "y": {"type": "number"}},
      required=["amount"], risk=CONTROL, group="control",
      describe=lambda a: f"Scroll {a.get('amount')}")
def scroll(ctx: ToolContext, a: dict):
    g = _gui()
    if a.get("x") is not None and a.get("y") is not None:
        _move_to(g, *to_screen(ctx, a["x"], a["y"]), 0.1)
    # Windows counts wheel movement in 120ths of a notch, X11 in notches
    g.scroll(-int(a["amount"]) * (120 if WINDOWS else 1))
    return after_action(ctx, f"Scrolled {a['amount']}.")


@tool("type_text", "Type text with the keyboard into whatever has focus. Set enter=true to press Enter after.",
      {"text": {"type": "string"}, "enter": {"type": "boolean"}},
      required=["text"], risk=CONTROL, group="control",
      describe=lambda a: f"Type: {str(a.get('text') or '')[:200]}")
def type_text(ctx: ToolContext, a: dict):
    g = _gui()
    if a["text"] is None:
        raise ToolError("text is empty.")
    text = str(a["text"])
    caps = _caps_lock_on()
    if caps:      # Caps Lock on turns typed letters upside down (hello -> HELLO, Hi -> hI): off while typing
        _press(ctx, g, ["capslock"], 0)
    try:
        _type(g, text)
    finally:
        if caps:
            _press(ctx, g, ["capslock"], 0)
    if a.get("enter"):
        g.press("enter")
    return after_action(ctx, f"Typed {len(text)} characters.")


def _caps_lock_on() -> bool:
    if not WINDOWS:
        return False
    try:
        return bool(ctypes.windll.user32.GetKeyState(0x14) & 1)
    except Exception:  # noqa: BLE001
        return False


def _type(g, text: str):
    if text.isascii():
        g.write(text, interval=0.004)
    else:  # pyautogui only types ASCII, so paste anything else
        import pyperclip

        old = None
        try:
            old = pyperclip.paste()
        except Exception:
            pass
        pyperclip.copy(text)
        g.hotkey("ctrl", "v")
        time.sleep(0.15)
        if old is not None:
            pyperclip.copy(old)


KEY_ALIASES = {"control": "ctrl", "escape": "esc", "return": "enter", "windows": "win", "super": "win",
               "cmd": "win", "meta": "win", "del": "delete", "pgup": "pageup", "pgdn": "pagedown",
               "page_up": "pageup", "page_down": "pagedown", "arrowup": "up", "arrowdown": "down",
               "arrowleft": "left", "arrowright": "right", "spacebar": "space", "bksp": "backspace"}


def parse_keys(keys) -> list[str]:
    if isinstance(keys, list):
        parts = [str(k) for k in keys]
    else:
        parts = [p for p in str(keys).replace(" ", "+").split("+") if p]
    out = []
    for p in parts:
        k = p.strip().lower()
        out.append(KEY_ALIASES.get(k, k))
    return out


@tool("press_keys", """
Press a key or a shortcut, e.g. "enter", "ctrl+s", "alt+tab", "win+r", "ctrl+shift+esc".
Use repeat to press it several times. In a game, hold_seconds keeps the keys down: "w" with
hold_seconds 2 walks forward for 2 seconds, "w+shift" runs, "w+d" goes forward and right.""",
      {"keys": {"type": "string"}, "repeat": {"type": "integer"},
       "hold_seconds": {"type": "number", "description": "Keep the keys down this long (up to 10)"}},
      required=["keys"], risk=CONTROL, group="control",
      describe=lambda a: f"Press {a.get('keys')}" + (f" x{a['repeat']}" if a.get("repeat") else "")
      + (f", held {a['hold_seconds']}s" if a.get("hold_seconds") else ""))
def press_keys(ctx: ToolContext, a: dict):
    g = _gui()
    keys = parse_keys(a["keys"])
    known = (lambda k: winput.vk_code(k) is not None) if WINDOWS else (lambda k: k in g.KEYBOARD_KEYS)
    bad = [k for k in keys if not known(k)]
    if bad:
        raise ToolError(f"Unknown key name(s): {bad}. Examples: w, a, s, d, space, ctrl, alt, shift, win, enter, "
                        "esc, tab, f5, up.")
    hold = _seconds(a)
    for i in range(max(1, min(50, int(a.get("repeat") or 1)))):
        if ctx.stop.is_set():
            break
        _press(ctx, g, keys, hold)
    return after_action(ctx, f"Pressed {'+'.join(keys)}" + (f", held {hold:g}s" if hold else "") + ".")


# ------------------------------------------------------------------ windows and apps
def _windows():
    _dpi_aware()
    try:
        import pygetwindow as gw
    except Exception as e:
        raise ToolError(f"Window listing is only available on Windows ({e}).")
    if not WINDOWS:
        raise ToolError("Window listing is only available on Windows.")
    return [w for w in gw.getAllWindows() if w.title.strip() and w.width > 0 and w.visible]


@tool("list_windows", "List the open windows (title, position, size, and which one is active).", {},
      group="control")
def list_windows(ctx: ToolContext, a: dict):
    wins = _windows()
    import pygetwindow as gw

    active = gw.getActiveWindow()
    lines = []
    for w in wins:
        mark = "* " if active and w._hWnd == active._hWnd else "  "
        state = " (minimized)" if w.isMinimized else ""
        lines.append(f"{mark}{w.title}  [{w.left},{w.top} {w.width}x{w.height}]{state}")
    return "Open windows (* = active):\n" + "\n".join(lines)


@tool("focus_window", "Bring a window to the front by (part of) its title.",
      {"title": {"type": "string"}}, required=["title"], risk=CONTROL, group="control",
      describe=lambda a: f"Bring '{a.get('title')}' to the front")
def focus_window(ctx: ToolContext, a: dict):
    want = a["title"].lower()
    matches = [w for w in _windows() if want in w.title.lower()]
    if not matches:
        raise ToolError(f"No window title contains '{a['title']}'. Use list_windows.")
    w = matches[0]
    try:
        if w.isMinimized:
            w.restore()
        _gui().press("alt")  # lets Windows hand focus to another app
        w.activate()
    except Exception:
        try:
            w.minimize()
            w.restore()
        except Exception as e:
            raise ToolError(f"Could not focus the window: {e}")
    return after_action(ctx, f"Focused '{w.title}'.")


# Opening one of these runs it, so it asks like run_command instead of like a click.
RUN_EXT = {".exe", ".com", ".bat", ".cmd", ".ps1", ".psm1", ".vbs", ".vbe", ".js", ".jse", ".wsf", ".wsh",
           ".msi", ".msp", ".msc", ".scr", ".cpl", ".hta", ".lnk", ".reg", ".jar", ".py", ".pyw", ".sh", ".appref-ms"}


def _open_risk(ctx: ToolContext, a: dict) -> str:
    import os
    from urllib.parse import urlparse

    target = str(a.get("target") or "").strip().strip('"')
    if "://" in target:   # https://example.com is a web page, https://x/setup.exe or file:///C:/a.bat not
        target = urlparse(target).path
    return RUN if os.path.splitext(target.rstrip("/\\. "))[1].lower() in RUN_EXT else CONTROL


@tool("open", """
Open a program, file, folder or web address the way double-clicking would, e.g. "notepad",
"C:/Users/me/Documents/report.docx", "https://example.com", "ms-settings:display".""",
      {"target": {"type": "string"}}, required=["target"], risk=CONTROL, group="control",
      risk_for=_open_risk, describe=lambda a: f"Open {a.get('target')}")
def open_target(ctx: ToolContext, a: dict):
    import os

    target = str(a["target"]).strip()
    p = ctx.resolve(target)
    if WINDOWS:
        # Only startfile: a "cmd /c start" fallback would run anything after an & in the target.
        try:
            os.startfile(str(p) if p.exists() else target)
        except OSError as e:
            raise ToolError(f"Windows couldn't open {target}: {e}")
    else:
        if p.exists() or "://" in target or target.startswith("mailto:"):
            cmd = ["xdg-open", str(p) if p.exists() else target]
        else:   # a program, by name only: no arguments, like Windows' startfile
            prog = shutil.which(target)
            if not prog:
                raise ToolError(f"There's no file, folder or program called '{target}'. Give a program name "
                                "(e.g. firefox), a path or a web address.")
            cmd = [prog]
        try:
            subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             start_new_session=True)
        except OSError as e:
            raise ToolError(f"Couldn't open {target}: {e}")
    time.sleep(1.5)
    return after_action(ctx, f"Opened {target}.")


# ------------------------------------------------------------------ UI elements (Windows accessibility)
CLICKABLE = {"Button", "MenuItem", "Hyperlink", "ListItem", "TabItem", "CheckBox", "RadioButton",
             "ComboBox", "Edit", "TreeItem", "SplitButton", "DataItem", "Document", "Text"}


def _uia_window(title: str | None = None):
    from pywinauto import Desktop

    desk = Desktop(backend="uia")
    if title:
        want = title.lower()
        wins = [w for w in desk.windows() if want in (w.window_text() or "").lower()]
        if wins:
            return wins[0]
        try:     # accessibility doesn't list some windows (frameless, always on top) that Windows itself does
            import pygetwindow as gw

            hits = [w for w in gw.getAllWindows() if want in (w.title or "").lower()]
            if hits:
                return desk.window(handle=hits[0]._hWnd).wrapper_object()
        except Exception:  # noqa: BLE001 - said below
            pass
        raise ToolError(f"No window title contains '{title}'.")
    hwnd = ctypes.windll.user32.GetForegroundWindow()
    return desk.window(handle=hwnd).wrapper_object()


def _uia_items(win, flt: str, limit: int, seconds: float) -> list[tuple[str, str, tuple[int, int, int, int]]]:
    """(control type, name, (left, top, right, bottom)) of the clickable things in a window (the
    one in front when win is None), for as long as `seconds` allows: big windows have thousands."""
    win = win if win is not None else _uia_window()
    items, start = [], time.time()
    for e in win.descendants():
        if time.time() - start > seconds or len(items) >= limit:
            break
        try:
            info = e.element_info
            name = (info.name or "").strip()
            ctype = info.control_type or ""
            if ctype not in CLICKABLE or not (name or ctype == "Edit"):
                continue
            if flt and flt not in name.lower():
                continue
            r = e.rectangle()
            if r.width() <= 0 or r.height() <= 0:
                continue
            items.append((ctype, name[:80], (r.left, r.top, r.right, r.bottom)))
        except Exception:
            continue
    return items


def _uia_quick(limit: int, seconds: float) -> list:
    """_uia_items of the window in front, given up after `seconds`: listing a big window's parts
    can take many seconds before the first one comes, and a screenshot mustn't wait that long."""
    box: list = []

    def work():
        try:
            import comtypes

            comtypes.CoInitializeEx()     # each thread that uses accessibility needs COM
        except Exception:  # noqa: BLE001 - already set up
            pass
        try:
            box.extend(_uia_items(None, "", limit, seconds))
        except Exception:  # noqa: BLE001 - no marks from accessibility this time
            pass

    th = threading.Thread(target=work, daemon=True, name="uia")
    th.start()
    th.join(seconds + 0.5)
    return list(box) if not th.is_alive() else []


@tool("ui_elements", """
List the buttons, menus, text boxes and links in the active window (or a window whose title
contains 'window') using Windows accessibility. Each has an index you can pass to
click_element. This is often more reliable than clicking by coordinates.""",
      {"window": {"type": "string"}, "filter": {"type": "string", "description": "Only names containing this"}},
      group="control")
def ui_elements(ctx: ToolContext, a: dict):
    if not WINDOWS:
        raise ToolError("ui_elements works on Windows only.")
    try:
        import pywinauto  # noqa: F401
    except ImportError:
        raise ToolError("pywinauto is not installed. Run setup.bat again.")
    win = _uia_window(a.get("window"))
    items = [(ctype, name, ((l + r) // 2, (t + b) // 2))
             for ctype, name, (l, t, r, b) in _uia_items(win, (a.get("filter") or "").lower(), 250, 8)]
    ctx.ui_cache[:] = items
    if not items:
        return f"No clickable elements found in '{win.window_text()}'. Try a screenshot instead."
    lines = [f"[{i}] {t}: {n}" for i, (t, n, _) in enumerate(items)]
    return f"Elements in '{win.window_text()}' (use click_element with the index):\n" + "\n".join(lines)


@tool("click_element", "Click an element from the last ui_elements list by its index.",
      {"index": {"type": "integer"}, "button": {"type": "string", "enum": ["left", "right"]},
       "clicks": {"type": "integer"}},
      required=["index"], risk=CONTROL, group="control",
      describe=lambda a: f"Click element [{a.get('index')}]")
def click_element(ctx: ToolContext, a: dict):
    i = int(a["index"])
    if not 0 <= i < len(ctx.ui_cache):
        raise ToolError("No such index. Run ui_elements first.")
    ctype, name, (px, py) = ctx.ui_cache[i]
    g = _gui()
    _move_to(g, px, py)
    _click_here(ctx, g, a.get("button") or "left", max(1, min(5, int(a.get("clicks") or 1))), 0)
    return after_action(ctx, f"Clicked {ctype} '{name}'.")


# ------------------------------------------------------------------ clipboard
@tool("get_clipboard", "Read the text on the clipboard.", {}, group="control")
def get_clipboard(ctx: ToolContext, a: dict):
    import pyperclip

    return pyperclip.paste() or "(clipboard is empty or not text)"


@tool("set_clipboard", "Put text on the clipboard.", {"text": {"type": "string"}}, required=["text"],
      risk=CONTROL, group="control", describe=lambda a: "Copy text to the clipboard")
def set_clipboard(ctx: ToolContext, a: dict):
    import pyperclip

    pyperclip.copy(a["text"])
    return "Copied to the clipboard."
