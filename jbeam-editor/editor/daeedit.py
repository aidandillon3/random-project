"""Change where the vertices of one object in a Collada (.dae) file are.

Used when nodes are moved and the 3D model should follow them (the game binds
a flexbody's vertices to its nodes where both are in the files, so moving the
nodes alone leaves the model behind).

reshape():     rewrite the numbers of the object's position array in place, so
               the rest of the file (UVs, materials, other objects, layout) stays.
copy_object(): for a model in a file that can't be changed (the game's, a zip),
               a small new .dae with just that object under a new name.

Positions come in the space dae.extract() gives them: placed by the node
transforms, Z up, in metres. They are turned back into the object's own space.
"""
import io
import math
import re
import xml.etree.ElementTree as ET
from xml.sax.saxutils import quoteattr

from .dae import _IDENT, _mat_mul, _node_matrix, _tag
from .daewrite import _f, _normals


class ReshapeError(Exception):
    pass


def _sources(geom):
    """{source id: (float_array id, stride)}, {vertices id: {semantic: source id}}, [(prim inputs)]"""
    mesh = next((c for c in geom if _tag(c) == "mesh"), None)
    if mesh is None:
        return None
    sources, verts, prims = {}, {}, []
    for ch in mesh:
        t = _tag(ch)
        if t == "source":
            fa, stride = None, 3
            for sub in ch:
                if _tag(sub) == "float_array":
                    fa = sub.get("id")
                elif _tag(sub) == "technique_common":
                    for acc in sub:
                        if _tag(acc) == "accessor":
                            stride = int(acc.get("stride", "3"))
            sources[ch.get("id")] = (fa, stride)
        elif t == "vertices":
            verts[ch.get("id")] = {i.get("semantic"): i.get("source", "").lstrip("#") for i in ch if _tag(i) == "input"}
        elif t in ("triangles", "polylist", "polygons"):
            prims.append([(i.get("semantic"), i.get("source", "").lstrip("#")) for i in ch if _tag(i) == "input"])
    return {"sources": sources, "verts": verts, "prims": prims}


def _scan(data):
    geoms, mat_names, scenes = {}, {}, []
    up, unit = "Z_UP", 1.0
    for _, el in ET.iterparse(io.BytesIO(data), events=("end",)):
        t = _tag(el)
        if t == "geometry":
            geoms[el.get("id")] = _sources(el)
            el.clear()
        elif t == "material" and el.get("id"):
            mat_names[el.get("id")] = el.get("name") or re.sub(r"-material$", "", el.get("id"))
        elif t == "up_axis":
            up = (el.text or "Z_UP").strip()
        elif t == "unit":
            try:
                unit = float(el.get("meter", "1"))
            except ValueError:
                unit = 1.0
        elif t == "visual_scene":
            scenes.append(el)
        elif t in ("library_images", "library_effects", "library_controllers", "library_animations"):
            el.clear()
    instances = []      # (node name, matrix, geometry id, {symbol: material id})

    def walk(node, parent):
        m = _mat_mul(parent, _node_matrix(node))
        name = node.get("name") or node.get("id") or ""
        for ch in node:
            t = _tag(ch)
            if t == "instance_geometry":
                binds = {e.get("symbol"): (e.get("target") or "").lstrip("#")
                         for e in ch.iter() if _tag(e) == "instance_material" and e.get("symbol")}
                instances.append((name, m, ch.get("url", "").lstrip("#"), binds))
            elif t == "node":
                walk(ch, m)

    for sc in scenes:
        for ch in sc:
            if _tag(ch) == "node":
                walk(ch, _IDENT)
    return {"geoms": geoms, "mat_names": mat_names, "up": up, "unit": unit, "instances": instances}


def _locate(info, name):
    mine = [x for x in info["instances"] if x[0] == name]
    if not mine:
        if not info["instances"] and name + "-mesh" in info["geoms"]:
            mine = [(name, list(_IDENT), name + "-mesh", {})]
        else:
            raise ReshapeError("the model %s is not in its file" % name)
    if len(mine) > 1:
        raise ReshapeError("the model %s is made of several pieces" % name)
    _, mat, gid, binds = mine[0]
    if sum(1 for x in info["instances"] if x[2] == gid) > 1:
        raise ReshapeError("the shape of %s is shared with other models in its file" % name)
    g = info["geoms"].get(gid)
    if not g:
        raise ReshapeError("the model %s has no mesh" % name)
    pos_src = None
    for inputs in g["prims"]:
        for sem, src in inputs:
            if sem == "VERTEX":
                pos_src = g["verts"].get(src, {}).get("POSITION", pos_src)
    if pos_src not in g["sources"]:
        raise ReshapeError("the model %s has no positions" % name)
    fa, stride = g["sources"][pos_src]
    if stride != 3 or not fa:
        raise ReshapeError("the model %s stores its positions in an unusual way" % name)
    return {"gid": gid, "matrix": mat, "binds": binds, "pos_array": fa, "geom": g}


