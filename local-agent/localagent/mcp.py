"""MCP servers as tools.

Any Model Context Protocol server (the ones Claude Desktop, Cursor and LM Studio use: files,
browsers, databases, Blender, Home Assistant...) adds its tools to the agent. List them in
data/mcp.json, in the same format as Claude Desktop's config:

    {"mcpServers": {
        "browser": {"command": "npx", "args": ["-y", "@playwright/mcp@latest"]},
        "fetch":   {"command": "uvx", "args": ["mcp-server-fetch"], "risk": "safe"}
    }}

Each server runs on this PC as a program the app starts, and talks JSON-RPC over its stdin and
stdout. Its tools are named <server>__<tool>. They ask before running (like run_command) unless
the server says otherwise with "risk": "safe" (never asks), "edit" (asks like file edits), or
"hints" (tools the server marks read-only don't ask, the rest do). "disabled": true skips a server
and "timeout" (seconds, default 300) limits each call.

Servers start in the background when the app opens; their tools are in every chat started after.
"""

from __future__ import annotations

import base64
import collections
import io
import json
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from .config import DATA_DIR
from .tools.base import EDIT, REGISTRY, RUN, SAFE, Tool, ToolError, ToolResult

PROTOCOL = "2025-06-18"
WINDOWS = sys.platform == "win32"
GROUP = "mcp"
RISKS = {"safe": SAFE, "edit": EDIT, "ask": RUN, "run": RUN}
MAX_DESCRIPTION = 600         # every description is sent with every request: keep them short


class McpError(Exception):
    pass


def config_path() -> Path:
    return DATA_DIR / "mcp.json"


def load_config(path: Path | None = None) -> dict[str, dict]:
    """The enabled servers in mcp.json ({} when there's no file). Raises ValueError for a broken file."""
    path = path or config_path()
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    if not text.strip():
        return {}
    try:
        data = json.loads(text)
    except ValueError as e:
        raise ValueError(f"{path.name} isn't valid JSON: {e}") from None
    servers = data.get("mcpServers", data.get("servers", {})) if isinstance(data, dict) else None
    if not isinstance(servers, dict):
        raise ValueError(f'{path.name} needs an "mcpServers" object: {{"mcpServers": {{"name": {{"command": ...}}}}}}')
    out = {}
    for name, cfg in servers.items():
        if not isinstance(cfg, dict):
            raise ValueError(f"Server '{name}' in {path.name} must be an object with a \"command\".")
        if cfg.get("disabled"):
            continue
        if not cfg.get("command"):
            raise ValueError(f"Server '{name}' in {path.name} has no \"command\" (only programs on this PC can "
                             "be used; servers on a URL aren't supported).")
        out[str(name)] = cfg
    return out


def tool_name(server: str, tool: str) -> str:
    """OpenAI-style tool names: letters, digits, _ and -, at most 64 characters."""
    clean = lambda s: re.sub(r"[^A-Za-z0-9_-]+", "_", s).strip("_") or "x"   # noqa: E731
    return f"{clean(server)[:20]}__{clean(tool)}"[:64]


