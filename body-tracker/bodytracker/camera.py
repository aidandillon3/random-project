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


def _attempts(backend: str) -> list[tuple[str, bool]]:
    """(backend, ask for MJPG/size/fps) pairs to try, best first.

    Phone-as-webcam apps (Iriun, DroidCam) and some cheap webcams are virtual or limited
    cameras: asking them for MJPG or a size they don't offer gives black frames or none at
    all. So if the fast settings don't give a picture, try the camera's own default mode,
    then the other Windows camera system.
    """
    windows = platform.system() == "Windows"
    if backend == "auto":
        # DirectShow lets us ask for MJPG at high frame rates on Windows and has
        # noticeably less buffering than Media Foundation on most webcams.
        if windows:
            return [("dshow", True), ("dshow", False), ("msmf", False)]
        backend = "any"
    return [(backend, True), (backend, False)]


def _blank(frame) -> bool:
    """Black or a single colour: what a camera gives when it opened but isn't really streaming."""
    return frame is None or frame.size == 0 or float(frame.std()) < 2.0


def _probe(cap, seconds: float) -> str:
    """'good' once a real picture arrives, 'blank' if only black frames came, else 'none'."""
    got = "none"
    end = time.perf_counter() + seconds
    while time.perf_counter() < end:
        ok, frame = cap.read()
        if ok and frame is not None:
            if not _blank(frame):
                return "good"
            got = "blank"
        else:
            time.sleep(0.02)
    return got


class Camera:
    def __init__(self, index: int = 0, width: int = 640, height: int = 480, fps: int = 60,
                 backend: str = "auto", probe_seconds: float = 2.5):
        self.note = ""
        fallback = None   # a camera that opened but only showed black, used if nothing better works
        self.cap = None
        for i, (name, tuned) in enumerate(_attempts(backend)):
            cap = cv2.VideoCapture(index, BACKENDS.get(name, cv2.CAP_ANY))
            if not cap.isOpened():
                cap.release()
                continue
            if tuned:
                # MJPG first: uncompressed YUY2 often caps at 30fps or less over USB.
                cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*"MJPG"))
                cap.set(cv2.CAP_PROP_FRAME_WIDTH, width)
                cap.set(cv2.CAP_PROP_FRAME_HEIGHT, height)
                cap.set(cv2.CAP_PROP_FPS, fps)
            cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
            result = _probe(cap, probe_seconds)
            if result == "good":
                self.cap = cap
                if i:
                    self.note = f"fell back to the camera's own mode, {name}"
                break
            if result == "blank" and fallback is None:
                fallback = cap
            else:
                cap.release()
        if self.cap is None and fallback is not None:
            self.cap, self.note = fallback, "only black frames so far: is the lens covered or the phone app closed?"
            fallback = None
        if fallback is not None:
            fallback.release()
        if self.cap is None:
            raise RuntimeError(
                f"Could not get a picture from camera {index}. Close other programs using it (Discord, "
                "OBS, browser tabs) or pick another camera. For a phone camera (Iriun, DroidCam), open "
                "the app on both the phone and the PC first.")

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
        return f"{w}x{h} @ {fps:.0f} fps requested" + (f" ({self.note})" if self.note else "")

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
