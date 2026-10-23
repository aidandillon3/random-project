"""Finding Scrap Mechanic, the creations saved in it, and what each of its shapes is.

Blueprints only name their shapes by uuid; how big a part is and what it does
(wheel, bearing, suspension, engine, seat...) is in the game's shapesets
(Objects/Database/ShapeSets/*.json) and the shown names in its language files.
Parts from workshop mods are looked up in the mods the blueprint lists as
dependencies, then in every installed mod.
"""
import glob
import json
import os
import re
import threading

from .. import jbeamfile, sources

CELL = 0.25                 # metres per grid cell
APP_ID = "387990"           # Scrap Mechanic on Steam
GAME_ROOTS = ("Data", "Survival", "ChallengeData")

# used only when the game is not found: the shapes every vehicle uses
FALLBACK = {
    "69e362c3-32aa-4cd1-adc0-dcfc47b92c0d": {"kind": "part", "title": "Small Wheel", "role": "wheel",
                                             "collision": "cylinder", "size": (3, 3, 1), "axis": 2, "density": 250,
                                             "material": "Rubber"},
    "db66f0b1-0c50-4b74-bdc7-771374204b1f": {"kind": "part", "title": "Big Wheel", "role": "wheel",
                                             "collision": "cylinder", "size": (5, 5, 2), "axis": 2, "density": 250,
                                             "material": "Rubber"},
    "4a1b886b-913e-4aad-b5b6-6e41b0db23a6": {"kind": "joint", "title": "Bearing", "role": "bearing",
                                             "collision": "box", "size": (1, 1, 1), "density": 500,
                                             "material": "Metal"},
}

# kg per cubic metre when a shapeset gives none
MATERIAL_DENSITY = {"wood": 250, "metal": 500, "mechanical": 500, "concrete": 750, "glass": 350, "plastic": 200,
                    "rubber": 250, "fruit": 200, "sand": 600, "cardboard": 60, "potato": 200, "default": 300}


# ---------------------------------------------------------------------------
# where things are
# ---------------------------------------------------------------------------

def steam_libraries():
    libs = []
    for lib in sources._steam_libraries():
        if lib and lib not in libs:
            libs.append(lib)
    for c in (r"C:\Program Files (x86)\Steam", r"C:\Program Files\Steam", os.path.expanduser("~/.steam/steam"),
              os.path.expanduser("~/.local/share/Steam")):
        if c not in libs and os.path.isdir(c):
            libs.append(c)
    return libs


def is_game_folder(path):
    return bool(path) and os.path.isdir(os.path.join(path, "Data", "Objects", "Database"))


def find_game_folder(configured=""):
    if configured and is_game_folder(configured):
        return configured
    for lib in steam_libraries():
        c = os.path.join(lib, "steamapps", "common", "Scrap Mechanic")
        if is_game_folder(c):
            return c
    return ""


def workshop_folder(game_folder):
    """steamapps/workshop/content/387990 next to the game (or in any Steam library)."""
    cands = []
    if game_folder:
        apps = os.path.dirname(os.path.dirname(os.path.normpath(game_folder)))   # .../steamapps
        cands.append(os.path.join(apps, "workshop", "content", APP_ID))
    for lib in steam_libraries():
        cands.append(os.path.join(lib, "steamapps", "workshop", "content", APP_ID))
    for c in cands:
        if os.path.isdir(c):
            return c
    return ""


def user_folders(configured=""):
    """%APPDATA%/Axolot Games/Scrap Mechanic/User/User_*: blueprints and local mods.

    configured may also name one of these folders, the User folder above them or a Blueprints folder."""
    out = []
    p = (configured or "").strip()
    if p and os.path.isdir(p):
        if os.path.isdir(os.path.join(p, "Blueprints")):
            out.append(p)
        elif os.path.basename(os.path.normpath(p)).lower() == "blueprints":
            out.append(os.path.dirname(os.path.normpath(p)))
        else:
            out += sorted(glob.glob(os.path.join(p, "User_*")) + glob.glob(os.path.join(p, "User", "User_*")))
    base = os.environ.get("APPDATA") or ""
    if base:
        out += sorted(glob.glob(os.path.join(base, "Axolot Games", "Scrap Mechanic", "User", "User_*")))
    seen, res = set(), []
    for q in out:
        k = os.path.normcase(os.path.abspath(q))
        if k not in seen and os.path.isdir(q):
            seen.add(k)
            res.append(q)
    return res


