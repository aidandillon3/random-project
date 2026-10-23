"""A Scrap Mechanic creation as a drivable BeamNG vehicle.

The chassis (every body that does not move with a wheel) becomes a rigid node
lattice that follows its blocks (voxel.py), drawn with its own blocks and
colours. Every wheel gets a real BeamNG wheel where it was, on a double
wishbone with a coilover; Scrap Mechanic suspension sets how soft and long
that is, bearings the driver's seat controls steer, and wheels on the engines'
bearings are driven. The engine sits where the creation's engine is. Springs,
dampers, gearing and brakes are worked out from the weight like the Vehicle
Builder does, and every setting can be changed before the mod is written.
"""
import math

from .. import vehiclegen as vg
from ..vehiclegen import chassis, lights, mesh as vmesh, parts as vparts, spec as specmod
from ..vehiclegen.chassis import FLT_MAX, Corner, Structure, clamp, dist, v_len, v_sub
from ..vehiclegen.shape import lerp
from ..vehiclegen.spec import _b, _c, _col, _r
from . import smmesh, voxel

DESIGN_FILE = "jbeam_editor_scrap.json"
VERSION = 1

GROUPS = [
    {"id": "fit", "title": "Size and direction", "icon": "ruler"},
    {"id": "wheels", "title": "Wheels and suspension", "icon": "wheel"},
    {"id": "engine", "title": "Engine and drive", "icon": "engine"},
    {"id": "weight", "title": "Weight and brakes", "icon": "weight"},
]

FIELDS = [
    _c("turn", "Front of the vehicle", "fit", [["0", "As found"], ["2", "Turned around"]],
       "Which end is the front. It is found from the steered wheels; turn it around if it faces the wrong way."),
    _r("scale", "Size", "fit", "×", 0.5, 2.5, 0.05,
       "1× is Scrap Mechanic's own size, where a block is 25 cm. The weight follows the size."),
    _c("steer", "Steering", "wheels", [["built", "As built"], ["front", "Front wheels"], ["all", "Front and rear"]],
       "As built: the wheels on bearings your driver's seat is connected to."),
    _r("steerAngle", "Steering angle", "wheels", "°", 15, 55, 1, "How far the wheels turn."),
    _r("travel", "Wheel travel", "wheels", "m", 0.04, 0.5, 0.005,
       "How far the wheels move up and down. Off-road suspension starts long, sport suspension short."),
    _r("springHz", "Springs", "wheels", "Hz", 0.8, 3.5, 0.05, "Soft and comfortable (low) to firm (high).",
       lowLabel="Soft", highLabel="Firm"),
    _r("damping", "Damping", "wheels", "", 0.15, 1.0, 0.01, "Bouncy (low) to tightly controlled (high).", "more",
       display="percent"),
    _r("swaybar", "Anti-roll bars", "wheels", "", 0.0, 1.0, 0.01, "Less body roll in corners.", "more",
       display="percent"),
    _c("axles", "Axles", "wheels", [["built", "As built"], ["independent", "All independent"]],
       "As built: two wheels on one sprung beam become a solid axle, the way they move in Scrap Mechanic.", "more"),
    _c("tireType", "Tires", "wheels", specmod.TIRE_TYPES, "Grip on the road against grip in the dirt."),
    _r("pressure", "Tire pressure", "wheels", "psi", 8, 65, 0.5, "", "more"),
    _c("rimStyle", "Rim style", "wheels", specmod.RIM_STYLES, "", "more"),
    _col("rimColor", "Rim colour", "wheels", "", "more"),
    _r("power", "Power", "engine", "hp", 20, 1500, 5, "Starts at what the creation's engines would give."),
    _c("drive", "Driven wheels", "engine", [["built", "As built"], ["rear", "Rear"], ["front", "Front"], ["all", "All"]],
       "As built: the wheels on bearings your engines are connected to."),
    _c("gearbox", "Gearbox", "engine", [["automatic", "Automatic"], ["manual", "Manual"]]),
    _r("topSpeed", "Top speed", "engine", "km/h", 50, 350, 5, "Sets the gearing."),
    _r("gears", "Gears", "engine", "", 3, 8, 1, "", "more"),
    _r("redline", "Redline", "engine", "rpm", 2500, 12000, 100, "", "more"),
    _r("torqueShape", "Engine character", "engine", "", 0.0, 1.0, 0.01,
       "Low-end pull like a truck, or high-revving like a race engine.", "more",
       lowLabel="Low-end pull", highLabel="High-revving"),
    _c("sound", "Engine sound", "engine", [["i4", "Inline 4"], ["b4", "Flat 4"]], "", "more"),
    _c("diff", "Differentials", "engine", [["open", "Open"], ["lsd", "Limited slip"], ["locked", "Locked"]], "", "more"),
    _r("fuel", "Fuel tank", "engine", "L", 5, 300, 1, "", "more"),
    _r("mass", "Weight", "weight", "kg", 100, 30000, 10, "Starts at what the creation weighs in Scrap Mechanic."),
    _r("brakePower", "Brake power", "weight", "×", 0.4, 2.0, 0.05, "", "more"),
    _r("brakeBias", "Front brake share", "weight", "%", 0.4, 0.85, 0.01, "", "more", display="percent"),
    _b("abs", "Anti-lock brakes (ABS)", "weight"),
]
FIELD = {f["key"]: f for f in FIELDS}

# wheel travel (m), spring frequency (Hz) and damping by the kind of suspension a wheel had
SUSPENSION = {"offroad": (0.3, 1.3, 0.35), "sport": (0.16, 2.1, 0.45), "generic": (0.22, 1.6, 0.4),
              "none": (0.1, 2.5, 0.45)}


def defaults(an):
    kinds = [w.kind() for w in an.wheels]
    main = max(sorted(set(kinds)), key=kinds.count) if kinds else "none"
    travel, hz, damp = SUSPENSION[main]
    angles = [w.steer_angle for w in an.wheels if w.steer_angle]
    angle = clamp(round(max(angles)), 15, 55) if angles else 32
    hp = an.engine_power()
    r = max((w.radius for w in an.wheels), default=1.5) * 0.25
    return {
        "turn": "0", "scale": 1.0,
        "steer": "built" if any(w.steered for w in an.wheels) else "front", "steerAngle": angle,
        "travel": travel, "springHz": hz, "damping": damp, "swaybar": 0.3,
        "axles": "built", "tireType": "allterrain" if main == "offroad" else "street",
        "pressure": round(clamp(30 - (r - 0.35) * 30, 12, 34) * 2) / 2,
        "rimStyle": "steel", "rimColor": "#b8bcc0",
        "power": round(clamp(hp or 120, 20, 1500) / 5) * 5,
        "drive": "built" if any(w.driven for w in an.wheels) else "rear", "gearbox": "automatic", "topSpeed": 150,
        "gears": 5, "redline": 6000, "torqueShape": 0.4, "sound": "i4", "diff": "open", "fuel": 50,
        "mass": round(clamp(an.mass(1.0), 100, 30000)), "brakePower": 1.0, "brakeBias": 0.62, "abs": True,
    }


