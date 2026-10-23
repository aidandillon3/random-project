"""Write an imported mesh as a Collada file and a material BeamNG can load."""
import json
import math
import os
import uuid
from xml.sax.saxutils import escape


def _normals(pos, idx):
    n = [0.0] * len(pos)
    for t in range(0, len(idx) - 2, 3):
        a, b, c = idx[t], idx[t + 1], idx[t + 2]
        ax, ay, az = pos[3 * a:3 * a + 3]
        bx, by, bz = pos[3 * b:3 * b + 3]
        cx, cy, cz = pos[3 * c:3 * c + 3]
        ux, uy, uz = bx - ax, by - ay, bz - az
        vx, vy, vz = cx - ax, cy - ay, cz - az
        nx, ny, nz = uy * vz - uz * vy, uz * vx - ux * vz, ux * vy - uy * vx
        for v in (a, b, c):
            n[3 * v] += nx
            n[3 * v + 1] += ny
            n[3 * v + 2] += nz
    for v in range(0, len(n), 3):
        ln = math.sqrt(n[v] ** 2 + n[v + 1] ** 2 + n[v + 2] ** 2) or 1.0
        n[v], n[v + 1], n[v + 2] = n[v] / ln, n[v + 1] / ln, n[v + 2] / ln
    return n


def _f(v):
    s = "%.6f" % v
    s = s.rstrip("0").rstrip(".")
    return "0" if s in ("-0", "") else s


def dae_text(mesh_name, material, pos, idx, uvs=None, groups=None):
    """A Z-up, metre-unit .dae with one object named mesh_name.

    groups: optional [(material, indices)] to give parts of the mesh different
    materials; without it the whole mesh (idx) uses `material`."""
    return dae_multi([(mesh_name, pos, groups or [(material, idx)], uvs)])


def _geometry(mesh_name, pos, groups, uvs):
    all_idx = [i for _, g in groups for i in g]
    nverts = len(pos) // 3
    nrm = _normals(pos, all_idx)
    if not uvs or len(uvs) != nverts * 2:
        uvs = [0.0] * (nverts * 2)
    gid = escape(mesh_name) + "-mesh"
    tris = "\n".join(
        '        <triangles material="{m}" count="{nt}">\n'
        '          <input semantic="VERTEX" source="#{gid}-vertices" offset="0"/>\n'
        '          <p>{tri}</p>\n'
        '        </triangles>'.format(m=escape(m), nt=len(g) // 3, gid=gid, tri=" ".join("%d" % i for i in g))
        for m, g in groups if len(g) >= 3)
    return """    <geometry id="{gid}" name="{name}">
      <mesh>
        <source id="{gid}-positions">
          <float_array id="{gid}-positions-array" count="{np}">{p}</float_array>
          <technique_common><accessor source="#{gid}-positions-array" count="{nv}" stride="3">
            <param name="X" type="float"/><param name="Y" type="float"/><param name="Z" type="float"/>
          </accessor></technique_common>
        </source>
        <source id="{gid}-normals">
          <float_array id="{gid}-normals-array" count="{np}">{n}</float_array>
          <technique_common><accessor source="#{gid}-normals-array" count="{nv}" stride="3">
            <param name="X" type="float"/><param name="Y" type="float"/><param name="Z" type="float"/>
          </accessor></technique_common>
        </source>
        <source id="{gid}-map-0">
          <float_array id="{gid}-map-0-array" count="{nuv}">{uv}</float_array>
          <technique_common><accessor source="#{gid}-map-0-array" count="{nv}" stride="2">
            <param name="S" type="float"/><param name="T" type="float"/>
          </accessor></technique_common>
        </source>
        <vertices id="{gid}-vertices">
          <input semantic="POSITION" source="#{gid}-positions"/>
          <input semantic="NORMAL" source="#{gid}-normals"/>
          <input semantic="TEXCOORD" source="#{gid}-map-0"/>
        </vertices>
{tris}
      </mesh>
    </geometry>""".format(gid=gid, name=escape(mesh_name), np=len(pos), nv=nverts, nuv=len(uvs),
                          p=" ".join(_f(v) for v in pos), n=" ".join(_f(v) for v in nrm),
                          uv=" ".join(_f(v) for v in uvs), tris=tris)


def dae_multi(objects):
    """A Z-up, metre-unit .dae holding several objects: [(name, pos, [(material, indices)], uvs or None)]."""
    mats = []
    for _, _, groups, _ in objects:
        for m, g in groups:
            if m not in mats and len(g) >= 3:
                mats.append(m)
    effects = "\n".join(
        '    <effect id="{m}-effect"><profile_COMMON><technique sid="common"><lambert>\n'
        '      <diffuse><color sid="diffuse">0.8 0.8 0.8 1</color></diffuse>\n'
        '    </lambert></technique></profile_COMMON></effect>'.format(m=escape(m)) for m in mats)
    materials = "\n".join('    <material id="{m}-material" name="{m}"><instance_effect url="#{m}-effect"/></material>'
                           .format(m=escape(m)) for m in mats)
    geoms = "\n".join(_geometry(name, pos, groups, uvs) for name, pos, groups, uvs in objects)
    nodes = []
    for name, _, groups, _ in objects:
        binds = "\n".join('            <instance_material symbol="{m}" target="#{m}-material"/>'.format(m=escape(m))
                          for m in dict.fromkeys(m for m, g in groups if len(g) >= 3))
        nodes.append("""      <node id="{name}" name="{name}" type="NODE">
        <matrix sid="transform">1 0 0 0 0 1 0 0 0 0 1 0 0 0 0 1</matrix>
        <instance_geometry url="#{gid}" name="{name}">
          <bind_material><technique_common>
{binds}
          </technique_common></bind_material>
        </instance_geometry>
      </node>""".format(name=escape(name), gid=escape(name) + "-mesh", binds=binds))
    return """<?xml version="1.0" encoding="utf-8"?>
<COLLADA xmlns="http://www.collada.org/2005/11/COLLADASchema" version="1.4.1">
  <asset>
    <contributor><authoring_tool>JBeam Editor</authoring_tool></contributor>
    <unit name="meter" meter="1"/>
    <up_axis>Z_UP</up_axis>
  </asset>
  <library_effects>
{effects}
  </library_effects>
  <library_materials>
{materials}
  </library_materials>
  <library_geometries>
{geoms}
  </library_geometries>
  <library_visual_scenes>
    <visual_scene id="Scene" name="Scene">
{nodes}
    </visual_scene>
  </library_visual_scenes>
  <scene><instance_visual_scene url="#Scene"/></scene>
</COLLADA>
""".format(effects=effects, materials=materials, geoms=geoms, nodes="\n".join(nodes))


def add_material(path, material, color, paint=False):
    """Text of a *.materials.json file with a simple coloured material added.

    paint=True makes the mesh take the vehicle's paint colour."""
    data = {}
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError):
            data = {}
    r, g, b = (list(color) + [0.8, 0.8, 0.8])[:3]
    data[material] = {
        "name": material,
        "mapTo": material,
        "class": "Material",
        "persistentId": (data.get(material) or {}).get("persistentId") or str(uuid.uuid4()),
        "Stages": [
            {"baseColorFactor": [r, g, b, 1], "roughnessFactor": 0.6, "metallicFactor": 0.1,
             "instanceDiffuse": bool(paint)},
            {}, {}, {},
        ],
        "materialTag0": "beamng",
        "materialTag1": "vehicle",
        "version": 1.5,
    }
    return json.dumps(data, indent=2) + "\n"
