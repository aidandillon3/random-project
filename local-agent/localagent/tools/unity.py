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
POLL = 1.0            # seconds between checks while Unity compiles or enters play mode


def discovery_dir() -> Path:
    return Path(os.environ.get("LOCALAGENT_UNITY_DIR") or Path(tempfile.gettempdir()) / "localagent-unity")


def _same(a: str, b: str) -> bool:
    return os.path.normcase(os.path.normpath(str(a))) == os.path.normcase(os.path.normpath(str(b)))


def is_unity_project(p: Path) -> bool:
    return (p / "Assets").is_dir() and (p / "ProjectSettings").is_dir()


def project_of(path: Path) -> Path | None:
    """The Unity project that contains path, if any."""
    for p in [path, *path.parents]:
        if is_unity_project(p):
            return p
    return None


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


def bridges(extra_projects=()) -> list[dict]:
    """Every bridge that has announced itself (they may not all be running)."""
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
    return found


class BridgeDown(ToolError):
    pass


class NoBridge(BridgeDown):
    """No Unity has announced a bridge at all: nothing to wait for."""


def _send(info: dict, payload: dict, timeout: float) -> dict:
    req = dict(payload, token=info["token"], timeout=max(5, int(timeout) - 5))
    try:
        with socket.create_connection(("127.0.0.1", int(info["port"])), timeout=5) as s:
            s.settimeout(timeout)
            s.sendall(json.dumps(req).encode("utf-8") + b"\n")
            buf = bytearray()
            while not buf.endswith(b"\n"):
                chunk = s.recv(1 << 16)
                if not chunk:
                    break
                buf += chunk
    except (ConnectionError, socket.timeout, OSError) as e:
        raise BridgeDown(f"Unity isn't answering ({e.__class__.__name__}).")
    if not buf:
        raise BridgeDown("Unity closed the connection (it may be reloading scripts).")
    return json.loads(buf.decode("utf-8"))


def _candidates(ctx: ToolContext) -> list[str]:
    st = _state(ctx)
    out = [st.get("project"), getattr(ctx.settings, "unity_project", "")]
    p = project_of(ctx.workspace)
    if p:
        out.append(str(p))
    return [x for x in out if x]


def _state(ctx: ToolContext) -> dict:
    return ctx.unity


def connect(ctx: ToolContext) -> dict:
    """Pick the bridge to talk to: the chosen project, else the only Unity that answers."""
    wanted = _candidates(ctx)
    found = bridges(wanted)
    chosen = _state(ctx).get("project") or getattr(ctx.settings, "unity_project", "")
    if chosen:
        match = [b for b in found if _same(b["project"], chosen)]
        if not match:
            raise BridgeDown(f"The Unity project {chosen} isn't open with the bridge running. Open it in Unity, "
                             "or install the bridge there with unity_install_bridge.")
        return match[0]
    alive = []
    for b in found:
        try:
            _send(b, {"cmd": "ping"}, 5)
            alive.append(b)
        except (BridgeDown, ValueError):
            continue
    ws = project_of(ctx.workspace)
    pick = alive[0] if len(alive) == 1 else next((b for b in alive if ws and _same(b["project"], ws)), None)
    if pick:
        _state(ctx)["project"] = pick["project"]     # stick with it, so later calls skip the search
        return pick
    if len(alive) > 1:
        raise ToolError("Several Unity projects are open: " + "; ".join(b["project"] for b in alive) +
                        ". Pick one with unity_status(project=...).")
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


def call(ctx: ToolContext, cmd: str, timeout: float = 60, wait_reload: float = 30, **args) -> dict:
    """Run one bridge command. While Unity reloads scripts the bridge is briefly gone: keep trying."""
    deadline = time.monotonic() + wait_reload
    payload = {"cmd": cmd, **{k: v for k, v in args.items() if v is not None}}
    while True:
        try:
            resp = _send(connect(ctx), payload, timeout)
            break
        except BridgeDown as e:
            if isinstance(e, NoBridge) or time.monotonic() >= deadline or ctx.stop.is_set():
                raise
            time.sleep(min(POLL, 1.0))
    if not resp.get("ok"):
        raise ToolError(resp.get("error") or "Unity said no.")
    return resp.get("result") or {}


