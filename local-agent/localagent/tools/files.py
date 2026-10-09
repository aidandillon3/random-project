"""Reading, searching and editing files."""

from __future__ import annotations

import fnmatch
import io
import os
import re
import shutil
from pathlib import Path

from .base import EDIT, ToolContext, ToolError, ToolResult, tool

SKIP_DIRS = {".git", "node_modules", ".venv", "venv", "__pycache__", ".mypy_cache", ".idea",
             ".pytest_cache", "dist", "build", ".next", "$RECYCLE.BIN", "System Volume Information"}
IMAGE_EXT = {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp"}


def _edit_risk(ctx: ToolContext, args: dict) -> str:
    """Edits inside the work folder are 'edit'. Outside it they count as 'run', so the
    'auto-approve edits' mode still asks."""
    paths = [args.get(k) for k in ("path", "source", "destination") if args.get(k)]
    for p in paths:
        if not ctx.inside_workspace(ctx.resolve(p)):
            return "run"
    return EDIT


def _read_text(path: Path) -> str:
    data = path.read_bytes()
    if b"\x00" in data[:8000]:
        raise ToolError(f"{path} looks like a binary file, not text.")
    for enc in ("utf-8", "utf-16", "cp1252"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def _image_png(path: Path, max_side: int) -> bytes:
    from PIL import Image

    img = Image.open(path)
    img.thumbnail((max_side, max_side))
    buf = io.BytesIO()
    img.convert("RGB").save(buf, "PNG")
    return buf.getvalue()


@tool("read_file", """
Read a text file and get it back with line numbers. For big files, read a window with
offset (first line, 1-based) and limit (number of lines). Image files (png, jpg...) are
shown to you as a picture.""",
      {"path": {"type": "string", "description": "File path, absolute or relative to the work folder"},
       "offset": {"type": "integer", "description": "First line to read (1-based). Default 1"},
       "limit": {"type": "integer", "description": "How many lines. Default 400"}},
      required=["path"])
def read_file(ctx: ToolContext, a: dict):
    path = ctx.resolve(a["path"])
    if not path.exists():
        raise ToolError(f"No such file: {path}")
    if path.is_dir():
        raise ToolError(f"{path} is a folder. Use list_dir.")
    if path.suffix.lower() in IMAGE_EXT:
        return ToolResult(f"Image {path.name} attached.",
                          images=[_image_png(path, ctx.settings.screenshot_max_side)])
    lines = _read_text(path).splitlines()
    start = max(1, int(a.get("offset") or 1))
    limit = max(1, int(a.get("limit") or 400))
    chunk = lines[start - 1:start - 1 + limit]
    width = len(str(start + len(chunk)))
    body = "\n".join(f"{i:>{width}}| {line}" for i, line in enumerate(chunk, start))
    more = ""
    if start - 1 + limit < len(lines):
        more = f"\n[file has {len(lines)} lines; read more with offset={start + limit}]"
    return f"{path} (lines {start}-{start + len(chunk) - 1} of {len(lines)})\n{body}{more}"


@tool("write_file", """
Create a file or replace its whole content. Makes missing folders. For small changes to an
existing file prefer edit_file.""",
      {"path": {"type": "string"}, "content": {"type": "string"}},
      required=["path", "content"], risk=EDIT, risk_for=_edit_risk,
      describe=lambda a: f"Write {len(a.get('content', ''))} characters to {a.get('path')}")
def write_file(ctx: ToolContext, a: dict):
    path = ctx.resolve(a["path"])
    path.parent.mkdir(parents=True, exist_ok=True)
    existed = path.exists()
    with open(path, "w", encoding="utf-8", newline="") as f:
        f.write(a["content"])
    return f"{'Replaced' if existed else 'Created'} {path} ({len(a['content'])} characters)." + check_syntax(path)


def _loose(s: str) -> str:
    return "\n".join(line.rstrip() for line in s.replace("\r\n", "\n").split("\n"))


def _apply_edit(path: Path, text: str, old: str, new: str, replace_all: bool) -> tuple[str, str]:
    """One exact replacement on a file's text. Returns (new text, what happened)."""
    if not old:
        raise ToolError("old_text is empty. Use write_file to create a file.")
    crlf = "\r\n" in text
    count = text.count(old)
    if count == 0 and crlf:
        old2, new2 = old.replace("\r\n", "\n").replace("\n", "\r\n"), new.replace("\r\n", "\n").replace("\n", "\r\n")
        if text.count(old2):
            old, new, count = old2, new2, text.count(old2)
    if count == 0:
        # Forgive trailing-whitespace differences, the most common near miss.
        loose_text, loose_old = _loose(text), _loose(old)
        n = loose_text.count(loose_old)
        if n == 1 or (n > 1 and replace_all):
            text = loose_text.replace(loose_old, new.replace("\r\n", "\n"))
            if crlf:
                text = text.replace("\n", "\r\n")
            return text, f"{n} match, ignoring trailing spaces"
        hint = _closest(text, old)
        raise ToolError(f"old_text was not found in {path}. Read the file again and copy the text exactly."
                        + (f"\nClosest line in the file: {hint!r}" if hint else ""))
    if count > 1 and not replace_all:
        raise ToolError(f"old_text matches {count} places. Add surrounding lines so it is unique, "
                        "or set replace_all to true.")
    text = text.replace(old, new) if replace_all else text.replace(old, new, 1)
    return text, f"{count} replacement{'s' if count > 1 else ''}"


@tool("edit_file", """
Replace an exact piece of text in a file. old_text must match the file exactly (copy it from
read_file without the line-number prefix) and must be unique unless replace_all is true.
Include a few surrounding lines to make it unique.""",
      {"path": {"type": "string"},
       "old_text": {"type": "string", "description": "Exact text to find"},
       "new_text": {"type": "string", "description": "Text to put there instead"},
       "replace_all": {"type": "boolean", "description": "Replace every match. Default false"}},
      required=["path", "old_text", "new_text"], risk=EDIT, risk_for=_edit_risk,
      describe=lambda a: f"Edit {a.get('path')}")
def edit_file(ctx: ToolContext, a: dict):
    path = ctx.resolve(a["path"])
    if not path.is_file():
        raise ToolError(f"No such file: {path}")
    text, what = _apply_edit(path, _read_text(path), a["old_text"], a["new_text"], bool(a.get("replace_all")))
    path.write_text(text, encoding="utf-8", newline="")
    return f"Edited {path} ({what})." + check_syntax(path)


@tool("multi_edit", """
Make several exact replacements in one file at once, in order. Each edit works like edit_file
(old_text must match exactly and be unique unless replace_all). If any edit fails, none are
saved, so the file is never left half-changed.""",
      {"path": {"type": "string"},
       "edits": {"type": "array", "items": {"type": "object", "properties": {
           "old_text": {"type": "string"}, "new_text": {"type": "string"},
           "replace_all": {"type": "boolean"}}, "required": ["old_text", "new_text"]}}},
      required=["path", "edits"], risk=EDIT, risk_for=_edit_risk,
      describe=lambda a: f"Make {len(a.get('edits') or [])} edits in {a.get('path')}")
def multi_edit(ctx: ToolContext, a: dict):
    path = ctx.resolve(a["path"])
    if not path.is_file():
        raise ToolError(f"No such file: {path}")
    edits = a.get("edits") or []
    if not edits:
        raise ToolError("edits is empty.")
    text = _read_text(path)
    for i, e in enumerate(edits, 1):
        if not isinstance(e, dict):
            raise ToolError(f"Edit {i} must be an object with old_text and new_text.")
        try:
            text, _ = _apply_edit(path, text, e.get("old_text", ""), e.get("new_text", ""), bool(e.get("replace_all")))
        except ToolError as err:
            raise ToolError(f"Edit {i} of {len(edits)} failed, so nothing was saved: {err}")
    path.write_text(text, encoding="utf-8", newline="")
    return f"Made {len(edits)} edits in {path}." + check_syntax(path)


def check_syntax(path: Path) -> str:
    """A quick syntax check after an edit, so a broken file is noticed straight away instead
    of several steps later. Returns a warning to append, or ''."""
    ext = path.suffix.lower()
    try:
        if ext in (".py", ".pyw"):
            compile(path.read_bytes(), str(path), "exec", dont_inherit=True)
        elif ext == ".json":
            import json

            json.loads(path.read_text(encoding="utf-8-sig"))
        elif ext == ".toml":
            import tomllib

            tomllib.loads(path.read_text(encoding="utf-8"))
        elif ext in (".js", ".mjs", ".cjs"):
            node = shutil.which("node")
            if not node:
                return ""
            import subprocess
            import sys

            r = subprocess.run([node, "--check", str(path)], capture_output=True, text=True, timeout=20,
                               creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0)
            if r.returncode != 0:
                return "\nWarning, the file now has a syntax error:\n" + (r.stderr or r.stdout).strip()[-1500:]
        else:
            return ""
    except SyntaxError as e:
        return f"\nWarning, the file now has a syntax error at line {e.lineno}: {e.msg}. Fix it."
    except ValueError as e:  # json and toml errors
        return f"\nWarning, the file is no longer valid {ext[1:].upper()}: {e}. Fix it."
    except Exception:  # noqa: BLE001 - the check is a bonus, never a reason to fail the edit
        return ""
    return ""


def _closest(text: str, old: str) -> str:
    import difflib

    first = next((l for l in old.splitlines() if l.strip()), "")
    if not first:
        return ""
    cands = difflib.get_close_matches(first.strip(), [l.strip() for l in text.splitlines()], n=1, cutoff=0.6)
    return cands[0] if cands else ""


@tool("list_dir", "List a folder. depth 1 shows only its direct contents.",
      {"path": {"type": "string", "description": "Folder. Default: the work folder"},
       "depth": {"type": "integer", "description": "How deep to go, 1-4. Default 1"}})
def list_dir(ctx: ToolContext, a: dict):
    root = ctx.resolve(a.get("path") or ".")
    if not root.is_dir():
        raise ToolError(f"Not a folder: {root}")
    depth = min(4, max(1, int(a.get("depth") or 1)))
    out, count = [str(root)], 0

    def walk(d: Path, level: int):
        nonlocal count
        try:
            entries = sorted(d.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower()))
        except OSError as e:
            out.append("  " * level + f"[cannot open: {e}]")
            return
        for p in entries:
            count += 1
            if count > 500:
                return
            if p.is_dir():
                out.append("  " * level + p.name + "/")
                if level < depth and p.name not in SKIP_DIRS:
                    walk(p, level + 1)
            else:
                try:
                    size = p.stat().st_size
                except OSError:
                    size = 0
                out.append("  " * level + f"{p.name}  ({_size(size)})")

    walk(root, 1)
    if count > 500:
        out.append("[stopped at 500 entries]")
    return "\n".join(out)


def _size(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.0f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def _walk_files(root: Path):
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for f in filenames:
            yield Path(dirpath) / f


@tool("find_files", "Find files by name pattern, like *.py or report*.docx, searching inside a folder.",
      {"pattern": {"type": "string"}, "path": {"type": "string", "description": "Folder. Default: work folder"}},
      required=["pattern"])
def find_files(ctx: ToolContext, a: dict):
    root = ctx.resolve(a.get("path") or ".")
    pat = a["pattern"]
    hits = []
    for p in _walk_files(root):
        rel = p.relative_to(root).as_posix()
        if fnmatch.fnmatch(p.name.lower(), pat.lower()) or fnmatch.fnmatch(rel.lower(), pat.lower()):
            hits.append(rel)
            if len(hits) >= 300 or ctx.stop.is_set():
                break
    if not hits:
        return f"No files matching {pat} in {root}"
    return f"{len(hits)} file(s) in {root}:\n" + "\n".join(hits)


@tool("search_text", """
Search inside files for a regular expression (like grep). Returns file:line: text.
Use file_glob to narrow it, e.g. *.py.""",
      {"pattern": {"type": "string"},
       "path": {"type": "string", "description": "File or folder. Default: work folder"},
       "file_glob": {"type": "string"},
       "ignore_case": {"type": "boolean"},
       "max_results": {"type": "integer", "description": "Default 100"}},
      required=["pattern"])
def search_text(ctx: ToolContext, a: dict):
    root = ctx.resolve(a.get("path") or ".")
    try:
        rx = re.compile(a["pattern"], re.IGNORECASE if a.get("ignore_case") else 0)
    except re.error as e:
        raise ToolError(f"Bad regular expression: {e}")
    glob = a.get("file_glob")
    limit = int(a.get("max_results") or 100)
    files = [root] if root.is_file() else _walk_files(root)
    hits = []
    for p in files:
        if ctx.stop.is_set():
            break
        if glob and not fnmatch.fnmatch(p.name, glob):
            continue
        try:
            if p.stat().st_size > 5_000_000:
                continue
            text = _read_text(p)
        except (OSError, ToolError):
            continue
        for i, line in enumerate(text.splitlines(), 1):
            if rx.search(line):
                rel = p.name if root.is_file() else p.relative_to(root).as_posix()
                hits.append(f"{rel}:{i}: {line.strip()[:300]}")
                if len(hits) >= limit:
                    break
        if len(hits) >= limit:
            hits.append(f"[stopped at {limit} results]")
            break
    return "\n".join(hits) if hits else "No matches."


@tool("move_path", "Move or rename a file or folder.",
      {"source": {"type": "string"}, "destination": {"type": "string"}},
      required=["source", "destination"], risk=EDIT, risk_for=_edit_risk,
      describe=lambda a: f"Move {a.get('source')} to {a.get('destination')}")
def move_path(ctx: ToolContext, a: dict):
    src, dst = ctx.resolve(a["source"]), ctx.resolve(a["destination"])
    if not src.exists():
        raise ToolError(f"No such path: {src}")
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(src), str(dst))
    return f"Moved {src} to {dst}"


@tool("copy_path", "Copy a file or folder.",
      {"source": {"type": "string"}, "destination": {"type": "string"}},
      required=["source", "destination"], risk=EDIT, risk_for=_edit_risk,
      describe=lambda a: f"Copy {a.get('source')} to {a.get('destination')}")
def copy_path(ctx: ToolContext, a: dict):
    src, dst = ctx.resolve(a["source"]), ctx.resolve(a["destination"])
    if src.is_dir():
        shutil.copytree(src, dst, dirs_exist_ok=True)
    elif src.is_file():
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
    else:
        raise ToolError(f"No such path: {src}")
    return f"Copied {src} to {dst}"


@tool("delete_path", "Delete a file or folder. It goes to the Recycle Bin when possible.",
      {"path": {"type": "string"}}, required=["path"], risk=EDIT, risk_for=_edit_risk,
      describe=lambda a: f"Delete {a.get('path')} (to the Recycle Bin)")
def delete_path(ctx: ToolContext, a: dict):
    p = ctx.resolve(a["path"])
    if not p.exists():
        raise ToolError(f"No such path: {p}")
    if p == Path(p.anchor) or p == Path.home():
        raise ToolError("Refusing to delete a drive root or the home folder.")
    try:
        from send2trash import send2trash

        send2trash(str(p))
        return f"Moved {p} to the Recycle Bin."
    except ImportError:
        pass
    if p.is_dir():
        shutil.rmtree(p)
    else:
        p.unlink()
    return f"Deleted {p}."
