"""The program: camera -> pose -> engine, an output thread, and a small control window."""

from __future__ import annotations

import argparse
import sys
import threading
import time

import cv2
import numpy as np

from .calibration import CalibrationError
from .config import DEFAULT_PATH, Config
from .engine import Engine
from .headset import Headset
from .outputs import make_outputs

WINDOW = "Body Tracker"
CAPTURE_S = 1.5

# MediaPipe pose connections we draw (legs + torso + head line)
DRAW_LINES = [(11, 12), (11, 23), (12, 24), (23, 24), (23, 25), (25, 27), (27, 29), (29, 31),
              (27, 31), (24, 26), (26, 28), (28, 30), (30, 32), (28, 32), (0, 11), (0, 12)]

# (label, config attribute, slider max, to_value, from_value)
SLIDERS = [
    # higher slider = smoother when still (= lower One Euro min_cutoff)
    ("Smoothing", "smoothing", 99, lambda v: (100 - v) / 10.0, lambda x: int(round(100 - x * 10))),
    ("Responsiveness", "responsiveness", 100, lambda v: v / 10.0, lambda x: int(round(x * 10))),
    ("Prediction ms", "prediction_ms", 100, float, lambda x: int(round(x))),
    ("Size %  (100=calib)", "scale_adjust", 40, lambda v: (80 + v) / 100.0, lambda x: int(round(x * 100 - 80))),
    ("Turn deg (180=0)", "yaw_offset_deg", 360, lambda v: v - 180.0, lambda x: int(round(x + 180))),
    ("Left/Right cm (50=0)", "offset_x_m", 100, lambda v: (v - 50) / 100.0, lambda x: int(round(x * 100 + 50))),
    ("Up/Down cm (50=0)", "offset_y_m", 100, lambda v: (v - 50) / 100.0, lambda x: int(round(x * 100 + 50))),
    ("Fwd/Back cm (50=0)", "offset_z_m", 100, lambda v: (v - 50) / 100.0, lambda x: int(round(x * 100 + 50))),
    ("Hip height cm (30=0)", "hip_offset_y_m", 60, lambda v: (v - 30) / 100.0, lambda x: int(round(x * 100 + 30))),
]

PLACEMENT_DEFAULTS = {
    "scale_adjust": 1.0, "yaw_offset_deg": 0.0, "offset_x_m": 0.0,
    "offset_y_m": 0.0, "offset_z_m": 0.0, "hip_offset_y_m": 0.0,
}