def _arg(a: dict, *keys):
    return {k: a[k] for k in keys if k in a and a[k] not in (None, "")}


# ---------------------------------------------------------------------- formatting
def fmt_status(st: dict) -> str:
    lines = [f"Connected to Unity {st.get('unity')} with the project {st.get('project')} "
             f"({st.get('render_pipeline')} render pipeline)."]
    mode = "playing" + (" (paused)" if st.get("paused") else "") if st.get("playing") else "edit mode"
    lines.append(f"Mode: {mode}." + (" Compiling scripts now." if st.get("compiling") else ""))
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
    return "\n".join(lines)


def _node_line(n: dict, indent: int) -> str:
    bits = [f"{'  ' * indent}- {n['name']} #{n['id']}"]
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
    if "scenes" in res:
        for s in res["scenes"]:
            head = f"Scene {s.get('name') or 'Untitled'} ({s.get('path') or 'never saved'}"
            head += ", unsaved changes)" if s.get("dirty") else ")"
            lines.append(head)
            fmt_tree(s.get("roots", []), 1, lines)
            if not s.get("roots"):
                lines.append("  (empty)")
    else:
        for r in res.get("roots", []):
            lines.append(f"Under {r.get('path')}:")
            fmt_tree([r], 1, lines)
    if res.get("truncated"):
        lines.append("(Cut short: use under=<path> or a smaller depth to see the rest.)")
    lines.append("Refer to objects by path (Parent/Child) or #id.")
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
    head = f"{res['path']} #{res['id']}: tag {res.get('tag')}, layer {res.get('layer')}"
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


def fmt_compile(msgs: list) -> str:
    errors = [m for m in msgs if m.get("type") == "error"]
    warnings = [m for m in msgs if m.get("type") != "error"]
    if not errors:
        text = "Compiled with no errors."
        if warnings:
            text += f" {len(warnings)} warning(s), e.g. " + "; ".join(
                f"{m['file']}:{m['line']} {m['message']}" for m in warnings[:3])
        return text
    lines = [f"{len(errors)} compile error(s). Fix them, then run unity_refresh again:"]
    lines += [f"{m['file']}({m['line']},{m['column']}): {m['message']}" for m in errors[:25]]
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


# ---------------------------------------------------------------------- tools
@tool("unity_status", """
Check the connection to the Unity Editor: Unity version, project, play mode, open scenes,
what's selected, compile errors. Use it first for any Unity work. If several projects are
open, pass project to pick one.""",
      {"project": {"type": "string", "description": "Path of the Unity project folder to work with"}},
      group="unity")
def unity_status(ctx: ToolContext, a: dict):
    if a.get("project"):
        p = Path(a["project"]).expanduser()
        if not is_unity_project(p):
            raise ToolError(f"{p} isn't a Unity project (it needs Assets and ProjectSettings folders).")
        _state(ctx)["project"] = str(p)
    try:
        st = call(ctx, "ping", timeout=15, wait_reload=8)
    except BridgeDown as e:
        return str(e)
    _state(ctx).setdefault("log_seen", 0)
    return fmt_status(st)