def clean(settings, an):
    """(settings with every field present and in range, the creation's defaults)."""
    settings = settings if isinstance(settings, dict) else {}
    d = defaults(an)
    out = {}
    for f in FIELDS:
        k = f["key"]
        v = settings.get(k, d[k])
        if f["type"] == "range":
            x = min(max(specmod._num(v, d[k]), f["min"]), f["max"])
            if f["step"] >= 1 and float(f["step"]).is_integer():
                x = round(x)
            out[k] = x
        elif f["type"] == "choice":
            out[k] = v if v in [o[0] for o in f["options"]] else d[k]
        elif f["type"] == "bool":
            out[k] = bool(v) if isinstance(v, (bool, int)) else bool(d[k])
        elif f["type"] == "color":
            out[k] = specmod._color(v, d[k])
    out["massAuto"] = bool(settings.get("massAuto", True))
    if out["massAuto"]:
        out["mass"] = round(clamp(an.mass(out["scale"]), 100, 30000))
    return out, d


def schema(an):
    d = defaults(an)
    return {"groups": GROUPS, "fields": FIELDS, "defaults": d}


class SMAxle:
    """One axle (or one lone wheel) of an imported creation; like chassis.Axle, per corner."""

    def __init__(self, gen, ax_id, word, wheels, front):
        st = gen.st
        self.id = ax_id
        self.p = ax_id.lower()
        self.word = word
        self.front = front
        self.solid = False
        self.wheels = wheels
        n = len(wheels)
        self.y = sum(w.p[1] for w in wheels) / n
        self.zc = sum(w.p[2] for w in wheels) / n
        self.r = max(w.r for w in wheels)
        self.tw = max(w.w for w in wheels)
        self.k = clamp(self.r / 0.33, 0.8, 1.6)
        self.travel = st["travel"]
        self.bump = self.travel * 0.55
        self.droop = self.travel * 0.45
        self.toward_center = 1 if self.y < 0 else -1
        self.steer = False
        self.steer_sign = 1
        self.steer_angle = st["steerAngle"]
        self.driven = False
        self.corners = []
        self.centers = {}
        for w in sorted(wheels, key=lambda w: -w.p[0]):
            s = 1 if (w.p[0] >= 0 if n == 1 else w is max(wheels, key=lambda x: x.p[0])) else -1
            c = Corner(self, s)
            c.wheel = w
            c.unsprung_mass = 0.0
            self.corners.append(c)
            self.centers[c.side] = (abs(w.p[0]), w.p[1], w.p[2])
        self.track = 2 * max(abs(w.p[0]) for w in wheels) if n == 1 else abs(wheels[0].p[0] - wheels[1].p[0])
        # middle of a pair (a solid axle is built about it)
        self.x0 = (wheels[0].p[0] + wheels[1].p[0]) / 2 if n == 2 else 0.0
        dia_in = self.r * 2 / specmod.INCH
        self.rim_in = int(clamp(round(dia_in * 0.6), 10, max(10, min(26, math.floor(dia_in - 3)))))
        self.freq = st["springHz"]
        self.zeta = st["damping"]
        self.swaybar = st["swaybar"]
        self.camber = 0.0
        self.pressure = st["pressure"]
        self.spring_ends = {}
        self.hub = {}
        self.arb = None
        self.mr = 1.0
        self.mount_nodes = []
        self.unsprung_nodes = []
        self.wheel_nodes = {}


