"""Low-latency camera capture.

Webcams and OpenCV both like to queue frames. A queued frame is an old frame,
and old frames are latency. So a background thread reads as fast as the camera
delivers and keeps only the newest frame; the tracker always works on the
freshest image and never on a backlog.
"""

from __future__ import annotations

import platform
import threading
import time

import cv2

BACKENDS = {
    "dshow": cv2.CAP_DSHOW,
    "msmf": cv2.CAP_MSMF,
    "v4l2": cv2.CAP_V4L2,
    "any": cv2.CAP_ANY,
}


def _open(index: int, backend: str):
    if backend == "auto":
        # DirectShow lets us ask for MJPG at high frame rates on Windows and has
        # noticeably less buffering than Media Foundation on most webcams.
        backend = "dshow" if platform.system() == "Windows" else "any"
    return cv2.VideoCapture(index, BACKENDS.get(backend, cv2.CAP_ANY))


class Camera:
    def __init__(self, index: int = 0, width: int = 640, height: int = 480, fps: int = 60,
                 backend: str = "auto"):
        self.cap = _open(index, backend)
        if not self.cap.isOpened():
            raise RuntimeError(f"Could not open camera {index}. Try another camera_index in config.json.")
        # MJPG first: uncompressed YUY2 often caps at 30fps or less over USB.
        self.cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*"MJPG"))
        self.cap.set(cv2.CAP_PROP_FRAME_WIDTH, width)
        self.cap.set(cv2.CAP_PROP_FRAME_HEIGHT, height)
        self.cap.set(cv2.CAP_PROP_FPS, fps)
        self.cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)

        self._cond = threading.Condition()
        self._frame = None
        self._stamp = 0.0
        self._seq = 0
        self._running = True
        self.measured_fps = 0.0
        self._thread = threading.Thread(target=self._loop, name="camera", daemon=True)
        self._thread.start()

    def describe(self) -> str:
        w = int(self.cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        h = int(self.cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        fps = self.cap.get(cv2.CAP_PROP_FPS)
        return f"{w}x{h} @ {fps:.0f} fps requested"

    def _loop(self) -> None:
        last = time.perf_counter()
        while self._running:
            ok, frame = self.cap.read()
            now = time.perf_counter()
            if not ok:
                time.sleep(0.005)
                continue
            dt = now - last
            last = now
            if dt > 0:
                self.measured_fps += 0.05 * (1.0 / dt - self.measured_fps)
            with self._cond:
                self._frame = frame
                self._stamp = now
                self._seq += 1
                self._cond.notify_all()

    def read(self, after_seq: int, timeout: float = 1.0):
        """Block until a frame newer than `after_seq` exists. Returns (seq, stamp, frame) or None."""
        with self._cond:
            if not self._cond.wait_for(lambda: self._seq > after_seq or not self._running, timeout):
                return None
            if not self._running:
                return None
            return self._seq, self._stamp, self._frame

    def close(self) -> None:
        self._running = False
        with self._cond:
            self._cond.notify_all()
        self._thread.join(timeout=1.0)
        self.cap.release()


class VideoFile:
    """Plays a video file as if it were a camera (for testing without a webcam)."""

    def __init__(self, path: str, realtime: bool = True):
        self.cap = cv2.VideoCapture(path)
        if not self.cap.isOpened():
            raise RuntimeError(f"Could not open video {path}")
        self.fps = self.cap.get(cv2.CAP_PROP_FPS) or 30.0
        self.realtime = realtime
        self._seq = 0
        self._t0 = time.perf_counter()
        self.measured_fps = self.fps
        self.finished = False

    def describe(self) -> str:
        return f"video file @ {self.fps:.0f} fps"

    def read(self, after_seq: int, timeout: float = 1.0):
        ok, frame = self.cap.read()
        if not ok:
            self.finished = True
            return None
        self._seq += 1
        if self.realtime:
            target = self._t0 + self._seq / self.fps
            delay = target - time.perf_counter()
            if delay > 0:
                time.sleep(delay)
            stamp = time.perf_counter()
        else:
            stamp = self._t0 + self._seq / self.fps
        return self._seq, stamp, frame

    def close(self) -> None:
        self.cap.release()