# ---------------------------------------------------------------------- one server
class Client:
    """One MCP server process and the JSON-RPC conversation with it."""

    def __init__(self, name: str, cfg: dict, cwd: Path | None = None):
        self.name, self.cfg, self.cwd = name, cfg, cwd
        self.root = cwd                # the work folder it's told about (roots/list); can change while it runs
        self.retired = False           # the Manager dropped it: never start it again
        self.proc: subprocess.Popen | None = None
        self.tools: list[dict] = []
        self.info: dict = {}
        self.stderr: collections.deque[str] = collections.deque(maxlen=100)
        self._id = 0
        self._pending: dict[int, queue.Queue] = {}
        self._lock = threading.Lock()
        self._start_lock = threading.Lock()

    @property
    def alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def start(self, timeout: float = 60) -> None:
        cmd = str(self.cfg["command"])
        exe = shutil.which(cmd) or cmd          # npx -> npx.cmd on Windows
        args = [exe] + [str(a) for a in self.cfg.get("args") or []]
        env = dict(os.environ)
        env.update({str(k): str(v) for k, v in (self.cfg.get("env") or {}).items()})
        cwd = self.cfg.get("cwd")
        if not cwd and self.cwd:
            cwd = str(self.cwd)
            try:
                Path(cwd).mkdir(parents=True, exist_ok=True)     # on a first run the work folder isn't made yet
            except OSError:
                cwd = None
        try:
            self.proc = subprocess.Popen(args, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                         env=env, cwd=cwd,
                                         creationflags=subprocess.CREATE_NO_WINDOW if WINDOWS else 0)
        except OSError as e:
            if cwd and (e.filename == cwd or not os.path.isdir(cwd)):     # the folder is the problem, not the program
                raise McpError(f"Couldn't start '{cmd}' in the folder {cwd}: {e.strerror or e}.") from None
            raise McpError(f"Couldn't start '{cmd}': {e}. Is it installed (Node.js for npx, uv for uvx)?") from None
        if self.retired:              # stopped while it was starting
            self.close()
            raise McpError(_gone(self.name))
        threading.Thread(target=self._read, args=(self.proc,), daemon=True).start()
        threading.Thread(target=self._drain, args=(self.proc,), daemon=True).start()
        res = self.request("initialize", {"protocolVersion": PROTOCOL, "capabilities": {"roots": {"listChanged": True}},
                                          "clientInfo": {"name": "local-agent", "version": "1.0"}}, timeout)
        self.info = res.get("serverInfo") or {}
        self.notify("notifications/initialized")
        tools, cursor = [], None
        for _ in range(50):
            res = self.request("tools/list", {"cursor": cursor} if cursor else {}, timeout)
            tools += [t for t in res.get("tools") or [] if isinstance(t, dict) and t.get("name")]
            cursor = res.get("nextCursor")
            if not cursor:
                break
        self.tools = tools

    def restart(self, timeout: float = 60) -> None:
        with self._start_lock:
            if self.retired:          # an open chat's tool after Reload: don't run a copy the app doesn't manage
                raise McpError(_gone(self.name))
            if not self.alive:
                self.close()
                self.start(timeout)

    def retire(self) -> None:
        """Close for good (the Manager stopped or replaced this server)."""
        self.retired = True
        self.close()

    def close(self) -> None:
        p, self.proc = self.proc, None
        if p is None:
            return
        try:
            p.stdin.close()
        except OSError:
            pass
        if p.poll() is None:
            if WINDOWS:      # npx and uvx start the real server as a child
                subprocess.run(["taskkill", "/T", "/F", "/PID", str(p.pid)], capture_output=True,
                               creationflags=subprocess.CREATE_NO_WINDOW)
            else:
                p.terminate()
            try:
                p.wait(5)
            except subprocess.TimeoutExpired:
                p.kill()
        for q in list(self._pending.values()):
            q.put({"error": {"message": f"The {self.name} server stopped."}})

    # -------------------------------------------------------------- JSON-RPC
    def _write(self, msg: dict) -> None:
        data = (json.dumps(msg, ensure_ascii=False) + "\n").encode("utf-8")
        with self._lock:
            p = self.proc
            if p is None or p.poll() is not None:
                raise McpError(self._died())
            try:
                p.stdin.write(data)
                p.stdin.flush()
            except OSError:
                raise McpError(self._died()) from None

    def notify(self, method: str, params: dict | None = None) -> None:
        try:
            self._write({"jsonrpc": "2.0", "method": method, **({"params": params} if params else {})})
        except McpError:
            pass

    def request(self, method: str, params: dict, timeout: float = 60, stop: threading.Event | None = None) -> dict:
        with self._lock:
            self._id += 1
            rid = self._id
        q: queue.Queue = queue.Queue()
        self._pending[rid] = q
        try:
            self._write({"jsonrpc": "2.0", "id": rid, "method": method, "params": params})
            end = time.monotonic() + timeout
            while True:
                try:
                    msg = q.get(timeout=0.2)
                except queue.Empty:
                    if not self.alive:
                        raise McpError(self._died()) from None
                    if stop is not None and stop.is_set():
                        self.notify("notifications/cancelled", {"requestId": rid, "reason": "Stopped by the user"})
                        raise McpError("Stopped.") from None
                    if time.monotonic() > end:
                        self.notify("notifications/cancelled", {"requestId": rid, "reason": "Timed out"})
                        raise McpError(f"The {self.name} server didn't answer {method} within {timeout:.0f} s.") \
                            from None
                    continue
                if msg.get("error") is not None:
                    err = msg["error"]
                    raise McpError(err.get("message", str(err)) if isinstance(err, dict) else str(err))
                res = msg.get("result")
                return res if isinstance(res, dict) else {}
        finally:
            self._pending.pop(rid, None)

    def _died(self) -> str:
        tail = "\n".join(list(self.stderr)[-8:])
        return f"The {self.name} MCP server isn't running." + (f" Its last output:\n{tail}" if tail else "")

    def _read(self, proc: subprocess.Popen) -> None:
        for raw in iter(proc.stdout.readline, b""):
            line = raw.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except ValueError:            # some servers print banners to stdout
                self.stderr.append(raw.decode("utf-8", "replace").rstrip())
                continue
            for m in msg if isinstance(msg, list) else [msg]:
                if isinstance(m, dict):
                    self._dispatch(m)

    def _dispatch(self, m: dict) -> None:
        if "method" in m:
            if "id" in m:                 # the server asks us something
                method = m["method"]
                if method == "ping":
                    reply = {"result": {}}
                elif method == "roots/list":
                    root = Path(self.root).resolve().as_uri() if self.root else None
                    reply = {"result": {"roots": [{"uri": root, "name": "work folder"}] if root else []}}
                else:
                    reply = {"error": {"code": -32601, "message": f"{method} isn't supported by this client"}}
                try:
                    self._write({"jsonrpc": "2.0", "id": m["id"], **reply})
                except McpError:
                    pass
            return                        # notifications (logs, progress, list changes): nothing to do
        q = self._pending.get(m.get("id"))
        if q is not None:
            q.put(m)

    def _drain(self, proc: subprocess.Popen) -> None:
        for raw in iter(proc.stderr.readline, b""):
            self.stderr.append(raw.decode("utf-8", "replace").rstrip())

    # -------------------------------------------------------------- tools
    def call(self, tool: str, args: dict, stop: threading.Event | None = None) -> ToolResult:
        if not self.alive:
            self.restart()
        timeout = float(self.cfg.get("timeout") or 300)
        res = self.request("tools/call", {"name": tool, "arguments": args or {}}, timeout, stop)
        texts, images = [], []
        for c in res.get("content") or []:
            if not isinstance(c, dict):
                continue
            kind = c.get("type")
            if kind == "text":
                texts.append(str(c.get("text", "")))
            elif kind == "image" and c.get("data"):
                png = _png(c["data"], c.get("mimeType", ""))
                if png:
                    images.append(png)
                else:
                    texts.append(f"[an image of type {c.get('mimeType')} that couldn't be shown]")
            elif kind == "resource":
                r = c.get("resource") or {}
                texts.append(str(r.get("text") or f"[resource {r.get('uri', '')}]"))
            elif kind == "resource_link":
                texts.append(f"[link] {c.get('name', '')} {c.get('uri', '')}".strip())
            else:
                texts.append(f"[{kind} content]")
        if not texts and res.get("structuredContent") is not None:
            texts.append(json.dumps(res["structuredContent"], ensure_ascii=False, indent=1))
        text = "\n".join(texts) or ("(the tool returned an image)" if images else "(no output)")
        return ToolResult(text, images=images, ok=not res.get("isError"))


