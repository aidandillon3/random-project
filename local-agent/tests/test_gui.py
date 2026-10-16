import gc
import os
import stat
import sys
import tempfile
import time
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("LOCALAGENT_DATA", tempfile.mkdtemp())
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

try:
    from PySide6.QtWidgets import QApplication
except ImportError:  # pragma: no cover
    QApplication = None

from tests.mockserver import MockServer, calls, text  # noqa: E402
from tests.test_launcher import NEW_HELP, moe_model  # noqa: E402


def free_port() -> int:
    import socket

    with socket.socket() as so:
        so.bind(("127.0.0.1", 0))
        return so.getsockname()[1]


FAKE_SERVER = """
import json, sys
from http.server import BaseHTTPRequestHandler, HTTPServer
port = int(sys.argv[sys.argv.index("--port") + 1])
class H(BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def do_GET(self):
        body = {"/health": {"status": "ok", "loaded": True, "service": "strata"},
                "/v1/models": {"data": [{"id": "swift-1.5-iq2_xs"}]}}.get(self.path, {})
        data = json.dumps(body).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)
print("ready: http://127.0.0.1:%d/v1" % port, flush=True)
HTTPServer(("127.0.0.1", port), H).serve_forever()
"""


def fake_strata(root: Path):
    """A stand-in for Strata's folder: setup.sh records its arguments and writes the config and the venv."""
    (root / "serve").mkdir(parents=True)
    (root / "setup.py").write_text("")
    (root / "serve" / "server.py").write_text(FAKE_SERVER)
    (root / "setup.sh").write_text(
        "#!/bin/sh\ncd \"$(dirname \"$0\")\"\necho \"$@\" > setup-args.txt\nmkdir -p .venv/bin\n"
        f"ln -sf {sys.executable} .venv/bin/python\necho {{}} > strata-swift-iq2_xs.json\necho setup done\n")


def serving_llama(d: Path, help_delay: float = 0.0) -> Path:
    """A llama-server stand-in: --help prints the options (after help_delay seconds), otherwise it serves."""
    (d / "help.txt").write_text(NEW_HELP)
    exe = d / "llama-server"
    exe.write_text(f"#!{sys.executable}\nimport sys, time\nif '--help' in sys.argv or '--version' in sys.argv:\n"
                   f"    time.sleep({help_delay}); print(open({str(d / 'help.txt')!r}).read()); sys.exit(0)\n"
                   + FAKE_SERVER)
    exe.chmod(0o755)
    return exe


def flash_files(d: Path):
    from localagent import catalog

    flash = catalog.BY_KEY["swift-flash-next"]
    (d / flash.folder).mkdir()
    for _, name in flash.all_files():
        with open(d / flash.folder / name, "wb") as f:
            f.truncate(flash.sizes[name])          # sparse: right size, no real data
    return flash


def settle_garbage(app, collector):
    """Free the windows the tests left behind here on the GUI thread, then hand collection back."""
    collector.stop()
    gc.collect()
    app.processEvents()


