"""JBeam "$=" expressions.

The game runs them as Lua: it strips "$=", turns every "$name" into a variable
lookup and evaluates "return <expr>" with math functions available without
the "math." prefix plus a few helpers. This is a small Lua expression
evaluator with the same rules (and/or return operands, only nil and false are
falsy, .. joins strings, ^ is power, ~= is not-equal).
"""
import math
import re


class ExprError(Exception):
    pass


_TOK = re.compile(r"""
    (?P<ws>\s+)
  | (?P<num>0[xX][0-9a-fA-F]+|(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?)
  | (?P<str>"(?:[^"\\]|\\.)*"|'(?:[^'\\]|\\.)*')
  | (?P<name>[A-Za-z_][A-Za-z0-9_]*)
  | (?P<op>\.\.|==|~=|<=|>=|[-+*/%^#<>(),\[\]{}.:=;])
""", re.X)

_KEYWORDS = {"and", "or", "not", "nil", "true", "false"}


def _tokenize(s):
    out = []
    i = 0
    while i < len(s):
        m = _TOK.match(s, i)
        if not m:
            raise ExprError("unexpected character %r" % s[i])
        i = m.end()
        kind = m.lastgroup
        if kind == "ws":
            continue
        v = m.group()
        if kind == "name" and v in _KEYWORDS:
            kind = "kw"
        out.append((kind, v))
    out.append(("end", ""))
    return out


# binary operator precedence (Lua 5.1): left, right binding power
_BIN = {
    "or": (1, 1), "and": (2, 2),
    "<": (3, 3), ">": (3, 3), "<=": (3, 3), ">=": (3, 3), "~=": (3, 3), "==": (3, 3),
    "..": (5, 4),  # right associative
    "+": (6, 6), "-": (6, 6),
    "*": (7, 7), "/": (7, 7), "%": (7, 7),
    "^": (10, 9),  # right associative, binds tighter than unary
}
_UNARY = 8


def _truthy(v):
    return v is not None and v is not False


def _tostr(v):
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        if isinstance(v, float) and v.is_integer() and abs(v) < 1e15:
            return str(int(v))
        return "%.14g" % v
    if v is None:
        raise ExprError("attempt to concatenate a nil value")
    return str(v)


def _num(v):
    if isinstance(v, bool) or v is None:
        raise ExprError("attempt to perform arithmetic on a %s value" % ("nil" if v is None else "boolean"))
    if isinstance(v, str):
        try:
            return float(v)
        except ValueError:
            raise ExprError("attempt to perform arithmetic on a string value")
    return v


def _arith(op, a, b):
    a, b = _num(a), _num(b)
    if op == "+":
        return a + b
    if op == "-":
        return a - b
    if op == "*":
        return a * b
    if op == "/":
        if b == 0:
            return math.copysign(math.inf, a) if a else math.nan
        return a / b
    if op == "%":
        if b == 0:
            return math.nan
        return a - math.floor(a / b) * b
    if op == "^":
        return float(a) ** b
    raise ExprError(op)


