"""The Garage: lift kits, wheel spacers, custom tire sizes, engine power, brakes and paint.

Everything here makes *copies* of the vehicle's parts with the change built in
and picks them in a new config, so the original vehicle is never touched:

* Lift: the suspension part of an axle is copied with a vertical "nodeMove7"
  passed to all of its tables and to the slots under it (hubs, rims, tires,
  springs). That is what the game does with a slot's nodeMove option: the whole
  suspension sits lower on the body, and the beams that tie it to the frame
  become longer, like the drop brackets of a real lift kit. Springs and shocks
  take their rest length at spawn, so they keep working.
* Spacers: the part holding the rim slot gets "nodeOffset6" on that slot; the
  game mirrors a nodeOffset's x for left and right, so both wheels move out.
* Custom tires: the tire part is copied with a new radius and width, and its
  meshes are scaled to match.
* Engine: the part with the torque curve is copied with the curve scaled.
* Brakes: brake parts are copied with their brakeTorque scaled.

Generated parts remember where they came from (information.jbeamEditor), so
opening a saved version again starts from the original parts, not a copy of a copy.
"""
import json
import math
import os
import re
import time

from . import build, expr, jbeamfile, partedit

MOVE_KEY = "nodeMove7"
AXLE_GAP = 0.3       # m along the vehicle: wheels closer than this share an axle
TRACK_KEY = "nodeOffset6"
META_KEY = "jbeamEditor"

BODY_WORDS = re.compile(r"body|cab|bed|box|door|hood|bonnet|fender|tailgate|trunk|roof|interior|dash|seat|glass|"
                        r"window|windshield|mirror|quarter|panel|grille", re.I)
NOT_BODY = re.compile(r"suspension|axle|frame|chassis|engine|transmission|gearbox|transfer|diff|fuel|exhaust|radiator|"
                      r"brake|hub|wheel|tire|tyre|steering|spring|shock|coilover|driveshaft|swaybar|leaf", re.I)
VAR_KINDS = (
    ("rideHeight", re.compile(r"springheight|rideheight|ride_height|height_[fr]$", re.I)),
    ("springs", re.compile(r"^\$spring_|springrate|spring_rate", re.I)),
    ("dampers", re.compile(r"damp", re.I)),
    ("swaybars", re.compile(r"arb|swaybar|antiroll", re.I)),
    ("track", re.compile(r"trackoffset|trackwidth|wheeloffset", re.I)),
    ("pressure", re.compile(r"pressure", re.I)),
    ("brakes", re.compile(r"brake", re.I)),
    ("gearing", re.compile(r"finaldrive|final_drive|gear_|gearratio", re.I)),
    ("engine", re.compile(r"revlimiter|boost|turbo|nitrous|n2o|fuel", re.I)),
    ("alignment", re.compile(r"camber|caster|toe", re.I)),
)


# ---------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------

def slot_of(part_path):
    """Slot path of a part path: "/a/b/partname" -> "/a/b/"."""
    return part_path[:part_path.rfind("/") + 1] or "/"


def parent_slot(slot_path):
    """"/a/b/" -> "/a/"."""
    segs = [s for s in slot_path.split("/") if s]
    return "/" + "/".join(segs[:-1]) + ("/" if len(segs) > 1 else "")


def ancestors(slot_path):
    """"/a/b/" -> ["/a/", "/a/b/"] (top first)."""
    segs = [s for s in slot_path.split("/") if s]
    return ["/" + "/".join(segs[:i]) + "/" for i in range(1, len(segs) + 1)]


def _text(v):
    """A slot description as text (some are {"description": ..., ...} tables)."""
    if isinstance(v, dict):
        v = v.get("description") or " ".join(str(x) for x in v.values() if isinstance(x, str))
    return v if isinstance(v, str) else ""


def spacer_of(meta, rim_id, default=0.0):
    """The spacer a garage copy gave one of its rim slots (older copies kept one for all)."""
    if not meta:
        return default
    per = meta.get("spacers")
    if isinstance(per, dict):
        return per.get(rim_id, default)
    return meta.get("spacer", default)


def walk(slot):
    yield slot
    for c in slot.get("children") or []:
        yield from walk(c)


def slug(text):
    s = re.sub(r"[^A-Za-z0-9]+", "_", text or "").strip("_").lower()
    return s[:40] or "custom"


def inches(m):
    return "%g in" % round(m / 0.0254, 1)


def fnum(v, d=3):
    return ("%%.%df" % d % v).rstrip("0").rstrip(".") if isinstance(v, float) else str(v)


