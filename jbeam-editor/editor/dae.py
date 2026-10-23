"""Read triangle meshes out of Collada (.dae) files for the 3D preview.

BeamNG finds a flexbody's mesh by the name of an object (a <node>) in any
.dae file of the vehicle or of vehicles/common. This module pulls each named
object's triangles out, already placed by its node transforms, in Z-up
metres. Compiled .cdae files can't be read; those flexbodies simply show no
mesh.
"""
import io
import math
import re
import xml.etree.ElementTree as ET
from array import array

_NAME_RE = re.compile(rb'<node\b[^>]*?\bname="([^"]+)"')


def scan_names(data):
    """Object names in a .dae without parsing it (fast)."""
    return {m.group(1).decode("utf-8", "replace") for m in _NAME_RE.finditer(data)}


def _tag(el):
    t = el.tag
    return t[t.index("}") + 1:] if t.startswith("{") else t


def _mat_mul(a, b):
    return [sum(a[r * 4 + k] * b[k * 4 + c] for k in range(4)) for r in range(4) for c in range(4)]


_IDENT = [1.0, 0, 0, 0, 0, 1.0, 0, 0, 0, 0, 1.0, 0, 0, 0, 0, 1.0]


def _node_matrix(node):
    m = list(_IDENT)
    for ch in node:
        t = _tag(ch)
        vals = [float(x) for x in (ch.text or "").split()]
        if t == "matrix" and len(vals) == 16:
            m = _mat_mul(m, vals)
        elif t == "translate" and len(vals) == 3:
            m = _mat_mul(m, [1, 0, 0, vals[0], 0, 1, 0, vals[1], 0, 0, 1, vals[2], 0, 0, 0, 1])
        elif t == "scale" and len(vals) == 3:
            m = _mat_mul(m, [vals[0], 0, 0, 0, 0, vals[1], 0, 0, 0, 0, vals[2], 0, 0, 0, 0, 1])
        elif t == "rotate" and len(vals) == 4:
            x, y, z, ang = vals
            ln = math.sqrt(x * x + y * y + z * z) or 1.0
            x, y, z = x / ln, y / ln, z / ln
            c, s = math.cos(math.radians(ang)), math.sin(math.radians(ang))
            C = 1 - c
            m = _mat_mul(m, [x * x * C + c, x * y * C - z * s, x * z * C + y * s, 0,
                             y * x * C + z * s, y * y * C + c, y * z * C - x * s, 0,
                             z * x * C - y * s, z * y * C + x * s, z * z * C + c, 0,
                             0, 0, 0, 1])
    return m


