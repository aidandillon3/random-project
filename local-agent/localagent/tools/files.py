"""Reading, searching and editing files."""

from __future__ import annotations

import codecs
import fnmatch
import io
import os
import re
import shutil
import stat
from collections import deque
from pathlib import Path

from ..config import DATA_DIR
from .base import EDIT, RUN, SAFE, ToolContext, ToolError, ToolResult, tool

SKIP_DIRS = {".git", "node_modules", ".venv", "venv", "__pycache__", ".mypy_cache", ".idea", ".worktrees",
             ".pytest_cache", "dist", "build", ".next", "$RECYCLE.BIN", "System Volume Information"}
IMAGE_EXT = {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp"}
EXEC_DIRS = {".git", ".husky", ".githooks"}   # files here make git run programs (hooks, core.fsmonitor)
SECRET_DIRS = {".ssh", ".aws", ".gnupg"}
SECRET_EXT = {".pem", ".key", ".pfx", ".p12"}
PS_EXT = {".ps1", ".psm1", ".psd1"}
BIG_FILE = 4_000_000   # bytes; read_file goes through bigger files line by line


def _is_app_settings(path: Path) -> bool:
    """This app's own settings file: it holds the GitHub token and the server's API key."""
    try:
        return path.name.lower() == "settings.json" and path.resolve() == (DATA_DIR / "settings.json").resolve()
    except (OSError, ValueError):
        return False


def _is_secret(path: Path) -> bool:
    """Keys and credentials. Reading one asks first, and search_text skips them."""
    name = path.name.lower()
    if any(part.lower() in SECRET_DIRS for part in path.parts) or name in (".git-credentials", ".netrc", "_netrc"):
        return True
    if path.suffix.lower() in SECRET_EXT:
        return True
    if re.match(r"id_(rsa|dsa|ecdsa|ed25519)", name) and not name.endswith(".pub"):
        return True
    return (name == ".env" or name.startswith(".env.")) and not name.endswith((".example", ".sample", ".template"))


def _read_risk(ctx: ToolContext, args: dict) -> str:
    return RUN if _is_secret(ctx.resolve(args.get("path") or ".")) else SAFE


def _edit_risk(ctx: ToolContext, args: dict) -> str:
    """Edits inside the work folder are 'edit'. Outside it, in git's own folders (a hook or
    .git/config runs programs on the next git command) or in this app's settings they count as
    'run', so the 'auto-approve edits' mode still asks."""
    for k in ("path", "source", "destination"):
        if not args.get(k):
            continue
        try:
            p = ctx.resolve(args[k])
            if not ctx.inside_workspace(p) or _is_app_settings(p):
                return RUN
            rel = p.relative_to(ctx.workspace.resolve())
        except (OSError, ValueError):   # e.g. a NUL in the path
            return RUN
        # Windows ignores trailing dots and spaces, and .git::$INDEX_ALLOCATION is the folder too
        if any(part.lower().split(":")[0].rstrip(". ") in EXEC_DIRS for part in rel.parts):
            return RUN
    return EDIT


_BOMS = ((codecs.BOM_UTF8, "utf-8-sig"), (codecs.BOM_UTF16_LE, "utf-16-le"), (codecs.BOM_UTF16_BE, "utf-16-be"))


def _encoding(data: bytes, path: Path) -> str:
    """The encoding of a file, from its start: a BOM, else UTF-8, else the Windows code page
    (cp1252, or latin-1 for the few bytes it lacks). UTF-16 is only trusted with a BOM: guessed,
    any even-length text decodes as junk."""
    for bom, enc in _BOMS:
        if data.startswith(bom):
            return enc
    if b"\x00" in data[:8000]:
        raise ToolError(f"{path} looks like a binary file, not text.")
    try:
        codecs.getincrementaldecoder("utf-8")().decode(data)   # a character cut at the end is fine here
        return "utf-8"
    except UnicodeDecodeError:
        pass
    try:
        data.decode("cp1252")
        return "cp1252"
    except UnicodeDecodeError:
        return "latin-1"


def _read_text_enc(path: Path) -> tuple[str, str]:
    """A file's text and its encoding, so an edit can save it the same way."""
    data = path.read_bytes()
    enc = _encoding(data, path)
    if enc.startswith("utf-16"):
        return data[2:].decode(enc, errors="replace"), enc
    try:
        return data.decode(enc), enc   # utf-8-sig drops the BOM
    except UnicodeDecodeError:          # UTF-8 that breaks after the first 8 KB
        return data.decode("cp1252", errors="replace"), "cp1252"


def _read_text(path: Path) -> str:
    return _read_text_enc(path)[0]


def _save(path: Path, text: str, enc: str = "utf-8") -> str:
    """Write text in the given encoding. Returns a note to add when that wasn't possible."""
    ps = path.suffix.lower() in PS_EXT
    if enc == "utf-8" and ps and not text.isascii():
        enc = "utf-8-sig"   # Windows PowerShell 5 reads a script without a BOM as ANSI
    bom = codecs.BOM_UTF16_LE if enc == "utf-16-le" else codecs.BOM_UTF16_BE if enc == "utf-16-be" else b""
    note = ""
    try:
        data = bom + text.encode(enc)
    except UnicodeEncodeError:
        fallback = "utf-8-sig" if ps else "utf-8"
        data, note = text.encode(fallback), f" Saved as UTF-8: the new text has characters {enc} can't hold."
    path.write_bytes(data)
    return note


def _lines(path: Path):
    """The file's lines (ends kept), split where editors split them: \\n, \\r\\n or \\r. A big file
    is read as it goes instead of all at once."""
    if path.stat().st_size <= BIG_FILE:
        return io.StringIO(_read_text(path), newline="")
    with open(path, "rb") as f:
        enc = _encoding(f.read(65536), path)
    enc = "utf-16" if enc.startswith("utf-16") else enc   # "utf-16" reads the BOM instead of returning it
    return open(path, encoding=enc, errors="replace", newline="")


def _window(path: Path, offset: int, limit: int) -> tuple[list[str], int, int | None]:
    """limit lines from line offset (1-based; negative counts from the end). Returns them, the
    first one's number, and the line count (None when a big file wasn't read to the end)."""
    big = path.stat().st_size > BIG_FILE
    with _lines(path) as src:
        if offset < 0:
            tail, total = deque(maxlen=-offset), 0
            for total, line in enumerate(src, 1):
                tail.append(line)
            return [ln.rstrip("\r\n") for ln in list(tail)[:limit]], total - len(tail) + 1, total
        chunk, total = [], 0
        for total, line in enumerate(src, 1):
            if total >= offset + limit:
                if big:
                    return chunk, offset, None
            elif total >= offset:
                chunk.append(line.rstrip("\r\n"))
        return chunk, offset, total


def _text(a: dict, key: str, default: str | None = None) -> str:
    v = a.get(key, default)
    if not isinstance(v, str):
        raise ToolError(f"{key} must be text, not {type(v).__name__}.")
    return v


def _image_png(path: Path, max_side: int) -> bytes:
    from PIL import Image

    img = Image.open(path)
    img.thumbnail((max_side, max_side))
    buf = io.BytesIO()
    img.convert("RGB").save(buf, "PNG")
    return buf.getvalue()


READ_CHARS = 8000      # a read without a limit stops at whole lines within this many characters


def _within(lines: list[str], chars: int) -> list[str]:
    out, n = [], 0
    for line in lines:
        n += len(line) + 1
        if out and n > chars:
            break
        out.append(line)
    return out


@tool("read_file", """
Read a text file and get it back with line numbers. For big files, read a window with
offset (first line, 1-based; negative counts from the end, e.g. -50 for the last 50 lines)
and limit (number of lines). Image files (png, jpg...) are shown to you as a picture.""",
      {"path": {"type": "string", "description": "File path, absolute or relative to the work folder"},
       "offset": {"type": "integer", "description": "First line to read (1-based). Default 1. Negative: from the end"},
       "limit": {"type": "integer", "description": "How many lines. Default: up to 400 lines or about 8,000 characters"}},
      required=["path"], risk_for=_read_risk, describe=lambda a: f"Read {a.get('path')}")
def read_file(ctx: ToolContext, a: dict):
    path = ctx.resolve(a["path"])
    if not path.exists():
        raise ToolError(f"No such file: {path}")
    if path.is_dir():
        raise ToolError(f"{path} is a folder: list it with run_command (Get-ChildItem) or list_dir.")
    if _is_app_settings(path):
        raise ToolError("That is this app's own settings file, which holds sign-in tokens, so it can't be read. "
                        "Ask the user about the setting you need.")
    if path.suffix.lower() in IMAGE_EXT:
        return ToolResult(f"Image {path.name} attached.",
                          images=[_image_png(path, ctx.settings.screenshot_max_side)])
    offset = int(a.get("offset") or 1)
    limit = max(1, int(a.get("limit") or 400))
    chunk, start, total = _window(path, offset, limit)
    if not chunk:
        return f"{path} has {total} lines; there is nothing from line {start} on."
    if not a.get("limit"):
        chunk = _within(chunk, READ_CHARS)    # about 2,000 tokens unless more lines are asked for
    end = start + len(chunk) - 1
    width = len(str(end + 1))
    body = "\n".join(f"{i:>{width}}| {line}" for i, line in enumerate(chunk, start))
    if total is None:
        return (f"{path} (lines {start}-{end}; a big file, not counted to the end)\n{body}"
                f"\n[read more with offset={end + 1}, or offset=-{limit} for the end]")
    more = f"\n[file has {total} lines; read more with offset={end + 1}]" if end < total else ""
    return f"{path} (lines {start}-{end} of {total})\n{body}{more}"


@tool("write_file", """
Create a file or replace its whole content. Makes missing folders. For small changes to an
existing file prefer edit_file.""",
      {"path": {"type": "string"}, "content": {"type": "string"}},
      required=["path", "content"], risk=EDIT, risk_for=_edit_risk,
      describe=lambda a: f"Write {len(str(a.get('content') or ''))} characters to {a.get('path')}")
def write_file(ctx: ToolContext, a: dict):
    path = ctx.resolve(a["path"])
    path.parent.mkdir(parents=True, exist_ok=True)
    existed = path.exists()
    content = _text(a, "content")
    if path.suffix.lower() in (".bat", ".cmd"):   # cmd.exe can misread labels and goto with Unix line ends
        content = content.replace("\r\n", "\n").replace("\n", "\r\n")
    note = _save(path, content)
    return (f"{'Replaced' if existed else 'Created'} {path} ({len(content)} characters).{note}"
            + check_syntax(path))


def _apply_edit(path: Path, text: str, old: str, new: str, replace_all: bool) -> tuple[str, str]:
    """One exact replacement on a file's text. Returns (new text, what happened)."""
    if not old:
        raise ToolError("old_text is empty. Use write_file to create a file.")
    # new text gets the file's own line ending (the one most of its lines use)
    new = new.replace("\r\n", "\n")
    if text.count("\r\n") * 2 > text.count("\n"):
        new = new.replace("\n", "\r\n")
    count = text.count(old)
    if count == 0 and "\r\n" in text:
        old2 = old.replace("\r\n", "\n").replace("\n", "\r\n")
        if text.count(old2):
            old, count = old2, text.count(old2)
    if count == 0 and old.strip():
        # Forgive trailing-whitespace and line-ending differences, the most common near miss. Only
        # the matched lines change; the rest of the file keeps its spaces and line endings.
        lines = old.replace("\r\n", "\n").split("\n")
        rx = re.compile(r"[ \t]*\r?\n".join(re.escape(line.rstrip()) for line in lines))
        n = sum(1 for _ in rx.finditer(text))
        if n == 1 or (n > 1 and replace_all):
            return rx.sub(lambda m: new, text), f"{n} match{'es' if n > 1 else ''}, ignoring trailing spaces"
    if count == 0:
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
    text, enc = _read_text_enc(path)
    text, what = _apply_edit(path, text, _text(a, "old_text"), _text(a, "new_text"), bool(a.get("replace_all")))
    note = _save(path, text, enc)
    return f"Edited {path} ({what}).{note}" + check_syntax(path)


@tool("multi_edit", """
Make several exact replacements in one file at once, in order. Each edit works like edit_file
(old_text must match exactly and be unique unless replace_all). If any edit fails, none are
saved, so the file is never left half-changed.""",
      {"path": {"type": "string"},
       "edits": {"type": "array", "items": {"type": "object", "properties": {
           "old_text": {"type": "string"}, "new_text": {"type": "string"},
           "replace_all": {"type": "boolean"}}, "required": ["old_text", "new_text"]}}},
      required=["path", "edits"], risk=EDIT, risk_for=_edit_risk,
      describe=lambda a: f"Make {len(a['edits']) if isinstance(a.get('edits'), list) else 0} edits in {a.get('path')}")
def multi_edit(ctx: ToolContext, a: dict):
    path = ctx.resolve(a["path"])
    if not path.is_file():
        raise ToolError(f"No such file: {path}")
    edits = a.get("edits") or []
    if not edits:
        raise ToolError("edits is empty.")
    text, enc = _read_text_enc(path)
    for i, e in enumerate(edits, 1):
        if not isinstance(e, dict):
            raise ToolError(f"Edit {i} must be an object with old_text and new_text.")
        try:
            text, _ = _apply_edit(path, text, _text(e, "old_text", ""), _text(e, "new_text", ""),
                                  bool(e.get("replace_all")))
        except ToolError as err:
            raise ToolError(f"Edit {i} of {len(edits)} failed, so nothing was saved: {err}")
    note = _save(path, text, enc)
    return f"Made {len(edits)} edits in {path}.{note}" + check_syntax(path)


def check_syntax(path: Path) -> str:
    """A quick syntax check after an edit, so a broken file is noticed straight away instead
    of several steps later. Returns a warning to append, or ''."""
    ext = path.suffix.lower()
    try:
        if ext in (".py", ".pyw"):
            compile(path.read_bytes(), str(path), "exec", dont_inherit=True)
        elif ext == ".json":
            import json

            json.loads(_read_text(path))
        elif ext == ".toml":
            import tomllib

            tomllib.loads(_read_text(path))
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


def _braces(pattern: str) -> list[str]:
    m = re.search(r"\{([^{}]*)\}", pattern)
    if not m:
        return [pattern]
    return [p for alt in m.group(1).split(",") for p in _braces(pattern[:m.start()] + alt + pattern[m.end():])]


def glob_match(pattern: str, rel: str) -> bool:
    """A file name pattern against a file's name or its path from the search folder, ignoring
    case. **/ may match no folder at all (**/*.py finds top-level files too) and {py,ts} picks one."""
    rel = rel.lower()
    name = rel.rsplit("/", 1)[-1]
    for pat in _braces(pattern.strip().lower().replace("\\", "/")):
        while pat.startswith("**/"):
            pat = pat[3:]
        for p in {pat, pat.replace("/**/", "/")}:
            if fnmatch.fnmatchcase(name, p) or fnmatch.fnmatchcase(rel, p):
                return True
    return False


@tool("find_files", "Find files by name pattern, like *.py or report*.docx, searching inside a folder.",
      {"pattern": {"type": "string"}, "path": {"type": "string", "description": "Folder. Default: work folder"}},
      required=["pattern"])
def find_files(ctx: ToolContext, a: dict):
    root = ctx.resolve(a.get("path") or ".")
    pat = str(a["pattern"])
    hits = []
    for p in _walk_files(root):
        rel = p.relative_to(root).as_posix()
        if glob_match(pat, rel):
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
      required=["pattern"], risk_for=_read_risk, describe=lambda a: f"Search {a.get('path') or 'the work folder'}")
