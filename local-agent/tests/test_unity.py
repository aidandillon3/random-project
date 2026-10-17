import base64
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("LOCALAGENT_DATA", tempfile.mkdtemp())

from localagent.config import Settings  # noqa: E402
from localagent.tools import REGISTRY, ToolContext, ToolError, ToolResult  # noqa: E402
from localagent.tools import unity  # noqa: E402

PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR4nGNgYGD4DwABBAEAwS2OUAAAAABJRU5ErkJggg==")


def run(ctx, tool_name, **args):
    out = REGISTRY[tool_name].func(ctx, args)
    return out if isinstance(out, ToolResult) else ToolResult(out)


class Busy(Exception):
    """The bridge's answer for a command it never started (main thread busy, or reloading)."""


BUSY = "Unity didn't answer in time. It may be busy importing, showing a dialog, or slowed down."


class FakeUnity:
    """Speaks the bridge protocol like LocalAgentBridge.cs does, with a tiny pretend scene.
    Compiling restarts it on a new port, like Unity's script reload."""

    def __init__(self, project: Path, ddir: Path, name="Game"):
        self.project, self.ddir, self.name = project, ddir, name
        self.token = os.urandom(8).hex()
        self.compile_count, self.compiling, self.playing = 0, False, False
        self.compile_failed, self.domain_loads, self.bridge = False, 1, str(unity.BRIDGE_VERSION)
        self.messages, self.next_messages = [], []
        self.scripts_changed = False
        self.logs, self.next_id = [], 1
        self.requests = []
        self.busy = {}                 # cmd -> how many times to answer "busy, try again"
        self.refuse_play = False       # like Unity with compile errors: asked to play, then doesn't
        self.frozen = False            # the game doesn't advance (Unity paused or throttled)
        self.frame, self.played_at = 100, None
        self.objects = {"Main Camera": 10, "Player": 20, "Player/Body": 21}
        self.sock = None
        self._start()

    def _start(self, reloading=False):
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(8)
        self.port = self.sock.getsockname()[1]
        self.announce(reloading)
        threading.Thread(target=self._loop, args=(self.sock,), daemon=True).start()

    def announce(self, reloading=False):
        self.ddir.mkdir(parents=True, exist_ok=True)
        info = {"project": str(self.project), "port": self.port, "token": self.token, "unity": "6000.0.1f1",
                "pid": os.getpid(), "bridge": self.bridge}
        if reloading:
            info["reloading"] = True
        (self.ddir / f"{self.name}.json").write_text(json.dumps(info))
        (self.project / "Library").mkdir(exist_ok=True)
        (self.project / "Library" / "LocalAgentBridge.json").write_text(json.dumps(info))

    def reload_for(self, seconds):
        """Go away like a script reload: announce it, close, come back on a new port later."""
        self.announce(reloading=True)
        self.close()

        def back():
            time.sleep(seconds)
            self.domain_loads += 1
            self._start()
        threading.Thread(target=back, daemon=True).start()

    def frame_now(self):
        if self.playing and not self.frozen and self.played_at is not None:
            return self.frame + int((time.monotonic() - self.played_at) * 60)
        return self.frame

    def close(self):
        try:
            self.sock.shutdown(socket.SHUT_RDWR)     # wakes the accept() so the port really closes
        except OSError:
            pass
        try:
            self.sock.close()
        except OSError:
            pass

    def _loop(self, sock):
        while True:
            try:
                c, _ = sock.accept()
            except OSError:
                return
            with c:
                f = c.makefile("rb")
                req = json.loads(f.readline())
                if req.get("token") != self.token:
                    resp = {"ok": False, "error": "Wrong key. Re-read the bridge file."}
                else:
                    self.requests.append(req)
                    try:
                        if self.busy.get(req["cmd"]):
                            self.busy[req["cmd"]] -= 1
                            raise Busy(BUSY)
                        resp = {"ok": True, "result": self.handle(req)}
                    except Busy as e:
                        resp = {"ok": False, "error": str(e), "retry": True}
                    except ValueError as e:
                        resp = {"ok": False, "error": str(e)}
                    except ConnectionAbortedError:
                        c.shutdown(socket.SHUT_RDWR)       # hang up without answering
                        continue
                c.sendall(json.dumps(resp).encode() + b"\n")

    def log(self, type_, msg, stack=""):
        self.logs.append({"id": self.next_id, "type": type_, "msg": msg, "stack": stack})
        self.next_id += 1

    def _reload(self):
        time.sleep(0.15)
        self.close()                   # the script reload: gone for a moment, back on another port
        time.sleep(0.2)
        self.messages = self.next_messages
        self.compile_count += 1
        self.compiling = False
        self.domain_loads += 1
        self._start()

    def handle(self, r):
        cmd = r["cmd"]
        if cmd == "ping":
            will_play = self.playing
            if self.refuse_play == "asked":
                will_play, self.refuse_play = True, "dropped"     # one look while Unity still means to
            return {"unity": "6000.0.1f1", "project": str(self.project), "render_pipeline": "UniversalRenderPipelineAsset",
                    "bridge": self.bridge, "playing": self.playing, "paused": False, "will_play": will_play,
                    "compiling": self.compiling, "updating": False,
                    "compile_count": self.compile_count, "compile_failed": self.compile_failed,
                    "compile_errors": sum(m["type"] == "error" for m in self.messages),
                    "domain_loads": self.domain_loads, "frame": self.frame_now(), "time": 0.0,
                    "scenes": [{"name": "Main", "path": "Assets/Scenes/Main.unity", "dirty": True, "active": True}],
                    "selection": ["Player"]}
        if cmd == "refresh":
            if self.scripts_changed:
                self.scripts_changed = False
                self.compiling = True
                threading.Thread(target=self._reload, daemon=True).start()
            return {"compiling": self.compiling, "compile_count": self.compile_count}
        if cmd == "compile":
            return {"compiling": False, "compile_count": self.compile_count, "messages": self.messages,
                    "compile_failed": self.compile_failed,
                    "error_count": sum(m["type"] == "error" for m in self.messages)}
        if cmd == "hierarchy":
            return {"scenes": [{"name": "Main", "path": "Assets/Scenes/Main.unity", "dirty": False, "roots": [
                {"name": "Main Camera", "id": 10, "components": ["Camera", "AudioListener"]},
                {"name": "Player", "id": 20, "children": [{"name": "Body", "id": 21, "active": False}]},
                {"name": "Enemies", "id": 30, "more": 12}]}], "truncated": False}
        if cmd == "inspect":
            if r["path"] not in self.objects and not r["path"].startswith("#"):
                raise ValueError(f"No object '{r['path']}' in the open scenes.")
            return {"path": r["path"], "id": self.objects.get(r["path"], 0), "tag": "Player", "layer": "Default",
                    "active": True, "components": [
                        {"type": "Transform", "props": [{"path": "position", "type": "Vector3", "value": "(0, 1, 0)"}]},
                        {"type": "Rigidbody", "props": [{"path": "m_Mass", "type": "Float", "value": "1"}]},
                        {"type": "PlayerController", "enabled": False,
                         "props": [{"path": "speed", "type": "Float", "value": "5"}]}]}
        if cmd == "create":
            path = (r.get("parent") + "/" if r.get("parent") else "") + r.get("name", "GameObject")
            self.objects[path] = 99
            return {"path": path, "id": 99, "notes": ["Rigidbody.mass = 2"] if r.get("set") else []}
        if cmd == "modify":
            return {"path": r["path"], "id": 20, "notes": [f"{k} = {v}" for k, v in (r.get("set") or {}).items()]}
        if cmd == "delete":
            return {"deleted": [p for p in r["paths"] if p in self.objects],
                    "notes": [f"{p}: No object '{p}' in the open scenes." for p in r["paths"] if p not in self.objects]}
        if cmd == "console":
            sel = [e for e in self.logs if e["id"] > r.get("since", 0)]
            if r.get("level") == "errors":
                sel = [e for e in sel if e["type"] in ("error", "exception")]
            return {"entries": sel[-r.get("limit", 50):], "total": len(sel), "last_id": self.next_id - 1}
        if cmd == "play":
            if r["action"] == "play":
                last = self.next_id - 1
                if self.refuse_play:
                    self.refuse_play = "asked"
                    return {"playing": False, "paused": False, "last_log_id": last, "will_play": True}
                self.playing, self.played_at = True, time.monotonic()
                self.log("log", "Player spawned")
                self.log("exception", "NullReferenceException: Object reference not set",
                         "PlayerController.Update () (at Assets/Scripts/PlayerController.cs:14)")
                return {"playing": True, "paused": False, "last_log_id": last}
            if r["action"] == "stop":
                self.playing = False
            return {"playing": self.playing, "paused": False, "last_log_id": self.next_id - 1}
        if cmd == "screenshot":
            return {"png": base64.b64encode(PNG).decode(), "width": 1, "height": 1,
                    "note": "Rendered from Main Camera." if r.get("view") == "game" else "Scene view."}
        if cmd == "scene":
            return {"build_scenes": ["Assets/Scenes/Main.unity"], "all_scenes": ["Assets/Scenes/Main.unity"]} \
                if r["action"] == "list" else {"saved": "Assets/Scenes/Main.unity"}
        if cmd == "save_prefab":
            return {"asset": r["asset_path"], "path": r["path"]}
        if cmd == "hang_up":
            raise ConnectionAbortedError
        raise ValueError("Unknown command " + cmd)


