"""Reading a Scrap Mechanic blueprint.json: where every block, part and joint is.

Positions are in grid cells (0.25 m) in the creation's own frame, z up. A
shape's rotation is given by where its own x and z axes point (xaxis, zaxis:
1/2/3 for +x/+y/+z, negative for the opposite way); its box is
pos + R * [0, size]. A joint fills the one cell at its posA. Child indices in
joints count through all bodies' childs in order.
"""
from .game import CELL, _read_json

AXES = {1: (1, 0, 0), -1: (-1, 0, 0), 2: (0, 1, 0), -2: (0, -1, 0), 3: (0, 0, 1), -3: (0, 0, -1)}


def cross(a, b):
    return (a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0])


def rotation(xaxis, zaxis):
    """(ex, ey, ez): where the shape's own axes point."""
    try:
        ex = AXES.get(int(xaxis), (1, 0, 0))
        ez = AXES.get(int(zaxis), (0, 0, 1))
    except (TypeError, ValueError):
        ex, ez = (1, 0, 0), (0, 0, 1)
    if abs(sum(a * b for a, b in zip(ex, ez))) > 0.5:
        ex, ez = (1, 0, 0), (0, 0, 1)
    return ex, cross(ez, ex), ez


def rot(R, v):
    ex, ey, ez = R
    return tuple(ex[k] * v[0] + ey[k] * v[1] + ez[k] * v[2] for k in range(3))


def _vec(d, default=(0, 0, 0)):
    if not isinstance(d, dict):
        return default
    try:
        return (float(d.get("x", 0)), float(d.get("y", 0)), float(d.get("z", 0)))
    except (TypeError, ValueError):
        return default


def parse_color(s, default=(0.55, 0.55, 0.55)):
    s = str(s or "").strip().lstrip("#")
    if len(s) < 6:
        return default
    try:
        return tuple(int(s[i:i + 2], 16) / 255.0 for i in (0, 2, 4))
    except ValueError:
        return default


class Shape:
    """One child of a body: a block or a part."""
    __slots__ = ("idx", "body", "uuid", "info", "kind", "role", "pos", "R", "size", "color", "controller", "lo", "hi",
                 "mass")

    def __init__(self, idx, body, d, db):
        self.idx = idx
        self.body = body
        self.uuid = str(d.get("shapeId") or "").lower()
        self.info = db.get(self.uuid) if db else None
        self.pos = _vec(d.get("pos"))
        self.R = rotation(d.get("xaxis", 1), d.get("zaxis", 3))
        bounds = d.get("bounds")
        if isinstance(bounds, dict):
            self.kind = "block"
            self.size = tuple(max(v, 1.0) for v in _vec(bounds, (1, 1, 1)))
        else:
            self.kind = self.info.kind if self.info else "part"
            self.size = tuple(self.info.size) if self.info else (1.0, 1.0, 1.0)
        self.role = self.info.role if self.info else ("block" if self.kind == "block" else "unknown")
        self.color = parse_color(d.get("color"))
        self.controller = d.get("controller") if isinstance(d.get("controller"), dict) else None
        corners = [rot(self.R, (sx, sy, sz)) for sx in (0, self.size[0]) for sy in (0, self.size[1])
                   for sz in (0, self.size[2])]
        self.lo = tuple(self.pos[k] + min(c[k] for c in corners) for k in range(3))
        self.hi = tuple(self.pos[k] + max(c[k] for c in corners) for k in range(3))
        density = self.info.density if self.info else 300.0
        if self.kind == "block":
            vol = self.size[0] * self.size[1] * self.size[2]
        else:
            vol = self.info.volume_cells() if self.info else 1.0
        self.mass = density * vol * CELL ** 3

    @property
    def center(self):
        return tuple((a + b) / 2 for a, b in zip(self.lo, self.hi))

    def own_axis(self, k):
        """Where the shape's own axis k (0 x, 1 y, 2 z) points in the creation."""
        return self.R[k]

    def title(self):
        if self.info and self.info.title:
            return self.info.title
        if self.info and self.info.name:
            return self.info.name
        return "Unknown part " + self.uuid[:8]