def search_text(ctx: ToolContext, a: dict):
    root = ctx.resolve(a.get("path") or ".")
    try:
        rx = re.compile(a["pattern"], re.IGNORECASE if a.get("ignore_case") else 0)
    except re.error as e:
        raise ToolError(f"Bad regular expression: {e}")
    glob = a.get("file_glob")
    limit = int(a.get("max_results") or 100)
    files = [root] if root.is_file() else _walk_files(root)
    secret_root = _is_secret(root)
    hits = []
    for p in files:
        if ctx.stop.is_set():
            break
        rel = p.name if root.is_file() else p.relative_to(root).as_posix()
        if glob and not glob_match(str(glob), rel):
            continue
        # keys and .env files only when the search is aimed at them (that asks first); the app's settings never
        if _is_app_settings(p) or (not secret_root and _is_secret(p)):
            continue
        try:
            if p.stat().st_size > 5_000_000:
                continue
            text = _read_text(p)
        except (OSError, ToolError):
            continue
        for i, line in enumerate(io.StringIO(text, newline=""), 1):   # numbered like read_file
            if rx.search(line.rstrip("\r\n")):       # so "$" matches at the end of a Windows (CRLF) line
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


def _as_given(ctx: ToolContext, path) -> Path:
    """The path made absolute without following links (resolve() would follow them)."""
    return Path(os.path.abspath(ctx.workspace / Path(str(path).strip().strip('"')).expanduser()))