class SMGenerator(vg.Generator):
    def __init__(self, an, settings, author="", source=None):
        self.an = an
        self.st, self.defaults = clean(settings, an)
        st = self.st
        raw = settings if isinstance(settings, dict) else {}
        spec = {k: st[k] for k in ("power", "redline", "torqueShape", "sound", "gearbox", "gears", "topSpeed", "diff",
                                   "fuel", "brakePower", "brakeBias", "abs", "tireType", "rimStyle", "rimColor",
                                   "steerAngle")}
        spec["camberF"] = spec["camberR"] = 0.0
        P, _ = specmod.normalize(spec)
        P.name = (str(raw.get("name") or "").strip()[:60]) or (an.name or "Scrap Mechanic creation")[:60]
        P.brand = (str(raw.get("brand") or "").strip()[:40]) or "Scrap Mechanic"
        P.model = specmod.model_id(raw.get("model") or P.name)
        P.author = str(author or raw.get("author") or "").strip()[:60]
        P.model_group = lambda s, m=P.model: m + "_" + s
        P.mass = float(st["mass"])
        P.power = float(st["power"])
        P.kw = P.power * 0.7457
        P.style = "scrap"
        P.metallic = 0.15
        self.P = P
        self.notes = list(an.notes)
        self.clean_spec = dict(st, name=P.name, brand=P.brand, model=P.model)
        self.source = dict(source or {})

    # -- the whole vehicle ---------------------------------------------------------------------
    def run(self):
        an, P, st = self.an, self.P, self.st
        fr = an.orient(int(st["turn"]), st["scale"])
        self.frame = fr
        self.chassis_bodies = set(an.chassis)
        for w in an.decor:
            self.chassis_bodies.add(w.body)
            self.chassis_bodies.update(w.unsprung)
        if an.decor:
            n = len(an.decor)
            self.notes.append("%d wheel%s that %s not stand on the ground (a spare?) %s part of the body." % (
                n, "s" if n > 1 else "", "do" if n > 1 else "does", "are" if n > 1 else "is"))
        self.S = S = Structure()
        S.kinds.update(chassis.beam_kinds(P))
        S.kinds.update(voxel.lattice_kinds(P.mass))
        S.kind("exhaust", beamSpring=301000, beamDamp=20, beamDeform=8000, beamStrength=30000)
        cells = self._cells()
        self.lattice = lat = voxel.Lattice(cells, fr.scale, fr.origin)
        lat.build(S, "body", [P.model_group("body")])
        body = [n for n in S.order if S.nodes[n].part == "body"]
        self.body_names = body
        self.lamps = self._lamps(body)
        self._make_axles()
        self.wheels = {}
        for ax in self.axle_list:
            self._wheel_nodes(ax)
        for ax in self.axle_list:
            if ax.solid:
                chassis.build_solid(S, None, P, ax, body, top=lat.nearest_cells_top)
            else:
                chassis.build_wishbone(S, None, P, ax, body, top=lat.nearest_cells_top)
        self._unsprung()
        self._engine(body)
        self.engine = dict(S.engine)
        self._exhaust(body)
        rest = [n for n in S.order if S.nodes[n].part != "body"]
        chassis.brace_degenerate(S, rest, body, kind="mount")
        self._drive_units()
        self._mass()
        self._springs()
        self._brakes()
        self._engine_curve()
        self._gearing()
        self._camera()
        self._refnodes()
        self._drag()
        self.meshes, self.materials, flex = self._meshes()
        self.susp_flex, self.coil_flex = flex["susp"], flex["coil"]
        self.engine_flex = [(P.model + "_exhaust", [P.model_group("body")])]
        self.parts = SMPartsBuilder(self).build()
        self._stats()
        self.notes += [n for n in S.notes if n not in self.notes]
        g = vg.Generated(self)
        g.design = (DESIGN_FILE, {"version": VERSION, "source": self.source, "settings": self.clean_spec})
        g.info_overrides = {"Body Style": "Scrap Mechanic creation", "Country": "Custom",
                            "Type": "Truck" if P.mass > 3500 else "Car",
                            "jbeamEditor": {"scrap": True, "version": VERSION}}
        g.config_info_overrides = {
            "Description": 'Imported from the Scrap Mechanic creation "%s" with the JBeam Editor.' % (an.name or P.name),
            "Drivetrain": P.drive}
        g.file_comment = ('%s, imported from Scrap Mechanic with the JBeam Editor.\nImport it again to change it; '
                          'edits made here are kept until you do.' % P.name)
        g.summary = self.summary()
        g.settings = self.clean_spec
        g.defaults = self.defaults
        return g

    # -- chassis cells ---------------------------------------------------------------------------
    def _cells(self):
        an, fr = self.an, self.frame
        k3 = self.st["scale"] ** 3
        cells = {}
        total = 0
        for s in an.bp.shapes:
            if s.body not in self.chassis_bodies:
                continue
            lo = [math.floor(s.lo[k] + 1e-6) for k in range(3)]
            hi = [max(math.ceil(s.hi[k] - 1e-6), lo[k] + 1) for k in range(3)]
            n = (hi[0] - lo[0]) * (hi[1] - lo[1]) * (hi[2] - lo[2])
            total += n
            if total > 600000:
                raise ValueError("this creation is too big to import (more than 600,000 blocks of space)")
            m = s.mass * k3 / n
            for i in range(lo[0], hi[0]):
                for j in range(lo[1], hi[1]):
                    for k in range(lo[2], hi[2]):
                        c = fr.cell((i, j, k))
                        e = cells.get(c)
                        if e is None:
                            cells[c] = [m, s.idx]
                        else:
                            e[0] += m
        for j in an.bp.joints:
            ba, bb = an.bp.body_of(j.a), an.bp.body_of(j.b)
            if ba in self.chassis_bodies and (bb in self.chassis_bodies or bb < 0):
                c = fr.cell(tuple(int(round(v)) for v in j.pos))
                e = cells.get(c)
                if e is None:
                    cells[c] = [j.mass * k3, -1]
                else:
                    e[0] += j.mass * k3
        if not cells:
            raise ValueError("the creation has no chassis to hold its wheels")
        return {c: (v[0], v[1]) for c, v in cells.items()}

    # -- axles -----------------------------------------------------------------------------------
    def _make_axles(self):
        an, st = self.an, self.st
        ws = sorted(an.used, key=lambda w: w.p[1])
        clusters = []
        for w in ws:
            if clusters and abs(w.p[1] - clusters[-1][0].p[1]) < 0.35:
                clusters[-1].append(w)
            else:
                clusters.append([w])
        groups = []
        for ci, cl in enumerate(clusters):
            left = sorted([w for w in cl if w.p[0] > 0.02], key=lambda w: w.p[0])
            right = sorted([w for w in cl if w.p[0] < -0.02], key=lambda w: -w.p[0])
            mid = [w for w in cl if -0.02 <= w.p[0] <= 0.02]
            pairs = []
            while left and right:
                a, b = left.pop(0), right.pop(0)
                if abs(a.r - b.r) < 0.03 and abs(a.w - b.w) < 0.06:
                    pairs.append([a, b])
                else:
                    pairs += [[a], [b]]
            pairs += [[w] for w in left + right + mid]
            groups += [(ci, g) for g in pairs]
        n_cl = len(clusters)
        axles = []
        count = {}
        mid_no = 0
        last_mid = None
        for ci, g in groups:
            if ci == 0 and n_cl > 1:
                base, word = "F", "Front"
            elif ci == n_cl - 1:
                base, word = "R", "Rear"
            else:
                if last_mid != ci:
                    mid_no += 1
                    last_mid = ci
                base, word = "M%d" % mid_no, "Middle" if n_cl == 3 else "Middle %d" % mid_no
            count[base] = count.get(base, 0) + 1
            ax_id = base if count[base] == 1 else "%s%d" % (base, count[base])
            if count[base] > 1:
                word = "%s %d" % (word, count[base])
            axles.append(SMAxle(self, ax_id, word, g, front=not axles))
        self.axle_list = axles
        self.axles = {ax.id: ax for ax in axles}
        if n_cl == 1:
            self.notes.append("All the wheels are on one axle, so the vehicle cannot stand on its own.")
        # steering
        mode = st["steer"]
        if not any(w.steered for w in an.used):
            if mode == "built":
                mode = "front"
            if mode == "front":
                self.notes.append("No steering was found (in Scrap Mechanic, connect the driver's seat to the front "
                                  "bearings): the front wheels steer.")
        first = [ax for ax in axles if ax.y <= axles[0].y + 0.35]
        for ax in axles:
            if mode == "built":
                ax.steer = any(w.steered for w in ax.wheels)
            elif mode == "front":
                ax.steer = ax in first
            else:
                ax.steer = True
            ax.steer_sign = 1 if ax.y < 0 or ax in first else -1
            if mode == "all" and ax.steer_sign < 0:
                ax.steer_angle = round(st["steerAngle"] * 0.6, 1)
        # driven wheels
        dmode = st["drive"]
        if not any(w.driven for w in an.used):
            if dmode == "built":
                dmode = "rear"
            if dmode == "rear":
                self.notes.append("No wheels are connected to an engine: the rear wheels are driven.")
        last = [ax for ax in axles if ax.y >= axles[-1].y - 0.35]
        for ax in axles:
            if dmode == "built":
                ax.driven = any(w.driven for w in ax.wheels)
            elif dmode == "rear":
                ax.driven = ax in last
            elif dmode == "front":
                ax.driven = ax in first
            else:
                ax.driven = True
        # two wheels on one sprung beam: a solid axle
        for ax in axles:
            if len(ax.wheels) == 2 and st["axles"] == "built":
                a, b = ax.wheels
                if a.suspension is not None and set(a.unsprung) & set(b.unsprung) and abs(a.p[2] - b.p[2]) < 0.05:
                    ax.solid = True
        driven = [ax for ax in axles if ax.driven]
        self.P.drive = "AWD" if len(driven) == len(axles) and len(axles) > 1 else (
            "FWD" if all(ax in first for ax in driven) and len(axles) > 1 else "RWD")
        self.P.steerAngle = st["steerAngle"]

    def _lamps(self, body):
        """Head and tail lights on the front and back of the blocks, so it can be driven at night."""
        S, lat = self.S, self.lattice
        lo, hi = lat.bounds()
        width = hi[0] - lo[0]
        lamps = []
        for kind, y_face, inward in (("head", lo[1], 0.3), ("tail", hi[1], -0.3)):
            for side, s_ in (("l", 1), ("r", -1)):
                x = clamp(s_ * min(0.6, max(0.25, 0.3 * width)), lo[0] + 0.12, hi[0] - 0.12)
                col = lat.column(x, y_face + inward, reach=0.35)
                z = col[0] + 0.6 * (col[1] - col[0]) if col else (lo[2] + hi[2]) / 2
                lamps.append((kind, side, (x, y_face, max(z, lo[2] + 0.1))))
        made = lights.add_lamps(S, self.P, lamps, body)
        chassis.brace_degenerate(S, [n for n in S.order if S.nodes[n].kind == "lamp"], body, kind="lamp")
        return made

    def _drive_units(self):
        """What each output of the gearbox turns: an axle's differential, two lone wheels' one, or a lone wheel."""
        driven = [ax for ax in self.axle_list if ax.driven]
        units, singles = [], []
        for ax in driven:
            if len(ax.corners) == 2:
                outs = [(1 if c.s > 0 else 2, ax, c) for c in sorted(ax.corners, key=lambda c: -c.s)]
                units.append({"id": ax.id, "word": vparts.axle_word(ax), "kind": "diff", "outs": outs, "y": ax.y})
            else:
                singles.append(ax)
        singles.sort(key=lambda a: a.y)
        while len(singles) >= 2:
            a, b = singles.pop(0), singles.pop(0)
            units.append({"id": a.id + b.id, "word": "%s and %s" % (vparts.axle_word(a), vparts.axle_word(b)),
                          "kind": "diff", "outs": [(1, a, a.corners[0]), (2, b, b.corners[0])], "y": (a.y + b.y) / 2})
        for a in singles:
            units.append({"id": a.id, "word": vparts.axle_word(a), "kind": "shaft", "outs": [(1, a, a.corners[0])],
                          "y": a.y})
        units.sort(key=lambda u: u["y"])
        self.drive_units = units

    def _unsprung(self):
        """Weight of the Scrap Mechanic parts that move with each wheel (its suspension's lower half)."""
        an, k3 = self.an, self.st["scale"] ** 3
        bm = an.body_mass
        seen = set()
        for ax in self.axle_list:
            for c in ax.corners:
                own = [b for b in c.wheel.unsprung if b not in seen]
                seen.update(own)
                c.carrier_bodies = own
                c.unsprung_mass = min(sum(bm[b] for b in own) * k3, 80.0)

    # -- engine, exhaust ---------------------------------------------------------------------------
    def _engine(self, body):
        S, P, an, fr = self.S, self.P, self.an, self.frame
        hp = P.power
        lo, hi = self.lattice.bounds()
        if an.engines:
            e = an.engines[0]
            a, b = fr.point(e.lo), fr.point(e.hi)
            elo = [min(a[k], b[k]) for k in range(3)]
            ehi = [max(a[k], b[k]) for k in range(3)]
            c = [(elo[k] + ehi[k]) / 2 for k in range(3)]
            size = [max((ehi[k] - elo[k]) * 0.85, 0.28) for k in range(3)]
        else:
            driven = [ax for ax in self.axle_list if ax.driven] or self.axle_list
            y = sum(ax.y for ax in driven) / len(driven)
            y = clamp(y + (-0.45 if y > 0 else 0.45), lo[1] + 0.3, hi[1] - 0.3)
            size = [0.5, 0.55, 0.42]
            col = self.lattice.column(0.0, y, reach=0.6)
            z = (col[0] if col else lo[2]) + size[2] / 2 + 0.05
            c = [0.0, y, z]
        hw, el, eh = size[0] / 2, size[1], size[2]
        z0 = c[2] - eh / 2
        m_e = clamp(70 + 0.38 * hp + (25 if P.gearbox == "automatic" else 0), 80, 480)
        w = m_e / 8.0
        grp = [P.model_group("engine")]
        names = {}
        for tag, dy, dz in (("1", -1, 0), ("2", 1, 0), ("3", -1, 1), ("4", 1, 1)):
            for side, s in chassis.SIDES:
                nm = "e%s%s" % (tag, side)
                n = S.node(nm, (c[0] + hw * s, c[1] + dy * el / 2, z0 + dz * eh), "engine", w=w, groups=grp,
                           kind="engine")
                n.coll, n.selfcoll = True, False
                names[tag + side] = nm
        allx = list(names.values())
        for i in range(len(allx)):
            for j in range(i + 1, len(allx)):
                S.beam(allx[i], allx[j], "engine", "engine")
        for tag in ("1l", "1r", "2l", "2r"):
            S.attach(names[tag], body, "engine", "engine_mount", n=3)
        for tag in ("3l", "3r", "4l", "4r"):
            S.attach(names[tag], body, "engine", "engine_mount", n=2)
        S.engine = {"y": c[1], "x": c[0], "z0": z0, "w": hw * 2, "l": el, "h": eh, "mass": m_e, "names": names}

    def _exhaust(self, body):
        S, P = self.S, self.P
        e = S.engine
        names = e["names"]
        S.nodes[names["2r"]].extra = {"isExhaust": "mainEngine"}
        lo, hi = self.lattice.bounds()
        y_tip = hi[1] - 0.05
        x_tip = clamp(e["x"] - 0.3, lo[0] + 0.12, hi[0] - 0.12)
        col = self.lattice.column(x_tip, y_tip, reach=0.4)
        z_tip = (col[0] if col else lo[2]) + 0.06
        y_mid = (e["y"] + e["l"] / 2 + y_tip) / 2
        col = self.lattice.column(x_tip, y_mid, reach=0.4)
        z_mid = max((col[0] if col else lo[2]) + 0.05, 0.08)
        n1 = S.node("ex1", (x_tip, y_mid, z_mid), "engine", w=2.0, groups=[P.model_group("body")], kind="piv")
        n0 = S.node("ex0", (x_tip, y_tip, z_tip), "engine", w=2.0, groups=[P.model_group("body")], kind="piv",
                    extra={"afterFireAudioCoef": 0.6, "afterFireVisualCoef": 0.6, "exhaustAudioMufflingCoef": 0.6})
        for n in (n1, n0):
            n.coll, n.selfcoll = False, False
        S.beam(names["2r"], "ex1", "engine", "exhaust", isExhaust="mainEngine")
        S.beam("ex1", "ex0", "engine", "exhaust", isExhaust="mainEngine")
        for nm in ("ex1", "ex0"):
            S.attach(nm, body, "engine", "exhaust", n=3)
        self.exhaust = [S.pos(names["2r"]), S.pos("ex1"), S.pos("ex0")]

    # -- weights and numbers -----------------------------------------------------------------------
    def _assign_masses(self):
        S, P = self.S, self.P
        body = self.body_names
        for ax in self.axle_list:
            for c in ax.corners:
                if c.unsprung_mass > 0:
                    for h in ax.hub[c.side].values():
                        S.nodes[h].w += c.unsprung_mass / 4
        fixed = sum(n.w for n in S.nodes.values() if n.part != "body")
        fixed += sum(w["generated_mass"] for w in self.wheels.values())
        budget = max(P.mass - fixed, 0.3 * P.mass, 2.0 * len(body))
        raw = [max(S.nodes[n].w, 0.0) for n in body]
        tot = sum(raw) or 1.0
        ws = [budget * w / tot for w in raw]
        # keep every node within reach of the average so none is a feather on stiff beams
        floor = 0.2 * budget / len(body)
        ws = [max(w, floor) for w in ws]
        k = budget / sum(ws)
        for n, w in zip(body, ws):
            S.nodes[n].w = w * k
        chassis.stabilize(S, self.wheels)
        if P.mass < fixed * 1.25:
            self.notes.append("The weight is low for the wheels, suspension and engine it needs; it was raised.")

    def _gearing(self):
        P = self.P
        e = self.engine
        n = int(P.gears)
        driven = [ax for ax in self.axle_list if ax.driven] or self.axle_list
        r = max(sum(ax.r for ax in driven) / len(driven), 0.15)
        v_top = P.topSpeed / 3.6
        red = P.redline
        overall_top = (red * 0.98 * 2 * math.pi / 60) * r / v_top
        top_gear = 0.72 if n >= 6 else 0.82 if n >= 5 else 1.0
        final = clamp(overall_top / top_gear, 2.2, 9.0)
        top_gear = overall_top / final
        share = clamp(sum(ax.share for ax in driven), 0.05, 1.0)
        traction = 1.05 * self.total_mass * chassis.G * share
        first_overall = traction * r / max(e["peak_torque"] * 0.92, 1.0)
        first = clamp(first_overall / final, top_gear * 2.2, 6.5)
        ratios = []
        for i in range(n):
            t = i / max(n - 1, 1)
            ratios.append(round(first * (top_gear / first) ** (t ** 0.92), 3))
        self.gearing = {"gears": ratios, "reverse": round(first * 1.05, 3), "final": round(final, 3)}

    def _camera(self):
        an, fr, S = self.an, self.frame, self.S
        lo, hi = self.lattice.bounds()
        if an.seats:
            s = an.seats[0]
            a, b = fr.point(s.lo), fr.point(s.hi)
            slo = [min(a[k], b[k]) for k in range(3)]
            shi = [max(a[k], b[k]) for k in range(3)]
            p = ((slo[0] + shi[0]) / 2, (slo[1] + shi[1]) / 2 - 0.05, slo[2] + 0.9 * self.st["scale"])
        else:
            p = (0.0, lo[1] + 0.35 * (hi[1] - lo[1]), hi[2] + 0.25)
            self.notes.append("No driver's seat was found: the driver's view is above the front of the vehicle.")
        nodes = S.nearest(p, self.body_names, 6, min_d=0.05)
        self.camera = {"p": p, "nodes": nodes, "seat_y": p[1], "seat_x": p[0], "floor": p[2] - 0.9}

    def _refnodes(self):
        S = self.S
        body = self.body_names
        lo, hi = self.lattice.bounds()
        mid = (0.0, (lo[1] + hi[1]) / 2, lo[2])
        ref = min(body, key=lambda n: dist(S.pos(n), mid))
        rp = S.pos(ref)

        def along(axis):
            best = None
            for n in body:
                d = v_sub(S.pos(n), rp)
                ln = v_len(d)
                if n == ref or ln < 0.12:
                    continue
                score = d[axis] / ln - 0.05 * abs(ln - 0.5)
                if best is None or score > best[0]:
                    best = (score, n)
            return best[1]
        back, left, up = along(1), along(0), along(2)
        lc = max(body, key=lambda n: -S.pos(n)[1] + 0.5 * S.pos(n)[0])
        rc = max(body, key=lambda n: -S.pos(n)[1] - 0.5 * S.pos(n)[0])
        self.ref_nodes = [ref, back, left, up, lc, rc]

    def _drag(self):
        lo, hi = self.lattice.bounds()
        self.cd = 0.6
        self.frontal_area = max(hi[0] - lo[0], 0.3) * max(hi[2] - max(lo[2], 0.0), 0.3)
        self.drag_per_tri = clamp(self.cd * 26.0, 4.0, 14.0)
        self.body_tri_count = sum(1 for t in self.S.tris if t[4] == "body")

    def _performance(self):
        P = self.P
        e, gr = self.engine, self.gearing
        mass = self.total_mass
        driven = [ax for ax in self.axle_list if ax.driven] or self.axle_list
        r = max(sum(ax.r for ax in driven) / len(driven), 0.15)
        cda = self.cd * self.frontal_area
        crr = 0.013 if P.tireType in ("street", "sport", "slick") else 0.02
        mu = {"street": 1.0, "sport": 1.1, "allterrain": 0.95, "mud": 0.9, "slick": 1.25}[P.tireType]
        share = clamp(sum(ax.share for ax in driven), 0.05, 1.0)
        curve = e["curve"]

        def torque(rpm):
            for (r0, t0), (r1, t1) in zip(curve, curve[1:]):
                if r0 <= rpm <= r1:
                    return lerp(t0, t1, (rpm - r0) / max(r1 - r0, 1))
            return 0.0
        v, t, dt = 0.1, 0.0, 0.05
        top, t100 = 0.0, None
        for _ in range(int(120 / dt)):
            best = 0.0
            for g in gr["gears"]:
                ratio = g * gr["final"]
                rpm = v / r * ratio * 60 / (2 * math.pi)
                if rpm > P.redline:
                    continue
                rpm = max(rpm, e["idle"] + 1500)
                best = max(best, torque(min(rpm, P.redline)) * ratio * 0.88 / r)
            best = min(best, mu * mass * chassis.G * share * 1.05)
            drag = 0.5 * 1.2 * cda * v * v + crr * mass * chassis.G
            a = (best - drag) / (mass * 1.08)
            if a <= 0.01:
                break
            v += a * dt
            t += dt
            if t100 is None and v >= 100 / 3.6:
                t100 = round(t + 0.35, 1)
            top = max(top, v)
        return t100, round(top * 3.6)

    def _stats(self):
        P = self.P
        e, gr = self.engine, self.gearing
        mass = self.total_mass
        axles = self.axle_list
        wb = axles[-1].y - axles[0].y
        steered = [ax for ax in axles if ax.steer]
        turning = None
        if steered and wb > 0.3:
            ang = math.radians(self.st["steerAngle"])
            if all(ax.steer for ax in axles) and len(axles) > 1:
                wb *= 0.62
            turning = round((wb / math.sin(ang) + axles[0].track / 2) * 2, 1)
        accel, top = self._performance()
        lo, hi = self.lattice.bounds()
        xs = [lo[0], hi[0]] + [w.p[0] + s * w.w / 2 for w in self.an.used for s in (-1, 1)]
        ys = [lo[1], hi[1]] + [w.p[1] + s * w.r for w in self.an.used for s in (-1, 1)]
        zs = [hi[2]] + [w.p[2] + w.r for w in self.an.used]
        sp = self.springs
        self.dims = {"length": max(ys) - min(ys), "width": max(xs) - min(xs), "height": max(zs)}
        clearance = max(lo[2], 0.0)
        self.stats = {
            "mass": round(mass), "massTarget": round(P.mass), "frontShare": round(self.front_share, 3),
            "cgHeight": round(self.cg[2], 3),
            "rollG": vg.rollover(axles, self.cg[2], P.tireType)[0],
            "rollRisk": vg.rollover(axles, self.cg[2], P.tireType)[1],
            "powerHP": round(P.power), "powerKW": round(P.kw), "torqueNm": round(e["peak_torque"]),
            "torqueRPM": int(round(e["peak_torque_rpm"], -2)), "powerRPM": int(round(e["peak_power_rpm"], -2)),
            "hpPerTon": round(P.power / (mass / 1000.0)),
            "zeroTo100": accel, "topSpeed": top, "gears": gr["gears"], "finalDrive": gr["final"],
            "turningCircle": turning, "length": round(self.dims["length"], 2), "width": round(self.dims["width"], 2),
            "height": round(self.dims["height"], 2), "wheelbase": round(wb if not steered else axles[-1].y - axles[0].y, 2),
            "clearance": round(clearance, 3),
            "axles": [{"id": ax.id, "name": vparts.axle_word(ax), "wheels": len(ax.corners), "steer": ax.steer,
                       "driven": ax.driven, "tire": self.wheels[ax.id]["size_text"],
                       "spring": round(sp[ax.id]["spring"], -2), "travel": round(ax.travel, 3)} for ax in axles],
            "nodes": len(self.S.nodes), "beams": len(self.S.beams), "triangles": len(self.S.tris),
            "stabilityAdded": round(getattr(self.S, "stability_added", 0.0), 1),
            "curve": [[int(r), round(t, 1)] for r, t in e["curve"]], "redline": int(P.redline), "cd": self.cd,
            "lattice": self.lattice.C * 0.25 * self.st["scale"],
        }

    def summary(self):
        s = self.an.summary()
        s["massBuilt"] = round(self.an.mass(1.0))
        s["wheelList"] = [{"axle": ax.id, "name": vparts.axle_word(ax), "side": c.side, "steered": c.wheel.steered,
                           "driven": c.wheel.driven, "suspension": c.wheel.kind(),
                           "diameter": round(c.wheel.r * 2, 3), "width": round(c.wheel.w, 3)}
                          for ax in self.axle_list for c in ax.corners]
        return s

    # -- 3D model ----------------------------------------------------------------------------------
    def _meshes(self):
        P, an, fr = self.P, self.an, self.frame
        m = P.model
        bp = an.bp
        # one material per Scrap Mechanic colour; the most used one is the paint
        use = {}
        for s in bp.shapes:
            if s.body in self.chassis_bodies or s.body in an.unsprung_bodies:
                key = self._colour_key(s)
                n = max((s.hi[0] - s.lo[0]) * (s.hi[1] - s.lo[1]) * (s.hi[2] - s.lo[2]), 1)
                use[key] = use.get(key, 0) + n
        keys = sorted(use, key=lambda k: -use[k])
        main = next((k for k in keys if not k[1]), None)
        if main is not None:
            P.paintRGB = list(main[0])
        mats = vmesh.materials(self)
        mat_of = {}
        for i, key in enumerate(keys):
            rgb, glass = key
            name = "%s_sm%d" % (m, i)
            mat_of[key] = name
            if glass:
                mats[name] = {"color": list(rgb), "metallic": 0.2, "roughness": 0.05, "opacity": 0.45, "glass": True}
            else:
                mats[name] = {"color": list(rgb), "metallic": 0.1, "roughness": 0.55, "paint": key == main}
        cs, o = fr.scale, fr.origin

        def to_m(i, j, k):
            return (i * cs + o[0], j * cs + o[1], k * cs + o[2])

        meshes = {}

        def solid(name, bodies):
            M = smmesh.new_mesh(name)
            cells = {}
            for s in bp.shapes:
                if s.body not in bodies:
                    continue
                key = self._colour_key(s)
                if s.kind == "block":
                    lo = [int(math.floor(s.lo[k] + 1e-6)) for k in range(3)]
                    hi = [int(math.ceil(s.hi[k] - 1e-6)) for k in range(3)]
                    for i in range(lo[0], hi[0]):
                        for j in range(lo[1], hi[1]):
                            for k in range(lo[2], hi[2]):
                                cells[fr.cell((i, j, k))] = key
                else:
                    a, b = fr.point(s.lo), fr.point(s.hi)
                    plo = tuple(min(a[k], b[k]) for k in range(3))
                    phi = tuple(max(a[k], b[k]) for k in range(3))
                    shape, axis = "box", None
                    if s.info is not None and s.info.collision in ("cylinder", "sphere"):
                        shape = s.info.collision
                        if shape == "cylinder":
                            v = fr.vec(s.R[s.info.axis])
                            axis = max(range(3), key=lambda q: abs(v[q]))
                    smmesh.add_part(M, mat_of[key], plo, phi, shape, axis)
            smmesh.add_cells(M, cells, to_m, lambda key: mat_of[key])
            return M

        body = solid(m + "_body", self.chassis_bodies)
        if not body.tri_count():
            body.box(m + "_metal", (0, 0, 0.5), (0.1, 0.1, 0.1))
        meshes[body.name] = body
        susp, flex, coil = vmesh.suspension_meshes(self)
        for M in susp:
            meshes[M.name] = M
        for ax in self.axle_list:
            for c in ax.corners:
                nm = ax.id + c.side.upper()
                if c.carrier_bodies:
                    M = solid("%s_carrier_%s" % (m, nm), set(c.carrier_bodies))
                    if M.tri_count():
                        meshes[M.name] = M
                        # a solid axle's beam moves with the axle, a lone wheel's carrier with its hub
                        grp = "axle_" + ax.id if ax.solid else "hub_" + nm
                        flex[ax.id].append((M.name, [P.model_group(grp)]))
                for M in (vmesh.rim_mesh("%s_rim_%s_%s" % (m, ax.id, c.side.upper()), self, ax, c.s),
                          vmesh.tire_mesh("%s_tire_%s_%s" % (m, ax.id, c.side.upper()), self, ax, c.s)):
                    meshes[M.name] = M
        ex = vmesh.exhaust_mesh(self)
        meshes[ex.name] = ex
        used = set()
        for M in meshes.values():
            used.update(k for k, v in M.t.items() if len(v) >= 3)
        mats = {k: v for k, v in mats.items() if k in used}
        return meshes, mats, {"susp": flex, "coil": coil}

    @staticmethod
    def _colour_key(s):
        glass = s.role == "glass" or bool(s.info and "glass" in s.info.material.lower())
        return (tuple(round(c, 3) for c in s.color), glass)


