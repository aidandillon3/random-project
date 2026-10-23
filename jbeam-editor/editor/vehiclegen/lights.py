"""Head, tail, brake and reverse lights that light up the road, not just glow.

The game casts light from SPOTLIGHT props. Each lamp gets a small square of
four nodes on the lamp's face, tied to the body, and the props sit on it the
way BeamNG's own vehicles do: reference node at the upper corner with the
smaller x, its X axis node straight below, its Y axis node across at the same
height. A spotlight shines backwards unless turned half round, so headlights
get baseRotation x 180 and sit 10 cm behind the face; rear lights are not
turned and sit 10 cm in front of theirs.
"""
from .chassis import FLT_MAX

HALF = 0.06          # half the side of a lamp's node square

HEAD = {"lightInnerAngle": 0, "lightOuterAngle": 120, "lightColor": {"r": 252, "g": 252, "b": 252, "a": 255},
        "lightAttenuation": {"x": 0, "y": 1, "z": 1}, "lightCastShadows": True, "flareName": "vehicleHeadLightFlare",
        "cookieName": "art/special/BNG_light_cookie_headlight.dds", "texSize": 512, "shadowSoftness": 0.5}
TAIL = {"lightInnerAngle": 0, "lightOuterAngle": 120, "lightColor": {"r": 201, "g": 26, "b": 9, "a": 255},
        "lightAttenuation": {"x": 0, "y": 1, "z": 1}, "lightCastShadows": False, "flareName": "vehicleBrakeLightFlare"}
REVERSE = {"lightColor": {"r": 255, "g": 255, "b": 255, "a": 255}, "flareName": "vehicleReverseLightFlare"}

ZERO = {"x": 0, "y": 0, "z": 0}


def add_lamps(S, P, lamps, body_names, part="body"):
    """Nodes for each lamp: lamps = [(kind "head"/"tail", side "l"/"r", (x, y, z) centre of its face)].

    Returns [(kind, side, ref, x node, y node)] for props()."""
    S.kind("lamp", beamSpring=601000, beamDamp=30, beamDeform=40000, beamStrength=FLT_MAX)
    grp = [P.model_group("body")]
    out = []
    for kind, side, (cx, y, cz) in lamps:
        tag = "lmp%s%s" % (kind[0], side)
        corners = {"ref": (cx - HALF, y, cz + HALF), "x": (cx - HALF, y, cz - HALF), "y": (cx + HALF, y, cz + HALF),
                   "o": (cx + HALF, y, cz - HALF)}
        names = {}
        for i, key in enumerate(("ref", "x", "y", "o"), 1):
            n = S.node("%s%d" % (tag, i), corners[key], part, w=0.5, groups=grp, kind="lamp")
            n.coll, n.selfcoll = False, False
            names[key] = n.name
        sq = list(names.values())
        for i in range(4):
            for j in range(i + 1, 4):
                S.beam(sq[i], sq[j], part, "lamp")
        for nm in sq:
            S.attach(nm, body_names, part, "lamp", n=3)
        out.append((kind, side, names["ref"], names["x"], names["y"]))
    return out


def _row(func, ref, xn, yn, rot_x, opts):
    return [func, "SPOTLIGHT", ref, xn, yn, {"x": rot_x, "y": 0, "z": 0}, dict(ZERO), dict(ZERO), 0, 0, 0, 1, opts]


def props(lamps):
    """The props table for the lamps add_lamps made (None when there are none).

    Option rows carry on to the rows after them, so the rear lights come first and the headlights' row,
    which sets every option it uses, last."""
    if not lamps:
        return None
    rows = [["func", "mesh", "idRef:", "idX:", "idY:", "baseRotation", "rotation", "translation", "min", "max",
             "offset", "multiplier"]]
    heads = [lp for lp in lamps if lp[0] == "head"]
    tails = [lp for lp in lamps if lp[0] == "tail"]
    if tails:
        rows.append(dict(TAIL))
        for _, _, ref, xn, yn in tails:
            inside = {"x": 0.5, "y": 0.5, "z": 0.1}
            rows.append(_row("brake", ref, xn, yn, 0, {"baseTranslation": dict(inside), "lightOuterAngle": 75,
                                                       "flareScale": 0.07, "lightBrightness": 0.06, "lightRange": 8}))
            rows.append(_row("lowhighbeam", ref, xn, yn, 0, {"baseTranslation": dict(inside), "lightOuterAngle": 75,
                                                             "flareScale": 0.05, "lightBrightness": 0.03,
                                                             "lightRange": 10}))
        rows.append(dict(REVERSE))
        for _, _, ref, xn, yn in tails:
            rows.append(_row("reverse", ref, xn, yn, 0, {"baseTranslation": {"x": 0.5, "y": 0.5, "z": 0.1},
                                                         "flareScale": 0.05, "lightBrightness": 0.06,
                                                         "lightRange": 12}))
    if heads:
        rows.append(dict(HEAD))
        for _, _, ref, xn, yn in heads:
            inside = {"x": 0.5, "y": 0.5, "z": -0.1}
            rows.append(_row("lowbeam", ref, xn, yn, 180, {"baseTranslation": dict(inside), "lightRange": 50,
                                                          "lightBrightness": 0.4, "lightOuterAngle": 75,
                                                          "flareScale": 0.07}))
            rows.append(_row("highbeam", ref, xn, yn, 180, {"baseTranslation": dict(inside), "lightRange": 80,
                                                           "lightBrightness": 0.7, "lightOuterAngle": 90,
                                                           "flareScale": 0.08}))
    return rows


HORN = "event:>Vehicle>Electrics>Horns>MER_V2"


def soundscape(node):
    """The horn (H in the game), sounding from a node at the front."""
    return [["name", "src"], {"node:": node}, ["horn", HORN]]