def scale_value(old, f):
    """old * f, keeping variables and formulas working."""
    if isinstance(old, bool):
        return None
    if isinstance(old, (int, float)):
        return round(old * f, 4)
    if isinstance(old, str) and old.startswith("$="):
        return "$=(%s)*%s" % (old[2:], fnum(round(f, 4), 4))
    if isinstance(old, str) and old.startswith("$"):
        return "$=%s*%s" % (old, fnum(round(f, 4), 4))
    return None


def resolve(v, scope):
    """A number from a jbeam value that may be a $variable or a $= formula."""
    if isinstance(v, bool) or v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, str) and v.startswith("$="):
        try:
            r = expr.evaluate(v, lambda n: scope.get(n))
            return float(r) if isinstance(r, (int, float)) and not isinstance(r, bool) else None
        except Exception:
            return None
    if isinstance(v, str) and v.startswith("$"):
        r = scope.get(v)
        return float(r) if isinstance(r, (int, float)) and not isinstance(r, bool) else None
    return None


def part_scope(part_data, scope):
    """The scene's variable values plus the defaults of the part's own variables."""
    s = dict(scope)
    for v in build.part_variables(part_data):
        n = v.get("name")
        if isinstance(n, str) and n not in s and isinstance(v.get("default"), (int, float)):
            s[n] = v["default"]
    return s


def pw_mods(part_data):
    """The modifier rows of a part's pressureWheels table, merged in order."""
    out = {}
    rows = part_data.get("pressureWheels")
    if isinstance(rows, list):
        for r in rows[1:]:
            if isinstance(r, dict):
                out.update(r)
    return out


def meta_of(part):
    info = part.data.get("information") if isinstance(part.data.get("information"), dict) else {}
    m = info.get(META_KEY)
    return m if isinstance(m, dict) else None


# ---------------------------------------------------------------------------
# what can be changed
# ---------------------------------------------------------------------------