def _isnum(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _compare(op, a, b):
    if op == "==":
        if _isnum(a) and _isnum(b):
            return a == b
        return type(a) is type(b) and a == b
    if op == "~=":
        return not _compare("==", a, b)
    if not (isinstance(a, str) and isinstance(b, str)):
        a, b = _num(a), _num(b)
    return {"<": a < b, ">": a > b, "<=": a <= b, ">=": a >= b}[op]


class _Parser:
    def __init__(self, tokens, env):
        self.t = tokens
        self.i = 0
        self.env = env

    def peek(self):
        return self.t[self.i]

    def take(self, value=None):
        tok = self.t[self.i]
        if value is not None and tok[1] != value:
            raise ExprError("expected %r, got %r" % (value, tok[1]))
        self.i += 1
        return tok

    # evaluation happens while parsing; "and"/"or" still parse their right
    # side when it is skipped, with evaluation switched off
    def expr(self, rbp=0, live=True):
        left = self.unary(live)
        while True:
            kind, v = self.peek()
            if (kind not in ("op", "kw")) or v not in _BIN:
                break
            lbp, nbp = _BIN[v]
            if lbp <= rbp:
                break
            self.take()
            if v == "and":
                right = self.expr(nbp, live and _truthy(left))
                if live:
                    left = right if _truthy(left) else left
            elif v == "or":
                right = self.expr(nbp, live and not _truthy(left))
                if live:
                    left = left if _truthy(left) else right
            else:
                right = self.expr(nbp - (1 if v in ("..", "^") else 0), live)
                if live:
                    if v == "..":
                        left = _tostr(left) + _tostr(right)
                    elif v in ("==", "~=", "<", ">", "<=", ">="):
                        left = _compare(v, left, right)
                    else:
                        left = _arith(v, left, right)
        return left

    def unary(self, live):
        kind, v = self.peek()
        if (kind == "op" and v in ("-", "#")) or (kind == "kw" and v == "not"):
            self.take()
            val = self.expr(_UNARY, live)
            if not live:
                return None
            if v == "-":
                return -_num(val)
            if v == "not":
                return not _truthy(val)
            return len(val) if val is not None else 0
        return self.postfix(live)

    def postfix(self, live):
        val = self.primary(live)
        while True:
            kind, v = self.peek()
            if kind == "op" and v == "(":
                self.take()
                args = []
                if self.peek()[1] != ")":
                    args.append(self.expr(0, live))
                    while self.peek()[1] == ",":
                        self.take()
                        args.append(self.expr(0, live))
                self.take(")")
                if live:
                    if not callable(val):
                        raise ExprError("attempt to call a non-function value")
                    val = val(*args)
            elif kind == "op" and v == ".":
                self.take()
                name = self.take()[1]
                if live:
                    val = _index(val, name)
            elif kind == "op" and v == "[":
                self.take()
                key = self.expr(0, live)
                self.take("]")
                if live:
                    val = _index(val, key)
            else:
                return val

    def primary(self, live):
        kind, v = self.take()
        if kind == "num":
            if v.lower().startswith("0x"):
                return int(v, 16)
            f = float(v)
            return int(f) if f.is_integer() and not any(c in v for c in ".eE") else f
        if kind == "str":
            return bytes(v[1:-1], "utf-8").decode("unicode_escape")
        if kind == "kw":
            return {"nil": None, "true": True, "false": False}.get(v) if v in ("nil", "true", "false") else self._bad(v)
        if kind == "name":
            return self.env(v) if live else None
        if kind == "op" and v == "(":
            val = self.expr(0, live)
            self.take(")")
            return val
        if kind == "op" and v == "{":
            items = []
            while self.peek()[1] != "}":
                items.append(self.expr(0, live))
                if self.peek()[1] in (",", ";"):
                    self.take()
            self.take("}")
            return items
        raise ExprError("unexpected %r" % v)

    def _bad(self, v):
        raise ExprError("unexpected %r" % v)


def _index(val, key):
    if isinstance(val, dict):
        return val.get(key)
    if isinstance(val, list) and isinstance(key, (int, float)):
        k = int(key) - 1
        return val[k] if 0 <= k < len(val) else None
    return None


# --- functions available to expressions -------------------------------------

def _clamp(x, lo, hi):
    return min(max(x, lo), hi)


def _smoothstep(x):
    x = _clamp(x, 0, 1)
    return x * x * (3 - 2 * x)


def _smootherstep(x):
    x = _clamp(x, 0, 1)
    return x * x * x * (x * (x * 6 - 15) + 10)


def _smoothmin(a, b, k=0.1):
    h = max(k - abs(a - b), 0) / k if k else 0
    return min(a, b) - h * h * k * 0.25


def _sign(x):
    return 1 if x > 0 else (-1 if x < 0 else 0)


def _case(selector, *args):
    if isinstance(selector, bool):
        idx = 1 if selector else 2
    elif isinstance(selector, (int, float)):
        idx = int(math.floor(selector))
    else:
        idx = 0
    if 1 <= idx <= len(args) and args[idx - 1] is not None:
        return args[idx - 1]
    return args[-1] if args else None


def _round(x):
    return math.floor(x + 0.5)


def _modf(x):
    f, i = math.modf(x)
    return i


def _concat(t, sep=""):
    return sep.join(_tostr(v) for v in (t or []))


_FUNCS = {
    "abs": abs, "ceil": math.ceil, "floor": math.floor, "sqrt": math.sqrt,
    "sin": math.sin, "cos": math.cos, "tan": math.tan, "asin": math.asin,
    "acos": math.acos, "atan": lambda y, x=None: math.atan(y) if x is None else math.atan2(y, x),
    "atan2": math.atan2, "exp": math.exp, "log": lambda x, b=None: math.log(x) if b is None else math.log(x, b),
    "log10": math.log10, "max": max, "min": min, "pow": math.pow, "rad": math.radians,
    "deg": math.degrees, "fmod": math.fmod, "modf": _modf, "sinh": math.sinh, "cosh": math.cosh,
    "tanh": math.tanh, "pi": math.pi, "huge": math.inf,
    "round": _round, "square": lambda x: x * x, "clamp": _clamp, "smoothstep": _smoothstep,
    "smootherstep": _smootherstep, "smoothmin": _smoothmin, "sign": _sign, "case": _case,
    "concat": _concat, "print": lambda v, label=None: v,
    "tostring": _tostr, "tonumber": lambda v: _num(v) if v is not None else None,
}
_FUNCS["math"] = dict(_FUNCS)


def evaluate(expr, lookup):
    """Evaluate a "$=..." string. lookup(name_with_dollar) returns a value or None."""
    body = expr[2:] if expr.startswith("$=") else expr
    if re.search(r"[^<>~=]=[^=]", body):
        raise ExprError("assignments are not allowed in expressions")
    body = body.replace("$", "var_")

    def env(name):
        if name.startswith("var_"):
            return lookup("$" + name[4:])
        return _FUNCS.get(name)

    p = _Parser(_tokenize(body), env)
    val = p.expr()
    if p.peek()[0] != "end":
        raise ExprError("unexpected %r" % p.peek()[1])
    return val
