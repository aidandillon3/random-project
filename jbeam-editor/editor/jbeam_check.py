#!/usr/bin/env python3
"""jbeam_check.py - offline checker for BeamNG.drive JBeam mods.

It cannot run the physics. It catches the mistakes that make BeamNG refuse a
part or spam beamng.log: files that don't parse, parts without slotType or
information, bad table rows, links to nodes that don't exist, slot defaults
and .pc parts that point at nothing, undefined $variables, duplicate names.

Usage (Python 3.8+, standard library only):

    python jbeam_check.py PATH [PATH ...] [--ref PATH ...] [--quiet] [--json]

PATH   a mod folder (e.g. mods/unpacked/my_mod), a vehicle folder, a single
       .jbeam/.pc/.json file, or a .zip.
--ref  extra folders or .zip files whose parts and nodes count as "known" but
       are not themselves reported on. Pass the game's vehicle zips when your
       mod adds parts to an existing car, e.g.
       --ref "<BeamNG>/content/vehicles/covet.zip" --ref "<BeamNG>/content/vehicles/common.zip"

Exit code: 0 = no errors (warnings allowed), 1 = errors found, 2 = bad usage.
"""
import argparse
import io
import json
import os
import re
import sys
import zipfile
from collections import defaultdict

# ----------------------------------------------------------------------------
# Relaxed JSON parser. BeamNG's reader accepts // and /* */ comments, trailing
# commas and even MISSING commas between values. Python's json module rejects
# all three, so this is a small hand-written parser. It also takes numbers
# written with a leading + and a stray comma after a key's colon, which real
# game files contain.
# ----------------------------------------------------------------------------


class ParseError(Exception):
    def __init__(self, msg, line, col):
        super().__init__("%s (line %d, col %d)" % (msg, line, col))
        self.line, self.col = line, col


_NUM_RE = re.compile(r"[-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?")


class RelaxedJson:
    def __init__(self, text):
        if text.startswith("﻿"):
            text = text[1:]
        self.t = text
        self.i = 0
        self.n = len(text)

    def _pos(self, i=None):
        i = self.i if i is None else i
        line = self.t.count("\n", 0, i) + 1
        col = i - (self.t.rfind("\n", 0, i) + 1) + 1
        return line, col

    def err(self, msg, i=None):
        j = self.i if i is None else i
        if j < self.n and ord(self.t[j]) < 32 and self.t[j] not in "\t\r\n":
            msg += " - an invisible control character (often pasted in by accident); delete it"
        line, col = self._pos(i)
        raise ParseError(msg, line, col)

    def skip(self, commas=False):
        t, n = self.t, self.n
        while self.i < n:
            c = t[self.i]
            if c in " \t\r\n" or (commas and c == ","):
                self.i += 1
            elif c == "/" and self.i + 1 < n and t[self.i + 1] == "/":
                j = t.find("\n", self.i)
                self.i = n if j < 0 else j + 1
            elif c == "/" and self.i + 1 < n and t[self.i + 1] == "*":
                j = t.find("*/", self.i + 2)
                if j < 0:
                    self.err("unclosed /* comment")
                self.i = j + 2
            else:
                break

    def parse(self):
        self.skip()
        v = self.value()
        self.skip(commas=True)
        if self.i < self.n:
            self.err("unexpected text after the end of the file")
        return v

    def value(self):
        self.skip()
        if self.i >= self.n:
            self.err("unexpected end of file")
        c = self.t[self.i]
        if c == "{":
            return self.obj()
        if c == "[":
            return self.arr()
        if c == '"':
            return self.string()
        for word, val in (("true", True), ("false", False), ("null", None)):
            if self.t.startswith(word, self.i):
                self.i += len(word)
                return val
        m = _NUM_RE.match(self.t, self.i)
        if m and m.end() > self.i:
            self.i = m.end()
            s = m.group(0)
            return float(s) if any(ch in s for ch in ".eE") else int(s)
        self.err("unexpected character %r" % c)

    def string(self):
        start = self.i
        self.i += 1
        out = []
        t = self.t
        while True:
            if self.i >= self.n:
                self.err("unclosed string", start)
            c = t[self.i]
            if c == '"':
                self.i += 1
                return "".join(out)
            if c == "\\":
                self.i += 1
                e = t[self.i:self.i + 1]
                if e == "u":
                    out.append(chr(int(t[self.i + 1:self.i + 5], 16)))
                    self.i += 5
                    continue
                out.append({"n": "\n", "t": "\t", "r": "\r", "b": "\b", "f": "\f"}.get(e, e))
                self.i += 1
                continue
            if c == "\n":
                self.err("line break inside a string (missing closing quote?)", start)
            out.append(c)
            self.i += 1

    def arr(self):
        self.i += 1
        out = []
        while True:
            self.skip(commas=True)
            if self.i >= self.n:
                self.err("unclosed [ list")
            if self.t[self.i] == "]":
                self.i += 1
                return out
            if self.t[self.i] in "}:":
                self.err("unexpected %r inside a [ list" % self.t[self.i])
            out.append(self.value())

    def obj(self):
        self.i += 1
        out = {}
        # remember the order keys appeared in; dicts keep insertion order
        while True:
            self.skip(commas=True)
            if self.i >= self.n:
                self.err("unclosed { object")
            c = self.t[self.i]
            if c == "}":
                self.i += 1
                return out
            if c != '"':
                self.err("expected a \"quoted key\" or }, got %r" % c)
            kpos = self.i
            k = self.string()
            self.skip()
            if self.i >= self.n or self.t[self.i] != ":":
                self.err("expected ':' after key %r" % k)
            self.i += 1
            self.skip(commas=True)  # the game even accepts "key":,{...}
            v = self.value()
            if k in out:
                # BeamNG keeps the last one silently; usually a copy-paste mistake
                dup = getattr(self, "dups", [])
                dup.append((k, self._pos(kpos)[0]))
                self.dups = dup
            out[k] = v


