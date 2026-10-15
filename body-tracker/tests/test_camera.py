"""Camera fallbacks, with a fake OpenCV camera (no webcam needed)."""

import numpy as np
import pytest

cv2 = pytest.importorskip("cv2")

from bodytracker import camera  # noqa: E402

PICTURE = np.random.default_rng(0).integers(0, 255, (480, 640, 3), dtype=np.uint8)
BLACK = np.zeros((480, 640, 3), np.uint8)


def fake_capture(behaviour, opened=None):
    """behaviour(backend, mjpg_requested) -> frame or None."""
    opened = opened if opened is not None else []

    class FakeCap:
        def __init__(self, index, backend):
            self.backend, self.mjpg, self.released = backend, False, False
            opened.append(self)

        def isOpened(self):
            return behaviour(self.backend, None) is not False

        def set(self, prop, value):
            if prop == cv2.CAP_PROP_FOURCC:
                self.mjpg = True
            return True

        def get(self, prop):
            return {cv2.CAP_PROP_FRAME_WIDTH: 640, cv2.CAP_PROP_FRAME_HEIGHT: 480}.get(prop, 30.0)

        def read(self):
            frame = behaviour(self.backend, self.mjpg)
            return (frame is not None), frame

        def release(self):
            self.released = True

    return FakeCap, opened


@pytest.fixture
def windows(monkeypatch):
    monkeypatch.setattr(camera.platform, "system", lambda: "Windows")


def make(monkeypatch, behaviour):
    cls, opened = fake_capture(behaviour)
    monkeypatch.setattr(camera.cv2, "VideoCapture", cls)
    cam = camera.Camera(0, probe_seconds=0.2)
    return cam, opened


def test_normal_webcam_keeps_fast_settings(monkeypatch, windows):
    cam, opened = make(monkeypatch, lambda b, mjpg: PICTURE)
    try:
        assert len(opened) == 1 and opened[0].mjpg and cam.note == ""
        assert cam.read(0) is not None
    finally:
        cam.close()


def test_virtual_camera_that_breaks_on_mjpg_falls_back(monkeypatch, windows):
    # like Iriun: opens fine, but asking for MJPG gives no frames
    cam, opened = make(monkeypatch, lambda b, mjpg: None if mjpg else PICTURE)
    try:
        assert len(opened) == 2 and opened[0].released and not opened[1].mjpg
        assert "own mode" in cam.note and "own mode" in cam.describe()
        assert cam.read(0) is not None
    finally:
        cam.close()


def test_black_on_directshow_tries_media_foundation(monkeypatch, windows):
    cam, opened = make(monkeypatch, lambda b, mjpg: PICTURE if b == cv2.CAP_MSMF else BLACK)
    try:
        assert opened[-1].backend == cv2.CAP_MSMF and cam.cap is opened[-1]
        assert all(c.released for c in opened[:-1])
    finally:
        cam.close()


def test_only_black_still_opens_with_a_hint(monkeypatch, windows):
    cam, opened = make(monkeypatch, lambda b, mjpg: BLACK)
    try:
        assert cam.cap is opened[0] and "black" in cam.note
        assert all(c.released for c in opened[1:])
    finally:
        cam.close()


def test_no_picture_at_all_explains_iriun(monkeypatch, windows):
    with pytest.raises(RuntimeError, match="Iriun"):
        make(monkeypatch, lambda b, mjpg: None)
