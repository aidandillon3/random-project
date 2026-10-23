"""A small fake BeamNG install for the tests.

make_install(root) writes:
  root/game/content/vehicles/common.zip   game wheels and tires (read only)
  root/game/content/vehicles/testcar.zip  a test car laid out like an official vehicle
  root/user/mods/unpacked/                an empty mods folder (plus db.json)

The test car follows the structure of BeamNG's tutorial template: a frame with
slots for front and rear suspension; each suspension places a common rim with
a nodeOffset and has a "wheeldata" part with the wheel rows, a coilover part,
and an engine with a gearbox, final drive and differential.
"""
import json
import os
import struct
import zipfile
import zlib

HERE = os.path.dirname(os.path.abspath(__file__))
EDITOR = os.path.dirname(HERE)

import sys  # noqa: E402
if EDITOR not in sys.path:
    sys.path.insert(0, EDITOR)

from editor import daewrite, jbeamfile  # noqa: E402


# ---------------------------------------------------------------------------
# meshes
# ---------------------------------------------------------------------------

def box_mesh(cx, cy, cz, sx, sy, sz):
    """positions, indices of an axis-aligned box (outward winding)."""
    x0, x1 = cx - sx / 2, cx + sx / 2
    y0, y1 = cy - sy / 2, cy + sy / 2
    z0, z1 = cz - sz / 2, cz + sz / 2
    p = [x0, y0, z0, x1, y0, z0, x1, y1, z0, x0, y1, z0,
         x0, y0, z1, x1, y0, z1, x1, y1, z1, x0, y1, z1]
    f = [(0, 2, 1), (0, 3, 2), (4, 5, 6), (4, 6, 7), (0, 1, 5), (0, 5, 4),
         (1, 2, 6), (1, 6, 5), (2, 3, 7), (2, 7, 6), (3, 0, 4), (3, 4, 7)]
    return p, [i for t in f for i in t]


def cylinder_mesh(radius, width, n=16):
    """A wheel-like cylinder around the X axis, centred on the origin."""
    import math
    p, idx = [], []
    for side in (-1, 1):
        for k in range(n):
            a = 2 * math.pi * k / n
            p += [side * width / 2, radius * math.cos(a), radius * math.sin(a)]
    for k in range(n):
        a, b = k, (k + 1) % n
        idx += [a, b, n + b, a, n + b, n + a]
    return p, idx


def multi_dae(objects, material="test_mat"):
    """A .dae with several objects: [(name, positions, indices)]."""
    parts = [daewrite.dae_text(n, material, p, i) for n, p, i in objects]
    # join the geometry and scene nodes of the single-object files into one file
    head, geos, nodes = None, [], []
    for t in parts:
        a = t.index("<library_geometries>")
        b = t.index("</library_geometries>")
        c = t.index('<visual_scene id="Scene" name="Scene">')
        d = t.index("</visual_scene>")
        if head is None:
            head = t[:a]
        geos.append(t[a + len("<library_geometries>"):b])
        nodes.append(t[c + len('<visual_scene id="Scene" name="Scene">'):d])
    return (head + "<library_geometries>" + "".join(geos) + "</library_geometries>\n"
            "  <library_visual_scenes>\n    <visual_scene id=\"Scene\" name=\"Scene\">" + "".join(nodes) +
            "</visual_scene>\n  </library_visual_scenes>\n  <scene><instance_visual_scene url=\"#Scene\"/></scene>\n"
            "</COLLADA>\n")


def tiny_png(w=4, h=3, rgb=(200, 80, 40)):
    raw = b"".join(b"\x00" + bytes(rgb) * w for _ in range(h))

    def chunk(t, d):
        return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xffffffff)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)) +
            chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


# ---------------------------------------------------------------------------
# the test car
# ---------------------------------------------------------------------------

# frame rows (y) from front to back; bottom nodes at z=0.35, top rail at z=0.9
ROWS = [-2.0, -1.3, -0.5, 0.5, 1.3, 2.0]

