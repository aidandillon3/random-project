"""Assemble a vehicle the way the game does, and turn it into a 3D scene.

Follows lua/common/jbeam in BeamNG 0.40: slot filling (slotSystem.lua),
variables and $= expressions (variables.lua), part merging (unifyParts),
table processing (tableSchema.lua), node offsets (utils.lua), scale keys
(scaling.lua). Every table row keeps a note of the file, part and row it came
from, so an edit in the 3D view can be written back to the right line.
"""
import json
import math

from . import expr

WHEEL_SECTIONS = ("pressureWheels", "wheels", "hubWheels", "hubWheelsTSV", "hubWheelsTSI", "monoHubWheels")
DEFAULTS = {"nodeWeight": 25.0, "beamSpring": 4300000.0, "beamDamp": 580.0, "beamDeform": 220000.0,
            "beamStrength": math.inf}
# row keys that are bookkeeping, not settings the user wrote
HIDDEN_KEYS = {"partOrigin", "partPath", "slotType", "partName", "__src", "__base", "__inline", "name", "id",
               "posX", "posY", "posZ", "id1", "id2", "id3", "id4", "ignoreNodeOffset"}


class RList(list):
    __slots__ = ("src",)


class RDict(dict):
    __slots__ = ("src",)


def _copy(v):
    if isinstance(v, dict):
        return {k: _copy(x) for k, x in v.items()}
    if isinstance(v, list):
        return [_copy(x) for x in v]
    return v


def _copy_keep(v):
    """Deep copy that keeps RList/RDict and their src notes."""
    if isinstance(v, RList):
        r = RList(_copy_keep(x) for x in v)
        r.src = v.src
        return r
    if isinstance(v, RDict):
        r = RDict((k, _copy_keep(x)) for k, x in v.items())
        r.src = v.src
        return r
    if isinstance(v, dict):
        return {k: _copy_keep(x) for k, x in v.items()}
    if isinstance(v, list):
        return [_copy_keep(x) for x in v]
    return v


def copy_part(part):
    """A working copy of a part where every table row remembers where it came from."""
    out = {}
    fkey = part.file.key
    for sec, val in part.data.items():
        if isinstance(val, list) and sec not in ("slots", "slots2"):
            rows = []
            for i, row in enumerate(val):
                if isinstance(row, list):
                    r = RList(_copy(row))
                    r.src = (fkey, part.name, sec, i)
                elif isinstance(row, dict):
                    r = RDict(_copy(row))
                    r.src = (fkey, part.name, sec, i)
                else:
                    r = row
                rows.append(r)
            out[sec] = rows
        else:
            out[sec] = _copy(val)
    out["partName"] = part.name
    return out


def is_dict(v):
    return isinstance(v, dict)


# ---------------------------------------------------------------------------
# Tables
# ---------------------------------------------------------------------------

def process_table(rows, base_options=None, keep_src=False):
    """tableSchema.processTableWithSchemaDestructive. Returns (dict of rows, warnings).

    Rows with an "id" column are keyed by it, others by their 1-based row
    counter (counting modifier rows too, like the game)."""
    out = {}
    warns = []
    if not rows or not isinstance(rows[0], list):
        return out, ["table has no header row"]
    header = list(rows[0])
    hsize = len(header)
    local = dict(base_options or {})
    counter = 1
    for row in rows[1:]:
        counter += 1
        if isinstance(row, dict):
            if row.get("include"):
                warns.append("CSV include %r is not loaded by the editor" % row.get("include"))
            local = dict(local)
            for k, v in row.items():
                if k != "include":
                    local[k] = v
            continue
        if not isinstance(row, list):
            continue
        if len(row) > hsize + 1:
            warns.append("row longer than the header: %s" % (row[:6],))
            continue
        new = dict(local)
        cells = list(row)
        inline = None
        if len(cells) == hsize + 1 and isinstance(cells[-1], dict):
            inline = cells[-1]
            new.update(inline)
            cells = cells[:-1]
        if new.get("disable") is True or (isinstance(new.get("variables"), dict) and new["variables"].get("disable") is True):
            continue
        for i, v in enumerate(cells):
            if i < hsize:
                new[header[i]] = v
        if keep_src:
            new["__src"] = getattr(row, "src", None)
            new["__base"] = local
            new["__inline"] = inline
        key = counter - 1
        if new.get("id") is not None:
            key = new.pop("id")
            new["name"] = key
        out[key] = new
    return out, warns


def process_slots(part):
    """Turn a part's slots/slots2 table into slot definition dicts (io.lua)."""
    if isinstance(part.get("slots2"), list) and part.get("slots2"):
        rows, _ = process_table(part["slots2"])
        slots = []
        for s in rows.values():
            s = dict(s)
            s["allowTypes"] = s.get("allowTypes") if isinstance(s.get("allowTypes"), list) else (
                [s["allowTypes"]] if isinstance(s.get("allowTypes"), str) else [])
            s["denyTypes"] = s.get("denyTypes") if isinstance(s.get("denyTypes"), list) else []
            slots.append(s)
        return slots
    if isinstance(part.get("slots"), list) and part.get("slots"):
        raw = list(part["slots"])
        if isinstance(raw[0], list) and raw[0][:1] != ["type"]:
            raw.insert(0, ["type", "default", "description"])
        rows, _ = process_table(raw)
        slots = []
        for s in rows.values():
            s = dict(s)
            s["name"] = s.get("name") or s.get("type")
            s["allowTypes"] = [s.get("type")]
            s.pop("type", None)
            s["denyTypes"] = []
            slots.append(s)
        return slots
    return []


