"""JBeam parts of a generated vehicle.

The part tree follows BeamNG's own vehicles, so the game's Parts and Tuning
menus and the editor's Garage work on it like on any other car:

  <m>                        main: controller, brake force
  └ <m>_body                 body shell, cameras, refNodes
    ├ <m>_suspension_F/R     arms, hubs, steering, anti-roll bar
    │ ├ <m>_coilover_F/R     springs, dampers, bump stops (ride height)
    │ ├ <m>_brake_F/R
    │ ├ <m>_wheel_F/R        rims and the wheels' axle nodes
    │ │ └ <m>_tire_F/R
    │ └ <m>_wheeldata_F/R    the wheels themselves
    ├ <m>_engine
    │ └ <m>_transmission
    │   └ <m>_differential_F/R
    └ <m>_fueltank
"""
import math

from . import lights
from .chassis import FLT_MAX, G, clamp, dist
from .shape import lerp

PW_HEAD = ["name", "hubGroup", "group", "node1:", "node2:", "nodeS", "nodeArm:", "wheelDir"]
VAR_HEAD = ["name", "type", "unit", "category", "default", "min", "max", "title", "description"]

# sample sets known to ship with the game (BeamNG's own template and docs use them)
SOUNDS = {
    "i4": {"engine": "i4p_01600cc_motorsport_02_engine", "exhaust": "i4p_01600cc_motorsport_02_exhaust",
           "starter": "v6_1995", "cyl": 4},
    "b4": {"engine": "B4_2_engine_rear", "exhaust": "B4_2_exhaust", "starter": "box4_1960", "cyl": 4},
}

TIRES = {
    #            friction sliding tread noLoad fullLoad slope     softness
    "street":     (1.0, 0.95, 0.70, 1.30, 0.45, 0.00018, 0.60),
    "sport":      (1.08, 1.0, 0.55, 1.38, 0.50, 0.00017, 0.70),
    "allterrain": (0.97, 0.93, 0.95, 1.26, 0.43, 0.00019, 0.55),
    "mud":        (0.92, 0.9, 1.15, 1.22, 0.40, 0.00020, 0.50),
    "slick":      (1.22, 1.06, 0.15, 1.46, 0.56, 0.00015, 0.85),
}


class Table:
    """A JBeam table with modifier rows written only when a value changes."""

    def __init__(self, header):
        self.rows = [list(header)]
        self.state = {}

    def set(self, **kw):
        ch = {k: v for k, v in kw.items() if self.state.get(k, Table) != v}
        if ch:
            self.rows.append(ch)
            self.state.update(ch)

    def add(self, *cells, **inline):
        r = list(cells)
        if inline:
            r.append(inline)
        self.rows.append(r)

    def raw(self, row):
        self.rows.append(row)


def _r(v, d=4):
    if isinstance(v, float):
        v = round(v, d)
        if v == int(v) and abs(v) < 1e9:
            return float(int(v)) if d > 0 else int(v)
    return v


def _var(name, unit, cat, default, lo, hi, title, desc, **extra):
    row = [name, "range", unit, cat, _r(default), _r(lo), _r(hi), title, desc]
    if extra:
        row.append(extra)
    return row


def _info(name, value, P, extra=None):
    d = {"authors": P.author or "JBeam Editor", "name": name, "value": int(value)}
    if extra:
        d.update(extra)
    return d


def _flex(rows):
    return [["mesh", "[group]:", "nonFlexMaterials"]] + rows


def _place(mesh, groups, pos=None):
    if pos is None:
        return [mesh, groups]
    return [mesh, groups, [], {"pos": {"x": _r(pos[0]), "y": _r(pos[1]), "z": _r(pos[2])},
                               "rot": {"x": 0, "y": 0, "z": 0}, "scale": {"x": 1, "y": 1, "z": 1}}]


def node_table(S, names, local=None):
    """Nodes table for names; local: {name: (x,y,z)} positions to write instead (slot-offset parts)."""
    t = Table(["id", "posX", "posY", "posZ"])
    for nm in names:
        n = S.nodes[nm]
        t.set(nodeWeight=_r(n.w, 3), group=n.groups if len(n.groups) != 1 else n.groups[0],
              collision=n.coll, selfCollision=n.selfcoll, nodeMaterial=n.mat, frictionCoef=n.fric)
        p = local.get(nm) if local else None
        p = p or n.p
        if n.extra:
            t.add(nm, _r(p[0]), _r(p[1]), _r(p[2]), **n.extra)
        else:
            t.add(nm, _r(p[0]), _r(p[1]), _r(p[2]))
    t.set(group="")
    return t.rows