MAIN_TEXT = r'''{
"testcar": {
    "information":{
        "authors":"tests",
        "name":"Test Car Frame",
        "value":10000,
    },
    "slotType" : "main",
    "slots": [
        ["type", "default", "description"],
        ["testcar_body","testcar_body", "Body", {"coreSlot":true}],
        ["testcar_suspension_F","testcar_suspension_F", "Front Suspension"],
        ["testcar_suspension_R","testcar_suspension_R", "Rear Suspension"],
        ["testcar_engine","testcar_engine_i4", "Engine"],
        ["testcar_fueltank","testcar_fueltank", "Fuel Tank"],
    ],
    "controller": [
        ["fileName"],
        ["vehicleController", {}],
    ],
    "variables": [
        ["name", "type", "unit", "category", "default", "min", "max", "title", "description"],
        ["$brakestrength", "range", "", "Brakes", 1, 0.6, 1.0, "Brake Force", "Scales the brake torque", {"minDis":60, "maxDis":100}],
    ],
    "refNodes":[
        ["ref:", "back:", "left:", "up:", "leftCorner:", "rightCorner:"],
        ["f3", "f4", "f3l", "t3", "f1l", "f1r"],
    ],
    "cameraExternal":{"distance":5.0, "distanceMin":2, "offset":{"x":0, "y":0, "z":0.6}, "fov":65},
    "flexbodies":[
        ["mesh", "[group]:", "nonFlexMaterials"],
        ["testcar_frame", ["testcar_frame"]],
    ],
    "nodes":[
        ["id", "posX", "posY", "posZ"],
        {"selfCollision":true},
        {"collision":true},
        {"nodeMaterial":"|NM_METAL"},
        {"frictionCoef":0.6},
        {"group":"testcar_frame"},
        {"nodeWeight":8},
%(nodes)s
        {"nodeWeight":3},
        //up reference above the ref node
        ["t3", 0.0, -0.5, 0.9],
        {"group":""},
    ],
    "beams":[
        ["id1:", "id2:"],
        {"deformLimitExpansion":1.2},
        {"beamPrecompression":1, "beamType":"|NORMAL"},
        {"beamSpring":3001000,"beamDamp":150},
        {"beamDeform":60000,"beamStrength":"FLT_MAX"},
%(beams)s
    ],
    "triangles": [
        ["id1:","id2:","id3:"],
        {"groundModel":"metal"},
%(tris)s
    ],
},
}
'''


def _frame():
    nodes, beams, tris = [], [], []
    for i, y in enumerate(ROWS, 1):
        for side, x in (("l", 0.6), ("", 0.0), ("r", -0.6)):
            nodes.append('        ["f%d%s", %s, %s, 0.35],' % (i, side, x, y))
        for side, x in (("l", 0.6), ("r", -0.6)):
            nodes.append('        ["t%d%s", %s, %s, 0.9],' % (i, side, x, y))
    pairs = []
    for i in range(1, len(ROWS) + 1):
        pairs += [("f%dl" % i, "f%d" % i), ("f%d" % i, "f%dr" % i), ("t%dl" % i, "t%dr" % i),
                  ("f%dl" % i, "t%dl" % i), ("f%dr" % i, "t%dr" % i), ("f%dl" % i, "t%dr" % i),
                  ("f%dr" % i, "t%dl" % i), ("f%d" % i, "t%dl" % i), ("f%d" % i, "t%dr" % i)]
        if i < len(ROWS):
            j = i + 1
            for a in ("f%dl", "f%d", "f%dr", "t%dl", "t%dr"):
                pairs.append((a % i, a % j))
            pairs += [("f%dl" % i, "f%d" % j), ("f%d" % i, "f%dl" % j), ("f%dr" % i, "f%d" % j), ("f%d" % i, "f%dr" % j),
                      ("t%dl" % i, "f%dl" % j), ("f%dl" % i, "t%dl" % j), ("t%dr" % i, "f%dr" % j), ("f%dr" % i, "t%dr" % j),
                      ("t%dl" % i, "t%dr" % j), ("t%dr" % i, "t%dl" % j)]
            # floor triangles, facing down (outward)
            tris += [("f%dl" % i, "f%d" % j, "f%d" % i), ("f%dl" % i, "f%dl" % j, "f%d" % j),
                     ("f%d" % i, "f%d" % j, "f%dr" % i), ("f%dr" % i, "f%d" % j, "f%dr" % j)]
    # ref node "t3" up brace
    pairs += [("t3", "f3"), ("t3", "t3l"), ("t3", "t3r"), ("t3", "f3l"), ("t3", "f3r"), ("t3", "t2l"), ("t3", "t4r")]
    beams = ['        ["%s","%s"],' % p for p in pairs]
    # one row without a comma before it, which the game accepts
    beams[3] = beams[3].rstrip(",")
    tris = ['        ["%s","%s","%s"],' % t for t in tris]
    return "\n".join(nodes), "\n".join(beams), "\n".join(tris)


