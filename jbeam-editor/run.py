#!/usr/bin/env python3
"""Start the JBeam Editor: python run.py  (then it opens in your browser)."""
import argparse
import os
import sys
import threading
import webbrowser

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from editor import server  # noqa: E402


def main():
    if sys.version_info < (3, 8):
        sys.exit("The JBeam Editor needs Python 3.8 or newer.")
    ap = argparse.ArgumentParser(description="Visual JBeam editor for BeamNG.drive vehicles")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--no-browser", action="store_true", help="don't open the browser")
    ap.add_argument("--data", default=os.path.join(HERE, "data"),
                    help="folder for settings, caches, backups and exports")
    a = ap.parse_args()
    app, httpd, port = server.serve(a.data, a.port)
    url = "http://127.0.0.1:%d/" % port
    print("JBeam Editor is running at %s" % url)
    print("BeamNG user folder: %s" % (app.cfg.get("user_folder") or "(not found - set it in Settings)"))
    print("BeamNG game folder: %s" % (app.cfg.get("game_folder") or "(not found - set it in Settings)"))
    print("Backups of every file it changes go to %s" % os.path.join(a.data, "backups"))
    print("Close this window to stop the editor.")
    if not a.no_browser:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
