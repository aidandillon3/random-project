"""Seeing the screen and using the mouse and keyboard.

Coordinates: the model works in the coordinates of the last screenshot it saw. Depending on
the model that is either the screenshot's own pixels or a 0-1000 grid over the screen (Qwen
vision models are trained on the 0-1000 grid). Everything here converts to real screen
pixels before touching the mouse.
"""

from __future__ import annotations

import ctypes
import io
import sys
import time

from .base import CONTROL, RUN, ToolContext, ToolError, ToolResult, tool

WINDOWS = sys.platform == "win32"

if WINDOWS:  # real pixels everywhere, so screenshots and clicks agree on high-DPI screens
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


def to_screen(ctx: ToolContext, x: float, y: float) -> tuple[int, int]:
    s = ctx.screen
    if not s:
        _shot(ctx)  # take one so we know the geometry
        s = ctx.screen
    if coord_mode(ctx) == "normalized":
        fx, fy = float(x) / 1000.0, float(y) / 1000.0
    else:
        fx, fy = float(x) / s["img_w"], float(y) / s["img_h"]
    px, py = int(s["left"] + fx * s["width"]), int(s["top"] + fy * s["height"])
    # Stay 1 pixel off the edges: a corner is pyautogui's emergency stop, and x=0 / x=1000 would land on one.
    return (min(max(px, s["left"] + 1), s["left"] + s["width"] - 2),
            min(max(py, s["top"] + 1), s["top"] + s["height"] - 2))


def from_screen(ctx: ToolContext, px: int, py: int) -> tuple[int, int]:
    s = ctx.screen
    fx, fy = (px - s["left"]) / s["width"], (py - s["top"]) / s["height"]
    if coord_mode(ctx) == "normalized":
        return round(fx * 1000), round(fy * 1000)
    return round(fx * s["img_w"]), round(fy * s["img_h"])


# ------------------------------------------------------------------ capture
def _grab(monitor: int, region=None):
    import mss
    from PIL import Image

    with mss.mss() as sct:
        mons = sct.monitors
        if monitor < 0 or monitor >= len(mons):
            monitor = 1 if len(mons) > 1 else 0
        mon = mons[monitor]
        box = region or mon
        raw = sct.grab(box)
        img = Image.frombytes("RGB", raw.size, raw.rgb)
    return img, mon


def _cursor():
    try:
        import pyautogui

        return pyautogui.position()
    except Exception:
        return None


def _png(img) -> bytes:
    buf = io.BytesIO()
    img.save(buf, "PNG", optimize=False, compress_level=3)
    return buf.getvalue()