def _png(data: str, mime: str) -> bytes | None:
    try:
        raw = base64.b64decode(data)
    except ValueError:
        return None
    if "png" in mime:
        return raw
    try:
        from PIL import Image

        buf = io.BytesIO()
        Image.open(io.BytesIO(raw)).save(buf, "PNG")
        return buf.getvalue()
    except Exception:  # noqa: BLE001
        return None


# ---------------------------------------------------------------------- the servers as tools
@dataclass
class McpTool(Tool):
    """A tool whose parameters come straight from the server's JSON schema."""
    input_schema: dict = field(default_factory=dict)

    def schema(self) -> dict:
        params = {k: v for k, v in self.input_schema.items() if k not in ("$schema", "title")}
        params.setdefault("type", "object")
        params.setdefault("properties", {})
        return {"type": "function",
                "function": {"name": self.name, "description": self.description, "parameters": params}}


def _gone(server: str) -> str:
    return (f"The {server} MCP server was stopped or reloaded; this tool is gone. "
            "Carry on without it, or ask the user to start a new chat to get the current MCP tools.")


def _short(text: str) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= MAX_DESCRIPTION else text[:MAX_DESCRIPTION].rsplit(" ", 1)[0] + "..."


def _register(client: Client, clients: dict[str, Client]) -> list[str]:
    """Add the server's tools. A call goes to the server running under that name when it's made
    (clients is the Manager's), so a chat that outlives a Reload uses the new one, never the old."""
    risk_cfg = str(client.cfg.get("risk", "ask")).lower()
    names = []
    for t in client.tools:
        name = tool_name(client.name, t["name"])
        old = REGISTRY.get(name)
        if old is not None and old.group != GROUP:
            continue                       # never shadow a built-in tool
        hint = bool((t.get("annotations") or {}).get("readOnlyHint"))
        risk = (SAFE if hint else RUN) if risk_cfg == "hints" else RISKS.get(risk_cfg, RUN)
        schema = t.get("inputSchema") if isinstance(t.get("inputSchema"), dict) else {}

        def run(ctx, a, _t=t["name"]):
            c = clients.get(client.name)
            try:
                if c is None:
                    raise McpError(_gone(client.name))
                return c.call(_t, a, ctx.stop)
            except McpError as e:
                raise ToolError(str(e)) from None

        REGISTRY[name] = McpTool(
            name=name, description=_short(f"[{client.name}] {t.get('description') or t.get('title') or t['name']}"),
            params=schema.get("properties") or {}, func=run, risk=risk, required=list(schema.get("required") or []),
            group=GROUP, describe=lambda a, _t=t["name"]: f"{client.name}: {_t} {json.dumps(a, ensure_ascii=False)[:300]}",
            input_schema=schema)
        names.append(name)
    return names


