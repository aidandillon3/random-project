"""Vehicle Builder: generated vehicles build cleanly, are stable, steer and drive the right way, and save as mods."""
import base64
import json
import math
import os
import random
import sys
import unittest
from array import array

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from test_backend import InstallCase  # noqa: E402
from editor import build, dae, jbeamfile, model, vehiclegen  # noqa: E402
from editor.vehiclegen import chassis, spec as vgspec  # noqa: E402

STYLES = [s["id"] for s in vgspec.STYLES]


def build_generated(g):
    m = model.Model.from_parts(None, g.P.model, g.parts)
    return build.build(m, {})


def node(scene, nid):
    for n in scene["nodes"]:
        if n["id"] == nid:
            return n
    raise KeyError(nid)


class GeneratorTests(unittest.TestCase):
    def test_every_style_builds_clean(self):
        for style in STYLES:
            with self.subTest(style=style):
                g = vehiclegen.generate({"style": style})
                sc = build_generated(g)
                self.assertEqual(sc["warnings"], [])
                self.assertEqual(len(sc["wheels"]), 4)
                self.assertEqual(sorted(w["name"] for w in sc["wheels"]), ["FL", "FR", "RL", "RR"])
                ids = {n["id"] for n in sc["nodes"]}
                for k, v in sc["refNodes"].items():
                    self.assertIn(v, ids, k)
                # every flexbody has its mesh, and every mesh has triangles
                for fb in sc["flexbodies"]:
                    self.assertIn(fb["mesh"], g.meshes, fb["mesh"])
                for name, M in g.meshes.items():
                    self.assertGreater(M.tri_count(), 0, name)
                self.assertEqual(g.notes, [], style)
                st = g.stats
                self.assertLess(abs(st["mass"] - st["massTarget"]) / st["massTarget"], 0.08)
                self.assertTrue(st["zeroTo100"] and 1.5 < st["zeroTo100"] < 30, st["zeroTo100"])
                self.assertGreater(st["topSpeed"], 80)

    def test_text_round_trip(self):
        g = vehiclegen.generate({"style": "pickup"})
        text = jbeamfile.dumps_part_file(g.parts)
        parsed = jbeamfile.loads(text)
        self.assertEqual(set(parsed), set(g.parts))
        m = model.Model.from_parts(None, g.P.model, parsed)
        sc = build.build(m, {})
        self.assertEqual(sc["warnings"], [])
        self.assertEqual(len(sc["nodes"]), len(build_generated(g)["nodes"]))

    def test_stable_node_weights(self):
        cases = [{"style": s} for s in STYLES] + [
            {"style": "roadster", "mass": 350, "power": 900, "tireDiaF": 30, "tireDiaR": 32},
            {"style": "van", "mass": 7500, "power": 120},
            {"style": "offroad", "tireDiaF": 46, "tireDiaR": 46, "tireWidthF": 450, "tireWidthR": 450,
             "travelF": 0.6, "travelR": 0.6, "clearance": 0.6},
            {"style": "supercar", "springF": 3.5, "springR": 3.5, "dampF": 1.0, "dampR": 1.0},
        ]
        for spec in cases:
            with self.subTest(spec=spec):
                g = vehiclegen.generate(spec)
                bad = chassis.stability_report(g.structure, g.structure and _wheels(g))
                self.assertEqual(bad, [])

    def test_no_node_free_to_swing(self):
        for spec in [{"style": s} for s in STYLES] + [
                {"style": "pickup", "wheelbase": 3.8, "tireDiaF": 35, "tireDiaR": 35},
                {"style": "roadster", "suspF": "solid", "suspR": "solid"}]:
            with self.subTest(spec=spec):
                g = vehiclegen.generate(spec)
                self.assertEqual(chassis.degenerate_nodes(g.structure), [])

    def test_random_designs(self):
        rnd = random.Random(7)
        for i in range(40):
            spec = {"style": rnd.choice(STYLES)}
            for f in vgspec.FIELDS:
                if rnd.random() < 0.5:
                    if f["type"] == "range":
                        spec[f["key"]] = rnd.uniform(f["min"], f["max"])
                    elif f["type"] == "choice":
                        spec[f["key"]] = rnd.choice(f["options"])[0]
                    elif f["type"] == "bool":
                        spec[f["key"]] = rnd.random() < 0.5
            with self.subTest(i=i, spec=spec):
                g = vehiclegen.generate(spec)
                sc = build_generated(g)
                self.assertEqual(sc["warnings"], [])
                self.assertEqual(len(sc["wheels"]), 4)
                self.assertEqual(chassis.stability_report(g.structure, _wheels(g)), [])
                self.assertEqual(chassis.degenerate_nodes(g.structure), [])

    def test_wheel_axle_nodes_follow_the_game(self):
        # node1 is the outer axle node ("rr"/"ll"); wheelDir 1 on the right, -1 on the left
        g = vehiclegen.generate({"style": "sedan"})
        sc = build_generated(g)
        for w in sc["wheels"]:
            n1, n2 = sc["nodes"][w["n1"]], sc["nodes"][w["n2"]]
            self.assertGreater(abs(n1["p"][0]), abs(n2["p"][0]), w["name"])
            self.assertTrue(n1["id"].endswith(("rr", "ll")), n1["id"])
        rows = [r for r in g.parts[g.P.model + "_wheeldata_F"]["pressureWheels"] if isinstance(r, list)][1:]
        dirs = {r[0]: r[7] for r in rows}
        self.assertEqual(dirs, {"FR": 1, "FL": -1})
        # wheel centres sit at half the track, at tire radius height
        fr = [w for w in sc["wheels"] if w["name"] == "FR"][0]
        c = [(sc["nodes"][fr["n1"]]["p"][k] + sc["nodes"][fr["n2"]]["p"][k]) / 2 for k in range(3)]
        self.assertAlmostEqual(c[0], -g.P.trackF / 2, places=4)
        self.assertAlmostEqual(c[2], g.P.rF, places=4)
        self.assertAlmostEqual(fr["radius"], g.P.rF, places=4)

    def test_steering_turns_right_for_positive_input(self):
        for susp in ("wishbone", "solid"):
            g = vehiclegen.generate({"style": "sedan", "suspF": susp})
            hyd = [r for r in g.parts[g.P.model + "_suspension_F"]["hydros"] if isinstance(r, list)][1:]
            self.assertEqual(len(hyd), 2)
            for a, b, opts in hyd:
                self.assertEqual(opts["inputSource"], "steering_input")
                inner, arm = (a, b) if "h3" in b else (b, a)
                S = g.structure
                kp = S.pos(arm.replace("h3", "h1"))
                self.assertGreater(S.pos(arm)[1], kp[1], "the steering arm is behind the kingpin")
                # right turn: the arm behind the kingpin swings to +x; tie rod length change has the factor's sign
                p_in, p_arm = S.pos(inner), S.pos(arm)
                before = chassis.dist(p_in, p_arm)
                after = chassis.dist(p_in, (p_arm[0] + 0.01, p_arm[1], p_arm[2]))
                self.assertEqual(math.copysign(1, after - before), math.copysign(1, opts["factor"]), (susp, a, b))

    def test_lights_shine_from_the_lamps(self):
        for style in ("sedan", "pickup", "roadster"):
            g = vehiclegen.generate({"style": style})
            check_lights(self, g)

    def test_springs_hold_the_design_ride_height(self):
        g = vehiclegen.generate({"style": "suv"})
        S = g.structure
        for ax in ("F", "R"):
            beams = g.parts[g.P.model + "_coilover_" + ax]["beams"]
            spring = [r for r in beams if isinstance(r, list) and len(r) == 3 and "precompressionRange" in r[2]]
            self.assertEqual(len(spring), 2)
            pre = float(spring[0][2]["precompressionRange"].split("+")[-1])
            k = [v for v in g.parts[g.P.model + "_coilover_" + ax]["variables"] if v[0] == "$spring_" + ax][0][4]
            axle = [a for a in _gen(g).axle_list if a.id == ax][0]
            load = axle.corner_sprung * chassis.G / axle.mr
            self.assertAlmostEqual(pre * k, load, delta=load * 0.03)

    def test_meshes_face_outward(self):
        g = vehiclegen.generate({"style": "sedan"})
        M = g.meshes[g.P.model + "_body"]
        out = inward = 0.0
        for mat, lst in M.t.items():
            for t in range(0, len(lst), 3):
                a, b, c = (M.at(i) for i in lst[t:t + 3])
                n = chassis.v_cross(chassis.v_sub(b, a), chassis.v_sub(c, a))
                area = chassis.v_len(n)
                cen = tuple((a[k] + b[k] + c[k]) / 3 for k in range(3))
                axis = (0.0, cen[1], g.P.height * 0.5)
                if chassis.v_dot(n, chassis.v_sub(cen, axis)) >= 0:
                    out += area
                else:
                    inward += area
        self.assertGreater(out / (out + inward), 0.9)

    def test_collision_triangles_face_outward(self):
        g = vehiclegen.generate({"style": "wagon"})
        S = g.structure
        good = total = 0
        for a, b, c, part, kind in S.tris:
            if part != "body":
                continue
            pa, pb, pc = S.pos(a), S.pos(b), S.pos(c)
            n = chassis.v_cross(chassis.v_sub(pb, pa), chassis.v_sub(pc, pa))
            cen = tuple((pa[k] + pb[k] + pc[k]) / 3 for k in range(3))
            axis = (0.0, cen[1], (g.P.clearance + g.P.beltline) / 2)
            total += 1
            good += chassis.v_dot(n, chassis.v_sub(cen, axis)) > 0
        self.assertGreater(good / total, 0.93)