class Info:
    """The Garage's view of a built vehicle."""

    def __init__(self, model, scene):
        self.m = model
        self.sc = scene
        self.slots = {s["path"]: s for s in walk(scene["tree"])}
        self.scope = {v["name"]: v.get("val") for v in scene["variables"]}
        self.wheels = self._wheels()
        self.axles = self._axles()
        self.engine = self._engine()
        self.brakes = self._brakes()
        self.body = self._body_slots()

    # -- parts and sources ---------------------------------------------------------
    def part(self, name):
        return self.m.parts.get(name)

    def source(self, name):
        """(original part, its garage settings) for a part that may be a garage copy."""
        p = self.part(name)
        if p is None:
            return name, {}
        meta = meta_of(p)
        if meta and isinstance(meta.get("from"), str) and meta["from"] in self.m.parts and meta["from"] != name:
            return meta["from"], meta
        return name, {}

    def slot_json(self, entry, extra=None):
        if entry is None:
            return None
        chosen = entry.get("chosen") or ""
        src, meta = self.source(chosen) if chosen else ("", {})
        out = {"path": entry["path"], "id": entry["id"], "description": _text(entry.get("description")) or entry["id"],
               "part": chosen, "title": self.sc["partTitles"].get(chosen) or (self.part(chosen).title if self.part(chosen) else chosen),
               "source": src, "meta": meta}
        if extra:
            out.update(extra)
        return out

    # -- wheels and axles ------------------------------------------------------------
    def _wheels(self):
        out = []
        nodes = self.sc["nodes"]
        for w in self.sc["wheels"]:
            a, b = nodes[w["n1"]], nodes[w["n2"]]
            c = [(a["p"][k] + b["p"][k]) / 2 for k in range(3)]
            pname = a.get("part")
            ppath = (self.sc["parts"].get(pname) or {}).get("path") or "/"
            out.append({"name": w["name"], "c": c, "r": w.get("radius") or 0.3, "width": w.get("width"),
                        "slot": slot_of(ppath)})
        return out

    def _axles(self):
        """One entry per axle (wheels at the same length along the vehicle, duals included).

        Axles in the front half are "F", "F2", ...; in the back half "R", "R2", ... (front to
        back), so a car keeps plain "F" and "R"."""
        wl = self.wheels
        if not wl:
            return []
        ys = [w["c"][1] for w in wl]
        mid = (min(ys) + max(ys)) / 2
        span = max(ys) - min(ys)
        rows = []                           # [[y, [wheels]]] front to back
        for w in sorted(wl, key=lambda w: w["c"][1]):
            if rows and w["c"][1] - rows[-1][0] < AXLE_GAP:
                rows[-1][1].append(w)
                rows[-1][0] = sum(x["c"][1] for x in rows[-1][1]) / len(rows[-1][1])
            else:
                rows.append([w["c"][1], [w]])
        half = lambda y: "F" if span > 0.3 and y < mid else "R"  # noqa: E731
        group = {w["name"]: half(y) for y, ws in rows for w in ws}
        counts = {"F": 0, "R": 0}
        for y, _ in rows:
            counts[half(y)] += 1
        axle_of, made, seen = {}, [], {"F": 0, "R": 0}
        for y, ws in rows:
            g = half(y)
            seen[g] += 1
            n = seen[g]
            word = "Front" if g == "F" else "Rear"
            made.append({"id": g if n == 1 else "%s%d" % (g, n), "group": g,
                         "label": word if counts[g] == 1 else "%s %d" % (word, n), "mine": ws})
            for w in ws:
                axle_of[w["name"]] = len(made) - 1

        def top_slot(w, same):
            """The highest slot above a wheel that holds no wheels but those that are same() as it."""
            for a in ancestors(w["slot"]):
                inside = [x for x in wl if x["slot"].startswith(a)]
                if inside and all(same(x) for x in inside):
                    return a if self.slots.get(a, {}).get("chosen") else None
            return None

        # what lifts: the suspension parts holding one end's wheels (a tandem's springs carry two axles)
        lift_slots = {}
        for w in wl:
            a = top_slot(w, lambda x, g=group[w["name"]]: group[x["name"]] == g)
            if a:
                lift_slots.setdefault(a, set()).add(axle_of[w["name"]])
        axles = []
        for i, ax in enumerate(made):
            mine = ax["mine"]
            slots = [a for a, owners in lift_slots.items() if i in owners]
            shared = sorted({j for a in slots for j in lift_slots[a]} - {i})
            # where the rim and tire are: the part tree of this axle alone
            own = []
            for w in mine:
                a = top_slot(w, lambda x: axle_of[x["name"]] == i)
                if a and a not in own:
                    own.append(a)
            alone = counts[ax["group"]] == 1
            rim, tire = self._wheel_slots(ax["group"], own) if own or alone else (None, None)
            xs = [w["c"][0] for w in mine]
            lift = spacer = 0.0
            for sp in slots:
                _, meta = self.source(self.slots[sp]["chosen"])
                lift = meta.get("lift", lift) if meta else lift
            if rim:
                owner = self.slots.get(parent_slot(rim["path"]))
                if owner and owner.get("chosen"):
                    _, meta = self.source(owner["chosen"])
                    spacer = spacer_of(meta, rim["id"], spacer)
            tire_meta = self.source(tire["part"])[1] if tire and tire.get("part") else {}
            axles.append({
                "id": ax["id"], "label": ax["label"], "group": ax["group"], "wheels": [w["name"] for w in mine],
                "slots": [self.slot_json(self.slots[sp]) for sp in slots],
                "liftWith": [made[j]["id"] for j in shared],
                "rim": rim, "tire": tire,
                "radius": sum(w["r"] for w in mine) / len(mine),
                "width": next((w["width"] for w in mine if w["width"]), None),
                "y": sum(w["c"][1] for w in mine) / len(mine), "z": sum(w["c"][2] for w in mine) / len(mine),
                "track": (max(xs) - min(xs)) if len(mine) > 1 else 0.0,
                "lift": lift, "spacer": spacer,
                "customTire": {k: tire_meta[k] for k in ("diameter", "width") if k in tire_meta} or None,
                "canSpacer": bool(rim and parent_slot(rim["path"]) != "/"),
            })
        return axles

    def _wheel_slots(self, g, susp_slots):
        """The rim and tire slots of an axle: (slot json with sized options, ...) or None."""
        rim = tire = None
        cands = []
        for e in walk(self.sc["tree"]):
            if e["path"] == "/" or not e.get("chosen"):
                continue
            inside = any(e["path"].startswith(sp) for sp in susp_slots)
            named = bool(re.search(r"(_%s(_|$)|\b%s)" % (g, "front" if g == "F" else "rear"),
                                   e["id"] + " " + _text(e.get("description")), re.I))
            if inside or (not susp_slots and named):
                cands.append(e)
        for e in cands:
            p = self.part(e["chosen"])
            if p is None:
                continue
            mods = pw_mods(p.data)
            st = " ".join(p.slot_types())
            if tire is None and ("radius" in mods or st.startswith("tire")) and not st.startswith("wheel_"):
                tire = self.slot_json(e, {"options": self._sized(e, "tire")})
            elif rim is None and ("hubRadius" in mods or "hubWidth" in mods or st.startswith("wheel_")) and "radius" not in mods:
                rim = self.slot_json(e, {"options": self._sized(e, "rim")})
        return rim, tire

    def _sized(self, entry, kind):
        out = []
        for name in entry.get("options") or []:
            p = self.part(name)
            if p is None or getattr(p, "virtual", False):
                continue
            mods = pw_mods(p.data)
            sc = part_scope(p.data, self.scope)
            item = {"name": name, "title": p.title}
            if kind == "tire":
                r = resolve(mods.get("radius"), sc)
                w = resolve(mods.get("tireWidth"), sc)
                psi = resolve(mods.get("pressurePSI"), sc)
                item.update({"diameter": r * 2 if r else None, "width": w, "psi": psi})
            else:
                r = resolve(mods.get("hubRadius"), sc)
                w = resolve(mods.get("hubWidth"), sc)
                item.update({"diameter": r * 2 if r else None, "width": w})
            meta = meta_of(p)
            if meta:
                item["generated"] = True
            out.append(item)
        return out

    # -- engine and brakes -------------------------------------------------------------
    def _engine(self):
        eng = self.sc.get("engine") or {}
        dev = eng.get("device") or "mainEngine"
        found = None
        for name, info in self.sc["parts"].items():
            p = self.part(name)
            if p is None:
                continue
            me = p.data.get(dev)
            if isinstance(me, dict) and isinstance(me.get("torque"), list):
                found = (name, info)
        if not found:
            return None
        name, info = found
        entry = self.slots.get(slot_of(info["path"]))
        src, meta = self.source(name)
        curve = eng.get("torque") or []
        f = meta.get("power", 1.0) if meta else 1.0
        base_curve = [[r, t / f] for r, t in curve] if f else curve
        peak_t = max((t for _, t in base_curve), default=0)
        peak_p = max(((t * r / 9549.3, r) for r, t in base_curve), default=(0, 0))
        return {"slot": self.slot_json(entry) if entry and entry["path"] != "/" else None,
                "part": name, "source": src, "meta": meta, "curve": base_curve,
                "maxRPM": (eng.get("maxRPM") or 0) - (meta.get("rpm", 0) if meta else 0),
                "peakTorque": peak_t, "peakKW": peak_p[0], "peakRPM": peak_p[1],
                "power": f, "rpm": meta.get("rpm", 0) if meta else 0, "device": dev, "electric": bool(eng.get("electric"))}

    def _brakes(self):
        out = []
        for name, info in self.sc["parts"].items():
            p = self.part(name)
            if p is None or "brakeTorque" not in pw_mods(p.data):
                continue
            entry = self.slots.get(slot_of(info["path"]))
            if entry is None or entry["path"] == "/":
                continue
            src, meta = self.source(name)
            out.append(self.slot_json(entry, {"torque": meta.get("brakes", 1.0) if meta else 1.0}))
        return out

    # -- body parts (for a body lift) --------------------------------------------------
    def _body_slots(self):
        susp = {s["path"] for a in self.axles for s in a["slots"]}
        parents = {"/"} | {parent_slot(p) for p in susp}
        out = []
        for e in walk(self.sc["tree"]):
            if e["path"] == "/" or not e.get("chosen") or e["path"] in susp:
                continue
            if parent_slot(e["path"]) not in parents:
                continue
            if any(sp.startswith(e["path"]) for sp in susp):
                continue   # holds a suspension: not a body part
            text = "%s %s %s" % (e["id"], e.get("description") or "", e.get("chosen"))
            if NOT_BODY.search(text) and not BODY_WORDS.search(e["id"]):
                continue
            src, meta = self.source(e["chosen"])
            out.append(self.slot_json(e, {"checked": bool(BODY_WORDS.search(text)) or bool(meta.get("bodyLift")),
                                          "bodyLift": meta.get("bodyLift", 0) if meta else 0}))
        return out

    # -- tuning variables -----------------------------------------------------------------
    def var_groups(self):
        out = {}
        for v in self.sc["variables"]:
            if v.get("hidden"):
                continue
            for kind, rx in VAR_KINDS:
                if rx.search(v["name"]):
                    out.setdefault(kind, []).append(v["name"])
                    break
        return out

    def ground(self):
        if not self.wheels:
            return min((n["p"][2] for n in self.sc["nodes"]), default=0.0)
        return min(w["c"][2] - w["r"] for w in self.wheels)

    def clearance(self):
        """Height of the lowest body node (not suspension or wheels) above the ground."""
        susp = [s["path"] for a in self.axles for s in a["slots"]]
        axle_nodes = {w["n1"] for w in self.sc["wheels"]} | {w["n2"] for w in self.sc["wheels"]}
        low = None
        for i, n in enumerate(self.sc["nodes"]):
            if i in axle_nodes:
                continue
            path = (self.sc["parts"].get(n.get("part")) or {}).get("path") or ""
            if any(path.startswith(sp) for sp in susp):
                continue
            low = n["p"][2] if low is None else min(low, n["p"][2])
        return None if low is None else low - self.ground()

    def to_json(self):
        body_lift = max((s.get("bodyLift") or 0 for s in self.body), default=0)
        return {"axles": self.axles, "engine": self.engine, "brakes": self.brakes, "body": self.body,
                "bodyLift": body_lift, "vars": self.var_groups(), "ground": self.ground(), "clearance": self.clearance(),
                "brakeFactor": max((b["torque"] for b in self.brakes), default=1.0)}


