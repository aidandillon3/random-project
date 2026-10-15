"""Git and GitHub: clone, branch, commit, push, and read or open pull requests and issues.

Sign-in, first match wins: the token in Settings, the GITHUB_TOKEN / GH_TOKEN environment
variable, or the GitHub CLI's login (`gh auth login`). The token is only ever sent to
github.com; it is never shown to the model or written into a repository.
"""

from __future__ import annotations

import base64
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from .base import EDIT, PUBLISH, ToolContext, ToolError, path_risk, tool, truncate

API = "https://api.github.com"
WINDOWS = sys.platform == "win32"
_cache: dict = {}
_seen_tokens: set = set()     # every token used so far, for redact_all
_no_hooks: list = []
# Anything shaped like a GitHub token (classic, OAuth, app, fine-grained), known to us or not
TOKEN_RX = re.compile(r"\b(gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{40,})\b")
SECRET_FILE = re.compile(r"(^|/)(\.env(\.[^/]*)?|id_(rsa|dsa|ecdsa|ed25519)[^/]*|\.git-credentials|[._]netrc)$"
                         r"|\.(pem|key|pfx|p12)$", re.IGNORECASE)
BIG_FILE = 50_000_000   # GitHub warns above 50 MB and refuses files over 100 MB


# ---------------------------------------------------------------------- sign-in
def _gh_cli_token() -> str:
    if "gh" not in _cache:
        tok = ""
        exe = shutil.which("gh")
        if exe:
            try:
                out = subprocess.run([exe, "auth", "token"], capture_output=True, text=True, timeout=15,
                                     creationflags=subprocess.CREATE_NO_WINDOW if WINDOWS else 0)
                tok = out.stdout.strip() if out.returncode == 0 else ""
            except (OSError, subprocess.SubprocessError):
                pass
        _cache["gh"] = tok
    return _cache["gh"]


def token(ctx: ToolContext) -> str:
    tok = (getattr(ctx.settings, "github_token", "") or os.environ.get("GITHUB_TOKEN")
           or os.environ.get("GH_TOKEN") or _gh_cli_token() or "").strip()
    if tok:
        _seen_tokens.add(tok)
    return tok


def redact_all(text: str, settings=None) -> str:
    """Hide GitHub tokens in any text before the model sees it: the token in settings (pass the
    app's Settings), GITHUB_TOKEN / GH_TOKEN, any token used so far, their git auth-header form,
    and anything else shaped like a GitHub token. Needs no ToolContext."""
    if not text:
        return text
    toks = {str(getattr(settings, "github_token", "") or ""), os.environ.get("GITHUB_TOKEN", ""),
            os.environ.get("GH_TOKEN", ""), _cache.get("gh", ""), *_seen_tokens}
    for tok in sorted({t.strip() for t in toks}, key=len, reverse=True):
        if len(tok) >= 8:
            text = text.replace(tok, "***")
            text = text.replace(base64.b64encode(f"x-access-token:{tok}".encode()).decode(), "***")
    return TOKEN_RX.sub("***", text)


def _redact(ctx: ToolContext, text: str) -> str:
    return redact_all(text, ctx.settings)


def api(ctx: ToolContext, method: str, path: str, json=None, params=None):
    import requests

    tok = token(ctx)
    headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28",
               "User-Agent": "local-agent"}
    if tok:
        headers["Authorization"] = f"Bearer {tok}"
    base = getattr(ctx.settings, "extra", {}).get("github_api", API)
    try:
        r = requests.request(method, base + path, headers=headers, json=json, params=params, timeout=30)
    except requests.RequestException as e:
        raise ToolError(f"Couldn't reach GitHub: {e}")
    if r.status_code == 401:
        raise ToolError("GitHub says you're not signed in. Add a token in Settings > GitHub, or run "
                        "`gh auth login` once in a terminal.")
    if r.status_code >= 400:
        try:
            body = r.json()
            msg = body.get("message", "")
            details = "; ".join(e.get("message", str(e)) if isinstance(e, dict) else str(e)
                                for e in body.get("errors", []))
        except ValueError:
            msg, details = r.text[:300], ""
        hint = " (not found, or your token can't see it)" if r.status_code == 404 else ""
        raise ToolError(f"GitHub error {r.status_code}{hint}: {msg} {details}".strip())
    return r.json() if r.content else {}


