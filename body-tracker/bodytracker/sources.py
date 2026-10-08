"""Body sources: one camera, two cameras, or a Kinect.

Every source hands the engine the same thing: 33 body points in MediaPipe-style
camera axes (x right, y down, z away, meters, centred between the hips) plus how
visible each point is. Everything downstream (calibration, smoothing, outputs)
doesn't care where they came from.
"""

from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

import numpy as np

from .config import Config


@dataclass
class View:
    """One camera image for the preview, with the 2D skeleton found on it."""
    name: str
    frame: np.ndarray | None
    image_xy: np.ndarray | None = None
    visibility: np.ndarray | None = None


@dataclass
class BodyFrame:
    stamp: float
    points: np.ndarray | None  # (33, 3) or None if no body
    visibility: np.ndarray | None
    views: list = field(default_factory=list)


def _open_camera(cfg: Config, index: int, video: str | None):
    if video:
        from .camera import VideoFile
        return VideoFile(video, realtime=True)
    from .camera import Camera
    return Camera(index, cfg.camera_width, cfg.camera_height, cfg.camera_fps, cfg.camera_backend)


def _estimator(cfg: Config):
    from .pose import PoseEstimator
    return PoseEstimator(cfg.model, cfg.min_detection_confidence, cfg.min_tracking_confidence)


class MonoSource:
    """One webcam + MediaPipe. Easiest setup."""

    name = "One camera"

    def __init__(self, cfg: Config, video: str | None = None):
        self.cfg = cfg
        self.cam = _open_camera(cfg, cfg.camera_index, video)
        self.pose = _estimator(cfg)
        self._seq = 0
        self.pose_ms = 0.0

    @property
    def finished(self) -> bool:
        return getattr(self.cam, "finished", False)

    @property
    def camera_fps(self) -> float:
        return self.cam.measured_fps

    def describe(self) -> str:
        return f"Camera {self.cfg.camera_index}: {self.cam.describe()}"

    def read(self, timeout: float = 1.0) -> BodyFrame | None:
        got = self.cam.read(self._seq, timeout)
        if got is None:
            return None
        self._seq, stamp, frame = got
        t0 = time.perf_counter()
        res = self.pose.process(frame, stamp)
        self.pose_ms += 0.1 * ((time.perf_counter() - t0) * 1000 - self.pose_ms)
        if res is None:
            return BodyFrame(stamp, None, None, [View("Camera", frame)])
        return BodyFrame(stamp, res.world, res.visibility,
                         [View("Camera", frame, res.image, res.visibility)])

    def close(self) -> None:
        self.cam.close()
        self.pose.close()