class Manager:
    """Starts the servers in mcp.json and keeps their tools in the tool registry."""

    def __init__(self):
        self.clients: dict[str, Client] = {}
        self.errors: dict[str, str] = {}
        self.starting = False
        self.root: Path | None = None          # the work folder the servers are told about
        self._launching: set[Client] = set()   # still starting up: stop() ends these too
        self._gen = 0                          # every start() and stop() adds one; an older start gives up
        self._lock = threading.Lock()

    def start(self, path: Path | None = None, cwd: Path | None = None, timeout: float = 90) -> None:
        """(Re)start every server; blocks until each one is up or failed (they start side by side).
        The lock isn't held while they start, so stop() and a newer start() don't wait for them."""
        with self._lock:
            self.starting = True
            self._stop()
            gen, self.root = self._gen, cwd
            try:
                cfg = load_config(path)
            except (OSError, ValueError) as e:
                self.errors["mcp.json"] = str(e)
                self.starting = False
                return
        threads = [threading.Thread(target=self._start_one, args=(gen, n, c, cwd, timeout), daemon=True)
                   for n, c in cfg.items()]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout + 10)
        with self._lock:
            if gen == self._gen:
                self.starting = False

    def start_background(self, path: Path | None = None, cwd: Path | None = None) -> threading.Thread:
        t = threading.Thread(target=self.start, args=(path, cwd), daemon=True)
        t.start()
        return t

    def _start_one(self, gen: int, name: str, cfg: dict, cwd: Path | None, timeout: float) -> None:
        c = Client(name, cfg, cwd)
        with self._lock:
            if gen != self._gen:
                return
            c.root = self.root
            self._launching.add(c)
        try:
            c.start(timeout)
        except Exception as e:  # noqa: BLE001
            tail = "\n".join(list(c.stderr)[-5:])
            with self._lock:
                self._launching.discard(c)
                if gen == self._gen:
                    self.errors[name] = f"{e}" + (f"\n{tail}" if tail and tail not in str(e) else "")
            c.close()
            return
        with self._lock:
            self._launching.discard(c)
            if gen != self._gen:           # stop() or a newer start() came first and closed it
                return
            self.clients[name] = c
            _register(c, self.clients)

    def _stop(self) -> None:
        self._gen += 1
        for name in [n for n, t in list(REGISTRY.items()) if t.group == GROUP]:
            REGISTRY.pop(name, None)
        for c in list(self.clients.values()) + list(self._launching):
            c.retire()
        self.clients.clear()
        self._launching.clear()
        self.errors.clear()

    def stop(self) -> None:
        with self._lock:
            self._stop()
            self.starting = False

    def set_root(self, path: Path | None) -> None:
        """The work folder changed: tell the servers (their roots). They keep running in the folder
        they started in. Doesn't wait for them."""
        path = Path(path) if path else None
        with self._lock:
            if path == self.root:
                return
            self.root = path
            for c in list(self._launching) + list(self.clients.values()):
                c.root = path              # ones still starting ask for the roots once they're up
            running = list(self.clients.values())
        for c in running:
            c.notify("notifications/roots/list_changed")

    def summary(self) -> str:
        if self.starting:
            return "Starting MCP servers..."
        parts = [f"{n}: {len(c.tools)} tools" for n, c in list(self.clients.items())]
        parts += [f"{n}: failed ({e.splitlines()[0][:120]})" for n, e in list(self.errors.items())]
        return "; ".join(parts) or "No MCP servers set up."


_manager: Manager | None = None


def manager() -> Manager:
    global _manager
    if _manager is None:
        _manager = Manager()
    return _manager