def _me(ctx: ToolContext) -> dict:
    key = ("me", token(ctx))
    if key not in _cache:
        _cache[key] = api(ctx, "GET", "/user")
    return _cache[key]


# ---------------------------------------------------------------------- git
def _git_exe() -> str:
    exe = shutil.which("git")
    if not exe:
        raise ToolError("Git isn't installed. Install Git for Windows from https://git-scm.com/download/win "
                        "(or run `winget install Git.Git`), then restart the app.")
    return exe


def _empty_hooks_dir() -> str:
    if not _no_hooks or not os.path.isdir(_no_hooks[0]):
        _no_hooks[:] = [Path(tempfile.mkdtemp(prefix="localagent-nohooks-")).as_posix()]
    return _no_hooks[0]


def git(ctx: ToolContext, args: list[str], cwd=None, auth: bool = False, timeout: int = 300,
        check: bool = True) -> str:
    from .shell import quiet_env, run_process

    # A repository's config can't make a plain read (status, diff) run a program. (Empty = off,
    # also for Git before 2.36, where the value is a program to run.)
    cmd = [_git_exe(), "-c", "core.fsmonitor="]
    tok = token(ctx) if auth else ""
    if tok:
        basic = base64.b64encode(f"x-access-token:{tok}".encode()).decode()
        cmd += ["-c", f"http.https://github.com/.extraheader=AUTHORIZATION: basic {basic}",
                # hooks would see that header (git hands -c settings to them), so none run meanwhile
                "-c", f"core.hooksPath={_empty_hooks_dir()}"]
    name = next((x for x in args if not x.startswith("-") and "=" not in x), "")
    code, text, why = run_process(cmd + args, str(cwd or ctx.workspace), quiet_env(), timeout, ctx.stop)
    text = redact_all(text.strip(), ctx.settings)
    if why:
        why = f"took longer than {timeout}s and was stopped" if why.startswith("timed") else "was stopped by you"
        raise ToolError(f"git {name} {why}." + (f"\n{text[-1500:]}" if text else ""))
    if check and code != 0:
        raise ToolError(f"git {name} failed:\n{text}")
    return text


def _repo_dir(ctx: ToolContext, a: dict):
    d = ctx.resolve(a.get("path") or ".")
    if not d.is_dir():
        raise ToolError(f"Not a folder: {d}")
    return d


def _remote_repo(ctx: ToolContext, d) -> str:
    url = git(ctx, ["remote", "get-url", "origin"], cwd=d, check=False)
    m = re.search(r"github\.com[:/]([\w.-]+)/([\w.-]+?)(?:\.git)?/?$", url.strip())
    if not m:
        raise ToolError("Couldn't tell which GitHub repository this is. Pass repo as owner/name.")
    return f"{m.group(1)}/{m.group(2)}"


def _repo(ctx: ToolContext, a: dict) -> str:
    repo = (a.get("repo") or "").strip()
    if repo:
        repo = re.sub(r"^https?://github\.com/|\.git$", "", repo).strip("/")
        if repo.count("/") != 1:
            raise ToolError("repo must look like owner/name.")
        return repo
    return _remote_repo(ctx, _repo_dir(ctx, a))


def _identity_args(ctx: ToolContext, d) -> list[str]:
    """Commit as the signed-in GitHub user when git has no name/email set up yet."""
    name = git(ctx, ["config", "user.name"], cwd=d, check=False)
    email = git(ctx, ["config", "user.email"], cwd=d, check=False)
    if name and email:
        return []
    if not token(ctx):
        raise ToolError("Git doesn't know your name yet. Run: git config --global user.name \"Your Name\" "
                        "and git config --global user.email you@example.com, or sign in to GitHub in Settings.")
    me = _me(ctx)
    login = me.get("login", "user")
    return ["-c", f"user.name={me.get('name') or login}",
            "-c", f"user.email={me.get('id', 0)}+{login}@users.noreply.github.com"]


