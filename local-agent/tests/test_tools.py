import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("LOCALAGENT_DATA", tempfile.mkdtemp())

from localagent.config import Settings  # noqa: E402
from localagent.tools import REGISTRY, ToolContext, ToolError  # noqa: E402
from localagent.tools.web import html_to_text, parse_ddg  # noqa: E402


def run(ctx, name, **args):
    out = REGISTRY[name].func(ctx, args)
    return out if isinstance(out, str) else out.text


class FileToolsTest(unittest.TestCase):
    def setUp(self):
        self.ws = Path(tempfile.mkdtemp())
        self.ctx = ToolContext(settings=Settings(), workspace=self.ws)

    def test_edit_exact_unique_and_replace_all(self):
        f = self.ws / "a.py"
        f.write_text("x = 1\ny = 1\n")
        with self.assertRaises(ToolError):
            run(self.ctx, "edit_file", path="a.py", old_text="1", new_text="2")
        run(self.ctx, "edit_file", path="a.py", old_text="1", new_text="2", replace_all=True)
        self.assertEqual(f.read_text(), "x = 2\ny = 2\n")
        run(self.ctx, "edit_file", path="a.py", old_text="y = 2", new_text="y = 3")
        self.assertEqual(f.read_text(), "x = 2\ny = 3\n")

    def test_edit_keeps_windows_line_endings(self):
        f = self.ws / "w.txt"
        f.write_bytes(b"one\r\ntwo\r\nthree\r\n")
        run(self.ctx, "edit_file", path="w.txt", old_text="one\ntwo", new_text="1\n2")
        self.assertEqual(f.read_bytes(), b"1\r\n2\r\nthree\r\n")

    def test_edit_forgives_trailing_spaces(self):
        f = self.ws / "t.txt"
        f.write_text("def a():   \n    pass\n")
        out = run(self.ctx, "edit_file", path="t.txt", old_text="def a():\n    pass", new_text="def b():\n    pass")
        self.assertIn("trailing", out)
        self.assertIn("def b():", f.read_text())

    def test_edit_not_found_gives_hint(self):
        (self.ws / "h.txt").write_text("print('hello world')\n")
        with self.assertRaises(ToolError) as e:
            run(self.ctx, "edit_file", path="h.txt", old_text="print('hello wrld')", new_text="x")
        self.assertIn("Closest line", str(e.exception))

    def test_read_window_and_search(self):
        (self.ws / "sub").mkdir()
        (self.ws / "sub" / "big.txt").write_text("\n".join(f"line {i}" for i in range(1, 1001)))
        out = run(self.ctx, "read_file", path="sub/big.txt", offset=500, limit=3)
        self.assertIn("500| line 500", out)
        self.assertIn("offset=503", out)
        hits = run(self.ctx, "search_text", pattern=r"line 99\d$")
        self.assertIn("sub/big.txt:990: line 990", hits)
        self.assertIn("sub/big.txt", run(self.ctx, "find_files", pattern="*.txt"))
        self.assertIn("big.txt", run(self.ctx, "list_dir", depth=2))

    def test_move_copy_delete(self):
        (self.ws / "a.txt").write_text("a")
        run(self.ctx, "copy_path", source="a.txt", destination="b/a.txt")
        run(self.ctx, "move_path", source="b/a.txt", destination="c.txt")
        self.assertTrue((self.ws / "c.txt").exists())
        run(self.ctx, "delete_path", path="c.txt")
        self.assertFalse((self.ws / "c.txt").exists())

    def test_shell(self):
        out = run(self.ctx, "run_command", command="echo hello && exit 3")
        self.assertIn("hello", out)
        self.assertIn("exit code 3", out)
        out = run(self.ctx, "run_command", command="sleep 5", timeout=1)
        self.assertIn("timed out", out)
        out = run(self.ctx, "run_python", code="print(6*7)")
        self.assertIn("42", out)

    def test_background_process(self):
        out = run(self.ctx, "run_command", command="echo start; sleep 2; echo end", background=True)
        self.assertIn("p", out)
        pid = out.split(" as ")[1].split(".")[0]
        import time
        time.sleep(2.5)
        self.assertIn("end", run(self.ctx, "check_process", id=pid))


class WebParseTest(unittest.TestCase):
    def test_html_to_text(self):
        t = html_to_text("<html><head><title>x</title><script>bad()</script></head><body><h1>Hi</h1><p>One &amp; two</p></body></html>")
        self.assertIn("# Hi", t)
        self.assertIn("One & two", t)
        self.assertNotIn("bad()", t)

    def test_ddg(self):
        page = ('<a rel="nofollow" class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2Fa&amp;rut=x">'
                'Example <b>A</b></a><a class="result__snippet" href="x">Snippet <b>one</b></a>'
                '<a rel="nofollow" class="result__a" href="https://b.org">B</a>')
        r = parse_ddg(page)
        self.assertEqual(r[0]["url"], "https://example.com/a")
        self.assertEqual(r[0]["title"], "Example A")
        self.assertEqual(r[0]["snippet"], "Snippet one")
        self.assertEqual(r[1]["url"], "https://b.org")


if __name__ == "__main__":
    unittest.main()
