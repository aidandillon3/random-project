"""A small stand-in Scrap Mechanic install and blueprints for the importer tests.

The shapesets look like the game's (blockList / partList / jointList with
collision boxes and cylinders), with titles in a language file. Wheels and
the bearing use the game's own uuids; the rest are made up.
"""
import json
import os

WOOD = "8aedf6c2-94e1-4506-89d4-a0227c552f1e"
METAL = "1897ee42-0291-43e4-9645-8c5a5d310398"
GLASS = "5f41af56-df4c-4837-9b3c-10781335757f"
SMALL_WHEEL = "69e362c3-32aa-4cd1-adc0-dcfc47b92c0d"
BIG_WHEEL = "db66f0b1-0c50-4b74-bdc7-771374204b1f"
BEARING = "4a1b886b-913e-4aad-b5b6-6e41b0db23a6"
SUSP_OFFROAD = "73f838db-783e-4a41-bc0f-9008967780f3"
SUSP_SPORT = "52855106-a301-4ec1-8b97-5fb9d1a85b6e"
PISTON = "2f004fdf-bfb0-46f3-a7ac-7711100bdbdb"
ENGINE = "d4d1a6bd-1d1d-4d55-9df2-c6a0b4ad6dc8"
SEAT = "ffa3a47e-fc0d-4977-802f-bd09e7ec7cb7"
MOD_WHEEL = "aa11bb22-cc33-4d44-8e55-ff6677889900"
MOD_ID = "424242"


