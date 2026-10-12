"""The app window (PySide6 / Qt)."""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path

from PySide6.QtCore import QBuffer, QByteArray, QIODevice, QObject, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QColor, QDesktopServices, QFont, QImage, QKeySequence, QPalette, QPixmap, QShortcut
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QDialog, QDialogButtonBox, QDoubleSpinBox, QFileDialog,
    QFormLayout, QFrame, QGroupBox, QHBoxLayout, QInputDialog, QLabel, QLineEdit, QListWidget,
    QListWidgetItem, QMainWindow, QMenu, QMessageBox, QPlainTextEdit, QProgressBar, QPushButton, QScrollArea,
    QSizePolicy, QSpinBox, QStackedWidget, QTextBrowser, QToolButton, QVBoxLayout, QWidget,
)

from . import catalog, launcher, mcp, plugins, strata
from .agent import Agent, AgentUI
from .config import APP_DIR, APPROVAL_MODES, DATA_DIR, SERVER_PRESETS, Settings
from .gguf import gpu_info, model_info
from .guide import GUIDE, sampling_for
from .llm import LLMClient, ServerError
from . import memory as memory_mod
from .permissions import ApprovalRequest, Decision, risk_text
from .tools import REGISTRY, RISK_LABELS

ACCENT = "#4f8cff"
OK = "#2fbf71"
WARN = "#e0a43a"
BAD = "#e5534b"

THINKING_LEVELS = [("auto", "Model default"), ("off", "Off (fastest)"), ("low", "Low"), ("medium", "Medium"),
                   ("high", "High (smartest)")]
# the chat bar's short names for the approval modes (the long ones are the tooltips)
PERMISSION_SHORT = {"read_only": "Read only", "ask": "Ask first", "auto_edit": "Auto-edit files",
                    "auto": "Full auto"}

STYLE = f"""
QWidget {{ font-size: 10pt; }}
QMainWindow, QWidget#page {{ background: #16181d; color: #e6e8ee; }}
QLabel, QCheckBox, QRadioButton {{ color: #e6e8ee; }}
QLabel[muted="true"] {{ color: #9aa1ad; }}
QLabel#h1 {{ font-size: 16pt; font-weight: 600; }}
QListWidget {{ background: #1e2128; border: 1px solid #2c303a; border-radius: 8px; color: #e6e8ee; }}
QListWidget::item {{ padding: 5px 6px; }}
QListWidget::item:selected {{ background: #2b3550; color: white; }}
QListWidget#nav {{ background: #101216; border: none; color: #c9ced8; font-size: 11pt; padding-top: 8px; }}
QListWidget#nav::item {{ padding: 10px 16px; }}
QListWidget#nav::item:selected {{ background: #232733; color: white; border-left: 3px solid {ACCENT}; }}
QFrame#user {{ background: #23304a; border-radius: 10px; }}
QFrame#assistant {{ background: transparent; }}
QFrame#tool {{ background: #1e2128; border: 1px solid #2c303a; border-radius: 8px; }}
QFrame#side {{ background: #1b1d23; border-left: 1px solid #2c303a; }}
QPushButton {{ background: #2a2f3a; color: #e6e8ee; border: 1px solid #3a404d; border-radius: 6px; padding: 6px 14px; }}
QPushButton:hover {{ background: #333948; }}
QPushButton:disabled {{ color: #6b717c; }}
QPushButton#primary {{ background: {ACCENT}; border: none; color: white; font-weight: 600; }}
QPushButton#primary:hover {{ background: #6a9dff; }}
QPushButton#danger {{ background: {BAD}; border: none; color: white; font-weight: 600; }}
QPushButton#primary:disabled, QPushButton#danger:disabled {{ background: #2a2f3a; color: #6b717c; }}
QToolButton {{ color: #9aa1ad; border: none; background: transparent; }}
QToolButton:hover {{ color: #e6e8ee; }}
QToolButton#menu {{ background: #2a2f3a; color: #e6e8ee; border: 1px solid #3a404d; border-radius: 6px; padding: 5px 10px; }}
QToolButton#menu::menu-indicator {{ image: none; }}
QDialog, QMessageBox, QInputDialog {{ background: #16181d; color: #e6e8ee; }}
QMenu {{ background: #1e2128; color: #e6e8ee; border: 1px solid #3a404d; }}
QMenu::item {{ padding: 6px 22px; }}
QMenu::item:selected {{ background: #2b3550; }}
QMenu::item:disabled {{ color: #6b717c; }}
QToolTip {{ background: #23262e; color: #e6e8ee; border: 1px solid #3a404d; }}
QGroupBox {{ border: 1px solid #2c303a; border-radius: 8px; margin-top: 14px; padding: 10px; color: #c9ced8; }}
QGroupBox::title {{ subcontrol-origin: margin; left: 10px; padding: 0 4px; }}
QTextBrowser, QPlainTextEdit {{ background: #1e2128; border: 1px solid #2c303a; border-radius: 8px; color: #e6e8ee; }}
QLineEdit, QComboBox, QSpinBox, QDoubleSpinBox {{ background: #23262e; color: #e6e8ee; border: 1px solid #3a404d;
    border-radius: 4px; padding: 3px 6px; }}
QLineEdit:disabled, QComboBox:disabled, QSpinBox:disabled, QDoubleSpinBox:disabled {{ background: #1b1d23;
    color: #6b717c; border-color: #2c303a; }}
QComboBox QAbstractItemView {{ background: #1e2128; color: #e6e8ee; selection-background-color: #2b3550; }}
QProgressBar {{ background: #23262e; border: 1px solid #3a404d; border-radius: 4px; text-align: center; color: white; height: 14px; }}
QProgressBar::chunk {{ background: {ACCENT}; border-radius: 4px; }}
QScrollArea {{ border: none; background: transparent; }}
"""


def in_background(fn, done, poll_ms: int = 50) -> threading.Thread:
    """Run fn() on a worker thread, then done(result, error) on the GUI thread: network calls and
    folder walks must never freeze the window."""
    box: dict = {}

    def work():
        try:
            box["result"] = fn()
        except Exception as e:  # noqa: BLE001
            box["error"] = e

    t = threading.Thread(target=work, daemon=True)
    t.start()

    def check():
        if t.is_alive():
            QTimer.singleShot(poll_ms, check)
        else:
            done(box.get("result"), box.get("error"))

    QTimer.singleShot(poll_ms, check)
    return t


def _stop_background(agent) -> None:
    """End the commands a chat left running in the background (servers, watchers)."""
    if agent is None:
        return
    from .tools import shell
    fn = getattr(shell, "stop_background", None)
    if fn:
        try:
            fn(agent.ctx)
        except Exception:  # noqa: BLE001
            pass


def png_bytes(img: QImage, max_side: int = 1600) -> bytes:
    if max(img.width(), img.height()) > max_side:
        img = img.scaled(max_side, max_side, Qt.KeepAspectRatio, Qt.SmoothTransformation)
    ba = QByteArray()
    buf = QBuffer(ba)
    buf.open(QIODevice.WriteOnly)
    img.save(buf, "PNG")
    return bytes(ba)


def thumb(png: bytes, width: int = 320) -> QLabel:
    lab = QLabel()
    pm = QPixmap()
    pm.loadFromData(png)
    lab.setPixmap(pm.scaledToWidth(min(width, pm.width() or width), Qt.SmoothTransformation))
    lab.setCursor(Qt.PointingHandCursor)
    lab.setToolTip("Click to view full size")
    del pm         # keep only the small copy and the PNG bytes; decode full size on click

    def show(_e, data=png):
        full = QPixmap()
        full.loadFromData(data)
        ImageDialog(full, lab.window()).exec()

    lab.mousePressEvent = show
    return lab


def muted(text: str) -> QLabel:
    lab = QLabel(text)
    lab.setProperty("muted", True)
    lab.setWordWrap(True)
    return lab


class ElidedLabel(QLabel):
    """One line of plain text that shortens itself with "..." instead of widening the window."""

    def __init__(self, text: str = ""):
        super().__init__()
        self._full = text
        self.setTextFormat(Qt.PlainText)
        self.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        self.setMinimumWidth(40)
        super().setText(text)

    def setText(self, text: str):
        self._full = text
        self._elide()

    def fullText(self) -> str:
        return self._full

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self._elide()

    def _elide(self):
        super().setText(self.fontMetrics().elidedText(self._full, Qt.ElideRight, max(40, self.width())))


