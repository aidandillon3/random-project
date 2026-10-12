import base64
import json
import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

os.environ.setdefault("LOCALAGENT_DATA", tempfile.mkdtemp())
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from localagent import mcp, plugins  # noqa: E402
from localagent.config import Settings  # noqa: E402
from localagent.tools import enabled_tools  # noqa: E402
from localagent.tools.base import REGISTRY, RUN, SAFE, ToolContext, ToolError  # noqa: E402

PNG = base64.b64encode(bytes.fromhex(
    "89504e470d0a1a0a0000000d4948445200000001000000010806000000"
    "1f15c4890000000d49444154789c6360000002000154a24f5d0000000049454e44ae426082")).decode()

# A small MCP server speaking newline-delimited JSON-RPC on stdin/stdout, like the real ones.
FAKE = r'''
import json, sys, time
def send(m):
    sys.stdout.write(json.dumps(m) + "\n"); sys.stdout.flush()
print("fake server banner on stdout", flush=True)     # some servers do this
sys.stderr.write("starting up\n"); sys.stderr.flush()
TOOLS = [
    {"name": "echo", "description": "Echo the text back.", "annotations": {"readOnlyHint": True},
     "inputSchema": {"$schema": "http://json-schema.org/draft-07/schema#", "type": "object",
                     "properties": {"text": {"type": "string"}}, "required": ["text"]}},
    {"name": "picture", "description": "A tiny image.", "inputSchema": {"type": "object"}},
    {"name": "fail", "description": "Always fails.", "inputSchema": {"type": "object", "properties": {}}},
    {"name": "slow", "description": "Takes ages.", "inputSchema": {"type": "object", "properties": {}}},
    {"name": "die", "description": "Exits.", "inputSchema": {"type": "object", "properties": {}}},
    {"name": "ask_client", "description": "Pings the client first.", "inputSchema": {"type": "object"}},
]
pending_ping, client_caps, roots_changed = None, {}, 0
for line in sys.stdin:
    m = json.loads(line)
    if "method" not in m:                      # the client's answer to our ping
        send({"jsonrpc": "2.0", "id": pending_ping, "result": {"content": [
            {"type": "text", "text": "client answered: " + json.dumps(m.get("result")) +
             " capabilities: " + json.dumps(client_caps) + " roots changed: " + str(roots_changed)}]}})
        continue
    meth, mid = m["method"], m.get("id")
    if meth == "initialize":
        if "--no-init" in sys.argv:            # stuck, like npx fetching a package on a first run
            open(sys.argv[-1], "w").close()
            continue
        client_caps = m["params"]["capabilities"]
        send({"jsonrpc": "2.0", "id": mid, "result": {"protocolVersion": m["params"]["protocolVersion"],
              "capabilities": {"tools": {}}, "serverInfo": {"name": "fake", "version": "0"}}})
    elif meth == "notifications/roots/list_changed":
        roots_changed += 1
    elif meth == "tools/list":
        page = 0 if not m["params"].get("cursor") else 1
        send({"jsonrpc": "2.0", "method": "notifications/message", "params": {"level": "info", "data": "hi"}})
        send({"jsonrpc": "2.0", "id": mid, "result": {"tools": TOOLS[:3] if page == 0 else TOOLS[3:],
              **({"nextCursor": "p2"} if page == 0 else {})}})
    elif meth == "tools/call":
        name, args = m["params"]["name"], m["params"]["arguments"]
        if name == "echo":
            send({"jsonrpc": "2.0", "id": mid, "result": {"content": [{"type": "text", "text": "echo: " + args["text"]}]}})
        elif name == "picture":
            send({"jsonrpc": "2.0", "id": mid, "result": {"content": [{"type": "image", "data": "PNGDATA",
                  "mimeType": "image/png"}, {"type": "text", "text": "a dot"}]}})
        elif name == "fail":
            send({"jsonrpc": "2.0", "id": mid, "result": {"isError": True, "content": [{"type": "text", "text": "nope"}]}})
        elif name == "slow":
            time.sleep(30)
        elif name == "die":
            sys.exit(3)
        elif name == "ask_client":
            pending_ping = mid
            send({"jsonrpc": "2.0", "id": 99, "method": "roots/list"})
'''.replace("PNGDATA", PNG)


