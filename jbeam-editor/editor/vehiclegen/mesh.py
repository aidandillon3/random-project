"""3D models of a generated vehicle: body shell, interior, engine or motor, suspension, rims and tires.

Everything is built from the same numbers as the node structure (the body is
lofted through Shape.section), modelled where it sits on the vehicle, except
rims and tires, which are modelled around their own centre and placed by the
wheel slot like the game's wheels. Triangles face outward (counter-clockwise
seen from outside).
"""
import base64
import math
from array import array

from .chassis import clamp, v_add, v_cross, v_dot, v_len, v_mul, v_norm, v_sub
from .shape import K, S_BELT, S_EDGE, S_SHOULDER, S_TOP, lerp


class Mesh:
    def __init__(self, name):
        self.name = name
        self.p = []
        self.t = {}

    def v(self, x, y=None, z=None):
        if y is None:
            x, y, z = x
        self.p += [float(x), float(y), float(z)]
        return len(self.p) // 3 - 1

    def at(self, i):
        return (self.p[3 * i], self.p[3 * i + 1], self.p[3 * i + 2])

    def tri(self, mat, a, b, c):
        self.t.setdefault(mat, []).extend((a, b, c))

    def quad(self, mat, a, b, c, d):
        self.tri(mat, a, b, c)
        self.tri(mat, a, c, d)

    def otri(self, mat, a, b, c, out):
        """Triangle turned so its front faces the direction out."""
        n = v_cross(v_sub(self.at(b), self.at(a)), v_sub(self.at(c), self.at(a)))
        if v_dot(n, out) < 0:
            b, c = c, b
        self.tri(mat, a, b, c)

    def oquad(self, mat, a, b, c, d, out):
        self.otri(mat, a, b, c, out)
        self.otri(mat, a, c, d, out)

    def count(self):
        return len(self.p) // 3

    def tri_count(self):
        return sum(len(v) for v in self.t.values()) // 3

    def merged(self):
        """(indices, [[start, count, material]]) with each material's triangles together."""
        idx, groups = [], []
        for mat, lst in self.t.items():
            if len(lst) < 3:
                continue
            groups.append([len(idx), len(lst), mat])
            idx += lst
        return idx, groups

    def pos_bytes(self):
        return array("f", self.p).tobytes()

    def idx_bytes(self):
        return array("I", self.merged()[0]).tobytes()

    def to_preview(self, materials):
        idx, groups = self.merged()
        return {"pos": base64.b64encode(array("f", self.p).tobytes()).decode("ascii"),
                "idx": base64.b64encode(array("I", idx).tobytes()).decode("ascii"),
                "groups": groups}

    # -- shapes ---------------------------------------------------------------------
    def tube(self, mat, a, b, r, sides=8, caps=True, r_b=None):
        """A round bar from a to b."""
        r_b = r if r_b is None else r_b
        axis = v_sub(b, a)
        ln = v_len(axis)
        if ln < 1e-6:
            return
        d = v_mul(axis, 1 / ln)
        ref = (0, 0, 1) if abs(d[2]) < 0.9 else (1, 0, 0)
        u = v_norm(v_cross(d, ref))
        w = v_cross(d, u)
        ra, rb = [], []
        for k in range(sides):
            ang = 2 * math.pi * k / sides
            c, s = math.cos(ang), math.sin(ang)
            off = v_add(v_mul(u, c), v_mul(w, s))
            ra.append(self.v(v_add(a, v_mul(off, r))))
            rb.append(self.v(v_add(b, v_mul(off, r_b))))
        for k in range(sides):
            k2 = (k + 1) % sides
            ang = 2 * math.pi * (k + 0.5) / sides
            out = v_add(v_mul(u, math.cos(ang)), v_mul(w, math.sin(ang)))
            self.oquad(mat, ra[k], ra[k2], rb[k2], rb[k], out)
        if caps:
            ca, cb = self.v(a), self.v(b)
            for k in range(sides):
                k2 = (k + 1) % sides
                self.otri(mat, ca, ra[k], ra[k2], v_mul(d, -1))
                self.otri(mat, cb, rb[k], rb[k2], d)

    def box(self, mat, c, h, axes=None):
        """A box centred at c with half sizes h along axes (default x, y, z)."""
        ax = axes or ((1, 0, 0), (0, 1, 0), (0, 0, 1))
        corners = {}
        for i in (-1, 1):
            for j in (-1, 1):
                for k in (-1, 1):
                    p = v_add(c, v_add(v_mul(ax[0], i * h[0]), v_add(v_mul(ax[1], j * h[1]), v_mul(ax[2], k * h[2]))))
                    corners[(i, j, k)] = self.v(p)
        faces = [((1, 0, 0), [(1, -1, -1), (1, 1, -1), (1, 1, 1), (1, -1, 1)]),
                 ((-1, 0, 0), [(-1, -1, -1), (-1, -1, 1), (-1, 1, 1), (-1, 1, -1)]),
                 ((0, 1, 0), [(-1, 1, -1), (-1, 1, 1), (1, 1, 1), (1, 1, -1)]),
                 ((0, -1, 0), [(-1, -1, -1), (1, -1, -1), (1, -1, 1), (-1, -1, 1)]),
                 ((0, 0, 1), [(-1, -1, 1), (1, -1, 1), (1, 1, 1), (-1, 1, 1)]),
                 ((0, 0, -1), [(-1, -1, -1), (-1, 1, -1), (1, 1, -1), (1, -1, -1)])]
        for n, quad in faces:
            out = v_add(v_mul(ax[0], n[0]), v_add(v_mul(ax[1], n[1]), v_mul(ax[2], n[2])))
            q = [corners[x] for x in quad]
            self.oquad(mat, q[0], q[1], q[2], q[3], out)

    def revolve(self, mat, profile, segments, centre=(0, 0, 0), radial_mod=None, flip=False):
        """Surface of revolution about the x axis through profile [(r, x), ...].

        Faces point to the right of the profile's direction in the (x, r) plane: away from the axis where x
        falls along the profile, towards +x where r grows. flip=True turns every face round. The game draws
        only the front of a face, so a surface turned the wrong way is invisible from outside."""
        rings = []
        for k in range(segments):
            ang = 2 * math.pi * k / segments
            c, s = math.cos(ang), math.sin(ang)
            ring = []
            for j, (r, x) in enumerate(profile):
                rr = r * (radial_mod(k, j) if radial_mod else 1.0)
                ring.append(self.v(centre[0] + x, centre[1] + c * rr, centre[2] + s * rr))
            rings.append(ring)
        for k in range(segments):
            k2 = (k + 1) % segments
            ang = 2 * math.pi * (k + 0.5) / segments
            radial = (0.0, math.cos(ang), math.sin(ang))
            for j in range(len(profile) - 1):
                (r0, x0), (r1, x1) = profile[j], profile[j + 1]
                # outward normal of this profile segment in the (x, r) plane, rotated into place
                tx, tr = x1 - x0, r1 - r0
                nx, nr = tr, -tx
                if flip:
                    nx, nr = -nx, -nr
                out = (nx, radial[1] * nr, radial[2] * nr)
                if abs(nx) < 1e-9 and abs(nr) < 1e-9:
                    continue
                self.oquad(mat, rings[k][j], rings[k2][j], rings[k2][j + 1], rings[k][j + 1], out)