def _read_geometry(geom):
    """(positions array('f'), triangle index array('I')) of one <geometry>."""
    mesh = None
    for ch in geom:
        if _tag(ch) == "mesh":
            mesh = ch
            break
    if mesh is None:
        return None
    sources, verts = {}, {}
    for ch in mesh:
        t = _tag(ch)
        if t == "source":
            fa = None
            stride = 3
            for sub in ch:
                st = _tag(sub)
                if st == "float_array":
                    fa = sub.text or ""
                elif st == "technique_common":
                    for acc in sub:
                        if _tag(acc) == "accessor":
                            stride = int(acc.get("stride", "3"))
            if fa is not None:
                sources[ch.get("id")] = (fa, stride)
        elif t == "vertices":
            for inp in ch:
                if _tag(inp) == "input" and inp.get("semantic") == "POSITION":
                    verts[ch.get("id")] = inp.get("source", "").lstrip("#")
    pos_src = None
    tris = array("I")
    for prim in mesh:
        t = _tag(prim)
        if t not in ("triangles", "polylist", "polygons"):
            continue
        offs = 0
        voff = None
        for inp in prim:
            if _tag(inp) == "input":
                o = int(inp.get("offset", "0"))
                offs = max(offs, o + 1)
                if inp.get("semantic") == "VERTEX":
                    voff = o
                    pos_src = verts.get(inp.get("source", "").lstrip("#"), pos_src)
        if voff is None:
            continue
        if t == "triangles":
            for p in prim:
                if _tag(p) == "p" and p.text:
                    idx = array("I", map(int, p.text.split()))
                    tris.extend(idx[voff::offs])
        elif t == "polylist":
            vcount, p_text = None, None
            for sub in prim:
                if _tag(sub) == "vcount":
                    vcount = [int(x) for x in (sub.text or "").split()]
                elif _tag(sub) == "p":
                    p_text = sub.text or ""
            if vcount is None or p_text is None:
                continue
            idx = array("I", map(int, p_text.split()))[voff::offs]
            k = 0
            for n in vcount:
                for j in range(1, n - 1):
                    tris.extend((idx[k], idx[k + j], idx[k + j + 1]))
                k += n
        else:
            for sub in prim:
                if _tag(sub) == "p" and sub.text:
                    idx = array("I", map(int, sub.text.split()))[voff::offs]
                    for j in range(1, len(idx) - 1):
                        tris.extend((idx[0], idx[j], idx[j + 1]))
    if pos_src is None or pos_src not in sources:
        return None
    text, stride = sources[pos_src]
    floats = array("f", map(float, text.split()))
    if stride != 3:
        floats = array("f", (floats[i + k] for i in range(0, len(floats), stride) for k in range(3)))
    return floats, tris


def extract(data):
    """{object name: (positions array('f'), indices array('I'))} for a .dae file's bytes."""
    geoms = {}
    scenes = []
    up = "Z_UP"
    unit = 1.0
    for event, el in ET.iterparse(io.BytesIO(data), events=("end",)):
        t = _tag(el)
        if t == "geometry":
            g = _read_geometry(el)
            if g is not None:
                geoms[el.get("id")] = g
            el.clear()
        elif t == "up_axis":
            up = (el.text or "Z_UP").strip()
        elif t == "unit":
            try:
                unit = float(el.get("meter", "1"))
            except ValueError:
                unit = 1.0
        elif t == "visual_scene":
            scenes.append(el)
        elif t in ("library_images", "library_effects", "library_materials", "library_controllers",
                   "library_animations"):
            el.clear()

    out = {}

    def place(name, mat, geom_id):
        g = geoms.get(geom_id)
        if g is None:
            return
        src, tri = g
        n = len(src) // 3
        pos = array("f", bytes(4 * 3 * n))
        a = mat
        for i in range(n):
            x, y, z = src[3 * i], src[3 * i + 1], src[3 * i + 2]
            X = a[0] * x + a[1] * y + a[2] * z + a[3]
            Y = a[4] * x + a[5] * y + a[6] * z + a[7]
            Z = a[8] * x + a[9] * y + a[10] * z + a[11]
            if up == "Y_UP":
                X, Y, Z = X, -Z, Y
            elif up == "X_UP":
                X, Y, Z = Y, Z, X
            pos[3 * i], pos[3 * i + 1], pos[3 * i + 2] = X * unit, Y * unit, Z * unit
        if name in out:
            p0, t0 = out[name]
            base = len(p0) // 3
            p0.extend(pos)
            t0.extend(i + base for i in tri)
        else:
            out[name] = (pos, array("I", tri))

    def walk(node, parent):
        m = _mat_mul(parent, _node_matrix(node))
        name = node.get("name") or node.get("id") or ""
        for ch in node:
            t = _tag(ch)
            if t == "instance_geometry":
                place(name, m, ch.get("url", "").lstrip("#"))
            elif t == "node":
                walk(ch, m)

    for sc in scenes:
        for ch in sc:
            if _tag(ch) == "node":
                walk(ch, _IDENT)
    # objects named after their geometry, for files without a scene
    if not out:
        for gid, (pos, tri) in geoms.items():
            out[gid.replace("-mesh", "")] = (pos, tri)
    return out