def _here(key: str):
    """Risk of a git command that changes a repository: EDIT in the work folder, RUN outside it."""
    return lambda ctx, a: path_risk(ctx, a.get(key) or ".")


PATH_PARAM = {"type": "string", "description": "The repository folder. Default: the work folder"}
REPO_PARAM = {"type": "string", "description": "owner/name. Default: the GitHub remote of the repository folder"}


@tool("git_clone", "Download a GitHub repository into the work folder (or folder).",
      {"repo": {"type": "string", "description": "owner/name or a clone URL"},
       "folder": {"type": "string", "description": "Where to put it. Default: the repository's name"}},
      required=["repo"], risk=EDIT, group="github", risk_for=_here("folder"),
      describe=lambda a: f"Clone {a.get('repo')} into {a.get('folder') or 'the work folder'}")
def git_clone(ctx: ToolContext, a: dict):
    src = str(a["repo"]).strip()
    if not re.match(r"^(https?://|git@)", src) and not os.path.exists(src):
        src = f"https://github.com/{src.removesuffix('.git')}.git"
    name = re.sub(r"\.git$", "", src.rstrip("/").split("/")[-1])
    dest = ctx.resolve(a.get("folder") or name)
    if dest.exists() and any(dest.iterdir()):
        raise ToolError(f"{dest} already exists and isn't empty.")
    out = git(ctx, ["clone", "--", src, str(dest)], auth=True, timeout=900)
    return f"Cloned into {dest}\n{out[-1500:]}"


@tool("git_status", "Show the branch, changed files and whether it is ahead/behind the remote.",
      {"path": PATH_PARAM}, group="github")
def git_status(ctx: ToolContext, a: dict):
    d = _repo_dir(ctx, a)
    return git(ctx, ["status", "--short", "--branch"], cwd=d) or "Clean: nothing changed."


@tool("git_diff", "Show the changes in the repository folder (unstaged and staged), or against another branch.",
      {"path": PATH_PARAM, "against": {"type": "string", "description": "Optional branch/commit, e.g. main"}},
      group="github")
def git_diff(ctx: ToolContext, a: dict):
    d = _repo_dir(ctx, a)
    against = str(a.get("against") or "").strip()
    if against.startswith("-"):   # --output=<file> would write a file
        raise ToolError("against must be a branch or commit, not an option.")
    if against:
        out = git(ctx, ["diff", "--no-ext-diff", f"{against}...HEAD"], cwd=d)
    else:
        out = (git(ctx, ["diff", "--no-ext-diff", "HEAD"], cwd=d, check=False)
               or git(ctx, ["diff", "--no-ext-diff"], cwd=d))
    return truncate(out or "No changes.", ctx.settings.max_tool_output)


@tool("git_log", "Show recent commits.", {"path": PATH_PARAM, "count": {"type": "integer"}}, group="github")
def git_log(ctx: ToolContext, a: dict):
    d = _repo_dir(ctx, a)
    n = min(100, max(1, int(a.get("count") or 15)))
    return git(ctx, ["log", f"-{n}", "--pretty=format:%h %ad %an  %s", "--date=short"], cwd=d)


@tool("git_branch", "Create a new branch and switch to it, or switch to an existing branch.",
      {"path": PATH_PARAM, "name": {"type": "string"}, "create": {"type": "boolean", "description": "Default true"}},
      required=["name"], risk=EDIT, group="github", risk_for=_here("path"),
      describe=lambda a: f"Switch to branch {a.get('name')}")