# ---------------------------------------------------------------------------
# materials
# ---------------------------------------------------------------------------

def materials(g):
    P = g.P
    m = P.model
    paint = P.paintRGB
    trim = {"black": ([0.05, 0.05, 0.055], 0.1, 0.55, False), "body": (paint, P.metallic, 0.3, True),
            "chrome": ([0.85, 0.86, 0.88], 1.0, 0.08, False)}[P.trim]
    tint = clamp(P.glassTint, 0.0, 1.0)
    glass = [lerp(0.55, 0.05, tint), lerp(0.62, 0.06, tint), lerp(0.66, 0.07, tint)]
    return {
        m + "_paint": {"color": paint, "metallic": P.metallic, "roughness": lerp(0.45, 0.2, P.metallic), "paint": True},
        m + "_trim": {"color": trim[0], "metallic": trim[1], "roughness": trim[2], "paint": trim[3]},
        m + "_black": {"color": [0.04, 0.04, 0.045], "metallic": 0.0, "roughness": 0.7},
        m + "_under": {"color": [0.05, 0.05, 0.05], "metallic": 0.0, "roughness": 0.9},
        m + "_glass": {"color": glass, "metallic": 0.2, "roughness": 0.05, "opacity": lerp(0.45, 0.85, tint),
                       "glass": True},
        m + "_headlight": {"color": [0.92, 0.94, 0.96], "metallic": 0.6, "roughness": 0.1, "glow": "head",
                           "emissive": [1.0, 0.97, 0.88]},
        m + "_taillight": {"color": [0.55, 0.03, 0.03], "metallic": 0.2, "roughness": 0.15, "glow": "tail",
                           "emissive": [1.0, 0.05, 0.03]},
        m + "_interior": {"color": [0.13, 0.13, 0.14], "metallic": 0.0, "roughness": 0.8},
        m + "_seat": {"color": [0.1, 0.1, 0.11], "metallic": 0.0, "roughness": 0.9},
        m + "_metal": {"color": [0.22, 0.23, 0.25], "metallic": 0.7, "roughness": 0.45},
        m + "_engine": {"color": [0.35, 0.36, 0.38], "metallic": 0.6, "roughness": 0.5},
        m + "_hv": {"color": [0.95, 0.42, 0.05], "metallic": 0.0, "roughness": 0.6},
        m + "_chrome": {"color": [0.86, 0.87, 0.9], "metallic": 1.0, "roughness": 0.08},
        m + "_rim": {"color": P.rimRGB, "metallic": 0.85, "roughness": 0.25},
        m + "_tire": {"color": [0.05, 0.05, 0.05], "metallic": 0.0, "roughness": 0.95},
        m + "_spring": {"color": [0.75, 0.18, 0.12], "metallic": 0.4, "roughness": 0.4},
    }


# ---------------------------------------------------------------------------
# body
# ---------------------------------------------------------------------------

