"""Write edits back into .jbeam files without reformatting them.

An edit names a file, a part and a row by its content (a node by its id, a
beam by its two node ids and which copy it is), not by line number, so a list
of edits made in the 3D view still lands on the right rows after earlier
edits in the list added or removed lines. Only the characters that change are
touched; comments and layout stay as the author wrote them.
"""
import json
import os
import shutil
import time

from . import jbeamfile
from .jbeamfile import dumps_inline, fmt_num


class EditError(Exception):
    pass


class TextDoc:
    """A file's text plus a list of pending (start, end, new text) edits."""

    def __init__(self, text):
        self.text = text
        self.nl = "\r\n" if "\r\n" in text else "\n"
        self._tree = None
        self.pending = []

    @property
    def tree(self):
        if self._tree is None:
            try:
                self._tree = jbeamfile.parse_spans(self.text)
            except jbeamfile.ParseError as e:
                raise EditError("the file doesn't parse: %s" % e)
        return self._tree

    def flush(self):
        if not self.pending:
            return
        for s, e, new in sorted(self.pending, key=lambda x: (x[0], x[1]), reverse=True):
            self.text = self.text[:s] + new + self.text[e:]
        self.pending = []
        self._tree = None

    def conflicts(self, s, e):
        for ps, pe, _ in self.pending:
            if max(s, ps) <= min(e, pe):
                return True
        return False

    def add(self, edits):
        """Queue edits computed on the current tree (they must not touch each other)."""
        for s, e, new in edits:
            self.pending.append((s, e, new))

    # -- text helpers -------------------------------------------------------
    def line_start(self, i):
        return self.text.rfind("\n", 0, i) + 1

    def line_end(self, i):
        j = self.text.find("\n", i)
        return len(self.text) if j < 0 else j

    def indent_of(self, i):
        ls = self.line_start(i)
        j = ls
        while j < len(self.text) and self.text[j] in " \t":
            j += 1
        return self.text[ls:j]

    def rest_is_blank(self, i):
        """After i on its line: only a comma, spaces and maybe a // comment?"""
        j = i
        t = self.text
        n = self.line_end(i)
        while j < n and t[j] in " \t\r,":
            j += 1
        return j >= n or t.startswith("//", j)

    def before_is_blank(self, i):
        return self.text[self.line_start(i):i].strip() == ""

    def after_comma(self, i):
        """Position after an optional comma (and spaces) following i."""
        j = i
        t = self.text
        while j < len(t) and t[j] in " \t":
            j += 1
        if j < len(t) and t[j] == ",":
            return j + 1
        return i


# ---------------------------------------------------------------------------
# Finding things in a file
# ---------------------------------------------------------------------------

def find_part(doc, pname):
    root = doc.tree
    if root.kind != "obj":
        raise EditError("the file is not a { } object of parts")
    p = root.get(pname)
    if p is None or p.kind != "obj":
        raise EditError("part %s is not in this file" % pname)
    return p


def find_section(doc, pname, sec, create=False):
    p = find_part(doc, pname)
    s = p.get(sec)
    if s is None:
        return None
    if s.kind != "arr":
        raise EditError("%s in part %s is not a table" % (sec, pname))
    return s


def header_len(sec_node):
    if sec_node.items and sec_node.items[0].kind == "arr":
        return len(sec_node.items[0].items)
    return 0


def data_rows(sec_node):
    return [r for r in sec_node.items[1:] if r.kind == "arr"]


def _cell(v):
    return v.value if v.kind in ("str", "num", "lit") else None


def find_row(sec_node, key):
    """key: ("id", name) for id-tables (last match wins, like the game), or
    ("cells", [c1, c2, ...], occurrence)."""
    if key[0] == "id":
        found = None
        for r in data_rows(sec_node):
            if r.items and _cell(r.items[0]) == key[1]:
                found = r
        return found
    cells, occ = key[1], key[2]
    n = 0
    for r in data_rows(sec_node):
        if len(r.items) >= len(cells) and all(_cell(r.items[i]) == c for i, c in enumerate(cells)):
            if n == occ:
                return r
            n += 1
    return None


