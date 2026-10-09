import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("LOCALAGENT_DATA", tempfile.mkdtemp())

from localagent.config import Settings  # noqa: E402
from localagent.tools import REGISTRY, ToolContext, ToolError  # noqa: E402
from localagent.tools.code import detect_tests, outline  # noqa: E402


def run(ctx, name, **args):
    out = REGISTRY[name].func(ctx, args)
    return out if isinstance(out, str) else out.text


def git(cwd, *args):
    subprocess.run(["git", "-c", "user.name=T", "-c", "user.email=t@example.com", *args], cwd=cwd,
                   check=True, capture_output=True)


CALC = "def add(a, b):\n    return a + b\n"
TEST = "from calc import add\n\n\ndef test_add():\n    assert add(2, 3) == 5\n"


class CodingModeTest(unittest.TestCase):
    def setUp(self):
        self.home = Path(tempfile.mkdtemp())
        self.repo = self.home / "proj"
        self.repo.mkdir()
        (self.repo / "calc.py").write_text(CALC)
        (self.repo / "test_calc.py").write_text(TEST)
        (self.repo / ".gitignore").write_text(".venv/\n")
        git(self.repo, "init", "-q", "-b", "main")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", "first")
        # the project's own virtualenv: run_tests should use its Python
        (self.repo / ".venv" / "bin").mkdir(parents=True)
        os.symlink(sys.executable, self.repo / ".venv" / "bin" / "python")
        os.environ.setdefault("GIT_AUTHOR_NAME", "T")
        self.ctx = ToolContext(settings=Settings(), workspace=self.home)

    def test_private_copy_test_loop_and_finish(self):
        out = run(self.ctx, "start_coding_task", repo="proj", task="Make add handle strings!")
        copy = self.home / ".worktrees" / "proj-make-add-handle-strings"
        self.assertEqual(self.ctx.workspace, copy)
        self.assertIn("agent/make-add-handle-strings", out)
        self.assertIn("-m pytest -q", out)
        self.assertIn(".venv", out)  # warns that the venv isn't in the copy
        self.assertEqual((copy / "calc.py").read_text(), CALC)

        self.assertTrue(run(self.ctx, "run_tests").startswith("PASSED"))

        # break it: the failure comes back, focused on what failed
        run(self.ctx, "edit_file", path="calc.py", old_text="a + b", new_text="a - b")
        self.assertEqual((self.repo / "calc.py").read_text(), CALC)   # the user's checkout is untouched
        out = run(self.ctx, "run_tests")
        self.assertTrue(out.startswith("FAILED"), out)
        self.assertIn("assert", out)
        self.assertIn("run_tests again", out)

        # a second task on the same name gets its own branch
        ctx2 = ToolContext(settings=Settings(), workspace=self.home)
        self.assertIn("agent/make-add-handle-strings-2", run(ctx2, "start_coding_task", repo=str(self.repo),
                                                              task="make add handle strings"))

        # fix it, commit, and leave
        run(self.ctx, "edit_file", path="calc.py", old_text="a - b", new_text="a + b")
        self.assertTrue(run(self.ctx, "run_tests").startswith("PASSED"))
        (copy / "new.txt").write_text("x")
        with self.assertRaises(ToolError):
            run(self.ctx, "finish_coding_task", delete_copy=True)  # uncommitted file
        (copy / "new.txt").unlink()
        out = run(self.ctx, "finish_coding_task", delete_copy=True)
        self.assertIn("Deleted the copy", out)
        self.assertFalse(copy.exists())
        self.assertEqual(self.ctx.workspace, self.home)

    def test_many_failures_suggest_reporting(self):
        run(self.ctx, "start_coding_task", repo="proj", task="x")
        out = ""
        for _ in range(6):
            out = run(self.ctx, "run_tests", command="exit 3")
        self.assertIn("failed 6 times in a row", out)

    def test_powershell_runs_quoted_python_with_ampersand(self):
        from localagent.tools import shell

        seen = {}
        orig = shell.run_command
        shell.run_command = lambda ctx, a: seen.setdefault("cmd", a["command"]) and "[exit code 0]\nok"
        try:
            self.ctx.settings.shell = "powershell"
            run(self.ctx, "run_tests", command='"C:/p/.venv/Scripts/python.exe" -m pytest -q')
        finally:
            shell.run_command = orig
            self.ctx.settings.shell = "auto"
        self.assertEqual(seen["cmd"], '& "C:/p/.venv/Scripts/python.exe" -m pytest -q')

    def test_not_a_repo(self):
        (self.home / "plain").mkdir()
        with self.assertRaises(ToolError):
            run(self.ctx, "start_coding_task", repo="plain", task="x")


