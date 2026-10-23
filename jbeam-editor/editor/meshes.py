"""Find and serve flexbody meshes for the 3D preview.

The first time a vehicle is opened every .dae under vehicles/<model>/ and
vehicles/common/ is scanned for object names (common.zip alone has ~700 MB of
.dae, so the result is cached on disk). Meshes are pulled out of a .dae only
when the preview asks for them, and also cached on disk.
"""
import hashlib
import json
import os
import pickle
import struct
import threading
from array import array
from collections import OrderedDict

from . import dae, sources


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
            else:
                self._index.pop(model, None)

    def _extract(self, root, rel):
        key = self._file_key(root, rel)
        with self.lock:
            if key in self._extracted:
                self._extracted.move_to_end(key)
                return self._extracted[key]
        path = os.path.join(self.dir, hashlib.sha1(key.encode("utf-8")).hexdigest()[:20] + ".pickle")
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
            data = {n: (p.tobytes(), i.tobytes()) for n, (p, i) in raw.items()}
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
        for (_, _), ((root, rel), ns) in by_file.items():
            data = self._extract(root, rel)
            for n in ns:
                got = data.get(n)
                if got is None:
                    missing.append(n)
                    continue
                pos, ind = got
                table[n] = [off, len(pos) // 4, off + len(pos), len(ind) // 4, rel]
                chunks.append(pos)
                chunks.append(ind)
                off += len(pos) + len(ind)
        head = json.dumps({"meshes": table, "missing": missing}).encode("utf-8")
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