def suspension(side):
    """Front ('F') or rear ('R') double wishbone with a common rim slot."""
    f = side == "F"
    p = "f" if f else "r"            # node prefix: fh1r / rh1r
    y = -1.3 if f else 1.3
    row_in, row_out = (2, 1) if f else (5, 6)
    name = "testcar_suspension_" + side
    hub = []
    for s, sx in (("r", -1), ("l", 1)):
        hub += [["%sh1%s" % (p, s), sx * 0.62, y, 0.2], ["%sh2%s" % (p, s), sx * 0.58, y, 0.42],
                ["%sh3%s" % (p, s), sx * 0.6, y + (0.12 if f else -0.12), 0.3],
                ["%sh5%s" % (p, s), sx * 0.64, y - 0.12 if f else y + 0.12, 0.31],
                ["%sx1%s" % (p, s), sx * 0.3, y - 0.12, 0.22], ["%sx2%s" % (p, s), sx * 0.3, y + 0.12, 0.22],
                ["%sx3%s" % (p, s), sx * 0.32, y, 0.45], ["%ss1%s" % (p, s), sx * 0.45, y, 0.75]]
    nodes = [["id", "posX", "posY", "posZ"], {"collision": True}, {"selfCollision": False},
             {"nodeMaterial": "|NM_METAL"}, {"frictionCoef": 0.5}, {"nodeWeight": 4},
             {"group": ["hub_" + side]}] + hub + [{"group": ""}]
    beams = [["id1:", "id2:"], {"beamPrecompression": 1, "beamType": "|NORMAL", "beamLongBound": 1, "beamShortBound": 1},
             {"beamSpring": 6001000, "beamDamp": 150}, {"beamDeform": 60000, "beamStrength": "FLT_MAX"}]
    for s in "rl":
        h1, h2, h3, h5 = ("%sh%d%s" % (p, k, s) for k in (1, 2, 3, 5))
        x1, x2, x3, s1 = "%sx1%s" % (p, s), "%sx2%s" % (p, s), "%sx3%s" % (p, s), "%ss1%s" % (p, s)
        beams += [[h1, h2], [h1, h3], [h2, h3], [h1, h5], [h2, h5], [h3, h5],
                  [x1, h1], [x2, h1], [x3, h2], [x1, x2], [x1, x3], [x2, x3], [s1, x3], [s1, x1], [s1, x2]]
        # mounts to the frame (drop brackets when lifted)
        fi, fo = "f%d%s" % (row_in, s), "f%d%s" % (row_out, s)
        beams += [[x1, fi], [x1, fo], [x2, fi], [x2, fo], [x3, fi], [x3, "t%d%s" % (row_in, s)],
                  [s1, "t%d%s" % (row_in, s)], [s1, "t%d%s" % (row_out, s)], [s1, fi]]
    beams += [{"optional": True}, {"breakGroup": "wheel_%sR" % side}]
    for s in "rl":
        w1, w2 = "%sw1%s" % (p, s), "%sw1%s%s" % (p, s, s)
        if s == "l":
            beams.append({"breakGroup": "wheel_%sL" % side})
        for h in (1, 2, 3, 5):
            beams += [["%sh%d%s" % (p, h, s), w1], ["%sh%d%s" % (p, h, s), w2]]
    beams += [{"breakGroup": ""}, {"optional": False}]
    slots2 = [["name", "allowTypes", "denyTypes", "default", "description"],
              ["wheel_%s_4" % side, ["wheel_%s_4" % side], [], "steelwheel_test_14x6_" + side, "%s Wheels" % ("Front" if f else "Rear"),
               {"nodeOffset": {"x": "$=$trackoffset_%s+0.25" % side, "y": y, "z": 0.3}}],
              ["testcar_wheeldata_" + side, ["testcar_wheeldata_" + side], [], "testcar_wheeldata_" + side, "Spindles",
               {"coreSlot": True}],
              ["testcar_coilover_" + side, ["testcar_coilover_" + side], [], "testcar_coilover_%s_stock" % side, "Springs"]]
    part = {
        "information": {"authors": "tests", "name": ("Front" if f else "Rear") + " Double Wishbone", "value": 900},
        "slotType": name,
        "slots2": slots2,
        "variables": [["name", "type", "unit", "category", "default", "min", "max", "title", "description"],
                      ["$trackoffset_" + side, "range", "+m", "Wheels", 0, -0.02, 0.05, "Track Offset", "Spacing of the wheel",
                       {"stepDis": 0.001}]],
        "flexbodies": [["mesh", "[group]:", "nonFlexMaterials"], ["testcar_arms_" + side, ["hub_" + side]]],
        "nodes": nodes,
        "beams": beams,
    }
    if f:
        part["hydros"] = [["id1:", "id2:"], {"beamSpring": 3001000, "beamDamp": 100, "inputSource": "steering_input",
                                             "inputFactor": 1, "inRate": 2, "outRate": 2},
                          ["fh3r", "fx3l", {"factor": -0.08}], ["fh3l", "fx3r", {"factor": 0.08}],
                          {"inputSource": "", "inputFactor": ""}]
    wheeldata = {
        "information": {"authors": "tests", "name": "Spindles", "value": 100},
        "slotType": "testcar_wheeldata_" + side,
        "pressureWheels": [
            ["name", "hubGroup", "group", "node1:", "node2:", "nodeS", "nodeArm:", "wheelDir"],
            {"selfCollision": False}, {"collision": True},
            {"axleBeams": ["axle_%sR" % side]},
            [side + "R", "wheel_%sR" % side, "tire_%sR" % side, "%sw1rr" % p, "%sw1r" % p, 9999, "%sh5r" % p, 1],
            {"axleBeams": ["axle_%sL" % side]},
            [side + "L", "wheel_%sL" % side, "tire_%sL" % side, "%sw1ll" % p, "%sw1l" % p, 9999, "%sh5l" % p, -1],
            {"axleBeams": []}, {"selfCollision": True},
        ],
    }
    if not f:
        wheeldata["powertrain"] = [["type", "name", "inputName", "inputIndex"],
                                   ["shaft", "wheelaxleRL", "differential_R", 1, {"connectedWheel": "RL"}],
                                   ["shaft", "wheelaxleRR", "differential_R", 2, {"connectedWheel": "RR"}]]
    coil = {
        "information": {"authors": "tests", "name": "Stock Coilovers", "value": 300},
        "slotType": "testcar_coilover_" + side,
        "variables": [["name", "type", "unit", "category", "default", "min", "max", "title", "description"],
                      ["$spring_" + side, "range", "N/m", "Suspension", 40000, 10000, 100000, "Spring Rate", "Stiffness",
                       {"stepDis": 500}],
                      ["$springheight_" + side, "range", "+m", "Suspension", 0, -0.05, 0.05, "Spring Height", "Ride height",
                       {"stepDis": 0.005}]],
        "flexbodies": [["mesh", "[group]:", "nonFlexMaterials"], ["testcar_coilover", ["hub_" + side]]],
        "beams": [["id1:", "id2:"], {"beamType": "|NORMAL"}, {"beamSpring": "$spring_" + side, "beamDamp": 0},
                  ["%sh1r" % p, "%ss1r" % p, {"precompressionRange": "$=$springheight_%s + 0.02" % side}],
                  ["%sh1l" % p, "%ss1l" % p, {"precompressionRange": "$=$springheight_%s + 0.02" % side}],
                  {"beamPrecompression": 1, "beamType": "|BOUNDED", "beamLongBound": 1, "beamShortBound": 1},
                  {"beamSpring": 0, "beamDamp": 3000},
                  ["%sh1r" % p, "%ss1r" % p, {"beamDampRebound": 6000}],
                  ["%sh1l" % p, "%ss1l" % p, {"beamDampRebound": 6000}],
                  {"beamPrecompression": 1, "beamType": "|NORMAL", "beamLongBound": 1, "beamShortBound": 1}],
    }
    return {name: part, "testcar_wheeldata_" + side: wheeldata, "testcar_coilover_%s_stock" % side: coil}


