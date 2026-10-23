"""The editor's local web server: serves the page and a small JSON API."""
import base64
import gzip
import json
import mimetypes
import os
import re
import socket
import subprocess
import sys
import threading
import time
import traceback
import zipfile
from array import array
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from . import appwindow, build, daewrite, garage, jbeam_check, jbeamfile, meshes, model, patch, smimport, sources, \
    vehiclegen
from .smimport import game as smgame
from .vehiclegen import files as vgfiles, spec as vgspec

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WEB = os.path.join(HERE, "web")
APP_ID = "jbeam-editor"
VERSION = "2.0"


class App:
    def __init__(self, data_dir):
        self.data_dir = data_dir
        os.makedirs(data_dir, exist_ok=True)
        self.cfg_path = os.path.join(data_dir, "config.json")
        self.cfg = self._load_cfg()
        self.ws = model.Workspace(self.cfg, os.path.join(data_dir, "cache"))
        self.meshes = meshes.MeshLibrary(self.ws, os.path.join(data_dir, "cache"))
        self.lock = threading.RLock()
        self.lifetime = None      # appwindow.Lifetime when the editor runs in its own window
        self.window_mode = None   # how the window was opened
        self.scrap = smimport.Importer()
        self._scrap_items = {}

    # -- config --------------------------------------------------------------
    def _load_cfg(self):
        cfg = {}
        try:
            with open(self.cfg_path, encoding="utf-8") as f:
                cfg = json.load(f)
        except (OSError, ValueError):
            pass
        if not cfg.get("user_folder"):
            cfg["user_folder"] = sources.find_user_folder()
        if not cfg.get("game_folder"):
            cfg["game_folder"] = sources.find_game_folder()
        cfg.setdefault("extra_folders", [])
        cfg.setdefault("author", "")
        cfg.setdefault("window", "auto")
        return cfg

    def _save_cfg(self):
        with open(self.cfg_path, "w", encoding="utf-8") as f:
            json.dump(self.cfg, f, indent=2)

    # -- API -----------------------------------------------------------------
    def api_state(self, req):
        with self.lock:
            return {
                "config": self.cfg,
                "vehicles": self.ws.vehicle_list(),
                "roots": [{"id": r.id, "label": r.label, "origin": r.origin, "editable": r.editable,
                           "inactive": r.inactive, "path": r.path} for r in self.ws.roots if r.origin != "game"],
                "gameFound": bool(self.cfg.get("game_folder")) and os.path.isdir(self.cfg.get("game_folder")),
                "userFound": bool(self.cfg.get("user_folder")) and os.path.isdir(self.cfg.get("user_folder")),
                "dataDir": self.data_dir,
                "version": VERSION,
                "window": {"mode": self.window_mode, **appwindow.available()},
                "platform": sys.platform,
            }

    def thumb(self, model):
        """(bytes, content type) of a vehicle's picture (vehicles/<model>/default.jpg or .png)."""
        with self.lock:
            for r in self.ws.roots:
                if r.inactive:
                    continue
                for name in ("default.jpg", "default.png", "default.jpeg"):
                    rel = "vehicles/%s/%s" % (model, name)
                    if r.has(rel):
                        try:
                            data = r.read(rel)
                        except (OSError, KeyError):
                            continue
                        return data, "image/png" if name.endswith(".png") else "image/jpeg"
        return None, None

    def api_ping(self, req):
        return {"app": APP_ID, "version": VERSION, "dataDir": os.path.abspath(self.data_dir)}

    def api_config(self, req):
        with self.lock:
            if req.get("window") in appwindow.WINDOW_MODES:
                self.cfg["window"] = req["window"]
            for k in ("user_folder", "game_folder", "author", "sm_folder", "sm_user_folder"):
                if k in req:
                    self.cfg[k] = (req[k] or "").strip()
            if "extra_folders" in req:
                self.cfg["extra_folders"] = [x.strip() for x in req["extra_folders"] if x and x.strip()]
            self._save_cfg()
            self.ws.cfg = self.cfg
            self.ws.refresh()
            self.meshes.forget()
        return self.api_state({})

    def api_open(self, req):
        with self.lock:
            m = self.ws.model(req["model"], reload=bool(req.get("reload")))
            if req.get("reload"):
                self.meshes.forget(req["model"])
            return m.summary()

    def _config_from(self, m, req):
        cfg = req.get("config")
        if cfg is None and req.get("configName"):
            cfg = m.read_config(req["configName"])
        return cfg or {}

    def api_build(self, req):
        with self.lock:
            m = self.ws.model(req["model"])
            cfg = self._config_from(m, req)
            spec = req.get("garage")
            if spec is not None:
                scene, info, plan, base, final_cfg = garage.preview(m, cfg, spec)
                scene["config"] = _config_out(cfg, base)
                scene["garage"] = {"info": info.to_json(), "changes": plan.changes, "notes": plan.notes,
                                   "finalConfig": _config_out(final_cfg, scene)}
                return scene
            scene = build.build(m, cfg)
            scene["config"] = _config_out(cfg, scene)
            return scene

    def _mod_root(self, req, model_name):
        """An editable mod folder from req["root"] (an id) or req["newMod"] (a name to create)."""
        root = self.ws.root_by_id(req.get("root") or "")
        if root is not None:
            if not root.editable:
                raise ValueError("that folder can't be written to")
            return root
        user = self.cfg.get("user_folder")
        if not user or not os.path.isdir(user):
            raise ValueError("set your BeamNG user folder in Settings first")
        name = re.sub(r"[^A-Za-z0-9_\-]", "_", req.get("newMod") or "").strip("_") or "my_%s_versions" % model_name
        dest = os.path.join(user, "mods", "unpacked", name)
        os.makedirs(os.path.join(dest, "vehicles", model_name), exist_ok=True)
        self.ws.refresh()
        self.meshes.forget()
        for r in self.ws.roots:
            if r.kind == "folder" and os.path.normcase(r.path) == os.path.normcase(dest):
                return r
        raise ValueError("could not make the mod folder %s" % dest)

    def api_garage_save(self, req):
        """Write the Garage's parts and a config (with its info file and picture) into a mod."""
        with self.lock:
            model_name = req["model"]
            root = self._mod_root(req, model_name)
            m = self.ws.model(model_name, reload=True)
            res = garage.write(m, root, req.get("configName"), req.get("garage") or {}, req.get("config") or {},
                               backup_dir=os.path.join(self.data_dir, "backups"))
            m = self.ws.model(model_name, reload=True)
            scene = build.build(m, res["finalConfig"])
            cfg = _config_out(res["finalConfig"], scene)
            paints = (req.get("garage") or {}).get("paints")
            if isinstance(paints, list) and paints:
                cfg["paints"] = paints
            vdir = os.path.join(root.path, "vehicles", model_name)
            pc = os.path.join(vdir, res["config"] + ".pc")
            if os.path.exists(pc):
                bdir = os.path.join(self.data_dir, "backups", root.label)
                os.makedirs(bdir, exist_ok=True)
                with open(pc, "rb") as f, open(os.path.join(bdir, res["config"] + ".pc.bak"), "wb") as g:
                    g.write(f.read())
            with open(pc, "w", encoding="utf-8") as f:
                f.write(jbeamfile.dumps_json(cfg))
            title = (req.get("title") or res["config"]).strip()
            info = {"Configuration": title, "Config Type": "Custom", "Source": "Custom",
                    "Description": "Made in the JBeam Editor Garage. " + "; ".join(res["changes"])}
            with open(os.path.join(vdir, "info_%s.json" % res["config"]), "w", encoding="utf-8") as f:
                f.write(jbeamfile.dumps_json(info))
            pic = req.get("picture") or ""
            if pic.startswith("data:image/jpeg;base64,"):
                with open(os.path.join(vdir, res["config"] + ".jpg"), "wb") as f:
                    f.write(base64.b64decode(pic.split(",", 1)[1]))
            root.refresh()
            m = self.ws.model(model_name, reload=True)
            return {"pc": pc, "jbeam": res["jbeam"], "configName": res["config"], "changes": res["changes"],
                    "notes": res["notes"], "summary": m.summary(), "root": root.id, "folder": root.path}

    # -- Vehicle Builder ------------------------------------------------------------
    def api_builder_schema(self, req):
        return vgspec.schema()

    def _model_owner(self, model_name):
        """(root that has vehicles/<model>/, is it a Builder vehicle) for an existing vehicle id."""
        r, made = self._made_by(model_name)
        return r, made == "builder"

    def _made_by(self, model_name):
        """(root that has vehicles/<model>/, "builder" / "scrap" / None: which of the editor's tools made it)."""
        for r in self.ws.roots:
            if r.inactive:
                continue
            if r.files_under("vehicles/%s" % model_name):
                if vgfiles.read_design(r, model_name) is not None:
                    return r, "builder"
                if r.has("vehicles/%s/%s" % (model_name, smimport.DESIGN_FILE)):
                    return r, "scrap"
                return r, None
        return None, None

    def api_builder_preview(self, req):
        """Generate the design and build it like any vehicle; meshes the page has already are left out."""
        g = vehiclegen.generate(req.get("spec") or {}, author=self.cfg.get("author"))
        with self.lock:
            m = model.Model.from_parts(self.ws, g.P.model, g.parts)
            scene = build.build(m, {})
            owner, is_builder = self._model_owner(g.P.model)
        keys, mesh_data = vehiclegen.preview_payload(g, req.get("have"))
        for fb in scene["flexbodies"]:
            if fb["mesh"] in keys:
                fb["mesh"] = "%s@%s" % (fb["mesh"], keys[fb["mesh"]])
        scene["config"] = _config_out({}, scene)
        return {"scene": scene, "meshes": mesh_data, "materials": g.materials, "stats": g.stats, "notes": g.notes,
                "spec": g.spec, "model": g.P.model,
                "exists": {"taken": owner is not None, "builder": is_builder,
                           "where": owner.label if owner is not None else None}}

    def api_builder_create(self, req):
        """Write the design as a vehicle mod (or update a vehicle the Builder made before)."""
        g = vehiclegen.generate(req.get("spec") or {}, author=self.cfg.get("author"))
        model_name = g.P.model
        with self.lock:
            user = self.cfg.get("user_folder")
            if not user or not os.path.isdir(user):
                raise ValueError("set your BeamNG user folder in Settings first")
            owner, is_builder = self._model_owner(model_name)
            if owner is not None and not (is_builder and owner.editable):
                raise ValueError('there is already a vehicle called "%s" (in %s); pick another name'
                                 % (model_name, owner.label))
            if owner is not None:
                mod_dir = owner.path
            else:
                mod = re.sub(r"[^A-Za-z0-9_\-]", "_", req.get("modName") or model_name).strip("_") or model_name
                mod_dir = os.path.join(user, "mods", "unpacked", mod)
            vdir = os.path.join(mod_dir, "vehicles", model_name)
            pic = req.get("picture") or ""
            picture = None
            if pic.startswith("data:image/") and "," in pic:
                picture = base64.b64decode(pic.split(",", 1)[1])
            backup = os.path.join(self.data_dir, "backups", "builder", "%s-%s" % (model_name, time.strftime("%Y%m%d-%H%M%S")))
            written = vgfiles.write(g, vdir, picture=picture, backup_dir=backup if owner is not None else None)
            self.ws.refresh()
            self.meshes.forget()
            return {"model": model_name, "folder": mod_dir, "path": vdir, "written": written,
                    "updated": owner is not None, "stats": g.stats}

    def api_builder_load(self, req):
        """The saved design of a vehicle the Builder made."""
        with self.lock:
            owner, is_builder = self._model_owner(req["model"])
            if owner is None or not is_builder:
                raise ValueError("%s was not made with the Vehicle Builder" % req["model"])
            spec = vgfiles.read_design(owner, req["model"])
        out = vgspec.clean(spec)
        for k in ("name", "brand", "model"):
            if isinstance(spec.get(k), str):
                out[k] = spec[k]
        return {"spec": out, "editable": owner.editable, "where": owner.label}

    # -- Scrap Mechanic -------------------------------------------------------------------------
    def _sm_folder(self):
        return smgame.find_game_folder(self.cfg.get("sm_folder") or "")

    def api_scrap_list(self, req):
        """The Scrap Mechanic creations on this computer."""
        gf = self._sm_folder()
        if req.get("refresh"):
            self.scrap.forget()
        items = self.scrap.creations(gf, self.cfg.get("sm_user_folder") or "")
        with self.lock:
            self._scrap_items = {e["id"]: e for e in items}
        out = [{"id": e["id"], "name": e["name"], "description": e["description"], "source": e["source"],
                "mtime": e["mtime"], "icon": bool(e["icon"])} for e in items]
        return {"game": {"found": bool(gf), "folder": gf, "workshop": smgame.workshop_folder(gf),
                         "userFolders": smgame.user_folders(self.cfg.get("sm_user_folder") or ""),
                         "configured": self.cfg.get("sm_folder") or ""},
                "creations": out}

    def _scrap_entry(self, cid=None, path=None):
        """The creation an id (from the list) or a path to a blueprint (file or folder) names."""
        if path:
            p = os.path.abspath(os.path.expanduser(str(path).strip().strip('"')))
            if os.path.isdir(p):
                p = os.path.join(p, "blueprint.json")
            if os.path.basename(p).lower() != "blueprint.json" or not os.path.isfile(p):
                raise ValueError("that is not a Scrap Mechanic creation: pick its blueprint.json (or the folder it is in)")
            e = smgame.creation_entry(os.path.dirname(p), "file") or {}
            e = dict(e, id="file:" + p, path=p, source="file")
            with self.lock:
                self._scrap_items[e["id"]] = e
            return e
        if cid and cid.startswith("file:"):
            return self._scrap_entry(path=cid[5:])
        with self.lock:
            e = self._scrap_items.get(cid or "")
        if e is None:
            self.api_scrap_list({})
            with self.lock:
                e = self._scrap_items.get(cid or "")
        if e is None:
            raise ValueError("that creation was not found any more (was it deleted in Scrap Mechanic?)")
        return e

    def api_scrap_entry(self, req):
        e = self._scrap_entry(req.get("id"), req.get("path"))
        return {"id": e["id"], "name": e.get("name") or "", "source": e.get("source"), "icon": bool(e.get("icon"))}

    def scrap_icon(self, cid):
        try:
            e = self._scrap_entry(cid)
        except ValueError:
            return None, None
        icon = e.get("icon")
        if not icon:
            return None, None
        try:
            with open(icon, "rb") as f:
                data = f.read()
        except OSError:
            return None, None
        return data, "image/png" if icon.lower().endswith(".png") else "image/jpeg"

    def _scrap_generate(self, req):
        e = self._scrap_entry(req.get("id"), req.get("path"))
        source = {"id": e["id"], "path": e["path"], "name": e.get("name") or ""}
        return e, self.scrap.generate(e["path"], self._sm_folder(), req.get("settings") or {},
                                      author=self.cfg.get("author"), source=source, name=e.get("name") or "",
                                      user_folder=self.cfg.get("sm_user_folder") or "")

    def api_scrap_preview(self, req):
        """The creation converted with the given settings, built like any vehicle, for the 3D view."""
        e, g = self._scrap_generate(req)
        with self.lock:
            m = model.Model.from_parts(self.ws, g.P.model, g.parts, label="Scrap Mechanic import")
            scene = build.build(m, {})
            owner, made = self._made_by(g.P.model)
        keys, mesh_data = vehiclegen.preview_payload(g, req.get("have"))
        for fb in scene["flexbodies"]:
            if fb["mesh"] in keys:
                fb["mesh"] = "%s@%s" % (fb["mesh"], keys[fb["mesh"]])
        scene["config"] = _config_out({}, scene)
        an = self.scrap.analysis(e["path"], self._sm_folder(), e.get("name") or "", self.cfg.get("sm_user_folder") or "")
        return {"scene": scene, "meshes": mesh_data, "materials": g.materials, "stats": g.stats, "notes": g.notes,
                "settings": g.settings, "schema": smimport.schema(an), "summary": g.summary, "model": g.P.model,
                "creation": {"id": e["id"], "name": e.get("name") or "", "source": e.get("source"),
                             "icon": bool(e.get("icon"))},
                "exists": {"taken": owner is not None, "scrap": made == "scrap",
                           "where": owner.label if owner is not None else None}}

    def api_scrap_create(self, req):
        """Write the converted creation as a vehicle mod (or update one imported before)."""
        e, g = self._scrap_generate(req)
        model_name = g.P.model
        with self.lock:
            user = self.cfg.get("user_folder")
            if not user or not os.path.isdir(user):
                raise ValueError("set your BeamNG user folder in Settings first")
            owner, made = self._made_by(model_name)
            if owner is not None and not (made == "scrap" and owner.editable):
                raise ValueError('there is already a vehicle called "%s" (in %s); pick another name'
                                 % (model_name, owner.label))
            if owner is not None:
                mod_dir = owner.path
            else:
                mod = re.sub(r"[^A-Za-z0-9_\-]", "_", req.get("modName") or model_name).strip("_") or model_name
                mod_dir = os.path.join(user, "mods", "unpacked", mod)
            vdir = os.path.join(mod_dir, "vehicles", model_name)
            pic = req.get("picture") or ""
            picture = None
            if pic.startswith("data:image/") and "," in pic:
                picture = base64.b64decode(pic.split(",", 1)[1])
            backup = os.path.join(self.data_dir, "backups", "scrap", "%s-%s" % (model_name, time.strftime("%Y%m%d-%H%M%S")))
            written = vgfiles.write(g, vdir, picture=picture, backup_dir=backup if owner is not None else None)
            self.ws.refresh()
            self.meshes.forget()
            return {"model": model_name, "folder": mod_dir, "path": vdir, "written": written,
                    "updated": owner is not None, "stats": g.stats}

    def api_scrap_load(self, req):
        """Where a vehicle imported from Scrap Mechanic came from, and the settings it was made with."""
        with self.lock:
            owner, made = self._made_by(req["model"])
            if owner is None or made != "scrap":
                raise ValueError("%s was not imported from Scrap Mechanic" % req["model"])
            data = jbeamfile.loads(jbeamfile.decode(owner.read("vehicles/%s/%s" % (req["model"], smimport.DESIGN_FILE))))
        src = data.get("source") if isinstance(data, dict) else None
        st = data.get("settings") if isinstance(data, dict) else None
        if not isinstance(src, dict) or not isinstance(st, dict):
            raise ValueError("the import settings of %s are unreadable" % req["model"])
        found = bool(src.get("path")) and os.path.isfile(src["path"])
        return {"source": src, "settings": st, "found": found, "editable": owner.editable, "where": owner.label}

    def api_save(self, req):
        with self.lock:
            m = self.ws.model(req["model"])
            s = patch.Session(m, os.path.join(self.data_dir, "backups"))
            try:
                for i, op in enumerate(req.get("ops") or []):
                    try:
                        s.apply(op)
                    except patch.EditError as e:
                        raise patch.EditError("edit %d of %d (%s): %s" % (i + 1, len(req["ops"]), op.get("op"), e))
                written = s.commit()
            except patch.EditError as e:
                return {"ok": False, "error": str(e)}
            m = self.ws.model(req["model"], reload=True)
            if any(p.lower().endswith(".dae") for p in written):
                self.meshes.forget(req["model"])
            out = {"ok": True, "written": written, "summary": m.summary()}
            if req.get("config") is not None:
                scene = build.build(m, req["config"])
                scene["config"] = _config_out(req["config"], scene)
                out["scene"] = scene
            return out

    def api_part_text(self, req):
        with self.lock:
            m = self.ws.model(req["model"])
            part = m.parts.get(req["part"])
            if part is None:
                raise ValueError("part not found")
            text = jbeamfile.decode(part.file.root.read(part.file.rel))
            root = jbeamfile.parse_spans(text)
            node = root.get(part.name)
            if node is None:
                raise ValueError("part not found in its file")
            line = text.count("\n", 0, node.start) + 1
            return {"text": text[node.start:node.end], "file": part.file.to_json(), "line": line,
                    "editable": part.file.root.editable}

    def api_pc_save(self, req):
        with self.lock:
            m = self.ws.model(req["model"])
            root = self.ws.root_by_id(req["root"])
            if root is None or not root.editable:
                raise ValueError("pick a mod folder to save into")
            name = re.sub(r"[^A-Za-z0-9 _.\-]", "_", req["name"]).strip() or "config"
            path = os.path.join(root.path, "vehicles", m.name, name + ".pc")
            cfg = dict(req["config"])
            cfg.setdefault("format", 2)
            cfg["model"] = m.name
            if os.path.exists(path):
                bdir = os.path.join(self.data_dir, "backups", root.label)
                os.makedirs(bdir, exist_ok=True)
                with open(path, "rb") as f, open(os.path.join(bdir, name + ".pc.bak"), "wb") as g:
                    g.write(f.read())
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                f.write(jbeamfile.dumps_json(cfg))
            m = self.ws.model(m.name, reload=True)
            return {"path": path, "summary": m.summary()}

    def api_check(self, req):
        with self.lock:
            m = self.ws.model(req["model"])
            root = self.ws.root_by_id(req.get("root") or "") or (m.editable_roots() or [None])[0]
            if root is None:
                raise ValueError("no editable mod folder for this vehicle")
            c = jbeam_check.Checker()
            target = os.path.join(root.path, "vehicles", m.name) if root.kind == "folder" else root.path
            for p in m.parts.values():
                if p.file.root is not root:
                    c.load_jbeam(p.file.rel, {p.name: p.data}, True)
            c.load(target)
            c.finish()
            return {"root": root.label, "parts": len(c.parts), "errors": c.errors, "warnings": c.warnings}

    def api_meshes(self, req):
        return self.meshes.pack(req["model"], req.get("names") or [])

    def api_new_part(self, req):
        """Create a part in a new or existing .jbeam file of a mod folder.

        mode "copy": copy an existing part's text under a new name.
        mode "data": write the part from a JSON object (built by the page)."""
        with self.lock:
            m = self.ws.model(req["model"])
            root = self.ws.root_by_id(req["root"])
            if root is None or not root.editable:
                raise ValueError("pick a mod folder to save into")
            name = req["name"].strip()
            if not re.match(r"^[A-Za-z0-9_\-.]+$", name):
                raise ValueError("part names may only use letters, digits, _ - and .")
            if name in m.parts:
                raise ValueError("a part called %s already exists" % name)
            rel = req.get("rel") or "vehicles/%s/%s.jbeam" % (m.name, name)
            rel = rel.replace("\\", "/").lstrip("/")
            if req.get("mode") == "copy":
                src = m.parts.get(req["source"])
                if src is None:
                    raise ValueError("part %s not found" % req["source"])
                text = jbeamfile.decode(src.file.root.read(src.file.rel))
                node = jbeamfile.parse_spans(text).get(src.name)
                body = text[node.start:node.end]
            else:
                body = None
            s = patch.Session(m, os.path.join(self.data_dir, "backups"))
            path = os.path.join(root.path, *rel.split("/"))
            if os.path.exists(path):
                fr = model.FileRef(root, rel)
                m.files.setdefault(fr.key, fr)
                op = {"op": "part.add", "f": fr.key, "name": name}
                if body is not None:
                    op["text"] = '"%s": %s,' % (name, body)
                else:
                    op["data"] = req["data"]
                s.apply(op)
            else:
                if body is not None:
                    text = '{\n"%s": %s,\n}\n' % (name, body)
                else:
                    text = jbeamfile.dumps_part_file({name: req["data"]}, comment=req.get("comment"))
                s.apply({"op": "file.new", "root": root.id, "rel": rel, "text": text})
            written = s.commit()
            # set the display name and slot type of a copied part
            if body is not None:
                root.refresh()
                m = self.ws.model(req["model"], reload=True)
                p = m.parts.get(name)
                if p is not None:
                    s = patch.Session(m, os.path.join(self.data_dir, "backups"))
                    if req.get("title"):
                        s.apply({"op": "part.set", "f": p.file.key, "part": name, "path": ["information", "name"],
                                 "value": req["title"]})
                    if req.get("slotType"):
                        s.apply({"op": "part.set", "f": p.file.key, "part": name, "path": ["slotType"],
                                 "value": req["slotType"]})
                    s.commit()
            root.refresh()
            m = self.ws.model(req["model"], reload=True)
            return {"written": written, "summary": m.summary(), "part": name}

    def api_new_mod(self, req):
        """Make a new unpacked mod folder for a vehicle (for add-on parts)."""
        with self.lock:
            user = self.cfg.get("user_folder")
            if not user or not os.path.isdir(user):
                raise ValueError("set your BeamNG user folder in Settings first")
            name = re.sub(r"[^A-Za-z0-9_\-]", "_", req.get("name") or "").strip("_")
            if not name:
                raise ValueError("give the mod a name")
            model_name = req["model"]
            dest = os.path.join(user, "mods", "unpacked", name)
            vdir = os.path.join(dest, "vehicles", model_name)
            os.makedirs(vdir, exist_ok=True)
            jb = os.path.join(vdir, name + ".jbeam")
            if not os.path.exists(jb):
                with open(jb, "w", encoding="utf-8") as f:
                    f.write("{\n}\n")
            self.ws.refresh()
            self.meshes.forget()
            m = self.ws.model(model_name, reload=True)
            root = next((r for r in self.ws.roots if r.kind == "folder" and os.path.normcase(r.path) ==
                         os.path.normcase(dest)), None)
            return {"path": dest, "root": root.id if root else None, "summary": m.summary()}

    def api_import_mesh(self, req):
        """Write an imported mesh into the mod as a .dae plus its material."""
        with self.lock:
            m = self.ws.model(req["model"])
            root = self.ws.root_by_id(req["root"])
            if root is None or not root.editable:
                raise ValueError("pick a mod folder to save into")
            name = re.sub(r"[^A-Za-z0-9_]", "_", req["name"]).strip("_") or "imported_mesh"
            pos = array("f")
            pos.frombytes(base64.b64decode(req["positions"]))
            idx = array("I")
            idx.frombytes(base64.b64decode(req["indices"]))
            uvs = None
            if req.get("uvs"):
                uvs = array("f")
                uvs.frombytes(base64.b64decode(req["uvs"]))
            material = re.sub(r"[^A-Za-z0-9_]", "_", req.get("material") or (name + "_mat"))
            vdir = os.path.join(root.path, "vehicles", m.name, "jbeam_editor_meshes")
            os.makedirs(vdir, exist_ok=True)
            dae_path = os.path.join(vdir, name + ".dae")
            if os.path.exists(dae_path) and not req.get("overwrite"):
                raise ValueError("a mesh called %s already exists; pick another name" % name)
            mat_path = os.path.join(vdir, "jbeam_editor.materials.json")
            # several colours: [{"color", "paint", "indices"}], each its own material
            groups = []
            for k, g in enumerate(req.get("groups") or []):
                gi = array("I")
                gi.frombytes(base64.b64decode(g["indices"]))
                groups.append(("%s_%d" % (material, k + 1), list(gi), g.get("color") or [0.6, 0.6, 0.6], bool(g.get("paint"))))
            if not groups:
                groups = [(material, list(idx), req.get("color") or [0.6, 0.6, 0.6], bool(req.get("paint")))]
            for mname, _, color, paint in groups:
                mat_text = daewrite.add_material(mat_path, mname, color, paint)
                with open(mat_path, "w", encoding="utf-8") as f:
                    f.write(mat_text)
            with open(dae_path, "w", encoding="utf-8") as f:
                f.write(daewrite.dae_text(name, material, list(pos), list(idx), list(uvs) if uvs else None,
                                          groups=[(g[0], g[1]) for g in groups]))
            for r in self.ws.roots:
                if r is root:
                    r.refresh()
            self.meshes.forget(m.name)
            return {"mesh": name, "material": material, "path": dae_path}

    def api_export(self, req):
        with self.lock:
            root = self.ws.root_by_id(req["root"])
            if root is None or root.kind != "folder":
                raise ValueError("pick an unpacked mod folder to pack")
            base = re.sub(r"[^A-Za-z0-9_\-]", "_", req.get("name") or root.label) or "my_mod"
            out_dir = os.path.join(self.data_dir, "exports")
            os.makedirs(out_dir, exist_ok=True)
            out = os.path.join(out_dir, base + ".zip")
            n = 0
            with zipfile.ZipFile(out + ".tmp", "w", zipfile.ZIP_DEFLATED) as z:
                for dirpath, dirs, files in os.walk(root.path):
                    dirs[:] = [d for d in dirs if not d.startswith(".")]
                    for fn in files:
                        if fn.endswith(".jbeam-editor-tmp"):
                            continue
                        full = os.path.join(dirpath, fn)
                        rel = os.path.relpath(full, root.path).replace("\\", "/")
                        z.write(full, rel)
                        n += 1
            os.replace(out + ".tmp", out)
            return {"path": out, "files": n, "folder": out_dir}

    def api_unpack(self, req):
        with self.lock:
            root = self.ws.root_by_id(req["root"])
            user = self.cfg.get("user_folder")
            if root is None or root.kind != "zip" or root.origin != "mod zip":
                raise ValueError("only mod zips from your mods folder can be unpacked")
            if not user:
                raise ValueError("set your BeamNG user folder first")
            dest = os.path.join(user, "mods", "unpacked", root.label)
            if os.path.exists(dest):
                raise ValueError("%s already exists" % dest)
            with zipfile.ZipFile(root.path) as z:
                for info in z.infolist():
                    parts = info.filename.replace("\\", "/").split("/")
                    if ".." in parts or info.filename.startswith("/"):
                        continue
                    z.extract(info, dest)
            self.ws.refresh()
            self.meshes.forget()
            return {"path": dest}

    def api_shortcut(self, req):
        """Put "JBeam Editor" shortcuts on the desktop and in the Start menu (Windows)."""
        if not sys.platform.startswith("win"):
            raise ValueError("shortcuts can only be made on Windows; start it with: python run.py")
        exe_dir = os.path.dirname(sys.executable)
        target = os.path.join(exe_dir, "pythonw.exe")
        if not os.path.isfile(target):
            target = sys.executable
        env = dict(os.environ, JBE_TARGET=target, JBE_ARGS='"%s"' % os.path.join(HERE, "run.py"),
                   JBE_DIR=HERE, JBE_ICON=os.path.join(WEB, "img", "icon.ico"))
        script = (
            "$ws = New-Object -ComObject WScript.Shell; $made = @();"
            "foreach ($f in @([Environment]::GetFolderPath('Desktop'), [Environment]::GetFolderPath('Programs'))) {"
            "  if (-not $f) { continue };"
            "  $p = Join-Path $f 'JBeam Editor.lnk'; $s = $ws.CreateShortcut($p);"
            "  $s.TargetPath = $env:JBE_TARGET; $s.Arguments = $env:JBE_ARGS; $s.WorkingDirectory = $env:JBE_DIR;"
            "  $s.IconLocation = $env:JBE_ICON; $s.Description = 'JBeam Editor for BeamNG.drive'; $s.Save(); $made += $p };"
            "$made -join [char]10")
        r = subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", script],
                           env=env, capture_output=True, text=True, timeout=60,
                           creationflags=0x08000000)
        if r.returncode != 0:
            raise ValueError("PowerShell could not make the shortcut: " + (r.stderr or r.stdout).strip()[:400])
        return {"made": [x for x in r.stdout.splitlines() if x.strip()]}

    _install = {"state": "idle", "log": ""}

    def api_install_native(self, req):
        """Install pywebview (the optional native window) with pip, in the background."""
        st = App._install
        if req.get("start") and st["state"] != "running":
            st.update(state="running", log="")

            def run():
                try:
                    r = subprocess.run([sys.executable, "-m", "pip", "install", "--user", "--upgrade", "pywebview"],
                                       capture_output=True, text=True, timeout=900,
                                       **({"creationflags": 0x08000000} if sys.platform.startswith("win") else {}))
                    st["log"] = (r.stdout + r.stderr)[-4000:]
                    st["state"] = "done" if r.returncode == 0 else "failed"
                except Exception as e:  # pip missing, no network...
                    st.update(state="failed", log=str(e))
            threading.Thread(target=run, daemon=True).start()
        return {"state": st["state"], "log": st["log"], "installed": appwindow.have_pywebview()}

    def api_log(self, req):
        """The editor's own log file (written when it runs without a console)."""
        path = os.path.join(self.data_dir, "editor.log")
        text = ""
        try:
            with open(path, encoding="utf-8", errors="replace") as f:
                text = f.read()[-20000:]
        except OSError:
            pass
        return {"path": path, "text": text}

    # -- Workshop projects (data/projects/*.json) ---------------------------------------------
    def _project_path(self, name):
        safe = re.sub(r"[^A-Za-z0-9 _.\-]", "_", name or "").strip(" .")
        if not safe:
            raise ValueError("give the project a name")
        d = os.path.join(self.data_dir, "projects")
        os.makedirs(d, exist_ok=True)
        return os.path.join(d, safe + ".json"), safe

    def api_projects(self, req):
        d = os.path.join(self.data_dir, "projects")
        out = []
        if os.path.isdir(d):
            for fn in os.listdir(d):
                if fn.endswith(".json"):
                    full = os.path.join(d, fn)
                    out.append({"name": fn[:-5], "modified": os.path.getmtime(full), "size": os.path.getsize(full)})
        out.sort(key=lambda x: -x["modified"])
        return {"projects": out}

    def api_project_save(self, req):
        path, name = self._project_path(req.get("name"))
        data = req.get("data")
        if not isinstance(data, dict):
            raise ValueError("nothing to save")
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f)
        os.replace(tmp, path)
        return {"name": name, "path": path}

    def api_project_load(self, req):
        path, name = self._project_path(req.get("name"))
        with open(path, encoding="utf-8") as f:
            return {"name": name, "data": json.load(f)}

    def api_project_delete(self, req):
        path, name = self._project_path(req.get("name"))
        if os.path.exists(path):
            os.remove(path)
        return {"name": name}

    def api_reveal(self, req):
        path = req.get("path") or ""
        if not os.path.exists(path):
            raise ValueError("not found: %s" % path)
        if sys.platform.startswith("win"):
            if os.path.isdir(path):
                os.startfile(path)  # noqa: S606 - opens Explorer on the user's own machine
            else:
                subprocess.Popen(["explorer", "/select,", os.path.normpath(path)])
        elif sys.platform == "darwin":
            subprocess.Popen(["open", "-R", path])
        else:
            subprocess.Popen(["xdg-open", os.path.dirname(path) if os.path.isfile(path) else path])
        return {"ok": True}


