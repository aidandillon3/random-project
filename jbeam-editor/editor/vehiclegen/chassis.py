"""The physics skeleton of a generated vehicle: nodes, beams, collision triangles.

Structure collects everything with the part it belongs to; parts.py turns it
into JBeam. The body is a tube of node rings that follow the skin (see
shape.py); each axle is a double wishbone or a solid axle with real pivot
points, springs, dampers, bump stops and an anti-roll bar; the engine is a
block of eight nodes on soft mounts. Masses are spread so the total matches the
design weight, then raised wherever a node would be too light for the beams on
it to stay stable at the game's 2000 Hz physics rate.
"""
import math

from .shape import S_BELT, S_EDGE, S_FLOOR, S_SIDE, S_TOP, clamp, lerp

DT = 0.0005          # the game's physics step
G = 9.81
FLT_MAX = "FLT_MAX"


def v_add(a, b):
    return (a[0] + b[0], a[1] + b[1], a[2] + b[2])


def v_sub(a, b):
    return (a[0] - b[0], a[1] - b[1], a[2] - b[2])


def v_mul(a, f):
    return (a[0] * f, a[1] * f, a[2] * f)


def v_dot(a, b):
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]


def v_cross(a, b):
    return (a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0])


def v_len(a):
    return math.sqrt(v_dot(a, a))


def v_norm(a):
    n = v_len(a) or 1.0
    return (a[0] / n, a[1] / n, a[2] / n)


def dist(a, b):
    return v_len(v_sub(a, b))


def mirror(p, s):
    """p given for the left side (x >= 0); s = 1 left, -1 right."""
    return (p[0] * s, p[1], p[2])


SIDES = (("l", 1), ("r", -1))


class Node:
    __slots__ = ("name", "p", "part", "w", "groups", "coll", "selfcoll", "mat", "fric", "extra", "fixed_w", "kind")

    def __init__(self, name, p, part, w=1.0, groups=(), coll=True, selfcoll=False, mat="|NM_METAL", fric=0.6,
                 kind="", extra=None):
        self.name = name
        self.p = p
        self.part = part
        self.w = w
        self.groups = list(groups)
        self.coll = coll
        self.selfcoll = selfcoll
        self.mat = mat
        self.fric = fric
        self.kind = kind
        self.extra = extra or {}
        self.fixed_w = False


class Beam:
    __slots__ = ("a", "b", "part", "kind", "extra", "sec")

    def __init__(self, a, b, part, kind, extra=None, sec="beams"):
        self.a, self.b, self.part, self.kind, self.extra, self.sec = a, b, part, kind, extra or {}, sec


class Structure:
    def __init__(self):
        self.nodes = {}
        self.order = []
        self.beams = []
        self._pairs = set()
        self.tris = []
        self.torsionbars = []
        self.kinds = {}          # beam kind -> dict of beam options
        self.notes = []

    def node(self, name, p, part, **kw):
        if name in self.nodes:
            raise ValueError("duplicate node " + name)
        n = Node(name, p, part, **kw)
        self.nodes[name] = n
        self.order.append(name)
        return n

    def kind(self, name, **opts):
        self.kinds[name] = opts

    def beam(self, a, b, part, kind, sec="beams", dup_ok=False, **extra):
        if a == b:
            return None
        key = (min(a, b), max(a, b), kind)
        if not dup_ok and key in self._pairs:
            return None
        self._pairs.add(key)
        bm = Beam(a, b, part, kind, extra, sec)
        self.beams.append(bm)
        return bm

    def has_pair(self, a, b):
        return any((min(a, b), max(a, b), k) in self._pairs for k in self.kinds)

    def tri(self, a, b, c, part, kind="body"):
        self.tris.append((a, b, c, part, kind))

    def pos(self, name):
        return self.nodes[name].p

    def nearest(self, p, names, n, min_d=0.05):
        cands = sorted(((dist(p, self.nodes[x].p), x) for x in names), key=lambda t: t[0])
        return [x for d, x in cands if d >= min_d][:n]

    def attach(self, name, cand, part, kind, n=4, min_d=0.08):
        """Tie a node to n nearby nodes so it is held in every direction."""
        p = self.pos(name)
        chosen = []
        cands = sorted(((dist(p, self.nodes[x].p), x) for x in cand if x != name), key=lambda t: t[0])
        for d, x in cands:
            if d < min_d:
                continue
            chosen.append(x)
            if len(chosen) >= n and _spread(p, [self.nodes[c].p for c in chosen]) > 0.08:
                break
            if len(chosen) >= n + 4:
                break
        for x in chosen:
            self.beam(name, x, part, kind)
        return chosen


def _neighbours(S):
    nbr = {}
    for b in S.beams:
        if b.sec not in ("beams", "hydros") or b.kind.startswith("calc_") or b.kind == "anti_inv":
            continue
        nbr.setdefault(b.a, set()).add(b.b)
        nbr.setdefault(b.b, set()).add(b.a)
    return nbr


def degenerate_nodes(S, names=None, limit=0.06):
    """Nodes whose beams all lie in (nearly) one plane or line: they can swing freely out of it."""
    nbr = _neighbours(S)
    out = []
    for nm in (names if names is not None else S.order):
        if S.nodes[nm].part.startswith("wheel_"):
            continue   # the game's wheel hub holds these
        pts = [S.nodes[x].p for x in nbr.get(nm, ()) if x in S.nodes]
        if len(pts) < 3 or _spread(S.nodes[nm].p, pts) < limit:
            out.append(nm)
    return out


def brace_degenerate(S, names, candidates, part_of=None, kind="b_cross", limit=0.08):
    """Tie each node that is held flat to nodes in the direction it can move freely."""
    nbr = _neighbours(S)
    for nm in degenerate_nodes(S, names, limit):
        p = S.nodes[nm].p
        part = part_of(nm) if part_of else S.nodes[nm].part
        for _ in range(3):
            pts = [S.nodes[x].p for x in nbr.get(nm, ()) if x in S.nodes]
            if len(pts) >= 3 and _spread(p, pts) >= limit:
                break
            weak = _weak_dir(p, pts)
            best = None
            for x in candidates:
                if x == nm or x in nbr.get(nm, ()):
                    continue
                d = v_sub(S.nodes[x].p, p)
                ln = v_len(d)
                if ln < 0.08 or ln > 1.6:
                    continue
                score = abs(v_dot(d, weak)) / ln - 0.15 * ln
                if best is None or score > best[0]:
                    best = (score, x)
            if best is None:
                break
            S.beam(nm, best[1], part, kind)
            nbr.setdefault(nm, set()).add(best[1])
            nbr.setdefault(best[1], set()).add(nm)