class App:
    def __init__(self, cfg: Config, args):
        self.cfg = cfg
        self.args = args
        self.engine = Engine(cfg)
        self.outputs = make_outputs(cfg)
        self.headset = Headset() if (cfg.use_headset and not args.video) else None
        self.hmd_pose = None
        self.paused = False
        self.running = True
        self.status = ""
        self.calib_state = None  # None | ("countdown", t_end) | ("capture", t_end)
        self.infer_ms = 0.0
        self.sent_hz = 0.0
        self.gui = cfg.show_preview and not args.no_preview

        if args.video:
            from .camera import VideoFile
            self.source = VideoFile(args.video, realtime=True)
        else:
            from .camera import Camera
            self.source = Camera(cfg.camera_index, cfg.camera_width, cfg.camera_height,
                                 cfg.camera_fps, cfg.camera_backend)
        from .pose import PoseEstimator
        self.pose = PoseEstimator(cfg.model, cfg.min_detection_confidence, cfg.min_tracking_confidence)

    # ---------------------------------------------------------------- output thread
    def _output_loop(self) -> None:
        period = 1.0 / max(self.cfg.output_rate_hz, 1.0)
        next_t = time.perf_counter()
        count, count_t0 = 0, time.perf_counter()
        while self.running:
            now = time.perf_counter()
            if self.headset is not None:
                self.hmd_pose = self.headset.pose()
            out = self.engine.compute(now, self.hmd_pose)
            # stop sending if the body has not been seen for 2s, rather than freeze forever
            if out is not None and not self.paused and self.cfg.calibration.valid and out.age_s < 2.0:
                for o in self.outputs:
                    try:
                        o.send(out.trackers, out.head_pos, out.head_rot)
                    except OSError:
                        pass
                count += 1
            if now - count_t0 >= 1.0:
                self.sent_hz = count / (now - count_t0)
                count, count_t0 = 0, now
            next_t += period
            delay = next_t - time.perf_counter()
            if delay > 0:
                time.sleep(delay)
            else:
                next_t = time.perf_counter()
        if self.headset is not None:
            self.headset.close()  # same thread that used it

    # -------------------------------------------------------------------- controls
    def start_calibration(self) -> None:
        self.calib_state = ("countdown", time.perf_counter() + self.cfg.calibration_delay_s)
        self.status = "Calibrating: stand straight, face the camera, look ahead"
        print(self.status)

    def _tick_calibration(self, now: float) -> None:
        if self.calib_state is None:
            return
        phase, t_end = self.calib_state
        if now < t_end:
            return
        if phase == "countdown":
            self.engine.begin_calibration()
            self.calib_state = ("capture", now + CAPTURE_S)
            return
        self.calib_state = None
        try:
            hmd = self.hmd_pose
            c = self.engine.finish_calibration(hmd)
            src = "headset height" if hmd is not None else f"your height {self.cfg.user_height_m:.2f} m"
            mirrored = ", mirrored" if c.mirror else ""
            self.status = f"Calibrated (size x{c.scale:.2f} from {src}{mirrored}). Press S to save."
        except CalibrationError as e:
            self.status = f"Calibration failed: {e}"
        print(self.status)

    def save(self) -> None:
        self.cfg.save(self.args.config)
        self.status = f"Saved settings to {self.args.config}"
        print(self.status)

    def handle_key(self, key: str) -> None:
        key = key.lower()
        if key == "c":
            self.start_calibration()
        elif key == "s":
            self.save()
        elif key == "p" or key == " ":
            self.paused = not self.paused
            for o in self.outputs:
                if self.paused:
                    o.disable()
            self.status = "Paused - trackers off" if self.paused else "Sending"
        elif key == "r":
            for k, v in PLACEMENT_DEFAULTS.items():
                setattr(self.cfg, k, v)
            self._sync_sliders()
            self.status = "Placement adjustments reset"
        elif key in ("q", "\x1b"):
            self.running = False

    # ------------------------------------------------------------------------ GUI
    def _setup_window(self) -> None:
        cv2.namedWindow(WINDOW, cv2.WINDOW_NORMAL)
        # some backends fire the callback on creation, so remember the real values first
        saved = {attr: getattr(self.cfg, attr) for _label, attr, *_rest in SLIDERS}
        for label, attr, maximum, to_value, _from in SLIDERS:
            def cb(v, attr=attr, to_value=to_value):
                setattr(self.cfg, attr, to_value(v))
            cv2.createTrackbar(label, WINDOW, 0, maximum, cb)
        for attr, value in saved.items():
            setattr(self.cfg, attr, value)
        self._sync_sliders()

    def _sync_sliders(self) -> None:
        if not self.gui:
            return
        for label, attr, maximum, _to, from_value in SLIDERS:
            cv2.setTrackbarPos(label, WINDOW, max(0, min(maximum, from_value(getattr(self.cfg, attr)))))

    def _draw(self, frame, result, now) -> np.ndarray:
        img = frame.copy()
        h, w = img.shape[:2]
        if result is not None:
            pts = (result.image * [w, h]).astype(int)
            for a, b in DRAW_LINES:
                ok = result.visibility[a] > self.cfg.min_visibility and result.visibility[b] > self.cfg.min_visibility
                cv2.line(img, tuple(pts[a]), tuple(pts[b]), (0, 220, 0) if ok else (0, 0, 220), 2)
            for i in (23, 24, 25, 26, 27, 28):
                cv2.circle(img, tuple(pts[i]), 6, (255, 200, 0), -1)
        hmd = "headset: none"
        if self.headset is not None:
            hmd = "headset: OK" if self.hmd_pose is not None else "headset: waiting for SteamVR"
        calibrated = "CALIBRATED" if self.cfg.calibration.valid else "NOT CALIBRATED - press C"
        lines = [
            f"camera {self.source.measured_fps:4.0f} fps | pose {self.infer_ms:4.1f} ms | send {self.sent_hz:4.0f} Hz",
            f"output: {self.cfg.output} | {hmd} | {calibrated}",
            "C calibrate  S save  P pause  R reset placement  Q quit",
            self.status,
        ]
        if self.calib_state is not None:
            phase, t_end = self.calib_state
            if phase == "countdown":
                lines.append(f"Stand still in {t_end - now:0.1f}s ...")
            else:
                lines.append("Hold still ...")
        lines = [line for line in lines if line]
        # darken a panel behind the text so it reads on any background
        panel = img[0:22 * len(lines) + 8, :]
        panel[:] = (panel * 0.35).astype(panel.dtype)
        y = 22
        for line in lines:
            cv2.putText(img, line, (10, y), cv2.FONT_HERSHEY_SIMPLEX, 0.55, (255, 255, 255), 1, cv2.LINE_AA)
            y += 22
        return img

    def _stdin_loop(self) -> None:
        for line in sys.stdin:
            self.handle_key(line.strip()[:1] or " ")
            if not self.running:
                break

    # ----------------------------------------------------------------------- main
    def run(self) -> None:
        print(f"Camera: {self.source.describe()}")
        if self.headset is not None and self.headset.error:
            print(f"Headset: {self.headset.error}. Trackers will be placed using the camera only.")
        if self.gui:
            try:
                self._setup_window()
            except cv2.error:
                print("No window support in this OpenCV build; running without preview.")
                self.gui = False
        if not self.gui:
            print("Type a letter and press Enter: c = calibrate, s = save, p = pause, r = reset, q = quit")
            threading.Thread(target=self._stdin_loop, daemon=True).start()
        if not self.cfg.calibration.valid:
            self.status = "Not calibrated: stand where the camera sees all of you and press C"
            print(self.status)
        if self.args.calibrate:
            self.start_calibration()

        out_thread = threading.Thread(target=self._output_loop, name="output", daemon=True)
        out_thread.start()
        seq = 0
        try:
            while self.running:
                got = self.source.read(seq)
                if got is None:
                    if getattr(self.source, "finished", False):
                        break
                    continue
                seq, stamp, frame = got
                t0 = time.perf_counter()
                result = self.pose.process(frame, stamp)
                self.infer_ms += 0.1 * ((time.perf_counter() - t0) * 1000 - self.infer_ms)
                if result is not None:
                    self.engine.update(result.world, result.visibility, stamp)
                now = time.perf_counter()
                self._tick_calibration(now)
                if self.gui:
                    cv2.imshow(WINDOW, self._draw(frame, result, now))
                    k = cv2.waitKey(1)
                    if k != -1:
                        self.handle_key(chr(k & 0xFF))
                    if cv2.getWindowProperty(WINDOW, cv2.WND_PROP_VISIBLE) < 1:
                        self.running = False
        except KeyboardInterrupt:
            pass
        finally:
            self.running = False
            out_thread.join(timeout=1.0)
            for o in self.outputs:
                try:
                    o.disable()
                    o.close()
                except OSError:
                    pass
            self.source.close()
            self.pose.close()
            if self.gui:
                cv2.destroyAllWindows()


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(prog="bodytracker", description="Low-latency webcam hip/knee/foot trackers")
    ap.add_argument("--config", default=str(DEFAULT_PATH), help="settings file (created on save)")
    ap.add_argument("--video", help="use a video file instead of the camera (testing)")
    ap.add_argument("--output", choices=["steamvr", "vrchat", "both", "none"], help="override output")
    ap.add_argument("--camera", type=int, help="camera index override")
    ap.add_argument("--model", choices=["lite", "full", "heavy"], help="pose model override")
    ap.add_argument("--no-preview", action="store_true", help="no window (control from the console)")
    ap.add_argument("--calibrate", action="store_true", help="start calibration countdown right away")
    ap.add_argument("--list-cameras", action="store_true", help="print which camera indexes open, then exit")
    args = ap.parse_args(argv)

    if args.list_cameras:
        for i in range(6):
            cap = cv2.VideoCapture(i)
            if cap.isOpened():
                ok, frame = cap.read()
                print(f"camera {i}: {'%dx%d' % (frame.shape[1], frame.shape[0]) if ok else 'opens, no frame'}")
            cap.release()
        return

    from pathlib import Path
    args.config = Path(args.config)
    cfg = Config.load(args.config)
    if args.output:
        cfg.output = args.output
    if args.camera is not None:
        cfg.camera_index = args.camera
    if args.model:
        cfg.model = args.model
    App(cfg, args).run()
