"""Make a changed copy of a part, working on its text so the layout and comments stay.

    c = PartCopy(model, "pickup_suspension_F", "pickup_suspension_F_lifted")
    c.set(["information", "name"], "Front Suspension (4 in lift)")
    c.wrap_tables({"nodeMove7": {"x": 0, "y": 0, "z": -0.1}}, {"nodeMove7": ""})
    c.slot_options(None, {"nodeMove7": {"x": 0, "y": 0, "z": -0.1}})
    chunk = c.chunk()       # '"pickup_suspension_F_lifted": {...}'
"""
import json

from . import jbeamfile, patch
from .jbeamfile import dumps_inline


class PartCopy:
    def __init__(self, model, source, new_name):
        part = model.parts[source]
        text = jbeamfile.decode(part.file.root.read(part.file.rel))
        node = jbeamfile.parse_spans(text).get(part.name)
        if node is None:
            raise ValueError("part %s not found in its file" % source)
        body = text[node.start:node.end]
        self.source = source
        self.name = new_name
        self.doc = patch.TextDoc('{\n%s: %s\n}\n' % (json.dumps(new_name), body))

    # -- reading -------------------------------------------------------------------------
    def part(self):
        return patch.find_part(self.doc, self.name)

    def get(self, path):
        cur = self.part()
        for k in path:
            if cur is None:
                return None
            if cur.kind == "obj":
                cur = cur.get(k)
            elif cur.kind == "arr" and isinstance(k, int) and 0 <= k < len(cur.items):
                cur = cur.items[k]
            else:
                return None
        return cur.to_py() if cur is not None else None

    def sections(self):
        """Names of the part's table sections (a list whose first row is a header list)."""
        out = []
        for k, _, v in self.part().items:
            if v.kind == "arr" and v.items and v.items[0].kind == "arr":
                out.append(k)
        return out

    # -- editing -------------------------------------------------------------------------
    def _apply(self, edits):
        if edits:
            self.doc.add(edits)
            self.doc.flush()

    def set(self, path, value):
        self._apply(patch.edits_set_path(self.doc, self.part(), path, value))

    def wrap_tables(self, first_row, last_row, skip=("slots", "slots2", "variables")):
        """Put a modifier row right after the header of every table and another one at its end,
        the way the game passes a slot's options to the part in it."""
        for sec in self.sections():
            if sec in skip:
                continue
            node = self.part().get(sec)
            self._apply(patch.edits_insert_row(self.doc, node, first_row, anchor=node.items[0]))
            node = self.part().get(sec)
            self._apply(patch.edits_insert_row(self.doc, node, last_row, anchor=node.items[-1]))

    def slot_rows(self):
        """[(section name, header length, row node, slot id)] for every slot of the part."""
        out = []
        p = self.part()
        for sec in ("slots", "slots2"):
            node = p.get(sec)
            if node is None or node.kind != "arr" or not node.items:
                continue
            rows = [r for r in node.items if r.kind == "arr"]
            if not rows:
                continue
            first = rows[0].items[0].value if rows[0].items and rows[0].items[0].kind == "str" else None
            if first in ("type", "name"):
                h = len(rows[0].items)
                rows = rows[1:]
            else:
                h = 3 if sec == "slots" else 5
            for r in rows:
                if r.items and r.items[0].kind == "str":
                    out.append((sec, h, r, r.items[0].value))
        return out

    def slot_options(self, slot_id, updates):
        """Add options to one slot (or to all slots when slot_id is None). Returns how many."""
        n = 0
        for _ in range(1000):
            hit = next(((h, r) for _sec, h, r, sid in self.slot_rows()
                        if (slot_id is None or sid == slot_id) and not _has_all(r, h, updates)), None)
            if hit is None:
                break
            self._apply(row_options(self.doc, hit[1], hit[0], updates))
            n += 1
        return n

    def table_rows(self, sec):
        """(header length, [data row nodes], [modifier dict nodes]) of a table section."""
        node = self.part().get(sec)
        if node is None or node.kind != "arr" or not node.items or node.items[0].kind != "arr":
            return 0, [], []
        h = len(node.items[0].items)
        return h, [r for r in node.items[1:] if r.kind == "arr"], [r for r in node.items[1:] if r.kind == "obj"]

    def map_modifiers(self, sec, key, fn):
        """Replace the value of `key` in every modifier row of a table: new = fn(old). Returns how many."""
        n = 0
        h, _, mods = self.table_rows(sec)
        for i in range(len(mods)):
            _, _, mods = self.table_rows(sec)
            v = mods[i].get(key)
            if v is None:
                continue
            new = fn(v.to_py())
            if new is None:
                continue
            self._apply([(v.start, v.end, dumps_inline(new, 6))])
            n += 1
        return n

    def map_row_options(self, sec, fn):
        """For every data row of a table: updates = fn(current inline options dict) is merged in."""
        h, rows, _ = self.table_rows(sec)
        for i in range(len(rows)):
            h, rows, _ = self.table_rows(sec)
            r = rows[i]
            cur = r.items[h].to_py() if len(r.items) == h + 1 and r.items[h].kind == "obj" else {}
            upd = fn(r.to_py(), cur)
            if upd:
                self._apply(row_options(self.doc, r, h, upd, pad=sec == "flexbodies"))

    def map_numbers(self, path, col, fn):
        """For a table inside a dict (like mainEngine.torque): number in column col of each data row = fn(row)."""
        node = self.part()
        for k in path:
            node = node.get(k) if node is not None and node.kind == "obj" else None
        if node is None or node.kind != "arr":
            return 0
        n = 0
        for r in node.items[1:]:
            if r.kind == "arr" and len(r.items) > col and r.items[col].kind == "num":
                new = fn([x.to_py() for x in r.items])
                if new is not None:
                    self.doc.add([(r.items[col].start, r.items[col].end, jbeamfile.fmt_num(new, 3))])
                    n += 1
        self.doc.flush()
        return n

    def append_rows(self, path, rows):
        """Add rows at the end of a table inside a dict (like mainEngine.torque)."""
        node = self.part()
        for k in path:
            node = node.get(k) if node is not None and node.kind == "obj" else None
        if node is None or node.kind != "arr" or not node.items:
            return
        for row in rows:
            node = self.part()
            for k in path:
                node = node.get(k)
            self._apply(patch.edits_insert_row(self.doc, node, row, anchor=node.items[-1]))

    # -- result ------------------------------------------------------------------------------
    def chunk(self):
        """'"name": {...}' for a .jbeam file."""
        p = self.part()
        return "%s: %s" % (json.dumps(self.name), self.doc.text[p.start:p.end])

    def data(self):
        v = jbeamfile.loads(self.doc.text)
        return v[self.name]