# ---------------------------------------------------------------------------
# the parts to make
# ---------------------------------------------------------------------------

class Plan:
    """Which parts to generate for a Garage spec.

    The spec holds only what the person changed; everything else keeps the
    value the vehicle already has (a saved version is read back from its
    parts). spec["reset"] takes every Garage change off. With full=True every
    change is generated again under this tag (for saving a self-contained
    version); otherwise only the slots the spec touches are (for previews)."""

    def __init__(self, info, spec, tag, full=False):
        self.info = info
        self.spec = spec or {}
        self.tag = tag
        self.full = full
        self.reset = bool(self.spec.get("reset"))
        self.slots = {}          # slot path -> {"entry", "source", "meta", "set", "touched"}
        self.copies = {}         # slot path -> {"entry", "source", "ops", "meta", "what"}
        self.config_parts = {}   # slot id -> part name
        self.changes = []        # what a person reads
        self.notes = []          # what could not be done
        self._collect()
        self._decide()

    def _slot(self, entry):
        s = self.slots.get(entry["path"])
        if s is None:
            src, meta = self.info.source(entry["chosen"])
            s = self.slots[entry["path"]] = {"entry": entry, "source": src, "meta": meta, "set": {}, "labels": {},
                                           "touched": False}
        return s

    def _pick(self, section, key, current):
        """(value, whether the spec set it)."""
        if self.reset:
            return None, True
        if isinstance(section, dict) and key in section:
            return section[key], True
        return current, False

    def _collect(self):
        info, spec = self.info, self.spec
        for ax in info.axles:
            sect = (spec.get("axles") or {}).get(ax["id"])
            lift, t = self._pick(sect, "lift", ax["lift"])
            lift = float(lift or 0)
            if abs(lift) > 0.0005 and not ax["slots"]:
                self.notes.append("%s: the suspension is built into the main part, so it can't be lifted here; "
                                  "use the Structure mode." % ax["label"])
            for sj in ax["slots"]:
                sl = self._slot(info.slots[sj["path"]])
                if "lift" in sl["set"] and not t:
                    continue        # a part shared with an axle already counted (a tandem's springs)
                sl["set"]["lift"] = lift
                sl["labels"]["lift"] = " and ".join(a["label"] for a in info.axles
                                                    if a["id"] == ax["id"] or a["id"] in ax["liftWith"])
                sl["touched"] |= t
            spacer, t = self._pick(sect, "spacer", ax["spacer"])
            spacer = float(spacer or 0)
            rim = ax.get("rim")
            owner = info.slots.get(parent_slot(rim["path"])) if rim else None
            if owner and owner["path"] != "/" and owner.get("chosen"):
                sl = self._slot(owner)
                sl["set"].setdefault("spacer", {})[rim["id"]] = spacer
                sl["labels"][rim["id"]] = ax["label"]
                sl["touched"] |= t
            elif abs(spacer) > 0.0005:
                self.notes.append("%s: no rim slot found under a part that can be copied, so no spacers." % ax["label"])
            ct, t = self._pick(sect, "customTire", ax.get("customTire"))
            if ax.get("tire"):
                sl = self._slot(info.slots[ax["tire"]["path"]])
                sl["set"]["tire"] = ct or None
                sl["labels"]["tire"] = ax["label"]
                sl["touched"] |= t
        if info.engine and info.engine.get("slot"):
            eng = spec.get("engine")
            power, t1 = self._pick(eng, "power", info.engine.get("power", 1.0))
            rpm, t2 = self._pick(eng, "rpm", info.engine.get("rpm", 0))
            sl = self._slot(info.slots[info.engine["slot"]["path"]])
            sl["set"]["power"] = float(power if power is not None else 1.0)
            sl["set"]["rpm"] = float(rpm or 0)
            sl["touched"] |= t1 or t2
        elif info.engine and (spec.get("engine") or {}).get("power", 1) != 1:
            self.notes.append("The engine is in the main part, so it can't be tuned here.")
        bf, t = self._pick(spec.get("brakes"), "torque", info.to_json()["brakeFactor"] if info.brakes else 1.0)
        for b in info.brakes:
            sl = self._slot(info.slots[b["path"]])
            sl["set"]["brakes"] = float(bf if bf is not None else 1.0)
            sl["touched"] |= t
        bl, t = self._pick(spec, "bodyLift", max((b.get("bodyLift") or 0 for b in info.body), default=0))
        if "bodySlots" in spec:
            chosen = set(spec.get("bodySlots") or [])
            t = True
        else:
            chosen = {b["path"] for b in info.body if b.get("bodyLift")} or {b["path"] for b in info.body if b.get("checked")}
        for b in info.body:
            sl = self._slot(info.slots[b["path"]])
            sl["set"]["bodyLift"] = float(bl or 0) if b["path"] in chosen else 0.0
            sl["touched"] |= t
        # garage copies of anything else (from an older version of the editor, say)
        for entry in info.slots.values():
            if entry["path"] != "/" and entry.get("chosen") and entry["path"] not in self.slots:
                src, meta = info.source(entry["chosen"])
                if meta and src != entry["chosen"]:
                    self._slot(entry)["touched"] = self.reset

    @staticmethod
    def _neutral(st):
        return (abs(st.get("lift", 0)) < 0.0005 and all(abs(v) < 0.0005 for v in (st.get("spacer") or {}).values())
                and not st.get("tire") and abs(st.get("power", 1) - 1) < 0.004 and abs(st.get("rpm", 0)) < 1
                and abs(st.get("brakes", 1) - 1) < 0.004 and abs(st.get("bodyLift", 0)) < 0.0005)

    def _decide(self):
        info = self.info
        for path, sl in self.slots.items():
            st = sl["set"]
            entry = sl["entry"]
            has_copy = bool(sl["meta"]) and sl["source"] != entry["chosen"]
            if self._neutral(st):
                if has_copy and (sl["touched"] or self.full):
                    self.config_parts[entry["id"]] = sl["source"]
                continue
            self._describe(sl)
            if has_copy and not sl["touched"] and not self.full:
                continue    # keep the copy it already has
            c = self.copies[path] = {"entry": entry, "source": sl["source"], "ops": [], "meta": {"from": sl["source"]},
                                     "what": []}
            lift = st.get("lift", 0)
            if abs(lift) > 0.0005:
                c["ops"].append(("move", -lift))
                c["meta"]["lift"] = round(lift, 4)
                c["what"].append("%s %s" % (inches(abs(lift)), "lift" if lift > 0 else "lower"))
            spacers = {k: round(v, 4) for k, v in (st.get("spacer") or {}).items() if abs(v) > 0.0005}
            for rim_id, spacer in spacers.items():
                c["ops"].append(("slotopt", rim_id, {TRACK_KEY: {"x": spacer, "y": 0, "z": 0}}))
            if spacers:
                c["meta"]["spacers"] = spacers
                c["meta"]["spacer"] = max(spacers.values(), key=abs)
                c["what"].append("%s mm spacers" % "/".join(sorted({"%d" % round(v * 1000) for v in spacers.values()})))
            ct = st.get("tire")
            if ct:
                srcp = info.part(sl["source"])
                mods = pw_mods(srcp.data)
                scp = part_scope(srcp.data, info.scope)
                r0 = resolve(mods.get("radius"), scp) or 0.3
                w0 = resolve(mods.get("tireWidth"), scp) or 0.2
                d = float(ct.get("diameter") or r0 * 2)
                w = float(ct.get("width") or w0)
                c["ops"].append(("tire", d / 2, w, r0, w0))
                c["meta"].update({"diameter": round(d, 4), "width": round(w, 4)})
                c["what"].append("%s x %s in" % (fnum(round(d / 0.0254, 1), 1), fnum(round(w / 0.0254, 1), 1)))
            power, rpm = st.get("power", 1), st.get("rpm", 0)
            if abs(power - 1) > 0.004 or abs(rpm) >= 1:
                c["ops"].append(("engine", power, rpm, (info.engine or {}).get("device") or "mainEngine"))
                c["meta"].update({"power": round(power, 4), "rpm": round(rpm)})
                c["what"].append(_engine_words(power, rpm))
            bf = st.get("brakes", 1)
            if abs(bf - 1) > 0.004:
                c["ops"].append(("brake", bf))
                c["meta"]["brakes"] = round(bf, 4)
                c["what"].append("%+d%% brake force" % round((bf - 1) * 100))
            bl = st.get("bodyLift", 0)
            if abs(bl) > 0.0005:
                c["ops"].append(("move", bl))
                c["meta"]["bodyLift"] = round(bl, 4)
                c["what"].append("%s body lift" % inches(bl))

    def _describe(self, sl):
        st, labels = sl["set"], sl["labels"]
        lift = st.get("lift", 0)
        if abs(lift) > 0.0005:
            self._say("%s suspension %s by %s (%d mm)" % (labels.get("lift", ""), "lifted" if lift > 0 else "lowered",
                                                         inches(abs(lift)), round(abs(lift) * 1000)))
        for rim_id, spacer in (st.get("spacer") or {}).items():
            if abs(spacer) > 0.0005:
                self._say("%s wheels %s by %d mm each side" % (labels.get(rim_id, ""), "pushed out" if spacer > 0
                                                             else "pulled in", round(abs(spacer) * 1000)))
        if st.get("tire"):
            label = labels.get("tire", "")
            d, w = st["tire"].get("diameter"), st["tire"].get("width")
            self._say("%s tires: %s tall%s" % (label, inches(d) if d else "same", ", %d mm wide" % round(w * 1000) if w else ""))
        if abs(st.get("power", 1) - 1) > 0.004 or abs(st.get("rpm", 0)) >= 1:
            self._say("Engine: " + _engine_words(st.get("power", 1), st.get("rpm", 0)))
        if abs(st.get("brakes", 1) - 1) > 0.004:
            self._say("Brakes: %+d%% stopping force" % round((st["brakes"] - 1) * 100))
        if abs(st.get("bodyLift", 0)) > 0.0005:
            self._say("Body lifted %s" % inches(st["bodyLift"]))

    def _say(self, text):
        if text not in self.changes:
            self.changes.append(text)

    # -- making the text ----------------------------------------------------------------
    def build_parts(self):
        """{new part name: (text chunk, data)} and fills config_parts."""
        out = {}
        m = self.info.m
        for path, c in self.copies.items():
            src = c["source"]
            name = "%s__%s" % (src, self.tag)
            pc = partedit.PartCopy(m, src, name)
            title = "%s (%s)" % (m.parts[src].title, "; ".join(w for w in c["what"] if w))
            meta = dict(c["meta"])
            meta["made"] = time.strftime("%Y-%m-%d")
            pc.set(["information", "name"], title)
            pc.set(["information", META_KEY], meta)
            dz = sum(op[1] for op in c["ops"] if op[0] == "move")
            if abs(dz) > 1e-6:
                v = {"x": 0, "y": 0, "z": round(dz, 4)}
                pc.wrap_tables({MOVE_KEY: v}, {MOVE_KEY: ""})
                pc.slot_options(None, {MOVE_KEY: v})
            for op in c["ops"]:
                if op[0] == "slotopt":
                    if not pc.slot_options(op[1], op[2]):
                        raise ValueError("slot %s not found in %s" % (op[1], src))
                elif op[0] == "tire":
                    _tire(pc, *op[1:])
                elif op[0] == "engine":
                    _engine(pc, op[1], op[2], op[3])
                elif op[0] == "brake":
                    pc.map_modifiers("pressureWheels", "brakeTorque", lambda v, f=op[1]: scale_value(v, f))
            out[name] = (pc.chunk(), pc.data())
            self.config_parts[c["entry"]["id"]] = name
        return out


