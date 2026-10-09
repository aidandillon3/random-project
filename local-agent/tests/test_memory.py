import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("LOCALAGENT_DATA", tempfile.mkdtemp())

from localagent.memory import Memory, fts_query, terms  # noqa: E402


class NotesTest(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.mem = Memory(self.dir / "memory.db")
        self.addCleanup(self.mem.close)

    def test_terms_drop_filler_and_build_prefix_query(self):
        self.assertEqual(terms("Can you fix the budget script for me please?"), ["fix", "budget", "script"])
        self.assertEqual(fts_query("budget script"), '"budget"* OR "script"*')
        self.assertEqual(fts_query("the and of"), "")

    def test_add_search_update_delete(self):
        a = self.mem.add("Aidan's Python projects live in D:/code", "Projects folder")
        self.mem.add("Prefers dark mode in every app", "Theme")
        self.mem.add("Runs Swift-1.5 Qwen3.8 27B on an RTX 5080", "GPU and model")
        hits = self.mem.search_notes("where are my projects")
        self.assertEqual(hits[0].id, a.id)
        # prefix matching: "budgets" finds "budget"
        self.mem.add("The budget spreadsheet is in Documents/Money", "Budget")
        self.assertEqual(self.mem.search_notes("budgets")[0].title, "Budget")
        # same title updates instead of duplicating
        self.mem.add("Python projects moved to E:/dev", "projects folder")
        self.assertEqual(self.mem.count_notes(), 4)
        self.assertIn("E:/dev", self.mem.search_notes("projects")[0].body)
        self.assertEqual(self.mem.search_notes("D:/code"), [])   # old text is gone from the index
        self.assertTrue(self.mem.delete(a.id))
        self.assertEqual(self.mem.search_notes("projects"), [])

    def test_recall_only_returns_good_matches_within_budget(self):
        self.mem.add("Aidan's VR setup: Quest 3 with Virtual Desktop", "VR setup")
        self.mem.add("Use PowerShell 7 for scripts", "Shell")
        self.mem.add("Body tracker uses VMT for SteamVR trackers", "Body tracker output")
        # one shared word in a long request isn't enough
        self.assertEqual(self.mem.recall_for("write me a python script that renames my holiday photos"), [])
        got = self.mem.recall_for("why don't my steamvr trackers show up from the body tracker")
        self.assertEqual([n.title for n in got], ["Body tracker output"])
        # pinned notes are already in the system prompt, so recall skips them
        self.mem.update(got[0].id, pinned=True)
        self.assertEqual(self.mem.recall_for("steamvr trackers body tracker"), [])
        for i in range(30):
            self.mem.add(f"Quest note {i}: quest headset fact number {i} " + "x" * 200, f"quest {i}")
        self.assertLessEqual(sum(len(n.line()) for n in self.mem.recall_for("quest headset fact")), 1500)

    def test_imports_old_memory_md(self):
        d = Path(tempfile.mkdtemp())
        (d / "memory.md").write_text("# Things\n- likes tea (2026-01-01)\n- uses Windows 11 (2026-01-02)\n")
        m = Memory(d / "memory.db")
        self.addCleanup(m.close)
        self.assertEqual(m.count_notes(), 2)
        self.assertFalse((d / "memory.md").exists())
        self.assertTrue((d / "memory.md.imported").exists())


class FileIndexTest(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.mem = Memory(Path(tempfile.mkdtemp()) / "memory.db")
        self.addCleanup(self.mem.close)

    def test_index_search_update_and_remove(self):
        (self.root / "src").mkdir()
        (self.root / "src" / "tracker.py").write_text("def smooth_pose(points):\n    return one_euro(points)\n")
        (self.root / "notes.md").write_text("Shopping list\n" + "filler\n" * 100 + "remember the quarterly invoice\n")
        (self.root / "Budget 2026.xlsx").write_bytes(b"PK\x03\x04 binary")
        (self.root / "node_modules").mkdir()
        (self.root / "node_modules" / "junk.js").write_text("quarterly invoice")
        st = self.mem.index_folder(self.root)
        self.assertEqual(st["updated"], 3)
        hit = self.mem.search_files("quarterly invoice")[0]
        self.assertTrue(hit.path.endswith("notes.md"))
        self.assertEqual(hit.line, 61)                 # the chunk that holds it, not the top of the file
        self.assertIn("[invoice]", hit.snippet)
        # binary files are found by name
        self.assertTrue(self.mem.search_files("budget")[0].path.endswith("Budget 2026.xlsx"))
        # unchanged files are skipped, changed ones re-read, deleted ones dropped
        self.assertEqual(self.mem.index_folder(self.root)["updated"], 0)
        time.sleep(0.01)
        (self.root / "src" / "tracker.py").write_text("def kalman_filter(points):\n    pass\n")
        os.utime(self.root / "src" / "tracker.py", (time.time() + 5, time.time() + 5))
        (self.root / "notes.md").unlink()
        st = self.mem.index_folder(self.root)
        self.assertEqual((st["updated"], st["removed"]), (1, 1))
        self.assertEqual(self.mem.search_files("quarterly"), [])
        self.assertEqual(self.mem.search_files("smooth_pose one_euro"), [])
        self.assertTrue(self.mem.search_files("kalman")[0].path.endswith("tracker.py"))
        self.assertEqual(self.mem.file_count(), 2)


if __name__ == "__main__":
    unittest.main()
