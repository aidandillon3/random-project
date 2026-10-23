"""Vehicles, their files and their parts.

Workspace knows every root (mod folders, mod zips, game zips) and caches
parsed files. Model gathers what the game would load for one vehicle: every
.jbeam under vehicles/<model>/ and vehicles/common/, the .pc configs and
info.json, and indexes the parts by name and slot type.
"""
import copy
import hashlib
import json
import os
import pickle
import threading

from . import jbeamfile, sources


class FileRef:
    """One file of a model, as the UI and the save code refer to it."""
    __slots__ = ("key", "root", "rel")

    def __init__(self, root, rel):
        self.root = root
        self.rel = rel
        self.key = root.id + "|" + sources.norm(rel)

    def to_json(self):
        return {"key": self.key, "rel": self.rel, "root": self.root.label, "origin": self.root.origin,
                "editable": self.root.editable,
                "path": self.root.full_path(self.rel) if self.root.kind == "folder" else self.root.path + "!" + self.rel}


class Part:
    __slots__ = ("name", "file", "data", "slot_type", "title", "in_common", "virtual")

    def __init__(self, name, file, data, in_common, virtual=False):
        self.virtual = virtual
        self.name = name
        self.file = file
        self.data = data
        st = data.get("slotType")
        self.slot_type = st
        info = data.get("information") if isinstance(data.get("information"), dict) else {}
        t = info.get("name")
        self.title = t if isinstance(t, str) and t else name
        self.in_common = in_common

    def slot_types(self):
        st = self.slot_type
        if isinstance(st, list):
            return [s for s in st if isinstance(s, str)]
        return [st] if isinstance(st, str) else []


class VirtualRoot:
    """Where preview-only parts (not yet written anywhere) live."""
    kind = "virtual"
    origin = "preview"
    editable = False
    inactive = False
    off_in_game = False
    path = ""

    def __init__(self, label):
        self.label = label
        self.id = "virtual:" + label

    def full_path(self, rel):
        return rel

    def read(self, rel):
        raise OSError("preview parts are not in a file yet")


class Workspace:
    def __init__(self, cfg, cache_dir):
        self.cfg = cfg
        self.cache_dir = cache_dir
        os.makedirs(cache_dir, exist_ok=True)
        self.lock = threading.RLock()
        self._parsed = {}     # (root.id, lower rel) -> (stamp, data, error)
        self._zipcache = {}   # root.id -> {lower rel: (stamp, data, error)}
        self.models = {}
        self.refresh()

    def refresh(self):
        with self.lock:
            for r in getattr(self, "roots", []):
                r.close()
            self.roots = sources.discover(self.cfg)
            self.models = {}

    def root_by_id(self, rid):
        for r in self.roots:
            if r.id == rid:
                return r
        return None

    # -- parsing with caches ---------------------------------------------
    def _zip_cache_path(self, root):
        h = hashlib.sha1(root.id.encode("utf-8")).hexdigest()[:16]
        return os.path.join(self.cache_dir, "zip-%s.pickle" % h)

    def _load_zip_cache(self, root):
        if root.id in self._zipcache:
            return self._zipcache[root.id]
        cache = {}
        try:
            with open(self._zip_cache_path(root), "rb") as f:
                stamp, data = pickle.load(f)
            if stamp == root.zip_stamp():
                cache = data
        except Exception:
            cache = {}
        cache.setdefault("__dirty__", False)
        self._zipcache[root.id] = cache
        return cache

    def save_caches(self):
        with self.lock:
            for rid, cache in self._zipcache.items():
                if cache.get("__dirty__"):
                    root = self.root_by_id(rid)
                    if root is None:
                        continue
                    cache["__dirty__"] = False
                    tmp = self._zip_cache_path(root) + ".tmp"
                    try:
                        with open(tmp, "wb") as f:
                            pickle.dump((root.zip_stamp(), cache), f, protocol=pickle.HIGHEST_PROTOCOL)
                        os.replace(tmp, self._zip_cache_path(root))
                    except OSError:
                        pass

    def parse(self, root, rel):
        """(data, error) for a file. Data is shared: never modify it."""
        low = sources.norm(rel)
        if root.kind == "zip":
            cache = self._load_zip_cache(root)
            hit = cache.get(low)
            if hit is not None:
                return hit[1], hit[2]
            data, err = self._parse_bytes(root, rel)
            cache[low] = (None, data, err)
            cache["__dirty__"] = True
            return data, err
        try:
            stamp = root.stamp(rel)
        except OSError as e:
            return None, str(e)
        hit = self._parsed.get((root.id, low))
        if hit and hit[0] == stamp:
            return hit[1], hit[2]
        data, err = self._parse_bytes(root, rel)
        self._parsed[(root.id, low)] = (stamp, data, err)
        return data, err

    @staticmethod
    def _parse_bytes(root, rel):
        try:
            return jbeamfile.load_bytes(root.read(rel)), None
        except jbeamfile.ParseError as e:
            return None, str(e)
        except (OSError, KeyError) as e:
            return None, "can't read: %s" % e

    # -- vehicles ----------------------------------------------------------
    def vehicle_list(self):
        found = {}
        for r in self.roots:
            if r.inactive:
                continue
            for m in r.models():
                # a folder with only saved configs (like the user folder) holds no parts to edit
                jb = any(f.lower().endswith(".jbeam") for f in r.files_under("vehicles/%s" % m))
                e = found.setdefault(m, {"model": m, "sources": [], "editable": False, "parts": False, "builder": False,
                                         "scrap": False})
                e["sources"].append(r.label)
                e["editable"] = e["editable"] or (r.editable and jb)
                e["parts"] = e["parts"] or jb
                e["builder"] = e["builder"] or r.has("vehicles/%s/jbeam_editor_builder.json" % m)
                e["scrap"] = e["scrap"] or r.has("vehicles/%s/jbeam_editor_scrap.json" % m)
        out = []
        for m, e in found.items():
            if not e.pop("parts"):
                continue
            name, brand, typ = m, "", ""
            for r in self.roots:
                if r.inactive or m not in r.models():
                    continue
                for rel in (("vehicles/%s/info.json" % m),):
                    if r.has(rel):
                        data, _ = self.parse(r, rel)
                        if isinstance(data, dict):
                            name = _label(data.get("Name")) or name
                            brand = _label(data.get("Brand"))
                            typ = _label(data.get("Type"))
                        break
                if name != m:
                    break
            e.update({"name": name, "brand": brand, "type": typ})
            out.append(e)
        self.save_caches()
        out.sort(key=lambda e: (not e["editable"], (e["brand"] + " " + e["name"]).lower()))
        return out

    def model(self, name, reload=False):
        with self.lock:
            m = self.models.get(name)
            if m is None or reload:
                for r in self.roots:
                    if r.kind == "folder":
                        r.refresh()
                m = Model(self, name)
                self.models[name] = m
                self.save_caches()
            return m


