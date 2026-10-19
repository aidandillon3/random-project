"""Long-term memory: short saved notes, plus a search index of your files.

Everything lives in one SQLite file (data/memory.db) and uses SQLite's built-in full-text
search, which ranks results the way a search engine does (BM25). No GPU or extra model is
needed. The point is to keep the model's context small: only the few notes that match the
current request are shown to it, and everything else stays one `recall` away.
"""

from __future__ import annotations

import datetime as dt
import os
import re
import sqlite3
import threading
import time
import unicodedata
from dataclasses import dataclass
from pathlib import Path

from .config import DATA_DIR

STOP_WORDS = set("""
a an and are as at be but by can could do does for from had has have how i if in into is it its
me my no not of on or our please so than that the their them then there these they this to up us
was we were what when where which who why will with would you your yes ok okay just also make
get let lets want need help some any all
hi hey hello yo sup hiya howdy thanks thank thx ty cool nice great good morning afternoon evening
night""".split())   # greetings and thanks say nothing about which notes matter

# Files whose contents are indexed. Every other file is still findable by its name.
TEXT_EXT = {
    ".txt", ".md", ".markdown", ".rst", ".log", ".csv", ".tsv", ".json", ".jsonl", ".yaml", ".yml",
    ".toml", ".ini", ".cfg", ".conf", ".env.example", ".xml", ".html", ".htm", ".css", ".scss",
    ".py", ".pyw", ".ipynb", ".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx", ".vue", ".svelte",
    ".java", ".kt", ".kts", ".scala", ".go", ".rs", ".c", ".h", ".cpp", ".cc", ".hpp", ".cs",
    ".fs", ".vb", ".swift", ".m", ".rb", ".php", ".pl", ".lua", ".r", ".jl", ".dart", ".sql",
    ".sh", ".bash", ".ps1", ".psm1", ".bat", ".cmd", ".gd", ".shader", ".hlsl", ".glsl", ".tex",
    ".gradle", ".cmake", ".mk", ".dockerfile", ".srt", ".vtt",
}
TEXT_NAMES = {"makefile", "dockerfile", "readme", "license", "agent.md", ".gitignore", "requirements.txt"}
SKIP_DIRS = {".git", "node_modules", ".venv", "venv", "env", "__pycache__", ".mypy_cache", ".idea",
             ".pytest_cache", "dist", "build", ".next", ".cache", "$RECYCLE.BIN", "System Volume Information",
             "AppData", ".worktrees", "site-packages", "target", "bin", "obj", ".gradle", ".tox"}
MAX_TEXT_BYTES = 1_000_000
CHUNK_LINES = 60
MAX_CHUNKS = 999            # chunk ids are file_id * 1000 + n
MAX_FILES_PER_RUN = 60_000   # files read per run; the rest are read on the next one
COMMIT_EVERY = 200           # files indexed per write to the disk
MAX_NOTE_CHARS = 4000        # a note is a fact or two, not a document (recall_for shows at most 1,500 characters)


def _fold(text: str) -> str:
    """café -> cafe, as the search index sees it, so a request typed without accents still matches. Only Latin
    letters: other scripts keep their marks (the index keeps them too)."""
    out: list[str] = []
    for c in unicodedata.normalize("NFD", text):
        if unicodedata.combining(c) and out and out[-1] < "\u0250":
            continue
        out.append(c)
    return unicodedata.normalize("NFC", "".join(out))


def terms(text: str, limit: int = 16) -> list[str]:
    """The words worth searching for: lower-case, no filler words, no repeats."""
    out: list[str] = []
    for w in re.findall(r"[^\W_]+", _fold(text.lower())):
        if len(w) < 2 or w in STOP_WORDS or w in out:
            continue
        out.append(w)
        if len(out) >= limit:
            break
    return out


def _stem(word: str) -> str:
    """Rough word stem, so 'budgets' and 'budget' or 'tracking' and 'tracker' count as the same word."""
    for end in ("ing", "ers", "er", "es", "ed", "s"):
        if word.endswith(end) and len(word) - len(end) >= 4:
            return word[:-len(end)]
    return word


