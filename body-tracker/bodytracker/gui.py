"""The app window (PySide6 / Qt)."""

from __future__ import annotations

import math
import sys

import cv2
import numpy as np
from PySide6.QtCore import QPointF, Qt, QTimer
from PySide6.QtGui import QColor, QFont, QImage, QPainter, QPalette, QPen, QPixmap
from PySide6.QtWidgets import (
    QApplication, QButtonGroup, QCheckBox, QComboBox, QFrame, QGridLayout,
    QGroupBox, QHBoxLayout, QLabel, QListWidget, QListWidgetItem, QMainWindow, QMessageBox,
    QProgressBar, QPushButton, QRadioButton, QScrollArea, QSizePolicy, QSlider, QSpinBox,
    QStackedWidget, QTextBrowser, QVBoxLayout, QWidget,
)

from . import guides
from .config import Config
from .runtime import Tracker

ACCENT = "#4f8cff"
OK = "#2fbf71"
WARN = "#e0a43a"
BAD = "#e5534b"

STYLE = f"""
QWidget {{ font-size: 10pt; }}
QMainWindow, QWidget#page {{ background: #16181d; color: #e6e8ee; }}
QLabel, QCheckBox, QRadioButton {{ color: #e6e8ee; }}
QLabel[muted="true"] {{ color: #9aa1ad; }}
QLabel#h1 {{ font-size: 18pt; font-weight: 600; }}
QLabel#h2 {{ font-size: 12pt; font-weight: 600; }}
QListWidget#nav {{ background: #101216; border: none; color: #c9ced8; font-size: 11pt; padding-top: 8px; }}
QListWidget#nav::item {{ padding: 10px 16px; }}
QListWidget#nav::item:selected {{ background: #232733; color: white; border-left: 3px solid {ACCENT}; }}
QFrame#card {{ background: #1e2128; border: 1px solid #2c303a; border-radius: 10px; }}
QFrame#card[current="true"] {{ border: 2px solid {ACCENT}; }}
QPushButton {{ background: #2a2f3a; color: #e6e8ee; border: 1px solid #3a404d; border-radius: 6px;
               padding: 6px 14px; }}
QPushButton:hover {{ background: #333948; }}
QPushButton#primary {{ background: {ACCENT}; border: none; color: white; font-weight: 600; }}
QPushButton#primary:hover {{ background: #6a9dff; }}
QPushButton#big {{ background: {ACCENT}; border: none; color: white; font-size: 14pt; font-weight: 600;
                   padding: 14px 28px; border-radius: 8px; }}
QGroupBox {{ border: 1px solid #2c303a; border-radius: 8px; margin-top: 14px; padding: 10px; color: #c9ced8; }}
QGroupBox::title {{ subcontrol-origin: margin; left: 10px; padding: 0 4px; }}
QTextBrowser {{ background: #1e2128; border: 1px solid #2c303a; border-radius: 8px; color: #e6e8ee; }}
QComboBox, QSpinBox, QDoubleSpinBox {{ background: #23262e; color: #e6e8ee; border: 1px solid #3a404d;
                                       border-radius: 4px; padding: 3px 6px; }}
QProgressBar {{ background: #23262e; border: 1px solid #3a404d; border-radius: 4px; text-align: center; color: white; }}
QProgressBar::chunk {{ background: {ACCENT}; border-radius: 4px; }}
QStatusBar {{ background: #101216; color: #c9ced8; }}
QScrollArea {{ border: none; background: transparent; }}
"""

# MediaPipe connections drawn on previews
DRAW_LINES = [(11, 12), (11, 23), (12, 24), (23, 24), (23, 25), (25, 27), (27, 29), (29, 31),
              (27, 31), (24, 26), (26, 28), (28, 30), (30, 32), (28, 32), (0, 11), (0, 12)]

PRESETS = {
    "Smooth": dict(smoothing=0.8, responsiveness=0.5, prediction_ms=15.0),
    "Balanced": dict(smoothing=1.5, responsiveness=0.8, prediction_ms=20.0),
    "Snappy": dict(smoothing=3.0, responsiveness=2.0, prediction_ms=30.0),
}


def list_cameras() -> list[tuple[int, str]]:
    """Camera indexes with names where Windows gives them to us."""
    try:
        from pygrabber.dshow_graph import FilterGraph  # Windows only

        names = FilterGraph().get_input_devices()
        if names:
            return list(enumerate(names))
    except Exception:
        pass
    return [(i, f"Camera {i}") for i in range(6)]


def to_pixmap(frame: np.ndarray, max_w: int, max_h: int) -> QPixmap:
    h, w = frame.shape[:2]
    s = min(max_w / w, max_h / h, 1.5)
    if s <= 0:
        return QPixmap()
    img = cv2.resize(frame, (max(1, int(w * s)), max(1, int(h * s))), interpolation=cv2.INTER_AREA)
    rgb = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
    qimg = QImage(rgb.data, rgb.shape[1], rgb.shape[0], rgb.strides[0], QImage.Format.Format_RGB888)
    return QPixmap.fromImage(qimg.copy())