class Model:
    def __init__(self, ws, name):
        self.ws = ws
        self.name = name
        self.files = {}       # key -> FileRef
        self.parts = {}       # part name -> Part
        self.by_slot = {}     # slot type -> [part names]
        self.duplicates = []  # (part name, kept file, ignored file)
        self.errors = []      # (file key, message)
        self.configs = []     # [{name, file key}]
        self.info = {}
        self.info_file = None
        self._load()

    def _collect(self, prefix):
        """{lower rel: (root, rel)} with the highest-priority root for each path."""
        seen = {}
        for r in self.ws.roots:
            if r.inactive:
                continue
            for rel in r.files_under(prefix):
                low = sources.norm(rel)
                if low not in seen:
                    seen[low] = (r, rel)
        return seen

    def _load(self):
        model_files = self._collect("vehicles/%s" % self.name)
        common_files = self._collect("vehicles/common")
        for group, in_common in ((model_files, False), (common_files, True)):
            for low, (root, rel) in sorted(group.items(), key=lambda kv: (not kv[1][0].editable, kv[0])):
                if low.endswith(".jbeam"):
                    self._load_jbeam(root, rel, in_common)
                elif not in_common and low.endswith(".pc"):
                    fr = self._fileref(root, rel)
                    base = os.path.splitext(os.path.basename(rel))[0]
                    self.configs.append({"name": base, "file": fr.key, "editable": root.editable})
                elif not in_common and low == "vehicles/%s/info.json" % self.name:
                    data, _ = self.ws.parse(root, rel)
                    if isinstance(data, dict):
                        self.info = data
                        self.info_file = self._fileref(root, rel)
        for pname, part in self.parts.items():
            for st in part.slot_types():
                self.by_slot.setdefault(st, []).append(pname)
        self.configs.sort(key=lambda c: c["name"].lower())

    def _fileref(self, root, rel):
        fr = FileRef(root, rel)
        self.files[fr.key] = fr
        return fr

    def _load_jbeam(self, root, rel, in_common):
        data, err = self.ws.parse(root, rel)
        fr = self._fileref(root, rel)
        if err:
            self.errors.append((fr.key, err))
            return
        if not isinstance(data, dict):
            self.errors.append((fr.key, "the file is not a { } object of parts"))
            return
        for pname, pdata in data.items():
            if not isinstance(pdata, dict):
                continue
            if "slotType" not in pdata:
                continue
            if pname in self.parts:
                self.duplicates.append((pname, self.parts[pname].file.key, fr.key))
                continue
            self.parts[pname] = Part(pname, fr, pdata, in_common)

    # -- helpers -----------------------------------------------------------
    def main_parts(self):
        return sorted(p.name for p in self.parts.values() if "main" in p.slot_types() and not p.in_common)

    def default_config_name(self):
        dpc = self.info.get("default_pc") or self.info.get("default_pc".lower())
        names = [c["name"] for c in self.configs]
        if dpc and dpc in names:
            return dpc
        return names[0] if names else None

    def read_config(self, name):
        for c in self.configs:
            if c["name"] == name:
                fr = self.files[c["file"]]
                data, err = self.ws.parse(fr.root, fr.rel)
                if err:
                    raise ValueError("%s: %s" % (fr.rel, err))
                return data
        raise KeyError(name)

    def editable_roots(self):
        out = []
        for r in self.ws.roots:
            if r.editable and not r.inactive and r.files_under("vehicles/%s" % self.name):
                out.append(r)
        return out

    def overlay(self, extra, label="preview"):
        """A copy of this model with extra parts {name: data} added (for previews; nothing is written)."""
        m = copy.copy(self)
        m.parts = dict(self.parts)
        m.by_slot = {k: list(v) for k, v in self.by_slot.items()}
        m.files = dict(self.files)
        fr = FileRef(VirtualRoot(label), "vehicles/%s/(%s).jbeam" % (self.name, label))
        m.files[fr.key] = fr
        for name, data in extra.items():
            p = Part(name, fr, data, False, virtual=True)
            m.parts[name] = p
            for st in p.slot_types():
                m.by_slot.setdefault(st, []).append(name)
        return m

    @classmethod
    def from_parts(cls, ws, name, parts, label="builder"):
        """A model made only of preview parts {name: data} (a vehicle that is not written anywhere yet)."""
        m = cls.__new__(cls)
        m.ws, m.name = ws, name
        m.files, m.parts, m.by_slot = {}, {}, {}
        m.duplicates, m.errors, m.configs = [], [], []
        m.info, m.info_file = {}, None
        fr = FileRef(VirtualRoot(label), "vehicles/%s/(%s).jbeam" % (name, label))
        m.files[fr.key] = fr
        for pname, data in parts.items():
            p = Part(pname, fr, data, False, virtual=True)
            m.parts[pname] = p
            for st in p.slot_types():
                m.by_slot.setdefault(st, []).append(pname)
        return m

    def summary(self):
        return {
            "model": self.name,
            "name": _label(self.info.get("Name")) or self.name,
            "brand": _label(self.info.get("Brand")),
            "configs": self.configs,
            "defaultConfig": self.default_config_name(),
            "mainParts": self.main_parts(),
            "editableRoots": [{"id": r.id, "label": r.label, "path": r.path, "offInGame": r.off_in_game}
                              for r in self.editable_roots()],
            "zipRoots": [{"id": r.id, "label": r.label, "path": r.path, "origin": r.origin}
                         for r in self.ws.roots if r.kind == "zip" and not r.inactive
                         and r.files_under("vehicles/%s" % self.name)],
            "errors": [{"file": self.files[k].rel, "root": self.files[k].root.label, "message": m}
                       for k, m in self.errors],
            "duplicates": [{"part": p, "kept": self.files[a].rel, "ignored": self.files[b].rel}
                           for p, a, b in self.duplicates],
            "partCount": len(self.parts),
            "defaultPaint": self.default_paint(),
        }

    def default_paint(self):
        """The vehicle's own main paint colour [r, g, b] from its info.json, or None."""
        info = self.info if isinstance(self.info, dict) else {}
        paints = info.get("paints")
        name = info.get("defaultPaintName1")
        if isinstance(paints, dict) and isinstance(paints.get(name), dict):
            c = paints[name].get("baseColor")
            if isinstance(c, list) and len(c) >= 3 and all(isinstance(x, (int, float)) for x in c[:3]):
                return [max(0.0, min(1.0, float(x))) for x in c[:3]]
        colors, dc = info.get("colors"), info.get("default_color")
        if isinstance(colors, dict) and isinstance(colors.get(dc), str):
            try:
                c = [float(x) for x in colors[dc].split()]
            except ValueError:
                return None
            if len(c) >= 3:
                return [max(0.0, min(1.0, x)) for x in c[:3]]
        return None


def _label(v):
    """Display text from info.json: skip translation keys like vehiclesData.covet.Name."""
    if not isinstance(v, str) or v.startswith("vehiclesData."):
        return ""
    return v


def dumps(v):
    return json.dumps(v, ensure_ascii=False, separators=(",", ":"))