def drivetrain():
    eng = {
        "information": {"authors": "tests", "name": "1.8L I4", "value": 3000},
        "slotType": "testcar_engine",
        "slots": [["type", "default", "description"], ["testcar_transmission", "testcar_transmission_5M", "Transmission"]],
        "powertrain": [["type", "name", "inputName", "inputIndex"], ["combustionEngine", "mainEngine", "dummy", 0]],
        "mainEngine": {
            "torque": [["rpm", "torque"], [0, 0], [1000, 120], [3000, 165], [5000, 160], [6500, 130], [7000, 100]],
            "idleRPM": 800, "maxRPM": 6800, "inertia": 0.1, "friction": 12, "dynamicFriction": 0.025,
            "engineBrakeTorque": 30, "energyStorage": "mainTank", "requiredEnergyType": "gasoline",
            "torqueReactionNodes:": ["e1l", "e2l", "e4r"],
            "soundConfig": "soundConfig",
        },
        "soundConfig": {"sampleName": "I4_2_engine", "mainGain": -6},
        "flexbodies": [["mesh", "[group]:", "nonFlexMaterials"], ["testcar_engine", ["testcar_engine"]]],
        "nodes": [["id", "posX", "posY", "posZ"], {"group": "testcar_engine"}, {"nodeWeight": 25},
                  ["e1r", -0.25, -1.6, 0.45], ["e1l", 0.25, -1.6, 0.45], ["e2r", -0.25, -1.0, 0.45],
                  ["e2l", 0.25, -1.0, 0.45], ["e3r", -0.25, -1.6, 0.85], ["e3l", 0.25, -1.6, 0.85],
                  ["e4r", -0.25, -1.0, 0.85], ["e4l", 0.25, -1.0, 0.85], {"group": ""}],
        "beams": [["id1:", "id2:"], {"beamSpring": 8001000, "beamDamp": 200},
                  ["e1r", "e1l"], ["e2r", "e2l"], ["e3r", "e3l"], ["e4r", "e4l"], ["e1r", "e2r"], ["e1l", "e2l"],
                  ["e3r", "e4r"], ["e3l", "e4l"], ["e1r", "e3r"], ["e1l", "e3l"], ["e2r", "e4r"], ["e2l", "e4l"],
                  ["e1r", "e4l"], ["e1l", "e4r"], ["e2r", "e3l"], ["e2l", "e3r"],
                  {"beamSpring": 1501000, "beamDamp": 300},
                  ["e1r", "f1r"], ["e1l", "f1l"], ["e2r", "f2r"], ["e2l", "f2l"], ["e3r", "t1r"], ["e3l", "t1l"],
                  ["e4r", "t2r"], ["e4l", "t2l"]],
    }
    trans = {
        "information": {"authors": "tests", "name": "5-Speed Manual", "value": 1500},
        "slotType": "testcar_transmission",
        "slots": [["type", "default", "description"], ["testcar_differential_R", "testcar_differential_R", "Rear Differential"]],
        "powertrain": [["type", "name", "inputName", "inputIndex"], ["frictionClutch", "clutch", "mainEngine", 1],
                       ["manualGearbox", "gearbox", "clutch", 1], ["shaft", "driveshaft", "gearbox", 1]],
        "gearbox": {"uiName": "Gearbox", "gearRatios": [-3.5, 0, 3.6, 2.1, 1.4, 1.0, 0.8], "friction": 3},
    }
    diff = {
        "information": {"authors": "tests", "name": "Open Differential", "value": 300},
        "slotType": "testcar_differential_R",
        "variables": [["name", "type", "unit", "category", "default", "min", "max", "title", "description"],
                      ["$finaldrive_R", "range", ":1", "Differentials", 3.9, 2.5, 6, "Final Drive", "Ratio"]],
        "powertrain": [["type", "name", "inputName", "inputIndex"],
                       ["differential", "differential_R", "driveshaft", 1, {"diffType": "open", "gearRatio": "$finaldrive_R"}]],
    }
    tank = {
        "information": {"authors": "tests", "name": "Fuel Tank", "value": 150},
        "slotType": "testcar_fueltank",
        "energyStorage": [["type", "name"], ["fuelTank", "mainTank"]],
        "mainTank": {"energyType": "gasoline", "fuelCapacity": 45, "startingFuelCapacity": 30},
    }
    return {"testcar_engine_i4": eng, "testcar_transmission_5M": trans, "testcar_differential_R": diff,
            "testcar_fueltank": tank}


