"""Watch mode: a side conversation that looks at the newest screenshot and runs short action lines,
with no tool calls and no thinking, and keeps only the newest picture."""

import json
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
from localagent.config import DATA_DIR, Settings  # noqa: E402
from localagent.tools import ToolContext, enabled_tools, live, screen  # noqa: E402
from tests.mockserver import MockServer, calls, text  # noqa: E402
from tests.test_computer_use import FakeMSS, fake_pyautogui  # noqa: E402


def images(msg) -> int:
    c = msg.get("content")
    return sum(1 for p in c if p.get("type") == "image_url") if isinstance(c, list) else 0


class UI(AgentUI):
    def __init__(self):
        self.results = []

    def tool_end(self, call_id, name, result):
        self.results.append((name, result))


class WatchModeTest(unittest.TestCase):
    def setUp(self):
        self.log = []
        mods = mock.patch.dict(sys.modules, {"mss": types.SimpleNamespace(MSS=FakeMSS),
                                             "pyautogui": fake_pyautogui(self.log)})
        mods.start()
        self.addCleanup(mods.stop)
        sleep = mock.patch("localagent.tools.screen.time.sleep")
        sleep.start()
        self.addCleanup(sleep.stop)

    def agent(self, replies, **kw):
        srv = MockServer(replies)
        self.addCleanup(srv.close)
        kw = {"auto_memory": False, "auto_recall": False, "computer_use": True, "coord_mode": "normalized",
              "monitor": 2, "thinking": "medium", **kw}
        ui = UI()
        return Agent(Settings(base_url=srv.url, workspace=tempfile.mkdtemp(), **kw), ui), ui, srv

    def test_action_lines_are_read_forgivingly(self):
        reply = ("```\n1. click 500 300 hold 1.5\n- key w hold 0.5\nAction: turn 200 -10\nI think the tree is left\n"
                 "press ctrl+s\ntype \"hello there\"\nscroll -3\nwait 1\ndone the tree fell\n```")
        self.assertEqual(live.parse(reply), [
            ("click", "500 300 hold 1.5"), ("key", "w hold 0.5"), ("turn", "200 -10"), ("key", "ctrl+s"),
            ("type", '"hello there"'), ("scroll", "-3"), ("wait", "1"), ("done", "the tree fell")])

    def test_it_acts_from_short_lines_and_keeps_only_the_newest_picture(self):
        agent, ui, srv = self.agent([
            calls(("watch_mode", {"goal": "hit the target", "seconds": 30})),
            text("click 500 500\nkey w hold 0.2\nwait 0.2"),
            text("move 250 250\nclick 600 200 right"),
            text("done the target is gone"),
            text("Finished.")])
        self.assertEqual(agent.run("play the game"), "Finished.")
        # 500 on the 0-1000 grid of screen 2 (2560 wide, from x=1920) is its middle
        self.assertEqual(self.log[:5], [("move", 3200, 720), ("down", "left"), ("up", "left"),
                                        ("keydown", "w"), ("keyup", "w")])
        self.assertIn(("down", "right"), self.log)
        watch = srv.requests[1:4]
        for r in watch:
            self.assertNotIn("tools", r)                                  # plain lines, no tool calls
            self.assertFalse(r["chat_template_kwargs"]["enable_thinking"])
            self.assertIn("watch mode", r["messages"][0]["content"])
            self.assertEqual(len(r["messages"]), 2)                     # sent fresh each look
            self.assertEqual(sum(images(m) for m in r["messages"]), 1)  # only the newest picture
        # earlier looks come as text; the short wait at the end was left out
        self.assertIn("- you: click 500 500 / key w hold 0.2 / wait 0.2 -> clicked 500 500; pressed w (held 0.2s)\n",
                      watch[1]["messages"][-1]["content"][0]["text"] + "\n")
        self.assertIn("move 250 250", watch[2]["messages"][-1]["content"][0]["text"])
        self.assertTrue(watch[2]["messages"][-1]["content"][0]["text"].startswith("Goal: hit the target"))
        name, result = ui.results[-1]
        self.assertEqual(name, "watch_mode")
        self.assertIn("the goal is reached: the target is gone", result.text)
        self.assertIn("3 looks", result.text)
        self.assertEqual(len(result.images), 1)                           # its last look goes to the chat
        self.assertEqual(srv.requests[-1]["chat_template_kwargs"]["enable_thinking"], True)   # the chat as set
        saved = json.loads((DATA_DIR / "watch" / "latest.json").read_text(encoding="utf-8"))
        self.assertEqual([len(x["did"]) for x in saved["looks"]], [2, 2, 0])
        self.assertTrue(agent.ctx.capture_after)

    def test_bad_lines_are_reported_and_replies_without_actions_end_it(self):
        agent, ui, srv = self.agent([
            calls(("watch_mode", {"goal": "do it"})),
            text("click here"), text("I am not sure"), text("hmm"), text("Ok."), text("Ok.")])
        agent.run("go")
        self.assertIn("'click here' failed: click needs 2 number(s)", srv.requests[2]["messages"][-1]["content"][0]["text"])
        self.assertIn("No action lines in your last reply", srv.requests[3]["messages"][-1]["content"][-1]["text"])
        result = dict(ui.results)["watch_mode"]
        self.assertIn("gave no actions three times", result.text)
        self.assertFalse(result.ok)

    def test_it_can_be_turned_off_and_the_prompt_mentions_it(self):
        on = Settings(computer_use=True)
        names = {t.name for t in enabled_tools(on)}
        self.assertIn("watch_mode", names)
        self.assertIn("use watch_mode", prompts.build_system_prompt(on, Path("."), enabled_tools(on), "cmd", False))
        off = Settings(computer_use=True, watch_tool=False)
        self.assertNotIn("watch_mode", {t.name for t in enabled_tools(off)})
        self.assertNotIn("watch_mode", prompts.build_system_prompt(off, Path("."), enabled_tools(off), "cmd", False))

    def test_its_screenshots_have_their_own_size(self):
        ctx = ToolContext(settings=Settings(), workspace=Path("."))
        self.assertEqual(screen.fit_size(ctx, 2560, 1440), (960, 512))                      # the chat's
        self.assertEqual(screen.fit_size(ctx, 2560, 1440, squares=live.SIZES["small"]), (672, 384))
        self.assertEqual(screen.fit_size(ctx, 2560, 1440, squares=live.SIZES["sharp"]), (1344, 768))


if __name__ == "__main__":
    unittest.main()