def _weak_dir(p, pts):
    """The direction a node held by beams to pts resists least (unit vector)."""
    m = [[0.0] * 3 for _ in range(3)]
    for q in pts:
        d = v_norm(v_sub(q, p))
        for i in range(3):
            for j in range(3):
                m[i][j] += d[i] * d[j]
    lam = _min_eig(m)
    a = [[m[i][j] - (lam if i == j else 0.0) for j in range(3)] for i in range(3)]
    best = (0.0, (0.0, 0.0, 1.0))
    for i in range(3):
        for j in range(i + 1, 3):
            c = v_cross(tuple(a[i]), tuple(a[j]))
            ln = v_len(c)
            if ln > best[0]:
                best = (ln, c)
    if best[0] < 1e-9:
        # one direction only (a line): anything across it is weak; pick the most vertical one
        line = v_norm(v_sub(pts[0], p)) if pts else (1.0, 0.0, 0.0)
        c = v_cross(line, (1.0, 0.0, 0.0))
        if v_len(c) < 1e-6:
            c = v_cross(line, (0.0, 1.0, 0.0))
        return v_norm(c)
    return v_norm(best[1])


def _spread(p, pts):
    """Smallest eigenvalue of sum(d d^T) of unit directions: > 0 when they span 3D."""
    m = [[0.0] * 3 for _ in range(3)]
    for q in pts:
        d = v_norm(v_sub(q, p))
        for i in range(3):
            for j in range(3):
                m[i][j] += d[i] * d[j]
    return _min_eig(m)


def _min_eig(m):
    # eigenvalues of a symmetric 3x3 matrix (trigonometric method)
    p1 = m[0][1] ** 2 + m[0][2] ** 2 + m[1][2] ** 2
    q = (m[0][0] + m[1][1] + m[2][2]) / 3
    if p1 < 1e-12:
        return min(m[0][0], m[1][1], m[2][2])
    p2 = (m[0][0] - q) ** 2 + (m[1][1] - q) ** 2 + (m[2][2] - q) ** 2 + 2 * p1
    p = math.sqrt(p2 / 6)
    b = [[(m[i][j] - (q if i == j else 0)) / p for j in range(3)] for i in range(3)]
    det = (b[0][0] * (b[1][1] * b[2][2] - b[1][2] * b[2][1]) - b[0][1] * (b[1][0] * b[2][2] - b[1][2] * b[2][0])
           + b[0][2] * (b[1][0] * b[2][1] - b[1][1] * b[2][0]))
    r = clamp(det / 2, -1.0, 1.0)
    phi = math.acos(r) / 3
    return q + 2 * p * math.cos(phi + 2 * math.pi / 3)


# ---------------------------------------------------------------------------
# body
# ---------------------------------------------------------------------------

RING_LEFT = ("bf", "bs", "bb", "bt")      # floor rail, side bottom, belt, top (left side)


def body_stations(sh):
    keys = [(sh.y0, 0), (sh.y1, 0)]
    for a in sh.axles:
        keys += [(a["y"], 1), (a["y"] - 0.8 * a["arch"], 2), (a["y"] + 0.8 * a["arch"], 2)]
    keys += [(sh.y_cowl, 3), (sh.y_rt, 3)]
    if not sh.P.openTop and not sh.P.bed:
        keys += [(sh.y_wst, 4), (sh.y_rb, 4)]
    if sh.y_open0 is not None:
        keys += [(sh.y_open0 - 0.04, 2), (sh.y_open0 + 0.03, 2), (sh.y_open1, 2)]
    keys = [(clamp(y, sh.y0, sh.y1), pr) for y, pr in keys]
    keys.sort()
    kept = []
    for y, pr in keys:
        if kept and y - kept[-1][0] < 0.17:
            if pr < kept[-1][1] or (pr == kept[-1][1] and pr == 0):
                if kept[-1][1] == 0 and len(kept) == 1:
                    continue
                kept[-1] = (y, pr)
            continue
        kept.append((y, pr))
    if kept[0][0] > sh.y0 + 1e-6:
        kept.insert(0, (sh.y0, 0))
    if kept[-1][0] < sh.y1 - 1e-6:
        if sh.y1 - kept[-1][0] < 0.12:
            kept[-1] = (sh.y1, 0)
        else:
            kept.append((sh.y1, 0))
    ys = [k[0] for k in kept]
    # fill long gaps evenly
    out = [ys[0]]
    for y in ys[1:]:
        prev = out[-1]
        gap = y - prev
        n = int(math.ceil(gap / 0.52))
        for k in range(1, n):
            out.append(prev + gap * k / n)
        out.append(y)
    return out


def top_point(pts, frac=0.55):
    """A point on the top surface (roof or hood) at frac of the edge's half-width."""
    xe = pts[S_EDGE][0]
    target = xe * frac
    seq = pts[S_EDGE:S_TOP + 1]
    for i in range(len(seq) - 1):
        a, b = seq[i], seq[i + 1]
        if a[0] >= target >= b[0]:
            t = (a[0] - target) / max(a[0] - b[0], 1e-9)
            return (target, lerp(a[1], b[1], t))
    return seq[-1]


