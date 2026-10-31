"""The computer use test: its tasks notice when they're done, and a run times and saves every
step (a stand-in agent does the tasks here)."""

import json
import os
import sys
import tempfile
import threading
import time
import unittest
import urllib.parse
import urllib.request
from pathlib import Path
from unittest import mock

os.environ.setdefault("LOCALAGENT_DATA", tempfile.mkdtemp())
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PySide6.QtCore import QPoint, QPointF, Qt  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QApplication, QLineEdit, QPushButton, QSlider, QWidget  # noqa: E402

from localagent import cubench  # noqa: E402
from localagent.agent import Turn  # noqa: E402
from localagent.config import Settings  # noqa: E402
from localagent.tools import ToolResult  # noqa: E402

app = QApplication.instance() or QApplication([])


def pump(seconds=0.05):
    end = time.time() + seconds
    while time.time() < end:
        app.processEvents()
        time.sleep(0.005)


class TasksTest(unittest.TestCase):
    def setUp(self):
        self.win = cubench.make_window(1)
        self.win.resize(1600, 900)
        self.win.show()
        pump()
        self.addCleanup(self.win.close)

    def setup(self, name):
        task = cubench.Task(name, name)
        self.win.setup(task, cubench.random.Random(3), "http://x/")
        pump()
        return task, self.win.state

    def test_words_wrong_then_right(self):
        task, st = self.setup("words")
        want = task.prompt.split("'")[1]
        buttons = {b.text(): b for b in self.win.area.findChildren(QPushButton)}
        other = next(n for n in buttons if n != want)
        buttons[other].click()
        self.assertEqual((st["wrong"], st["done"].is_set()), (1, False))
        buttons[want].click()
        self.assertTrue(st["done"].is_set())
        self.assertEqual(st["note"], f"clicked {other}")      # the wrong try is still said

    def test_small_dot(self):
        task, st = self.setup("small")
        c = [w for w in self.win.area.findChildren(QWidget) if hasattr(w, "red")][-1]
        QTest.mouseClick(c, Qt.LeftButton, pos=QPoint(5, 5))
        self.assertEqual(st["wrong"], 1)
        QTest.mouseClick(c, Qt.LeftButton, pos=QPoint(int(c.red[0] * c.width()), int(c.red[1] * c.height())))
        self.assertTrue(st["done"].is_set())

    def test_type_and_enter(self):
        task, st = self.setup("type")
        edit = self.win.area.findChildren(QLineEdit)[0]
        QTest.keyClicks(edit, "Glowy Ball")
        QTest.keyClick(edit, Qt.Key_Return)
        self.assertEqual(st["wrong"], 1)
        QTest.keyClicks(edit, " Bedtime")
        QTest.keyClick(edit, Qt.Key_Return)
        self.assertTrue(st["done"].is_set())

    def test_game_menu_has_no_buttons_inside(self):
        task, st = self.setup("game")
        c = [w for w in self.win.area.findChildren(QWidget) if hasattr(w, "spots")][-1]
        self.assertEqual(c.findChildren(QPushButton), [])     # painted, like a game: accessibility sees nothing
        rects = {text: r for r, text, _ in c.spots}
        p = rects["UPGRADE"].center().toPoint()
        QTest.mouseClick(c, Qt.LeftButton, pos=p)
        self.assertEqual(st["wrong"], 1)
        QTest.mouseClick(c, Qt.LeftButton, pos=rects["SELL ALL"].center().toPoint())
        self.assertTrue(st["done"].is_set())

    def test_slider_and_drag(self):
        task, st = self.setup("slider")
        self.win.area.findChildren(QSlider)[0].setValue(70)
        self.assertTrue(st["done"].is_set())
        task, st = self.setup("drag")
        c = [w for w in self.win.area.findChildren(QWidget) if hasattr(w, "st")][-1]
        start, goal = c.st["box"].center().toPoint(), c.st["target"].center().toPoint()
        QTest.mousePress(c, Qt.LeftButton, pos=start)
        from PySide6.QtGui import QMouseEvent

        ev = QMouseEvent(QMouseEvent.MouseMove, QPointF(goal), QPointF(c.mapToGlobal(goal)), Qt.NoButton,
                         Qt.LeftButton, Qt.NoModifier)
        QApplication.sendEvent(c, ev)
        QTest.mouseRelease(c, Qt.LeftButton, pos=goal)
        self.assertTrue(st["done"].is_set())

    def test_chop_the_tree_next_to_your_character(self):
        _, st = self.setup("chop")
        c = [w for w in self.win.area.findChildren(QWidget) if hasattr(w, "trees")][-1]
        clock = [100.0]

        def hold(x, y, seconds):
            QTest.mousePress(c, Qt.LeftButton, pos=QPoint(int(x), int(y)))
            clock[0] += seconds
            QTest.mouseRelease(c, Qt.LeftButton, pos=QPoint(int(x), int(y)))

        (nx, nh), (ox, oh) = c.trees[0], c.trees[1]
        self.assertLess(abs(nx - c.me), 60)                                # and the tree right next to it
        with mock.patch.object(cubench.time, "time", lambda: clock[0]):
            hold(nx + 30, c.ground - nh / 2, 1.2)                          # beside the trunk: a miss, measured
            self.assertEqual((st["wrong"], st["note"]),
                             (1, "held +30 px across and +0 px down from the middle of the right trunk"))
            hold(ox, c.ground - oh / 2, 1.2)
            self.assertEqual(st["note"], "held on the tree next to the other person")
            hold(nx, c.ground - nh / 2, 0.3)
            self.assertIn("held only 0.3 s", st["note"])
            hold(nx + 5, c.ground - 20, 1.0)
        self.assertTrue(st["done"].is_set())

    def test_held_key_counts_only_long_enough(self):
        task, st = self.setup("hold")
        clock = [100.0]
        with mock.patch.object(cubench.time, "time", lambda: clock[0]):
            QTest.keyPress(self.win, Qt.Key_D)
            clock[0] += 0.3
            QTest.keyRelease(self.win, Qt.Key_D)
            self.assertEqual((st["wrong"], st["note"]), (1, "held D for 0.3 s"))
            QTest.keyPress(self.win, Qt.Key_D)
            clock[0] += 2.0
            QTest.keyRelease(self.win, Qt.Key_D)
        self.assertTrue(st["done"].is_set())


