"""Running commands and code."""

from __future__ import annotations

import itertools
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time

from .base import RUN, SAFE, ToolContext, ToolError, tool, truncate

WINDOWS = sys.platform == "win32"
_ids = itertools.count(1)


def shell_kind(ctx: ToolContext) -> str:
    kind = getattr(ctx.settings, "shell", "auto")
    if kind != "auto":
        return kind
    return "powershell" if WINDOWS else "bash"


def _argv(kind: str, command: str) -> list[str]:
    if kind == "powershell":
        exe = shutil.which("pwsh") or shutil.which("powershell") or "powershell"
        prefix = "[Console]::OutputEncoding=[Text.Encoding]::UTF8; $ProgressPreference='SilentlyContinue'; "
        return [exe, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", prefix + command]
    if kind == "cmd":
        return ["cmd", "/d", "/s", "/c", command]
    return [shutil.which("bash") or "/bin/sh", "-c", command]


def _flags():
    return subprocess.CREATE_NO_WINDOW if WINDOWS else 0


@tool("run_command", """
Run a shell command and get its output (PowerShell on Windows). Runs in the work folder unless
cwd is given. Set background=true for things that keep running (servers, watchers); then read
them with check_process. Long silent commands are stopped after timeout seconds (default 120).""",
      {"command": {"type": "string"},
       "cwd": {"type": "string"},
       "timeout": {"type": "integer", "description": "Seconds, default 120, max 1800"},
       "background": {"type": "boolean"}},
      required=["command"], risk=RUN, group="shell",
      describe=lambda a: a.get("command", ""))
def run_command(ctx: ToolContext, a: dict):
    cwd = ctx.resolve(a.get("cwd") or ".")
    if not cwd.is_dir():
        raise ToolError(f"cwd is not a folder: {cwd}")
    argv = _argv(shell_kind(ctx), a["command"])
    env = dict(os.environ, PYTHONIOENCODING="utf-8", **(a.get("_env") or {}))
    if a.get("background"):
        return _start_background(ctx, argv, cwd, env, a["command"])
    timeout = min(1800, max(1, int(a.get("timeout") or 120)))
    proc = subprocess.Popen(argv, cwd=cwd, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            stdin=subprocess.DEVNULL, creationflags=_flags())
    out: list[bytes] = []
    reader = threading.Thread(target=lambda: out.extend(iter(lambda: proc.stdout.read(4096), b"")), daemon=True)
    reader.start()
    start = time.time()
    while proc.poll() is None:
        if ctx.stop.is_set() or time.time() - start > timeout:
            _kill(proc)
            reader.join(2)
            why = "stopped by you" if ctx.stop.is_set() else f"timed out after {timeout}s"
            text = b"".join(out).decode("utf-8", "replace")
            return f"[command {why}]\n" + truncate(text, ctx.settings.max_tool_output)
        time.sleep(0.05)
    reader.join(5)
    text = b"".join(out).decode("utf-8", "replace").strip()
    return f"[exit code {proc.returncode}]\n" + (truncate(text, ctx.settings.max_tool_output) or "(no output)")


def _kill(proc: subprocess.Popen):
    try:
        if WINDOWS:
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)], capture_output=True,
                           creationflags=_flags())
        else:
            proc.kill()
    except OSError:
        pass


def _start_background(ctx, argv, cwd, env, command):
    proc = subprocess.Popen(argv, cwd=cwd, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            stdin=subprocess.DEVNULL, creationflags=_flags())
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
      describe=lambda a: "Run Python:\n" + a.get("code", ""))
def run_python(ctx: ToolContext, a: dict):
    with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False, encoding="utf-8") as f:
        f.write(a["code"])
        script = f.name
    try:
        proc = subprocess.run([sys.executable, script], cwd=ctx.workspace, capture_output=True,
                              timeout=min(1800, int(a.get("timeout") or 120)), creationflags=_flags(),
                              env=dict(os.environ, PYTHONIOENCODING="utf-8"))
    except subprocess.TimeoutExpired:
        return "[script timed out]"
    finally:
        os.unlink(script)
    text = (proc.stdout + proc.stderr).decode("utf-8", "replace").strip()
    return f"[exit code {proc.returncode}]\n" + (truncate(text, ctx.settings.max_tool_output) or "(no output)")