def _config_out(cfg, scene):
    """The config as the game would save it: every slot's chosen part."""
    parts = {}

    def walk(slot):
        for c in slot.get("children") or []:
            parts[c["id"]] = c.get("chosen") or ""
            walk(c)
    walk(scene["tree"])
    out = {"format": 2, "model": scene["model"], "mainPartName": scene["mainPart"], "parts": parts,
           "vars": {v["name"]: v["val"] for v in scene["variables"] if v.get("val") is not None}}
    for k in ("paints", "licenseName"):
        if isinstance(cfg, dict) and k in cfg:
            out[k] = cfg[k]
    return out


ROUTES = {
    "/api/ping": "api_ping",
    "/api/state": "api_state", "/api/config": "api_config", "/api/open": "api_open",
    "/api/build": "api_build", "/api/save": "api_save", "/api/part_text": "api_part_text",
    "/api/pc_save": "api_pc_save", "/api/check": "api_check", "/api/meshes": "api_meshes",
    "/api/import_mesh": "api_import_mesh", "/api/export": "api_export", "/api/unpack": "api_unpack",
    "/api/reveal": "api_reveal", "/api/new_part": "api_new_part", "/api/new_mod": "api_new_mod",
    "/api/garage_save": "api_garage_save",
    "/api/builder_schema": "api_builder_schema", "/api/builder_preview": "api_builder_preview",
    "/api/builder_create": "api_builder_create", "/api/builder_load": "api_builder_load",
    "/api/scrap_list": "api_scrap_list", "/api/scrap_entry": "api_scrap_entry", "/api/scrap_preview": "api_scrap_preview",
    "/api/scrap_create": "api_scrap_create", "/api/scrap_load": "api_scrap_load",
    "/api/projects": "api_projects", "/api/project_save": "api_project_save", "/api/project_load": "api_project_load",
    "/api/project_delete": "api_project_delete",
    "/api/shortcut": "api_shortcut", "/api/install_native": "api_install_native", "/api/log": "api_log",
}