def body_stations(sh):
    step = 0.03
    n = int(math.ceil((sh.y1 - sh.y0) / step))
    ys = [sh.y0 + (sh.y1 - sh.y0) * i / n for i in range(n + 1)]
    extra = []
    if sh.y_open0 is not None:
        extra += [sh.y_open0 - 0.006, sh.y_open0 + 0.006, sh.y_open1 - 0.006, sh.y_open1 + 0.006]
    for a in sh.axles:
        extra += [a["y"] - a["arch"] - 0.004, a["y"] - a["arch"] + 0.004, a["y"] + a["arch"] - 0.004,
                  a["y"] + a["arch"] + 0.004]
    ys = sorted(set(round(y, 5) for y in ys + [e for e in extra if sh.y0 < e < sh.y1]))
    out = [ys[0]]
    for y in ys[1:]:
        if y - out[-1] > 0.003:
            out.append(y)
    return out


def ring_points(pts, y):
    """34 vertices: bottom centre, left side up, top centre, right side down."""
    left = [(x, y, z) for x, z in pts]
    right = [(-x, y, z) for x, z in pts[1:K - 1]]
    return left + right[::-1]


def section_k(i):
    return i if i < K else 2 * (K - 1) - i


def body_mesh(g, mats):
    sh, P = g.shape, g.P
    m = P.model
    M = Mesh(m + "_body")
    paint, glass, trim, under = m + "_paint", m + "_glass", m + "_trim", m + "_under"
    head, tail, black, interior = m + "_headlight", m + "_taillight", m + "_black", m + "_interior"
    ys = body_stations(sh)
    rings, info = [], []
    for y in ys:
        pts = sh.section(y)
        rings.append([M.v(p) for p in ring_points(pts, y)])
        info.append((y, sh.kind(y), pts))
    n = len(rings[0])
    for j in range(len(rings) - 1):
        r0, r1 = rings[j], rings[j + 1]
        y0, kind0, pts0 = info[j]
        y1, kind1, _ = info[j + 1]
        ym = (y0 + y1) / 2
        kind = kind0 if kind0 == kind1 else ("wall" if "open" in (kind0, kind1) else kind0)
        for i in range(n):
            i2 = (i + 1) % n
            k = min(section_k(i), section_k(i2))
            pa = M.at(r0[i])
            pb = M.at(r0[i2])
            zc = (pa[2] + pb[2]) / 2
            xc = (abs(pa[0]) + abs(pb[0])) / 2
            mat = _body_material(sh, P, ym, k, zc, xc, kind, mats)
            # the triangle rule: (A, D, C) and (A, C, B) face outward with this ring order
            a, b, c, d = r0[i], r0[i2], r1[i2], r1[i]
            M.tri(mat, a, d, c)
            M.tri(mat, a, c, b)
    _caps(M, sh, P, rings, info, mats)
    _mirrors(M, sh, P)
    if P.openTop:
        _roadster_windshield(M, sh, P)
    return M


def _body_material(sh, P, y, k, z, x, kind, mats):
    m = P.model
    near_front = y < sh.y0 + 0.16
    near_back = y > sh.y1 - 0.12
    if k < 4:
        return m + "_under"
    if k < 6:
        return m + "_trim" if (near_front or near_back) and P.trim != "body" else m + "_paint"
    if kind == "wall":
        if k >= S_BELT:
            return m + "_paint" if k < S_BELT + 1 else (m + "_black" if P.bed else m + "_interior")
        return m + "_paint"
    if k < S_BELT:
        if near_front and k >= 7 and x > 0.35 * P.width / 2 and z > sh.shoulder(y) - 0.07:
            return m + "_headlight"
        if near_back and k >= 7 and x > 0.45 * P.width / 2 and z > sh.shoulder(y) - 0.1:
            return m + "_taillight"
        return m + "_paint"
    if kind == "open":
        if k == S_BELT:
            return m + "_paint" if not P.bed else m + "_trim"
        return m + "_black" if P.bed else m + "_interior"
    if k < S_EDGE:
        return m + "_glass" if kind == "cabin" and sh.side_window(y, z) else m + "_paint"
    if kind == "cabin" and (sh.windshield(y) or sh.rear_window(y)):
        return m + "_glass"
    return m + "_paint"


