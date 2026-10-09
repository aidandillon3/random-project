"""Coding mode, the way Claude Code works: a private git copy to work in, a test loop, and a
map of the code so the model finds its way around without reading every file."""

from __future__ import annotations

import ast
import json
import os
import re
import shutil
import sys
from pathlib import Path

from .base import EDIT, RUN, ToolContext, ToolError, tool, truncate

WINDOWS = sys.platform == "win32"
CODE_EXT = {".py", ".js", ".jsx", ".mjs", ".ts", ".tsx", ".go", ".rs", ".java", ".kt", ".cs", ".c", ".h",
            ".cpp", ".cc", ".hpp", ".rb", ".php", ".swift", ".lua", ".gd", ".dart", ".scala", ".vue", ".svelte"}
SKIP = {".git", "node_modules", ".venv", "venv", "__pycache__", "dist", "build", ".next", "target", "bin",
        "obj", ".worktrees", ".mypy_cache", ".pytest_cache", ".tox", "vendor", "coverage"}
MAX_FAILS_BEFORE_REPORT = 6


def _git(ctx, args, cwd, **kw):
    from .github import git

    return git(ctx, args, cwd=cwd, **kw)


def _slug(text: str) -> str:
    words = re.findall(r"[a-z0-9]+", text.lower())[:5]
    return "-".join(words) or "task"


def _repo_name(src: str) -> str:
    return re.sub(r"\.git$", "", src.rstrip("/\\").replace("\\", "/").split("/")[-1]) or "repo"


# ---------------------------------------------------------------------- test commands
def _python_for(*dirs: Path) -> str:
    for d in dirs:
        for rel in ((".venv", "Scripts", "python.exe"), ("venv", "Scripts", "python.exe"),
                    (".venv", "bin", "python"), ("venv", "bin", "python")):
            p = d.joinpath(*rel)
            if p.exists():
                return f'"{p}"'
    return "python" if shutil.which("python") else f'"{sys.executable}"'


def detect_tests(d: Path, source: Path | None = None) -> str:
    """The usual way to run this project's tests, or '' if it can't tell."""
    def has(*names):
        return any((d / n).exists() for n in names)

    pkg = d / "package.json"
    if pkg.exists():
        try:
            test = json.loads(pkg.read_text(encoding="utf-8")).get("scripts", {}).get("test", "")
        except ValueError:
            test = ""
        if test and "no test specified" not in test:
            if has("pnpm-lock.yaml"):
                return "pnpm test"
            if has("yarn.lock"):
                return "yarn test"
            return "npm test"
    if has("pytest.ini", "conftest.py", "tox.ini") or (d / "tests").is_dir() or list(d.glob("test_*.py")) or \
            ("pytest" in _read(d / "pyproject.toml")) or ("pytest" in _read(d / "setup.cfg")):
        py = _python_for(d, *([source] if source else []))
        return f"{py} -m pytest -q"
    if has("Cargo.toml"):
        return "cargo test"
    if has("go.mod"):
        return "go test ./..."
    if list(d.glob("*.sln")) or list(d.glob("*.csproj")):
        return "dotnet test"
    if has("pom.xml"):
        return "mvn -q test"
    if has("gradlew", "gradlew.bat"):
        return ".\\gradlew.bat test" if WINDOWS else "./gradlew test"
    if has("build.gradle", "build.gradle.kts"):
        return "gradle test"
    if re.search(r"^test\s*:", _read(d / "Makefile"), re.M):
        return "make test"
    return ""


def _read(p: Path) -> str:
    try:
        return p.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _focus(output: str, limit: int) -> str:
    """The useful part of a long test log: the failures and the summary, not the progress dots."""
    for marker in ("= FAILURES =", "= ERRORS =", "short test summary info", "failures:", "FAIL ", "Failed "):
        i = output.find(marker)
        if i > 0:
            output = output[max(0, i - 200):]
            break
    if len(output) <= limit:
        return output
    return "[... earlier output cut ...]\n" + output[-limit:]