@tool("unity_install_bridge", """
Install (or update) the small editor script that lets you work in a Unity project. It goes in
Assets/Editor/LocalAgent/. Unity loads it when its window is focused next.""",
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
    _state(ctx)["project"] = str(p)
    deadline = time.monotonic() + (20 if verb == "installed" else 3)
    while time.monotonic() < deadline and not ctx.stop.is_set():
        try:
            return f"Bridge {verb} in {dest}.\n" + fmt_status(call(ctx, "ping", timeout=10, wait_reload=0))
        except (BridgeDown, ToolError):
            time.sleep(POLL)
    return (f"Bridge {verb} in {dest}. Unity hasn't loaded it yet: it needs the project open and the Unity "
            "window clicked once so it imports the new script. Then call unity_status.")


@tool("unity_hierarchy", """
See the objects in the open scenes as a tree, with their #ids. Use under to look inside one
object (or a prefab asset, e.g. Assets/Prefabs/Enemy.prefab), and components=true to list
each object's components.""",
      {"under": {"type": "string", "description": "Path or #id of an object, or a prefab asset path"},
       "scene": {"type": "string", "description": "Only this open scene"},
       "depth": {"type": "integer", "description": "Levels to show. Default 3"},
       "components": {"type": "boolean"}},
      group="unity")
def unity_hierarchy(ctx: ToolContext, a: dict):
    res = call(ctx, "hierarchy", **_arg(a, "under", "scene", "depth", "components"))
    return truncate(fmt_hierarchy(res), 16000)


@tool("unity_inspect", """
See an object's components and all their settings (like Unity's Inspector), or an asset's
settings (material, ScriptableObject, prefab: give its Assets/... path).""",
      {"path": {"type": "string", "description": "Object path like Player/Camera, #id, or an asset path"},
       "component": {"type": "string", "description": "Only this component, e.g. Rigidbody"}},
      required=["path"], group="unity")
def unity_inspect(ctx: ToolContext, a: dict):
    return truncate(fmt_inspect(call(ctx, "inspect", **_arg(a, "path", "component"))), 16000)


@tool("unity_find", """
Find objects in the open scenes by part of their name, a component they have, or a tag.""",
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
Search the project's assets. filter uses Unity's search syntax: words in the name and t:Type,
e.g. "t:Prefab enemy", "t:Material", "t:Scene", "t:Texture2D grass", "t:Script player".""",
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
SET = {"type": "object", "description": 'Component settings, e.g. {"Rigidbody.mass": 2, "Light.color": "#ffcc88", '
                                        '"PlayerController.speed": 5, "MeshRenderer.materials": ["Assets/Mats/Red.mat"]}. '
                                        "References: an object path or an asset path."}


@tool("unity_create", """
Add an object to the open scene (or inside a prefab asset, with parent=Assets/...prefab): an
empty object, a primitive, or a copy of a prefab. Can add components and set their values.""",
      {"name": {"type": "string"},
       "primitive": {"type": "string", "enum": ["empty", "cube", "sphere", "capsule", "cylinder", "plane", "quad"]},
       "prefab": {"type": "string", "description": "Prefab asset to place, e.g. Assets/Prefabs/Enemy.prefab"},
       "parent": {"type": "string", "description": "Path or #id of the parent. Default: the scene root"},
       "position": VEC, "rotation": {**VEC, "description": "Euler angles in degrees"},
       "scale": {"type": "array", "items": {"type": "number"}},
       "components": {"type": "array", "items": {"type": "string"}, "description": "e.g. [\"Rigidbody\", \"BoxCollider\"]"},
       "set": SET},
      risk=EDIT, group="unity",
      describe=lambda a: f"Add '{a.get('name') or a.get('prefab') or a.get('primitive') or 'object'}' to the Unity scene")
def unity_create(ctx: ToolContext, a: dict):
    res = call(ctx, "create", **_arg(a, "name", "primitive", "prefab", "parent", "position", "rotation", "scale",
                                     "components", "set"))
    return f"Created {res['path']} #{res['id']}." + fmt_notes(res)


@tool("unity_modify", """
Change an object (or something inside a prefab asset, or a material/ScriptableObject asset):
rename, move, rotate, scale, re-parent, (de)activate, tag, layer, add or remove components, and
set component values. Positions are local to the parent unless world=true.""",
      {"path": {"type": "string", "description": "Object path or #id, or an asset path"},
       "name": {"type": "string"}, "active": {"type": "boolean"},
       "parent": {"type": "string", "description": "New parent path; \"/\" for the scene root"},
       "position": VEC, "rotation": {**VEC, "description": "Euler angles in degrees"},
       "scale": {"type": "array", "items": {"type": "number"}}, "world": {"type": "boolean"},
       "tag": {"type": "string"}, "layer": {"type": "string"},
       "add_components": {"type": "array", "items": {"type": "string"}},
       "remove_components": {"type": "array", "items": {"type": "string"}},
       "set": SET,
       "in_play_mode": {"type": "boolean", "description": "Allow changes during play mode (they're lost when it stops)"}},
      required=["path"], risk=EDIT, group="unity",
      describe=lambda a: f"Change {a.get('path')} in Unity")
def unity_modify(ctx: ToolContext, a: dict):
    res = call(ctx, "modify", **_arg(a, "path", "name", "active", "parent", "position", "rotation", "scale", "world",
                                     "tag", "layer", "add_components", "remove_components", "set", "in_play_mode"))
    return f"Changed {res['path']}." + fmt_notes(res)


@tool("unity_delete", """
Delete objects from the scene (or from inside a prefab asset). Unity's undo can bring them back.""",
      {"paths": {"type": "array", "items": {"type": "string"}, "description": "Object paths or #ids"}},
      required=["paths"], risk=EDIT, group="unity",
      describe=lambda a: "Delete from the Unity scene: " + ", ".join(map(str, a.get("paths") or [])))
def unity_delete(ctx: ToolContext, a: dict):
    paths = a.get("paths") or []
    if isinstance(paths, str):
        paths = [paths]
    res = call(ctx, "delete", paths=paths)
    return "Deleted " + ", ".join(res.get("deleted", [])) + "."


@tool("unity_create_asset", """
Make a new asset: a material, a folder, a ScriptableObject of one of the project's classes, a
physics material, or a prefab from a scene object (kind=prefab, from_object=its path). Write
C# scripts with write_file instead, then unity_refresh.""",
      {"kind": {"type": "string", "enum": ["material", "folder", "scriptable_object", "physics_material", "prefab"]},
       "path": {"type": "string", "description": "e.g. Assets/Materials/Red.mat or Assets/Prefabs/Enemy.prefab"},
       "shader": {"type": "string", "description": "Materials: shader name. Default: the project's default lit shader"},
       "type": {"type": "string", "description": "scriptable_object: the class name"},
       "from_object": {"type": "string", "description": "prefab: the scene object to save as a prefab"},
       "set": {"type": "object", "description": 'Initial values, e.g. {"_BaseColor": "#ff0000"} for a URP material'}},
      required=["kind", "path"], risk=EDIT, group="unity",
      describe=lambda a: f"Create the Unity asset {a.get('path')}")
def unity_create_asset(ctx: ToolContext, a: dict):
    if a.get("kind") == "prefab":
        if not a.get("from_object"):
            raise ToolError("Give from_object: the scene object to turn into a prefab.")
        res = call(ctx, "save_prefab", path=a["from_object"], asset_path=a["path"])
        return f"Saved {res['path']} as the prefab {res['asset']}; the scene object is now linked to it."
    res = call(ctx, "create_asset", **_arg(a, "kind", "path", "shader", "type", "set"))
    return f"Created {res['path']}." + fmt_notes(res)


@tool("unity_refresh", """
Make Unity import changed files and compile scripts, wait for it, and get the compile errors
back. Run it after writing or editing any .cs file, and fix every error before going on.""",
      {"timeout": {"type": "integer", "description": "Seconds to wait. Default 300"}},
      risk=RUN, group="unity", describe=lambda a: "Make Unity import changes and compile scripts")
def unity_refresh(ctx: ToolContext, a: dict):
    before = call(ctx, "ping", timeout=15)
    c0 = before.get("compile_count", 0)
    r = call(ctx, "refresh", timeout=600)
    busy = bool(r.get("compiling") or r.get("updating"))
    start = time.monotonic()
    deadline = start + int(a.get("timeout") or 300)
    finished = False
    while time.monotonic() < deadline and not ctx.stop.is_set():
        time.sleep(POLL)
        try:
            st = call(ctx, "ping", timeout=10, wait_reload=0)
        except BridgeDown:
            busy = True          # reloading scripts
            continue
        if st.get("compiling") or st.get("updating"):
            busy = True
            continue
        if st.get("compile_count", 0) > c0 or (not busy and time.monotonic() - start > 3 * POLL + 2):
            finished = True
            break
    if not finished:
        return "Unity is still busy importing or compiling. Call unity_refresh again in a bit."
    res = call(ctx, "compile")
    text = fmt_compile(res.get("messages", []))
    if res.get("compile_count", 0) == c0:
        text = "No scripts needed compiling. " + text
    return text


@tool("unity_console", """
Read Unity's console: logs, warnings and errors (with where they happened). By default only
messages that are new since you last looked.""",
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
    deadline = time.monotonic() + seconds
    st = {}
    while time.monotonic() < deadline and not ctx.stop.is_set():
        try:
            st = call(ctx, "ping", timeout=10, wait_reload=0)
            if bool(st.get("playing")) == playing and not st.get("compiling"):
                return st
        except BridgeDown:
            pass
        time.sleep(POLL)
    return st


def _shot(ctx: ToolContext, **args) -> tuple[bytes, str]:
    res = call(ctx, "screenshot", timeout=60, **args)
    return base64.b64decode(res["png"]), res.get("note", "")


@tool("unity_play", """
Control play mode: play, stop, pause, resume or step one frame. With play and seconds, it runs
the game that long, then brings back new console errors/logs and a picture of the game view,
and stops again (unless keep_playing=true) - a quick way to test a change.""",
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
            return "Unity didn't enter play mode. Check unity_console and unity_status."
        secs = float(a.get("seconds") or 0)
        if secs <= 0:
            return "Playing. Use unity_console to read the logs and unity_play(action=stop) to stop."
        end = time.monotonic() + min(secs, 300)
        while time.monotonic() < end and not ctx.stop.is_set():
            time.sleep(max(0.0, min(0.25, end - time.monotonic())))
        log = call(ctx, "console", since=res.get("last_log_id", 0), level="all", limit=40)
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
        text = f"Played for {secs:g} s.{stopped}\nConsole while playing:\n{fmt_console(log)}"
        if images:
            text += f"\nThe game view at the end is attached. {note}"
        return ToolResult(text, images=images)
    if action == "stop":
        _wait_play(ctx, False, 30)
        return "Stopped play mode."
    return f"Done: {action}. Playing: {res.get('playing')}, paused: {res.get('paused')}."


@tool("unity_screenshot", """
See the scene: a picture from the Scene view (optionally framed on one object, from an angle)
or from the game's camera(s).""",
      {"view": {"type": "string", "enum": ["scene", "game"], "description": "Default scene"},
       "look_at": {"type": "string", "description": "Scene view: object path or #id to frame"},
       "angle": {"type": "string", "enum": ["current", "iso", "top", "front", "back", "left", "right"]},
       "width": {"type": "integer"}, "height": {"type": "integer"}},
      group="unity")
def unity_screenshot(ctx: ToolContext, a: dict):
    png, note = _shot(ctx, **_arg(a, "view", "look_at", "angle", "width", "height"))
    return ToolResult(f"{note} Picture attached.".strip(), images=[png])


@tool("unity_scene", """
Scenes: save the open scene (give path for a new one), open another, make a new one, list
the project's scenes, or add a scene to the build.""",
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
Run a Unity menu command by its path, e.g. "GameObject/Light/Directional Light",
"Edit/Play", or a [MenuItem] from the project's own editor scripts.""",
      {"item": {"type": "string"}},
      required=["item"], risk=RUN, group="unity",
      describe=lambda a: f"Run the Unity menu command '{a.get('item')}'")
def unity_menu(ctx: ToolContext, a: dict):
    call(ctx, "menu", item=a["item"], timeout=300)
    return f"Ran {a['item']}."


@tool("unity_undo", """
Undo (or redo) your last changes in Unity, like Ctrl+Z.""",
      {"steps": {"type": "integer", "description": "Default 1"}, "redo": {"type": "boolean"}},
      risk=EDIT, group="unity", describe=lambda a: ("Redo" if a.get("redo") else "Undo") + " in Unity")
def unity_undo(ctx: ToolContext, a: dict):
    res = call(ctx, "undo", **_arg(a, "steps", "redo"))
    return ", ".join(f"{k} {v} step(s)" for k, v in res.items()) + "."