def body():
    nodes = [["id", "posX", "posY", "posZ"], {"group": "testcar_body"}, {"nodeWeight": 3},
             {"selfCollision": True}, {"collision": True}]
    for i, y in enumerate((-1.8, -0.6, 0.6, 1.8), 1):
        for s, x in (("l", 0.7), ("r", -0.7)):
            nodes.append(["b%d%s" % (i, s), x, y, 1.15])
    nodes.append({"group": ""})
    beams = [["id1:", "id2:"], {"beamSpring": 1001000, "beamDamp": 80}, {"beamDeform": 20000, "beamStrength": 60000}]
    for i in range(1, 5):
        beams += [["b%dl" % i, "b%dr" % i]]
        if i < 4:
            beams += [["b%dl" % i, "b%dl" % (i + 1)], ["b%dr" % i, "b%dr" % (i + 1)], ["b%dl" % i, "b%dr" % (i + 1)]]
    row = {1: 1, 2: 3, 3: 4, 4: 6}
    for i, fr in row.items():
        for s in "lr":
            beams += [["b%d%s" % (i, s), "t%d%s" % (fr, s)], ["b%d%s" % (i, s), "f%d%s" % (fr, s)],
                      ["b%d%s" % (i, s), "t%d%s" % (fr, "r" if s == "l" else "l")]]
    return {"testcar_body": {
        "information": {"authors": "tests", "name": "Sedan Body", "value": 5000},
        "slotType": "testcar_body",
        "flexbodies": [["mesh", "[group]:", "nonFlexMaterials"], ["testcar_body", ["testcar_body", "testcar_frame"]]],
        "nodes": nodes,
        "beams": beams,
        "triangles": [["id1:", "id2:", "id3:"], ["b1l", "b2l", "b2r"], ["b1l", "b2r", "b1r"]],
    }}