# ---------------------------------------------------------------------------
# Edit builders (return lists of (start, end, text))
# ---------------------------------------------------------------------------

def edits_replace(node, value, digits=4):
    return [(node.start, node.end, dumps_inline(value, digits))]


def edits_row_options(doc, sec_node, row, updates):
    """Set (or with None, remove) keys in a row's inline options dict."""
    h = header_len(sec_node)
    items = row.items
    if len(items) == h + 1 and items[h].kind == "obj":
        cur = items[h].to_py()
        for k, v in updates.items():
            if v is None:
                cur.pop(k, None)
            else:
                cur[k] = v
        if cur:
            return [(items[h].start, items[h].end, dumps_inline(cur))]
        # remove ", {...}" entirely
        return [(items[h - 1].end, items[h].end, "")]
    if len(items) == h:
        cur = {k: v for k, v in updates.items() if v is not None}
        if not cur:
            return []
        return [(items[h - 1].end, items[h - 1].end, ", " + dumps_inline(cur))]
    raise EditError("row %s has %d cells but the header has %d; fix the row first"
                    % (dumps_inline(row.to_py())[:60], len(items), h))


def edits_insert_row(doc, sec_node, row_value, anchor=None):
    """Insert a new row after anchor (a row SNode) or after the last data row."""
    rows = data_rows(sec_node)
    if anchor is None:
        anchor = rows[-1] if rows else (sec_node.items[0] if sec_node.items else None)
    text = dumps_inline(row_value)
    nl = doc.nl
    if anchor is None:
        # empty section: put the row right after "["
        ind = doc.indent_of(sec_node.start) + "    "
        return [(sec_node.start + 1, sec_node.start + 1, nl + ind + text + ",")]
    after = doc.after_comma(anchor.end)
    comma = "" if after != anchor.end else ","
    if doc.rest_is_blank(anchor.end):
        eol = doc.line_end(anchor.end)
        if eol > 0 and doc.text[eol - 1] == "\r":
            eol -= 1
        ind = doc.indent_of(anchor.start)
        edits = []
        if comma:
            edits.append((anchor.end, anchor.end, ","))
        edits.append((eol, eol, nl + ind + text + ","))
        return edits
    return [(after, after, comma + " " + text + ",")]


def edits_delete(doc, node):
    """Delete a list element (a row): whole lines when it has them to itself."""
    if doc.before_is_blank(node.start) and doc.rest_is_blank(node.end):
        s = doc.line_start(node.start)
        e = doc.line_end(node.end)
        if e < len(doc.text):
            e += 1  # the newline
        return [(s, e, "")]
    e = doc.after_comma(node.end)
    while e < len(doc.text) and doc.text[e] in " \t":
        e += 1
    return [(node.start, e, "")]


def edits_set_path(doc, obj, path, value):
    """Set a value inside nested dicts, creating keys as needed."""
    cur = obj
    for i, k in enumerate(path):
        if cur.kind != "obj":
            raise EditError("%s is not a { } object" % "/".join(path[:i]))
        child = cur.get(k)
        if child is None:
            rest = value
            for kk in reversed(path[i + 1:]):
                rest = {kk: rest}
            txt = "%s: %s" % (json.dumps(k, ensure_ascii=False), dumps_inline(rest))
            if cur.items:
                last = cur.items[-1][2]
                pos = doc.after_comma(last.end)
                sep = "" if pos != last.end else ","
                ind = doc.indent_of(cur.items[-1][1])
                if doc.rest_is_blank(last.end):
                    eol = doc.line_end(last.end)
                    if eol > 0 and doc.text[eol - 1] == "\r":
                        eol -= 1
                    edits = [(last.end, last.end, sep)] if sep else []
                    edits.append((eol, eol, doc.nl + ind + txt + ","))
                    return edits
                return [(pos, pos, sep + " " + txt)]
            return [(cur.start + 1, cur.start + 1, txt)]
        if i == len(path) - 1:
            if value is None:
                for _k, kpos, v in cur.items:
                    if _k == k:
                        e = doc.after_comma(v.end)
                        return [(kpos, e, "")]
            return [(child.start, child.end, dumps_inline(value))]
        cur = child
    return []