def _engine_words(power, rpm):
    bits = []
    if abs(power - 1) > 0.004:
        bits.append("%+d%% power" % round((power - 1) * 100))
    if abs(rpm) >= 1:
        bits.append("%+d rpm" % round(rpm))
    return ", ".join(bits)


def _tire(pc, r, w, r0, w0):
    if not pc.map_modifiers("pressureWheels", "radius", lambda v: round(r, 4)):
        raise ValueError("the tire part has no radius to change")
    if not pc.map_modifiers("pressureWheels", "tireWidth", lambda v: round(w, 4)):
        pass   # tires without a width take it from the hub; the mesh is still scaled below
    sr = r / r0 if r0 else 1.0
    sw = w / w0 if w0 else 1.0
    # a bigger tire has more rubber: its nodes get heavier with its size (which also keeps them stable)
    f = max(0.5, min(sr * sw, 3.0))
    if abs(f - 1) > 0.01:
        pc.map_modifiers("pressureWheels", "nodeWeight", lambda v: scale_value(v, f))

    def scaled(row, cur):
        s = cur.get("scale") if isinstance(cur.get("scale"), dict) else {}
        return {"scale": {"x": round(float(s.get("x", 1)) * sw, 4), "y": round(float(s.get("y", 1)) * sr, 4),
                          "z": round(float(s.get("z", 1)) * sr, 4)}}
    if "flexbodies" in pc.sections():
        pc.map_row_options("flexbodies", scaled)


