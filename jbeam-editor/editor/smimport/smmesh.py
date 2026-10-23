"""3D model of a Scrap Mechanic creation: its blocks as merged faces, its parts as simple solids, in its colours."""
from ..vehiclegen.mesh import Mesh

AXIS = ((1, 0, 0), (0, 1, 0), (0, 0, 1))


def _rects(mask):
    """Cover a 2D mask {(u, v): key} with rectangles of one key each: [(u0, v0, u1, v1, key)], ends exclusive."""
    done = set()
    out = []
    for (u, v) in sorted(mask):
        if (u, v) in done:
            continue
        key = mask[(u, v)]
        u1 = u
        while (u1 + 1, v) in mask and (u1 + 1, v) not in done and mask[(u1 + 1, v)] == key:
            u1 += 1
        v1 = v
        while all((x, v1 + 1) in mask and (x, v1 + 1) not in done and mask[(x, v1 + 1)] == key
                  for x in range(u, u1 + 1)):
            v1 += 1
        for x in range(u, u1 + 1):
            for y in range(v, v1 + 1):
                done.add((x, y))
        out.append((u, v, u1 + 1, v1 + 1, key))
    return out


def add_cells(M, cells, to_m, mat_of, hide=None):
    """The outside faces of cells {(i, j, k): key}, merged into rectangles per key.

    to_m(i, j, k) gives metres of a grid corner; hide is a set of other cells that cover faces too."""
    hide = hide or ()
    for a in range(3):
        u_ax, v_ax = [k for k in range(3) if k != a]
        for sgn in (-1, 1):
            planes = {}
            for c, key in cells.items():
                nb = list(c)
                nb[a] += sgn
                nb = tuple(nb)
                if nb in cells or nb in hide:
                    continue
                plane = c[a] + (1 if sgn > 0 else 0)
                planes.setdefault(plane, {})[(c[u_ax], c[v_ax])] = key
            normal = tuple(sgn * x for x in AXIS[a])
            for plane, mask in planes.items():
                for u0, v0, u1, v1, key in _rects(mask):
                    idx = []
                    for uu, vv in ((u0, v0), (u1, v0), (u1, v1), (u0, v1)):
                        g = [0, 0, 0]
                        g[a], g[u_ax], g[v_ax] = plane, uu, vv
                        idx.append(M.v(to_m(*g)))
                    M.oquad(mat_of(key), idx[0], idx[1], idx[2], idx[3], normal)


def add_part(M, mat, lo, hi, shape="box", axis=None, inset=0.004):
    """A part as a box (or a cylinder along axis 0/1/2) filling lo..hi (metres)."""
    lo = [lo[k] + inset for k in range(3)]
    hi = [hi[k] - inset for k in range(3)]
    c = tuple((lo[k] + hi[k]) / 2 for k in range(3))
    h = tuple(max((hi[k] - lo[k]) / 2, 0.005) for k in range(3))
    if shape == "cylinder" and axis is not None:
        a = axis
        r = min(h[k] for k in range(3) if k != a)
        p0 = list(c)
        p1 = list(c)
        p0[a] -= h[a]
        p1[a] += h[a]
        M.tube(mat, tuple(p0), tuple(p1), r, 16)
    elif shape == "sphere":
        r = min(h)
        # a rounded block reads fine at vehicle scale
        M.box(mat, c, (r * 0.9, r * 0.9, r * 0.9))
    else:
        M.box(mat, c, h)


def new_mesh(name):
    return Mesh(name)
