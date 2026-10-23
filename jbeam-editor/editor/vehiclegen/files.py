"""Writing a generated vehicle as a mod: vehicles/<model>/ with JBeam, meshes, materials, info and a config."""
import json
import os
import uuid

from .. import daewrite, jbeamfile
from . import spec as specmod

DESIGN_FILE = "jbeam_editor_builder.json"
CONFIG_NAME = "base"


def material_json(name, d):
    stage = {"baseColorFactor": list(d["color"]) + [d.get("opacity", 1.0)], "metallicFactor": d.get("metallic", 0.0),
             "roughnessFactor": d.get("roughness", 0.5)}
    if d.get("paint"):
        stage["instanceDiffuse"] = True
        stage["clearCoatFactor"] = 1.0
        stage["clearCoatRoughnessFactor"] = 0.05
    if d.get("emissive_on"):
        stage["emissiveFactor"] = d["emissive_on"]
        stage["glow"] = True
    out = {
        "name": name, "mapTo": name, "class": "Material",
        "persistentId": str(uuid.uuid5(uuid.NAMESPACE_URL, "jbeam-editor-builder/" + name)),
        "Stages": [stage, {}, {}, {}],
        "materialTag0": "beamng", "materialTag1": "vehicle", "version": 1.5,
    }
    if d.get("glass"):
        out["translucent"] = True
        out["translucentBlendOp"] = "PreMulAlpha"
        out["translucentZWrite"] = False
        stage["opacityFactor"] = d.get("opacity", 0.6)
    return out


def materials_file(g):
    data = {}
    for name, d in g.materials.items():
        data[name] = material_json(name, d)
        if d.get("glow"):
            for suffix, f in (("_on", 0.6), ("_on_intense", 1.0)):
                on = dict(d)
                on["emissive_on"] = [round(c * f, 3) for c in d["emissive"]]
                data[name + suffix] = material_json(name + suffix, on)
    return json.dumps(data, indent=2) + "\n"


def glow_map(g):
    m = g.P.model
    out = {}
    if m + "_headlight" in g.materials:
        out[m + "_headlight"] = {"simpleFunction": {"lowbeam": 0.49, "highbeam": 1}, "off": m + "_headlight",
                                 "on": m + "_headlight_on", "on_intense": m + "_headlight_on_intense"}
    if m + "_taillight" in g.materials:
        out[m + "_taillight"] = {"simpleFunction": {"lowhighbeam": 0.49, "brake": 1}, "off": m + "_taillight",
                                 "on": m + "_taillight_on", "on_intense": m + "_taillight_on_intense"}
    return out


def paint(g):
    P = g.P
    return {"baseColor": [round(c, 4) for c in P.paintRGB] + [1.2], "metallic": round(P.metallic, 3),
            "roughness": round(0.45 - 0.25 * P.metallic, 3), "clearcoat": 1, "clearcoatRoughness": 0.05}


def info_json(g):
    P = g.P
    style = specmod.STYLE.get(P.style) or specmod.STYLE["sedan"]
    p = paint(g)
    truck = P.style in ("pickup", "van") or P.mass > 2600
    return {
        "Name": P.name, "Brand": P.brand, "Author": P.author or "JBeam Editor", "Country": "Custom",
        "Type": "Truck" if truck else "Car", "Body Style": style["title"],
        "default_pc": CONFIG_NAME, "Years": {"min": 2026, "max": 2026},
        "Derby Class": "Custom",
        "paints": {"Factory": p},
        "defaultPaintName1": "Factory", "defaultPaintName2": "Factory", "defaultPaintName3": "Factory",
        "colors": {"Factory": "%s %s %s 1.2" % tuple(round(c, 3) for c in P.paintRGB)},
        "default_color": "Factory",
        "jbeamEditor": {"builder": True, "version": 1},
    }


def config_json(g):
    p = paint(g)
    return {"format": 2, "model": g.P.model, "mainPartName": g.P.model, "parts": {}, "vars": {},
            "paints": [p, p, p]}