def _inverse(m):
    """Inverse of an affine 4x4 (row-major list)."""
    a, b, c, d, e, f, g, h, i = m[0], m[1], m[2], m[4], m[5], m[6], m[8], m[9], m[10]
    det = a * (e * i - f * h) - b * (d * i - f * g) + c * (d * h - e * g)
    if abs(det) < 1e-12:
        raise ReshapeError("the model is scaled flat in its file")
    r = [(e * i - f * h) / det, (c * h - b * i) / det, (b * f - c * e) / det,
         (f * g - d * i) / det, (a * i - c * g) / det, (c * d - a * f) / det,
         (d * h - e * g) / det, (b * g - a * h) / det, (a * e - b * d) / det]
    t = [m[3], m[7], m[11]]
    tr = [-(r[0] * t[0] + r[1] * t[1] + r[2] * t[2]),
          -(r[3] * t[0] + r[4] * t[1] + r[5] * t[2]),
          -(r[6] * t[0] + r[7] * t[1] + r[8] * t[2])]
    return [r[0], r[1], r[2], tr[0], r[3], r[4], r[5], tr[1], r[6], r[7], r[8], tr[2], 0, 0, 0, 1], det


def _to_local(info, loc, pos):
    """Positions as extract() gives them -> the numbers in the object's own position array."""
    inv, _ = _inverse(loc["matrix"])
    up, unit = info["up"], info["unit"] or 1.0
    out = []
    for k in range(0, len(pos), 3):
        X, Y, Z = pos[k] / unit, pos[k + 1] / unit, pos[k + 2] / unit
        if up == "Y_UP":
            x, y, z = X, Z, -Y
        elif up == "X_UP":
            x, y, z = Z, X, Y
        else:
            x, y, z = X, Y, Z
        out += [inv[0] * x + inv[1] * y + inv[2] * z + inv[3],
                inv[4] * x + inv[5] * y + inv[6] * z + inv[7],
                inv[8] * x + inv[9] * y + inv[10] * z + inv[11]]
    return out


def _array_span(text, array_id):
    """(start, end) of the numbers inside <float_array id=array_id>...</float_array>."""
    m = re.search(rb'<float_array\b[^>]*\bid\s*=\s*["\']' + re.escape(array_id.encode("utf-8")) + rb'["\'][^>]*>',
                  text)
    if not m:
        raise ReshapeError("can't find the positions of the model in its file")
    end = text.find(b"</float_array>", m.end())
    if end < 0:
        raise ReshapeError("the model's file is damaged")
    return m.end(), end


def _numbers(vals):
    return " ".join(_f(v) for v in vals).encode("ascii")


def _count(text, s, e):
    return len(text[s:e].split())


def _triangles(text, loc):
    """Triangle indices of the object, read from the file (for new normals)."""
    gm = re.search(rb'<geometry\b[^>]*\bid\s*=\s*["\']' + re.escape(loc["gid"].encode("utf-8")) + rb'["\'][^>]*>', text)
    end = text.find(b"</geometry>", gm.end())
    geom = ET.fromstring(b'<g xmlns="http://www.collada.org/2005/11/COLLADASchema">' + text[gm.start():end + 11] + b"</g>")
    from .dae import _read_geometry
    got = _read_geometry(geom[0])
    return list(got[1]) if got else []


def reshape(data, name, pos):
    """The .dae bytes with object `name` given new vertex positions (same count and order as extract())."""
    info = _scan(data)
    loc = _locate(info, name)
    s, e = _array_span(data, loc["pos_array"])
    if _count(data, s, e) != len(pos):
        raise ReshapeError("the model %s changed in its file since it was loaded; open the vehicle again" % name)
    local = _to_local(info, loc, pos)
    return _vertex_normals(data[:s] + _numbers(local) + data[e:], loc, local)


def _vertex_normals(text, loc, pos):
    """Normals stored one per position (like the editor's own meshes) are worked out again for the new shape."""
    g = loc["geom"]
    vid = next((src for inputs in g["prims"] for sem, src in inputs if sem == "VERTEX"), None)
    nsrc = g["verts"].get(vid, {}).get("NORMAL")
    if not (nsrc and nsrc in g["sources"] and g["sources"][nsrc][1] == 3 and g["sources"][nsrc][0]):
        return text
    tris = _triangles(text, loc)
    ns, ne = _array_span(text, g["sources"][nsrc][0])
    if not tris or _count(text, ns, ne) != len(pos):
        return text
    return text[:ns] + _numbers(_normals(pos, tris)) + text[ne:]


