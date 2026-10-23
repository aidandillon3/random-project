"""What a Vehicle Builder design is made of: the settings, their ranges and the body styles.

A design ("spec") is a flat dict of plain values in the units people use for
cars: metres for sizes, inches for tire and rim diameters, millimetres for tire
width, degrees, horsepower, km/h and kg. normalize() fills in what is missing
from the chosen body style, keeps every value in its range and returns them
with SI helpers for the generator.
"""
import math
import re
from types import SimpleNamespace

INCH = 0.0254

GROUPS = [
    {"id": "size", "title": "Size", "icon": "ruler"},
    {"id": "shape", "title": "Body shape", "icon": "car"},
    {"id": "wheels", "title": "Wheels and tires", "icon": "wheel"},
    {"id": "suspension", "title": "Suspension", "icon": "spring"},
    {"id": "engine", "title": "Engine and drivetrain", "icon": "engine"},
    {"id": "weight", "title": "Weight and brakes", "icon": "weight"},
    {"id": "looks", "title": "Paint and looks", "icon": "paint"},
]


def _r(key, label, group, unit, lo, hi, step, help_="", level="basic", **kw):
    d = {"key": key, "type": "range", "label": label, "group": group, "unit": unit, "min": lo, "max": hi,
         "step": step, "help": help_, "level": level}
    d.update(kw)
    return d


def _c(key, label, group, options, help_="", level="basic"):
    return {"key": key, "type": "choice", "label": label, "group": group, "options": options, "help": help_,
            "level": level}


def _b(key, label, group, help_="", level="more"):
    return {"key": key, "type": "bool", "label": label, "group": group, "help": help_, "level": level}


def _col(key, label, group, help_="", level="basic"):
    return {"key": key, "type": "color", "label": label, "group": group, "help": help_, "level": level}


TIRE_TYPES = [["street", "Street"], ["sport", "Sport"], ["allterrain", "All-terrain"], ["mud", "Mud"],
              ["slick", "Racing slick"]]
RIM_STYLES = [["spoke5", "5 spoke"], ["spoke6", "6 spoke"], ["mesh", "Mesh"], ["steel", "Steel"],
              ["beadlock", "Off-road beadlock"], ["dish", "Deep dish"]]

