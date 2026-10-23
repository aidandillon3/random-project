"""Vehicle Builder: a complete, drivable BeamNG vehicle from a handful of settings.

generate(spec) lays out the body, suspension, wheels and drivetrain from sizes
like length, wheelbase and track width (see spec.py for every setting), works
out spring rates, damping, gearing and brake torque from the weight and
power, and returns the JBeam parts and the 3D meshes of a vehicle mod.

Vehicle space as in the game: +x left, +y back, +z up; z = 0 is the ground
under the tires and y = 0 the middle of the wheelbase.
"""
import hashlib
import json
import math

from . import chassis, lights, mesh, parts, spec as specmod
from .chassis import G, SIDES, Axle, Structure, clamp, dist, v_sub
from .shape import Shape, lerp

VERSION = 1


class Generated:
    """What generate() returns."""

    def __init__(self, gen):
        self.P = gen.P
        self.spec = gen.clean_spec
        self.parts = gen.parts
        self.meshes = gen.meshes
        self.materials = gen.materials
        self.stats = gen.stats
        self.notes = gen.notes
        self.structure = gen.S

    def mesh_keys(self):
        """{mesh name: content hash} so a preview only sends meshes that changed."""
        out = {}
        for name, m in self.meshes.items():
            h = hashlib.sha1()
            h.update(m.pos_bytes())
            h.update(m.idx_bytes())
            out[name] = h.hexdigest()[:12]
        return out