def _same_word(term: str, word: str) -> bool:
    """A request word matches a note word with the same stem ("budgets" ~ "budget", "tracking" ~
    "tracker"). Short words only match whole: "hi" is not "his" or "history"."""
    if len(term) < 4:
        return word in (term, term + "s")
    return word.startswith(_stem(term))


def fts_query(text: str) -> str:
    """Any of the words. The index stems words (budgets -> budget) and each word also matches
    longer words that start with it."""
    return " OR ".join(f'"{t}"*' for t in terms(text))


@dataclass
class Note:
    id: int
    title: str
    body: str
    pinned: bool = False
    updated: str = ""
    score: float = 0.0

    def line(self) -> str:
        return f"(#{self.id}) {self.title}: {self.body}" if self.title else f"(#{self.id}) {self.body}"


@dataclass
class FileHit:
    path: str
    line: int
    snippet: str
    score: float = 0.0


class Memory:
    def __init__(self, path: Path | None = None):
        self.path = Path(path or DATA_DIR / "memory.db")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.db = sqlite3.connect(str(self.path), check_same_thread=False, timeout=30)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=NORMAL")     # safe with WAL, and much faster for the file index
        self._setup()
        self._import_old_notes()

    # ------------------------------------------------------------------ setup
    def _setup(self):
        with self.lock, self.db:
            self.db.executescript("""
                CREATE TABLE IF NOT EXISTS notes(
                    id INTEGER PRIMARY KEY, title TEXT NOT NULL DEFAULT '', body TEXT NOT NULL,
                    pinned INTEGER NOT NULL DEFAULT 0, created TEXT, updated TEXT, used INTEGER DEFAULT 0);
                CREATE VIRTUAL TABLE IF NOT EXISTS notes_fts USING fts5(title, body, tokenize='porter unicode61');
                CREATE TABLE IF NOT EXISTS files(
                    id INTEGER PRIMARY KEY, path TEXT UNIQUE NOT NULL, mtime REAL, size INTEGER, root TEXT);
                CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(path, start UNINDEXED, text, tokenize='porter unicode61');
            """)

    def _import_old_notes(self):
        """Older versions kept memory in data/memory.md, one line per fact."""
        old = self.path.parent / "memory.md"
        if not old.exists() or self.count_notes():
            return
        for line in old.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if line.startswith("- "):
                self.add(line[2:].strip())
        old.rename(old.with_suffix(".md.imported"))

    def close(self):
        with self.lock:
            self.db.close()

    # ------------------------------------------------------------------ notes
    def add(self, body: str, title: str = "", pinned: bool = False, replaces: int | None = None) -> Note:
        """A new note; with replaces, that note is changed instead. Another note with the same title is left
        alone (it may hold a different fact)."""
        if replaces:
            return self.update(int(replaces), body=body, title=title or None, pinned=pinned or None)
        body, title = body.strip(), title.strip()
        if not body:
            raise ValueError("The note is empty.")
        _check_length(body)
        now = dt.datetime.now().strftime("%Y-%m-%d %H:%M")
        with self.lock, self.db:
            cur = self.db.execute("INSERT INTO notes(title, body, pinned, created, updated) VALUES(?,?,?,?,?)",
                                  (title, body, int(pinned), now, now))
            nid = cur.lastrowid
            self.db.execute("INSERT INTO notes_fts(rowid, title, body) VALUES(?,?,?)", (nid, title, body))
        return self.get(nid)

    def update(self, nid: int, body: str | None = None, title: str | None = None,
               pinned: bool | None = None) -> Note:
        note = self.get(nid)
        if note is None:
            raise KeyError(f"There is no note #{nid}.")
        body = note.body if body is None else body.strip()
        title = note.title if title is None else title.strip()
        if not body:
            raise ValueError("The note is empty.")
        if body != note.body:
            _check_length(body)
        pinned = note.pinned if pinned is None else pinned
        now = dt.datetime.now().strftime("%Y-%m-%d %H:%M")
        with self.lock, self.db:
            self.db.execute("UPDATE notes SET title=?, body=?, pinned=?, updated=? WHERE id=?",
                            (title, body, int(pinned), now, nid))
            self.db.execute("DELETE FROM notes_fts WHERE rowid=?", (nid,))
            self.db.execute("INSERT INTO notes_fts(rowid, title, body) VALUES(?,?,?)", (nid, title, body))
        return self.get(nid)

    def delete(self, nid: int) -> bool:
        with self.lock, self.db:
            n = self.db.execute("DELETE FROM notes WHERE id=?", (nid,)).rowcount
            self.db.execute("DELETE FROM notes_fts WHERE rowid=?", (nid,))
        return bool(n)

    def get(self, nid: int) -> Note | None:
        with self.lock:
            r = self.db.execute("SELECT id, title, body, pinned, updated FROM notes WHERE id=?", (nid,)).fetchone()
        return Note(r[0], r[1], r[2], bool(r[3]), r[4] or "") if r else None

    def notes(self, query: str = "") -> list[Note]:
        """All notes (newest first), or the ones matching a search, best first."""
        if query.strip():
            return self.search_notes(query, limit=500)
        with self.lock:
            rows = self.db.execute("SELECT id, title, body, pinned, updated FROM notes "
                                   "ORDER BY pinned DESC, updated DESC").fetchall()
        return [Note(r[0], r[1], r[2], bool(r[3]), r[4] or "") for r in rows]

    def pinned(self) -> list[Note]:
        return [n for n in self.notes() if n.pinned]

    def count_notes(self) -> int:
        with self.lock:
            return self.db.execute("SELECT count(*) FROM notes").fetchone()[0]

    def search_notes(self, query: str, limit: int = 8) -> list[Note]:
        q = fts_query(query)
        if not q:
            return []
        with self.lock:
            rows = self.db.execute(
                "SELECT n.id, n.title, n.body, n.pinned, n.updated, bm25(notes_fts, 3.0, 1.0) AS s "
                "FROM notes_fts JOIN notes n ON n.id = notes_fts.rowid WHERE notes_fts MATCH ? "
                "ORDER BY s LIMIT ?", (q, limit)).fetchall()
        return [Note(r[0], r[1], r[2], bool(r[3]), r[4] or "", -r[5]) for r in rows]

    def recall_for(self, request: str, limit: int = 5, budget: int = 1500,
                   exclude: set[int] | frozenset[int] = frozenset()) -> list[Note]:
        """Notes worth showing the model with this request, unasked. Stricter than a search:
        a note has to share two real words with the request (or the one word of a short request),
        so the model isn't handed loosely related clutter. exclude: ids of notes the conversation
        already has. All of their line()s together stay within budget characters (a long note is cut)."""
        want = terms(request)
        if not want:
            return []
        need = 1 if len(want) <= 2 else 2
        out, used = [], 0
        for n in self.search_notes(request, limit=limit * 3 + len(exclude)):
            if n.pinned or n.id in exclude:
                continue  # pinned ones are in the system prompt already
            words = set(terms(f"{n.title} {n.body}", limit=400))
            hits = sum(1 for t in want if any(_same_word(t, w) for w in words))
            if hits < need:
                continue
            over = used + len(n.line()) - budget
            if over > 0:
                if out:
                    break
                n.body = n.body[:max(0, len(n.body) - over - 4)].rstrip() + " ..."   # the first one, cut to fit
            out.append(n)
            used += len(n.line())
            if len(out) >= limit:
                break
        if out:
            with self.lock, self.db:
                self.db.executemany("UPDATE notes SET used = used + 1 WHERE id=?", [(n.id,) for n in out])
        return out

    # ------------------------------------------------------------------ file index
    def search_files(self, query: str, limit: int = 10) -> list[FileHit]:
        q = fts_query(query)
        if not q:
            return []
        with self.lock:
            rows = self.db.execute(
                "SELECT path, start, snippet(chunks_fts, 2, '[', ']', ' ... ', 20), bm25(chunks_fts, 4.0, 0.0, 1.0) AS s "
                "FROM chunks_fts WHERE chunks_fts MATCH ? ORDER BY s LIMIT ?", (q, limit * 4)).fetchall()
        out, seen = [], set()
        for path, start, snip, s in rows:
            if path in seen:
                continue  # one hit per file, the best one
            seen.add(path)
            out.append(FileHit(path, int(start), " ".join(snip.split()), -s))
            if len(out) >= limit:
                break
        return out

    def file_count(self) -> int:
        with self.lock:
            return self.db.execute("SELECT count(*) FROM files").fetchone()[0]

    def index_folder(self, root: Path, stop: threading.Event | None = None,
                     progress=lambda n, path: None, max_files: int = MAX_FILES_PER_RUN) -> dict:
        """Bring the index of one folder up to date: new and changed files are (re)read, files
        that are gone are dropped. Unchanged files cost one stat call. At most max_files are read
        per run ("left" says how many wait for the next one). A folder inside another indexed
        folder shares its files' entries."""
        root = Path(root).expanduser().resolve()
        stats = {"seen": 0, "updated": 0, "removed": 0, "left": 0}
        if not root.is_dir():
            return stats
        key = str(root)
        prefix = key if key.endswith(os.sep) else key + os.sep
        with self.lock:      # every file under root, whichever indexed folder added it
            known = {p: (fid, m, s, r) for fid, p, m, s, r in self.db.execute(
                "SELECT id, path, mtime, size, root FROM files WHERE root=? OR (path >= ? AND path < ?)",
                (key, prefix, prefix[:-1] + chr(ord(prefix[-1]) + 1)))}
        seen: set[str] = set()
        try:
            for dirpath, dirnames, filenames in os.walk(root):
                if stop is not None and stop.is_set():
                    return stats
                dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS and not d.startswith(".")]
                for name in filenames:
                    p = os.path.join(dirpath, name)
                    try:
                        st = os.stat(p)
                    except OSError:
                        continue
                    seen.add(p)
                    stats["seen"] += 1
                    old = known.get(p)
                    if old and old[1] == st.st_mtime and old[2] == st.st_size:
                        continue
                    if stats["updated"] >= max_files:
                        stats["left"] += 1          # read on the next run; the walk goes on so removal can run
                        continue
                    self._index_file(p, st, key, old[0] if old else None)
                    stats["updated"] += 1
                    if stats["updated"] % COMMIT_EVERY == 0:
                        with self.lock:
                            self.db.commit()
                        progress(stats["seen"], p)
            # the walk got through: drop what's gone. A file another indexed folder added and this walk
            # skipped (e.g. inside a .hidden folder) only when it's really gone.
            gone = [fid for p, (fid, _m, _s, r) in known.items()
                    if p not in seen and (r == key or not os.path.exists(p))]
            with self.lock:
                for fid in gone:
                    self._drop_chunks(fid)
                    self.db.execute("DELETE FROM files WHERE id=?", (fid,))
            stats["removed"] = len(gone)
        except BaseException:
            with self.lock:
                self.db.rollback()
            raise
        finally:
            with self.lock:
                if self.db.in_transaction:
                    self.db.commit()
        return stats

    def forget_folder(self, root: Path):
        key = str(Path(root).expanduser().resolve())
        with self.lock, self.db:
            for (fid,) in self.db.execute("SELECT id FROM files WHERE root=?", (key,)).fetchall():
                self._drop_chunks(fid)
            self.db.execute("DELETE FROM files WHERE root=?", (key,))

    def _drop_chunks(self, fid: int):
        self.db.execute("DELETE FROM chunks_fts WHERE rowid BETWEEN ? AND ?", (fid * 1000, fid * 1000 + 999))

    def _index_file(self, path: str, st, root: str, fid: int | None):
        """Writes without committing: index_folder commits every COMMIT_EVERY files."""
        text = _read_text(path, st.st_size)
        with self.lock:
            if fid is None:      # another indexing run may have added it meanwhile
                row = self.db.execute("SELECT id FROM files WHERE path=?", (path,)).fetchone()
                fid = row[0] if row else None
            if fid is None:
                fid = self.db.execute("INSERT INTO files(path, mtime, size, root) VALUES(?,?,?,?)",
                                      (path, st.st_mtime, st.st_size, root)).lastrowid
            else:
                self.db.execute("UPDATE files SET mtime=?, size=? WHERE id=?", (st.st_mtime, st.st_size, fid))
                self._drop_chunks(fid)
            if not text:
                self.db.execute("INSERT INTO chunks_fts(rowid, path, start, text) VALUES(?,?,?,?)",
                                (fid * 1000, path, 1, ""))
                return
            lines = text.splitlines()
            for n, start in enumerate(range(0, max(1, len(lines)), CHUNK_LINES)):
                if n >= MAX_CHUNKS:
                    break
                chunk = "\n".join(lines[start:start + CHUNK_LINES])
                self.db.execute("INSERT INTO chunks_fts(rowid, path, start, text) VALUES(?,?,?,?)",
                                (fid * 1000 + n, path, start + 1, chunk))


