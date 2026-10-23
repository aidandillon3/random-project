"""Garage: lift kits, spacers, custom tires, engine tune, body lift, saving and re-editing."""
import json
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from test_backend import InstallCase  # noqa: E402
from editor import jbeamfile  # noqa: E402


class GarageTests(InstallCase):
    def preview(self, spec, config=None):
        return self.build(config, garage=spec)

    def test_info(self):
        sc = self.preview({})
        info = sc["garage"]["info"]
        ax = {a["id"]: a for a in info["axles"]}
        self.assertEqual(set(ax), {"F", "R"})
        self.assertEqual([s["path"] for s in ax["F"]["slots"]], ["/testcar_suspension_F/"])
        self.assertEqual(ax["F"]["rim"]["id"], "wheel_F_4")
        self.assertEqual(ax["F"]["tire"]["id"], "tire_F_14x6")
        sizes = {o["name"]: round(o["diameter"], 3) for o in ax["F"]["tire"]["options"]}
        self.assertEqual(sizes, {"tire_F_185_60_14_test": 0.58, "tire_F_205_75_14_test": 0.66})
        self.assertAlmostEqual(ax["F"]["track"], 1.45)
        self.assertEqual(info["engine"]["source"], "testcar_engine_i4")
        self.assertAlmostEqual(info["engine"]["peakTorque"], 165)
        self.assertIn("rideHeight", info["vars"])
        self.assertEqual([b["id"] for b in info["body"]], ["testcar_body"])
        self.assertEqual(sc["garage"]["changes"], [])

    def test_lift_moves_whole_suspension(self):
        base = self.build()
        sc = self.preview({"axles": {"F": {"lift": 0.1}}})
        for nid in ("fh1r", "fs1l", "fw1r", "fw1ll"):
            self.assertAlmostEqual(self.node(sc, nid)["p"][2], self.node(base, nid)["p"][2] - 0.1, places=6, msg=nid)
        for nid in ("f1r", "rh1r", "rw1r", "b1l", "e1r"):
            self.assertEqual(self.node(sc, nid)["p"], self.node(base, nid)["p"], nid)
        self.assertEqual(len(sc["wheels"]), 4)
        self.assertEqual(sc["warnings"], [])
        name = "testcar_suspension_F__garage_preview"
        self.assertIn(name, sc["parts"])
        self.assertTrue(sc["parts"][name]["virtual"])
        self.assertIn("lift", sc["parts"][name]["title"])
        # the config the page keeps is still the original one
        self.assertEqual(sc["config"]["parts"]["testcar_suspension_F"], "testcar_suspension_F")
        self.assertEqual(sc["garage"]["finalConfig"]["parts"]["testcar_suspension_F"], name)
        # meshes of the suspension and the wheels move with it
        fb = [f for f in sc["flexbodies"] if f["mesh"] == "steelwheel_test_14x6" and f["part"] == "steelwheel_test_14x6_F"]
        self.assertTrue(fb and all(abs(f["xf"]["t"][2] - 0.2) < 1e-6 for f in fb), fb)

    def test_spacers(self):
        sc = self.preview({"axles": {"F": {"spacer": 0.03}}})
        self.assertAlmostEqual(self.node(sc, "fw1r")["p"][0], -0.83)
        self.assertAlmostEqual(self.node(sc, "fw1l")["p"][0], 0.83)
        self.assertAlmostEqual(self.node(sc, "fh1r")["p"][0], -0.62)
        self.assertAlmostEqual(self.node(sc, "rw1r")["p"][0], -0.8)

    def test_lift_and_spacers_share_one_copy(self):
        sc = self.preview({"axles": {"F": {"lift": 0.05, "spacer": 0.02}}})
        made = [p for p, v in sc["parts"].items() if v.get("virtual")]
        self.assertEqual(made, ["testcar_suspension_F__garage_preview"])
        self.assertAlmostEqual(self.node(sc, "fw1r")["p"][0], -0.82)
        self.assertAlmostEqual(self.node(sc, "fw1r")["p"][2], 0.25)

    def test_custom_tire(self):
        sc = self.preview({"axles": {"R": {"customTire": {"diameter": 0.72, "width": 0.25}}}})
        rr = next(w for w in sc["wheels"] if w["name"] == "RR")
        fr = next(w for w in sc["wheels"] if w["name"] == "FR")
        self.assertAlmostEqual(rr["radius"], 0.36)
        self.assertAlmostEqual(rr["width"], 0.25)
        self.assertAlmostEqual(fr["radius"], 0.29)
        fb = [f for f in sc["flexbodies"] if f["mesh"] == "tire_test_185_60_14_test" and f["part"].endswith("garage_preview")]
        self.assertEqual(len(fb), 2)
        self.assertAlmostEqual(fb[0]["scale"][1], 0.36 / 0.29, places=3)
        self.assertAlmostEqual(fb[0]["scale"][0], 0.25 / 0.185, places=3)
        # the bigger tire is heavier in proportion (16 rays, rim nodes 0.4 kg, tire nodes 0.12 kg in the fixture)
        stock = self.preview({})
        rr0 = next(w for w in stock["wheels"] if w["name"] == "RR")
        tire = lambda w: w["mass"] / 32 - 0.4  # noqa: E731
        self.assertAlmostEqual(tire(rr0), 0.12, places=4)
        self.assertAlmostEqual(tire(rr) / tire(rr0), (0.36 / 0.29) * (0.25 / 0.185), delta=0.01)

    def test_engine_tune(self):
        sc = self.preview({"engine": {"power": 1.5, "rpm": 500}})
        e = sc["engine"]
        self.assertAlmostEqual(max(t for _, t in e["torque"]), 247.5)
        self.assertEqual(e["maxRPM"], 7300)
        self.assertGreaterEqual(max(r for r, _ in e["torque"]), 7300)
        info = sc["garage"]["info"]["engine"]
        self.assertAlmostEqual(info["peakTorque"], 165)   # the original curve, for the before/after

    def test_body_lift(self):
        base = self.build()
        sc = self.preview({"bodyLift": 0.05})
        self.assertAlmostEqual(self.node(sc, "b1l")["p"][2], self.node(base, "b1l")["p"][2] + 0.05)
        self.assertEqual(self.node(sc, "f1l")["p"], self.node(base, "f1l")["p"])

    def test_save_and_edit_again(self):
        spec = {"axles": {"F": {"lift": 0.1}, "R": {"lift": 0.1, "spacer": 0.02}},
                "engine": {"power": 1.25}, "paints": [{"baseColor": [0.8, 0.1, 0.1, 1], "metallic": 0.3,
                                                       "roughness": 0.4, "clearcoat": 1, "clearcoatRoughness": 0.05}]}
        r = self.app.api_garage_save({"model": "testcar", "newMod": "my_test_versions", "configName": "Trail Rig",
                                      "title": "Trail Rig", "garage": spec, "config": self.build()["config"],
                                      "picture": "data:image/jpeg;base64,/9j/4AAQ"})
        self.assertEqual(r["configName"], "Trail Rig")
        self.assertTrue(os.path.isfile(r["pc"]))
        self.assertTrue(os.path.isfile(r["jbeam"]))
        with open(r["jbeam"], encoding="utf-8") as f:
            parts = jbeamfile.loads(f.read())
        self.assertEqual(set(parts), {"testcar_suspension_F__trail_rig", "testcar_suspension_R__trail_rig",
                                      "testcar_engine_i4__trail_rig"})
        with open(r["pc"], encoding="utf-8") as f:
            pc = json.load(f)
        self.assertEqual(pc["parts"]["testcar_suspension_F"], "testcar_suspension_F__trail_rig")
        self.assertEqual(pc["paints"][0]["baseColor"], [0.8, 0.1, 0.1, 1])
        vdir = os.path.dirname(r["pc"])
        self.assertTrue(os.path.isfile(os.path.join(vdir, "info_Trail Rig.json")))
        self.assertTrue(os.path.isfile(os.path.join(vdir, "Trail Rig.jpg")))
        self.assertIn("Trail Rig", [c["name"] for c in r["summary"]["configs"]])
        # open the saved version: it is lifted, and the Garage reads its settings back
        base = self.build()
        saved = self.app.api_build({"model": "testcar", "configName": "Trail Rig"})
        self.assertAlmostEqual(self.node(saved, "fh1r")["p"][2], self.node(base, "fh1r")["p"][2] - 0.1)
        self.assertAlmostEqual(self.node(saved, "rw1r")["p"][0], -0.82)
        sc = self.app.api_build({"model": "testcar", "configName": "Trail Rig", "garage": {}})
        ax = {a["id"]: a for a in sc["garage"]["info"]["axles"]}
        self.assertAlmostEqual(ax["F"]["lift"], 0.1)
        self.assertAlmostEqual(ax["R"]["spacer"], 0.02)
        self.assertEqual(ax["F"]["slots"][0]["source"], "testcar_suspension_F")
        self.assertAlmostEqual(sc["garage"]["info"]["engine"]["power"], 1.25)
        self.assertAlmostEqual(sc["garage"]["info"]["engine"]["peakTorque"], 165)
        # changing the lift starts from the original part (no lift on top of a lift)
        sc2 = self.app.api_build({"model": "testcar", "config": sc["config"], "garage": {"axles": {"F": {"lift": 0.05}}}})
        self.assertAlmostEqual(self.node(sc2, "fh1r")["p"][2], self.node(base, "fh1r")["p"][2] - 0.05)
        # and taking everything back off gives the original parts again
        sc3 = self.app.api_build({"model": "testcar", "config": sc["config"], "garage": {}})
        self.assertEqual(sc3["garage"]["finalConfig"]["parts"]["testcar_suspension_F"], "testcar_suspension_F__trail_rig")
        sc4 = self.app.api_build({"model": "testcar", "config": sc["config"], "garage": {"axles": {}, "reset": True}})
        self.assertEqual(sc4["garage"]["finalConfig"]["parts"]["testcar_suspension_F"], "testcar_suspension_F")


if __name__ == "__main__":
    unittest.main()