def _caps(M, sh, P, rings, info, mats):
    m = P.model
    for idx, front in ((0, True), (-1, False)):
        # own vertices, so the flat face is not shaded like the rounded sides it meets
        ring = [M.v(M.at(i)) for i in rings[idx]]
        y, kind, pts = info[idx]
        poly = [(M.at(i)[0], M.at(i)[2]) for i in ring]
        tris = ear_clip(poly)
        out = (0, -1, 0) if front else (0, 1, 0)
        for a, b, c in tris:
            M.otri(m + "_paint", ring[a], ring[b], ring[c], out)
        # bumper, grille and lights as thin panels just in front of the face
        w = sh.half_width(y)
        zb = sh.bottom(y)
        zsh = sh.shoulder(y)
        zbelt = sh.belt(y)
        dy = -0.004 if front else 0.004
        yy = y + dy
        bumper = m + ("_trim" if P.trim != "body" else "_paint")
        _panel(M, bumper, yy, -w * 0.93, w * 0.93, zb + 0.015, zb + min(0.16, (zsh - zb) * 0.45), out)
        lamps = lamp_layout(sh)
        if front:
            lz0 = lamps["lz0"]
            gz0 = zb + min(0.17, (zsh - zb) * 0.48) + 0.02
            gz1 = max(lz0 - 0.015, gz0 + 0.06)
            if gz1 > gz0 + 0.04:
                _panel(M, m + "_black", yy, -w * 0.38, w * 0.38, gz0, gz1, out)
            for x0, x1, z0, z1 in lamps["head"]:
                _panel(M, m + "_headlight", yy, x0, x1, z0, z1, out)
        else:
            for x0, x1, z0, z1 in lamps["tail"]:
                _panel(M, m + "_taillight", yy, x0, x1, z0, z1, out)
            # number plate recess
            pz0 = zb + min(0.17, (zsh - zb) * 0.48) + 0.02
            if pz0 + 0.12 < zbelt:
                _panel(M, m + "_black", yy + 0.001, -0.26, 0.26, pz0, pz0 + 0.12, out)


def lamp_layout(sh):
    """Where the head and tail lamps are: {"head": [(x0, x1, z0, z1)] on the front face (y0),
    "tail": [...] on the back face (y1)}, left lamp first."""
    out = {"head": [], "tail": [], "lz0": 0.0}
    for front in (True, False):
        y = sh.y0 if front else sh.y1
        w, zb, zsh, zbelt = sh.half_width(y), sh.bottom(y), sh.shoulder(y), sh.belt(y)
        if front:
            lz1 = min(zbelt - 0.015, zsh + 0.05)
            lz0 = max(lz1 - clamp((zsh - zb) * 0.3, 0.07, 0.2), zb + 0.2)
            out["lz0"] = lz0
            span = (0.46, 0.88)
        else:
            lz0, lz1 = zsh - 0.1, min(zbelt - 0.015, zsh + 0.04)
            span = (0.6, 0.92)
        if lz1 > lz0 + 0.025:
            for s in (1, -1):
                x0, x1 = sorted((s * w * span[0], s * w * span[1]))
                out["head" if front else "tail"].append((x0, x1, lz0, lz1))
    return out


def _panel(M, mat, y, x0, x1, z0, z1, out):
    a, b = M.v(x0, y, z0), M.v(x1, y, z0)
    c, d = M.v(x1, y, z1), M.v(x0, y, z1)
    M.oquad(mat, a, b, c, d, out)


def _mirrors(M, sh, P):
    if P.openTop:
        y = sh.y_cowl + 0.18
    else:
        y = min(sh.y_cowl + 0.22, sh.y_wst - 0.05)
    for s in (1, -1):
        w = sh.half_width(y)
        z = sh.belt(y) + 0.06
        c = (s * (w + 0.07), y, z)
        M.box(P.model + ("_trim" if P.trim == "black" else "_paint"), c, (0.06, 0.035, 0.05))
        M.tube(P.model + "_black", (s * (w - 0.01), y + 0.01, z - 0.02), (s * (w + 0.03), y + 0.01, z - 0.01), 0.012, 6)


def _roadster_windshield(M, sh, P):
    m = P.model
    rake = math.radians(P.windshieldAngle)
    y0, z0 = sh.y_cowl + 0.02, sh.z_cowl
    h = 0.32
    y1, z1 = y0 + h * math.cos(rake), z0 + h * math.sin(rake)
    w0 = sh.half_width(y0) * 0.86
    w1 = w0 * 0.92
    a, b = M.v(w0, y0, z0), M.v(-w0, y0, z0)
    c, d = M.v(-w1, y1, z1), M.v(w1, y1, z1)
    normal = (0, -math.sin(rake), math.cos(rake))
    M.oquad(m + "_glass", a, b, c, d, normal)
    a2, b2 = M.v(w0, y0 + 0.004, z0), M.v(-w0, y0 + 0.004, z0)
    c2, d2 = M.v(-w1, y1 + 0.004, z1), M.v(w1, y1 + 0.004, z1)
    M.oquad(m + "_glass", a2, b2, c2, d2, v_mul(normal, -1))
    for s in (1, -1):
        M.tube(m + "_black", (s * w0, y0, z0), (s * w1, y1, z1), 0.016, 6)
    M.tube(m + "_black", (w1, y1, z1), (-w1, y1, z1), 0.016, 6)


