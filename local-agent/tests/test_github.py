import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("LOCALAGENT_DATA", tempfile.mkdtemp())

from localagent.config import Settings  # noqa: E402
from localagent.permissions import Policy  # noqa: E402
from localagent.tools import REGISTRY, ToolContext, ToolError  # noqa: E402
from localagent.tools import github as gh  # noqa: E402

TOKEN = "ghp_secret123"


class FakeGitHub:
    """Just enough of the GitHub REST API."""

    def __init__(self):
        self.calls = []
        outer = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, code, obj):
                data = json.dumps(obj).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def _handle(self, method):
                n = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(n)) if n else None
                outer.calls.append((method, self.path, body, self.headers.get("Authorization")))
                if self.headers.get("Authorization") != f"Bearer {TOKEN}":
                    return self._send(401, {"message": "Bad credentials"})
                p = self.path.split("?")[0]
                if p == "/user":
                    return self._send(200, {"login": "aidan", "id": 7, "name": "Aidan"})
                if p == "/repos/aidan/proj":
                    return self._send(200, {"default_branch": "main"})
                if p == "/repos/aidan/proj/pulls" and method == "POST":
                    return self._send(201, {"number": 12, "html_url": "https://github.com/aidan/proj/pull/12"})
                if p == "/repos/aidan/proj/pulls/12/requested_reviewers":
                    return self._send(201, {})
                if p == "/repos/aidan/proj/pulls":
                    return self._send(200, [{"number": 12, "title": "Fix", "draft": True, "html_url": "u",
                                             "head": {"ref": "fix"}, "base": {"ref": "main"},
                                             "user": {"login": "aidan"}}])
                if p == "/repos/aidan/proj/pulls/12":
                    return self._send(200, {"number": 12, "title": "Fix", "html_url": "u", "state": "open",
                                            "head": {"ref": "fix"}, "base": {"ref": "main"},
                                            "user": {"login": "aidan"}, "body": "b", "comments": 130,
                                            "review_comments": 1})
                if p == "/repos/aidan/proj/pulls/12/files":
                    return self._send(200, [{"status": "modified", "filename": "a.py", "additions": 1, "deletions": 0}])
                if p == "/repos/aidan/proj/pulls/12/reviews":
                    return self._send(200, [{"user": {"login": "rev"}, "state": "COMMENTED", "body": ""}])
                if p == "/repos/aidan/proj/pulls/12/comments":
                    return self._send(200, [{"user": {"login": "rev"}, "path": "a.py", "line": 3,
                                             "body": "off by one here"}])
                if p == "/repos/aidan/proj/issues/12/comments":
                    page = int(dict(q.split("=") for q in self.path.split("?")[1].split("&")).get("page", 1))
                    items = [{"user": {"login": "x"}, "body": f"c{i}"} for i in range(1, 131)]
                    return self._send(200, items[(page - 1) * 100:page * 100])
                if p == "/repos/aidan/proj/issues":
                    return self._send(200, [{"number": 3, "title": "Bug", "html_url": "u", "user": {"login": "x"}},
                                            {"number": 12, "title": "PR", "html_url": "u", "user": {"login": "x"},
                                             "pull_request": {}}])
                return self._send(404, {"message": "Not Found"})

            def do_GET(self):
                self._handle("GET")

            def do_POST(self):
                self._handle("POST")

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()


def run(ctx, tool_name, **a):
    out = REGISTRY[tool_name].func(ctx, a)
    return out if isinstance(out, str) else out.text