class Generator:
    def __init__(self, spec, author=""):
        P, notes = specmod.normalize(spec)
        self.clean_spec = specmod.clean(spec)
        raw = spec if isinstance(spec, dict) else {}
        style = specmod.STYLE.get(P.style) or specmod.STYLE["sedan"]
        P.name = (str(raw.get("name") or "").strip()[:60]) or "My " + style["title"]
        P.brand = (str(raw.get("brand") or "").strip()[:40]) or "Custom"
        P.model = specmod.model_id(raw.get("model") or P.name)
        P.author = (str(author or raw.get("author") or "").strip()[:60])
        P.model_group = lambda s, m=P.model: m + "_" + s
        for k in ("name", "brand", "model"):
            self.clean_spec[k] = getattr(P, k)
        self.P = P
        self.notes = list(notes)

    def run(self):
        P = self.P
        self.shape = sh = Shape(P)
        self.S = S = Structure()
        S.kinds.update(chassis.beam_kinds(P))
        S.kind("exhaust", beamSpring=301000, beamDamp=20, beamDeform=8000, beamStrength=30000)
        self.axles = {"F": Axle(P, sh, "F"), "R": Axle(P, sh, "R")}
        self.axle_list = [self.axles["F"], self.axles["R"]]
        self.wheels = {}
        for ax in self.axle_list:
            self._wheel_nodes(ax)
        rings = chassis.build_body(S, sh, P)
        body_names = [n for n in S.order if S.nodes[n].part == "body"]
        self.lamps = self._lamps(body_names)
        low_body = [n for n in body_names if S.nodes[n].kind in ("fc", "fl", "sl", "bl")]
        for ax in self.axle_list:
            if ax.solid:
                chassis.build_solid(S, sh, P, ax, low_body)
            else:
                chassis.build_wishbone(S, sh, P, ax, low_body)
        chassis.build_engine(S, sh, P, self.axles, low_body)
        self.engine = dict(S.engine)
        self._exhaust(low_body)
        # nothing may be left free to swing out of a plane
        rest = [n for n in S.order if S.nodes[n].part != "body"]
        chassis.brace_degenerate(S, rest, low_body, kind="mount")
        self._mass()
        self._springs()
        self._brakes()
        self._engine_curve()
        self._gearing()
        self._camera(rings)
        self._drag()
        self.meshes, self.materials, flex = mesh.build_all(self)
        self.susp_flex, self.coil_flex = flex["susp"], flex["coil"]
        self.parts = parts.PartsBuilder(self).build()
        self._stats()
        self.notes += [n for n in sh.notes + S.notes if n not in self.notes]
        return Generated(self)

    # -- wheels -------------------------------------------------------------------------
    def _wheel_nodes(self, ax):
        P, S = self.P, self.S
        p = ax.p
        rim_in = getattr(ax, "rim_in", None) or (P.rimDiaF if ax.front else P.rimDiaR)
        rim_r = rim_in * specmod.INCH / 2
        tw = ax.tw
        rays = 16 if ax.r <= 0.42 else 20
        hub_w = clamp(tw * 0.85, 0.11, 0.42)
        hub_node_w = clamp(0.36 * (rim_r / 0.216) * (hub_w / 0.19), 0.18, 1.2)
        tire_node_w = clamp(0.27 * (ax.r / 0.33) * (tw / 0.225) ** 0.8, 0.1, 1.4)
        tire_scale = clamp((ax.r / 0.33) * (tw / 0.225) ** 0.5, 0.6, 2.6)
        hub_side_k = 1001000
        generated = 2 * rays * (hub_node_w + tire_node_w)
        axle_w = clamp(4.5 + 6.0 * (hub_node_w * 2 * rays) / 25.0, 4.5, 12.0)
        ax.wheel_nodes = {}
        loads = {}
        for c in ax.corners:
            side, s = c.side, c.s
            nm = ax.id + side.upper()
            inner = "%sw1%s" % (p, side)
            outer = "%sw1%s%s" % (p, side, side)
            x, y, z = ax.centers[side]
            for name, dx in ((inner, -0.1), (outer, 0.1)):
                n = S.node(name, (s * (x + dx), y, z), "wheel_" + ax.id, w=axle_w, groups=["wheelhub_" + nm],
                           kind="wheel", fric=0.5)
                n.coll, n.selfcoll = True, False
                n.extra = {"staticCollision": False}
                loads[name] = 2 * rays * hub_side_k
            ax.wheel_nodes[side] = (inner, outer)
        style = dict((k, v) for k, v in specmod.RIM_STYLES)[P.rimStyle]
        self.wheels[ax.id] = {
            "r": ax.r, "tw": tw, "rays": rays, "hub_r": max(rim_r - 0.005, 0.1), "hub_w": hub_w, "rim_in": rim_in,
            "hub_k": int(round(900000 * clamp(rim_r / 0.216, 0.8, 1.6), -3)) + 1000, "hub_side_k": hub_side_k,
            "hub_node_w": hub_node_w, "tire_node_w": tire_node_w, "tire_scale": tire_scale,
            "hub_deform": int(round(15000 * tire_scale, -3)), "hub_strength": int(round(66000 * tire_scale, -3)),
            "generated_mass": generated * len(ax.corners), "axle_load": loads, "style_title": style,
            "size_text": specmod.tire_size_text(tw * 1000, ax.r * 2 / specmod.INCH, rim_in),
        }

    def _lamps(self, body_names):
        """Light sources behind the head and tail lamps the body is drawn with."""
        sh = self.shape
        lay = mesh.lamp_layout(sh)
        lamps = []
        for kind, y in (("head", sh.y0), ("tail", sh.y1)):
            for x0, x1, z0, z1 in lay[kind]:
                cx = (x0 + x1) / 2
                lamps.append((kind, "l" if cx > 0 else "r", (cx, y, (z0 + z1) / 2)))
        made = lights.add_lamps(self.S, self.P, lamps, body_names)
        chassis.brace_degenerate(self.S, [n for n in self.S.order if self.S.nodes[n].kind == "lamp"], body_names,
                                 kind="lamp")
        return made

    def _exhaust(self, body_names):
        S, P, sh = self.S, self.P, self.shape
        e = self.engine
        names = e["names"]
        S.nodes[names["2r"]].extra = {"isExhaust": "mainEngine"}
        right = -1
        y_tip = sh.y1 - 0.06 if P.enginePos != "rear" else sh.y1 - 0.04
        x_tip = right * min(sh.half_width(y_tip) * 0.55, 0.55)
        z_tip = sh.bottom(y_tip) + 0.07
        y_mid = (e["y"] + e["l"] / 2 + y_tip) / 2
        z_mid = max(P.clearance + 0.05, sh.bottom(y_mid) + 0.04)
        x_mid = right * min(0.25, sh.x_in() * 0.6)
        n1 = S.node("ex1", (x_mid, y_mid, z_mid), "engine", w=2.0, groups=[P.model_group("body")], kind="piv")
        n0 = S.node("ex0", (x_tip, y_tip, z_tip), "engine", w=2.0, groups=[P.model_group("body")], kind="piv",
                    extra={"afterFireAudioCoef": 0.6, "afterFireVisualCoef": 0.6, "exhaustAudioMufflingCoef": 0.6})
        for n in (n1, n0):
            n.coll, n.selfcoll = False, False
        S.beam(names["2r"], "ex1", "engine", "exhaust", isExhaust="mainEngine")
        S.beam("ex1", "ex0", "engine", "exhaust", isExhaust="mainEngine")
        for nm in ("ex1", "ex0"):
            S.attach(nm, body_names, "engine", "exhaust", n=3)
        self.exhaust = [S.pos(names["2r"]), S.pos("ex1"), S.pos("ex0")]

    # -- physics numbers ---------------------------------------------------------------
    def _assign_masses(self):
        chassis.assign_masses(self.S, self.P, self.axles, self.wheels)

    def _mass(self):
        S = self.S
        self._assign_masses()
        self.masses = {}
        total = sum(n.w for n in S.nodes.values()) + sum(w["generated_mass"] for w in self.wheels.values())
        self.total_mass = total
        # centre of gravity and axle loads
        mx = my = mz = 0.0
        for n in S.nodes.values():
            mx += n.w * n.p[0]
            my += n.w * n.p[1]
            mz += n.w * n.p[2]
        for ax in self.axle_list:
            gm = self.wheels[ax.id]["generated_mass"]
            my += gm * ax.y
            mz += gm * ax.zc
        self.cg = (mx / total, my / total, mz / total)
        shares = axle_shares([ax.y for ax in self.axle_list], self.cg[1])
        for ax, sh_ in zip(self.axle_list, shares):
            ax.share = sh_
        self.front_share = clamp(self.axle_list[0].share, 0.05, 0.95)
        for ax in self.axle_list:
            unsprung = sum(S.nodes[n].w for n in set(ax.unsprung_nodes))
            unsprung += sum(S.nodes[n].w for c in ax.corners for n in ax.wheel_nodes[c.side])
            unsprung += self.wheels[ax.id]["generated_mass"]
            share = ax.share
            # a solid axle housing's weight is part of it
            ax.unsprung = unsprung
            n = len(ax.corners)
            ax.corner_sprung = max(total * share - unsprung, total * share * 0.35) / n
            ax.corner_load = total * share / n

    def _springs(self):
        P, S = self.P, self.S
        self.springs = {}
        for ax in self.axle_list:
            f = getattr(ax, "freq", None) or (P.springF if ax.front else P.springR)
            zeta = getattr(ax, "zeta", None) or (P.dampF if ax.front else P.dampR)
            m = ax.corner_sprung
            mr = clamp(ax.mr, 0.5, 1.0)
            k_wheel = m * (2 * math.pi * f) ** 2
            k_spring = k_wheel / (mr * mr)
            static = m * G / mr
            preload = static / k_spring
            c_crit = 2 * math.sqrt(k_wheel * m)
            bump = zeta * c_crit * 0.75 / (mr * mr)
            rebound = zeta * c_crit * 1.45 / (mr * mr)
            lo, hi = ax.spring_ends[ax.corners[0].side]
            length = dist(S.pos(lo), S.pos(hi))
            bump_range = max(ax.bump * mr, 0.02)
            droop_range = max(ax.droop * mr, 0.02)
            hard = clamp((length - bump_range - 0.03) / length, 0.3, 0.97)
            sw = getattr(ax, "swaybar", None)
            sw = sw if sw is not None else (P.swaybarF if ax.front else P.swaybarR)
            arb = sw * k_wheel * 1.6
            self.springs[ax.id] = {
                "spring": k_spring, "wheel_rate": k_wheel, "static": static, "preload": round(preload, 4),
                "bump": bump, "rebound": rebound, "bump_range": bump_range, "droop_range": droop_range,
                "hard_stop": round(hard, 3), "stop_rate": max(8 * k_spring, 120000), "stop_damp": max(0.8 * rebound, 3000),
                "arb_rate": arb, "length": length, "freq": f, "mr": mr,
            }
            sp = self.springs[ax.id]
            # the coilover's beams (written by parts.py) as one stand-in for the stability check
            S.kind("calc_coil_" + ax.id, beamSpring=k_spring + sp["stop_rate"] * 5, beamDamp=rebound + sp["stop_damp"])
            for c in ax.corners:
                lo, hi = ax.spring_ends[c.side]
                S.beam(lo, hi, "calc_coil_" + ax.id, "calc_coil_" + ax.id)
        # the coilover beams were not known when the masses were set: check stability again
        chassis.stabilize(S, self.wheels)

    def _brakes(self):
        P = self.P
        self.brakes = {}
        force = 1.15 * self.total_mass * G * P.brakePower
        for ax in self.axle_list:
            rim_in = getattr(ax, "rim_in", None) or (P.rimDiaF if ax.front else P.rimDiaR)
            dia = clamp(rim_in * specmod.INCH - 0.075, 0.2, 0.42)
            coef = force * ax.r / 2.0           # per wheel, if all the force were on this axle
            parking = 0.0 if ax.front else 0.45 * force * ax.r / 2.0
            self.brakes[ax.id] = {"coef": coef, "parking": parking, "diameter": dia,
                                  "mass": clamp(4.0 + 40 * (dia - 0.25), 3.0, 14.0)}

    def _engine_curve(self):
        P = self.P
        c = P.torqueShape
        red = P.redline
        fr = [0.0, 0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 1.0, 1.1, 1.25]
        low = [0.0, 0.72, 0.86, 0.95, 1.0, 0.99, 0.96, 0.91, 0.85, 0.78, 0.7, 0.55, 0.35]
        high = [0.0, 0.42, 0.52, 0.61, 0.7, 0.79, 0.87, 0.94, 0.99, 1.0, 0.95, 0.75, 0.45]
        shape = [lerp(a, b, c) for a, b in zip(low, high)]
        # scale so the peak power (up to the redline) is what was asked for
        best = max(t * f * red for f, t in zip(fr, shape) if f <= 1.0)
        peak_t = P.power * 745.7 / (best * 2 * math.pi / 60)
        curve = [(f * red, t * peak_t) for f, t in zip(fr, shape)]
        idle = int(round(lerp(700, 1050, c), -1))
        self.engine.update({
            "curve": curve, "peak_torque": max(t for _, t in curve), "idle": idle,
            "max_rpm": int(round(red * 1.12, -2)),
            "inertia": clamp(0.06 + P.power / 2600.0, 0.05, 0.6) * lerp(1.3, 0.75, c),
            "friction": clamp(8 + P.power / 22.0, 8, 60), "dyn_friction": 0.022 + 0.008 * c,
            "brake_torque": clamp(peak_t * 0.18, 15, 300),
        })
        e = self.engine
        e["peak_power_rpm"] = max(curve, key=lambda rt: rt[1] * rt[0] if rt[0] <= red else 0)[0]
        e["peak_torque_rpm"] = max(curve, key=lambda rt: rt[1])[0]

    def _gearing(self):
        P = self.P
        e = self.engine
        n = int(P.gears)
        r = max(self.axles["R"].r if P.drive != "FWD" else self.axles["F"].r, 0.2)
        v_top = P.topSpeed / 3.6
        red = P.redline
        overall_top = (red * 0.98 * 2 * math.pi / 60) * r / v_top
        top_gear = 0.72 if n >= 6 else 0.82 if n >= 5 else 1.0
        final = clamp(overall_top / top_gear, 2.2, 7.5)
        top_gear = overall_top / final
        driven_share = 1.0 if P.drive == "AWD" else (self.front_share if P.drive == "FWD" else 1 - self.front_share)
        traction = 1.05 * self.total_mass * G * driven_share
        first_overall = traction * r / max(e["peak_torque"] * 0.92, 1.0)
        first = clamp(first_overall / final, top_gear * 2.2, 5.5)
        ratios = []
        for i in range(n):
            t = i / max(n - 1, 1)
            # slightly progressive spacing, like real gearboxes
            ratios.append(round(first * (top_gear / first) ** (t ** 0.92), 3))
        self.gearing = {"gears": ratios, "reverse": round(first * 1.05, 3), "final": round(final, 3)}

    def _camera(self, rings):
        S, P, sh = self.S, self.P, self.shape
        y_seat = sh.y_wst + 0.35 if not P.openTop else sh.y_open0 + 0.45
        y_seat = clamp(y_seat, sh.y_cowl + 0.35, sh.y_rt - 0.05 if not P.openTop else sh.y_open1 - 0.2)
        x = clamp(0.36 * P.width / 1.8, 0.25, 0.55)
        floor = P.clearance + 0.08
        z = min(floor + 1.0, (sh.top(y_seat) - 0.13) if sh.kind(y_seat) == "cabin" else floor + 1.0)
        z = max(z, floor + 0.65)
        near = sorted(rings, key=lambda r: abs(r["y"] - y_seat))[:2]
        ids = []
        for r in near:
            ids += [r["ids"]["fll"], r["ids"]["flr"], r["ids"]["bll"]]
        self.camera = {"p": (x, y_seat, z), "nodes": ids[:6], "seat_y": y_seat, "seat_x": x, "floor": floor}

    def _drag(self):
        P = self.P
        cd = {"sedan": 0.3, "hatchback": 0.32, "coupe": 0.3, "wagon": 0.32, "suv": 0.37, "offroad": 0.45,
              "pickup": 0.43, "van": 0.38, "roadster": 0.36, "supercar": 0.32}.get(P.style, 0.34)
        area = P.width * (P.height - P.clearance) * 0.85
        self.cd, self.frontal_area = cd, area
        n = sum(1 for t in self.S.tris if t[4] == "body")
        # the game sums drag over every triangle facing the air; aim the body's total at cd * area
        self.drag_per_tri = clamp(cd * 26.0, 4.0, 14.0)
        self.body_tri_count = n

    # -- numbers for the page -------------------------------------------------------------
    def _stats(self):
        P = self.P
        e, gr = self.engine, self.gearing
        mass = self.total_mass
        aF, aR = self.axles["F"], self.axles["R"]
        wb = aR.y - aF.y
        steer = math.radians(P.steerAngle)
        turning = (wb / math.sin(steer) + P.trackF / 2) * 2
        sh = self.shape
        # approach and departure angles from the bumper bottoms to the tire contact patches
        app = math.degrees(math.atan2(sh.bottom(sh.y0 + 0.03), aF.y - aF.r * 0.3 - sh.y0))
        dep = math.degrees(math.atan2(sh.bottom(sh.y1 - 0.03), sh.y1 - (aR.y + aR.r * 0.3)))
        brk = math.degrees(2 * math.atan2(P.clearance, wb / 2))
        accel, top = self._performance()
        sp = self.springs
        self.stats = {
            "mass": round(mass), "massTarget": round(P.mass), "frontShare": round(self.front_share, 3),
            "cgHeight": round(self.cg[2], 3),
            "rollG": rollover(self.axle_list, self.cg[2], P.tireType)[0],
            "rollRisk": rollover(self.axle_list, self.cg[2], P.tireType)[1],
            "powerHP": round(P.power), "powerKW": round(P.kw), "torqueNm": round(e["peak_torque"]),
            "torqueRPM": int(round(e["peak_torque_rpm"], -2)), "powerRPM": int(round(e["peak_power_rpm"], -2)),
            "hpPerTon": round(P.power / (mass / 1000.0)),
            "zeroTo100": accel, "topSpeed": top, "gears": gr["gears"], "finalDrive": gr["final"],
            "turningCircle": round(turning, 1), "approach": round(app, 1), "departure": round(dep, 1),
            "breakover": round(brk, 1), "rearOverhang": round(P.rearOverhang, 3),
            "tireF": self.wheels["F"]["size_text"], "tireR": self.wheels["R"]["size_text"],
            "springF": round(sp["F"]["spring"], -2), "springR": round(sp["R"]["spring"], -2),
            "wheelRateF": round(sp["F"]["wheel_rate"], -2), "wheelRateR": round(sp["R"]["wheel_rate"], -2),
            "nodes": len(self.S.nodes), "beams": len(self.S.beams), "triangles": len(self.S.tris),
            "stabilityAdded": round(getattr(self.S, "stability_added", 0.0), 1),
            "curve": [[int(r), round(t, 1)] for r, t in e["curve"]], "redline": int(P.redline),
            "cd": self.cd,
        }

    def _performance(self):
        """Rough 0-100 km/h time and top speed: power and grip against drag and rolling."""
        P = self.P
        e, gr = self.engine, self.gearing
        mass = self.total_mass
        r = self.axles["R"].r if P.drive != "FWD" else self.axles["F"].r
        rho = 1.2
        cda = self.cd * self.frontal_area
        crr = 0.013 if P.tireType in ("street", "sport", "slick") else 0.02
        mu = {"street": 1.0, "sport": 1.1, "allterrain": 0.95, "mud": 0.9, "slick": 1.25}[P.tireType]
        driven = 1.0 if P.drive == "AWD" else (self.front_share if P.drive == "FWD" else 1 - self.front_share)
        curve = e["curve"]

        def torque(rpm):
            for (r0, t0), (r1, t1) in zip(curve, curve[1:]):
                if r0 <= rpm <= r1:
                    return lerp(t0, t1, (rpm - r0) / max(r1 - r0, 1))
            return 0.0

        v, t = 0.1, 0.0
        dt = 0.05
        top = 0.0
        t100 = None
        for _ in range(int(120 / dt)):
            best = 0.0
            for g in gr["gears"]:
                ratio = g * gr["final"]
                rpm = v / r * ratio * 60 / (2 * math.pi)
                if rpm > P.redline:
                    continue
                rpm = max(rpm, e["idle"] + 1500)
                f = torque(min(rpm, P.redline)) * ratio * 0.88 / r
                best = max(best, f)
            best = min(best, mu * mass * G * driven * 1.05)
            drag = 0.5 * rho * cda * v * v + crr * mass * G
            a = (best - drag) / (mass * 1.08)
            if a <= 0.01:
                break
            v += a * dt
            t += dt
            if t100 is None and v >= 100 / 3.6:
                t100 = round(t + 0.35, 1)
            top = max(top, v)
        return t100, round(top * 3.6)


