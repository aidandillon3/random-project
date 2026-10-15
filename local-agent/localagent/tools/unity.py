"""Unity: look at and change the open Unity Editor (scenes, objects, components, prefabs,
materials, scripts, the console, play mode and pictures of the scene).

The work happens inside Unity, in a small editor script (unity/LocalAgentBridge.cs) that
unity_install_bridge copies into the project. It listens on 127.0.0.1 and writes its port and a
random key to <project>/Library/LocalAgentBridge.json and to the temp folder, where these tools
find it. One request per connection: a line of JSON in, a line of JSON out.
"""

from __future__ import annotations

import base64
import json
import os
import shutil
import socket
import tempfile
import time
from pathlib import Path

from .base import EDIT, RUN, SAFE, ToolContext, ToolError, ToolResult, tool, truncate

BRIDGE_DIR = Path(__file__).resolve().parent.parent.parent / "unity"
BRIDGE_SRC = BRIDGE_DIR / "LocalAgentBridge.cs"
# its own assembly, so it still loads when the project's own scripts have compile errors
BRIDGE_FILES = ["LocalAgentBridge.cs", "LocalAgent.Editor.asmdef"]
BRIDGE_DEST = Path("Assets") / "Editor" / "LocalAgent"
BRIDGE_VERSION = 2    # LocalAgentBridge.BridgeVersion that these tools need
POLL = 1.0            # seconds between checks while Unity compiles or enters play mode
RELOAD_WAIT = 90      # seconds to wait for Unity to finish reloading scripts
INSTALL_WAIT = 25     # seconds to wait for Unity to load a newly installed bridge
# Tools to offer even when no Unity project is detected: they find or set up the bridge.
ALWAYS_ON = ("unity_status", "unity_install_bridge")
# Commands that are safe to send again when Unity dropped the connection before answering.
RESEND_OK = {"ping", "hierarchy", "inspect", "find", "assets", "console", "compile", "screenshot", "refresh"}


def discovery_dir() -> Path:
    return Path(os.environ.get("LOCALAGENT_UNITY_DIR") or Path(tempfile.gettempdir()) / "localagent-unity")


def _same(a: str, b: str) -> bool:
    return os.path.normcase(os.path.realpath(str(a))) == os.path.normcase(os.path.realpath(str(b)))


def is_unity_project(p: Path) -> bool:
    return (p / "Assets").is_dir() and (p / "ProjectSettings").is_dir()


def project_of(path: Path) -> Path | None:
    """The Unity project that contains path, if any."""
    for p in [path, *path.parents]:
        if is_unity_project(p):
            return p
    return None


def detected(workspace: Path | None, settings=None) -> bool:
    """Whether Unity tools are worth offering: a bridge has announced itself, a project is set, or
    the work folder is in a Unity project. Only looks at files, so it's cheap."""
    try:
        d = discovery_dir()
        if d.is_dir() and any(d.glob("*.json")):
            return True
    except OSError:
        pass
    if getattr(settings, "unity_project", ""):
        return True
    if workspace:
        ws = Path(workspace)
        for p in [ws, *list(ws.parents)[:3]]:
            try:
                if (p / "ProjectSettings" / "ProjectVersion.txt").is_file():
                    return True
            except OSError:
                pass
    return False


def hub_projects() -> list[str]:
    """Projects listed in Unity Hub, newest first (Windows keeps them in %APPDATA%/UnityHub)."""
    appdata = os.environ.get("APPDATA")
    if not appdata:
        return []
    try:
        data = json.loads((Path(appdata) / "UnityHub" / "projects-v1.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    items = (data.get("data") or {}).values() if isinstance(data, dict) else []
    items = sorted(items, key=lambda x: x.get("lastModified", 0) if isinstance(x, dict) else 0, reverse=True)
    return [x["path"] for x in items if isinstance(x, dict) and x.get("path")]


def _read(f: Path) -> dict | None:
    try:
        info = json.loads(f.read_text(encoding="utf-8"))
        return info if info.get("port") and info.get("token") else None
    except (OSError, ValueError, AttributeError):
        return None


def _pid_alive(pid) -> bool:
    """False only when that process surely isn't running (Unity crashed and left its file behind)."""
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return True
    if pid <= 0:
        return True
    if os.name == "nt":       # os.kill would end the process on Windows
        try:
            import ctypes
            k32 = ctypes.WinDLL("kernel32", use_last_error=True)
            k32.OpenProcess.restype = ctypes.c_void_p
            k32.OpenProcess.argtypes = [ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong]
            k32.GetExitCodeProcess.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong)]
            k32.CloseHandle.argtypes = [ctypes.c_void_p]
            h = k32.OpenProcess(0x1000, False, pid)        # PROCESS_QUERY_LIMITED_INFORMATION
            if not h:
                return ctypes.get_last_error() != 87       # ERROR_INVALID_PARAMETER: no such process
            code = ctypes.c_ulong()
            ok = k32.GetExitCodeProcess(h, ctypes.byref(code))
            k32.CloseHandle(h)
            return not ok or code.value == 259             # STILL_ACTIVE
        except (OSError, AttributeError):
            return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        return True
    return True


def bridges(extra_projects=()) -> list[dict]:
    """Every bridge that has announced itself and whose Unity is still running."""
    found = []
    d = discovery_dir()
    if d.is_dir():
        for f in sorted(d.glob("*.json")):
            info = _read(f)
            if info:
                found.append(info)
    for p in extra_projects:
        if p:
            info = _read(Path(p) / "Library" / "LocalAgentBridge.json")
            if info and not any(_same(info["project"], b["project"]) for b in found):
                found.append(info)
    return [b for b in found if _pid_alive(b.get("pid"))]


