import io
import json
import os
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
