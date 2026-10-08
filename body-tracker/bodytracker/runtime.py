"""The running tracker: body source -> engine -> outputs, on background threads.

Both the app window and the console mode drive this same object. All its public
methods are safe to call from the UI thread.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path

from .calibration import CalibrationError
from .config import DEFAULT_PATH, Config
from .engine import Engine
from .headset import Headset
from .outputs import make_outputs

CAPTURE_S = 1.5  # how long you hold still for body calibration
CAMERA_PAIR_TIMEOUT_S = 60.0


class Tracker:
    def __init__(self, cfg: Config, config_path: Path = DEFAULT_PATH, video: str | None = None,
                 video_b: str | None = None):
        self.cfg = cfg
        self.config_path = Path(config_path)
        self.video = video
        self.video_b = video_b
        self.engine = Engine(cfg)
        self.outputs = make_outputs(cfg)
        self.headset = Headset() if (cfg.use_headset and not video) else None
        self.hmd_pose = None
        self.source = None
        self.source_error: str | None = None

        self.paused = False
        self.status = ""
        self.calib_state = None  # None | ("countdown", t_end) | ("capture", t_end)
        self.pairing_started: float | None = None
        self.sent_hz = 0.0
        self.last_frame = None  # newest BodyFrame, for previews
        self.last_output = None  # newest engine Output, for the body view
        self.body_seen = False

        self._lock = threading.Lock()
        self._running = False
        self._threads: list[threading.Thread] = []
        self._source_stop = threading.Event()

    # ------------------------------------------------------------------ lifecycle
    def start(self) -> None:
        self._running = True
        self._start_source()
        t = threading.Thread(target=self._output_loop, name="output", daemon=True)
        t.start()
        self._threads.append(t)

    def stop(self) -> None:
        self._running = False
        self._stop_source()
        for t in self._threads:
            t.join(timeout=1.5)
        self._threads.clear()
        for o in self.outputs:
            try:
                o.disable()
                o.close()
            except OSError:
                pass

    def _start_source(self) -> None:
        from .sources import make_source

        self.source_error = None
        self.last_frame = None
        try:
            self.source = make_source(self.cfg, self.video, self.video_b)
        except Exception as e:  # camera missing, Kinect SDK missing...
            self.source = None
            self.source_error = str(e)
            self.status = f"Could not start: {e}"
            return
        self._source_stop.clear()
        t = threading.Thread(target=self._source_loop, args=(self.source,), name="source", daemon=True)
        t.start()
        self._source_thread = t

    def _stop_source(self) -> None:
        self._source_stop.set()
        t = getattr(self, "_source_thread", None)
        if t is not None:
            t.join(timeout=2.0)
            self._source_thread = None
        if self.source is not None:
            try:
                self.source.close()
            except Exception:
                pass
            self.source = None

    def restart_source(self) -> None:
        """Apply a change of mode or camera."""
        if self.calib_state is not None:
            self.cancel_body_calibration()
        self.pairing_started = None
        self._stop_source()
        self.engine.filter.reset()
        if self._running:
            self._start_source()

    def rebuild_outputs(self) -> None:
        old, self.outputs = self.outputs, make_outputs(self.cfg)
        for o in old:
            try:
                o.disable()
                o.close()
            except OSError:
                pass

    def save(self) -> None:
        self.cfg.save(self.config_path)

    # ------------------------------------------------------------------- threads
    def _source_loop(self, source) -> None:
        while self._running and not self._source_stop.is_set():
            try:
                frame = source.read(0.5)
            except Exception as e:
                self.status = f"Camera error: {e}"
                time.sleep(0.2)
                continue
            if frame is None:
                if getattr(source, "finished", False):
                    break
                continue
            self.body_seen = frame.points is not None
            if frame.points is not None:
                self.engine.update(frame.points, frame.visibility, frame.stamp)
            self.last_frame = frame
            self._tick_calibration(time.perf_counter())

    def _output_loop(self) -> None:
        period = 1.0 / max(self.cfg.output_rate_hz, 1.0)
        next_t = time.perf_counter()
        count, count_t0 = 0, time.perf_counter()
        while self._running:
            now = time.perf_counter()
            if self.headset is not None:
                self.hmd_pose = self.headset.pose()
            out = self.engine.compute(now, self.hmd_pose)
            self.last_output = out
            if out is not None and not self.paused and self.cfg.calibration.valid and out.age_s < 2.0:
                for o in list(self.outputs):
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

    # ------------------------------------------------------------- calibration
    def start_body_calibration(self) -> None:
        self.calib_state = ("countdown", time.perf_counter() + self.cfg.calibration_delay_s)
        self.status = "Get ready: stand straight, face the camera, look straight ahead."

    def cancel_body_calibration(self) -> None:
        self.calib_state = None
        if self.engine.calibrating:
            self.engine._calib_samples = None
        self.status = "Calibration cancelled."

    def calibration_phase(self):
        """(phase, seconds_left) or None."""
        if self.calib_state is None:
            return None
        phase, t_end = self.calib_state
        return phase, max(0.0, t_end - time.perf_counter())

    def _tick_calibration(self, now: float) -> None:
        if self.calib_state is None:
            return
        phase, t_end = self.calib_state
        if now < t_end:
            return
        if phase == "countdown":
            self.engine.begin_calibration()
            self.calib_state = ("capture", now + CAPTURE_S)
            self.status = "Hold still..."
            return
        self.calib_state = None
        try:
            hmd = self.hmd_pose
            c = self.engine.finish_calibration(hmd)
            src = "your headset" if hmd is not None else f"your height ({self.cfg.user_height_m:.2f} m)"
            self.status = f"Calibrated! Size taken from {src}."
            if c.mirror:
                self.status += " (Camera image was mirrored; that's handled.)"
            self.save()
        except CalibrationError as e:
            self.status = f"Calibration failed: {e}"

    # -------------------------------------------------------- camera pairing
    @property
    def can_pair_cameras(self) -> bool:
        return self.source is not None and hasattr(self.source, "begin_camera_calibration")

    def start_camera_pairing(self) -> None:
        if not self.can_pair_cameras:
            return
        self.source.begin_camera_calibration()
        self.pairing_started = time.perf_counter()
        self.status = "Walk around slowly where both cameras can see you. Lift your knees, turn around."

    def camera_pairing_progress(self) -> float | None:
        if self.pairing_started is None or not self.can_pair_cameras:
            return None
        return self.source.camera_calibration_progress()

    def finish_camera_pairing(self) -> bool:
        """Call when progress hits 1.0. Returns True on success."""
        from .stereo import StereoCalibrationError

        self.pairing_started = None
        try:
            r = self.source.finish_camera_calibration()
        except StereoCalibrationError as e:
            self.status = f"Camera pairing failed: {e}"
            return False
        quality = "great" if r.error_px < 3 else "good" if r.error_px < 6 else "rough - try again"
        self.status = f"Cameras paired ({quality}, {r.error_px:.1f} px). Now calibrate your body."
        self.cfg.calibration.valid = False  # the 3D space changed
        self.save()
        return True

    def cancel_camera_pairing(self) -> None:
        if self.can_pair_cameras:
            self.source.cancel_camera_calibration()
        self.pairing_started = None

    # ------------------------------------------------------------------ misc
    def set_paused(self, paused: bool) -> None:
        self.paused = paused
        if paused:
            for o in self.outputs:
                o.disable()

    @property
    def camera_fps(self) -> float:
        return self.source.camera_fps if self.source is not None else 0.0

    @property
    def pose_ms(self) -> float:
        return self.source.pose_ms if self.source is not None else 0.0

    @property
    def headset_connected(self) -> bool:
        return self.hmd_pose is not None

    @property
    def sending(self) -> bool:
        return self.sent_hz > 1 and not self.paused