# ---------------------------------------------------------------------------
# creations
# ---------------------------------------------------------------------------

def _read_json(path):
    with open(path, "rb") as f:
        data = f.read()
    text = jbeamfile.decode(data)
    try:
        return json.loads(text)
    except ValueError:
        return jbeamfile.loads(text)


def creation_entry(folder, source):
    """What the list shows about one blueprint folder, or None if it has no blueprint."""
    bp = os.path.join(folder, "blueprint.json")
    if not os.path.isfile(bp):
        return None
    desc = {}
    try:
        d = _read_json(os.path.join(folder, "description.json"))
        if isinstance(d, dict):
            desc = d
    except (OSError, ValueError, jbeamfile.ParseError):
        pass
    if desc.get("type") and str(desc.get("type")).lower() not in ("blueprint", "creation"):
        return None
    name = str(desc.get("name") or "").strip() or os.path.basename(folder)
    icon = ""
    for fn in ("icon.png", "icon.jpg", "preview.png", "preview.jpg"):
        if os.path.isfile(os.path.join(folder, fn)):
            icon = os.path.join(folder, fn)
            break
    try:
        mtime = os.path.getmtime(bp)
    except OSError:
        mtime = 0
    return {"id": source + ":" + os.path.basename(folder), "name": name[:80],
            "description": str(desc.get("description") or "")[:400], "source": source, "path": bp,
            "folder": folder, "icon": icon, "mtime": mtime}


def list_creations(game_folder="", user_folder=""):
    """Every blueprint: the player's own first (newest first), then workshop downloads."""
    out = []
    seen = set()
    for uf in user_folders(user_folder):
        for d in glob.glob(os.path.join(uf, "Blueprints", "*")):
            e = creation_entry(d, "mine")
            if e and e["id"] not in seen:
                seen.add(e["id"])
                out.append(e)
    ws = workshop_folder(game_folder)
    if ws:
        for d in glob.glob(os.path.join(ws, "*")):
            e = creation_entry(d, "workshop")
            if e and e["id"] not in seen:
                seen.add(e["id"])
                out.append(e)
    out.sort(key=lambda e: (e["source"] != "mine", -e["mtime"]))
    return out


# ---------------------------------------------------------------------------
# shapes
# ---------------------------------------------------------------------------

class ShapeInfo:
    __slots__ = ("uuid", "kind", "name", "title", "role", "collision", "size", "axis", "density", "material",
                 "level", "extra", "source")

    def __init__(self, uuid, kind):
        self.uuid = uuid
        self.kind = kind            # block, part, joint
        self.name = ""
        self.title = ""
        self.role = kind
        self.collision = "box"
        self.size = (1, 1, 1)       # cells, in the shape's own axes
        self.axis = 2               # cylinder axis (0 x, 1 y, 2 z)
        self.density = 0.0
        self.material = ""
        self.level = 0
        self.extra = {}
        self.source = ""

    def text(self):
        return (self.name + " " + self.title).lower()

    def volume_cells(self):
        x, y, z = self.size
        if self.collision == "cylinder":
            d = [x, y, z]
            depth = d.pop(self.axis)
            return 3.14159 * (max(d) / 2.0) ** 2 * depth
        if self.collision == "sphere":
            return 4.18879 * (x / 2.0) ** 3
        return x * y * z


def _num(v, d=0.0):
    try:
        return float(v)
    except (TypeError, ValueError):
        return d


