"""Backend tests on a fake BeamNG install (see fixture.py).

Run from the jbeam-editor folder:  python -m unittest discover -s tests
"""
import json
import os
import shutil
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

import fixture  # noqa: E402
from editor import jbeamfile, server  # noqa: E402


class InstallCase(unittest.TestCase):
    """Each test gets a fresh install and editor app."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="jbe-test-")
        self.inst = fixture.make_install(self.tmp)
        self.data = os.path.join(self.tmp, "data")
        os.makedirs(self.data)
        with open(os.path.join(self.data, "config.json"), "w") as f:
            json.dump({"user_folder": self.inst["user"], "game_folder": self.inst["game"]}, f)
        self.app = server.App(self.data)

    def tearDown(self):
        for r in self.app.ws.roots:
            r.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def build(self, config=None, **kw):
        req = {"model": "testcar"}
        if config is None:
            req["configName"] = "base"
        else:
            req["config"] = config
        req.update(kw)
        return self.app.api_build(req)

    def node(self, scene, nid):
        for n in scene["nodes"]:
            if n["id"] == nid:
                return n
        raise KeyError(nid)

    def unpack_copy(self, part_name, mod="mytest"):
        """Make an editable copy of a game part in a new mod; returns the api result."""
        r = self.app.api_new_mod({"model": "testcar", "name": mod})
        return self.app.api_new_part({"model": "testcar", "root": r["root"], "name": part_name + "_copy",
                                      "mode": "copy", "source": part_name, "title": "Copy"}), r


class BuildTests(InstallCase):
    def test_vehicle_listed_and_built(self):
        st = self.app.api_state({})
        self.assertEqual([v["model"] for v in st["vehicles"]], ["testcar"])
        sc = self.build()
        self.assertEqual(len(sc["wheels"]), 4)
        self.assertEqual(sc["warnings"], [])
        fr = next(w for w in sc["wheels"] if w["name"] == "FR")
        self.assertAlmostEqual(fr["radius"], 0.29)
        # the common rim's axle nodes are moved to the hub by the slot's nodeOffset
        self.assertEqual(self.node(sc, "fw1r")["p"], [-0.8, -1.3, 0.3])
        self.assertEqual(self.node(sc, "fw1rr")["p"], [-0.65, -1.3, 0.3])

    def test_swap_tire_changes_radius(self):
        sc = self.build()
        cfg = sc["config"]
        cfg["parts"]["tire_F_14x6"] = "tire_F_205_75_14_test"
        sc2 = self.build(cfg)
        fr = next(w for w in sc2["wheels"] if w["name"] == "FR")
        rr = next(w for w in sc2["wheels"] if w["name"] == "RR")
        self.assertAlmostEqual(fr["radius"], 0.33)
        self.assertAlmostEqual(rr["radius"], 0.29)

    def test_tuning_variable_moves_wheels(self):
        sc = self.build({"vars": {"$trackoffset_F": 0.03}})
        self.assertAlmostEqual(self.node(sc, "fw1r")["p"][0], -0.83)
        self.assertAlmostEqual(self.node(sc, "fw1l")["p"][0], 0.83)


class SaveTests(InstallCase):
    def test_copy_part_and_move_node(self):
        res, mod = self.unpack_copy("testcar_body")
        self.assertIn("testcar_body_copy", self.app.ws.model("testcar").parts)
        sc = self.build({"parts": {"testcar_body": "testcar_body_copy"}})
        n = self.node(sc, "b1l")
        f = sc["files"][n["f"]]
        self.assertTrue(f["editable"])
        r = self.app.api_save({"model": "testcar", "ops": [
            {"op": "node.move", "f": f["key"], "part": "testcar_body_copy", "id": "b1l", "pos": [0.71, -1.8, 1.2]}]})
        self.assertTrue(r["ok"], r)
        sc = self.build({"parts": {"testcar_body": "testcar_body_copy"}})
        self.assertEqual(self.node(sc, "b1l")["p"], [0.71, -1.8, 1.2])
        # the file still parses and kept its other rows
        with open(r["written"][0], encoding="utf-8") as fh:
            data = jbeamfile.loads(fh.read())
        self.assertIn("testcar_body_copy", data)


if __name__ == "__main__":
    unittest.main()