class Joint:
    __slots__ = ("idx", "id", "uuid", "info", "role", "a", "b", "pos", "R", "color", "controller", "lo", "hi",
                 "mass")

    def __init__(self, idx, d, db):
        self.idx = idx
        self.id = d.get("id", idx)
        self.uuid = str(d.get("shapeId") or "").lower()
        self.info = db.get(self.uuid) if db else None
        self.role = self.info.role if self.info else "unknown"
        a, b = d.get("childA"), d.get("childB")
        self.a = a if isinstance(a, int) else -1
        self.b = b if isinstance(b, int) else -1
        self.pos = _vec(d.get("posA"))
        self.R = rotation(d.get("xaxisA", 1), d.get("zaxisA", 3))
        self.color = parse_color(d.get("color"))
        self.controller = d.get("controller") if isinstance(d.get("controller"), dict) else None
        self.lo = self.pos
        self.hi = tuple(v + 1 for v in self.pos)
        density = self.info.density if self.info else 500.0
        self.mass = density * CELL ** 3

    @property
    def axis(self):
        """The joint's own z axis: what a bearing turns about and a suspension or piston moves along."""
        return self.R[2]

    @property
    def center(self):
        return tuple(v + 0.5 for v in self.pos)

    def title(self):
        if self.info and self.info.title:
            return self.info.title
        if self.info and self.info.name:
            return self.info.name
        return "Unknown joint " + self.uuid[:8]


class Blueprint:
    def __init__(self, data, db, path=""):
        self.path = path
        self.shapes = []
        self.bodies = []
        self.joints = []
        self.unknown = set()
        if not isinstance(data, dict):
            raise ValueError("this is not a Scrap Mechanic blueprint")
        deps = data.get("dependencies")
        self.dependencies = deps if isinstance(deps, list) else []
        uuids = set()
        for body in data.get("bodies") or []:
            for c in (body or {}).get("childs") or []:
                if isinstance(c, dict) and not isinstance(c.get("bounds"), dict):
                    uuids.add(str(c.get("shapeId") or "").lower())
        for j in data.get("joints") or []:
            if isinstance(j, dict):
                uuids.add(str(j.get("shapeId") or "").lower())
        if db is not None:
            db.resolve(uuids, self.dependencies)
        for bi, body in enumerate(data.get("bodies") or []):
            members = []
            for c in (body or {}).get("childs") or []:
                if not isinstance(c, dict):
                    continue
                s = Shape(len(self.shapes), bi, c, db)
                if s.info is None and s.kind != "block":
                    self.unknown.add(s.uuid)
                self.shapes.append(s)
                members.append(s.idx)
            self.bodies.append(members)
        for j in data.get("joints") or []:
            if isinstance(j, dict):
                jt = Joint(len(self.joints), j, db)
                if jt.info is None:
                    self.unknown.add(jt.uuid)
                self.joints.append(jt)
        if not self.shapes:
            raise ValueError("the blueprint is empty")

    def body_of(self, child):
        if 0 <= child < len(self.shapes):
            return self.shapes[child].body
        return -1

    def controlled_joints(self, shape):
        """Ids of the joints a part's controller drives (an engine's bearings, a seat's steering)."""
        c = shape.controller or {}
        out = []
        for key in ("joints", "steering"):
            for e in c.get(key) or []:
                if isinstance(e, dict) and "id" in e:
                    out.append((e["id"], e))
        return out

    def controlled_ids(self, shape):
        c = shape.controller or {}
        return [e["id"] for e in c.get("controllers") or [] if isinstance(e, dict) and "id" in e]


def load(path, db):
    data = _read_json(path)
    return Blueprint(data, db, path)