# ---------------------------------------------------------------------------
# Variables
# ---------------------------------------------------------------------------

class Vars:
    """Variable scope: name -> record dict (with "val") or plain value."""


def var_value(v):
    if isinstance(v, dict) and "val" in v and "__var__" in v:
        return v["val"]
    return v


class VarContext:
    def __init__(self, warn):
        self.warn = warn

    def resolve(self, s, vars_, assign_only, root, allow_component):
        if not isinstance(s, str) or not s.startswith("$") or len(s) < 2:
            return s, False
        c2 = s[1]
        if c2 == "=":
            try:
                return expr.evaluate(s, lambda n: var_value(vars_.get(n))), True
            except (expr.ExprError, ArithmeticError, TypeError, ValueError, IndexError, RecursionError) as e:
                self.warn("formula %s failed: %s" % (s, e))
                return None, True
        if assign_only:
            return s, False
        if allow_component and s.startswith("$>>"):
            cur = root
            for k in s[3:].split("."):
                cur = cur.get(k) if isinstance(cur, dict) else None
            if cur is None:
                self.warn("component path not found: %s" % s)
            return _copy(cur), True
        if c2 == ".":
            return "%s%s%s" % (var_value(vars_.get("$prefix")) or "", s[2:], var_value(vars_.get("$suffix")) or ""), True
        if c2 not in "+<>" and (allow_component or c2 != "*"):
            if s not in vars_:
                self.warn("missing variable %s" % s)
                return None, True
            return var_value(vars_[s]), True
        return s, False

    def apply(self, data, vars_, assign_only=False, keys_only_in_variables=False):
        root = data
        stack = [(data, True)]
        while stack:
            d, process_values = stack.pop()
            if isinstance(d, dict):
                renames = None
                for key, v in list(d.items()):
                    if isinstance(key, str) and key.startswith("$"):
                        nk, changed = self.resolve(key, vars_, assign_only, root, False)
                        if changed and nk is not None and nk != key:
                            renames = renames or []
                            renames.append((key, nk))
                    if process_values and isinstance(v, str):
                        nv, changed = self.resolve(v, vars_, assign_only, root, True)
                        if changed:
                            d[key] = nv
                    elif isinstance(v, (dict, list)):
                        if key != "variables":
                            stack.append((v, process_values))
                        elif keys_only_in_variables:
                            stack.append((v, False))
                if renames:
                    for old, new in renames:
                        if old in d:
                            d[new] = d.pop(old)
            elif isinstance(d, list):
                for i, v in enumerate(d):
                    if process_values and isinstance(v, str):
                        nv, changed = self.resolve(v, vars_, assign_only, root, True)
                        if changed:
                            d[i] = nv
                    elif isinstance(v, (dict, list)):
                        stack.append((v, process_values))

    def slot_vars(self, slot_vars, scope):
        """applySlotVars: resolve a slot's "variables" dict against the parent scope."""
        slot_vars = dict(slot_vars or {})
        if not scope:
            return dict(slot_vars)
        vars_ = dict(scope)
        ok = {}
        for _ in range(400):
            passed = False
            for k, v in list(slot_vars.items()):
                if isinstance(v, str) and v.startswith("$"):
                    if v[1:2] == "=":
                        try:
                            res = expr.evaluate(v, lambda n: var_value(vars_.get(n)))
                        except Exception:
                            continue
                        passed = True
                        ok[k] = vars_[k] = res
                        del slot_vars[k]
                    elif v[1:2] not in ("+", "<", ">"):
                        passed = True
                        del slot_vars[k]
                        if v in vars_:
                            ok[k] = vars_[k] = var_value(vars_[v])
                else:
                    passed = True
                    ok[k] = vars_[k] = v
                    del slot_vars[k]
            if not passed:
                break
        for k, v in slot_vars.items():
            try:
                ok[k] = expr.evaluate(v, lambda n: var_value(vars_.get(n)))
            except Exception as e:
                self.warn("slot variable %s = %s failed: %s" % (k, v, e))
        return ok


def part_variables(part):
    rows = part.get("variables")
    if not isinstance(rows, list) or not rows:
        return []
    table, _ = process_table(_copy(rows))
    return [dict(v) for v in table.values()]


