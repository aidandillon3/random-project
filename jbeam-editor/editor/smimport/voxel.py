"""A node-and-beam body for a shape made of grid cells.

The cells are grouped into coarse cells (2 x 2 x 2 by default, more for big
creations). Every coarse cell with something in it becomes a box of 8 nodes
braced on all 28 node pairs, so it is rigid; boxes sharing a face share 4
nodes. A node on the outside is pulled in to where the material really ends,
so a one-block-thick floor stays one block thick. Boxes that only touch along
an edge or a corner, and pieces that do not touch at all, are braced to each
other so nothing can hinge. Outside faces become collision triangles facing
outward, and each cell's weight goes to the nodes around it.
"""
import math

from ..vehiclegen.chassis import FLT_MAX, clamp, dist, v_cross, v_dot, v_sub

MAX_COARSE = 420          # coarse cells before the boxes are made bigger


def _corner_offsets():
    return [(dx, dy, dz) for dx in (0, 1) for dy in (0, 1) for dz in (0, 1)]


CORNERS = _corner_offsets()
FACES = []
for _k in range(3):
    for _side in (0, 1):
        quad = [d for d in CORNERS if d[_k] == _side]
        # put the four corners in a loop around the face
        a, b = [i for i in range(3) if i != _k]
        quad.sort(key=lambda d, a=a, b=b: math.atan2(d[b] - 0.5, d[a] - 0.5))
        n = [0, 0, 0]
        n[_k] = 1 if _side else -1
        FACES.append((_k, _side, quad, tuple(n)))


