#!/usr/bin/env python3
"""Start the JBeam Editor in its own window:  python run.py

Options: --window auto|native|app|browser picks how it is shown (see
editor/appwindow.py), --no-window only starts the server (for development),
--port and --data as before.
"""
import argparse
import json
import os
import sys
import threading
import traceback
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)


def _log_to_file(data_dir):
    """Without a console (pythonw) print() output goes to data/editor.log."""
    os.makedirs(data_dir, exist_ok=True)
    path = os.path.join(data_dir, "editor.log")
    try:
        if os.path.getsize(path) > 2_000_000:
            os.replace(path, path + ".old")
    except OSError:
        pass
    f = open(path, "a", encoding="utf-8", buffering=1)
    sys.stdout = sys.stderr = f
    return path


def _message_box(text):
    if sys.platform.startswith("win"):
        try:
            import ctypes
            ctypes.windll.user32.MessageBoxW(None, text, "JBeam Editor", 0x10)
            return
        except Exception:
            pass
    print(text)


def _running_instance(data_dir):
    """URL of an editor already running on this data folder, or None."""
    try:
        with open(os.path.join(data_dir, "running.json"), encoding="utf-8") as f:
            info = json.load(f)
        url = "http://127.0.0.1:%d/" % int(info["port"])
        with urllib.request.urlopen(url + "api/ping", timeout=1.5) as r:
            ping = json.loads(r.read().decode("utf-8"))
        if ping.get("app") == "jbeam-editor" and os.path.normcase(ping.get("dataDir", "")) == \
                os.path.normcase(os.path.abspath(data_dir)):
            return url
    except Exception:
        pass
    return None


def main():
    if sys.version_info < (3, 8):
        sys.exit("The JBeam Editor needs Python 3.8 or newer.")
    ap = argparse.ArgumentParser(description="Visual JBeam editor for BeamNG.drive vehicles")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--data", default=os.path.join(HERE, "data"),
                    help="folder for settings, caches, backups and exports")
    ap.add_argument("--window", choices=["auto", "native", "app", "browser"],
                    help="how to show the editor (default: the choice in Settings)")
    ap.add_argument("--no-window", action="store_true", help="only start the server")
    ap.add_argument("--no-browser", action="store_true", help=argparse.SUPPRESS)  # old name of --no-window
    a = ap.parse_args()
    data = os.path.abspath(a.data)
    if sys.stdout is None or sys.stderr is None:
        _log_to_file(data)

    from editor import appwindow, server

    no_window = a.no_window or a.no_browser
    if not no_window:
        url = _running_instance(data)
        if url:
            # already running: just open another window onto it
            cfg_mode = a.window or "auto"
            appwindow.open_window(url, cfg_mode, data, log=print)
            return

    app, httpd, port = server.serve(data, a.port)
    url = "http://127.0.0.1:%d/" % port
    print("JBeam Editor is running at %s" % url)
    print("BeamNG user folder: %s" % (app.cfg.get("user_folder") or "(not found - set it in Settings)"))
    print("BeamNG game folder: %s" % (app.cfg.get("game_folder") or "(not found - set it in Settings)"))
    print("Backups of every file it changes go to %s" % os.path.join(data, "backups"))
    running = os.path.join(data, "running.json")
    try:
        with open(running, "w", encoding="utf-8") as f:
            json.dump({"port": port, "pid": os.getpid()}, f)
    except OSError:
        pass

    done = threading.Event()

    def stop():
        if not done.is_set():
            done.set()
            threading.Thread(target=httpd.shutdown, daemon=True).start()

    serving = threading.Thread(target=httpd.serve_forever, daemon=True)
    serving.start()
    try:
        if no_window:
            print("Press Ctrl+C to stop.")
        else:
            app.lifetime = appwindow.Lifetime(stop)
            app.lifetime.watch()
            mode = a.window or app.cfg.get("window") or "auto"
            icon = os.path.join(HERE, "web", "img", "icon.ico")
            how = appwindow.open_window(url, mode, data, icon=icon, on_closed=stop, log=print)
            app.window_mode = how
            print("Opened in %s mode. The editor stops when its window is closed." % how)
            if how == "native":
                stop()   # open_window returned: the window was closed
        while not done.wait(0.5):
            pass
    except KeyboardInterrupt:
        stop()
    finally:
        try:
            os.remove(running)
        except OSError:
            pass
        app.ws.save_caches()


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception:
        tb = traceback.format_exc()
        print(tb)
        _message_box("The JBeam Editor could not start:\n\n" + tb[-1500:])
        sys.exit(1)