class BridgeDown(ToolError):
    """Unity didn't take the command: trying again later is safe."""


class NoBridge(BridgeDown):
    """No Unity has announced a bridge at all: nothing to wait for."""


class BridgeBusy(BridgeDown):
    """Unity answered, but couldn't start the command in time (busy or reloading). It never ran."""


class BridgeReloading(BridgeDown):
    """Unity is reloading scripts; the bridge comes back when it's done."""


DOWN = "Unity isn't answering. It may be busy, showing a dialog box, or closed."
RELOADING = "Unity is reloading scripts (it does after compiling and when play mode starts)."


def _send(info: dict, payload: dict, timeout: float) -> dict:
    wait = max(4, int(timeout) - 6)   # the bridge drops a command still queued by then, and says so
    req = dict(payload, token=info["token"], timeout=wait)
    sent = False
    buf = bytearray()
    try:
        with socket.create_connection(("127.0.0.1", int(info["port"])), timeout=5) as s:
            s.settimeout(wait + 6)    # it answers within wait + 3 s, even when the command started late
            s.sendall(json.dumps(req).encode("utf-8") + b"\n")
            sent = True
            while not buf.endswith(b"\n"):
                chunk = s.recv(1 << 16)
                if not chunk:
                    break
                buf += chunk
    except OSError:
        pass
    if not buf.endswith(b"\n"):
        if sent and payload.get("cmd") not in RESEND_OK:
            raise ToolError(f"Unity stopped answering in the middle of '{payload.get('cmd')}', so it may or may "
                            "not have happened. Look (unity_hierarchy or unity_inspect) before trying it again.")
        raise (BridgeReloading if info.get("reloading") else BridgeDown)(RELOADING if info.get("reloading") else DOWN)
    try:
        resp = json.loads(buf.decode("utf-8"))
    except ValueError:
        raise BridgeDown("Unity sent an answer that couldn't be read.")
    if not resp.get("ok") and resp.get("retry"):
        raise BridgeBusy(resp.get("error") or "Unity is busy.")
    return resp


def _candidates(ctx: ToolContext, want=None) -> list[str]:
    out = [want, _state(ctx).get("project"), getattr(ctx.settings, "unity_project", "")]
    p = project_of(ctx.workspace)
    if p:
        out.append(str(p))
    return [x for x in out if x]


def _state(ctx: ToolContext) -> dict:
    return ctx.unity


def _enable_tools(ctx: ToolContext):
    """Unity is there: ask the agent to offer the rest of the Unity tools."""
    fn = getattr(ctx, "enable_group", None)
    if callable(fn):
        try:
            fn("unity")
        except Exception:  # noqa: BLE001, S110 - the tools still work without it
            pass


def connect(ctx: ToolContext, want: str | None = None) -> dict:
    """Pick the bridge to talk to: want, else the chosen project, else the only Unity that answers."""
    found = bridges(_candidates(ctx, want))
    chosen = want or _state(ctx).get("project") or getattr(ctx.settings, "unity_project", "")
    if chosen:
        match = [b for b in found if _same(b["project"], chosen)]
        if not match:
            raise NoBridge(f"The Unity project {chosen} isn't open in Unity with the bridge running. Open it in "
                           "Unity (install the bridge with unity_install_bridge if needed), then call unity_status.")
        return match[0]
    alive, reloading = [], []
    for b in found:
        try:
            _send(b, {"cmd": "ping"}, 10)
            alive.append(b)
        except BridgeBusy:
            alive.append(b)            # busy, but there
        except BridgeDown:
            if b.get("reloading"):
                reloading.append(b)
    ws = project_of(ctx.workspace)
    pick = alive[0] if len(alive) == 1 else next((b for b in alive if ws and _same(b["project"], ws)), None)
    if pick:
        _state(ctx)["project"] = pick["project"]     # stick with it, so later calls skip the search
        return pick
    if len(alive) > 1:
        raise ToolError("Several Unity projects are open: " + "; ".join(b["project"] for b in alive) +
                        ". Pick one with unity_status(project=...).")
    if reloading:
        raise BridgeReloading(RELOADING)
    raise (BridgeDown if found else NoBridge)(not_connected_help(ctx))


def not_connected_help(ctx: ToolContext) -> str:
    lines = ["No Unity editor with the Local Agent bridge is running."]
    hub = hub_projects()[:8]
    ws = project_of(ctx.workspace)
    if ws:
        lines.append(f"The work folder is the Unity project {ws}.")
    if hub:
        lines.append("Unity Hub projects: " + "; ".join(hub))
    lines.append("If the project is open in Unity, install the bridge with unity_install_bridge(project=...); "
                 "if it's installed, make sure the project is open in Unity and has no compile errors that stop "
                 "editor scripts from loading.")
    return "\n".join(lines)


