"""Scrap Mechanic import: finding creations, reading blueprints, and turning them into vehicles that build,
stand, steer the right way and save as mods."""
import base64
import json
import math
import os
import shutil
import sys
import tempfile
import unittest
from array import array

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import ptcheck  # noqa: E402
import smfixture as F  # noqa: E402
from test_backend import InstallCase  # noqa: E402
from editor import build, jbeamfile, model, smimport  # noqa: E402
from editor.smimport import analyze, blueprint, convert, game  # noqa: E402
from editor.vehiclegen import chassis  # noqa: E402


class SMCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="jbe-sm-")
        self.game = F.make_game(self.tmp)
        self.imp = smimport.Importer()
        game.forget()

    def tearDown(self):
        game.forget()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def save(self, name, data):
        return F.save_creation(os.path.join(self.tmp, "bp", name), data, name.replace("_", " ").title())

    def analysis(self, data, name="car"):
        return self.imp.analysis(self.save(name, data), self.game)

    def convert(self, data, settings=None, name="car"):
        return self.imp.generate(self.save(name, data), self.game, settings or {})

    def scene(self, g):
        return build.build(model.Model.from_parts(None, g.P.model, g.parts), {})

    def wheel_centres(self, sc):
        out = {}
        for w in sc["wheels"]:
            a, b = sc["nodes"][w["n1"]]["p"], sc["nodes"][w["n2"]]["p"]
            out[w["name"]] = tuple(round((a[k] + b[k]) / 2, 4) for k in range(3))
        return out


class GameTests(SMCase):
    def test_shapes_from_the_game(self):
        db = game.shape_db(self.game)
        self.assertTrue(db.found_game)
        w = db.get(F.SMALL_WHEEL)
        self.assertEqual((w.role, w.title, w.collision, w.size, w.axis), ("wheel", "Small Wheel", "cylinder", (3, 3, 1), 2))
        self.assertEqual(db.get(F.BEARING).role, "bearing")
        off = db.get(F.SUSP_OFFROAD)
        self.assertEqual(off.role, "suspension")
        self.assertTrue(off.extra["offroad"])
        self.assertTrue(db.get(F.SUSP_SPORT).extra["sport"])
        self.assertEqual(db.get(F.PISTON).role, "piston")
        eng = db.get(F.ENGINE)
        self.assertEqual((eng.role, eng.level, eng.size), ("engine", 3, (3, 3, 2)))
        self.assertFalse(eng.extra["electric"])
        self.assertTrue(db.get(F.ELECTRIC_ENGINE).extra["electric"])
        self.assertEqual(db.get(F.SEAT).role, "driverseat")
        self.assertEqual(db.get(F.GLASS).role, "glass")
        self.assertEqual(db.get(F.WOOD).density, 250.0)
        # mod parts are found when asked for
        self.assertIsNone(db.get(F.MOD_WHEEL))
        self.assertEqual(db.resolve([F.MOD_WHEEL]), set())
        self.assertEqual(db.get(F.MOD_WHEEL).title, "Monster Tyre")

    def test_finding_the_game_and_creations(self):
        self.assertEqual(game.find_game_folder(self.game), self.game)
        self.assertEqual(game.find_game_folder(os.path.join(self.tmp, "nope")), game.find_game_folder(""))
        users = os.path.join(self.tmp, "smuser")
        bps = os.path.join(users, "User_1", "Blueprints")
        F.save_creation(os.path.join(bps, "a"), F.car()[0], "First")
        F.save_creation(os.path.join(bps, "b"), F.trike(), "Second")
        os.makedirs(os.path.join(bps, "empty"))
        with open(os.path.join(bps, "a", "icon.png"), "wb") as f:
            f.write(b"\x89PNG\r\n\x1a\n")
        # a workshop download next to the game
        ws = game.workshop_folder(self.game)
        self.assertTrue(ws.endswith("387990"))
        F.save_creation(os.path.join(ws, "777"), F.six_wheeler(), "Workshop Truck")
        for folder in (users, os.path.join(users, "User_1"), bps):
            items = game.list_creations(self.game, folder)
            names = [e["name"] for e in items]
            self.assertEqual(sorted(names[:2]), ["First", "Second"], folder)
            self.assertEqual(names[2:], ["Workshop Truck"])   # the player's own come first
            self.assertTrue(next(e for e in items if e["name"] == "First")["icon"].endswith("icon.png"))
        # the mod folder itself is not a creation
        self.assertFalse(any(e["name"] == "Monster wheels" for e in items))


