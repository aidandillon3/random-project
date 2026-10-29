"""Build the Nurburgring Nordschleife for Scrap Mechanic: tiles plus a world that places them.

  python build.py --template "<Tiles>/<folder of the template tile>" --out out

The template is a Large tile saved by the Tile Editor with one "Blank Road (Matte)"
piece in it. Every road piece copies that asset (uuid, colour), turned and tilted to
follow the track and stretched to ROAD_WIDTH across. The ground follows real elevation
data (dem/), flattened under the road and blended back into the hills beside it.

Each tile is written like the editor does: .tile (what the game loads), .tile.export
(same bytes), .tileson (what the Tile Editor opens; road only, its ground stays flat),
description.json and a preview picture. The world covers the whole lap.
"""

import argparse
import copy
import json
import math
import os
import uuid

import numpy as np
from PIL import Image, ImageDraw
from scipy.spatial import cKDTree

import dem as demlib
import smtile
import track

ROAD_WIDTH = 20.0      # metres (Scrap Mechanic cars are ~3x real size)
PIECE_SIZE = 32.0      # Blank Road (Matte) mesh is 32 x 32 m at scale 1 (collision .obj)
PIECE_THICK = 0.5      # ... and 0.5 m thick
THICK_SCALE = 0.5      # mesh is 0.5 m thick at scale 1 (local z 0..0.5), so the slab is 0.25 m
ROAD_SINK = 0.15       # slab bottom sits this far below the road profile, top 0.10 above
TILE_STEP = 0.03       # metres each piece's front end sits above the next piece (roof tiles)
START_AHEAD = 10       # pieces between the spawn point (T13) and the start/finish line
STEP = 4.0             # metres of centre line per road piece (short pieces lean across each
                       # other less in bends on hills, so the steps stay small)
GROUND_UNDER = 0.06    # ground sits this far below the road profile (inside the slab)
CARVE = 0.12           # ... and at least this far below the top of every piece over it
FLAT_EDGE = ROAD_WIDTH / 2 + 2.0   # ground is flat at road height out to here ...
BLEND_EDGE = 45.0                  # ... and back to the real hillside by here
PROFILE_SMOOTH = 60.0  # metres either side used to smooth the road's height profile
TILE_CELLS = 4         # Large tile = 4 x 4 cells of 64 m
TILE_SIZE = TILE_CELLS * smtile.CELL_SIZE
VERTEX = 2.0           # ground vertex spacing (33 x 33 per cell)
# Decorations: the game's own forest assets (Data/Terrain/Database/AssetSets/forest.assetset),
# with their default colours. The Nordschleife runs through the Eifel forest, mostly spruce.
TREES = [  # (uuid, {colour name: RRGGBB}, how often)
    ("3958948a-9da3-4ef2-bbc6-f3f6c8efd69b", {"leaves": "5b7110", "trunk": "453724"}, 5),  # pine01
    ("89cf550e-96e0-4b8d-9fcf-c4cb701eb2ce", {"leaves": "4f622b", "trunk": "483430"}, 5),  # pine02
    ("ad7428d8-b1f9-497e-b20e-7b6c23303ba3", {"leaves": "405d2b", "trunk": "4f3734"}, 5),  # pine03
    ("ab2221f1-b86a-45aa-b28f-dac06d2c71d8", {"leaves": "6faf0a", "trunk": "804620"}, 2),  # oak01
    ("2160d36d-a3bb-409f-93c9-9ece9cd45310", {"leaves": "6faf0a", "trunk": "804620"}, 2),  # oak02
    ("6076c649-dae7-4fe5-8ca9-4dd98b35fd73", {"leaves": "409748", "trunk": "5d4a3c"}, 2),  # oak03
    ("80535813-ae09-427b-a914-e6c320df6005", {"leaves": "659526", "trunk": "4a4015"}, 2),  # oak05
    ("bc8b746d-4b85-46a9-924c-f1edb0eef5f6", {"leaves": "d89c1a", "trunk": "f5e9f5"}, 1),  # birch01
    ("81ca86c6-7c74-4a90-82ed-dc08734baa6d", {"leaves": "f1c52e", "trunk": "f5e9f5"}, 1),  # birch02
]
TREE_CLEAR = 32.0      # no trees closer than this to the centre line (22 m of grass beside the road)
TREE_BAND = 170.0      # ... or further than this (keeps the tree count, and load time, down)
TREE_SPACING = 13.0    # metres between trees in the forest (jittered grid)
FOREST_SHARE = 0.7     # roughly this much of the band is forest, the rest open meadow
KERB_RADIUS = 200.0    # red and white kerbs on both sides of bends tighter than this
KERB_WIDTH = 1.5
KERB_RED, KERB_WHITE = 0xFF2828D2, 0xFFF0F0F0  # 0xAABBGGRR
ASSET_LIST = 1         # the list the Tile Editor put the road piece in


