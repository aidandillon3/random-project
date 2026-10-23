// A node-and-beam structure that follows the shape of a triangle mesh, for turning
// a 3D model into a BeamNG part. No three.js here, so node can test it.
//
// 1. The mesh is simplified on a grid: every grid cell that the surface passes
//    through gets one node, placed where it best fits the surface planes around it
//    (so corners and edges stay sharp), and the simplified triangles become the
//    collision triangles, facing outward like the model's own faces.
// 2. Beams: every edge of those triangles, a diagonal across every fold (so the
//    surface can't hinge), and links to nearby nodes so solid shapes are braced
//    through. Flat panels and thin bars get rigidifier nodes, the way the BeamNG
//    tutorial builds hoods. Separate pieces are tied together.
// 3. Weights follow the surface area and are raised where a node needs more mass
//    to stay stable with its beams at the game's 2000 Hz physics step.

export const DT = 1 / 2000;

export const STIFFNESS = {
  soft: { spring: 400000, damp: 40, deform: 15000, strength: 'FLT_MAX', label: 'Soft (plastic trim, light panels)' },
  panel: { spring: 1500000, damp: 100, deform: 40000, strength: 'FLT_MAX', label: 'Body panel (most parts)' },
  stiff: { spring: 6000000, damp: 200, deform: 150000, strength: 'FLT_MAX', label: 'Stiff (bumpers, racks, steel boxes)' },
};

// detail level -> node spacing as a share of the model's largest size
export const DETAIL = { coarse: 1 / 4, medium: 1 / 7, fine: 1 / 11 };

export function spacingFor(size, detail = 'medium') {
  const maxDim = Math.max(size[0], size[1], size[2]);
  return Math.min(1.0, Math.max(0.035, maxDim * (DETAIL[detail] || DETAIL.medium)));
}