def copy_object(data, name, new_name, pos):
    """A new .dae (text) holding just object `name` of `data`, called new_name, with the given positions.
    It is written in vehicle space (no node transform), Z up, in metres."""
    info = _scan(data)
    loc = _locate(info, name)
    m = loc["matrix"]
    _, det = _inverse(m)
    if det < 0:
        raise ReshapeError("the model %s is mirrored in its file and can't be copied" % name)
    gid = loc["gid"].encode("utf-8")
    gm = re.search(rb'<geometry\b[^>]*\bid\s*=\s*["\']' + re.escape(gid) + rb'["\'][^>]*>', data)
    if not gm:
        raise ReshapeError("can't find the mesh of %s" % name)
    ge = data.find(b"</geometry>", gm.end())
    geom = data[gm.start():ge + len(b"</geometry>")]
    s, e = _array_span(geom, loc["pos_array"])
    if _count(geom, s, e) != len(pos):
        raise ReshapeError("the model %s changed in its file since it was loaded; open the vehicle again" % name)
    geom = geom[:s] + _numbers(pos) + geom[e:]
    # normals turn with the node transform (and the up axis) that is now baked into the positions
    inv, _ = _inverse(m)
    nm = [inv[0], inv[4], inv[8], inv[1], inv[5], inv[9], inv[2], inv[6], inv[10]]     # inverse transpose
    g = loc["geom"]
    normal_srcs = {src for inputs in g["prims"] for sem, src in inputs if sem == "NORMAL"}
    for v in g["verts"].values():
        if v.get("NORMAL"):
            normal_srcs.add(v["NORMAL"])
    for src in normal_srcs:
        fa, stride = g["sources"].get(src, (None, 0))
        if not fa or stride != 3:
            continue
        ns, ne = _array_span(geom, fa)
        vals = [float(x) for x in geom[ns:ne].split()]
        out = []
        for k in range(0, len(vals) - 2, 3):
            x, y, z = vals[k:k + 3]
            X, Y, Z = nm[0] * x + nm[1] * y + nm[2] * z, nm[3] * x + nm[4] * y + nm[5] * z, nm[6] * x + nm[7] * y + nm[8] * z
            if info["up"] == "Y_UP":
                X, Y, Z = X, -Z, Y
            elif info["up"] == "X_UP":
                X, Y, Z = Y, Z, X
            ln = math.sqrt(X * X + Y * Y + Z * Z) or 1.0
            out += [X / ln, Y / ln, Z / ln]
        geom = geom[:ns] + _numbers(out) + geom[ne:]
    geom = _vertex_normals(geom, loc, pos)
    mats = []
    for sym, target in loc["binds"].items():
        mname = info["mat_names"].get(target) or re.sub(r"-material$", "", target) or sym
        mats.append((sym, mname))
    effects = "\n".join(
        '    <effect id={eid}><profile_COMMON><technique sid="common"><lambert>'
        '<diffuse><color sid="diffuse">0.8 0.8 0.8 1</color></diffuse></lambert></technique></profile_COMMON></effect>'
        .format(eid=quoteattr("jbe-mat%d-effect" % k)) for k in range(len(mats)))
    materials = "\n".join('    <material id={mid} name={n}><instance_effect url={eu}/></material>'.format(
        mid=quoteattr("jbe-mat%d" % k), n=quoteattr(mn), eu=quoteattr("#jbe-mat%d-effect" % k))
        for k, (_, mn) in enumerate(mats))
    binds = "\n".join('            <instance_material symbol={s} target={t}/>'.format(
        s=quoteattr(sym), t=quoteattr("#jbe-mat%d" % k)) for k, (sym, _) in enumerate(mats))
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
    {geom}
  </library_geometries>
  <library_visual_scenes>
    <visual_scene id="Scene" name="Scene">
      <node id={nid} name={nid} type="NODE">
        <matrix sid="transform">1 0 0 0 0 1 0 0 0 0 1 0 0 0 0 1</matrix>
        <instance_geometry url={gurl} name={nid}>
          <bind_material><technique_common>
{binds}
          </technique_common></bind_material>
        </instance_geometry>
      </node>
    </visual_scene>
  </library_visual_scenes>
  <scene><instance_visual_scene url="#Scene"/></scene>
</COLLADA>
""".format(effects=effects, materials=materials, geom=geom.decode("utf-8"), nid=quoteattr(new_name),
           gurl=quoteattr("#" + loc["gid"]), binds=binds)

