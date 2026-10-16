"""Runs inside the virtual computer: a Linux desktop on a screen nobody sees (Xvfb), with a window
manager (openbox) and a panel (tint2). The app starts it (start.sh in WSL, or directly on Linux) and
talks to it with one JSON object per line:

  app -> here   {"id": 1, "op": "tool", "name": "click", "args": {...}, "opts": {...}}
                {"id": 2, "op": "peek", "width": 640}       a small picture for the live view
                {"id": 3, "op": "input", "kind": "click", "x": 10, "y": 20}   the user's own clicks and keys
                {"op": "quit"}
  here -> app   {"event": "ready", "width": 1280, "height": 800} once the desktop is up, then
                {"id": 1, "ok": true, "text": "...", "images": [base64 PNG], "cursor": [x, y]}

The screen tools are the app's own (vtools/screen.py, copied next to this file), so they work here
exactly as they do on Windows. Only the standard library is imported at the top: this file must
also start (and say what's wrong) when a package is missing.
"""

from __future__ import annotations

import argparse
import base64
import io
import json
import os
import queue
import re
import select
import shutil
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

_IN = sys.stdin
_OUT = sys.stdout
_out_lock = threading.Lock()

BACKGROUND = "#2b303b"
# Launchers on the panel, the first of each line that is installed
APPS = [("firefox.desktop",), ("pcmanfm.desktop",), ("org.xfce.mousepad.desktop", "mousepad.desktop"),
        ("libreoffice-writer.desktop",), ("libreoffice-calc.desktop",), ("libreoffice-impress.desktop",),
        ("debian-xterm.desktop", "xterm.desktop")]


def claim_stdio() -> None:
    """The protocol gets private copies of stdin and stdout. fd 0 and 1 are pointed at /dev/null and
    stderr, so nothing started from here (Xvfb, the apps the agent opens) can read or write them."""
    global _IN, _OUT
    _IN = os.fdopen(os.dup(0), "r", encoding="utf-8", errors="replace")
    _OUT = os.fdopen(os.dup(1), "w", encoding="utf-8")
    os.dup2(os.open(os.devnull, os.O_RDONLY), 0)
    os.dup2(2, 1)
    sys.stdout = sys.stderr


def send(msg: dict) -> None:
    line = json.dumps(msg, ensure_ascii=False) + "\n"
    with _out_lock:
        try:
            _OUT.write(line)
            _OUT.flush()
        except (OSError, ValueError):   # the app has gone
            pass


def log(*parts) -> None:
    print("guest:", *parts, file=sys.stderr, flush=True)


