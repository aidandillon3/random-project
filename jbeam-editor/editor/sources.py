"""Where vehicle files come from: mod folders, mod zips and the game's zips.

A Root is one folder or zip that has a vehicles/ folder inside. Paths inside a
root are always forward-slash and compared in lower case, like the game does.
Only folders outside the game install are editable.
"""
import json
import os
import zipfile

TEXT_EXTS = (".jbeam", ".pc", ".json")


def norm(p):
    return p.replace("\\", "/").lower()


class Root:
    def __init__(self, kind, path, label, editable, origin):
        self.kind = kind          # "folder" or "zip"
        self.path = path
        self.label = label        # what the UI shows, e.g. the mod name
        self.editable = editable
        self.origin = origin      # "unpacked", "mod zip", "game", "folder"
        self.id = "%s:%s" % (kind, os.path.normcase(os.path.abspath(path)))
        self._names = None        # lower relpath -> real relpath
        self._zip = None

    def __repr__(self):
        return "<Root %s %s>" % (self.origin, self.path)

    # -- listing ----------------------------------------------------------
    def names(self):
        if self._names is None:
            names = {}
            if self.kind == "zip":
                try:
                    with zipfile.ZipFile(self.path) as z:
                        for n in z.namelist():
                            if not n.endswith("/"):
                                names[norm(n)] = n
                except (OSError, zipfile.BadZipFile):
                    pass
            else:
                vdir = os.path.join(self.path, "vehicles")
                for base, dirs, files in os.walk(vdir):
                    dirs[:] = [d for d in dirs if not d.startswith(".")]
                    for f in files:
                        full = os.path.join(base, f)
                        rel = os.path.relpath(full, self.path).replace("\\", "/")
                        names[norm(rel)] = rel
            self._names = names
        return self._names

    def refresh(self):
        self._names = None
        self.close()

    def models(self):
        out = set()
        for n in self.names():
            parts = n.split("/")
            if len(parts) >= 3 and parts[0] == "vehicles" and parts[1] != "common":
                out.add(parts[1])
        return out

    def files_under(self, prefix):
        prefix = norm(prefix).rstrip("/") + "/"
        return [real for low, real in self.names().items() if low.startswith(prefix)]

    def has(self, rel):
        return norm(rel) in self.names()

    # -- reading ----------------------------------------------------------
    def _zf(self):
        if self._zip is None:
            self._zip = zipfile.ZipFile(self.path)
        return self._zip

    def close(self):
        if self._zip is not None:
            self._zip.close()
            self._zip = None

    def read(self, rel):
        real = self.names().get(norm(rel), rel)
        if self.kind == "zip":
            return self._zf().read(real)
        with open(os.path.join(self.path, *real.split("/")), "rb") as f:
            return f.read()

    def stamp(self, rel):
        real = self.names().get(norm(rel), rel)
        if self.kind == "zip":
            info = self._zf().getinfo(real)
            return (info.CRC, info.file_size)
        st = os.stat(os.path.join(self.path, *real.split("/")))
        return (st.st_mtime_ns, st.st_size)

    def full_path(self, rel):
        """Disk path of a file (folders only)."""
        real = self.names().get(norm(rel), rel)
        return os.path.join(self.path, *real.split("/"))

    def zip_stamp(self):
        st = os.stat(self.path)
        return "%d-%d" % (st.st_mtime_ns, st.st_size)


# ---------------------------------------------------------------------------
# Finding BeamNG
# ---------------------------------------------------------------------------

