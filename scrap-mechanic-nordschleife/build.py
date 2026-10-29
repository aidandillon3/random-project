"""Build Nordschleife road tiles (and a world that places them) for Scrap Mechanic.

  python build.py --template "<Tiles>/<uuid of nords template>" --out out --blocks 2x2

The template is a Large tile saved by the Tile Editor with one "Blank Road (Matte)"
piece in it. Every road piece copies that asset (uuid, colour), turned to follow the
track and stretched to ROAD_WIDTH across. Each tile is written three ways, like the
editor does: .tile (what the game loads), .tile.export (same bytes) and .tileson
(what the Tile Editor opens), plus description.json and a preview picture.
"""

import argparse
import copy
import json
import math
import os
import uuid

from PIL import Image, ImageDraw

import smtile
import track

ROAD_WIDTH = 20.0      # metres (Scrap Mechanic cars are ~3x real size)
PIECE_SIZE = 32.0      # Blank Road (Matte) mesh is 32 x 32 m at scale 1 (collision .obj)
THICK_SCALE = 0.5      # mesh is 0.5 m thick at scale 1, so the road slab is 0.25 m
STEP = 8.0             # metres of centre line per road piece
ROAD_Z = 0.05          # metres above the flat ground
TILE_CELLS = 4         # Large tile = 4 x 4 cells of 64 m
TILE_SIZE = TILE_CELLS * smtile.CELL_SIZE
ASSET_LIST = 1         # the list the Tile Editor put the road piece in


def road_pieces(pts):
    """(x, y, yaw, length) for each segment of a closed, evenly resampled centre line."""
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
        yield (a[0] + b[0]) / 2, (a[1] + b[1]) / 2, yaw, seg + 2 * overlap


def best_block(pts, bx, by, within):
    w, h = bx * TILE_SIZE, by * TILE_SIZE
    first = pts[:int(within / STEP)]
    best = None
    for x0 in range(int(min(p[0] for p in first) - w), int(max(p[0] for p in first)), 16):
        for y0 in range(int(min(p[1] for p in first) - h), int(max(p[1] for p in first)), 16):
            n = sum(1 for p in pts if x0 + 10 < p[0] < x0 + w - 10 and y0 + 10 < p[1] < y0 + h - 10)
            if best is None or n > best[0]:
                best = (n, x0, y0)
    return float(best[1]), float(best[2])