class Lattice:
    """cells: {(i, j, k) in the vehicle-aligned grid: (mass, info)}; cs: metres per cell; origin: metres of cell 0."""

    def __init__(self, cells, cs, origin, coarse=None):
        if not cells:
            raise ValueError("the chassis is empty")
        self.cells = cells
        self.cs = cs
        self.origin = origin
        lo = [min(c[k] for c in cells) for k in range(3)]
        self.lo = lo
        C = coarse or 2
        while True:
            occ = {}
            for c in cells:
                q = tuple((c[k] - lo[k]) // C for k in range(3))
                occ.setdefault(q, []).append(c)
            if coarse or len(occ) <= MAX_COARSE or C >= 8:
                break
            C += 1
        self.C = C
        self.occ = occ
        self.box = {}
        for q, cs_ in occ.items():
            self.box[q] = (tuple(min(c[k] for c in cs_) for k in range(3)),
                           tuple(max(c[k] for c in cs_) + 1 for k in range(3)))
        self._vertices()

    # -- geometry ------------------------------------------------------------------------
    def fine_to_m(self, f):
        """A fine-grid coordinate (cells, may be fractional) to metres."""
        return tuple(f[k] * self.cs + self.origin[k] for k in range(3))

    def _vertices(self):
        C, lo = self.C, self.lo
        touch = {}
        for q, (blo, bhi) in self.box.items():
            for d in CORNERS:
                g = (q[0] + d[0], q[1] + d[1], q[2] + d[2])
                t = touch.setdefault(g, [[[], []], [[], []], [[], []]])
                for k in range(3):
                    if d[k] == 0:
                        t[k][0].append(blo[k])     # the cell lies on the + side of this vertex
                    else:
                        t[k][1].append(bhi[k])     # ... on the - side
        self.vpos = {}
        for g, t in touch.items():
            f = []
            for k in range(3):
                plus, minus = t[k]
                if plus and minus:
                    f.append(lo[k] + g[k] * C)
                elif plus:
                    f.append(min(plus))
                else:
                    f.append(max(minus))
            self.vpos[g] = self.fine_to_m(f)

    def cell_vertices(self, q):
        return [(q[0] + d[0], q[1] + d[1], q[2] + d[2]) for d in CORNERS]

    # -- building ------------------------------------------------------------------------
    def build(self, S, part, groups, name_prefix="b", kinds=None, mass_scale=1.0):
        """Add nodes, beams and collision triangles to S. Returns {vertex: node name}."""
        kinds = kinds or {}
        k_edge = kinds.get("edge", "v_edge")
        k_face = kinds.get("face", "v_face")
        k_diag = kinds.get("diag", "v_diag")
        k_brace = kinds.get("brace", "v_brace")
        names = {}
        outside = set()
        faces = []
        for q in self.occ:
            for k, side, quad, n in FACES:
                nb = list(q)
                nb[k] += 1 if side else -1
                if tuple(nb) in self.occ:
                    continue
                verts = [(q[0] + d[0], q[1] + d[1], q[2] + d[2]) for d in quad]
                outside.update(verts)
                faces.append((verts, n))
        order = sorted(self.vpos, key=lambda g: (g[1], g[2], g[0]))
        for i, g in enumerate(order):
            nm = "%s%d" % (name_prefix, i)
            node = S.node(nm, self.vpos[g], part, groups=list(groups), kind="lat",
                          selfcoll=g in outside)
            node.w = 0.0
            names[g] = nm
        self.names = names
        self.outside = outside
        # every box braced on all of its node pairs
        done = set()
        for q in self.occ:
            vs = self.cell_vertices(q)
            for i in range(8):
                for j in range(i + 1, 8):
                    a, b = vs[i], vs[j]
                    key = (a, b) if a < b else (b, a)
                    if key in done:
                        continue
                    done.add(key)
                    nd = sum(1 for k in range(3) if a[k] != b[k])
                    kind = k_edge if nd == 1 else k_face if nd == 2 else k_diag
                    S.beam(names[a], names[b], part, kind)
        self._brace(S, part, k_brace, done)
        # outside faces: two triangles each, front facing out
        for verts, n in faces:
            p = [S.pos(names[v]) for v in verts]
            kind = "under" if n[2] < 0 else "body"
            for tri in ((0, 1, 2), (0, 2, 3)):
                a, b, c = (verts[t] for t in tri)
                pa, pb, pc = p[tri[0]], p[tri[1]], p[tri[2]]
                nrm = v_cross(v_sub(pb, pa), v_sub(pc, pa))
                if v_dot(nrm, n) < 0:
                    b, c = c, b
                S.tri(names[a], names[b], names[c], part, kind)
        self._masses(S, mass_scale)
        return names

    def _brace(self, S, part, kind, done):
        """Tie together groups of boxes that only touch at an edge or corner, or not at all."""
        parent = {q: q for q in self.occ}

        def find(x):
            while parent[x] != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x
        for q in self.occ:
            for k in range(3):
                nb = list(q)
                nb[k] += 1
                nb = tuple(nb)
                if nb in self.occ:
                    ra, rb = find(q), find(nb)
                    if ra != rb:
                        parent[ra] = rb
        comps = {}
        for q in self.occ:
            comps.setdefault(find(q), []).append(q)
        if len(comps) == 1:
            return
        comp_of = {q: find(q) for q in self.occ}
        linked = {}
        # touching at an edge or corner
        for q in self.occ:
            for dx in (-1, 0, 1):
                for dy in (-1, 0, 1):
                    for dz in (-1, 0, 1):
                        if abs(dx) + abs(dy) + abs(dz) < 2:
                            continue
                        nb = (q[0] + dx, q[1] + dy, q[2] + dz)
                        if nb not in self.occ:
                            continue
                        ca, cb = comp_of[q], comp_of[nb]
                        if ca == cb:
                            continue
                        key = (ca, cb) if ca < cb else (cb, ca)
                        linked.setdefault(key, []).append((q, nb))
        # union the groups as we brace them so every group ends up held
        uf = {c: c for c in comps}

        def find2(x):
            while uf[x] != x:
                uf[x] = uf[uf[x]]
                x = uf[x]
            return x
        for (ca, cb), pairs in sorted(linked.items(), key=lambda t: -len(t[1])):
            if find2(ca) == find2(cb):
                continue
            na, nb_ = set(), set()
            for q, r in pairs[:6]:
                na.update(self.cell_vertices(q))
                nb_.update(self.cell_vertices(r))
            if self._tie(S, part, kind, done, na, nb_):
                uf[find2(ca)] = find2(cb)
        # still apart: tie each loose group to its nearest neighbour group
        groups = {}
        for c in comps:
            groups.setdefault(find2(c), []).extend(comps[c])
        while len(groups) > 1:
            keys = sorted(groups, key=lambda g: -len(groups[g]))
            main = keys[0]
            best = None
            mv = {v for q in groups[main] for v in self.cell_vertices(q)}
            for other in keys[1:]:
                ov = {v for q in groups[other] for v in self.cell_vertices(q)}
                d = min(dist(self.vpos[a], self.vpos[b]) for a in mv for b in ov)
                if best is None or d < best[0]:
                    best = (d, other, ov)
            _, other, ov = best
            self._tie(S, part, kind, done, mv, ov, far=True)
            groups[main].extend(groups.pop(other))

    def _tie(self, S, part, kind, done, va, vb, far=False):
        shared = va & vb
        a_only = [v for v in va if v not in shared]
        b_only = [v for v in vb if v not in shared]
        cands = []
        for a in a_only:
            for b in b_only:
                d = dist(self.vpos[a], self.vpos[b])
                if d > 0.05:
                    cands.append((d, a, b))
        cands.sort()
        used = {}
        n = 0
        want = 4 if far or not shared else 3
        for d, a, b in cands:
            if used.get(a, 0) >= 2 or used.get(b, 0) >= 2:
                continue
            key = (a, b) if a < b else (b, a)
            if key in done:
                continue
            done.add(key)
            S.beam(self.names[a], self.names[b], part, kind)
            used[a] = used.get(a, 0) + 1
            used[b] = used.get(b, 0) + 1
            n += 1
            if n >= want:
                break
        return n > 0

    def _masses(self, S, mass_scale):
        """Each cell's weight goes to the 8 nodes of its box, more to the nearer ones."""
        C, lo = self.C, self.lo
        total = 0.0
        for q, cs_ in self.occ.items():
            for c in cs_:
                m = self.cells[c][0] * mass_scale
                if m <= 0:
                    continue
                total += m
                # where the cell's centre sits inside its box (0..1 on each axis)
                t = [clamp(((c[k] + 0.5) - (lo[k] + q[k] * C)) / C, 0.0, 1.0) for k in range(3)]
                for d in CORNERS:
                    f = 1.0
                    for k in range(3):
                        f *= t[k] if d[k] else (1 - t[k])
                    g = (q[0] + d[0], q[1] + d[1], q[2] + d[2])
                    S.nodes[self.names[g]].w += m * f
        self.total_mass = total

    # -- queries ---------------------------------------------------------------------------
    def _columns(self):
        cols = getattr(self, "_cols", None)
        if cols is None:
            cols = {}
            for c in self.cells:
                key = (c[0], c[1])
                if key in cols:
                    a, b = cols[key]
                    cols[key] = (min(a, c[2]), max(b, c[2]))
                else:
                    cols[key] = (c[2], c[2])
            self._cols = cols
        return cols

    def column(self, x, y, reach=None):
        """(bottom, top) in metres of the material near (x, y), or None."""
        reach = reach if reach is not None else self.cs * 1.6
        cols = self._columns()
        ox, oy, oz = self.origin
        i0, i1 = int(math.floor((x - reach - ox) / self.cs)), int(math.floor((x + reach - ox) / self.cs))
        j0, j1 = int(math.floor((y - reach - oy) / self.cs)), int(math.floor((y + reach - oy) / self.cs))
        best = None
        for i in range(i0, i1 + 1):
            for j in range(j0, j1 + 1):
                col = cols.get((i, j))
                if col is None:
                    continue
                z0 = col[0] * self.cs + oz
                z1 = (col[1] + 1) * self.cs + oz
                best = (min(best[0], z0), max(best[1], z1)) if best else (z0, z1)
        return best

    def nearest_cells_top(self, x, y):
        """Top of the material at (x, y), else of the nearest material to it."""
        col = self.column(x, y)
        if col:
            return col[1]
        best = None
        ox, oy, oz = self.origin
        for (i, j), (k0, k1) in self._columns().items():
            px, py = (i + 0.5) * self.cs + ox, (j + 0.5) * self.cs + oy
            d = math.hypot(px - x, py - y)
            top = (k1 + 1) * self.cs + oz
            if best is None or d < best[0] - 1e-6 or (abs(d - best[0]) <= 1e-6 and top > best[1]):
                best = (d, top)
        return best[1]

    def bounds(self):
        lo = [min(p[k] for p in self.vpos.values()) for k in range(3)]
        hi = [max(p[k] for p in self.vpos.values()) for k in range(3)]
        return tuple(lo), tuple(hi)


def lattice_kinds(mass):
    """Beam settings for the lattice, stiffer for heavier vehicles."""
    ks = clamp(mass / 1500.0, 0.3, 5.0) ** 0.65
    return {
        "v_edge": dict(beamSpring=round(2.2e6 * ks, -3) + 1000, beamDamp=round(110 * ks), beamDeform=round(80000 * ks),
                       beamStrength=FLT_MAX),
        "v_face": dict(beamSpring=round(1.6e6 * ks, -3) + 1000, beamDamp=round(90 * ks), beamDeform=round(55000 * ks),
                       beamStrength=FLT_MAX),
        "v_diag": dict(beamSpring=round(1.2e6 * ks, -3) + 1000, beamDamp=round(70 * ks), beamDeform=round(40000 * ks),
                       beamStrength=FLT_MAX),
        "v_brace": dict(beamSpring=round(1.6e6 * ks, -3) + 1000, beamDamp=round(90 * ks), beamDeform=round(55000 * ks),
                        beamStrength=FLT_MAX),
    }