# ---------------------------------------------------------------------- tools
@tool("start_coding_task", """
Start a coding task in a private copy of a git repository: a new branch in its own folder (a
git worktree), so the user's own checkout and files aren't touched. The copy becomes your work
folder. repo is a local repository folder, or owner/name on GitHub (cloned first if needed).
Tells you how to run the tests.""",
      {"repo": {"type": "string", "description": "Folder of a git repository, or owner/name"},
       "task": {"type": "string", "description": "A few words about the change, used for the branch name"},
       "base": {"type": "string", "description": "Branch to start from. Default: the repository's main branch"}},
      required=["repo", "task"], risk=EDIT, group="code",
      describe=lambda a: f"Make a private copy of {a.get('repo')} on a new branch for: {a.get('task')}")
def start_coding_task(ctx: ToolContext, a: dict):
    home = Path(ctx.coding.get("home") or ctx.workspace)
    src_txt = a["repo"].strip()
    local = Path(src_txt).expanduser()
    if not local.is_absolute():
        local = home / local
    if (local / ".git").exists():
        source = local.resolve()
    elif re.match(r"^(https?://|git@)|^[\w.-]+/[\w.-]+$", src_txt):
        source = (home / "repos" / _repo_name(src_txt)).resolve()
        if not (source / ".git").exists():
            url = src_txt if re.match(r"^(https?://|git@)", src_txt) else f"https://github.com/{src_txt}.git"
            source.parent.mkdir(parents=True, exist_ok=True)
            _git(ctx, ["clone", url, str(source)], cwd=source.parent, auth=True, timeout=900)
    else:
        raise ToolError(f"{local} isn't a git repository, and '{src_txt}' doesn't look like owner/name.")

    _git(ctx, ["fetch", "--quiet", "origin"], cwd=source, auth=True, check=False, timeout=300)
    base = (a.get("base") or "").strip()
    if not base:
        head = _git(ctx, ["symbolic-ref", "--short", "refs/remotes/origin/HEAD"], cwd=source, check=False)
        base = head if head.startswith("origin/") and "\n" not in head else "HEAD"
    slug = _slug(a["task"])
    branch, n = f"agent/{slug}", 1
    while _git(ctx, ["rev-parse", "--verify", "--quiet", branch], cwd=source, check=False):
        n += 1
        branch = f"agent/{slug}-{n}"
    folder = home / ".worktrees" / f"{source.name}-{branch.split('/', 1)[1]}"
    folder.parent.mkdir(parents=True, exist_ok=True)
    _git(ctx, ["worktree", "add", "-b", branch, str(folder), base], cwd=source)

    ctx.coding.update(home=str(home), source=str(source), folder=str(folder), branch=branch, fails=0,
                      test_cmd=detect_tests(folder, source))
    if ctx.agent is not None:
        ctx.agent.set_workspace(folder)
    else:
        ctx.workspace = folder
    tests = ctx.coding["test_cmd"]
    deps = [n for n in ("node_modules", ".venv", "venv") if (source / n).exists() and not (folder / n).exists()]
    lines = [f"Your private copy is ready: {folder}",
             f"Branch {branch}, started from {base}. This is now your work folder; relative paths start here.",
             f"Tests: run_tests will use `{tests}`." if tests else
             "Tests: couldn't tell how to run them. Look at the README, then pass command to run_tests."]
    if deps:
        lines.append(f"Note: {', '.join(deps)} from the original folder isn't in the copy. If the tests need "
                     "installed packages, install them in the copy first (e.g. npm install), or run_tests "
                     "with the original's Python.")
    return "\n".join(lines)


@tool("run_tests", """
Run the project's tests in the work folder and get back a short result: passed, or the
failures. With no command it uses the project's usual test command (pytest, npm test, cargo
test, go test, dotnet test...). Run it after every change; if tests fail, fix the cause and
run them again until they pass.""",
      {"command": {"type": "string", "description": "Only if the automatic choice is wrong, e.g. 'pytest tests/test_api.py -q'"},
       "path": {"type": "string", "description": "Folder to run in. Default: the work folder"},
       "timeout": {"type": "integer", "description": "Seconds. Default 600"}},
      risk=RUN, group="code",
      describe=lambda a: f"Run tests: {a.get('command') or 'the project test command'}")