def qmul(a, b):
    ax, ay, az, aw = a
    bx, by, bz, bw = b
    return (aw * bx + ax * bw + ay * bz - az * by,
            aw * by - ax * bz + ay * bw + az * bx,
            aw * bz + ax * by - ay * bx + az * bw,
            aw * bw - ax * bx - ay * by - az * bz)


def road_rotation(yaw, pitch):
    """Rz(yaw) * Ry(-pitch). The mesh lies flat as it is: local x along the road,
    y across, z up (0..0.5 m)."""
    qz = (0.0, 0.0, math.sin(yaw / 2), math.cos(yaw / 2))
    qy = (0.0, math.sin(-pitch / 2), 0.0, math.cos(-pitch / 2))
    return qmul(qz, qy)


def editor_angles(q):
    """The Tile Editor's (.tileson) rotation: intrinsic X-Y-Z Euler angles in degrees."""
    from scipy.spatial.transform import Rotation
    return [float(a) for a in Rotation.from_quat(q).as_euler("XYZ", degrees=True)]


def smooth_closed(values, half_window):
    n = len(values)
    k = np.ones(2 * half_window + 1) / (2 * half_window + 1)
    padded = np.concatenate([values[-half_window:], values, values[:half_window]])
    return np.convolve(padded, k, mode="valid")[:n]


def road_pieces(pts, zs):
    """(x, y, z, yaw, pitch, length) per segment of a closed, evenly resampled centre line,
    in driving direction.

    The pieces are laid like roof tiles: where two pieces overlap, the one you drive off
    sits on top of the one you drive onto, so a car only ever drops a few centimetres onto
    the next piece and never meets an edge. Each piece is tilted up slightly so its rear
    end is under the previous piece and its front end over the next one."""
    from scipy.spatial.transform import Rotation
    n = len(pts)
    seg = [math.dist(pts[i], pts[(i + 1) % n]) for i in range(n)]
    heading = [math.atan2(pts[(i + 1) % n][1] - pts[i][1], pts[(i + 1) % n][0] - pts[i][0]) for i in range(n)]
    grade = [(zs[(i + 1) % n] - zs[i]) / max(seg[i], 1e-3) for i in range(n)]
    # ext[i]: how far pieces i and i+1 reach past the point where they meet, so the outside
    # of a bend has no gap.
    ext = []
    for i in range(n):
        turn = (heading[(i + 1) % n] - heading[i] + math.pi) % (2 * math.pi) - math.pi
        ext.append(ROAD_WIDTH / 2 * math.tan(min(abs(turn), 1.2) / 2) + 0.5)
    top = PIECE_THICK * THICK_SCALE

    def lay(sep):
        out = []
        for i in range(n):
            a, b = pts[i], pts[(i + 1) % n]
            back, front = ext[i - 1], ext[i]
            length = seg[i] + back + front
            shift = (front - back) / 2  # centre moves forward by this much along the road
            c, s_ = math.cos(heading[i]), math.sin(heading[i])
            x = (a[0] + b[0]) / 2 + c * shift
            y = (a[1] + b[1]) / 2 + s_ * shift
            z = (zs[i] + zs[(i + 1) % n]) / 2 + grade[i] * shift
            rear_drop, front_lift = sep[i - 1] / 2, sep[i] / 2
            z += (front_lift - rear_drop) / 2
            pitch = math.atan(grade[i]) + math.atan((front_lift + rear_drop) / length)
            out.append((x, y, z, heading[i], pitch, length / math.cos(math.atan(grade[i]))))
        return out

    def rise(p, q):
        """How far the rear edge of piece q pokes above the top of piece p (worst of its
        two corners and middle). On a grade in a bend the two pieces lean across each other,
        so one corner of q comes up through p unless the step is big enough."""
        mp = Rotation.from_quat(road_rotation(p[3], p[4])).as_matrix()
        mq = Rotation.from_quat(road_rotation(q[3], q[4])).as_matrix()
        op, oq = np.array(p[:3]), np.array(q[:3])
        worst = -1e9
        for w in (-ROAD_WIDTH / 2, 0.0, ROAD_WIDTH / 2):
            e = oq + mq @ np.array([-q[5] / 2, w, top])
            worst = max(worst, (mp.T @ (e - op))[2] - top)
        return worst

    # Start with a small step everywhere, then raise it wherever a rear edge still shows.
    sep = [TILE_STEP] * n
    for _ in range(6):
        pieces = lay(sep)
        worst = 0.0
        for i in range(n):
            r = rise(pieces[i], pieces[(i + 1) % n])
            if r > -TILE_STEP / 2:
                sep[i] += r + TILE_STEP
                worst = max(worst, r)
        if worst == 0.0:
            break
    road_pieces.steps = sep
    return pieces


