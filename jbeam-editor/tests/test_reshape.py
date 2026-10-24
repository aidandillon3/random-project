"""The 3D model follows moved nodes: .dae positions rewritten in place, or copied out of the game."""
import base64
import os
import sys
import unittest
from array import array

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

import fixture  # noqa: E402
from editor import dae, daeedit  # noqa: E402
from test_backend import InstallCase  # noqa: E402

BLENDER_Y_UP = b"""<?xml version="1.0" encoding="utf-8"?>
<COLLADA xmlns="http://www.collada.org/2005/11/COLLADASchema" version="1.4.1">
  <asset><unit name="centimetre" meter="0.01"/><up_axis>Y_UP</up_axis></asset>
  <library_materials><material id="m1-material" name="m1"/></library_materials>
  <library_geometries>
    <geometry id="tri-mesh" name="tri">
      <mesh>
        <source id="tri-pos"><float_array id="tri-pos-array" count="9">0 0 0 100 0 0 0 100 0</float_array>
          <technique_common><accessor source="#tri-pos-array" count="3" stride="3"/></technique_common></source>
        <source id="tri-nrm"><float_array id="tri-nrm-array" count="3">0 0 1</float_array>
          <technique_common><accessor source="#tri-nrm-array" count="1" stride="3"/></technique_common></source>
        <source id="tri-uv"><float_array id="tri-uv-array" count="6">0 0 1 0 0 1</float_array>
          <technique_common><accessor source="#tri-uv-array" count="3" stride="2"/></technique_common></source>
        <vertices id="tri-verts"><input semantic="POSITION" source="#tri-pos"/></vertices>
        <triangles material="m1" count="1">
          <input semantic="VERTEX" source="#tri-verts" offset="0"/>
          <input semantic="NORMAL" source="#tri-nrm" offset="1"/>
          <input semantic="TEXCOORD" source="#tri-uv" offset="2" set="0"/>
          <p>0 0 0 1 0 1 2 0 2</p>
        </triangles>
      </mesh>
    </geometry>
  </library_geometries>
  <library_visual_scenes><visual_scene id="Scene" name="Scene">
    <node id="tri" name="tri" type="NODE">
      <translate>10 20 30</translate><rotate>0 0 1 90</rotate>
      <instance_geometry url="#tri-mesh"><bind_material><technique_common>
        <instance_material symbol="m1" target="#m1-material"/></technique_common></bind_material></instance_geometry>
    </node>
  </visual_scene></library_visual_scenes>
</COLLADA>
"""


class DaeEditTests(unittest.TestCase):
    def test_in_place_keeps_everything_else(self):
        data = fixture.multi_dae([("a",) + tuple(fixture.box_mesh(0, 0, 1, 1, 1, 1)),
                                  ("b",) + tuple(fixture.box_mesh(2, 0, 0, 1, 1, 1))]).encode()
        before = dae.extract(data)
        moved = [v * 1.5 if k % 3 == 0 else v for k, v in enumerate(before["a"][0])]
        out = daeedit.reshape(data, "a", moved)
        after = dae.extract(out)
        self.assertLess(max(abs(x - y) for x, y in zip(after["a"][0], moved)), 1e-5)
        self.assertEqual(list(after["b"][0]), list(before["b"][0]))
        self.assertEqual(list(after["a"][1]), list(before["a"][1]))
        # only the numbers of a's arrays changed
        self.assertEqual(out[:out.index(b"a-mesh")], data[:data.index(b"a-mesh")])

    def test_node_transform_units_and_up_axis(self):
        before = dae.extract(BLENDER_Y_UP)["tri"][0]
        want = [v + 0.25 for v in before]
        out = daeedit.reshape(BLENDER_Y_UP, "tri", want)
        got = dae.extract(out)["tri"][0]
        self.assertLess(max(abs(x - y) for x, y in zip(got, want)), 1e-5)
        self.assertIn(b"0 0 1 0 0 1", out)          # UVs untouched

    def test_copy_is_in_vehicle_space(self):
        before = dae.extract(BLENDER_Y_UP)["tri"][0]
        want = [v * 2 for v in before]
        text = daeedit.copy_object(BLENDER_Y_UP, "tri", "tri_reshaped", want)
        got = dae.extract(text.encode())
        self.assertEqual(list(got), ["tri_reshaped"])
        self.assertLess(max(abs(x - y) for x, y in zip(got["tri_reshaped"][0], want)), 1e-5)
        self.assertEqual(got["tri_reshaped"][2][0][2], "m1")    # the same material name for materials.json
        # the normal turned with the node (Y up file, +90 degrees about the file's Z)
        nrm = text[text.index('id="tri-nrm-array"'):]
        nrm = [float(x) for x in nrm[nrm.index(">") + 1:nrm.index("<")].split()]
        self.assertLess(max(abs(a - b) for a, b in zip(nrm, [0, -1, 0])), 1e-6)

    def test_wrong_count_is_refused(self):
        with self.assertRaises(daeedit.ReshapeError):
            daeedit.reshape(BLENDER_Y_UP, "tri", [0.0] * 6)
        with self.assertRaises(daeedit.ReshapeError):
            daeedit.reshape(BLENDER_Y_UP, "nope", [0.0] * 9)