def build_body(S, sh, P, part="body"):
    s_mass = clamp(P.mass / 1500.0, 0.35, 4.0)
    ks = s_mass ** 0.65
    S.kind("b_floor", beamSpring=round(2.2e6 * ks, -3) + 1000, beamDamp=round(110 * ks), beamDeform=round(90000 * ks),
           beamStrength=FLT_MAX)
    S.kind("b_side", beamSpring=round(1.8e6 * ks, -3) + 1000, beamDamp=round(90 * ks), beamDeform=round(45000 * ks),
           beamStrength=FLT_MAX)
    S.kind("b_roof", beamSpring=round(1.3e6 * ks, -3) + 1000, beamDamp=round(70 * ks), beamDeform=round(28000 * ks),
           beamStrength=FLT_MAX)
    S.kind("b_cross", beamSpring=round(1.2e6 * ks, -3) + 1000, beamDamp=round(70 * ks), beamDeform=round(35000 * ks),
           beamStrength=FLT_MAX)
    S.kind("b_long", beamSpring=round(1.5e6 * ks, -3) + 1000, beamDamp=round(80 * ks), beamDeform=round(60000 * ks),
           beamStrength=FLT_MAX)
    ys = body_stations(sh)
    rings = []
    grp = [P.model_group("body")]
    for i, y in enumerate(ys):
        pts = sh.section(y)
        kind = sh.kind(y)
        ring = {"y": y, "kind": kind, "ids": {}}
        fl = pts[S_FLOOR]
        sl = pts[S_SIDE]
        bl = pts[S_BELT]
        has_top = kind != "open"
        tl = top_point(pts) if has_top else None
        tc = pts[S_TOP] if has_top else None
        ring["ids"]["fc"] = S.node("bf%d" % i, (0.0, y, pts[0][1]), part, groups=grp, kind="fc", selfcoll=True).name
        for side, s in SIDES:
            ring["ids"]["fl" + side] = S.node("bf%d%s" % (i, side), (fl[0] * s, y, fl[1]), part, groups=grp,
                                              kind="fl", selfcoll=True).name
            ring["ids"]["sl" + side] = S.node("bs%d%s" % (i, side), (sl[0] * s, y, sl[1]), part, groups=grp,
                                              kind="sl", selfcoll=True).name
            ring["ids"]["bl" + side] = S.node("bb%d%s" % (i, side), (bl[0] * s, y, bl[1]), part, groups=grp,
                                              kind="bl", selfcoll=True).name
            if has_top:
                ring["ids"]["tl" + side] = S.node("bt%d%s" % (i, side), (tl[0] * s, y, tl[1]), part, groups=grp,
                                                  kind="tl", selfcoll=True).name
        if has_top:
            ring["ids"]["tc"] = S.node("bt%d" % i, (0.0, y, tc[1]), part, groups=grp, kind="tc", selfcoll=True).name
        rings.append(ring)

    def ring_order(r):
        ids = r["ids"]
        if "tc" in ids:
            return [ids["fc"], ids["fll"], ids["sll"], ids["bll"], ids["tll"], ids["tc"], ids["tlr"], ids["blr"],
                    ids["slr"], ids["flr"]]
        return [ids["fc"], ids["fll"], ids["sll"], ids["bll"], ids["blr"], ids["slr"], ids["flr"]]

    def edge_kind(a, b):
        ka, kb = S.nodes[a].kind, S.nodes[b].kind
        both = {ka, kb}
        if both <= {"fc", "fl"}:
            return "b_floor"
        if both & {"tl", "tc"}:
            return "b_roof" if both <= {"tl", "tc", "bl"} else "b_cross"
        return "b_side"

    # rings: perimeter and braces
    for r in rings:
        ids = r["ids"]
        order = ring_order(r)
        for k in range(len(order)):
            a, b = order[k], order[(k + 1) % len(order)]
            S.beam(a, b, part, edge_kind(a, b))
        for side, _ in SIDES:
            S.beam(ids["fl" + side], ids["bl" + side], part, "b_cross")
            S.beam(ids["fc"], ids["sl" + side], part, "b_cross")
            S.beam(ids["fc"], ids["bl" + side], part, "b_cross")
            if "tc" in ids:
                S.beam(ids["sl" + side], ids["tl" + side], part, "b_cross")
                S.beam(ids["tc"], ids["bl" + side], part, "b_cross")
        S.beam(ids["bll"], ids["blr"], part, "b_cross")
        if "tc" in ids:
            S.beam(ids["fc"], ids["tc"], part, "b_cross")
            S.beam(ids["sll"], ids["tlr"], part, "b_cross")
            S.beam(ids["slr"], ids["tll"], part, "b_cross")

    # between rings: longitudinal, face diagonals, collision triangles
    for i in range(len(rings) - 1):
        r0, r1 = rings[i], rings[i + 1]
        common = [k for k in r0["ids"] if k in r1["ids"]]
        for k in common:
            S.beam(r0["ids"][k], r1["ids"][k], part, "b_long" if k[:2] in ("fc", "fl", "sl", "bl") else "b_roof")
        o0, o1 = ring_order(r0), ring_order(r1)
        keys0 = _ring_keys(r0)
        keys1 = _ring_keys(r1)
        if keys0 == keys1:
            n = len(o0)
            for k in range(n):
                a0, b0 = o0[k], o0[(k + 1) % n]
                a1, b1 = o1[k], o1[(k + 1) % n]
                kind = edge_kind(a0, b0)
                S.beam(a0, b1, part, kind)
                S.beam(b0, a1, part, kind)
                _quad_tris(S, a0, b0, a1, b1, part, keys0[k], keys0[(k + 1) % n])
        else:
            # closed <-> open: join the parts both have, and wall off the top of the closed one
            closed, opened, toward_open = (r0, r1, 1) if "tc" in r0["ids"] else (r1, r0, -1)
            seq = ["fc", "fll", "sll", "bll", "blr", "slr", "flr"]
            oc, oo = [closed["ids"][k] for k in seq], [opened["ids"][k] for k in seq]
            first, second = (oc, oo) if r0 is closed else (oo, oc)
            for k in range(len(seq)):
                a0, b0 = first[k], first[(k + 1) % len(seq)]
                a1, b1 = second[k], second[(k + 1) % len(seq)]
                kind = edge_kind(a0, b0)
                S.beam(a0, b1, part, kind)
                S.beam(b0, a1, part, kind)
                # no skin across the top of the open part
                if not (seq[k] == "bll" and seq[(k + 1) % len(seq)] == "blr"):
                    _quad_tris(S, a0, b0, a1, b1, part, seq[k], seq[(k + 1) % len(seq)])
            ids = closed["ids"]
            wall = [ids["bll"], ids["tll"], ids["tc"], ids["tlr"], ids["blr"]]
            # the wall faces the open side: +y when the open part is behind
            for k in range(1, len(wall) - 1):
                a, b, c = wall[0], wall[k], wall[k + 1]
                if toward_open > 0:
                    S.tri(a, c, b, part, "body")
                else:
                    S.tri(a, b, c, part, "body")
            for side, _ in SIDES:
                S.beam(ids["tl" + side], opened["ids"]["bl" + side], part, "b_cross")
        # skip-one longitudinals for bending stiffness
        if i + 2 < len(rings):
            r2 = rings[i + 2]
            for k in ("fc", "fll", "flr", "bll", "blr", "tc"):
                if k in r0["ids"] and k in r2["ids"]:
                    S.beam(r0["ids"][k], r2["ids"][k], part, "b_long")

    # end caps (bumper faces): fan from the floor centre; front faces -y, rear +y
    for r, front in ((rings[0], True), (rings[-1], False)):
        order = ring_order(r)
        for k in range(1, len(order) - 1):
            a, b, c = order[0], order[k], order[k + 1]
            if front:
                S.tri(a, b, c, part, "body")
            else:
                S.tri(a, c, b, part, "body")
            if k + 1 < len(order) - 1:
                S.beam(order[0], order[k + 1], part, "b_cross")
    # a node held only in its own ring's plane (an end ring next to an open bed) gets braced lengthwise
    body = [n for n in S.order if S.nodes[n].part == part]
    brace_degenerate(S, body, body)
    S.body_rings = rings
    return rings