def sanitize_vars(var_list, user_vals, warn=None):
    out = {}
    for vv in var_list:
        vv = dict(vv)
        name = vv.get("name")
        if not isinstance(name, str):
            continue
        if vv.get("type") != "range":
            if warn:
                warn("variable %s ignored: type must be \"range\"" % name)
            continue
        if not all(isinstance(vv.get(k), (int, float)) and not isinstance(vv.get(k), bool)
                   for k in ("min", "max", "default")):
            if warn:
                warn("variable %s ignored: default/min/max must be numbers" % name)
            continue
        if vv.get("unit") == "":
            vv["unit"] = None
        uv = user_vals.get(name)
        vv["val"] = uv if isinstance(uv, (int, float)) and not isinstance(uv, bool) else vv["default"]
        unit = vv.get("unit")
        if vv.get("minDis") is None:
            vv["minDis"] = vv["min"] if unit else -100
        if vv.get("maxDis") is None:
            vv["maxDis"] = vv["max"] if unit else 100
        if vv.get("stepDis") is None:
            vv["stepDis"] = (vv["maxDis"] - vv["minDis"]) / 100.0 if unit else 1
        try:
            vv["step"] = vv["stepDis"] * (vv["max"] - vv["min"]) / (vv["maxDis"] - vv["minDis"])
        except (ZeroDivisionError, TypeError):
            vv["step"] = vv["stepDis"]
        if not unit:
            vv["unit"] = "%"
        cat = vv.get("category") or "alignment"
        if isinstance(cat, str) and "." in cat:
            cat, vv["subCategory"] = cat.rsplit(".", 1)
        vv["category"] = cat
        lo, hi = min(vv["min"], vv["max"]), max(vv["min"], vv["max"])
        vv["val"] = min(max(vv["val"], lo), hi)
        vv["__var__"] = True
        out[name] = vv
    return out


# ---------------------------------------------------------------------------
# Node offsets (jbeam/utils.lua)
# ---------------------------------------------------------------------------

def _quat_from_euler(x, y, z):
    x, y, z = x * 0.5, y * 0.5, z * 0.5
    sx, cx, sy, cy, sz, cz = math.sin(x), math.cos(x), math.sin(y), math.cos(y), math.sin(z), math.cos(z)
    return (cz * sx * cy - sz * cx * sy, cz * cx * sy + sz * sx * cy, sz * cx * cy + cz * sx * sy, cz * cx * cy - sz * sx * sy)


def _qmul(a, b):
    ax, ay, az, aw = a
    bx, by, bz, bw = b
    return (aw * bx + ax * bw + ay * bz - az * by,
            aw * by - ax * bz + ay * bw + az * bx,
            aw * bz + ax * by - ay * bx + az * bw,
            aw * bw - ax * bx - ay * by - az * bz)


def qrot(q, v):
    x, y, z, w = q
    vx, vy, vz = v
    # v + 2w(q x v) + 2 q x (q x v)
    tx = 2 * (y * vz - z * vy)
    ty = 2 * (z * vx - x * vz)
    tz = 2 * (x * vy - y * vx)
    return (vx + w * tx + (y * tz - z * ty), vy + w * ty + (z * tx - x * tz), vz + w * tz + (x * ty - y * tx))


def _ops(row):
    rots, offs, moves = {}, {}, {}
    for k, v in row.items():
        if not (isinstance(k, str) and k.startswith("node") and isinstance(v, dict)):
            continue
        if not all(isinstance(v.get(a), (int, float)) and not isinstance(v.get(a), bool) for a in "xyz"):
            continue
        for prefix, target in (("nodeRotate", rots), ("nodeOffset", offs), ("nodeMove", moves)):
            if k.startswith(prefix):
                if prefix == "nodeOffset" and row.get("ignoreNodeOffset"):
                    break
                suffix = k[len(prefix):]
                idx = int(suffix) if suffix.isdigit() else 0
                target[idx] = v
                break
    return rots, offs, moves


def node_transform(row, raw_x):
    """(quat, translation) the game applies to a node, flexbody or prop position."""
    rots, offs, moves = _ops(row)
    if not (rots or offs or moves):
        return None
    sgn = 1 if raw_x > 0 else (-1 if raw_x < 0 else 0)
    q = (0.0, 0.0, 0.0, 1.0)
    tx = ty = tz = 0.0
    for i in sorted(set(rots) | set(offs) | set(moves)):
        if i in rots:
            op = rots[i]
            px = op.get("px") if isinstance(op.get("px"), (int, float)) else 0
            py = op.get("py") if isinstance(op.get("py"), (int, float)) else 0
            pz = op.get("pz") if isinstance(op.get("pz"), (int, float)) else 0
            r = _quat_from_euler(math.radians(-op["x"]), math.radians(-op["y"]), math.radians(-op["z"]))
            v = qrot(r, (px, py, pz))
            v = qrot(q, (px - v[0], py - v[1], pz - v[2]))
            tx, ty, tz = tx + v[0], ty + v[1], tz + v[2]
            q = _qmul(r, q)
        if i in offs:
            op = offs[i]
            v = qrot(q, (sgn * op["x"], op["y"], op["z"]))
            tx, ty, tz = tx + v[0], ty + v[1], tz + v[2]
        if i in moves:
            op = moves[i]
            v = qrot(q, (op["x"], op["y"], op["z"]))
            tx, ty, tz = tx + v[0], ty + v[1], tz + v[2]
    return q, (tx, ty, tz)


def apply_transform(xf, p):
    if xf is None:
        return p
    q, t = xf
    if q != (0.0, 0.0, 0.0, 1.0):
        p = qrot(q, p)
    return (p[0] + t[0], p[1] + t[1], p[2] + t[2])


