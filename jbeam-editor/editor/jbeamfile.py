"""Reading and writing BeamNG's relaxed JSON (.jbeam, .pc, info .json).

The game accepts // and /* */ comments, trailing commas, missing commas,
numbers with a leading + and even "key":,{...}. Python's json module accepts
none of that, so:

* loads() turns the text into strict JSON with one regex pass and hands it to
  the json module (fast enough for the ~900 files in common.zip). If that
  fails it reruns the slow hand-written parser to get the exact line/col.
* parse_spans() builds a tree that remembers where every value sits in the
  text. The save code uses it to change only the characters it has to, so
  comments and the author's formatting survive an edit.
"""
import json
import re

# ---------------------------------------------------------------------------
# Fast loader
# ---------------------------------------------------------------------------

_TOKEN_RE = re.compile(
    r'"(?:[^"\\\n]|\\.)*"'              # string (a raw line break ends it: error)
    r'|//[^\n]*|/\*[\s\S]*?\*/'         # comments
    r'|[ \t\r\n,]+'                     # whitespace and commas (we re-add commas)
    r'|[\[\]{}:]'
    r'|[-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?'
    r'|true|false|null'
    r'|.', re.S)


class ParseError(Exception):
    def __init__(self, msg, line=0, col=0):
        super().__init__("%s (line %d, col %d)" % (msg, line, col) if line else msg)
        self.msg, self.line, self.col = msg, line, col


def _to_strict(text):
    out = []
    app = out.append
    need_comma = False
    for m in _TOKEN_RE.finditer(text):
        t = m.group()
        c = t[0]
        if c in " \t\r\n,":
            continue
        if c == "/" and len(t) > 1 and t[1] in "/*":
            continue
        if c == "]" or c == "}":
            app(t)
            need_comma = True
            continue
        if c == ":":
            app(t)
            need_comma = False
            continue
        if need_comma:
            app(",")
        if c == "[" or c == "{":
            app(t)
            need_comma = False
            continue
        if c == '"' or c == "t" or c == "f" or c == "n":
            if c != '"' and t not in ("true", "false", "null"):
                raise ValueError(m.start())
            app(t)
        elif c in "+-.0123456789":
            if c == "+":
                t = t[1:]
            if "." in t:
                if t.startswith(".") or t.startswith("-."):
                    t = t.replace(".", "0.", 1)
                if t.endswith(".") or ".e" in t or ".E" in t:
                    t = t.replace(".", ".0", 1)
            app(t)
        else:
            raise ValueError(m.start())
        need_comma = True
    return "".join(out)


def loads(text):
    """Parse relaxed JSON text. Raises ParseError with line/col on bad input."""
    if text.startswith("﻿"):
        text = text[1:]
    try:
        return json.loads(_to_strict(text), strict=False)
    except (ValueError, RecursionError):
        pass
    # slow path: precise error message (or a value if the fast path was too strict)
    return SpanParser(text).parse().to_py()


def load_bytes(data):
    return loads(decode(data))


def decode(data):
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError:
        return data.decode("latin-1")


# ---------------------------------------------------------------------------
# Span parser
# ---------------------------------------------------------------------------

_NUM_RE = re.compile(r"[-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?")


class SNode:
    """A value in the text. kind: obj, arr, str, num, lit.

    start/end are character offsets of the value itself. For obj, items is a
    list of (key, key_start, value SNode); for arr, a list of SNodes.
    """
    __slots__ = ("kind", "start", "end", "value", "items")

    def __init__(self, kind, start, end=0, value=None, items=None):
        self.kind, self.start, self.end, self.value, self.items = kind, start, end, value, items

    def to_py(self):
        if self.kind == "obj":
            return {k: v.to_py() for k, _, v in self.items}
        if self.kind == "arr":
            return [v.to_py() for v in self.items]
        return self.value

    def get(self, key):
        if self.kind != "obj":
            return None
        found = None
        for k, _, v in self.items:
            if k == key:
                found = v  # last one wins, like the game
        return found

    def is_dict(self):
        return self.kind == "obj"