class ImageDialog(QDialog):
    def __init__(self, pm: QPixmap, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Image")
        area = QScrollArea()
        lab = QLabel()
        lab.setPixmap(pm)
        area.setWidget(lab)
        lay = QVBoxLayout(self)
        lay.addWidget(area)
        self.resize(min(pm.width() + 40, 1600), min(pm.height() + 40, 1000))


# ====================================================================== chat widgets
class UserBubble(QFrame):
    def __init__(self, text: str, images: list[bytes]):
        super().__init__()
        self.setObjectName("user")
        lay = QVBoxLayout(self)
        lab = QLabel(text)
        lab.setTextFormat(Qt.PlainText)      # what you typed, never rendered as HTML
        lab.setWordWrap(True)
        lab.setTextInteractionFlags(Qt.TextSelectableByMouse)
        lay.addWidget(lab)
        for p in images:
            lay.addWidget(thumb(p, 240))


class AssistantBlock(QFrame):
    def __init__(self):
        super().__init__()
        self.setObjectName("assistant")
        self.text, self.reasoning = "", ""
        self._dirty = False
        self._next = 0.0               # when the next re-render may happen (long replies render slower)
        self._think_start = None
        self._think_secs = None
        lay = QVBoxLayout(self)
        lay.setContentsMargins(4, 2, 4, 2)
        self.think_btn = QToolButton(text="▸ Thinking")
        self.think_btn.setVisible(False)
        self.think_btn.clicked.connect(self._toggle)
        self.preview = muted("")       # the latest line of thinking while it's collapsed, so it doesn't look stuck
        self.preview.setTextFormat(Qt.PlainText)
        self.preview.setWordWrap(False)
        self.preview.setVisible(False)
        self.think = muted("")
        self.think.setTextFormat(Qt.PlainText)
        self.think.setVisible(False)
        self.think.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.body = QLabel()
        self.body.setWordWrap(True)
        self.body.setTextFormat(Qt.MarkdownText)
        self.body.setTextInteractionFlags(Qt.TextSelectableByMouse | Qt.LinksAccessibleByMouse)
        self.body.setOpenExternalLinks(True)
        self.body.setVisible(False)
        for w in (self.think_btn, self.preview, self.think, self.body):
            lay.addWidget(w)

    def _label(self) -> str:
        arrow = "▾" if self.think.isVisible() else "▸"
        if self._think_secs is not None:
            secs = self._think_secs
            return f"{arrow} Thought for " + (f"{secs:.0f} s" if secs >= 1 else "a moment")
        if self._think_start is not None:
            return f"{arrow} Thinking... {time.time() - self._think_start:.0f} s"
        return f"{arrow} Thinking"

    def _toggle(self):
        self.think.setVisible(not self.think.isVisible())
        self.think_btn.setText(self._label())
        self._dirty = True
        self._next = 0.0
        self.refresh()

    def add_text(self, s):
        if self._think_start is not None and self._think_secs is None and s.strip():
            self._think_secs = time.time() - self._think_start
        self.text += s
        self._dirty = True

    def add_reasoning(self, s):
        if self._think_start is None:
            self._think_start = time.time()
        self.reasoning += s
        self._dirty = True

    def finish(self):
        if self._think_start is not None and self._think_secs is None:
            self._think_secs = time.time() - self._think_start
        self._dirty = True
        self._next = 0.0
        self.refresh()

    def refresh(self):
        if self.reasoning and self._think_secs is None:
            self.think_btn.setText(self._label())      # the seconds keep ticking while it thinks
        if not self._dirty or time.time() < self._next:
            return
        t0 = time.time()
        self._dirty = False
        if self.reasoning:
            self.think_btn.setVisible(True)
            self.think_btn.setText(self._label())
            thinking = self._think_secs is None and not self.text.strip()
            last = self.reasoning.strip().rsplit("\n", 1)[-1]
            self.preview.setText(last[-160:])
            self.preview.setVisible(thinking and not self.think.isVisible())
            if self.think.isVisible():
                self.think.setText(self.reasoning[-20000:])
        if self.text.strip():
            self.body.setVisible(True)
            self.body.setText(self.text)
        # rendering a long Markdown reply takes a while; leave the window free in between
        self._next = time.time() + 3 * (time.time() - t0)

    def is_empty(self):
        return not self.text.strip() and not self.reasoning.strip()


class ToolCard(QFrame):
    def __init__(self, name: str, summary: str, args: dict):
        super().__init__()
        self.setObjectName("tool")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(10, 6, 10, 6)
        top = QHBoxLayout()
        self.icon = QLabel("⏳")
        self.title = QLabel(f"<b>{name}</b>  <span style='color:#9aa1ad'>{_esc(summary[:160])}</span>")
        self.title.setWordWrap(True)
        self.more = QToolButton(text="details")
        self.more.clicked.connect(self._toggle)
        top.addWidget(self.icon)
        top.addWidget(self.title, 1)
        top.addWidget(self.more)
        lay.addLayout(top)
        self.detail = QLabel(_esc(json.dumps(args, indent=1, ensure_ascii=False)[:3000]))
        self.detail.setFont(QFont("Consolas", 9))
        self.detail.setWordWrap(True)
        self.detail.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.detail.setVisible(False)
        self.detail.setTextFormat(Qt.RichText)
        lay.addWidget(self.detail)
        self.images = QHBoxLayout()
        lay.addLayout(self.images)

    def _toggle(self):
        self.detail.setVisible(not self.detail.isVisible())

    def done(self, result):
        self.icon.setText("✅" if result.ok else "⚠️")
        self.detail.setText(self.detail.text() + "<hr>" + _esc(result.text[:4000]).replace("\n", "<br>"))
        if not result.ok:
            self.detail.setVisible(True)
        for p in result.images[:3]:
            self.images.addWidget(thumb(p, 260))
        self.images.addStretch(1)


def _esc(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


class InputBox(QPlainTextEdit):
    send = Signal()
    image_pasted = Signal(bytes)

    def keyPressEvent(self, e):
        if e.key() in (Qt.Key_Return, Qt.Key_Enter) and not (e.modifiers() & Qt.ShiftModifier):
            self.send.emit()
            return
        super().keyPressEvent(e)

    def insertFromMimeData(self, source):
        if source.hasImage():
            img = QImage(source.imageData())
            if not img.isNull():
                self.image_pasted.emit(png_bytes(img))
                return
        super().insertFromMimeData(source)


# ====================================================================== agent <-> GUI
class Bridge(QObject):
    assistant_start = Signal()
    text = Signal(str)
    reasoning = Signal(str)
    assistant_end = Signal(object)
    tool_start = Signal(str, str, object, str)
    tool_end = Signal(str, str, object)
    status = Signal(str)
    notice = Signal(str)
    todos = Signal(object)
    usage = Signal(int, int, object)
    approval = Signal(object)
    ask = Signal(object)
    finished = Signal(str)
    failed = Signal(str)
    hide_window = Signal(object)


class Waiter:
    def __init__(self, payload):
        self.payload = payload
        self.event = threading.Event()
        self.result = None


class GuiUI(AgentUI):
    """Runs on the agent's thread; everything goes to the window through Qt signals."""

    def __init__(self, bridge: Bridge, stop: threading.Event, settings: Settings):
        self.b, self.stop, self.settings = bridge, stop, settings

    def assistant_start(self):
        self.b.assistant_start.emit()

    def text(self, s):
        self.b.text.emit(s)

    def reasoning(self, s):
        self.b.reasoning.emit(s)

    def assistant_end(self, turn):
        self.b.assistant_end.emit(turn)

    def tool_start(self, call_id, name, args, summary):
        tool = REGISTRY.get(name)
        if tool and tool.group == "control" and self.settings.extra.get("minimize_during_control", True):
            self._wait(self.b.hide_window, True)
        self.b.tool_start.emit(call_id, name, args, summary)

    def tool_end(self, call_id, name, result):
        self.b.tool_end.emit(call_id, name, result)

    def status(self, s):
        self.b.status.emit(s)

    def notice(self, s):
        self.b.notice.emit(s)

    def todos(self, t):
        self.b.todos.emit(t)

    def usage(self, used, limit, tps):
        self.b.usage.emit(used, limit, tps)

    def _wait(self, signal, payload):
        w = Waiter(payload)
        signal.emit(w)
        while not w.event.wait(0.1):
            if self.stop.is_set():
                return None
        return w.result

    def approve(self, req):
        d = self._wait(self.b.approval, req)
        tool = REGISTRY.get(req.tool)
        if d and d.allow and tool and tool.group == "control" and self.settings.extra.get("minimize_during_control", True):
            self._wait(self.b.hide_window, True)   # the dialog brought the window back; get out of the way again
        return d or Decision(False, note="Stopped by the user.")

    def ask(self, question, options):
        return self._wait(self.b.ask, (question, options)) or ""


class ApprovalDialog(QDialog):
    def __init__(self, req: ApprovalRequest, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Allow this action?")
        self.setMinimumWidth(560)
        self.decision = Decision(False)
        lay = QVBoxLayout(self)
        head = QLabel(f"The AI wants to {risk_text(req.risk)}.")
        head.setStyleSheet("font-size: 12pt; font-weight: 600;")
        head.setTextFormat(Qt.PlainText)
        head.setWordWrap(True)
        lay.addWidget(head)
        what = muted(req.summary)
        what.setTextFormat(Qt.PlainText)
        lay.addWidget(what)
        if req.dangerous:
            warn = QLabel("⚠️ This looks like a command that can delete data or change your system. "
                          "Read it carefully.")
            warn.setStyleSheet(f"color: {BAD}; font-weight: 600;")
            warn.setWordWrap(True)
            lay.addWidget(warn)
        box = QPlainTextEdit(req.pretty_args())
        box.setReadOnly(True)
        box.setFont(QFont("Consolas", 9))
        box.setMinimumHeight(140)
        lay.addWidget(box)
        self.note = QLineEdit()
        self.note.setPlaceholderText("Optional: if you deny, tell it what to do instead (Enter denies)")
        self.note.returnPressed.connect(lambda: self._done(False, False))
        lay.addWidget(self.note)
        row = QHBoxLayout()
        allow = QPushButton("Allow")
        allow.setObjectName("primary")
        allow.setAutoDefault(False)          # a stray Enter must never allow anything
        allow.clicked.connect(lambda: self._done(True, False))
        row.addWidget(allow)
        if not req.dangerous:
            always = QPushButton(f"Allow all '{RISK_LABELS.get(req.risk, req.risk)}' for this chat")
            always.setAutoDefault(False)
            always.clicked.connect(lambda: self._done(True, True))
            row.addWidget(always)
        row.addStretch(1)
        deny = QPushButton("Deny")
        deny.setDefault(True)
        deny.clicked.connect(lambda: self._done(False, False))
        row.addWidget(deny)
        lay.addLayout(row)
        deny.setFocus()

    def _done(self, allow, always):
        self.decision = Decision(allow, always, self.note.text().strip())
        self.accept()

    def reject(self):
        self.decision = Decision(False, note=self.note.text().strip())
        super().reject()


# ====================================================================== chat page
class ChatPage(QWidget):
    def __init__(self, win: "MainWindow"):
        super().__init__()
        self.setObjectName("page")
        self.win = win
        self.s = win.settings
        self.bridge = Bridge()
        self.stop = threading.Event()
        self.agent: Agent | None = None
        self._agent_sig = None
        self.thread: threading.Thread | None = None
        self.pending_images: list[bytes] = []
        self.current: AssistantBlock | None = None
        self.cards: dict[str, ToolCard] = {}
        self.chat_file: Path | None = None
        self._build()
        self._wire()
        self.timer = QTimer(self, interval=80, timeout=self._tick)
        self.timer.start()

    # ---------------------------------------------------------------- layout
    def _build(self):
        outer = QHBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        main = QVBoxLayout()
        main.setContentsMargins(16, 10, 16, 10)

        top = QHBoxLayout()
        self.dot = QLabel("●")
        self.dot.setStyleSheet(f"color: {BAD};")
        top.addWidget(self.dot)
        self.conn = ElidedLabel("not connected")
        top.addWidget(self.conn, 1)
        self.model_btn = QToolButton(text="Model ▾")
        self.model_btn.setObjectName("menu")
        self.model_btn.setToolTip("Switch model, or start / stop it")
        self.model_btn.setPopupMode(QToolButton.InstantPopup)
        self.model_menu = QMenu(self.model_btn)
        self.model_menu.aboutToShow.connect(self._fill_model_menu)
        self.model_btn.setMenu(self.model_menu)
        top.addWidget(self.model_btn)
        top.addSpacing(8)
        self.mode = QComboBox()
        for k, v in PERMISSION_SHORT.items():
            self.mode.addItem(v, k)
            self.mode.setItemData(self.mode.count() - 1, APPROVAL_MODES[k], Qt.ToolTipRole)
        self.mode.setToolTip("What it may do without asking you first")
        self.mode.setCurrentIndex(max(0, self.mode.findData(self.s.approval_mode)))
        self.mode.currentIndexChanged.connect(self._mode_changed)
        top.addWidget(self.mode)
        self.think = QComboBox()
        for k, v in THINKING_LEVELS:
            self.think.addItem(f"Thinking: {v}", k)
        self.think.setToolTip("How long it thinks before answering. Applies from your next message. "
                              "High is best for hard tasks, Off or Low for quick clicks.")
        self.sync_thinking()
        self.think.currentIndexChanged.connect(self._thinking_changed)
        top.addWidget(self.think)
        b = QPushButton("New chat")
        b.clicked.connect(self.new_chat)
        top.addWidget(b)
        more = QToolButton(text="⋯")
        more.setObjectName("menu")
        more.setToolTip("Old chats, work folder")
        more.setPopupMode(QToolButton.InstantPopup)
        m = QMenu(more)
        m.addAction("Open an old chat...", self._open_chat)
        m.addAction("Change the work folder...", self._pick_workspace)
        more.setMenu(m)
        top.addWidget(more)
        main.addLayout(top)

        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        holder = QWidget()
        holder.setObjectName("page")
        self.feed = QVBoxLayout(holder)
        self.feed.setSpacing(8)
        self.feed.addStretch(1)
        self.scroll.setWidget(holder)
        self.scroll.verticalScrollBar().rangeChanged.connect(self._autoscroll)
        main.addWidget(self.scroll, 1)

        self.welcome = QWidget()
        wl = QVBoxLayout(self.welcome)
        wl.setContentsMargins(0, 0, 0, 0)
        wl.addWidget(muted("Ask for anything: \"tidy up my Downloads folder\", \"what's on my screen?\", "
                           "\"write a script that renames these photos by date\", \"open Notepad and write a "
                           "poem\". It asks before changing anything (you can loosen that at the top).\n\n"
                           "Emergency stop while it uses the mouse: press Stop (or Esc), or slam the mouse into "
                           "a screen corner."))
        # first run: one click to a working model
        self.setup_card = QGroupBox("No model running yet. Pick one to get started:")
        sc = QVBoxLayout(self.setup_card)
        for p in catalog.PRESETS:
            gb = p.total_bytes() / catalog.GB
            b = QPushButton(f"{p.title}  ({gb:.0f} GB download if you don't have it)")
            b.setStyleSheet("text-align: left; padding: 8px 14px;")
            b.clicked.connect(lambda _=False, k=p.key: self.win.server.switch_to(k))
            sc.addWidget(b)
        b = QPushButton("I already run LM Studio or Ollama")
        b.setStyleSheet("text-align: left; padding: 8px 14px;")
        b.clicked.connect(lambda: self.win.nav.setCurrentRow(2))
        sc.addWidget(b)
        self.setup_card.setVisible(False)
        wl.addWidget(self.setup_card)
        self.feed.insertWidget(0, self.welcome)

        self.chips = QHBoxLayout()
        main.addLayout(self.chips)
        row = QHBoxLayout()
        self.input = InputBox()
        self.input.setPlaceholderText("Message (Enter to send, Shift+Enter for a new line, paste images with Ctrl+V)")
        self.input.setFixedHeight(84)
        row.addWidget(self.input, 1)
        col = QVBoxLayout()
        self.send_btn = QPushButton("Send")
        self.send_btn.setObjectName("primary")
        self.stop_btn = QPushButton("Stop")
        self.stop_btn.setObjectName("danger")
        self.stop_btn.setEnabled(False)
        col.addWidget(self.send_btn)
        col.addWidget(self.stop_btn)
        row.addLayout(col)
        main.addLayout(row)
        tools = QHBoxLayout()
        b = QPushButton("Attach image")
        b.clicked.connect(self._attach)
        tools.addWidget(b)
        b = QPushButton("Add screenshot")
        b.clicked.connect(self._add_screenshot)
        tools.addWidget(b)
        tools.addStretch(1)
        self.status = muted("")
        tools.addWidget(self.status)
        main.addLayout(tools)
        outer.addLayout(main, 1)

        side = QFrame()
        side.setObjectName("side")
        side.setFixedWidth(260)
        sl = QVBoxLayout(side)
        sl.addWidget(QLabel("<b>Plan</b>"))
        self.todo = muted("No plan yet. It makes one for bigger tasks.")
        self.todo.setTextFormat(Qt.RichText)
        sl.addWidget(self.todo)
        sl.addSpacing(12)
        sl.addWidget(QLabel("<b>Context used</b>"))
        self.ctx_bar = QProgressBar()
        self.ctx_bar.setFormat("%p%  (%v / %m tokens)")
        sl.addWidget(self.ctx_bar)
        self.ctx_hint = muted("")
        sl.addWidget(self.ctx_hint)
        self.speed = muted("")
        sl.addWidget(self.speed)
        sl.addSpacing(12)
        sl.addWidget(QLabel("<b>Work folder</b>"))
        row = QHBoxLayout()
        self.ws_label = ElidedLabel("")
        self.ws_label.setProperty("muted", True)
        row.addWidget(self.ws_label, 1)
        self.ws_btn = QToolButton(text="Change")
        self.ws_btn.clicked.connect(self._pick_workspace)
        row.addWidget(self.ws_btn)
        sl.addLayout(row)
        sl.addStretch(1)
        outer.addWidget(side)
        self._update_ws_btn()

    def _wire(self):
        b = self.bridge
        b.assistant_start.connect(self._on_assistant_start)
        b.text.connect(lambda s: self.current and self.current.add_text(s))
        b.reasoning.connect(lambda s: self.current and self.current.add_reasoning(s))
        b.assistant_end.connect(self._on_assistant_end)
        b.tool_start.connect(self._on_tool_start)
        b.tool_end.connect(self._on_tool_end)
        b.status.connect(self.status.setText)
        b.notice.connect(lambda s: self._add(muted("ℹ️ " + s)))
        b.todos.connect(self._on_todos)
        b.usage.connect(self._on_usage)
        b.approval.connect(self._on_approval)
        b.ask.connect(self._on_ask)
        b.finished.connect(self._on_finished)
        b.failed.connect(self._on_failed)
        b.hide_window.connect(self._on_hide)
        self.input.send.connect(self.send)
        self.input.image_pasted.connect(self._add_pending)
        self.send_btn.clicked.connect(self.send)
        self.stop_btn.clicked.connect(self.request_stop)
        QShortcut(QKeySequence("Escape"), self, activated=self.request_stop)

    # ---------------------------------------------------------------- helpers
    def _add(self, w: QWidget):
        self.welcome.setVisible(False)
        self.feed.insertWidget(self.feed.count() - 1, w)

    def _autoscroll(self, _min, maximum):
        bar = self.scroll.verticalScrollBar()
        if maximum - bar.value() < 400:
            bar.setValue(maximum)

    def _tick(self):
        if self.current:
            self.current.refresh()

    def _update_ws_btn(self):
        self.ws_label.setText(str(self.s.workspace_path()))
        self.ws_label.setToolTip(str(self.s.workspace_path()))

    def _fill_model_menu(self):
        """Switch between the ready-made modes, or start / stop the model, without leaving the chat."""
        m, srv = self.model_menu, self.win.server
        m.clear()
        current = catalog.matching(self.s.model_path)
        for p in catalog.PRESETS:
            a = m.addAction(p.title, lambda k=p.key: srv.switch_to(k))
            a.setCheckable(True)
            a.setChecked(p is current)
        m.addSeparator()
        if srv.proc.running:
            m.addAction("Stop the model", srv.stop)
        elif self.s.model_path:
            m.addAction(f"Start {Path(self.s.model_path).name}", srv.start)
        m.addAction("Model server page...", lambda: self.win.nav.setCurrentRow(1))

    def sync_thinking(self):
        self.think.blockSignals(True)
        self.think.setCurrentIndex(max(0, self.think.findData("high" if self.s.thinking == "on" else self.s.thinking)))
        self.think.blockSignals(False)

    def _thinking_changed(self):
        self.s.thinking = self.think.currentData()
        self.s.save()
        if self.agent and self.agent.messages:
            # the level sits at the very start of the prompt, so the server can't reuse what it already read
            self.status.setText("Thinking changed: the next reply re-reads this chat once, so it starts slower.")

    def _mode_changed(self):
        self.s.approval_mode = self.mode.currentData()
        self.s.save()
        if self.agent:
            self.agent.policy.mode = self.s.approval_mode

    def _pick_workspace(self):
        if self.busy():
            QMessageBox.information(self, "Work folder", "Wait for the current task to finish (or press Stop) "
                                    "before changing the work folder.")
            return
        d = QFileDialog.getExistingDirectory(self, "Folder the AI works in", str(self.s.workspace_path()))
        if d:
            self.s.workspace = str(Path(d))
            self.s.save()
            self._update_ws_btn()

    def _attach(self):
        paths, _ = QFileDialog.getOpenFileNames(self, "Images", "", "Images (*.png *.jpg *.jpeg *.bmp *.webp *.gif)")
        for p in paths:
            img = QImage(p)
            if not img.isNull():
                self._add_pending(png_bytes(img))

    def _add_screenshot(self):
        from .tools import ToolContext
        from .tools.screen import _shot

        self.win.showMinimized()
        QApplication.processEvents()
        time.sleep(0.4)
        try:
            png = _shot(ToolContext(settings=self.s, workspace=self.s.workspace_path()))
        except Exception as e:
            QMessageBox.warning(self, "Screenshot", f"Couldn't take a screenshot: {e}")
            return
        finally:
            self.win.showNormal()
            self.win.activateWindow()
        self._add_pending(png)

    def _add_pending(self, png: bytes):
        self.pending_images.append(png)
        lab = thumb(png, 90)
        lab.setToolTip("Will be sent with your next message")
        self.chips.addWidget(lab)

    def _clear_pending(self):
        self.pending_images = []
        while self.chips.count():
            w = self.chips.takeAt(0).widget()
            if w:
                w.deleteLater()

    # ---------------------------------------------------------------- agent
    def _ensure_agent(self):
        s = self.s
        sig = (s.base_url, s.model, s.api_key, s.workspace, s.tool_mode, s.enable_shell, s.enable_control,
               s.enable_web, s.enable_subagents, s.enable_github, s.enable_code, s.enable_unity, s.github_token, s.coord_mode,
               s.shell)
        if self.agent is None or sig != self._agent_sig:
            prev = self.agent
            self.agent = Agent(s, GuiUI(self.bridge, self.stop, s), stop=self.stop)
            if prev is not None:
                self.agent.messages, self.agent.todos = prev.messages, prev.todos
                if prev.ctx.coding and sig[3] == (self._agent_sig or sig)[3]:
                    # mid coding task: stay in its private copy
                    self.agent.ctx.coding.update(prev.ctx.coding)
                    self.agent.set_workspace(prev.workspace)
            self._agent_sig = sig
        pol = self.agent.policy   # safety switches apply straight away, even mid-chat
        pol.mode, pol.always_ask_dangerous, pol.always_ask_publish = (
            s.approval_mode, s.always_ask_dangerous, s.always_ask_publish)

    def send(self):
        text = self.input.toPlainText().strip()
        if (not text and not self.pending_images) or self.busy():
            return
        self._ensure_agent()
        images = self.pending_images
        self._add(UserBubble(text, images))
        self.input.clear()
        self._clear_pending()
        if self.chat_file is None:
            self.chat_file = DATA_DIR / "chats" / (time.strftime("%Y-%m-%d_%H-%M-%S") + ".json")
        self.stop.clear()
        self.send_btn.setEnabled(False)
        self.stop_btn.setEnabled(True)

        def work():
            try:
                final = self.agent.run(text, images)
                self.bridge.finished.emit(final)
            except ServerError as e:
                self.bridge.failed.emit(str(e))
            except Exception as e:  # noqa: BLE001
                import traceback

                self.bridge.failed.emit(f"{type(e).__name__}: {e}\n\n{traceback.format_exc()[-2000:]}")

        self.thread = threading.Thread(target=work, daemon=True)
        self.thread.start()

    def busy(self) -> bool:
        return self.thread is not None and self.thread.is_alive()

    def request_stop(self):
        if self.busy():
            self.stop.set()
            self.status.setText("Stopping...")

    def new_chat(self):
        if self.busy():
            return
        if self.agent:
            _stop_background(self.agent)
            self.agent.reset()
        self.chat_file = None
        while self.feed.count() > 1:
            w = self.feed.takeAt(0).widget()
            if w and w is not self.welcome:
                w.deleteLater()
        self.feed.insertWidget(0, self.welcome)
        self.welcome.setVisible(True)
        self._on_todos([])
        self.ctx_bar.setValue(0)

    def _open_chat(self):
        if self.busy():
            return
        files = sorted((DATA_DIR / "chats").glob("*.json"), reverse=True)[:100]
        if not files:
            QMessageBox.information(self, "Old chats", "No saved chats yet.")
            return
        labels = []
        for f in files:
            try:
                msgs = json.loads(f.read_text(encoding="utf-8"))["messages"]
                first = next((m["content"] for m in msgs if m["role"] == "user" and isinstance(m["content"], str)), "")
            except (OSError, ValueError, KeyError):
                first = ""
            labels.append(f"{f.stem}   {first[:60]}")
        label, ok = QInputDialog.getItem(self, "Old chats", "Open which chat?", labels, 0, False)
        if not ok:
            return
        f = files[labels.index(label)]
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:     # leave the open chat alone
            QMessageBox.warning(self, "Old chats", f"Couldn't open that chat: {e}")
            return
        self.new_chat()
        self._ensure_agent()
        self.agent.load_json(data)
        self.chat_file = f
        self._render_history()

    def _render_history(self):
        from .tools import ToolResult

        for m in self.agent.messages:
            c = m.get("content")
            text = c if isinstance(c, str) else " ".join(p.get("text", "") for p in c or [] if p.get("type") == "text")
            text = m.get("_shown", text)
            if m["role"] == "user" and "_kind" not in m:
                self._add(UserBubble(text, []))
            elif m["role"] == "assistant" and text.strip():
                blk = AssistantBlock()
                blk.add_text(text)
                blk.refresh()
                self._add(blk)
            elif m["role"] == "tool" and m.get("name") != "todo_write":
                card = ToolCard(m.get("name", "tool"), "", {})
                card.done(ToolResult(text))
                self._add(card)
            elif m.get("_kind") == "summary":
                self._add(muted("ℹ️ Earlier messages were summarized."))

    # ---------------------------------------------------------------- signal handlers
    def _on_assistant_start(self):
        self.current = AssistantBlock()
        self._add(self.current)

    def _on_assistant_end(self, turn):
        if self.current:
            self.current.finish()
            if self.current.is_empty():
                self.current.setVisible(False)
        self.current = None

    def _on_tool_start(self, call_id, name, args, summary):
        if name == "todo_write":       # the plan already shows on the right
            return
        card = ToolCard(name, summary, args)
        self.cards[call_id] = card
        self._add(card)

    def _on_tool_end(self, call_id, name, result):
        card = self.cards.pop(call_id, None)
        if card:
            card.done(result)

    def _on_todos(self, todos):
        if not todos:
            self.todo.setText("No plan yet. It makes one for bigger tasks.")
            return
        marks = {"pending": "○", "in_progress": f"<span style='color:{ACCENT}'>◉</span>",
                 "done": f"<span style='color:{OK}'>✓</span>"}
        self.todo.setText("<br>".join(f"{marks.get(t['status'], '○')} {_esc(t['content'])}" for t in todos))

    def _on_usage(self, used, limit, tps):
        self.ctx_bar.setMaximum(max(1, limit))
        self.ctx_bar.setValue(min(used, limit))
        self.ctx_hint.setText("Nearly full: older steps get summarized soon. For a new task, press New chat."
                              if limit and used / limit > 0.8 else "")
        if tps:
            self.speed.setText(f"{tps:.1f} tokens/second")
            self.win.last_tps = tps

    def _on_hide(self, w: Waiter):
        if self.win.isMinimized():
            w.event.set()
            return
        self.win.showMinimized()
        QTimer.singleShot(450, w.event.set)

    def _restore(self):
        if self.win.isMinimized():
            self.win.showNormal()
            self.win.raise_()
            self.win.activateWindow()

    def _on_approval(self, w: Waiter):
        self._restore()
        dlg = ApprovalDialog(w.payload, self)
        dlg.exec()
        w.result = dlg.decision
        w.event.set()

    def _on_ask(self, w: Waiter):
        self._restore()
        question, options = w.payload
        if options:
            ans, ok = QInputDialog.getItem(self, "The AI is asking", question, options + ["(type my own answer)"], 0, False)
            if ok and ans == "(type my own answer)":
                ans, ok = QInputDialog.getMultiLineText(self, "The AI is asking", question)
        else:
            ans, ok = QInputDialog.getMultiLineText(self, "The AI is asking", question)
        w.result = ans if ok else ""
        w.event.set()

    def _done_common(self):
        self.send_btn.setEnabled(True)
        self.stop_btn.setEnabled(False)
        self.status.setText("")
        self._restore()
        self.current = None
        if self.agent and self.chat_file:
            try:
                self.agent.save(self.chat_file)
            except OSError:
                pass

    def _on_finished(self, final):
        self._done_common()

    def _on_failed(self, msg):
        self._done_common()
        lab = QLabel(f"<span style='color:{BAD}'>⚠️ {_esc(msg)}</span>".replace("\n", "<br>"))
        lab.setWordWrap(True)
        lab.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self._add(lab)
        self.win.refresh_connection()

    def set_connection(self, state: str, text: str, first_run: bool = False, tip: str = ""):
        """state: "ok" (a model answers), "busy" (ours is loading) or "off"."""
        self.dot.setStyleSheet(f"color: {dict(ok=OK, busy=WARN).get(state, BAD)};")
        self.conn.setText(text)
        self.conn.setToolTip(tip or text)
        self.setup_card.setVisible(first_run)
        self._update_ws_btn()


# ====================================================================== model server page
CTX_CHOICES = [8192, 16384, 32768, 49152, 65536, 131072, 262144]
SPEEDUPS = [("off", "Off"), ("mtp", "MTP: built-in draft layers (about 1.3-2x faster)"),
            ("ngram", "N-gram: reuse repeated text (great for code edits)"),
            ("draft", "Draft model: a small model guesses ahead")]


class ServerPage(QWidget):
    def __init__(self, win: "MainWindow"):
        super().__init__()
        self.setObjectName("page")
        self.win = win
        self.s = win.settings
        self.proc = launcher.ServerProcess()
        self.info = None
        self.exe = None
        self.launch = None
        self.vram = 16.0
        self._was_ready = False
        self._phase = None             # what a Strata start is doing, set from its worker thread
        self._strata_job: threading.Thread | None = None
        self._located: dict = {}       # preset key -> catalog.model_path(), from the last folder scan
        self._last_model = self.s.model_path    # what autostart may start: the model used last time
        self._scanning: threading.Thread | None = None
        self._ready_job = None         # (thread, result) of the /health check in flight
        self._startable = False
        self._log_sig = None
        self._build()
        self.timer = QTimer(self, interval=500, timeout=self._poll)
        self.timer.start()
        QTimer.singleShot(100, self._initial_scan)

    def _build(self):
        area = QScrollArea()
        area.setWidgetResizable(True)
        body = QWidget()
        body.setObjectName("page")
        lay = QVBoxLayout(body)
        lay.setContentsMargins(20, 14, 20, 14)
        h = QLabel("Model server")
        h.setObjectName("h1")
        lay.addWidget(h)
        lay.addWidget(muted("Pick a mode and press the button: it downloads what's missing and starts the model with "
                            "settings worked out for your card. Already use LM Studio or Ollama? Pick it under "
                            "Settings > Connection instead."))
        self.gpu = muted("")
        lay.addWidget(self.gpu)

        # ---- the mode: what most people need
        g = QGroupBox("Mode")
        f = QFormLayout(g)
        row = QHBoxLayout()
        self.preset_box = QComboBox()
        for p in catalog.PRESETS:
            self.preset_box.addItem(p.title, p.key)
        self.preset_box.addItem("Another model file (pick it below)", "")
        self.preset_box.currentIndexChanged.connect(self._preset_picked)
        row.addWidget(self.preset_box, 1)
        self.preset_btn = QPushButton("Use it")
        self.preset_btn.setObjectName("primary")
        self.preset_btn.clicked.connect(self._use_preset)
        row.addWidget(self.preset_btn)
        f.addRow(row)
        self.preset_info = muted("")
        f.addRow(self.preset_info)
        self.strata_box = QCheckBox("Run it with Strata, an engine made for this model (recommended: about 2-4x "
                                    "faster than llama.cpp here)")
        self.strata_box.setToolTip("Strata (github.com/Niko1221/Strata) keeps the busiest experts on the GPU and "
                                   "adds a draft layer. The first start downloads it and runs its setup once.")
        self.strata_box.setVisible(False)
        self.strata_box.toggled.connect(self._engine_toggled)
        f.addRow(self.strata_box)
        self.preset_bar = QProgressBar()
        self.preset_bar.setRange(0, 1000)
        self.preset_bar.setVisible(False)
        f.addRow(self.preset_bar)
        self._dl_state = None
        row = QHBoxLayout()
        self.ctx = QComboBox()
        for n in CTX_CHOICES:
            self.ctx.addItem(f"{n // 1024}K tokens", n)
        self.ctx.setToolTip("How much text the model can see at once. More needs more memory.")
        row.addWidget(QLabel("Context:"))
        row.addWidget(self.ctx)
        row.addSpacing(16)
        self.vision = QCheckBox("Let it see images and the screen")
        self.vision.setToolTip("Loads the model's vision file (mmproj)")
        row.addWidget(self.vision)
        row.addStretch(1)
        f.addRow(row)
        lay.addWidget(g)

        # ---- start / stop and what's happening
        row = QHBoxLayout()
        self.start_btn = QPushButton("Start model")
        self.start_btn.setObjectName("primary")
        self.start_btn.setMinimumWidth(130)
        self.start_btn.clicked.connect(self._start_stop)
        row.addWidget(self.start_btn)
        self.state = QLabel("Stopped")
        self.state.setWordWrap(True)
        row.addWidget(self.state, 1)
        self.log_btn = QToolButton(text="▸ Show log")
        self.log_btn.clicked.connect(lambda: self._show_log(not self.log.isVisible()))
        row.addWidget(self.log_btn)
        lay.addLayout(row)
        self.plan_label = QLabel()
        self.plan_label.setWordWrap(True)
        lay.addWidget(self.plan_label)
        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setFont(QFont("Consolas", 8))
        self.log.setMinimumHeight(220)
        self.log.setVisible(False)
        lay.addWidget(self.log)

        # ---- a model file of your own (shown when no ready-made mode is picked)
        self.file_group = g = QGroupBox("Model file")
        f = QFormLayout(g)
        row = QHBoxLayout()
        self.dir_label = ElidedLabel()
        row.addWidget(self.dir_label, 1)
        b = QPushButton("Models folder...")
        b.clicked.connect(self._browse_dir)
        row.addWidget(b)
        f.addRow("Looking in:", row)
        row = QHBoxLayout()
        self.model_box = QComboBox()
        self.model_box.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon)
        self.model_box.currentIndexChanged.connect(self._model_changed)
        row.addWidget(self.model_box, 1)
        b = QPushButton("Pick file...")
        b.clicked.connect(self._browse_model)
        row.addWidget(b)
        f.addRow("Model:", row)
        self.info_label = muted("")
        f.addRow(self.info_label)
        row = QHBoxLayout()
        self.vision_btn = QPushButton("Find vision file online")
        self.vision_btn.setToolTip("Searches Hugging Face for the image adapter (mmproj) that matches this model "
                                   "and downloads it next to the model")
        self.vision_btn.clicked.connect(self._find_vision)
        row.addWidget(self.vision_btn)
        self.vision_status = muted("")
        row.addWidget(self.vision_status, 1)
        f.addRow(row)
        lay.addWidget(g)

        # ---- everything else, folded away
        self.adv_btn = QToolButton(text="▸ Advanced: llama.cpp program, memory split, speed-up, port")
        self.adv_btn.clicked.connect(lambda: self._show_advanced(not self.adv.isVisible()))
        lay.addWidget(self.adv_btn)
        self.adv = QWidget()
        al = QVBoxLayout(self.adv)
        al.setContentsMargins(0, 0, 0, 0)
        g = QGroupBox("llama.cpp")
        f = QFormLayout(g)
        self.exe_label = QLabel()
        self.exe_label.setWordWrap(True)
        f.addRow("Program:", self.exe_label)
        row = QHBoxLayout()
        self.dl_btn = QPushButton("Download / update llama.cpp")
        self.dl_btn.clicked.connect(self._download)
        row.addWidget(self.dl_btn)
        b = QPushButton("Browse...")
        b.clicked.connect(self._browse_exe)
        row.addWidget(b)
        row.addStretch(1)
        f.addRow(row)
        self.dl_bar = QProgressBar()
        self.dl_bar.setVisible(False)
        f.addRow(self.dl_bar)
        al.addWidget(g)

        g = QGroupBox("Settings (the defaults are tuned for an RTX 5080 16 GB)")
        f = QFormLayout(g)
        self.kv = QComboBox()
        for k, label in [("f16", "f16 (most memory)"), ("q8_0", "q8_0 (recommended)"), ("q4_0", "q4_0 (least memory, a bit worse)")]:
            self.kv.addItem(label, k)
        f.addRow("Context memory (KV cache):", self.kv)
        self.placement = QComboBox()
        self.placement.addItem("Automatic (recommended)", "auto")
        self.placement.addItem("Manual", "manual")
        f.addRow("GPU / RAM split:", self.placement)
        row = QHBoxLayout()
        self.ngl = QSpinBox(maximum=999)
        self.ncm = QSpinBox(maximum=200)
        row.addWidget(QLabel("GPU layers"))
        row.addWidget(self.ngl)
        row.addWidget(QLabel("MoE layers with experts in RAM"))
        row.addWidget(self.ncm)
        row.addStretch(1)
        f.addRow(row)
        self.speed = QComboBox()
        for k, label in SPEEDUPS:
            self.speed.addItem(label, k)
        f.addRow("Speed-up:", self.speed)
        row = QHBoxLayout()
        self.draft_label = QLabel()
        row.addWidget(self.draft_label, 1)
        b = QPushButton("Draft model...")
        b.clicked.connect(self._browse_draft)
        row.addWidget(b)
        f.addRow(row)
        self.port = QSpinBox(minimum=1024, maximum=65535)
        f.addRow("Port:", self.port)
        self.extra = QLineEdit()
        self.extra.setPlaceholderText("Extra llama-server options (optional)")
        f.addRow("Extra:", self.extra)
        self.autostart = QCheckBox("Start the last model when the app opens")
        f.addRow(self.autostart)
        al.addWidget(g)
        al.addWidget(muted("The command it runs:"))
        self.cmd = QPlainTextEdit()
        self.cmd.setReadOnly(True)
        self.cmd.setFixedHeight(70)
        self.cmd.setFont(QFont("Consolas", 9))
        al.addWidget(self.cmd)
        self.adv.setVisible(False)
        lay.addWidget(self.adv)
        lay.addStretch(1)
        area.setWidget(body)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(area)

        # load settings into the controls, then react to changes
        s = self.s
        self._sync_controls()
        self.ngl.setValue(s.gpu_layers)
        self.ncm.setValue(s.n_cpu_moe)
        self.port.setValue(s.port)
        self.extra.setText(s.extra_args)
        self.autostart.setChecked(s.extra.get("autostart_model", True))
        for w in (self.ctx, self.kv, self.placement, self.speed):
            w.currentIndexChanged.connect(self._changed)
        for w in (self.ngl, self.ncm, self.port):
            w.valueChanged.connect(self._changed)
        self.vision.toggled.connect(self._changed)
        self.autostart.toggled.connect(self._changed)
        self.extra.editingFinished.connect(self._changed)

    def _sync_controls(self):
        """Settings -> the combo boxes, without firing their change handlers."""
        s = self.s
        boxes = (self.ctx, self.kv, self.placement, self.speed, self.vision)
        for w in boxes:
            w.blockSignals(True)
        if self.ctx.findData(s.ctx_size) < 0:     # a size from settings.json that isn't in the list
            self.ctx.addItem(f"{s.ctx_size // 1024}K tokens", s.ctx_size)
        self.ctx.setCurrentIndex(self.ctx.findData(s.ctx_size))
        self.kv.setCurrentIndex(max(0, self.kv.findData(s.kv_type)))
        self.placement.setCurrentIndex(max(0, self.placement.findData(s.placement)))
        self.speed.setCurrentIndex(max(0, self.speed.findData(s.speedup)))
        self.vision.setChecked(s.use_vision)
        for w in boxes:
            w.blockSignals(False)

    def _show_advanced(self, on: bool):
        self.adv.setVisible(on)
        self.adv_btn.setText(("▾" if on else "▸") + self.adv_btn.text()[1:])

    def _show_log(self, on: bool):
        self.log.setVisible(on)
        self.log_btn.setText(("▾ Hide log" if on else "▸ Show log"))

    def _start_stop(self):
        if self.proc.running or self._phase is not None:
            self.stop()
        else:
            self.start()

    def switch_to(self, key: str):
        """From the chat: pick a ready-made mode and use it (downloading what's missing)."""
        self.win.nav.setCurrentRow(1)
        self.preset_box.setCurrentIndex(max(0, self.preset_box.findData(key)))
        self._use_preset()

    # ---------------------------------------------------------------- scanning
    def _initial_scan(self):
        self._scan(first=True)

    def _refresh_exe(self):
        self._show_exe(launcher.find_server(self.s))

    def _show_exe(self, exe):
        self.exe = exe
        self.exe_label.setText(str(exe) if exe else "<span style='color:#e0a43a'>Not found. Press Download (or "
                               "just Start: it offers to download it).</span>")

    def _dirs(self) -> list[Path]:
        dirs = [Path(self.s.models_dir)] if self.s.models_dir else []
        return dirs + launcher.default_model_dirs()

    def _scan(self, first: bool = False):
        """Look for model files (and, the first time, the GPU and llama.cpp) on a worker thread: walking
        Downloads or asking nvidia-smi can take seconds, and the window must stay responsive."""
        dirs = self._dirs()
        self.dir_label.setText(", ".join(str(d) for d in dirs[:3]) or "(no folders yet: pick your models folder)")
        s, root = self.s, self._models_root()
        out = {}

        def work():
            if first:
                out["gpu"] = gpu_info()
                exe = launcher.find_server(s)
                if exe:
                    launcher.server_help(exe)        # cached for the first plan
                out["exe"] = exe
            out["models"] = launcher.find_models(dirs)
            out["located"] = {p.key: catalog.model_path(p, dirs + [root]) for p in catalog.PRESETS}

        t = threading.Thread(target=work, daemon=True)
        t.start()
        self._scanning = t

        def done():
            if t.is_alive():
                QTimer.singleShot(50, done)
                return
            if self._scanning is t:
                self._apply_scan(out, first)

        done()

    def _apply_scan(self, out: dict, first: bool):
        if first:
            g = out.get("gpu")
            if g:
                self.vram = g[1]
                self.gpu.setText(f"GPU: {g[0]}, {g[1]:.1f} GB ({g[2]:.1f} GB in use right now)")
            else:
                self.gpu.setText("No NVIDIA GPU found by nvidia-smi; planning for 16 GB.")
            self._show_exe(out.get("exe"))
        self._located = out.get("located") or {}
        models = list(out.get("models") or [])
        saved = Path(self.s.model_path) if self.s.model_path else None
        if saved and saved not in models:
            models.insert(0, saved)      # kept even when its drive is unplugged: never swap it for another
        self.model_box.blockSignals(True)
        self.model_box.clear()
        for m in models:
            self.model_box.addItem(m.name + ("" if m.exists() else "  (not found)"), str(m))
        idx = next((i for i, m in enumerate(models) if saved and m == saved), 0)
        self.model_box.setCurrentIndex(idx)
        self.model_box.blockSignals(False)
        if not models:
            self.info_label.setText("No .gguf models found. Pick your models folder, or a file, or a mode above.")
        self._model_changed()
        p = catalog.matching(self.s.model_path)
        if self._dl_state is None:
            self.preset_box.blockSignals(True)
            self.preset_box.setCurrentIndex(max(0, self.preset_box.findData(p.key if p else "")))
            self.preset_box.blockSignals(False)
            self._preset_picked()
        if first:
            self._autostart()

    def _autostart(self):
        """Start the model used last time, unless something already serves that port."""
        s = self.s
        p = catalog.matching(s.model_path)
        if not (s.extra.get("autostart_model", True) and s.model_path and Path(s.model_path).exists()) \
                or self.proc.running or s.model_path != self._last_model:
            return      # (a model the folder scan just picked on a first run isn't started unasked)
        if strata.active(s, p):
            if strata.needs_setup(p, Path(s.model_path), s):
                return              # the first Strata setup asks first; it isn't started unasked
        elif not self.exe:
            return
        port = s.port
        result = {}
        t = threading.Thread(target=lambda: result.update(busy=launcher.ServerProcess().ready(port)), daemon=True)
        t.start()

        def go():
            if t.is_alive():
                QTimer.singleShot(100, go)
            elif not result.get("busy") and not self.proc.running:
                self.start()

        go()

    def _browse_dir(self):
        d = QFileDialog.getExistingDirectory(self, "Folder with your .gguf models", self.s.models_dir or str(Path.home()))
        if d:
            self.s.models_dir = str(Path(d))
            self.s.save()
            self._scan()

    def _browse_model(self):
        f, _ = QFileDialog.getOpenFileName(self, "Model file", self.s.models_dir or str(Path.home()), "GGUF models (*.gguf)")
        if f:
            self.s.model_path = str(Path(f))     # the dialog says C:/..., the folder scan C:\...
            self.s.mmproj_path = ""
            self.s.save()
            self._scan()

    def _browse_exe(self):
        f, _ = QFileDialog.getOpenFileName(self, "llama-server program", str(Path.home()), "llama-server (llama-server*)")
        if f:
            self.s.server_exe = str(Path(f))
            self.s.save()
            self._refresh_exe()
            self._changed()

    def _browse_draft(self):
        f, _ = QFileDialog.getOpenFileName(self, "Small draft model (same family)", self.s.models_dir or str(Path.home()),
                                           "GGUF models (*.gguf)")
        if f:
            self.s.draft_path = str(Path(f))
            self.s.save()
            self._changed()

    # ---------------------------------------------------------------- ready-made modes
    def _models_root(self) -> Path:
        return Path(self.s.models_dir) if self.s.models_dir else APP_DIR / "models"

    def _preset(self):
        return catalog.BY_KEY.get(self.preset_box.currentData() or "")

    def _locate(self, p):
        """(model, vision file, missing names) for a mode: from the last folder scan while its files are
        still there (searching the folders again can take seconds), else searched now."""
        got = self._located.get(p.key)
        if got is not None:
            model, mm, _missing = got
            if all(f is None or f.is_file() for f in (model, mm)):
                return got
        got = self._located[p.key] = catalog.model_path(p, self._dirs() + [self._models_root()])
        return got

    def _preset_picked(self):
        p = self._preset()
        if self._dl_state is not None:
            return
        active = p is not None and catalog.matching(self.s.model_path) is p
        self.file_group.setVisible(p is None)
        self.strata_box.setVisible(strata.supports(p))
        if strata.supports(p):
            self.strata_box.blockSignals(True)
            self.strata_box.setChecked(strata.active(self.s, p) if active else p.launch.get("engine") == "strata")
            self.strata_box.blockSignals(False)
        if not p:
            self.preset_info.setText("Pick the model file below, then press Start model.")
            self.preset_btn.setVisible(False)
            return
        model, mm, missing = self._locate(p)
        self.preset_btn.setVisible(bool(missing) or not active)    # else Start model below does it
        if not missing:
            where = f"On this PC in {model.parent}."
            self.preset_btn.setText("Switch to it")
        else:
            gb = sum(p.sizes.get(f, 0) for f in missing) / catalog.GB
            dest = model.parent if model else self._models_root() / p.folder
            where = f"Needs a {gb:.1f} GB download into {dest}."
            self.preset_btn.setText(f"Download ({gb:.0f} GB) and use it" if gb >= 1 else "Download and use it")
        self.preset_info.setText(f"{p.blurb} {where}")

    def _use_preset(self):
        st = self._dl_state
        if st is not None:          # the button pauses a running download
            st["stop"].set()
            return
        p = self._preset()
        if not p:
            return
        model, mm, missing = self._locate(p)
        if not missing:
            self._activate(p, model, mm)
            return
        dest = model.parent if model else self._models_root() / p.folder
        gb = sum(p.sizes.get(f, 0) for f in missing) / catalog.GB
        if QMessageBox.question(self, "Download model",
                                f"Download {gb:.1f} GB from Hugging Face ({p.repo}) into\n{dest}?\n\n"
                                "You can pause it and carry on later; it resumes where it stopped.") != QMessageBox.Yes:
            return
        st = self._dl_state = {"msg": "Starting...", "frac": 0.0, "done": None, "error": None,
                               "stop": threading.Event()}

        def work():
            try:
                st["done"] = catalog.download(p, dest, lambda m, f: st.update(msg=m, frac=f), st["stop"])
            except Exception as e:  # noqa: BLE001
                st["error"] = e

        threading.Thread(target=work, daemon=True).start()
        self.preset_bar.setVisible(True)
        self.preset_box.setEnabled(False)
        self.preset_btn.setText("Pause download")

        def tick():
            self.preset_bar.setValue(int(st["frac"] * 1000))
            self.preset_info.setText(st["msg"])
            if st["done"] is None and st["error"] is None:
                QTimer.singleShot(300, tick)
                return
            self._dl_state = None
            self._located.pop(p.key, None)      # files changed: look again
            self.preset_bar.setVisible(False)
            self.preset_box.setEnabled(True)
            if st["error"] is not None:
                self._preset_picked()
                self.preset_info.setText(f"{st['error']}")
                return
            model2, mm2 = st["done"]
            if model is not None:       # only the vision file was missing
                model2 = model
            note = st["msg"].removeprefix("Download complete.").strip()    # e.g. a file updated on Hugging Face
            self._activate(p, model2, mm2 or mm)
            if note:
                self.preset_info.setText(f"{self.preset_info.text()} {note}")

        tick()

    def _engine_toggled(self, on: bool):
        p = self._preset()
        if p and catalog.matching(self.s.model_path) is p:   # the running mode: switch engines now
            self.s.engine = "strata" if on else "llama.cpp"
            self.s.save()
            self._changed()

    def _activate(self, p, model: Path, mm: Path | None):
        """Switch to a mode: its file, its vision file and its server settings, then (re)start."""
        catalog.apply(p, self.s, model, mm)
        s = self.s
        if strata.supports(p):
            s.engine = "strata" if self.strata_box.isChecked() else "llama.cpp"
            s.save()
        self._sync_controls()
        try:
            self.info = model_info(str(model))    # the plan needs it now, not when the folder scan is back
        except Exception:  # noqa: BLE001
            self.info = None
        self._scan()
        if self.proc.running or self._phase is not None:
            self.stop()
        self.start()
        if self.proc.running or self._phase is not None:
            self.preset_info.setText(f"Switched to {p.title}. Loading it now; the chat uses it as soon as it's "
                                     "ready. Sampling and thinking levels follow the model automatically.")

    def _model_changed(self):
        path = self.model_box.currentData()
        self.info = None
        if path:
            if path != self.s.model_path:
                self.s.model_path = path
                self.s.mmproj_path = ""
                self.s.save()
            try:
                self.info = model_info(path)
                mm = Path(self.s.mmproj_path) if self.s.mmproj_path else launcher.find_mmproj(Path(path), self.info)
                vis = f"vision file: {mm.name}" if mm else "no vision (mmproj) file next to it yet"
                self.info_label.setText(f"{self.info.summary()}. {vis}.")
                self.vision_btn.setText("Find a different vision file" if mm else "Find vision file online")
            except Exception as e:
                self.info_label.setText(f"Couldn't read this file: {e}")
        self._changed()

    # ---------------------------------------------------------------- plan + command
    def _changed(self):
        s = self.s
        s.ctx_size = self.ctx.currentData()
        s.kv_type = self.kv.currentData()
        s.use_vision = self.vision.isChecked()
        s.placement = self.placement.currentData()
        s.gpu_layers = self.ngl.value()
        s.n_cpu_moe = self.ncm.value()
        s.speedup = self.speed.currentData()
        s.port = self.port.value()
        s.extra_args = self.extra.text()
        s.extra["autostart_model"] = self.autostart.isChecked()
        s.save()
        manual = s.placement == "manual"
        self.ngl.setEnabled(manual)
        self.ncm.setEnabled(manual)
        self.draft_label.setText(f"Draft model: {Path(s.draft_path).name}" if s.draft_path else "No draft model picked")
        p = catalog.matching(s.model_path)
        on_strata = strata.active(s, p)
        for w in (self.kv, self.placement, self.speed, self.extra):
            w.setEnabled(not on_strata)
        if on_strata:
            self.ngl.setEnabled(False)
            self.ncm.setEnabled(False)
            self.launch = None
            self._startable = True
            model = Path(s.model_path)
            args = strata.setup_args(p, model, s) if strata.needs_setup(p, model, s) else strata.serve_args(p, s)
            self.cmd.setPlainText(" ".join(f'"{a}"' if " " in a else a for a in args))
            first = not strata.installed() or strata.needs_setup(p, model, s)
            self.plan_label.setText(
                f"<b style='color:{OK}'>Runs on Strata.</b> It places the model itself: attention and the busiest "
                "experts on the GPU, the other experts in RAM (worked on by the CPU at the same time), the n-gram "
                "table on the SSD, plus a draft layer for faster writing. Context length, vision and port below "
                "still apply; the other settings are llama.cpp's."
                + ("<br>The first start downloads Strata (about 55 MB) and runs its setup, which installs Python if "
                   "needed and fetches its engine and a ~6 GB draft layer. Later starts take 30-90 seconds."
                   if first else ""))
            return
        self._startable = bool(s.model_path)     # without llama.cpp, Start offers to download it
        if not (self.exe and s.model_path):
            self.launch = None
            self.cmd.setPlainText("")
            self.plan_label.setText("llama.cpp, the program that runs the model, isn't on this PC yet. Start model "
                                    "offers to download it first." if s.model_path else "")
            self._sync_start_btn()
            return
        lp = launcher.build_launch(s, self.exe, launcher.server_help(self.exe), self.info, self.vram)
        self.launch = lp
        self.cmd.setPlainText(" ".join(f'"{a}"' if " " in a else a for a in lp.args))
        lines = []
        if lp.plan:
            p = lp.plan
            if p.fits_fully:
                lines.append(f"<b style='color:{OK}'>Fits fully on the GPU</b> (about {p.gpu_gb:.1f} GB incl. "
                             f"{p.kv_gb:.1f} GB of context memory).")
            elif p.n_cpu_moe:
                lines.append(f"<b style='color:{OK}'>MoE split:</b> about {p.gpu_gb:.1f} GB on the GPU, {p.ram_gb:.1f} GB "
                             "of experts in RAM. This is the fast way to run MoE models that are bigger than the card.")
            else:
                lines.append(f"<b style='color:{WARN}'>Doesn't fit:</b> {p.ram_gb:.1f} GB of layers would run on the CPU.")
            if p.tokens_per_sec:
                lines.append(f"Rough speed guess: {p.tokens_per_sec:.0f} tokens/second (measure it in the chat).")
        lines += lp.notes
        self.plan_label.setText("<br>".join(lines))
        if manual and lp.plan and self.ngl.value() == 999 and self.ncm.value() == 0:
            self.ncm.setValue(lp.plan.n_cpu_moe)

    # ---------------------------------------------------------------- running
    def start(self):
        self._changed()
        p = catalog.matching(self.s.model_path)
        if strata.active(self.s, p):
            self._start_strata(p)
            return
        if self.s.model_path and not self.exe:
            if QMessageBox.question(self, "llama.cpp", "The model runs in llama.cpp's server program, which isn't on "
                                    "this PC yet. Download the newest build for your graphics card from GitHub now, "
                                    "then start the model?") == QMessageBox.Yes:
                self._download(then_start=True)
            return
        if not self.launch:
            return
        try:
            self.proc.start(self.launch.args)
        except OSError as e:
            QMessageBox.warning(self, "Model server", f"Couldn't start llama-server: {e}")
            return
        self._was_ready = False
        self._ready_job = None
        self.state.setText("Loading the model...")
        self._sync_start_btn()
        self.win.refresh_connection()

    def stop(self):
        self.proc.stop()
        self._phase = None
        self._was_ready = False
        self._ready_job = None
        self.state.setText("Stopped")
        self._sync_start_btn()
        self.win.refresh_connection()

    def _sync_start_btn(self):
        if self._phase is not None:
            text = "Cancel setup"
        elif self.proc.running:
            text = "Stop model"
        else:
            text = "Start model"
        if self.start_btn.text() != text:
            self.start_btn.setText(text)
        self.start_btn.setEnabled(text != "Start model" or self._startable)

    def _start_strata(self, p):
        s = self.s
        model = Path(s.model_path)
        if self._strata_job is not None and self._strata_job.is_alive():
            # a stopped start can still be unpacking its download; two at once would trip over each other
            self.state.setText("Still stopping the last start. Press Start model again in a few seconds.")
            return
        if not strata.installed():
            if QMessageBox.question(
                    self, "Strata",
                    "Download Strata (about 55 MB, from github.com/Niko1221/Strata) into\n"
                    f"{strata.STRATA_DIR}\nand run its setup?\n\nSetup installs Python for your account if it's "
                    "missing, then fetches Strata's engine and a ~6 GB draft layer into\n"
                    f"{strata.data_dir(model)}.\nYour model files are used where they are.") \
                    != QMessageBox.Yes:
                return
        self.proc.stop()
        self.proc.lines.clear()
        self.proc.cancelled.clear()
        self._was_ready = False
        self._phase = "Setting up Strata (first time only; see the log)..."
        self.state.setText(self._phase)
        stop = self.proc.cancelled

        def say(msg, frac=None):
            if not stop.is_set():
                self._phase = msg

        def work():
            try:
                if not strata.installed():
                    strata.download(say)
                    self.proc.lines.append(f"Strata {strata.COMMIT[:7]} downloaded to {strata.STRATA_DIR}")
                if stop.is_set():
                    return
                if strata.needs_setup(p, model, s):
                    say("Running Strata's setup (the first time it downloads its engine and draft layer)...")
                    code = self.proc.run_step(strata.setup_args(p, model, s), strata.STRATA_DIR)
                    if stop.is_set():
                        return
                    if code != 0 or not strata.config_path(p).is_file():
                        say(f"error: Strata's setup stopped (exit code {code}). The log below says why; press "
                            "Start model to try again, it carries on where it stopped.")
                        return
                    strata.mark_setup_done(p, model, s)
                if stop.is_set():
                    return
                self.proc.start(strata.serve_args(p, s), cwd=strata.STRATA_DIR, clear=False)
                say("Loading the model...")
            except Exception as e:  # noqa: BLE001
                say(f"error: {e}")

        self._strata_job = threading.Thread(target=work, daemon=True)
        self._strata_job.start()
        self._sync_start_btn()
        self.win.refresh_connection()

    def _poll(self):
        phase = self._phase
        if phase is not None:          # a Strata start in progress
            if phase.startswith("error: "):
                self.state.setText(f"<span style='color:{BAD}'>{_esc(phase[7:])}</span>")
                self._phase = None
                self._show_log(True)
            else:
                self.state.setText(_esc(phase))
                if phase.startswith("Loading"):       # from here on, as for llama.cpp
                    self._phase = None
        lines = self.proc.lines
        sig = (len(lines), lines[-1] if lines else None)
        if sig != self._log_sig:
            self._log_sig = sig
            self.log.setPlainText("\n".join(list(lines)[-400:]))
            self.log.verticalScrollBar().setValue(self.log.verticalScrollBar().maximum())
        running = self.proc.running
        if running and self._phase is None and not self._was_ready:
            if self._ready_check() and self.proc.running:
                self._was_ready = True
                self.state.setText(f"<span style='color:{OK}'>Ready</span>")
                self.s.base_url = f"http://127.0.0.1:{self.s.port}/v1"
                self.s.model = ""
                self.s.save()
                self.win.chat._agent_sig = None     # a new model: the chat re-reads what it can do
                self.win.refresh_connection()
        elif not running and self._was_ready:
            self._was_ready = False
            self.state.setText(f"<span style='color:{BAD}'>The model server stopped. The log says why; press "
                               "Start model to run it again.</span>")
            self._show_log(True)
            self.win.refresh_connection()
        elif not running and self._phase is None and self.state.text().startswith("Loading"):
            self.state.setText(f"<span style='color:{BAD}'>The server stopped while loading. The log says why.</span>")
            self._show_log(True)
        self._sync_start_btn()

    def _ready_check(self) -> bool:
        """Ask /health on a worker thread (a slow answer mustn't freeze the window): True once a check
        that already finished said ready. One check at a time."""
        job = self._ready_job
        if job is not None:
            t, res = job
            if t.is_alive():
                return False
            self._ready_job = None
            if res.get("ok"):
                return True
        port, res = self.s.port, {}
        t = threading.Thread(target=lambda: res.update(ok=launcher.ServerProcess().ready(port)), daemon=True)
        t.start()
        self._ready_job = (t, res)
        return False

    def _find_vision(self):
        from . import vision

        if not self.s.model_path:
            return
        model = Path(self.s.model_path)
        info = self.info
        self.vision_btn.setEnabled(False)
        state = {"msg": "", "frac": 0.0, "found": None, "path": None, "error": None, "phase": "search"}

        def search():
            try:
                state["found"] = vision.find(info, model, progress=lambda m: state.update(msg=m))
            except Exception as e:  # noqa: BLE001
                state["error"] = e
            state["phase"] = "searched"

        def fetch(c):
            try:
                state["path"] = vision.download(c, model.parent, lambda m, f: state.update(msg=m, frac=f))
            except Exception as e:  # noqa: BLE001
                state["error"] = e
            state["phase"] = "downloaded"

        threading.Thread(target=search, daemon=True).start()

        def tick():
            if state["phase"] == "downloading":
                self.vision_status.setText(f"{state['msg']} {state['frac'] * 100:.0f}%")
            else:
                self.vision_status.setText(state["msg"])
            if state["phase"] in ("search", "downloading"):
                QTimer.singleShot(200, tick)
                return
            if state["error"] is not None:
                self.vision_btn.setEnabled(True)
                self.vision_status.setText("")
                QMessageBox.warning(self, "Vision file", f"That didn't work: {state['error']}")
                return
            if state["phase"] == "searched":
                c = state["found"]
                if c is None:
                    self.vision_btn.setEnabled(True)
                    self.vision_status.setText("No matching vision file found.")
                    QMessageBox.information(self, "Vision file", "I couldn't find a vision file that fits this model "
                                            "on Hugging Face. This model may not support images.")
                    return
                size = f" ({c.mb} MB)" if c.size else ""
                ok = QMessageBox.question(
                    self, "Vision file",
                    f"Found {c.file}{size} in {c.repo}. It's made for {c.base_name}, which your model is built "
                    f"on, and its header check says it {c.note}.\n\nDownload it next to your model?") == QMessageBox.Yes
                if not ok:
                    self.vision_btn.setEnabled(True)
                    self.vision_status.setText("")
                    return
                state["phase"] = "downloading"
                threading.Thread(target=fetch, args=(c,), daemon=True).start()
                QTimer.singleShot(200, tick)
                return
            # downloaded
            self.vision_btn.setEnabled(True)
            self.s.mmproj_path = str(state["path"])
            self.vision.setChecked(True)
            self.s.save()
            self._model_changed()
            self.vision_status.setText(f"Vision ready: {Path(state['path']).name}.")
            if self.proc.running:
                self.vision_status.setText(self.vision_status.text() + " Restart the model to use it.")

        tick()

    def _download(self, then_start: bool = False):
        self.dl_btn.setEnabled(False)
        self.dl_bar.setVisible(True)
        self.dl_bar.setRange(0, 100)
        state = {"msg": "", "frac": 0.0, "done": None}

        def work():
            try:
                state["done"] = launcher.download_llamacpp(lambda m, f: state.update(msg=m, frac=f))
            except Exception as e:  # noqa: BLE001
                state["done"] = e

        threading.Thread(target=work, daemon=True).start()

        def tick():
            self.dl_bar.setValue(int(state["frac"] * 100))
            self.dl_bar.setFormat(state["msg"][:80] + "  %p%")
            if then_start:
                self.state.setText(f"Downloading llama.cpp: {state['msg'][:80]} {state['frac'] * 100:.0f}%")
            if state["done"] is None:
                QTimer.singleShot(200, tick)
                return
            self.dl_btn.setEnabled(True)
            self.dl_bar.setVisible(False)
            if then_start:
                self.state.setText("Stopped")
            if isinstance(state["done"], Exception):
                QMessageBox.warning(self, "Download", f"Download failed: {state['done']}")
            else:
                self.s.server_exe = str(state["done"])
                self.s.save()
                self._refresh_exe()
                self._changed()
                if then_start and not self.proc.running:
                    self.start()

        tick()


# ====================================================================== settings page
class SettingsPage(QWidget):
    def __init__(self, win: "MainWindow"):
        super().__init__()
        self.setObjectName("page")
        self.win = win
        s = self.s = win.settings
        area = QScrollArea()
        area.setWidgetResizable(True)
        body = QWidget()
        body.setObjectName("page")
        lay = QVBoxLayout(body)
        lay.setContentsMargins(20, 14, 20, 14)
        h = QLabel("Settings")
        h.setObjectName("h1")
        lay.addWidget(h)
        lay.addWidget(muted("Changes save straight away and apply to the next message. Thinking and what it may "
                            "do without asking are picked in the chat bar."))

        g = QGroupBox("What it may use")
        f = QFormLayout(g)
        self.shell = QCheckBox("Run commands and Python")
        self.shell.setChecked(s.enable_shell)
        self.control = QCheckBox("See the screen, use mouse and keyboard, open programs")
        self.control.setChecked(s.enable_control)
        self.web = QCheckBox("Search and read the web")
        self.web.setChecked(s.enable_web)
        self.sub = QCheckBox("Start helper agents for big searches")
        self.sub.setChecked(s.enable_subagents)
        self.code = QCheckBox("Coding mode: work in a private git copy, run tests until they pass, map the code")
        self.code.setChecked(s.enable_code)
        self.unity = QCheckBox("Unity: work in the open Unity editor (scenes, objects, scripts, play mode)")
        self.unity.setChecked(s.enable_unity)
        for w in (self.shell, self.control, self.web, self.sub, self.code, self.unity):
            w.toggled.connect(self._save)
            f.addRow(w)
        self.dangerous = QCheckBox("Always ask before dangerous commands (format, delete everything, registry...)")
        self.dangerous.setChecked(s.always_ask_dangerous)
        self.dangerous.toggled.connect(self._save)
        f.addRow(self.dangerous)
        lay.addWidget(g)

        g = QGroupBox("GitHub")
        f = QFormLayout(g)
        f.addRow(muted("Lets it clone your repositories, make branches and commits, push, and open pull "
                       "requests and issues. Sign in with a token below, or run `gh auth login` once if you "
                       "have the GitHub CLI. The token stays in this app's data folder on your PC."))
        self.github = QCheckBox("Use git and GitHub")
        self.github.setChecked(s.enable_github)
        self.github.toggled.connect(self._save)
        f.addRow(self.github)
        row = QHBoxLayout()
        self.gh_token = QLineEdit(s.github_token)
        self.gh_token.setEchoMode(QLineEdit.Password)
        self.gh_token.setPlaceholderText("Personal access token (optional if you use the GitHub CLI)")
        self.gh_token.editingFinished.connect(self._save)
        row.addWidget(self.gh_token, 1)
        self.gh_check = QPushButton("Check sign-in")
        self.gh_check.clicked.connect(self._check_github)
        row.addWidget(self.gh_check)
        b = QPushButton("Make a token...")
        b.clicked.connect(lambda: __import__("webbrowser").open(
            "https://github.com/settings/personal-access-tokens/new"))
        row.addWidget(b)
        f.addRow("Token:", row)
        self.publish = QCheckBox("Always ask before pushing code or posting to GitHub, even in full auto")
        self.publish.setChecked(s.always_ask_publish)
        self.publish.toggled.connect(self._save)
        f.addRow(self.publish)
        lay.addWidget(g)

        g = QGroupBox("Add-on tools")
        f = QFormLayout(g)
        f.addRow(muted("Connect MCP servers (the format Claude Desktop uses) for browsers, databases, Blender and "
                       "more, or drop Python files with your own tools into the plugins folder. New tools show up "
                       "in the next new chat."))
        row = QHBoxLayout()
        b = QPushButton("Edit MCP servers...")
        b.clicked.connect(self._edit_mcp)
        row.addWidget(b)
        b = QPushButton("Open plugins folder")
        b.clicked.connect(lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(str(plugins.plugins_dir()))))
        row.addWidget(b)
        b = QPushButton("Reload")
        b.setToolTip("Restart the MCP servers and load the plugin files again")
        b.clicked.connect(self._reload_addons)
        row.addWidget(b)
        row.addStretch(1)
        f.addRow(row)
        self.addons_status = muted("")
        self.addons_status.setTextFormat(Qt.PlainText)
        f.addRow(self.addons_status)
        lay.addWidget(g)

        g = QGroupBox("Connection")
        f = QFormLayout(g)
        f.addRow(muted("The Model server page fills this in when it starts a model. Change it only to use LM "
                       "Studio, Ollama or another server."))
        self.preset = QComboBox()
        self.preset.addItem("Custom", "")
        for k, v in SERVER_PRESETS.items():
            self.preset.addItem(k, v)
        self.preset.currentIndexChanged.connect(self._preset)
        f.addRow("Server:", self.preset)
        self.url = QLineEdit(s.base_url)
        self.url.editingFinished.connect(self._save_connection)
        f.addRow("Address:", self.url)
        row = QHBoxLayout()
        self.model = QComboBox()
        self.model.setEditable(True)
        self.model.setEditText(s.model)
        self.model.lineEdit().setPlaceholderText("(whatever the server has loaded)")
        self.model.lineEdit().editingFinished.connect(self._save_connection)
        self.model.activated.connect(lambda _i: self._save_connection())
        row.addWidget(self.model, 1)
        self.test_btn = QPushButton("Test connection")
        self.test_btn.clicked.connect(self._test)
        row.addWidget(self.test_btn)
        f.addRow("Model:", row)
        lay.addWidget(g)

        self.adv_btn = QToolButton(text="▸ Advanced: tool calling, sampling, screen, limits")
        self.adv_btn.clicked.connect(lambda: self._show_advanced(not self.adv.isVisible()))
        lay.addWidget(self.adv_btn)
        self.adv = QWidget()
        al = QVBoxLayout(self.adv)
        al.setContentsMargins(0, 0, 0, 0)

        g = QGroupBox("Answers")
        f = QFormLayout(g)
        self.tool_mode = self._combo([("auto", "Automatic"), ("native", "Standard (server tool calling)"),
                                      ("prompt", "In the prompt (for servers without tool support)")], s.tool_mode)
        f.addRow("Tool calling:", self.tool_mode)
        self.auto_sampling = QCheckBox("Use the model maker's recommended sampling, switched automatically "
                                       "when thinking goes on or off")
        self.auto_sampling.setChecked(s.auto_sampling)
        self.auto_sampling.toggled.connect(self._save)
        f.addRow(self.auto_sampling)
        self.temp = self._dspin(s.temperature, 0, 2, 0.05)
        self.top_p = self._dspin(s.top_p, 0, 1, 0.05)
        self.top_k = self._spin(s.top_k, 0, 200)
        self.min_p = self._dspin(s.min_p, 0, 1, 0.01)
        self.max_tokens = self._spin(s.max_tokens, 256, 131072)
        f.addRow("Temperature:", self.temp)
        f.addRow("Top P:", self.top_p)
        f.addRow("Top K:", self.top_k)
        f.addRow("Min P:", self.min_p)
        f.addRow("Max reply tokens:", self.max_tokens)
        b = QPushButton("Use the model maker's recommended values")
        b.clicked.connect(self._recommended)
        f.addRow(b)
        al.addWidget(g)

        g = QGroupBox("Screen")
        f = QFormLayout(g)
        self.max_side = self._spin(s.screenshot_max_side, 640, 3840)
        f.addRow("Screenshot size (longest side, pixels):", self.max_side)
        self.monitor = self._spin(s.monitor, 1, 8)
        f.addRow("Which screen:", self.monitor)
        self.coords = self._combo([("auto", "Automatic by model"), ("pixels", "Screenshot pixels"),
                                   ("normalized", "0-1000 grid (Qwen vision models)")], s.coord_mode)
        f.addRow("Click coordinates:", self.coords)
        self.after = QCheckBox("Take a screenshot after every click/keypress so it can check its work")
        self.after.setChecked(s.screenshot_after_action)
        self.grid = QCheckBox("Draw a coordinate grid on screenshots (helps weaker models aim)")
        self.grid.setChecked(s.grid_overlay)
        self.minimize = QCheckBox("Minimize this window while it uses the screen")
        self.minimize.setChecked(s.extra.get("minimize_during_control", True))
        for w in (self.after, self.grid, self.minimize):
            w.toggled.connect(self._save)
            f.addRow(w)
        al.addWidget(g)

        g = QGroupBox("Limits")
        f = QFormLayout(g)
        self.ctx_limit = self._spin(s.context_limit, 0, 1_048_576)
        self.ctx_limit.setSpecialValueText("Ask the server")
        f.addRow("Context size:", self.ctx_limit)
        self.keep_images = self._spin(s.keep_images, 1, 20)
        f.addRow("Screenshots kept in the conversation:", self.keep_images)
        self.max_steps = self._spin(s.max_steps, 5, 2000)
        f.addRow("Steps before it checks in:", self.max_steps)
        self.max_out = self._spin(s.max_tool_output, 2000, 200000)
        f.addRow("Longest tool result (characters):", self.max_out)
        al.addWidget(g)
        self.adv.setVisible(False)
        lay.addWidget(self.adv)
        lay.addStretch(1)
        area.setWidget(body)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(area)
        self._sync_preset()
        for w in (self.temp, self.top_p, self.top_k, self.min_p):
            w.setEnabled(not s.auto_sampling)

    def _show_advanced(self, on: bool):
        self.adv.setVisible(on)
        self.adv_btn.setText(("▾" if on else "▸") + self.adv_btn.text()[1:])

    def _combo(self, items, current):
        c = QComboBox()
        for k, label in items:
            c.addItem(label, k)
        c.setCurrentIndex(max(0, c.findData(current)))
        c.currentIndexChanged.connect(self._save)
        return c

    def _spin(self, v, lo, hi):
        w = QSpinBox(minimum=lo, maximum=hi)
        w.setValue(min(hi, max(lo, int(v))))
        w.valueChanged.connect(self._save)
        return w

    def _dspin(self, v, lo, hi, step):
        w = QDoubleSpinBox(minimum=lo, maximum=hi, singleStep=step, decimals=2)
        w.setValue(min(hi, max(lo, float(v))))
        w.valueChanged.connect(self._save)
        return w

    def _sync_preset(self):
        idx = self.preset.findData(self.s.base_url)
        self.preset.blockSignals(True)
        self.preset.setCurrentIndex(idx if idx > 0 else 0)
        self.preset.blockSignals(False)

    def _preset(self):
        url = self.preset.currentData()
        if url:
            self.url.setText(url)
            self._save_connection()

    def reload(self):
        """The Model server page changes the address and model when it starts one: show what's saved."""
        self.url.setText(self.s.base_url)
        self.model.setEditText(self.s.model)
        self._sync_preset()
        self._show_addons()

    def _show_addons(self):
        m = mcp.manager()
        self.addons_status.setText(f"MCP: {m.summary()}\nPlugins: {plugins.summary()}")
        if m.starting:
            QTimer.singleShot(500, self._show_addons)

    def _reload_addons(self):
        plugins.load()
        mcp.manager().start_background(cwd=self.s.workspace_path())
        QTimer.singleShot(100, self._show_addons)

    def _edit_mcp(self):
        path = mcp.config_path()
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            text = '{\n  "mcpServers": {\n  }\n}\n'
        dlg = QDialog(self)
        dlg.setWindowTitle("MCP servers")
        dlg.resize(720, 480)
        lay = QVBoxLayout(dlg)
        lay.addWidget(muted('One entry per server: a name, the program to run and its arguments. Example:\n'
                            '"browser": {"command": "npx", "args": ["-y", "@playwright/mcp@latest"]}\n'
                            'Their tools ask before running; add "risk": "safe" for a server you trust to only '
                            'read, or "hints" to trust what the server marks as read-only.'))
        box = QPlainTextEdit(text)
        box.setFont(QFont("Consolas", 10))
        lay.addWidget(box, 1)
        btns = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        lay.addWidget(btns)
        btns.rejected.connect(dlg.reject)

        def save():
            new = box.toPlainText()
            tmp = path.with_name(path.name + ".check")
            try:
                tmp.parent.mkdir(parents=True, exist_ok=True)
                tmp.write_text(new, encoding="utf-8")
                mcp.load_config(tmp)
                tmp.replace(path)
            except (OSError, ValueError) as e:
                tmp.unlink(missing_ok=True)
                QMessageBox.warning(dlg, "MCP servers", str(e))
                return
            dlg.accept()
            self._reload_addons()

        btns.accepted.connect(save)
        dlg.exec()

    def _save_connection(self):
        """Only the address and model, and only from their own boxes: saving them along with every other
        switch would write back an address the Model server page has since changed."""
        s = self.s
        url, model = self.url.text().strip() or s.base_url, self.model.currentText().strip()
        if (url, model) == (s.base_url, s.model):
            return
        s.base_url, s.model = url, model
        s.save()
        self._sync_preset()
        self.win.refresh_connection()

    def _save(self, *_):
        s = self.s
        s.tool_mode = self.tool_mode.currentData()
        s.auto_sampling = self.auto_sampling.isChecked()
        for w in (self.temp, self.top_p, self.top_k, self.min_p):
            w.setEnabled(not s.auto_sampling)   # the recommended values win while that box is ticked
        s.temperature, s.top_p = self.temp.value(), self.top_p.value()
        s.top_k, s.min_p, s.max_tokens = self.top_k.value(), self.min_p.value(), self.max_tokens.value()
        s.enable_shell, s.enable_control = self.shell.isChecked(), self.control.isChecked()
        s.enable_web, s.enable_subagents = self.web.isChecked(), self.sub.isChecked()
        s.enable_code = self.code.isChecked()
        s.enable_unity = self.unity.isChecked()
        s.always_ask_dangerous = self.dangerous.isChecked()
        s.enable_github, s.always_ask_publish = self.github.isChecked(), self.publish.isChecked()
        s.github_token = self.gh_token.text().strip()
        s.screenshot_max_side, s.monitor = self.max_side.value(), self.monitor.value()
        s.coord_mode = self.coords.currentData()
        s.screenshot_after_action, s.grid_overlay = self.after.isChecked(), self.grid.isChecked()
        s.extra["minimize_during_control"] = self.minimize.isChecked()
        s.context_limit, s.keep_images = self.ctx_limit.value(), self.keep_images.value()
        s.max_steps, s.max_tool_output = self.max_steps.value(), self.max_out.value()
        s.save()

    def _check_github(self):
        self._save()
        from .tools import ToolContext
        from .tools import github as gh

        gh._cache.clear()
        ctx = ToolContext(settings=self.s, workspace=self.s.workspace_path())
        self.gh_check.setEnabled(False)
        self.gh_check.setText("Checking...")

        def work():
            if not gh.token(ctx):
                return None
            return gh.api(ctx, "GET", "/user"), bool(__import__("shutil").which("git"))

        def done(res, err):
            self.gh_check.setEnabled(True)
            self.gh_check.setText("Check sign-in")
            if err is not None:
                QMessageBox.warning(self, "GitHub", str(err))
            elif res is None:
                QMessageBox.information(self, "GitHub", "Not signed in: paste a token, or run `gh auth login` in a "
                                        "terminal and press Check again.")
            else:
                me, git = res
                QMessageBox.information(self, "GitHub", f"Signed in as {me.get('login')}. Git is "
                                        + ("found." if git else "NOT installed (get it from git-scm.com)."))

        in_background(work, done)

    def _test(self):
        self._save_connection()
        url = self.s.base_url
        client = LLMClient(url, self.s.api_key, self.s.model)
        self.test_btn.setEnabled(False)
        self.test_btn.setText("Testing...")

        def done(res, err):
            self.test_btn.setEnabled(True)
            self.test_btn.setText("Test connection")
            if err is not None:
                QMessageBox.warning(self, "Connection", f"Couldn't reach {url}:\n{err}")
                return
            ids, ctx = res
            current = self.model.currentText()
            self.model.blockSignals(True)
            self.model.clear()
            self.model.addItems(ids)
            self.model.setEditText(current)
            self.model.blockSignals(False)
            QMessageBox.information(self, "Connection", f"Connected. Models: {', '.join(ids) or '(none listed)'}"
                                    + (f"\nContext size: {ctx:,} tokens" if ctx else ""))
            self.win.refresh_connection()

        in_background(lambda: (client.models(), client.context_size()), done)

    def _recommended(self):
        name = self.s.model or self.win.connected_model or Path(self.s.model_path).name
        vals = sampling_for(name)
        if not vals:
            QMessageBox.information(self, "Recommended values", f"No recommendation stored for '{name}'.")
            return
        self.temp.setValue(vals["temperature"])
        self.top_p.setValue(vals["top_p"])
        self.top_k.setValue(vals["top_k"])
        self.min_p.setValue(vals["min_p"])


# ====================================================================== memory
class NoteDialog(QDialog):
    def __init__(self, parent=None, title="", body="", pinned=False):
        super().__init__(parent)
        self.setWindowTitle("Note")
        self.resize(520, 300)
        lay = QFormLayout(self)
        self.title = QLineEdit(title)
        self.title.setPlaceholderText("A few words, e.g. Projects folder")
        lay.addRow("Title:", self.title)
        self.body = QPlainTextEdit(body)
        self.body.setPlaceholderText("One fact, short and specific")
        lay.addRow("Note:", self.body)
        self.pinned = QCheckBox("Pinned: always show it to the AI (for things needed in nearly every chat)")
        self.pinned.setChecked(pinned)
        lay.addRow(self.pinned)
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        lay.addRow(bb)


class MemoryPage(QWidget):
    """Saved notes and the file index. The AI only sees the notes that match what you ask
    (plus pinned ones), so a long list here doesn't slow it down."""

    def __init__(self, win: "MainWindow"):
        super().__init__()
        self.setObjectName("page")
        self.win = win
        s = self.s = win.settings
        lay = QVBoxLayout(self)
        lay.setContentsMargins(20, 14, 20, 14)
        h = QLabel("Memory")
        h.setObjectName("h1")
        lay.addWidget(h)
        lay.addWidget(muted("Notes the AI keeps between chats. With each message it's shown only the few notes "
                            "that match what you asked, plus pinned ones, so it doesn't get bogged down. It can "
                            "search everything else, including your indexed files, with its recall tool."))
        self.recall = QCheckBox("Show it matching notes with each message")
        self.recall.setChecked(s.auto_recall)
        self.auto = QCheckBox("After bigger tasks, let it save what's worth remembering")
        self.auto.setChecked(s.auto_memory)
        for w in (self.recall, self.auto):
            w.toggled.connect(self._save)
            lay.addWidget(w)

        row = QHBoxLayout()
        self.search = QLineEdit()
        self.search.setPlaceholderText("Search notes...")
        self.search.textChanged.connect(self.refresh)
        row.addWidget(self.search, 1)
        for label, fn in (("New note", self._new), ("Edit", self._edit), ("Pin / unpin", self._pin),
                          ("Delete", self._delete)):
            b = QPushButton(label)
            b.clicked.connect(fn)
            row.addWidget(b)
        lay.addLayout(row)
        self.list = QListWidget()
        self.list.setWordWrap(True)
        self.list.itemDoubleClicked.connect(lambda _i: self._edit())
        lay.addWidget(self.list, 1)
        self.count = muted("")
        lay.addWidget(self.count)

        g = QGroupBox("File index")
        v = QVBoxLayout(g)
        v.addWidget(muted("Lets it find your files by what's in them (\"the budget spreadsheet\", \"where I "
                          "handle login\"). The work folder is always included. Updates itself every 10 "
                          "minutes; only changed files are read again."))
        self.index_on = QCheckBox("Keep a search index of these folders")
        self.index_on.setChecked(s.index_files)
        self.index_on.toggled.connect(self._save)
        v.addWidget(self.index_on)
        self.folders = QListWidget()
        self.folders.setMaximumHeight(120)
        v.addWidget(self.folders)
        row = QHBoxLayout()
        for label, fn in (("Add folder...", self._add_folder), ("Remove", self._remove_folder),
                          ("Update now", self._reindex)):
            b = QPushButton(label)
            b.clicked.connect(fn)
            row.addWidget(b)
        row.addStretch(1)
        v.addLayout(row)
        self.index_status = muted("")
        v.addWidget(self.index_status)
        lay.addWidget(g)
        self.timer = QTimer(self, interval=1000, timeout=self._tick)
        self.timer.start()
        self.refresh()

    def _save(self, *_):
        s = self.s
        s.auto_recall, s.auto_memory = self.recall.isChecked(), self.auto.isChecked()
        was = s.index_files
        s.index_files = self.index_on.isChecked()
        s.save()
        if s.index_files and not was:
            self._reindex()

    def refresh(self, *_):
        mem = memory_mod.get_memory()
        self.list.clear()
        for n in mem.notes(self.search.text()):
            it = QListWidgetItem(("📌 " if n.pinned else "") + (f"{n.title}: " if n.title else "") + n.body)
            it.setData(Qt.UserRole, n.id)
            it.setToolTip(f"Note #{n.id}, last changed {n.updated}")
            self.list.addItem(it)
        self.count.setText(f"{mem.count_notes()} notes saved.")
        self.folders.clear()
        for i, r in enumerate(memory_mod.index_roots(self.s)):
            self.folders.addItem(f"{r}  (work folder)" if i == 0 else str(r))

    def _selected(self):
        it = self.list.currentItem()
        return memory_mod.get_memory().get(it.data(Qt.UserRole)) if it else None

    def _new(self):
        d = NoteDialog(self)
        if d.exec() and d.body.toPlainText().strip():
            try:
                memory_mod.get_memory().add(d.body.toPlainText(), d.title.text(), d.pinned.isChecked())
            except ValueError as e:          # too long for one note
                QMessageBox.warning(self, "Note", str(e))
            self.refresh()

    def _edit(self):
        n = self._selected()
        if not n:
            return
        d = NoteDialog(self, n.title, n.body, n.pinned)
        if d.exec() and d.body.toPlainText().strip():
            try:
                memory_mod.get_memory().update(n.id, d.body.toPlainText(), d.title.text(), d.pinned.isChecked())
            except ValueError as e:
                QMessageBox.warning(self, "Note", str(e))
            self.refresh()

    def _pin(self):
        n = self._selected()
        if n:
            memory_mod.get_memory().update(n.id, pinned=not n.pinned)
            self.refresh()

    def _delete(self):
        n = self._selected()
        if n and QMessageBox.question(self, "Delete note", f"Delete this note?\n\n{n.body[:300]}") == QMessageBox.Yes:
            memory_mod.get_memory().delete(n.id)
            self.refresh()

    def _add_folder(self):
        d = QFileDialog.getExistingDirectory(self, "Folder to index", str(Path.home()))
        if d and d not in self.s.index_folders:
            self.s.index_folders = list(self.s.index_folders) + [d]
            self.s.save()
            self.refresh()
            self._reindex()

    def _remove_folder(self):
        row = self.folders.currentRow()
        if row <= 0:  # the work folder always stays
            return
        root = memory_mod.index_roots(self.s)[row]
        self.s.index_folders = [f for f in self.s.index_folders if Path(f).expanduser().resolve() != root]
        self.s.save()
        threading.Thread(target=memory_mod.get_memory().forget_folder, args=(root,), daemon=True).start()
        self.refresh()

    def _reindex(self):
        idx = memory_mod.indexer()
        if idx is None or not idx.is_alive():
            memory_mod.start_indexer(self.s)
        else:
            idx.refresh_now()

    def _tick(self):
        idx = memory_mod.indexer()
        if not self.s.index_files:
            self.index_status.setText("File index is off.")
        elif idx is not None:
            self.index_status.setText(idx.status)


# ====================================================================== window
class MainWindow(QMainWindow):
    def __init__(self, settings: Settings | None = None):
        super().__init__()
        self.settings = settings or Settings.load()
        self.connected_model = ""
        self.last_tps = 0.0
        self.setWindowTitle("Local Agent")
        self.resize(1280, 860)
        central = QWidget()
        central.setObjectName("page")
        lay = QHBoxLayout(central)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        self.nav = QListWidget()
        self.nav.setObjectName("nav")
        self.nav.setFixedWidth(170)
        self.stack = QStackedWidget()
        self.chat = ChatPage(self)
        self.server = ServerPage(self)
        self.settings_page = SettingsPage(self)
        self.memory = MemoryPage(self)
        guide = QTextBrowser()
        guide.setMarkdown(GUIDE)
        guide.setOpenExternalLinks(True)
        for name, page in [("Chat", self.chat), ("Model server", self.server), ("Settings", self.settings_page),
                           ("Memory", self.memory), ("Guide", guide)]:
            self.nav.addItem(name)
            self.stack.addWidget(page)
        self.nav.currentRowChanged.connect(self._nav)
        lay.addWidget(self.nav)
        lay.addWidget(self.stack, 1)
        self.setCentralWidget(central)
        self.nav.setCurrentRow(0)
        QTimer.singleShot(300, self.refresh_connection)
        plugins.load()
        mcp.manager().start_background(cwd=self.settings.workspace_path())
        if self.settings.index_files:
            memory_mod.start_indexer(self.settings)

    def _nav(self, i):
        self.stack.setCurrentIndex(i)
        if i == 2:
            self.settings_page.reload()
        if i == 3:
            self.memory.refresh()
        if i == 0:
            self.chat.sync_thinking()

    def refresh_connection(self):
        s = self.settings
        result = {}

        def work():
            c = LLMClient(s.base_url, s.api_key, s.model)
            try:
                ids = c.models()
                result["ok"] = (s.model or (ids[0] if ids else "connected"))
            except Exception:  # noqa: BLE001
                result["ok"] = None

        t = threading.Thread(target=work, daemon=True)
        t.start()

        def check():
            if t.is_alive():
                QTimer.singleShot(150, check)
                return
            name = result.get("ok")
            self.connected_model = name or ""
            srv = self.server
            mode = catalog.matching(s.model_path)
            ours = srv.proc.running and s.base_url.startswith(f"http://127.0.0.1:{s.port}/")
            if name:
                self.chat.set_connection("ok", mode.title.split(" (")[0] if mode and ours else name)
            elif srv.proc.running or srv._phase is not None:
                self.chat.set_connection("busy", "Loading the model. The chat can use it as soon as it's ready.")
            else:
                self.chat.set_connection("off", "No model running",
                                         first_run=not s.model_path,
                                         tip="Start one from the Model menu, or point Settings > Connection at "
                                             "LM Studio or Ollama.")

        check()

    def closeEvent(self, e):
        if self.server.proc.running:
            r = QMessageBox.question(self, "Quit", "Stop the model server too?",
                                     QMessageBox.Yes | QMessageBox.No | QMessageBox.Cancel)
            if r == QMessageBox.Cancel:
                e.ignore()
                return
            if r == QMessageBox.Yes:
                self.server.proc.stop()
        self.chat.stop.set()
        _stop_background(self.chat.agent)
        mcp.manager().stop()
        super().closeEvent(e)


def dark_palette() -> QPalette:
    """For what the style sheet doesn't reach (message boxes, file dialogs, disabled text)."""
    pal = QPalette()
    for role, col in [(QPalette.Window, "#16181d"), (QPalette.WindowText, "#e6e8ee"), (QPalette.Base, "#1e2128"),
                      (QPalette.AlternateBase, "#23262e"), (QPalette.Text, "#e6e8ee"), (QPalette.Button, "#2a2f3a"),
                      (QPalette.ButtonText, "#e6e8ee"), (QPalette.ToolTipBase, "#23262e"),
                      (QPalette.ToolTipText, "#e6e8ee"), (QPalette.Highlight, ACCENT),
                      (QPalette.HighlightedText, "#ffffff"), (QPalette.PlaceholderText, "#6b717c"),
                      (QPalette.Link, ACCENT)]:
        pal.setColor(role, QColor(col))
    for role in (QPalette.Text, QPalette.ButtonText, QPalette.WindowText):
        pal.setColor(QPalette.Disabled, role, QColor("#6b717c"))
    return pal


def main(argv=None):
    app = QApplication.instance() or QApplication([])
    app.setStyle("Fusion")
    app.setPalette(dark_palette())
    app.setStyleSheet(STYLE)
    w = MainWindow()
    w.show()
    return app.exec()
