"""Find and serve flexbody meshes for the 3D preview.

The first time a vehicle is opened every .dae under vehicles/<model>/ and
vehicles/common/ is scanned for object names (common.zip alone has ~700 MB of
.dae, so the result is cached on disk). Meshes are pulled out of a .dae only
when the preview asks for them, and also cached on disk.

Each mesh comes with the materials its triangles use; the colours of those
that have one (plain coloured materials, paint, glass) are read from the
vehicle's *.materials.json files. Textures are not used.
"""
import hashlib
import json
import os
import pickle
import struct
import threading
from array import array
from collections import OrderedDict

from . import dae, jbeamfile, sources

CACHE_VERSION = 2       # bump when the extracted mesh format changes


class MeshLibrary:
    def __init__(self, ws, cache_dir):
        self.ws = ws
        self.dir = os.path.join(cache_dir, "meshes")
        os.makedirs(self.dir, exist_ok=True)
        self.lock = threading.RLock()
        self._names = {}      # (root.id, lower rel) -> set of names
        self._index = {}      # model -> {name: (root, rel)}
        self._extracted = OrderedDict()
        self._scanning = {}
        self._names_disk = self._load_json("names.json")
        self._mat_index = {}      # model -> {material name: definition}
        self._mat_files = {}      # file key -> {material name: definition}

    def _load_json(self, fn):
        try:
            with open(os.path.join(self.dir, fn), encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError):
            return {}

    def _save_names(self):
        tmp = os.path.join(self.dir, "names.json.tmp")
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self._names_disk, f)
            os.replace(tmp, os.path.join(self.dir, "names.json"))
        except OSError:
            pass

    @staticmethod
    def _stamp(root, rel):
        try:
            return "%s-%s" % root.stamp(rel)
        except (OSError, KeyError):
            return "?"

    def _file_key(self, root, rel):
        return "%s|%s|%s" % (root.id, sources.norm(rel), self._stamp(root, rel))

    def _dae_files(self, model):
        seen = {}
        for prefix in ("vehicles/%s" % model, "vehicles/common"):
            for r in self.ws.roots:
                if r.inactive:
                    continue
                for rel in r.files_under(prefix):
                    low = sources.norm(rel)
                    if low.endswith(".dae") and low not in seen:
                        seen[low] = (r, rel)
        return list(seen.values())

    def names_in(self, root, rel):
        key = self._file_key(root, rel)
        hit = self._names_disk.get(key)
        if hit is not None:
            return set(hit)
        try:
            names = dae.scan_names(root.read(rel))
        except (OSError, KeyError):
            names = set()
        self._names_disk[key] = sorted(names)
        return names

    def index(self, model):
        """{object name: (root, rel)}; model folder files win over common ones."""
        with self.lock:
            idx = self._index.get(model)
            if idx is not None:
                return idx
            idx = {}
            for root, rel in self._dae_files(model):
                for n in self.names_in(root, rel):
                    idx.setdefault(n, (root, rel))
            self._save_names()
            self._index[model] = idx
            return idx

    def forget(self, model=None):
        with self.lock:
            if model is None:
                self._index.clear()
                self._mat_index.clear()
            else:
                self._index.pop(model, None)
                self._mat_index.pop(model, None)

    def _extract(self, root, rel):
        key = self._file_key(root, rel)
        with self.lock:
            if key in self._extracted:
                self._extracted.move_to_end(key)
                return self._extracted[key]
        path = os.path.join(self.dir, hashlib.sha1(("%s|v%d" % (key, CACHE_VERSION)).encode("utf-8")).hexdigest()[:20]
                            + ".pickle")
        data = None
        try:
            with open(path, "rb") as f:
                data = pickle.load(f)
        except Exception:
            data = None
        if data is None:
            try:
                raw = dae.extract(root.read(rel))
            except Exception as e:  # a broken .dae shouldn't stop the editor
                raw = {}
                print("can't read mesh file %s: %s" % (rel, e))
            data = {n: (p.tobytes(), i.tobytes(), [list(x) for x in g]) for n, (p, i, g) in raw.items()}
            try:
                with open(path + ".tmp", "wb") as f:
                    pickle.dump(data, f, protocol=pickle.HIGHEST_PROTOCOL)
                os.replace(path + ".tmp", path)
            except OSError:
                pass
        with self.lock:
            self._extracted[key] = data
            while len(self._extracted) > 6:
                self._extracted.popitem(last=False)
        return data

    def pack(self, model, names):
        """Binary blob: uint32 JSON length, JSON {name: [posOffset, posCount, idxOffset, idxCount, file]}, data."""
        idx = self.index(model)
        by_file = {}
        missing = []
        for n in names:
            loc = idx.get(n)
            if loc is None:
                missing.append(n)
            else:
                by_file.setdefault((loc[0].id, loc[1]), (loc, []))[1].append(n)
        chunks, table, off = [], {}, 0
        used = set()
        for (_, _), ((root, rel), ns) in by_file.items():
            data = self._extract(root, rel)
            for n in ns:
                got = data.get(n)
                if got is None:
                    missing.append(n)
                    continue
                pos, ind, groups = got[0], got[1], (got[2] if len(got) > 2 else [])
                table[n] = [off, len(pos) // 4, off + len(pos), len(ind) // 4, rel, groups]
                used.update(g[2] for g in groups)
                chunks.append(pos)
                chunks.append(ind)
                off += len(pos) + len(ind)
        mats = self.material_info(model, used) if used else {}
        head = json.dumps({"meshes": table, "missing": missing, "materials": mats}).encode("utf-8")
        head += b" " * ((4 - len(head) % 4) % 4)
        return struct.pack("<I", len(head)) + head + b"".join(chunks)

    def mesh_arrays(self, model, name):
        loc = self.index(model).get(name)
        if loc is None:
            return None
        got = self._extract(*loc).get(name)
        if got is None:
            return None
        p, i = array("f"), array("I")
        p.frombytes(got[0])
        i.frombytes(got[1])
        return p, i

    # -- materials ---------------------------------------------------------------
    def _material_files(self, model):
        seen = {}
        for prefix in ("vehicles/%s" % model, "vehicles/common"):
            for r in self.ws.roots:
                if r.inactive:
                    continue
                for rel in r.files_under(prefix):
                    low = sources.norm(rel)
                    if low.endswith("materials.json") and low not in seen:
                        seen[low] = (r, rel)
        return list(seen.values())

    def material_index(self, model):
        """{material name (and mapTo): definition} from the vehicle's and the common materials files."""
        with self.lock:
            idx = self._mat_index.get(model)
            if idx is not None:
                return idx
        idx = {}
        for root, rel in self._material_files(model):
            key = self._file_key(root, rel)
            with self.lock:
                defs = self._mat_files.get(key)
            if defs is None:
                defs = {}
                try:
                    data = jbeamfile.loads(jbeamfile.decode(root.read(rel)))
                except Exception:
                    data = None
                if isinstance(data, dict):
                    for k, d in data.items():
                        if isinstance(d, dict) and d.get("class", "Material") == "Material":
                            for nm in (d.get("mapTo"), d.get("name"), k):
                                if isinstance(nm, str) and nm:
                                    defs.setdefault(nm, d)
                with self.lock:
                    self._mat_files[key] = defs
            for nm, d in defs.items():
                idx.setdefault(nm, d)
        with self.lock:
            self._mat_index[model] = idx
        return idx

    def material_info(self, model, names):
        """{name: {color, metallic, roughness, opacity, paint, glass}} for the materials that have a colour to show."""
        idx = self.material_index(model)
        out = {}
        for n in names:
            d = idx.get(n)
            info = describe_material(d) if d is not None else None
            if info:
                out[n] = info
        return out


def _rgb(v):
    if isinstance(v, str):
        try:
            v = [float(x) for x in v.split()]
        except ValueError:
            return None
    if isinstance(v, (list, tuple)) and len(v) >= 3 and all(isinstance(x, (int, float)) for x in v[:3]):
        return [max(0.0, min(1.0, float(x))) for x in v[:3]]
    return None


def describe_material(d):
    """How a material looks in the preview, or None when it has nothing but textures to show."""
    stages = d.get("Stages") or d.get("stages") or []
    st = stages[0] if stages and isinstance(stages[0], dict) else {}
    paint = any(isinstance(x, dict) and x.get("instanceDiffuse") for x in stages)
    glass = bool(d.get("translucent")) and not d.get("alphaTest")
    col = _rgb(st.get("baseColorFactor")) or _rgb(st.get("diffuseColor"))
    textured = any(st.get(k) for k in ("baseColorMap", "colorMap", "diffuseMap"))
    out = {}
    if col is not None and not (textured and min(col) > 0.95):
        out["color"] = col
    if paint:
        out["paint"] = True
    if glass:
        out["glass"] = True
        out["opacity"] = 0.35
        out.setdefault("color", [0.55, 0.62, 0.68])
    if not out:
        return None
    out.setdefault("color", [0.667, 0.702, 0.761])
    for key, name in (("metallicFactor", "metallic"), ("roughnessFactor", "roughness")):
        v = st.get(key)
        if isinstance(v, (int, float)):
            out[name] = max(0.0, min(1.0, float(v)))
    return out