def _collision(info, d):
    for key in ("box", "hull", "slant"):
        c = d.get(key)
        if isinstance(c, dict):
            info.collision = "box" if key != "slant" else "slant"
            info.size = (max(_num(c.get("x"), 1), 0.1), max(_num(c.get("y"), 1), 0.1), max(_num(c.get("z"), 1), 0.1))
            return
    c = d.get("cylinder")
    if isinstance(c, dict):
        dia = max(_num(c.get("diameter"), 1), 0.1)
        depth = max(_num(c.get("depth"), 1), 0.1)
        ax = str(c.get("axis") or "z").strip().lower()[:1]
        info.axis = {"x": 0, "y": 1}.get(ax, 2)
        size = [dia, dia, dia]
        size[info.axis] = depth
        info.collision = "cylinder"
        info.size = tuple(size)
        return
    c = d.get("sphere")
    if isinstance(c, dict):
        dia = max(_num(c.get("diameter"), 1), 0.1)
        info.collision = "sphere"
        info.size = (dia, dia, dia)


_LEVEL_RE = re.compile(r"(?:_|\s|lvl|level|mk)0*([1-9])\b")


def classify(info, d):
    """The role a shape plays on a vehicle, from its keys, names and shown title."""
    text = info.text()
    keys = set(d) if isinstance(d, dict) else set()
    script = ""
    sc = d.get("scripted") if isinstance(d, dict) else None
    if isinstance(sc, dict):
        script = str(sc.get("classname") or "").lower()
    text_all = text + " " + script
    m = _LEVEL_RE.search(text)
    if m:
        info.level = int(m.group(1))
    if isinstance(d, dict) and d.get("qualityLevel"):
        info.level = int(_num(d.get("qualityLevel"), 0)) or info.level
    mat = info.material.lower()
    if info.kind == "block":
        return "glass" if "glass" in mat or "glass" in text else "block"
    if info.kind == "joint":
        if "bearing" in keys or "bearing" in text_all:
            return "bearing"
        if "piston" in keys or "piston" in text_all:
            return "piston"
        if {"spring", "suspension", "shock"} & keys or any(w in text_all for w in ("susp", "spring", "shock")):
            info.extra["offroad"] = "off" in text_all and "road" in text_all
            info.extra["sport"] = "sport" in text_all
            return "suspension"
        return "joint"
    if "steering" not in text_all and any(w in text_all for w in ("wheel", "tire", "tyre")):
        return "wheel"
    if "seat" in keys or any(w in text_all for w in ("seat", "saddle")):
        driver = any(w in text_all for w in ("driver", "pilot", "control"))
        return "driverseat" if driver else "seat"
    if "thruster" in text_all or "rocket" in text_all:
        return "thruster"
    if "engine" in text_all or "motor" in text_all:
        info.extra["electric"] = "electric" in text_all
        return "engine"
    if any(w in text_all for w in ("gas container", "gas canister", "fuel", "battery", "gasoline")):
        return "fuel"
    if "headlight" in text_all or "light" in text_all:
        return "light"
    return "part"


def _make_info(uuid, kind, d, titles, source):
    info = ShapeInfo(uuid, kind)
    info.source = source
    name = str(d.get("name") or "")
    rend = d.get("renderable")
    if isinstance(rend, str):
        name += " " + os.path.splitext(os.path.basename(rend))[0]
    elif isinstance(rend, dict):
        for lod in rend.get("lodList") or []:
            if isinstance(lod, dict) and isinstance(lod.get("mesh"), str):
                name += " " + os.path.splitext(os.path.basename(lod["mesh"]))[0]
                break
    info.name = name.strip()
    t = titles.get(uuid)
    info.title = t if isinstance(t, str) else ""
    info.material = str(d.get("physicsMaterial") or "")
    info.density = _num(d.get("density"), 0.0)
    if kind != "block":
        _collision(info, d)
    if info.density <= 0:
        info.density = MATERIAL_DENSITY.get(info.material.lower(), MATERIAL_DENSITY["default"])
    info.role = classify(info, d)
    return info