def beam_table(S, beams, base=None):
    t = Table(["id1:", "id2:"])
    t.set(beamPrecompression=1, beamType="|NORMAL", beamLongBound=1, beamShortBound=1)
    if base:
        t.set(**base)
    for b in beams:
        opts = dict(S.kinds[b.kind])
        opts.setdefault("beamType", "|NORMAL")
        opts.setdefault("beamLongBound", 1)
        opts.setdefault("beamShortBound", 1)
        t.set(**opts)
        extra = dict(b.extra)
        for k in ("breakGroup", "optional"):
            if k in extra:
                t.set(**{k: extra.pop(k)})
        if "breakGroup" not in b.extra and t.state.get("breakGroup"):
            t.set(breakGroup="")
        if "optional" not in b.extra and t.state.get("optional"):
            t.set(optional=False)
        if extra:
            t.add(b.a, b.b, **extra)
        else:
            t.add(b.a, b.b)
    t.set(breakGroup="", optional=False)
    t.set(beamPrecompression=1, beamType="|NORMAL", beamLongBound=1, beamShortBound=1)
    return t.rows


def tri_table(S, tris, drag=8.0):
    t = Table(["id1:", "id2:", "id3:"])
    t.set(groundModel="metal")
    for a, b, c, part, kind in tris:
        if kind == "under":
            t.set(dragCoef=2, triangleType="NORMALTYPE")
        elif kind == "nc":
            t.set(dragCoef=0, triangleType="NONCOLLIDABLE")
        else:
            t.set(dragCoef=drag, triangleType="NORMALTYPE")
        t.add(a, b, c)
    t.set(triangleType="NORMALTYPE", dragCoef=drag)
    return t.rows


# ---------------------------------------------------------------------------