class SMPartsBuilder(vparts.PartsBuilder):
    """The Builder's part tree, with a lattice body and any number of axles and driven wheels."""

    def build(self):
        self.main()
        self.body()
        for ax in self.g.axle_list:
            self.suspension(ax)
            self.coilover(ax)
            self.brake(ax)
            self.wheel(ax)
            self.tire(ax)
            self.wheeldata(ax)
        self.engine()
        self.transmission()
        for u in self.g.drive_units:
            if u["kind"] == "diff":
                self.diff_unit(u)
        self.fueltank()
        return self.parts

    def main(self):
        super().main()
        info = self.parts[self.m]["information"]
        info.pop("jbeamEditorBuilder", None)
        info["jbeamEditorScrap"] = True

    def body(self):
        P, S, g = self.P, self.S, self.g
        nodes, beams, _, tris = self.by_part("body")
        cam = g.camera
        lo, hi = g.lattice.bounds()
        L = max(hi[1] - lo[1], 1.0)
        H = max(hi[2], 0.8)
        slots = [["type", "default", "description"]]
        for ax in g.axle_list:
            slots.append([self.name("suspension_" + ax.id), self.name("suspension_" + ax.id),
                          vparts.axle_word(ax) + " Suspension"])
        slots += [[self.name("engine"), self.name("engine"), "Engine"],
                  [self.name("fueltank"), self.name("fueltank"), "Fuel Tank"]]
        self.parts[self.name("body")] = {
            "information": vparts._info(P.name + " Body", 8000, P),
            "slotType": self.name("body"),
            "slots": slots,
            "refNodes": [["ref:", "back:", "left:", "up:", "leftCorner:", "rightCorner:"], g.ref_nodes],
            "cameraExternal": {"distance": vparts._r(clamp(L * 1.2, 3.6, 14.0)),
                               "distanceMin": vparts._r(clamp(L * 0.45, 1.6, 6.0)),
                               "offset": {"x": 0, "y": 0, "z": vparts._r(H * 0.35)}, "fov": 65},
            "cameraChase": {"distance": vparts._r(clamp(L * 1.15, 3.6, 14.0)),
                            "distanceMin": vparts._r(clamp(L * 0.45, 1.6, 6.0)),
                            "defaultRotation": {"x": 0, "y": -11.2, "z": 0},
                            "offset": {"x": 0, "y": 0, "z": vparts._r(H * 0.45)}, "fov": 65},
            "camerasInternal": [
                ["type", "x", "y", "z", "fov", "id1:", "id2:", "id3:", "id4:", "id5:", "id6:"],
                {"nodeWeight": 1.3}, {"selfCollision": False}, {"collision": False},
                {"beamSpring": 46000, "beamDamp": 435},
                ["dash", vparts._r(cam["p"][0]), vparts._r(cam["p"][1]), vparts._r(cam["p"][2]), 70] + cam["nodes"] +
                [{"beamDeform": 5001000, "beamStrength": FLT_MAX}],
            ],
            "flexbodies": vparts._flex([vparts._place(self.name("body"), [P.model_group("body")])]),
            "nodes": vparts.node_table(S, nodes),
            "beams": vparts.beam_table(S, beams, {"deformLimitExpansion": 1.2}),
            "triangles": vparts.tri_table(S, tris, drag=round(g.drag_per_tri, 2)),
        }
        pr = lights.props(g.lamps)
        if pr:
            self.parts[self.name("body")]["props"] = pr
        front = min(g.body_names, key=lambda n: S.pos(n)[1] + 0.3 * abs(S.pos(n)[0]))
        self.parts[self.name("body")]["soundscape"] = lights.soundscape(front)

    def transmission(self):
        P, g = self.P, self.g
        gr = g.gearing
        n = len(gr["gears"])
        V = vparts._var
        variables = [vparts.VAR_HEAD,
                     V("$gear_R", ":1", "Transmission", gr["reverse"], 0.5, 7, "Reverse Gear Ratio",
                       "Torque multiplication ratio", stepDis=0.01)]
        for i, r in enumerate(gr["gears"], 1):
            variables.append(V("$gear_%d" % i, ":1", "Transmission", r, 0.3, 7, "%s Gear Ratio" % vparts._ordinal(i),
                               "Torque multiplication ratio", stepDis=0.01))
        variables.append(V("$finaldrive", ":1", "Differentials", gr["final"], 1.5, 12, "Final Drive Gear Ratio",
                           "Torque multiplication ratio of every driven axle", stepDis=0.01))
        ratios = ["$=-$gear_R*$finaldrive", 0] + ["$=$gear_%d*$finaldrive" % i for i in range(1, n + 1)]
        pt = [["type", "name", "inputName", "inputIndex"]]
        d = {"information": vparts._info("%d-Speed %s" % (n, "Automatic" if P.gearbox == "automatic" else "Manual"),
                                         1500, P),
             "slotType": self.name("transmission"), "variables": variables}
        peak = g.engine["peak_torque"]
        if P.gearbox == "automatic":
            pt += [["torqueConverter", "torqueConverter", "mainEngine", 1],
                   ["automaticGearbox", "gearbox", "torqueConverter", 1]]
            d["torqueConverter"] = {"uiName": "Torque Converter", "converterDiameter": 0.26, "converterStiffness": 10,
                                    "couplingAVRatio": 0.95, "stallTorqueRatio": 2.0,
                                    "lockupClutchTorque": round(peak * 1.6)}
            d["gearbox"] = {"uiName": "Gearbox", "gearRatios": ratios, "parkLockTorque": 3000 + round(P.mass),
                            "oneWayViscousCoef": 40, "friction": 2, "dynamicFriction": 0.002, "torqueLossCoef": 0.02}
            d["vehicleController"] = {"automaticModes": "PRND21", "calculateOptimalLoadShiftPoints": True,
                                      "torqueConverterLockupRPM": int(g.engine["idle"] + 600),
                                      "torqueConverterLockupMinGear": 2}
        else:
            pt += [["frictionClutch", "clutch", "mainEngine", 1], ["manualGearbox", "gearbox", "clutch", 1]]
            d["clutch"] = {"uiName": "Clutch", "clutchMass": 3, "additionalEngineInertia": 0.01}
            d["gearbox"] = {"uiName": "Gearbox", "gearRatios": ratios, "friction": 2, "dynamicFriction": 0.002,
                            "torqueLossCoef": 0.02}
            d["vehicleController"] = {"calculateOptimalLoadShiftPoints": True, "shiftDownRPMOffsetCoef": 1.2,
                                      "clutchLaunchStartRPM": int(g.engine["idle"] + 700),
                                      "clutchLaunchTargetRPM": int(g.engine["idle"] + 1500)}
        slots = [["type", "default", "description"]]
        units = g.drive_units
        stage = [0]

        def feed(us, src, idx):
            if len(us) == 1:
                u = us[0]
                pt.append(["shaft", "driveshaft_" + u["id"], src, idx,
                           {"uiName": u["word"] + " Driveshaft", "friction": 1}])
                if u["kind"] == "shaft":
                    _, ax, c = u["outs"][0]
                    nm = ax.id + c.side.upper()
                    pt.append(["shaft", "wheelaxle" + nm, "driveshaft_" + u["id"], 1,
                               {"connectedWheel": nm, "breakTriggerBeam": "axle_" + nm,
                                "uiName": u["word"] + " Axle", "friction": 1}])
                return
            stage[0] += 1
            name = "transfercase" if stage[0] == 1 else "transfercase%d" % stage[0]
            if P.diff == "locked":
                opts = {"diffType": "locked", "lockTorque": round(peak * 30)}
            else:
                opts = {"diffType": "viscous", "viscousCoef": 80, "viscousTorque": round(peak * 2.5)}
            opts.update({"diffTorqueSplit": round(1.0 / len(us), 4), "uiName": "Center Differential",
                         "defaultVirtualInertia": 0.1, "friction": 2})
            pt.append(["differential", name, src, idx, opts])
            feed(us[:1], name, 1)
            feed(us[1:], name, 2)
        feed(units, "gearbox", 1)
        for u in units:
            if u["kind"] == "diff":
                slots.append([self.name("differential_" + u["id"]), self.name("differential_" + u["id"]),
                              u["word"] + " Differential"])
        d["slots"] = slots
        d["powertrain"] = pt
        self.parts[self.name("transmission")] = d

    def diff_unit(self, u):
        P = self.P
        a = u["id"]
        word = u["word"]
        variables = [vparts.VAR_HEAD]
        opts = {"gearRatio": 1, "uiName": word + " Differential", "defaultVirtualInertia": 0.25, "friction": 3}
        if P.diff == "lsd":
            opts.update({"diffType": "lsd", "lsdPreload": "$lsdpreload_" + a, "lsdLockCoef": "$lsdlockcoef_" + a,
                         "lsdRevLockCoef": "$lsdlockcoefrev_" + a})
            V = vparts._var
            variables += [
                V("$lsdpreload_" + a, "N/m", "Differentials", 100, 0, 500, "Pre-load Torque",
                  "Initial locking torque between the wheels", subCategory=word),
                V("$lsdlockcoef_" + a, "", "Differentials", 0.2, 0, 0.5, "Power Lock Rate",
                  "Extra locking under power", minDis=0, maxDis=100, subCategory=word),
                V("$lsdlockcoefrev_" + a, "", "Differentials", 0.1, 0, 0.5, "Coast Lock Rate",
                  "Extra locking when coasting", minDis=0, maxDis=100, subCategory=word)]
        elif P.diff == "locked":
            opts.update({"diffType": "locked", "lockTorque": round(self.g.engine["peak_torque"] * 40)})
        else:
            opts["diffType"] = "open"
        pt = [["type", "name", "inputName", "inputIndex"],
              ["differential", "differential_" + a, "driveshaft_" + a, 1, opts]]
        for idx, ax, c in u["outs"]:
            nm = ax.id + c.side.upper()
            pt.append(["shaft", "wheelaxle" + nm, "differential_" + a, idx,
                       {"connectedWheel": nm, "breakTriggerBeam": "axle_" + nm,
                        "uiName": "%s %s Axle" % (vparts.axle_word(ax), "Left" if c.s > 0 else "Right"),
                        "friction": 1}])
        d = {"information": vparts._info("%s %s Differential" % (word, dict(open="Open", lsd="Limited Slip",
                                                                           locked="Locked")[P.diff]), 400, P),
             "slotType": self.name("differential_" + a), "powertrain": pt}
        if len(variables) > 1:
            d["variables"] = variables
        self.parts[self.name("differential_" + a)] = d


def generate(an, settings, author="", source=None):
    return SMGenerator(an, settings, author, source).run()