def call(ctx: ToolContext, cmd: str, timeout: float = 60, wait_reload: float = 30, want: str | None = None,
         **args) -> dict:
    """Run one bridge command. While Unity is busy or reloading scripts, keep trying for a while."""
    start = time.monotonic()
    payload = {"cmd": cmd, **{k: v for k, v in args.items() if v is not None}}
    while True:
        try:
            resp = _send(connect(ctx, want), payload, timeout)
            break
        except BridgeDown as e:
            if isinstance(e, NoBridge) or ctx.stop.is_set():
                raise
            reload = isinstance(e, BridgeReloading) and wait_reload > 0
            waited = time.monotonic() - start
            if waited >= (max(wait_reload, RELOAD_WAIT) if reload else wait_reload):
                if reload:
                    raise BridgeReloading(f"Unity has been reloading scripts for over {waited:.0f} s. Wait a "
                                          "little, then try again (unity_status says when it's back).") from e
                raise
            time.sleep(POLL)
    if not resp.get("ok"):
        err = resp.get("error") or "Unity said no."
        if err.startswith("Unknown command"):
            err += " The bridge script in the project is probably out of date: run unity_install_bridge."
        raise ToolError(err)
    return resp.get("result") or {}


def _ping(ctx: ToolContext) -> dict | None:
    """One quick look while waiting on Unity; None while it can't answer."""
    try:
        return call(ctx, "ping", timeout=10, wait_reload=0)
    except NoBridge:
        raise
    except BridgeDown:
        return None


def _arg(a: dict, *keys):
    return {k: a[k] for k in keys if k in a and a[k] not in (None, "")}


def _num(a: dict, key: str, default: float) -> float:
    try:
        return float(a[key]) if a.get(key) not in (None, "") else default
    except (TypeError, ValueError):
        return default


def _outdated(st: dict) -> str:
    try:
        v = int(str(st.get("bridge") or 0))
    except ValueError:
        v = 0
    if v >= BRIDGE_VERSION:
        return ""
    return (f"The bridge script in this project is out of date (version {st.get('bridge') or '?'}; these tools "
            f"need {BRIDGE_VERSION}). Update it with unity_install_bridge(project=...).")


# ---------------------------------------------------------------------- formatting
def fmt_status(st: dict) -> str:
    lines = [f"Connected to Unity {st.get('unity')} with the project {st.get('project')} "
             f"({st.get('render_pipeline')} render pipeline)."]
    mode = "playing" + (" (paused)" if st.get("paused") else "") if st.get("playing") else "edit mode"
    lines.append(f"Mode: {mode}." + (" Compiling scripts now." if st.get("compiling") else ""))
    if st.get("prefab_stage"):
        p = st["prefab_stage"]
        lines.append(f"Prefab Mode is open on {p}: its objects are {p}/Child, and new objects go into it.")
    for s in st.get("scenes", []):
        bits = [s.get("path") or "never saved"]
        if s.get("active"):
            bits.append("active")
        if s.get("dirty"):
            bits.append("unsaved changes")
        lines.append(f"Scene: {s.get('name') or 'Untitled'} ({', '.join(bits)})")
    if st.get("selection"):
        lines.append("Selected in Unity: " + ", ".join(st["selection"]))
    if st.get("compile_errors"):
        lines.append(f"Compile errors: {st['compile_errors']} (unity_refresh lists them).")
    elif st.get("compile_failed"):
        lines.append("Scripts have compile errors (unity_refresh lists them).")
    if _outdated(st):
        lines.append(_outdated(st))
    return "\n".join(lines)


def _node_line(n: dict, indent: int) -> str:
    bits = [f"{'  ' * indent}- {n['name']}" + (f" #{n['id']}" if n.get("id") is not None else "")]
    if n.get("active") is False:
        bits.append("(inactive)")
    if n.get("components"):
        bits.append("[" + ", ".join(n["components"]) + "]")
    if n.get("more"):
        bits.append(f"(+{n['more']} children not shown)")
    return " ".join(bits)


def fmt_tree(nodes: list, indent: int = 0, out: list | None = None) -> list:
    out = [] if out is None else out
    for n in nodes:
        out.append(_node_line(n, indent))
        fmt_tree(n.get("children", []), indent + 1, out)
    return out


def fmt_hierarchy(res: dict) -> str:
    lines = []
    hint = "Refer to objects by path (Parent/Child) or #id."
    if "scenes" in res:
        for s in res["scenes"]:
            if s.get("prefab_mode"):
                lines.append(f"Prefab Mode: {s.get('path')}" + (" (unsaved changes)" if s.get("dirty") else ""))
                hint += f" In Prefab Mode: {s.get('path')}/Child or #id."
            else:
                head = f"Scene {s.get('name') or 'Untitled'} ({s.get('path') or 'never saved'}"
                lines.append(head + (", unsaved changes)" if s.get("dirty") else ")"))
            fmt_tree(s.get("roots", []), 1, lines)
            if not s.get("roots"):
                lines.append("  (empty)")
    else:
        for r in res.get("roots", []):
            lines.append(f"Under {r.get('path')}:")
            fmt_tree([r], 1, lines)
        if res.get("asset") and res.get("roots"):
            hint = f"Inside a prefab asset, refer to objects by path, e.g. {res['roots'][0].get('path')}/Child."
    if res.get("truncated"):
        lines.append("(Cut short: use under=<path> or a smaller depth to see the rest.)")
    lines.append(hint)
    return "\n".join(lines)


def fmt_props(props: list, indent: str = "  ") -> list:
    return [f"{indent}{p['path']} = {p['value']}" + (f"  ({p['type']})" if p.get("type") and p["type"] not in
                                                    ("Vector3", "") else "") for p in props]