def _ring_keys(r):
    ids = r["ids"]
    if "tc" in ids:
        return ["fc", "fll", "sll", "bll", "tll", "tc", "tlr", "blr", "slr", "flr"]
    return ["fc", "fll", "sll", "bll", "blr", "slr", "flr"]


def _quad_tris(S, a0, b0, a1, b1, part, ka, kb):
    """Two outward triangles between ring edges (a0,b0) at y0 and (a1,b1) at the next ring."""
    kind = "under" if {ka[:2], kb[:2]} <= {"fc", "fl"} else "body"
    S.tri(a0, a1, b1, part, kind)
    S.tri(a0, b1, b0, part, kind)


# ---------------------------------------------------------------------------
# suspension
# ---------------------------------------------------------------------------

class Corner:
    """Where things are for one wheel; s = 1 left, -1 right."""

    def __init__(self, axle, s):
        self.axle = axle
        self.s = s
        self.side = "l" if s > 0 else "r"
        self.name = axle.id + ("L" if s > 0 else "R")


class Axle:
    def __init__(self, P, sh, ax_id):
        self.id = ax_id
        self.front = ax_id == "F"
        self.p = "f" if self.front else "r"
        self.solid = (P.suspF if self.front else P.suspR) == "solid"
        self.steer = self.front
        self.y = sh.yF if self.front else sh.yR
        self.r = P.rF if self.front else P.rR
        self.rim = P.rimF if self.front else P.rimR
        self.tw = P.twF if self.front else P.twR
        self.track = P.trackF if self.front else P.trackR
        self.travel = P.travelF if self.front else P.travelR
        self.bump = self.travel * 0.55
        self.droop = self.travel * 0.45
        self.zc = self.r
        self.k = clamp(self.r / 0.33, 0.8, 1.6)
        self.toward_center = 1 if self.front else -1
        self.driven = (P.drive == "AWD") or (P.drive == "RWD" and not self.front) or (P.drive == "FWD" and self.front)
        self.corners = [Corner(self, s) for _, s in SIDES]
        # wheel centre of each corner: (distance from the centre line, y, z)
        self.centers = {side: (self.track / 2, self.y, self.zc) for side, _ in SIDES}
        # filled in by the builders
        self.spring_ends = {}     # side -> (lower node, upper node)
        self.hub = {}             # side -> {"h1":..}
        self.arb = None           # [n1, n2, n3, n4], lever
        self.mr = 1.0
        self.mount_nodes = []
        self.unsprung_nodes = []
        self.wheel_nodes = {}     # side -> (inner, outer) names


def _clamp_z(sh, y, z, margin=0.06):
    return min(z, sh.top(y) - margin)


