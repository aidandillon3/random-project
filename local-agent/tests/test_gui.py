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


@unittest.skipIf(QApplication is None, "PySide6 not installed")
class GuiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from localagent import gui

        cls.gui = gui
        cls.app = QApplication.instance() or QApplication([])
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
        w.nav.setCurrentRow(2)
        w.settings_page.thinking.setCurrentIndex(w.settings_page.thinking.findData("medium"))
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
        page._use_preset()
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