@unittest.skipIf(shutil.which("git") is None, "git not installed")
class GitHubToolsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.api = FakeGitHub()

    def setUp(self):
        gh._cache.clear()
        self.ws = Path(tempfile.mkdtemp())
        s = Settings(github_token=TOKEN)
        s.extra["github_api"] = self.api.url
        self.ctx = ToolContext(settings=s, workspace=self.ws)
        # a "GitHub" remote that is really a local bare repo
        self.remote = Path(tempfile.mkdtemp()) / "proj.git"
        subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(self.remote)], check=True)
        seed = Path(tempfile.mkdtemp())
        g = lambda *a: subprocess.run(["git", "-C", str(seed), *a], check=True, capture_output=True)
        g("init", "-q", "-b", "main")
        (seed / "README.md").write_text("hi\n")
        g("add", "-A")
        g("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "first")
        g("push", "-q", str(self.remote), "main")

    def test_clone_branch_commit_push_and_pr(self):
        out = run(self.ctx, "git_clone", repo=str(self.remote), folder="proj")
        self.assertIn("Cloned", out)
        repo = self.ws / "proj"
        run(self.ctx, "git_branch", path="proj", name="fix-typo")
        (repo / "README.md").write_text("hello\n")
        self.assertIn("README.md", run(self.ctx, "git_status", path="proj"))
        self.assertIn("+hello", run(self.ctx, "git_diff", path="proj"))
        run(self.ctx, "git_commit", path="proj", message="Fix typo")
        # no git identity configured in the clone -> commits as the GitHub user
        log = subprocess.run(["git", "-C", str(repo), "log", "-1", "--pretty=%an <%ae>"], capture_output=True,
                             text=True).stdout.strip()
        if not subprocess.run(["git", "config", "--global", "user.name"], capture_output=True).stdout:
            self.assertEqual(log, "Aidan <7+aidan@users.noreply.github.com>")
        self.assertIn("Pushed fix-typo", run(self.ctx, "git_push", path="proj"))
        branches = subprocess.run(["git", "-C", str(self.remote), "branch"], capture_output=True, text=True).stdout
        self.assertIn("fix-typo", branches)
        self.assertIn("Fix typo", run(self.ctx, "git_log", path="proj"))

        out = run(self.ctx, "github_create_pr", repo="aidan/proj", path="proj", title="Fix typo",
                  body="Small fix", reviewers=["friend"])
        self.assertIn("#12", out)
        self.assertIn("Review requested from friend", out)
        posted = [c for c in self.api.calls if c[0] == "POST" and c[1].endswith("/pulls")][-1]
        self.assertEqual(posted[2]["head"], "fix-typo")
        self.assertEqual(posted[2]["base"], "main")

    def test_pr_from_main_refused(self):
        run(self.ctx, "git_clone", repo=str(self.remote), folder="p2")
        with self.assertRaises(ToolError):
            run(self.ctx, "github_create_pr", repo="aidan/proj", path="p2", title="x")

    def test_lists(self):
        self.assertIn("#12 Fix (draft)", run(self.ctx, "github_list_prs", repo="https://github.com/aidan/proj.git"))
        issues = run(self.ctx, "github_list_issues", repo="aidan/proj")
        self.assertIn("#3 Bug", issues)
        self.assertNotIn("#12", issues)

    def test_bad_token(self):
        self.ctx.settings.github_token = "nope"
        with self.assertRaises(ToolError) as e:
            run(self.ctx, "github_list_prs", repo="aidan/proj")
        self.assertIn("not signed in", str(e.exception))

    def test_token_never_in_output(self):
        self.assertEqual(gh._redact(self.ctx, f"fatal: {TOKEN} oops"), "fatal: *** oops")
        # without a ToolContext too, for every tool result: known tokens, their git header form, and
        # anything shaped like a GitHub token
        import base64

        header = base64.b64encode(f"x-access-token:{TOKEN}".encode()).decode()
        other = "ghp_" + "A1b2" * 9
        out = gh.redact_all(f"{TOKEN} / {header} / {other} / github_pat_{'x' * 50}", self.ctx.settings)
        self.assertEqual(out, "*** / *** / *** / ***")

    def test_reads_dont_run_programs_from_the_repo_config(self):
        repo = self.ws / "r"
        repo.mkdir()
        subprocess.run(["git", "-C", str(repo), "init", "-q"], check=True)
        marker = Path(tempfile.mkdtemp()) / "PWNED"
        cfg = repo / ".git" / "config"
        cfg.write_text(cfg.read_text().replace("[core]", f'[core]\n\tfsmonitor = "touch {marker}; false"'))
        run(self.ctx, "git_status", path="r")
        self.assertFalse(marker.exists())
        target = Path(tempfile.mkdtemp()) / "planted"
        with self.assertRaises(ToolError):
            run(self.ctx, "git_diff", path="r", against=f"--output={target}")
        self.assertEqual(list(target.parent.iterdir()), [])

    @unittest.skipIf(sys.platform == "win32", "shell hook")
    def test_no_hooks_run_while_the_token_is_passed(self):
        run(self.ctx, "git_clone", repo=str(self.remote), folder="proj")
        repo = self.ws / "proj"
        marker = Path(tempfile.mkdtemp()) / "hook-ran"
        hook = repo / ".git" / "hooks" / "pre-push"
        hook.write_text(f'#!/bin/sh\ntouch {marker}\necho "$GIT_CONFIG_PARAMETERS" | rev >&2\n')
        hook.chmod(0o755)
        out = run(self.ctx, "git_push", path="proj")
        self.assertFalse(marker.exists())
        self.assertNotIn("redaehartxe", out)

    def test_push_uploads_lfs_files_itself(self):
        run(self.ctx, "git_clone", repo=str(self.remote), folder="proj")
        (self.ws / "proj" / ".gitattributes").write_text("*.bin filter=lfs diff=lfs merge=lfs -text\n")
        calls, orig = [], gh.git

        def fake(ctx, args, **kw):
            calls.append(args[0])
            return "" if args[0] == "lfs" else orig(ctx, args, **kw)

        gh.git = fake
        try:
            run(self.ctx, "git_push", path="proj")
        finally:
            gh.git = orig
        self.assertLess(calls.index("lfs"), calls.index("push"))

    def test_commit_lists_files_and_warns_about_secrets(self):
        run(self.ctx, "git_clone", repo=str(self.remote), folder="proj")
        repo = self.ws / "proj"
        (repo / ".env").write_text("KEY=1\n")
        (repo / "app.py").write_text("x = 1\n")
        out = run(self.ctx, "git_commit", path="proj", message="Add app")
        self.assertIn("2 file(s) in the commit", out)
        self.assertIn("  app.py", out)
        self.assertIn(".env: looks like keys or passwords", out)

    def test_view_pr_shows_inline_and_latest_comments(self):
        out = run(self.ctx, "github_view_pr", repo="aidan/proj", number=12)
        self.assertIn("rev on a.py:3: off by one here", out)
        self.assertIn("x: c130", out)
        self.assertIn("x: c111", out)
        self.assertNotIn("x: c110", out)

    def test_remote_detection(self):
        d = self.ws / "r"
        d.mkdir()
        subprocess.run(["git", "-C", str(d), "init", "-q"], check=True)
        subprocess.run(["git", "-C", str(d), "remote", "add", "origin", "git@github.com:aidan/proj.git"], check=True)
        self.assertEqual(gh._repo(self.ctx, {"path": "r"}), "aidan/proj")

    def test_publish_policy(self):
        self.assertTrue(Policy("auto").needs_approval("publish", False))
        self.assertFalse(Policy("auto", always_ask_publish=False).needs_approval("publish", False))
        self.assertTrue(Policy("auto_edit").needs_approval("publish", False))
        self.assertEqual(REGISTRY["git_push"].risk, "publish")
        self.assertEqual(REGISTRY["github_create_pr"].risk, "publish")


if __name__ == "__main__":
    unittest.main()