def fmt_inspect(res: dict) -> str:
    if "components" not in res:   # an asset
        lines = [f"{res['path']} ({res.get('type')})"]
        if res.get("shader"):
            lines.append(f"Shader: {res['shader']}")
        if res.get("class"):
            lines.append(f"Class: {res['class']}")
        lines += fmt_props(res.get("props", []))
        return "\n".join(lines)
    head = res["path"] + (f" #{res['id']}" if res.get("id") is not None else "")
    head += f": tag {res.get('tag')}, layer {res.get('layer')}"
    head += "" if res.get("active", True) else ", inactive"
    head += ", static" if res.get("static") else ""
    lines = [head]
    if res.get("prefab"):
        lines.append(f"Prefab instance of {res['prefab']}")
    for c in res["components"]:
        state = "" if c.get("enabled", True) else " (disabled)"
        lines.append(f"{c['type']}{state}:")
        if c.get("materials"):
            lines.append("  materials = " + ", ".join(c["materials"]))
        lines += fmt_props(c.get("props", []))
    lines.append("Change values with unity_modify(set={\"Component.property\": value}).")
    return "\n".join(lines)


def fmt_notes(res: dict) -> str:
    notes = res.get("notes") or []
    return ("\n" + "\n".join(f"- {n}" for n in notes)) if notes else ""


def fmt_compile(res: dict) -> str:
    msgs = res.get("messages") or []
    errors = [m for m in msgs if m.get("type") == "error"]
    warnings = [m for m in msgs if m.get("type") != "error"]
    n_err = max(len(errors), int(res.get("error_count") or 0))
    if not n_err:
        if res.get("compile_failed"):     # never "no errors" while Unity says the scripts don't compile
            return ("Unity says the scripts have compile errors but didn't say which (that compile ran before the "
                    "bridge was listening). Run unity_refresh again to recompile and list them.")
        text = "Compiled with no errors."
        if warnings:
            text += f" {len(warnings)} warning(s), e.g. " + "; ".join(
                f"{m['file']}:{m['line']} {m['message']}" for m in warnings[:3])
        return text
    lines = [f"{n_err} compile error(s). Fix them, then run unity_refresh again:"]
    lines += [f"{m['file']}({m['line']},{m['column']}): {m['message']}" for m in errors[:25]]
    if n_err > 25:
        lines.append(f"(and {n_err - 25} more)")
    if warnings:
        lines.append(f"(and {len(warnings)} warning(s))")
    return "\n".join(lines)


def fmt_console(res: dict) -> str:
    entries = res.get("entries", [])
    if not entries:
        return "No new console messages."
    lines = []
    if res.get("total", 0) > len(entries):
        lines.append(f"(showing the last {len(entries)} of {res['total']})")
    for e in entries:
        lines.append(f"[{e['type']}] {e['msg']}")
        if e.get("stack"):
            lines.append("    " + e["stack"].strip().replace("\n", "\n    "))
    return truncate("\n".join(lines), 12000)


ERROR_TYPES = ("error", "exception")


def fmt_play_log(res: dict, recent: int = 15) -> str:
    """Errors first, each repeated message once with a count, then the latest other messages."""
    entries = res.get("entries", [])
    if not entries:
        return "No console messages."
    groups: dict = {}
    for e in entries:
        g = groups.setdefault((e.get("type"), e.get("msg")), {"n": 0, "last": 0, "e": e})
        g["n"] += 1
        g["last"] = e.get("id", 0)
    lines: list[str] = []

    def show(g):
        e = g["e"]
        lines.append(f"[{e.get('type')}] {e.get('msg')}" + (f"  (x{g['n']})" if g["n"] > 1 else ""))
        if e.get("stack") and e.get("type") in ERROR_TYPES:
            lines.extend("    " + s for s in e["stack"].strip().splitlines()[:4])

    errs = [g for k, g in groups.items() if k[0] in ERROR_TYPES]
    other = sorted((g for k, g in groups.items() if k[0] not in ERROR_TYPES), key=lambda g: g["last"])
    if errs:
        lines.append(f"Errors ({sum(g['n'] for g in errs)}):")
        for g in errs[:20]:
            show(g)
        if len(errs) > 20:
            lines.append(f"(and {len(errs) - 20} more different errors)")
    else:
        lines.append("No errors.")
    if other:
        lines.append("Other messages, latest last" + (f" ({len(other) - recent} older ones not shown)"
                                                       if len(other) > recent else "") + ":")
        for g in other[-recent:]:
            show(g)
    if res.get("total", 0) > len(entries):
        lines.append(f"(Counts cover the last {len(entries)} of {res['total']} messages.)")
    return truncate("\n".join(lines), 12000)


def fmt_frames(start: dict, end: dict) -> str:
    """How far the game got; warns when it barely moved (results then say nothing about the game)."""
    if not isinstance(start.get("frame"), (int, float)) or not isinstance(end.get("frame"), (int, float)):
        return ""
    frames = int(end["frame"] - start["frame"])
    game_time = float(end.get("time") or 0) - float(start.get("time") or 0)
    if frames < 2:
        return (f"Warning: the game hardly ran ({frames} frames), so this doesn't show how it plays. Unity may be "
                "paused (unity_play action=resume) or slowed down in the background (Unity Preferences > General > "
                "Interaction Mode > No Throttling). Click the Unity window, then try again.\n")
    return f"The game ran {frames} frames ({game_time:.1f} s of game time).\n"