def make_project(root: Path, name="Game") -> Path:
    p = root / name
    (p / "Assets").mkdir(parents=True)
    (p / "ProjectSettings").mkdir()
    (p / "ProjectSettings" / "ProjectVersion.txt").write_text("m_EditorVersion: 6000.0.1f1\n")
    return p


class UnityToolsTest(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.ddir = self.root / "discovery"
        os.environ["LOCALAGENT_UNITY_DIR"] = str(self.ddir)
        self.addCleanup(os.environ.pop, "LOCALAGENT_UNITY_DIR", None)
        unity.POLL = 0.05
        self.project = make_project(self.root)
        self.ctx = ToolContext(settings=Settings(), workspace=self.root / "ws")
        self.ctx.workspace.mkdir()

    def fake(self, project=None, name="Game"):
        f = FakeUnity(project or self.project, self.ddir, name)
        self.addCleanup(f.close)
        return f

    def test_not_connected_explains_what_to_do(self):
        out = run(self.ctx, "unity_status").text
        self.assertIn("No Unity editor", out)
        self.assertIn("unity_install_bridge", out)

    def test_install_bridge_copies_the_script(self):
        f = self.fake()
        out = run(self.ctx, "unity_install_bridge", project=str(self.project)).text
        dest = self.project / "Assets" / "Editor" / "LocalAgent"
        self.assertEqual((dest / "LocalAgentBridge.cs").read_bytes(), unity.BRIDGE_SRC.read_bytes())
        self.assertEqual(json.loads((dest / "LocalAgent.Editor.asmdef").read_text())["includePlatforms"], ["Editor"])
        self.assertIn("installed", out)
        self.assertIn("Connected to Unity 6000.0.1f1", out)
        self.assertIn("already installed", run(self.ctx, "unity_install_bridge", project=str(self.project)).text)
        with self.assertRaises(ToolError):
            run(self.ctx, "unity_install_bridge", project=str(self.root))
        self.assertTrue(f.requests)

    def test_status_hierarchy_inspect(self):
        self.fake()
        out = run(self.ctx, "unity_status").text
        self.assertIn("Scene: Main (Assets/Scenes/Main.unity, active, unsaved changes)", out)
        self.assertIn("Selected in Unity: Player", out)
        tree = run(self.ctx, "unity_hierarchy").text
        self.assertIn("  - Main Camera #10 [Camera, AudioListener]", tree)
        self.assertIn("    - Body #21 (inactive)", tree)
        self.assertIn("(+12 children not shown)", tree)
        ins = run(self.ctx, "unity_inspect", path="Player").text
        self.assertIn("Rigidbody:", ins)
        self.assertIn("  m_Mass = 1  (Float)", ins)
        self.assertIn("PlayerController (disabled):", ins)
        with self.assertRaises(ToolError) as e:
            run(self.ctx, "unity_inspect", path="Nobody")
        self.assertIn("No object 'Nobody'", str(e.exception))

    def test_changes_are_sent_as_given(self):
        f = self.fake()
        out = run(self.ctx, "unity_create", name="Crate", primitive="cube", position=[0, 2, 0],
                  components=["Rigidbody"], set={"Rigidbody.mass": 2}).text
        self.assertIn("Created Crate #99", out)
        self.assertIn("Rigidbody.mass = 2", out)
        req = f.requests[-1]
        self.assertEqual((req["cmd"], req["primitive"], req["position"]), ("create", "cube", [0, 2, 0]))
        run(self.ctx, "unity_modify", path="Player", set={"PlayerController.speed": 8})
        self.assertEqual(f.requests[-1]["set"], {"PlayerController.speed": 8})
        self.assertIn("Deleted Crate", run(self.ctx, "unity_delete", paths=["Crate"]).text)
        self.assertIn("prefab Assets/Prefabs/Crate.prefab", run(
            self.ctx, "unity_create_asset", kind="prefab", path="Assets/Prefabs/Crate.prefab", from_object="Crate").text)

    def test_wrong_key_is_refused(self):
        f = self.fake()
        f.token = "changed"
        with self.assertRaises(ToolError) as e:
            run(self.ctx, "unity_hierarchy")
        self.assertIn("Wrong key", str(e.exception))

    def test_refresh_waits_through_the_reload_and_reports_errors(self):
        f = self.fake()
        f.scripts_changed = True
        f.next_messages = [
            {"type": "error", "file": "Assets/Scripts/Player.cs", "line": 12, "column": 5, "message": "CS1002: ; expected"},
            {"type": "warning", "file": "Assets/Scripts/Old.cs", "line": 3, "column": 1, "message": "CS0618: obsolete"}]
        old_port = f.port
        out = run(self.ctx, "unity_refresh").text
        self.assertNotEqual(f.port, old_port)       # it really came back on another port
        self.assertIn("1 compile error(s)", out)
        self.assertIn("Assets/Scripts/Player.cs(12,5): CS1002: ; expected", out)
        self.assertIn("1 warning", out)
        self.assertIn("Compile errors: 1", run(self.ctx, "unity_status").text)
        # nothing changed: no compile happens
        f.messages = []
        out = run(self.ctx, "unity_refresh").text
        self.assertIn("No scripts needed compiling", out)
        self.assertIn("no errors", out)

    def test_play_for_a_while_reports_logs_and_a_picture(self):
        f = self.fake()
        f.log("log", "old message from before")
        res = run(self.ctx, "unity_play", action="play", seconds=0.2)
        self.assertIn("Played for 0.2 s. Play mode stopped again.", res.text)
        self.assertIn("[exception] NullReferenceException", res.text)
        self.assertIn("PlayerController.cs:14", res.text)
        self.assertNotIn("old message", res.text)
        self.assertEqual(res.images, [PNG])
        self.assertFalse(f.playing)
        # the console only shows what's new since then
        self.assertEqual(run(self.ctx, "unity_console").text, "No new console messages.")
        f.log("warning", "Something new")
        self.assertIn("[warning] Something new", run(self.ctx, "unity_console").text)
        self.assertIn("old message", run(self.ctx, "unity_console", all_history=True).text)

    def test_screenshot_returns_an_image(self):
        self.fake()
        res = run(self.ctx, "unity_screenshot", view="scene", look_at="Player", angle="top")
        self.assertEqual(res.images, [PNG])

    def test_two_editors_need_a_choice(self):
        self.fake()
        other = make_project(self.root, "Other")
        self.fake(other, name="Other")
        with self.assertRaises(ToolError) as e:
            run(self.ctx, "unity_hierarchy")
        self.assertIn("Several Unity projects", str(e.exception))
        out = run(self.ctx, "unity_status", project=str(other)).text
        self.assertIn(str(other), out)
        self.assertIn("Main Camera", run(self.ctx, "unity_hierarchy").text)
        # the work folder being inside a project also decides it
        ctx2 = ToolContext(settings=Settings(), workspace=self.project / "Assets")
        self.assertIn(str(self.project), run(ctx2, "unity_status").text)

    def test_tools_and_prompt_follow_the_setting(self):
        from localagent.prompts import build_system_prompt
        from localagent.tools import enabled_tools

        tools = enabled_tools(Settings(), workspace=self.project)      # a Unity project: every Unity tool
        self.assertIn("unity_play", [t.name for t in tools])
        prompt = build_system_prompt(Settings(), self.root, tools, "powershell", False)
        self.assertIn("unity_refresh", prompt)
        lite = [t.name for t in enabled_tools(Settings(), workspace=self.ctx.workspace) if t.group == "unity"]
        self.assertEqual(sorted(lite), sorted(unity.ALWAYS_ON))       # nothing Unity around: just the way in
        off = enabled_tools(Settings(enable_unity=False))
        self.assertNotIn("unity_play", [t.name for t in off])
        self.assertNotIn("Unity games", build_system_prompt(Settings(), self.root, off, "powershell", False))

    # ------------------------------------------------------------------ busy, reloading, lost
    def test_busy_answers_are_retried(self):
        f = self.fake()
        f.busy["hierarchy"] = 2
        self.assertIn("Main Camera", run(self.ctx, "unity_hierarchy").text)
        self.assertEqual([r["cmd"] for r in f.requests].count("hierarchy"), 3)
        # an ordinary refusal is not sent again
        with self.assertRaises(ToolError) as e:
            run(self.ctx, "unity_inspect", path="Nobody")
        self.assertNotIsInstance(e.exception, unity.BridgeDown)
        self.assertEqual([r["cmd"] for r in f.requests].count("inspect"), 1)
        # still busy after the wait: it says so, as a BridgeDown the waiting loops understand
        f.busy["find"] = 1000
        with self.assertRaises(unity.BridgeBusy):
            unity.call(self.ctx, "find", name="x", wait_reload=0.2)

    def test_play_waits_while_unity_is_busy(self):
        f = self.fake()
        f.busy["ping"] = 0
        orig = f.handle

        def handle(r):
            if r["cmd"] == "play" and r["action"] == "play":
                f.busy["ping"] = 2          # entering play mode keeps Unity busy for a moment
            return orig(r)
        f.handle = handle
        res = run(self.ctx, "unity_play", action="play", seconds=0.2)
        self.assertIn("Played for 0.2 s", res.text)

    def test_waits_for_a_script_reload(self):
        f = self.fake()
        run(self.ctx, "unity_status")
        f.reload_for(0.6)
        t = time.monotonic()
        # wait_reload is short, but the bridge said it's reloading: that is waited for
        self.assertIn("scenes", unity.call(self.ctx, "hierarchy", wait_reload=0.1))
        self.assertGreater(time.monotonic() - t, 0.4)
        # a plain "not answering" isn't waited for beyond wait_reload
        f.close()
        f.announce()
        with self.assertRaises(unity.BridgeDown) as e:
            unity.call(self.ctx, "hierarchy", wait_reload=0.1)
        self.assertNotIsInstance(e.exception, unity.BridgeReloading)

    def test_a_dropped_change_is_not_sent_twice(self):
        f = self.fake()
        with self.assertRaises(ToolError) as e:
            unity.call(self.ctx, "hang_up")
        self.assertNotIsInstance(e.exception, unity.BridgeDown)
        self.assertIn("may or may not have happened", str(e.exception))
        self.assertEqual([r["cmd"] for r in f.requests].count("hang_up"), 1)

    def test_a_crashed_unity_is_not_waited_for(self):
        f = self.fake()
        f.close()
        p = subprocess.Popen([sys.executable, "-c", "pass"])
        p.wait()
        info = json.loads((self.ddir / "Game.json").read_text())
        info.update(pid=p.pid, reloading=True)
        (self.ddir / "Game.json").write_text(json.dumps(info))
        (self.project / "Library" / "LocalAgentBridge.json").write_text(json.dumps(info))
        t = time.monotonic()
        self.assertIn("No Unity editor", run(self.ctx, "unity_status").text)
        self.assertLess(time.monotonic() - t, 2)

    # ------------------------------------------------------------------ compiling
    def test_refresh_finishes_when_busy_then_idle(self):
        f = self.fake()
        orig = f.handle

        def handle(r):
            if r["cmd"] == "refresh":
                f.busy["ping"] = 3          # importing for a moment, then nothing to compile
            return orig(r)
        f.handle = handle
        t = time.monotonic()
        out = run(self.ctx, "unity_refresh", timeout=20).text
        self.assertLess(time.monotonic() - t, 5)
        self.assertIn("No scripts needed compiling", out)

    def test_refresh_waits_for_the_reload_after_a_clean_compile(self):
        f = self.fake()
        f.scripts_changed = True
        loads = f.domain_loads
        out = run(self.ctx, "unity_refresh").text
        self.assertIn("Compiled with no errors", out)
        self.assertGreater(f.domain_loads, loads)

    def test_compile_failed_is_never_no_errors(self):
        f = self.fake()
        f.compile_failed = True
        out = run(self.ctx, "unity_refresh").text
        self.assertNotIn("no errors", out)
        self.assertIn("compile errors", out)
        self.assertIn("Scripts have compile errors", run(self.ctx, "unity_status").text)
        f.compile_failed = False
        f.messages = [{"type": "error", "file": "A.cs", "line": 1, "column": 1, "message": "CS1"}] + [
            {"type": "warning", "file": "B.cs", "line": 1, "column": 1, "message": "CS2"}] * 3
        self.assertIn("A.cs(1,1): CS1", unity.fmt_compile({"messages": f.messages, "error_count": 30}))
        self.assertIn("30 compile error(s)", unity.fmt_compile({"messages": f.messages, "error_count": 30}))

    # ------------------------------------------------------------------ play mode
    def test_play_refused_stops_waiting(self):
        f = self.fake()
        f.refuse_play = True
        t = time.monotonic()
        out = run(self.ctx, "unity_play", action="play", seconds=1).text
        self.assertLess(time.monotonic() - t, 3)
        self.assertIn("refused to enter play mode", out)
        self.assertIn("unity_refresh", out)

    def test_play_reports_frames_and_warns_when_the_game_stood_still(self):
        f = self.fake()
        out = run(self.ctx, "unity_play", action="play", seconds=0.3).text
        self.assertRegex(out, r"The game ran \d+ frames")
        f.frozen = True
        out = run(self.ctx, "unity_play", action="play", seconds=0.3).text
        self.assertIn("hardly ran (0 frames)", out)

    def test_play_log_puts_errors_first_and_collapses_repeats(self):
        f = self.fake()
        orig = f.handle

        def handle(r):
            res = orig(r)
            if r["cmd"] == "play" and r["action"] == "play":
                for i in range(30):
                    f.log("log", f"tick {i}")
                    if i % 10 == 0:
                        f.log("error", "Enemy has no target", "Enemy.Update () (at Assets/Enemy.cs:9)")
                f.log("warning", "Low fps")
            return res
        f.handle = handle
        out = run(self.ctx, "unity_play", action="play", seconds=0.1).text
        log = out.split("Console while playing:\n", 1)[1]
        self.assertTrue(log.startswith("Errors (4):"), log)
        self.assertIn("[error] Enemy has no target  (x3)", log)
        self.assertIn("Assets/Enemy.cs:9", log)
        self.assertLess(log.index("Enemy has no target"), log.index("Other messages"))
        self.assertIn("[warning] Low fps", log)
        self.assertNotIn("tick 3\n", log)          # only the latest others
        self.assertIn("tick 29", log)

    # ------------------------------------------------------------------ picking the project
    def test_status_resolves_relative_projects_and_pins_only_on_success(self):
        self.fake()
        other = make_project(self.root, "Other")      # a Unity project, but not open in Unity
        self.ctx.workspace = self.root
        out = run(self.ctx, "unity_status", project="Other").text
        self.assertIn("isn't open in Unity", out)
        self.assertNotIn("project", self.ctx.unity)   # not stuck on it
        self.assertIn(str(self.project), run(self.ctx, "unity_status", project="Game").text)
        self.assertTrue(unity._same(self.ctx.unity["project"], self.project))
        self.assertIn("Main Camera", run(self.ctx, "unity_hierarchy").text)
        with self.assertRaises(ToolError):
            run(self.ctx, "unity_status", project="ws")
        self.assertTrue(other.is_dir())

    def test_enable_group_is_called_when_unity_is_found(self):
        calls = []
        self.ctx.enable_group = calls.append
        run(self.ctx, "unity_status")                  # nothing running
        self.assertEqual(calls, [])
        self.fake()
        run(self.ctx, "unity_status")
        self.assertEqual(calls, ["unity"])
        run(self.ctx, "unity_install_bridge", project=str(self.project))
        self.assertEqual(calls, ["unity", "unity"])

    def test_detected(self):
        ws = self.root / "plain"
        ws.mkdir()
        self.assertFalse(unity.detected(ws, Settings()))
        self.assertFalse(unity.detected(None, Settings()))
        self.assertTrue(unity.detected(ws, Settings(unity_project="C:/Games/Thing")))
        self.assertTrue(unity.detected(self.project, Settings()))
        self.assertTrue(unity.detected(self.project / "Assets", Settings()))      # its parent is the project
        self.ddir.mkdir()
        (self.ddir / "x.json").write_text("{}")
        self.assertTrue(unity.detected(ws, Settings()))

    def test_old_bridge_is_reported(self):
        f = self.fake()
        f.bridge = "1"
        out = run(self.ctx, "unity_status").text
        self.assertIn("out of date", out)
        self.assertIn("unity_install_bridge", out)
        src = unity.BRIDGE_SRC.read_text(encoding="utf-8")
        self.assertIn(f'BridgeVersion = "{unity.BRIDGE_VERSION}"', src)

    def test_install_has_unity_load_the_new_bridge(self):
        f = self.fake()
        f.bridge = "1"
        orig = f.handle

        def handle(r):
            if r["cmd"] == "refresh":
                f.bridge = str(unity.BRIDGE_VERSION)      # Unity imported the new script and reloaded
            return orig(r)
        f.handle = handle
        out = run(self.ctx, "unity_install_bridge", project=str(self.project)).text
        self.assertIn("Bridge installed", out)
        self.assertNotIn("out of date", out)
        self.assertIn("refresh", [r["cmd"] for r in f.requests])

    def test_install_names_compile_errors_when_unity_doesnt_load_it(self):
        self.addCleanup(setattr, unity, "INSTALL_WAIT", unity.INSTALL_WAIT)
        unity.INSTALL_WAIT = 0.3
        out = run(self.ctx, "unity_install_bridge", project=str(self.project)).text
        self.assertIn("hasn't loaded it", out)
        self.assertIn("compile errors", out)

    # ------------------------------------------------------------------ results
    def test_delete_reports_each_path(self):
        self.fake()
        out = run(self.ctx, "unity_delete", paths=["Player", "Nobody"]).text
        self.assertIn("Deleted Player.", out)
        self.assertIn("- Nobody: No object 'Nobody'", out)
        self.assertIn("Nothing was deleted", run(self.ctx, "unity_delete", paths=["Nobody"]).text)

    def test_prefab_overwrite_is_passed_only_when_asked(self):
        f = self.fake()
        run(self.ctx, "unity_create_asset", kind="prefab", path="Assets/P.prefab", from_object="Player")
        self.assertNotIn("overwrite", f.requests[-1])
        run(self.ctx, "unity_create_asset", kind="prefab", path="Assets/P.prefab", from_object="Player", overwrite=True)
        self.assertIs(f.requests[-1]["overwrite"], True)

    def test_prefab_asset_contents_are_named_by_path(self):
        res = {"roots": [{"name": "Enemy", "path": "Assets/Enemy.prefab",
                          "children": [{"name": "Gun"}]}], "asset": True}
        out = unity.fmt_hierarchy(res)
        self.assertIn("  - Enemy\n    - Gun", out)
        self.assertNotIn("#", out)
        self.assertIn("Assets/Enemy.prefab/Child", out)
        self.assertNotIn("#", unity.fmt_inspect({"path": "Assets/Enemy.prefab/Gun", "components": []}).split("\n")[0])


@unittest.skipUnless(shutil.which("mcs") and os.environ.get("UNITY_REF_DIR"),
                     "needs Mono's mcs and UNITY_REF_DIR pointing at UnityEngine.dll and UnityEditor.dll")
class BridgeCompilesTest(unittest.TestCase):
    def test_bridge_compiles_against_unity(self):
        ref = Path(os.environ["UNITY_REF_DIR"])
        out = Path(tempfile.mkdtemp()) / "bridge.dll"
        r = subprocess.run(["mcs", "-target:library", "-define:UNITY_EDITOR", "-warnaserror+",
                            f"-r:{ref / 'UnityEngine.dll'}", f"-r:{ref / 'UnityEditor.dll'}", "-r:System.Core.dll",
                            f"-out:{out}", str(unity.BRIDGE_SRC)], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        if not shutil.which("mono"):
            return
        # The JSON reader runs before the key is checked: deep nesting must fail cleanly, not crash Unity.
        d = out.parent
        for dll in ("UnityEngine.dll", "UnityEditor.dll"):
            shutil.copy(ref / dll, d / dll)
        (d / "j.cs").write_text("""
using System; using System.Threading;
class J { static void Main() {
  Console.WriteLine(LocalAgent.Json.Write(LocalAgent.Json.Parse("{\\"a\\":[1,2.5,{\\"b\\":\\"x\\\\u0041\\"}],\\"c\\":null}")));
  var t = new Thread(() => {
    try { LocalAgent.Json.Parse(new string('[', 100000)); Console.WriteLine("parsed"); }
    catch (FormatException e) { Console.WriteLine(e.Message); }
  }, 256 * 1024);
  t.Start(); t.Join();
}}""")
        r = subprocess.run(["mcs", f"-r:{out}", f"-out:{d / 'j.exe'}", str(d / "j.cs")], capture_output=True, text=True,
                           check=False)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        r = subprocess.run(["mono", str(d / "j.exe")], capture_output=True, text=True, cwd=d,
                           timeout=60, check=False)
        self.assertEqual(r.stdout.splitlines(), ['{"a":[1,2.5,{"b":"xA"}],"c":null}', "JSON nested too deeply"],
                         r.stdout + r.stderr)


if __name__ == "__main__":
    unittest.main()