@unittest.skipIf(QApplication is None, "PySide6 not installed")
class GuiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from localagent import gui

        cls.gui = gui
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setStyle("Fusion")
        cls.app.setPalette(gui.dark_palette())
        cls.app.setStyleSheet(gui.STYLE)
        cls.collector = gui.GuiThreadGC().start()      # as in the app: no Qt garbage freed on worker threads

    @classmethod
    def tearDownClass(cls):
        settle_garbage(cls.app, cls.collector)

    def wait_until(self, cond, timeout=15):
        end = time.time() + timeout
        while time.time() < end:
            self.app.processEvents()
            if cond():
                return True
            time.sleep(0.02)
        return False

    def test_chat_round_trip_with_approval(self):
        from localagent.config import Settings
        from localagent.permissions import Decision

        ws = Path(tempfile.mkdtemp())
        srv = MockServer([calls(("todo_write", {"todos": [{"content": "write file", "status": "in_progress"}]})),
                          calls(("write_file", {"path": "hello.txt", "content": "hi"})),
                          text("Done! I wrote **hello.txt**.", reasoning="easy")])
        self.addCleanup(srv.close)
        s = Settings(base_url=srv.url, workspace=str(ws), enable_control=False)
        seen = []

        def fake_exec(dlg):
            seen.append(dlg.windowTitle())
            dlg.decision = Decision(True)
            return 1

        orig = self.gui.ApprovalDialog.exec
        self.gui.ApprovalDialog.exec = fake_exec
        self.addCleanup(setattr, self.gui.ApprovalDialog, "exec", orig)

        w = self.gui.MainWindow(s)
        w.show()
        chat = w.chat
        # the thinking picker in the chat bar changes the setting straight away
        chat.think.setCurrentIndex(chat.think.findData("low"))
        self.assertEqual(s.thinking, "low")
        s.thinking = "medium"           # changed elsewhere: the chat bar shows it when the chat comes back
        w.nav.setCurrentRow(2)
        self.assertFalse(hasattr(w.settings_page, "thinking"))    # one place to pick it: the chat bar
        w.nav.setCurrentRow(0)
        self.assertEqual(chat.think.currentData(), "medium")
        chat.input.setPlainText("write hello.txt")
        chat.send()
        self.assertTrue(self.wait_until(lambda: not chat.busy() and chat.send_btn.isEnabled()))
        self.wait_until(lambda: False, 0.3)   # let Qt show the widgets it just added
        self.assertEqual((ws / "hello.txt").read_text(), "hi")
        self.assertEqual(seen, ["Allow this action?"])
        self.assertIn("write file", chat.todo.text())
        bodies = [chat.feed.itemAt(i).widget() for i in range(chat.feed.count() - 1)]
        blocks = [b for b in bodies if isinstance(b, self.gui.AssistantBlock) and not b.isHidden()]
        self.assertIn("hello.txt", blocks[-1].text)
        self.assertFalse(blocks[-1].think_btn.isHidden())
        self.assertTrue(list((Path(os.environ["LOCALAGENT_DATA"]) / "chats").glob("*.json")))
        out = os.environ.get("GUI_SHOT")
        if out:
            w.grab().save(out)
        # reopening the saved chat renders it again
        chat.new_chat()
        chat._ensure_agent()
        import json

        f = sorted((Path(os.environ["LOCALAGENT_DATA"]) / "chats").glob("*.json"))[-1]
        chat.agent.load_json(json.loads(f.read_text()))
        chat._render_history()
        self.assertGreater(chat.feed.count(), 3)
        w.close()

    def test_server_page_builds_command(self):
        from localagent.config import Settings

        d = Path(tempfile.mkdtemp())
        model = d / "Qwen3.8-27B-Q4_K_M.gguf"
        moe_model(model)
        from tests.test_launcher import projector

        projector(d / "mmproj-F16.gguf", 2048)
        exe = d / "llama-server"
        exe.write_text("#!/bin/sh\ncat <<'EOF'\n" + NEW_HELP + "\nEOF\n")
        exe.chmod(exe.stat().st_mode | stat.S_IEXEC)
        s = Settings(server_exe=str(exe), models_dir=str(d), base_url="http://127.0.0.1:9/v1")
        w = self.gui.MainWindow(s)
        page = w.server
        self.assertTrue(self.wait_until(lambda: page.model_box.findData(str(model)) >= 0, 10))
        # a first run: the folder scan doesn't pick one (the "pick a model" card stays up)
        self.assertEqual((page.model_box.currentData(), s.model_path), ("", ""))
        page.model_box.setCurrentIndex(page.model_box.findData(str(model)))
        self.assertTrue(self.wait_until(lambda: "--fit on" in page.cmd.toPlainText(), 10))
        cmd = page.cmd.toPlainText()
        self.assertIn("--mmproj", cmd)
        self.assertIn("MoE", page.info_label.text())
        page.speed.setCurrentIndex(page.speed.findData("mtp"))
        self.assertIn("draft-mtp", page.cmd.toPlainText())

        # "Find vision file online": downloads next to the model and turns vision on (MTP stays on)
        from localagent import gguf, vision
        from tests.test_vision import FakeHF

        hf = FakeHF()
        orig_find, orig_q = vision.find, self.gui.QMessageBox.question
        vision.find = lambda info, model, progress=None: orig_find(info, model, base=hf.url)
        self.gui.QMessageBox.question = staticmethod(lambda *a, **k: self.gui.QMessageBox.Yes)
        try:
            page.vision.setChecked(False)
            page._find_vision()
            self.assertTrue(self.wait_until(lambda: "Vision ready" in page.vision_status.text(), 15),
                            page.vision_status.text())
        finally:
            vision.find, self.gui.QMessageBox.question = orig_find, orig_q
        self.assertTrue(page.vision.isChecked())
        self.assertEqual(page.speed.currentData(), "mtp")
        # the test model is 2048 wide, so the 2048 projector is the one that fits
        self.assertEqual(Path(s.mmproj_path).parent, d)
        self.assertEqual(gguf.read_meta_bytes(Path(s.mmproj_path).read_bytes())["clip.vision.projection_dim"], 2048)
        self.assertIn(s.mmproj_path, page.cmd.toPlainText())
        out = os.environ.get("GUI_SHOT")
        if out:
            w.nav.setCurrentRow(1)
            w.resize(1280, 1000)
            self.app.processEvents()
            w.grab().save(out.replace(".png", "_server.png"))
        w.close()

    def test_ready_made_mode_switches_model_and_settings(self):
        from localagent import catalog
        from localagent.config import Settings

        d = Path(tempfile.mkdtemp())
        flash = catalog.BY_KEY["swift-flash-next"]
        folder = d / flash.folder
        folder.mkdir()
        for _, name in flash.all_files():
            with open(folder / name, "wb") as f:
                f.truncate(flash.sizes[name])          # sparse: right size, no real data
        exe = d / "llama-server"
        exe.write_text("#!/bin/sh\ncat <<'EOF'\n" + NEW_HELP + "\nEOF\n")
        exe.chmod(exe.stat().st_mode | stat.S_IEXEC)
        s = Settings(server_exe=str(exe), models_dir=str(d), base_url="http://127.0.0.1:9/v1", speedup="mtp",
                     ctx_size=32768)
        w = self.gui.MainWindow(s)
        page = w.server
        self.assertTrue(self.wait_until(lambda: page.exe is not None, 10))
        page.preset_box.setCurrentIndex(page.preset_box.findData("swift-27b"))
        self.assertIn("Needs a 11.1 GB download", page.preset_info.text())
        page.preset_box.setCurrentIndex(page.preset_box.findData("swift-flash-next"))
        self.assertIn("On this PC", page.preset_info.text())
        self.assertTrue(page.strata_box.isChecked())       # Strata by default for Flash Next...
        page.strata_box.setChecked(False)                  # ...this test runs it on llama.cpp
        page._use_preset()
        self.assertEqual(s.engine, "llama.cpp")
        self.assertEqual(Path(s.model_path).name, flash.files[0])
        self.assertEqual(Path(s.mmproj_path).name, "mmproj-Swift-Qwen3.8-Flash-Next-BF16.gguf")
        self.assertEqual((s.ctx_size, s.speedup), (65536, "off"))
        self.assertEqual(page.ctx.currentData(), 65536)
        self.assertEqual(page.speed.currentData(), "off")
        cmd = page.cmd.toPlainText()
        self.assertIn("-c 65536", cmd)
        self.assertIn("mmproj-Swift-Qwen3.8-Flash-Next-BF16.gguf", cmd)
        self.assertNotIn("draft-mtp", cmd)
        page.stop()
        # files copied in after the folder scan are found when the mode is picked
        self.assertTrue(self.wait_until(lambda: not page._scanning.is_alive(), 10))
        self.app.processEvents()
        b27 = catalog.BY_KEY["swift-27b"]
        (d / b27.folder).mkdir()
        for _, name in b27.all_files():
            with open(d / b27.folder / name, "wb") as f:
                f.truncate(b27.sizes[name])
        page.preset_box.setCurrentIndex(page.preset_box.findData("swift-27b"))
        self.assertTrue(self.wait_until(lambda: "On this PC" in page.preset_info.text(), 10), page.preset_info.text())
        w.close()

    def test_flash_next_runs_on_strata(self):
        from localagent import catalog, strata
        from localagent.config import Settings

        d = Path(tempfile.mkdtemp())
        flash = catalog.BY_KEY["swift-flash-next"]
        folder = d / flash.folder
        folder.mkdir()
        for _, name in flash.all_files():
            with open(folder / name, "wb") as f:
                f.truncate(flash.sizes[name])
        root = d / "strata"
        fake_strata(root)
        old, strata.STRATA_DIR = strata.STRATA_DIR, root
        self.addCleanup(setattr, strata, "STRATA_DIR", old)
        port = free_port()
        s = Settings(models_dir=str(d), base_url="http://127.0.0.1:9/v1", port=port, use_vision=True)
        w = self.gui.MainWindow(s)
        page = w.server
        page.preset_box.setCurrentIndex(page.preset_box.findData("swift-flash-next"))
        self.assertTrue(page.strata_box.isVisible() or page.strata_box.isVisibleTo(page))
        page._use_preset()                                  # no llama.cpp needed
        self.assertEqual(s.engine, "strata")
        self.assertIn("Runs on Strata", page.plan_label.text())
        self.assertFalse(page.speed.isEnabled())
        self.assertTrue(self.wait_until(lambda: "Ready" in page.state.text(), 30), page.state.text())
        self.assertEqual(s.base_url, f"http://127.0.0.1:{port}/v1")
        setup = (root / "setup-args.txt").read_text().split()
        self.assertEqual(setup[setup.index("--gguf-dir") + 1], str(folder))
        self.assertEqual(setup[setup.index("--context") + 1], "65536")
        self.assertEqual(setup[setup.index("--vision") + 1], "gpu")
        self.assertFalse(strata.needs_setup(flash, Path(s.model_path), s))
        # a second start skips setup and goes straight to the server
        (root / "setup-args.txt").unlink()
        page.stop()
        page.start()
        self.assertTrue(self.wait_until(lambda: "Ready" in page.state.text(), 30), page.state.text())
        self.assertFalse((root / "setup-args.txt").exists())
        # the 27B mode has no Strata switch
        page.preset_box.setCurrentIndex(page.preset_box.findData("swift-27b"))
        self.assertFalse(page.strata_box.isVisibleTo(page))
        page.stop()
        w.close()

    def test_enter_in_approval_dialog_denies(self):
        from PySide6.QtCore import Qt
        from PySide6.QtTest import QTest

        from localagent.permissions import ApprovalRequest

        req = ApprovalRequest("run_command", "run", "Run: rm -rf build", {"command": "rm -rf build"}, dangerous=True)
        dlg = self.gui.ApprovalDialog(req)
        dlg.show()
        self.app.processEvents()
        QTest.keyClick(dlg, Qt.Key_Return)
        self.assertFalse(dlg.decision.allow)
        dlg = self.gui.ApprovalDialog(req)
        dlg.show()
        dlg.note.setText("use git clean instead")
        QTest.keyClick(dlg.note, Qt.Key_Return)
        self.assertFalse(dlg.decision.allow)
        self.assertEqual(dlg.decision.note, "use git clean instead")

    def test_long_names_dont_widen_the_window(self):
        lab = self.gui.ElidedLabel("C:/Users/someone/" + "very-long-folder-name/" * 30)
        lab.resize(200, 20)
        lab.show()
        self.app.processEvents()
        self.assertLess(lab.sizeHint().width(), 400)
        self.assertTrue(lab.text().endswith("…") or "…" in lab.text())
        self.assertTrue(lab.fullText().startswith("C:/Users/someone/"))

    def test_server_ready_then_dies(self):
        from localagent.config import Settings

        port = free_port()
        script = Path(tempfile.mkdtemp()) / "server.py"
        script.write_text(FAKE_SERVER)
        s = Settings(base_url="http://127.0.0.1:9/v1", port=port)
        w = self.gui.MainWindow(s)
        page = w.server
        page.proc.start([sys.executable, str(script), "--port", str(port)], cwd=script.parent)
        page.state.setText("Loading the model...")
        self.assertTrue(self.wait_until(lambda: "Ready" in page.state.text(), 20), page.state.text())
        self.assertEqual(s.base_url, f"http://127.0.0.1:{port}/v1")
        self.assertEqual(page.start_btn.text(), "Stop model")
        self.assertIsNone(w.chat._agent_sig)          # the chat re-reads what the new model can do
        page.proc.proc.kill()
        self.assertTrue(self.wait_until(lambda: "stopped" in page.state.text(), 10), page.state.text())
        self.assertTrue(page.log.isVisibleTo(page))
        self.assertEqual(page.start_btn.text(), "Start model")
        w.close()

    def test_settings_page_keeps_the_model_servers_address(self):
        from localagent.config import Settings

        s = Settings(base_url="http://127.0.0.1:9/v1")
        w = self.gui.MainWindow(s)
        page = w.settings_page
        s.base_url, s.model = "http://127.0.0.1:8081/v1", ""     # what the Model server page does on Ready
        page.web.setChecked(not page.web.isChecked())             # any other switch on the Settings page
        self.assertEqual(s.base_url, "http://127.0.0.1:8081/v1")
        w.nav.setCurrentRow(2)
        self.assertEqual(page.url.text(), "http://127.0.0.1:8081/v1")
        self.assertFalse(page.adv.isVisibleTo(page))              # the rarely used settings start folded
        s.base_url = "http://127.0.0.1:8082/v1"      # a model started again while the page was open
        page._save_connection()                     # Enter in the model box, or Test connection
        self.assertEqual((s.base_url, page.url.text()), ("http://127.0.0.1:8082/v1", "http://127.0.0.1:8082/v1"))
        page.url.setText("http://localhost:1234/v1")
        page._save_connection()
        self.assertEqual(s.base_url, "http://localhost:1234/v1")
        w.close()

    def test_start_without_llamacpp_offers_the_download(self):
        from localagent.config import Settings

        d = Path(tempfile.mkdtemp())
        model = d / "Qwen3.8-27B-Q4_K_M.gguf"
        moe_model(model)
        s = Settings(models_dir=str(d), model_path=str(model), base_url="http://127.0.0.1:9/v1",
                     server_exe=str(d / "missing-llama-server"))
        s.extra["autostart_model"] = False
        w = self.gui.MainWindow(s)
        page = w.server
        self.assertTrue(self.wait_until(lambda: page.info is not None, 10))
        page.exe = None
        page._changed()
        page._sync_start_btn()
        self.assertTrue(page.start_btn.isEnabled())
        self.assertIn("isn't on this PC yet", page.plan_label.text())
        asked = []
        orig = self.gui.QMessageBox.question
        self.gui.QMessageBox.question = staticmethod(lambda *a, **k: asked.append(a[2]) or self.gui.QMessageBox.No)
        try:
            page.start()
        finally:
            self.gui.QMessageBox.question = orig
        self.assertTrue(asked and "llama.cpp" in asked[0])
        self.assertFalse(page.proc.running)
        w.close()

    def test_model_menu_switches_mode_and_first_run_card(self):
        from localagent.config import Settings

        s = Settings(base_url="http://127.0.0.1:9/v1")
        w = self.gui.MainWindow(s)
        w.show()
        chat = w.chat
        self.assertTrue(self.wait_until(lambda: chat.setup_card.isVisibleTo(chat), 10))   # nothing set up yet
        picked = []
        w.server._use_preset = lambda: picked.append(w.server.preset_box.currentData())
        chat._fill_model_menu()
        acts = [a for a in chat.model_menu.actions() if a.text().startswith("Swift 1.5 Qwen3.8 27B")]
        acts[0].trigger()
        self.assertEqual(picked, ["swift-27b"])
        self.assertEqual(w.nav.currentRow(), 1)
        told = []
        orig = self.gui.QMessageBox.information
        self.gui.QMessageBox.information = staticmethod(lambda *a, **k: told.append(a[2]))
        self.addCleanup(setattr, self.gui.QMessageBox, "information", orig)
        w.server._dl_state = {"stop": __import__("threading").Event()}       # a mode downloading
        chat._fill_model_menu()
        [a for a in chat.model_menu.actions() if a.text().startswith("Swift 1.5 Qwen3.8 Flash")][0].trigger()
        self.assertEqual(picked, ["swift-27b"])                              # not paused, not switched
        self.assertFalse(w.server._dl_state["stop"].is_set())
        self.assertTrue(told and "downloading" in told[0])
        w.server._dl_state = None
        out = os.environ.get("GUI_SHOT")
        if out:
            w.nav.setCurrentRow(0)
            w.resize(1280, 860)
            self.app.processEvents()
            w.grab().save(out.replace(".png", "_firstrun.png"))
            w.nav.setCurrentRow(2)
            self.app.processEvents()
            w.grab().save(out.replace(".png", "_settings.png"))
        w.close()

    def test_addon_tools_from_settings(self):
        import json

        from localagent import mcp
        from localagent.config import Settings
        from localagent.tools import REGISTRY
        from tests.test_mcp import FAKE

        d = Path(tempfile.mkdtemp())
        (d / "fake.py").write_text(FAKE)
        mcp.config_path().parent.mkdir(parents=True, exist_ok=True)
        mcp.config_path().write_text(json.dumps({"mcpServers": {"fake": {"command": sys.executable,
                                                                         "args": [str(d / "fake.py")]}}}))
        self.addCleanup(mcp.config_path().unlink)
        self.addCleanup(mcp.manager().stop)
        w = self.gui.MainWindow(Settings(base_url="http://127.0.0.1:9/v1"))
        w.nav.setCurrentRow(2)
        page = w.settings_page
        self.assertTrue(self.wait_until(lambda: "fake: 6 tools" in page.addons_status.text(), 20),
                        page.addons_status.text())
        self.assertIn("fake__echo", REGISTRY)
        w.close()
        self.assertNotIn("fake__echo", REGISTRY)        # closing the app stops the servers

    def test_a_cancelled_strata_start_leaves_the_next_model_alone(self):
        import threading

        from localagent import strata
        from localagent.config import Settings

        d = Path(tempfile.mkdtemp())
        flash_files(d)
        exe = serving_llama(d)
        root = d / "strata"
        old_dir, old_dl = strata.STRATA_DIR, strata.download
        self.addCleanup(setattr, strata, "STRATA_DIR", old_dir)
        self.addCleanup(setattr, strata, "download", old_dl)
        strata.STRATA_DIR = root
        release = threading.Event()

        def stuck_download(say, *a, **k):       # a download that can't be interrupted
            say("Downloading Strata from GitHub...", 0.0)
            release.wait(10)
            fake_strata(root)
            return root

        strata.download = stuck_download
        s = Settings(server_exe=str(exe), models_dir=str(d), base_url="http://127.0.0.1:9/v1", port=free_port())
        s.extra["autostart_model"] = False
        w = self.gui.MainWindow(s)
        page = w.server
        self.assertTrue(self.wait_until(lambda: page.exe is not None, 10))
        orig = self.gui.QMessageBox.question
        self.gui.QMessageBox.question = staticmethod(lambda *a, **k: self.gui.QMessageBox.Yes)
        self.addCleanup(setattr, self.gui.QMessageBox, "question", orig)
        page.preset_box.setCurrentIndex(page.preset_box.findData("swift-flash-next"))
        page._use_preset()
        self.assertTrue(self.wait_until(lambda: "Downloading Strata" in page.state.text(), 5))
        w.chat._fill_model_menu()
        self.assertIn("Stop the model", [a.text() for a in w.chat.model_menu.actions()])
        page._start_stop()                                  # Cancel setup
        page.strata_box.setChecked(False)                   # run it on llama.cpp instead
        page._start_stop()
        llama = page.proc.proc
        self.assertTrue(self.wait_until(lambda: "Ready" in page.state.text(), 20), page.state.text())
        release.set()                                       # the old start's download ends now
        self.assertTrue(self.wait_until(lambda: not page._strata_job.is_alive(), 10))
        self.wait_until(lambda: False, 0.5)
        self.assertIs(page.proc.proc, llama)                # still the one Stop stops
        self.assertIsNone(llama.poll())
        self.assertFalse((root / "setup-args.txt").exists())   # setup never ran
        self.assertIn("Ready", page.state.text())
        page.stop()
        self.assertIsNotNone(llama.poll())
        w.close()

    def test_strata_setup_keeps_the_settings_it_started_with(self):
        import json

        from localagent import strata
        from localagent.config import Settings

        d = Path(tempfile.mkdtemp())
        flash = flash_files(d)
        root = d / "strata"
        fake_strata(root)
        sh = (root / "setup.sh").read_text().replace("echo setup done", "sleep 1\necho setup done")
        (root / "setup.sh").write_text(sh)                  # setup takes a while (it downloads ~6 GB)
        old, strata.STRATA_DIR = strata.STRATA_DIR, root
        self.addCleanup(setattr, strata, "STRATA_DIR", old)
        port = free_port()
        s = Settings(models_dir=str(d), base_url="http://127.0.0.1:9/v1", port=port)
        s.extra["autostart_model"] = False
        w = self.gui.MainWindow(s)
        page = w.server
        page.preset_box.setCurrentIndex(page.preset_box.findData("swift-flash-next"))
        page._use_preset()
        self.assertTrue(self.wait_until(lambda: "Running Strata's setup" in page.state.text(), 10))
        page.ctx.setCurrentIndex(page.ctx.findData(131072))     # changed while setup runs...
        page._show_advanced(True)
        page.port.setValue(port + 1)                             # ...and the port too
        self.assertTrue(self.wait_until(lambda: "Ready" in page.state.text(), 30), page.state.text())
        args = (root / "setup-args.txt").read_text().split()
        self.assertEqual(args[args.index("--context") + 1], "65536")
        self.assertEqual(json.loads((root / strata.STAMP).read_text())["context"], 65536)
        self.assertTrue(strata.needs_setup(flash, Path(s.model_path), s))     # the next start sets up again
        self.assertEqual(s.base_url, f"http://127.0.0.1:{port}/v1")         # where it really runs
        page.stop()
        w.close()

    def test_start_before_the_folder_scan_is_back(self):
        import time as _time

        from localagent.config import Settings

        d = Path(tempfile.mkdtemp())
        model = d / "Qwen3.8-27B-Q4_K_M.gguf"
        moe_model(model)
        exe = serving_llama(d, help_delay=1.0)
        orig_gpu = self.gui.gpu_info
        self.gui.gpu_info = lambda: (_time.sleep(1.5), ("RTX 4070", 12.0, 1.0))[1]     # a slow nvidia-smi
        self.addCleanup(setattr, self.gui, "gpu_info", orig_gpu)
        s = Settings(server_exe=str(exe), models_dir=str(d), model_path=str(model), base_url="http://127.0.0.1:9/v1",
                     port=free_port())
        s.extra["autostart_model"] = False
        w = self.gui.MainWindow(s)
        page = w.server
        self.wait_until(lambda: page._scanning is not None, 2)
        page._scan()                     # a second scan (a new folder, a mode switch) before the first is back
        asked = []
        orig = self.gui.QMessageBox.question
        self.gui.QMessageBox.question = staticmethod(lambda *a, **k: asked.append(a[2]) or self.gui.QMessageBox.No)
        self.addCleanup(setattr, self.gui.QMessageBox, "question", orig)
        w.chat._fill_model_menu()
        [a for a in w.chat.model_menu.actions() if a.text().startswith("Start ")][0].trigger()
        t0 = _time.time()
        self.app.processEvents()
        self.assertLess(_time.time() - t0, 0.5)           # --help is read on a worker thread
        self.assertEqual(asked, [])                       # llama.cpp is there: no download offered
        self.assertTrue(self.wait_until(lambda: page.proc.running, 15), page.state.text())
        self.assertTrue(self.wait_until(lambda: page.vram == 12.0, 10))      # the GPU still got read
        self.assertIn("RTX 4070", page.gpu.text())
        page.stop()
        w.close()

    def test_one_llama_download_at_a_time(self):
        import threading

        from localagent import launcher
        from localagent.config import Settings

        d = Path(tempfile.mkdtemp())
        model = d / "Qwen3.8-27B-Q4_K_M.gguf"
        moe_model(model)
        exe = serving_llama(d)
        calls = []
        done = threading.Event()

        def fake_download(progress, prefer="cuda"):
            calls.append(1)
            progress("Downloading llama-b9999-bin-win-cuda-12.4-x64.zip...", 0.5)
            done.wait(10)
            return exe

        orig_dl = launcher.download_llamacpp
        launcher.download_llamacpp = fake_download
        self.addCleanup(setattr, launcher, "download_llamacpp", orig_dl)
        s = Settings(models_dir=str(d), model_path=str(model), base_url="http://127.0.0.1:9/v1",
                     server_exe=str(d / "missing"), port=free_port())
        s.extra["autostart_model"] = False
        w = self.gui.MainWindow(s)
        page = w.server
        self.assertTrue(self.wait_until(lambda: page.info is not None, 10))
        asked = []
        orig = self.gui.QMessageBox.question
        self.gui.QMessageBox.question = staticmethod(lambda *a, **k: asked.append(1) or self.gui.QMessageBox.Yes)
        self.addCleanup(setattr, self.gui.QMessageBox, "question", orig)
        page._start_stop()                    # "download llama.cpp first?" Yes
        self.wait_until(lambda: False, 0.3)
        self.assertEqual(page.start_btn.text(), "Downloading llama.cpp...")
        self.assertFalse(page.start_btn.isEnabled())
        page.start()                          # the chat's Model menu: Start
        self.assertEqual((len(calls), len(asked)), (1, 1))
        done.set()
        self.assertTrue(self.wait_until(lambda: page.proc.running, 15), page.state.text())
        self.assertEqual(page.start_btn.text(), "Stop model")
        page.stop()
        w.close()

    def test_background_commands_outlive_a_new_agent(self):
        from localagent.config import Settings
        from localagent.tools import REGISTRY

        s = Settings(base_url="http://127.0.0.1:9/v1", workspace=tempfile.mkdtemp(), approval_mode="auto")
        w = self.gui.MainWindow(s)
        chat = w.chat
        chat._ensure_agent()
        REGISTRY["run_command"].func(chat.agent.ctx, {"command": f'"{sys.executable}" -c "import time; time.sleep(60)"',
                                                      "background": True})
        proc = list(chat.agent.ctx.processes.values())[0]["proc"]
        chat._agent_sig = None                # a model became ready, or a setting changed: a new Agent
        chat._ensure_agent()
        self.assertIn(proc, [b["proc"] for b in chat.agent.ctx.processes.values()])
        chat.new_chat()
        self.assertIsNotNone(proc.wait(10))   # New chat stops it
        w.close()

    def test_ready_is_checked_on_the_port_it_started_on(self):
        from localagent.config import Settings

        port = free_port()
        script = Path(tempfile.mkdtemp()) / "server.py"
        script.write_text("import time; time.sleep(1)\n" + FAKE_SERVER)     # takes a moment to load
        s = Settings(base_url="http://127.0.0.1:9/v1", port=port)
        s.extra["autostart_model"] = False
        w = self.gui.MainWindow(s)
        page = w.server
        page.proc.start([sys.executable, str(script), "--port", str(port)], cwd=script.parent)
        page.state.setText("Loading the model...")
        page._show_advanced(True)
        page.port.setValue(port + 1)          # changed while it loads
        self.assertTrue(self.wait_until(lambda: "Ready" in page.state.text(), 15), page.state.text())
        self.assertEqual(s.base_url, f"http://127.0.0.1:{port}/v1")
        page.stop()
        w.close()

    def test_a_late_closing_think_tag_ends_the_thinking_line(self):
        from types import SimpleNamespace

        from localagent.config import Settings

        w = self.gui.MainWindow(Settings(base_url="http://127.0.0.1:9/v1"))
        chat = w.chat
        chat._on_assistant_start()
        blk = chat.current
        blk.add_text("The user wants a file listing. I should call list_dir.")
        blk.refresh()
        chat._on_assistant_end(SimpleNamespace(reasoning="The user wants a file listing. I should call list_dir.",
                                               content="", usage={}))
        self.assertTrue(blk.think_btn.text().startswith("▸ Thought for"), blk.think_btn.text())
        self.assertFalse(blk.preview.isVisibleTo(blk))
        w.close()

    def test_memory_page(self):
        from localagent import memory
        from localagent.config import Settings

        ws = Path(tempfile.mkdtemp())
        (ws / "plan.md").write_text("the zeppelin launch checklist")
        s = Settings(base_url="http://127.0.0.1:9/v1", workspace=str(ws))
        w = self.gui.MainWindow(s)
        page = w.memory
        mem = memory.get_memory()
        mem.add("Aidan's favourite colour is teal", "Colour")
        w.nav.setCurrentRow(3)
        self.assertTrue(any("teal" in page.list.item(i).text() for i in range(page.list.count())))
        page.search.setText("colour")
        self.assertEqual(page.list.count(), 1)
        page.list.setCurrentRow(0)
        page._pin()
        self.assertTrue(page.list.item(0).text().startswith("📌"))
        self.assertIn("(work folder)", page.folders.item(0).text())
        # the background indexer picks up the work folder
        self.assertTrue(self.wait_until(lambda: mem.search_files("zeppelin"), 15))
        self.assertTrue(self.wait_until(lambda: "indexed. Last check" in page.index_status.text(), 5),
                        page.index_status.text())
        out = os.environ.get("GUI_SHOT")
        if out:
            w.resize(1280, 860)
            self.app.processEvents()
            w.grab().save(out.replace(".png", "_memory.png"))
        memory.indexer().stop()
        w.close()