class SpanParser:
    def __init__(self, text):
        self.t = text
        self.i = 0
        self.n = len(text)

    def pos(self, i):
        line = self.t.count("\n", 0, i) + 1
        col = i - (self.t.rfind("\n", 0, i) + 1) + 1
        return line, col

    def err(self, msg, i=None):
        j = self.i if i is None else i
        if j < self.n and ord(self.t[j]) < 32 and self.t[j] not in "\t\r\n":
            msg += " - an invisible control character; delete it"
        raise ParseError(msg, *self.pos(j))

    def skip(self, commas=False):
        t, n = self.t, self.n
        while self.i < n:
            c = t[self.i]
            if c in " \t\r\n﻿" or (commas and c == ","):
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
            s = self.i
            v = self.string()
            return SNode("str", s, self.i, v)
        for word, val in (("true", True), ("false", False), ("null", None)):
            if self.t.startswith(word, self.i):
                s = self.i
                self.i += len(word)
                return SNode("lit", s, self.i, val)
        m = _NUM_RE.match(self.t, self.i)
        if m and m.end() > self.i:
            s = self.i
            self.i = m.end()
            txt = m.group(0)
            v = float(txt) if any(ch in txt for ch in ".eE") else int(txt)
            return SNode("num", s, self.i, v)
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
        node = SNode("arr", self.i, items=[])
        self.i += 1
        while True:
            self.skip(commas=True)
            if self.i >= self.n:
                self.err("unclosed [ list", node.start)
            if self.t[self.i] == "]":
                self.i += 1
                node.end = self.i
                return node
            if self.t[self.i] in "}:":
                self.err("unexpected %r inside a [ list" % self.t[self.i])
            node.items.append(self.value())

    def obj(self):
        node = SNode("obj", self.i, items=[])
        self.i += 1
        while True:
            self.skip(commas=True)
            if self.i >= self.n:
                self.err("unclosed { object", node.start)
            c = self.t[self.i]
            if c == "}":
                self.i += 1
                node.end = self.i
                return node
            if c != '"':
                self.err("expected a \"quoted key\" or }, got %r" % c)
            kpos = self.i
            k = self.string()
            self.skip()
            if self.i >= self.n or self.t[self.i] != ":":
                self.err("expected ':' after key %r" % k)
            self.i += 1
            self.skip(commas=True)
            node.items.append((k, kpos, self.value()))


def parse_spans(text):
    return SpanParser(text).parse()


# ---------------------------------------------------------------------------
# Writing values in JBeam style
# ---------------------------------------------------------------------------

def fmt_num(v, digits=4):
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, int):
        return str(v)
    if v != v:
        return "0"
    if v in (float("inf"), float("-inf")):
        return '"FLT_MAX"' if v > 0 else '"MINUS_FLT_MAX"'
    if abs(v) >= 1e15 or (v != 0 and abs(v) < 10 ** -digits):
        return repr(float(v))
    s = ("%." + str(digits) + "f") % v
    if "." in s:
        s = s.rstrip("0").rstrip(".")
    if s in ("-0", ""):
        s = "0"
    return s


def dumps_inline(v, digits=4):
    """One-line JBeam text for a value: ["a", 0.5, {"k": 1}]."""
    if v is None:
        return "null"
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return fmt_num(v, digits)
    if isinstance(v, str):
        return json.dumps(v, ensure_ascii=False)
    if isinstance(v, dict):
        return "{" + ", ".join("%s: %s" % (json.dumps(k, ensure_ascii=False), dumps_inline(x, digits))
                               for k, x in v.items()) + "}"
    if isinstance(v, (list, tuple)):
        return "[" + ", ".join(dumps_inline(x, digits) for x in v) + "]"
    return json.dumps(v)


def dumps_part_file(parts, comment=None):
    """A whole .jbeam file in the usual layout: tables one row per line."""
    lines = []
    if comment:
        for c in comment.splitlines():
            lines.append("// " + c)
    lines.append("{")
    for pname, part in parts.items():
        lines.append("%s: {" % json.dumps(pname))
        for key, val in part.items():
            lines.append(_dump_section(key, val, "    "))
        lines.append("},")
    lines.append("}")
    return "\n".join(lines) + "\n"


def _dump_section(key, val, ind):
    k = json.dumps(key)
    if isinstance(val, list) and val and all(isinstance(r, (list, dict)) for r in val):
        out = ["%s%s: [" % (ind, k)]
        for row in val:
            out.append("%s    %s," % (ind, dumps_inline(row)))
        out.append("%s]," % ind)
        return "\n".join(out)
    if isinstance(val, dict) and len(dumps_inline(val)) > 90:
        out = ["%s%s: {" % (ind, k)]
        for k2, v2 in val.items():
            out.append(_dump_section(k2, v2, ind + "    "))
        out.append("%s}," % ind)
        return "\n".join(out)
    return "%s%s: %s," % (ind, k, dumps_inline(val))


def dumps_json(v):
    """Plain JSON with 2-space indent (for .pc and info.json files)."""
    return json.dumps(v, indent=2, ensure_ascii=False) + "\n"
