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
        # (the fake screen never changes, which it is told)
        self.assertIn("- clicked 500 500; pressed w (held 0.2s); the screen looks the same as before it\n",
                      watch[1]["messages"][-1]["content"][0]["text"] + "\n")
        self.assertIn("- moved to 250 250", watch[2]["messages"][-1]["content"][0]["text"])
        self.assertTrue(watch[2]["messages"][-1]["content"][0]["text"].startswith("Goal: hit the target"))
        name, result = ui.results[-1]
        self.assertEqual(name, "watch_mode")
        self.assertIn("the goal is reached: the target is gone", result.text)
        self.assertIn("3 looks", result.text)
        self.assertEqual(len(result.images), 1)                           # its last look goes to the chat
        self.assertEqual(srv.requests[-1]["chat_template_kwargs"]["enable_thinking"], True)   # the chat as set
        saved = json.loads((DATA_DIR / "watch" / "latest.json").read_text(encoding="utf-8"))
        self.assertEqual([len(x["did"]) for x in saved["looks"]], [3, 3, 0])    # + "the screen looks the same"
        self.assertTrue(agent.ctx.capture_after)

    def test_a_goal_from_a_bigger_screenshot_is_rescaled_and_the_chat_gets_its_own_size_back(self):
        agent, ui, srv = self.agent([
            calls(("screenshot", {})),
            calls(("watch_mode", {"goal": "hold a click on the trunk at x=625"})),
            text("click 400 200"), text("done"), text("Ok.")], coord_mode="pixels", watch_size="small")
        agent.run("chop")
        first = srv.requests[2]["messages"][-1]["content"][0]["text"]
        self.assertIn("Positions in this goal are of a 960x512 screenshot; yours is 672x384, so multiply them by 0.70",
                      first)
        name, result = ui.results[-1]
        self.assertIn("Screen 2, image 960x512", result.text)
        self.assertEqual(agent.ctx.screen["img_w"], 960)

    def test_fewer_earlier_looks_are_told_once_a_look_reads_too_much(self):
        hist = [f"- clicked {i} {i}" for i in range(6)]
        self.assertEqual(live._messages("s", "g", hist, [], "t")[1]["content"][0]["text"].count("- clicked"), 4)
        self.assertEqual(live._messages("s", "g", hist, [], "t", keep=1)[1]["content"][0]["text"].count("- clicked"), 1)
        self.assertNotIn("last looks", live._messages("s", "g", hist, [], "t", keep=0)[1]["content"][0]["text"])

    def test_actions_on_one_line_or_copied_from_results_are_split(self):
        self.assertEqual(live.parse("key w hold 0.8 / wait 0.3 / click 480 270 hold 0.9 / key e"), [
            ("key", "w hold 0.8"), ("wait", "0.3"), ("click", "480 270 hold 0.9"), ("key", "e")])
        self.assertEqual(live.parse("- pressed w (held 1.2s); clicked 480 270 (held 0.9s); turned -30 0"), [
            ("key", "w (held 1.2s)"), ("click", "480 270 (held 0.9s)"), ("turn", "-30 0")])

    def test_a_click_without_a_position_doesnt_move_the_mouse_and_esc_ends_the_reply(self):
        agent, ui, srv = self.agent([
            calls(("watch_mode", {"goal": "break the log"})),
            text("click hold 0.5\nkey esc\nturn 100 0"), text("done"), text("Ok.")])
        sys.modules["pyautogui"].KEYBOARD_KEYS.append("esc")
        agent.run("go")
        self.assertEqual([x for x in self.log if x[0] == "move"], [])           # no pointer move: no camera turn
        self.assertIn(("down", "left"), self.log)
        first = srv.requests[2]["messages"][-1]["content"][0]["text"]
        self.assertIn("- clicked here (held 0.5s); pressed esc; the rest of that reply was left out: look after Esc",
                      first)
        self.assertNotIn("turned", first)

    def test_its_own_model_is_used_with_the_grid_or_the_chats_when_it_cant_be(self):
        from localagent import eyes

        agent, ui, srv = self.agent([calls(("watch_mode", {"goal": "go"})), text("done"), text("Ok.")],
                                    watch_model="eyes", coord_mode="pixels")
        with mock.patch.object(eyes, "files", lambda s: (None, None, ["x"])), \
                mock.patch.object(eyes.Eyes, "_ours", lambda self, s: False):
            agent.run("go")
        self.assertIn("the chat's model looked: Watch mode's own model isn't downloaded", dict(ui.results)["watch_mode"].text)
        other = MockServer([text("click 500 500"), text("done")])
        self.addCleanup(other.close)
        from localagent.llm import LLMClient
        agent, ui, srv = self.agent([calls(("watch_mode", {"goal": "go"})), text("Ok.")],
                                    watch_model="eyes", coord_mode="pixels")
        with mock.patch.object(eyes.EYES, "client", lambda *a, **k: LLMClient(other.url, model="Qwen3.5-4B-Q6_K.gguf")):
            agent.run("go")
        self.assertIn("0-1000 grid", other.requests[0]["messages"][0]["content"])
        self.assertEqual(other.requests[0]["temperature"], 0.3)
        self.assertEqual(self.log[0], ("move", 3200, 720))          # 500 500 on the grid: the middle of screen 2
        self.assertIn("It looked with its own model (Qwen3.5 4B)", dict(ui.results)["watch_mode"].text)
        self.assertNotIn("coords_for_now", agent.ctx.screen)

    def test_a_screen_that_didnt_change_is_told_apart_from_one_that_did(self):
        import io

        from PIL import Image

        def png(colour):
            out = io.BytesIO()
            Image.new("RGB", (960, 512), colour).save(out, "PNG")
            return out.getvalue()

        self.assertLess(live._changed(png((10, 120, 10)), png((10, 120, 10))), live.STILL)
        self.assertGreater(live._changed(png((10, 120, 10)), png((90, 60, 200))), live.STILL)
        self.assertEqual(live._let_go("Clicked. But Windows says the button was let go after 0.4 s of the 3 s hold"),
                         " but it was let go after 0.4s")

    def test_bad_lines_are_reported_and_replies_without_actions_end_it(self):
        agent, ui, srv = self.agent([
            calls(("watch_mode", {"goal": "do it"})),
            text("click 12"), text("I am not sure"), text("hmm"), text("Ok."), text("Ok.")])
        agent.run("go")
        self.assertIn("'click 12' failed: click needs 2 number(s)", srv.requests[2]["messages"][-1]["content"][0]["text"])
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