# ---------------------------------------------------------------------- tools
@tool("unity_status", """
Check the connection to the Unity Editor: version, play mode, open scenes, selection, compile
errors. Call it first. If several projects are open, pass project to pick one.""",
      {"project": {"type": "string", "description": "Unity project folder to work with"}},
      group="unity")
def unity_status(ctx: ToolContext, a: dict):
    want = None
    if a.get("project"):
        p = ctx.resolve(a["project"])
        if not is_unity_project(p):
            raise ToolError(f"{p} isn't a Unity project (it needs Assets and ProjectSettings folders).")
        want = str(p)
    try:
        st = call(ctx, "ping", timeout=15, wait_reload=8, want=want)
    except BridgeDown as e:
        return str(e)
    if want:
        _state(ctx)["project"] = st.get("project") or want    # only now that it answered
    _state(ctx).setdefault("log_seen", 0)
    _enable_tools(ctx)
    return fmt_status(st)


@tool("unity_install_bridge", """
Install or update the editor script the Unity tools need, in <project>/Assets/Editor/LocalAgent/.""",
      {"project": {"type": "string", "description": "The Unity project folder"}},
      required=["project"], risk=EDIT, group="unity",
      describe=lambda a: f"Add the Local Agent editor script to the Unity project {a.get('project')}")
def unity_install_bridge(ctx: ToolContext, a: dict):
    p = ctx.resolve(a["project"])
    if not is_unity_project(p):
        raise ToolError(f"{p} isn't a Unity project (it needs Assets and ProjectSettings folders).")
    dest = p / BRIDGE_DEST
    same = all((dest / f).exists() and (dest / f).read_bytes() == (BRIDGE_DIR / f).read_bytes() for f in BRIDGE_FILES)
    if same:
        verb = "already installed"
    else:
        dest.mkdir(parents=True, exist_ok=True)
        for f in BRIDGE_FILES:
            shutil.copyfile(BRIDGE_DIR / f, dest / f)
        verb = "installed"
    start = time.monotonic()
    deadline = start + (INSTALL_WAIT if verb == "installed" else 3)
    old = None
    while time.monotonic() < deadline and not ctx.stop.is_set():
        try:
            st = call(ctx, "ping", timeout=10, wait_reload=0, want=str(p))
        except BridgeDown:
            time.sleep(POLL)
            continue
        if not _outdated(st):
            _state(ctx)["project"] = st.get("project") or str(p)
            _enable_tools(ctx)
            return f"Bridge {verb} in {dest}.\n" + fmt_status(st)
        if old is None:          # the old bridge still runs: have Unity import the new one now
            old = st
            deadline = max(deadline, start + INSTALL_WAIT)
            try:
                call(ctx, "refresh", timeout=60, wait_reload=0, want=str(p))
            except ToolError:
                pass
        time.sleep(POLL)
    if old is not None:
        return (f"Bridge {verb} in {dest}, but Unity is still running the old one (version {old.get('bridge')}). "
                "Click the Unity window so it imports the new script, then call unity_status.")
    return (f"Bridge {verb} in {dest}, but Unity hasn't loaded it yet. Unity loads it when it imports the new "
            "script: the project must be open in Unity, and clicking the Unity window makes it look. Unity won't "
            "load new editor code while any script in the project has compile errors, so those must be fixed first "
            "(Unity's Console lists them). Then call unity_status.")


@tool("unity_hierarchy", """
See the objects in the open scenes as a tree with #ids. under: look inside one object or a prefab
asset (Assets/...prefab). components=true lists each object's components.""",
      {"under": {"type": "string", "description": "Path or #id of an object, or a prefab asset path"},
       "scene": {"type": "string", "description": "Only this open scene"},
       "depth": {"type": "integer", "description": "Levels to show. Default 3"},
       "components": {"type": "boolean"}},
      group="unity")
def unity_hierarchy(ctx: ToolContext, a: dict):
    res = call(ctx, "hierarchy", **_arg(a, "under", "scene", "depth", "components"))
    return truncate(fmt_hierarchy(res), 16000)


@tool("unity_inspect", """
See an object's components and their values (like the Inspector), or an asset's settings
(material, ScriptableObject, prefab: give its Assets/... path).""",
      {"path": {"type": "string", "description": "Object path like Player/Camera, #id, or an asset path"},
       "component": {"type": "string", "description": "Only this component, e.g. Rigidbody or BoxCollider[1]"}},
      required=["path"], group="unity")
def unity_inspect(ctx: ToolContext, a: dict):
    return truncate(fmt_inspect(call(ctx, "inspect", **_arg(a, "path", "component"))), 16000)


@tool("unity_find", """
Find objects in the open scenes by part of their name, a component type, or a tag.""",
      {"name": {"type": "string"}, "component": {"type": "string", "description": "e.g. Light, Rigidbody, PlayerController"},
       "tag": {"type": "string"}},
      group="unity")
def unity_find(ctx: ToolContext, a: dict):
    if not _arg(a, "name", "component", "tag"):
        raise ToolError("Give a name, component or tag to look for.")
    res = call(ctx, "find", **_arg(a, "name", "component", "tag"))
    objs = res.get("objects", [])
    if not objs:
        return "No matching objects."
    lines = [f"{o['path']} #{o['id']}" + ("" if o.get("active", True) else " (inactive)") for o in objs]
    if res.get("total", 0) > len(objs):
        lines.append(f"(and {res['total'] - len(objs)} more)")
    return "\n".join(lines)


