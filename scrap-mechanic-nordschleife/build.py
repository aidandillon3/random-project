"""Build Nordschleife road tiles for Scrap Mechanic from a Tile Editor template.

  python build.py --template "nords template.tile" --osm osm/nordschleife.json --out out

Every road piece is one "Blank Road (Matte)" asset copied from the template tile
(so its uuid, colours and flags are exactly what the editor wrote), stretched to
ROAD_WIDTH across and to the segment length along the track.
"""

import argparse
import copy
import json
import math
import os

import smtile
import track

ROAD_WIDTH = 20.0      # metres (Scrap Mechanic cars are ~3x real size)
STEP = 8.0             # metres of centre line per road piece
TILE_CELLS = 4         # Large tile = 4x4 cells of 64 m = 256 m
TILE_SIZE = TILE_CELLS * smtile.CELL_SIZE
ROAD_LIFT = 0.05       # metres above flat ground, avoids z-fighting with the terrain


def load_piece(cfg_path):
    with open(cfg_path, encoding="utf-8") as f:
        return json.load(f)


def road_pieces(pts, piece, closed=True):
    """Yield (x, y, z, yaw, length) for each road segment of a resampled centre line."""
    n = len(pts)
    seg_count = n if closed else n - 1
    for i in range(seg_count):
        a, b = pts[i], pts[(i + 1) % n]
        dx, dy = b[0] - a[0], b[1] - a[1]
        seg = math.hypot(dx, dy)
        if seg < 1e-3:
            continue
        yaw = math.atan2(dy, dx)
        # Turn to the next segment: extend the piece so the outside of the bend has no gap.
        nxt = pts[(i + 2) % n] if closed or i + 2 < n else b
        turn = 0.0
        if nxt != b:
            turn = math.atan2(nxt[1] - b[1], nxt[0] - b[0]) - yaw
            turn = (turn + math.pi) % (2 * math.pi) - math.pi
        overlap = ROAD_WIDTH / 2 * math.tan(min(abs(turn), 1.2) / 2) + 0.25
        yield ((a[0] + b[0]) / 2, (a[1] + b[1]) / 2, ROAD_LIFT, yaw, seg + 2 * overlap)


def make_asset(template_asset, piece, x, y, z, yaw, seg_len):
    a = copy.deepcopy(template_asset)
    # piece["length_axis"]: which local axis of the mesh runs along the road ("x" or "y").
    if piece["length_axis"] == "y":
        yaw -= math.pi / 2
        sx, sy = ROAD_WIDTH / piece["size"][0], seg_len / piece["size"][1]
    else:
        sx, sy = seg_len / piece["size"][0], ROAD_WIDTH / piece["size"][1]
    a.rot = (0.0, 0.0, math.sin(yaw / 2), math.cos(yaw / 2))
    a.scale = (sx, sy, template_asset.scale[2])
    # Mesh pivot may not be at its centre: shift by the rotated, scaled pivot offset.
    ox, oy = piece.get("pivot_offset", (0.0, 0.0))
    ox, oy = ox * sx, oy * sy
    c, s = math.cos(yaw), math.sin(yaw)
    a.pos = (x - (ox * c - oy * s), y - (ox * s + oy * c), z + piece.get("z_offset", 0.0))
    return a


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--template", required=True, help="Large .tile saved by the Tile Editor with one road piece")
    ap.add_argument("--piece", default="piece.json", help="road piece size/axis info")
    ap.add_argument("--osm", default="osm/nordschleife.json")
    ap.add_argument("--out", default="out")
    ap.add_argument("--tiles", default="", help="only these tiles, e.g. 0,0;1,0 (default: all with road)")
    ap.add_argument("--start", default="0,0", help="move this world point (m) to the corner of tile 0,0")
    args = ap.parse_args()

    piece = load_piece(args.piece)
    template = smtile.Tile.load(args.template)
    road_asset, road_list = None, 0
    for cy in range(template.height):
        for cx in range(template.width):
            for lst in range(4):
                for a in template.assets(cx, cy, lst):
                    road_asset, road_list = a, lst
    if road_asset is None:
        raise SystemExit("template tile has no asset in it")

    ways = [w for w in track.load_ways(args.osm) if track.is_nordschleife(w)]
    line = track.stitch(ways)
    pts = track.project(line)
    pts = track.resample(track.smooth(track.resample(pts, 4.0), passes=3), STEP)
    closed = math.dist(pts[0], pts[-1]) < STEP * 2
    sx0, sy0 = map(float, args.start.split(","))
    minx = min(p[0] for p in pts); miny = min(p[1] for p in pts)
    # Tile grid origin: bottom-left of the track's bounding box, unless --start says otherwise.
    gx = minx - ROAD_WIDTH if args.start == "0,0" else sx0
    gy = miny - ROAD_WIDTH if args.start == "0,0" else sy0
    print(f"centre line {track.length(pts) / 1000:.2f} km, {len(pts)} points, closed={closed}")

    cells = {}  # (tx, ty) -> {(cx, cy): [assets]}
    for x, y, z, yaw, seg in road_pieces(pts, piece, closed):
        lx, ly = x - gx, y - gy
        tx, ty = int(lx // TILE_SIZE), int(ly // TILE_SIZE)
        cx = int((lx - tx * TILE_SIZE) // smtile.CELL_SIZE)
        cy = int((ly - ty * TILE_SIZE) // smtile.CELL_SIZE)
        px = lx - tx * TILE_SIZE - cx * smtile.CELL_SIZE
        py = ly - ty * TILE_SIZE - cy * smtile.CELL_SIZE
        cells.setdefault((tx, ty), {}).setdefault((cx, cy), []).append(
            make_asset(road_asset, piece, px, py, z, yaw, seg))

    wanted = None
    if args.tiles:
        wanted = {tuple(map(int, t.split(","))) for t in args.tiles.split(";")}
    os.makedirs(args.out, exist_ok=True)
    made = []
    for (tx, ty), tcells in sorted(cells.items()):
        if wanted is not None and (tx, ty) not in wanted:
            continue
        t = smtile.Tile.load(args.template)
        for cy in range(t.height):
            for cx in range(t.width):
                for lst in range(4):
                    t.set_assets(cx, cy, lst, [])
        for (cx, cy), assets in tcells.items():
            t.set_assets(cx, cy, road_list, assets)
        u = t.set_uuid()
        name = f"Nords {tx:02d}-{ty:02d}"
        t.save(os.path.join(args.out, name + ".tile"))
        made.append({"name": name, "tile": [tx, ty], "uuid": str(u),
                     "pieces": sum(len(v) for v in tcells.values())})
        print(f"{name}: {made[-1]['pieces']} road pieces")
    with open(os.path.join(args.out, "tiles.json"), "w", encoding="utf-8") as f:
        json.dump({"grid_origin_m": [gx, gy], "tile_size_m": TILE_SIZE, "tiles": made}, f, indent=1)


if __name__ == "__main__":
    main()
