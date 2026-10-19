import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("LOCALAGENT_DATA", tempfile.mkdtemp())

from localagent.memory import MAX_NOTE_CHARS, Indexer, Memory, fts_query, terms  # noqa: E402


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
        # changing a note takes its id; the same title alone is a new note (it may be another fact)
        self.mem.add("Python projects moved to E:/dev", "projects folder", replaces=a.id)
        self.assertEqual(self.mem.count_notes(), 4)
        self.assertIn("E:/dev", self.mem.search_notes("projects")[0].body)
        self.assertEqual(self.mem.search_notes("D:/code"), [])   # old text is gone from the index
        b = self.mem.add("Unity projects live in D:/unity", "Projects folder")
        self.assertEqual(self.mem.count_notes(), 5)
        self.assertIn("E:/dev", self.mem.get(a.id).body)
        self.assertTrue(self.mem.delete(a.id))
        self.assertEqual([n.id for n in self.mem.search_notes("projects")], [b.id])

    def test_notes_have_a_length_cap(self):
        with self.assertRaises(ValueError):
            self.mem.add("x " * MAX_NOTE_CHARS, "too long")
        n = self.mem.add("short", "ok")
        with self.assertRaises(ValueError):
            self.mem.update(n.id, body="y" * (MAX_NOTE_CHARS + 1))
        self.assertTrue(self.mem.update(n.id, pinned=True).pinned)

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

    def test_recall_cuts_a_long_note_and_skips_ones_already_shown(self):
        # a note from before the length cap: shown cut down to the budget, not whole
        nid = self.mem.db.execute("INSERT INTO notes(title, body) VALUES(?, ?)",
                                  ("budget tracker", "budget tracker spreadsheet " + "x " * 20000)).lastrowid
        self.mem.db.execute("INSERT INTO notes_fts(rowid, title, body) VALUES(?, ?, ?)",
                            (nid, "budget tracker", "budget tracker spreadsheet " + "x " * 20000))
        got = self.mem.recall_for("update the budget tracker spreadsheet")
        self.assertEqual([n.id for n in got], [nid])
        self.assertLessEqual(len(got[0].line()), 1500)
        self.assertTrue(got[0].line().endswith("..."))
        self.assertEqual(len(self.mem.get(nid).body), len("budget tracker spreadsheet " + "x " * 20000))  # kept whole
        other = self.mem.add("The budget tracker sheet is in Documents/Money", "Budget tracker file")
        self.assertEqual([n.id for n in self.mem.recall_for("budget tracker spreadsheet", exclude={nid})], [other.id])

    def test_a_greeting_brings_up_no_notes(self):
        # "hi" used to match any note with a word starting "hi" (his, high, hinge...), so a bare hello
        # came with the user's latest project attached and the model offered to carry on with it
        self.mem.add("Stormworks ray tracer: his hinge and high-speed hit detection are in tracer.lua", "Stormworks")
        self.mem.add("Aidan's GPU is an RTX 5080; the gpus list in config lists both", "PC")
        for hello in ("hi", "hey", "hello", "thanks!", "good morning", "hi there"):
            self.assertEqual(self.mem.recall_for(hello), [], hello)
        self.assertEqual([n.title for n in self.mem.recall_for("stormworks")], ["Stormworks"])
        self.assertEqual([n.title for n in self.mem.recall_for("gpu")], ["PC"])           # short words: whole
        self.assertEqual([n.title for n in self.mem.recall_for("fix the hit detection")], ["Stormworks"])

    def test_recall_ignores_accents(self):
        self.mem.add("Le café préféré est à Montréal", "cafe")
        self.assertEqual([n.title for n in self.mem.recall_for("Montreal")], ["cafe"])
        self.assertEqual([n.title for n in self.mem.recall_for("cafe prefere")], ["cafe"])
        self.assertEqual(terms("Café Montréal"), ["cafe", "montreal"])
        self.assertEqual(terms("한국어 がっこう"), ["한국어", "がっこう"])   # other scripts stay as they are

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


    def test_folder_inside_another_indexed_folder(self):
        ws = self.root / "home" / "AgentWorkspace"
        ws.mkdir(parents=True)
        (ws / "a.py").write_text("print('alpha')\n")
        (self.root / "home" / "b.md").write_text("bravo\n")
        self.mem.index_folder(ws)
        st = self.mem.index_folder(self.root / "home")          # used to stop with a UNIQUE constraint error
        self.assertEqual((st["seen"], st["updated"]), (2, 1))   # a.py is already indexed and unchanged
        self.assertEqual(self.mem.file_count(), 2)
        self.assertEqual(self.mem.index_folder(ws)["updated"], 0)
        (ws / "a.py").unlink()
        self.assertEqual(self.mem.index_folder(self.root / "home")["removed"], 1)
        self.assertEqual(self.mem.search_files("alpha"), [])
        # the other way round: the parent first, then the folder inside it
        (ws / "c.py").write_text("print('charlie')\n")
        self.mem.index_folder(self.root / "home")
        self.assertEqual(self.mem.index_folder(ws)["updated"], 0)
        # a folder the parent's walk skips (hidden) but that was added on its own keeps its files
        hidden = self.root / "home" / ".secret"
        hidden.mkdir()
        (hidden / "d.md").write_text("delta\n")
        self.mem.index_folder(hidden)
        self.mem.index_folder(self.root / "home")
        self.assertTrue(self.mem.search_files("delta"))

    def test_big_folder_is_read_over_several_runs_and_removal_still_runs(self):
        for i in range(30):
            (self.root / f"f{i:03d}.txt").write_text(f"word{i}\n")
        runs = [self.mem.index_folder(self.root, max_files=10) for _ in range(3)]
        self.assertEqual([r["updated"] for r in runs], [10, 10, 10])
        self.assertEqual([r["left"] for r in runs], [20, 10, 0])
        self.assertEqual(self.mem.file_count(), 30)
        (self.root / "f000.txt").unlink()
        for i in range(30, 50):
            (self.root / f"f{i:03d}.txt").write_text(f"word{i}\n")
        st = self.mem.index_folder(self.root, max_files=10)     # more new files than the cap: removal runs anyway
        self.assertEqual((st["updated"], st["removed"]), (10, 1))
        self.assertEqual(self.mem.search_files("word0"), [])

    def test_indexer_survives_an_error(self):
        class S:
            index_files, index_folders = True, []

            def workspace_path(self):
                raise RuntimeError("no work folder")

        ix = Indexer(S(), every_s=0.05)
        ix.start()
        self.addCleanup(ix.stop)
        for _ in range(100):
            if ix.status.startswith("Indexing failed"):
                break
            time.sleep(0.02)
        self.assertTrue(ix.is_alive())
        self.assertIn("no work folder", ix.status)


if __name__ == "__main__":
    unittest.main()