@tool("unity_assets", """
Search the project's assets. filter uses Unity's syntax: name words and t:Type, e.g.
"t:Prefab enemy", "t:Material", "t:Scene", "t:Script player".""",
      {"filter": {"type": "string"}, "folder": {"type": "string", "description": "e.g. Assets/Prefabs"}},
      group="unity")
def unity_assets(ctx: ToolContext, a: dict):
    res = call(ctx, "assets", **_arg(a, "filter", "folder"))
    items = res.get("assets", [])
    if not items:
        return "No matching assets."
    lines = [f"{x['path']} ({x['type']})" for x in items]
    if res.get("total", 0) > len(items):
        lines.append(f"(and {res['total'] - len(items)} more: narrow the filter)")
    return "\n".join(lines)


VEC = {"type": "array", "items": {"type": "number"}, "description": "[x, y, z]"}
SET = {"type": "object", "description": 'Component values, e.g. {"Rigidbody.mass": 2, "Light.color": "#ffcc88", '
                                        '"MeshRenderer.materials": ["Assets/Mats/Red.mat"]}. "BoxCollider[1].size" '
                                        "is the second BoxCollider. References: an object or asset path."}


@tool("unity_create", """
Add an object: empty, a primitive, or a copy of a prefab, with optional components and values.
It goes in the open scene, inside parent (which can be Assets/...prefab), or in Prefab Mode into
the open prefab.""",
      {"name": {"type": "string"},
       "primitive": {"type": "string", "enum": ["empty", "cube", "sphere", "capsule", "cylinder", "plane", "quad"]},
       "prefab": {"type": "string", "description": "Prefab asset to place, e.g. Assets/Prefabs/Enemy.prefab"},
       "parent": {"type": "string", "description": "Path or #id of the parent"},
       "position": VEC, "rotation": {**VEC, "description": "Euler angles in degrees"},
       "scale": {"type": "array", "items": {"type": "number"}},
       "components": {"type": "array", "items": {"type": "string"}, "description": "e.g. [\"Rigidbody\", \"BoxCollider\"]"},
       "set": SET},
      risk=EDIT, group="unity",
      describe=lambda a: f"Add '{a.get('name') or a.get('prefab') or a.get('primitive') or 'object'}' to the Unity scene")
def unity_create(ctx: ToolContext, a: dict):
    res = call(ctx, "create", **_arg(a, "name", "primitive", "prefab", "parent", "position", "rotation", "scale",
                                     "components", "set"))
    shown = res["path"] + (f" #{res['id']}" if res.get("id") is not None else "")
    return f"Created {shown}." + fmt_notes(res)


@tool("unity_modify", """
Change an object (in a scene or inside a prefab asset) or a material/ScriptableObject asset:
name, position, rotation, scale, parent, active, tag, layer, components and their values.
Positions are local to the parent unless world=true.""",
      {"path": {"type": "string", "description": "Object path or #id, or an asset path"},
       "name": {"type": "string"}, "active": {"type": "boolean"},
       "parent": {"type": "string", "description": "New parent path; \"/\" for the scene root"},
       "position": VEC, "rotation": {**VEC, "description": "Euler angles in degrees"},
       "scale": {"type": "array", "items": {"type": "number"}}, "world": {"type": "boolean"},
       "tag": {"type": "string"}, "layer": {"type": "string"},
       "add_components": {"type": "array", "items": {"type": "string"}},
       "remove_components": {"type": "array", "items": {"type": "string"}},
       "set": SET,
       "in_play_mode": {"type": "boolean", "description": "Allow changes in play mode (lost when it stops)"}},
      required=["path"], risk=EDIT, group="unity",
      describe=lambda a: f"Change {a.get('path')} in Unity")
def unity_modify(ctx: ToolContext, a: dict):
    res = call(ctx, "modify", **_arg(a, "path", "name", "active", "parent", "position", "rotation", "scale", "world",
                                     "tag", "layer", "add_components", "remove_components", "set", "in_play_mode"))
    return f"Changed {res['path']}." + fmt_notes(res)


@tool("unity_delete", """
Delete objects from the scene or from inside a prefab asset. unity_undo brings back scene
deletes, but not deletes inside prefab assets.""",
      {"paths": {"type": "array", "items": {"type": "string"}, "description": "Object paths or #ids"}},
      required=["paths"], risk=EDIT, group="unity",
      describe=lambda a: "Delete from the Unity scene: " + ", ".join(map(str, a.get("paths") or [])))
def unity_delete(ctx: ToolContext, a: dict):
    paths = a.get("paths") or []
    if isinstance(paths, str):
        paths = [paths]
    res = call(ctx, "delete", paths=paths)
    gone = res.get("deleted") or []
    return (("Deleted " + ", ".join(gone) + ".") if gone else "Nothing was deleted.") + fmt_notes(res)


@tool("unity_create_asset", """
Make an asset: a material, folder, ScriptableObject, physics material, or a prefab from a scene
object (kind=prefab, from_object). For C# scripts use write_file, then unity_refresh.""",
      {"kind": {"type": "string", "enum": ["material", "folder", "scriptable_object", "physics_material", "prefab"]},
       "path": {"type": "string", "description": "e.g. Assets/Materials/Red.mat or Assets/Prefabs/Enemy.prefab"},
       "shader": {"type": "string", "description": "Material shader. Default: the project's lit shader"},
       "type": {"type": "string", "description": "scriptable_object: the class name"},
       "from_object": {"type": "string", "description": "prefab: the scene object to save"},
       "overwrite": {"type": "boolean", "description": "prefab: replace an existing prefab at path"},
       "set": {"type": "object", "description": 'Initial values, e.g. {"_BaseColor": "#ff0000"}'}},
      required=["kind", "path"], risk=EDIT, group="unity",
      describe=lambda a: f"{'Replace' if a.get('overwrite') else 'Create'} the Unity asset {a.get('path')}")