def _write(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        if isinstance(data, str):
            f.write(data)
        else:
            json.dump(data, f, indent=1)


def make_game(root):
    """steamapps/common/Scrap Mechanic with shapesets and titles, plus one workshop mod. Returns the game folder."""
    game = os.path.join(root, "steamapps", "common", "Scrap Mechanic")
    sets = os.path.join(game, "Data", "Objects", "Database", "ShapeSets")
    _write(os.path.join(sets, "blocks.json"), {"blockList": [
        {"uuid": WOOD, "name": "blk_wood1", "color": "9c7a54", "physicsMaterial": "Wood", "density": 250.0},
        {"uuid": METAL, "name": "blk_metal1", "color": "6e6e6e", "physicsMaterial": "Metal", "density": 500.0},
        {"uuid": GLASS, "name": "blk_glass", "color": "c8e6ff", "physicsMaterial": "Glass", "density": 400.0},
    ]})
    # comments and a trailing comma, as hand-edited game files have
    _write(os.path.join(sets, "vehicle.json"), """{
  // wheels and seats
  "partList": [
    {"uuid": "%s", "renderable": "$GAME_DATA/Objects/Renderable/Vehicle/obj_vehicle_smallwheel.rend",
     "color": "222222", "cylinder": {"diameter": 3, "depth": 1, "axis": "Z"}, "physicsMaterial": "Rubber",
     "density": 250.0},
    {"uuid": "%s", "renderable": "$GAME_DATA/Objects/Renderable/Vehicle/obj_vehicle_bigwheel.rend",
     "color": "222222", "cylinder": {"diameter": 5, "depth": 2, "axis": "Z"}, "physicsMaterial": "Rubber",
     "density": 250.0},
    {"uuid": "%s", "renderable": "$GAME_DATA/Objects/Renderable/Interactive/obj_interactive_gasengine_03.rend",
     "box": {"x": 3, "y": 3, "z": 2}, "physicsMaterial": "Mechanical", "density": 400.0,
     "scripted": {"filename": "$GAME_DATA/Scripts/interactable/GasEngine.lua", "classname": "GasEngine"}},
    {"uuid": "%s", "renderable": "$GAME_DATA/Objects/Renderable/Interactive/obj_interactive_driverseat_01.rend",
     "hull": {"x": 2, "y": 2, "z": 2}, "physicsMaterial": "Mechanical", "density": 200.0,
     "scripted": {"filename": "$GAME_DATA/Scripts/interactable/DriverSeat.lua", "classname": "DriverSeat"}},
  ],
}""" % (SMALL_WHEEL, BIG_WHEEL, ENGINE, SEAT))
    _write(os.path.join(sets, "joints.json"), {"jointList": [
        {"uuid": BEARING, "renderable": "$GAME_DATA/Objects/Renderable/Joints/jnt_bearing.rend", "bearing": {},
         "physicsMaterial": "Mechanical", "density": 500.0},
        {"uuid": SUSP_OFFROAD, "renderable": "$GAME_DATA/Objects/Renderable/Joints/jnt_suspension_offroad_03.rend",
         "physicsMaterial": "Mechanical", "density": 500.0},
        {"uuid": SUSP_SPORT, "renderable": "$GAME_DATA/Objects/Renderable/Joints/jnt_suspension_sport_03.rend",
         "physicsMaterial": "Mechanical", "density": 500.0},
        {"uuid": PISTON, "renderable": "$GAME_DATA/Objects/Renderable/Joints/jnt_piston.rend", "piston": {},
         "physicsMaterial": "Mechanical", "density": 500.0},
    ]})
    _write(os.path.join(game, "Data", "Gui", "Language", "English", "InventoryItemDescriptions.json"), {
        SMALL_WHEEL: {"title": "Small Wheel"}, BIG_WHEEL: {"title": "Big Wheel"}, BEARING: {"title": "Bearing"},
        SUSP_OFFROAD: {"title": "Off-Road Suspension"}, SUSP_SPORT: {"title": "Sport Suspension"},
        PISTON: {"title": "Piston"}, ENGINE: {"title": "Gas Engine"}, SEAT: {"title": "Driver's Seat"},
        WOOD: {"title": "Wood Block"}, METAL: {"title": "Metal Block"}, GLASS: {"title": "Glass Block"},
    })
    mod = os.path.join(root, "steamapps", "workshop", "content", "387990", MOD_ID)
    _write(os.path.join(mod, "description.json"), {"name": "Monster wheels", "type": "Blocks and Parts"})
    _write(os.path.join(mod, "Objects", "Database", "ShapeSets", "monster.shapeset"), {"partList": [
        {"uuid": MOD_WHEEL, "renderable": "$CONTENT_DATA/Objects/Renderable/monster_tire.rend",
         "cylinder": {"diameter": 6, "depth": 2, "axis": "Z"}, "physicsMaterial": "Rubber", "density": 200.0}]})
    _write(os.path.join(mod, "Gui", "Language", "English", "inventoryDescriptions.json"),
           {MOD_WHEEL: {"title": "Monster Tyre"}})
    return game


class Blueprint:
    """Builds a blueprint.json: bodies of childs and the joints between them."""

    def __init__(self):
        self.bodies = []
        self.joints = []
        self.count = 0
        self.ctl = 100

    def body(self):
        self.bodies.append([])
        return len(self.bodies) - 1

    def _child(self, body, d):
        self.bodies[body].append(d)
        self.count += 1
        return self.count - 1

    def block(self, body, uuid, pos, bounds, color="7f7f7f"):
        return self._child(body, {"shapeId": uuid, "pos": dict(zip("xyz", pos)), "bounds": dict(zip("xyz", bounds)),
                                  "xaxis": 1, "zaxis": 3, "color": color})

    def part(self, body, uuid, pos, xaxis=1, zaxis=3, color="dddddd", controller=None):
        d = {"shapeId": uuid, "pos": dict(zip("xyz", pos)), "xaxis": xaxis, "zaxis": zaxis, "color": color}
        if controller is not None:
            d["controller"] = controller
        return self._child(body, d)

    def joint(self, uuid, a, b, pos, xaxis, zaxis, color="aaaaaa"):
        jid = len(self.joints)
        self.ctl += 1
        self.joints.append({"id": jid, "shapeId": uuid, "childA": a, "childB": b, "posA": dict(zip("xyz", pos)),
                            "xaxisA": xaxis, "zaxisA": zaxis, "posB": dict(zip("xyz", pos)), "xaxisB": xaxis,
                            "zaxisB": zaxis, "color": color, "controller": {"id": self.ctl}})
        return jid

    def data(self, dependencies=None):
        out = {"bodies": [{"childs": b} for b in self.bodies], "joints": self.joints, "version": 4}
        if dependencies:
            out["dependencies"] = dependencies
        return out


def car(suspension=None, steer=True, drive=True, wheel=SMALL_WHEEL, piston=False):
    """A four-wheeled car facing +y (Scrap Mechanic frame): a metal floor, a wooden cabin with a glass
    windshield, a driver's seat, a gas engine at the back and wheels on bearings, optionally on suspension.

    Returns (blueprint data, info) with the cells the wheels sit at."""
    bp = Blueprint()
    ch = bp.body()
    floor = bp.block(ch, METAL, (0, 0, 2), (8, 16, 1), "4a4a4a")
    bp.block(ch, WOOD, (0, 7, 3), (1, 5, 4), "c23b22")
    bp.block(ch, WOOD, (7, 7, 3), (1, 5, 4), "c23b22")
    bp.block(ch, WOOD, (0, 7, 7), (8, 5, 1), "c23b22")
    bp.block(ch, WOOD, (1, 7, 3), (6, 1, 4), "c23b22")
    bp.block(ch, GLASS, (1, 11, 3), (6, 1, 4), "c8e6ff")
    seat_ctl = {"id": 1, "joints": [], "controllers": [{"id": 2}]}
    seat = bp.part(ch, SEAT, (3, 8, 3), controller=seat_ctl)
    eng_ctl = {"id": 2, "joints": []}
    bp.part(ch, ENGINE, (2, 1, 3), controller=eng_ctl)
    big = wheel != SMALL_WHEEL
    dia, depth = (3, 1) if wheel == SMALL_WHEEL else (5, 2) if wheel == BIG_WHEEL else (6, 2)
    z_lo = 2 - (dia - 1) // 2                        # wheel centred on the bearing's row
    wheels = {}
    for name, y0 in (("F", 11), ("R", 1)):
        for side in ("L", "R"):
            out = -1 if side == "L" else 1           # Scrap Mechanic -x is the vehicle's left
            x_face = -1 if side == "L" else 8        # first cell outside the floor
            yb = y0 + (dia - 1) // 2
            holder, x_at = floor, x_face
            if suspension:
                susp = SUSP_OFFROAD if suspension == "offroad" else SUSP_SPORT
                carrier_body = bp.body()
                cx = x_face + out
                carrier = bp.block(carrier_body, METAL, (cx, yb, 2), (1, 1, 1), "333333")
                bp.joint(susp, floor, carrier, (x_face, yb, 2), 2, out)
                holder, x_at = carrier, cx + out
            wb = bp.body()
            # the wheel's own z axis (the cylinder's) points out to the side, its x axis forward
            if out < 0:
                w = bp.part(wb, wheel, (x_at, y0, z_lo + dia), xaxis=2, zaxis=-1, color="222222")
            else:
                w = bp.part(wb, wheel, (x_at + 1, y0, z_lo), xaxis=2, zaxis=1, color="222222")
            j = bp.joint(BEARING, holder, w, (x_at, yb, 2), 2, out)
            wheels[name + side] = {"joint": j, "child": w, "x": (x_at - depth / 2) if out < 0 else (x_at + 1 + depth / 2),
                                   "y": y0 + dia / 2, "z": z_lo + dia / 2}
            if name == "F" and steer:
                seat_ctl["joints"].append({"id": j, "leftAngleLimit": 28, "rightAngleLimit": 28, "unlocked": False})
            if name == "R" and drive:
                eng_ctl["joints"].append({"id": j, "reverse": side == "R"})
    if piston:
        pb = bp.body()
        box = bp.block(pb, WOOD, (3, 14, 4), (2, 2, 1), "ffcc00")
        bp.joint(PISTON, floor, box, (3, 14, 3), 1, 3)
    deps = None
    if wheel == MOD_WHEEL:
        deps = [{"contentId": "x", "name": "Monster wheels", "shapeIds": [MOD_WHEEL], "steamFileId": int(MOD_ID)}]
    return bp.data(deps), {"wheels": wheels, "seat": seat, "big": big, "dia": dia, "depth": depth}


def save_creation(folder, data, name):
    os.makedirs(folder, exist_ok=True)
    _write(os.path.join(folder, "blueprint.json"), data)
    _write(os.path.join(folder, "description.json"), {"name": name, "description": "A test creation",
                                                       "type": "Blueprint", "version": 0})
    return os.path.join(folder, "blueprint.json")


AXIS_CODE = {(1, 0, 0): 1, (-1, 0, 0): -1, (0, 1, 0): 2, (0, -1, 0): -2, (0, 0, 1): 3, (0, 0, -1): -3}
CODE_AXIS = {v: k for k, v in AXIS_CODE.items()}


def rotate(data, turns=1):
    """The same blueprint turned by quarter turns about the vertical (to the left)."""
    data = json.loads(json.dumps(data))

    def q(v):
        x, y, z = v
        for _ in range(turns % 4):
            x, y = -y, x
        return (x, y, z)

    def vec(d):
        return (d["x"], d["y"], d["z"])

    def put(d, v):
        d["x"], d["y"], d["z"] = v
    for body in data["bodies"]:
        for c in body["childs"]:
            put(c["pos"], q(vec(c["pos"])))
            c["xaxis"] = AXIS_CODE[q(CODE_AXIS[c["xaxis"]])]
            c["zaxis"] = AXIS_CODE[q(CODE_AXIS[c["zaxis"]])]
    shift = q((1, 1, 1))
    for j in data["joints"]:
        for p, xa, za in (("posA", "xaxisA", "zaxisA"), ("posB", "xaxisB", "zaxisB")):
            # a joint fills the cell at its pos: keep that cell's lowest corner as the pos
            v = q(vec(j[p]))
            put(j[p], tuple(v[k] - (1 if shift[k] < 0 else 0) for k in range(3)))
            j[xa] = AXIS_CODE[q(CODE_AXIS[j[xa]])]
            j[za] = AXIS_CODE[q(CODE_AXIS[j[za]])]
    return data


def _wheel(bp, holder, x_at, y0, out, steer=None, drive=None, wheel=SMALL_WHEEL):
    """A small wheel on a bearing on the side x_at of holder, facing out (-1 or 1)."""
    wb = bp.body()
    if out < 0:
        w = bp.part(wb, wheel, (x_at, y0, 4), xaxis=2, zaxis=-1, color="222222")
    else:
        w = bp.part(wb, wheel, (x_at + 1, y0, 1), xaxis=2, zaxis=1, color="222222")
    j = bp.joint(BEARING, holder, w, (x_at, y0 + 1, 2), 2, out)
    if steer is not None:
        steer["joints"].append({"id": j, "leftAngleLimit": 30, "rightAngleLimit": 30})
    if drive is not None:
        drive["joints"].append({"id": j, "reverse": out > 0})
    return j


def trike():
    """One steered wheel in the middle at the front, two driven ones at the back."""
    bp = Blueprint()
    ch = bp.body()
    floor = bp.block(ch, METAL, (0, 0, 2), (6, 14, 1), "2255aa")
    nose = bp.block(ch, METAL, (2, 14, 2), (2, 3, 1), "2255aa")
    seat_ctl = {"id": 1, "joints": []}
    bp.part(ch, SEAT, (2, 5, 3), controller=seat_ctl)
    eng_ctl = {"id": 2, "joints": []}
    bp.part(ch, ENGINE, (1, 0, 3), controller=eng_ctl)
    # the front wheel hangs under the nose on a bearing at its side
    _wheel(bp, nose, 4, 15, 1, steer=seat_ctl)
    _wheel(bp, floor, -1, 1, -1, drive=eng_ctl)
    _wheel(bp, floor, 6, 1, 1, drive=eng_ctl)
    return bp.data()


def six_wheeler():
    """Three axles: steering at the front, all six driven."""
    bp = Blueprint()
    ch = bp.body()
    floor = bp.block(ch, METAL, (0, 0, 2), (8, 26, 1), "777744")
    bp.block(ch, WOOD, (0, 18, 3), (8, 6, 5), "aa8844")
    seat_ctl = {"id": 1, "joints": []}
    bp.part(ch, SEAT, (3, 20, 8), controller=seat_ctl)
    eng_ctl = {"id": 2, "joints": []}
    bp.part(ch, ENGINE, (2, 10, 3), controller=eng_ctl)
    for y0, steer in ((21, seat_ctl), (5, None), (1, None)):
        _wheel(bp, floor, -1, y0, -1, steer=steer, drive=eng_ctl)
        _wheel(bp, floor, 8, y0, 1, steer=steer, drive=eng_ctl)
    return bp.data()