FIELDS = [
    # -- size
    _r("length", "Length", "size", "m", 2.4, 7.5, 0.01, "Bumper to bumper."),
    _r("width", "Width", "size", "m", 1.3, 2.6, 0.01, "The widest point of the body."),
    _r("height", "Height", "size", "m", 0.9, 3.0, 0.01, "Ground to roof."),
    _r("wheelbase", "Wheelbase", "size", "m", 1.6, 4.8, 0.01, "Distance between the front and rear wheel centres."),
    _r("frontOverhang", "Front overhang", "size", "m", 0.2, 1.8, 0.01,
       "From the front wheel centres to the front bumper. The rest of the length is the rear overhang.", "more"),
    _r("trackF", "Front track width", "size", "m", 1.0, 2.3, 0.01, "Distance between the centres of the front tires."),
    _r("trackR", "Rear track width", "size", "m", 1.0, 2.3, 0.01, "Distance between the centres of the rear tires."),
    _r("clearance", "Ground clearance", "size", "m", 0.05, 0.7, 0.005, "Height of the floor above the ground."),
    # -- shape
    _c("roof", "Roof", "shape", [["closed", "Hard top"], ["open", "Open top (roadster)"]]),
    _c("rear", "Back", "shape", [["closed", "Trunk or hatch"], ["bed", "Pickup bed"]]),
    _r("hoodLength", "Hood length", "shape", "m", 0.1, 2.8, 0.01, "Front bumper to the bottom of the windshield."),
    _r("windshieldAngle", "Windshield slope", "shape", "°", 15, 85, 1,
       "Laid back for sports cars, upright for trucks and vans."),
    _r("roofLength", "Roof length", "shape", "m", 0.2, 4.8, 0.01, "Top of the windshield to the back of the roof."),
    _r("rearAngle", "Rear window slope", "shape", "°", 8, 90, 1, "Shallow for fastbacks, upright for wagons and vans.",
       "more"),
    _r("beltline", "Window line", "shape", "m", 0.4, 1.9, 0.01, "Height of the bottom edge of the side windows.", "more"),
    _r("noseHeight", "Nose height", "shape", "m", 0.3, 1.7, 0.01, "Height of the front edge of the hood.", "more"),
    _r("tailHeight", "Tail height", "shape", "m", 0.3, 1.9, 0.01, "Height of the trunk lid or tailgate.", "more"),
    _r("roofWidth", "Roof width", "shape", "%", 0.6, 1.0, 0.01, "How much narrower the roof is than the body.", "more",
       display="percent"),
    _r("roundness", "Roundness", "shape", "%", 0.0, 1.0, 0.01, "Boxy to rounded corners.", "more", display="percent"),
    # -- wheels
    _r("tireDiaF", "Front tire size", "wheels", "in", 17, 50, 0.1, "Overall diameter of the front tires."),
    _r("tireDiaR", "Rear tire size", "wheels", "in", 17, 50, 0.1, "Overall diameter of the rear tires."),
    _r("tireWidthF", "Front tire width", "wheels", "mm", 125, 450, 5),
    _r("tireWidthR", "Rear tire width", "wheels", "mm", 125, 450, 5),
    _r("rimDiaF", "Front rim size", "wheels", "in", 12, 26, 1, "", "more"),
    _r("rimDiaR", "Rear rim size", "wheels", "in", 12, 26, 1, "", "more"),
    _c("tireType", "Tires", "wheels", TIRE_TYPES, "Grip on the road against grip in the dirt."),
    _r("pressureF", "Front tire pressure", "wheels", "psi", 8, 65, 0.5, "", "more"),
    _r("pressureR", "Rear tire pressure", "wheels", "psi", 8, 65, 0.5, "", "more"),
    _c("rimStyle", "Rim style", "wheels", RIM_STYLES, "", "more"),
    _col("rimColor", "Rim colour", "wheels", "", "more"),
    # -- suspension
    _c("suspF", "Front suspension", "suspension",
       [["wishbone", "Independent (double wishbone)"], ["solid", "Solid axle"]],
       "Independent rides and handles better; a solid axle is tough and flexes off-road."),
    _c("suspR", "Rear suspension", "suspension",
       [["wishbone", "Independent (double wishbone)"], ["solid", "Solid axle"]]),
    _r("travelF", "Front wheel travel", "suspension", "m", 0.08, 0.6, 0.005, "How far the wheels move up and down in total."),
    _r("travelR", "Rear wheel travel", "suspension", "m", 0.08, 0.6, 0.005),
    _r("springF", "Front springs", "suspension", "Hz", 0.7, 3.5, 0.05,
       "Soft and comfortable (low) to firm and sporty (high).", lowLabel="Soft", highLabel="Firm"),
    _r("springR", "Rear springs", "suspension", "Hz", 0.7, 3.5, 0.05, "", lowLabel="Soft", highLabel="Firm"),
    _r("dampF", "Front damping", "suspension", "", 0.1, 1.0, 0.01, "Bouncy (low) to tightly controlled (high).",
       "more", display="percent"),
    _r("dampR", "Rear damping", "suspension", "", 0.1, 1.0, 0.01, "", "more", display="percent"),
    _r("swaybarF", "Front anti-roll bar", "suspension", "", 0.0, 1.0, 0.01, "Less body roll in corners.", "more",
       display="percent"),
    _r("swaybarR", "Rear anti-roll bar", "suspension", "", 0.0, 1.0, 0.01, "", "more", display="percent"),
    _r("camberF", "Front camber", "suspension", "°", -5.0, 3.0, 0.1, "Negative tilts the tops of the wheels inward.",
       "more"),
    _r("camberR", "Rear camber", "suspension", "°", -5.0, 3.0, 0.1, "", "more"),
    _r("steerAngle", "Steering angle", "suspension", "°", 18, 55, 1, "How far the front wheels turn.", "more"),
    # -- engine
    _c("enginePos", "Engine position", "engine", [["front", "Front"], ["mid", "Middle"], ["rear", "Rear"]]),
    _r("power", "Power", "engine", "hp", 20, 2000, 5),
    _r("redline", "Redline", "engine", "rpm", 2500, 12000, 100, "", "more"),
    _r("torqueShape", "Engine character", "engine", "", 0.0, 1.0, 0.01,
       "Low-end pull like a truck or diesel, or high-revving like a race engine.", "more",
       lowLabel="Low-end pull", highLabel="High-revving"),
    _c("sound", "Engine sound", "engine", [["i4", "Inline 4"], ["b4", "Flat 4"]], "", "more"),
    _c("drive", "Driven wheels", "engine", [["RWD", "Rear (RWD)"], ["FWD", "Front (FWD)"], ["AWD", "All (AWD)"]]),
    _c("gearbox", "Gearbox", "engine", [["manual", "Manual"], ["automatic", "Automatic"]]),
    _r("gears", "Gears", "engine", "", 3, 8, 1, "", "more"),
    _r("topSpeed", "Top speed", "engine", "km/h", 50, 450, 5, "Sets the gearing.", "more"),
    _c("diff", "Differential", "engine", [["open", "Open"], ["lsd", "Limited slip"], ["locked", "Locked (welded)"]],
       "Locked and limited slip put power down better off-road and in drifts.", "more"),
    _r("fuel", "Fuel tank", "engine", "L", 5, 300, 1, "", "more"),
    # -- weight
    _r("mass", "Weight", "weight", "kg", 250, 8000, 10, "The weight of the vehicle ready to drive."),
    _r("weightBias", "Weight balance", "weight", "", -0.15, 0.15, 0.005,
       "Move the body's weight toward the front (negative) or the rear (positive).", "more",
       lowLabel="Front", highLabel="Rear"),
    _r("brakePower", "Brake power", "weight", "×", 0.4, 2.0, 0.05, "", "more"),
    _r("brakeBias", "Front brake share", "weight", "%", 0.4, 0.85, 0.01, "", "more", display="percent"),
    _b("abs", "Anti-lock brakes (ABS)", "weight"),
    # -- looks
    _col("paint", "Paint", "looks"),
    _r("metallic", "Metallic", "looks", "%", 0.0, 1.0, 0.01, "", "more", display="percent"),
    _r("glassTint", "Window tint", "looks", "%", 0.0, 1.0, 0.01, "", "more", display="percent"),
    _c("trim", "Bumpers and trim", "looks", [["black", "Black plastic"], ["body", "Body colour"], ["chrome", "Chrome"]],
       "", "more"),
]
FIELD = {f["key"]: f for f in FIELDS}