def unity_create_asset(ctx: ToolContext, a: dict):
    if a.get("kind") == "prefab":
        if not a.get("from_object"):
            raise ToolError("Give from_object: the scene object to turn into a prefab.")
        res = call(ctx, "save_prefab", path=a["from_object"], asset_path=a["path"],
                   overwrite=True if a.get("overwrite") is True else None)
        return f"Saved {res['path']} as the prefab {res['asset']}; the scene object is now linked to it."
    res = call(ctx, "create_asset", **_arg(a, "kind", "path", "shader", "type", "set"))
    return f"Created {res['path']}." + fmt_notes(res)


def _wait_reloaded(ctx: ToolContext, loads: int, seconds: float = 30) -> None:
    """After a clean compile Unity reloads its scripts; new components exist only after that."""
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline and not ctx.stop.is_set():
        st = _ping(ctx)
        if st is not None and st.get("domain_loads", loads) > loads:
            return
        time.sleep(POLL)


@tool("unity_refresh", """
Make Unity import changed files and compile scripts, wait, and get the compile errors. Run it
after writing or editing any .cs file, and fix every error before going on.""",
      {"timeout": {"type": "integer", "description": "Seconds to wait. Default 300"}},
      risk=RUN, group="unity", describe=lambda a: "Make Unity import changes and compile scripts")
def unity_refresh(ctx: ToolContext, a: dict):
    before = call(ctx, "ping", timeout=15)
    c0 = before.get("compile_count", 0)
    call(ctx, "refresh", timeout=600)
    limit = _num(a, "timeout", 300)
    deadline = time.monotonic() + limit
    idle = 0        # looks in a row at an idle Unity
    st: dict = {}
    while True:
        if time.monotonic() >= deadline or ctx.stop.is_set():
            return f"Unity is still importing or compiling after {limit:g} s. Call unity_refresh again in a bit."
        time.sleep(POLL)
        st = _ping(ctx) or {}
        if not st or st.get("compiling") or st.get("updating"):
            idle = 0                 # busy, reloading or compiling
            continue
        idle += 1
        if st.get("compile_count", 0) > c0 or idle >= 3:     # compiled, or nothing to compile
            break
    res = call(ctx, "compile")
    text = fmt_compile(res)
    if res.get("compile_count", 0) <= c0:
        return "No scripts needed compiling. " + text
    clean = not res.get("compile_failed") and not res.get("error_count") and \
        not any(m.get("type") == "error" for m in res.get("messages") or [])
    if clean and before.get("domain_loads") is not None and not st.get("playing"):
        _wait_reloaded(ctx, before["domain_loads"])
    return text


@tool("unity_console", """
Read Unity's console: logs, warnings and errors with where they happened. By default only
messages new since you last looked.""",
      {"level": {"type": "string", "enum": ["all", "warnings", "errors"], "description": "Default all"},
       "all_history": {"type": "boolean", "description": "Include messages you've already seen"},
       "stack": {"type": "boolean", "description": "Full stack traces"}},
      group="unity")
def unity_console(ctx: ToolContext, a: dict):
    st = _state(ctx)
    since = 0 if a.get("all_history") else st.get("log_seen", 0)
    res = call(ctx, "console", since=since, level=a.get("level") or "all", stack=bool(a.get("stack")))
    if res.get("last_id", 0) < since:      # Unity restarted: ids start again
        res = call(ctx, "console", since=0, level=a.get("level") or "all", stack=bool(a.get("stack")))
    st["log_seen"] = res.get("last_id", 0)
    return fmt_console(res)


def _wait_play(ctx: ToolContext, playing: bool, seconds: float = 90) -> dict:
    """Wait until Unity is (or isn't) playing. Marks refused when Unity dropped the request to play."""
    deadline = time.monotonic() + seconds
    st: dict = {}
    while time.monotonic() < deadline and not ctx.stop.is_set():
        st = _ping(ctx) or st
        if st and not st.get("compiling"):
            if bool(st.get("playing")) == playing:
                return st
            if playing and st.get("will_play") is False:
                return dict(st, refused=True)    # Unity decided not to (compile errors, usually)
        time.sleep(POLL)
    return st


def _shot(ctx: ToolContext, **args) -> tuple[bytes, str]:
    res = call(ctx, "screenshot", timeout=60, **args)
    return base64.b64decode(res["png"]), res.get("note", "")


def _play_log(ctx: ToolContext, since: int) -> dict:
    """Console messages since the play started: every error, and the latest of the rest."""
    errs = call(ctx, "console", since=since, level="errors", limit=60)
    rest = call(ctx, "console", since=since, level="all", limit=150)
    merged = {e["id"]: e for e in errs.get("entries", []) + rest.get("entries", [])}
    return {"entries": [merged[k] for k in sorted(merged)], "total": rest.get("total", len(merged)),
            "last_id": max(errs.get("last_id", 0), rest.get("last_id", 0))}


