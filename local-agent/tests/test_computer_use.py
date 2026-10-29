"""Computer use: the screen, mouse and keyboard tools are there only while the toggle is on, and
then never ask. The screen and the mouse are stand-ins here (no desktop in the tests)."""

import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("LOCALAGENT_DATA", tempfile.mkdtemp())
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from localagent import prompts  # noqa: E402
from localagent.agent import Agent, AgentUI  # noqa: E402
from localagent.config import Settings  # noqa: E402
from localagent.permissions import Decision, Policy  # noqa: E402
from localagent.tools import CONTROL, REGISTRY, RUN, ToolContext, enabled_tools  # noqa: E402
from tests.mockserver import MockServer, calls, text  # noqa: E402

SCREENS = [{"left": 0, "top": 0, "width": 4480, "height": 1440},          # mss: 0 = all of them
           {"left": 0, "top": 0, "width": 1920, "height": 1080},
           {"left": 1920, "top": 0, "width": 2560, "height": 1440}]


class FakeMSS:
    monitors = SCREENS

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def grab(self, box):
        w, h = box["width"], box["height"]
        return types.SimpleNamespace(size=(w, h), rgb=b"\x80" * (w * h * 3))


class FailSafeException(Exception):
    pass


def fake_pyautogui(log, fail=False):
    m = types.ModuleType("pyautogui")
    m.KEYBOARD_KEYS = ["ctrl", "s", "enter", "alt", "tab"]
    m.position = lambda: (2000, 100)

    def click(x, y, **kw):
        if fail:
            raise FailSafeException("corner")
        log.append(("click", x, y))
    m.click = click
    m.moveTo = lambda x, y, **kw: log.append(("move", x, y))
    m.write = lambda t, **kw: log.append(("write", t))
    m.press = lambda k: log.append(("press", k))
    m.hotkey = lambda *k: log.append(("hotkey",) + k)
    return m


class AskUI(AgentUI):
    def __init__(self):
        self.asked, self.results = [], []

    def approve(self, req):
        self.asked.append(req.tool)
        return Decision(True)

    def tool_end(self, call_id, name, result):
        self.results.append((name, result))