def build_wishbone(S, sh, P, ax, body_names, top=None):
    """Double wishbones at each corner of ax. top(x, y): height of the body there (default: the shape's)."""
    part = "susp_" + ax.id
    k = ax.k
    p = ax.p
    caster = -0.015 if ax.front else 0.0
    hub_w = clamp(3.4 * (P.mass / 1500.0) ** 0.4, 2.4, 8.0)
    piv_w = clamp(3.0 * (P.mass / 1500.0) ** 0.4, 2.0, 7.0)
    # a pair of wheels keeps its arms on its own side; a lone wheel's arms may cross the middle
    paired = len(ax.corners) == 2
    for c in ax.corners:
        side, s = c.side, c.s
        T2, y, zc = ax.centers[side]
        x_lo = T2 - 0.075 - clamp(0.5 * T2, 0.26, 0.55)
        x_lo = max(x_lo, 0.06) if paired else x_lo
        L_lo = T2 - 0.075 - x_lo
        x_up = T2 - 0.125 - 0.72 * L_lo
        x_up = max(x_up, 0.05) if paired else x_up
        z_want = zc + 0.165 * k + 0.26 + 0.35 * ax.travel
        if top is None:
            zt_spring = _clamp_z(sh, y, z_want, 0.07)
        else:
            zt_spring = min(z_want, top(s * (T2 - 0.205), y) - 0.07)
        zt_spring = max(zt_spring, zc - 0.115 * k + 0.32)
        g_hub = P.model_group("hub_" + ax.id + side.upper())
        h1 = S.node("%sh1%s" % (p, side), mirror((T2 - 0.075, y, zc - 0.115 * k), s), part, w=hub_w,
                    groups=[g_hub, P.model_group("larm_" + ax.id + side.upper()),
                            P.model_group("coil_" + ax.id + side.upper())], kind="hub")
        h2 = S.node("%sh2%s" % (p, side), mirror((T2 - 0.125, y + caster, zc + 0.165 * k), s), part, w=hub_w,
                    groups=[g_hub, P.model_group("uarm_" + ax.id + side.upper())], kind="hub")
        h3 = S.node("%sh3%s" % (p, side), mirror((T2 - 0.105, y + 0.135 * k, zc - 0.02), s), part, w=hub_w,
                    groups=[g_hub, P.model_group("tie_" + ax.id + side.upper())], kind="hub")
        h5 = S.node("%sh5%s" % (p, side), mirror((T2 - 0.075, y - 0.13 * k, zc + 0.03), s), part, w=hub_w,
                    groups=[g_hub], kind="hub")
        lf = S.node("%sl1%s" % (p, side), mirror((x_lo, y - 0.17 * k, zc - 0.115 * k + 0.025), s), part, w=piv_w,
                    groups=[P.model_group("larm_" + ax.id + side.upper()), P.model_group("sub_" + ax.id),
                            P.model_group("coil_" + ax.id + side.upper())], kind="piv")
        lr = S.node("%sl2%s" % (p, side), mirror((x_lo, y + 0.17 * k, zc - 0.115 * k + 0.025), s), part, w=piv_w,
                    groups=[P.model_group("larm_" + ax.id + side.upper()), P.model_group("sub_" + ax.id),
                            P.model_group("tie_" + ax.id + side.upper())], kind="piv")
        uf = S.node("%su1%s" % (p, side), mirror((x_up, y - 0.13 * k, zc + 0.165 * k + 0.035), s), part, w=piv_w,
                    groups=[P.model_group("uarm_" + ax.id + side.upper()), P.model_group("sub_" + ax.id)], kind="piv")
        ur = S.node("%su2%s" % (p, side), mirror((x_up, y + 0.13 * k, zc + 0.165 * k + 0.035), s), part, w=piv_w,
                    groups=[P.model_group("uarm_" + ax.id + side.upper()), P.model_group("sub_" + ax.id)], kind="piv")
        st = S.node("%ss1%s" % (p, side), mirror((T2 - 0.075 - 0.13, y, zt_spring), s), part, w=piv_w,
                    groups=[P.model_group("coil_" + ax.id + side.upper()), P.model_group("sub_" + ax.id)], kind="piv")
        ti = S.node("%st1%s" % (p, side), mirror((x_lo + 0.02, y + 0.135 * k, zc - 0.01), s), part, w=piv_w,
                    groups=[P.model_group("tie_" + ax.id + side.upper()), P.model_group("sub_" + ax.id)], kind="piv")
        for n in (h1, h2, h3, h5):
            n.coll, n.selfcoll = True, False
        for n in (lf, lr, uf, ur, st, ti):
            n.coll, n.selfcoll = False, False
        hub = {"h1": h1.name, "h2": h2.name, "h3": h3.name, "h5": h5.name}
        ax.hub[side] = hub
        ax.unsprung_nodes += list(hub.values())
        ax.spring_ends[side] = (h1.name, st.name)
        # knuckle
        names = list(hub.values())
        for i in range(4):
            for j in range(i + 1, 4):
                S.beam(names[i], names[j], part, "hub")
        # arms
        cam = "$camber_" + ax.id
        S.beam(lf.name, h1.name, part, "arm", dampCutoffHz=500)
        S.beam(lr.name, h1.name, part, "arm", dampCutoffHz=500)
        S.beam(uf.name, h2.name, part, "arm_up", dampCutoffHz=500,
               beamPrecompression="$=1+%s*%s" % (cam, _fmt(_camber_k(S, h1.name, h2.name, uf.name))),
               beamPrecompressionTime=0.5)
        S.beam(ur.name, h2.name, part, "arm_up", dampCutoffHz=500,
               beamPrecompression="$=1+%s*%s" % (cam, _fmt(_camber_k(S, h1.name, h2.name, ur.name))),
               beamPrecompressionTime=0.5)
        # tie rod / toe link
        if ax.steer:
            opts = _steer_opts(S, ti.name, h3.name, h1.name, h2.name, getattr(ax, "steer_angle", None) or P.steerAngle,
                               s * getattr(ax, "steer_sign", 1))
            S.beam(ti.name, h3.name, part, "steer", sec="hydros", **opts)
        else:
            S.beam(ti.name, h3.name, part, "link")
        # the subframe: pivots, shock tower and tie rod end held together
        piv = [lf.name, lr.name, uf.name, ur.name, st.name, ti.name]
        for i in range(len(piv)):
            for j in range(i + 1, len(piv)):
                S.beam(piv[i], piv[j], part, "sub")
        for n in piv:
            S.attach(n, body_names, part, "mount", n=3)
        ax.mount_nodes += piv
        # keep the arms from folding through
        S.beam(h1.name, uf.name, part, "anti_inv", beamPrecompression=0.55)
        S.beam(h1.name, ur.name, part, "anti_inv", beamPrecompression=0.55)
        S.beam(h2.name, lf.name, part, "anti_inv", beamPrecompression=0.55)
        S.beam(h2.name, lr.name, part, "anti_inv", beamPrecompression=0.55)
    if len(ax.corners) == 2:
        # cross-car ties between the two halves of the subframe
        for a, b in (("l1", "l1"), ("l2", "l2"), ("u1", "u1"), ("s1", "s1")):
            S.beam("%s%sl" % (p, a), "%s%sr" % (p, b), part, "sub")
        S.beam("%sl1l" % p, "%sl2r" % p, part, "sub")
        S.beam("%sl2l" % p, "%sl1r" % p, part, "sub")
        # anti-roll bar: twist about the line through the lower front pivots
        ax.arb = ["%sh1l" % p, "%sl1l" % p, "%sl1r" % p, "%sh1r" % p]
        ax.arb_lever = _lever(S, "%sh1l" % p, "%sl1l" % p, "%sl1r" % p)
    _wheel_attach(S, ax, part)
    lo, hi = ax.spring_ends[ax.corners[0].side]
    ax.mr = abs(v_norm(v_sub(S.pos(hi), S.pos(lo)))[2])


