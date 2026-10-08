"""Entry point: the app window by default, or a console mode for testing."""

from __future__ import annotations

import argparse
import sys
import threading
import time
from pathlib import Path

from .config import DEFAULT_PATH, Config
from .runtime import Tracker


def run_console(tracker: Tracker, calibrate: bool, duration: float | None) -> None:
    print("Console mode. Type a letter and press Enter: c = calibrate body, p = pair cameras, "
          "space = pause/resume, q = quit")
    stop = threading.Event()

    def commands():
        for line in sys.stdin:
            key = (line.strip()[:1] or " ").lower()
            if key == "c":
                tracker.start_body_calibration()
            elif key == "p":
                tracker.start_camera_pairing()
            elif key == " ":
                tracker.set_paused(not tracker.paused)
            elif key == "q":
                stop.set()
                return
        # stdin closed (e.g. started from a script): keep running until the source ends

    threading.Thread(target=commands, daemon=True).start()
    tracker.start()
    if calibrate:
        tracker.start_body_calibration()
    t0 = time.perf_counter()
    last_status = ""
    try:
        while not stop.is_set():
            time.sleep(0.2)
            if tracker.status != last_status:
                last_status = tracker.status
                print(last_status)
            prog = tracker.camera_pairing_progress()
            if prog is not None and prog >= 1.0:
                tracker.finish_camera_pairing()
            src = tracker.source
            if src is not None and getattr(src, "finished", False):
                break
            if duration is not None and time.perf_counter() - t0 > duration:
                break
    except KeyboardInterrupt:
        pass
    finally:
        tracker.save()
        tracker.stop()


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(prog="bodytracker", description="Low-latency hip/knee/foot trackers")
    ap.add_argument("--config", default=str(DEFAULT_PATH), help="settings file")
    ap.add_argument("--mode", choices=["mono", "stereo", "kinect"], help="override tracking mode")
    ap.add_argument("--video", help="use a video file instead of camera A (testing)")
    ap.add_argument("--video-b", help="video file for camera B (testing two-camera mode)")
    ap.add_argument("--output", choices=["steamvr", "vrchat", "both", "none"], help="override output")
    ap.add_argument("--no-gui", action="store_true", help="run in the console without a window")
    ap.add_argument("--calibrate", action="store_true", help="console mode: start body calibration at once")
    ap.add_argument("--seconds", type=float, help="console mode: quit after this many seconds")
    args = ap.parse_args(argv)

    path = Path(args.config)
    cfg = Config.load(path)
    if args.mode:
        cfg.mode = args.mode
    if args.output:
        cfg.output = args.output
    tracker = Tracker(cfg, path, video=args.video, video_b=args.video_b)

    if args.no_gui:
        run_console(tracker, args.calibrate, args.seconds)
        return
    try:
        from .gui import run_gui
    except ImportError as e:
        print(f"The app window needs PySide6 ({e}). Run setup.bat again, or use --no-gui.")
        run_console(tracker, args.calibrate, args.seconds)
        return
    sys.exit(run_gui(tracker))