def b64(vals):
    return base64.b64encode(array("f", vals).tobytes()).decode()


class SaveReshapeTests(InstallCase):
    def test_game_model_copied_into_the_mod(self):
        r, mod = self.unpack_copy("testcar_body")
        cfg = self.build()["config"]
        cfg["parts"]["testcar_body"] = "testcar_body_copy"
        sc = self.build(cfg)
        fb = next(f for f in sc["flexbodies"] if f["part"] == "testcar_body_copy")
        pos, _ = self.app.meshes.mesh_arrays("testcar", "testcar_body")
        wide = [v * 1.2 if k % 3 == 0 else v for k, v in enumerate(pos)]
        out = self.app.api_save({"model": "testcar", "ops": [], "config": cfg, "meshes": [
            {"mesh": "testcar_body", "pos": b64(wide), "f": sc["files"][fb["f"]]["key"], "part": fb["part"], "row": fb["row"]}]})
        self.assertTrue(out["ok"], out)
        self.assertEqual(out["reshaped"], ["testcar_body"])
        fb2 = next(f for f in out["scene"]["flexbodies"] if f["part"] == "testcar_body_copy")
        self.assertEqual(fb2["mesh"], "testcar_body_reshaped")
        got, _ = self.app.meshes.mesh_arrays("testcar", "testcar_body_reshaped")
        self.assertLess(max(abs(a - b) for a, b in zip(got, wide)), 1e-5)
        # the game's own model is untouched
        orig, _ = self.app.meshes.mesh_arrays("testcar", "testcar_body")
        self.assertEqual(list(orig), list(pos))

    def test_mod_model_changed_in_place_with_a_backup(self):
        r, mod = self.unpack_copy("testcar_body")
        cfg = self.build()["config"]
        cfg["parts"]["testcar_body"] = "testcar_body_copy"
        sc = self.build(cfg)
        fb = next(f for f in sc["flexbodies"] if f["part"] == "testcar_body_copy")
        pos, _ = self.app.meshes.mesh_arrays("testcar", "testcar_body")
        f = {"mesh": "testcar_body", "pos": b64(list(pos)), "f": sc["files"][fb["f"]]["key"], "part": fb["part"], "row": fb["row"]}
        self.app.api_save({"model": "testcar", "ops": [], "config": cfg, "meshes": [f]})
        mine, _ = self.app.meshes.mesh_arrays("testcar", "testcar_body_reshaped")
        taller = [v + 0.1 if k % 3 == 2 else v for k, v in enumerate(mine)]
        sc = self.build(cfg)
        fb = next(f for f in sc["flexbodies"] if f["part"] == "testcar_body_copy")
        out = self.app.api_save({"model": "testcar", "ops": [], "config": cfg, "meshes": [
            {"mesh": "testcar_body_reshaped", "pos": b64(taller), "f": sc["files"][fb["f"]]["key"], "part": fb["part"], "row": fb["row"]}]})
        self.assertTrue(out["ok"], out)
        self.assertEqual(out["notes"], [])
        got, _ = self.app.meshes.mesh_arrays("testcar", "testcar_body_reshaped")
        self.assertLess(max(abs(a - b) for a, b in zip(got, taller)), 1e-5)
        backups = [os.path.join(d, x) for d, _, fs in os.walk(os.path.join(self.data, "backups")) for x in fs]
        self.assertTrue(any(p.endswith("testcar_body_reshaped.dae") for p in backups), backups)

    def test_two_objects_of_one_file_in_one_save(self):
        # a deck and its pistons live in one .dae: both reshapes must land in it
        r = self.app.api_new_mod({"model": "testcar", "name": "pair"})
        root = self.app.ws.root_by_id(r["root"])
        path = os.path.join(root.path, "vehicles", "testcar", "pair.dae")
        with open(path, "w") as f:
            f.write(fixture.multi_dae([("pair_a",) + tuple(fixture.box_mesh(0, 0, 1, 1, 1, 1)),
                                       ("pair_b",) + tuple(fixture.box_mesh(2, 0, 0, 1, 1, 1))]))
        cdae = os.path.join(root.path, "vehicles", "testcar", "pair.cdae")
        with open(cdae, "wb") as f:
            f.write(b"compiled old shape")
        self.app.ws.refresh()
        self.app.meshes.forget()
        cfg = self.build()["config"]
        a, _ = self.app.meshes.mesh_arrays("testcar", "pair_a")
        b, _ = self.app.meshes.mesh_arrays("testcar", "pair_b")
        wide_a = [v * 1.5 if k % 3 == 0 else v for k, v in enumerate(a)]
        tall_b = [v + 0.2 if k % 3 == 2 else v for k, v in enumerate(b)]
        out = self.app.api_save({"model": "testcar", "ops": [], "config": cfg, "meshes": [
            {"mesh": "pair_a", "pos": b64(wide_a)}, {"mesh": "pair_b", "pos": b64(tall_b)}]})
        self.assertTrue(out["ok"], out)
        self.assertEqual(out["reshaped"], ["pair_a", "pair_b"])
        got_a, _ = self.app.meshes.mesh_arrays("testcar", "pair_a")
        got_b, _ = self.app.meshes.mesh_arrays("testcar", "pair_b")
        self.assertLess(max(abs(x - y) for x, y in zip(got_a, wide_a)), 1e-5)
        self.assertLess(max(abs(x - y) for x, y in zip(got_b, tall_b)), 1e-5)
        # its stale compiled copy is gone (backed up), so the game builds it again from the new shape
        self.assertFalse(os.path.exists(cdae))
        self.assertEqual(len(out["notes"]), 1, out["notes"])
        backups = [x for d, _, fs in os.walk(os.path.join(self.data, "backups")) for x in fs]
        self.assertIn("pair.cdae", backups)