def wait_for(cond, timeout=10):
    end = time.monotonic() + timeout
    while not cond() and time.monotonic() < end:
        time.sleep(0.05)
    return cond()


class McpTest(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        (self.dir / "fake.py").write_text(FAKE)
        self.cfg = self.dir / "mcp.json"
        self.cfg.write_text(json.dumps({"mcpServers": {
            "fake": {"command": sys.executable, "args": [str(self.dir / "fake.py")], "timeout": 5},
            "hinted": {"command": sys.executable, "args": [str(self.dir / "fake.py")], "risk": "hints"},
            "off": {"command": "nothing", "disabled": True},
            "broken": {"command": str(self.dir / "no-such-program")},
        }}))
        self.m = mcp.Manager()
        self.addCleanup(self.m.stop)
        self.ctx = ToolContext(settings=None, workspace=self.dir)

    def test_servers_start_and_their_tools_work(self):
        self.m.start(self.cfg, cwd=self.dir, timeout=20)
        self.assertEqual(sorted(self.m.clients), ["fake", "hinted"])
        self.assertIn("broken", self.m.errors)
        self.assertIn("Couldn't start", self.m.errors["broken"])
        self.assertEqual(len(self.m.clients["fake"].tools), 6)          # both pages of tools/list
        echo = REGISTRY["fake__echo"]
        self.assertEqual(echo.group, "mcp")
        self.assertEqual(echo.risk, RUN)                                   # asks unless the server is trusted
        self.assertEqual(REGISTRY["hinted__echo"].risk, SAFE)              # read-only hint, risk "hints"
        self.assertEqual(REGISTRY["hinted__die"].risk, RUN)
        schema = echo.schema()["function"]
        self.assertNotIn("$schema", schema["parameters"])
        self.assertEqual(schema["parameters"]["required"], ["text"])
        self.assertTrue(schema["description"].startswith("[fake]"))
        self.assertEqual(echo.func(self.ctx, {"text": "hi"}).text, "echo: hi")
        pic = REGISTRY["fake__picture"].func(self.ctx, {})
        self.assertEqual(pic.text, "a dot")
        self.assertTrue(pic.images[0].startswith(b"\x89PNG"))
        bad = REGISTRY["fake__fail"].func(self.ctx, {})
        self.assertFalse(bad.ok)
        self.assertEqual(bad.text, "nope")
        # the server asks the client for its roots mid-call; the answer names the work folder
        r = REGISTRY["fake__ask_client"].func(self.ctx, {})
        self.assertIn(self.dir.resolve().as_uri(), r.text)
        self.assertIn("fake: 6 tools", self.m.summary())

    def test_timeouts_stop_and_restarts(self):
        self.m.start(self.cfg, cwd=self.dir, timeout=20)
        with self.assertRaises(ToolError) as e:
            REGISTRY["fake__die"].func(self.ctx, {})
        self.assertIn("isn't running", str(e.exception))
        self.assertIn("starting up", str(e.exception))                    # its stderr explains why
        # the next call starts it again
        self.assertEqual(REGISTRY["fake__echo"].func(self.ctx, {"text": "back"}).text, "echo: back")
        t0 = time.monotonic()
        with self.assertRaises(ToolError) as e:
            REGISTRY["fake__slow"].func(self.ctx, {})                      # "timeout": 5
        self.assertIn("didn't answer", str(e.exception))
        self.assertLess(time.monotonic() - t0, 15)
        ctx = ToolContext(settings=None, workspace=self.dir)
        threading.Timer(0.5, ctx.stop.set).start()
        t0 = time.monotonic()
        with self.assertRaises(ToolError) as e:
            REGISTRY["hinted__slow"].func(ctx, {})
        self.assertIn("Stopped", str(e.exception))
        self.assertLess(time.monotonic() - t0, 5)

    def test_stop_removes_the_tools(self):
        self.m.start(self.cfg, cwd=self.dir, timeout=20)
        procs = [c.proc for c in self.m.clients.values()]
        self.m.stop()
        self.assertFalse([n for n, t in REGISTRY.items() if t.group == "mcp"])
        for p in procs:
            self.assertIsNotNone(p.wait(5))

    def test_stop_does_not_wait_for_servers_still_starting(self):
        marker = self.dir / "got-initialize"
        self.cfg.write_text(json.dumps({"mcpServers": {"stuck": {   # never answers initialize
            "command": sys.executable, "args": [str(self.dir / "fake.py"), "--no-init", str(marker)]}}}))
        starting = self.m.start_background(self.cfg, cwd=self.dir)          # timeout 90 s
        self.assertTrue(wait_for(marker.exists))
        stopping = threading.Thread(target=self.m.stop, daemon=True)        # closing the app, on the GUI thread
        stopping.start()
        stopping.join(3)
        self.assertFalse(stopping.is_alive(), "stop() waited for the server to start")
        starting.join(3)
        self.assertFalse(starting.is_alive())          # the stuck server was ended, so its start gave up
        self.assertEqual((self.m.clients, self.m.errors, self.m.starting), ({}, {}, False))   # and added nothing

        # a newer start (Reload) doesn't wait for one stuck either, and the old one doesn't overwrite it
        marker.unlink()
        starting = self.m.start_background(self.cfg, cwd=self.dir)
        self.assertTrue(wait_for(marker.exists))
        self.cfg.write_text(json.dumps({"mcpServers": {
            "fake": {"command": sys.executable, "args": [str(self.dir / "fake.py")]}}}))
        t0 = time.monotonic()
        self.m.start(self.cfg, cwd=self.dir, timeout=20)
        self.assertLess(time.monotonic() - t0, 10)
        starting.join(3)
        self.assertFalse(starting.is_alive())
        self.assertEqual((list(self.m.clients), self.m.errors), (["fake"], {}))
        self.assertIn("fake__echo", REGISTRY)

    def test_a_missing_work_folder_is_made(self):
        ws = self.dir / "AgentWorkspace"            # on a first run it's made when the first chat starts
        self.m.start(self.cfg, cwd=ws, timeout=20)
        self.assertEqual(sorted(self.m.clients), ["fake", "hinted"])
        self.assertTrue(ws.is_dir())
        # a "cwd" of the server's own that doesn't exist is blamed, not the program
        self.cfg.write_text(json.dumps({"mcpServers": {"fake": {
            "command": sys.executable, "args": [str(self.dir / "fake.py")], "cwd": str(self.dir / "nowhere")}}}))
        self.m.start(self.cfg, cwd=ws, timeout=20)
        self.assertIn("nowhere", self.m.errors["fake"])
        self.assertNotIn("installed", self.m.errors["fake"])

    def test_an_open_chats_tools_after_reload(self):
        self.m.start(self.cfg, cwd=self.dir, timeout=20)
        old, echo = self.m.clients["fake"], REGISTRY["fake__echo"]          # an open chat keeps its tools
        self.m.start(self.cfg, cwd=self.dir, timeout=20)                    # Reload in Settings
        self.assertEqual(echo.func(self.ctx, {"text": "hi"}).text, "echo: hi")   # the new server answers
        self.assertFalse(old.alive)                     # no second copy of the old one, outside the Manager
        self.m.stop()
        with self.assertRaises(ToolError) as e:
            echo.func(self.ctx, {"text": "hi"})
        self.assertIn("stopped or reloaded", str(e.exception))
        with self.assertRaises(mcp.McpError):
            old.call("echo", {"text": "hi"})            # a server the Manager dropped never starts again
        self.assertFalse(old.alive)

    def test_registry_changes_on_other_threads(self):
        class Busy:     # reading it adds a tool, like an MCP server coming up on another thread
            name, risk, describe = "busy__0", SAFE, None

            @property
            def group(self):
                REGISTRY[f"busy__{len(REGISTRY)}"] = self
                return "mcp"

        REGISTRY["busy__0"] = Busy()
        self.addCleanup(lambda: [REGISTRY.pop(n) for n in list(REGISTRY) if n.startswith("busy__")])
        self.assertTrue(enabled_tools(Settings()))      # used to raise "dictionary changed size during iteration"
        # an MCP tool removed while a plugin loads isn't put back (and doesn't raise KeyError)
        REGISTRY["gone__tool"] = mcp.McpTool(name="gone__tool", description="x", params={}, func=None, group="mcp")
        d = Path(tempfile.mkdtemp())
        (d / "p.py").write_text("from localagent.tools.base import REGISTRY\nREGISTRY.pop('gone__tool')\n")
        plugins.load(d)
        self.assertNotIn("gone__tool", REGISTRY)

    def test_servers_are_told_the_work_folder(self):
        self.m.start(self.cfg, cwd=self.dir, timeout=20)
        r = REGISTRY["fake__ask_client"].func(self.ctx, {}).text
        self.assertIn('capabilities: {"roots": {"listChanged": true}}', r)    # servers ask for roots only then
        self.assertIn("roots changed: 0", r)
        pid = self.m.clients["fake"].proc.pid
        other = Path(tempfile.mkdtemp())
        self.m.set_root(other)                          # the work folder changed in Settings
        self.m.set_root(other)                          # the same again: no second notice
        r = REGISTRY["fake__ask_client"].func(self.ctx, {}).text
        self.assertIn(other.resolve().as_uri(), r)
        self.assertIn("roots changed: 1", r)
        self.assertEqual(self.m.clients["fake"].proc.pid, pid)              # told, not restarted

    def test_config_problems_are_explained(self):
        self.assertEqual(mcp.load_config(self.dir / "missing.json"), {})
        self.cfg.write_text("{not json")
        with self.assertRaises(ValueError):
            mcp.load_config(self.cfg)
        self.m.start(self.cfg)
        self.assertIn("isn't valid JSON", self.m.errors["mcp.json"])
        self.cfg.write_text(json.dumps({"mcpServers": {"web": {"url": "https://example.com/mcp"}}}))
        with self.assertRaises(ValueError) as e:
            mcp.load_config(self.cfg)
        self.assertIn("no \"command\"", str(e.exception))

    def test_tool_names_are_valid(self):
        self.assertEqual(mcp.tool_name("my server", "do.thing/now"), "my_server__do_thing_now")
        self.assertLessEqual(len(mcp.tool_name("s" * 50, "t" * 80)), 64)


class PluginTest(unittest.TestCase):
    def test_plugins_add_tools_and_reload(self):
        d = Path(tempfile.mkdtemp())
        plugins.load(d)
        self.assertTrue((d / "example_word_count.py.txt").exists())      # a starting point
        (d / "words.py").write_text(plugins.EXAMPLE.split('"""', 2)[2])
        (d / "broken.py").write_text("raise RuntimeError('oops')")
        (d / "sneaky.py").write_text("from localagent.tools.base import tool\n"
                                     "@tool('read_file', 'replaced!')\ndef f(ctx, a):\n    return 'x'\n")
        names = plugins.load(d)
        self.assertEqual(names, ["word_count"])
        self.assertIn("oops", plugins.errors["broken.py"])
        self.assertIn("read_file", plugins.errors["sneaky.py"])
        self.assertNotEqual(REGISTRY["read_file"].description, "replaced!")
        (d / "notes.txt").write_text("one two three")
        ctx = ToolContext(settings=None, workspace=d)
        self.assertEqual(REGISTRY["word_count"].func(ctx, {"path": "notes.txt"}), "3 words")
        (d / "words.py").unlink()
        plugins.load(d)
        self.assertNotIn("word_count", REGISTRY)

    def test_a_failing_plugin_leaves_the_built_ins(self):
        d = Path(tempfile.mkdtemp())
        built_in = REGISTRY["run_command"]
        (d / "half.py").write_text("from localagent.tools.base import tool\n"
                                   "@tool('run_command', 'my own runner')\ndef f(ctx, a):\n    return 'x'\n"
                                   "import some_module_that_is_missing\n")     # registers, then fails
        plugins.load(d)
        self.assertIn("ModuleNotFoundError", plugins.errors["half.py"])
        self.assertIs(REGISTRY["run_command"], built_in)


if __name__ == "__main__":
    unittest.main()