# ---------------------------------------------------------------------------
# The builder
# ---------------------------------------------------------------------------

def _num(v, default=None):
    if isinstance(v, bool):
        return default
    if isinstance(v, (int, float)):
        return float(v)
    if v == "FLT_MAX":
        return math.inf
    if v == "MINUS_FLT_MAX":
        return -math.inf
    return default


def _beam_type(v):
    if isinstance(v, str) and v.startswith("|"):
        for t in v.split("|")[1:]:
            if t in ("NORMAL", "HYDRO", "ANISOTROPIC", "TIRESIDE", "BOUNDED", "PRESSURED", "SUPPORT", "LBEAM"):
                return "ANISOTROPIC" if t == "TIRESIDE" else t
        return "NORMAL" if v.strip("|") in ("NORMAL", "") else "UNKNOWN:" + v
    return "NORMAL"


def _jsonable(v):
    if isinstance(v, float):
        if v != v:
            return None
        if v in (math.inf, -math.inf):
            return "FLT_MAX" if v > 0 else "MINUS_FLT_MAX"
        return v
    if isinstance(v, dict):
        return {str(k): _jsonable(x) for k, x in v.items() if k not in ("__src", "__var__", "__base", "__inline")}
    if isinstance(v, (list, tuple)):
        return [_jsonable(x) for x in v]
    return v