def git_branch(ctx: ToolContext, a: dict):
    d = _repo_dir(ctx, a)
    if str(a["name"]).startswith("-"):
        raise ToolError("A branch name can't start with '-'.")
    create = a.get("create", True)
    exists = git(ctx, ["rev-parse", "--verify", "--quiet", a["name"]], cwd=d, check=False)
    args = ["switch", a["name"]] if exists or not create else ["switch", "-c", a["name"]]
    return git(ctx, args, cwd=d) or f"On branch {a['name']}."


@tool("git_commit", "Commit changes. By default every changed file is included; pass files to pick some.",
      {"path": PATH_PARAM, "message": {"type": "string"},
       "files": {"type": "array", "items": {"type": "string"}}},
      required=["message"], risk=EDIT, group="github", risk_for=_here("path"),
      describe=lambda a: f"Commit: {str(a.get('message') or '')[:120]}")
def git_commit(ctx: ToolContext, a: dict):
    d = _repo_dir(ctx, a)
    files = a.get("files") or []
    git(ctx, ["add", "--", *files] if files else ["add", "-A"], cwd=d)
    staged = [f for f in git(ctx, ["diff", "--cached", "--name-only", "-z"], cwd=d).split("\0") if f]
    if not staged:
        raise ToolError("Nothing to commit.")
    out = git(ctx, [*_identity_args(ctx, d), "commit", "-m", str(a["message"])], cwd=d)
    return out + _commit_report(Path(git(ctx, ["rev-parse", "--show-toplevel"], cwd=d)), staged)


def _commit_report(top: Path, staged: list[str]) -> str:
    """What went into the commit, and a warning about files that look like secrets or are huge."""
    lines = [f"\n{len(staged)} file(s) in the commit:"] + [f"  {f}" for f in staged[:50]]
    if len(staged) > 50:
        lines.append(f"  ... and {len(staged) - 50} more")
    warn = []
    for f in staged:
        if SECRET_FILE.search(f) and not f.lower().endswith((".example", ".sample", ".template", ".pub")):
            warn.append(f"  {f}: looks like keys or passwords")
            continue
        try:
            size = (top / f).stat().st_size
        except OSError:   # deleted in this commit
            continue
        if size > BIG_FILE:
            warn.append(f"  {f}: {size // 1_000_000} MB (GitHub refuses files over 100 MB)")
    if warn:
        lines += ["Warning: check these before pushing. If they don't belong in the repository, take them out "
                  "(git rm --cached <file>, add them to .gitignore) and amend the commit:"] + warn
    return "\n".join(lines)


@tool("git_pull", "Get the latest commits from GitHub into the current branch.", {"path": PATH_PARAM},
      risk=EDIT, group="github", risk_for=_here("path"), describe=lambda a: "git pull")
def git_pull(ctx: ToolContext, a: dict):
    d = _repo_dir(ctx, a)
    return git(ctx, [*_identity_args(ctx, d), "pull", "--no-rebase"], cwd=d, auth=True) or "Up to date."


@tool("git_push", "Upload the current branch's commits to GitHub (never force-pushes).",
      {"path": PATH_PARAM}, risk=PUBLISH, group="github",
      describe=lambda a: f"Push the current branch of {a.get('path') or 'the work folder'} to GitHub")
def git_push(ctx: ToolContext, a: dict):
    d = _repo_dir(ctx, a)
    branch = git(ctx, ["rev-parse", "--abbrev-ref", "HEAD"], cwd=d)
    if branch == "HEAD":
        raise ToolError("Not on a branch. Use git_branch first.")
    top = Path(git(ctx, ["rev-parse", "--show-toplevel"], cwd=d))
    if token(ctx) and "filter=lfs" in _read(top / ".gitattributes"):
        # Git LFS uploads its files from a pre-push hook, and hooks are off while the token is passed
        git(ctx, ["lfs", "push", "origin", branch], cwd=d, auth=True, timeout=1800)
    out = git(ctx, ["push", "-u", "origin", branch], cwd=d, auth=True)
    return f"Pushed {branch}.\n{out[-1500:]}"