def quat_for_yaw(yaw):
    """Rz(yaw) * Rx(90 deg): the mesh is Y-up, the editor stands it up with X = 90."""
    h = math.sqrt(0.5)
    s, c = math.sin(yaw / 2), math.cos(yaw / 2)
    return (c * h, s * h, s * h, c * h)  # x, y, z, w


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--template", required=True, help="Tile Editor folder of the template tile")
    ap.add_argument("--osm", default=os.path.join(os.path.dirname(__file__), "osm", "nordschleife.json"))
    ap.add_argument("--out", default="out")
    ap.add_argument("--blocks", default="2x2", help="how many large tiles to make around the start, e.g. 2x2")
    ap.add_argument("--within-km", type=float, default=4.0)
    ap.add_argument("--world-cells", type=int, default=32, help="world size in 64 m cells")
    ap.add_argument("--name", default="Nordschleife")
    args = ap.parse_args()

    tdir = args.template
    tname = next(f for f in os.listdir(tdir) if f.endswith(".tile"))[:-5]
    template = smtile.Tile.load(os.path.join(tdir, tname + ".tile"))
    with open(os.path.join(tdir, tname + ".tileson"), encoding="utf-8") as f:
        tileson_tpl = json.load(f)
    with open(os.path.join(tdir, "description.json"), encoding="utf-8") as f:
        desc_tpl = json.load(f)
    road = template.assets(1, 1, ASSET_LIST)
    road = road[0] if road else next(a for y in range(4) for x in range(4)
                                     for l in range(4) for a in template.assets(x, y, l))
    road_json = tileson_tpl["entities"]["assets"][0]

    # Centre line in metres, x east / y north, starting at T13 (start of the OSM relation).
    pts = track.project(track.stitch(track.load_ways(args.osm)))
    if math.dist(pts[0], pts[-1]) < 1:
        pts = pts[:-1]
    pts = track.resample(track.smooth(track.resample(pts + [pts[0]], 4.0), passes=3), STEP)
    if math.dist(pts[0], pts[-1]) < STEP / 2:
        pts = pts[:-1]
    print(f"centre line {track.length(pts + [pts[0]]) / 1000:.2f} km, {len(pts)} pieces")

    # Tile grid: place the block of tiles where it holds the most road within the
    # first --within-km of the lap (so the first tiles are the opening of the track).
    bx, by = map(int, args.blocks.lower().split("x"))
    gx, gy = best_block(pts, bx, by, args.within_km * 1000)

    tiles = {}  # (tx, ty) -> {(cx, cy): [(Asset, tileson dict)]}
    for x, y, yaw, seg in road_pieces(pts):
        lx, ly = x - gx, y - gy
        tx, ty = math.floor(lx / TILE_SIZE), math.floor(ly / TILE_SIZE)
        if not (0 <= tx < bx and 0 <= ty < by):
            continue
        px, py = lx - tx * TILE_SIZE, ly - ty * TILE_SIZE
        cx, cy = int(px // smtile.CELL_SIZE), int(py // smtile.CELL_SIZE)
        scale = (seg / PIECE_SIZE, THICK_SCALE, ROAD_WIDTH / PIECE_SIZE)
        a = copy.deepcopy(road)
        a.pos = (px - cx * smtile.CELL_SIZE, py - cy * smtile.CELL_SIZE, ROAD_Z)
        a.rot = quat_for_yaw(yaw)
        a.scale = scale
        j = copy.deepcopy(road_json)
        j["guid"] = str(uuid.uuid4())
        j["transform"] = {"position": [px, py, ROAD_Z], "rotation": [90.0, math.degrees(yaw), 0.0],
                          "scale": list(scale)}
        tiles.setdefault((tx, ty), {}).setdefault((cx, cy), []).append((a, j))

    os.makedirs(args.out, exist_ok=True)
    tiles_out = os.path.join(args.out, "Tiles")
    made = {}
    for ty in range(by):
        for tx in range(bx):
            cells = tiles.get((tx, ty), {})
            name = f"{args.name} {chr(ord('A') + tx)}{ty + 1}"
            local_id, tile_uuid = uuid.uuid4(), uuid.uuid4()
            folder = os.path.join(tiles_out, str(local_id))
            os.makedirs(folder, exist_ok=True)

            t = smtile.Tile(bytes(template.data))
            t.rebuild({k: [a for a, _ in v] for k, v in cells.items()}, ASSET_LIST)
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

            preview(cells, os.path.join(folder, f"{tile_uuid}.png"))
            made[(tx, ty)] = (str(local_id), name)
            print(f"{name}: {sum(len(v) for v in cells.values())} road pieces")

    write_world(args, made, bx, by)


def preview(cells, path):
    """Top-down 220x150 picture like the editor's thumbnail: grass with the road on it."""
    im = Image.new("RGB", (220, 150), (86, 80, 110))
    d = ImageDraw.Draw(im)
    s, ox, oy = 140 / TILE_SIZE, 40, 5
    d.rectangle([ox, oy, ox + 140, oy + 140], fill=(120, 160, 40))
    for (cx, cy), v in cells.items():
        for a, _ in v:
            x = ox + (cx * smtile.CELL_SIZE + a.pos[0]) * s
            y = oy + 140 - (cy * smtile.CELL_SIZE + a.pos[1]) * s
            r = ROAD_WIDTH / 2 * s
            d.ellipse([x - r, y - r, x + r, y + r], fill=(46, 46, 46))
    im.save(path)


def write_world(args, made, bx, by):
    n = args.world_cells
    half = n // 2
    # Put the block of tiles in the middle of the world.
    x0 = -(bx * TILE_CELLS) // 2
    y0 = -(by * TILE_CELLS) // 2
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
    print(f"world {args.name}: {n}x{n} cells, folder {wid}")


if __name__ == "__main__":
    main()