class CodingAgentTest(CodingModeTest):
    """A whole coding task driven through the agent loop, with a scripted model."""

    def test_full_task(self):
        from localagent.agent import Agent, AgentUI
        from tests.mockserver import MockServer, calls, text

        (self.repo / "calc.py").write_text(CALC.replace("a + b", "a * b"))   # a bug to fix
        git(self.repo, "commit", "-qam", "bug")
        srv = MockServer([
            calls(("start_coding_task", {"repo": "proj", "task": "fix add"})),
            calls(("code_map", {})),
            calls(("run_tests", {})),
            calls(("edit_file", {"path": "calc.py", "old_text": "a * b", "new_text": "a + b"})),
            calls(("run_tests", {})),
            calls(("git_commit", {"message": "Fix add"})),
            calls(("finish_coding_task", {})),
            text("Fixed add; tests pass. Branch agent/fix-add."),
            text("[]"),
        ])
        self.addCleanup(srv.close)
        os.environ.update(GIT_AUTHOR_NAME="T", GIT_AUTHOR_EMAIL="t@example.com",
                          GIT_COMMITTER_NAME="T", GIT_COMMITTER_EMAIL="t@example.com")
        s = Settings(base_url=srv.url, workspace=str(self.home), approval_mode="auto", enable_control=False)
        results = []

        class UI(AgentUI):
            def tool_end(self, call_id, name, result):
                results.append((name, result.text))

        agent = Agent(s, UI())
        self.assertIn("start_coding_task", agent.system_prompt())
        out = agent.run("add() multiplies instead of adding in my proj repo, fix it")
        self.assertIn("Fixed add", out)
        by = dict(results)
        self.assertIn("calc.py", by["code_map"])
        self.assertTrue([r for n, r in results if n == "run_tests"][0].startswith("FAILED"))
        self.assertTrue([r for n, r in results if n == "run_tests"][1].startswith("PASSED"))
        self.assertEqual(agent.workspace, self.home)        # back home after finishing
        log = subprocess.run(["git", "log", "--oneline", "agent/fix-add"], cwd=self.repo, capture_output=True,
                             text=True).stdout
        self.assertIn("Fix add", log)
        self.assertIn("a * b", (self.repo / "calc.py").read_text())   # main checkout untouched

    # the inherited tests already ran in CodingModeTest
    test_private_copy_test_loop_and_finish = test_many_failures_suggest_reporting = test_not_a_repo = None
    test_powershell_runs_quoted_python_with_ampersand = None


class CodeToolsTest(unittest.TestCase):
    def setUp(self):
        self.ws = Path(tempfile.mkdtemp())
        self.ctx = ToolContext(settings=Settings(), workspace=self.ws)

    def test_multi_edit_is_all_or_nothing(self):
        f = self.ws / "a.py"
        f.write_text("x = 1\ny = 2\nz = 3\n")
        run(self.ctx, "multi_edit", path="a.py", edits=[{"old_text": "x = 1", "new_text": "x = 10"},
                                                       {"old_text": "z = 3", "new_text": "z = 30"}])
        self.assertEqual(f.read_text(), "x = 10\ny = 2\nz = 30\n")
        with self.assertRaises(ToolError) as e:
            run(self.ctx, "multi_edit", path="a.py", edits=[{"old_text": "y = 2", "new_text": "y = 20"},
                                                           {"old_text": "nope", "new_text": "?"}])
        self.assertIn("Edit 2 of 2", str(e.exception))
        self.assertEqual(f.read_text(), "x = 10\ny = 2\nz = 30\n")

    def test_syntax_warning_after_edit(self):
        (self.ws / "b.py").write_text("def f():\n    return 1\n")
        out = run(self.ctx, "edit_file", path="b.py", old_text="return 1", new_text="return (1")
        self.assertIn("syntax error at line", out)
        out = run(self.ctx, "write_file", path="c.json", content='{"a": 1,}')
        self.assertIn("no longer valid JSON", out)
        self.assertNotIn("Warning", run(self.ctx, "write_file", path="d.json", content='{"a": 1}'))

    def test_code_map(self):
        (self.ws / "pkg").mkdir()
        (self.ws / "pkg" / "m.py").write_text(
            "class Tracker:\n    def update(self, pts, *, dt=0.0):\n        pass\n\n\nasync def main():\n    pass\n")
        (self.ws / "web.ts").write_text(
            "export class App {\n}\nexport async function load(url: string) {\n}\n"
            "export const save = async (x) => {\n}\nif (x) {\n}\n")
        (self.ws / "node_modules").mkdir()
        (self.ws / "node_modules" / "skip.js").write_text("function hidden() {}\n")
        out = run(self.ctx, "code_map")
        self.assertIn("pkg/m.py", out)
        self.assertIn("L1 class Tracker", out)
        self.assertIn("L2 def update(self, pts, dt)", out)
        self.assertIn("L6 async def main()", out)
        self.assertIn("L3 export async function load(url: string)", out)
        self.assertIn("L5 export const save", out)
        self.assertNotIn("if (x)", out)
        self.assertNotIn("hidden", out)
        self.assertEqual(outline(self.ws / "pkg" / "m.py")[0], "L1 class Tracker")

    def test_detect_tests(self):
        d = Path(tempfile.mkdtemp())
        self.assertEqual(detect_tests(d), "")
        (d / "package.json").write_text(json.dumps({"scripts": {"test": "echo \"Error: no test specified\""}}))
        self.assertEqual(detect_tests(d), "")
        (d / "package.json").write_text(json.dumps({"scripts": {"test": "vitest run"}}))
        (d / "pnpm-lock.yaml").write_text("")
        self.assertEqual(detect_tests(d), "pnpm test")
        d2 = Path(tempfile.mkdtemp())
        (d2 / "Cargo.toml").write_text("[package]\n")
        self.assertEqual(detect_tests(d2), "cargo test")
        d3 = Path(tempfile.mkdtemp())
        (d3 / "pyproject.toml").write_text("[tool.pytest.ini_options]\n")
        self.assertIn("-m pytest -q", detect_tests(d3))


if __name__ == "__main__":
    unittest.main()