def _engine(pc, power, rpm, dev="mainEngine"):
    """Scale the torque curve of the engine (or electric motor) dev and move its rev limit."""
    if abs(power - 1) > 0.004:
        pc.map_numbers([dev, "torque"], 1, lambda row: row[1] * power)
        for key in ("maxTorqueRating", "maxOverTorqueDamage", "maximumWantedRegenTorque"):
            v = pc.get([dev, key])
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                pc.set([dev, key], round(v * (power if key == "maximumWantedRegenTorque" else max(power, 1.0)), 1))
    if abs(rpm) >= 1:
        for path in ([dev, "maxRPM"], [dev, "revLimiterRPM"], ["vehicleController", "highShiftUpRPM"]):
            v = pc.get(path)
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                pc.set(path, round(v + rpm))
        if rpm > 0:
            table = pc.get([dev, "torque"]) or []
            rows = [r for r in table[1:] if isinstance(r, list) and len(r) >= 2 and isinstance(r[0], (int, float))]
            mx = pc.get([dev, "maxRPM"])
            if rows and isinstance(mx, (int, float)) and rows[-1][0] < mx + 250:
                last = rows[-1]
                pc.append_rows([dev, "torque"], [[round(mx + 500), round(float(last[1]) * 0.9, 1)]])


# ---------------------------------------------------------------------------
# entry points used by the server
# ---------------------------------------------------------------------------

