"""Opens the app window offscreen and clicks through it (no camera: uses a fake source)."""

import os

import numpy as np
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
QtWidgets = pytest.importorskip("PySide6.QtWidgets")

from bodytracker.config import Config  # noqa: E402
from bodytracker.runtime import Tracker  # noqa: E402
from bodytracker.sources import BodyFrame, View  # noqa: E402

from .synthetic import camera_rotation, standing_room, to_mediapipe  # noqa: E402


class FakeSource:
    name = "fake"
    finished = False
    camera_fps = 60.0
    pose_ms = 10.0

    def __init__(self):
        self.t = 0.0

    def describe(self):
        return "fake"

    def read(self, timeout=1.0):
        import time
        time.sleep(1 / 60)
        self.t += 1 / 60
        pts = to_mediapipe(standing_room(), camera_rotation(10, 0))
        img = np.zeros((48, 64, 3), np.uint8)
        view = View("Camera", img, np.full((33, 2), 0.5), np.ones(33))
        return BodyFrame(time.perf_counter(), pts, np.ones(33), [view])

    def close(self):
        pass


@pytest.fixture
def window(tmp_path, monkeypatch):
    from bodytracker import gui, runtime

    monkeypatch.setattr("bodytracker.sources.make_source", lambda *a, **k: FakeSource())
    cfg = Config(output="none", calibration_delay_s=0.2, use_headset=False)
    tracker = Tracker(cfg, tmp_path / "config.json")
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    win = gui.MainWindow(tracker)
    tracker.start()
    win.show()
    yield app, win, tracker
    win.close()
    del runtime


def _spin(app, seconds):
    import time
    end = time.time() + seconds
    while time.time() < end:
        app.processEvents()
        time.sleep(0.01)


def test_every_page_opens(window):
    app, win, _ = window
    for name in win.NAV:
        win.go(name)
        _spin(app, 0.1)


def test_calibrate_button_calibrates(window):
    app, win, tracker = window
    win.go("Calibrate")
    win._calibrate_clicked()
    _spin(app, 2.5)
    assert tracker.cfg.calibration.valid
    assert "Calibrated" in tracker.status
    assert (tracker.config_path).exists()


def test_switching_mode_rebuilds_guide(window):
    app, win, tracker = window
    win.set_mode("stereo")
    _spin(app, 0.2)
    assert tracker.cfg.mode == "stereo"
    assert "pair" in win.step_marks
    assert win.NAV[win.pages.currentIndex()] == "Setup guide"


def test_presets_and_sliders_change_settings(window):
    _, win, tracker = window
    win._apply_values({"smoothing": 3.0, "prediction_ms": 30.0})
    assert tracker.cfg.smoothing == 3.0
    win.sliders["offset_y_m"].slider.setValue(20)  # 20 * 5 mm
    assert tracker.cfg.offset_y_m == pytest.approx(0.1)
