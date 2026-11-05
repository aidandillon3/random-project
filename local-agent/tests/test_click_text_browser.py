"""Clicking by words (Windows text recognition, a stand-in here), numbered marks, and the browser
it drives directly (a real headless Chromium when Playwright is installed)."""

import http.server
import os
import sys
import tempfile
import threading
import types
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("LOCALAGENT_DATA", tempfile.mkdtemp())
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from localagent.config import Settings  # noqa: E402
from localagent.tools import REGISTRY, ToolContext, ToolError, enabled_tools, ocr  # noqa: E402
from tests.test_computer_use import FakeMSS, fake_pyautogui  # noqa: E402


def line(*words):
    """ocr.Line from (text, left, top, right, bottom) words."""
    return ocr._line(list(words))


# what text recognition "reads" off screen 2 (2560x1440, at 1920,0): a game's menu
MENU = [line(("WORKSHOP", 960, 60, 1140, 100), ("UPGRADE", 1250, 60, 1420, 100), ("SELL", 1530, 60, 1620, 100)),
        line(("SELL", 1400, 590, 1470, 630), ("ALL", 1485, 590, 1550, 630)),
        line(("Play", 100, 300, 180, 330)),
        line(("Play", 100, 900, 180, 930)),
        line(("This", 100, 1200, 150, 1230), ("is", 160, 1200, 180, 1230), ("a", 190, 1200, 200, 1230),
             ("long", 210, 1200, 260, 1230), ("sentence", 270, 1200, 360, 1230), ("of", 370, 1200, 390, 1230),
             ("text", 400, 1200, 450, 1230))]


class OcrMatchTest(unittest.TestCase):
    def test_a_row_of_buttons_is_split_into_its_buttons(self):
        parts = [ln.text for ln in ocr.split_wide(MENU[:1])]
        self.assertEqual(parts, ["WORKSHOP", "UPGRADE", "SELL"])

    def test_finding_words_exactly_inside_and_misread(self):
        lines = ocr.split_wide(MENU)
        best = ocr.find(lines, "sell all")[0]
        self.assertEqual((best[0], best[1].text), (1.0, "SELL ALL"))
        self.assertEqual(ocr.find(lines, "SELL")[0][1].text, "SELL")          # the button on its own first
        misread = ocr.find([line(("SELI", 0, 0, 40, 20), ("ALL", 50, 0, 80, 20))], "Sell All")
        self.assertTrue(misread and 0.7 < misread[0][0] < 1.0)
        self.assertEqual(ocr.find(lines, "Quit game"), [])


class ScreenWordsTest(unittest.TestCase):
    def setUp(self):
        self.log = []
        for p in (mock.patch.dict(sys.modules, {"mss": types.SimpleNamespace(MSS=FakeMSS),
                                                "pyautogui": fake_pyautogui(self.log)}),
                  mock.patch("localagent.tools.screen.time.sleep"),
                  mock.patch.object(ocr, "missing", lambda: None),
                  mock.patch.object(ocr, "read", lambda img: [ocr._line(list(ln.words)) for ln in MENU])):
            p.start()
            self.addCleanup(p.stop)
        self.ctx = ToolContext(settings=Settings(computer_use=True, monitor=2, screenshot_after_action=False),
                               workspace=Path("."))

    def test_click_text_clicks_the_middle_of_the_words(self):
        out = REGISTRY["click_text"].func(self.ctx, {"text": "sell all"})
        # screen 2 starts at x=1920; the words span 1400-1550 x 590-630 of it
        self.assertEqual(self.log[:3], [("move", 1920 + 1475, 610), ("down", "left"), ("up", "left")])
        self.assertIn("'SELL ALL'", out.text)

    def test_the_same_words_twice_and_nth(self):
        out = REGISTRY["click_text"].func(self.ctx, {"text": "Play", "nth": 2})
        self.assertEqual(self.log[0], ("move", 1920 + 140, 915))
        self.assertIn("2 matches", out.text)
        with self.assertRaises(ToolError):
            REGISTRY["click_text"].func(self.ctx, {"text": "Play", "nth": 3})

    def test_words_that_arent_there_say_what_is(self):
        with self.assertRaises(ToolError) as e:
            REGISTRY["click_text"].func(self.ctx, {"text": "Quit"})
        self.assertIn("WORKSHOP", str(e.exception))
        self.assertEqual(self.log, [])

    def test_marks_number_short_text_and_click_by_number(self):
        out = REGISTRY["screenshot"].func(self.ctx, {"marks": True})
        marks = self.ctx.screen["marks"]
        labels = [m["label"] for m in marks]
        self.assertEqual(labels, ["WORKSHOP", "UPGRADE", "SELL", "SELL ALL", "Play", "Play"])   # no long sentence
        self.assertIn("4 SELL ALL", out.text)
        self.log.clear()
        REGISTRY["click"].func(self.ctx, {"mark": 4})
        self.assertEqual(self.log[0], ("move", 1920 + 1475, 610))
        with self.assertRaises(ToolError):
            REGISTRY["click"].func(self.ctx, {"mark": 40})
        with self.assertRaises(ToolError):
            REGISTRY["click"].func(self.ctx, {})            # neither x, y nor a mark

    def test_screen_settles_early_when_nothing_moves(self):
        from localagent.tools import screen

        grabs = []
        real = screen._grab
        with mock.patch.object(screen, "_grab", lambda *a: grabs.append(1) or real(*a)):
            screen._settle(1)
        self.assertEqual(len(grabs), 2)       # two the same: done, not the whole 0.6 s