class StereoSource:
    """Two webcams, each with MediaPipe, fused into true 3D. Handles crossed legs."""

    name = "Two cameras"

    def __init__(self, cfg: Config, video: str | None = None, video_b: str | None = None):
        self.cfg = cfg
        self.cam_a = _open_camera(cfg, cfg.camera_index, video)
        self.cam_b = _open_camera(cfg, cfg.camera_b_index, video_b or video)
        self.pose_a = _estimator(cfg)
        self.pose_b = _estimator(cfg)
        self.pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="pose")
        self._seq_a = 0
        self._seq_b = 0
        self._last_b = None
        self.pose_ms = 0.0
        self.calibrator = None  # set while calibrating the camera pair

    @property
    def finished(self) -> bool:
        return getattr(self.cam_a, "finished", False)

    @property
    def camera_fps(self) -> float:
        return min(self.cam_a.measured_fps, self.cam_b.measured_fps)

    def describe(self) -> str:
        return (f"Camera A {self.cfg.camera_index}: {self.cam_a.describe()} | "
                f"Camera B {self.cfg.camera_b_index}: {self.cam_b.describe()}")

    # --- pairing the cameras ---
    def begin_camera_calibration(self) -> None:
        from .stereo import StereoCalibrator
        self.calibrator = StereoCalibrator(self.cfg.camera_hfov_deg, self.cfg.camera_b_hfov_deg)

    def camera_calibration_progress(self) -> float:
        return self.calibrator.progress if self.calibrator else 0.0

    def finish_camera_calibration(self):
        cal, self.calibrator = self.calibrator, None
        result = cal.solve()
        self.cfg.stereo = result
        return result

    def cancel_camera_calibration(self) -> None:
        self.calibrator = None

    def read(self, timeout: float = 1.0) -> BodyFrame | None:
        from .stereo import ViewSample, fuse

        got_a = self.cam_a.read(self._seq_a, timeout)
        if got_a is None:
            return None
        self._seq_a, stamp_a, frame_a = got_a
        # camera B: take its newest frame (don't wait unless we have none yet)
        got_b = self.cam_b.read(self._seq_b, 0.0 if self._last_b is not None else timeout)
        if got_b is not None:
            self._seq_b, stamp_b, frame_b = got_b
            self._last_b = (stamp_b, frame_b)
        if self._last_b is None:
            return None
        stamp_b, frame_b = self._last_b

        t0 = time.perf_counter()
        fa = self.pool.submit(self.pose_a.process, frame_a, stamp_a)
        fb = self.pool.submit(self.pose_b.process, frame_b, stamp_b)
        ra, rb = fa.result(), fb.result()
        self.pose_ms += 0.1 * ((time.perf_counter() - t0) * 1000 - self.pose_ms)
        views = [
            View("Camera A", frame_a, ra.image if ra else None, ra.visibility if ra else None),
            View("Camera B", frame_b, rb.image if rb else None, rb.visibility if rb else None),
        ]
        stamp = 0.5 * (stamp_a + stamp_b)
        if ra is None and rb is None:
            return BodyFrame(stamp, None, None, views)

        def sample(r, frame):
            h, w = frame.shape[:2]
            return ViewSample(image=r.image, world=r.world, visibility=r.visibility, width=w, height=h)

        sa = sample(ra, frame_a) if ra else None
        sb = sample(rb, frame_b) if rb else None
        if self.calibrator is not None and sa and sb:
            self.calibrator.add(sa, sb)

        if self.cfg.stereo.valid and sa and sb:
            pts, vis = fuse(sa, sb, self.cfg.stereo, self.cfg.min_visibility)
            return BodyFrame(stamp, pts, vis, views)
        # cameras not paired yet (or one lost you): use camera A, else camera B rotated into A
        if sa is not None:
            return BodyFrame(stamp, sa.world, sa.visibility, views)
        if self.cfg.stereo.valid:
            return BodyFrame(stamp, sb.world @ self.cfg.stereo.Rm(), sb.visibility, views)
        return BodyFrame(stamp, None, None, views)

    def close(self) -> None:
        self.pool.shutdown(wait=False)
        self.cam_a.close()
        self.cam_b.close()
        self.pose_a.close()
        self.pose_b.close()


class KinectBodySource:
    """Kinect v2 skeleton (depth camera). Real 3D from one device."""

    name = "Kinect v2"

    def __init__(self, cfg: Config):
        from .kinect import KinectSource
        self.cfg = cfg
        self.k = KinectSource(cfg.kinect_swap_sides)
        self._seq = 0
        self.pose_ms = 0.0

    finished = False

    @property
    def camera_fps(self) -> float:
        return self.k.measured_fps

    def describe(self) -> str:
        return self.k.describe()

    def read(self, timeout: float = 1.0) -> BodyFrame | None:
        got = self.k.read(self._seq, timeout)
        if got is None:
            return None
        self._seq, stamp, result = got
        self.k.swap_sides = self.cfg.kinect_swap_sides
        if result is None:
            return BodyFrame(stamp, None, None, [])
        pts, vis = result
        return BodyFrame(stamp, pts, vis, [])

    def close(self) -> None:
        self.k.close()


def make_source(cfg: Config, video: str | None = None, video_b: str | None = None):
    if cfg.mode == "stereo":
        return StereoSource(cfg, video, video_b)
    if cfg.mode == "kinect":
        return KinectBodySource(cfg)
    return MonoSource(cfg, video)