# a sensible family sedan; every style starts from this and changes what makes it that style
BASE = {
    "style": "sedan",
    "length": 4.7, "width": 1.84, "height": 1.45, "wheelbase": 2.8, "frontOverhang": 0.95,
    "trackF": 1.58, "trackR": 1.58, "clearance": 0.14,
    "roof": "closed", "rear": "closed",
    "hoodLength": 1.3, "windshieldAngle": 27, "roofLength": 1.18, "rearAngle": 30, "beltline": 0.95,
    "noseHeight": 0.74, "tailHeight": 1.0, "roofWidth": 0.8, "roundness": 0.55,
    "tireDiaF": 26.0, "tireDiaR": 26.0, "tireWidthF": 225, "tireWidthR": 225, "rimDiaF": 17, "rimDiaR": 17,
    "tireType": "street", "pressureF": 32, "pressureR": 32, "rimStyle": "spoke5", "rimColor": "#c4c8cc",
    "suspF": "wishbone", "suspR": "wishbone", "travelF": 0.2, "travelR": 0.22, "springF": 1.35, "springR": 1.5,
    "dampF": 0.35, "dampR": 0.35, "swaybarF": 0.5, "swaybarR": 0.25, "camberF": -0.8, "camberR": -1.2,
    "steerAngle": 36,
    "enginePos": "front", "power": 200, "redline": 6500, "torqueShape": 0.5, "sound": "i4", "drive": "RWD",
    "gearbox": "manual", "gears": 6, "topSpeed": 220, "diff": "open", "fuel": 60,
    "mass": 1500, "weightBias": 0.0, "brakePower": 1.0, "brakeBias": 0.65, "abs": True,
    "paint": "#8c1c22", "metallic": 0.6, "glassTint": 0.55, "trim": "black",
}