def draw_skeleton(frame: np.ndarray, image_xy, visibility, min_vis: float) -> np.ndarray:
    img = frame.copy()
    if image_xy is None:
        return img
    h, w = img.shape[:2]
    pts = (np.asarray(image_xy) * [w, h]).astype(int)
    thick = max(2, w // 240)
    for a, b in DRAW_LINES:
        ok = visibility is None or (visibility[a] > min_vis and visibility[b] > min_vis)
        cv2.line(img, tuple(pts[a]), tuple(pts[b]), (113, 191, 47) if ok else (75, 83, 229), thick, cv2.LINE_AA)
    for i in (23, 24, 25, 26, 27, 28):
        cv2.circle(img, tuple(pts[i]), thick * 3, (255, 140, 79), -1, cv2.LINE_AA)
    return img


def muted(text: str) -> QLabel:
    lab = QLabel(text)
    lab.setProperty("muted", True)
    lab.setWordWrap(True)
    return lab


def heading(text: str, level: int = 1) -> QLabel:
    lab = QLabel(text)
    lab.setObjectName("h1" if level == 1 else "h2")
    lab.setWordWrap(True)
    return lab


def page(*widgets, stretch=True) -> QWidget:
    w = QWidget()
    w.setObjectName("page")
    lay = QVBoxLayout(w)
    lay.setContentsMargins(28, 22, 28, 22)
    lay.setSpacing(12)
    for x in widgets:
        if isinstance(x, QWidget):
            lay.addWidget(x)
        else:
            lay.addLayout(x)
    if stretch:
        lay.addStretch(1)
    return w


def scroll(widget: QWidget) -> QScrollArea:
    sa = QScrollArea()
    sa.setWidgetResizable(True)
    sa.setWidget(widget)
    return sa


class Preview(QLabel):
    """Shows a camera image scaled to fit."""

    def __init__(self, text="No image yet"):
        super().__init__(text)
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setMinimumSize(320, 200)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.setStyleSheet("background:#0d0f13; border-radius:8px; color:#6b7280;")

    def show_frame(self, frame) -> None:
        if frame is None:
            return
        self.setPixmap(to_pixmap(frame, self.width() - 4, self.height() - 4))


class BodyView(QWidget):
    """Front and side view of the trackers being sent, so you can see what the game gets."""

    def __init__(self):
        super().__init__()
        self.setMinimumSize(320, 240)
        self.output = None

    def set_output(self, out) -> None:
        self.output = out
        self.update()

    def paintEvent(self, _ev) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.fillRect(self.rect(), QColor("#0d0f13"))
        w, h = self.width(), self.height()
        p.setPen(QColor("#6b7280"))
        p.drawText(10, 18, "From behind")
        p.drawText(w // 2 + 10, 18, "From the side")
        out = self.output
        if out is None:
            p.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, "Waiting for a body...")
            return
        pts = {t.name: t.position for t in out.trackers}
        pts["head"] = out.head_pos
        hip = pts.get("hip", out.head_pos - np.array([0, 0.75, 0]))
        floor_y = min(v[1] for v in pts.values()) - 0.05
        scale = (h - 40) / max(out.head_pos[1] - floor_y + 0.2, 1.0)

        # face-on axes: use the hip yaw so "front" really is the front of you
        from .mathutil import matrix_from_quat
        hip_t = next((t for t in out.trackers if t.name == "hip"), None)
        if hip_t is not None:
            m = matrix_from_quat(hip_t.rotation)
            right = m[:, 0] * np.array([1, 0, 1])
            fwd = -m[:, 2] * np.array([1, 0, 1])
            right = right / (np.linalg.norm(right) or 1)
            fwd = fwd / (np.linalg.norm(fwd) or 1)
        else:
            right, fwd = np.array([1.0, 0, 0]), np.array([0, 0, -1.0])

        def proj(v, cx, axis):
            d = v - hip
            return QPointF(cx + float(np.dot(d, axis)) * scale, h - 20 - (v[1] - floor_y) * scale)

        bones = [("head", "hip"), ("hip", "left_knee"), ("left_knee", "left_foot"),
                 ("hip", "right_knee"), ("right_knee", "right_foot")]
        for cx, axis in ((w * 0.25, right), (w * 0.75, fwd)):
            p.setPen(QPen(QColor("#2c303a"), 1))
            p.drawLine(int(cx - w * 0.2), h - 20, int(cx + w * 0.2), h - 20)
            for a, b in bones:
                if a in pts and b in pts:
                    color = QColor("#e6e8ee")
                    if "left" in b:
                        color = QColor(ACCENT)
                    elif "right" in b:
                        color = QColor("#ff8c4f")
                    p.setPen(QPen(color, 3))
                    p.drawLine(proj(pts[a], cx, axis), proj(pts[b], cx, axis))
            p.setPen(Qt.PenStyle.NoPen)
            for name, v in pts.items():
                p.setBrush(QColor("#ffffff") if name == "head" else QColor(OK))
                p.drawEllipse(proj(v, cx, axis), 5, 5)
        p.setPen(QColor("#6b7280"))
        p.drawText(10, h - 4, "blue = left, orange = right")


class LabeledSlider(QWidget):
    def __init__(self, label, help_text, lo, hi, step, value, fmt, on_change):
        super().__init__()
        self.step = step
        self.fmt = fmt
        self.on_change = on_change
        g = QGridLayout(self)
        g.setContentsMargins(0, 2, 0, 2)
        self.name = QLabel(label)
        self.value_label = QLabel()
        self.value_label.setMinimumWidth(70)
        self.value_label.setAlignment(Qt.AlignmentFlag.AlignRight)
        self.slider = QSlider(Qt.Orientation.Horizontal)
        self.slider.setRange(int(round(lo / step)), int(round(hi / step)))
        self.slider.valueChanged.connect(self._changed)
        g.addWidget(self.name, 0, 0)
        g.addWidget(self.value_label, 0, 1)
        g.addWidget(self.slider, 1, 0, 1, 2)
        g.addWidget(muted(help_text), 2, 0, 1, 2)
        self.set_value(value)

    def set_value(self, v) -> None:
        self.slider.blockSignals(True)
        self.slider.setValue(int(round(v / self.step)))
        self.slider.blockSignals(False)
        self.value_label.setText(self.fmt(v))

    def _changed(self, raw) -> None:
        v = raw * self.step
        self.value_label.setText(self.fmt(v))
        self.on_change(v)


class StatusDot(QLabel):
    def set_state(self, state: str, text: str) -> None:
        color = {"ok": OK, "warn": WARN, "bad": BAD}.get(state, "#6b7280")
        self.setText(f"<span style='color:{color}'>●</span> {text}")


class MainWindow(QMainWindow):
    NAV = ["Start", "Setup guide", "Cameras", "Calibrate", "Adjust", "Output", "Help"]

    def __init__(self, tracker: Tracker):
        super().__init__()
        self.t = tracker
        self.cfg: Config = tracker.cfg
        self.setWindowTitle("Body Tracker")
        self.resize(1180, 760)

        self._save_timer = QTimer(self)
        self._save_timer.setSingleShot(True)
        self._save_timer.setInterval(800)
        self._save_timer.timeout.connect(self.t.save)

        root = QWidget()
        lay = QHBoxLayout(root)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        self.nav = QListWidget()
        self.nav.setObjectName("nav")
        self.nav.setFixedWidth(190)
        for name in self.NAV:
            QListWidgetItem(name, self.nav)
        self.pages = QStackedWidget()
        lay.addWidget(self.nav)
        lay.addWidget(self.pages, 1)
        self.setCentralWidget(root)

        self.pages.addWidget(self._start_page())
        self.pages.addWidget(self._guide_page())
        self.pages.addWidget(self._cameras_page())
        self.pages.addWidget(self._calibrate_page())
        self.pages.addWidget(self._adjust_page())
        self.pages.addWidget(self._output_page())
        self.pages.addWidget(self._help_page())
        self.nav.currentRowChanged.connect(self.pages.setCurrentIndex)
        self.nav.setCurrentRow(0 if not self.cfg.setup_done else 3)

        sb = self.statusBar()
        self.sb_mode = QLabel()
        self.sb_cam = StatusDot()
        self.sb_head = StatusDot()
        self.sb_send = StatusDot()
        self.sb_msg = QLabel()
        for wdg in (self.sb_mode, self.sb_cam, self.sb_head, self.sb_send):
            sb.addPermanentWidget(wdg)
        sb.addWidget(self.sb_msg, 1)

        self.timer = QTimer(self)
        self.timer.timeout.connect(self._tick)
        self.timer.start(33)
        self._refresh_mode_widgets()

    # ------------------------------------------------------------------ helpers
    def changed(self) -> None:
        """Something in the settings changed: save shortly."""
        self._save_timer.start()

    def go(self, page_name: str) -> None:
        self.nav.setCurrentRow(self.NAV.index(page_name))

    # -------------------------------------------------------------------- Start
    def _mode_card(self, key: str) -> QFrame:
        info = guides.MODES[key]
        card = QFrame()
        card.setObjectName("card")
        v = QVBoxLayout(card)
        v.setContentsMargins(16, 14, 16, 14)
        top = QHBoxLayout()
        top.addWidget(heading(info["title"], 2))
        tag = QLabel(info["tag"])
        tag.setStyleSheet(f"background:#263252;color:{ACCENT};border-radius:9px;padding:2px 8px;font-size:9pt;")
        top.addWidget(tag)
        top.addStretch(1)
        v.addLayout(top)
        v.addWidget(muted("Best for: " + info["best_for"]))
        for items, sign, color in ((info["pros"], "+", OK), (info["cons"], "−", WARN)):
            for x in items:
                lab = QLabel(f"<span style='color:{color}'>{sign}</span> {x}")
                lab.setWordWrap(True)
                v.addWidget(lab)
        v.addWidget(muted("Needs: " + info["needs"]))
        v.addStretch(1)
        btn = QPushButton("Use this")
        btn.setObjectName("primary")
        btn.clicked.connect(lambda: self.set_mode(key))
        v.addWidget(btn)
        card.setMinimumWidth(250)
        return card

    def _start_page(self) -> QWidget:
        self.cards = {k: self._mode_card(k) for k in ("mono", "stereo", "kinect")}
        row = QHBoxLayout()
        row.setSpacing(14)
        for c in self.cards.values():
            row.addWidget(c, 1)

        # help me choose
        box = QGroupBox("Not sure? Help me choose")
        g = QGridLayout(box)
        self.q_cams = QComboBox()
        self.q_cams.addItems(["1 webcam", "2 or more webcams"])
        self.q_kinect = QComboBox()
        self.q_kinect.addItems(["No", "Yes, Kinect v2 with PC adapter"])
        self.q_act = QComboBox()
        self.q_act.addItems(["Standing, walking, hanging out", "Sitting or lying down",
                             "Dancing, crossing legs, fast footwork"])
        g.addWidget(QLabel("How many webcams can you use?"), 0, 0)
        g.addWidget(self.q_cams, 0, 1)
        g.addWidget(QLabel("Do you have a Kinect?"), 1, 0)
        g.addWidget(self.q_kinect, 1, 1)
        g.addWidget(QLabel("What do you do most in VR?"), 2, 0)
        g.addWidget(self.q_act, 2, 1)
        self.rec_label = QLabel()
        self.rec_label.setWordWrap(True)
        self.rec_btn = QPushButton()
        self.rec_btn.setObjectName("primary")
        g.addWidget(self.rec_label, 3, 0, 1, 2)
        g.addWidget(self.rec_btn, 4, 0, 1, 1)
        for c in (self.q_cams, self.q_kinect, self.q_act):
            c.currentIndexChanged.connect(self._update_recommendation)
        self.rec_btn.clicked.connect(lambda: self.set_mode(self._rec_mode))
        self._update_recommendation()

        intro = muted("Your Quest handles head, arms and chest. This adds hip, knee and foot trackers "
                      "from a camera. Pick how you want to track; you can change it any time.")
        return scroll(page(heading("Choose how to track your legs"), intro, row, box))

    def _update_recommendation(self) -> None:
        cams = "2" if self.q_cams.currentIndex() == 1 else "1"
        act = ["casual", "sitting", "active"][self.q_act.currentIndex()]
        mode, why = guides.recommend(cams, self.q_kinect.currentIndex() == 1, act)
        self._rec_mode = mode
        self.rec_label.setText(f"<b>Recommended: {guides.MODES[mode]['title']}.</b> {why}")
        self.rec_btn.setText(f"Use {guides.MODES[mode]['title']}")

    def set_mode(self, mode: str) -> None:
        if mode != self.cfg.mode:
            self.cfg.mode = mode
            self.cfg.calibration.valid = False  # different source, different space
            self.t.restart_source()
            self.changed()
        self._refresh_mode_widgets()
        self.go("Setup guide")

    def _refresh_mode_widgets(self) -> None:
        for k, c in self.cards.items():
            c.setProperty("current", k == self.cfg.mode)
            c.style().unpolish(c)
            c.style().polish(c)
        self._build_guide_steps()
        self._refresh_camera_widgets()
        self.sb_mode.setText(f"Mode: {guides.MODES[self.cfg.mode]['title']}  ")

    # -------------------------------------------------------------- Setup guide
    def _guide_page(self) -> QWidget:
        self.guide_box = QVBoxLayout()
        self.guide_title = heading("")
        done = QPushButton("I'm all set, take me to Calibrate")
        done.setObjectName("primary")
        done.clicked.connect(self._guide_done)
        w = page(self.guide_title, muted("Work through these in order. Steps with a live check tick themselves."),
                 self.guide_box, done)
        return scroll(w)

    def _guide_done(self) -> None:
        self.cfg.setup_done = True
        self.changed()
        self.go("Calibrate")

    def _build_guide_steps(self) -> None:
        while self.guide_box.count():
            item = self.guide_box.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        self.guide_title.setText(f"Setup guide: {guides.MODES[self.cfg.mode]['title']}")
        self.step_marks = {}
        for n, (key, title, body) in enumerate(guides.steps_for(self.cfg.mode), 1):
            card = QFrame()
            card.setObjectName("card")
            h = QHBoxLayout(card)
            mark = QLabel("○")
            mark.setFixedWidth(28)
            mark.setAlignment(Qt.AlignmentFlag.AlignTop)
            mark.setStyleSheet("font-size:16pt;color:#6b7280;")
            col = QVBoxLayout()
            col.addWidget(heading(f"{n}. {title}", 2))
            text = QLabel(body)
            text.setWordWrap(True)
            text.setOpenExternalLinks(True)
            text.setTextFormat(Qt.TextFormat.RichText)
            col.addWidget(text)
            buttons = QHBoxLayout()
            for label, target in (("Open Cameras", "Cameras"),) if key in ("camera", "body", "pair") else \
                    (("Open Calibrate", "Calibrate"),) if key == "calibrated" else ():
                b = QPushButton(label)
                b.clicked.connect(lambda _=False, tg=target: self.go(tg))
                buttons.addWidget(b)
            buttons.addStretch(1)
            col.addLayout(buttons)
            h.addWidget(mark)
            h.addLayout(col, 1)
            self.guide_box.addWidget(card)
            self.step_marks[key] = mark

    def _step_checks(self) -> dict:
        t = self.t
        return {
            "camera": t.source is not None and t.camera_fps > 1,
            "body": t.body_seen,
            "pair": self.cfg.stereo.valid,
            "sdk": t.source is not None and self.cfg.mode == "kinect",
            "steamvr": t.headset_connected,
            "calibrated": self.cfg.calibration.valid,
            "sending": t.sending,
        }

    # ----------------------------------------------------------------- Cameras
    def _cameras_page(self) -> QWidget:
        self.cam_list = list_cameras()
        self.cam_a = QComboBox()
        self.cam_b = QComboBox()
        for i, name in self.cam_list:
            self.cam_a.addItem(f"{i}: {name}", i)
            self.cam_b.addItem(f"{i}: {name}", i)
        self.fov_a = QSpinBox()
        self.fov_b = QSpinBox()
        for s in (self.fov_a, self.fov_b):
            s.setRange(30, 150)
            s.setSuffix("°")
        self.fov_a.setValue(int(self.cfg.camera_hfov_deg))
        self.fov_b.setValue(int(self.cfg.camera_b_hfov_deg))
        self.model_box = QComboBox()
        self.model_box.addItem("Fast (lite)", "lite")
        self.model_box.addItem("Balanced (full) - recommended", "full")
        self.model_box.addItem("Accurate (heavy) - needs a fast CPU", "heavy")
        self.model_box.setCurrentIndex(max(0, self.model_box.findData(self.cfg.model)))
        apply_btn = QPushButton("Apply camera changes")
        apply_btn.setObjectName("primary")
        apply_btn.clicked.connect(self._apply_cameras)

        form = QGridLayout()
        self.lbl_cam_a = QLabel("Camera")
        form.addWidget(self.lbl_cam_a, 0, 0)
        form.addWidget(self.cam_a, 0, 1)
        self.lbl_fov_a = QLabel("Field of view")
        form.addWidget(self.lbl_fov_a, 0, 2)
        form.addWidget(self.fov_a, 0, 3)
        self.lbl_cam_b = QLabel("Camera B")
        form.addWidget(self.lbl_cam_b, 1, 0)
        form.addWidget(self.cam_b, 1, 1)
        self.lbl_fov_b = QLabel("Field of view")
        form.addWidget(self.lbl_fov_b, 1, 2)
        form.addWidget(self.fov_b, 1, 3)
        self.lbl_model = QLabel("Pose model")
        form.addWidget(self.lbl_model, 2, 0)
        form.addWidget(self.model_box, 2, 1)
        form.addWidget(apply_btn, 2, 3)
        form.setColumnStretch(1, 1)

        self.kinect_swap = QCheckBox("Swap left and right (if the Kinect mixes them up)")
        self.kinect_swap.setChecked(self.cfg.kinect_swap_sides)
        self.kinect_swap.toggled.connect(lambda v: (setattr(self.cfg, "kinect_swap_sides", v), self.changed()))

        self.prev_a = Preview()
        self.prev_b = Preview()
        self.prev_body = BodyView()
        prev_row = QHBoxLayout()
        prev_row.addWidget(self.prev_a, 1)
        prev_row.addWidget(self.prev_b, 1)
        prev_row.addWidget(self.prev_body, 1)

        # pairing (two cameras)
        self.pair_box = QGroupBox("Pair the cameras")
        pv = QVBoxLayout(self.pair_box)
        pv.addWidget(muted("Click Pair, then walk slowly around your play space for 10-20 seconds. "
                           "Turn around and lift your knees. Both cameras must see all of you."))
        ph = QHBoxLayout()
        self.pair_btn = QPushButton("Pair cameras")
        self.pair_btn.setObjectName("primary")
        self.pair_btn.clicked.connect(self._toggle_pairing)
        self.pair_progress = QProgressBar()
        self.pair_progress.setRange(0, 100)
        self.pair_state = QLabel()
        ph.addWidget(self.pair_btn)
        ph.addWidget(self.pair_progress, 1)
        pv.addLayout(ph)
        pv.addWidget(self.pair_state)

        self.cam_error = QLabel()
        self.cam_error.setWordWrap(True)
        self.cam_error.setStyleSheet(f"color:{BAD};")
        w = page(heading("Cameras"), form, self.kinect_swap, self.cam_error, prev_row, self.pair_box,
                 muted("Green lines: joints seen clearly. Red: hidden or guessed. "
                       "The right-hand view shows the trackers the game receives."), stretch=False)
        return w

    def _refresh_camera_widgets(self) -> None:
        mode = self.cfg.mode
        stereo, kinect = mode == "stereo", mode == "kinect"
        for wdg in (self.cam_a, self.lbl_cam_a, self.model_box, self.lbl_model):
            wdg.setVisible(not kinect)
        for wdg in (self.cam_b, self.lbl_cam_b, self.fov_a, self.fov_b, self.lbl_fov_a, self.lbl_fov_b,
                    self.prev_b, self.pair_box):
            wdg.setVisible(stereo)
        self.prev_a.setVisible(not kinect)
        self.kinect_swap.setVisible(kinect)
        self.lbl_cam_a.setText("Camera A" if stereo else "Camera")
        self.cam_a.setCurrentIndex(max(0, self.cam_a.findData(self.cfg.camera_index)))
        self.cam_b.setCurrentIndex(max(0, self.cam_b.findData(self.cfg.camera_b_index)))

    def _apply_cameras(self) -> None:
        self.cfg.camera_index = int(self.cam_a.currentData())
        self.cfg.camera_b_index = int(self.cam_b.currentData())
        if self.cfg.mode == "stereo" and self.cfg.camera_index == self.cfg.camera_b_index:
            QMessageBox.warning(self, "Body Tracker", "Camera A and camera B must be different cameras.")
            return
        fov_changed = (self.fov_a.value() != self.cfg.camera_hfov_deg or
                       self.fov_b.value() != self.cfg.camera_b_hfov_deg)
        self.cfg.camera_hfov_deg = float(self.fov_a.value())
        self.cfg.camera_b_hfov_deg = float(self.fov_b.value())
        if fov_changed:
            self.cfg.stereo.valid = False
        self.cfg.model = self.model_box.currentData()
        self.t.restart_source()
        self.changed()

    def _toggle_pairing(self) -> None:
        if self.t.pairing_started is None:
            if not self.t.can_pair_cameras:
                QMessageBox.information(self, "Body Tracker", "Both cameras need to be running first.")
                return
            self.t.start_camera_pairing()
            self.pair_btn.setText("Cancel")
        else:
            self.t.cancel_camera_pairing()
            self.pair_btn.setText("Pair cameras")

    # --------------------------------------------------------------- Calibrate
    def _calibrate_page(self) -> QWidget:
        self.height_cm = QSpinBox()
        self.height_cm.setRange(100, 230)
        self.height_cm.setSuffix(" cm")
        self.height_cm.setValue(int(round(self.cfg.user_height_m * 100)))
        self.height_cm.valueChanged.connect(self._height_changed)
        self.height_ft = QLabel()
        self.delay = QSpinBox()
        self.delay.setRange(2, 30)
        self.delay.setSuffix(" s")
        self.delay.setValue(int(self.cfg.calibration_delay_s))
        self.delay.valueChanged.connect(lambda v: (setattr(self.cfg, "calibration_delay_s", float(v)), self.changed()))
        form = QGridLayout()
        form.addWidget(QLabel("Your height"), 0, 0)
        form.addWidget(self.height_cm, 0, 1)
        form.addWidget(self.height_ft, 0, 2)
        form.addWidget(QLabel("Time to get ready"), 1, 0)
        form.addWidget(self.delay, 1, 1)
        form.setColumnStretch(3, 1)
        self._height_changed(self.height_cm.value())

        steps = QLabel(
            "<ol><li>Put your headset on and stand where you'll play.</li>"
            "<li>Face the camera (camera A with two cameras). Feet a little apart.</li>"
            "<li>Stand up straight and <b>look straight ahead</b>, not at your feet.</li>"
            "<li>Hold still until it says <b>Calibrated</b>.</li></ol>")
        steps.setWordWrap(True)
        self.cal_btn = QPushButton("Calibrate")
        self.cal_btn.setObjectName("big")
        self.cal_btn.clicked.connect(self._calibrate_clicked)
        self.cal_big = QLabel("")
        self.cal_big.setAlignment(Qt.AlignmentFlag.AlignCenter)
        f = QFont()
        f.setPointSize(28)
        f.setBold(True)
        self.cal_big.setFont(f)
        self.cal_state = QLabel()
        self.cal_state.setWordWrap(True)
        self.cal_body = BodyView()
        self.cal_body.setMinimumHeight(300)
        left = QVBoxLayout()
        left.addLayout(form)
        left.addWidget(steps)
        left.addWidget(self.cal_btn, 0, Qt.AlignmentFlag.AlignLeft)
        left.addWidget(self.cal_big)
        left.addWidget(self.cal_state)
        left.addStretch(1)
        row = QHBoxLayout()
        row.addLayout(left, 1)
        row.addWidget(self.cal_body, 1)
        return page(heading("Calibrate"), muted("Do this the first time, and again whenever a camera moves."),
                    row)

    def _height_changed(self, cm: int) -> None:
        self.cfg.user_height_m = cm / 100.0
        inches = cm / 2.54
        self.height_ft.setText(f"= {int(inches // 12)} ft {int(round(inches % 12))} in")
        self.changed()

    def _calibrate_clicked(self) -> None:
        if self.t.calibration_phase() is not None:
            self.t.cancel_body_calibration()
        else:
            self.t.start_body_calibration()

    # ------------------------------------------------------------------ Adjust
    def _adjust_page(self) -> QWidget:
        cfg = self.cfg

        def setter(attr):
            def f(v):
                setattr(cfg, attr, v)
                self.changed()
            return f

        cm = lambda v: f"{v * 100:+.0f} cm"  # noqa: E731
        self.sliders = {
            "smoothing": LabeledSlider("Smoothing (when still)",
                                       "Lower = steadier when standing still. Higher = reacts to small moves.",
                                       0.2, 8.0, 0.1, cfg.smoothing, lambda v: f"{v:.1f} Hz", setter("smoothing")),
            "responsiveness": LabeledSlider("Responsiveness (when moving)",
                                            "Higher = less lag on kicks and steps, a little more shake.",
                                            0.0, 6.0, 0.1, cfg.responsiveness, lambda v: f"{v:.1f}",
                                            setter("responsiveness")),
            "prediction_ms": LabeledSlider("Prediction",
                                           "How far ahead to guess. Raise if feet trail behind; "
                                           "lower if they overshoot.",
                                           0, 80, 1, cfg.prediction_ms, lambda v: f"{v:.0f} ms",
                                           setter("prediction_ms")),
            "scale_adjust": LabeledSlider("Size", "Scales your legs around your head. Use if feet float or sink.",
                                          0.8, 1.2, 0.005, cfg.scale_adjust, lambda v: f"{v * 100:.1f} %",
                                          setter("scale_adjust")),
            "yaw_offset_deg": LabeledSlider("Turn", "Spins your legs around you if they point a bit off.",
                                            -45, 45, 0.5, cfg.yaw_offset_deg, lambda v: f"{v:+.1f}°",
                                            setter("yaw_offset_deg")),
            "offset_x_m": LabeledSlider("Left / right", "Shifts all trackers sideways.", -0.3, 0.3, 0.005,
                                        cfg.offset_x_m, cm, setter("offset_x_m")),
            "offset_y_m": LabeledSlider("Up / down", "Shifts all trackers up or down.", -0.3, 0.3, 0.005,
                                        cfg.offset_y_m, cm, setter("offset_y_m")),
            "offset_z_m": LabeledSlider("Forward / back", "Shifts all trackers forward or back.", -0.3, 0.3,
                                        0.005, cfg.offset_z_m, cm, setter("offset_z_m")),
            "hip_offset_y_m": LabeledSlider("Hip height", "Moves only the hip tracker (belt height).", -0.3, 0.3,
                                            0.005, cfg.hip_offset_y_m, cm, setter("hip_offset_y_m")),
        }
        presets = QHBoxLayout()
        presets.addWidget(QLabel("Presets:"))
        for name, values in PRESETS.items():
            b = QPushButton(name)
            b.clicked.connect(lambda _=False, v=values: self._apply_values(v))
            presets.addWidget(b)
        presets.addStretch(1)
        feel = QGroupBox("Feel")
        fv = QVBoxLayout(feel)
        fv.addLayout(presets)
        for k in ("smoothing", "responsiveness", "prediction_ms"):
            fv.addWidget(self.sliders[k])
        place = QGroupBox("Placement")
        pv = QVBoxLayout(place)
        for k in ("scale_adjust", "yaw_offset_deg", "offset_x_m", "offset_y_m", "offset_z_m", "hip_offset_y_m"):
            pv.addWidget(self.sliders[k])
        reset = QPushButton("Reset placement")
        reset.clicked.connect(lambda: self._apply_values(dict(scale_adjust=1.0, yaw_offset_deg=0.0, offset_x_m=0.0,
                                                              offset_y_m=0.0, offset_z_m=0.0, hip_offset_y_m=0.0)))
        pv.addWidget(reset, 0, Qt.AlignmentFlag.AlignLeft)

        opts = QGroupBox("Trackers and fixes")
        ov = QVBoxLayout(opts)
        self.tracker_checks = {}
        row = QHBoxLayout()
        for name, label in (("hip", "Hip"), ("feet", "Feet"), ("knees", "Knees")):
            cb = QCheckBox(label)
            names = {"hip": ["hip"], "feet": ["left_foot", "right_foot"], "knees": ["left_knee", "right_knee"]}[name]
            cb.setChecked(all(n in cfg.enabled_trackers for n in names))
            cb.toggled.connect(lambda v, ns=names: self._toggle_trackers(ns, v))
            row.addWidget(cb)
        row.addStretch(1)
        ov.addLayout(row)
        swap = QCheckBox("Fix left/right leg mix-ups (recommended)")
        swap.setChecked(cfg.leg_swap_guard)
        swap.toggled.connect(lambda v: (setattr(cfg, "leg_swap_guard", v), self.changed()))
        bones = QCheckBox("Keep legs their real length (recommended)")
        bones.setChecked(cfg.lock_bone_lengths)
        bones.toggled.connect(lambda v: (setattr(cfg, "lock_bone_lengths", v), self.changed()))
        ov.addWidget(swap)
        ov.addWidget(bones)

        grid = QGridLayout()
        grid.addWidget(feel, 0, 0)
        grid.addWidget(opts, 1, 0)
        grid.addWidget(place, 0, 1, 2, 1)
        grid.setColumnStretch(0, 1)
        grid.setColumnStretch(1, 1)
        return scroll(page(heading("Adjust"), muted("Changes apply instantly and are saved automatically."), grid))

    def _apply_values(self, values: dict) -> None:
        for k, v in values.items():
            setattr(self.cfg, k, v)
            if k in self.sliders:
                self.sliders[k].set_value(v)
        self.changed()

    def _toggle_trackers(self, names, on) -> None:
        current = [n for n in self.cfg.enabled_trackers if n not in names]
        if on:
            current += names
        order = ["hip", "left_foot", "right_foot", "left_knee", "right_knee"]
        self.cfg.enabled_trackers = [n for n in order if n in current]
        self.changed()

    # ------------------------------------------------------------------ Output
    def _output_page(self) -> QWidget:
        cfg = self.cfg
        box = QGroupBox("Send trackers to")
        v = QVBoxLayout(box)
        self.out_group = QButtonGroup(self)
        options = [
            ("steamvr", "SteamVR (Virtual Motion Tracker) - recommended",
             "Real SteamVR trackers. Works in every SteamVR game, VRChat included. Needs VMT installed."),
            ("vrchat", "VRChat OSC",
             "VRChat's built-in OSC trackers. Use this if VRChat runs without SteamVR (Virtual Desktop VDXR). "
             "Turn on OSC in VRChat's Action Menu."),
            ("both", "Both", "Only if you know you need it; VRChat would get two sets of trackers."),
        ]
        for key, label, desc in options:
            rb = QRadioButton(label)
            rb.setChecked(cfg.output == key)
            rb.toggled.connect(lambda on, k=key: on and self._set_output(k))
            self.out_group.addButton(rb)
            v.addWidget(rb)
            d = muted(desc)
            d.setContentsMargins(24, 0, 0, 6)
            v.addWidget(d)

        self.pause_btn = QPushButton("Pause trackers")
        self.pause_btn.setCheckable(True)
        self.pause_btn.toggled.connect(self._pause)
        self.out_status = QLabel()
        self.out_status.setWordWrap(True)
        head = QCheckBox("Use my headset position from SteamVR (much lower latency when walking)")
        head.setChecked(cfg.use_headset)
        head.toggled.connect(lambda val: (setattr(cfg, "use_headset", val), self.changed(),
                                          self.sb_msg.setText("Restart the app to apply the headset setting.")))

        adv = QGroupBox("Advanced")
        g = QGridLayout(adv)
        self.vmt_port = QSpinBox()
        self.vmt_port.setRange(1, 65535)
        self.vmt_port.setValue(cfg.vmt_port)
        self.vrc_port = QSpinBox()
        self.vrc_port.setRange(1, 65535)
        self.vrc_port.setValue(cfg.vrchat_port)
        self.rate = QSpinBox()
        self.rate.setRange(30, 250)
        self.rate.setSuffix(" Hz")
        self.rate.setValue(int(cfg.output_rate_hz))
        g.addWidget(QLabel("VMT port"), 0, 0)
        g.addWidget(self.vmt_port, 0, 1)
        g.addWidget(QLabel("VRChat OSC port"), 1, 0)
        g.addWidget(self.vrc_port, 1, 1)
        g.addWidget(QLabel("Send rate"), 2, 0)
        g.addWidget(self.rate, 2, 1)
        apply_adv = QPushButton("Apply")
        apply_adv.clicked.connect(self._apply_advanced)
        g.addWidget(apply_adv, 3, 1)
        g.setColumnStretch(2, 1)
        self.pause_btn.setMaximumWidth(220)
        return page(heading("Output"), box, head, self.pause_btn, self.out_status, adv)

    def _set_output(self, key: str) -> None:
        self.cfg.output = key
        self.t.rebuild_outputs()
        self.changed()

    def _apply_advanced(self) -> None:
        self.cfg.vmt_port = self.vmt_port.value()
        self.cfg.vrchat_port = self.vrc_port.value()
        self.cfg.output_rate_hz = float(self.rate.value())
        self.t.rebuild_outputs()
        self.changed()
        self.sb_msg.setText("Saved. The send rate applies after a restart.")

    def _pause(self, on: bool) -> None:
        self.t.set_paused(on)
        self.pause_btn.setText("Resume trackers" if on else "Pause trackers")

    # -------------------------------------------------------------------- Help
    def _help_page(self) -> QWidget:
        tb = QTextBrowser()
        tb.setOpenExternalLinks(True)
        tb.setHtml(guides.HELP_HTML)
        w = QWidget()
        w.setObjectName("page")
        lay = QVBoxLayout(w)
        lay.setContentsMargins(28, 22, 28, 22)
        lay.addWidget(heading("Help"))
        lay.addWidget(tb, 1)
        return w

    # -------------------------------------------------------------------- tick
    def _tick(self) -> None:
        t = self.t
        current = self.NAV[self.pages.currentIndex()]
        frame = t.last_frame
        out = t.last_output

        if current == "Cameras":
            views = frame.views if frame is not None else []
            for prev, idx in ((self.prev_a, 0), (self.prev_b, 1)):
                if prev.isVisible() and idx < len(views) and views[idx].frame is not None:
                    vw = views[idx]
                    prev.show_frame(draw_skeleton(vw.frame, vw.image_xy, vw.visibility, self.cfg.min_visibility))
            self.prev_body.set_output(out)
            self.cam_error.setText(t.source_error or "")
            prog = t.camera_pairing_progress()
            if prog is not None:
                self.pair_progress.setValue(int(prog * 100))
                if prog >= 1.0:
                    self.pair_state.setText("Working it out...")
                    QApplication.processEvents()
                    t.finish_camera_pairing()
                    self.pair_btn.setText("Pair cameras")
                    self.pair_state.setText(t.status)
            elif self.cfg.stereo.valid:
                self.pair_progress.setValue(100)
                if not self.pair_state.text():
                    self.pair_state.setText(
                        f"Cameras are paired ({self.cfg.stereo.error_px:.1f} px). Pair again if a camera moved.")
        elif current == "Calibrate":
            self.cal_body.set_output(out)
            phase = t.calibration_phase()
            if phase is None:
                self.cal_btn.setText("Calibrate again" if self.cfg.calibration.valid else "Calibrate")
                self.cal_big.setText("")
            else:
                name, left = phase
                self.cal_btn.setText("Cancel")
                self.cal_big.setText(f"Get ready... {math.ceil(left)}" if name == "countdown" else "Hold still!")
            if not t.body_seen and phase is not None:
                self.cal_state.setText("I can't see you yet. Make sure your whole body is in view.")
            else:
                self.cal_state.setText(t.status)
        elif current == "Setup guide":
            checks = self._step_checks()
            for key, mark in self.step_marks.items():
                if checks.get(key):
                    mark.setText("✓")
                    mark.setStyleSheet(f"font-size:16pt;color:{OK};")
                else:
                    mark.setText("○")
                    mark.setStyleSheet("font-size:16pt;color:#6b7280;")
        elif current == "Output":
            if t.paused:
                self.out_status.setText("Paused. Trackers are switched off in SteamVR.")
            elif not self.cfg.calibration.valid:
                self.out_status.setText("Not sending yet: calibrate first.")
            elif t.sending:
                self.out_status.setText(f"Sending {len(self.cfg.enabled_trackers)} trackers at {t.sent_hz:.0f} Hz.")
            else:
                self.out_status.setText("Not sending: no body seen in the last 2 seconds.")

        # status bar
        if t.source is None:
            self.sb_cam.set_state("bad", "No camera  ")
        else:
            fps = t.camera_fps
            state = "ok" if fps > 45 else "warn" if fps > 20 else "bad"
            self.sb_cam.set_state(state, f"{fps:.0f} fps, pose {t.pose_ms:.0f} ms  ")
        if self.cfg.use_headset:
            self.sb_head.set_state("ok" if t.headset_connected else "warn",
                                   "Headset  " if t.headset_connected else "No SteamVR  ")
        else:
            self.sb_head.set_state("", "Headset off  ")
        if not self.cfg.calibration.valid:
            self.sb_send.set_state("warn", "Not calibrated  ")
        elif t.sending:
            self.sb_send.set_state("ok", f"Sending {t.sent_hz:.0f} Hz  ")
        else:
            self.sb_send.set_state("warn", "Paused  " if t.paused else "Not sending  ")
        if current != "Calibrate":
            self.sb_msg.setText(t.status)

    def closeEvent(self, ev) -> None:
        self.timer.stop()
        self.t.save()
        self.t.stop()
        super().closeEvent(ev)


def run_gui(tracker: Tracker) -> int:
    app = QApplication.instance() or QApplication(sys.argv)
    app.setApplicationName("Body Tracker")
    app.setStyle("Fusion")
    pal = app.palette()
    pal.setColor(QPalette.ColorRole.Link, QColor(ACCENT))
    app.setPalette(pal)
    app.setStyleSheet(STYLE)
    win = MainWindow(tracker)
    tracker.start()
    win.show()
    return app.exec()
