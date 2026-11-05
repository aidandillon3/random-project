"""Running commands and code."""

from __future__ import annotations

import itertools
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time

from .base import PUBLISH, RUN, SAFE, ToolContext, ToolError, tool, truncate

WINDOWS = sys.platform == "win32"
_ids = itertools.count(1)

# Nobody sits at the console of what the agent starts, so tools must fail instead of waiting for
# a typed answer (git credentials, ssh host keys, pip, test runners in watch mode).
QUIET_ENV = {"GIT_TERMINAL_PROMPT": "0", "GCM_INTERACTIVE": "never", "PIP_NO_INPUT": "1", "CI": "1"}

# git, maybe with -C <folder>, -c key=value or --options before the subcommand
GIT_CMD = r"""\bgit(?:\.exe)?(?:\s+(?:-[Cc]\s*(?:"[^"]*"|'[^']*'|\S+)|--\S+))*\s+"""
# Commands that post to GitHub (or a package registry) under the user's name. They ask like
# git_push does, even in full auto.
PUBLISH_COMMAND = re.compile(
    GIT_CMD + r"push\b"
    r"|\bgh(?:\.exe)?\s+(?:pr|issue|release|repo|gist)\s+"
    r"(?:create|merge|delete|edit|comment|close|reopen|review|upload|rename|archive|fork)\b"
    r"|\bgh(?:\.exe)?\s+api\b[^|;&\n]*\s(?:-X\s*|--method[\s=]*)(?:POST|PUT|PATCH|DELETE)\b"
    r"|\bgh(?:\.exe)?\s+api\b[^|;&\n]*\s(?:-f|--field|--raw-field|--input)\b"
    r"|\b(?:npm|pnpm|yarn)\s+publish\b|\btwine\s+upload\b|\bcargo\s+publish\b|\bdotnet\s+nuget\s+push\b",
    re.IGNORECASE)


def command_risk(ctx: ToolContext, a: dict) -> str:
    return PUBLISH if PUBLISH_COMMAND.search(str(a.get("command") or "")) else RUN


def quiet_env(**extra) -> dict:
    env = dict(os.environ, **QUIET_ENV)
    if "GIT_SSH_COMMAND" not in env and "GIT_SSH" not in env:  # keep the user's own ssh setup
        env["GIT_SSH_COMMAND"] = "ssh -o BatchMode=yes"
    env.update(extra)
    return env


def git_bash() -> str | None:
    """Git for Windows' bash (what pi uses on Windows), not WSL's bash.exe in System32."""
    if not WINDOWS:
        return shutil.which("bash")
    roots = [os.environ.get(k) for k in ("ProgramFiles", "ProgramW6432", "ProgramFiles(x86)", "LOCALAPPDATA")]
    places = [Path(r) / "Git" / "bin" / "bash.exe" for r in roots if r]
    places += [Path(os.environ["LOCALAPPDATA"]) / "Programs" / "Git" / "bin" / "bash.exe"] \
        if os.environ.get("LOCALAPPDATA") else []
    git = shutil.which("git")
    if git:       # ...\Git\cmd\git.exe or ...\Git\bin\git.exe
        places.append(Path(git).resolve().parent.parent / "bin" / "bash.exe")
    return next((str(p) for p in places if p.is_file()), None)


def shell_kind(ctx: ToolContext) -> str:
    """auto: bash, like pi (Git Bash on Windows: the shell models know best), else PowerShell."""
    kind = getattr(ctx.settings, "shell", "auto")
    if kind != "auto":
        return kind
    return "bash" if git_bash() else "powershell" if WINDOWS else "bash"