def preview(model, config, spec):
    """(scene, info json, plan) for the vehicle with the Garage changes, nothing written."""
    base = build.build(model, config)
    info = Info(model, base)
    plan = Plan(info, spec, "garage_preview")
    made = plan.build_parts()
    if not made and not plan.config_parts and not (spec or {}).get("vars"):
        return base, info, plan, base, config or {}
    final_cfg = json.loads(json.dumps(config or {}))
    final_cfg.setdefault("parts", {})
    final_cfg["parts"].update(plan.config_parts)
    if isinstance((spec or {}).get("vars"), dict):
        final_cfg.setdefault("vars", {}).update(spec["vars"])
    m2 = model.overlay({n: d for n, (_, d) in made.items()}, "garage preview") if made else model
    scene = build.build(m2, final_cfg)
    return scene, info, plan, base, final_cfg


def write(model, root, config_name, spec, config, title=None, picture=None, backup_dir=None):
    """Write the generated parts and a .pc config into root (an editable mod folder).

    Returns {"jbeam": path or None, "pc": path, "config": name, "changes": [...]}"""
    name = re.sub(r"[^A-Za-z0-9 _.\-]", "_", config_name or "").strip() or "my_version"
    tag = slug(name)
    base = build.build(model, config)
    info = Info(model, base)
    plan = Plan(info, spec, tag, full=True)
    made = plan.build_parts()
    vdir = os.path.join(root.path, "vehicles", model.name)
    os.makedirs(vdir, exist_ok=True)
    jb = os.path.join(vdir, "jbe_garage_%s.jbeam" % tag)
    written = []
    if made:
        text = partedit.file_text([c for c, _ in made.values()],
                                  comment="Made by the JBeam Editor Garage for the config \"%s\".\n"
                                          "Changes: %s" % (name, "; ".join(plan.changes)))
        _write_file(jb, text, backup_dir, root)
        written.append(jb)
    elif os.path.exists(jb):
        _write_file(jb, "{\n}\n", backup_dir, root)
    final_cfg = json.loads(json.dumps(config or {}))
    final_cfg.setdefault("parts", {}).update(plan.config_parts)
    if isinstance(spec.get("vars"), dict):
        final_cfg.setdefault("vars", {}).update(spec["vars"])
    root.refresh()
    return {"jbeam": jb if made else None, "written": written, "config": name, "configFile": name,
            "finalConfig": final_cfg, "changes": plan.changes, "notes": plan.notes, "tag": tag}


def _write_file(path, text, backup_dir, root):
    if os.path.exists(path) and backup_dir:
        dst = os.path.join(backup_dir, re.sub(r"[^\w\-. ]", "_", root.label), time.strftime("%Y-%m-%d_%H-%M-%S"),
                           os.path.basename(path))
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        with open(path, "rb") as f, open(dst, "wb") as g:
            g.write(f.read())
    tmp = path + ".jbeam-editor-tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
    os.replace(tmp, path)