const sub = (a, b) => [a[0] - b[0], a[1] - b[1], a[2] - b[2]];
const cross = (a, b) => [a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0]];
const dot = (a, b) => a[0] * b[0] + a[1] * b[1] + a[2] * b[2];
const len = a => Math.sqrt(dot(a, a));
const d2 = (a, b) => (a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2 + (a[2] - b[2]) ** 2;

function bbox(pos) {
  const lo = [Infinity, Infinity, Infinity], hi = [-Infinity, -Infinity, -Infinity];
  for (let i = 0; i < pos.length; i += 3) for (let k = 0; k < 3; k++) {
    lo[k] = Math.min(lo[k], pos[i + k]); hi[k] = Math.max(hi[k], pos[i + k]);
  }
  return { lo, hi, size: [hi[0] - lo[0], hi[1] - lo[1], hi[2] - lo[2]] };
}

// a simple spatial hash for points
class Grid {
  constructor(cell) { this.cell = cell; this.map = new Map(); }
  key(p) { return Math.floor(p[0] / this.cell) + ',' + Math.floor(p[1] / this.cell) + ',' + Math.floor(p[2] / this.cell); }
  add(p, v) { const k = this.key(p); (this.map.get(k) || this.map.set(k, []).get(k)).push(v); }
  near(p, r) {
    const out = [];
    const n = Math.ceil(r / this.cell);
    const c = [Math.floor(p[0] / this.cell), Math.floor(p[1] / this.cell), Math.floor(p[2] / this.cell)];
    for (let i = -n; i <= n; i++) for (let j = -n; j <= n; j++) for (let k = -n; k <= n; k++) {
      const l = this.map.get((c[0] + i) + ',' + (c[1] + j) + ',' + (c[2] + k));
      if (l) out.push(...l);
    }
    return out;
  }
}

// is the mesh a mirror image of itself across x = 0 (left/right)?
export function isSymmetric(pos, tol) {
  const b = bbox(pos);
  if (Math.abs(b.lo[0] + b.hi[0]) > tol * 2) return false;
  const g = new Grid(tol * 2);
  const n = pos.length / 3;
  for (let i = 0; i < n; i++) g.add([pos[i * 3], pos[i * 3 + 1], pos[i * 3 + 2]], i);
  const step = Math.max(1, Math.floor(n / 600));
  let hit = 0, tried = 0;
  for (let i = 0; i < n; i += step) {
    const m = [-pos[i * 3], pos[i * 3 + 1], pos[i * 3 + 2]];
    tried++;
    if (g.near(m, tol).some(j => d2(m, [pos[j * 3], pos[j * 3 + 1], pos[j * 3 + 2]]) <= tol * tol)) hit++;
  }
  return tried > 0 && hit / tried >= 0.9;
}

// solve the 3x3 system A x = b (null when singular)
function solve3(A, b) {
  const [a, bb, c, d, e, f, g, h, i] = A;
  const det = a * (e * i - f * h) - bb * (d * i - f * g) + c * (d * h - e * g);
  if (!isFinite(det) || Math.abs(det) < 1e-18) return null;
  return [
    (b[0] * (e * i - f * h) - bb * (b[1] * i - f * b[2]) + c * (b[1] * h - e * b[2])) / det,
    (a * (b[1] * i - f * b[2]) - b[0] * (d * i - f * g) + c * (d * b[2] - b[1] * g)) / det,
    (a * (e * b[2] - b[1] * h) - bb * (d * b[2] - b[1] * g) + b[0] * (d * h - e * g)) / det,
  ];
}

// ---- 1. simplify the surface onto a grid ------------------------------------------------------------
function simplify(pos, idx, s, sym) {
  const b = bbox(pos);
  const OFF = 1 << 11, M = 1 << 12;
  const cellOf = (x, y, z) => {
    const ix = sym ? Math.round(x / s) : Math.floor((x - b.lo[0]) / s);
    const iy = Math.floor((y - b.lo[1]) / s), iz = Math.floor((z - b.lo[2]) / s);
    return ((ix + OFF) * M + (iy + OFF)) * M + (iz + OFF);
  };
  const ixOf = key => Math.floor(key / (M * M)) - OFF;
  const nv = pos.length / 3;
  const vcell = new Float64Array(nv);
  const cells = new Map();   // key -> { q: Float64Array(10), sum: [x,y,z], n }
  for (let v = 0; v < nv; v++) {
    const x = pos[v * 3], y = pos[v * 3 + 1], z = pos[v * 3 + 2];
    const k = cellOf(x, y, z);
    vcell[v] = k;
    let c = cells.get(k);
    if (!c) cells.set(k, c = { q: new Float64Array(10), sum: [0, 0, 0], n: 0 });
    c.sum[0] += x; c.sum[1] += y; c.sum[2] += z; c.n++;
  }
  const tris = new Map();   // sorted cell triple -> { w: [a,b,c] cell keys in model winding, n: summed normal }
  for (let t = 0; t + 2 < idx.length; t += 3) {
    const ia = idx[t], ib = idx[t + 1], ic = idx[t + 2];
    const A = [pos[ia * 3], pos[ia * 3 + 1], pos[ia * 3 + 2]];
    const B = [pos[ib * 3], pos[ib * 3 + 1], pos[ib * 3 + 2]];
    const C = [pos[ic * 3], pos[ic * 3 + 1], pos[ic * 3 + 2]];
    const n = cross(sub(B, A), sub(C, A));
    const ln = len(n);
    if (ln < 1e-14) continue;
    const u = [n[0] / ln, n[1] / ln, n[2] / ln], dd = -dot(u, A), area = ln / 2;
    const p = [u[0], u[1], u[2], dd];
    const qadd = q => {
      let k = 0;
      for (let r = 0; r < 4; r++) for (let c2 = r; c2 < 4; c2++) q[k++] += area * p[r] * p[c2];
    };
    const ka = vcell[ia], kb = vcell[ib], kc = vcell[ic];
    qadd(cells.get(ka).q);
    if (kb !== ka) qadd(cells.get(kb).q);
    if (kc !== ka && kc !== kb) qadd(cells.get(kc).q);
    if (ka === kb || kb === kc || ka === kc) continue;
    const key = [ka, kb, kc].sort((x, y) => x - y).join(',');
    let e = tris.get(key);
    if (!e) tris.set(key, e = { w: [ka, kb, kc], n: [0, 0, 0] });
    e.n[0] += n[0]; e.n[1] += n[1]; e.n[2] += n[2];
  }
  // node per cell: the point closest to its planes, kept near the cell's vertices
  const keys = [...cells.keys()];
  const index = new Map(keys.map((k, i) => [k, i]));
  const pts = keys.map(k => {
    const c = cells.get(k);
    const mean = [c.sum[0] / c.n, c.sum[1] / c.n, c.sum[2] / c.n];
    const q = c.q;   // a11 a12 a13 a14 a22 a23 a24 a33 a34 a44
    const A = [q[0], q[1], q[2], q[1], q[4], q[5], q[2], q[5], q[7]];
    const lam = 1e-3 * (q[0] + q[4] + q[7]) / 3 + 1e-12;
    const Am = [A[0] * mean[0] + A[1] * mean[1] + A[2] * mean[2] + q[3],
      A[3] * mean[0] + A[4] * mean[1] + A[5] * mean[2] + q[6],
      A[6] * mean[0] + A[7] * mean[1] + A[8] * mean[2] + q[8]];
    const d = solve3([A[0] + lam, A[1], A[2], A[3], A[4] + lam, A[5], A[6], A[7], A[8] + lam], [-Am[0], -Am[1], -Am[2]]);
    let p = mean;
    if (d && len(d) < s * 0.9) p = [mean[0] + d[0], mean[1] + d[1], mean[2] + d[2]];
    return p;
  });
  const ix = keys.map(ixOf);
  if (sym) {
    // exact mirror pairs, and centre cells on x = 0
    const byKey = new Map(keys.map((k, i) => [k, i]));
    // (off-centre nodes stay at least a quarter cell off the centre line)
    const minX = 0.25 * s;
    keys.forEach((k, i) => {
      if (ix[i] === 0) { pts[i][0] = 0; return; }
      if (ix[i] < 0) {
        if (byKey.get(k - 2 * ix[i] * M * M) === undefined) pts[i][0] = Math.min(pts[i][0], -minX);
        return;
      }
      const j = byKey.get(k - 2 * ix[i] * M * M);
      if (j === undefined) { pts[i][0] = Math.max(pts[i][0], minX); return; }
      const x = Math.max((pts[i][0] - pts[j][0]) / 2, minX), y = (pts[i][1] + pts[j][1]) / 2, z = (pts[i][2] + pts[j][2]) / 2;
      pts[i] = [x, y, z]; pts[j] = [-x, y, z];
    });
  }
  // the simplified triangles, facing the way most of the model's faces do
  const centre = [(b.lo[0] + b.hi[0]) / 2, (b.lo[1] + b.hi[1]) / 2, (b.lo[2] + b.hi[2]) / 2];
  const out = [];
  for (const e of tris.values()) {
    let [a, bb, c] = e.w.map(k => index.get(k));
    const n = cross(sub(pts[bb], pts[a]), sub(pts[c], pts[a]));
    let side = dot(n, e.n);
    if (Math.abs(side) < 1e-12 * (len(n) * len(e.n) + 1e-30)) {
      const m = [(pts[a][0] + pts[bb][0] + pts[c][0]) / 3, (pts[a][1] + pts[bb][1] + pts[c][1]) / 3, (pts[a][2] + pts[bb][2] + pts[c][2]) / 3];
      side = dot(n, sub(m, centre));
    }
    if (side < 0) [bb, c] = [c, bb];
    out.push([a, bb, c]);
  }
  return { pts, tris: out, xi: ix, bounds: b };
}

// ---- merging nodes that ended up almost on top of each other -------------------------------------------
function mergeClose(fit, r) {
  const { pts } = fit;
  const parent = pts.map((_, i) => i);
  const find = i => { while (parent[i] !== i) i = parent[i] = parent[parent[i]]; return i; };
  const g = new Grid(r);
  pts.forEach((p, i) => g.add(p, i));
  pts.forEach((p, i) => {
    for (const j of g.near(p, r)) {
      if (j <= i || d2(p, pts[j]) > r * r) continue;
      // keep left, right and centre-line nodes apart so the part stays a mirror image
      if (fit.sym && Math.sign(p[0]) !== Math.sign(pts[j][0])) continue;
      const a = find(i), b = find(j);
      if (a !== b) parent[b] = a;
    }
  });
  const groups = new Map();
  pts.forEach((p, i) => { const k = find(i); (groups.get(k) || groups.set(k, []).get(k)).push(i); });
  const remap = new Array(pts.length);
  const npts = [];
  for (const members of groups.values()) {
    const c = [0, 0, 0];
    for (const i of members) for (let k = 0; k < 3; k++) c[k] += pts[i][k] / members.length;
    for (const i of members) remap[i] = npts.length;
    npts.push(c);
  }
  const seen = new Set();
  const tris = [];
  for (const t of fit.tris) {
    const m = t.map(i => remap[i]);
    if (m[0] === m[1] || m[1] === m[2] || m[0] === m[2]) continue;
    const k = m.slice().sort((a, b) => a - b).join(',');
    if (seen.has(k)) continue;
    seen.add(k);
    tris.push(m);
  }
  return { ...fit, pts: npts, tris };
}

// ---- principal axes (for spotting flat panels and thin bars) -------------------------------------------------
function principal(pts) {
  const n = pts.length;
  const c = [0, 0, 0];
  for (const p of pts) for (let k = 0; k < 3; k++) c[k] += p[k] / n;
  const C = [0, 0, 0, 0, 0, 0, 0, 0, 0];
  for (const p of pts) {
    const d = sub(p, c);
    for (let i = 0; i < 3; i++) for (let j = 0; j < 3; j++) C[i * 3 + j] += d[i] * d[j] / n;
  }
  // power iteration with deflation: three axes, largest spread first
  const axes = [];
  let M = C.slice();
  for (let a = 0; a < 3; a++) {
    let v = a === 0 ? [1, 0.3, 0.2] : a === 1 ? [0.2, 1, 0.3] : [0.3, 0.2, 1];
    for (const u of axes) { const k = dot(v, u.v); v = sub(v, [u.v[0] * k, u.v[1] * k, u.v[2] * k]); }
    for (let it = 0; it < 60; it++) {
      let w = [M[0] * v[0] + M[1] * v[1] + M[2] * v[2], M[3] * v[0] + M[4] * v[1] + M[5] * v[2], M[6] * v[0] + M[7] * v[1] + M[8] * v[2]];
      for (const u of axes) { const k = dot(w, u.v); w = sub(w, [u.v[0] * k, u.v[1] * k, u.v[2] * k]); }
      const l = len(w);
      if (l < 1e-15) break;
      v = [w[0] / l, w[1] / l, w[2] / l];
    }
    const l = len(v) || 1;
    v = [v[0] / l, v[1] / l, v[2] / l];
    const lam = dot(v, [M[0] * v[0] + M[1] * v[1] + M[2] * v[2], M[3] * v[0] + M[4] * v[1] + M[5] * v[2], M[6] * v[0] + M[7] * v[1] + M[8] * v[2]]);
    axes.push({ v, spread: Math.sqrt(Math.max(0, lam)) });
    M = M.map((x, k) => x - lam * v[Math.floor(k / 3)] * v[k % 3]);
  }
  return { centre: c, axes };
}

// ---- 2. beams, rigidifiers, names --------------------------------------------------------------------------
export function smartFit(positions, indices, {
  spacing = null, detail = 'medium', prefix = 'sf', symmetric = 'auto', maxNodes = 360, inside = null,
} = {}) {
  const pos = positions instanceof Float32Array || Array.isArray(positions) ? positions : Float32Array.from(positions);
  const b = bbox(pos);
  let s = spacing || spacingFor(b.size, detail);
  const tol = Math.max(0.004, s * 0.12);
  const sym = symmetric === 'auto' ? isSymmetric(pos, tol) : !!symmetric;
  let fit = null;
  for (let attempt = 0; attempt < 8; attempt++) {
    fit = mergeClose({ ...simplify(pos, indices, s, sym), sym }, s * 0.3);
    if (fit.pts.length <= maxNodes) break;
    s *= 1.25;
  }
  fit.spacing = s;
  return brace(fit, { prefix, inside });
}

function brace(fit, { prefix, inside }) {
  const { pts, tris } = fit;
  const s = fit.spacing;
  const n = pts.length;
  const beams = new Map();   // "i,j" -> kind ('surface' | 'fold' | 'brace' | 'rigid' | 'join')
  const key = (a, b) => (a < b ? a + ',' + b : b + ',' + a);
  const add = (a, b, kind) => { if (a !== b && !beams.has(key(a, b))) beams.set(key(a, b), kind); };
  // surface edges and fold diagonals
  const edgeTris = new Map();
  for (const t of tris) {
    for (let e = 0; e < 3; e++) {
      const a = t[e], c = t[(e + 1) % 3], o = t[(e + 2) % 3];
      add(a, c, 'surface');
      const k = key(a, c);
      (edgeTris.get(k) || edgeTris.set(k, []).get(k)).push(o);
    }
  }
  for (const others of edgeTris.values()) {
    if (others.length === 2 && d2(pts[others[0]], pts[others[1]]) < (3.2 * s) ** 2) add(others[0], others[1], 'fold');
  }
  // bracing to nearby nodes (through solid shapes)
  const g = new Grid(s);
  pts.forEach((p, i) => g.add(p, i));
  // (nodes already well tied by the surface get fewer: every beam adds to the mass a node needs)
  const R = 1.6 * s;
  const have = new Array(n).fill(0);
  for (const k of beams.keys()) { const [a, c] = k.split(',').map(Number); have[a]++; have[c]++; }
  for (let i = 0; i < n; i++) {
    const near = g.near(pts[i], R).filter(j => j !== i && d2(pts[i], pts[j]) <= R * R)
      .sort((a, c) => d2(pts[i], pts[a]) - d2(pts[i], pts[c]));
    let added = 0;
    const want = Math.max(1, Math.min(4, 11 - have[i]));
    for (const j of near) {
      if (added >= want) break;
      if (!beams.has(key(i, j))) { add(i, j, 'brace'); added++; have[i]++; have[j]++; }
    }
  }
  // rigidifiers for flat panels and thin bars
  const all = pts.map(p => p.slice());
  const extra = [];
  const pc = n >= 3 ? principal(pts) : null;
  if (pc && n >= 4) {
    const [e1, e2, e3] = pc.axes.map(a => a.spread);
    const want = [];
    if (e3 < 0.15 * e1) want.push(pc.axes[2].v);          // flat: off the panel
    if (e2 < 0.15 * e1) want.push(pc.axes[1].v);          // a bar: also off to the side
    for (const v0 of want) {
      let v = v0;
      if (inside) { if (dot(v, sub(inside, pc.centre)) < 0) v = [-v[0], -v[1], -v[2]]; }
      else {
        const avg = [0, 0, 0];
        for (const t of tris) { const nn = cross(sub(pts[t[1]], pts[t[0]]), sub(pts[t[2]], pts[t[0]])); for (let k = 0; k < 3; k++) avg[k] += nn[k]; }
        if (dot(avg, v) > 0) v = [-v[0], -v[1], -v[2]];   // away from the outside faces
      }
      if (fit.sym && Math.abs(v[0]) > 0.7) continue;      // a mirrored part must keep its rigidifier on the centre line
      const off = Math.max(0.15, Math.min(0.6, e1 * 1.2));
      const p = [pc.centre[0] + v[0] * off, pc.centre[1] + v[1] * off, pc.centre[2] + v[2] * off];
      if (fit.sym) p[0] = 0;
      const id = all.length;
      all.push(p);
      extra.push(id);
      for (let i = 0; i < n; i++) add(i, id, 'rigid');
    }
  }
  // tie separate pieces together
  const parent = all.map((_, i) => i);
  const find = i => { while (parent[i] !== i) i = parent[i] = parent[parent[i]]; return i; };
  const unite = (a, c) => { parent[find(a)] = find(c); };
  for (const k of beams.keys()) { const [a, c] = k.split(',').map(Number); unite(a, c); }
  for (let guard = 0; guard < all.length; guard++) {
    const comps = new Map();
    all.forEach((_, i) => { const r = find(i); (comps.get(r) || comps.set(r, []).get(r)).push(i); });
    if (comps.size <= 1) break;
    const list = [...comps.values()].sort((a, c) => c.length - a.length);
    const main = list[0], other = list[1];
    const pairs = [];
    for (const i of other) for (const j of main) pairs.push([d2(all[i], all[j]), i, j]);
    pairs.sort((a, c) => a[0] - c[0]);
    const used = new Set();
    let made = 0;
    for (const [, i, j] of pairs) {
      if (made >= 3 || used.has(i)) continue;
      used.add(i);
      add(i, j, 'join');
      unite(i, j);
      made++;
    }
  }
  // every node held by at least three beams
  const deg = new Array(all.length).fill(0);
  for (const k of beams.keys()) { const [a, c] = k.split(',').map(Number); deg[a]++; deg[c]++; }
  for (let i = 0; i < all.length; i++) {
    if (deg[i] >= 3) continue;
    const cand = all.map((p, j) => [d2(all[i], p), j]).filter(([, j]) => j !== i && !beams.has(key(i, j))).sort((a, c) => a[0] - c[0]);
    for (const [, j] of cand) { if (deg[i] >= 3) break; add(i, j, 'brace'); deg[i]++; deg[j]++; }
  }
  // names: front to back, bottom to top; mirrored pairs share a number with l (left, +x) / r
  const order = [...Array(n).keys()].sort((a, c) => (all[a][1] - all[c][1]) || (all[a][2] - all[c][2]) || (Math.abs(all[a][0]) - Math.abs(all[c][0])));
  const ids = new Array(all.length);
  const posKey = p => [Math.abs(p[0]).toFixed(4), p[1].toFixed(4), p[2].toFixed(4)].join(',');
  const pairOf = new Map();
  if (fit.sym) {
    const byPos = new Map();
    for (let i = 0; i < n; i++) if (Math.abs(all[i][0]) > 1e-9) {
      const k = posKey(all[i]);
      (byPos.get(k) || byPos.set(k, []).get(k)).push(i);
    }
    for (const l of byPos.values()) if (l.length === 2 && Math.sign(all[l[0]][0]) !== Math.sign(all[l[1]][0])) { pairOf.set(l[0], l[1]); pairOf.set(l[1], l[0]); }
  }
  let num = 1;
  for (const i of order) {
    if (ids[i]) continue;
    const j = pairOf.get(i);
    if (j !== undefined) {
      ids[i] = prefix + num + (all[i][0] > 0 ? 'l' : 'r');
      ids[j] = prefix + num + (all[j][0] > 0 ? 'l' : 'r');
    } else if (fit.sym && Math.abs(all[i][0]) < 1e-9) ids[i] = prefix + num;
    else ids[i] = prefix + num + (fit.sym ? (all[i][0] > 0 ? 'l' : 'r') : '');
    num++;
  }
  extra.forEach((id, k) => { ids[id] = prefix + 'rig' + (k + 1); });
  // results in the shape the importer writes out
  const r4 = v => +v.toFixed(4);
  const nodes = all.map((p, i) => [ids[i], r4(p[0]), r4(p[1]), r4(p[2])]);
  const beamList = { main: [], rigid: [] };
  for (const [k, kind] of beams) {
    const [a, c] = k.split(',').map(Number);
    (kind === 'rigid' ? beamList.rigid : beamList.main).push([ids[a], ids[c]]);
  }
  const triList = tris.map(t => t.map(i => ids[i]));
  // surface area each node stands for (for the weights)
  const area = new Array(all.length).fill(0);
  for (const t of tris) {
    const a = len(cross(sub(all[t[1]], all[t[0]]), sub(all[t[2]], all[t[0]]))) / 2;
    for (const i of t) area[i] += a / 3;
  }
  return {
    nodes, beams: beamList, tris: triList, rigidifiers: extra.map(i => ids[i]),
    pos: new Map(nodes.map(r => [r[0], [r[1], r[2], r[3]]])),
    area: new Map(all.map((_, i) => [ids[i], area[i]])),
    spacing: s, symmetric: fit.sym,
  };
}

// node weights: the mass shared by surface area, then raised where the beams need it
export function assignWeights(fit, mass, { spring, damp, rigidFactor = 0.5, attach = [], attachSpring = spring, minWeight = 0.2 } = {}) {
  const K = new Map(), C = new Map();
  for (const id of fit.pos.keys()) { K.set(id, 0); C.set(id, 0); }
  const addK = (id, k, c) => { if (K.has(id)) { K.set(id, K.get(id) + k); C.set(id, C.get(id) + c); } };
  for (const [a, b] of fit.beams.main) { addK(a, spring, damp); addK(b, spring, damp); }
  for (const [a, b] of fit.beams.rigid) { addK(a, spring * rigidFactor, damp * rigidFactor); addK(b, spring * rigidFactor, damp * rigidFactor); }
  for (const [a, b] of attach) { addK(a, attachSpring, damp); addK(b, attachSpring, damp); }
  const rig = new Set(fit.rigidifiers);
  let totalArea = 0;
  for (const [id, a] of fit.area) if (!rig.has(id)) totalArea += a;
  const bare = [...fit.pos.keys()].filter(id => !rig.has(id));
  const w = new Map();
  for (const id of fit.pos.keys()) {
    let share = rig.has(id) ? mass * 0.03 : totalArea > 0 ? mass * 0.97 * (0.15 / bare.length + 0.85 * fit.area.get(id) / totalArea) : mass / fit.pos.size;
    const need = Math.max(K.get(id) * DT * DT / 4, C.get(id) * DT / 2.4) * 1.05;
    w.set(id, +Math.max(share, need, minWeight).toFixed(2));
  }
  let total = 0, needed = 0;
  for (const [id, v] of w) { total += v; needed += Math.max(minWeight, Math.max(K.get(id) * DT * DT / 4, C.get(id) * DT / 2.4) * 1.05); }
  return { weights: w, total, needed, K, C };
}