STYLES = [
    {"id": "sedan", "title": "Sedan", "text": "Four doors and a trunk.", "values": {}},
    {"id": "hatchback", "title": "Hatchback", "text": "Small and practical, with a hatch at the back.", "values": {
        "length": 4.15, "width": 1.78, "height": 1.47, "wheelbase": 2.6, "frontOverhang": 0.85,
        "trackF": 1.53, "trackR": 1.52, "hoodLength": 1.05, "windshieldAngle": 30, "roofLength": 1.6,
        "rearAngle": 62, "beltline": 0.96, "noseHeight": 0.76, "tailHeight": 0.98, "roofWidth": 0.83,
        "tireDiaF": 24.5, "tireDiaR": 24.5, "tireWidthF": 205, "tireWidthR": 205, "rimDiaF": 16, "rimDiaR": 16,
        "power": 130, "drive": "FWD", "mass": 1200, "paint": "#1f5fae", "swaybarR": 0.45, "springR": 1.6}},
    {"id": "coupe", "title": "Coupe", "text": "Two doors, long hood, sloping roof.", "values": {
        "length": 4.55, "width": 1.88, "height": 1.32, "wheelbase": 2.68, "frontOverhang": 0.92,
        "trackF": 1.6, "trackR": 1.6, "clearance": 0.12, "hoodLength": 1.55, "windshieldAngle": 24,
        "roofLength": 0.95, "rearAngle": 19, "beltline": 0.9, "noseHeight": 0.66, "tailHeight": 0.92,
        "roofWidth": 0.76, "tireDiaF": 26.0, "tireDiaR": 26.5, "tireWidthF": 245, "tireWidthR": 275,
        "rimDiaF": 18, "rimDiaR": 18, "tireType": "sport", "power": 380, "redline": 7000, "torqueShape": 0.6,
        "mass": 1550, "diff": "lsd", "springF": 1.6, "springR": 1.75, "dampF": 0.45, "dampR": 0.45,
        "topSpeed": 270, "paint": "#e8b923"}},
    {"id": "wagon", "title": "Wagon", "text": "A long roof and a big boot.", "values": {
        "length": 4.85, "height": 1.5, "wheelbase": 2.85, "hoodLength": 1.25, "roofLength": 2.1,
        "rearAngle": 72, "tailHeight": 1.02, "roofWidth": 0.84, "mass": 1600, "paint": "#2f5d46"}},
    {"id": "suv", "title": "SUV", "text": "Tall, roomy and good on rough roads.", "values": {
        "length": 4.8, "width": 1.95, "height": 1.8, "wheelbase": 2.85, "frontOverhang": 0.9,
        "trackF": 1.66, "trackR": 1.66, "clearance": 0.22, "hoodLength": 1.25, "windshieldAngle": 33,
        "roofLength": 2.05, "rearAngle": 70, "beltline": 1.15, "noseHeight": 1.0, "tailHeight": 1.22,
        "roofWidth": 0.84, "roundness": 0.5, "tireDiaF": 30.0, "tireDiaR": 30.0, "tireWidthF": 255,
        "tireWidthR": 255, "rimDiaF": 18, "rimDiaR": 18, "tireType": "allterrain", "travelF": 0.24,
        "travelR": 0.26, "power": 280, "drive": "AWD", "gearbox": "automatic", "gears": 8, "mass": 2100,
        "topSpeed": 210, "paint": "#3d4b57"}},
    {"id": "offroad", "title": "Off-roader", "text": "Big tires, solid axles and lots of travel.", "values": {
        "length": 4.35, "width": 1.92, "height": 1.95, "wheelbase": 2.6, "frontOverhang": 0.72,
        "trackF": 1.68, "trackR": 1.68, "clearance": 0.33, "hoodLength": 1.2, "windshieldAngle": 52,
        "roofLength": 1.85, "rearAngle": 84, "beltline": 1.32, "noseHeight": 1.18, "tailHeight": 1.3,
        "roofWidth": 0.92, "roundness": 0.15, "tireDiaF": 35.0, "tireDiaR": 35.0, "tireWidthF": 315,
        "tireWidthR": 315, "rimDiaF": 17, "rimDiaR": 17, "tireType": "mud", "pressureF": 22, "pressureR": 22,
        "rimStyle": "beadlock", "rimColor": "#2b2d30", "suspF": "solid", "suspR": "solid", "travelF": 0.36,
        "travelR": 0.4, "springF": 1.15, "springR": 1.25, "swaybarF": 0.2, "swaybarR": 0.0, "camberF": 0.0,
        "camberR": 0.0, "steerAngle": 38, "power": 285, "redline": 5500, "torqueShape": 0.2, "drive": "AWD",
        "gearbox": "automatic", "gears": 5, "topSpeed": 160, "diff": "locked", "fuel": 80, "mass": 2100,
        "paint": "#5b6b3a", "trim": "black"}},
    {"id": "pickup", "title": "Pickup truck", "text": "A cab up front and an open bed at the back.", "values": {
        "rear": "bed", "length": 5.45, "width": 2.0, "height": 1.88, "wheelbase": 3.4, "frontOverhang": 0.95,
        "trackF": 1.72, "trackR": 1.72, "clearance": 0.25, "hoodLength": 1.45, "windshieldAngle": 34,
        "roofLength": 1.25, "rearAngle": 86, "beltline": 1.28, "noseHeight": 1.2, "tailHeight": 1.28,
        "roofWidth": 0.88, "roundness": 0.3, "tireDiaF": 31.5, "tireDiaR": 31.5, "tireWidthF": 265,
        "tireWidthR": 265, "rimDiaF": 17, "rimDiaR": 17, "tireType": "allterrain", "rimStyle": "spoke6",
        "suspF": "wishbone", "suspR": "solid", "travelF": 0.24, "travelR": 0.3, "springF": 1.3,
        "springR": 1.45, "swaybarR": 0.1, "camberF": -0.3, "camberR": 0.0, "power": 380, "redline": 5800,
        "torqueShape": 0.3, "gearbox": "automatic", "gears": 6, "topSpeed": 190, "fuel": 100, "mass": 2300,
        "paint": "#b8bcc2", "trim": "chrome"}},
    {"id": "van", "title": "Van", "text": "A box on wheels: lots of room.", "values": {
        "length": 5.3, "width": 2.0, "height": 2.25, "wheelbase": 3.35, "frontOverhang": 0.85,
        "trackF": 1.72, "trackR": 1.72, "clearance": 0.17, "hoodLength": 0.75, "windshieldAngle": 40,
        "roofLength": 3.85, "rearAngle": 86, "beltline": 1.08, "noseHeight": 1.0, "tailHeight": 1.15,
        "roofWidth": 0.94, "roundness": 0.35, "tireDiaF": 28.0, "tireDiaR": 28.0, "tireWidthF": 235,
        "tireWidthR": 235, "rimDiaF": 16, "rimDiaR": 16, "rimStyle": "steel", "rimColor": "#3a3d42",
        "suspR": "solid", "travelR": 0.24, "camberR": 0.0, "power": 170, "redline": 5200, "torqueShape": 0.25,
        "drive": "RWD", "gearbox": "automatic", "gears": 6, "topSpeed": 165, "mass": 2400, "fuel": 80,
        "paint": "#e9ecef"}},
    {"id": "roadster", "title": "Roadster", "text": "Two seats and no roof.", "values": {
        "roof": "open", "length": 4.0, "width": 1.75, "height": 1.25, "wheelbase": 2.35, "frontOverhang": 0.82,
        "trackF": 1.5, "trackR": 1.5, "clearance": 0.12, "hoodLength": 1.45, "windshieldAngle": 30,
        "roofLength": 0.9, "rearAngle": 25, "beltline": 0.88, "noseHeight": 0.68, "tailHeight": 0.9,
        "roundness": 0.75, "tireDiaF": 24.5, "tireDiaR": 24.5, "tireWidthF": 205, "tireWidthR": 215,
        "rimDiaF": 16, "rimDiaR": 16, "tireType": "sport", "power": 180, "redline": 7200, "torqueShape": 0.65,
        "sound": "b4", "mass": 1050, "springF": 1.7, "springR": 1.8, "diff": "lsd", "topSpeed": 220,
        "paint": "#2369c4"}},
    {"id": "supercar", "title": "Supercar", "text": "Low, wide and mid-engined.", "values": {
        "length": 4.5, "width": 1.98, "height": 1.16, "wheelbase": 2.65, "frontOverhang": 1.0,
        "trackF": 1.68, "trackR": 1.65, "clearance": 0.1, "hoodLength": 1.25, "windshieldAngle": 21,
        "roofLength": 1.0, "rearAngle": 14, "beltline": 0.84, "noseHeight": 0.6, "tailHeight": 0.98,
        "roofWidth": 0.68, "roundness": 0.8, "tireDiaF": 26.0, "tireDiaR": 27.5, "tireWidthF": 255,
        "tireWidthR": 315, "rimDiaF": 20, "rimDiaR": 20, "tireType": "sport", "rimStyle": "spoke5",
        "rimColor": "#2b2d30", "travelF": 0.15, "travelR": 0.16, "springF": 2.0, "springR": 2.2, "dampF": 0.5,
        "dampR": 0.5, "swaybarF": 0.7, "swaybarR": 0.5, "camberF": -1.8, "camberR": -2.2, "steerAngle": 32,
        "enginePos": "mid", "power": 650, "redline": 8500, "torqueShape": 0.75, "drive": "RWD", "gears": 7,
        "topSpeed": 330, "diff": "lsd", "mass": 1500, "brakePower": 1.3, "brakeBias": 0.6,
        "paint": "#d24a12", "metallic": 0.7}},
]
STYLE = {s["id"]: s for s in STYLES}