def parse_relaxed(text):
    p = RelaxedJson(text)
    v = p.parse()
    return v, getattr(p, "dups", [])


# ----------------------------------------------------------------------------
# Loading files from folders and zips
# ----------------------------------------------------------------------------

EXTS = (".jbeam", ".pc", ".json")


def iter_files(path):
    """Yield (display_name, text) for .jbeam/.pc/info*.json files under path."""
    if os.path.isfile(path) and path.lower().endswith(".zip"):
        with zipfile.ZipFile(path) as z:
            for name in z.namelist():
                low = name.lower()
                if low.endswith(".jbeam") or low.endswith(".pc") or re.search(r"(^|/)info[^/]*\.json$", low):
                    yield "%s!%s" % (path, name), z.read(name).decode("utf-8", "replace")
        return
    if os.path.isfile(path):
        with io.open(path, encoding="utf-8", errors="replace") as f:
            yield path, f.read()
        return
    for root, dirs, files in os.walk(path):
        dirs.sort()
        for fn in sorted(files):
            low = fn.lower()
            if low.endswith(".jbeam") or low.endswith(".pc") or re.match(r"info[^/]*\.json$", low):
                p = os.path.join(root, fn)
                with io.open(p, encoding="utf-8", errors="replace") as f:
                    yield p, f.read()


# ----------------------------------------------------------------------------
# The checker
# ----------------------------------------------------------------------------

