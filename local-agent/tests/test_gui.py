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
        (d / "mmproj-F16.gguf").write_bytes(b"x" * 100)
        exe = d / "llama-server"
        exe.write_text("#!/bin/sh\ncat <<'EOF'\n" + NEW_HELP + "\nEOF\n")
        exe.chmod(exe.stat().st_mode | stat.S_IEXEC)
        s = Settings(server_exe=str(exe), models_dir=str(d), base_url="http://127.0.0.1:9/v1")
        w = self.gui.MainWindow(s)
        page = w.server
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


if __name__ == "__main__":
    unittest.main()
