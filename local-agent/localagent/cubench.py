"""The computer use test: a window of small, randomly laid out tasks (buttons with words, a tiny
dot, a text box, a game-style menu that Windows accessibility can't see, a slider, a drag, a held
key, a web form) that the model does with your real mouse and keyboard, one after another. Each
task checks itself, and every step is timed: how long the model thought, how long the tools took,
tokens, screenshots. The results go to data/bench/ as JSON (for finding what is slow or misses)
and as a short summary.

Run it from Settings > Computer use, or with computer-use-test.bat.
"""

from __future__ import annotations

import copy
import http.server
import json
import math
import random
import tempfile
import threading
import time
import urllib.parse
from dataclasses import dataclass, field
from pathlib import Path

from .agent import Agent, AgentUI
from .config import DATA_DIR
from .permissions import Decision

TITLE = "Computer use test"
FRUITS = ["Apple", "Banana", "Cherry", "Mango", "Peach", "Plum", "Lemon", "Lime", "Kiwi", "Grape", "Melon", "Pear",
          "Fig", "Guava", "Papaya", "Apricot", "Coconut", "Olive"]
NAME = "Glowy Ball Bedtime"


@dataclass
class Task:
    name: str
    label: str            # for people
    prompt: str = ""      # filled in when it is set up (it names random things)


TASKS = [Task("words", "Click a button by its words"),
         Task("small", "Click a small dot"),
         Task("type", "Type into a box and press Enter"),
         Task("game", "Press a button in a game-style menu"),
         Task("slider", "Set a slider to 70"),
         Task("drag", "Drag a box into a square"),
         Task("hold", "Hold a key for 2 seconds"),
         Task("chop", "Hold a click on a thin tree trunk in a side-view game"),
         Task("chase", "Click a target that jumps somewhere else every 2.5 seconds, 3 times"),
         Task("web", "Fill in and send a web form")]

# What a run can be compared against (Settings fields set for that run only).
VARIANTS = {
    "as set": {},
    "helpers off": {"text_tools": False, "browser_tools": False, "screen_marks": False},
    "thinking low": {"thinking": "low"},
    "marks always": {"screen_marks": True},
    "big screenshots": {"screen_max_side": 1344},
    "0-1000 grid": {"coord_mode": "normalized"},
    "watch mode off": {"watch_tool": False},
}


# ---------------------------------------------------------------------- what the agent did
@dataclass
class Record:
    turns: list = field(default_factory=list)
    tools: list = field(default_factory=list)
    notices: list = field(default_factory=list)
    corner_stop: bool = False


class BenchUI(AgentUI):
    """Times every step and keeps what the model thought and did (shortened) for reading later."""

    def __init__(self, rec: Record):
        self.rec = rec
        self._turn: dict | None = None
        self._tools: dict = {}

    def assistant_start(self):
        self._turn = dict(start=time.time(), reasoning="", text="", reasoning_chars=0)

    def reasoning(self, s: str):
        if self._turn is not None:
            self._turn["reasoning_chars"] += len(s)
            if len(self._turn["reasoning"]) < 2000:
                self._turn["reasoning"] += s

    def text(self, s: str):
        if self._turn is not None and len(self._turn["text"]) < 600:
            self._turn["text"] += s

    def assistant_end(self, turn):
        t = self._turn or dict(start=time.time(), reasoning="", text="", reasoning_chars=0)
        self._turn = None
        usage = dict(turn.usage or {})
        t.update(seconds=round(time.time() - t["start"], 2), finish=turn.finish_reason, stopped=turn.stopped,
                 calls=[{"name": c.name, "args": c.arguments[:300]} for c in turn.tool_calls],
                 prompt_tokens=int(usage.get("prompt_tokens") or 0),
                 completion_tokens=int(usage.get("completion_tokens") or 0),
                 cached_tokens=int(usage.get("cache_n") or 0), prompt_ms=usage.get("prompt_ms"),
                 predicted_ms=usage.get("predicted_ms"), tokens_per_sec=turn.tokens_per_sec)
        t["reasoning"] = t["reasoning"][:2000]
        t.pop("start")
        self.rec.turns.append(t)

    def tool_start(self, call_id, name, args, summary):
        self._tools[call_id] = (name, time.time(), json.dumps(args)[:300])

    def tool_end(self, call_id, name, result):
        name, start, args = self._tools.pop(call_id, (name, time.time(), ""))
        text = result.text or ""
        if "emergency stop" in text.lower():
            self.rec.corner_stop = True
        self.rec.tools.append(dict(name=name, args=args, seconds=round(time.time() - start, 2), ok=result.ok,
                                   images=len(result.images), result=text[:400]))

    def notice(self, s: str):
        self.rec.notices.append(s[:300])

    def approve(self, req) -> Decision:
        # clicks and keys never ask; anything else (commands, files) isn't what is being tested
        return Decision(False, note="This is a computer use test: do it with the screen or browser tools.")

    def ask(self, question, options):
        return ""