def main():
    ap = argparse.ArgumentParser()
    here = os.path.dirname(os.path.abspath(__file__))
    ap.add_argument("--template", required=True, help="Tile Editor folder of the template tile")
    ap.add_argument("--osm", default=os.path.join(here, "osm", "nordschleife.json"))
    ap.add_argument("--dem", default=os.path.join(here, "dem"))
    ap.add_argument("--flat", action="store_true", help="flat ground instead of real hills")
    ap.add_argument("--out", default="out")
    ap.add_argument("--world-cells", type=int, default=128, help="world size in 64 m cells")
    ap.add_argument("--name", default="Nordschleife")
    ap.add_argument("--mod", action="store_true",
                    help="write a Custom Game mod (Mods/Nordschleife) instead of World Builder tiles + world")
    args = ap.parse_args()

    tdir = args.template
    tname = next(f for f in os.listdir(tdir) if f.endswith(".tile"))[:-5]
    template = smtile.Tile.load(os.path.join(tdir, tname + ".tile"))
    with open(os.path.join(tdir, tname + ".tileson"), encoding="utf-8") as f:
        tileson_tpl = json.load(f)
    with open(os.path.join(tdir, "description.json"), encoding="utf-8") as f:
        desc_tpl = json.load(f)
    road = next(a for y in range(4) for x in range(4) for l in range(4) for a in template.assets(x, y, l))
    road_json = tileson_tpl["entities"]["assets"][0]
    template_mips = [template.mip_raw(0, 0, m) for m in range(6)]

    # Centre line in metres, x east / y north.
    latlon = track.stitch(track.load_ways(args.osm))
    origin = latlon[0]
    pts = track.project(latlon, origin)
    if math.dist(pts[0], pts[-1]) < 1:
        pts = pts[:-1]
    pts = track.resample(track.smooth(track.resample(pts + [pts[0]], 4.0), passes=3), STEP)
    if math.dist(pts[0], pts[-1]) < STEP / 2:
        pts = pts[:-1]
    # Race direction (clockwise: T13 -> Hatzenbach -> Flugplatz ...) and start the list at T13.
    t13 = track.project([track.start_of(args.osm, "T13")], origin)[0]
    hatz = track.project([track.start_of(args.osm, "Hatzenbach")], origin)[0]
    i13 = min(range(len(pts)), key=lambda i: math.dist(pts[i], t13))
    ihz = min(range(len(pts)), key=lambda i: math.dist(pts[i], hatz))
    if (ihz - i13) % len(pts) > len(pts) // 2:
        pts = pts[::-1]
        i13 = len(pts) - 1 - i13
    pts = pts[i13:] + pts[:i13]
    print(f"centre line {track.length(pts + [pts[0]]) / 1000:.2f} km, {len(pts)} road pieces")

    # Tile grid: the lap's bounding box plus a margin, centred in whole tiles.
    xs = [p[0] for p in pts]; ys = [p[1] for p in pts]
    margin = BLEND_EDGE + 20
    tw = math.ceil((max(xs) - min(xs) + 2 * margin) / TILE_SIZE)
    th = math.ceil((max(ys) - min(ys) + 2 * margin) / TILE_SIZE)
    gx = (min(xs) + max(xs)) / 2 - tw * TILE_SIZE / 2
    gy = (min(ys) + max(ys)) / 2 - th * TILE_SIZE / 2
    print(f"{tw} x {th} large tiles")

    # Ground height on a 2 m vertex grid over the whole area, and the road's height profile.
    nx, ny = tw * TILE_CELLS * 32 + 1, th * TILE_CELLS * 32 + 1
    vx = gx + np.arange(nx) * VERTEX
    vy = gy + np.arange(ny) * VERTEX
    if args.flat:
        ground = np.zeros((ny, nx))
        zs = np.zeros(len(pts))
    else:
        d = demlib.Dem(args.dem)
        ground = dem_grid(d, vx, vy, origin)
        raw = np.array([d.height(*track.unproject(x, y, origin)) for x, y in pts])
        zs = smooth_closed(smooth_closed(raw, int(PROFILE_SMOOTH / STEP)), int(PROFILE_SMOOTH / STEP))
        base = min(ground.min(), zs.min()) - 2.0
        ground -= base
        zs -= base
        print(f"road height {zs.min():.0f} to {zs.max():.0f} m, ground {ground.min():.0f} to {ground.max():.0f} m")

    # Road pieces into tiles/cells.
    tiles = {}  # (tx, ty) -> {(cx, cy): [(Asset, tileson dict)]}

    def place(x, y, z, rot, scale, colors=None, asset_uuid=None):
        lx, ly = x - gx, y - gy
        tx, ty = math.floor(lx / TILE_SIZE), math.floor(ly / TILE_SIZE)
        px, py = lx - tx * TILE_SIZE, ly - ty * TILE_SIZE
        cx, cy = int(px // smtile.CELL_SIZE), int(py // smtile.CELL_SIZE)
        a = copy.deepcopy(road)
        if asset_uuid:
            a.uuid_bytes = uuid.UUID(asset_uuid).bytes
        a.pos = (px - cx * smtile.CELL_SIZE, py - cy * smtile.CELL_SIZE, z)
        a.rot = rot
        a.scale = scale
        j = copy.deepcopy(road_json)
        if colors:
            a.colors = [(k, v) for k, v in colors.items()]
            j["colorMap"] = {k: "#%02X%02X%02XFF" % (v & 255, (v >> 8) & 255, (v >> 16) & 255)
                             for k, v in colors.items()}
        j["guid"] = str(uuid.uuid4())
        if asset_uuid:
            j["uuid"] = asset_uuid
        j["transform"] = {"position": [px, py, z], "rotation": editor_angles(rot), "scale": list(scale)}
        tiles.setdefault((tx, ty), {}).setdefault((cx, cy), []).append((a, j))

    pieces = list(road_pieces(pts, zs))
    if not args.flat:
        ground = carve_under(shape_ground(ground, vx, vy, pts, zs), vx, vy, pieces)
    steps = sorted(road_pieces.steps)
    print(f"steps between pieces: median {steps[len(steps) // 2] * 100:.0f} cm, "
          f"95% under {steps[int(len(steps) * .95)] * 100:.0f} cm, max {steps[-1] * 100:.0f} cm")
    for x, y, z, yaw, pitch, length in pieces:
        place(x, y, z - ROAD_SINK, road_rotation(yaw, pitch),
              (length / PIECE_SIZE, ROAD_WIDTH / PIECE_SIZE, THICK_SCALE))
    start_line(place, pieces[START_AHEAD])
    kerbs(place, pieces)
    if not args.flat:
        trees(place, pts, ground, vx, vy)
    spawn_yaw = pieces[0][3]

    if args.mod:
        mod_dir = os.path.join(args.out, "Mods", args.name)
        tiles_out = os.path.join(mod_dir, "Terrain", "Tiles")
    else:
        tiles_out = os.path.join(args.out, "Tiles")
    os.makedirs(tiles_out, exist_ok=True)
    made = {}
    for ty in range(th):
        for tx in range(tw):
            cells = tiles.get((tx, ty), {})
            name = f"{args.name} {tx:02d}-{ty:02d}"
            # Stable ids, so a rebuilt tile replaces the old one in existing saves.
            local_id = uuid.uuid5(uuid.NAMESPACE_URL, f"nordschleife/{name}/local")
            tile_uuid = uuid.uuid5(uuid.NAMESPACE_URL, f"nordschleife/{name}/tile")
            folder = tiles_out if args.mod else os.path.join(tiles_out, str(local_id))
            os.makedirs(folder, exist_ok=True)

            mips = {}
            for cy in range(TILE_CELLS):
                for cx in range(TILE_CELLS):
                    r0 = (ty * TILE_CELLS + cy) * 32
                    c0 = (tx * TILE_CELLS + cx) * 32
                    mips[(cx, cy)] = smtile.pack_mips(ground[r0:r0 + 33, c0:c0 + 33], template_mips)
            t = smtile.Tile(bytes(template.data))
            t.rebuild({k: [a for a, _ in v] for k, v in cells.items()}, ASSET_LIST, mips)
            t.set_uuid(tile_uuid)
            t.save(os.path.join(folder, name + ".tile"))
            if args.mod:
                made[(tx, ty)] = (None, name)
                continue
            t.save(os.path.join(folder, name + ".tile.export"))

            ts = copy.deepcopy(tileson_tpl)
            ts["entities"]["assets"] = [j for v in cells.values() for _, j in v]
            ts["info"]["uuid"] = str(tile_uuid)
            with open(os.path.join(folder, name + ".tileson"), "w", encoding="utf-8") as f:
                json.dump(ts, f, indent="\t")

            desc = copy.deepcopy(desc_tpl)
            desc.pop("fileId", None)
            desc["localId"], desc["name"] = str(local_id), name + ".tile"
            with open(os.path.join(folder, "description.json"), "w", encoding="utf-8") as f:
                json.dump(desc, f, indent="\t")

            r0, c0 = ty * TILE_CELLS * 32, tx * TILE_CELLS * 32
            preview(cells, ground[r0:r0 + 129, c0:c0 + 129], ground.min(), ground.max(),
                    os.path.join(folder, f"{tile_uuid}.png"))
            made[(tx, ty)] = (str(local_id), name)
    print(f"{len(made)} tiles, {sum(len(c) for t in tiles.values() for c in t.values())} road pieces")

    if args.mod:
        spawn = track.project([track.start_of(args.osm, "T13")], origin)[0]
        write_mod(args, mod_dir, made, tw, th, ((spawn[0] - gx), (spawn[1] - gy)), spawn_yaw)
    else:
        write_world(args, made, tw, th)
    overview(pts, gx, gy, tw, th, ground, os.path.join(args.out, "overview.png"))


def hex_colour(rgb):
    """"RRGGBB" -> the tile's 0xAABBGGRR."""
    r, g, b = int(rgb[0:2], 16), int(rgb[2:4], 16), int(rgb[4:6], 16)
    return 0xFF000000 | b << 16 | g << 8 | r


def kerbs(place, pieces):
    """Red and white kerb strips just outside both road edges in the bends, laid in the
    road piece's own plane and 2 cm lower, so a wheel coming off the road drops onto them."""
    from scipy.spatial.transform import Rotation
    n = len(pieces)
    top = PIECE_THICK * THICK_SCALE
    count = 0
    for i, (x, y, z, yaw, pitch, length) in enumerate(pieces):
        # Radius from the heading change over the pieces either side (about 16 m of road).
        turn = (pieces[(i + 2) % n][3] - pieces[i - 2][3] + math.pi) % (2 * math.pi) - math.pi
        if abs(turn) < 1e-6 or 4 * STEP / abs(turn) > KERB_RADIUS:
            continue
        rot = road_rotation(yaw, pitch)
        m = Rotation.from_quat(rot).as_matrix()
        # Long enough to close the gap on the outside of the bend.
        kl = STEP / math.cos(pitch) + 0.6 + (ROAD_WIDTH / 2 + KERB_WIDTH) * abs(turn) / 4
        for side in (-1, 1):
            off = side * (ROAD_WIDTH / 2 + KERB_WIDTH / 2)
            p = np.array([x, y, z - ROAD_SINK - 0.02]) + m @ np.array([0.0, off, 0.0])
            colour = KERB_RED if i % 2 else KERB_WHITE
            place(p[0], p[1], p[2], rot, (kl / PIECE_SIZE, KERB_WIDTH / PIECE_SIZE, THICK_SCALE),
                  {"Road2": colour})
            count += 1
    print(f"{count} kerb pieces")


def trees(place, pts, ground, vx, vy):
    """Forest beside the track: a jittered grid of the game's trees in a band beside the
    road, with open meadows where a smooth noise field is low. Same trees every build."""
    rng = np.random.default_rng(1927)  # the year the Nordschleife opened
    tree = cKDTree(np.array(pts))
    x0, y0, x1, y1 = vx[0], vy[0], vx[-1], vy[-1]
    gx, gy = np.meshgrid(np.arange(x0 + TREE_SPACING / 2, x1, TREE_SPACING),
                         np.arange(y0 + TREE_SPACING / 2, y1, TREE_SPACING))
    cand = np.column_stack([gx.ravel(), gy.ravel()])
    cand += rng.uniform(-0.4, 0.4, cand.shape) * TREE_SPACING
    dist, _ = tree.query(cand, distance_upper_bound=TREE_BAND)
    cand = cand[(dist > TREE_CLEAR) & np.isfinite(dist)]
    cand = cand[(cand[:, 0] > x0 + 2) & (cand[:, 0] < x1 - 2) & (cand[:, 1] > y0 + 2) & (cand[:, 1] < y1 - 2)]
    # Meadows: value noise on a 150 m grid, smoothly interpolated.
    cs = 150.0
    nx, ny = int((x1 - x0) / cs) + 3, int((y1 - y0) / cs) + 3
    lattice = rng.random((ny, nx))
    fx, fy = (cand[:, 0] - x0) / cs, (cand[:, 1] - y0) / cs
    ix, iy = fx.astype(int), fy.astype(int)
    tx, ty = fx - ix, fy - iy
    tx, ty = tx * tx * (3 - 2 * tx), ty * ty * (3 - 2 * ty)
    noise = ((lattice[iy, ix] * (1 - tx) + lattice[iy, ix + 1] * tx) * (1 - ty)
             + (lattice[iy + 1, ix] * (1 - tx) + lattice[iy + 1, ix + 1] * tx) * ty)
    cand = cand[noise > np.quantile(noise, 1 - FOREST_SHARE)]
    # Ground height under each tree (bilinear on the 2 m grid), sunk a little.
    px, py = (cand[:, 0] - x0) / VERTEX, (cand[:, 1] - y0) / VERTEX
    i, j = px.astype(int), py.astype(int)
    fx, fy = px - i, py - j
    h = ((ground[j, i] * (1 - fx) + ground[j, i + 1] * fx) * (1 - fy)
         + (ground[j + 1, i] * (1 - fx) + ground[j + 1, i + 1] * fx) * fy)
    weights = np.array([w for _, _, w in TREES], dtype=float)
    kinds = rng.choice(len(TREES), size=len(cand), p=weights / weights.sum())
    yaws = rng.uniform(0, 2 * math.pi, len(cand))
    sizes = rng.uniform(0.8, 1.25, len(cand))
    for (x, y), z, k, yaw, s in zip(cand, h, kinds, yaws, sizes):
        u, cols, _ = TREES[k]
        place(x, y, z - 0.3, (0.0, 0.0, math.sin(yaw / 2), math.cos(yaw / 2)), (s, s, s),
              {name: hex_colour(c) for name, c in cols.items()}, asset_uuid=u)
    print(f"{len(cand)} trees")


def start_line(place, piece):
    """Chequered start/finish line across the road: 2 rows of 2 m squares, lying on top of
    the road piece (5 mm proud of its surface)."""
    from scipy.spatial.transform import Rotation
    x, y, z, yaw, pitch, length = piece
    rot = road_rotation(yaw, pitch)
    m = Rotation.from_quat(rot).as_matrix()
    origin = np.array([x, y, z - ROAD_SINK])
    top = PIECE_THICK * THICK_SCALE
    sq, t = 2.0, 0.02
    for row in range(2):
        for col in range(int(ROAD_WIDTH / sq)):
            local = np.array([(row - 0.5) * sq, (col + 0.5) * sq - ROAD_WIDTH / 2, top + 0.005 - t])
            p = origin + m @ local
            white = (row + col) % 2 == 0
            place(p[0], p[1], p[2], rot, (sq / PIECE_SIZE, sq / PIECE_SIZE, t / PIECE_THICK),
                  {"Road2": 0xFFFFFFFF if white else 0xFF141414})


def dem_grid(d, vx, vy, origin):
    """DEM height at every vertex (bilinear), vectorised."""
    lat0, lon0 = origin
    k = math.cos(math.radians(lat0))
    lat = lat0 + np.degrees(vy / track.EARTH_R)
    lon = lon0 + np.degrees(vx / (track.EARTH_R * k))
    n = 2 ** d.z * 256
    px = (lon + 180) / 360 * n - d.x0 * 256 - 0.5
    py = (1 - np.arcsinh(np.tan(np.radians(lat))) / np.pi) / 2 * n - d.y0 * 256 - 0.5
    x0 = np.floor(px).astype(int); fx = px - x0
    y0 = np.floor(py).astype(int); fy = py - y0
    h = d.h
    top = h[np.ix_(y0, x0)] * (1 - fx) + h[np.ix_(y0, x0 + 1)] * fx
    bot = h[np.ix_(y0 + 1, x0)] * (1 - fx) + h[np.ix_(y0 + 1, x0 + 1)] * fx
    return top * (1 - fy)[:, None] + bot * fy[:, None]


def shape_ground(ground, vx, vy, pts, zs):
    """Flatten the ground under the road and blend it back to the hillside beside it."""
    dense, dz = [], []
    n = len(pts)
    for i in range(n):
        a, b = pts[i], pts[(i + 1) % n]
        for t in np.linspace(0, 1, 5, endpoint=False):
            dense.append((a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t))
            dz.append(zs[i] + (zs[(i + 1) % n] - zs[i]) * t)
    tree = cKDTree(np.array(dense))
    dz = np.array(dz)
    out = ground.copy()
    for r in range(0, len(vy), 256):
        gxx, gyy = np.meshgrid(vx, vy[r:r + 256])
        dist, idx = tree.query(np.column_stack([gxx.ravel(), gyy.ravel()]), distance_upper_bound=BLEND_EDGE)
        dist = dist.reshape(gxx.shape)
        near = np.isfinite(dist)
        road_h = np.where(near, dz[np.minimum(idx, len(dz) - 1)].reshape(gxx.shape) - GROUND_UNDER, 0)
        t = np.clip((dist - FLAT_EDGE) / (BLEND_EDGE - FLAT_EDGE), 0, 1)
        t = t * t * (3 - 2 * t)
        blk = out[r:r + 256]
        out[r:r + 256] = np.where(near, road_h * (1 - t) + blk * t, blk)
    return out


def carve_under(ground, vx, vy, pieces):
    """Push the ground below every road piece. The pieces are flat slabs, so in bends and
    over crests their corners can sit lower than the smoothed road line the ground was
    shaped to, and the grass would poke up through them. Reaches 2 m (one ground vertex)
    past each piece so the ground's triangles can't come up between vertices either."""
    out = ground.copy()
    x0, y0 = vx[0], vy[0]
    top = PIECE_THICK * THICK_SCALE
    reach = VERTEX
    for x, y, z, yaw, pitch, length in pieces:
        half_l = length * math.cos(pitch) / 2
        r = math.hypot(half_l, ROAD_WIDTH / 2) + reach
        i0, i1 = max(0, int((x - r - x0) / VERTEX)), min(len(vx), int((x + r - x0) / VERTEX) + 2)
        j0, j1 = max(0, int((y - r - y0) / VERTEX)), min(len(vy), int((y + r - y0) / VERTEX) + 2)
        if i0 >= i1 or j0 >= j1:
            continue
        gxx, gyy = np.meshgrid(vx[i0:i1] - x, vy[j0:j1] - y)
        u = gxx * math.cos(yaw) + gyy * math.sin(yaw)
        v = -gxx * math.sin(yaw) + gyy * math.cos(yaw)
        inside = (np.abs(u) <= half_l + reach) & (np.abs(v) <= ROAD_WIDTH / 2 + reach)
        h = z - ROAD_SINK + np.clip(u, -half_l, half_l) * math.tan(pitch) + top / math.cos(pitch) - CARVE
        blk = out[j0:j1, i0:i1]
        out[j0:j1, i0:i1] = np.where(inside, np.minimum(blk, h), blk)
    cut = ground - out
    print(f"ground pushed under the road at {(cut > 0.01).sum()} vertices, "
          f"{(cut > 0.1).sum()} by over 10 cm, most {cut.max():.2f} m")
    return out


def preview(cells, heights, lo, hi, path):
    """Top-down 220x150 picture like the editor's thumbnail: shaded ground, road on top."""
    g = (heights[::-1] - lo) / max(hi - lo, 1)
    rgb = np.stack([60 + 80 * g, 110 + 90 * g, 30 + 30 * g], axis=-1).astype(np.uint8)
    im = Image.new("RGB", (220, 150), (86, 80, 110))
    im.paste(Image.fromarray(rgb).resize((140, 140)), (40, 5))
    d = ImageDraw.Draw(im)
    s = 140 / TILE_SIZE
    r = ROAD_WIDTH / 2 * s
    for (cx, cy), v in cells.items():
        for a, _ in v:
            x = 40 + (cx * smtile.CELL_SIZE + a.pos[0]) * s
            y = 145 - (cy * smtile.CELL_SIZE + a.pos[1]) * s
            d.ellipse([x - r, y - r, x + r, y + r], fill=(46, 46, 46))
    im.save(path)


def overview(pts, gx, gy, tw, th, ground, path):
    s = 0.2
    g = ground[::-1][::5, ::5]
    g = (g - g.min()) / max(g.max() - g.min(), 1)
    rgb = np.stack([40 + 120 * g, 70 + 140 * g, 30 + 60 * g], axis=-1).astype(np.uint8)
    im = Image.fromarray(rgb).resize((int(tw * TILE_SIZE * s), int(th * TILE_SIZE * s)))
    d = ImageDraw.Draw(im)
    for i in range(tw + 1):
        d.line([(i * TILE_SIZE * s, 0), (i * TILE_SIZE * s, im.height)], fill=(0, 0, 0))
    for j in range(th + 1):
        d.line([(0, j * TILE_SIZE * s), (im.width, j * TILE_SIZE * s)], fill=(0, 0, 0))
    d.line([((x - gx) * s, im.height - (y - gy) * s) for x, y in pts + [pts[0]]], fill=(250, 250, 250), width=3)
    im.save(path)


MOD_LOCAL_ID = "25693dc9-936b-49b4-9226-4f3a25dd1a5e"
MOD_SOURCE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "mod")