class BlueprintTests(SMCase):
    def test_rotations_are_right_handed(self):
        for xa in (1, -1, 2, -2, 3, -3):
            for za in (1, -1, 2, -2, 3, -3):
                if abs(xa) == abs(za):
                    continue
                ex, ey, ez = blueprint.rotation(xa, za)
                det = (ex[0] * (ey[1] * ez[2] - ey[2] * ez[1]) - ex[1] * (ey[0] * ez[2] - ey[2] * ez[0])
                       + ex[2] * (ey[0] * ez[1] - ey[1] * ez[0]))
                self.assertEqual(det, 1, (xa, za))
                self.assertEqual(ex, blueprint.AXES[xa])
                self.assertEqual(ez, blueprint.AXES[za])
        # the shape's y axis follows: +x / +y gives -z (z x x)
        self.assertEqual(blueprint.rotation(1, 2)[1], (0, 0, -1))

    def test_boxes(self):
        db = game.shape_db(self.game)
        b = blueprint.Shape(0, 0, {"shapeId": F.METAL, "pos": {"x": 1, "y": 2, "z": 3},
                                   "bounds": {"x": 4, "y": 5, "z": 6}, "xaxis": 1, "zaxis": 3}, db)
        self.assertEqual((b.lo, b.hi), ((1, 2, 3), (5, 7, 9)))
        self.assertAlmostEqual(b.mass, 500 * 4 * 5 * 6 * 0.25 ** 3)
        # a wheel lying on its side: its own z (the cylinder's axis) along -x
        w = blueprint.Shape(1, 0, {"shapeId": F.SMALL_WHEEL, "pos": {"x": 0, "y": 0, "z": 4}, "xaxis": 2, "zaxis": -1}, db)
        self.assertEqual((w.lo, w.hi), ((-1, 0, 1), (0, 3, 4)))
        self.assertEqual(w.own_axis(2), (-1, 0, 0))


class AnalysisTests(SMCase):
    def test_car(self):
        data, info = F.car()
        an = self.analysis(data)
        self.assertEqual(len(an.wheels), 4)
        self.assertEqual(sorted((w.steered, w.driven) for w in an.wheels),
                         [(False, True), (False, True), (True, False), (True, False)])
        for w in an.wheels:
            self.assertEqual((w.radius, w.width, w.kind()), (1.5, 1.0, "none"))
            self.assertAlmostEqual(abs(w.axis[0]), 1.0)
        self.assertEqual(len(an.seats), 1)
        self.assertEqual(an.engine_power(), analyze.ENGINE_HP[3])
        self.assertEqual(an.chassis, {0})
        an.orient()
        self.assertEqual(an.front_guess, "steering")
        steered = [w for w in an.wheels if w.steered]
        self.assertTrue(all(w.p[1] < 0 for w in steered), "steered wheels are at the front (-y)")
        self.assertAlmostEqual(min(w.p[2] - w.r for w in an.wheels), 0.0)

    def test_suspension_carriers_move_with_the_wheels(self):
        an = self.analysis(F.car(suspension="offroad")[0])
        self.assertEqual({w.kind() for w in an.wheels}, {"offroad"})
        for w in an.wheels:
            self.assertEqual(len(w.unsprung), 1)
        self.assertEqual(len(an.unsprung_bodies), 4)
        self.assertEqual(an.chassis, {0})

    def test_without_the_game(self):
        # no shapesets: the wheels' own ids still say what they are, and unknown parts on a bearing are wheels too
        data, _ = F.car()
        for body in data["bodies"]:
            for c in body["childs"]:
                if c["shapeId"] == F.SMALL_WHEEL and c["pos"]["x"] < 0:
                    c["shapeId"] = "0badc0de-0000-4000-8000-000000000000"
        game.forget()
        an = smimport.Importer().analysis(self.save("nogame", data), "")
        self.assertEqual(len(an.wheels), 4)
        an.orient()
        ys = sorted(round(w.p[1], 3) for w in an.wheels)
        rs = {round(w.r, 3) for w in an.wheels}
        self.assertEqual(rs, {0.375})
        self.assertEqual(ys, [-1.25, -1.25, 1.25, 1.25])
        self.assertTrue(any("not installed" in n for n in an.notes))

    def test_problems_are_explained(self):
        data, _ = F.car()
        data["bodies"] = data["bodies"][:1]
        data["joints"] = []
        with self.assertRaisesRegex(ValueError, "no wheels on bearings"):
            self.analysis(data, "no_wheels")
        data, _ = F.car()
        data["bodies"][0]["childs"].append({"shapeId": "12345678-0000-0000-0000-000000000000", "pos": {"x": 2, "y": 14, "z": 3},
                                            "xaxis": 1, "zaxis": 3, "color": "ffffff"})
        an = self.analysis(data, "unknown_part")
        self.assertTrue(any("not installed" in n for n in an.notes), an.notes)