GRIP = {"street": 1.0, "sport": 1.1, "allterrain": 0.95, "mud": 0.9, "slick": 1.25}


def rollover(axles, cg_height, tire_type):
    """(sideways g at which the vehicle starts to tip, does it tip before the tires slide).

    Static stability: half the track over the centre of gravity height, less a tenth for body roll."""
    tot = sum(ax.share for ax in axles) or 1.0
    track = sum(ax.track * ax.share for ax in axles) / tot
    g_tip = round(0.9 * track / (2 * max(cg_height, 0.05)), 2)
    return g_tip, g_tip < GRIP.get(tire_type, 1.0)


def axle_shares(ys, y_cg):
    """Share of the weight each axle carries (lever rule; spread evenly over the middle axles of 3+)."""
    n = len(ys)
    if n == 1:
        return [1.0]
    if n == 2:
        a, b = ys
        f = clamp((b - y_cg) / (b - a) if b != a else 0.5, 0.05, 0.95)
        return [f, 1 - f]
    mean = sum(ys) / n
    var = sum((y - mean) ** 2 for y in ys) or 1.0
    out = [max(1.0 / n + (y - mean) * (y_cg - mean) / var, 0.04) for y in ys]
    t = sum(out)
    return [v / t for v in out]


def generate(spec, author=""):
    return Generator(spec, author).run()


def preview_payload(g, have=()):
    """Mesh data the page doesn't have yet, keyed by name@hash, plus material colours."""
    keys = g.mesh_keys()
    have = set(have or ())
    out = {}
    for name, m in g.meshes.items():
        key = "%s@%s" % (name, keys[name])
        if key in have:
            continue
        out[key] = m.to_preview(g.materials)
    return keys, out