def common_wheels():
    """Common rims (with their axle nodes at the origin) and tires, like vehicles/common."""
    parts = {}
    for side in ("F", "R"):
        p = "f" if side == "F" else "r"
        rim = {
            "information": {"authors": "tests", "name": "14x6 Steel Wheels", "value": 120},
            "slotType": "wheel_%s_4" % side,
            "slots": [["type", "default", "description"],
                      ["tire_%s_14x6" % side, "tire_%s_185_60_14_test" % side, "%s Tires" % ("Front" if side == "F" else "Rear")]],
            "nodes": [["id", "posX", "posY", "posZ"], {"nodeWeight": 3}, {"collision": False}, {"selfCollision": False},
                      {"group": ""},
                      ["%sw1r" % p, -0.55, 0.0, 0.0], ["%sw1rr" % p, -0.4, 0.0, 0.0],
                      ["%sw1l" % p, 0.55, 0.0, 0.0], ["%sw1ll" % p, 0.4, 0.0, 0.0]],
            "pressureWheels": [["name", "hubGroup", "group", "node1:", "node2:", "nodeS", "nodeArm:", "wheelDir"],
                               {"hubRadius": 0.178}, {"hubWidth": 0.152}, {"numRays": 16},
                               {"hubNodeWeight": 0.4}, {"hasTire": False}],
            "flexbodies": [["mesh", "[group]:", "nonFlexMaterials"],
                           ["steelwheel_test_14x6", ["wheel_%sR" % side, "wheelhub_%sR" % side], [],
                            {"pos": {"x": -0.475, "y": 0, "z": 0}, "rot": {"x": 0, "y": 0, "z": 0}}],
                           ["steelwheel_test_14x6", ["wheel_%sL" % side, "wheelhub_%sL" % side], [],
                            {"pos": {"x": 0.475, "y": 0, "z": 0}, "rot": {"x": 0, "y": 0, "z": 180}}]],
        }
        parts["steelwheel_test_14x6_" + side] = rim
        for name, title, radius, width, psi in (("185_60_14_test", "185/60R14 Street", 0.29, 0.185, 30),
                                                ("205_75_14_test", "205/75R14 All-Terrain", 0.33, 0.205, 32)):
            parts["tire_%s_%s" % (side, name)] = {
                "information": {"authors": "tests", "name": title, "value": 200},
                "slotType": "tire_%s_14x6" % side,
                "pressureWheels": [["name", "hubGroup", "group", "node1:", "node2:", "nodeS", "nodeArm:", "wheelDir"],
                                   {"hasTire": True}, {"radius": radius}, {"tireWidth": width},
                                   {"pressurePSI": psi}, {"nodeWeight": 0.12}, {"frictionCoef": 1.0}],
                "flexbodies": [["mesh", "[group]:", "nonFlexMaterials"],
                               ["tire_test_" + name, ["wheel_%sR" % side, "tire_%sR" % side], [],
                                {"pos": {"x": -0.475, "y": 0, "z": 0}}],
                               ["tire_test_" + name, ["wheel_%sL" % side, "tire_%sL" % side], [],
                                {"pos": {"x": 0.475, "y": 0, "z": 0}, "rot": {"x": 0, "y": 0, "z": 180}}]],
            }
    return parts