class ConvertTests(SMCase):
    def test_car_becomes_a_vehicle(self):
        g = self.convert(F.car()[0])
        sc = self.scene(g)
        self.assertEqual(sc["warnings"], [])
        self.assertEqual(g.notes, [])
        c = self.wheel_centres(sc)
        self.assertEqual(sorted(c), ["FL", "FR", "RL", "RR"])
        # 11 cells between the wheel centres, 10 between the axles, centred; wheel radius 1.5 cells
        self.assertEqual(c["FL"], (1.375, -1.25, 0.375))
        self.assertEqual(c["RR"], (-1.375, 1.25, 0.375))
        for w in sc["wheels"]:
            self.assertAlmostEqual(w["radius"], 0.375, places=4)
        ids = {n["id"] for n in sc["nodes"]}
        self.assertEqual(len(set(sc["refNodes"].values())), 6)
        for v in sc["refNodes"].values():
            self.assertIn(v, ids)
        S = g.structure
        self.assertEqual(chassis.degenerate_nodes(S), [])
        gen = convert.SMGenerator(self.imp.analysis(self.save("car", F.car()[0]), self.game), {})
        gen.run()
        self.assertEqual(chassis.stability_report(gen.S, gen.wheels), [])
        self.assertLess(abs(g.stats["mass"] - g.stats["massTarget"]) / g.stats["massTarget"], 0.08)
        for fb in sc["flexbodies"]:
            self.assertIn(fb["mesh"], g.meshes)
        for M in g.meshes.values():
            self.assertGreater(M.tri_count(), 0, M.name)
            for mat in M.t:
                self.assertIn(mat, g.materials)
        # the floor's colour is the paint
        paint = [k for k, v in g.materials.items() if v.get("paint")]
        self.assertEqual(len(paint), 1)
        self.assertEqual([round(c * 255) for c in g.materials[paint[0]]["color"]], [0x4a, 0x4a, 0x4a])

    def test_lights(self):
        from test_builder import check_lights
        check_lights(self, self.convert(F.car()[0], name="lit"))

    def test_every_way_round_is_the_same_vehicle(self):
        data = F.car()[0]
        base = self.wheel_centres(self.scene(self.convert(data, name="round0")))
        for turns in (1, 2, 3):
            c = self.wheel_centres(self.scene(self.convert(F.rotate(data, turns), name="round%d" % turns)))
            self.assertEqual(c, base, turns)
        # and turned around on request
        g = self.convert(data, {"turn": "2"}, name="around")
        c = self.wheel_centres(self.scene(g))
        self.assertEqual(c["FL"], (1.375, -1.25, 0.375))
        axles = {a["id"]: a for a in g.stats["axles"]}
        self.assertTrue(axles["R"]["steer"] and axles["F"]["driven"])

    def test_steering_and_drive(self):
        g = self.convert(F.car()[0], {"steer": "all", "drive": "all"})
        S = g.structure
        signs = {}
        for part, d in g.parts.items():
            for r in d.get("hydros", [])[1:]:
                if not isinstance(r, list):
                    continue
                a, b, opts = r
                inner, arm = (a, b) if "h3" in b else (b, a)
                p_in, p_arm = S.pos(inner), S.pos(arm)
                self.assertGreater(p_arm[1], S.pos(arm.replace("h3", "h1"))[1], "steering arm behind the kingpin")
                grow = chassis.dist(p_in, (p_arm[0] + 0.01, p_arm[1], p_arm[2])) - chassis.dist(p_in, p_arm)
                # positive input: the arm behind the kingpin goes to +x (the wheel's front turns right)
                signs[arm[0]] = signs.get(arm[0], set()) | {math.copysign(1, grow) == math.copysign(1, opts["factor"])}
        self.assertEqual(signs, {"f": {True}, "r": {False}}, "front turns with the input, rear against it")
        pt = [r for d in g.parts.values() for r in d.get("powertrain", [])[1:]]
        names = {r[1]: r for r in pt}
        self.assertEqual(names["transfercase"][2], "gearbox")
        self.assertEqual({tuple(names["driveshaft_F"][2:4]), tuple(names["driveshaft_R"][2:4])},
                         {("transfercase", 1), ("transfercase", 2)})
        tr = g.parts[g.P.model + "_transmission"]
        gb = tr["gearbox"]["gearRatios"]
        self.assertEqual(gb[1], 0)
        self.assertTrue(all("$finaldrive" in str(x) for x in gb if x != 0))
        self.assertIn("$finaldrive", [v[0] for v in tr["variables"][1:]])

    def test_electric_engines_make_an_electric_vehicle(self):
        data = F.car(engine=F.ELECTRIC_ENGINE)[0]
        an = self.analysis(data, "volt")
        self.assertEqual(convert.clean({}, an)[1]["engineType"], "electric")
        self.assertEqual(convert.clean({}, self.analysis(F.car()[0], "petrol"))[1]["engineType"], "gas")
        for settings in ({}, {"drive": "all"}, {"drive": "front", "diff": "locked"}):
            with self.subTest(settings=settings):
                g = self.convert(data, settings, "volt")
                sc = self.scene(g)
                self.assertEqual(sc["warnings"], [])
                names = [w["name"] for w in sc["wheels"]]
                self.assertEqual(ptcheck.problems(g.parts, names, names if settings.get("drive") == "all" else None), [])
                m = g.P.model
                self.assertIn(m + "_battery", g.parts)
                self.assertNotIn(m + "_fueltank", g.parts)
                # the motor turns the wheels through one reduction gear, with no gearbox and no exhaust
                pt = {r[1]: r for r in g.parts[m + "_transmission"]["powertrain"][1:]}
                self.assertEqual(pt["reduction"][2], "mainMotor")
                self.assertEqual(pt["reduction"][4]["gearRatio"], "$finaldrive")
                self.assertFalse(any("exhaust" in name for name in g.meshes))
                self.assertFalse(any(n.extra.get("isExhaust") for n in g.structure.nodes.values() if n.extra))
                st = g.stats
                self.assertTrue(st["electric"])
                self.assertGreater(st["range"], 50)
                self.assertTrue(st["zeroTo100"] and st["topSpeed"] > 100)
        # and back to an engine when asked
        g = self.convert(data, {"engineType": "gas"}, "volt")
        self.assertIn(g.P.model + "_fueltank", g.parts)
        self.assertEqual(ptcheck.problems(g.parts, [w["name"] for w in self.scene(g)["wheels"]]), [])

    def test_odd_layouts(self):
        g = self.convert(F.trike(), name="trike")
        sc = self.scene(g)
        self.assertEqual(sc["warnings"], [])
        self.assertEqual(sorted(self.wheel_centres(sc)), ["FR", "RL", "RR"])
        axles = {a["id"]: a for a in g.stats["axles"]}
        self.assertEqual((axles["F"]["wheels"], axles["F"]["steer"], axles["R"]["driven"]), (1, True, True))
        self.assertEqual(chassis.degenerate_nodes(g.structure), [])
        g = self.convert(F.six_wheeler(), name="six")
        sc = self.scene(g)
        self.assertEqual(sc["warnings"], [])
        self.assertEqual(sorted(self.wheel_centres(sc)), ["FL", "FR", "M1L", "M1R", "RL", "RR"])
        pt = [r for d in g.parts.values() for r in d.get("powertrain", [])[1:]]
        self.assertEqual(sum(1 for r in pt if r[0] == "differential" and r[1].startswith("transfercase")), 2)
        self.assertEqual(sum(1 for r in pt if r[0] == "shaft" and r[1].startswith("wheelaxle")), 6)

    def test_settings(self):
        data = F.car(suspension="sport")[0]
        an = self.analysis(data, "sporty")
        st, d = convert.clean({"scale": 9, "tireType": "spaceship", "power": "250", "steerAngle": -4}, an)
        self.assertEqual((st["scale"], st["tireType"], st["power"], st["steerAngle"]), (2.5, d["tireType"], 250, 15))
        self.assertEqual((d["travel"], d["springHz"]), convert.SUSPENSION["sport"][:2])
        # the weight follows the size until it is set by hand
        m1 = convert.clean({}, an)[0]["mass"]
        m2 = convert.clean({"scale": 2}, an)[0]["mass"]
        self.assertAlmostEqual(m2 / m1, 8, delta=0.05)
        self.assertEqual(convert.clean({"scale": 2, "mass": 1234, "massAuto": False}, an)[0]["mass"], 1234)
        g = self.convert(data, {"scale": 2, "mass": 3000, "massAuto": False, "power": 500, "gearbox": "manual"}, "sporty")
        self.assertLess(abs(g.stats["mass"] - 3000) / 3000, 0.1)
        self.assertEqual(g.stats["powerHP"], 500)
        self.assertIn("clutch", g.parts[g.P.model + "_transmission"])
        sc = self.scene(g)
        self.assertEqual(sc["warnings"], [])
        self.assertAlmostEqual(self.wheel_centres(sc)["FL"][2], 0.75)

    def test_shared_beams_become_solid_axles(self):
        g = self.convert(F.solid_axles(1), name="solid")
        sc = self.scene(g)
        self.assertEqual(sc["warnings"], [])
        m = g.P.model
        for ax in ("F", "R"):
            self.assertIn("Solid Axle", g.parts["%s_suspension_%s" % (m, ax)]["information"]["name"])
        S = g.structure
        c = self.wheel_centres(sc)
        for ax, p in (("F", "f"), ("R", "r")):
            mid = (c[ax + "L"][0] + c[ax + "R"][0]) / 2
            self.assertAlmostEqual(S.pos(p + "a3")[0], mid, places=6)       # the housing's middle is between its wheels
        self.assertNotAlmostEqual(c["RL"][0] + c["RR"][0], 0.0, places=3)    # the rear axle really is off-centre
        self.assertEqual(chassis.degenerate_nodes(S), [])
        # the axle beam's blocks ride on the axle
        flex = [r for r in g.parts[m + "_suspension_R"]["flexbodies"][1:]]
        self.assertTrue(any("carrier" in r[0] and r[1] == [m + "_axle_R"] for r in flex), flex)
        # steering on the solid front axle still turns right for positive input
        for r in g.parts[m + "_suspension_F"]["hydros"][1:]:
            if not isinstance(r, list):
                continue
            a, b, opts = r
            inner, arm = (a, b) if "h3" in b else (b, a)
            p_in, p_arm = S.pos(inner), S.pos(arm)
            grow = chassis.dist(p_in, (p_arm[0] + 0.01, p_arm[1], p_arm[2])) - chassis.dist(p_in, p_arm)
            self.assertEqual(math.copysign(1, grow), math.copysign(1, opts["factor"]))
        g = self.convert(F.solid_axles(), {"axles": "independent"}, name="solid_ind")
        self.assertIn("Double Wishbone", g.parts[g.P.model + "_suspension_F"]["information"]["name"])

    def test_spare_wheels_stay_on_the_body(self):
        g = self.convert(F.car(spare=True)[0], name="spare")
        sc = self.scene(g)
        self.assertEqual(sc["warnings"], [])
        self.assertEqual(sorted(self.wheel_centres(sc)), ["FL", "FR", "RL", "RR"])
        self.assertEqual(self.wheel_centres(sc)["FL"], (1.375, -1.25, 0.375))
        self.assertTrue(any("spare" in n for n in g.notes), g.notes)

    def test_mod_wheels_and_pistons(self):
        g = self.convert(F.car(wheel=F.MOD_WHEEL, piston=True)[0], name="monster")
        sc = self.scene(g)
        self.assertEqual(sc["warnings"], [])
        for w in sc["wheels"]:
            self.assertAlmostEqual(w["radius"], 0.75, places=4)
        self.assertTrue(any("Pistons" in n for n in g.notes))

    def test_collision_faces_point_out(self):
        g = self.convert(F.car()[0])
        S = g.structure
        body = [n for n in S.order if S.nodes[n].part == "body"]
        mid = tuple(sum(S.pos(n)[k] for n in body) / len(body) for k in range(3))
        good = total = 0
        for a, b, c, part, kind in S.tris:
            pa, pb, pc = S.pos(a), S.pos(b), S.pos(c)
            nrm = chassis.v_cross(chassis.v_sub(pb, pa), chassis.v_sub(pc, pa))
            cen = tuple((pa[k] + pb[k] + pc[k]) / 3 for k in range(3))
            total += 1
            good += chassis.v_dot(nrm, chassis.v_sub(cen, mid)) > 0
        self.assertGreater(good / total, 0.85)

    def test_text_round_trip(self):
        g = self.convert(F.car(suspension="offroad")[0], name="trip")
        text = jbeamfile.dumps_part_file(g.parts)
        parsed = jbeamfile.loads(text)
        sc = build.build(model.Model.from_parts(None, g.P.model, parsed), {})
        self.assertEqual(sc["warnings"], [])
        self.assertEqual(len(sc["nodes"]), len(self.scene(g)["nodes"]))