def _argv(kind: str, command: str) -> list[str]:
    if kind == "powershell":
        exe = shutil.which("pwsh") or shutil.which("powershell") or "powershell"
        prefix = "[Console]::OutputEncoding=[Text.Encoding]::UTF8; $ProgressPreference='SilentlyContinue'; "
        return [exe, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", prefix + command]
    if kind == "cmd":
        # cmd.exe has its own quoting: with /s it drops the outer quotes and runs the rest as typed.
        # A list would be re-quoted with \" escapes, which cmd doesn't understand.
        return f'cmd /d /s /c "{command}"' if WINDOWS else ["cmd", "/d", "/s", "/c", command]
    return [git_bash() or shutil.which("bash") or "/bin/sh", "-c", command]


def _flags():
    return subprocess.CREATE_NO_WINDOW if WINDOWS else 0


def run_process(argv, cwd, env, timeout: float, stop=None) -> tuple[int | None, str, str]:
    """Run a program until it ends, times out or Stop is pressed. Returns (exit code, output, why it
    was stopped or ''). Output is read on a thread and kept when the program is stopped, and a
    grandchild still holding the pipe (a server the command started) can't make this hang."""
    proc = subprocess.Popen(argv, cwd=cwd, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            stdin=subprocess.DEVNULL, creationflags=_flags(), start_new_session=not WINDOWS)
    out: list[bytes] = []

    def pump():
        for chunk in iter(lambda: proc.stdout.read1(65536), b""):
            out.append(chunk)

    reader = threading.Thread(target=pump, daemon=True)
    reader.start()
    start, why = time.time(), ""
    while proc.poll() is None:
        if stop is not None and stop.is_set():
            why = "stopped by you"
        elif time.time() - start > timeout:
            why = f"timed out after {timeout}s"
        else:
            time.sleep(0.05)
            continue
        _kill(proc)
        break
    reader.join(2 if why else 5)
    if not reader.is_alive():
        proc.stdout.close()
    return (None if why else proc.returncode), b"".join(out).decode("utf-8", "replace"), why


@tool("run_command", """
Run a shell command and get its output (the shell is named in the system prompt; bash unless
Settings picks another). Runs in the work folder unless cwd is given. Set background=true for things that keep running (servers, watchers); then read
them with check_process. Commands still running after timeout seconds (default 120) are stopped.""",
      {"command": {"type": "string"},
       "cwd": {"type": "string"},
       "timeout": {"type": "integer", "description": "Seconds, default 120, max 1800"},
       "background": {"type": "boolean"}},
      required=["command"], risk=RUN, group="shell", risk_for=command_risk,
      describe=lambda a: str(a.get("command") or ""))
def run_command(ctx: ToolContext, a: dict):
    cwd = ctx.resolve(a.get("cwd") or ".")
    if not cwd.is_dir():
        raise ToolError(f"cwd is not a folder: {cwd}")
    command = str(a["command"])
    argv = _argv(shell_kind(ctx), command)
    env = quiet_env(PYTHONIOENCODING="utf-8", **(a.get("_env") or {}))
    if a.get("background"):
        return _start_background(ctx, argv, cwd, env, command)
    timeout = min(1800, max(1, int(a.get("timeout") or 120)))
    code, text, why = run_process(argv, cwd, env, timeout, ctx.stop)
    if why:
        return f"[command {why}]\n" + truncate(text, ctx.settings.max_tool_output)
    return f"[exit code {code}]\n" + (truncate(text.strip(), ctx.settings.max_tool_output) or "(no output)")


def _kill(proc: subprocess.Popen):
    """Stop a program and everything it started."""
    try:
        if WINDOWS:
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)], capture_output=True, timeout=15,
                           creationflags=_flags())
        else:
            os.killpg(proc.pid, signal.SIGKILL)   # its own process group (start_new_session)
    except (OSError, subprocess.SubprocessError):
        pass
    if proc.poll() is None:
        try:
            proc.kill()
        except OSError:
            pass


def stop_background(ctx: ToolContext) -> int:
    """Stop every background command of this chat, so servers and watchers don't keep running unseen
    after the chat is reset or the app closes. Returns how many were still running."""
    running = 0
    for buf in list(ctx.processes.values()):
        if buf["proc"].poll() is None:
            _kill(buf["proc"])
            running += 1
    ctx.processes.clear()
    return running


def _start_background(ctx, argv, cwd, env, command):
    proc = subprocess.Popen(argv, cwd=cwd, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            stdin=subprocess.DEVNULL, creationflags=_flags(), start_new_session=not WINDOWS)
    pid = f"p{next(_ids)}"
    buf = {"proc": proc, "out": bytearray(), "read": 0, "command": command}

    def pump():
        for chunk in iter(lambda: proc.stdout.read1(4096), b""):
            buf["out"] += chunk
            if len(buf["out"]) > 2_000_000:
                del buf["out"][:1_000_000]
                buf["read"] = max(0, buf["read"] - 1_000_000)

    threading.Thread(target=pump, daemon=True).start()
    ctx.processes[pid] = buf
    time.sleep(1.0)
    return f"Started in the background as {pid}.\n" + _drain(ctx, pid)


def _drain(ctx, pid) -> str:
    buf = ctx.processes[pid]
    new = bytes(buf["out"][buf["read"]:])
    buf["read"] = len(buf["out"])
    code = buf["proc"].poll()
    state = "still running" if code is None else f"exited with code {code}"
    text = new.decode("utf-8", "replace")
    return f"[{pid} {state}]\n" + (truncate(text, ctx.settings.max_tool_output) or "(no new output)")


@tool("check_process", "Read new output from a background command started with run_command.",
      {"id": {"type": "string"}}, required=["id"], group="shell")
def check_process(ctx: ToolContext, a: dict):
    if a["id"] not in ctx.processes:
        running = ", ".join(ctx.processes) or "none"
        raise ToolError(f"No background process {a['id']}. Known: {running}")
    return _drain(ctx, a["id"])


@tool("stop_process", "Stop a background command started with run_command.",
      {"id": {"type": "string"}}, required=["id"], risk=SAFE, group="shell")
def stop_process(ctx: ToolContext, a: dict):
    buf = ctx.processes.get(a["id"])
    if not buf:
        raise ToolError(f"No background process {a['id']}")
    _kill(buf["proc"])
    return f"Stopped {a['id']}."


@tool("run_python", """
Run a Python script and get its output. Good for calculations, data processing and quick
automation. The script runs with the same Python as this app, in the work folder.""",
      {"code": {"type": "string"}, "timeout": {"type": "integer"}},
      required=["code"], risk=RUN, group="shell",
      describe=lambda a: "Run Python:\n" + str(a.get("code") or ""))
def run_python(ctx: ToolContext, a: dict):
    with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False, encoding="utf-8") as f:
        f.write(str(a["code"]))
        script = f.name
    try:
        code, text, why = run_process([sys.executable, script], ctx.workspace, quiet_env(PYTHONIOENCODING="utf-8"),
                                      min(1800, max(1, int(a.get("timeout") or 120))), ctx.stop)
    finally:
        try:
            os.unlink(script)
        except OSError:
            pass
    if why:
        return f"[script {why}]\n" + truncate(text, ctx.settings.max_tool_output)
    return f"[exit code {code}]\n" + (truncate(text.strip(), ctx.settings.max_tool_output) or "(no output)")