def run_tests(ctx: ToolContext, a: dict):
    from .shell import run_command

    if not getattr(ctx.settings, "enable_shell", True):
        raise ToolError("Running commands is switched off in Settings, so tests can't run.")
    d = ctx.resolve(a.get("path") or ".")
    cmd = (a.get("command") or "").strip()
    if cmd:
        ctx.coding["test_cmd"] = cmd
    else:
        cmd = ctx.coding.get("test_cmd") if ctx.coding.get("folder") == str(d) else ""
        cmd = cmd or detect_tests(d, Path(ctx.coding["source"]) if ctx.coding.get("source") else None)
    if not cmd:
        raise ToolError("Couldn't tell how to run this project's tests. Check the README or CI config and "
                        "pass command.")
    from .shell import shell_kind

    if cmd.startswith('"') and shell_kind(ctx) == "powershell":
        cmd = "& " + cmd   # PowerShell needs & to run a quoted program path
    # No .pyc files: an edit made within a second of the last run, same size, would otherwise be
    # missed and the old code tested again.
    out = run_command(ctx, {"command": cmd, "cwd": str(d), "timeout": int(a.get("timeout") or 600),
                            "_env": {"PYTHONDONTWRITEBYTECODE": "1"}})
    m = re.match(r"\[exit code (-?\d+)\]", out)
    code = int(m.group(1)) if m else None
    body = out.split("\n", 1)[1] if "\n" in out else ""
    limit = min(int(ctx.settings.max_tool_output), 9000)
    if code == 0:
        ctx.coding["fails"] = 0
        tail = "\n".join([ln for ln in body.strip().splitlines() if ln.strip()][-4:])
        return f"PASSED: `{cmd}` finished with exit code 0.\n{tail}"
    ctx.coding["fails"] = ctx.coding.get("fails", 0) + 1
    why = f"exit code {code}" if code is not None else out.split("\n", 1)[0].strip("[]")
    hint = ("Read the failure, fix the cause (not the test, unless the test itself is wrong), then "
            "run_tests again.")
    if ctx.coding["fails"] >= MAX_FAILS_BEFORE_REPORT:
        hint = (f"The tests have failed {ctx.coding['fails']} times in a row. If you're stuck, stop and tell "
                "the user what fails and what you tried, instead of guessing further.")
    return f"FAILED: `{cmd}` ({why}).\n{_focus(body, limit)}\n\n{hint}"


@tool("code_map", """
A map of a code folder: every source file with its classes and functions (and line numbers),
so you can see how a project is laid out without reading every file. Use it at the start of a
coding task, then read_file the parts you need.""",
      {"path": {"type": "string", "description": "Folder or file. Default: the work folder"},
       "max_chars": {"type": "integer", "description": "Default 12000"}},
      group="code")
def code_map(ctx: ToolContext, a: dict):
    root = ctx.resolve(a.get("path") or ".")
    limit = int(a.get("max_chars") or 12000)
    files = [root] if root.is_file() else sorted(_code_files(root))
    if not files:
        return f"No source files found in {root}."
    out, used, listed_only = [], 0, []
    for f in files:
        rel = f.name if root.is_file() else f.relative_to(root).as_posix()
        if used > limit:
            listed_only.append(rel)
            continue
        defs = outline(f)
        block = rel + ("\n" + "\n".join(f"  {d}" for d in defs) if defs else "")
        out.append(block)
        used += len(block)
    text = "\n".join(out)
    if listed_only:
        text += f"\n\n[{len(listed_only)} more files, names only]\n" + "\n".join(listed_only[:400])
    return truncate(f"{root}\n{text}", int(ctx.settings.max_tool_output))


def _code_files(root: Path):
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in SKIP and not d.startswith("."))
        for n in filenames:
            p = Path(dirpath) / n
            if p.suffix.lower() in CODE_EXT:
                yield p


_PATTERNS = [
    re.compile(r"^\s*(?:export\s+)?(?:default\s+)?(?:abstract\s+)?(class|interface|struct|enum|trait|type)\s+(\w+)"),
    re.compile(r"^\s*(?:export\s+)?(?:async\s+)?function\s*\*?\s*(\w+)\s*\("),
    re.compile(r"^\s*(?:export\s+)?(?:const|let|var)\s+(\w+)\s*=\s*(?:async\s*)?(?:\([^)]*\)|\w+)\s*=>"),
    re.compile(r"^\s*(?:pub(?:\([\w:]+\))?\s+)?(?:async\s+)?fn\s+(\w+)"),
    re.compile(r"^func\s+(?:\([^)]*\)\s*)?(\w+)"),
    re.compile(r"^\s*(?:public|private|protected|internal|static|override|virtual|async|\s)+[\w<>\[\],?]+\s+(\w+)\s*\([^;]*$"),
    re.compile(r"^\s*def\s+(\w+)"),
    re.compile(r"^\s*(?:local\s+)?function\s+([\w.:]+)"),
    re.compile(r"^\s*impl(?:<[^>]*>)?\s+([\w:<>]+)"),
]