def _is_link(p: Path) -> bool:
    """A symlink or a Windows junction."""
    try:
        st = os.lstat(p)
    except (OSError, ValueError):
        return False
    return stat.S_ISLNK(st.st_mode) or getattr(st, "st_reparse_tag", 0) == 0xA0000003   # IO_REPARSE_TAG_MOUNT_POINT


def _delete_risk(ctx: ToolContext, a: dict) -> str:
    """Deleting a whole folder asks even when edits are auto-approved."""
    risk = _edit_risk(ctx, a)
    p = _as_given(ctx, a.get("path") or ".")
    return RUN if risk == EDIT and p.is_dir() and not _is_link(p) else risk


@tool("delete_path", "Delete a file or folder. It goes to the Recycle Bin when possible.",
      {"path": {"type": "string"}}, required=["path"], risk=EDIT, risk_for=_delete_risk,
      describe=lambda a: f"Delete {a.get('path')} (to the Recycle Bin)")
def delete_path(ctx: ToolContext, a: dict):
    keep = {ctx.workspace.resolve(), Path.home().resolve(), _as_given(ctx, ctx.workspace), _as_given(ctx, Path.home())}

    def refuse(x: Path):
        if x == Path(x.anchor) or any(k == x or k.is_relative_to(x) for k in keep):
            raise ToolError("Refusing to delete a drive root, the home folder, the work folder, or a folder "
                            "that holds one of them.")

    link = _as_given(ctx, a["path"])
    if _is_link(link):   # remove the link itself, never what it points to
        refuse(link)
        os.unlink(link)  # on Windows this removes a folder link or junction too
        return f"Removed the link {link}; what it pointed to is untouched."
    p = ctx.resolve(a["path"])
    if not p.exists():
        raise ToolError(f"No such path: {p}")
    refuse(p)
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