class ComputerUseTest(unittest.TestCase):
    def setUp(self):
        self.log = []
        self.mods = mock.patch.dict(sys.modules, {"mss": types.SimpleNamespace(MSS=FakeMSS),
                                                  "pyautogui": fake_pyautogui(self.log)})
        self.mods.start()
        self.addCleanup(self.mods.stop)
        sleep = mock.patch("localagent.tools.screen.time.sleep")
        sleep.start()
        self.addCleanup(sleep.stop)

    def agent(self, replies, **kw):
        srv = MockServer(replies)
        self.addCleanup(srv.close)
        kw = {"auto_memory": False, "auto_recall": False, "approval_mode": "ask", **kw}
        ui = AskUI()
        return Agent(Settings(base_url=srv.url, workspace=tempfile.mkdtemp(), **kw), ui), ui, srv

    def test_off_by_default_and_the_prompt_says_so(self):
        s = Settings()
        self.assertFalse(s.computer_use)
        names = {t.name for t in enabled_tools(s)}
        self.assertFalse(names & {"screenshot", "click", "type_text"})
        text_ = prompts.build_system_prompt(s, Path("."), enabled_tools(s), "powershell", False)
        self.assertIn("You can't see or use the user's screen", text_)
        self.assertNotIn("Computer use is on", text_)

    def test_on_gives_the_tools_to_the_main_agent_only(self):
        s = Settings(computer_use=True)
        names = {t.name for t in enabled_tools(s)}
        self.assertTrue({"screenshot", "click", "type_text", "press_keys", "scroll", "drag", "open"} <= names)
        self.assertNotIn("click", {t.name for t in enabled_tools(s, depth=1)})   # helpers run side by side
        text_ = prompts.build_system_prompt(s, Path("."), enabled_tools(s), "powershell", False, "Grid help.")
        self.assertIn("Computer use is on", text_)
        self.assertIn("Grid help.", text_)
        self.assertNotIn("You can't see", text_)

    def test_clicks_never_ask_but_program_files_and_read_only_do(self):
        for mode in ("ask", "auto_edit", "auto"):
            self.assertFalse(Policy(mode=mode).needs_approval(CONTROL, dangerous=True), mode)
        self.assertIsNone(Policy(mode="read_only").needs_approval(CONTROL, False))
        ctx = ToolContext(settings=Settings(), workspace=Path("."))
        risk = REGISTRY["open"].risk_for
        self.assertEqual(risk(ctx, {"target": "C:/Games/setup.exe"}), RUN)
        self.assertEqual(risk(ctx, {"target": "start server.bat"}), RUN)
        self.assertEqual(risk(ctx, {"target": "https://example.com"}), CONTROL)
        self.assertTrue(Policy(mode="ask").needs_approval(RUN, False))

    def test_screenshot_click_and_type_on_the_second_screen_without_asking(self):
        agent, ui, srv = self.agent([
            calls(("screenshot", {"monitor": 2})),
            calls(("click", {"x": 500, "y": 500}), ("type_text", {"text": "hello", "enter": True})),
            text("Done.")], computer_use=True, coord_mode="normalized")
        self.assertEqual(agent.run("type hello on my second screen"), "Done.")
        self.assertEqual(ui.asked, [])
        # 500 on the 0-1000 grid of screen 2 (2560 wide, starting at x=1920) is its middle
        self.assertEqual(self.log, [("click", 3200, 720), ("write", "hello"), ("press", "enter")])
        shots = dict(ui.results)
        self.assertIn("Screen 2: 2560x1440", shots["screenshot"].text)
        self.assertIn("Screens: 1 = 1920x1080 (main), 2 = 2560x1440", shots["screenshot"].text)
        self.assertEqual(len(shots["screenshot"].images), 1)
        # of the two actions in one step only the last waits and takes a screenshot
        self.assertEqual([len(r.images) for n, r in ui.results if n in ("click", "type_text")], [0, 1])
        system = srv.requests[0]["messages"][0]["content"]
        self.assertIn("Computer use is on", system)
        self.assertIn("0-1000 grid", system)

    def test_turning_it_off_takes_the_tools_away(self):
        agent, ui, srv = self.agent([text("hi"), calls(("click", {"x": 1, "y": 1})), text("ok")],
                                    computer_use=True)
        agent.run("hello")
        self.assertIn("click", srv.requests[-1]["tools"].__str__())
        agent.settings.computer_use = False
        agent.run("click something")
        tools = [t["function"]["name"] for t in srv.requests[-2]["tools"]]
        self.assertNotIn("click", tools)
        self.assertIn("You can't see or use the user's screen", srv.requests[-2]["messages"][0]["content"])
        self.assertEqual(self.log, [])     # the click the model still sent was not done
        self.assertIn("no tool called 'click'", str(agent.messages[-2]["content"]))

    def test_turned_off_mid_task_stops_the_next_click(self):
        agent, ui, _ = self.agent([calls(("click", {"x": 1, "y": 1}), ("click", {"x": 2, "y": 2})), text("ok")],
                                  computer_use=True)

        def end(call_id, name, result):
            ui.results.append((name, result))
            agent.settings.computer_use = False      # the user presses the button after the first click
        ui.tool_end = end
        agent.run("click twice")
        self.assertEqual(len(self.log), 1)
        self.assertIn("turned Computer use off", ui.results[1][1].text)

    def test_mouse_in_a_corner_stops_the_task(self):
        sys.modules["pyautogui"] = fake_pyautogui(self.log, fail=True)
        agent, ui, _ = self.agent([calls(("click", {"x": 1, "y": 1})), text("never")], computer_use=True)
        agent.run("click")
        self.assertTrue(agent.stop.is_set())
        self.assertIn("emergency stop", ui.results[0][1].text)


if __name__ == "__main__":
    unittest.main()