def _read(p: Path) -> str:
    try:
        return p.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


# ---------------------------------------------------------------------- GitHub API
def _latest(ctx: ToolContext, path: str, total, keep: int) -> list:
    """The newest `keep` items of a list GitHub pages oldest first (total: its length, if known)."""
    last = max(1, -(-int(total or 0) // 100))
    items = api(ctx, "GET", path, params={"per_page": 100, "page": last})
    if len(items) < keep and last > 1:
        items = api(ctx, "GET", path, params={"per_page": 100, "page": last - 1}) + items
    return items[-keep:]


def _pr_line(p: dict) -> str:
    flags = " (draft)" if p.get("draft") else ""
    return (f"#{p['number']} {p['title']}{flags}  [{p['head']['ref']} -> {p['base']['ref']}] "
            f"by {p['user']['login']}  {p['html_url']}")


@tool("github_list_repos", "List your GitHub repositories (most recently updated first), or another user's/org's.",
      {"owner": {"type": "string", "description": "Optional user or organization"}}, group="github")
def github_list_repos(ctx: ToolContext, a: dict):
    if a.get("owner"):
        repos = api(ctx, "GET", f"/users/{a['owner']}/repos", params={"sort": "updated", "per_page": 50})
    else:
        repos = api(ctx, "GET", "/user/repos", params={"sort": "updated", "per_page": 50})
    lines = [f"{r['full_name']}{' (private)' if r.get('private') else ''}  {r.get('description') or ''}".strip()
             for r in repos]
    return "\n".join(lines) or "No repositories."


@tool("github_list_prs", "List pull requests of a repository.",
      {"repo": REPO_PARAM, "path": PATH_PARAM, "state": {"type": "string", "enum": ["open", "closed", "all"]}},
      group="github")
def github_list_prs(ctx: ToolContext, a: dict):
    repo = _repo(ctx, a)
    prs = api(ctx, "GET", f"/repos/{repo}/pulls", params={"state": a.get("state") or "open", "per_page": 30})
    return "\n".join(_pr_line(p) for p in prs) or f"No {a.get('state') or 'open'} pull requests in {repo}."


@tool("github_view_pr", "Read a pull request: description, changed files, reviews and comments.",
      {"repo": REPO_PARAM, "path": PATH_PARAM, "number": {"type": "integer"}},
      required=["number"], group="github")
def github_view_pr(ctx: ToolContext, a: dict):
    repo, n = _repo(ctx, a), int(a["number"])
    p = api(ctx, "GET", f"/repos/{repo}/pulls/{n}")
    files = api(ctx, "GET", f"/repos/{repo}/pulls/{n}/files", params={"per_page": 100})
    reviews = api(ctx, "GET", f"/repos/{repo}/pulls/{n}/reviews", params={"per_page": 100})[-20:]
    inline = _latest(ctx, f"/repos/{repo}/pulls/{n}/comments", p.get("review_comments"), 40)
    comments = _latest(ctx, f"/repos/{repo}/issues/{n}/comments", p.get("comments"), 20)
    state = "merged" if p.get("merged") else p["state"]
    out = [_pr_line(p), f"State: {state}. Mergeable: {p.get('mergeable')}.", "", p.get("body") or "(no description)",
           "", "Files:"]
    out += [f"  {f['status']:<9} {f['filename']} (+{f['additions']} -{f['deletions']})" for f in files]
    if reviews:
        out += ["", "Reviews:"] + [f"  {r['user']['login']}: {r['state']} {r.get('body') or ''}".rstrip() for r in reviews]
    if inline:
        out += ["", "Review comments on the code (newest last):"]
        out += [f"  {c['user']['login']} on {c.get('path')}:{c.get('line') or c.get('original_line') or '?'}: "
                f"{c['body']}" for c in inline]
    if comments:
        out += ["", "Comments (newest last):"] + [f"  {c['user']['login']}: {c['body']}" for c in comments]
    return truncate("\n".join(out), ctx.settings.max_tool_output)


@tool("github_create_pr", """
Open a pull request on GitHub. Push the branch first with git_push. head is the branch with the
changes; base defaults to the repository's default branch. reviewers is an optional list of
GitHub usernames to ask for a review.""",
      {"repo": REPO_PARAM, "path": PATH_PARAM, "title": {"type": "string"}, "body": {"type": "string"},
       "head": {"type": "string", "description": "Default: the current branch of the repository folder"},
       "base": {"type": "string"}, "draft": {"type": "boolean"},
       "reviewers": {"type": "array", "items": {"type": "string"}}},
      required=["title"], risk=PUBLISH, group="github",
      describe=lambda a: f"Open pull request '{a.get('title')}'" + (f" on {a['repo']}" if a.get("repo") else ""))
def github_create_pr(ctx: ToolContext, a: dict):
    repo = _repo(ctx, a)
    head = a.get("head") or git(ctx, ["rev-parse", "--abbrev-ref", "HEAD"], cwd=_repo_dir(ctx, a))
    base = a.get("base") or api(ctx, "GET", f"/repos/{repo}")["default_branch"]
    if head == base:
        raise ToolError(f"head and base are both '{base}'. Make a branch with git_branch first.")
    p = api(ctx, "POST", f"/repos/{repo}/pulls", json={"title": a["title"], "body": a.get("body") or "",
                                                         "head": head, "base": base, "draft": bool(a.get("draft"))})
    note = ""
    if a.get("reviewers"):
        try:
            api(ctx, "POST", f"/repos/{repo}/pulls/{p['number']}/requested_reviewers",
                json={"reviewers": list(a["reviewers"])})
            note = f"\nReview requested from {', '.join(a['reviewers'])}."
        except ToolError as e:
            note = f"\nThe pull request is open, but requesting reviewers failed: {e}"
    return f"Opened pull request #{p['number']}: {p['html_url']}{note}"


@tool("github_comment", "Comment on a pull request or issue.",
      {"repo": REPO_PARAM, "path": PATH_PARAM, "number": {"type": "integer"}, "body": {"type": "string"}},
      required=["number", "body"], risk=PUBLISH, group="github",
      describe=lambda a: f"Comment on #{a.get('number')}: {str(a.get('body') or '')[:150]}")
def github_comment(ctx: ToolContext, a: dict):
    repo = _repo(ctx, a)
    c = api(ctx, "POST", f"/repos/{repo}/issues/{int(a['number'])}/comments", json={"body": a["body"]})
    return f"Commented: {c.get('html_url', '')}"


@tool("github_list_issues", "List issues of a repository (pull requests left out).",
      {"repo": REPO_PARAM, "path": PATH_PARAM, "state": {"type": "string", "enum": ["open", "closed", "all"]}},
      group="github")
def github_list_issues(ctx: ToolContext, a: dict):
    repo = _repo(ctx, a)
    items = api(ctx, "GET", f"/repos/{repo}/issues", params={"state": a.get("state") or "open", "per_page": 30})
    lines = [f"#{i['number']} {i['title']}  by {i['user']['login']}  {i['html_url']}"
             for i in items if "pull_request" not in i]
    return "\n".join(lines) or f"No {a.get('state') or 'open'} issues in {repo}."


@tool("github_create_issue", "Open an issue on GitHub.",
      {"repo": REPO_PARAM, "path": PATH_PARAM, "title": {"type": "string"}, "body": {"type": "string"}},
      required=["title"], risk=PUBLISH, group="github",
      describe=lambda a: f"Open issue '{a.get('title')}'")
def github_create_issue(ctx: ToolContext, a: dict):
    repo = _repo(ctx, a)
    i = api(ctx, "POST", f"/repos/{repo}/issues", json={"title": a["title"], "body": a.get("body") or ""})
    return f"Opened issue #{i['number']}: {i['html_url']}"
