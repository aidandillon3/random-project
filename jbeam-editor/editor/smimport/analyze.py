"""Working out what a Scrap Mechanic creation is made of: chassis, wheels, suspension, steering, drive.

A wheel is a wheel part on a body of its own, held by a bearing. Walking from
that bearing back to the chassis tells the rest: a suspension joint on the way
means the wheel is sprung, a bearing the driver's seat controls means it
steers, and the wheel bearing being on an engine's list means it is driven.
Everything else joined to the chassis (pistons, free bearings) is fixed in
place, the way the creation was saved.

The creation is turned so the wheels' axles run left-right and the steered
wheels are at the front (BeamNG: +x left, -y forward, z up), with the ground
under the lowest tire at z = 0 and y = 0 halfway between the outer axles.
"""
import math

from .blueprint import cross
from .game import CELL

# hp of a Scrap Mechanic engine by level (gas and electric engines alike)
ENGINE_HP = {0: 120, 1: 60, 2: 95, 3: 140, 4: 210, 5: 300}


def dot(a, b):
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]


def norm(a):
    n = math.sqrt(dot(a, a)) or 1.0
    return (a[0] / n, a[1] / n, a[2] / n)


class Frame:
    """Turns creation cells into vehicle coordinates; only quarter turns about the vertical."""

    def __init__(self, fwd):
        u = (0, 0, 1)
        left = cross(u, fwd)
        self.fwd = fwd
        self.rows = (left, tuple(-v for v in fwd), u)
        self.rowsum = tuple(sum(r) for r in self.rows)
        self.origin = (0.0, 0.0, 0.0)     # vehicle point of the creation's (0,0,0), in metres
        self.scale = CELL

    def rot(self, v):
        return tuple(dot(r, v) for r in self.rows)

    def point(self, p):
        """A point (in cells) to metres in the vehicle."""
        r = self.rot(p)
        return tuple(r[k] * self.scale + self.origin[k] for k in range(3))

    def vec(self, v):
        return self.rot(v)

    def box(self, lo, hi):
        a, b = self.rot(lo), self.rot(hi)
        return tuple(min(a[k], b[k]) for k in range(3)), tuple(max(a[k], b[k]) for k in range(3))

    def cell(self, c):
        """The rotated cell (integer lattice) a creation cell lands in."""
        v = self.rot(c)
        return tuple(int(round(v[k])) - (1 if self.rowsum[k] < 0 else 0) for k in range(3))


class Wheel:
    def __init__(self, shape):
        self.shape = shape
        self.body = shape.body
        self.bearing = None
        self.carrier = None          # body the wheel's bearing sits on
        self.path = []               # joints from the wheel bearing back to the chassis
        self.unsprung = []           # bodies between the chassis and the wheel
        self.suspension = None
        self.steer_joint = None
        self.steer_angle = None
        self.steered = False
        self.driven = False
        self.center = shape.center   # cells, creation frame
        self.axis = (1, 0, 0)
        self.radius = 1.5            # cells
        self.width = 1.0
        # vehicle space, set once the frame is known
        self.p = (0.0, 0.0, 0.0)
        self.r = 0.375
        self.w = 0.25

    def kind(self):
        if self.suspension is None:
            return "none"
        info = self.suspension.info
        if info is not None and info.extra.get("offroad"):
            return "offroad"
        if info is not None and info.extra.get("sport"):
            return "sport"
        return "generic"