class ToolListTest(unittest.TestCase):
    def test_tools_that_cant_work_cost_no_tokens(self):
        with mock.patch.object(ocr, "missing", lambda: "no winrt"):
            names = {t.name for t in enabled_tools(Settings(computer_use=True))}
        self.assertNotIn("click_text", names)
        with mock.patch.object(ocr, "missing", lambda: None):
            names = {t.name for t in enabled_tools(Settings(computer_use=True, browser_tools=False))}
        self.assertIn("click_text", names)
        self.assertFalse(any(n.startswith("browser_") for n in names))
        self.assertFalse(any(n.startswith("browser_") or n == "click_text" for n in
                             {t.name for t in enabled_tools(Settings(computer_use=False))}))


FORM = b"""<!doctype html><title>Order</title>
<form method=post action=/done>
<label>Name <input name=name></label>
<label>Colour <select name=colour><option>Red</option><option>Blue</option></select></label>
<label><input type=checkbox name=pumpkins value=yes> I like pumpkins</label>
<button>Submit</button></form><a href="/other">Another page</a> <a href="/other" target=_blank>In a new tab</a>"""


def _chromium():
    import glob

    for p in glob.glob("/opt/pw-browsers/chromium-*/chrome-linux/chrome"):
        return p
    return None


@unittest.skipUnless(__import__("importlib").util.find_spec("playwright") and _chromium(),
                     "Playwright or Chromium not installed")
class BrowserTest(unittest.TestCase):
    def setUp(self):
        got = self.got = []

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                self.end_headers()
                self.wfile.write(FORM if self.path == "/" else b"<title>Other</title><p>The other page.</p>")

            def do_POST(self):
                got.append(self.rfile.read(int(self.headers["Content-Length"])).decode())
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                self.end_headers()
                self.wfile.write(b"<title>Thanks</title><p>Order received.</p>")

            def log_message(self, *a):
                pass

        self.srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.addCleanup(self.srv.shutdown)
        env = mock.patch.dict(os.environ, {"LOCALAGENT_BROWSER_EXE": _chromium(), "LOCALAGENT_BROWSER_HEADLESS": "1"})
        env.start()
        self.addCleanup(env.stop)
        from localagent.tools import browser

        self.addCleanup(lambda: browser.BROWSER.call(lambda b: b.close()))
        self.ctx = ToolContext(settings=Settings(computer_use=True), workspace=Path("."))

    def test_fill_and_send_a_form_by_numbers(self):
        url = f"http://127.0.0.1:{self.srv.server_address[1]}/"
        page = REGISTRY["browser_open"].func(self.ctx, {"url": url})
        self.assertIn("Page: Order", page)
        refs = {}
        for ln in page.splitlines():
            if ln.startswith("["):
                n, rest = ln[1:].split("] ", 1)
                refs[rest.split(" '")[0] + " " + rest.split("'")[1]] = int(n)
        REGISTRY["browser_type"].func(self.ctx, {"ref": refs["textbox Name"], "text": "Aidan"})
        REGISTRY["browser_type"].func(self.ctx, {"ref": refs["select Colour"], "text": "Blue"})
        after = REGISTRY["browser_click"].func(self.ctx, {"ref": refs["checkbox I like pumpkins"]})
        self.assertIn("I like pumpkins' (ticked)", after)
        done = REGISTRY["browser_click"].func(self.ctx, {"text": "Submit"})
        self.assertIn("Order received", done)
        self.assertEqual(self.got, ["name=Aidan&colour=Blue&pumpkins=yes"])
        back = REGISTRY["browser_open"].func(self.ctx, {"url": "back"})
        self.assertIn("Page: Order", back)
        with self.assertRaises(ToolError):
            REGISTRY["browser_click"].func(self.ctx, {"ref": 99})
        shot = REGISTRY["browser_screenshot"].func(self.ctx, {})
        self.assertEqual(len(shot.images), 1)

    def test_other_tabs_dont_take_over_but_its_own_new_tabs_do(self):
        from localagent.tools import browser

        url = f"http://127.0.0.1:{self.srv.server_address[1]}/"
        REGISTRY["browser_open"].func(self.ctx, {"url": url})
        # a tab the browser opens by itself (Edge restoring yesterday's tabs): the page it reads stays
        browser.BROWSER.call(lambda b: b.context.new_page().goto("about:blank"))
        self.assertIn("Page: Order", REGISTRY["browser_read"].func(self.ctx, {}))
        # a link on its page that opens a new tab: it carries on there
        after = REGISTRY["browser_click"].func(self.ctx, {"text": "In a new tab"})
        self.assertIn("The other page", after + REGISTRY["browser_read"].func(self.ctx, {}))


if __name__ == "__main__":
    unittest.main()
