"""The saved chats (data/chats/*.json) for the list on the left: their titles, renaming and deleting.

A chat's title is its first request unless it was renamed. Titles are kept in data/chat_titles.json with each
file's time, so only chats that changed since are read again (a chat with images can be megabytes).
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from pathlib import Path

from . import config

TITLE_LEN = 60


@dataclass
class Chat:
    path: Path
    title: str
    mtime: float


def folder() -> Path:
    return config.DATA_DIR / "chats"


def _index_path() -> Path:
    return config.DATA_DIR / "chat_titles.json"


def _load_index() -> dict:
    try:
        data = json.loads(_index_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _save_index(index: dict) -> None:
    path = _index_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(index, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        pass                      # only a cache: the titles are worked out again next time


def short(text: str) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= TITLE_LEN else text[:TITLE_LEN - 1].rstrip() + "…"


def first_request(data: dict) -> str:
    """What the chat was started with: the first message typed (not a summary or a note the app added)."""
    for m in data.get("messages") or []:
        if not isinstance(m, dict) or m.get("role") != "user" or "_kind" in m:
            continue
        c = m.get("content")
        if isinstance(c, list):
            c = " ".join(p.get("text", "") for p in c if isinstance(p, dict) and p.get("type") == "text")
        text = m.get("_shown", c)
        if isinstance(text, str) and text.strip():
            return text
    return ""


def title_of(path: Path) -> str:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return path.stem
    return short(first_request(data) if isinstance(data, dict) else "") or "Untitled chat"


def list_chats(limit: int = 300) -> list[Chat]:
    """Newest activity first."""
    try:
        files = [(f, f.stat().st_mtime) for f in folder().glob("*.json")]
    except OSError:
        return []
    files.sort(key=lambda x: x[1], reverse=True)
    index, changed, out = _load_index(), False, []
    for f, mtime in files[:limit]:
        e = index.get(f.stem)
        if not isinstance(e, dict) or (not e.get("custom") and e.get("mtime") != mtime):
            e = index[f.stem] = {"title": title_of(f), "mtime": mtime}     # a renamed one keeps its name
            changed = True
        out.append(Chat(f, str(e.get("title") or f.stem), mtime))
    gone = [k for k in index if not (folder() / f"{k}.json").exists()]
    for k in gone:
        del index[k]
    if changed or gone:
        _save_index(index)
    return out


def title(path: Path) -> str:
    e = _load_index().get(path.stem)
    if isinstance(e, dict) and e.get("title"):
        return str(e["title"])
    return title_of(path) if path.exists() else "New chat"


def rename(path: Path, new_title: str) -> None:
    index = _load_index()
    index[path.stem] = {"title": short(new_title) or "Untitled chat", "mtime": 0, "custom": True}
    _save_index(index)


def delete(path: Path) -> None:
    path.unlink(missing_ok=True)
    index = _load_index()
    if index.pop(path.stem, None) is not None:
        _save_index(index)


def group(mtime: float, now: float | None = None) -> str:
    """The heading a chat goes under in the list, as in the Claude app."""
    now = time.time() if now is None else now
    today = time.mktime(time.localtime(now)[:3] + (0, 0, 0, 0, 0, -1))
    if mtime >= today:
        return "Today"
    if mtime >= today - 86400:
        return "Yesterday"
    if mtime >= today - 7 * 86400:
        return "Previous 7 days"
    if mtime >= today - 30 * 86400:
        return "Previous 30 days"
    return "Older"