def style_values(style_id):
    """A complete design for a body style."""
    st = STYLE.get(style_id) or STYLE["sedan"]
    out = dict(BASE)
    out.update(st["values"])
    out["style"] = st["id"]
    return out


def schema():
    """Everything the page needs to draw the Builder's controls."""
    return {
        "groups": GROUPS,
        "fields": FIELDS,
        "styles": [{"id": s["id"], "title": s["title"], "text": s["text"], "values": style_values(s["id"])}
                   for s in STYLES],
    }


def _num(v, d):
    if isinstance(v, bool):
        return d
    if isinstance(v, (int, float)) and math.isfinite(v):
        return float(v)
    if isinstance(v, str):
        try:
            f = float(v)
            return f if math.isfinite(f) else d
        except ValueError:
            return d
    return d


def _color(v, d):
    if isinstance(v, str) and re.match(r"^#[0-9a-fA-F]{6}$", v):
        return v.lower()
    return d


def rgb(hexstr):
    return [int(hexstr[k:k + 2], 16) / 255.0 for k in (1, 3, 5)]


def clean(spec):
    """The design with every known setting present and in range (plain values, same units)."""
    spec = spec if isinstance(spec, dict) else {}
    base = style_values(spec.get("style"))
    out = {"style": base["style"]}
    for f in FIELDS:
        k = f["key"]
        v = spec.get(k, base[k])
        if f["type"] == "range":
            x = min(max(_num(v, base[k]), f["min"]), f["max"])
            if f["step"] >= 1 and float(f["step"]).is_integer():
                x = round(x)
            out[k] = x
        elif f["type"] == "choice":
            vals = [o[0] for o in f["options"]]
            out[k] = v if v in vals else base[k]
        elif f["type"] == "bool":
            out[k] = bool(v) if isinstance(v, (bool, int)) else bool(base[k])
        elif f["type"] == "color":
            out[k] = _color(v, base[k])
    return out