def _check_length(body: str):
    if len(body) > MAX_NOTE_CHARS:
        raise ValueError(f"The note is {len(body):,} characters; keep it under {MAX_NOTE_CHARS:,}: one or two "
                         "short facts per note (save a long text as a file instead).")


def _read_text(path: str, size: int) -> str:
    name = os.path.basename(path).lower()
    ext = os.path.splitext(name)[1]
    if size > MAX_TEXT_BYTES or not (ext in TEXT_EXT or name in TEXT_NAMES):
        return ""
    try:
        with open(path, "rb") as f:
            data = f.read()
    except OSError:
        return ""
    if b"\x00" in data[:4000]:
        return ""
    for enc in ("utf-8", "cp1252"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", "replace")


# ---------------------------------------------------------------------- shared instance
_memory: Memory | None = None
_memory_lock = threading.Lock()


def get_memory() -> Memory:
    global _memory
    with _memory_lock:
        if _memory is None or _memory.path != DATA_DIR / "memory.db":
            _memory = Memory()
        return _memory


def index_roots(settings) -> list[Path]:
    roots = [settings.workspace_path()] + [Path(p).expanduser() for p in (settings.index_folders or [])]
    out: list[Path] = []
    for r in roots:
        try:
            r = r.resolve()
        except OSError:
            continue
        if r not in out:
            out.append(r)
    return out


class Indexer(threading.Thread):
    """Keeps the file index fresh in the background: once at start, then every few minutes."""

    def __init__(self, settings, every_s: float = 600):
        super().__init__(name="indexer", daemon=True)
        self.settings = settings
        self.every_s = every_s
        self.stop_event = threading.Event()
        self.wake = threading.Event()
        self.status = "Not started"

    def run(self):
        while not self.stop_event.is_set():
            if getattr(self.settings, "index_files", True):
                try:
                    self.run_once()
                except Exception as e:  # noqa: BLE001 - keep the thread alive; it tries again next time
                    self.status = f"Indexing failed: {e}"
            self.wake.wait(self.every_s)
            self.wake.clear()

    def run_once(self):
        mem = get_memory()
        total = {"seen": 0, "updated": 0, "removed": 0, "left": 0}
        started = time.time()
        for root in index_roots(self.settings):
            self.status = f"Indexing {root}..."
            st = mem.index_folder(root, self.stop_event,
                                  progress=lambda n, p: setattr(self, "status", f"Indexing... {n} files"))
            for k in total:
                total[k] += st[k]
        n = mem.file_count()
        self.status = (f"{n} file{'' if n == 1 else 's'} indexed. Last check {time.strftime('%H:%M')} "
                       f"({total['updated']} new or changed, {time.time() - started:.0f}s).")
        if total["left"] and not self.stop_event.is_set():
            self.status += f" {total['left']} more to read..."
            self.wake.set()       # carry on right away

    def refresh_now(self):
        self.wake.set()

    def stop(self):
        self.stop_event.set()
        self.wake.set()


_indexer: Indexer | None = None


def start_indexer(settings) -> Indexer:
    global _indexer
    if _indexer is None or not _indexer.is_alive():
        _indexer = Indexer(settings)
        _indexer.start()
    elif _indexer.settings is not settings:
        _indexer.settings = settings
        _indexer.refresh_now()
    return _indexer


def indexer() -> Indexer | None:
    return _indexer