class Builder:
    def __init__(self, model, config):
        self.m = model
        self.config = config or {}
        self.warnings = []
        self._warned = set()
        self.vc = VarContext(self.warn)

    def warn(self, msg):
        if msg not in self._warned and len(self._warned) < 400:
            self._warned.add(msg)
            self.warnings.append(msg)

    # -- slots ---------------------------------------------------------------
    def compatible(self, slot):
        allow = slot.get("allowTypes") or []
        deny = set(slot.get("denyTypes") or [])
        out, seen = [], set()
        for t in allow:
            for pname in self.m.by_slot.get(t, []):
                if pname in seen:
                    continue
                part = self.m.parts[pname]
                sts = part.slot_types()
                if deny and any(s in deny for s in sts):
                    continue
                seen.add(pname)
                out.append(pname)
        return out

    @staticmethod
    def fits(part, slot):
        sts = part.slot_types()
        allow = slot.get("allowTypes") or []
        deny = slot.get("denyTypes") or []
        if isinstance(part.slot_type, str):
            return part.slot_type in allow
        return any(s in allow for s in sts) and not any(s in deny for s in sts)

    def fill(self, parent_copy, parent_slots, level, slot_opts_in, path, tree_children, user_node, recursion):
        if level > 50:
            self.warn("more than 50 slot levels; check for a part that fits inside itself")
            return
        parts_map = self.config.get("parts") if isinstance(self.config.get("parts"), dict) else None
        for slot in parent_slots:
            slot_opts = dict(_copy(slot_opts_in or {}))
            slot_opts.update(_copy(slot))
            sid = slot.get("name") or (slot.get("allowTypes") or [""])[0]
            rlevel = recursion.get(sid, 0) + 1
            if isinstance(slot.get("maxRecursion"), (int, float)) and rlevel > slot["maxRecursion"]:
                continue
            new_path = path + str(sid) + "/"
            for k in ("name", "type", "allowTypes", "denyTypes", "default", "description", "maxRecursion"):
                slot_opts.pop(k, None)
            options = self.compatible(slot)
            entry = {"id": sid, "path": new_path, "description": slot.get("description") or sid,
                     "default": slot.get("default"), "allowTypes": slot.get("allowTypes") or [],
                     "coreSlot": bool(slot.get("coreSlot")),
                     "options": options, "chosen": "", "decision": "", "children": []}
            user = None
            if parts_map is not None:
                user = parts_map.get(sid)
                if user == "none":
                    user = ""
                if user is None:
                    user = parts_map.get(new_path)
            elif isinstance(user_node, dict):
                child = (user_node.get("children") or {}).get(sid)
                if isinstance(child, dict):
                    user = child.get("chosenPartName")
                    if user == "none":
                        user = ""
            tree_children.append(entry)
            if user == "":
                entry["decision"] = "user-empty"
                continue
            chosen = None
            if user:
                p = self.m.parts.get(user)
                if p is not None and self.fits(p, slot):
                    chosen = p
                    entry["decision"] = "user"
                elif p is not None:
                    self.warn('slot "%s": part "%s" has the wrong slot type; using the default' % (sid, user))
                else:
                    self.warn('slot "%s": part "%s" from the config was not found; using the default' % (sid, user))
            if chosen is None and slot.get("default") is not None:
                d = slot.get("default")
                if d == "":
                    entry["decision"] = "default-empty"
                    continue
                chosen = self.m.parts.get(d)
                entry["decision"] = "default"
                if chosen is None:
                    self.warn('slot "%s": default part "%s" was not found, slot left empty' % (sid, d))
            if chosen is None:
                continue
            entry["chosen"] = chosen.name
            if chosen.name not in options:
                options.insert(0, chosen.name)
            slot_opts.pop("coreSlot", None)
            slot_opts.pop("variables", None)
            child_copy = copy_part(chosen)
            child_slots = process_slots(chosen.data)
            child_user = None
            if isinstance(user_node, dict):
                child_user = (user_node.get("children") or {}).get(sid)
            self.journal_pre.append((parent_copy, child_copy, level, slot_opts, new_path + chosen.name, slot))
            self.active.append((new_path + chosen.name, chosen))
            if child_slots:
                prev = recursion.get(sid)
                recursion[sid] = rlevel
                self.fill(child_copy, child_slots, level + 1, slot_opts, new_path, entry["children"], child_user,
                          recursion)
                if prev is None:
                    recursion.pop(sid, None)
                else:
                    recursion[sid] = prev
            self.journal.append((parent_copy, child_copy, level, slot_opts, new_path + chosen.name, slot))

    # -- merging -------------------------------------------------------------
    @staticmethod
    def unify(target, source, slot_opts, part_path):
        for sec, val in source.items():
            if sec in ("slots", "slots2", "information"):
                continue
            if sec not in target:
                target[sec] = val
                if isinstance(val, list):
                    lso = dict(_copy(slot_opts) or {})
                    lso["partOrigin"] = source.get("partName")
                    lso["partPath"] = part_path
                    val.insert(1, lso)
                    val.append({k: "" for k in lso})
            elif isinstance(target[sec], (list, dict)) and isinstance(val, (list, dict)):
                tgt = target[sec]
                if isinstance(val, list):
                    if isinstance(tgt, dict):
                        continue  # list into dict: the game would mix keys; skip
                    lso = dict(_copy(slot_opts) or {})
                    lso["partOrigin"] = source.get("partName")
                    lso["partPath"] = part_path
                    tgt.append(lso)
                    tgt.extend(val[1:])
                    tgt.append({k: "" for k in lso})
                else:
                    if isinstance(tgt, list):
                        continue
                    for k, v in val.items():
                        if isinstance(v, (int, float)) and not isinstance(v, bool) and isinstance(k, str) \
                                and k.startswith("$") and len(k) > 2:
                            real = k[2:]
                            existing = tgt.get(real)
                            existing_mod = tgt.get(k)
                            if isinstance(existing_mod, (int, float)) and not isinstance(existing_mod, bool):
                                existing, real = existing_mod, k
                            if isinstance(existing, (int, float)) and not isinstance(existing, bool):
                                op = k[1]
                                if op == "+":
                                    tgt[real] = existing + v
                                elif op == "*":
                                    tgt[real] = existing * v
                                elif op == "<":
                                    tgt[real] = min(existing, v)
                                elif op == ">":
                                    tgt[real] = max(existing, v)
                                else:
                                    tgt[k] = v
                            else:
                                tgt[k] = v
                        else:
                            tgt[k] = v
            else:
                if sec not in ("slotType", "partName"):
                    target[sec] = val

    # -- main ----------------------------------------------------------------
    def run(self):
        m = self.m
        main_name = self.config.get("mainPartName")
        if not main_name or main_name not in m.parts:
            mains = m.main_parts()
            if not mains:
                raise ValueError("no part with slotType \"main\" in vehicles/%s" % m.name)
            main_name = mains[0]
        root_part = m.parts[main_name]
        root = copy_part(root_part)
        self.journal, self.journal_pre, self.active = [], [], [("/" + main_name, root_part)]
        tree = {"id": "main", "path": "/", "description": "Main", "chosen": main_name,
                "options": m.main_parts(), "children": [], "decision": "user", "allowTypes": ["main"],
                "default": None, "coreSlot": True}
        self.fill(root, process_slots(root_part.data), 1, None, "/", tree["children"],
                  self.config.get("partsTree") if isinstance(self.config.get("partsTree"), dict) else None, {})

        user_vals = self.config.get("vars") if isinstance(self.config.get("vars"), dict) else {}
        all_vars = self.get_all_variables(root, user_vals)
        self.process_components(root, all_vars)
        self.process_parts(root, all_vars)
        for parent, child, level, slot_opts, path, slot in self.journal:
            self.unify(parent, child, slot_opts, path)
        vehicle = root
        return self.scene(vehicle, tree, all_vars, main_name)

    def get_all_variables(self, root, user_vals):
        root_list = part_variables(root)
        self.vc.apply({"_v": root_list}, {}, True)
        scope = sanitize_vars(root_list, user_vals, self.warn)
        scope["$components"] = {"val": root.get("components"), "__var__": True}
        stack = {id(root): scope}
        all_list = list(root_list)
        index = {v.get("name"): i for i, v in enumerate(all_list)}
        for parent, part, level, slot_opts, path, slot in reversed(self.journal):
            pscope = stack.get(id(parent), scope)
            sv = self.vc.slot_vars(_copy(slot.get("variables") or {}), pscope)
            s2 = dict(pscope)
            s2.update(sv)
            plist = part_variables(part)
            if plist:
                self.vc.apply({"_v": plist}, s2, True)
                for v in plist:
                    n = v.get("name")
                    if n in index:
                        all_list[index[n]] = v
                    elif n:
                        index[n] = len(all_list)
                        all_list.append(v)
                s2.update(sanitize_vars(plist, user_vals))
            stack[id(part)] = s2
        return sanitize_vars(all_list, user_vals)

    def process_components(self, root, all_vars):
        comps = root.get("components") if isinstance(root.get("components"), dict) else {}
        root["components"] = comps
        for parent, part, level, slot_opts, path, slot in self.journal_pre:
            c = part.pop("components", None)
            if isinstance(c, dict):
                for k, v in c.items():
                    if isinstance(v, dict):
                        cur = comps.setdefault(k, {})
                        if isinstance(cur, dict):
                            cur.update(_copy(v))
                    else:
                        comps[k] = v

    def process_parts(self, root, all_vars):
        scope = dict(all_vars)
        scope["$components"] = {"val": root.get("components"), "__var__": True}
        comps = root.pop("components", None)
        if isinstance(comps, dict):
            self.vc.apply(comps, scope)
        self.vc.apply(root, scope)
        root["components"] = comps
        stack = {id(root): scope}
        for parent, part, level, slot_opts, path, slot in reversed(self.journal):
            pscope = stack.get(id(parent), scope)
            sv = self.vc.slot_vars(_copy(slot.get("variables") or {}), pscope)
            s2 = dict(pscope)
            s2.update(sv)
            stack[id(part)] = s2
            self.vc.apply(slot_opts, s2)
            self.vc.apply(part, s2)

    # -- scene ---------------------------------------------------------------
    def scene(self, vehicle, tree, all_vars, main_name):
        m = self.m
        options = {k: v for k, v in vehicle.items() if not isinstance(v, (list, dict))}
        tables = {}
        for sec, val in vehicle.items():
            if isinstance(val, list) and sec not in ("slots", "slots2", "variables") and val \
                    and isinstance(val[0], list):
                rows, warns = process_table(val, options, keep_src=True)
                for w in warns[:3]:
                    self.warn("%s: %s" % (sec, w))
                for r in rows.values():
                    _apply_scale(r)
                tables[sec] = rows

        files, file_index = [], {}

        def fidx(key):
            if key not in file_index:
                file_index[key] = len(files)
                files.append(m.files[key].to_json())
            return file_index[key]

        def src_of(row):
            s = row.get("__src")
            if not s:
                return None, None, None
            return fidx(s[0]), s[1], s[3]

        # raw rows, to give each element a key the save code can find again
        def raw_row(row):
            s = row.get("__src")
            if not s:
                return None
            part = m.parts.get(s[1])
            if part is None:
                return None
            sec = part.data.get(s[2])
            if isinstance(sec, list) and 0 <= s[3] < len(sec):
                return sec[s[3]]
            return None

        def link(name, what, row=None):
            i = node_index.get(name)
            if i is None and name is not None and not (row is not None and row.get("optional") is True):
                self.warn("%s: node %r not found" % (what, name))
            return i

        # many rows share the same options; send each distinct set once
        optsets, optset_index = [], {}

        def oset(d):
            d = _jsonable(d)
            key = json.dumps(d, sort_keys=True)
            i = optset_index.get(key)
            if i is None:
                i = optset_index[key] = len(optsets)
                optsets.append(d)
            return i

        # ---- nodes
        nodes, node_index, xforms, xf_index = [], {}, [None], {}
        for name, row in (tables.get("nodes") or {}).items():
            if not isinstance(name, str):
                continue
            raw = raw_row(row)
            rawpos = list(raw[1:4]) if isinstance(raw, list) and len(raw) >= 4 else [None, None, None]
            px, py, pz = (_num(row.get("posX"), None), _num(row.get("posY"), None), _num(row.get("posZ"), None))
            if px is None or py is None or pz is None:
                self.warn("node %s has a position that is not a number" % name)
                px, py, pz = px or 0.0, py or 0.0, pz or 0.0
            xf = node_transform(row, px)
            wx, wy, wz = apply_transform(xf, (px, py, pz))
            xi = 0
            if xf is not None:
                k = (xf[0], xf[1])
                if k not in xf_index:
                    xf_index[k] = len(xforms)
                    xforms.append({"q": list(xf[0]), "t": list(xf[1])})
                xi = xf_index[k]
            weight = _num(row.get("nodeWeight"), None)
            if weight is None:
                if row.get("nodeWeight") is not None:
                    self.warn("node %s: nodeWeight %r is not a number; the game uses 25" % (name, row.get("nodeWeight")))
                weight = DEFAULTS["nodeWeight"]
            f, pname, ri = src_of(row)
            inline_w = isinstance(raw, list) and len(raw) >= 5 and isinstance(raw[-1], dict) and "nodeWeight" in raw[-1]
            node = {
                "id": name, "p": [wx, wy, wz], "raw": [px, py, pz], "rawCells": _jsonable(rawpos), "xf": xi,
                "w": weight, "wInline": inline_w,
                "part": pname, "f": f, "row": ri, "rid": raw[0] if isinstance(raw, list) and raw else name,
                "col": row.get("collision", True) is not False,
                "sc": bool(row.get("selfCollision", False)),
                "fixed": bool(row.get("fixed", False)),
                "grp": _groups(row.get("group")),
                "bo": oset(_opts(row["__base"], True)), "io": _jsonable(row["__inline"]) if row.get("__inline") else None,
            }
            node_index[name] = len(nodes)
            nodes.append(node)

        # occurrence counters for duplicate rows inside one part
        def occ_key(counter, row, cells):
            s = row.get("__src")
            k = (s[0], s[1], s[2]) + tuple(cells) if s else None
            n = counter.get(k, 0)
            counter[k] = n + 1
            return n

        # ---- beams (+ hydros)
        beams = []
        for sec in ("beams", "hydros"):
            occ = {}
            for _, row in (tables.get(sec) or {}).items():
                a, b = row.get("id1:"), row.get("id2:")
                if not isinstance(a, str) or not isinstance(b, str):
                    continue
                ia, ib = link(a, sec, row), link(b, sec, row)
                if ia is None or ib is None:
                    continue
                raw = raw_row(row)
                rk = [raw[0], raw[1]] if isinstance(raw, list) and len(raw) >= 2 else [a, b]
                f, pname, ri = src_of(row)
                bt = "HYDRO" if sec == "hydros" else _beam_type(row.get("beamType"))
                beams.append({
                    "a": ia, "b": ib, "t": bt, "sec": sec,
                    "k": _jsonable(_num(row.get("beamSpring"), DEFAULTS["beamSpring"])),
                    "c": _jsonable(_num(row.get("beamDamp"), DEFAULTS["beamDamp"])),
                    "d": _jsonable(_num(row.get("beamDeform"), DEFAULTS["beamDeform"])),
                    "s": _jsonable(_num(row.get("beamStrength"), DEFAULTS["beamStrength"])),
                    "part": pname, "f": f, "row": ri, "rk": _jsonable(rk) + [occ_key(occ, row, rk)],
                    "bo": oset(_opts(row["__base"])), "io": _jsonable(row["__inline"]) if row.get("__inline") else None,
                })

        # ---- triangles and quads
        tris = []
        for sec, n in (("triangles", 3), ("quads", 4)):
            occ = {}
            for _, row in (tables.get(sec) or {}).items():
                ids = [row.get("id%d:" % i) for i in range(1, n + 1)]
                if not all(isinstance(x, str) for x in ids):
                    continue
                idx = [link(x, sec, row) for x in ids]
                if any(i is None for i in idx):
                    continue
                raw = raw_row(row)
                rk = list(raw[:n]) if isinstance(raw, list) and len(raw) >= n else ids
                f, pname, ri = src_of(row)
                tt = row.get("triangleType")
                base = {"part": pname, "f": f, "row": ri, "sec": sec, "rk": _jsonable(rk) + [occ_key(occ, row, rk)],
                        "nc": isinstance(tt, str) and "NONCOLLIDABLE" in tt,
                        "bo": oset(_opts(row["__base"])),
                        "io": _jsonable(row["__inline"]) if row.get("__inline") else None}
                if n == 3:
                    tris.append(dict(base, n=idx))
                else:
                    tris.append(dict(base, n=[idx[0], idx[1], idx[2]], half=0))
                    tris.append(dict(base, n=[idx[2], idx[3], idx[0]], half=1))

        # ---- wheels
        wheels = []
        for sec in WHEEL_SECTIONS:
            for _, row in (tables.get(sec) or {}).items():
                n1, n2 = row.get("node1:"), row.get("node2:")
                if not isinstance(n1, str) or not isinstance(n2, str):
                    continue
                i1, i2 = link(n1, sec, row), link(n2, sec, row)
                if i1 is None or i2 is None:
                    continue
                f, pname, ri = src_of(row)
                wheels.append({
                    "name": row.get("name"), "sec": sec, "n1": i1, "n2": i2,
                    "radius": _num(row.get("radius"), 0.3), "hubRadius": _num(row.get("hubRadius"), None),
                    "width": _num(row.get("tireWidth"), None) or _num(row.get("hubWidth"), None),
                    "hubWidth": _num(row.get("hubWidth"), None), "pressurePSI": _num(row.get("pressurePSI"), None),
                    "numRays": _num(row.get("numRays"), None),
                    "mass": _wheel_mass(row),
                    "part": pname, "f": f, "row": ri,
                    "o": oset(_opts(row)),
                })

        # ---- flexbodies
        flex = []
        for _, row in (tables.get("flexbodies") or {}).items():
            mesh = row.get("mesh")
            if not isinstance(mesh, str):
                continue
            f, pname, ri = src_of(row)
            pos = row.get("pos") if isinstance(row.get("pos"), dict) else None
            rot = row.get("rot") if isinstance(row.get("rot"), dict) else None
            scale = row.get("scale") if isinstance(row.get("scale"), dict) else None
            p = (_num((pos or {}).get("x"), 0.0), _num((pos or {}).get("y"), 0.0), _num((pos or {}).get("z"), 0.0))
            xf = node_transform(row, p[0])
            flex.append({
                "mesh": mesh, "groups": _groups(row.get("[group]:")), "part": pname, "f": f, "row": ri,
                "pos": list(p) if pos else None,
                "rot": [_num(rot.get(a), 0.0) for a in "xyz"] if rot else None,
                "scale": [_num(scale.get(a), 1.0) for a in "xyz"] if scale else None,
                "xf": {"q": list(xf[0]), "t": list(xf[1])} if xf else None,
            })

        # ---- refNodes
        ref = {}
        for _, row in (tables.get("refNodes") or {}).items():
            for k in ("ref:", "back:", "left:", "up:", "leftCorner:", "rightCorner:"):
                if isinstance(row.get(k), str):
                    ref[k[:-1]] = row[k]

        # ---- parts list
        parts = {}
        for path, p in self.active:
            parts[p.name] = {"title": p.title, "f": fidx(p.file.key), "slotType": p.slot_type, "path": path,
                             "common": p.in_common, "virtual": bool(getattr(p, "virtual", False))}

        variables = []
        for name, v in all_vars.items():
            if name.startswith("$components"):
                continue
            variables.append(_jsonable({k: v.get(k) for k in (
                "name", "title", "description", "category", "subCategory", "unit", "min", "max", "default", "val",
                "minDis", "maxDis", "stepDis", "step", "hidden")}))
        variables.sort(key=lambda v: ((v.get("category") or ""), (v.get("title") or v.get("name") or "")))

        all_parts = {}
        for slot in _walk(tree):
            for name in slot["options"]:
                if name not in all_parts and name in m.parts:
                    all_parts[name] = m.parts[name].title

        return {
            "model": m.name,
            "mainPart": main_name,
            "engine": _engine_summary(vehicle),
            "files": files,
            "nodes": nodes, "beams": beams, "tris": tris, "wheels": wheels, "flexbodies": flex,
            "xforms": xforms, "refNodes": ref, "optsets": optsets,
            "tree": tree, "parts": parts, "partTitles": all_parts, "variables": variables,
            "warnings": self.warnings,
            "sections": sorted(tables.keys()),
        }