def ear_clip(poly):
    """Triangles (index triples) of a simple polygon given as [(x, z)]."""
    n = len(poly)
    idx = list(range(n))
    area = sum(poly[i][0] * poly[(i + 1) % n][1] - poly[(i + 1) % n][0] * poly[i][1] for i in range(n))
    ccw = area > 0
    out = []
    guard = 0
    # drop exact duplicates first (degenerate wheel-well points)
    clean = []
    for i in idx:
        if clean and abs(poly[i][0] - poly[clean[-1]][0]) < 1e-7 and abs(poly[i][1] - poly[clean[-1]][1]) < 1e-7:
            continue
        clean.append(i)
    if len(clean) > 1 and abs(poly[clean[0]][0] - poly[clean[-1]][0]) < 1e-7 and \
            abs(poly[clean[0]][1] - poly[clean[-1]][1]) < 1e-7:
        clean.pop()
    idx = clean

    def cross(o, a, b):
        return (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0])

    def inside(p, a, b, c):
        d1, d2, d3 = cross(a, b, p), cross(b, c, p), cross(c, a, p)
        if ccw:
            return d1 > 1e-12 and d2 > 1e-12 and d3 > 1e-12
        return d1 < -1e-12 and d2 < -1e-12 and d3 < -1e-12

    while len(idx) > 3 and guard < 10000:
        guard += 1
        found = False
        for k in range(len(idx)):
            i0, i1, i2 = idx[k - 1], idx[k], idx[(k + 1) % len(idx)]
            a, b, c = poly[i0], poly[i1], poly[i2]
            cr = cross(a, b, c)
            if (cr <= 1e-12) if ccw else (cr >= -1e-12):
                continue
            if any(inside(poly[j], a, b, c) for j in idx if j not in (i0, i1, i2)):
                continue
            out.append((i0, i1, i2))
            idx.pop(k)
            found = True
            break
        if not found:
            # left-over slivers: cut a fan so nothing is left open
            for k in range(1, len(idx) - 1):
                out.append((idx[0], idx[k], idx[k + 1]))
            idx = []
            break
    if len(idx) == 3:
        out.append(tuple(idx))
    return out


# ---------------------------------------------------------------------------
# interior, engine, exhaust
# ---------------------------------------------------------------------------