def write_mod(args, mod_dir, made, tw, th, spawn_local, spawn_heading):
    """Custom Game mod: our scripts, the tiles (already written), a world file that only
    covers the tile area, and the spawn point. The road asset itself (mesh, textures,
    asset set) is copied in on the PC from the "Sugar Rush Matte Road" mod."""
    import shutil
    shutil.copytree(MOD_SOURCE, mod_dir, dirs_exist_ok=True)
    x0 = -(tw * TILE_CELLS) // 2
    y0 = -(th * TILE_CELLS) // 2
    cell_data = []
    for cy in range(th * TILE_CELLS):
        for cx in range(tw * TILE_CELLS):
            _, name = made[(cx // TILE_CELLS, cy // TILE_CELLS)]
            cell_data.append({"offsetX": cx % TILE_CELLS, "offsetY": cy % TILE_CELLS,
                              "path": f"$CONTENT_DATA/Terrain/Tiles/{name}.tile",
                              "rotation": 0, "x": x0 + cx, "y": y0 + cy})
    os.makedirs(os.path.join(mod_dir, "Terrain", "Worlds"), exist_ok=True)
    with open(os.path.join(mod_dir, "Terrain", "Worlds", args.name + ".world"), "w", encoding="utf-8") as f:
        json.dump({"cellData": cell_data, "version": 1}, f)
    sx = x0 * smtile.CELL_SIZE + spawn_local[0]
    sy = y0 * smtile.CELL_SIZE + spawn_local[1]
    with open(os.path.join(mod_dir, "Scripts", "spawn.lua"), "w", encoding="utf-8") as f:
        f.write("-- Written by build.py: where new players appear (T13, start of the lap)\n"
                "-- and the cells the world covers.\n"
                f"NORDS_SPAWN = {{ x = {sx:.1f}, y = {sy:.1f}, yaw = {spawn_heading - math.pi / 2:.4f} }}\n"
                f"NORDS_BOUNDS = {{ xMin = {x0}, xMax = {x0 + tw * TILE_CELLS - 1}, "
                f"yMin = {y0}, yMax = {y0 + th * TILE_CELLS - 1} }}\n")
    with open(os.path.join(mod_dir, "description.json"), "w", encoding="utf-8") as f:
        json.dump({"allow_add_mods": True, "custom_icons": False,
                   "description": "Nurburgring Nordschleife at real size with real hills, 20 m wide road.",
                   "localId": MOD_LOCAL_ID, "name": args.name, "type": "Custom Game", "version": 0},
                  f, indent="\t")
    print(f"mod {mod_dir}: {len(made)} tiles, spawn {sx:.0f}, {sy:.0f}")


def write_world(args, made, tw, th):
    n = args.world_cells
    half = n // 2
    if tw * TILE_CELLS > n or th * TILE_CELLS > n:
        raise SystemExit(f"world too small: need {tw * TILE_CELLS} x {th * TILE_CELLS} cells")
    # Put the tiles in the middle of the world.
    x0 = -(tw * TILE_CELLS) // 2
    y0 = -(th * TILE_CELLS) // 2
    cell_data = []
    for y in range(-half, n - half):
        for x in range(-half, n - half):
            entry = {"offsetX": 0, "offsetY": 0, "path": "", "rotation": 0, "x": x, "y": y}
            tx, ty = (x - x0) // TILE_CELLS, (y - y0) // TILE_CELLS
            if (tx, ty) in made:
                local_id, name = made[(tx, ty)]
                entry.update(offsetX=(x - x0) % TILE_CELLS, offsetY=(y - y0) % TILE_CELLS,
                             path=f"$CONTENT_{local_id}/{name}.tile")
            cell_data.append(entry)
    corners = [{"type": 0, "x": x, "y": y}
               for y in range(-half, n - half + 1) for x in range(-half, n - half + 1)]
    deps = [{"contentId": lid, "name": name + ".tile", "steamFileId": 0} for lid, name in made.values()]
    world = {"cellData": cell_data, "cornerData": corners, "dependencies": deps, "version": 1}
    wid = uuid.uuid4()
    folder = os.path.join(args.out, "Worlds", str(wid))
    os.makedirs(folder, exist_ok=True)
    with open(os.path.join(folder, args.name + ".world"), "w", encoding="utf-8") as f:
        json.dump(world, f, indent="\t")
    with open(os.path.join(folder, "description.json"), "w", encoding="utf-8") as f:
        json.dump({"dependencies": None, "description": "Nurburgring Nordschleife, 20 m wide road",
                   "localId": str(wid), "name": args.name, "type": "World", "version": 0}, f, indent="\t")
    print(f"world {args.name}: {n} x {n} cells, folder {wid}")


if __name__ == "__main__":
    main()