def _titles_in(root):
    """{uuid: shown name} from <root>/Gui/Language/English/*.json."""
    out = {}
    for fn in glob.glob(os.path.join(root, "Gui", "Language", "English", "*.json")):
        try:
            d = _read_json(fn)
        except (OSError, ValueError, jbeamfile.ParseError):
            continue
        if not isinstance(d, dict):
            continue
        for k, v in d.items():
            if isinstance(v, dict) and isinstance(v.get("title"), str):
                out[k.lower()] = v["title"]
    return out


def _shapeset_files(root):
    files = []
    for pat in ("*.json", "*.shapeset"):
        files += glob.glob(os.path.join(root, "Objects", "Database", "ShapeSets", "**", pat), recursive=True)
    return sorted(set(files))


def _load_root(root, into, source):
    titles = _titles_in(root)
    n = 0
    for fn in _shapeset_files(root):
        try:
            d = _read_json(fn)
        except (OSError, ValueError, jbeamfile.ParseError):
            continue
        if not isinstance(d, dict):
            continue
        for key, kind in (("blockList", "block"), ("partList", "part"), ("jointList", "joint")):
            for e in d.get(key) or []:
                if not isinstance(e, dict) or not isinstance(e.get("uuid"), str):
                    continue
                u = e["uuid"].lower()
                if u in into:
                    continue
                into[u] = _make_info(u, kind, e, titles, source)
                n += 1
    return n


class ShapeDB:
    """uuid -> ShapeInfo for the game's own shapes, plus mods as they are needed."""

    def __init__(self, game_folder, user_folder=""):
        self.game_folder = game_folder
        self.user_folder = user_folder
        self.shapes = {}
        self.loaded_mods = set()
        self.scanned_all_mods = False
        self.found_game = False
        if game_folder:
            for r in GAME_ROOTS:
                root = os.path.join(game_folder, r)
                if os.path.isdir(root):
                    _load_root(root, self.shapes, "game")
            self.found_game = bool(self.shapes)
        for u, d in FALLBACK.items():
            if u not in self.shapes:
                info = ShapeInfo(u, d["kind"])
                for k in ("title", "role", "collision", "size", "axis", "density", "material"):
                    if k in d:
                        setattr(info, k, d[k])
                info.source = "built-in"
                self.shapes[u] = info
        self.lock = threading.Lock()

    def get(self, uuid):
        return self.shapes.get((uuid or "").lower())

    def mod_folders(self):
        out = []
        ws = workshop_folder(self.game_folder)
        if ws:
            out += glob.glob(os.path.join(ws, "*"))
        for uf in user_folders(self.user_folder):
            out += glob.glob(os.path.join(uf, "Mods", "*"))
        return [p for p in out if os.path.isdir(os.path.join(p, "Objects"))]

    def load_mod(self, folder):
        if folder in self.loaded_mods:
            return 0
        self.loaded_mods.add(folder)
        return _load_root(folder, self.shapes, "mod:" + os.path.basename(folder))

    def resolve(self, uuids, dependencies=()):
        """Load mods until every uuid is known (or there is nothing left to look in). Returns unknown uuids."""
        with self.lock:
            missing = {u.lower() for u in uuids if u and u.lower() not in self.shapes}
            if not missing:
                return set()
            ws = workshop_folder(self.game_folder)
            for dep in dependencies or ():
                if not isinstance(dep, dict):
                    continue
                fid = str(dep.get("steamFileId") or dep.get("fileId") or "").strip()
                if ws and fid and fid != "0":
                    self.load_mod(os.path.join(ws, fid))
            missing = {u for u in missing if u not in self.shapes}
            if missing and not self.scanned_all_mods:
                self.scanned_all_mods = True
                for folder in self.mod_folders():
                    self.load_mod(folder)
                    if all(u in self.shapes for u in missing):
                        break
            return {u for u in missing if u not in self.shapes}


_DB = {}
_DB_LOCK = threading.Lock()


def shape_db(game_folder, user_folder=""):
    """One ShapeDB per game folder for the life of the editor."""
    with _DB_LOCK:
        db = _DB.get((game_folder, user_folder))
        if db is None:
            db = _DB[(game_folder, user_folder)] = ShapeDB(game_folder, user_folder)
        return db


def forget():
    with _DB_LOCK:
        _DB.clear()