# ------------------------------------------------------------------ the desktop
class Desktop:
    def __init__(self, width: int, height: int):
        self.width, self.height = width, height
        self.display = ""
        self.procs: list[subprocess.Popen] = []
        self.dbus_pid = 0

    def start(self) -> None:
        missing = [p for p in ("Xvfb", "openbox") if not shutil.which(p)]
        if missing:
            raise RuntimeError(f"{' and '.join(missing)} not installed. Run the virtual computer's setup again.")
        env = os.environ
        # Only this hidden screen: no way onto the real desktop (WSLg's Wayland, X and sound) from here.
        for k in ("WAYLAND_DISPLAY", "PULSE_SERVER", "DBUS_SESSION_BUS_ADDRESS", "XAUTHORITY", "SESSION_MANAGER",
                  "WSL2_GUI_APPS_ENABLED", "DISPLAY"):
            env.pop(k, None)
        runtime = Path(f"/tmp/runtime-{os.getuid()}")
        runtime.mkdir(mode=0o700, exist_ok=True)
        runtime.chmod(0o700)
        env.update(XDG_RUNTIME_DIR=str(runtime), GDK_BACKEND="x11", MOZ_ENABLE_WAYLAND="0", QT_QPA_PLATFORM="xcb",
                   NO_AT_BRIDGE="1", XDG_SESSION_TYPE="x11", XDG_CURRENT_DESKTOP="openbox",
                   LANG=env.get("LANG") or "C.UTF-8")
        if shutil.which("firefox"):
            env["BROWSER"] = "firefox"     # xdg-open's choice for web addresses without a desktop environment
        self.display = self._xvfb()
        env["DISPLAY"] = self.display
        # Xvfb runs without a cookie, but python-xlib (the mouse and keyboard) won't connect without the file
        auth = Path.home() / ".Xauthority"
        if not auth.exists():
            auth.touch(mode=0o600)
        self._dbus()
        self._run(["xsetroot", "-solid", BACKGROUND], wait=True)
        if shutil.which("xrdb"):
            subprocess.run(["xrdb", "-merge"], input=XRESOURCES, text=True, capture_output=True, timeout=10)
        self.procs.append(self._run(["openbox", "--config-file", str(self._openbox_rc())]))
        self._wait_for_wm()
        if shutil.which("tint2"):
            self.procs.append(self._run(["tint2", "-c", str(self._tint2_rc())]))

    def _run(self, args, wait=False):
        if not shutil.which(args[0]):
            return None
        p = subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=2)
        if wait:
            try:
                p.wait(10)
            except subprocess.TimeoutExpired:
                p.kill()
        return p

    def _xvfb(self) -> str:
        """Starts Xvfb on a free display number, which it reports once it accepts connections."""
        r, w = os.pipe()
        p = subprocess.Popen(["Xvfb", "-displayfd", str(w), "-screen", "0", f"{self.width}x{self.height}x24",
                              "-nolisten", "tcp", "-dpi", "96", "-noreset"],
                             stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=2, pass_fds=(w,))
        os.close(w)
        self.procs.append(p)
        got, end = b"", time.time() + 20
        try:
            while not got.endswith(b"\n") and time.time() < end:
                ready, _, _ = select.select([r], [], [], 0.5)
                if ready:
                    chunk = os.read(r, 64)
                    if not chunk:
                        break
                    got += chunk
                elif p.poll() is not None:
                    break
        finally:
            os.close(r)
        if not got.strip().isdigit():
            raise RuntimeError("The hidden screen (Xvfb) didn't start. Its messages are in the log.")
        return ":" + got.strip().decode()

    def _dbus(self):
        """A session bus, which Firefox, LibreOffice and the file manager expect."""
        if not shutil.which("dbus-launch"):
            return
        try:
            out = subprocess.run(["dbus-launch", "--sh-syntax"], capture_output=True, text=True, timeout=10,
                                 stdin=subprocess.DEVNULL).stdout
        except (OSError, subprocess.SubprocessError) as e:
            log("dbus-launch failed:", e)
            return
        m = re.search(r"DBUS_SESSION_BUS_ADDRESS='([^']*)'", out)
        if m:
            os.environ["DBUS_SESSION_BUS_ADDRESS"] = m.group(1)
        m = re.search(r"DBUS_SESSION_BUS_PID=(\d+)", out)
        self.dbus_pid = int(m.group(1)) if m else 0

    def _openbox_rc(self) -> Path:
        """openbox's own settings with one desktop, so nothing can end up on a desktop nobody sees."""
        path = Path.home() / ".config" / "localagent" / "openbox-rc.xml"
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            rc = Path("/etc/xdg/openbox/rc.xml").read_text(encoding="utf-8")
            rc = re.sub(r"(<desktops>.*?<number>)\d+(</number>)", r"\g<1>1\2", rc, count=1, flags=re.S)
        except OSError:
            rc = "<openbox_config><desktops><number>1</number></desktops></openbox_config>"
        path.write_text(rc, encoding="utf-8")
        return path

    def _tint2_rc(self) -> Path:
        apps = []
        for names in APPS:
            for n in names:
                p = Path("/usr/share/applications") / n
                if p.is_file():
                    apps.append(p)
                    break
        path = Path.home() / ".config" / "localagent" / "tint2rc"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(TINT2.format(launchers="\n".join(f"launcher_item_app = {a}" for a in apps)), encoding="utf-8")
        return path

    def _wait_for_wm(self):
        end = time.time() + 10
        while time.time() < end:
            if not shutil.which("wmctrl") or subprocess.run(["wmctrl", "-m"], capture_output=True).returncode == 0:
                return
            time.sleep(0.1)
        log("the window manager didn't report in; carrying on")

    def stop(self):
        for p in reversed(self.procs):
            if p and p.poll() is None:
                p.terminate()
        for p in reversed(self.procs):
            if p:
                try:
                    p.wait(3)
                except subprocess.TimeoutExpired:
                    p.kill()
        if self.dbus_pid:
            try:
                os.kill(self.dbus_pid, signal.SIGTERM)
            except OSError:
                pass


# A terminal that is easy to read in a screenshot
XRESOURCES = """XTerm*faceName: DejaVu Sans Mono
XTerm*faceSize: 11
XTerm*geometry: 100x30
XTerm*background: #1e2128
XTerm*foreground: #e6e8ee
XTerm*cursorColor: #e6e8ee
XTerm*selectToClipboard: true
XTerm*scrollBar: false
XTerm*saveLines: 5000
"""