def make_handler(app):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt, *args):
            pass

        def _send(self, code, body, ctype, cache=False):
            if isinstance(body, str):
                body = body.encode("utf-8")
            gz = len(body) > 20000 and "gzip" in (self.headers.get("Accept-Encoding") or "") \
                and not ctype.startswith("image/")
            if gz:
                body = gzip.compress(body, 5)
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "max-age=3600" if cache else "no-store")
            if gz:
                self.send_header("Content-Encoding", "gzip")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            path = urlparse(self.path).path
            if path == "/":
                path = "/index.html"
            if path == "/api/events":
                return self._events()
            if path == "/api/thumb":
                q = parse_qs(urlparse(self.path).query)
                data, ctype = app.thumb((q.get("model") or [""])[0])
                if data is None:
                    return self._send(404, "no picture", "text/plain")
                return self._send(200, data, ctype, cache=True)
            if path == "/api/scrap_icon":
                q = parse_qs(urlparse(self.path).query)
                data, ctype = app.scrap_icon((q.get("id") or [""])[0])
                if data is None:
                    return self._send(404, "no picture", "text/plain")
                return self._send(200, data, ctype, cache=True)
            if path.startswith("/api/"):
                return self._api(path, {})
            full = os.path.normpath(os.path.join(WEB, path.lstrip("/")))
            if not full.startswith(WEB) or not os.path.isfile(full):
                return self._send(404, "not found", "text/plain")
            ctype = mimetypes.guess_type(full)[0] or "application/octet-stream"
            if full.endswith(".js"):
                ctype = "text/javascript"
            with open(full, "rb") as f:
                self._send(200, f.read(), ctype)

        def do_POST(self):
            path = urlparse(self.path).path
            n = int(self.headers.get("Content-Length") or 0)
            try:
                req = json.loads(self.rfile.read(n) or b"{}")
            except ValueError:
                return self._send(400, json.dumps({"error": "bad JSON"}), "application/json")
            self._api(path, req)

        def _events(self):
            """A page holds this open while it is shown, so the editor knows when its window closed."""
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.close_connection = True
            lt = app.lifetime
            if lt:
                lt.connect()
            try:
                self.wfile.write(b"retry: 1000\nevent: hello\ndata: {}\n\n")
                self.wfile.flush()
                while True:
                    time.sleep(4)
                    self.wfile.write(b": ping\n\n")
                    self.wfile.flush()
            except OSError:
                pass
            finally:
                if lt:
                    lt.disconnect()

        def _api(self, path, req):
            name = ROUTES.get(path)
            if name is None:
                return self._send(404, json.dumps({"error": "no such API"}), "application/json")
            # only accept requests from the editor page itself
            origin = self.headers.get("Origin")
            if origin and not re.match(r"^https?://(127\.0\.0\.1|localhost)(:\d+)?$", origin):
                return self._send(403, json.dumps({"error": "forbidden"}), "application/json")
            try:
                out = getattr(app, name)(req)
            except ValueError as e:  # a problem to tell the user about, in plain words
                traceback.print_exc()
                return self._send(500, json.dumps({"error": str(e)}), "application/json")
            except Exception as e:  # report every failure to the page instead of dying
                traceback.print_exc()
                return self._send(500, json.dumps({"error": "%s: %s" % (type(e).__name__, e)}), "application/json")
            if isinstance(out, bytes):
                return self._send(200, out, "application/octet-stream")
            self._send(200, json.dumps(out, ensure_ascii=False, separators=(",", ":"), allow_nan=False,
                                       default=str), "application/json")

    return Handler


class EditorServer(ThreadingHTTPServer):
    """An HTTP server that never shares its port.

    Python's servers set SO_REUSEADDR, which on Windows lets a second server bind a port another one is
    still listening on (an older editor left running in the background); the new editor's window would
    then talk to the old one. Here the bind fails instead, and serve() moves on to the next port."""
    allow_reuse_address = os.name != "nt"

    def server_bind(self):
        if os.name == "nt" and hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


def serve(data_dir, port=8765, host="127.0.0.1"):
    app = App(data_dir)
    httpd = None
    for p in range(port, port + 20):
        try:
            httpd = EditorServer((host, p), make_handler(app))
            port = p
            break
        except OSError:
            continue
    if httpd is None:
        raise SystemExit("no free port between %d and %d" % (port, port + 19))
    httpd.daemon_threads = True
    return app, httpd, port