class PartsBuilder:
    def __init__(self, gen):
        self.g = gen
        self.P = gen.P
        self.S = gen.S
        self.m = gen.P.model
        self.parts = {}

    def name(self, suffix=""):
        return self.m + ("_" + suffix if suffix else "")

    def by_part(self, part):
        S = self.S
        nodes = [n for n in S.order if S.nodes[n].part == part]
        beams = [b for b in S.beams if b.part == part and b.sec == "beams"]
        hydros = [b for b in S.beams if b.part == part and b.sec == "hydros"]
        tris = [t for t in S.tris if t[3] == part]
        return nodes, beams, hydros, tris

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
        for ax in self.g.axle_list:
            if ax.driven:
                self.differential(ax)
        self.fueltank()
        return self.parts

    # -- main and body -----------------------------------------------------------------
    def main(self):
        P = self.P
        self.parts[self.m] = {
            "information": _info(P.name, 20000 + P.power * 60, P, {"jbeamEditorBuilder": True}),
            "slotType": "main",
            "slots": [["type", "default", "description"],
                      [self.name("body"), self.name("body"), "Body", {"coreSlot": True}]],
            "controller": [["fileName"], ["vehicleController", {}]],
            "variables": [VAR_HEAD,
                          _var("$brakestrength", "", "Brakes", 1, 0.5, 1.3, "Brake Force",
                               "Scales the overall brake torque", minDis=50, maxDis=130),
                          _var("$ffbstrength", "", "Chassis", 1, 0.5, 1.5, "Force Feedback",
                               "Scales the force feedback strength", minDis=50, maxDis=150)],
        }

    def body(self):
        P, S, g = self.P, self.S, self.g
        part = "body"
        nodes, beams, _, tris = self.by_part(part)
        sh = g.shape
        rings = S.body_rings
        mid = min(rings, key=lambda r: abs(r["y"]))
        back = rings[min(rings.index(mid) + 1, len(rings) - 1)]
        top = mid if "tc" in mid["ids"] else next((r for r in rings if "tc" in r["ids"]), mid)
        front = rings[0]
        ref = [mid["ids"]["fc"], back["ids"]["fc"], mid["ids"]["fll"], top["ids"].get("tc", mid["ids"]["bll"]),
               front["ids"]["bll"], front["ids"]["blr"]]
        cam = g.camera
        L = P.length
        slots = [["type", "default", "description"],
                 [self.name("suspension_F"), self.name("suspension_F"), "Front Suspension"],
                 [self.name("suspension_R"), self.name("suspension_R"), "Rear Suspension"],
                 [self.name("engine"), self.name("engine"), "Engine"],
                 [self.name("fueltank"), self.name("fueltank"), "Fuel Tank"]]
        flex = [_place(self.name("body"), [P.model_group("body")]),
                _place(self.name("interior"), [P.model_group("body")])]
        self.parts[self.name("body")] = {
            "information": _info(P.name + " Body", 8000, P),
            "slotType": self.name("body"),
            "slots": slots,
            "refNodes": [["ref:", "back:", "left:", "up:", "leftCorner:", "rightCorner:"], ref],
            "cameraExternal": {"distance": _r(clamp(L * 1.15, 3.6, 9.0)), "distanceMin": _r(clamp(L * 0.45, 1.6, 4.0)),
                               "offset": {"x": 0, "y": 0, "z": _r(P.height * 0.35)}, "fov": 65},
            "cameraChase": {"distance": _r(clamp(L * 1.1, 3.6, 9.0)), "distanceMin": _r(clamp(L * 0.45, 1.6, 4.0)),
                            "defaultRotation": {"x": 0, "y": -11.2, "z": 0},
                            "offset": {"x": 0, "y": 0, "z": _r(P.height * 0.45)}, "fov": 65},
            "camerasInternal": [
                ["type", "x", "y", "z", "fov", "id1:", "id2:", "id3:", "id4:", "id5:", "id6:"],
                {"nodeWeight": 1.3}, {"selfCollision": False}, {"collision": False},
                {"beamSpring": 46000, "beamDamp": 435},
                ["dash", _r(cam["p"][0]), _r(cam["p"][1]), _r(cam["p"][2]), 68] + cam["nodes"] +
                [{"beamDeform": 5001000, "beamStrength": FLT_MAX}],
            ],
            "flexbodies": _flex(flex),
            "nodes": node_table(S, nodes),
            "beams": beam_table(S, beams, {"deformLimitExpansion": 1.2}),
            "triangles": tri_table(S, tris, drag=round(g.drag_per_tri, 2)),
        }
        pr = lights.props(getattr(g, "lamps", None))
        if pr:
            self.parts[self.name("body")]["props"] = pr

    # -- suspension ----------------------------------------------------------------------
    def suspension(self, ax):
        P, S = self.P, self.S
        part = "susp_" + ax.id
        nodes, beams, hydros, _ = self.by_part(part)
        side_word = axle_word(ax)
        slots = [["type", "default", "description"],
                 [self.name("coilover_" + ax.id), self.name("coilover_" + ax.id), side_word + " Springs"],
                 [self.name("brake_" + ax.id), self.name("brake_" + ax.id), side_word + " Brakes"],
                 [self.name("wheel_" + ax.id), self.name("wheel_" + ax.id), side_word + " Wheels",
                  {"nodeOffset": {"x": "$trackoffset_" + ax.id, "y": 0, "z": 0}}],
                 [self.name("wheeldata_" + ax.id), self.name("wheeldata_" + ax.id), side_word + " Spindles",
                  {"coreSlot": True}]]
        k_arb = self.g.springs[ax.id]["arb_rate"]
        variables = [VAR_HEAD,
                     _var("$camber_" + ax.id, "°", "Wheel Alignment", _axle_val(ax, "camber", P.camberF, P.camberR), -5, 3,
                          "Camber", "Tilt of the wheels; negative leans their tops inward",
                          stepDis=0.1, subCategory=side_word),
                     _var("$trackoffset_" + ax.id, "+m", "Wheels", 0, -0.02, 0.08, "Wheel Offset",
                          "Spacing of the wheels from the hubs", stepDis=0.001, subCategory=side_word),
                     ]
        if ax.arb:
            variables.append(_var("$arb_" + ax.id, "N/m", "Suspension", round(k_arb), 0, round(max(k_arb * 3, 60000)),
                                  "Anti-Roll Bar", "Stiffness of the anti-roll bar at the wheels", stepDis=500,
                                  subCategory=side_word))
        d = {
            "information": _info("%s %s Suspension" % (side_word, "Solid Axle" if ax.solid else "Double Wishbone"),
                                 900, P),
            "slotType": self.name("suspension_" + ax.id),
            "slots": slots,
            "variables": variables,
            "flexbodies": _flex([_place(mesh, groups) for mesh, groups in self.g.susp_flex[ax.id]]),
            "nodes": node_table(S, nodes),
            "beams": beam_table(S, beams, {"deformLimitExpansion": 1.2}),
        }
        if ax.arb:
            d["torsionbars"] = [["id1:", "id2:", "id3:", "id4:"],
                                {"spring": "$=$arb_%s*%s/2" % (ax.id, _fmt(ax.arb_lever ** 2)), "damp": 10,
                                 "deform": 50000, "strength": 9999999},
                                ax.arb]
        if hydros:
            t = Table(["id1:", "id2:"])
            for b in hydros:
                t.set(**S.kinds[b.kind])
                t.add(b.a, b.b, **b.extra)
            t.set(beamPrecompression=1, beamType="|NORMAL", beamLongBound=1, beamShortBound=1)
            d["hydros"] = t.rows
        if ax.front:
            d["input"] = {"FFBcoef": "$=$ffbstrength*%s" % _fmt(clamp(P.mass / 120.0, 4, 30))}
        self.parts[self.name("suspension_" + ax.id)] = d

    def coilover(self, ax):
        P, S = self.P, self.S
        sp = self.g.springs[ax.id]
        side_word = axle_word(ax)
        a = ax.id
        t = Table(["id1:", "id2:"])
        t.set(beamPrecompression=1, beamType="|NORMAL", beamLongBound=1, beamShortBound=1)
        t.set(beamSpring="$spring_" + a, beamDamp=0, beamDeform=round(max(30000, sp["static"] * 6)),
              beamStrength=FLT_MAX)
        for c in ax.corners:
            lo, hi = ax.spring_ends[c.side]
            t.add(lo, hi, precompressionRange="$=$springheight_%s+%s" % (a, _fmt(sp["preload"])))
        t.set(beamType="|BOUNDED", beamLongBound=1.0, beamShortBound=1.0)
        t.set(beamLimitSpring=0, beamLimitDamp=0)
        t.set(beamSpring=250, beamDamp="$damp_bump_" + a)
        for c in ax.corners:
            lo, hi = ax.spring_ends[c.side]
            t.add(lo, hi, beamDampRebound="$damp_rebound_" + a, beamDampVelocitySplit=0.25,
                  beamDampFast="$=$damp_bump_%s/3" % a, beamDampReboundFast="$=$damp_rebound_%s/3" % a,
                  dampCutoffHz=500)
        # bump stop and droop limiter
        t.set(beamSpring=0, beamDamp=0, beamLimitSpring=round(sp["stop_rate"]), beamLimitDamp=round(sp["stop_damp"]))
        for c in ax.corners:
            lo, hi = ax.spring_ends[c.side]
            t.add(lo, hi, shortBoundRange=_r(sp["bump_range"]), longBoundRange=_r(sp["droop_range"]), boundZone=0.03,
                  beamLimitDampRebound=0, dampCutoffHz=500)
        t.set(beamLimitSpring=0, beamLimitDamp=0)
        # hard stop well past the bump stop
        t.set(beamType="|SUPPORT", beamLongBound=2.0, beamPrecompression=_r(sp["hard_stop"]),
              beamSpring=round(sp["stop_rate"] * 4), beamDamp=500, beamDeform=round(max(60000, sp["static"] * 12)),
              beamStrength=FLT_MAX)
        for c in ax.corners:
            lo, hi = ax.spring_ends[c.side]
            t.add(lo, hi)
        t.set(beamPrecompression=1, beamType="|NORMAL", beamLongBound=1, beamShortBound=1)
        k = sp["spring"]
        variables = [VAR_HEAD,
                     _var("$spring_" + a, "N/m", "Suspension", round(k, -2), round(k * 0.4, -2), round(k * 2.5, -2),
                          "Spring Rate", "Spring stiffness", stepDis=100, subCategory=side_word),
                     _var("$springheight_" + a, "+m", "Suspension", 0, -0.06, 0.12, "Spring Height",
                          "Raise or lower the suspension", stepDis=0.005, subCategory=side_word),
                     _var("$damp_bump_" + a, "N/m/s", "Suspension", round(sp["bump"], -1), round(sp["bump"] * 0.3, -1),
                          round(sp["bump"] * 3, -1), "Bump Damping", "Damper rate in compression", stepDis=10,
                          subCategory=side_word),
                     _var("$damp_rebound_" + a, "N/m/s", "Suspension", round(sp["rebound"], -1),
                          round(sp["rebound"] * 0.3, -1), round(sp["rebound"] * 3, -1), "Rebound Damping",
                          "Damper rate in extension", stepDis=10, subCategory=side_word)]
        self.parts[self.name("coilover_" + a)] = {
            "information": _info("%s Coilovers" % side_word, 600, P),
            "slotType": self.name("coilover_" + a),
            "variables": variables,
            "flexbodies": _flex([_place(mesh, groups) for mesh, groups in self.g.coil_flex[a]]),
            "beams": t.rows,
        }

    def brake(self, ax):
        P = self.P
        b = self.g.brakes[ax.id]
        side_word = axle_word(ax)
        rows = [PW_HEAD]
        if ax.front:
            rows.append({"brakeTorque": "$=$brakestrength*%d*$brakebias" % round(b["coef"])})
        else:
            rows.append({"brakeTorque": "$=$brakebias == nil and $brakestrength*%d or $brakestrength*%d*(1-$brakebias)"
                         % (round(b["coef"] * 0.35), round(b["coef"]))})
        rows += [{"brakeInputSplit": 1}, {"brakeSplitCoef": 1}, {"parkingTorque": round(b["parking"])},
                 {"brakeSpring": 80}, {"enableBrakeThermals": True}, {"brakeDiameter": _r(b["diameter"], 3)},
                 {"brakeMass": _r(b["mass"], 2)}, {"brakeType": "vented-disc"}, {"rotorMaterial": "steel"},
                 {"brakeVentingCoef": 1.0}, {"enableABS": bool(P.abs)}]
        d = {
            "information": _info("%s Disc Brakes" % side_word, 400, P),
            "slotType": self.name("brake_" + ax.id),
            "pressureWheels": rows,
        }
        if ax.front:
            d["variables"] = [VAR_HEAD, _var("$brakebias", "", "Brakes", P.brakeBias, 0.3, 0.9, "Front/Rear Bias",
                                             "Share of the brake force on the front wheels", minDis=30, maxDis=90)]
        self.parts[self.name("brake_" + ax.id)] = d

    def wheel(self, ax):
        P, S = self.P, self.S
        w = self.g.wheels[ax.id]
        side_word = axle_word(ax)
        part = "wheel_" + ax.id
        nodes, beams, _, _ = self.by_part(part)
        rows = [PW_HEAD,
                {"disableMeshBreaking": False, "disableHubMeshBreaking": False, "enableHubcaps": False,
                 "hasTire": False},
                {"hubRadius": _r(w["hub_r"], 4)}, {"wheelOffset": 0}, {"hubWidth": _r(w["hub_w"], 4)},
                {"numRays": w["rays"]},
                {"hubTreadBeamSpring": w["hub_k"], "hubTreadBeamDamp": 10},
                {"hubPeripheryBeamSpring": w["hub_k"], "hubPeripheryBeamDamp": 10},
                {"hubSideBeamSpring": w["hub_side_k"], "hubSideBeamDamp": 10},
                {"hubNodeWeight": _r(w["hub_node_w"], 3)}, {"hubNodeMaterial": "|NM_METAL"}, {"hubFrictionCoef": 0.5},
                {"hubBeamDeform": w["hub_deform"], "hubBeamStrength": w["hub_strength"]}]
        flex = []
        for c in ax.corners:
            nm = ax.id + c.side.upper()
            x, y, z = ax.centers[c.side]
            flex.append(_place(self.name("rim_%s_%s" % (ax.id, c.side.upper())), ["wheel_" + nm, "wheelhub_" + nm],
                               (x * c.s, y, z)))
        self.parts[self.name("wheel_" + ax.id)] = {
            "information": _info('%s %d" %s Wheels' % (side_word, round(w["rim_in"]), w["style_title"]), 500, P),
            "slotType": self.name("wheel_" + ax.id),
            "slots": [["type", "default", "description"],
                      [self.name("tire_" + ax.id), self.name("tire_" + ax.id), side_word + " Tires"]],
            "flexbodies": _flex(flex),
            "nodes": node_table(S, nodes),
            "beams": [["id1:", "id2:"], {"beamType": "|NORMAL", "beamLongBound": 1.0, "beamShortBound": 1.0},
                      {"beamDeform": FLT_MAX, "beamStrength": FLT_MAX}, {"beamSpring": 1501000, "beamDamp": 25}] +
                     [list(ax.wheel_nodes[c.side]) for c in ax.corners] +
                     [{"beamPrecompression": 1, "beamType": "|NORMAL", "beamLongBound": 1.0, "beamShortBound": 1.0}],
            "pressureWheels": rows,
        }

    def tire(self, ax):
        P = self.P
        w = self.g.wheels[ax.id]
        a = ax.id
        side_word = axle_word(ax)
        fr, sl, tread, nl, fl, slope, soft = TIRES[P.tireType]
        ts = w["tire_scale"]
        k = lambda v: int(round(v * ts, -3)) + 1000  # noqa: E731
        rows = [PW_HEAD,
                {"disableMeshBreaking": False, "disableHubMeshBreaking": False},
                {"hasTire": True}, {"enableTireReinfBeams": False}, {"enableTireLbeams": True},
                {"enableTireSideReinfBeams": False}, {"enableTreadReinfBeams": True},
                {"enableTirePeripheryReinfBeams": True},
                {"radius": _r(w["r"], 4)}, {"tireWidth": _r(w["tw"], 4)},
                {"wheelSideBeamSpring": "$=$tirepressure_%s*%d" % (a, round(560 * ts)), "wheelSideBeamDamp": 22},
                {"wheelSideBeamSpringExpansion": k(280000), "wheelSideBeamDampExpansion": 30},
                {"wheelSideTransitionZone": 0.09, "wheelSideBeamPrecompression": 0.98},
                {"wheelReinfBeamSpring": k(15000), "wheelReinfBeamDamp": 140},
                {"wheelReinfBeamDampCutoffHz": 500, "wheelReinfBeamPrecompression": 0.98},
                {"wheelTreadBeamSpring": k(52000), "wheelTreadBeamDamp": 50},
                {"wheelTreadBeamDampCutoffHz": 500, "wheelTreadBeamPrecompression": 0.98},
                {"wheelTreadReinfBeamSpring": k(120000), "wheelTreadReinfBeamDamp": 40},
                {"wheelTreadReinfBeamDampCutoffHz": 500, "wheelTreadReinfBeamPrecompression": 0.98},
                {"wheelPeripheryBeamSpring": k(38000), "wheelPeripheryBeamDamp": 23},
                {"wheelPeripheryBeamDampCutoffHz": 500, "wheelPeripheryBeamPrecompression": 0.98},
                {"wheelPeripheryReinfBeamSpring": k(95000), "wheelPeripheryReinfBeamDamp": 23},
                {"wheelPeripheryReinfBeamDampCutoffHz": 500, "wheelPeripheryReinfBeamPrecompression": 0.98},
                {"nodeWeight": _r(w["tire_node_w"], 3)}, {"nodeMaterial": "|NM_RUBBER"},
                {"triangleCollision": False}, {"pressurePSI": "$tirepressure_" + a}, {"dragCoef": 5},
                {"frictionCoef": fr}, {"slidingFrictionCoef": sl}, {"treadCoef": tread},
                {"noLoadCoef": nl}, {"loadSensitivitySlope": slope}, {"fullLoadCoef": fl}, {"softnessCoef": soft},
                {"wheelSideBeamDeform": k(11000), "wheelSideBeamStrength": k(15000)},
                {"wheelTreadBeamDeform": k(10000), "wheelTreadBeamStrength": k(13000)},
                {"wheelPeripheryBeamDeform": k(40000), "wheelPeripheryBeamStrength": k(40000)}]
        flex = []
        for c in ax.corners:
            nm = a + c.side.upper()
            x, y, z = ax.centers[c.side]
            flex.append(_place(self.name("tire_%s_%s" % (a, c.side.upper())), ["wheel_" + nm, "tire_" + nm],
                               (x * c.s, y, z)))
        psi = _axle_val(ax, "pressure", P.pressureF, P.pressureR)
        self.parts[self.name("tire_" + a)] = {
            "information": _info("%s %s %s Tires" % (side_word, w["size_text"], dict(
                street="Street", sport="Sport", allterrain="All-Terrain", mud="Mud", slick="Racing Slick")[P.tireType]),
                250, P),
            "slotType": self.name("tire_" + a),
            "variables": [VAR_HEAD, _var("$tirepressure_" + a, "psi", "Wheels", psi, 0, 80, "Tire Pressure",
                                         "Air pressure in the tires", stepDis=0.5, subCategory=side_word)],
            "flexbodies": _flex(flex),
            "pressureWheels": rows,
        }

    def wheeldata(self, ax):
        P = self.P
        a = ax.id
        side_word = axle_word(ax)
        rows = [PW_HEAD, {"selfCollision": False}, {"collision": True}]
        for c in sorted(ax.corners, key=lambda c: c.s):
            side, s = c.side, c.s
            nm = a + side.upper()
            inner, outer = ax.wheel_nodes[side]
            hub = ax.hub[side]
            inline = {"torqueCoupling:": hub["h1"], "torqueArm:": hub["h5"], "torqueArm2:": hub["h3"]}
            if ax.steer:
                inline.update({"steerAxisUp:": hub["h2"], "steerAxisDown:": hub["h1"]})
            rows.append({"axleBeams": ["axle_" + nm]})
            rows.append([nm, "wheel_" + nm, "tire_" + nm, outer, inner, 9999, hub["h5"], 1 if s < 0 else -1, inline])
        rows += [{"axleBeams": []}, {"selfCollision": True}]
        self.parts[self.name("wheeldata_" + a)] = {
            "information": _info("%s Spindles" % side_word, 100, P),
            "slotType": self.name("wheeldata_" + a),
            "pressureWheels": rows,
        }

    # -- powertrain ----------------------------------------------------------------------
    def engine(self):
        P, S, g = self.P, self.S, self.g
        e = g.engine
        nodes, beams, _, tris = self.by_part("engine")
        snd = SOUNDS.get(P.sound, SOUNDS["i4"])
        st = snd["starter"]
        main = {
            "torque": [["rpm", "torque"]] + [[int(r), _r(t, 1)] for r, t in e["curve"]],
            "idleRPM": e["idle"], "maxRPM": e["max_rpm"], "hasRevLimiter": True,
            "revLimiterType": "timeBased", "revLimiterRPM": "$revLimiterRPM", "revLimiterCutTime": 0.1,
            "inertia": _r(e["inertia"], 3), "friction": _r(e["friction"], 1), "dynamicFriction": _r(e["dyn_friction"], 4),
            "engineBrakeTorque": _r(e["brake_torque"], 1),
            "burnEfficiency": [[0, 0.1], [0.05, 0.22], [0.4, 0.3], [0.7, 0.33], [1, 0.25]],
            "energyStorage": "mainTank", "requiredEnergyType": "gasoline",
            "thermalsEnabled": False,
            "maxTorqueRating": round(e["peak_torque"] * 3), "maxOverTorqueDamage": 1000000,
            "torqueReactionNodes:": [e["names"]["1l"], e["names"]["2l"], e["names"]["4r"]],
            "uiName": "Engine", "soundConfig": "soundConfig", "soundConfigExhaust": "soundConfigExhaust",
            "starterSample": "event:>Engine>Starter>%s_eng" % st,
            "starterSampleExhaust": "event:>Engine>Starter>%s_exh" % st,
            "shutOffSampleEngine": "event:>Engine>Shutoff>%s_eng" % st,
            "shutOffSampleExhaust": "event:>Engine>Shutoff>%s_exh" % st,
            "starterVolume": 0.75, "starterVolumeExhaust": 0.6, "shutOffVolumeEngine": 0.5,
            "shutOffVolumeExhaust": 0.5, "starterThrottleKillTime": 1.2, "idleRPMStartRate": 1.25,
            "idleRPMStartCoef": 1.2, "particulates": 0.04,
        }
        gain = clamp(-10 + 6 * math.log10(max(P.power, 30) / 100.0) * 2, -12, 2)
        variables = [VAR_HEAD, _var("$revLimiterRPM", "rpm", "Engine", P.redline, round(P.redline * 0.6, -2),
                                    e["max_rpm"], "RPM Limit", "Where the rev limiter cuts in", stepDis=50)]
        d = {
            "information": _info("%d hp Engine" % round(P.power), 3000 + P.power * 20, P),
            "slotType": self.name("engine"),
            "slots": [["type", "default", "description"],
                      [self.name("transmission"), self.name("transmission"), "Transmission"]],
            "variables": variables,
            "powertrain": [["type", "name", "inputName", "inputIndex"],
                           ["combustionEngine", "mainEngine", "dummy", 0]],
            "mainEngine": main,
            "soundConfig": {"sampleName": snd["engine"], "intakeMuffling": 0.6, "mainGain": _r(gain, 1),
                            "onLoadGain": 1.0, "offLoadGain": 0.85, "maxLoadMix": 0.8, "minLoadMix": 0,
                            "fundamentalFrequencyCylinderCount": snd["cyl"], "eqFundamentalGain": 0},
            "soundConfigExhaust": {"sampleName": snd["exhaust"], "mainGain": _r(gain + 4, 1), "onLoadGain": 1.0,
                                   "offLoadGain": 0.9, "maxLoadMix": 0.8, "minLoadMix": 0,
                                   "fundamentalFrequencyCylinderCount": snd["cyl"], "eqFundamentalGain": -2},
            "vehicleController": {"highShiftUpRPM": int(P.redline - 250)},
            "flexbodies": _flex([_place(mesh, groups) for mesh, groups in getattr(g, "engine_flex", None) or [
                (self.name("engine"), [P.model_group("engine")]), (self.name("exhaust"), [P.model_group("body")])]]),
            "nodes": node_table(S, nodes),
            "beams": beam_table(S, beams),
        }
        if tris:
            d["triangles"] = tri_table(S, tris, drag=4)
        self.parts[self.name("engine")] = d

    def transmission(self):
        P, g = self.P, self.g
        gr = g.gearing
        n = len(gr["gears"])
        variables = [VAR_HEAD, _var("$gear_R", ":1", "Transmission", gr["reverse"], 0.5, 6, "Reverse Gear Ratio",
                                    "Torque multiplication ratio", stepDis=0.01)]
        for i, r in enumerate(gr["gears"], 1):
            variables.append(_var("$gear_%d" % i, ":1", "Transmission", r, 0.3, 6, "%s Gear Ratio" % _ordinal(i),
                                  "Torque multiplication ratio", stepDis=0.01))
        ratios = ["$=-$gear_R", 0] + ["$gear_%d" % i for i in range(1, n + 1)]
        pt = [["type", "name", "inputName", "inputIndex"]]
        d = {"information": _info("%d-Speed %s" % (n, "Automatic" if P.gearbox == "automatic" else "Manual"),
                                  1500, P),
             "slotType": self.name("transmission"), "variables": variables}
        if P.gearbox == "automatic":
            pt += [["torqueConverter", "torqueConverter", "mainEngine", 1],
                   ["automaticGearbox", "gearbox", "torqueConverter", 1]]
            d["torqueConverter"] = {"uiName": "Torque Converter", "converterDiameter": 0.26, "converterStiffness": 10,
                                    "couplingAVRatio": 0.95, "stallTorqueRatio": 2.0,
                                    "lockupClutchTorque": round(g.engine["peak_torque"] * 1.6)}
            d["gearbox"] = {"uiName": "Gearbox", "gearRatios": ratios, "parkLockTorque": 3000,
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
        if P.drive == "AWD":
            locked = P.diff == "locked"
            opts = {"diffType": "locked", "lockTorque": round(g.engine["peak_torque"] * 30)} if locked else \
                {"diffType": "viscous", "viscousCoef": 80, "viscousTorque": round(g.engine["peak_torque"] * 2.5)}
            opts.update({"diffTorqueSplit": 0.5, "uiName": "Center Differential", "defaultVirtualInertia": 0.1,
                         "friction": 2})
            pt += [["differential", "transfercase", "gearbox", 1, opts],
                   ["shaft", "driveshaft_F", "transfercase", 1, {"uiName": "Front Driveshaft", "friction": 1}],
                   ["shaft", "driveshaft_R", "transfercase", 2, {"uiName": "Rear Driveshaft", "friction": 1}]]
            slots += [[self.name("differential_F"), self.name("differential_F"), "Front Differential"],
                      [self.name("differential_R"), self.name("differential_R"), "Rear Differential"]]
        else:
            ax = "F" if P.drive == "FWD" else "R"
            pt += [["shaft", "driveshaft_" + ax, "gearbox", 1, {"uiName": "Driveshaft", "friction": 1}]]
            slots += [[self.name("differential_" + ax), self.name("differential_" + ax),
                       ("Front" if ax == "F" else "Rear") + " Differential"]]
        d["slots"] = slots
        d["powertrain"] = pt
        self.parts[self.name("transmission")] = d

    def differential(self, ax):
        P, g = self.P, self.g
        a = ax.id
        side_word = axle_word(ax)
        fd = g.gearing["final"]
        variables = [VAR_HEAD, _var("$finaldrive_" + a, ":1", "Differentials", fd, 1.5, 8, "Final Drive Gear Ratio",
                                    "Torque multiplication ratio", stepDis=0.01, subCategory=side_word)]
        opts = {"gearRatio": "$finaldrive_" + a, "uiName": side_word + " Differential",
                "defaultVirtualInertia": 0.25, "friction": 3}
        if P.diff == "lsd":
            opts.update({"diffType": "lsd", "lsdPreload": "$lsdpreload_" + a, "lsdLockCoef": "$lsdlockcoef_" + a,
                         "lsdRevLockCoef": "$lsdlockcoefrev_" + a})
            variables += [
                _var("$lsdpreload_" + a, "N/m", "Differentials", 100, 0, 500, "Pre-load Torque",
                     "Initial locking torque between left and right wheels", subCategory=side_word),
                _var("$lsdlockcoef_" + a, "", "Differentials", 0.2, 0, 0.5, "Power Lock Rate",
                     "Extra locking under power", minDis=0, maxDis=100, subCategory=side_word),
                _var("$lsdlockcoefrev_" + a, "", "Differentials", 0.1, 0, 0.5, "Coast Lock Rate",
                     "Extra locking when coasting", minDis=0, maxDis=100, subCategory=side_word)]
        elif P.diff == "locked":
            opts.update({"diffType": "locked", "lockTorque": round(g.engine["peak_torque"] * fd * 12)})
        else:
            opts["diffType"] = "open"
        pt = [["type", "name", "inputName", "inputIndex"],
              ["differential", "differential_" + a, "driveshaft_" + a, 1, opts]]
        for side, idx in [(c.side.upper(), 1 if c.s > 0 else 2) for c in sorted(ax.corners, key=lambda c: -c.s)]:
            nm = a + side
            pt.append(["shaft", "wheelaxle" + nm, "differential_" + a, idx,
                       {"connectedWheel": nm, "breakTriggerBeam": "axle_" + nm,
                        "uiName": "%s %s Axle" % (side_word, "Left" if side == "L" else "Right"), "friction": 1}])
        self.parts[self.name("differential_" + a)] = {
            "information": _info("%s %s Differential" % (side_word, dict(open="Open", lsd="Limited Slip",
                                                                         locked="Locked")[P.diff]), 400, P),
            "slotType": self.name("differential_" + a),
            "variables": variables,
            "powertrain": pt,
        }

    def fueltank(self):
        P = self.P
        cap = round(P.fuel)
        self.parts[self.name("fueltank")] = {
            "information": _info("%d L Fuel Tank" % cap, 150, P),
            "slotType": self.name("fueltank"),
            "variables": [VAR_HEAD, _var("$fuel", "L", "Chassis", round(cap * 0.75), 0, cap, "Fuel Volume",
                                         "Fuel in the tank at the start", stepDis=0.5)],
            "energyStorage": [["type", "name"], ["fuelTank", "mainTank"]],
            "mainTank": {"energyType": "gasoline", "fuelCapacity": cap, "startingFuelCapacity": "$fuel"},
        }


def _axle_val(ax, key, front, rear):
    """A per-axle setting (vehicles with any number of axles) or the design's front/rear one."""
    v = getattr(ax, key, None)
    return v if v is not None else (front if ax.front else rear)


def axle_word(ax):
    """ "Front", "Rear", or "Middle" (vehicles with more than two axles)."""
    return getattr(ax, "word", None) or ("Front" if ax.front else "Rear")


def _ordinal(i):
    return {1: "1st", 2: "2nd", 3: "3rd"}.get(i, "%dth" % i)


def _fmt(v):
    return ("%.5f" % v).rstrip("0").rstrip(".") or "0"