SECTION_NAME_RE = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*$")
PART_NAME_RE = re.compile(r"^[A-Za-z0-9_.\-]+$")
VAR_REF_RE = re.compile(r"\$([A-Za-z_][A-Za-z0-9_]*)")
SPECIAL_STRINGS = {"FLT_MAX", "MINUS_FLT_MAX"}
# dict-valued keys whose node links are written with a trailing ':' on the key
NOT_NODE_LINK_KEYS = {"[group]:", "[engineGroup]:", "broken:"}
# pressureWheels and friends use 9999 for "no node"
NO_NODE = {9999, "9999", ""}
# variables the game itself provides to every vehicle (not defined in jbeam)
BUILTIN_VARS = {"components"}
BEAM_TYPES = {"|NORMAL", "|BOUNDED", "|SUPPORT", "|ANISOTROPIC", "|TIRESIDE", "|LBEAM", "|PRESSURED", "|HYDRO", ""}
# list sections that are NOT tables (no header row)
NON_TABLE_LISTS = {"slotType", "energyStorage", "deformGroups", "torqueReactionNodes:"}


class Checker:
    def __init__(self):
        self.parts = {}            # partName -> (file, part dict)
        self.ref_parts = {}        # same, from --ref
        self.nodes = defaultdict(set)      # nodeId -> set of partNames (checked + ref)
        self.node_links = []       # (file, partName, section, nodeId)
        self.var_defs = set()
        self.var_uses = []         # (file, partName, varName)
        self.pcs = []              # (file, data)
        self.errors = []
        self.warnings = []

    def E(self, where, msg):
        self.errors.append("%s: %s" % (where, msg))

    def W(self, where, msg):
        self.warnings.append("%s: %s" % (where, msg))

    # --- loading -----------------------------------------------------------
    def load(self, path, is_ref=False):
        for name, text in iter_files(path):
            low = name.lower()
            try:
                data, dups = parse_relaxed(text)
            except ParseError as e:
                if is_ref:
                    continue
                self.E(name, "does not parse: %s" % e)
                continue
            if not is_ref:
                for k, line in dups:
                    self.W(name, "key %r appears twice in one object (line %d); the last one wins" % (k, line))
            if low.endswith(".jbeam"):
                self.load_jbeam(name, data, is_ref)
            elif low.endswith(".pc"):
                if not is_ref:
                    self.pcs.append((name, data))
            else:
                if not is_ref:
                    self.check_info(name, data)

    def load_jbeam(self, fname, data, is_ref):
        if not isinstance(data, dict):
            if not is_ref:
                self.E(fname, "the top level must be { \"partName\": {...}, ... }")
            return
        for pname, part in data.items():
            where = "%s [%s]" % (fname, pname)
            if not isinstance(part, dict):
                if not is_ref:
                    self.E(where, "a part must be an object {...}")
                continue
            st = part.get("slotType")
            if not isinstance(st, (str, list)):
                if not is_ref:
                    self.E(where, "no slotType: BeamNG ignores this whole part")
                continue
            target = self.ref_parts if is_ref else self.parts
            if pname in self.parts or (not is_ref and pname in self.ref_parts):
                if not is_ref:
                    other = (self.parts.get(pname) or self.ref_parts.get(pname))[0]
                    self.E(where, "part name already used in %s (duplicate part names clash; pick a unique name)" % other)
            target[pname] = (fname, part)
            self.collect(fname, pname, part, is_ref)
            if not is_ref:
                self.check_part(where, pname, part)

    # --- collecting nodes / links / variables ------------------------------
    def collect(self, fname, pname, part, is_ref):
        for sec, val in part.items():
            if sec == "variables" and isinstance(val, list) and val and isinstance(val[0], list):
                for row in val[1:]:
                    if isinstance(row, list) and row and isinstance(row[0], str):
                        self.var_defs.add(row[0].lstrip("$"))
            if sec in ("slots", "slots2") and isinstance(val, list):
                # slot options can hand variables down: {"variables":{"$x":1}}
                for row in val[1:]:
                    if isinstance(row, list) and row and isinstance(row[-1], dict):
                        for k in (row[-1].get("variables") or {}):
                            self.var_defs.add(str(k).lstrip("$"))
            if isinstance(val, list) and val and isinstance(val[0], list) and all(isinstance(h, str) for h in val[0]):
                self.collect_table(fname, pname, sec, val, is_ref)
            elif isinstance(val, dict):
                self.collect_dict_links(fname, pname, sec, val, is_ref)
        if not is_ref:
            self.collect_var_uses(fname, pname, part)

    def collect_table(self, fname, pname, sec, rows, is_ref):
        header = rows[0]
        if sec == "nodes":
            idcol = header.index("id") if "id" in header else 0
            for row in rows[1:]:
                if isinstance(row, list) and len(row) > idcol and isinstance(row[idcol], str):
                    self.nodes[row[idcol]].add(pname)
        if is_ref:
            return
        linkcols = [i for i, h in enumerate(header) if h.endswith(":") and not h.startswith("[")]
        optional = False  # rows under {"optional":true} may link to nodes that aren't there
        for row in rows[1:]:
            if isinstance(row, list):
                opts = row[-1] if len(row) == len(header) + 1 and isinstance(row[-1], dict) else {}
                if optional or opts.get("optional") is True:
                    continue
                for i in linkcols:
                    if i < len(row):
                        self.add_link(fname, pname, sec, row[i])
                if opts:
                    self.collect_dict_links(fname, pname, sec, opts, is_ref, shallow=True)
            elif isinstance(row, dict):
                if "optional" in row:
                    optional = row["optional"] is True
                self.collect_dict_links(fname, pname, sec, row, is_ref, shallow=True)

    def collect_dict_links(self, fname, pname, sec, d, is_ref, shallow=False):
        if is_ref:
            return
        for k, v in d.items():
            if isinstance(k, str) and k.endswith(":") and k not in NOT_NODE_LINK_KEYS and not k.startswith("["):
                vals = v if isinstance(v, list) else [v]
                for x in vals:
                    self.add_link(fname, pname, "%s.%s" % (sec, k), x)
            elif isinstance(v, dict) and not shallow:
                self.collect_dict_links(fname, pname, sec, v, is_ref)

    def add_link(self, fname, pname, sec, nid):
        if isinstance(nid, str) and nid and not nid.startswith("$") and nid not in NO_NODE:
            self.node_links.append((fname, pname, sec, nid))

    def collect_var_uses(self, fname, pname, part):
        def walk(v):
            if isinstance(v, str):
                if v.startswith("$") and not v.startswith("$>>"):
                    for m in VAR_REF_RE.finditer(v):
                        yield m.group(1)
            elif isinstance(v, list):
                for x in v:
                    yield from walk(x)
            elif isinstance(v, dict):
                for x in v.values():
                    yield from walk(x)
        for sec, val in part.items():
            if sec == "variables":
                # defaults/min/max may themselves be expressions, still check them
                if isinstance(val, list):
                    for row in val[1:]:
                        if isinstance(row, list):
                            for x in row[4:]:
                                for name in walk(x):
                                    self.var_uses.append((fname, pname, name))
                continue
            for name in walk(val):
                self.var_uses.append((fname, pname, name))

    # --- per-part structure checks ----------------------------------------
    def check_part(self, where, pname, part):
        if not PART_NAME_RE.match(pname):
            self.W(where, "part name has unusual characters; stick to letters, digits, _ - .")
        info = part.get("information")
        if not isinstance(info, dict):
            self.E(where, "no \"information\" object (the loader reads information.name; a missing one breaks loading)")
        elif "name" not in info:
            self.W(where, "information has no \"name\" (shown in the parts menu)")
        if "slots" in part and "slots2" in part:
            self.W(where, "has both slots and slots2; the game uses slots2 and ignores slots")
        for sec, val in part.items():
            if not SECTION_NAME_RE.match(sec):
                self.W(where, "section name %r is not a valid identifier; it is skipped" % sec)
            if isinstance(val, list) and sec not in NON_TABLE_LISTS:
                self.check_table(where, sec, val)
        for sk in ("slots", "slots2"):
            if isinstance(part.get(sk), list):
                self.check_slots_header(where, sk, part[sk])
        self.check_flag_values(where, part)

    def check_flag_values(self, where, part):
        """beamType / nodeMaterial strings are looked up case-sensitively; a typo silently becomes 0 (NORMAL / default)."""
        bad = set()

        def walk(v):
            if isinstance(v, dict):
                for k, x in v.items():
                    if k == "beamType" and isinstance(x, str) and x not in BEAM_TYPES and not x.startswith("$"):
                        bad.add("beamType %r (valid: %s)" % (x, ", ".join(sorted(BEAM_TYPES - {""}))))
                    elif k in ("nodeMaterial", "hubNodeMaterial") and isinstance(x, str) and x and not x.startswith("$") and not re.match(r"^\|NM_[A-Z_]+$", x):
                        bad.add("%s %r (write it like \"|NM_METAL\")" % (k, x))
                    walk(x)
            elif isinstance(v, list):
                for x in v:
                    walk(x)
        walk(part)
        for b in sorted(bad):
            self.W(where, "unknown " + b + "; the game silently treats it as the default")

    def check_table(self, where, sec, rows):
        if not rows:
            return
        header = rows[0]
        if not isinstance(header, list):
            if all(isinstance(x, str) for x in rows):
                return  # plain list of strings, e.g. a list value, fine
            self.W("%s.%s" % (where, sec), "list section does not start with a header row like [\"id\", ...]")
            return
        if not all(isinstance(h, str) for h in header):
            return  # e.g. a numeric matrix like torque curves inside dicts (not top level), ignore
        hl = len(header)
        for idx, row in enumerate(rows[1:], start=1):
            w = "%s.%s row %d" % (where, sec, idx)
            if isinstance(row, dict):
                continue  # modifier row
            if isinstance(row, str):
                if row != "":
                    self.W(w, "a bare string row %r is ignored" % row)
                continue
            if not isinstance(row, list):
                self.E(w, "row must be a [list] or a {modifier}, got %r" % (row,))
                continue
            n = len(row)
            if n > hl + 1:
                self.E(w, "has %d cells but the header has %d (only one extra {options} cell is allowed)" % (n, hl))
            elif n == hl + 1 and not isinstance(row[-1], dict):
                self.E(w, "has one cell more than the header and the extra cell is not an {options} object")
            elif n < hl:
                # trailing cells may be left out in some sections, but usually it's a mistake
                if sec in ("nodes", "beams", "triangles", "quads", "torsionbars", "hydros", "slidenodes", "refNodes", "flexbodies", "props", "pressureWheels"):
                    if not (sec == "flexbodies" and n >= 2) and not (sec == "props" and n >= 5):
                        self.W(w, "has %d cells but the header has %d" % (n, hl))
            if sec == "nodes" and n >= 4:
                for c in row[1:4]:
                    if not isinstance(c, (int, float)) and not (isinstance(c, str) and c.startswith("$")):
                        self.E(w, "node position must be numbers, got %r" % (c,))
                        break

    def check_slots_header(self, where, sk, rows):
        if not rows or not isinstance(rows[0], list):
            return
        h = rows[0]
        want = ["type", "default", "description"] if sk == "slots" else ["name", "allowTypes", "denyTypes", "default", "description"]
        if h[:len(want)] != want:
            self.W("%s.%s" % (where, sk), "header is %r; the game expects %r" % (h, want))

    def check_beam_rows(self, fname, pn, part):
        """Same node twice in one beam/triangle, and duplicate beams (the game warns)."""
        where = "%s [%s]" % (fname, pn)
        for sec, n in (("beams", 2), ("triangles", 3), ("quads", 4), ("torsionbars", 4)):
            rows = part.get(sec)
            if not (isinstance(rows, list) and rows and isinstance(rows[0], list)):
                continue
            mods = {}
            seen = {}
            for row in rows[1:]:
                if isinstance(row, dict):
                    mods.update(row)
                    continue
                if not isinstance(row, list) or len(row) < n:
                    continue
                ids = row[:n]
                if not all(isinstance(x, str) for x in ids):
                    continue
                if len(set(ids)) < n:
                    self.E(where, "%s row %s uses the same node twice (degenerate)" % (sec, ids))
                if sec != "beams":
                    continue
                opts = row[n] if len(row) > n and isinstance(row[n], dict) else {}
                eff = dict(mods)
                eff.update(opts)
                btype = eff.get("beamType", "|NORMAL") or "|NORMAL"
                if eff.get("optional") is True or eff.get("deformGroup") or "BOUNDED" in str(btype) or eff.get("beamSpring") == 0:
                    continue
                key = (frozenset(ids), btype)
                if key in seen:
                    self.W(where, "duplicate %s beam %s-%s (also at row %d); the game warns 'duplicated beam'" % (btype, ids[0], ids[1], seen[key]))
                else:
                    seen[key] = rows.index(row)

    # --- info.json ---------------------------------------------------------
    def check_info(self, fname, data):
        if not isinstance(data, dict):
            self.E(fname, "info file must be an object {...}")
            return
        base = os.path.basename(fname.split("!")[-1]).lower()
        if base == "info.json":
            for k in ("Name", "Type"):
                if k not in data:
                    self.W(fname, "info.json has no %r" % k)
            dp = data.get("default_pc")
            if dp:
                folder = os.path.dirname(fname.split("!")[-1])
                self._default_pcs = getattr(self, "_default_pcs", [])
                self._default_pcs.append((fname, folder, dp))

    # --- cross-file checks -------------------------------------------------
    def all_parts(self):
        d = dict(self.ref_parts)
        d.update(self.parts)
        return d

    def finish(self):
        allp = self.all_parts()
        slot_types = defaultdict(list)
        for pn, (_, part) in allp.items():
            st = part.get("slotType")
            for s in ([st] if isinstance(st, str) else st or []):
                slot_types[s].append(pn)

        # nodes: missing links, duplicates within a part
        missing = defaultdict(list)
        for fname, pname, sec, nid in self.node_links:
            if nid not in self.nodes and nid not in missing[(fname, pname)]:
                missing[(fname, pname)].append(nid)
        dynamic = any(n.startswith("$") for n in self.nodes)
        for (fname, pname), nids in missing.items():
            shown = ", ".join(nids[:8]) + (" ... (+%d more)" % (len(nids) - 8) if len(nids) > 8 else "")
            hint = "typo, or pass --ref with the base vehicle"
            if dynamic:
                hint += "; some scanned nodes are named by variables ($.x or $=...), which this checker can't expand"
            self.W("%s [%s]" % (fname, pname), "links to %d node(s) no scanned part defines: %s (%s)" % (len(nids), shown, hint))
        for pn, (fname, part) in self.parts.items():
            self.check_beam_rows(fname, pn, part)
            rows = part.get("nodes")
            if isinstance(rows, list) and rows and isinstance(rows[0], list):
                seen = set()
                for row in rows[1:]:
                    if isinstance(row, list) and row and isinstance(row[0], str):
                        if row[0] in seen:
                            self.W("%s [%s]" % (fname, pn), "node %r defined twice in this part; the later row replaces the earlier one" % row[0])
                        seen.add(row[0])

        # slots: defaults must exist and fit
        for pn, (fname, part) in self.parts.items():
            where = "%s [%s]" % (fname, pn)
            for sk in ("slots2", "slots"):
                rows = part.get(sk)
                if not (isinstance(rows, list) and rows and isinstance(rows[0], list)):
                    continue
                h = rows[0]
                for row in rows[1:]:
                    if not isinstance(row, list) or not row:
                        continue
                    r = dict(zip(h, row))
                    default = r.get("default")
                    if sk == "slots2":
                        allow = r.get("allowTypes") or []
                        allow = [allow] if isinstance(allow, str) else allow
                        sname = r.get("name")
                    else:
                        allow = [r.get("type")]
                        sname = r.get("type")
                    if not any(slot_types.get(a) for a in allow):
                        self.W(where, "slot %r: no scanned part has slotType %s (fine if the parts come from elsewhere)" % (sname, allow))
                    if isinstance(default, str) and default:
                        if default not in allp:
                            self.E(where, "slot %r default part %r does not exist in the scanned files" % (sname, default))
                        else:
                            dst = allp[default][1].get("slotType")
                            dst = [dst] if isinstance(dst, str) else (dst or [])
                            if not set(dst) & set(allow):
                                self.E(where, "slot %r default part %r has slotType %s, which the slot does not accept %s" % (sname, default, dst, allow))
                    if sk == "slots2" and sname != (allow[0] if allow else None) and len(allow) == 1:
                        pass  # allowed: slot name may differ from the type

        # main parts per vehicle folder
        mains = [pn for pn, (_, p) in self.parts.items() if p.get("slotType") == "main"]

        # .pc configs
        for fname, data in self.pcs:
            if not isinstance(data, dict):
                self.E(fname, ".pc must be an object")
                continue
            if "parts" not in data and "format" not in data:
                self.W(fname, "old format-1 config (a flat slot:part map); the game still reads it, new configs use {\"format\":2, \"model\":..., \"parts\":{...}, \"vars\":{...}}")
                data = {"format": 1, "parts": data}
            elif data.get("format") != 2:
                self.W(fname, "format is %r; current configs use \"format\": 2" % data.get("format"))
            parts = data.get("parts")
            if not isinstance(parts, dict):
                self.E(fname, "no \"parts\" object")
                continue
            for slot, part in parts.items():
                if isinstance(part, str) and part and part != "none" and part not in allp:
                    self.E(fname, "slot %r uses part %r, which does not exist in the scanned files" % (slot, part))
            for v in (data.get("vars") or {}):
                if not v.startswith("$"):
                    self.W(fname, "var %r should start with $" % v)
                elif v[1:] not in self.var_defs:
                    self.W(fname, "var %r is not defined in any scanned variables table" % v)

        for fname, folder, dp in getattr(self, "_default_pcs", []):
            names = {os.path.splitext(os.path.basename(f.split("!")[-1]))[0] for f, _ in self.pcs if os.path.dirname(f.split("!")[-1]) == folder}
            if self.pcs and dp not in names:
                self.W(fname, "default_pc %r has no matching %s.pc next to it" % (dp, dp))

        # variables
        reported = set()
        for fname, pname, name in self.var_uses:
            if name in self.var_defs or name in BUILTIN_VARS:
                continue
            key = (pname, name)
            if key in reported:
                continue
            reported.add(key)
            self.W("%s [%s]" % (fname, pname), "uses $%s but no scanned variables table defines it (it will be nil)" % name)

        return mains


