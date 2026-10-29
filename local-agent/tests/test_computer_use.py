"""Computer use: the screen, mouse and keyboard tools are there only while the toggle is on, and
then never ask. The screen and the mouse are stand-ins here (no desktop in the tests)."""

import os
import sys
import tempfile
import time
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
    m.KEYBOARD_KEYS = ["ctrl", "s", "enter", "alt", "tab", "w", "shift"]
    m.position = lambda: (2000, 100)

    def corner():
        if fail:
            raise FailSafeException("corner")
    m.failSafeCheck = corner
    m.mouseDown = lambda button="left": log.append(("down", button))
    m.mouseUp = lambda button="left": log.append(("up", button))
    m.keyDown = lambda k: log.append(("keydown", k))
    m.keyUp = lambda k: log.append(("keyup", k))
    m.moveRel = lambda x, y: log.append(("rel", x, y))
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
        self.assertEqual(self.log, [("move", 3200, 720), ("down", "left"), ("up", "left"), ("write", "hello"),
                                    ("press", "enter")])
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
        self.assertEqual([e for e in self.log if e[0] == "down"], [("down", "left")])
        self.assertIn("turned Computer use off", ui.results[1][1].text)

    def test_mouse_in_a_corner_stops_the_task(self):
        sys.modules["pyautogui"] = fake_pyautogui(self.log, fail=True)
        agent, ui, _ = self.agent([calls(("click", {"x": 1, "y": 1})), text("never")], computer_use=True)
        agent.run("click")
        self.assertTrue(agent.stop.is_set())
        self.assertIn("emergency stop", ui.results[0][1].text)
        self.assertEqual(self.log[-1], ("up", "left"))      # never left pressed down

    def ctx(self, **kw):
        ctx = ToolContext(settings=Settings(computer_use=True, coord_mode="normalized", **kw), workspace=Path("."))
        REGISTRY["screenshot"].func(ctx, {})
        self.log.clear()
        return ctx

    def test_keys_held_for_games(self):
        ctx = self.ctx(screenshot_after_action=False)
        start = time.time()
        REGISTRY["press_keys"].func(ctx, {"keys": "w+shift", "hold_seconds": 0.3})
        self.assertGreaterEqual(time.time() - start, 0.3)
        self.assertEqual(self.log, [("keydown", "w"), ("keydown", "shift"), ("keyup", "shift"), ("keyup", "w")])
        self.assertIn("held 10s", REGISTRY["press_keys"].describe({"keys": "w", "hold_seconds": 10}))

    def test_caps_lock_is_off_while_it_types(self):
        ctx = self.ctx(screenshot_after_action=False)
        with mock.patch("localagent.tools.screen._caps_lock_on", lambda: True):
            REGISTRY["type_text"].func(ctx, {"text": "Glowy Ball"})
        self.assertEqual(self.log, [("keydown", "capslock"), ("keyup", "capslock"), ("write", "Glowy Ball"),
                                    ("keydown", "capslock"), ("keyup", "capslock")])

    def test_camera_turn_is_a_relative_move_in_small_steps(self):
        ctx = self.ctx(screenshot_after_action=False)
        out = REGISTRY["move_mouse_by"].func(ctx, {"dx": 300, "dy": -45})
        moves = [e for e in self.log if e[0] == "rel"]
        self.assertGreater(len(moves), 5)
        self.assertEqual((sum(m[1] for m in moves), sum(m[2] for m in moves)), (300, -45))
        self.assertIn("(300, -45)", out.text)

    def test_click_inside_the_zoomed_view(self):
        ctx = self.ctx(screenshot_after_action=False)
        with self.assertRaises(Exception):
            REGISTRY["click"].func(ctx, {"x": 1, "y": 1, "in_zoom": True})     # no zoom yet
        out = REGISTRY["zoom"].func(ctx, {"x": 500, "y": 500, "width": 100, "height": 100})
        self.assertIn("in_zoom=true", out.text)
        self.log.clear()
        REGISTRY["click"].func(ctx, {"x": 500, "y": 500, "in_zoom": True})
        # the middle of the zoomed box: screen 1 is 1920x1080, the box 960-1152 x 540-648
        self.assertEqual(self.log[0], ("move", 1056, 594))
        self.assertEqual((ctx.screen["img_w"], ctx.screen["img_h"]), (960, 512))
        self.assertEqual(out.images[0][16:24], (576).to_bytes(4, "big") + (320).to_bytes(4, "big"))   # 3x, whole squares

    def test_screenshots_fit_what_the_vision_part_sees_at_any_size(self):
        from localagent.tools.screen import fit_size

        auto = ToolContext(settings=Settings(), workspace=Path("."))
        for w, h in ((1920, 1080), (2560, 1440), (3840, 2160), (3440, 1440)):
            iw, ih = fit_size(auto, w, h)
            self.assertEqual((iw % 32, ih % 32), (0, 0))
            self.assertLessEqual(iw * ih, 512 * 32 * 32)           # the quick size the test picked
            self.assertGreater(iw * ih, 420 * 32 * 32)
            self.assertAlmostEqual(iw / ih, w / h, delta=0.1)
            zw, zh = fit_size(auto, w, h, zoom=True)                # zooms: the most the vision part takes
            self.assertLessEqual(zw * zh, 1024 * 32 * 32)           # never shrunk again by the model server
            self.assertGreater(zw * zh, 900 * 32 * 32)
        self.assertEqual(fit_size(auto, 2560, 1440), (960, 512))    # the same picture as 1920x1080
        self.assertEqual(fit_size(auto, 2560, 1440, zoom=True), (1344, 768))
        self.assertEqual(fit_size(auto, 800, 450), (800, 448))     # a small screen isn't blown up
        raised = ToolContext(settings=Settings(screen_max_side=1344), workspace=Path("."))
        self.assertEqual(fit_size(raised, 2560, 1440), (1344, 736))
        huge = ToolContext(settings=Settings(screen_max_side=3840), workspace=Path("."))
        self.assertEqual(fit_size(huge, 2560, 1440), (1344, 768))   # still under the vision limit

    def test_lowered_size_still_lands_clicks_in_the_right_place(self):
        ctx = self.ctx(screenshot_after_action=False, screen_max_side=640)
        self.assertEqual((ctx.screen["img_w"], ctx.screen["img_h"]), (640, 352))
        ctx.settings.coord_mode = "pixels"
        REGISTRY["click"].func(ctx, {"x": 320, "y": 176})
        self.assertEqual(self.log[0], ("move", 960, 540))      # the middle of the 1920x1080 screen

    def test_follow_up_screenshots_say_their_size_too(self):
        # e.g. the app restarted mid-chat at another size: the first follow-up shot explains it again
        ctx = ToolContext(settings=Settings(computer_use=True), workspace=Path("."))
        first = REGISTRY["press_keys"].func(ctx, {"keys": "w"}).text
        self.assertIn("real pixels", first)
        self.assertIn("960x512", first)
        self.assertIn("Screen 1, image 960x512.", REGISTRY["press_keys"].func(ctx, {"keys": "w"}).text)

    def test_the_pointer_is_drawn_and_said_where_it_is(self):
        # the fake mouse is at 2000, 100: 80, 100 into screen 2 (2560x1440, shown at 960x512)
        ctx = ToolContext(settings=Settings(computer_use=True, monitor=2, coord_mode="pixels"), workspace=Path("."))
        out = REGISTRY["screenshot"].func(ctx, {})
        self.assertIn("Mouse pointer (magenta ring): 30, 36.", out.text)
        import io

        from PIL import Image

        img = Image.open(io.BytesIO(out.images[0])).convert("RGB")
        self.assertEqual(img.getpixel((30 + 7, 36)), (255, 0, 255))     # the ring, at the same size at any scale
        self.assertNotEqual(img.getpixel((30, 36)), (255, 0, 255))      # and the point itself left clear
        ctx.settings.coord_mode = "normalized"
        self.assertIn("Mouse pointer (magenta ring): 31, 69.", REGISTRY["screenshot"].func(ctx, {}).text)
        ctx.settings.coord_mode = "pixels"
        z = REGISTRY["zoom"].func(ctx, {"x": 0, "y": 0, "width": 100, "height": 100})
        self.assertIn("magenta ring) at", z.text)
        far = REGISTRY["zoom"].func(ctx, {"x": 500, "y": 300, "width": 100, "height": 100})
        self.assertNotIn("magenta", far.text)                            # not in that part of the screen
        ctx.settings.monitor = 1                                         # the pointer is on another screen
        self.assertNotIn("Mouse pointer", REGISTRY["screenshot"].func(ctx, {"monitor": 1}).text)

    def test_screenshot_notes_get_short_after_the_first(self):
        ctx = ToolContext(settings=Settings(computer_use=True), workspace=Path("."))
        first = REGISTRY["screenshot"].func(ctx, {}).text
        second = REGISTRY["screenshot"].func(ctx, {}).text
        self.assertIn("real pixels", first)
        self.assertNotIn("real pixels", second)
        self.assertLess(len(second), len(first) / 2)
        third = REGISTRY["screenshot"].func(ctx, {"monitor": 2}).text   # another screen: explained again
        self.assertIn("real pixels", third)

    def test_off_grid_positions_are_explained_not_clamped(self):
        ctx = self.ctx()
        with self.assertRaises(Exception) as e:
            REGISTRY["click"].func(ctx, {"x": 1167, "y": 490})     # pixels of the 1280-wide image, not the grid
        self.assertIn("off the 0-1000 grid", str(e.exception))
        self.assertEqual(self.log, [])
        # a zoom box running past the edge stops at it
        out = REGISTRY["zoom"].func(ctx, {"x": 900, "y": 900, "width": 300, "height": 300})
        self.assertEqual(len(out.images), 1)

    def test_windows_input_goes_through_sendinput(self):
        from localagent.tools import screen, winput

        ctx = self.ctx(screenshot_after_action=False)
        sent = []
        with mock.patch.object(screen, "WINDOWS", True), \
                mock.patch.object(winput, "key", lambda k, up: sent.append(("key", k, up))), \
                mock.patch.object(winput, "button", lambda b, up: sent.append(("button", b, up))), \
                mock.patch.object(winput, "move_by", lambda x, y: sent.append(("by", x, y))), \
                mock.patch.object(winput, "move_to", lambda x, y, s=0: sent.append(("to", x, y))), \
                mock.patch.object(winput, "vk_code", lambda k: 0x57 if k == "w" else None), \
                mock.patch.object(winput, "window_at", lambda x, y: (2, 1, "Rainbow Six")), \
                mock.patch.object(winput, "bring_to_front", lambda h: sent.append(("front", h))):
            out = REGISTRY["click"].func(ctx, {"x": 500, "y": 500, "hold_seconds": 0.1})
            REGISTRY["press_keys"].func(ctx, {"keys": "w"})
            REGISTRY["move_mouse_by"].func(ctx, {"dx": 10, "dy": 0, "seconds": 0})
            with self.assertRaises(Exception) as e:
                REGISTRY["press_keys"].func(ctx, {"keys": "notakey"})
        self.assertEqual(sent, [("to", 960, 540), ("to", 961, 540), ("to", 960, 540), ("front", 2), ("button", "left", False), ("button", "left", True),
                                ("key", "w", False), ("key", "w", True), ("by", 10, 0)])
        self.assertIn("'Rainbow Six' was brought to the front first", out.text)
        self.assertIn("Unknown key", str(e.exception))

    def test_a_hold_that_something_else_lets_go_is_reported(self):
        from localagent.tools import screen, winput

        ctx = self.ctx(screenshot_after_action=False)
        with mock.patch.object(screen, "WINDOWS", True), \
                mock.patch.object(winput, "button", lambda b, up: None), \
                mock.patch.object(winput, "move_to", lambda x, y, s=0: None), \
                mock.patch.object(winput, "window_at", lambda x, y: (1, 1, "")), \
                mock.patch.object(winput, "is_down", lambda b: False):
            out = REGISTRY["click"].func(ctx, {"x": 500, "y": 500, "hold_seconds": 0.4})
        self.assertIn("Windows says the button was let go after 0.0 s of the 0.4 s hold", out.text)
        with mock.patch.object(screen, "WINDOWS", True), \
                mock.patch.object(winput, "button", lambda b, up: None), \
                mock.patch.object(winput, "move_to", lambda x, y, s=0: None), \
                mock.patch.object(winput, "window_at", lambda x, y: (1, 1, "")), \
                mock.patch.object(winput, "is_down", lambda b: True):
            out = REGISTRY["click"].func(ctx, {"x": 500, "y": 500, "hold_seconds": 0.4})
        self.assertNotIn("let go", out.text)


    def test_set_slider_sets_the_value_through_accessibility(self):
        from localagent.tools import screen

        class RV:
            CurrentMinimum, CurrentMaximum, CurrentValue, CurrentIsReadOnly = 0, 100, 27, False

            def SetValue(self, v):
                self.CurrentValue = v

        class Info:
            def __init__(self, ctype, name):
                self.control_type, self.name = ctype, name

        slider = types.SimpleNamespace(element_info=Info("Slider", "Volume"), iface_range_value=RV())
        thumb = types.SimpleNamespace(element_info=Info("Thumb", ""), parent=lambda: slider)
        at = []

        class Desktop:
            def __init__(self, backend):
                pass

            def from_point(self, x, y):
                at.append((x, y))
                return thumb if x < 1000 else None

        ctx = self.ctx(screenshot_after_action=False)
        with mock.patch.object(screen, "WINDOWS", True), \
                mock.patch.dict(sys.modules, {"pywinauto": types.SimpleNamespace(Desktop=Desktop)}):
            out = REGISTRY["set_slider"].func(ctx, {"x": 100, "y": 100, "value": 70})
            self.assertIn("Set the slider 'Volume' from 27 to 70 (it goes 0 to 100).", out.text)
            out = REGISTRY["set_slider"].func(ctx, {"x": 100, "y": 100, "value": 150})
            self.assertIn("150 is outside that, so it got 100", out.text)
            with self.assertRaises(Exception) as e:
                REGISTRY["set_slider"].func(ctx, {"x": 900, "y": 100, "value": 70})
            self.assertIn("sees no slider there", str(e.exception))
        self.assertEqual(len(at), 3)

if __name__ == "__main__":
    unittest.main()