def _shot(ctx: ToolContext, monitor: int | None = None) -> bytes:
    from PIL import Image, ImageDraw

    monitor = ctx.settings.monitor if monitor is None else monitor
    img, mon = _grab(monitor)
    real_w, real_h = img.size
    # draw the mouse pointer so the model can see where it is
    cur = _cursor()
    draw = ImageDraw.Draw(img)
    if cur and mon["left"] <= cur[0] < mon["left"] + real_w and mon["top"] <= cur[1] < mon["top"] + real_h:
        cx, cy = cur[0] - mon["left"], cur[1] - mon["top"]
        r = max(6, real_w // 250)
        draw.ellipse([cx - r, cy - r, cx + r, cy + r], outline=(255, 0, 0), width=max(2, r // 3))
    max_side = int(ctx.settings.screenshot_max_side or 1280)
    scale = min(1.0, max_side / max(real_w, real_h))
    if scale < 1.0:
        img = img.resize((round(real_w * scale), round(real_h * scale)), Image.LANCZOS)
    ctx.screen.update(left=mon["left"], top=mon["top"], width=real_w, height=real_h,
                      img_w=img.size[0], img_h=img.size[1], monitor=monitor)
    if ctx.settings.grid_overlay:
        _grid(ctx, img)
    return _png(img)


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
    s = ctx.screen
    return (f"Screen {s['monitor']}: {s['width']}x{s['height']} real pixels, image shown at "
            f"{s['img_w']}x{s['img_h']}. {coord_help(ctx)}")


def after_action(ctx: ToolContext, text: str) -> ToolResult:
    # capture_after is False for all but the last control action of a step: one fresh screenshot
    # at the end instead of several stale ones.
    if not ctx.settings.screenshot_after_action or not getattr(ctx, "capture_after", True):
        return ToolResult(text)
    time.sleep(0.6)
    try:
        png = _shot(ctx, ctx.screen.get("monitor"))   # the screen the model is working on
    except Exception as e:  # never fail the action because the follow-up shot failed
        return ToolResult(f"{text}\n(could not take a follow-up screenshot: {e})")
    return ToolResult(f"{text}\nScreenshot after the action is attached.", images=[png])


@tool("screenshot", """
Take a screenshot so you can see the screen. The red circle is the mouse pointer. Use the
coordinates of this image with click, drag, scroll and zoom.""",
      {"monitor": {"type": "integer", "description": "1 = main screen, 2 = second screen... Default: the one set in settings"}},
      group="control")
def screenshot(ctx: ToolContext, a: dict):
    png = _shot(ctx, a.get("monitor"))
    return ToolResult(_geometry_note(ctx), images=[png])


@tool("zoom", """
Look closer at part of the screen at full resolution, to read small text. Give the box in
the same coordinates as the last screenshot.""",
      {"x": {"type": "number"}, "y": {"type": "number"},
       "width": {"type": "number"}, "height": {"type": "number"}},
      required=["x", "y", "width", "height"], group="control")
def zoom(ctx: ToolContext, a: dict):
    from PIL import Image

    x1, y1 = to_screen(ctx, a["x"], a["y"])
    x2, y2 = to_screen(ctx, float(a["x"]) + float(a["width"]), float(a["y"]) + float(a["height"]))
    if x2 - x1 < 4 or y2 - y1 < 4:
        raise ToolError("That box is too small.")
    img, _ = _grab(ctx.screen.get("monitor", ctx.settings.monitor),
                   {"left": x1, "top": y1, "width": x2 - x1, "height": y2 - y1})
    max_side = int(ctx.settings.screenshot_max_side or 1280)
    scale = min(3.0, max_side / max(img.size))
    img = img.resize((max(1, round(img.width * scale)), max(1, round(img.height * scale))), Image.LANCZOS)
    return ToolResult("Zoomed view attached. Keep using the full-screenshot coordinates for clicks.",
                      images=[_png(img)])


# ------------------------------------------------------------------ mouse and keyboard
def _gui():
    try:
        import pyautogui
    except (Exception, SystemExit) as e:  # pragma: no cover - depends on the desktop
        raise ToolError(f"Mouse and keyboard control is not available: {e}")
    pyautogui.FAILSAFE = True   # slam the mouse into a screen corner to stop it
    pyautogui.PAUSE = 0.03
    return pyautogui


def _where(a: dict) -> str:
    return f"({a.get('x')}, {a.get('y')})"


@tool("click", "Click at a point on the screen (coordinates of the last screenshot).",
      {"x": {"type": "number"}, "y": {"type": "number"},
       "button": {"type": "string", "enum": ["left", "right", "middle"]},
       "clicks": {"type": "integer", "description": "2 for double-click"}},
      required=["x", "y"], risk=CONTROL, group="control",
      describe=lambda a: f"{a.get('button', 'left')} click x{a.get('clicks', 1)} at {_where(a)}")
def click(ctx: ToolContext, a: dict):
    g = _gui()
    px, py = to_screen(ctx, a["x"], a["y"])
    g.click(px, py, clicks=int(a.get("clicks") or 1), interval=0.08, button=a.get("button") or "left")
    return after_action(ctx, f"Clicked at {_where(a)}.")


@tool("move_mouse", "Move the mouse pointer without clicking (for hover menus and tooltips).",
      {"x": {"type": "number"}, "y": {"type": "number"}}, required=["x", "y"], risk=CONTROL,
      group="control", describe=lambda a: f"Move mouse to {_where(a)}")
def move_mouse(ctx: ToolContext, a: dict):
    g = _gui()
    g.moveTo(*to_screen(ctx, a["x"], a["y"]), duration=0.15)
    return after_action(ctx, f"Mouse moved to {_where(a)}.")


@tool("drag", "Drag with the left mouse button from one point to another.",
      {"from_x": {"type": "number"}, "from_y": {"type": "number"},
       "to_x": {"type": "number"}, "to_y": {"type": "number"}},
      required=["from_x", "from_y", "to_x", "to_y"], risk=CONTROL, group="control",
      describe=lambda a: f"Drag from ({a.get('from_x')}, {a.get('from_y')}) to ({a.get('to_x')}, {a.get('to_y')})")
def drag(ctx: ToolContext, a: dict):
    g = _gui()
    x1, y1 = to_screen(ctx, a["from_x"], a["from_y"])
    x2, y2 = to_screen(ctx, a["to_x"], a["to_y"])
    g.moveTo(x1, y1, duration=0.1)
    g.mouseDown()
    g.moveTo(x2, y2, duration=0.4)
    g.mouseUp()
    return after_action(ctx, "Dragged.")


@tool("scroll", "Scroll the mouse wheel. Positive amount scrolls down, negative scrolls up. Optional point to scroll over.",
      {"amount": {"type": "integer", "description": "Wheel notches, e.g. 5 or -5"},
       "x": {"type": "number"}, "y": {"type": "number"}},
      required=["amount"], risk=CONTROL, group="control",
      describe=lambda a: f"Scroll {a.get('amount')}")
def scroll(ctx: ToolContext, a: dict):
    g = _gui()
    if a.get("x") is not None and a.get("y") is not None:
        g.moveTo(*to_screen(ctx, a["x"], a["y"]), duration=0.1)
    g.scroll(-int(a["amount"]) * 120)
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
    if a.get("enter"):
        g.press("enter")
    return after_action(ctx, f"Typed {len(text)} characters.")


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
Use repeat to press it several times.""",
      {"keys": {"type": "string"}, "repeat": {"type": "integer"}},
      required=["keys"], risk=CONTROL, group="control",
      describe=lambda a: f"Press {a.get('keys')}" + (f" x{a['repeat']}" if a.get("repeat") else ""))
def press_keys(ctx: ToolContext, a: dict):
    g = _gui()
    keys = parse_keys(a["keys"])
    bad = [k for k in keys if k not in g.KEYBOARD_KEYS]
    if bad:
        raise ToolError(f"Unknown key name(s): {bad}. Examples: ctrl, alt, shift, win, enter, esc, tab, f5, up.")
    for _ in range(max(1, min(50, int(a.get("repeat") or 1)))):
        g.hotkey(*keys) if len(keys) > 1 else g.press(keys[0])
    return after_action(ctx, f"Pressed {'+'.join(keys)}.")


# ------------------------------------------------------------------ windows and apps
def _windows():
    try:
        import pygetwindow as gw
    except Exception as e:
        raise ToolError(f"Window listing is only available on Windows ({e}).")
    return [w for w in gw.getAllWindows() if w.title.strip() and w.width > 0 and w.visible]


@tool("list_windows", "List the open windows (title, position, size, and which one is active).", {},
      group="control")
def list_windows(ctx: ToolContext, a: dict):
    import pygetwindow as gw

    wins = _windows()
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
    import subprocess

    target = str(a["target"]).strip()
    p = ctx.resolve(target)
    if WINDOWS:
        # Only startfile: a "cmd /c start" fallback would run anything after an & in the target.
        try:
            os.startfile(str(p) if p.exists() else target)
        except OSError as e:
            raise ToolError(f"Windows couldn't open {target}: {e}")
    else:
        subprocess.Popen(["xdg-open", str(p) if p.exists() else target])
    time.sleep(1.5)
    return after_action(ctx, f"Opened {target}.")


# ------------------------------------------------------------------ UI elements (Windows accessibility)
CLICKABLE = {"Button", "MenuItem", "Hyperlink", "ListItem", "TabItem", "CheckBox", "RadioButton",
             "ComboBox", "Edit", "TreeItem", "SplitButton", "DataItem", "Document", "Text"}


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
        from pywinauto import Desktop
    except ImportError:
        raise ToolError("pywinauto is not installed. Run setup.bat again.")
    desk = Desktop(backend="uia")
    if a.get("window"):
        want = a["window"].lower()
        wins = [w for w in desk.windows() if want in (w.window_text() or "").lower()]
        if not wins:
            raise ToolError(f"No window title contains '{a['window']}'.")
        win = wins[0]
    else:
        hwnd = ctypes.windll.user32.GetForegroundWindow()
        win = desk.window(handle=hwnd).wrapper_object()
    flt = (a.get("filter") or "").lower()
    items, start = [], time.time()
    for e in win.descendants():
        if time.time() - start > 8 or len(items) >= 250:
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
            items.append((ctype, name[:80], ((r.left + r.right) // 2, (r.top + r.bottom) // 2)))
        except Exception:
            continue
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
    _gui().click(px, py, clicks=int(a.get("clicks") or 1), button=a.get("button") or "left")
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
