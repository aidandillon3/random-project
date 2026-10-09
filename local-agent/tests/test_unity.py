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


class FakeUnity:
    """Speaks the bridge protocol like LocalAgentBridge.cs does, with a tiny pretend scene.
    Compiling restarts it on a new port, like Unity's script reload."""

    def __init__(self, project: Path, ddir: Path, name="Game"):
        self.project, self.ddir, self.name = project, ddir, name
        self.token = os.urandom(8).hex()
        self.compile_count, self.compiling, self.playing = 0, False, False
        self.messages, self.next_messages = [], []
        self.scripts_changed = False
        self.logs, self.next_id = [], 1
        self.requests = []
        self.objects = {"Main Camera": 10, "Player": 20, "Player/Body": 21}
        self.sock = None
        self._start()

    def _start(self):
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(8)
        self.port = self.sock.getsockname()[1]
        self.ddir.mkdir(parents=True, exist_ok=True)
        info = {"project": str(self.project), "port": self.port, "token": self.token, "unity": "6000.0.1f1", "pid": 1}
        (self.ddir / f"{self.name}.json").write_text(json.dumps(info))
        (self.project / "Library").mkdir(exist_ok=True)
        (self.project / "Library" / "LocalAgentBridge.json").write_text(json.dumps(info))
        threading.Thread(target=self._loop, args=(self.sock,), daemon=True).start()

    def close(self):
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
                        resp = {"ok": True, "result": self.handle(req)}
                    except ValueError as e:
                        resp = {"ok": False, "error": str(e)}
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
        self._start()

    def handle(self, r):
        cmd = r["cmd"]
        if cmd == "ping":
            return {"unity": "6000.0.1f1", "project": str(self.project), "render_pipeline": "UniversalRenderPipelineAsset",
                    "playing": self.playing, "paused": False, "compiling": self.compiling, "updating": False,
                    "compile_count": self.compile_count,
                    "compile_errors": sum(m["type"] == "error" for m in self.messages),
                    "scenes": [{"name": "Main", "path": "Assets/Scenes/Main.unity", "dirty": True, "active": True}],
                    "selection": ["Player"]}
        if cmd == "refresh":
            if self.scripts_changed:
                self.scripts_changed = False
                self.compiling = True
                threading.Thread(target=self._reload, daemon=True).start()
            return {"compiling": self.compiling, "compile_count": self.compile_count}
        if cmd == "compile":
            return {"compiling": False, "compile_count": self.compile_count, "messages": self.messages}
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
            return {"deleted": r["paths"]}
        if cmd == "console":
            sel = [e for e in self.logs if e["id"] > r.get("since", 0)]
            if r.get("level") == "errors":
                sel = [e for e in sel if e["type"] in ("error", "exception")]
            return {"entries": sel[-r.get("limit", 50):], "total": len(sel), "last_id": self.next_id - 1}
        if cmd == "play":
            if r["action"] == "play":
                last = self.next_id - 1
                self.playing = True
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
        raise ValueError("Unknown command " + cmd)


def make_project(root: Path, name="Game") -> Path:
    p = root / name
    (p / "Assets").mkdir(parents=True)
    (p / "ProjectSettings").mkdir()
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

        tools = enabled_tools(Settings())
        self.assertIn("unity_play", [t.name for t in tools])
        prompt = build_system_prompt(Settings(), self.root, tools, "", "powershell", False)
        self.assertIn("unity_refresh", prompt)
        off = enabled_tools(Settings(enable_unity=False))
        self.assertNotIn("unity_play", [t.name for t in off])
        self.assertNotIn("Unity games", build_system_prompt(Settings(), self.root, off, "", "powershell", False))


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


if __name__ == "__main__":
    unittest.main()