TINT2 = """# The virtual computer's panel: app launchers, open windows, a clock
panel_items = LTC
panel_size = 100% 34
panel_position = bottom center horizontal
panel_layer = top
panel_monitor = all
panel_background_id = 1
panel_padding = 4 3 6
strut_policy = follow_size
wm_menu = 1
font_shadow = 0
# 1: panel
rounded = 0
border_width = 0
background_color = #1b1d23 100
border_color = #1b1d23 0
# 2: window buttons
rounded = 4
border_width = 0
background_color = #2a2f3a 100
border_color = #2a2f3a 0
# 3: the active window's button
rounded = 4
border_width = 0
background_color = #3d4a66 100
border_color = #3d4a66 0
launcher_padding = 2 0 6
launcher_background_id = 0
launcher_icon_size = 24
launcher_icon_theme = Adwaita
launcher_tooltip = 1
{launchers}
taskbar_mode = single_desktop
taskbar_padding = 6 0 6
taskbar_background_id = 0
taskbar_name = 0
task_icon = 1
task_text = 1
task_centered = 0
task_maximum_size = 240 28
task_padding = 6 2 6
task_font = Sans 9
task_font_color = #e6e8ee 100
task_background_id = 2
task_active_background_id = 3
task_tooltip = 1
time1_format = %H:%M
time1_font = Sans 10
clock_font_color = #e6e8ee 100
clock_padding = 8 0
clock_background_id = 0
"""


# ------------------------------------------------------------------ the screen tools
class Opts:
    """The app's screen settings, as the screen tools read them (sent along with every call)."""
    screenshot_max_side = 1280
    coord_mode = "auto"
    grid_overlay = False
    screenshot_after_action = True
    monitor = 1
    model = ""


class Tools:
    def __init__(self, work: Path):
        from vtools import base, screen   # noqa: E402 - after the X display is up

        self.base, self.screen = base, screen
        work.mkdir(parents=True, exist_ok=True)
        self.ctx = base.ToolContext(settings=Opts(), workspace=work)
        # mss isn't safe to use from two threads at once (the tools' thread and the live view's)
        self.grab_lock = threading.Lock()
        grab = screen._grab

        def locked_grab(*a, **k):
            with self.grab_lock:
                return grab(*a, **k)
        screen._grab = locked_grab
        self._gui = None

    def gui(self):
        if self._gui is None:
            g = self.screen._gui()
            # Slamming the mouse into a corner is the real desktop's emergency stop. Nobody holds
            # this mouse, so a corner is just a corner.
            g.FAILSAFE_POINTS[:] = []
            self._gui = g
        return self._gui

    def run(self, req: dict) -> dict:
        name = str(req.get("name") or "")
        tool = self.base.REGISTRY.get(name)
        if tool is None or tool.group != "control":
            return {"ok": False, "error": f"There is no screen tool called {name!r} here."}
        o = req.get("opts") or {}
        s = self.ctx.settings
        for k in ("screenshot_max_side", "coord_mode", "grid_overlay", "screenshot_after_action"):
            if k in o:
                setattr(s, k, o[k])
        s.monitor = 1
        self.ctx.screen["model_name"] = str(o.get("model_name") or "")
        self.ctx.capture_after = bool(o.get("capture_after", True))
        self.gui()
        args = req.get("args") if isinstance(req.get("args"), dict) else {}
        try:
            out = tool.func(self.ctx, args)
        except self.base.ToolError as e:
            return {"ok": False, "error": str(e), "cursor": pointer()}
        except Exception as e:  # noqa: BLE001 - goes back to the model like any tool error
            return {"ok": False, "error": f"{type(e).__name__}: {e}", "cursor": pointer()}
        r = out if isinstance(out, self.base.ToolResult) else self.base.ToolResult(str(out))
        return {"ok": r.ok, "text": r.text, "images": [base64.b64encode(i).decode() for i in r.images],
                "cursor": pointer()}

    def user_input(self, req: dict) -> dict:
        """The user's own mouse and keys from the app's live view, in real screen pixels."""
        g = self.gui()
        kind = req.get("kind")
        x, y = int(req.get("x") or 0), int(req.get("y") or 0)
        if kind == "click":
            g.click(x, y, clicks=2 if req.get("double") else 1, interval=0.05, button=req.get("button") or "left")
        elif kind == "drag":
            g.moveTo(x, y)
            g.mouseDown()
            g.moveTo(int(req.get("x2") or x), int(req.get("y2") or y), duration=0.2)
            g.mouseUp()
        elif kind == "scroll":
            g.scroll(-int(req.get("amount") or 0), x=x, y=y)
        elif kind == "keys":
            keys = [k for k in self.screen.parse_keys(req.get("keys") or "") if k in g.KEYBOARD_KEYS]
            if keys:
                g.hotkey(*keys) if len(keys) > 1 else g.press(keys[0])
        elif kind == "text":
            text = str(req.get("text") or "")
            if text.isascii():
                g.write(text)
            else:
                import pyperclip

                pyperclip.copy(text)
                g.hotkey("ctrl", "v")
        else:
            return {"ok": False, "error": f"unknown input {kind!r}"}
        return {"ok": True, "cursor": pointer()}

    def peek(self, req: dict) -> dict:
        from PIL import Image, ImageDraw

        img, _ = self.screen._grab(1)      # the locked one
        real_w, real_h = img.size
        cur = pointer()
        if cur:   # the screen grab has no mouse pointer: draw one
            x, y = cur
            ImageDraw.Draw(img).polygon([(x, y), (x, y + 17), (x + 4, y + 13), (x + 7, y + 20), (x + 10, y + 19),
                                         (x + 7, y + 12), (x + 12, y + 12)], fill="white", outline="black")
        width = max(160, min(real_w, int(req.get("width") or 640)))
        if width < real_w:
            img = img.resize((width, max(1, round(real_h * width / real_w))), Image.BILINEAR)
        buf = io.BytesIO()
        img.save(buf, "JPEG", quality=max(30, min(95, int(req.get("quality") or 70))))
        return {"ok": True, "jpeg": base64.b64encode(buf.getvalue()).decode(), "width": real_w,
                "height": real_h, "cursor": cur, "window": active_title()}