def _wheel_mass(row):
    """Mass of the nodes the game makes for a pressure wheel (hub rings and tire rings)."""
    rays = _num(row.get("numRays"), None)
    if not rays:
        return None
    hub = _num(row.get("hubNodeWeight"), 0.0) or 0.0
    tire = _num(row.get("nodeWeight"), 0.0) or 0.0
    has_tire = row.get("hasTire") is not False
    return round(2 * rays * (hub + (tire if has_tire else 0.0)), 3)


def _table_pairs(t):
    """[[x, y], ...] of a two-column table like [["rpm", "torque"], [0, 0], ...]."""
    out = []
    if isinstance(t, list):
        for r in t[1:]:
            if isinstance(r, list) and len(r) >= 2:
                x, y = _num(r[0]), _num(r[1])
                if x is not None and y is not None:
                    out.append([x, y])
    return out


def engine_device(vehicle):
    """(name, electric) of the powertrain device that drives the vehicle: its first combustion engine, else its
    first electric motor."""
    found = {}
    rows = vehicle.get("powertrain")
    for r in rows if isinstance(rows, list) else []:
        if isinstance(r, list) and len(r) >= 2 and r[0] in ("combustionEngine", "electricMotor") \
                and isinstance(r[1], str):
            found.setdefault(r[0], r[1])
    if "combustionEngine" in found:
        return found["combustionEngine"], False
    if "electricMotor" in found:
        return found["electricMotor"], True
    return "mainEngine", False