# ---------------------------------------------------------------------- the web task's page
FORM = """<!doctype html><html><head><meta charset=utf-8><title>Pumpkin order</title>
<style>body{font:18px sans-serif;margin:40px;background:#fdf6ec} label{display:block;margin:14px 0}
input,select,button{font-size:18px;padding:6px}</style></head><body><h1>Pumpkin order</h1>
<form method=post action=/done>
<label>Name <input name=name autocomplete=off></label>
<label>Colour <select name=colour><option>Red</option><option>Green</option><option>Blue</option></select></label>
<label><input type=checkbox name=pumpkins value=yes> I like pumpkins</label>
<button>Submit</button></form></body></html>"""


class _FormServer:
    def __init__(self, on_post):
        class H(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _page(self, body: str):
                data = body.encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                self._page(FORM)

            def do_POST(self):
                raw = self.rfile.read(int(self.headers.get("Content-Length") or 0)).decode()
                ok = on_post(urllib.parse.parse_qs(raw))
                self._page("<title>Thanks</title><h1>" + ("Order received." if ok else "That's not quite right.")
                           + "</h1>")

        self.srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.srv.server_address[1]}/"

    def close(self):
        self.srv.shutdown()
        self.srv.server_close()


# ---------------------------------------------------------------------- the window (Qt)
def make_window(monitor: int):
    """The test window, full screen on the screen the model looks at. Qt is imported here so the
    rest of this file (and the results) work without it."""
    from PySide6.QtCore import QPointF, QRectF, Qt, QTimer, Signal
    from PySide6.QtGui import QColor, QFont, QGuiApplication, QLinearGradient, QPainter, QPainterPath, QPen
    from PySide6.QtWidgets import QHBoxLayout, QLabel, QLineEdit, QPushButton, QSlider, QVBoxLayout, QWidget

    class Canvas(QWidget):
        """A painted area with no accessibility inside, like a game."""

        def __init__(self, parent, state):
            super().__init__(parent)
            self.state = state
            self.setMouseTracking(False)

    class Window(QWidget):
        run_on_gui = Signal(object)

        def __init__(self):
            super().__init__(None, Qt.Window | Qt.WindowStaysOnTopHint | Qt.FramelessWindowHint)
            self.setWindowTitle(TITLE)
            self.setStyleSheet("QWidget#bench{background:#20242c;} QLabel{color:#e8e8e8;}"
                               "QPushButton{font-size:15px;padding:6px 10px;}")
            self.setObjectName("bench")
            self.setAttribute(Qt.WA_StyledBackground, True)
            lay = QVBoxLayout(self)
            lay.setContentsMargins(0, 0, 0, 0)
            bar = QHBoxLayout()
            bar.setContentsMargins(12, 6, 12, 6)
            self.status = QLabel(TITLE)
            self.status.setStyleSheet("color:#9aa4b2;font-size:13px;")
            bar.addWidget(self.status, 1)
            self.stop_btn = QPushButton("Stop the test")
            self.stop_btn.setStyleSheet("font-size:12px;padding:2px 8px;")
            bar.addWidget(self.stop_btn)
            lay.addLayout(bar)
            self.area = QWidget(self)
            self.area.setFocusPolicy(Qt.StrongFocus)
            lay.addWidget(self.area, 1)
            self.state: dict = {}
            self.stopped = threading.Event()
            self.stop_btn.clicked.connect(self.stopped.set)
            self.run_on_gui.connect(lambda job: job(), Qt.QueuedConnection)
            self.key_down: float | None = None
            screen = _qscreen(monitor) or QGuiApplication.primaryScreen()
            self.setScreen(screen)
            self.setGeometry(screen.geometry())

        # -- from the test's thread: run fn on the GUI thread and wait for it
        def call(self, fn):
            done, box = threading.Event(), {}

            def job():
                try:
                    box["value"] = fn()
                except Exception as e:  # noqa: BLE001 - re-raised on the test's thread
                    box["error"] = e
                done.set()
            self.run_on_gui.emit(job)
            done.wait(30)
            if "error" in box:
                raise box["error"]
            return box.get("value")

        def clear(self):
            for w in self.area.findChildren(QWidget, options=Qt.FindDirectChildrenOnly):
                w.deleteLater()

        def place(self, w, rng, size, taken):
            """Somewhere free and random in the area (not under the top bar)."""
            aw, ah = max(400, self.area.width()), max(300, self.area.height())
            for _ in range(200):
                x, y = rng.randint(40, aw - size[0] - 40), rng.randint(40, ah - size[1] - 40)
                r = QRectF(x - 20, y - 20, size[0] + 40, size[1] + 40)
                if not any(r.intersects(t) for t in taken):
                    break
            taken.append(QRectF(x, y, *size))
            w.setGeometry(x, y, *size)
            w.show()
            return x, y

        def done(self):
            self.state["done"].set()      # the note of a wrong try before it stays

        def wrong(self, note):
            self.state["wrong"] += 1
            self.state["note"] = note

        def setup(self, task: Task, rng: random.Random, url: str = ""):
            self.clear()
            self.state = {"done": threading.Event(), "wrong": 0, "note": ""}
            taken: list = []
            make = getattr(self, "_task_" + task.name)
            task.prompt = make(rng, taken, url)
            if task.name == "web":
                self.showMinimized()       # the browser has to be seen
            else:
                self.showFullScreen()
                self.raise_()
                self.activateWindow()
                self.area.setFocus()
                _to_front(self)

        def show_status(self, text):
            self.status.setText(text)

        # -- the tasks; each returns what the model is asked
        def _task_words(self, rng, taken, url):
            names = rng.sample(FRUITS, 16)
            want = rng.choice(names)
            for n in names:
                b = QPushButton(n, self.area)
                b.clicked.connect(lambda _=False, n=n: self.done() if n == want else self.wrong(f"clicked {n}"))
                self.place(b, rng, (130, 42), taken)
            return f"Click the button that says '{want}'."

        def _task_small(self, rng, taken, url):
            win = self
            dots = [(rng.uniform(0.05, 0.95), rng.uniform(0.05, 0.95)) for _ in range(30)]
            red = (rng.uniform(0.1, 0.9), rng.uniform(0.1, 0.9))

            class Dots(Canvas):
                def paintEvent(self, e):
                    p = QPainter(self)
                    p.setRenderHint(QPainter.Antialiasing)
                    p.setPen(Qt.NoPen)
                    p.setBrush(QColor("#5b6270"))
                    for fx, fy in dots:
                        p.drawEllipse(QPointF(fx * self.width(), fy * self.height()), 9, 9)
                    p.setBrush(QColor("#ff3030"))
                    p.drawEllipse(QPointF(red[0] * self.width(), red[1] * self.height()), 6, 6)

                def mousePressEvent(self, e):
                    d = math.dist((e.position().x(), e.position().y()), (red[0] * self.width(), red[1] * self.height()))
                    win.done() if d <= 10 else win.wrong(f"clicked {d:.0f} px from the dot")

            c = Dots(self.area, self.state)
            c.red = red
            c.setGeometry(self.area.rect())
            c.show()
            return "Click the small red dot (it's smaller than the grey ones)."

        def _task_type(self, rng, taken, url):
            box = QWidget(self.area)
            row = QHBoxLayout(box)
            lab = QLabel("Name:")
            lab.setStyleSheet("font-size:16px;")
            edit = QLineEdit()
            edit.setStyleSheet("font-size:16px;padding:4px;")
            edit.setAccessibleName("Name")
            row.addWidget(lab)
            row.addWidget(edit, 1)

            def entered():
                t = edit.text().strip()
                self.done() if t == NAME else self.wrong(f"entered {t!r}")
            edit.returnPressed.connect(entered)
            self.place(box, rng, (420, 56), taken)
            return f"Type '{NAME}' into the Name box, then press Enter."

        def _task_game(self, rng, taken, url):
            win = self
            labels = ["WORKSHOP", "UPGRADE", "SELL ALL", "SETTINGS"]
            rng.shuffle(labels)
            spots = []

            class Menu(Canvas):
                def paintEvent(self, e):
                    p = QPainter(self)
                    p.setRenderHint(QPainter.Antialiasing)
                    g = QLinearGradient(0, 0, 0, self.height())
                    g.setColorAt(0, QColor("#3b2a4f"))
                    g.setColorAt(1, QColor("#1b3a2a"))
                    p.fillRect(self.rect(), g)
                    p.setPen(Qt.NoPen)
                    r2 = random.Random(7)
                    for _ in range(25):       # some "scenery"
                        p.setBrush(QColor(r2.randint(40, 120), r2.randint(40, 120), r2.randint(40, 120), 120))
                        p.drawEllipse(QPointF(r2.uniform(0, self.width()), r2.uniform(0, self.height())),
                                      r2.uniform(20, 90), r2.uniform(20, 90))
                    f = QFont("Arial Black", 18)
                    f.setBold(True)
                    p.setFont(f)
                    for rect, text, color in spots:
                        bg = QLinearGradient(rect.topLeft(), rect.bottomLeft())
                        bg.setColorAt(0, color.lighter(130))
                        bg.setColorAt(1, color.darker(120))
                        p.setPen(QPen(QColor("#1a1a1a"), 3))
                        p.setBrush(bg)
                        p.drawRoundedRect(rect, 14, 14)
                        path = QPainterPath()
                        fm = p.fontMetrics()
                        w = fm.horizontalAdvance(text)
                        path.addText(rect.center().x() - w / 2, rect.center().y() + fm.ascent() / 2 - 3, f, text)
                        p.setPen(QPen(QColor("#202020"), 4))
                        p.setBrush(Qt.NoBrush)
                        p.drawPath(path)
                        p.fillPath(path, QColor("#ffffff"))

                def mousePressEvent(self, e):
                    self._down = e.position()

                def mouseReleaseEvent(self, e):
                    pt = e.position()
                    for rect, text, _ in spots:
                        if rect.contains(pt) and rect.contains(getattr(self, "_down", pt)):
                            win.done() if text == "SELL ALL" else win.wrong(f"pressed {text}")
                            return

            c = Menu(self.area, self.state)
            c.spots = spots
            c.setGeometry(self.area.rect())
            colors = [QColor("#e6a817"), QColor("#8e5bd6"), QColor("#3dbb4a"), QColor("#d64545")]
            aw, ah = max(400, self.area.width()), max(300, self.area.height())
            for text, color in zip(labels, colors):
                for _ in range(200):
                    r = QRectF(rng.randint(60, aw - 300), rng.randint(60, ah - 120), 230, 64)
                    if not any(r.adjusted(-30, -30, 30, 30).intersects(s[0]) for s in spots):
                        break
                spots.append((r, text, color))
            c.show()
            return "This is a game's menu: press its SELL ALL button."

        def _task_slider(self, rng, taken, url):
            box = QWidget(self.area)
            row = QHBoxLayout(box)
            s = QSlider(Qt.Horizontal)
            s.setRange(0, 100)
            s.setValue(rng.choice([rng.randint(5, 30), rng.randint(85, 98)]))
            s.setAccessibleName("Volume")
            val = QLabel(str(s.value()))
            val.setStyleSheet("font-size:18px;min-width:40px;")
            row.addWidget(QLabel("Volume"))
            row.addWidget(s, 1)
            row.addWidget(val)

            def check(*_):
                val.setText(str(s.value()))
                if 68 <= s.value() <= 72 and not s.isSliderDown():
                    self.done()
            s.valueChanged.connect(check)
            s.sliderReleased.connect(check)
            self.place(box, rng, (560, 60), taken)
            return "Set the Volume slider to 70 (the number next to it shows its value; 68 to 72 counts)."

        def _task_drag(self, rng, taken, url):
            win = self
            st = {}

            class Board(Canvas):
                def paintEvent(self, e):
                    p = QPainter(self)
                    p.setRenderHint(QPainter.Antialiasing)
                    p.setPen(QPen(QColor("#cfd6e0"), 3, Qt.DashLine))
                    p.setBrush(Qt.NoBrush)
                    p.drawRect(st["target"])
                    p.setPen(Qt.NoPen)
                    p.setBrush(QColor("#ff8c1a"))
                    p.drawRoundedRect(st["box"], 8, 8)

                def mousePressEvent(self, e):
                    if st["box"].contains(e.position()):
                        st["grab"] = e.position() - st["box"].topLeft()

                def mouseMoveEvent(self, e):
                    if st.get("grab") is not None:
                        st["box"].moveTopLeft(e.position() - st["grab"])
                        self.update()

                def mouseReleaseEvent(self, e):
                    if st.get("grab") is None:
                        return
                    st["grab"] = None
                    if st["target"].contains(st["box"].center()):
                        win.done()
                    else:
                        win.wrong("let go outside the square")

            aw, ah = max(400, self.area.width()), max(300, self.area.height())
            st["box"] = QRectF(rng.randint(60, aw // 2 - 100), rng.randint(60, ah - 120), 56, 56)
            st["target"] = QRectF(rng.randint(aw // 2 + 60, aw - 180), rng.randint(60, ah - 180), 120, 120)
            c = Board(self.area, self.state)
            c.st = st
            c.setGeometry(self.area.rect())
            c.setMouseTracking(False)
            c.show()
            return "Drag the orange box into the dashed square (hold the mouse button down while moving)."

        def _task_hold(self, rng, taken, url):
            lab = QLabel("Hold the D key for about 2 seconds", self.area)
            lab.setStyleSheet("font-size:28px;color:#ffd24a;")
            lab.setAlignment(Qt.AlignCenter)
            self.place(lab, rng, (620, 80), taken)
            self.key_down = None
            return "Hold down the D key for about 2 seconds (1.5 to 4 seconds counts), then let go."

        def _task_chop(self, rng, taken, url):
            """Like chopping a tree in Terraria: thin trunks, your character in the middle (the camera
            follows it) and someone else who looks a lot like it, next to another tree."""
            win = self
            aw, ah = max(400, self.area.width()), max(300, self.area.height())
            ground = int(ah * 0.62)
            me = aw // 2
            side = rng.choice((-1, 1))
            near = me + side * rng.randint(34, 50)            # the trunk next to your character
            other = rng.choice([x for x in range(80, aw - 80, 40) if abs(x - me) > aw // 4])
            xs = [near, other + rng.choice((-1, 1)) * 40]
            for _ in range(400):
                if len(xs) >= 14:
                    break
                x = rng.randint(60, aw - 60)
                if all(abs(x - t) > 70 for t in xs) and abs(x - me) > 110:
                    xs.append(x)
            trees = [(x, rng.randint(150, 300)) for x in xs]
            st = {"down": None}
            TRUNK = 18

            def trunk(x, h):
                return QRectF(x - TRUNK / 2, ground - h, TRUNK, h)

            class Scene(Canvas):
                def paintEvent(self, e):
                    p = QPainter(self)
                    sky = QLinearGradient(0, 0, 0, ground)
                    sky.setColorAt(0, QColor("#3a6fd8"))
                    sky.setColorAt(1, QColor("#9cc8f0"))
                    p.fillRect(QRectF(0, 0, self.width(), ground), sky)
                    p.setPen(Qt.NoPen)
                    r2 = random.Random(11)
                    for _ in range(18):       # hills and bushes behind
                        p.setBrush(QColor(40, r2.randint(90, 130), 60))
                        p.drawEllipse(QPointF(r2.uniform(0, self.width()), ground), r2.uniform(60, 220),
                                      r2.uniform(40, 140))
                    p.fillRect(QRectF(0, ground, self.width(), self.height() - ground), QColor("#5a3d22"))
                    p.fillRect(QRectF(0, ground, self.width(), 8), QColor("#3fbf3f"))
                    for x, h in trees:
                        p.fillRect(trunk(x, h), QColor("#a0703c"))
                        p.fillRect(QRectF(x - TRUNK / 2, ground - h, 4, h), QColor("#7a5228"))
                        p.setBrush(QColor("#2f9e3a"))
                        p.drawEllipse(QPointF(x, ground - h), 34, 26)
                    # your character (dark hood) and someone else (brown hair), both about 18x40
                    for x, head in ((me, "#2b2b40"), (other, "#8a4a1c")):
                        p.fillRect(QRectF(x - 8, ground - 26, 16, 18), QColor("#c9b08a"))     # shirt
                        p.fillRect(QRectF(x - 8, ground - 8, 16, 8), QColor("#34508a"))       # trousers
                        p.setBrush(QColor("#e8c09a"))
                        p.drawEllipse(QPointF(x, ground - 32), 7, 7)
                        p.setBrush(QColor(head))
                        p.drawChord(QRectF(x - 9, ground - 42, 18, 16), 0, 180 * 16)

                def mousePressEvent(self, e):
                    st["down"] = (time.time(), e.position())

                def mouseReleaseEvent(self, e):
                    if st["down"] is None:
                        return
                    t0, pt = st["down"]
                    st["down"] = None
                    held = time.time() - t0
                    if trunk(*trees[0]).adjusted(-2, 0, 2, 0).contains(pt):
                        win.done() if held >= 0.8 else win.wrong(f"on the trunk but held only {held:.1f} s")
                        return
                    hit = next((i for i, t in enumerate(trees) if trunk(*t).adjusted(-2, 0, 2, 0).contains(pt)), None)
                    if hit == 1:
                        win.wrong("held on the tree next to the other person")
                    else:
                        win.wrong(f"held {round(pt.x() - near):+d} px across and {round(pt.y() - (ground - trees[0][1] / 2)):+d} "
                                  "px down from the middle of the right trunk" + (" (another tree)" if hit else ""))

            c = Scene(self.area, self.state)
            c.trees, c.ground, c.me = trees, ground, me
            c.setGeometry(self.area.rect())
            c.show()
            return ("This is a side-view game. Chop the tree that stands right next to your character: hold the left "
                    "mouse button on that tree's trunk for at least 1 second.")

        def _task_chase(self, rng, taken, url):
            """Something that doesn't wait: a target that jumps every JUMP seconds. A step that takes longer
            than that clicks where it was."""
            win = self
            JUMP, R, NEED = 2.5, 22, 3
            st = {"hits": 0, "at": None, "moved": 0.0}

            class Field(Canvas):
                def jump(self):
                    w, h = max(400, self.width()), max(300, self.height())
                    st["at"] = QPointF(rng.randint(60, w - 60), rng.randint(60, h - 60))
                    st["moved"] = time.time()
                    self.update()

                def paintEvent(self, e):
                    p = QPainter(self)
                    p.setRenderHint(QPainter.Antialiasing)
                    p.fillRect(self.rect(), QColor("#1d2b22"))
                    p.setPen(QColor("#e8e8e8"))
                    p.setFont(QFont("Arial", 16))
                    p.drawText(20, 30, f"Hits: {st['hits']} of {NEED}")
                    if st["at"] is not None:
                        p.setPen(QPen(QColor("#ffffff"), 3))
                        p.setBrush(QColor("#ff4d2e"))
                        p.drawEllipse(st["at"], R, R)

                def mousePressEvent(self, e):
                    if st["at"] is None:
                        return
                    d = math.dist((e.position().x(), e.position().y()), (st["at"].x(), st["at"].y()))
                    if d <= R + 3:
                        st["hits"] += 1
                        if st["hits"] >= NEED:
                            win.done()
                        self.jump()
                    else:
                        win.wrong(f"clicked {d:.0f} px from the target {time.time() - st['moved']:.1f} s after it "
                                  "last jumped")

            c = Field(self.area, self.state)
            c.st = st
            c.setGeometry(self.area.rect())
            c.show()
            c.jump()
            c._timer = QTimer(c)
            c._timer.timeout.connect(c.jump)
            c._timer.start(int(JUMP * 1000))
            return (f"Click the red target {NEED} times. It jumps to a new place every {JUMP:g} seconds and after "
                    "each hit, so be quick.")

        def _task_web(self, rng, taken, url):
            return (f"Open {url} in a web browser and fill in its form: Name 'Aidan', Colour 'Blue', tick "
                    "'I like pumpkins', then press Submit.")

        def keyPressEvent(self, e):
            if self.state and e.key() == Qt.Key_D and not e.isAutoRepeat():
                self.key_down = time.time()

        def keyReleaseEvent(self, e):
            if self.state and e.key() == Qt.Key_D and not e.isAutoRepeat() and self.key_down:
                held = time.time() - self.key_down
                self.key_down = None
                self.done() if 1.5 <= held <= 4 else self.wrong(f"held D for {held:.1f} s")

    def _to_front(w):
        """Windows only lets a program take the front right after a key press (this app is minimized
        then): the same trick the click tool uses."""
        import sys

        if sys.platform == "win32":
            try:
                from .tools import winput

                winput.bring_to_front(int(w.winId()))
            except Exception:  # noqa: BLE001 - it stays where it is
                pass

    def _qscreen(n: int):
        """Qt's screen for mss monitor n (Qt counts in scaled pixels and in its own order)."""
        try:
            from .tools.screen import monitors

            mons = monitors()
            m = mons[n - 1]
        except Exception:  # noqa: BLE001
            return None
        best = None
        for s in QGuiApplication.screens():
            g, dpr = s.geometry(), s.devicePixelRatio()
            size_ok = abs(g.width() * dpr - m["width"]) < 4 and abs(g.height() * dpr - m["height"]) < 4
            near = abs(g.x() * dpr - m["left"]) + abs(g.y() * dpr - m["top"])
            if size_ok and (best is None or near < best[0]):
                best = (near, s)
        return best[1] if best else None

    w = Window()
    w._timer = QTimer(w)        # keeps Qt's event loop ticking while the test thread waits
    w._timer.start(200)
    return w


# ---------------------------------------------------------------------- running it
def run(settings, window, variants=("as set",), tasks=None, timeout: float = 150.0, progress=None,
        agent_factory=Agent) -> dict:
    """Run the tasks once per variant. window: from make_window (or a stand-in with the same
    call / setup / state / stopped / show_status). Returns the results (also saved)."""
    tasks = [t for t in TASKS if not tasks or t.name in tasks]
    progress = progress or (lambda s: None)

    def posted(q) -> bool:
        ok = (q.get("name", [""])[0].strip().lower() == "aidan" and q.get("colour", [""])[0] == "Blue"
              and q.get("pumpkins", [""])[0] == "yes")
        st = window.state
        if ok:
            window.call(lambda: window.done())
        elif st:
            window.call(lambda: window.wrong(f"sent {dict(q)}"))
        return ok

    server = _FormServer(posted)
    out = {"when": time.strftime("%Y-%m-%d %H:%M:%S"), "model": "", "timeout": timeout,
           "settings": {k: getattr(settings, k, None) for k in ("thinking", "screen_max_side", "monitor", "coord_mode",
                                                                   "screen_marks", "browser_tools", "text_tools", "watch_tool",
                                                                   "watch_size", "watch_frames",
                                                                   "screenshot_after_action", "model")},
           "variants": []}
    rng = random.Random()
    try:
        for vi, vname in enumerate(variants):
            over = VARIANTS.get(vname, {})
            results = []
            for ti, task in enumerate(tasks):
                if window.stopped.is_set():
                    break
                # nothing in it for the model to act on: the task's own name hinted at how to do it, and
                # "don't touch the mouse" (meant for the person) made it wonder whether it may click
                label = f"{TITLE} · {vname} · {ti + 1} of {len(tasks)}"
                window.call(lambda: window.show_status(label))
                window.call(lambda: window.setup(task, rng, server.url))
                time.sleep(0.6)       # drawn and in front
                progress(f"{vname}: {task.label}")
                results.append(_one(settings, over, window, task, timeout, agent_factory, out))
                if results[-1].get("corner_stop"):
                    window.stopped.set()
            out["variants"].append({"name": vname, "overrides": over, "tasks": results, "summary": summarize(results)})
    finally:
        server.close()
    out["text"] = summary_text(out)
    save(out)
    return out


def _one(settings, over: dict, window, task: Task, timeout: float, agent_factory, out: dict) -> dict:
    s = copy.deepcopy(settings)
    for k, v in {"computer_use": True, "auto_memory": False, "auto_recall": False, "approval_mode": "ask",
                 "max_steps": 0, "workspace": tempfile.mkdtemp(prefix="cutest-"), **over}.items():
        setattr(s, k, v)
    rec = Record()
    agent = agent_factory(s, BenchUI(rec))
    state = window.state
    start = time.time()
    error = ""

    def watch():
        end = start + timeout
        while time.time() < end and not state["done"].is_set() and not window.stopped.is_set():
            state["done"].wait(0.2)
        agent.stop.set()      # done, out of time or stopped: the agent stops now, even mid-reply

    watcher = threading.Thread(target=watch, daemon=True)
    watcher.start()
    prompt = (f"The '{TITLE}' window fills screen {s.monitor}. " + task.prompt)
    try:
        agent.run(prompt)
    except Exception as e:  # noqa: BLE001 - one task failing doesn't end the test
        error = f"{type(e).__name__}: {e}"[:300]
    finished = time.time()
    agent.stop.set()
    watcher.join(2)
    ok = state["done"].is_set()
    if not out.get("model"):
        out["model"] = getattr(getattr(agent, "client", None), "model", "") or ""
    turns, tools = rec.turns, rec.tools
    return dict(task=task.name, label=task.label, prompt=prompt, ok=ok, wrong=state["wrong"], note=state["note"],
                seconds=round(finished - start, 1), steps=len(turns),
                model_seconds=round(sum(t["seconds"] for t in turns), 1),
                tool_seconds=round(sum(t["seconds"] for t in tools), 1),
                prompt_tokens=sum(t["prompt_tokens"] for t in turns),
                completion_tokens=sum(t["completion_tokens"] for t in turns),
                reasoning_chars=sum(t["reasoning_chars"] for t in turns),
                images=sum(t["images"] for t in tools), tools_used=[t["name"] for t in tools],
                timed_out=not ok and finished - start >= timeout - 1, corner_stop=rec.corner_stop, error=error,
                turns=turns, tool_calls=tools, notices=rec.notices)


def summarize(results: list[dict]) -> dict:
    done = [r for r in results if r["ok"]]
    n = max(1, len(done))
    return dict(done=len(done), total=len(results),
                seconds=round(sum(r["seconds"] for r in done) / n, 1),
                model_seconds=round(sum(r["model_seconds"] for r in done) / n, 1),
                tool_seconds=round(sum(r["tool_seconds"] for r in done) / n, 1),
                steps=round(sum(r["steps"] for r in done) / n, 1),
                tokens=round(sum(r["prompt_tokens"] + r["completion_tokens"] for r in done) / n),
                thinking_chars=round(sum(r["reasoning_chars"] for r in done) / n))


def summary_text(out: dict) -> str:
    lines = [f"Computer use test, {out['when']}" + (f", model {out['model']}" if out.get("model") else "")]
    for v in out["variants"]:
        s = v["summary"]
        lines.append(f"\n{v['name']}: {s['done']} of {s['total']} done. Per finished task: {s['seconds']} s "
                     f"(model {s['model_seconds']} s, tools {s['tool_seconds']} s), {s['steps']} steps, "
                     f"{s['tokens']:,} tokens.")
        for r in v["tasks"]:
            mark = "✓" if r["ok"] else "✗"
            why = "" if r["ok"] else (" (out of time)" if r["timed_out"] else f" ({r['error']})" if r["error"] else "")
            extra = f", {r['wrong']} wrong: {r['note']}" if r["wrong"] else ""
            lines.append(f"  {mark} {r['label']}: {r['seconds']} s, {r['steps']} steps{extra}{why}")
    return "\n".join(lines)


def save(out: dict) -> Path:
    folder = DATA_DIR / "bench"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"cu-{time.strftime('%Y-%m-%d_%H-%M-%S')}.json"
    path.write_text(json.dumps(out, indent=1, default=str), encoding="utf-8")
    (folder / "latest.txt").write_text(out.get("text", ""), encoding="utf-8")
    out["path"] = str(path)
    return path


def main(argv=None):
    """computer-use-test.bat: python -m localagent.cubench [--compare helpers,thinking,marks] [--tasks a,b]"""
    import argparse
    import sys

    from PySide6.QtWidgets import QApplication

    from .config import Settings

    ap = argparse.ArgumentParser(description="Computer use test")
    ap.add_argument("--compare", default="", help="also run with: helpers (off), thinking (low), marks (always), "
                                                  "big (screenshots 1344 wide), grid (0-1000 coordinates), "
                                                  "watch (watch mode off)")
    ap.add_argument("--tasks", default="", help="only these tasks: " + ",".join(t.name for t in TASKS))
    ap.add_argument("--timeout", type=float, default=150)
    a = ap.parse_args(argv)
    names = {"helpers": "helpers off", "thinking": "thinking low", "marks": "marks always",
             "big": "big screenshots", "grid": "0-1000 grid", "watch": "watch mode off"}
    variants = ["as set"] + [names[c] for c in a.compare.split(",") if c in names]
    app = QApplication.instance() or QApplication(sys.argv)
    settings = Settings.load()
    win = make_window(int(settings.monitor or 1))
    win.showFullScreen()
    result: dict = {}

    def work():
        try:
            result.update(run(settings, win, variants, [t for t in a.tasks.split(",") if t], a.timeout,
                              progress=lambda s: print(s, flush=True)))
        finally:
            win.call(app.quit)

    threading.Thread(target=work, daemon=True).start()
    app.exec()
    print(result.get("text", "The test didn't finish."))
    if result.get("path"):
        print(f"\nDetails: {result['path']}")


if __name__ == "__main__":
    main()