def check_lights(tc, g):
    """Head, tail, brake and reverse lights set up the way BeamNG's own vehicles have them, on the front and
    back faces of the body."""
    S = g.structure
    ys = [S.nodes[n].p[1] for n in S.order if S.nodes[n].part == "body" and S.nodes[n].kind != "lamp"]
    y_front, y_back = min(ys), max(ys)
    table = g.parts[g.P.model + "_body"]["props"]
    rows = [r for r in table[1:] if isinstance(r, list)]
    tc.assertEqual(sorted(r[0] for r in rows), sorted(["lowbeam", "highbeam", "brake", "lowhighbeam", "reverse"] * 2))
    cookie = False
    for r in table[1:]:
        if isinstance(r, dict):
            cookie = cookie or "cookieName" in r
            continue
        func, mesh, ref, xn, yn, rot = r[:6]
        tc.assertEqual(mesh, "SPOTLIGHT")
        pr, px, py = S.pos(ref), S.pos(xn), S.pos(yn)
        # the X node straight below the reference, the Y node across at the same height toward +x
        tc.assertLess(px[2], pr[2])
        tc.assertAlmostEqual(px[0], pr[0])
        tc.assertGreater(py[0], pr[0])
        tc.assertAlmostEqual(py[2], pr[2])
        front = func in ("lowbeam", "highbeam")
        tc.assertEqual(rot["x"], 180 if front else 0, func)
        tc.assertEqual(r[-1]["baseTranslation"]["z"], -0.1 if front else 0.1)
        tc.assertAlmostEqual(pr[1], y_front if front else y_back, delta=0.02, msg=func)
        # rear lights come before the headlights' options (the cookie texture) are set
        tc.assertEqual(cookie, front, func)
    tc.assertEqual(chassis.degenerate_nodes(S, [n for n in S.order if S.nodes[n].kind == "lamp"]), [])
    # and a horn at the front
    ss = g.parts[g.P.model + "_body"]["soundscape"]
    tc.assertEqual(ss[0], ["name", "src"])
    tc.assertEqual(ss[2][0], "horn")
    tc.assertAlmostEqual(S.pos(ss[1]["node:"])[1], y_front, delta=0.3)