class ScrapApiTests(InstallCase):
    def setUp(self):
        super().setUp()
        game.forget()
        self.app.cfg["sm_folder"] = self.inst["sm"]
        self.app.cfg["sm_user_folder"] = self.inst["smuser"]

    def tearDown(self):
        game.forget()
        super().tearDown()

    def creation(self, name):
        r = self.app.api_scrap_list({})
        self.assertTrue(r["game"]["found"])
        return next(c for c in r["creations"] if c["name"] == name)

    def test_list_icon_and_preview(self):
        c = self.creation("Mud Truck")
        self.assertTrue(c["icon"])
        data, ctype = self.app.scrap_icon(c["id"])
        self.assertEqual(ctype, "image/png")
        self.assertTrue(data.startswith(b"\x89PNG"))
        r = self.app.api_scrap_preview({"id": c["id"], "settings": {}})
        self.assertEqual(r["scene"]["warnings"], [])
        self.assertEqual(r["model"], "mud_truck")
        self.assertEqual(r["summary"]["wheels"], 4)
        self.assertEqual(r["summary"]["suspension"], {"offroad": 4})
        self.assertIn("fields", r["schema"])
        names = {fb["mesh"] for fb in r["scene"]["flexbodies"]}
        self.assertTrue(all("@" in n for n in names))
        self.assertEqual(names, set(r["meshes"]))
        one = r["meshes"][sorted(names)[0]]
        idx = array("I")
        idx.frombytes(base64.b64decode(one["idx"]))
        self.assertEqual(sum(gr[1] for gr in one["groups"]), len(idx))
        r2 = self.app.api_scrap_preview({"id": c["id"], "settings": {"power": 300}, "have": sorted(names)})
        self.assertEqual(r2["meshes"], {})          # power is not geometry
        self.assertEqual(r2["stats"]["powerHP"], 300)

    def test_create_open_reimport(self):
        c = self.creation("Rusty Hatch")
        res = self.app.api_scrap_create({"id": c["id"], "settings": {"name": "Hatch Back", "drive": "all"}})
        self.assertEqual(res["model"], "hatch_back")
        self.assertFalse(res["updated"])
        self.assertTrue(os.path.isfile(os.path.join(res["path"], smimport.DESIGN_FILE)))
        v = [x for x in self.app.api_state({})["vehicles"] if x["model"] == "hatch_back"][0]
        self.assertTrue(v["scrap"] and v["editable"] and not v["builder"])
        sc = self.app.api_build({"model": "hatch_back", "configName": "base"})
        self.assertEqual(sc["warnings"], [])
        self.assertEqual(len(sc["wheels"]), 4)
        with open(os.path.join(res["path"], "info.json"), encoding="utf-8") as f:
            info = json.load(f)
        self.assertEqual(info["Body Style"], "Scrap Mechanic creation")
        # reopened: same creation, same settings; creating again updates it (with a backup)
        ld = self.app.api_scrap_load({"model": "hatch_back"})
        self.assertTrue(ld["found"])
        self.assertEqual(ld["settings"]["drive"], "all")
        res2 = self.app.api_scrap_create({"id": ld["source"]["id"], "settings": dict(ld["settings"], power=222)})
        self.assertTrue(res2["updated"])
        self.assertTrue(os.listdir(os.path.join(self.data, "backups", "scrap")))
        # a vehicle the importer did not make is never overwritten
        with self.assertRaisesRegex(ValueError, "already a vehicle"):
            self.app.api_scrap_create({"id": c["id"], "settings": {"name": "Testcar"}})
        with self.assertRaisesRegex(ValueError, "not imported from Scrap Mechanic"):
            self.app.api_scrap_load({"model": "testcar"})

    def test_created_vehicle_shows_its_colours(self):
        c = self.creation("Rusty Hatch")
        res = self.app.api_scrap_create({"id": c["id"], "settings": {"name": "Colour Check"}})
        sc = self.app.api_build({"model": res["model"], "configName": "base"})
        names = sorted({fb["mesh"] for fb in sc["flexbodies"]})
        blob = self.app.api_meshes({"model": res["model"], "names": names})
        n = int.from_bytes(blob[:4], "little")
        head = json.loads(blob[4:4 + n].decode("utf-8"))
        self.assertEqual(head["missing"], [])
        body = head["meshes"]["colour_check_body"]
        mats = {g[2] for g in body[5]}
        self.assertGreaterEqual(len(mats), 3)            # floor, cabin and glass at least
        for m in mats:
            self.assertIn(m, head["materials"])
        info = [head["materials"][m] for m in mats]
        self.assertEqual(sum(1 for i in info if i.get("paint")), 1)
        self.assertEqual(sum(1 for i in info if i.get("glass")), 1)
        reds = [i for i in info if [round(x * 255) for x in i["color"]] == [0xc2, 0x3b, 0x22]]
        self.assertEqual(len(reds), 1)                    # the wooden cabin's colour
        # the groups cover every triangle once
        self.assertEqual(sum(g[1] for g in body[5]), body[3])

    def test_imports_pass_the_checker(self):
        for c in self.app.api_scrap_list({})["creations"]:
            for st in ({}, {"drive": "all", "steer": "all", "diff": "lsd", "gearbox": "manual", "axles": "independent"}):
                res = self.app.api_scrap_create({"id": c["id"], "settings": dict(st, name="Chk %s %d" % (c["name"], len(st)))})
                out = self.app.api_check({"model": res["model"]})
                with self.subTest(creation=c["name"], settings=st):
                    self.assertEqual(out["errors"], [])
                    self.assertEqual(out["warnings"], [])

    def test_blueprint_from_a_path(self):
        folder = os.path.join(self.tmp, "elsewhere", "thing")
        F.save_creation(folder, F.trike(), "Odd Trike")
        e = self.app.api_scrap_entry({"path": folder})
        self.assertEqual(e["name"], "Odd Trike")
        r = self.app.api_scrap_preview({"id": e["id"]})
        self.assertEqual(len(r["scene"]["wheels"]), 3)
        with self.assertRaisesRegex(ValueError, "not a Scrap Mechanic creation"):
            self.app.api_scrap_entry({"path": os.path.join(self.tmp, "elsewhere")})


if __name__ == "__main__":
    unittest.main()