def build_solid(S, sh, P, ax, body_names, top=None):
    """A solid axle under ax. top(x, y): height of the body there (default: the shape's).

    The axle's middle is at ax.x0 (0 unless the pair of wheels is off the vehicle's centre line)."""
    part = "susp_" + ax.id
    k = ax.k
    T2 = ax.track / 2
    p = ax.p
    zc, y = ax.zc, ax.y
    tc = ax.toward_center
    x0 = getattr(ax, "x0", 0.0)

    def mirror(q, s):
        return (x0 + q[0] * s, q[1], q[2])
    m_ax = clamp(45 + 0.014 * P.mass, 45, 160) * (1.15 if ax.driven else 0.85)
    w_ax = m_ax / 9.0
    hub_w = clamp(3.4 * (P.mass / 1500.0) ** 0.4, 2.4, 8.0)
    piv_w = clamp(3.0 * (P.mass / 1500.0) ** 0.4, 2.0, 7.0)
    g_ax = P.model_group("axle_" + ax.id)
    xa = T2 - 0.16
    L = clamp(0.55 + 0.6 * ax.travel, 0.55, 1.15)
    a = {}
    for side, s in SIDES:
        a["1" + side] = S.node("%sa1%s" % (p, side), mirror((xa, y, zc), s), part, w=w_ax, groups=[g_ax], kind="axle").name
        a["2" + side] = S.node("%sa2%s" % (p, side), mirror((xa, y, zc - 0.11 * k), s), part, w=w_ax, groups=[g_ax],
                               kind="axle").name
    a["3"] = S.node("%sa3" % p, (x0, y, zc), part, w=w_ax, groups=[g_ax], kind="axle").name
    a["4"] = S.node("%sa4" % p, (x0, y, zc + 0.13 * k), part, w=w_ax, groups=[g_ax], kind="axle").name
    a["5"] = S.node("%sa5" % p, (x0, y + tc * 0.2, zc - 0.02), part, w=w_ax, groups=[g_ax], kind="axle").name
    for n in a.values():
        S.nodes[n].coll, S.nodes[n].selfcoll = True, False
    housing = list(a.values())
    for i in range(len(housing)):
        for j in range(i + 1, len(housing)):
            S.beam(housing[i], housing[j], part, "axle")
    ax.unsprung_nodes += housing
    z_want = zc + 0.32 + 0.5 * ax.travel
    zt_spring = _clamp_z(sh, y, z_want, 0.07) if top is None else min(z_want, top(x0 + xa, y) - 0.07)
    zt_spring = max(zt_spring, zc + 0.28)
    for side, s in SIDES:
        g_hub = P.model_group("hub_" + ax.id + side.upper())
        h1 = S.node("%sh1%s" % (p, side), mirror((T2 - 0.105, y, zc - 0.115 * k), s), part, w=hub_w,
                    groups=[g_hub, g_ax], kind="hub")
        h2 = S.node("%sh2%s" % (p, side), mirror((T2 - 0.125, y, zc + 0.135 * k), s), part, w=hub_w,
                    groups=[g_hub, g_ax], kind="hub")
        h3 = S.node("%sh3%s" % (p, side), mirror((T2 - 0.105, y + 0.14 * k, zc - 0.02), s), part, w=hub_w,
                    groups=[g_hub, P.model_group("tie_" + ax.id + side.upper())], kind="hub")
        h5 = S.node("%sh5%s" % (p, side), mirror((T2 - 0.075, y - 0.13 * k, zc + 0.03), s), part, w=hub_w,
                    groups=[g_hub], kind="hub")
        for n in (h1, h2, h3, h5):
            n.coll, n.selfcoll = True, False
        hub = {"h1": h1.name, "h2": h2.name, "h3": h3.name, "h5": h5.name}
        ax.hub[side] = hub
        ax.unsprung_nodes += list(hub.values())
        own = [a["1" + side], a["2" + side], a["3"], a["4"]]
        # kingpins are part of the housing
        for kp in (h1.name, h2.name):
            for n in own:
                S.beam(kp, n, part, "axle")
        S.beam(h1.name, h2.name, part, "axle")
        if ax.steer:
            # the knuckle (steering arm, caliper, wheel) turns about the kingpins
            for n in (h3.name, h5.name):
                S.beam(n, h1.name, part, "hub")
                S.beam(n, h2.name, part, "hub")
            S.beam(h3.name, h5.name, part, "hub")
            sr = S.node("%st1%s" % (p, side), mirror((max(xa - 0.3, 0.12), y + 0.14 * k, zc - 0.02), s), part,
                        w=hub_w, groups=[g_ax, P.model_group("tie_" + ax.id + side.upper())], kind="axle")
            sr.coll, sr.selfcoll = False, False
            ax.unsprung_nodes.append(sr.name)
            for n in own:
                S.beam(sr.name, n, part, "axle")
            S.beam(sr.name, h1.name, part, "axle")
            S.beam(sr.name, h3.name, part, "steer", sec="hydros",
                   **_steer_opts(S, sr.name, h3.name, h1.name, h2.name, getattr(ax, "steer_angle", None) or P.steerAngle,
                                 s * getattr(ax, "steer_sign", 1)))
        else:
            for n in (h3.name, h5.name):
                for m in own + [h1.name, h2.name]:
                    S.beam(n, m, part, "axle")
            S.beam(h3.name, h5.name, part, "axle")
        # links to the body
        k1 = S.node("%sk1%s" % (p, side), mirror((T2 - 0.26, y + tc * L, zc - 0.07), s), part, w=piv_w,
                    groups=[P.model_group("link_" + ax.id + side.upper()), P.model_group("sub_" + ax.id)], kind="piv")
        k2 = S.node("%sk2%s" % (p, side), mirror((0.36, y + tc * 0.78 * L, zc + 0.17), s), part, w=piv_w,
                    groups=[P.model_group("ulink_" + ax.id + side.upper()), P.model_group("sub_" + ax.id)], kind="piv")
        st = S.node("%ss1%s" % (p, side), mirror((xa, y, zt_spring), s), part, w=piv_w,
                    groups=[P.model_group("coil_" + ax.id + side.upper()), P.model_group("sub_" + ax.id)], kind="piv")
        for n in (k1, k2, st):
            n.coll, n.selfcoll = False, False
        # flexbody groups need three nodes that are not in a line
        for nm, grp in ((a["2" + side], "link_"), (a["1" + side], "link_"), (a["4"], "ulink_"), (a["3"], "ulink_"),
                        (a["1" + side], "coil_"), (a["2" + side], "coil_")):
            g = P.model_group(grp + ax.id + side.upper())
            if g not in S.nodes[nm].groups:
                S.nodes[nm].groups.append(g)
        if ax.steer:
            g = P.model_group("tie_" + ax.id + side.upper())
            if g not in S.nodes[h1.name].groups:
                S.nodes[h1.name].groups.append(g)
        S.beam(k1.name, a["2" + side], part, "link")
        S.beam(k2.name, a["4"], part, "link")
        for n in (k1.name, k2.name, st.name):
            S.attach(n, body_names, part, "mount", n=4)
        ax.mount_nodes += [k1.name, k2.name, st.name]
        ax.spring_ends[side] = (a["1" + side], st.name)
    # body-side link mounts tied across the car
    S.beam("%sk1l" % p, "%sk1r" % p, part, "sub")
    S.beam("%sk2l" % p, "%sk2r" % p, part, "sub")
    S.beam("%sk1l" % p, "%sk2r" % p, part, "sub")
    S.beam("%sk1r" % p, "%sk2l" % p, part, "sub")
    ax.arb = [a["1l"], "%sk1l" % p, "%sk1r" % p, a["1r"]]
    ax.arb_lever = _lever(S, a["1l"], "%sk1l" % p, "%sk1r" % p)
    _wheel_attach(S, ax, part)
    lo, hi = ax.spring_ends["l"]
    ax.mr = abs(v_norm(v_sub(S.pos(hi), S.pos(lo)))[2])