class FakeAgent:
    """Does the task the quick way, telling the UI what a real one would."""
    mode = "do"

    def __init__(self, settings, ui):
        self.settings, self.ui = settings, ui
        self.stop = threading.Event()
        self.client = type("C", (), {"model": "fake-model"})()

    def run(self, prompt):
        self.ui.assistant_start()
        self.ui.reasoning("I should click it.")
        time.sleep(0.05)
        self.ui.assistant_end(Turn(usage={"prompt_tokens": 1200, "completion_tokens": 30}))
        self.ui.tool_start("1", "click_text", {"text": "x"}, "")
        win = FakeAgent.window
        if FakeAgent.mode == "do":
            if "http://" in prompt:
                url = prompt.split("Open ")[1].split(" in a")[0]
                data = urllib.parse.urlencode({"name": "Aidan", "colour": "Blue", "pumpkins": "yes"}).encode()
                urllib.request.urlopen(url + "done", data, timeout=5).read()
            else:
                win.call(lambda: win.done())
        self.ui.tool_end("1", "click_text", ToolResult("Clicked.", images=[b"png"]))
        self.stop.wait(5 if FakeAgent.mode == "idle" else 0)


class RunTest(unittest.TestCase):
    def run_bench(self, **kw):
        win = cubench.make_window(1)
        win.show()
        FakeAgent.window = win
        self.status_seen = []
        real = win.show_status
        win.show_status = lambda s: (self.status_seen.append(s), real(s))
        box = {}
        th = threading.Thread(target=lambda: box.update(cubench.run(Settings(), win, agent_factory=FakeAgent, **kw)))
        th.start()
        while th.is_alive():
            pump(0.02)
        win.close()
        return box

    def test_a_run_times_everything_and_saves_it(self):
        FakeAgent.mode = "do"
        out = self.run_bench(variants=("as set", "helpers off"), tasks=["words", "web", "game"])
        self.assertEqual([v["name"] for v in out["variants"]], ["as set", "helpers off"])
        first = out["variants"][0]
        self.assertEqual([r["ok"] for r in first["tasks"]], [True, True, True])
        self.assertEqual(first["summary"]["done"], 3)
        r = first["tasks"][0]
        self.assertEqual((r["steps"], r["prompt_tokens"], r["images"], r["tools_used"]), (1, 1200, 1, ["click_text"]))
        self.assertEqual(r["turns"][0]["reasoning"], "I should click it.")
        self.assertEqual(out["model"], "fake-model")
        saved = json.loads(Path(out["path"]).read_text())
        self.assertEqual(saved["variants"][1]["overrides"]["browser_tools"], False)
        self.assertIn("as set: 3 of 3 done", out["text"])
        self.assertNotIn("Fill in", self.status_seen[-1])      # the window says nothing that hints how
        self.assertIn("✓ Fill in and send a web form", out["text"])

    def test_out_of_time(self):
        FakeAgent.mode = "idle"
        out = self.run_bench(tasks=["slider"], timeout=0.5)
        r = out["variants"][0]["tasks"][0]
        self.assertFalse(r["ok"])
        self.assertTrue(r["timed_out"])
        self.assertIn("✗ Set a slider to 70", out["text"])
        self.assertIn("out of time", out["text"])


if __name__ == "__main__":
    unittest.main()