def outline(path: Path, max_items: int = 60) -> list[str]:
    """Classes and functions in a file: 'L12 class Foo', 'L20 def bar(a, b)'."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    if path.suffix == ".py":
        try:
            return _py_outline(ast.parse(text), max_items)
        except SyntaxError as e:
            return [f"(syntax error at line {e.lineno})"]
    out = []
    for i, line in enumerate(text.splitlines(), 1):
        if len(line) > 300:
            continue
        for rx in _PATTERNS:
            m = rx.match(line)
            if m:
                out.append(f"L{i} {line.strip()[:120].rstrip('{').strip()}")
                break
        if len(out) >= max_items:
            out.append("...")
            break
    return out


def _py_outline(tree, max_items: int) -> list[str]:
    out = []

    def sig(fn) -> str:
        args = [x.arg for x in fn.args.posonlyargs + fn.args.args]
        if fn.args.vararg:
            args.append("*" + fn.args.vararg.arg)
        args += [x.arg for x in fn.args.kwonlyargs]
        if fn.args.kwarg:
            args.append("**" + fn.args.kwarg.arg)
        pre = "async def" if isinstance(fn, ast.AsyncFunctionDef) else "def"
        return f"{pre} {fn.name}({', '.join(args)})"

    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            out.append(f"L{node.lineno} {sig(node)}")
        elif isinstance(node, ast.ClassDef):
            out.append(f"L{node.lineno} class {node.name}")
            for sub in node.body:
                if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    out.append(f"  L{sub.lineno} {sig(sub)}")
        if len(out) >= max_items:
            out.append("...")
            break
    return out


CACHE_NAMES = {"__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache", "node_modules", ".venv", "venv",
               "target", "bin", "obj", "dist", "build", ".coverage", "coverage", ".tox", ".next"}


def _is_cache(path: str) -> bool:
    return any(part in CACHE_NAMES for part in path.strip().strip('"').rstrip("/").split("/")) or path.endswith(".pyc")


@tool("finish_coding_task", """
Leave the private copy and go back to the normal work folder, after the change is committed
(and pushed / in a pull request if the user wanted that). delete_copy=true also removes the
copy's folder, but only if nothing in it is left uncommitted; the branch is kept.""",
      {"delete_copy": {"type": "boolean"}}, risk=EDIT, group="code",
      describe=lambda a: "Leave the private copy" + (" and delete its folder" if a.get("delete_copy") else ""))
def finish_coding_task(ctx: ToolContext, a: dict):
    if not ctx.coding.get("folder"):
        raise ToolError("No coding task is active.")
    folder, home, source = Path(ctx.coding["folder"]), Path(ctx.coding["home"]), Path(ctx.coding["source"])
    msg = [f"Back in {home}. The branch {ctx.coding['branch']} is kept."]
    if a.get("delete_copy"):
        dirty = [ln for ln in _git(ctx, ["status", "--porcelain"], cwd=folder, check=False).splitlines()
                 if ln.strip() and not (ln.startswith("??") and _is_cache(ln[3:]))]
        if dirty:
            raise ToolError("The copy has uncommitted changes, so it wasn't deleted:\n" + "\n".join(dirty)[:1500])
        # --force only because of leftover caches (__pycache__ etc.); real changes were checked above
        _git(ctx, ["worktree", "remove", "--force", str(folder)], cwd=source)
        msg.append(f"Deleted the copy at {folder}.")
    else:
        msg.append(f"The copy stays at {folder}.")
    if ctx.agent is not None:
        ctx.agent.set_workspace(home)
    else:
        ctx.workspace = home
    ctx.coding.clear()
    return " ".join(msg)