def _wheel_attach(S, ax, part):
    """Beams from the knuckle to the wheel's axle nodes (defined by the wheel part)."""
    for c in ax.corners:
        side = c.side
        inner, outer = ax.wheel_nodes[side]
        hub = ax.hub[side]
        wname = ax.id + side.upper()
        for i, h in enumerate(("h1", "h2", "h3", "h5")):
            for j, w in enumerate((inner, outer)):
                extra = {"breakGroup": "wheel_" + wname, "optional": True}
                if i == 0 and j == 0:
                    extra["name"] = "axle_" + wname
                S.beam(hub[h], w, part, "wheel", **extra)


def _lever(S, arm, ax0, ax1):
    """Distance of node arm from the line ax0-ax1."""
    p, a, b = S.pos(arm), S.pos(ax0), S.pos(ax1)
    d = v_norm(v_sub(b, a))
    v = v_sub(p, a)
    perp = v_sub(v, v_mul(d, v_dot(v, d)))
    return max(v_len(perp), 0.05)


def _camber_k(S, h1, h2, piv):
    """Change of the upper arm's length factor per degree of camber."""
    kp = dist(S.pos(h1), S.pos(h2))
    arm = dist(S.pos(h2), S.pos(piv))
    return round(kp * math.pi / 180 / max(arm, 0.05), 5)


def _steer_opts(S, inner, h3, h1, h2, angle_deg, s):
    """Hydro options for a tie rod: positive steering input turns right (BeamNG convention).

    The steering arm (h3) is behind the kingpin; turning right swings it toward +x, so the
    right tie rod gets shorter and the left one longer."""
    kp_mid = v_mul(v_add(S.pos(h1), S.pos(h2)), 0.5)
    arm = abs(S.pos(h3)[1] - kp_mid[1]) or 0.12
    length = dist(S.pos(inner), S.pos(h3))
    f = arm * math.sin(math.radians(angle_deg)) / max(length, 0.05)
    f = round(clamp(f, 0.02, 0.9), 4)
    return {"factor": f if s > 0 else -f, "steeringWheelLock": 450, "inRate": 1.8, "outRate": 1.8,
            "inputSource": "steering_input"}


def _fmt(v):
    return ("%.5f" % v).rstrip("0").rstrip(".")


# ---------------------------------------------------------------------------
# engine
# ---------------------------------------------------------------------------

def build_engine(S, sh, P, axles, body_names, part="engine"):
    hp = P.power
    ew = clamp(0.46 + 0.00028 * hp, 0.46, 0.8)
    el = clamp(0.5 + 0.00035 * hp, 0.5, 0.95)
    eh = clamp(0.44 + 0.00018 * hp, 0.44, 0.66)
    aF, aR = axles["F"], axles["R"]
    if P.enginePos == "front":
        yc = aF.y + (0.14 if P.drive != "FWD" else -0.02)
        yc = clamp(yc, sh.y0 + el / 2 + 0.12, sh.y_cowl + 0.3 - el / 2)
    elif P.enginePos == "mid":
        yc = aR.y - el / 2 - 0.3
    else:
        yc = aR.y + 0.32
        yc = min(yc, sh.y1 - el / 2 - 0.12)
    z0 = max(P.clearance + 0.07, min(aF.zc, aR.zc) - 0.18)
    room = min(sh.top(yc - el / 2), sh.top(yc), sh.top(yc + el / 2)) - 0.05 - z0
    if room < eh:
        eh = max(0.28, room)
        if room < 0.28:
            S.notes.append("The engine does not fit under the hood: it sticks out of the body.")
    hw = min(ew / 2, sh.x_in() - 0.03)
    m_e = clamp(70 + 0.38 * hp + (25 if P.gearbox == "automatic" else 0), 80, 480)
    w = m_e / 8.0
    grp = [P.model_group("engine")]
    names = {}
    for tag, dy, dz in (("1", -1, 0), ("2", 1, 0), ("3", -1, 1), ("4", 1, 1)):
        for side, s in SIDES:
            nm = "e%s%s" % (tag, side)
            n = S.node(nm, (hw * s, yc + dy * el / 2, z0 + dz * eh), part, w=w, groups=grp, kind="engine")
            n.coll, n.selfcoll = True, False
            names[tag + side] = nm
    allx = list(names.values())
    for i in range(len(allx)):
        for j in range(i + 1, len(allx)):
            S.beam(allx[i], allx[j], part, "engine")
    for tag in ("1l", "1r", "2l", "2r"):
        S.attach(names[tag], body_names, part, "engine_mount", n=3)
    for tag in ("3l", "3r", "4l", "4r"):
        S.attach(names[tag], body_names, part, "engine_mount", n=2)
    S.engine = {"y": yc, "z0": z0, "w": hw * 2, "l": el, "h": eh, "mass": m_e, "names": names}
    return names