if __name__ == "__main__":
    unittest.main()


class ShiftModelTests(InstallCase):
    def _copy(self):
        self.unpack_copy("testcar_body")
        cfg = self.build()["config"]
        cfg["parts"]["testcar_body"] = "testcar_body_copy"
        return cfg, self.build(cfg)

    def _shift(self, cfg, sc, d, part="testcar_body_copy"):
        fb = next(f for f in sc["flexbodies"] if f["part"] == part)
        return self.app.api_save({"model": "testcar", "ops": [], "config": cfg, "meshes": [
            {"mesh": fb["mesh"], "shift": d, "f": sc["files"][fb["f"]]["key"], "part": fb["part"], "row": fb["row"]}]})

    def test_moved_model_gets_a_new_position(self):
        cfg, sc = self._copy()
        # the row has no options (and no nonFlexMaterials) yet
        out = self._shift(cfg, sc, [0, 0.5, 0])
        self.assertTrue(out["ok"], out)
        self.assertEqual(out["reshaped"], [])
        fb = next(f for f in out["scene"]["flexbodies"] if f["part"] == "testcar_body_copy")
        self.assertEqual(fb["pos"], [0, 0.5, 0])
        self.assertEqual(fb["mesh"], "testcar_body")       # the model file is left alone
        out = self._shift(cfg, out["scene"], [0.25, -0.5, 0.1])
        fb = next(f for f in out["scene"]["flexbodies"] if f["part"] == "testcar_body_copy")
        self.assertEqual(fb["pos"], [0.25, 0, 0.1])

    def test_part_that_cant_be_edited_stays(self):
        cfg = self.build()["config"]
        sc = self.build(cfg)
        tire = next(f for f in sc["flexbodies"] if f["mesh"].startswith("tire_test"))
        out = self._shift(cfg, sc, [0, 1, 0], part=tire["part"])
        self.assertTrue(out["ok"], out)
        self.assertEqual(out["written"], [])
        self.assertIn("can't edit", " ".join(out["notes"]))

    def test_wheels_name_their_groups(self):
        sc = self.build()
        fr = next(w for w in sc["wheels"] if w["name"] == "FR")
        self.assertEqual(fr["groups"], ["wheel_FR", "tire_FR"])