def _engine_summary(vehicle):
    """The main engine's (or electric motor's) torque curve and limits, after all parts were merged."""
    name, electric = engine_device(vehicle)
    e = vehicle.get(name)
    if not isinstance(e, dict):
        return None
    out = {"torque": _table_pairs(e.get("torque")), "device": name, "electric": electric}
    for k in ("torqueModMult", "torqueModIntake", "torqueModExhaust"):
        if isinstance(e.get(k), list):
            out[k] = _table_pairs(e.get(k))
    for k in ("idleRPM", "maxRPM", "revLimiterRPM", "inertia"):
        v = _num(e.get(k))
        if v is not None:
            out[k] = v
    out["hasRevLimiter"] = e.get("hasRevLimiter")
    return _jsonable(out)


def _opts(row, node=False):
    """Options worth showing: no bookkeeping keys, no node offsets, no link columns."""
    return {k: v for k, v in row.items() if k not in HIDDEN_KEYS and not k.endswith(":")
            and not (k.startswith("node") and k not in ("nodeWeight", "nodeMaterial"))}


def _walk(slot):
    yield slot
    for c in slot.get("children") or []:
        yield from _walk(c)


def _groups(g):
    if isinstance(g, str):
        return [g] if g else []
    if isinstance(g, list):
        return [x for x in g if isinstance(x, str) and x]
    return []


def _apply_scale(row):
    for k in [k for k in row if isinstance(k, str) and k.startswith("scale") and len(k) > 5]:
        v = row[k]
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            target = k[5:]
            if isinstance(row.get(target), (int, float)) and not isinstance(row.get(target), bool):
                row[target] = row[target] * v
            del row[k]


def build(model, config):
    return Builder(model, config).run()