def _steam_libraries():
    libs = []
    try:
        import winreg  # Windows only
        for hive, key in ((winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam"),
                          (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Valve\Steam")):
            try:
                with winreg.OpenKey(hive, key) as k:
                    for name in ("SteamPath", "InstallPath"):
                        try:
                            libs.append(winreg.QueryValueEx(k, name)[0])
                        except OSError:
                            pass
            except OSError:
                pass
    except ImportError:
        pass
    more = []
    for lib in libs:
        vdf = os.path.join(lib, "steamapps", "libraryfolders.vdf")
        try:
            with open(vdf, encoding="utf-8", errors="replace") as f:
                for line in f:
                    line = line.strip()
                    if line.startswith('"path"'):
                        more.append(line.split('"')[3].replace("\\\\", "\\"))
        except OSError:
            pass
    return libs + more


def find_game_folder():
    cands = [os.path.join(lib, "steamapps", "common", "BeamNG.drive") for lib in _steam_libraries()]
    cands.append(r"E:\Games folder\Steam library\steamapps\common\BeamNG.drive")
    cands.append(r"C:\Program Files (x86)\Steam\steamapps\common\BeamNG.drive")
    for c in cands:
        if os.path.isfile(os.path.join(c, "content", "vehicles", "common.zip")):
            return c
    return ""


def find_user_folder():
    local = os.environ.get("LOCALAPPDATA", "")
    cands = []
    if local:
        base = os.path.join(local, "BeamNG", "BeamNG.drive")
        cands.append(os.path.join(base, "current"))
        if os.path.isdir(base):
            vers = sorted((d for d in os.listdir(base) if d[:1].isdigit()), reverse=True)
            cands += [os.path.join(base, d) for d in vers]
        cands.append(os.path.join(local, "BeamNG.drive"))
    for c in cands:
        if os.path.isdir(os.path.join(c, "mods")) or os.path.isdir(os.path.join(c, "vehicles")):
            return c
    return ""


# ---------------------------------------------------------------------------
# All roots
# ---------------------------------------------------------------------------

def _inactive_mods(user):
    """Mod names switched off in the game's mod manager (mods/db.json)."""
    off = set()
    try:
        with open(os.path.join(user, "mods", "db.json"), encoding="utf-8") as f:
            db = json.load(f)
        for name, m in (db.get("mods") or {}).items():
            if isinstance(m, dict) and m.get("active") is False:
                off.add(name.lower())
                fp = m.get("fullpath") or ""
                if fp:
                    off.add(os.path.splitext(os.path.basename(fp))[0].lower())
    except (OSError, ValueError, AttributeError):
        pass
    return off


def discover(cfg):
    """All roots in load priority: unpacked mods, extra folders, mod zips, game."""
    roots = []
    user = cfg.get("user_folder") or ""
    game = cfg.get("game_folder") or ""
    inactive = _inactive_mods(user) if user else set()

    if user:
        unpacked = os.path.join(user, "mods", "unpacked")
        if os.path.isdir(unpacked):
            for d in sorted(os.listdir(unpacked)):
                full = os.path.join(unpacked, d)
                if os.path.isdir(os.path.join(full, "vehicles")):
                    r = Root("folder", full, d, True, "unpacked")
                    r.inactive = d.lower() in inactive
                    roots.append(r)
        # loose files directly in the user folder (old style)
        if os.path.isdir(os.path.join(user, "vehicles")):
            roots.append(Root("folder", user, "user folder", True, "folder"))
    for extra in cfg.get("extra_folders") or []:
        if os.path.isdir(os.path.join(extra, "vehicles")):
            roots.append(Root("folder", extra, os.path.basename(extra.rstrip("/\\")), True, "folder"))
    if user:
        for sub in ("mods", os.path.join("mods", "repo")):
            mdir = os.path.join(user, sub)
            if os.path.isdir(mdir):
                for f in sorted(os.listdir(mdir)):
                    if f.lower().endswith(".zip"):
                        r = Root("zip", os.path.join(mdir, f), f[:-4], False, "mod zip")
                        r.inactive = f[:-4].lower() in inactive
                        roots.append(r)
    if game:
        vdir = os.path.join(game, "content", "vehicles")
        if os.path.isdir(vdir):
            for f in sorted(os.listdir(vdir)):
                if f.lower().endswith(".zip"):
                    roots.append(Root("zip", os.path.join(vdir, f), "game: " + f[:-4], False, "game"))
    for r in roots:
        if not hasattr(r, "inactive"):
            r.inactive = False
    return roots


def inside(path, folder):
    if not folder:
        return False
    a = os.path.normcase(os.path.abspath(path))
    b = os.path.normcase(os.path.abspath(folder))
    return a == b or a.startswith(b.rstrip("\\/") + os.sep)