class Analysis:
    def __init__(self, bp, name=""):
        self.bp = bp
        self.name = name
        self.notes = []
        self.wheels = []
        self.chassis = set()
        self.left_out = set()
        self.engines = []
        self.seats = []
        self.fuel = []
        self.frame = None
        self._run()

    # -- graph ---------------------------------------------------------------------------
    def _run(self):
        bp = self.bp
        nb = len(bp.bodies)
        mass = [0.0] * nb
        for s in bp.shapes:
            mass[s.body] += s.mass
        self.body_mass = mass
        edges = {i: [] for i in range(nb)}
        for j in bp.joints:
            ba, bb = bp.body_of(j.a), bp.body_of(j.b)
            if ba < 0 or bb < 0 or ba == bb:
                continue
            edges[ba].append((bb, j))
            edges[bb].append((ba, j))
        self.edges = edges
        # wheels: wheel parts on a body that a joint holds
        wheel_bodies = {}
        for s in bp.shapes:
            if s.role == "wheel" and edges[s.body]:
                wheel_bodies.setdefault(s.body, []).append(s)
        # unknown parts alone on a jointed body: probably a modded wheel
        for bi, members in enumerate(bp.bodies):
            if bi in wheel_bodies or len(members) != 1 or not edges[bi]:
                continue
            s = bp.shapes[members[0]]
            if s.role == "unknown" and any(j.role in ("bearing", "unknown") for _, j in edges[bi]):
                wheel_bodies[bi] = [s]
        for s in bp.shapes:
            if s.role == "engine":
                self.engines.append(s)
            elif s.role == "driverseat":
                self.seats.append(s)
            elif s.role == "fuel":
                self.fuel.append(s)
        if not self.seats:
            self.seats = [s for s in bp.shapes if s.role == "seat"]
        # chassis: the heaviest body that is not a wheel
        cands = [i for i in range(nb) if i not in wheel_bodies]
        if not cands:
            raise ValueError("this creation is only wheels")
        root = max(cands, key=lambda i: mass[i])
        self.root = root
        # walk out from the chassis without passing through wheels
        parent = {root: None}
        order = [root]
        k = 0
        while k < len(order):
            b = order[k]
            k += 1
            for nbody, j in edges[b]:
                if nbody in parent or nbody in wheel_bodies:
                    continue
                parent[nbody] = (b, j)
                order.append(nbody)
        self.parent = parent
        unsprung = set()
        for bi, shapes in wheel_bodies.items():
            # the joint that holds the wheel: prefer a bearing on a body we reached
            holds = [(nb_, j) for nb_, j in edges[bi] if nb_ in parent]
            if not holds:
                self.notes.append("A wheel is not connected to the rest of the creation and was left out.")
                continue
            holds.sort(key=lambda t: (t[1].role != "bearing", t[1].role == "suspension"))
            carrier, bearing = holds[0]
            path = [bearing]
            chain = []
            b = carrier
            while parent.get(b) is not None:
                chain.append(b)
                pb, pj = parent[b]
                path.append(pj)
                b = pb
            for s in shapes:
                w = Wheel(s)
                w.bearing = bearing
                w.carrier = carrier
                w.path = path
                w.unsprung = list(chain)
                if not self._wheel_shape(w):
                    continue
                for j in path:
                    if j.role == "suspension" and w.suspension is None:
                        w.suspension = j
                self.wheels.append(w)
        # bodies between a suspension joint and its wheel move with the wheel; the rest is chassis
        for w in self.wheels:
            if w.suspension is None:
                w.unsprung = []
                continue
            keep = []
            for b in w.unsprung:
                keep.append(b)
                if parent[b][1] is w.suspension:
                    break
            w.unsprung = keep
            unsprung.update(keep)
        self.unsprung_bodies = unsprung
        for b in order:
            if b not in unsprung and b not in wheel_bodies:
                # a body hanging off an unsprung one moves with it
                x = b
                hang = False
                while parent.get(x) is not None:
                    x = parent[x][0]
                    if x in unsprung:
                        hang = True
                        break
                if hang:
                    unsprung.add(b)
                else:
                    self.chassis.add(b)
        self.left_out = {i for i in range(nb) if i not in self.chassis and i not in unsprung and i not in wheel_bodies}
        lost = [i for i in self.left_out if i not in wheel_bodies and not any(nb_ in wheel_bodies for nb_, _ in edges[i])]
        if lost:
            self.notes.append("%d loose piece%s not joined to the vehicle %s left out." % (
                len(lost), "s" if len(lost) > 1 else "", "were" if len(lost) > 1 else "was"))
        if any(j.role == "piston" for b in self.chassis for _, j in edges[b]):
            self.notes.append("Pistons are fixed at the length they were saved with.")
        self._controls()
        if bp.unknown:
            n = sum(1 for x in bp.shapes if x.info is None and x.kind != "block") + sum(
                1 for j in bp.joints if j.info is None)
            self.notes.append("%d part%s from mods that are not installed %s built as plain blocks." % (
                n, "s" if n != 1 else "", "are" if n != 1 else "is"))
        if not self.wheels:
            raise ValueError("no wheels on bearings were found: Scrap Mechanic wheels need to sit on bearings "
                             "(or suspension) to become vehicle wheels")

    def _wheel_shape(self, w):
        """Spin axis, radius and width of a wheel (cells). False if it lies flat."""
        s = w.shape
        info = s.info
        ext = [s.hi[k] - s.lo[k] for k in range(3)]
        if info is not None and info.collision == "cylinder":
            axis = s.own_axis(info.axis)
            size = list(s.size)
            depth = size.pop(info.axis)
            w.radius = max(size) / 2.0
            w.width = depth
        else:
            a = w.bearing.axis if w.bearing is not None else None
            if a is None:
                k = min(range(3), key=lambda i: ext[i])
                a = tuple(1 if i == k else 0 for i in range(3))
            axis = a
            k = max(range(3), key=lambda i: abs(axis[i]))
            w.width = ext[k]
            w.radius = max(ext[i] for i in range(3) if i != k) / 2.0
        w.axis = norm(tuple(float(v) for v in axis))
        if abs(w.axis[2]) > 0.7:
            self.notes.append("A wheel lying flat (turning about the vertical) is not used as a wheel.")
            return False
        w.radius = max(w.radius, 0.5)
        w.width = max(w.width, 0.4)
        return True

    def _controls(self):
        bp = self.bp
        by_id = {}
        for j in bp.joints:
            by_id.setdefault(j.id, j)
            if j.controller and "id" in j.controller:
                by_id.setdefault(("c", j.controller["id"]), j)

        def joint_of(ref):
            return by_id.get(ref) or by_id.get(("c", ref))
        steer = {}
        for s in self.seats:
            for ref, e in bp.controlled_joints(s):
                j = joint_of(ref)
                if j is not None:
                    steer[j.idx] = e
        drive = set()
        for s in self.engines:
            for ref, e in bp.controlled_joints(s):
                j = joint_of(ref)
                if j is not None:
                    drive.add(j.idx)
        for w in self.wheels:
            for j in w.path:
                if j.idx in steer:
                    w.steered = True
                    w.steer_joint = j
                    e = steer[j.idx]
                    lim = [e.get(k) for k in ("leftAngleLimit", "rightAngleLimit", "angleLimit") if
                           isinstance(e.get(k), (int, float))]
                    if lim:
                        w.steer_angle = max(lim)
                    break
            w.driven = w.bearing is not None and w.bearing.idx in drive

    # -- turning it into a vehicle ---------------------------------------------------------------
    def orient(self, turn=0, scale=1.0):
        """Pick the frame: wheel axles left-right, steered wheels at the front. turn: extra quarter turns.

        Wheels that roll the other way, or hang clear of the ground without being steered or driven (a
        spare on the roof), are left out of the wheels the vehicle stands on (self.used / self.decor)."""
        ws = self.wheels
        lat_x = sum(1 for w in ws if abs(w.axis[0]) >= abs(w.axis[1]))
        lateral_is_x = lat_x * 2 >= len(ws)
        lat = 0 if lateral_is_x else 1
        aligned = [w for w in ws if abs(w.axis[lat]) >= 0.7] or ws
        ground = min(w.center[2] - w.radius for w in aligned)
        used = [w for w in aligned if (w.center[2] - w.radius) - ground <= max(0.5, 0.35 * w.radius)
                or w.steered or w.driven]
        self.used = used
        self.decor = [w for w in ws if w not in used]
        fwd_axis = 1 if lateral_is_x else 0
        cen = [sum(w.center[k] for w in used) / len(used) for k in range(3)]
        steered = [w for w in used if w.steered]
        sign = 1
        self.front_guess = "default"
        if steered and len(steered) < len(used):
            sc = sum(w.center[fwd_axis] for w in steered) / len(steered)
            if abs(sc - cen[fwd_axis]) > 0.5:
                sign = 1 if sc > cen[fwd_axis] else -1
                self.front_guess = "steering"
        fwd = [0, 0, 0]
        fwd[fwd_axis] = sign
        for _ in range(int(turn) % 4):
            # a quarter turn to the left about the vertical
            fwd = [-fwd[1], fwd[0], 0]
        fr = Frame(tuple(fwd))
        fr.scale = CELL * scale
        self.frame = fr
        # wheel positions in metres (before centring)
        for w in ws:
            w.p = fr.point(w.center)
            w.r = w.radius * fr.scale
            w.w = w.width * fr.scale
        ground = min(w.p[2] - w.r for w in used)
        xs = [w.p[0] for w in used]
        ys = [w.p[1] for w in used]
        origin = (-(max(xs) + min(xs)) / 2, -(max(ys) + min(ys)) / 2, -ground)
        fr.origin = origin
        for w in ws:
            w.p = fr.point(w.center)
        return fr

    def bounds(self, bodies=None):
        """(lo, hi) in metres of the given bodies' shapes (default: chassis, unsprung and wheels)."""
        fr = self.frame
        if bodies is None:
            bodies = set(self.chassis) | set(self.unsprung_bodies) | {w.body for w in self.wheels}
        lo = [1e9] * 3
        hi = [-1e9] * 3
        for s in self.bp.shapes:
            if s.body not in bodies:
                continue
            a, b = fr.point(s.lo), fr.point(s.hi)
            for k in range(3):
                lo[k] = min(lo[k], a[k], b[k])
                hi[k] = max(hi[k], a[k], b[k])
        return tuple(lo), tuple(hi)

    def engine_power(self):
        if not self.engines:
            return None
        hp = 0.0
        for e in self.engines:
            lvl = e.info.level if e.info else 0
            hp += ENGINE_HP.get(min(max(lvl, 0), 5), 120)
        return hp

    def mass(self, scale=1.0):
        bodies = set(self.chassis) | set(self.unsprung_bodies) | {w.body for w in self.wheels}
        m = sum(s.mass for s in self.bp.shapes if s.body in bodies)
        jm = sum(j.mass for j in self.bp.joints if self.bp.body_of(j.a) in bodies)
        return (m + jm) * scale ** 3

    def summary(self):
        bp = self.bp
        blocks = sum(1 for s in bp.shapes if s.kind == "block")
        susp = {}
        for w in self.wheels:
            susp[w.kind()] = susp.get(w.kind(), 0) + 1
        return {
            "name": self.name, "blocks": blocks, "parts": len(bp.shapes) - blocks, "joints": len(bp.joints),
            "bodies": len(bp.bodies), "wheels": len(self.wheels),
            "steered": sum(1 for w in self.wheels if w.steered), "driven": sum(1 for w in self.wheels if w.driven),
            "suspension": susp, "engines": [e.title() for e in self.engines],
            "enginePower": self.engine_power(), "seat": bool(self.seats), "unknown": len(bp.unknown),
            "frontFrom": getattr(self, "front_guess", "default"),
        }