def make_install(root):
    """Write the fake install under root; returns {"game": ..., "user": ...}."""
    game = os.path.join(root, "game")
    user = os.path.join(root, "user")
    vdir = os.path.join(game, "content", "vehicles")
    os.makedirs(vdir, exist_ok=True)
    os.makedirs(os.path.join(user, "mods", "unpacked"), exist_ok=True)
    with open(os.path.join(user, "mods", "db.json"), "w") as f:
        json.dump({"mods": {}}, f)

    nodes, beams, tris = _frame()
    main_text = MAIN_TEXT % {"nodes": nodes, "beams": beams, "tris": tris}
    jbeamfile.loads(main_text)   # must parse
    files = {
        "vehicles/testcar/testcar.jbeam": main_text,
        "vehicles/testcar/testcar_suspension_F.jbeam": jbeamfile.dumps_part_file(suspension("F"), comment="front suspension"),
        "vehicles/testcar/testcar_suspension_R.jbeam": jbeamfile.dumps_part_file(suspension("R"), comment="rear suspension"),
        "vehicles/testcar/testcar_engine.jbeam": jbeamfile.dumps_part_file(drivetrain()),
        "vehicles/testcar/testcar_body.jbeam": jbeamfile.dumps_part_file(body()),
        "vehicles/testcar/info.json": json.dumps({"Name": "Test Car", "Brand": "Testco", "Type": "Car",
                                                 "default_pc": "base", "Body Style": "Sedan"}, indent=2),
        "vehicles/testcar/base.pc": json.dumps({"format": 2, "model": "testcar", "mainPartName": "testcar",
                                               "parts": {"testcar_engine": "testcar_engine_i4"}, "vars": {}}, indent=2),
        "vehicles/testcar/info_base.json": json.dumps({"Configuration": "Base", "Value": 12000}),
    }
    meshes = [("testcar_frame",) + tuple(box_mesh(0, 0, 0.6, 1.2, 4.0, 0.5)),
              ("testcar_body",) + tuple(box_mesh(0, 0, 1.1, 1.5, 4.2, 0.6)),
              ("testcar_arms_F",) + tuple(box_mesh(0, -1.3, 0.3, 1.3, 0.3, 0.2)),
              ("testcar_arms_R",) + tuple(box_mesh(0, 1.3, 0.3, 1.3, 0.3, 0.2)),
              ("testcar_coilover",) + tuple(box_mesh(0.5, -1.3, 0.5, 0.1, 0.1, 0.5)),
              ("testcar_engine",) + tuple(box_mesh(0, -1.3, 0.65, 0.5, 0.6, 0.4))]
    with zipfile.ZipFile(os.path.join(vdir, "testcar.zip"), "w") as z:
        for rel, text in files.items():
            z.writestr(rel, text)
        z.writestr("vehicles/testcar/testcar.dae", multi_dae(meshes))
        z.writestr("vehicles/testcar/default.png", tiny_png())
        z.writestr("vehicles/testcar/testcar.materials.json", json.dumps({"test_mat": {"name": "test_mat", "class": "Material"}}))

    cw = common_wheels()
    wheel_meshes = [("steelwheel_test_14x6",) + tuple(cylinder_mesh(0.178, 0.152)),
                    ("tire_test_185_60_14_test",) + tuple(cylinder_mesh(0.29, 0.185)),
                    ("tire_test_205_75_14_test",) + tuple(cylinder_mesh(0.33, 0.205))]
    with zipfile.ZipFile(os.path.join(vdir, "common.zip"), "w") as z:
        z.writestr("vehicles/common/wheels/test_wheels.jbeam", jbeamfile.dumps_part_file(cw))
        z.writestr("vehicles/common/wheels/test_wheels.dae", multi_dae(wheel_meshes))
    out = {"game": game, "user": user}
    out.update(make_scrap(root))
    return out


def make_scrap(root):
    """A stand-in Scrap Mechanic install and two saved creations (see smfixture.py)."""
    import smfixture
    sm = smfixture.make_game(os.path.join(root, "smroot"))
    users = os.path.join(root, "smuser")
    bps = os.path.join(users, "User_76561198000000000", "Blueprints")
    for i, (name, kw, rgb) in enumerate((("Rusty Hatch", {}, (190, 70, 40)),
                                          ("Mud Truck", {"suspension": "offroad", "wheel": smfixture.BIG_WHEEL},
                                           (60, 120, 60)))):
        folder = os.path.join(bps, "0000000%d-1111-4222-8333-444444444444" % (i + 1))
        data, _ = smfixture.car(**kw)
        smfixture.save_creation(folder, data, name)
        with open(os.path.join(folder, "icon.png"), "wb") as f:
            f.write(tiny_png(8, 8, rgb))
    return {"sm": sm, "smuser": users}


if __name__ == "__main__":
    out = make_install(sys.argv[1] if len(sys.argv) > 1 else os.path.join(HERE, "_install"))
    print(json.dumps(out, indent=2))
