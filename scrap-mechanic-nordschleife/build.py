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
THICK_SCALE = 0.5      # mesh is 0.5 m thick at scale 1, so the road slab is 0.25 m
STEP = 8.0             # metres of centre line per road piece
GROUND_UNDER = 0.3     # ground sits this far below the road pieces' origin
FLAT_EDGE = ROAD_WIDTH / 2 + 2.0   # ground is flat at road height out to here ...
BLEND_EDGE = 45.0                  # ... and back to the real hillside by here
PROFILE_SMOOTH = 60.0  # metres either side used to smooth the road's height profile
TILE_CELLS = 4         # Large tile = 4 x 4 cells of 64 m
TILE_SIZE = TILE_CELLS * smtile.CELL_SIZE
VERTEX = 2.0           # ground vertex spacing (33 x 33 per cell)
ASSET_LIST = 1         # the list the Tile Editor put the road piece in


def qmul(a, b):
    ax, ay, az, aw = a
    bx, by, bz, bw = b
    return (aw * bx + ax * bw + ay * bz - az * by,
            aw * by - ax * bz + ay * bw + az * bx,
            aw * bz + ax * by - ay * bx + az * bw,
            aw * bw - ax * bx - ay * by - az * bz)


def road_rotation(yaw, pitch):
    """Rz(yaw) * Ry(-pitch) * Rx(90): the mesh is Y-up, the editor stands it up with X = 90.
    Editor angles for the same rotation are (90, yaw, pitch)."""
    qz = (0.0, 0.0, math.sin(yaw / 2), math.cos(yaw / 2))
    qy = (0.0, math.sin(-pitch / 2), 0.0, math.cos(-pitch / 2))
    qx = (math.sin(math.pi / 4), 0.0, 0.0, math.cos(math.pi / 4))
    return qmul(qmul(qz, qy), qx)


def smooth_closed(values, half_window):
    n = len(values)
    k = np.ones(2 * half_window + 1) / (2 * half_window + 1)
    padded = np.concatenate([values[-half_window:], values, values[:half_window]])
    return np.convolve(padded, k, mode="valid")[:n]


def road_pieces(pts, zs):
    """(x, y, z, yaw, pitch, length) per segment of a closed, evenly resampled centre line."""
    n = len(pts)
    for i in range(n):
        a, b, c = pts[i], pts[(i + 1) % n], pts[(i + 2) % n]
        seg = math.dist(a, b)
        if seg < 1e-3:
            continue
        yaw = math.atan2(b[1] - a[1], b[0] - a[0])
        turn = math.atan2(c[1] - b[1], c[0] - b[0]) - yaw
        turn = (turn + math.pi) % (2 * math.pi) - math.pi
        # Lengthen each piece so the outside of a bend has no gap between pieces.
        overlap = ROAD_WIDTH / 2 * math.tan(min(abs(turn), 1.2) / 2) + 0.3
        za, zb = zs[i], zs[(i + 1) % n]
        pitch = math.atan2(zb - za, seg)
        yield ((a[0] + b[0]) / 2, (a[1] + b[1]) / 2, (za + zb) / 2, yaw, pitch,
               seg / math.cos(pitch) + 2 * overlap)


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
        ground = shape_ground(ground, vx, vy, pts, zs)

    # Road pieces into tiles/cells.
    tiles = {}  # (tx, ty) -> {(cx, cy): [(Asset, tileson dict)]}
    for x, y, z, yaw, pitch, length in road_pieces(pts, zs):
        lx, ly = x - gx, y - gy
        tx, ty = math.floor(lx / TILE_SIZE), math.floor(ly / TILE_SIZE)
        px, py = lx - tx * TILE_SIZE, ly - ty * TILE_SIZE
        cx, cy = int(px // smtile.CELL_SIZE), int(py // smtile.CELL_SIZE)
        scale = (length / PIECE_SIZE, THICK_SCALE, ROAD_WIDTH / PIECE_SIZE)
        a = copy.deepcopy(road)
        a.pos = (px - cx * smtile.CELL_SIZE, py - cy * smtile.CELL_SIZE, z)
        a.rot = road_rotation(yaw, pitch)
        a.scale = scale
        j = copy.deepcopy(road_json)
        j["guid"] = str(uuid.uuid4())
        j["transform"] = {"position": [px, py, z],
                          "rotation": [90.0, math.degrees(yaw), math.degrees(pitch)],
                          "scale": list(scale)}
        tiles.setdefault((tx, ty), {}).setdefault((cx, cy), []).append((a, j))

    tiles_out = os.path.join(args.out, "Tiles")
    os.makedirs(tiles_out, exist_ok=True)
    made = {}
    for ty in range(th):
        for tx in range(tw):
            cells = tiles.get((tx, ty), {})
            name = f"{args.name} {tx:02d}-{ty:02d}"
            local_id, tile_uuid = uuid.uuid4(), uuid.uuid4()
            folder = os.path.join(tiles_out, str(local_id))
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

    write_world(args, made, tw, th)
    overview(pts, gx, gy, tw, th, ground, os.path.join(args.out, "overview.png"))


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