def _has_all(row, h, updates):
    if len(row.items) == h + 1 and row.items[h].kind == "obj":
        cur = row.items[h].to_py()
        return all(cur.get(k) == v for k, v in updates.items())
    return False


def row_options(doc, row, h, updates, pad=False):
    """Edits that merge `updates` into a table row's inline options.

    pad: rows shorter than the header get empty lists for the missing cells
    first (flexbody rows often leave out nonFlexMaterials)."""
    items = row.items
    if len(items) == h + 1 and items[h].kind == "obj":
        cur = items[h].to_py()
        cur.update(updates)
        return [(items[h].start, items[h].end, dumps_inline(cur))]
    if len(items) == h:
        return [(items[-1].end, items[-1].end, ", " + dumps_inline(updates))]
    if pad and 0 < len(items) < h:
        return [(items[-1].end, items[-1].end, ", []" * (h - len(items)) + ", " + dumps_inline(updates))]
    raise patch.EditError("row %s has %d cells but the header has %d" % (dumps_inline(row.to_py())[:60], len(items), h))


def file_text(chunks, comment=None):
    """A .jbeam file made of part chunks."""
    lines = []
    for c in (comment or "").splitlines():
        lines.append("// " + c)
    lines.append("{")
    lines.append(",\n".join(chunks))
    lines.append("}")
    text = "\n".join(lines) + "\n"
    jbeamfile.loads(text)
    return text