def main(argv=None):
    ap = argparse.ArgumentParser(description="Offline checker for BeamNG JBeam mods.")
    ap.add_argument("paths", nargs="+")
    ap.add_argument("--ref", action="append", default=[], help="extra folders/zips whose parts and nodes are known (not reported)")
    ap.add_argument("--quiet", action="store_true", help="only print errors")
    ap.add_argument("--json", action="store_true", help="print a JSON report")
    a = ap.parse_args(argv)

    c = Checker()
    for r in a.ref:
        if not os.path.exists(r):
            print("ref path not found: %s" % r, file=sys.stderr)
            return 2
        c.load(r, is_ref=True)
    for p in a.paths:
        if not os.path.exists(p):
            print("path not found: %s" % p, file=sys.stderr)
            return 2
        c.load(p)
    c.finish()

    if a.json:
        print(json.dumps({"parts": len(c.parts), "errors": c.errors, "warnings": c.warnings}, indent=1))
    else:
        for e in c.errors:
            print("ERROR   " + e)
        if not a.quiet:
            for w in c.warnings:
                print("warning " + w)
        print("\nChecked %d parts, %d .pc files: %d errors, %d warnings." % (len(c.parts), len(c.pcs), len(c.errors), len(c.warnings)))
    return 1 if c.errors else 0


if __name__ == "__main__":
    sys.exit(main())