def interior_mesh(g):
    sh, P = g.shape, g.P
    m = P.model
    M = Mesh(m + "_interior")
    cam = g.camera
    floor = cam["floor"]
    xs = cam["seat_x"]
    ys = cam["seat_y"] + 0.12
    # everything scales down to fit under a low roof
    room = (sh.top(ys + 0.3) - floor - 0.06) if sh.kind(ys + 0.3) == "cabin" else 1.1
    f = clamp(room / 1.05, 0.55, 1.0)
    for s in (1, -1):
        x = s * xs
        M.box(m + "_seat", (x, ys, floor + 0.2 * f), (0.24, 0.24, 0.07 * f))
        back = (x, ys + 0.24, floor + 0.55 * f)
        M.box(m + "_seat", back, (0.23, 0.07, 0.3 * f), ((1, 0, 0), v_norm((0, 1, 0.25)), v_norm((0, -0.25, 1))))
        M.box(m + "_seat", (x, ys + 0.3, floor + 0.93 * f), (0.12, 0.05, 0.08 * f))
    # rear bench when the cabin is long enough
    if not P.openTop and not P.bed and (sh.y_rt - sh.y_wst) > 1.35:
        yb = ys + 0.85
        rf = clamp(((sh.top(yb + 0.3) - floor - 0.06) if sh.kind(yb + 0.3) == "cabin" else 0.9) / 0.9, 0.4, 1.0)
        M.box(m + "_seat", (0, yb, floor + 0.22 * rf), (xs + 0.22, 0.25, 0.08 * rf))
        M.box(m + "_seat", (0, yb + 0.26, floor + 0.55 * rf), (xs + 0.2, 0.07, 0.3 * rf))
    # dashboard across the cabin under the windshield
    yd = (sh.y_cowl + 0.28) if not P.openTop else sh.y_cowl + 0.2
    wd = sh.half_width(yd) - 0.1
    zd = sh.belt(yd) - 0.1
    M.box(m + "_interior", (0, yd, zd), (wd, 0.17, 0.11))
    # steering wheel in front of the driver (left-hand drive: +x)
    sx = xs
    sy = cam["seat_y"] - 0.28
    sz = zd + 0.12
    ring = []
    tilt = math.radians(25)
    nrm = (0, math.cos(tilt), -math.sin(tilt))
    u = (1, 0, 0)
    w = v_norm(v_cross(nrm, u))
    seg = 16
    pts = []
    for k in range(seg):
        a = 2 * math.pi * k / seg
        pts.append(v_add((sx, sy, sz), v_add(v_mul(u, 0.18 * math.cos(a)), v_mul(w, 0.18 * math.sin(a)))))
    for k in range(seg):
        M.tube(m + "_black", pts[k], pts[(k + 1) % seg], 0.016, 6, caps=False)
    M.tube(m + "_black", (sx, sy, sz), v_add((sx, sy, sz), (0, 0.32, -0.12)), 0.025, 6)
    for k in (0, seg // 2, 3 * seg // 4):
        M.tube(m + "_black", (sx, sy, sz), pts[k], 0.012, 5)
    return M


def engine_mesh(g):
    P = g.P
    e = g.engine
    m = P.model
    M = Mesh(m + "_engine")
    yc, z0, w, l, h = e["y"], e["z0"], e["w"], e["l"], e["h"]
    if P.electric:
        # the motor lies across the vehicle with its inverter on top and an orange cable to the battery
        r = min(l, h) * 0.4
        zc = z0 + r + 0.02
        M.tube(m + "_engine", (w / 2 * 0.9, yc, zc), (-w / 2 * 0.9, yc, zc), r, 16)
        top = zc + r
        M.box(m + "_black", (0, yc, top + h * 0.08), (w / 2 * 0.62, l / 2 * 0.7, h * 0.08))
        to_mid = 1 if yc < 0 else -1
        M.tube(m + "_hv", (w * 0.18, yc + to_mid * l / 2 * 0.7, top + h * 0.08),
               (w * 0.18, yc + to_mid * (l / 2 + 0.2), z0 + 0.04), 0.018, 6)
        return M
    M.box(m + "_engine", (0, yc, z0 + h * 0.4), (w / 2 * 0.92, l / 2 * 0.96, h * 0.4))
    M.box(m + "_black", (0, yc, z0 + h * 0.86), (w / 2 * 0.7, l / 2 * 0.86, h * 0.1))
    M.tube(m + "_metal", (0, yc - l / 2 - 0.02, z0 + h * 0.35), (0, yc - l / 2 + 0.03, z0 + h * 0.35), 0.09, 12)
    M.tube(m + "_chrome", (0.0, yc, z0 + h * 0.97), (0.0, yc, z0 + h * 1.05), min(0.14, w * 0.25), 12)
    return M


def exhaust_mesh(g):
    P = g.P
    m = P.model
    M = Mesh(m + "_exhaust")
    a, b, c = g.exhaust
    M.tube(m + "_metal", a, b, 0.032, 8, caps=False)
    M.tube(m + "_metal", b, v_add(c, (0, -0.25, 0)), 0.032, 8, caps=False)
    mid = v_add(b, v_mul(v_sub(c, b), 0.55))
    M.box(m + "_metal", mid, (0.13, 0.22, 0.07))
    M.tube(m + "_chrome", v_add(c, (0, -0.25, 0)), v_add(c, (0, 0.035, 0)), 0.04, 10)
    return M


# ---------------------------------------------------------------------------
# suspension
# ---------------------------------------------------------------------------

def suspension_meshes(g):
    S, P = g.S, g.P
    m = P.model
    meshes, flex, coil_flex = [], {}, {}
    pos = S.pos
    for ax in g.axle_list:
        flex[ax.id], coil_flex[ax.id] = [], []
        k = ax.k
        p = ax.p
        for c in ax.corners:
            side = c.side
            nm = ax.id + side.upper()
            hub = ax.hub[side]
            # knuckle
            M = Mesh("%s_knuckle_%s" % (m, nm))
            h1, h2, h3, h5 = (pos(hub[x]) for x in ("h1", "h2", "h3", "h5"))
            M.tube(m + "_metal", h1, h2, 0.03 * k, 8)
            M.tube(m + "_metal", v_add(h1, v_mul(v_sub(h2, h1), 0.3)), h3, 0.02 * k, 6)
            M.tube(m + "_metal", v_add(h1, v_mul(v_sub(h2, h1), 0.6)), h5, 0.022 * k, 6)
            meshes.append(M)
            flex[ax.id].append((M.name, [P.model_group("hub_" + nm)]))
            if ax.solid:
                ten = pos("%sa1%s" % (p, side))
                low = pos("%sa2%s" % (p, side))
                M = Mesh("%s_link_%s" % (m, nm))
                M.tube(m + "_metal", pos("%sk1%s" % (p, side)), low, 0.028 * k, 8)
                meshes.append(M)
                flex[ax.id].append((M.name, [P.model_group("link_" + nm)]))
                M = Mesh("%s_ulink_%s" % (m, nm))
                M.tube(m + "_metal", pos("%sk2%s" % (p, side)), pos("%sa4" % p), 0.024 * k, 8)
                meshes.append(M)
                flex[ax.id].append((M.name, [P.model_group("ulink_" + nm)]))
                if ax.steer:
                    M = Mesh("%s_tie_%s" % (m, nm))
                    M.tube(m + "_metal", pos("%st1%s" % (p, side)), h3, 0.018 * k, 6)
                    meshes.append(M)
                    flex[ax.id].append((M.name, [P.model_group("tie_" + nm)]))
                lo, hi = ten, pos("%ss1%s" % (p, side))
            else:
                M = Mesh("%s_larm_%s" % (m, nm))
                M.tube(m + "_metal", pos("%sl1%s" % (p, side)), h1, 0.022 * k, 8)
                M.tube(m + "_metal", pos("%sl2%s" % (p, side)), h1, 0.022 * k, 8)
                meshes.append(M)
                flex[ax.id].append((M.name, [P.model_group("larm_" + nm)]))
                M = Mesh("%s_uarm_%s" % (m, nm))
                M.tube(m + "_metal", pos("%su1%s" % (p, side)), h2, 0.018 * k, 8)
                M.tube(m + "_metal", pos("%su2%s" % (p, side)), h2, 0.018 * k, 8)
                meshes.append(M)
                flex[ax.id].append((M.name, [P.model_group("uarm_" + nm)]))
                M = Mesh("%s_tie_%s" % (m, nm))
                M.tube(m + "_metal", pos("%st1%s" % (p, side)), h3, 0.015 * k, 6)
                meshes.append(M)
                flex[ax.id].append((M.name, [P.model_group("tie_" + nm)]))
                lo, hi = h1, pos("%ss1%s" % (p, side))
            M = Mesh("%s_coil_%s" % (m, nm))
            _coilover(M, m, lo, hi, 0.055 * k)
            meshes.append(M)
            coil_flex[ax.id].append((M.name, [P.model_group("coil_" + nm)]))
        if ax.solid:
            M = Mesh("%s_axle_%s" % (m, ax.id))
            a1l, a1r = pos("%sa1l" % p), pos("%sa1r" % p)
            M.tube(m + "_metal", a1l, a1r, 0.045 * k, 10)
            c = pos("%sa3" % p)
            M.tube(m + "_metal", v_add(c, (0, -0.13 * k, 0)), v_add(c, (0, 0.13 * k, 0)), 0.15 * k, 12)
            M.tube(m + "_metal", c, pos("%sa5" % p), 0.06 * k, 8)
            meshes.append(M)
            flex[ax.id].append((M.name, [P.model_group("axle_" + ax.id)]))
    return meshes, flex, coil_flex


def _coilover(M, m, lo, hi, r):
    axis = v_sub(hi, lo)
    ln = v_len(axis)
    if ln < 1e-4:
        return
    d = v_mul(axis, 1 / ln)
    # damper body and rod
    M.tube(m + "_metal", lo, v_add(lo, v_mul(d, ln * 0.55)), r * 0.42, 8)
    M.tube(m + "_chrome", v_add(lo, v_mul(d, ln * 0.5)), hi, r * 0.18, 6)
    # the spring: a helix of short bars
    ref = (0, 0, 1) if abs(d[2]) < 0.9 else (1, 0, 0)
    u = v_norm(v_cross(d, ref))
    w = v_cross(d, u)
    turns = 6
    steps = turns * 10
    a0, a1 = 0.12, 0.92
    prev = None
    for i in range(steps + 1):
        t = i / steps
        ang = 2 * math.pi * turns * t
        p = v_add(lo, v_add(v_mul(d, ln * lerp(a0, a1, t)), v_add(v_mul(u, r * math.cos(ang)), v_mul(w, r * math.sin(ang)))))
        if prev is not None:
            M.tube(m + "_spring", prev, p, r * 0.13, 5, caps=False)
        prev = p


# ---------------------------------------------------------------------------
# wheels
# ---------------------------------------------------------------------------

def rim_mesh(name, g, ax, s):
    """A rim around its own centre; the face points to +x for s = 1 (left) and -x for s = -1."""
    P = g.P
    m = P.model
    w = g.wheels[ax.id]
    R = w["hub_r"] + 0.005
    W = w["hub_w"]
    M = Mesh(name)
    style = P.rimStyle
    mat = m + "_rim"
    dish = {"dish": 0.075, "beadlock": 0.02, "steel": 0.035}.get(style, 0.025)
    lip = 0.016 if style != "beadlock" else 0.03
    x_out = W / 2 - 0.005
    x_face = x_out - dish
    # barrel: inside surface (seen through the spokes) and the two lips
    prof_in = [(R - 0.006, x_out), (R - 0.006, -W / 2 + 0.01)]
    M.revolve(m + "_metal", prof_in, 32, flip=True)
    # the lips: the outer one faces out of the wheel (+x), the inner one into the car, their edge away from the axis
    for x0, sign in ((x_out, 1), (-W / 2 + 0.01, -1)):
        M.revolve(mat, [(R - 0.006, x0), (R + lip, x0)] if sign > 0 else [(R + lip, x0), (R - 0.006, x0)], 32)
    M.revolve(mat, [(R + lip, x_out), (R + lip, x_out - 0.012)], 32)
    # face
    hub_r = 0.075 * clamp(R / 0.22, 0.8, 1.4)
    M.revolve(mat, [(0.0, x_face + 0.03), (hub_r * 0.6, x_face + 0.028), (hub_r, x_face + 0.015)], 16)
    M.revolve(m + "_chrome", [(0.0, x_face + 0.034), (hub_r * 0.45, x_face + 0.031)], 12)
    if style == "steel":
        M.revolve(mat, [(hub_r, x_face + 0.012), (R - 0.006, x_face)], 32)
        for k in range(6):
            a = 2 * math.pi * (k + 0.5) / 6
            cy, cz = math.cos(a) * (hub_r + R) * 0.52, math.sin(a) * (hub_r + R) * 0.52
            M.tube(m + "_black", (x_face + 0.002, cy, cz), (x_face + 0.0035, cy, cz), 0.03, 8)
    else:
        n = {"spoke5": 5, "spoke6": 6, "mesh": 10, "beadlock": 6, "dish": 5}[style]
        width = {"spoke5": 0.38, "spoke6": 0.3, "mesh": 0.12, "beadlock": 0.3, "dish": 0.3}[style]
        for k in range(n):
            a = 2 * math.pi * k / n
            _spoke(M, mat, a, hub_r * 0.85, R - 0.004, x_face + 0.012, x_face, width / n * 2 * math.pi * 0.5, 0.02)
            if style == "mesh":
                _spoke(M, mat, a + math.pi / n, hub_r * 0.9, R - 0.004, x_face + 0.008, x_face - 0.004,
                       width / n * 2 * math.pi * 0.4, 0.014, twist=0.35)
        if style == "beadlock":
            for k in range(16):
                a = 2 * math.pi * k / 16
                cy, cz = math.cos(a) * (R + lip * 0.4), math.sin(a) * (R + lip * 0.4)
                M.tube(m + "_chrome", (x_out, cy, cz), (x_out + 0.008, cy, cz), 0.007, 6)
    if s < 0:
        _mirror_x(M)
    return M


def _spoke(M, mat, ang, r0, r1, x_front, x_back, half_angle_at_hub, thick, twist=0.0):
    def pt(r, a, x):
        return (x, math.cos(a) * r, math.sin(a) * r)
    ha0 = half_angle_at_hub
    ha1 = ha0 * 0.45 * (r0 / r1) * 2.2
    a0l, a0r = ang - ha0, ang + ha0
    a1l, a1r = ang + twist - ha1, ang + twist + ha1
    f = [M.v(pt(r0, a0l, x_front)), M.v(pt(r0, a0r, x_front)), M.v(pt(r1, a1r, x_back + thick * 0.4)),
         M.v(pt(r1, a1l, x_back + thick * 0.4))]
    b = [M.v(pt(r0, a0l, x_front - thick)), M.v(pt(r0, a0r, x_front - thick)), M.v(pt(r1, a1r, x_back - thick)),
         M.v(pt(r1, a1l, x_back - thick))]
    M.oquad(mat, f[0], f[1], f[2], f[3], (1, 0, 0))
    M.oquad(mat, b[0], b[1], b[2], b[3], (-1, 0, 0))
    M.oquad(mat, f[0], f[3], b[3], b[0], _side_out(ang, -1))
    M.oquad(mat, f[1], b[1], b[2], f[2], _side_out(ang, 1))


def _side_out(ang, sign):
    # perpendicular to the spoke direction in the wheel plane
    return (0.0, -math.sin(ang) * sign, math.cos(ang) * sign)


def _mirror_x(M):
    for i in range(0, len(M.p), 3):
        M.p[i] = -M.p[i]
    for mat, lst in M.t.items():
        for k in range(0, len(lst), 3):
            lst[k + 1], lst[k + 2] = lst[k + 2], lst[k + 1]


def tire_mesh(name, g, ax, s):
    P = g.P
    m = P.model
    w = g.wheels[ax.id]
    R, Rr, W = w["r"], w["hub_r"] + 0.005, w["tw"]
    h = R - Rr
    bead = min(W * 0.42, w["hub_w"] / 2)
    prof = [(Rr - 0.003, -bead), (Rr + 0.18 * h, -W * 0.47), (Rr + 0.55 * h, -W * 0.5),
            (R - 0.16 * h, -W * 0.49), (R - 0.04 * h, -W * 0.45), (R, -W * 0.38), (R, -W * 0.13), (R, W * 0.13),
            (R, W * 0.38), (R - 0.04 * h, W * 0.45), (R - 0.16 * h, W * 0.49), (Rr + 0.55 * h, W * 0.5),
            (Rr + 0.18 * h, W * 0.47), (Rr - 0.003, bead)]
    segs = 64 if R < 0.42 else 80
    lug = {"mud": 0.028, "allterrain": 0.014, "street": 0.0, "sport": 0.0, "slick": 0.0}[P.tireType]
    tread_rows = {5: 0, 6: 1, 7: 1, 8: 0, 4: 0, 9: 0}

    def mod(k, j):
        if not lug or j < 4 or j > 9:
            return 1.0
        block = (k // 2) % 2
        row = tread_rows.get(j, 0)
        if (block + row + (1 if j >= 7 else 0)) % 2 == 0:
            return 1.0 + lug / R
        return 1.0
    M = Mesh(name)
    # the profile runs from one bead over the tread to the other, which puts its right-hand side inside the tire
    M.revolve(m + "_tire", prof, segs, radial_mod=mod, flip=True)
    if s < 0:
        _mirror_x(M)
    return M


# ---------------------------------------------------------------------------

def build_all(g):
    mats = materials(g)
    meshes = {}

    def add(M):
        meshes[M.name] = M
    add(body_mesh(g, mats))
    add(interior_mesh(g))
    add(engine_mesh(g))
    if g.exhaust:
        add(exhaust_mesh(g))
    susp, flex, coil = suspension_meshes(g)
    for M in susp:
        add(M)
    for ax in g.axle_list:
        for c in ax.corners:
            add(rim_mesh("%s_rim_%s_%s" % (g.P.model, ax.id, c.side.upper()), g, ax, c.s))
            add(tire_mesh("%s_tire_%s_%s" % (g.P.model, ax.id, c.side.upper()), g, ax, c.s))
    used = set()
    for M in meshes.values():
        used.update(k for k, v in M.t.items() if len(v) >= 3)
    mats = {k: v for k, v in mats.items() if k in used}
    return meshes, mats, {"susp": flex, "coil": coil}
