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
from tests.test_launcher import NEW_HELP, moe_model, sized_file  # noqa: E402


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
    from localagent import strata
    (root / "serve").mkdir(parents=True)
    (root / "setup.py").write_text("")
    (root / "serve" / "server.py").write_text(FAKE_SERVER)
    (root / strata.INSTALLED).write_text(strata.COMMIT)       # the pinned version, so no update download
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
        sized_file(d / flash.folder / name, flash.sizes[name])
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
        s = Settings(base_url=srv.url, workspace=str(ws))
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
        # no screen control unless Computer use is turned on
        self.assertIn("only uses your mouse, keyboard and screen while Computer use is on", chat.welcome_text.text())
        self.assertFalse(chat.computer_btn.isChecked())
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
        self.assertIn("StepGroup", [type(b).__name__ for b in bodies])        # its steps folded into one line
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
            sized_file(folder / name, flash.sizes[name])
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
            sized_file(d / b27.folder / name, b27.sizes[name])
        page.preset_box.setCurrentIndex(page.preset_box.findData("swift-27b"))
        self.assertTrue(self.wait_until(lambda: "On this PC" in page.preset_info.text(), 10), page.preset_info.text())
        w.close()

    def test_one_bit_mode_runs_on_its_own_engine(self):
        from localagent import catalog, odp
        from localagent.config import Settings

        d = Path(tempfile.mkdtemp())
        p = catalog.BY_KEY["qwen27b-1bit-odp"]
        folder = d / p.folder
        folder.mkdir()
        for _, name in p.all_files():
            sized_file(folder / name, p.sizes[name])
        old, odp.ODP_DIR = odp.ODP_DIR, d / "odp-llama"
        self.addCleanup(setattr, odp, "ODP_DIR", old)
        llama = d / "llama"
        llama.mkdir()
        s = Settings(server_exe=str(serving_llama(llama)), models_dir=str(d), base_url="http://127.0.0.1:9/v1",
                     engine="strata")
        w = self.gui.MainWindow(s)
        page = w.server
        self.assertTrue(self.wait_until(lambda: page.exe is not None, 10))
        page.preset_box.setCurrentIndex(page.preset_box.findData(p.key))
        self.assertFalse(page.strata_box.isVisibleTo(page))
        asked = []
        orig = self.gui.QMessageBox.question
        self.gui.QMessageBox.question = lambda *a, **k: asked.append(a) or self.gui.QMessageBox.No
        self.addCleanup(setattr, self.gui.QMessageBox, "question", orig)
        page._use_preset()
        self.assertTrue(asked)                    # offered to download the engine (said no here)
        page.stop()
        self.assertEqual(Path(s.model_path).name, p.files[0])
        self.assertEqual(s.engine, "strata")      # Flash Next's engine choice stays as it was
        # no engine yet: llama.cpp's own build can't run it, so Start downloads the fork's build first
        self.assertIn("its own build of llama.cpp", page.plan_label.text())
        self.assertEqual(page.cmd.toPlainText(), "")
        odp.ODP_DIR.mkdir()
        engine = serving_llama(odp.ODP_DIR)
        page._changed()
        self.assertIn("needs a newer build", page.plan_label.text())     # build 1 had DLLs missing
        (odp.ODP_DIR / "BUILD.json").write_text('{"build": %d}' % odp.BUILD)
        page._changed()
        self.assertTrue(self.wait_until(lambda: "--odp-blocks" in page.cmd.toPlainText(), 10))
        cmd = page.cmd.toPlainText()
        self.assertTrue(cmd.startswith(str(engine)))
        self.assertIn(str(folder / p.odp["blocks"]), cmd)
        self.assertNotIn("--spec-type", cmd)
        self.assertNotIn("--mmproj", cmd)
        w.close()

    def test_model_and_quant_pickers(self):
        from localagent.config import Settings

        w = self.gui.MainWindow(Settings(base_url="http://127.0.0.1:9/v1"))
        page = w.server
        page._use_preset = lambda: None
        page.preset_box.setCurrentIndex(page.preset_box.findData("swift-flash-next-iq3s"))    # e.g. from the chat
        self.assertEqual(page.family_box.currentText(), "Qwen3.8 Flash Next")
        self.assertEqual(page.quant_box.count(), 4)
        self.assertEqual(page.quant_box.currentData(), "swift-flash-next-iq3s")
        self.assertIn("85 GB", page.quant_box.currentText())
        self.assertFalse(page.preset_box.isVisibleTo(page))
        page.quant_box.setCurrentIndex(page.quant_box.findData("flash-next-coder"))
        self.assertEqual(page.preset_box.currentData(), "flash-next-coder")
        page.family_box.setCurrentIndex(page.family_box.findData("Qwen3.8 27B"))
        self.assertEqual(page.preset_box.currentData(), "swift-27b")                         # its first quant
        self.assertEqual([page.quant_box.itemData(i) for i in range(page.quant_box.count())],
                         ["swift-27b", "swift-27b-iq3s", "qwen27b-1bit-odp"])
        page.family_box.setCurrentIndex(page.family_box.findData(""))
        self.assertEqual(page.preset_box.currentData(), "")
        self.assertTrue(page.quant_box.isHidden())
        self.assertFalse(page.file_group.isHidden())
        w.close()

    def test_flash_next_runs_on_strata(self):
        from localagent import catalog, strata
        from localagent.config import Settings

        d = Path(tempfile.mkdtemp())
        flash = catalog.BY_KEY["swift-flash-next"]
        folder = d / flash.folder
        folder.mkdir()
        for _, name in flash.all_files():
            sized_file(folder / name, flash.sizes[name])
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

    @unittest.skipIf(os.name == "nt", "the stand-in basalt is a shell script")
    def test_leftover_server_on_the_port_is_offered_to_end_first(self):
        import socket
        from localagent import launcher
        from localagent.config import Settings

        held = socket.socket()
        held.bind(("127.0.0.1", 0))
        held.listen()
        port = held.getsockname()[1]
        s = Settings(base_url="http://127.0.0.1:9/v1", port=port, model_path=str(Path(tempfile.mkdtemp()) / "m.basalt"))
        s.extra["autostart_model"] = False
        w = self.gui.MainWindow(s)
        page = w.server
        asked, warned, killed = [], [], []
        orig = (self.gui.QMessageBox.question, self.gui.QMessageBox.warning, launcher.port_owners, launcher.kill_tree)
        try:
            self.gui.QMessageBox.warning = staticmethod(lambda *a, **k: warned.append(a[2]))
            launcher.port_owners = lambda p: []          # held by something we can't name: say so, don't start
            page.start()
            self.assertIn(f"port {port}", warned[-1])
            self.assertFalse(page.proc.running)
            launcher.port_owners = lambda p: [4242]
            self.gui.QMessageBox.question = staticmethod(lambda *a, **k: asked.append(a[2]) or self.gui.QMessageBox.No)
            page.start()
            self.assertIn("still running", asked[-1])
            self.assertFalse(page.proc.running)
            self.gui.QMessageBox.question = staticmethod(lambda *a, **k: self.gui.QMessageBox.Yes)
            launcher.kill_tree = lambda pid: (killed.append(pid), held.close())
            self.assertTrue(page._clear_port())
            self.assertEqual(killed, [4242])
        finally:
            (self.gui.QMessageBox.question, self.gui.QMessageBox.warning, launcher.port_owners,
             launcher.kill_tree) = orig
            held.close()
            w.close()

    def test_basalt_file_runs_on_its_own_build(self):
        from localagent.config import Settings

        port = free_port()
        root = Path(tempfile.mkdtemp()) / "basalt"
        (root / "bin").mkdir(parents=True)
        (root / "models").mkdir()
        (root / "server.py").write_text(FAKE_SERVER)
        (root / "basalt_config.json").write_text('{"engine": {"kv": "int8"}}')
        exe = root / "bin" / "basalt"
        exe.write_text(f'#!/bin/sh\necho "$@" > "{root}/args.txt"\nexec "{sys.executable}" "{root}/server.py" "$@"\n')
        exe.chmod(exe.stat().st_mode | stat.S_IEXEC)
        model = root / "models" / "Qwen3.8-FN-GSQ-RCO-IQ3_S.basalt"
        model.write_bytes(b"BASALT")
        s = Settings(base_url="http://127.0.0.1:9/v1", port=port, model_path=str(model))
        s.extra["autostart_model"] = False
        w = self.gui.MainWindow(s)
        page = w.server
        self.assertTrue(self.wait_until(lambda: "Runs on Basalt" in page.plan_label.text(), 10), page.plan_label.text())
        self.assertIn("Basalt model file", page.info_label.text())
        page.start()
        self.assertTrue(self.wait_until(lambda: "Ready" in page.state.text(), 20), page.state.text())
        self.assertEqual(s.base_url, f"http://127.0.0.1:{port}/v1")
        self.assertEqual((root / "args.txt").read_text().split(),
                         ["serve", "--config", str(root / "basalt_config.json"), "--model", str(model),
                          "--port", str(port), "--max-context", str(s.ctx_size),
                          "--vram-reserve-mib", "1728",        # no GPU found: the desktop is taken to be on it
                          "--relaxed-drafting", "0.3", "--fit-max-tokens"])
        page.stop()
        self.assertEqual(s.basalt_models, [str(model)])                  # remembered for the model lists
        other = Path(tempfile.mkdtemp()) / "Qwen3.8-27B-Q4_K_M.gguf"
        moe_model(other)
        s.model_path = str(other)                                        # another model picked...
        page._scan()
        self.assertTrue(self.wait_until(lambda: page.model_box.findData(str(other)) >= 0, 10))
        self.assertGreaterEqual(page.model_box.findData(str(model)), 0)  # ...the Basalt one is still listed
        w.chat._fill_model_menu()
        menu = [a for a in w.chat.model_menu.actions() if a.text() == "Basalt"][0].menu()
        [a for a in menu.actions() if a.text() == model.name][0].trigger()     # and one click away in the chat
        self.assertTrue(self.wait_until(lambda: page.model_box.currentData() == str(model), 10))
        self.assertEqual(s.model_path, str(model))
        s.model_path = str(Path(tempfile.mkdtemp()) / "x.basalt")      # away from any build: it can't start
        page._changed()
        self.assertIn("No Basalt build found", page.plan_label.text())
        self.assertTrue(self.wait_until(lambda: not page.start_btn.isEnabled(), 5))
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
        menus = {a.text(): a.menu() for a in chat.model_menu.actions() if a.menu()}
        self.assertEqual(list(menus), ["Qwen3.8 27B", "Qwen3.8 Flash Next", "Qwen3.6 35B-A3B", "Ornith 1.5 122B"])       # one entry per model
        self.assertEqual([a.text() for a in menus["Qwen3.8 Flash Next"].actions()][-1],
                         "Coder IQ1_M (for coding, less RAM)")                  # and its quants inside
        menus["Qwen3.8 27B"].actions()[0].trigger()
        self.assertEqual(picked, ["swift-27b"])
        self.assertEqual(w.nav.currentRow(), 1)
        told = []
        orig = self.gui.QMessageBox.information
        self.gui.QMessageBox.information = staticmethod(lambda *a, **k: told.append(a[2]))
        self.addCleanup(setattr, self.gui.QMessageBox, "information", orig)
        w.server._dl_state = {"stop": __import__("threading").Event()}       # a mode downloading
        chat._fill_model_menu()
        [a for a in chat.model_menu.actions() if a.text() == "Qwen3.8 Flash Next"][0].menu().actions()[0].trigger()
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
        self.assertEqual(json.loads((root / strata.STAMP).read_text())["swift:IQ2_XS"]["context"], 65536)
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
        from localagent.gguf import Gpu
        orig_gpu = self.gui.list_gpus
        rtx = Gpu(0, "GPU-1", "RTX 4070", 12.0, 1.0, 8.9)
        self.gui.list_gpus = lambda: (_time.sleep(1.5), [rtx])[1]     # a slow nvidia-smi
        self.addCleanup(setattr, self.gui, "list_gpus", orig_gpu)
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

    def test_pick_the_gpu_with_two(self):
        from localagent.config import Settings
        from localagent.gguf import Gpu

        d = Path(tempfile.mkdtemp())
        model = d / "Qwen3.8-27B-Q4_K_M.gguf"
        moe_model(model)
        exe = serving_llama(d)
        two = [Gpu(0, "GPU-aaa", "RTX 3080 Ti", 12.0, 3.0, 8.6), Gpu(1, "GPU-bbb", "RTX 5080", 16.0, 0.4, 12.0)]
        orig_gpu = self.gui.list_gpus
        self.gui.list_gpus = lambda: two
        self.addCleanup(setattr, self.gui, "list_gpus", orig_gpu)
        s = Settings(server_exe=str(exe), models_dir=str(d), model_path=str(model), base_url="http://127.0.0.1:9/v1",
                     port=free_port())
        s.extra["autostart_model"] = False
        w = self.gui.MainWindow(s)
        page = w.server
        self.assertTrue(self.wait_until(lambda: page._first_scan_done, 10))
        self.assertFalse(page.gpu_row.isHidden())
        items = [page.gpu_pick.itemText(i) for i in range(page.gpu_pick.count())]
        self.assertEqual(items, ["RTX 5080 only", "RTX 3080 Ti only", "Both GPUs",
                                 "RTX 5080, with the RTX 3080 Ti holding experts (Strata)"])
        self.assertEqual(page.gpu_pick.currentText(), "RTX 5080 only")             # the newest card by default
        self.assertEqual(page.vram, 16.0)
        page.gpu_pick.setCurrentIndex(2)
        self.assertEqual(s.gpus, "all")
        self.assertEqual(page.vram, 28.0)
        self.assertIn("RTX 5080 + RTX 3080 Ti", page.gpu.text())
        page.gpu_pick.setCurrentIndex(1)
        self.assertEqual(s.gpus, "GPU-aaa")
        page.gpu_pick.setCurrentIndex(3)
        self.assertEqual((s.gpus, page.vram), ("peer", 16.0))           # planned for the card that runs it
        self.assertIn("holds experts", page.gpu.text())
        page.start()
        self.assertTrue(self.wait_until(lambda: page.proc.running, 15), page.state.text())
        self.assertEqual(page.proc.env["CUDA_VISIBLE_DEVICES"], "1")    # llama.cpp has no expert tier: the 5080 alone
        self.assertTrue(any(line.startswith("GPUs: ") for line in page.proc.lines))
        page.gpu_pick.setCurrentIndex(0)
        self.assertIn("next time", page.gpu_note.text())
        self.assertTrue(page.monitors.isChecked())           # nvidia-smi didn't say where they are: the safe guess
        page.monitors.setChecked(False)
        self.assertEqual(s.monitors_on_ai_gpu, "no")
        self.assertFalse(page._monitors())
        page.gpu_pick.setCurrentIndex(2)                     # another GPU: found again
        self.assertEqual(s.monitors_on_ai_gpu, "auto")
        self.assertTrue(page.monitors.isChecked())
        page.stop()
        w.close()

    def test_one_gpu_shows_no_picker(self):
        from localagent.config import Settings
        from localagent.gguf import Gpu

        d = Path(tempfile.mkdtemp())
        orig_gpu = self.gui.list_gpus
        self.gui.list_gpus = lambda: [Gpu(0, "GPU-bbb", "RTX 5080", 16.0, 3.0, 12.0)]
        self.addCleanup(setattr, self.gui, "list_gpus", orig_gpu)
        s = Settings(models_dir=str(d), base_url="http://127.0.0.1:9/v1", port=free_port())
        s.extra["autostart_model"] = False
        w = self.gui.MainWindow(s)
        page = w.server
        self.assertTrue(self.wait_until(lambda: page._first_scan_done, 10))
        self.assertTrue(page.gpu_row.isHidden())
        self.assertTrue(page.monitors.isChecked())
        self.assertEqual(page.gpu.text(), "GPU: RTX 5080, 16.0 GB (3.0 GB in use right now)")
        w.close()

    def test_computer_use_toggle_and_screen_picker(self):
        import importlib.util
        from unittest import mock

        from localagent.config import Settings

        screens = [{"left": 0, "top": 0, "width": 1920, "height": 1080},
                   {"left": 1920, "top": 0, "width": 2560, "height": 1440}]
        mons = mock.patch.object(self.gui.screen_tools, "monitors", lambda: [dict(m) for m in screens])
        mons.start()
        self.addCleanup(mons.stop)
        s = Settings(base_url="http://127.0.0.1:9/v1")
        s.extra["autostart_model"] = False
        w = self.gui.MainWindow(s)
        w.move(0, 0)
        w.show()
        chat = w.chat
        self.assertFalse(chat.computer_btn.isChecked())
        # the add-ons missing and the offer declined: it stays off
        real = importlib.util.find_spec
        with mock.patch("importlib.util.find_spec", lambda n, *a: None if n == "mss" else real(n, *a)), \
                mock.patch.object(self.gui.QMessageBox, "question", return_value=self.gui.QMessageBox.No):
            chat.computer_btn.click()
        self.assertFalse(chat.computer_btn.isChecked())
        self.assertFalse(s.computer_use)
        # installed: one click turns it on (and it's saved), another turns it off
        with mock.patch("importlib.util.find_spec", lambda n, *a: object()):
            chat.computer_btn.click()
            self.assertTrue(chat.computer_btn.isChecked())
            self.assertTrue(Settings.load().computer_use)
            chat.computer_btn.click()
        self.assertFalse(s.computer_use)
        # the arrow lists the screens; picking one shows in Settings too
        chat._fill_computer_menu()
        labels = [a.text() for a in chat.computer_menu.actions() if a.text()]
        self.assertEqual(labels[:2], ["Look at screen 1: 1920x1080 (main)", "Look at screen 2: 2560x1440"])
        chat.computer_menu.actions()[1].trigger()
        self.assertEqual(s.monitor, 2)
        w.show_settings("computer")
        self.assertEqual(w.settings_page.monitor.currentText(), "Screen 2: 2560x1440")
        w.settings_page.monitor.setCurrentIndex(0)
        self.assertEqual(s.monitor, 1)
        # the window gets out of the way only of the screen it is on
        self.assertTrue(self.gui._window_on_monitor(w, 1))
        self.assertFalse(self.gui._window_on_monitor(w, 2))
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

    def test_steps_fold_into_one_line(self):
        from types import SimpleNamespace

        from localagent.config import Settings
        from localagent.tools import ToolResult

        w = self.gui.MainWindow(Settings(base_url="http://127.0.0.1:9/v1"))
        chat = w.chat
        feed = lambda: [chat.feed.itemAt(i).widget() for i in range(chat.feed.count() - 1)  # noqa: E731
                        if chat.feed.itemAt(i).widget() is not chat.welcome]

        def turn(reasoning, content, calls=()):
            chat._on_assistant_start()
            chat.current.add_reasoning(reasoning)
            chat.current.add_text(content)
            chat._on_assistant_end(SimpleNamespace(reasoning=reasoning, content=content, usage={},
                                                   tool_calls=list(calls)))

        turn("Find mods first.", "Let me search for popular mods.", calls=["web_search"])
        chat._on_tool_start("1", "web_search", {"query": "beamng mods"}, "Search the web for beamng mods")
        group = chat.group
        self.assertIn("Search the web for beamng mods", group.head.text())        # what it is doing now
        chat._on_tool_end("1", "web_search", ToolResult("1. Mods"))
        turn("Open the first one.", "")
        chat._on_tool_start("2", "web_fetch", {"url": "https://x.org"}, "Open https://x.org")
        chat._on_tool_start("3", "web_fetch", {"url": "https://y.org"}, "Open https://y.org")
        chat._on_tool_end("2", "web_fetch", ToolResult("page"))
        chat._on_tool_end("3", "web_fetch", ToolResult("Error", ok=False))
        chat._on_tool_start("4", "download_file", {"url": "https://x.org/a.zip"}, "Download")
        chat._on_tool_end("4", "download_file", ToolResult("Saved"))
        turn("Done.", "I saved 1 mod.")
        items = feed()
        self.assertEqual([type(x).__name__ for x in items], ["StepGroup", "AssistantBlock"])   # one line, then the answer
        self.assertEqual(group.summary(), "Searched the web, read web pages, downloaded files · 4 steps · 1 failed")
        self.assertTrue(group.head.text().startswith("▸ ⚠️ Searched the web"))
        self.assertFalse(group.body.isVisibleTo(group))                          # folded until clicked
        self.assertEqual(group.rows.count(), 6)                                  # a note, a thinking step, 4 tools
        self.assertIn("Let me search", group.rows.itemAt(0).widget().text)          # the note is folded too
        group.toggle()
        self.assertTrue(group.body.isVisibleTo(group))
        self.assertIn("I saved 1 mod.", items[1].text)
        # the next steps start a new line under the answer
        chat._on_tool_start("5", "read_file", {"path": "a"}, "Read a")
        self.assertIsNot(chat.group, group)
        chat._on_tool_end("5", "read_file", ToolResult("x"))
        chat._done_common()
        self.assertIsNone(chat.group)
        # a saved chat shows the same way
        chat.new_chat()
        chat._ensure_agent()
        chat.agent.messages = [
            {"role": "user", "content": "get mods"},
            {"role": "assistant", "content": "", "tool_calls": [{"id": "a"}]},
            {"role": "tool", "name": "web_search", "tool_call_id": "a", "content": "1. Mods"},
            {"role": "tool", "name": "run_command", "tool_call_id": "b", "content": "ok"},
            {"role": "assistant", "content": "Here they are."}]
        chat._render_history()
        self.assertEqual([type(x).__name__ for x in feed()], ["UserBubble", "StepGroup", "AssistantBlock"])
        self.assertEqual(feed()[1].summary(), "Searched the web, ran commands · 2 steps")
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

    def test_chat_list_on_the_left_and_text_size(self):
        import json

        from localagent import chats
        from localagent.config import Settings

        folder = chats.folder()
        folder.mkdir(parents=True, exist_ok=True)
        for f in folder.glob("*.json"):
            f.unlink()
        old = folder / "2026-01-02_10-00-00.json"
        old.write_text(json.dumps({"messages": [
            {"role": "user", "content": "[summary]", "_kind": "summary"},
            {"role": "user", "content": "fix the   crash in my game\nplease"},
            {"role": "assistant", "content": "Fixed it."}]}))
        os.utime(old, (time.time() - 3 * 86400, time.time() - 3 * 86400))
        new = folder / "2026-01-05_10-00-00.json"
        new.write_text(json.dumps({"messages": [{"role": "user", "content": "rename my photos"}]}))
        s = Settings(base_url="http://127.0.0.1:9/v1")
        w = self.gui.MainWindow(s)
        w.show()
        side, chat = w.sidebar, w.chat
        texts = [side.list.item(i).text() for i in range(side.list.count())]
        self.assertEqual(texts, ["Today", "rename my photos", "Previous 7 days", "fix the crash in my game please"])
        side._clicked(side.list.item(3))
        self.assertEqual(chat.chat_file, old)
        self.assertEqual(chat.title.fullText(), "fix the crash in my game please")
        bodies = [chat.feed.itemAt(i).widget() for i in range(chat.feed.count() - 1)]
        self.assertTrue(any(isinstance(b, self.gui.AssistantBlock) and "Fixed it." in b.text for b in bodies))
        self.assertEqual([i.text() for i in side.list.selectedItems()], ["fix the crash in my game please"])
        # renamed: the new name sticks even after the chat changes
        chats.rename(old, "Game crash")
        old.write_text(old.read_text())
        side.refresh()
        self.assertIn("Game crash", [side.list.item(i).text() for i in range(side.list.count())])
        side.search.setText("photos")
        self.assertEqual([side.list.item(i).text() for i in range(side.list.count())], ["Today", "rename my photos"])
        side.search.setText("")
        # deleting the open chat starts a new one
        orig = self.gui.QMessageBox.question
        self.gui.QMessageBox.question = staticmethod(lambda *a, **k: self.gui.QMessageBox.Yes)
        self.addCleanup(setattr, self.gui.QMessageBox, "question", orig)
        side._delete(old, "Game crash")
        self.assertFalse(old.exists())
        self.assertIsNone(chat.chat_file)
        self.assertTrue(chat.welcome.isVisibleTo(chat))
        # text size: Ctrl + / Ctrl -, saved, and Settings shows it
        start = s.chat_text_pt
        chat.change_text_size(1)
        chat.change_text_size(1)
        self.assertEqual(s.chat_text_pt, start + 2)
        self.assertIn(f"font-size: {start + 2}pt", chat.chat_area.styleSheet())
        chat.change_text_size(0)
        self.assertEqual(s.chat_text_pt, self.gui.DEFAULT_TEXT_PT)
        w.nav.setCurrentRow(2)
        self.assertEqual(w.settings_page.text_pt.value(), self.gui.DEFAULT_TEXT_PT)
        w.settings_page.text_pt.setValue(15)
        self.assertEqual(s.chat_text_pt, 15)
        self.assertIn("font-size: 15pt", chat.chat_area.styleSheet())
        # the list hides and comes back (Ctrl+B)
        w.toggle_sidebar()
        self.assertFalse(side.isVisible())
        self.assertFalse(s.sidebar_open)
        w.toggle_sidebar()
        self.assertTrue(side.isVisible())
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


if __name__ == "__main__":
    unittest.main()