# ---------------------------------------------------------------------------
# masses and stability
# ---------------------------------------------------------------------------

def beam_kinds(P):
    s = clamp(P.mass / 1500.0, 0.35, 4.0)
    ks = s ** 0.5
    return {
        "hub": dict(beamSpring=round(7e6 * ks, -3) + 1000, beamDamp=100, beamDeform=round(60000 * ks),
                    beamStrength=round(350000 * ks)),
        "wheel": dict(beamSpring=round(6e6 * ks, -3) + 1000, beamDamp=80, beamDeform=round(60000 * ks),
                      beamStrength=round(170000 * ks)),
        "arm": dict(beamSpring=round(5.5e6 * ks, -3) + 1000, beamDamp=300, beamDeform=round(50000 * ks),
                    beamStrength=round(375000 * ks)),
        "arm_up": dict(beamSpring=round(5.5e6 * ks, -3) + 1000, beamDamp=300, beamDeform=round(50000 * ks),
                       beamStrength=round(375000 * ks)),
        "link": dict(beamSpring=round(6e6 * ks, -3) + 1000, beamDamp=250, beamDeform=round(70000 * ks),
                     beamStrength=round(400000 * ks)),
        "steer": dict(beamSpring=round(5e6 * ks, -3) + 1000, beamDamp=60, beamDeform=FLT_MAX,
                      beamStrength=round(160000 * ks)),
        "sub": dict(beamSpring=round(5e6 * ks, -3) + 1000, beamDamp=150, beamDeform=round(80000 * ks),
                    beamStrength=FLT_MAX),
        "mount": dict(beamSpring=round(4e6 * ks, -3) + 1000, beamDamp=150, beamDeform=round(60000 * ks),
                      beamStrength=FLT_MAX),
        "axle": dict(beamSpring=round(8e6 * ks, -3) + 1000, beamDamp=150, beamDeform=round(120000 * ks),
                     beamStrength=FLT_MAX),
        "anti_inv": dict(beamType="|SUPPORT", beamLongBound=2.0, beamSpring=round(2.5e6 * ks, -3) + 1000,
                         beamDamp=100, beamDeform=round(50000 * ks), beamStrength=round(250000 * ks)),
        "engine": dict(beamSpring=round(9e6 * ks, -3) + 1000, beamDamp=250, beamDeform=200000, beamStrength=FLT_MAX),
        "engine_mount": dict(beamSpring=round(5e5 * ks, -3) + 1000, beamDamp=round(2500 * ks),
                             beamDeform=round(20000 * ks), beamStrength=round(60000 * ks)),
    }


def assign_masses(S, P, axles, wheels):
    """Spread the design weight over the nodes, then make every node heavy enough to be stable."""
    total = P.mass
    fixed = 0.0
    for n in S.nodes.values():
        if n.kind in ("hub", "piv", "axle", "engine", "wheel"):
            fixed += n.w
    fixed += sum(w["generated_mass"] for w in wheels.values())
    body = [n for n in S.nodes.values() if n.kind in ("fc", "fl", "sl", "bl", "tl", "tc")]
    budget = total - fixed
    floor_min = 2.0 * len(body)
    if budget < 0.28 * total:
        budget = max(0.28 * total, floor_min)
    factor = {"fc": 1.35, "fl": 1.6, "sl": 1.25, "bl": 1.0, "tl": 0.6, "tc": 0.55}
    ys = sorted({round(n.p[1], 4) for n in body})
    span = {}
    for i, y in enumerate(ys):
        lo = ys[i - 1] if i > 0 else y
        hi = ys[i + 1] if i + 1 < len(ys) else y
        span[y] = max((hi - lo) / 2, 0.08)
    L = max(P.length, 1.0)
    raw = {}
    for n in body:
        bias = 1.0 + 2.2 * P.weightBias * (n.p[1] / L)
        raw[n.name] = factor[n.kind] * span[round(n.p[1], 4)] * max(bias, 0.3)
    tot = sum(raw.values()) or 1.0
    for n in body:
        n.w = budget * raw[n.name] / tot
    added = stabilize(S, wheels)
    return added


def node_loads(S):
    """Sum of spring and damping of the beams on each node."""
    k = {n: 0.0 for n in S.nodes}
    c = {n: 0.0 for n in S.nodes}
    for b in S.beams:
        opts = dict(S.kinds.get(b.kind, {}))
        opts.update(b.extra)
        ks = _beam_num(opts.get("beamSpring"), 0.0)
        kl = _beam_num(opts.get("beamLimitSpring"), 0.0)
        cs = max(_beam_num(opts.get("beamDamp"), 0.0), _beam_num(opts.get("beamDampRebound"), 0.0),
                 _beam_num(opts.get("beamLimitDamp"), 0.0))
        for x in (b.a, b.b):
            if x in k:
                k[x] += ks + kl
                c[x] += cs
    return k, c


def _beam_num(v, d):
    if isinstance(v, bool):
        return d
    if isinstance(v, (int, float)):
        return float(v)
    return d


def stabilize(S, wheels, safety=1.5):
    k, c = node_loads(S)
    for w in wheels.values():
        for nm, extra_k in w.get("axle_load", {}).items():
            if nm in k:
                k[nm] += extra_k
    added = 0.0
    for name, n in S.nodes.items():
        need = max(k[name] * DT * DT / 4.0, c[name] * DT / 2.0) * safety
        if n.w < need:
            added += need - n.w
            n.w = need
    S.stability_added = added
    return added


def stability_report(S, wheels):
    """[(node, weight, needed)] for nodes below the stable weight (should be empty)."""
    k, c = node_loads(S)
    for w in wheels.values():
        for nm, extra_k in w.get("axle_load", {}).items():
            if nm in k:
                k[nm] += extra_k
    out = []
    for name, n in S.nodes.items():
        need = max(k[name] * DT * DT / 4.0, c[name] * DT / 2.0)
        if n.w + 1e-9 < need:
            out.append((name, n.w, need))
    return out
