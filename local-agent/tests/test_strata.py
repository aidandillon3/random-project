import io
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("LOCALAGENT_DATA", tempfile.mkdtemp())

from localagent import catalog, launcher, strata  # noqa: E402
from localagent.config import Settings  # noqa: E402
from localagent.guide import sampling_for  # noqa: E402


def serve(routes: dict) -> tuple[ThreadingHTTPServer, str]:
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            status, body = routes.get(self.path, (404, b""))
            self.send_response(status)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, f"http://127.0.0.1:{httpd.server_address[1]}"


class StrataTest(unittest.TestCase):
    def setUp(self):
        self.flash = catalog.BY_KEY["swift-flash-next"]
        self.root = Path(tempfile.mkdtemp()) / "strata"
        self.model = Path("/models/Swift-1.5-Qwen3.8-Flash-Next") / self.flash.files[0]

    def test_only_flash_next_runs_on_it(self):
        self.assertTrue(strata.supports(self.flash))
        self.assertFalse(strata.supports(catalog.BY_KEY["swift-27b"]))
        self.assertFalse(strata.supports(None))
        s = Settings(engine="strata")
        self.assertTrue(strata.active(s, self.flash))
        self.assertFalse(strata.active(s, catalog.BY_KEY["swift-27b"]))
        self.assertFalse(strata.active(Settings(engine="llama.cpp"), self.flash))
        # Strata's own setup expects these exact file names for --family swift --model IQ2_XS
        self.assertEqual(self.flash.files[0], "Swift-Qwen3.8-Flash-Next-GSQ-RCO-IQ2_XS-00001-of-00002.gguf")

    def test_setup_and_serve_commands(self):
        s = Settings(ctx_size=65536, use_vision=True, port=8085)
        a = strata.setup_args(self.flash, self.model, s, self.root)
        self.assertIn("--yes", a)
        self.assertIn("--no-start", a)
        val = lambda k: a[a.index(k) + 1]  # noqa: E731
        self.assertEqual((val("--family"), val("--model")), ("swift", "IQ2_XS"))
        self.assertEqual(val("--gguf-dir"), str(self.model.parent))
        self.assertEqual(val("--data-dir"), str(Path("/models/Strata-data")))
        self.assertEqual((val("--context"), val("--vision"), val("--port")), ("65536", "gpu", "8085"))
        s.use_vision = False
        self.assertEqual(strata.setup_args(self.flash, self.model, s, self.root)[-3], "no")
        cmd = strata.serve_args(self.flash, s, self.root)
        self.assertEqual(cmd[1:4], [str(self.root / "serve" / "server.py"), "--engine", "strata"])
        self.assertEqual(cmd[cmd.index("--config") + 1], str(self.root / "strata-swift-iq2_xs.json"))
        # a prompt plus max_tokens longer than the context gets a shorter answer, not an error
        self.assertIn("--fit-max-tokens", cmd)

    def test_windows_setup_command_survives_spaces(self):
        def after_cmd_c(line: str) -> str:
            """What cmd.exe runs for "cmd ... /c <rest>" (cmd /?): when <rest> starts with a quote and has more
            than two, the first and the last quote are removed."""
            rest = line.split(" /c ", 1)[1].lstrip()
            if rest.startswith('"') and rest.count('"') > 2:
                i = rest.rindex('"')
                rest = rest[1:i] + rest[i + 1:]
            return rest

        root = Path("C:/Users/Aidan Dillon/Local Agent/strata")
        model = Path("C:/Users/Aidan Dillon/Local Agent/models/Swift-1.5-Qwen3.8-Flash-Next") / self.flash.files[0]
        s = Settings(ctx_size=65536, use_vision=True, port=8080)
        old = strata.WINDOWS
        self.addCleanup(setattr, strata, "WINDOWS", old)
        strata.WINDOWS = True
        # the old way: the .bat's path loses its opening quote -> cmd runs C:\Users\Aidan
        bat = str(root / "START-HERE.bat")
        broken = subprocess.list2cmdline(["cmd", "/c", bat, "--gguf-dir", str(model.parent)])
        self.assertTrue(after_cmd_c(broken).startswith(bat.split(" ")[0] + " "))
        # first setup (no .venv yet): "call" first, so every quote stays where it was
        a = strata.setup_args(self.flash, model, s, root)
        self.assertEqual(a[:5], ["cmd", "/d", "/c", "call", bat])
        line = subprocess.list2cmdline(a)
        self.assertEqual(after_cmd_c(line), line.split(" /c ", 1)[1])
        self.assertIn(f'call "{bat}" --yes', line)
        self.assertIn(f'--gguf-dir "{model.parent}"', line)
        # later setups: the .venv's Python runs setup.py directly, no cmd.exe at all
        venv_root = Path(tempfile.mkdtemp()) / "Local Agent" / "strata"
        strata.python(venv_root).parent.mkdir(parents=True)
        strata.python(venv_root).write_text("")
        a = strata.setup_args(self.flash, model, s, venv_root)
        self.assertEqual(a[:2], [str(venv_root / ".venv" / "Scripts" / "python.exe"), str(venv_root / "setup.py")])
        self.assertIn("--yes", a)

    def test_engine_cache_options_are_put_back_after_setup(self):
        self.root.mkdir(parents=True)
        cfg_path = strata.config_path(self.flash, self.root)
        self.assertFalse(strata.tune_config(self.flash, self.root))          # no config yet: nothing to do
        cfg_path.write_text(json.dumps({"exe": "engine/strata.exe", "args": ["--serve", "--prompt-cache", "4"],
                                        "sampling": {"temperature": 1.0}}), encoding="utf-8")
        strata.serve_args(self.flash, Settings(), self.root)                  # done before each start
        cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
        args = cfg["args"]
        val = lambda k: args[args.index(k) + 1]  # noqa: E731
        self.assertEqual(val("--prompt-cache"), "4")                           # set by hand: kept
        self.assertEqual((val("--conversation-cache-mib"), val("--conversation-cache-min-tokens")), ("4096", "8192"))
        self.assertEqual(cfg["sampling"], {"temperature": 1.0})
        self.assertFalse(strata.tune_config(self.flash, self.root))            # already there: file left alone
        cfg["args"] = ["--serve"]                                              # setup ran again and rewrote it
        cfg_path.write_text(json.dumps(cfg), encoding="utf-8")
        self.assertTrue(strata.tune_config(self.flash, self.root))
        args = json.loads(cfg_path.read_text(encoding="utf-8"))["args"]
        self.assertEqual(args.count("--prompt-cache"), 1)
        self.assertEqual(args[args.index("--prompt-cache") + 1], "10")

    def test_setup_runs_again_only_when_its_choices_change(self):
        s = Settings(ctx_size=65536, use_vision=True)
        self.root.mkdir(parents=True)
        self.assertTrue(strata.needs_setup(self.flash, self.model, s, self.root))
        strata.config_path(self.flash, self.root).write_text("{}")
        strata.python(self.root).parent.mkdir(parents=True)
        strata.python(self.root).write_text("")
        strata.mark_setup_done(self.flash, self.model, s, self.root)
        self.assertFalse(strata.needs_setup(self.flash, self.model, s, self.root))
        s.port = 9999                       # given at each start, no new setup
        self.assertFalse(strata.needs_setup(self.flash, self.model, s, self.root))
        s.ctx_size = 131072
        self.assertTrue(strata.needs_setup(self.flash, self.model, s, self.root))

    def test_each_size_has_its_own_setup(self):
        s = Settings(ctx_size=65536, use_vision=True)
        iq3 = catalog.BY_KEY["swift-flash-next-iq3s"]
        iq3_model = self.model.with_name(iq3.files[0])
        self.root.mkdir(parents=True)
        strata.python(self.root).parent.mkdir(parents=True)
        strata.python(self.root).write_text("")
        # an older version's stamp: IQ2_XS set up, one setup's choices only
        strata.config_path(self.flash, self.root).write_text("{}")
        (self.root / strata.STAMP).write_text(json.dumps(strata.wanted(self.flash, self.model, s)))
        self.assertFalse(strata.needs_setup(self.flash, self.model, s, self.root))
        self.assertTrue(strata.set_up_before(self.root))
        # IQ3_S: its own setup, config and engine options
        self.assertTrue(strata.needs_setup(iq3, iq3_model, s, self.root))
        a = strata.setup_args(iq3, iq3_model, s, self.root)
        self.assertEqual(a[a.index("--model") + 1], "IQ3_S")
        cfg_path = strata.config_path(iq3, self.root)
        self.assertEqual(cfg_path.name, "strata-swift-iq3_s.json")
        cfg_path.write_text(json.dumps({"args": ["--serve"]}))
        strata.mark_setup_done(iq3, iq3_model, s, self.root)
        # switching back and forth sets neither up again
        self.assertFalse(strata.needs_setup(iq3, iq3_model, s, self.root))
        self.assertFalse(strata.needs_setup(self.flash, self.model, s, self.root))
        cmd = strata.serve_args(iq3, s, self.root)
        self.assertEqual(cmd[cmd.index("--config") + 1], str(cfg_path))
        args = json.loads(cfg_path.read_text())["args"]
        # its experts take ~50 of the 64 GB of RAM: less room to park chats (one 64K chat still fits)
        self.assertEqual(args[args.index("--conversation-cache-mib") + 1], "1024")
        self.assertEqual(args[args.index("--prompt-cache") + 1], "10")

    def test_coder_runs_as_its_own_family(self):
        s = Settings(ctx_size=65536, use_vision=True)
        coder = catalog.BY_KEY["flash-next-coder"]
        model = Path("/models/Qwen3.8-Flash-Next-Coder") / coder.files[0]
        a = strata.setup_args(coder, model, s, self.root)
        self.assertEqual((a[a.index("--family") + 1], a[a.index("--model") + 1]), ("coder", "IQ1_M"))
        self.assertEqual(a[a.index("--gguf-dir") + 1], str(model.parent))
        # setup.py names it strata-<family tag><size>.json
        cfg_path = strata.config_path(coder, self.root)
        self.assertEqual(cfg_path.name, "strata-coder-iq1_m.json")
        self.root.mkdir(parents=True)
        cfg_path.write_text(json.dumps({"args": ["--serve"]}))
        strata.tune_config(coder, self.root)
        args = json.loads(cfg_path.read_text())["args"]
        # only 23 GB of experts in RAM: room to park more chats
        self.assertEqual(args[args.index("--conversation-cache-mib") + 1], "8192")

    def test_download_unpacks_and_keeps_an_earlier_install(self):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            z.writestr(f"Strata-{strata.COMMIT}/setup.py", "# new")
            z.writestr(f"Strata-{strata.COMMIT}/serve/server.py", "# server")
            z.writestr(f"Strata-{strata.COMMIT}/setup.sh", "#!/bin/sh\n")
        httpd, url = serve({"/s.zip": (200, buf.getvalue())})
        self.addCleanup(httpd.shutdown)
        (self.root / ".venv").mkdir(parents=True)
        (self.root / "setup.py").write_text("# old")
        seen = []
        strata.download(lambda m, f: seen.append(f), self.root, url + "/s.zip")
        self.assertTrue(strata.installed(self.root))
        self.assertEqual((self.root / "setup.py").read_text(), "# new")
        self.assertTrue((self.root / ".venv").is_dir())
        self.assertEqual(seen[-1], 1.0)
        self.assertIn("/archive/" + strata.COMMIT, strata.ZIP_URL)

    def test_ready_waits_for_strata_to_load(self):
        routes = {"/health": (200, json.dumps({"status": "ok", "loaded": False}).encode())}
        httpd, _ = serve(routes)
        self.addCleanup(httpd.shutdown)
        port = httpd.server_address[1]
        proc = launcher.ServerProcess()
        self.assertFalse(proc.ready(port))
        routes["/health"] = (200, json.dumps({"status": "ok", "loaded": True}).encode())
        self.assertTrue(proc.ready(port))
        routes["/health"] = (503, b'{"error": "loading"}')       # llama.cpp while loading
        self.assertFalse(proc.ready(port))
        routes["/health"] = (200, b'{"status": "ok"}')
        self.assertTrue(proc.ready(port))

    def test_strata_model_name_gets_qwen38_sampling(self):
        self.assertEqual(sampling_for("swift-1.5-iq2_xs", thinking=True)["temperature"], 1.0)
        self.assertEqual(sampling_for("swift-1.5-iq2_xs", thinking=False)["presence_penalty"], 1.5)


if __name__ == "__main__":
    unittest.main()
