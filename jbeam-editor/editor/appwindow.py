"""Show the editor in its own window instead of a browser tab.

In order of preference:
  1. a native window from pywebview, if it is installed (pip install pywebview);
  2. Microsoft Edge, Chrome or Brave in "app" mode: a plain window with no
     tabs or address bar, using its own profile folder so it is a separate app;
  3. the default web browser.

The server keeps running while a page is open: pages send a heartbeat, and
when the last window closes the editor stops by itself.
"""
import os
import shutil
import subprocess
import sys
import threading
import time
import webbrowser

WINDOW_MODES = ("auto", "native", "app", "browser")


def find_chromium():
    """Path of Edge, Chrome, Chromium or Brave, or ''."""
    cands = []
    if sys.platform.startswith("win"):
        try:
            import winreg
            for exe in ("msedge.exe", "chrome.exe", "brave.exe"):
                for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
                    try:
                        cands.append(winreg.QueryValue(
                            hive, r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\%s" % exe))
                    except OSError:
                        pass
        except ImportError:
            pass
        for env in ("PROGRAMFILES(X86)", "PROGRAMFILES", "LOCALAPPDATA"):
            base = os.environ.get(env)
            if base:
                cands += [os.path.join(base, "Microsoft", "Edge", "Application", "msedge.exe"),
                          os.path.join(base, "Google", "Chrome", "Application", "chrome.exe"),
                          os.path.join(base, "BraveSoftware", "Brave-Browser", "Application", "brave.exe")]
    elif sys.platform == "darwin":
        for app in ("Microsoft Edge", "Google Chrome", "Chromium", "Brave Browser"):
            cands.append("/Applications/%s.app/Contents/MacOS/%s" % (app, app))
    else:
        for name in ("microsoft-edge", "microsoft-edge-stable", "google-chrome", "google-chrome-stable",
                     "chromium", "chromium-browser", "brave-browser"):
            p = shutil.which(name)
            if p:
                cands.append(p)
    for c in cands:
        if c and os.path.isfile(c.strip('"')):
            return c.strip('"')
    return ""


def have_pywebview():
    try:
        import webview  # noqa: F401
        return True
    except Exception:
        return False


def available():
    """What this computer can do, for the Settings page."""
    return {"native": have_pywebview(), "app": find_chromium(), "modes": list(WINDOW_MODES)}


class Lifetime:
    """Stops the editor once no editor page is open any more.

    Every open page holds a connection to /api/events. When the last one
    closes and none comes back within `grace` seconds (a reload reconnects
    at once), stop() is called. If no page ever connects, it stops after
    `first_wait` seconds."""

    def __init__(self, stop, grace=8.0, first_wait=120.0):
        self.stop = stop
        self.grace = grace
        self.first_wait = first_wait
        self.count = 0
        self.seen = False
        self.empty_since = None
        self.started = time.time()
        self.lock = threading.Lock()

    def connect(self):
        with self.lock:
            self.count += 1
            self.seen = True
            self.empty_since = None

    def disconnect(self):
        with self.lock:
            self.count = max(0, self.count - 1)
            if self.count == 0:
                self.empty_since = time.time()

    def should_stop(self, now=None):
        now = now or time.time()
        with self.lock:
            if not self.seen:
                return now - self.started > self.first_wait
            return self.count == 0 and self.empty_since is not None and now - self.empty_since > self.grace

    def watch(self):
        def loop():
            while not self.should_stop():
                time.sleep(1)
            self.stop()
        threading.Thread(target=loop, daemon=True).start()


def open_window(url, mode, data_dir, title="JBeam Editor", icon=None, on_closed=None, log=print):
    """Open the editor; returns how it was shown ("native", "app" or "browser").

    For "native" this blocks until the window is closed (pywebview needs the
    main thread); the other modes return at once."""
    if mode not in WINDOW_MODES:
        mode = "auto"
    if mode in ("auto", "native") and have_pywebview():
        try:
            import webview
            kw = {"width": 1500, "height": 940, "min_size": (1000, 640), "background_color": "#15171c",
                  "text_select": True}
            webview.create_window(title, url, **kw)
            start_kw = {}
            if icon and os.path.isfile(icon):
                start_kw["icon"] = icon
            try:
                webview.start(private_mode=False, storage_path=os.path.join(data_dir, "window"), **start_kw)
            except TypeError:
                webview.start()
            if on_closed:
                on_closed()
            return "native"
        except Exception as e:  # fall back to the next way
            log("Native window failed (%s); using an app window instead." % e)
    if mode in ("auto", "native", "app"):
        exe = find_chromium()
        if exe:
            profile = os.path.join(data_dir, "window-profile")
            os.makedirs(profile, exist_ok=True)
            args = [exe, "--app=" + url, "--user-data-dir=" + profile, "--no-first-run",
                    "--no-default-browser-check", "--window-size=1500,940", "--disable-features=Translate",
                    "--disable-background-mode", "--class=JBeamEditor"]
            try:
                kw = {}
                if sys.platform.startswith("win"):
                    kw["creationflags"] = 0x08000000  # CREATE_NO_WINDOW for the launcher process
                proc = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **kw)
                if on_closed:
                    def wait():
                        t0 = time.time()
                        proc.wait()
                        # a quick exit means it handed the window to an open instance: the
                        # heartbeat decides when to stop instead
                        if time.time() - t0 > 5:
                            on_closed()
                    threading.Thread(target=wait, daemon=True).start()
                return "app"
            except OSError as e:
                log("Could not start %s (%s); using the web browser." % (exe, e))
    webbrowser.open(url)
    return "browser"
