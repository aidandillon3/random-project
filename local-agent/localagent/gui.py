"""The app window (PySide6 / Qt)."""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path

from PySide6.QtCore import QBuffer, QByteArray, QIODevice, QObject, Qt, QTimer, Signal
from PySide6.QtGui import QFont, QImage, QKeySequence, QPixmap, QShortcut
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QDialog, QDialogButtonBox, QDoubleSpinBox, QFileDialog,
    QFormLayout, QFrame, QGroupBox, QHBoxLayout, QInputDialog, QLabel, QLineEdit, QListWidget,
    QListWidgetItem, QMainWindow, QMessageBox, QPlainTextEdit, QProgressBar, QPushButton, QScrollArea,
    QSpinBox, QStackedWidget, QTextBrowser, QToolButton, QVBoxLayout, QWidget,
)

from . import launcher
from .agent import Agent, AgentUI
from .config import APPROVAL_MODES, DATA_DIR, SERVER_PRESETS, Settings
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
QToolButton {{ color: #9aa1ad; border: none; background: transparent; }}
QGroupBox {{ border: 1px solid #2c303a; border-radius: 8px; margin-top: 14px; padding: 10px; color: #c9ced8; }}
QGroupBox::title {{ subcontrol-origin: margin; left: 10px; padding: 0 4px; }}
QTextBrowser, QPlainTextEdit {{ background: #1e2128; border: 1px solid #2c303a; border-radius: 8px; color: #e6e8ee; }}
QLineEdit, QComboBox, QSpinBox, QDoubleSpinBox {{ background: #23262e; color: #e6e8ee; border: 1px solid #3a404d;
    border-radius: 4px; padding: 3px 6px; }}
QProgressBar {{ background: #23262e; border: 1px solid #3a404d; border-radius: 4px; text-align: center; color: white; height: 14px; }}
QProgressBar::chunk {{ background: {ACCENT}; border-radius: 4px; }}
QScrollArea {{ border: none; background: transparent; }}
"""


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
    lab.mousePressEvent = lambda e, p=pm: ImageDialog(p, lab.window()).exec()
    return lab


def muted(text: str) -> QLabel:
    lab = QLabel(text)
    lab.setProperty("muted", True)
    lab.setWordWrap(True)
    return lab


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
        lay = QVBoxLayout(self)
        lay.setContentsMargins(4, 2, 4, 2)
        self.think_btn = QToolButton(text="▸ Thinking")
        self.think_btn.setVisible(False)
        self.think_btn.clicked.connect(self._toggle)
        self.think = muted("")
        self.think.setVisible(False)
        self.think.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.body = QLabel()
        self.body.setWordWrap(True)
        self.body.setTextFormat(Qt.MarkdownText)
        self.body.setTextInteractionFlags(Qt.TextSelectableByMouse | Qt.LinksAccessibleByMouse)
        self.body.setOpenExternalLinks(True)
        self.body.setVisible(False)
        for w in (self.think_btn, self.think, self.body):
            lay.addWidget(w)

    def _toggle(self):
        show = not self.think.isVisible()
        self.think.setVisible(show)
        self.think_btn.setText(("▾" if show else "▸") + " Thinking")

    def add_text(self, s):
        self.text += s
        self._dirty = True

    def add_reasoning(self, s):
        self.reasoning += s
        self._dirty = True

    def refresh(self):
        if not self._dirty:
            return
        self._dirty = False
        if self.reasoning:
            self.think_btn.setVisible(True)
            self.think.setText(self.reasoning[-20000:])
        if self.text.strip():
            self.body.setVisible(True)
            self.body.setText(self.text)

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
        head = QLabel(f"<b>The AI wants to {risk_text(req.risk)}.</b>")
        head.setWordWrap(True)
        lay.addWidget(head)
        if req.dangerous:
            warn = QLabel("⚠️ This looks like a command that can delete data or change your system. "
                          "Read it carefully.")
            warn.setStyleSheet(f"color: {BAD}; font-weight: 600;")
            warn.setWordWrap(True)
            lay.addWidget(warn)
        box = QPlainTextEdit(req.summary + "\n\n" + req.pretty_args())
        box.setReadOnly(True)
        box.setFont(QFont("Consolas", 9))
        box.setMinimumHeight(160)
        lay.addWidget(box)
        self.note = QLineEdit()
        self.note.setPlaceholderText("Optional: if you deny, tell it what to do instead")
        lay.addWidget(self.note)
        row = QHBoxLayout()
        allow = QPushButton("Allow")
        allow.setObjectName("primary")
        allow.setDefault(True)
        allow.clicked.connect(lambda: self._done(True, False))
        row.addWidget(allow)
        if not req.dangerous:
            always = QPushButton(f"Allow all '{RISK_LABELS.get(req.risk, req.risk)}' for this chat")
            always.clicked.connect(lambda: self._done(True, True))
            row.addWidget(always)
        row.addStretch(1)
        deny = QPushButton("Deny")
        deny.clicked.connect(lambda: self._done(False, False))
        row.addWidget(deny)
        lay.addLayout(row)

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
        self.conn = QLabel("●  not connected")
        top.addWidget(self.conn, 1)
        top.addSpacing(12)
        top.addWidget(QLabel("Permissions:"))
        self.mode = QComboBox()
        for k, v in APPROVAL_MODES.items():
            self.mode.addItem(v, k)
        self.mode.setCurrentIndex(list(APPROVAL_MODES).index(self.s.approval_mode))
        self.mode.currentIndexChanged.connect(self._mode_changed)
        top.addWidget(self.mode)
        top.addWidget(QLabel("Thinking:"))
        self.think = QComboBox()
        for k, v in THINKING_LEVELS:
            self.think.addItem(v, k)
        self.think.setToolTip("How long it thinks before answering. Applies from your next message. "
                              "High is best for hard tasks, Off or Low for quick clicks.")
        self.sync_thinking()
        self.think.currentIndexChanged.connect(self._thinking_changed)
        top.addWidget(self.think)
        self.ws_btn = QPushButton()
        self.ws_btn.clicked.connect(self._pick_workspace)
        top.addWidget(self.ws_btn)
        b = QPushButton("Old chats")
        b.clicked.connect(self._open_chat)
        top.addWidget(b)
        b = QPushButton("New chat")
        b.clicked.connect(self.new_chat)
        top.addWidget(b)
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

        self.welcome = muted("Ask for anything: \"tidy up my Downloads folder\", \"what's on my screen?\", "
                             "\"write a script that renames these photos by date\", \"open Notepad and write a "
                             "poem\". It asks before changing anything (you can loosen that at the top).\n\n"
                             "Emergency stop while it uses the mouse: press Stop, or slam the mouse into a "
                             "screen corner.")
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
        self.ctx_bar.setFormat("%v / %m")
        sl.addWidget(self.ctx_bar)
        self.speed = muted("")
        sl.addWidget(self.speed)
        sl.addSpacing(12)
        self.side_mode = muted("")
        sl.addWidget(self.side_mode)
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
        self.ws_btn.setText(f"Work folder: {self.s.workspace_path().name}")
        self.ws_btn.setToolTip(str(self.s.workspace_path()))
        self.side_mode.setText(f"Model server: {self.s.base_url}")

    def sync_thinking(self):
        self.think.blockSignals(True)
        self.think.setCurrentIndex(max(0, self.think.findData("high" if self.s.thinking == "on" else self.s.thinking)))
        self.think.blockSignals(False)

    def _thinking_changed(self):
        self.s.thinking = self.think.currentData()
        self.s.save()

    def _mode_changed(self):
        self.s.approval_mode = self.mode.currentData()
        self.s.save()
        if self.agent:
            self.agent.policy.mode = self.s.approval_mode

    def _pick_workspace(self):
        d = QFileDialog.getExistingDirectory(self, "Folder the AI works in", str(self.s.workspace_path()))
        if d:
            self.s.workspace = d
            self.s.save()
            self._update_ws_btn()
            self._agent_sig = None

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
               s.enable_web, s.enable_subagents, s.enable_github, s.enable_code, s.github_token, s.coord_mode,
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
        self.new_chat()
        self._ensure_agent()
        self.agent.load_json(json.loads(f.read_text(encoding="utf-8")))
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
            elif m["role"] == "tool":
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
            self.current.refresh()
            if self.current.is_empty():
                self.current.setVisible(False)
        self.current = None

    def _on_tool_start(self, call_id, name, args, summary):
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
        if tps:
            self.speed.setText(f"{tps:.1f} tokens/second")

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

    def set_connection(self, ok: bool, text: str):
        color = OK if ok else BAD
        self.conn.setText(f"<span style='color:{color}'>●</span>  {_esc(text)}")
        self._update_ws_btn()


# ====================================================================== model server page
CTX_CHOICES = [8192, 16384, 32768, 65536, 131072, 262144]
SPEEDUPS = [("off", "Off"), ("mtp", "MTP: built-in draft layers (~2x, no vision)"),
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
        lay.addWidget(muted("Runs llama.cpp on your GPU with fast settings worked out for your card. Already use "
                            "LM Studio or Ollama? Skip this page and pick it under Settings > Connection."))
        self.gpu = muted("")
        lay.addWidget(self.gpu)

        g = QGroupBox("1. llama.cpp")
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
        lay.addWidget(g)

        g = QGroupBox("2. Model")
        f = QFormLayout(g)
        row = QHBoxLayout()
        self.dir_label = QLabel()
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

        g = QGroupBox("3. Settings (the defaults are tuned for an RTX 5080 16 GB)")
        f = QFormLayout(g)
        self.ctx = QComboBox()
        for n in CTX_CHOICES:
            self.ctx.addItem(f"{n // 1024}K tokens", n)
        f.addRow("Context length:", self.ctx)
        self.kv = QComboBox()
        for k, label in [("f16", "f16 (most memory)"), ("q8_0", "q8_0 (recommended)"), ("q4_0", "q4_0 (least memory, a bit worse)")]:
            self.kv.addItem(label, k)
        f.addRow("Context memory (KV cache):", self.kv)
        self.vision = QCheckBox("Let it see images and the screen (loads the mmproj file)")
        f.addRow(self.vision)
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
        lay.addWidget(g)

        self.plan_label = QLabel()
        self.plan_label.setWordWrap(True)
        lay.addWidget(self.plan_label)
        self.cmd = QPlainTextEdit()
        self.cmd.setReadOnly(True)
        self.cmd.setFixedHeight(70)
        self.cmd.setFont(QFont("Consolas", 9))
        lay.addWidget(self.cmd)
        row = QHBoxLayout()
        self.start_btn = QPushButton("Start model")
        self.start_btn.setObjectName("primary")
        self.start_btn.clicked.connect(self.start)
        self.stop_btn = QPushButton("Stop model")
        self.stop_btn.clicked.connect(self.stop)
        self.state = QLabel("Stopped")
        row.addWidget(self.start_btn)
        row.addWidget(self.stop_btn)
        row.addWidget(self.state)
        row.addStretch(1)
        lay.addLayout(row)
        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setFont(QFont("Consolas", 8))
        self.log.setMinimumHeight(220)
        lay.addWidget(self.log)
        area.setWidget(body)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(area)

        # load settings into the controls, then react to changes
        s = self.s
        self.ctx.setCurrentIndex(max(0, self.ctx.findData(s.ctx_size)))
        self.kv.setCurrentIndex(max(0, self.kv.findData(s.kv_type)))
        self.vision.setChecked(s.use_vision)
        self.placement.setCurrentIndex(max(0, self.placement.findData(s.placement)))
        self.ngl.setValue(s.gpu_layers)
        self.ncm.setValue(s.n_cpu_moe)
        self.speed.setCurrentIndex(max(0, self.speed.findData(s.speedup)))
        self.port.setValue(s.port)
        self.extra.setText(s.extra_args)
        for w in (self.ctx, self.kv, self.placement, self.speed):
            w.currentIndexChanged.connect(self._changed)
        for w in (self.ngl, self.ncm, self.port):
            w.valueChanged.connect(self._changed)
        self.vision.toggled.connect(self._changed)
        self.extra.editingFinished.connect(self._changed)

    # ---------------------------------------------------------------- scanning
    def _initial_scan(self):
        g = gpu_info()
        if g:
            self.vram = g[1]
            self.gpu.setText(f"GPU: {g[0]}, {g[1]:.1f} GB ({g[2]:.1f} GB in use right now)")
        else:
            self.gpu.setText("No NVIDIA GPU found by nvidia-smi; planning for 16 GB.")
        self._refresh_exe()
        self._scan()

    def _refresh_exe(self):
        exe = launcher.find_server(self.s)
        self.exe = exe
        self.exe_label.setText(str(exe) if exe else "<span style='color:#e0a43a'>Not found. Press Download.</span>")

    def _dirs(self) -> list[Path]:
        dirs = [Path(self.s.models_dir)] if self.s.models_dir else []
        return dirs + launcher.default_model_dirs()

    def _scan(self):
        dirs = self._dirs()
        self.dir_label.setText(", ".join(str(d) for d in dirs[:3]) or "(no folders yet: pick your models folder)")
        models = launcher.find_models(dirs)
        if self.s.model_path and Path(self.s.model_path).exists() and Path(self.s.model_path) not in models:
            models.insert(0, Path(self.s.model_path))
        self.model_box.blockSignals(True)
        self.model_box.clear()
        for m in models:
            self.model_box.addItem(m.name, str(m))
        idx = self.model_box.findData(self.s.model_path)
        self.model_box.setCurrentIndex(idx if idx >= 0 else 0)
        self.model_box.blockSignals(False)
        if not models:
            self.info_label.setText("No .gguf models found. Pick your models folder, or a file.")
        self._model_changed()

    def _browse_dir(self):
        d = QFileDialog.getExistingDirectory(self, "Folder with your .gguf models", self.s.models_dir or str(Path.home()))
        if d:
            self.s.models_dir = d
            self.s.save()
            self._scan()

    def _browse_model(self):
        f, _ = QFileDialog.getOpenFileName(self, "Model file", self.s.models_dir or str(Path.home()), "GGUF models (*.gguf)")
        if f:
            self.s.model_path = f
            self.s.mmproj_path = ""
            self.s.save()
            self._scan()

    def _browse_exe(self):
        f, _ = QFileDialog.getOpenFileName(self, "llama-server program", str(Path.home()), "llama-server (llama-server*)")
        if f:
            self.s.server_exe = f
            self.s.save()
            self._refresh_exe()
            self._changed()

    def _browse_draft(self):
        f, _ = QFileDialog.getOpenFileName(self, "Small draft model (same family)", self.s.models_dir or str(Path.home()),
                                           "GGUF models (*.gguf)")
        if f:
            self.s.draft_path = f
            self.s.save()
            self._changed()

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
                mm = Path(self.s.mmproj_path) if self.s.mmproj_path else launcher.find_mmproj(Path(path))
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
        s.save()
        manual = s.placement == "manual"
        self.ngl.setEnabled(manual)
        self.ncm.setEnabled(manual)
        self.draft_label.setText(f"Draft model: {Path(s.draft_path).name}" if s.draft_path else "No draft model picked")
        if not (self.exe and s.model_path):
            self.cmd.setPlainText("")
            self.plan_label.setText("")
            self.start_btn.setEnabled(False)
            return
        self.start_btn.setEnabled(True)
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
        if not self.launch:
            return
        try:
            self.proc.start(self.launch.args)
        except OSError as e:
            QMessageBox.warning(self, "Model server", f"Couldn't start llama-server: {e}")
            return
        self._was_ready = False
        self.state.setText("Loading the model...")

    def stop(self):
        self.proc.stop()
        self.state.setText("Stopped")
        self.win.refresh_connection()

    def _poll(self):
        if self.proc.lines:
            text = "\n".join(list(self.proc.lines)[-400:])
            if text != self.log.toPlainText():
                self.log.setPlainText(text)
                self.log.verticalScrollBar().setValue(self.log.verticalScrollBar().maximum())
        if self.proc.running and not self._was_ready and self.proc.ready(self.s.port):
            self._was_ready = True
            self.state.setText(f"<span style='color:{OK}'>Ready</span>")
            self.s.base_url = f"http://127.0.0.1:{self.s.port}/v1"
            self.s.model = ""
            self.s.save()
            self.win.refresh_connection()
        elif not self.proc.running and self.state.text().startswith("Loading"):
            self.state.setText(f"<span style='color:{BAD}'>The server stopped. See the log below.</span>")

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
            note = ""
            if self.speed.currentData() == "mtp":
                self.speed.setCurrentIndex(self.speed.findData("off"))
                note = " The MTP speed-up was switched off, because it doesn't work together with vision."
            self.s.save()
            self._model_changed()
            self.vision_status.setText(f"Vision ready: {Path(state['path']).name}.{note}")
            if self.proc.running:
                self.vision_status.setText(self.vision_status.text() + " Restart the model to use it.")

        tick()

    def _download(self):
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
            if state["done"] is None:
                QTimer.singleShot(200, tick)
                return
            self.dl_btn.setEnabled(True)
            self.dl_bar.setVisible(False)
            if isinstance(state["done"], Exception):
                QMessageBox.warning(self, "Download", f"Download failed: {state['done']}")
            else:
                self.s.server_exe = str(state["done"])
                self.s.save()
                self._refresh_exe()
                self._changed()

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
        lay.addWidget(muted("Changes save straight away and apply to the next message."))

        g = QGroupBox("Connection")
        f = QFormLayout(g)
        self.preset = QComboBox()
        self.preset.addItem("Custom", "")
        for k, v in SERVER_PRESETS.items():
            self.preset.addItem(k, v)
        self.preset.currentIndexChanged.connect(self._preset)
        f.addRow("Server:", self.preset)
        self.url = QLineEdit(s.base_url)
        self.url.editingFinished.connect(self._save)
        f.addRow("Address:", self.url)
        row = QHBoxLayout()
        self.model = QComboBox()
        self.model.setEditable(True)
        self.model.setEditText(s.model)
        self.model.lineEdit().setPlaceholderText("(whatever the server has loaded)")
        self.model.currentTextChanged.connect(lambda _t: self._save())
        row.addWidget(self.model, 1)
        b = QPushButton("Test connection")
        b.clicked.connect(self._test)
        row.addWidget(b)
        f.addRow("Model:", row)
        self.tool_mode = self._combo([("auto", "Automatic"), ("native", "Standard (server tool calling)"),
                                      ("prompt", "In the prompt (for servers without tool support)")], s.tool_mode)
        f.addRow("Tool calling:", self.tool_mode)
        lay.addWidget(g)

        g = QGroupBox("Answers")
        f = QFormLayout(g)
        self.thinking = self._combo(THINKING_LEVELS, "high" if s.thinking == "on" else s.thinking)
        self.thinking.setToolTip("How long the model thinks before answering. Models with levels (Qwen3.8: "
                                 "low / medium / xhigh, gpt-oss: low / medium / high) get the closest one; "
                                 "High means the model's highest. Models with only on/off treat every "
                                 "level as on.")
        f.addRow("Thinking:", self.thinking)
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
        lay.addWidget(g)

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
        for w in (self.shell, self.control, self.web, self.sub, self.code):
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
        b = QPushButton("Check sign-in")
        b.clicked.connect(self._check_github)
        row.addWidget(b)
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
        lay.addWidget(g)

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
        lay.addWidget(g)
        lay.addStretch(1)
        area.setWidget(body)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(area)
        self._sync_preset()
        for w in (self.temp, self.top_p, self.top_k, self.min_p):
            w.setEnabled(not s.auto_sampling)

    def _combo(self, items, current):
        c = QComboBox()
        for k, label in items:
            c.addItem(label, k)
        c.setCurrentIndex(max(0, c.findData(current)))
        c.currentIndexChanged.connect(self._save)
        return c

    def _spin(self, v, lo, hi):
        w = QSpinBox(minimum=lo, maximum=hi)
        w.setValue(int(v))
        w.valueChanged.connect(self._save)
        return w

    def _dspin(self, v, lo, hi, step):
        w = QDoubleSpinBox(minimum=lo, maximum=hi, singleStep=step, decimals=2)
        w.setValue(float(v))
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
            self._save()

    def reload(self):
        self.thinking.blockSignals(True)
        self.thinking.setCurrentIndex(max(0, self.thinking.findData(
            "high" if self.s.thinking == "on" else self.s.thinking)))
        self.thinking.blockSignals(False)
        self.url.setText(self.s.base_url)
        self.model.setEditText(self.s.model)
        self._sync_preset()

    def _save(self, *_):
        s = self.s
        s.base_url = self.url.text().strip() or s.base_url
        s.model = self.model.currentText().strip()
        s.tool_mode = self.tool_mode.currentData()
        s.thinking = self.thinking.currentData()
        s.auto_sampling = self.auto_sampling.isChecked()
        for w in (self.temp, self.top_p, self.top_k, self.min_p):
            w.setEnabled(not s.auto_sampling)   # the recommended values win while that box is ticked
        s.temperature, s.top_p = self.temp.value(), self.top_p.value()
        s.top_k, s.min_p, s.max_tokens = self.top_k.value(), self.min_p.value(), self.max_tokens.value()
        s.enable_shell, s.enable_control = self.shell.isChecked(), self.control.isChecked()
        s.enable_web, s.enable_subagents = self.web.isChecked(), self.sub.isChecked()
        s.enable_code = self.code.isChecked()
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
        self._sync_preset()

    def _check_github(self):
        self._save()
        from .tools import ToolContext, ToolError
        from .tools import github as gh

        gh._cache.clear()
        ctx = ToolContext(settings=self.s, workspace=self.s.workspace_path())
        if not gh.token(ctx):
            QMessageBox.information(self, "GitHub", "Not signed in: paste a token, or run `gh auth login` in a "
                                    "terminal and press Check again.")
            return
        try:
            me = gh.api(ctx, "GET", "/user")
        except ToolError as e:
            QMessageBox.warning(self, "GitHub", str(e))
            return
        git = "found" if __import__("shutil").which("git") else "NOT installed (get it from git-scm.com)"
        QMessageBox.information(self, "GitHub", f"Signed in as {me.get('login')}. Git is {git}.")

    def _test(self):
        self._save()
        client = LLMClient(self.s.base_url, self.s.api_key, self.s.model)
        try:
            ids = client.models()
        except Exception as e:  # noqa: BLE001
            QMessageBox.warning(self, "Connection", f"Couldn't reach {self.s.base_url}:\n{e}")
            return
        current = self.model.currentText()
        self.model.blockSignals(True)
        self.model.clear()
        self.model.addItems(ids)
        self.model.setEditText(current)
        self.model.blockSignals(False)
        ctx = client.context_size()
        QMessageBox.information(self, "Connection", f"Connected. Models: {', '.join(ids) or '(none listed)'}"
                                + (f"\nContext size: {ctx:,} tokens" if ctx else ""))
        self.win.refresh_connection()

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
            memory_mod.get_memory().add(d.body.toPlainText(), d.title.text(), d.pinned.isChecked())
            self.refresh()

    def _edit(self):
        n = self._selected()
        if not n:
            return
        d = NoteDialog(self, n.title, n.body, n.pinned)
        if d.exec() and d.body.toPlainText().strip():
            memory_mod.get_memory().update(n.id, d.body.toPlainText(), d.title.text(), d.pinned.isChecked())
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
            if name:
                self.chat.set_connection(True, f"{name}")
            else:
                self.chat.set_connection(False, "No model server. Start one on the Model server page, "
                                                "or point Settings at LM Studio / Ollama.")

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
        super().closeEvent(e)


def main(argv=None):
    app = QApplication.instance() or QApplication([])
    app.setStyle("Fusion")
    app.setStyleSheet(STYLE)
    w = MainWindow()
    w.show()
    return app.exec()