@unittest.skipIf(QApplication is None, "PySide6 not installed")
class DesktopGuiTest(unittest.TestCase):
    """The virtual computer in the window: side panel, watch window, setup, approvals."""

    @classmethod
    def setUpClass(cls):
        from localagent import gui

        cls.gui = gui
        cls.app = QApplication.instance() or QApplication([])
        cls.collector = gui.GuiThreadGC().start()

    @classmethod
    def tearDownClass(cls):
        settle_garbage(cls.app, cls.collector)

    def setUp(self):
        from unittest import mock

        from localagent import vdesktop
        from PySide6.QtCore import QBuffer, QByteArray, QIODevice
        from PySide6.QtGui import QColor, QImage
        from tests.test_vdesktop import FAKE_GUEST, FakeDesktop

        self.vd = vdesktop
        tmp = Path(tempfile.mkdtemp())
        script = tmp / "fake_guest.py"
        script.write_text(FAKE_GUEST, encoding="utf-8")
        img = QImage(64, 40, QImage.Format_RGB32)
        img.fill(QColor("#336699"))
        data = QByteArray()
        buf = QBuffer(data)
        buf.open(QIODevice.WriteOnly)
        img.save(buf, "JPG")
        jpeg = bytes(data)

        class Desk(FakeDesktop):
            peeks = 0

            def peek(self, width=640, quality=70):
                frame = super().peek(width, quality)
                if frame:
                    Desk.peeks += 1
                    frame["jpeg"] = jpeg
                return frame

        self.desk = Desk(script)
        self.addCleanup(self.desk.stop)
        for target, attr, value in [(vdesktop, "_desktop", self.desk), (vdesktop, "HOME", tmp / "vd"),
                                    (vdesktop, "status", lambda: {"kind": "wsl", "running": self.desk.running,
                                                                  "installed": True, "problem": "", "wsl": True})]:
            patcher = mock.patch.object(target, attr, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def wait_until(self, cond, timeout=15):
        end = time.time() + timeout
        while time.time() < end:
            self.app.processEvents()
            if cond():
                return True
            time.sleep(0.02)
        return False

    def test_the_agent_clicks_on_the_virtual_computer_and_the_panel_shows_it(self):
        from localagent.config import Settings
        from localagent.permissions import Decision

        ws = Path(tempfile.mkdtemp())
        srv = MockServer([calls(("click", {"x": 500, "y": 300})), text("Clicked it.")])
        self.addCleanup(srv.close)
        s = Settings(base_url=srv.url, workspace=str(ws), computer="virtual", approval_mode="ask",
                     auto_memory=False, auto_recall=False)
        asked = []

        def fake_exec(dlg):
            asked.append(dlg.head.text())
            dlg.decision = Decision(True)
            return 1
        orig = self.gui.ApprovalDialog.exec
        self.gui.ApprovalDialog.exec = fake_exec
        self.addCleanup(setattr, self.gui.ApprovalDialog, "exec", orig)
        w = self.gui.MainWindow(s)
        w.show()
        chat, panel = w.chat, w.chat.desk_panel
        self.assertTrue(panel.isVisible())
        self.assertIn("own virtual computer", chat.welcome_text.text())
        hidden = []
        chat.bridge.hide_window.connect(lambda waiter: hidden.append(waiter) or waiter.event.set())
        chat.input.setPlainText("click the button")
        chat.send()
        self.assertTrue(self.wait_until(lambda: not chat.busy() and chat.send_btn.isEnabled()))
        self.assertEqual(asked, ["The AI wants to use its virtual computer (not your desktop)."])
        self.assertEqual(hidden, [])                       # the app window stays put
        self.assertTrue(self.wait_until(lambda: panel.activity.count() == 1))
        self.assertEqual(panel.activity.item(0).text()[8:], "  ✓  left click x1 at (500, 300)")
        self.assertIn("Running", panel.state.text())
        self.assertEqual(panel.start_btn.text(), "Stop")
        self.assertTrue(self.wait_until(lambda: panel.view.pm is not None, 5))    # the live view
        sent = [m for m in srv.requests[0]["tools"] if m["function"]["name"] == "screenshot"][0]
        self.assertIn("virtual computer", sent["function"]["description"])

        # watching it bigger, and using it yourself
        panel.big_btn.click()
        win = panel.window
        self.assertTrue(win.isVisible())
        self.assertEqual(win.activity.count(), 1)           # what happened before it opened
        self.assertTrue(self.wait_until(lambda: win.view.pm is not None, 5))
        win.take.setChecked(True)
        self.assertTrue(win.view.interactive)
        got = []
        orig_input = self.desk.user_input
        self.desk.user_input = lambda ev: got.append(ev) or orig_input(ev)
        win.view.user_input.emit({"kind": "click", "x": 7, "y": 8, "button": "left"})
        self.assertTrue(self.wait_until(lambda: got, 5))
        self.assertEqual(got[0]["x"], 7)
        win.close()
        self.assertFalse(win.take.isChecked())

        # Stop from the panel
        panel.start_btn.click()
        self.assertTrue(self.wait_until(lambda: not self.desk.running and "Stopped" in panel.state.text(), 10))

        # back to the real desktop: no panel, the Windows-only tools come back
        w.nav.setCurrentRow(2)
        page = w.settings_page
        page.computer.setCurrentIndex(page.computer.findData("desktop"))
        self.assertEqual(s.computer, "desktop")
        w.nav.setCurrentRow(0)
        self.assertFalse(panel.isVisible())
        chat._ensure_agent()
        self.assertIn("ui_elements", chat.agent.tools)
        w.close()

    def test_closing_the_app_stops_it(self):
        from localagent.config import Settings

        s = Settings(base_url="http://127.0.0.1:9/v1", workspace=tempfile.mkdtemp(), computer="virtual")
        w = self.gui.MainWindow(s)
        w.show()
        self.desk.start(s)
        w.chat.desk_panel.start_btn.setEnabled(True)
        self.assertTrue(self.desk.running)
        w.close()
        self.assertFalse(self.desk.running)

    def test_live_view_maps_clicks_and_keys(self):
        from PySide6.QtCore import QEvent, QPoint, QPointF, Qt
        from PySide6.QtGui import QKeyEvent
        from PySide6.QtTest import QTest

        gui = self.gui
        view = gui.LiveView()
        view.real = (1280, 800)
        view.resize(640, 500)            # letterboxed: 400 high, 50 empty above and below
        self.assertEqual(view.to_real(QPointF(320, 250)), (640, 400))
        self.assertIsNone(view.to_real(QPointF(320, 20)))
        clicked, sent = [], []
        view.activated.connect(lambda: clicked.append(1))
        view.user_input.connect(sent.append)
        QTest.mouseClick(view, Qt.LeftButton, Qt.NoModifier, QPoint(320, 250))
        self.assertEqual((clicked, sent), ([1], []))       # watching only: opens the big view
        view.set_interactive(True)
        QTest.mouseClick(view, Qt.RightButton, Qt.NoModifier, QPoint(320, 250))
        QTest.mousePress(view, Qt.LeftButton, Qt.NoModifier, QPoint(100, 100))
        QTest.mouseRelease(view, Qt.LeftButton, Qt.NoModifier, QPoint(300, 200))
        self.assertEqual(sent, [{"kind": "click", "x": 640, "y": 400, "button": "right"},
                                {"kind": "drag", "x": 200, "y": 100, "x2": 600, "y2": 300}])

        def key(k, mods=Qt.NoModifier, txt=""):
            return gui.key_input(QKeyEvent(QEvent.KeyPress, k, mods, txt))
        self.assertEqual(key(Qt.Key_A, txt="a"), {"kind": "text", "text": "a"})
        self.assertEqual(key(Qt.Key_A, Qt.ControlModifier, "\x01"), {"kind": "keys", "keys": "ctrl+a"})
        self.assertEqual(key(Qt.Key_Return, txt="\r"), {"kind": "keys", "keys": "enter"})
        self.assertEqual(key(Qt.Key_Tab, Qt.ShiftModifier), {"kind": "keys", "keys": "shift+tab"})
        self.assertEqual(key(Qt.Key_Space, txt=" "), {"kind": "text", "text": " "})
        self.assertEqual(key(Qt.Key_F5), {"kind": "keys", "keys": "f5"})
        self.assertIsNone(key(Qt.Key_Shift))

    def test_activity_list(self):
        lst = self.gui.ActivityList()
        lst.add_event({"type": "action", "summary": "Open firefox", "time": 0})
        lst.add_event({"type": "result", "ok": True})
        lst.add_event({"type": "action", "summary": "Bring 'x' to the front", "time": 0})
        lst.add_event({"type": "result", "ok": False, "text": "No window title contains 'x'."})
        lst.add_event({"type": "result", "ok": True})           # a stray result changes nothing
        self.assertEqual(lst.count(), 2)
        self.assertIn("✓  Open firefox", lst.item(0).text())
        self.assertIn("✗  Bring 'x' to the front\nNo window title", lst.item(1).text())

    def test_setup_dialog(self):
        from unittest import mock

        def fake_install(progress, log, stop):
            progress("Downloading Ubuntu (10 of 390 MB)...", 0.03)
            log("Get:1 http://archive.ubuntu.com noble InRelease")
            progress("Installing Firefox (step 4 of 7)...", None)
            return ["Firefox couldn't be installed from Mozilla."]

        with mock.patch.object(self.vd, "status", lambda: {"kind": "wsl", "running": False, "installed": False,
                                                           "problem": "", "wsl": True}), \
                mock.patch.object(self.vd, "install", fake_install):
            dlg = self.gui.DesktopSetupDialog()
            self.assertTrue(self.wait_until(lambda: dlg.go.isEnabled()))
            dlg.go.click()
            self.assertTrue(self.wait_until(lambda: "ready" in dlg.msg.text()))
        self.assertIn("Firefox couldn't be installed", dlg.msg.text())
        self.assertIn("InRelease", dlg.log.toPlainText())
        self.assertEqual(dlg.go.text(), "Update / repair")

        def slow_install(progress, log, stop):
            stop.wait(10)
            raise self.vd.Stopped()

        with mock.patch.object(self.vd, "install", slow_install):
            dlg = self.gui.DesktopSetupDialog()
            self.assertTrue(self.wait_until(lambda: dlg.go.isEnabled()))
            dlg.go.click()
            dlg.reject()                      # Cancel: stops it, closes once it has stopped
            self.assertTrue(self.wait_until(lambda: not dlg.running and not dlg.isVisible(), 5))
        self.assertIn("Cancelled", dlg.msg.text())

        with mock.patch.object(self.vd, "status", lambda: {"kind": "wsl", "running": False, "installed": False,
                                                           "problem": self.vd.NO_WSL, "wsl": False}):
            dlg = self.gui.DesktopSetupDialog()
            self.assertTrue(self.wait_until(lambda: dlg.go.isEnabled()))
            self.assertTrue(dlg.wsl_btn.isVisibleTo(dlg))


if __name__ == "__main__":
    unittest.main()