def _gen(g):
    # the Generator that made g (for numbers not kept on the result)
    gen = vehiclegen.Generator(g.spec)
    gen.run()
    return gen


def _wheels(g):
    gen = _gen(g)
    return gen.wheels


class BuilderApiTests(InstallCase):
    def test_every_style_passes_the_checker(self):
        # the same full check the editor's Check button runs: no errors, and nothing the game would warn about
        for style in STYLES:
            spec = {"style": style, "name": "Check " + style}
            if style in ("pickup", "offroad"):
                spec.update({"suspR": "solid", "drive": "AWD", "diff": "lsd"})
            res = self.app.api_builder_create({"spec": spec})
            out = self.app.api_check({"model": res["model"]})
            with self.subTest(style=style):
                self.assertEqual(out["errors"], [])
                self.assertEqual(out["warnings"], [])

    def test_preview_payload(self):
        r = self.app.api_builder_preview({"spec": {"style": "coupe", "name": "Speedy"}})
        self.assertEqual(r["model"], "speedy")
        self.assertEqual(r["scene"]["warnings"], [])
        names = {fb["mesh"] for fb in r["scene"]["flexbodies"]}
        self.assertTrue(all("@" in n for n in names))
        self.assertEqual(names, set(r["meshes"]))
        one = r["meshes"][sorted(names)[0]]
        pos = array("f")
        pos.frombytes(base64.b64decode(one["pos"]))
        idx = array("I")
        idx.frombytes(base64.b64decode(one["idx"]))
        self.assertEqual(len(pos) % 3, 0)
        self.assertEqual(sum(gr[1] for gr in one["groups"]), len(idx))
        self.assertLess(max(idx), len(pos) // 3)
        for gr in one["groups"]:
            self.assertIn(gr[2], r["materials"])
        # meshes the page already has are not sent again
        r2 = self.app.api_builder_preview({"spec": {"style": "coupe", "name": "Speedy"}, "have": sorted(names)})
        self.assertEqual(r2["meshes"], {})
        r3 = self.app.api_builder_preview({"spec": {"style": "coupe", "name": "Speedy", "paint": "#00ff00"},
                                          "have": sorted(names)})
        self.assertEqual(r3["meshes"], {})   # paint is a material, not geometry
        self.assertNotEqual(r3["materials"]["speedy_paint"]["color"], r["materials"]["speedy_paint"]["color"])
        self.assertFalse(r["exists"]["taken"])

    def test_create_open_and_drive_setup(self):
        res = self.app.api_builder_create({"spec": {"style": "pickup", "name": "Work Truck"}})
        self.assertEqual(res["model"], "work_truck")
        vdir = res["path"]
        for fn in ("work_truck.jbeam", "work_truck_suspension.jbeam", "work_truck_powertrain.jbeam",
                   "work_truck.dae", "main.materials.json", "info.json", "base.pc", "info_base.json",
                   "jbeam_editor_builder.json"):
            self.assertTrue(os.path.exists(os.path.join(vdir, fn)), fn)
        st = self.app.api_state({})
        v = [x for x in st["vehicles"] if x["model"] == "work_truck"][0]
        self.assertTrue(v["editable"] and v["builder"])
        self.assertEqual(v["name"], "Work Truck")
        summ = self.app.api_open({"model": "work_truck"})
        self.assertEqual(summ["defaultConfig"], "base")
        self.assertEqual(summ["errors"], [])
        sc = self.app.api_build({"model": "work_truck", "configName": "base"})
        self.assertEqual(sc["warnings"], [])
        self.assertEqual(len(sc["wheels"]), 4)
        # every mesh is found in the written .dae
        want = sorted({fb["mesh"] for fb in sc["flexbodies"]})
        blob = self.app.api_meshes({"model": "work_truck", "names": want})
        n = int.from_bytes(blob[:4], "little")
        head = json.loads(blob[4:4 + n])
        self.assertEqual(head["missing"], [])
        self.assertEqual(set(head["meshes"]), set(want))
        # materials for every material the meshes use, with lights that glow
        with open(os.path.join(vdir, "main.materials.json")) as f:
            mats = json.load(f)
        with open(os.path.join(vdir, "work_truck.dae"), "rb") as f:
            text = f.read().decode("utf-8")
        for mname in set(__import__("re").findall(r'<material id="([^"]+)-material"', text)):
            self.assertIn(mname, mats)
        self.assertIn("work_truck_headlight_on", mats)
        body = self.app.ws.model("work_truck").parts["work_truck_body"].data
        self.assertIn("work_truck_headlight", body["glowMap"])
        # the design is kept for the Builder
        self.assertEqual(self.app.api_builder_load({"model": "work_truck"})["spec"]["style"], "pickup")
        # the Garage understands it: a lift moves the front suspension down
        g = self.app.api_build({"model": "work_truck", "configName": "base", "garage": {"axles": {"F": {"lift": 0.08}}}})
        self.assertEqual(g["warnings"], [])
        self.assertAlmostEqual(node(g, "fh1l")["p"][2], node(sc, "fh1l")["p"][2] - 0.08, places=5)
        self.assertEqual(node(g, "bf3l")["p"], node(sc, "bf3l")["p"])

    def test_names_and_updates(self):
        with self.assertRaises(ValueError):
            self.app.api_builder_create({"spec": {"style": "sedan", "name": "testcar"}})
        a = self.app.api_builder_create({"spec": {"style": "sedan", "name": "Family"}})
        self.assertFalse(a["updated"])
        r = self.app.api_builder_preview({"spec": {"style": "sedan", "name": "Family"}})
        self.assertEqual(r["exists"], {"taken": True, "builder": True, "where": "Family".lower()})
        b = self.app.api_builder_create({"spec": {"style": "sedan", "name": "Family", "power": 300},
                                         "picture": "data:image/png;base64," + base64.b64encode(b"\x89PNG\r\n").decode()})
        self.assertTrue(b["updated"])
        self.assertEqual(a["path"], b["path"])
        self.assertTrue(os.path.exists(os.path.join(b["path"], "default.png")))
        self.assertEqual(self.app.api_builder_load({"model": "family"})["spec"]["power"], 300)
        backups = os.path.join(self.data, "backups", "builder")
        self.assertTrue(os.listdir(backups))


if __name__ == "__main__":
    unittest.main()