def normalize(spec):
    """(P, notes): the cleaned design as attributes, made consistent, plus SI helpers.

    notes are short sentences about settings that had to give way to others."""
    s = clean(spec)
    notes = []

    def note(msg):
        if msg not in notes:
            notes.append(msg)

    # sizes that depend on each other
    if s["wheelbase"] > s["length"] - 0.5:
        s["wheelbase"] = round(s["length"] - 0.5, 3)
        note("The wheelbase was shortened to fit the length.")
    max_fo = s["length"] - s["wheelbase"] - 0.2
    if s["frontOverhang"] > max_fo:
        s["frontOverhang"] = round(max(0.2, max_fo), 3)
        note("The front overhang was shortened to leave room for the rear.")
    for ax in "FR":
        rim_max = s["tireDia" + ax] - 3.0
        if s["rimDia" + ax] > rim_max:
            s["rimDia" + ax] = max(12, math.floor(rim_max))
            note("The %s rims were made smaller so the tires have a sidewall." % ("front" if ax == "F" else "rear"))
    if s["height"] < s["clearance"] + 0.55:
        s["height"] = round(s["clearance"] + 0.55, 3)
        note("The height was raised to make room for the cabin.")
    lo_belt = s["clearance"] + 0.3
    hi_belt = s["height"] - (0.08 if s["roof"] == "open" else 0.22)
    if s["beltline"] < lo_belt or s["beltline"] > hi_belt:
        s["beltline"] = round(min(max(s["beltline"], lo_belt), hi_belt), 3)
        note("The window line was moved to fit between the floor and the roof.")
    s["noseHeight"] = round(min(max(s["noseHeight"], s["clearance"] + 0.2), s["beltline"] + 0.05), 3)
    s["tailHeight"] = round(min(max(s["tailHeight"], s["clearance"] + 0.25), s["height"] - 0.05), 3)
    if s["hoodLength"] > s["length"] - 1.0:
        s["hoodLength"] = round(s["length"] - 1.0, 3)
        note("The hood was shortened to leave room for the cabin.")

    P = SimpleNamespace(**s)
    P.notes = notes
    # SI helpers
    P.rF, P.rR = s["tireDiaF"] * INCH / 2, s["tireDiaR"] * INCH / 2
    P.rimF, P.rimR = s["rimDiaF"] * INCH / 2, s["rimDiaR"] * INCH / 2
    P.twF, P.twR = s["tireWidthF"] / 1000.0, s["tireWidthR"] / 1000.0
    P.rearOverhang = s["length"] - s["wheelbase"] - s["frontOverhang"]
    P.openTop = s["roof"] == "open"
    P.bed = s["rear"] == "bed"
    P.paintRGB = rgb(s["paint"])
    P.rimRGB = rgb(s["rimColor"])
    P.kw = s["power"] * 0.7457
    return P, notes


def tire_size_text(width_mm, dia_in, rim_in):
    """ "245/45R18" style size of a tire."""
    side_mm = (dia_in - rim_in) * 25.4 / 2
    aspect = int(round(side_mm / max(width_mm, 1) * 100 / 5.0) * 5)
    return "%d/%dR%d" % (int(round(width_mm)), aspect, int(round(rim_in)))


def model_id(text):
    """A folder-safe vehicle id from a name: "My Truck 2" -> "my_truck_2"."""
    s = re.sub(r"[^a-z0-9]+", "_", (text or "").lower()).strip("_")
    if not s:
        s = "my_vehicle"
    if s[0].isdigit():
        s = "v_" + s
    return s[:40]