_x = threading.local()   # one X connection per thread


def _xdisplay():
    d = getattr(_x, "d", None)
    if d is None:
        from Xlib import display

        d = _x.d = display.Display()
    return d


def pointer():
    try:
        p = _xdisplay().screen().root.query_pointer()
        return [p.root_x, p.root_y]
    except Exception:  # noqa: BLE001
        return None


def active_title() -> str:
    try:
        from Xlib import X

        d = _xdisplay()
        root = d.screen().root
        prop = root.get_full_property(d.intern_atom("_NET_ACTIVE_WINDOW"), X.AnyPropertyType)
        wid = int(prop.value[0]) if prop and len(prop.value) else 0
        if not wid:
            return ""
        w = d.create_resource_object("window", wid)
        name = w.get_full_property(d.intern_atom("_NET_WM_NAME"), d.intern_atom("UTF8_STRING"))
        value = name.value if name else w.get_wm_name()
        return (value.decode("utf-8", "replace") if isinstance(value, bytes) else str(value or ""))[:200]
    except Exception:  # noqa: BLE001
        return ""


# ------------------------------------------------------------------ main loop
def _worker(q: queue.Queue, handle):
    while True:
        req = q.get()
        if req is None:
            return
        try:
            resp = handle(req)
        except Exception as e:  # noqa: BLE001
            resp = {"ok": False, "error": f"{type(e).__name__}: {e}"}
        if "id" in req:
            send({"id": req["id"], **resp})


def serve(tools: Tools) -> None:
    jobs: queue.Queue = queue.Queue()     # tools and the user's input, one at a time, in order
    peeks: queue.Queue = queue.Queue()    # the live view's pictures, not held up by a slow tool

    def job(req):
        return tools.user_input(req) if req.get("op") == "input" else tools.run(req)
    threading.Thread(target=_worker, args=(jobs, job), daemon=True).start()
    threading.Thread(target=_worker, args=(peeks, tools.peek), daemon=True).start()
    for line in _IN:
        try:
            req = json.loads(line)
        except ValueError:
            continue
        if not isinstance(req, dict):
            continue
        op = req.get("op")
        if op == "quit":
            break
        if op == "ping":
            send({"id": req.get("id"), "ok": True})
        elif op == "peek":
            peeks.put(req)
        elif op in ("tool", "input"):
            jobs.put(req)
        elif "id" in req:
            send({"id": req["id"], "ok": False, "error": f"unknown op {op!r}"})


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--size", default="1280x800")
    ap.add_argument("--work", default=str(Path.home() / "Work"))
    a = ap.parse_args(argv)
    claim_stdio()
    m = re.fullmatch(r"(\d{3,4})x(\d{3,4})", a.size)
    width, height = (int(m.group(1)), int(m.group(2))) if m else (1280, 800)
    desk = Desktop(width, height)
    signal.signal(signal.SIGTERM, lambda *_: (desk.stop(), os._exit(0)))
    try:
        work = Path(a.work)
        work.mkdir(parents=True, exist_ok=True)
        link = Path.home() / "Work"
        if not link.exists() and not link.is_symlink() and work != link:
            link.symlink_to(work, target_is_directory=True)   # ~/Work is the work folder everywhere
        os.chdir(work)        # where the programs it opens start
        desk.start()
        tools = Tools(work)
        tools.gui()     # loads the mouse and keyboard now, so a missing package shows up straight away
    except Exception as e:  # noqa: BLE001
        log("start failed:", repr(e))
        plain = isinstance(e, RuntimeError) or type(e).__name__ == "ToolError"
        send({"event": "error", "message": str(e) if plain else f"{type(e).__name__}: {e}"})
        desk.stop()
        return 1
    send({"event": "ready", "width": width, "height": height, "display": desk.display})
    try:
        serve(tools)
    finally:
        desk.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