@tool("unity_play", """
Play mode: play, stop, pause, resume or step. play with seconds runs the game that long, then
reports console errors and logs with a game view picture, and stops unless keep_playing=true.""",
      {"action": {"type": "string", "enum": ["play", "stop", "pause", "resume", "step"]},
       "seconds": {"type": "number", "description": "play only: run this long, then report"},
       "keep_playing": {"type": "boolean"}},
      required=["action"], risk=RUN, group="unity",
      describe=lambda a: f"Unity play mode: {a.get('action')}" + (f" for {a['seconds']} s" if a.get("seconds") else ""))
def unity_play(ctx: ToolContext, a: dict):
    action = a.get("action") or "play"
    res = call(ctx, "play", action=action)
    if action == "play":
        st = _wait_play(ctx, True)
        if not st.get("playing"):
            if st.get("refused") or st.get("compile_failed") or st.get("compile_errors"):
                return ("Unity refused to enter play mode, most likely because scripts have compile errors. "
                        "Run unity_refresh to see them, and fix them.")
            return "Unity didn't enter play mode within 90 s. Check unity_status and unity_console."
        secs = _num(a, "seconds", 0)
        if secs <= 0:
            return "Playing. Use unity_console to read the logs and unity_play(action=stop) to stop."
        end = time.monotonic() + min(secs, 300)
        while time.monotonic() < end and not ctx.stop.is_set():
            time.sleep(max(0.0, min(0.25, end - time.monotonic())))
        now = _ping(ctx) or {}
        log = _play_log(ctx, res.get("last_log_id", 0))
        _state(ctx)["log_seen"] = log.get("last_id", 0)
        images, note = [], ""
        try:
            png, note = _shot(ctx, view="game")
            images.append(png)
        except ToolError as e:
            note = f"(no game view picture: {e})"
        stopped = ""
        if not a.get("keep_playing"):
            call(ctx, "play", action="stop")
            _wait_play(ctx, False, 30)
            stopped = " Play mode stopped again."
        text = f"Played for {secs:g} s.{stopped}\n{fmt_frames(st, now)}Console while playing:\n{fmt_play_log(log)}"
        if images:
            text += f"\nThe game view at the end is attached. {note}"
        elif note:
            text += f"\n{note}"
        return ToolResult(text, images=images)
    if action == "stop":
        _wait_play(ctx, False, 30)
        return "Stopped play mode."
    return f"Done: {action}. Playing: {res.get('playing')}, paused: {res.get('paused')}."


@tool("unity_screenshot", """
A picture of the Scene view (optionally framed on an object, from an angle) or from the game's
camera(s).""",
      {"view": {"type": "string", "enum": ["scene", "game"], "description": "Default scene"},
       "look_at": {"type": "string", "description": "Scene view: object path or #id to frame"},
       "angle": {"type": "string", "enum": ["current", "iso", "top", "front", "back", "left", "right"]},
       "width": {"type": "integer"}, "height": {"type": "integer"}},
      group="unity")
def unity_screenshot(ctx: ToolContext, a: dict):
    png, note = _shot(ctx, **_arg(a, "view", "look_at", "angle", "width", "height"))
    return ToolResult(f"{note} Picture attached.".strip(), images=[png])


@tool("unity_scene", """
Scenes: save the open scene (path for a new one), open or make one, list them, or add one to the
build.""",
      {"action": {"type": "string", "enum": ["save", "open", "new", "list", "add_to_build"]},
       "path": {"type": "string", "description": "e.g. Assets/Scenes/Level2.unity"},
       "additive": {"type": "boolean", "description": "open: keep the current scene open too"},
       "discard": {"type": "boolean", "description": "open/new: throw away unsaved changes (only if the user agreed)"}},
      required=["action"], risk=EDIT, group="unity",
      risk_for=lambda ctx, a: SAFE if a.get("action") == "list" else EDIT,
      describe=lambda a: f"Unity scene: {a.get('action')} {a.get('path') or ''}".strip())
def unity_scene(ctx: ToolContext, a: dict):
    res = call(ctx, "scene", **_arg(a, "action", "path", "additive", "discard"))
    if a.get("action") == "list":
        return ("Scenes in the build: " + (", ".join(res.get("build_scenes", [])) or "none") +
                "\nAll scenes: " + (", ".join(res.get("all_scenes", [])) or "none"))
    return "Done: " + ", ".join(f"{k} {v}" for k, v in res.items())


@tool("unity_menu", """
Run a Unity menu command by its path, e.g. "GameObject/Light/Directional Light", or a [MenuItem]
from the project's own editor scripts.""",
      {"item": {"type": "string"}},
      required=["item"], risk=RUN, group="unity",
      describe=lambda a: f"Run the Unity menu command '{a.get('item')}'")
def unity_menu(ctx: ToolContext, a: dict):
    call(ctx, "menu", item=a["item"], timeout=300)
    return f"Ran {a['item']}."


@tool("unity_undo", """
Undo (or redo) your changes in Unity, like Ctrl+Z. Each of your Unity tool calls is one step.""",
      {"steps": {"type": "integer", "description": "Default 1"}, "redo": {"type": "boolean"}},
      risk=EDIT, group="unity", describe=lambda a: ("Redo" if a.get("redo") else "Undo") + " in Unity")
def unity_undo(ctx: ToolContext, a: dict):
    res = call(ctx, "undo", **_arg(a, "steps", "redo"))
    return ", ".join(f"{k} {v} step(s)" for k, v in res.items()) + "."
