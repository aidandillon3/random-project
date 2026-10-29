"""Nordschleife centre line: OpenStreetMap ways -> ordered loop in local metres."""

import json
import math

# Overpass query used to fetch osm/nordschleife.json (run fetch_osm.py, or paste
# this at https://overpass-turbo.eu and export the raw JSON).
OVERPASS_QUERY = """[out:json][timeout:90];
way["highway"="raceway"](50.31,6.90,50.40,7.02);
out body geom;"""

EARTH_R = 6378137.0


def load_ways(path):
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    ways = []
    for e in data["elements"]:
        if e.get("type") == "way" and "geometry" in e:
            ways.append({
                "id": e["id"],
                "tags": e.get("tags", {}),
                "nodes": e["nodes"],
                "geom": [(p["lat"], p["lon"]) for p in e["geometry"]],
            })
    return ways


def is_nordschleife(way):
    text = " ".join(str(v) for v in way["tags"].values()).lower()
    return "nordschleife" in text


def stitch(ways):
    """Chain ways that share end nodes into the longest path (a loop for the full track)."""
    remaining = list(ways)
    best = []
    while remaining:
        chain_nodes = list(remaining[0]["nodes"])
        chain_geom = list(remaining[0]["geom"])
        remaining.pop(0)
        grown = True
        while grown:
            grown = False
            for w in remaining:
                n, g = w["nodes"], w["geom"]
                if n[0] == chain_nodes[-1]:
                    chain_nodes += n[1:]; chain_geom += g[1:]
                elif n[-1] == chain_nodes[-1]:
                    chain_nodes += n[-2::-1]; chain_geom += g[-2::-1]
                elif n[-1] == chain_nodes[0]:
                    chain_nodes = n[:-1] + chain_nodes; chain_geom = g[:-1] + chain_geom
                elif n[0] == chain_nodes[0]:
                    chain_nodes = n[:0:-1] + chain_nodes; chain_geom = g[:0:-1] + chain_geom
                else:
                    continue
                remaining.remove(w)
                grown = True
                break
        if len(chain_geom) > len(best):
            best = chain_geom
    return best


def project(latlon, origin=None):
    """Equirectangular projection to metres: x east, y north."""
    if origin is None:
        origin = latlon[0]
    lat0, lon0 = map(math.radians, origin)
    k = math.cos(lat0)
    return [((math.radians(lon) - lon0) * k * EARTH_R, (math.radians(lat) - lat0) * EARTH_R)
            for lat, lon in latlon]


def length(pts):
    return sum(math.dist(pts[i], pts[i + 1]) for i in range(len(pts) - 1))


def resample(pts, step):
    """Evenly spaced points along a polyline."""
    out = [pts[0]]
    carry = 0.0
    for i in range(len(pts) - 1):
        (x0, y0), (x1, y1) = pts[i], pts[i + 1]
        seg = math.dist(pts[i], pts[i + 1])
        d = step - carry
        while d <= seg:
            t = d / seg
            out.append((x0 + (x1 - x0) * t, y0 + (y1 - y0) * t))
            d += step
        carry = seg - (d - step)
    if math.dist(out[-1], pts[-1]) > step * 0.25:
        out.append(pts[-1])
    return out


def smooth(pts, passes=2, closed=True):
    """Light Chaikin-free averaging to take the kinks out of OSM nodes."""
    for _ in range(passes):
        n = len(pts)
        new = []
        for i in range(n):
            if not closed and (i == 0 or i == n - 1):
                new.append(pts[i]); continue
            a, b, c = pts[i - 1], pts[i], pts[(i + 1) % n]
            new.append(((a[0] + 2 * b[0] + c[0]) / 4, (a[1] + 2 * b[1] + c[1]) / 4))
        pts = new
    return pts


def unproject(x, y, origin):
    """Inverse of project(): local metres back to (lat, lon)."""
    lat0, lon0 = origin
    k = math.cos(math.radians(lat0))
    return lat0 + math.degrees(y / EARTH_R), lon0 + math.degrees(x / (EARTH_R * k))