# ---------------------------------------------------------------------------
# Applying a list of operations
# ---------------------------------------------------------------------------

def _num3(v):
    return [float(x) for x in v]


class Session:
    """Applies operations to files of one model; call commit() to write."""

    def __init__(self, model, backup_dir):
        self.model = model
        self.backup_dir = backup_dir
        self.docs = {}       # file key -> TextDoc
        self.new_files = {}  # disk path -> text
        self.originals = {}  # file key -> original text

    def doc(self, fkey):
        if fkey in self.docs:
            return self.docs[fkey]
        fr = self.model.files.get(fkey)
        if fr is None:
            raise EditError("unknown file")
        if not fr.root.editable:
            raise EditError("%s is inside a zip or the game folder and can't be edited. Unpack the mod first."
                            % fr.rel)
        text = jbeamfile.decode(fr.root.read(fr.rel))
        self.originals[fkey] = text
        d = self.docs[fkey] = TextDoc(text)
        return d

    def _try(self, doc, fn):
        """Run an edit builder; if it fails or clashes with queued edits, flush and retry once."""
        try:
            edits = fn()
        except EditError:
            if not doc.pending:
                raise
            doc.flush()
            edits = fn()
        if any(doc.conflicts(s, e) for s, e, _ in edits):
            doc.flush()
            edits = fn()
        doc.add(edits)

    def apply(self, op):
        t = op.get("op")
        if t == "file.new":
            return self._file_new(op)
        doc = self.doc(op["f"])
        pname = op.get("part")

        if t == "node.move":
            def fn():
                sec = find_section(doc, pname, "nodes")
                row = find_row(sec, ("id", op["id"])) if sec else None
                if row is None:
                    raise EditError("node %s not found in part %s" % (op["id"], pname))
                if len(row.items) < 4:
                    raise EditError("node %s row is too short" % op["id"])
                x, y, z = _num3(op["pos"])
                return [(row.items[1].start, row.items[1].end, fmt_num(x)),
                        (row.items[2].start, row.items[2].end, fmt_num(y)),
                        (row.items[3].start, row.items[3].end, fmt_num(z))]
            self._try(doc, fn)
        elif t in ("node.set", "beam.set", "tri.set", "row.opts"):
            sec_name = {"node.set": "nodes", "tri.set": op.get("sec", "triangles")}.get(t, op.get("sec", "beams"))

            def fn():
                sec = find_section(doc, pname, sec_name)
                key = ("id", op["id"]) if t == "node.set" else ("cells", op["rk"][:-1], op["rk"][-1])
                row = find_row(sec, key) if sec else None
                if row is None:
                    raise EditError("row %s not found in %s of part %s" % (key[1], sec_name, pname))
                return edits_row_options(doc, sec, row, op["opts"])
            self._try(doc, fn)
        elif t in ("node.add", "beam.add", "tri.add", "row.add"):
            sec_name = {"node.add": "nodes", "tri.add": op.get("sec", "triangles")}.get(t, op.get("sec", "beams"))

            def fn():
                sec = find_section(doc, pname, sec_name)
                if sec is None:
                    raise EditError("part %s has no %s table; add one first" % (pname, sec_name))
                anchor = None
                if op.get("after") is not None:
                    a = op["after"]
                    anchor = find_row(sec, ("id", a) if isinstance(a, str) else ("cells", a[:-1], a[-1]))
                return edits_insert_row(doc, sec, op["row"], anchor)
            if not self._section_exists(doc, pname, sec_name):
                self._add_section(doc, pname, sec_name)
            self._try(doc, fn)
        elif t in ("node.del", "beam.del", "tri.del"):
            sec_name = {"node.del": "nodes", "tri.del": op.get("sec", "triangles")}.get(t, op.get("sec", "beams"))

            def fn():
                sec = find_section(doc, pname, sec_name)
                key = ("id", op["id"]) if t == "node.del" else ("cells", op["rk"][:-1], op["rk"][-1])
                row = find_row(sec, key) if sec else None
                if row is None:
                    raise EditError("row %s not found in %s of part %s" % (key[1], sec_name, pname))
                return edits_delete(doc, row)
            self._try(doc, fn)
        elif t == "tri.flip":
            def fn():
                sec = find_section(doc, pname, op.get("sec", "triangles"))
                row = find_row(sec, ("cells", op["rk"][:-1], op["rk"][-1])) if sec else None
                if row is None:
                    raise EditError("triangle %s not found in part %s" % (op["rk"][:-1], pname))
                i1, i2 = (1, 2) if len(op["rk"]) - 1 == 3 else (1, 3)
                a, b = row.items[i1], row.items[i2]
                return [(a.start, a.end, doc.text[b.start:b.end]), (b.start, b.end, doc.text[a.start:a.end])]
            self._try(doc, fn)
        elif t == "cell.set":
            def fn():
                sec = find_section(doc, pname, op["sec"])
                if sec is None or not (0 <= op["row"] < len(sec.items)):
                    raise EditError("row %s of %s not found" % (op["row"], op["sec"]))
                row = sec.items[op["row"]]
                if row.kind != "arr" or not (0 <= op["col"] < len(row.items)):
                    raise EditError("cell not found")
                return edits_replace(row.items[op["col"]], op["value"], 6)
            self._try(doc, fn)
        elif t == "rowidx.del":
            def fn():
                sec = find_section(doc, pname, op["sec"])
                if sec is None or not (1 <= op["row"] < len(sec.items)):
                    raise EditError("row not found")
                return edits_delete(doc, sec.items[op["row"]])
            self._try(doc, fn)
        elif t == "part.set":
            def fn():
                return edits_set_path(doc, find_part(doc, pname), op["path"], op.get("value"))
            self._try(doc, fn)
        elif t == "part.replace":
            try:
                v = jbeamfile.loads(op["text"])
            except jbeamfile.ParseError as e:
                raise EditError("the part text doesn't parse: %s" % e)
            if not isinstance(v, dict):
                raise EditError("a part must be a { } object")

            def fn():
                p = find_part(doc, pname)
                return [(p.start, p.end, op["text"].strip())]
            self._try(doc, fn)
        elif t == "part.add":
            def fn():
                root = doc.tree
                if root.kind != "obj":
                    raise EditError("the file is not a { } object of parts")
                if root.get(op["name"]) is not None:
                    raise EditError("part %s already exists in this file" % op["name"])
                body = op.get("text") or part_text(op["name"], op["data"], doc.nl)
                if root.items:
                    last = root.items[-1][2]
                    pos = doc.after_comma(last.end)
                    sep = "" if pos != last.end else ","
                    return [(last.end, last.end, sep), (root.end - 1, root.end - 1, body + doc.nl)] if sep else \
                        [(root.end - 1, root.end - 1, body + doc.nl)]
                return [(root.end - 1, root.end - 1, doc.nl + body + doc.nl)]
            self._try(doc, fn)
        elif t == "slot.add":
            def fn():
                p = find_part(doc, pname)
                s2 = p.get("slots2")
                s1 = p.get("slots")
                if s2 is not None and s2.kind == "arr":
                    row = [op["type"], [op["type"]], [], op.get("default", ""), op.get("description") or op["type"]]
                    return edits_insert_row(doc, s2, row)
                if s1 is not None and s1.kind == "arr":
                    row = [op["type"], op.get("default", ""), op.get("description") or op["type"]]
                    return edits_insert_row(doc, s1, row)
                return edits_set_path(doc, p, ["slots"], [["type", "default", "description"],
                                                         [op["type"], op.get("default", ""),
                                                          op.get("description") or op["type"]]])
            self._try(doc, fn)
        elif t == "part.del":
            def fn():
                root = doc.tree
                for k, kpos, v in root.items:
                    if k == pname:
                        s = doc.line_start(kpos) if doc.before_is_blank(kpos) else kpos
                        e = doc.after_comma(v.end)
                        if doc.rest_is_blank(e):
                            e = doc.line_end(e) + 1
                        return [(s, min(e, len(doc.text)), "")]
                raise EditError("part %s not found" % pname)
            self._try(doc, fn)
        else:
            raise EditError("unknown edit %r" % t)

    def _section_exists(self, doc, pname, sec):
        doc.flush()
        return find_part(doc, pname).get(sec) is not None

    def _add_section(self, doc, pname, sec):
        headers = {"nodes": ["id", "posX", "posY", "posZ"], "beams": ["id1:", "id2:"],
                   "triangles": ["id1:", "id2:", "id3:"], "flexbodies": ["mesh", "[group]:", "nonFlexMaterials"],
                   "hydros": ["id1:", "id2:"]}
        if sec not in headers:
            raise EditError("part %s has no %s table" % (pname, sec))
        p = find_part(doc, pname)
        ind = (doc.indent_of(p.items[-1][1]) if p.items else doc.indent_of(p.start) + "    ")
        nl = doc.nl
        txt = '"%s": [%s%s    %s,%s%s],' % (sec, nl, ind, dumps_inline(headers[sec]), nl, ind)
        if p.items:
            last = p.items[-1][2]
            pos = doc.after_comma(last.end)
            edits = [] if pos != last.end else [(last.end, last.end, ",")]
            eol = doc.line_end(last.end)
            if eol > 0 and doc.text[eol - 1] == "\r":
                eol -= 1
            edits.append((eol, eol, nl + ind + txt))
        else:
            edits = [(p.start + 1, p.start + 1, nl + ind + txt + nl)]
        doc.add(edits)
        doc.flush()

    def _file_new(self, op):
        root = self.model.ws.root_by_id(op["root"])
        if root is None or not root.editable:
            raise EditError("that mod folder can't be written to")
        rel = op["rel"].replace("\\", "/").lstrip("/")
        if ".." in rel.split("/") or not rel.lower().startswith("vehicles/"):
            raise EditError("new files must go under vehicles/")
        path = os.path.join(root.path, *rel.split("/"))
        if os.path.exists(path) and not op.get("overwrite"):
            raise EditError("%s already exists" % rel)
        self.new_files[path] = op["text"]

    def commit(self, label="edit"):
        """Validate and write every changed file. Returns the list of written paths."""
        outputs = {}
        for fkey, doc in self.docs.items():
            doc.flush()
            if doc.text == self.originals[fkey]:
                continue
            try:
                jbeamfile.loads(doc.text)
            except jbeamfile.ParseError as e:
                raise EditError("the edit would break %s (%s); nothing was saved" % (self.model.files[fkey].rel, e))
            fr = self.model.files[fkey]
            outputs[fr.root.full_path(fr.rel)] = (doc.text, fr.root.label, fr.rel)
        for path, text in self.new_files.items():
            if path.lower().endswith((".jbeam", ".pc", ".json")):
                try:
                    jbeamfile.loads(text)
                except jbeamfile.ParseError as e:
                    raise EditError("new file %s doesn't parse (%s)" % (os.path.basename(path), e))
            outputs[path] = (text, None, None)
        stamp = time.strftime("%Y-%m-%d_%H-%M-%S")
        written = []
        for path, (text, label_root, rel) in outputs.items():
            if os.path.exists(path) and label_root:
                dst = os.path.join(self.backup_dir, _safe(label_root), stamp, *rel.split("/"))
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                shutil.copy2(path, dst)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            tmp = path + ".jbeam-editor-tmp"
            with open(tmp, "w", encoding="utf-8", newline="") as f:
                f.write(text)
            os.replace(tmp, path)
            written.append(path)
        return written


def _safe(name):
    return "".join(c if c.isalnum() or c in "-_ ." else "_" for c in name)


def part_text(name, data, nl="\n"):
    """'"name": { ... },' in the usual JBeam layout."""
    txt = jbeamfile.dumps_part_file({name: data})
    lines = txt.strip().splitlines()[1:-1]  # drop the outer { }
    return nl.join(lines)