def config_info(g):
    P, st = g.P, g.stats
    return {
        "Configuration": "Base", "Config Type": "Factory", "Source": "Custom",
        "Description": "Built with the JBeam Editor's Vehicle Builder.",
        "Value": int(15000 + P.power * 80), "Power": int(st["powerHP"]), "Torque": int(st["torqueNm"]),
        "Weight": int(st["mass"]), "Drivetrain": P.drive, "Propulsion": "ICE", "Fuel Type": "Gasoline",
        "Transmission": "Automatic" if P.gearbox == "automatic" else "Manual",
        "Top Speed": round(st["topSpeed"] / 3.6, 2) if st.get("topSpeed") else None,
        "0-100 km/h": st.get("zeroTo100"),
    }


def file_texts(g):
    """{relative path inside vehicles/<model>/: text or bytes} for the whole vehicle."""
    P = g.P
    m = P.model
    parts = dict(g.parts)
    body = parts.get(m + "_body")
    if body is not None:
        body = dict(body)
        gm = glow_map(g)
        if gm:
            body["glowMap"] = gm
        parts[m + "_body"] = body
    head = "%s, made with the JBeam Editor's Vehicle Builder.\nOpen it in the Builder again to change it; edits made here " \
           "are kept until you rebuild." % P.name
    groups = {
        m + ".jbeam": [m, m + "_body"],
        m + "_suspension.jbeam": [k for k in parts if any(k.startswith(m + "_" + w) for w in
                                                         ("suspension_", "coilover_", "brake_", "wheel_", "tire_",
                                                          "wheeldata_"))],
        m + "_powertrain.jbeam": [k for k in parts if any(k.startswith(m + "_" + w) for w in
                                                         ("engine", "transmission", "differential_", "fueltank"))],
    }
    out = {}
    for fn, names in groups.items():
        out[fn] = jbeamfile.dumps_part_file({k: parts[k] for k in names if k in parts}, comment=head)
    objects = []
    for name, M in g.meshes.items():
        idx, grp = M.merged()
        objects.append((name, M.p, [(mat, idx[s:s + c]) for s, c, mat in grp], None))
    out[m + ".dae"] = daewrite.dae_multi(objects)
    out["main.materials.json"] = materials_file(g)
    out["info.json"] = jbeamfile.dumps_json(info_json(g))
    out[CONFIG_NAME + ".pc"] = jbeamfile.dumps_json(config_json(g))
    out["info_%s.json" % CONFIG_NAME] = jbeamfile.dumps_json({k: v for k, v in config_info(g).items() if v is not None})
    out[DESIGN_FILE] = jbeamfile.dumps_json({"version": 1, "spec": g.spec, "stats": {
        k: g.stats[k] for k in ("mass", "powerHP", "torqueNm", "zeroTo100", "topSpeed")}})
    return out


def write(g, vdir, picture=None, backup_dir=None):
    """Write the vehicle into vdir (vehicles/<model>/). Returns the written paths."""
    os.makedirs(vdir, exist_ok=True)
    texts = file_texts(g)
    if picture:
        texts["default.jpg" if picture[:3] == b"\xff\xd8\xff" else "default.png"] = picture
    written = []
    for rel, data in texts.items():
        path = os.path.join(vdir, rel)
        if backup_dir and os.path.exists(path):
            os.makedirs(backup_dir, exist_ok=True)
            with open(path, "rb") as f:
                old = f.read()
            with open(os.path.join(backup_dir, rel), "wb") as f:
                f.write(old)
        tmp = path + ".jbeam-editor-tmp"
        mode = "wb" if isinstance(data, bytes) else "w"
        with open(tmp, mode, **({} if isinstance(data, bytes) else {"encoding": "utf-8", "newline": "\n"})) as f:
            f.write(data)
        os.replace(tmp, path)
        written.append(path)
    # the config's thumbnail uses the same picture
    if picture:
        ext = ".jpg" if picture[:3] == b"\xff\xd8\xff" else ".png"
        path = os.path.join(vdir, CONFIG_NAME + ext)
        with open(path, "wb") as f:
            f.write(picture)
        written.append(path)
    return written


def read_design(root, model):
    """The saved design of a vehicle made by the Builder, or None."""
    rel = "vehicles/%s/%s" % (model, DESIGN_FILE)
    try:
        if not root.has(rel):
            return None
        data = json.loads(jbeamfile.decode(root.read(rel)))
    except (OSError, KeyError, ValueError):
        return None
    sp = data.get("spec") if isinstance(data, dict) else None
    return sp if isinstance(sp, dict) else None
