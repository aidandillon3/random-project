// A small polygon mesh for the 3D Workshop: shapes, extrude, inset, subdivide,
// delete, flip, weld, mirror and triangulation. A mesh is { v: [[x,y,z]...],
// f: [[i0, i1, i2, ...]...] } with faces wound counter-clockwise seen from
// outside, in vehicle coordinates (x left, y back, z up). No three.js here.

export const clone = m => ({ v: m.v.map(p => p.slice()), f: m.f.map(f => f.slice()) });

const add = (a, b) => [a[0] + b[0], a[1] + b[1], a[2] + b[2]];
const sub = (a, b) => [a[0] - b[0], a[1] - b[1], a[2] - b[2]];
const mul = (a, s) => [a[0] * s, a[1] * s, a[2] * s];
const dot = (a, b) => a[0] * b[0] + a[1] * b[1] + a[2] * b[2];
const cross = (a, b) => [a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0]];
const len = a => Math.sqrt(dot(a, a));
const norm = a => { const l = len(a) || 1; return [a[0] / l, a[1] / l, a[2] / l]; };

// ---- shapes ----------------------------------------------------------------------------------------
// axis: the direction a round shape's length runs along ('x', 'y' or 'z')
function axisMap(axis) {
  // local (u, v, w) with w along the axis -> vehicle (x, y, z), right-handed
  if (axis === 'x') return p => [p[2], p[0], p[1]];
  if (axis === 'y') return p => [p[1], p[2], p[0]];
  return p => p;
}
function place(m, center = [0, 0, 0]) { for (const p of m.v) { p[0] += center[0]; p[1] += center[1]; p[2] += center[2]; } return m; }

export function box({ w = 1, l = 1, h = 1, seg = [1, 1, 1], center } = {}) {
  const [nx, ny, nz] = seg.map(s => Math.max(1, Math.round(s)));
  const v = [], f = [], index = new Map();
  const vid = (i, j, k) => {
    const key = i + ',' + j + ',' + k;
    if (!index.has(key)) { index.set(key, v.length); v.push([-w / 2 + w * i / nx, -l / 2 + l * j / ny, -h / 2 + h * k / nz]); }
    return index.get(key);
  };
  // each face grid, wound to face outward
  for (let j = 0; j < ny; j++) for (let i = 0; i < nx; i++) {
    f.push([vid(i, j, 0), vid(i, j + 1, 0), vid(i + 1, j + 1, 0), vid(i + 1, j, 0)]);       // bottom
    f.push([vid(i, j, nz), vid(i + 1, j, nz), vid(i + 1, j + 1, nz), vid(i, j + 1, nz)]);   // top
  }
  for (let k = 0; k < nz; k++) for (let i = 0; i < nx; i++) {
    f.push([vid(i, 0, k), vid(i + 1, 0, k), vid(i + 1, 0, k + 1), vid(i, 0, k + 1)]);       // front (-y)
    f.push([vid(i, ny, k), vid(i, ny, k + 1), vid(i + 1, ny, k + 1), vid(i + 1, ny, k)]);   // back (+y)
  }
  for (let k = 0; k < nz; k++) for (let j = 0; j < ny; j++) {
    f.push([vid(0, j, k), vid(0, j, k + 1), vid(0, j + 1, k + 1), vid(0, j + 1, k)]);       // right (-x)
    f.push([vid(nx, j, k), vid(nx, j + 1, k), vid(nx, j + 1, k + 1), vid(nx, j, k + 1)]);   // left (+x)
  }
  return place({ v, f }, center);
}

export function cylinder({ r = 0.5, h = 1, sides = 24, axis = 'z', caps = true, center } = {}) {
  return cone({ r1: r, r2: r, h, sides, axis, caps, center });
}

export function cone({ r1 = 0.5, r2 = 0, h = 1, sides = 24, axis = 'z', caps = true, center } = {}) {
  const map = axisMap(axis);
  const v = [], f = [];
  const n = Math.max(3, Math.round(sides));
  const ring = (r, z) => { const base = v.length; for (let i = 0; i < n; i++) { const a = 2 * Math.PI * i / n; v.push(map([r * Math.cos(a), r * Math.sin(a), z])); } return base; };
  const b0 = ring(r1, -h / 2);
  if (r2 > 1e-9) {
    const b1 = ring(r2, h / 2);
    for (let i = 0; i < n; i++) f.push([b0 + i, b0 + (i + 1) % n, b1 + (i + 1) % n, b1 + i]);
    if (caps) f.push([...Array(n).keys()].map(i => b1 + i));
  } else {
    const tip = v.length;
    v.push(map([0, 0, h / 2]));
    for (let i = 0; i < n; i++) f.push([b0 + i, b0 + (i + 1) % n, tip]);
  }
  if (caps) f.push([...Array(n).keys()].map(i => b0 + n - 1 - i));
  return place({ v, f }, center);
}

export function sphere({ r = 0.5, seg = 24, rings = 12, center } = {}) {
  const v = [], f = [];
  const n = Math.max(3, Math.round(seg)), m = Math.max(2, Math.round(rings));
  v.push([0, 0, -r]);
  for (let j = 1; j < m; j++) {
    const t = Math.PI * j / m;
    for (let i = 0; i < n; i++) { const a = 2 * Math.PI * i / n; v.push([r * Math.sin(t) * Math.cos(a), r * Math.sin(t) * Math.sin(a), -r * Math.cos(t)]); }
  }
  v.push([0, 0, r]);
  const at = (j, i) => 1 + (j - 1) * n + ((i % n) + n) % n;
  for (let i = 0; i < n; i++) f.push([0, at(1, i + 1), at(1, i)]);
  for (let j = 1; j < m - 1; j++) for (let i = 0; i < n; i++) f.push([at(j, i), at(j, i + 1), at(j + 1, i + 1), at(j + 1, i)]);
  const top = v.length - 1;
  for (let i = 0; i < n; i++) f.push([at(m - 1, i), at(m - 1, i + 1), top]);
  return place({ v, f }, center);
}

// a ramp: full height at the back (+y), sloping down to the front edge
export function wedge({ w = 1, l = 1, h = 0.5, center } = {}) {
  const x0 = -w / 2, x1 = w / 2, y0 = -l / 2, y1 = l / 2, z0 = -h / 2, z1 = h / 2;
  const v = [[x0, y0, z0], [x1, y0, z0], [x1, y1, z0], [x0, y1, z0], [x0, y1, z1], [x1, y1, z1]];
  const f = [[0, 3, 2, 1], [0, 1, 5, 4], [2, 3, 4, 5], [0, 4, 3], [1, 2, 5]];
  return place({ v, f }, center);
}

export function tube({ r = 0.5, t = 0.05, h = 1, sides = 24, axis = 'z', center } = {}) {
  const map = axisMap(axis);
  const n = Math.max(3, Math.round(sides)), ri = Math.max(0.001, r - t);
  const v = [], f = [];
  for (const [rr, z] of [[r, -h / 2], [r, h / 2], [ri, -h / 2], [ri, h / 2]]) {
    for (let i = 0; i < n; i++) { const a = 2 * Math.PI * i / n; v.push(map([rr * Math.cos(a), rr * Math.sin(a), z])); }
  }
  const O0 = 0, O1 = n, I0 = 2 * n, I1 = 3 * n;
  for (let i = 0; i < n; i++) {
    const j = (i + 1) % n;
    f.push([O0 + i, O0 + j, O1 + j, O1 + i]);      // outside
    f.push([I0 + j, I0 + i, I1 + i, I1 + j]);      // inside, facing in
    f.push([O1 + i, O1 + j, I1 + j, I1 + i]);      // top ring
    f.push([O0 + j, O0 + i, I0 + i, I0 + j]);      // bottom ring
  }
  return place({ v, f }, center);
}

export function plane({ w = 1, l = 1, seg = [1, 1], center } = {}) {
  const [nx, ny] = seg.map(s => Math.max(1, Math.round(s)));
  const v = [], f = [];
  for (let j = 0; j <= ny; j++) for (let i = 0; i <= nx; i++) v.push([-w / 2 + w * i / nx, -l / 2 + l * j / ny, 0]);
  for (let j = 0; j < ny; j++) for (let i = 0; i < nx; i++) {
    const a = j * (nx + 1) + i;
    f.push([a, a + 1, a + nx + 2, a + nx + 1]);
  }
  return place({ v, f }, center);
}

export function torus({ R = 0.5, r = 0.12, seg = 24, sides = 12, axis = 'z', center } = {}) {
  const map = axisMap(axis);
  const n = Math.max(3, Math.round(seg)), m = Math.max(3, Math.round(sides));
  const v = [], f = [];
  for (let i = 0; i < n; i++) {
    const a = 2 * Math.PI * i / n;
    for (let j = 0; j < m; j++) {
      const b = 2 * Math.PI * j / m;
      const rr = R + r * Math.cos(b);
      v.push(map([rr * Math.cos(a), rr * Math.sin(a), r * Math.sin(b)]));
    }
  }
  for (let i = 0; i < n; i++) for (let j = 0; j < m; j++) {
    const a = i * m + j, b = ((i + 1) % n) * m + j, c = ((i + 1) % n) * m + (j + 1) % m, d = i * m + (j + 1) % m;
    f.push([a, b, c, d]);
  }
  return place({ v, f }, center);
}

export const SHAPES = { box, cylinder, cone, sphere, wedge, tube, plane, torus };

// ---- measuring --------------------------------------------------------------------------------------------
export function faceNormal(m, face) {
  // Newell's method: works for any polygon
  const n = [0, 0, 0];
  for (let i = 0; i < face.length; i++) {
    const a = m.v[face[i]], b = m.v[face[(i + 1) % face.length]];
    n[0] += (a[1] - b[1]) * (a[2] + b[2]);
    n[1] += (a[2] - b[2]) * (a[0] + b[0]);
    n[2] += (a[0] - b[0]) * (a[1] + b[1]);
  }
  return n;   // length = 2 × area
}
export const faceArea = (m, face) => len(faceNormal(m, face)) / 2;
export function faceCenter(m, face) {
  const c = [0, 0, 0];
  for (const i of face) for (let k = 0; k < 3; k++) c[k] += m.v[i][k] / face.length;
  return c;
}
export function bounds(m) {
  const lo = [Infinity, Infinity, Infinity], hi = [-Infinity, -Infinity, -Infinity];
  for (const p of m.v) for (let k = 0; k < 3; k++) { lo[k] = Math.min(lo[k], p[k]); hi[k] = Math.max(hi[k], p[k]); }
  return { lo, hi, size: sub(hi, lo), center: mul(add(lo, hi), 0.5) };
}
export function edges(m) {
  const seen = new Set(), out = [];
  for (const f of m.f) for (let i = 0; i < f.length; i++) {
    const a = f[i], b = f[(i + 1) % f.length];
    const k = a < b ? a + ',' + b : b + ',' + a;
    if (!seen.has(k)) { seen.add(k); out.push([Math.min(a, b), Math.max(a, b)]); }
  }
  return out;
}
// vertices used by a selection
export function vertsOf(m, mode, sel) {
  const out = new Set();
  if (mode === 'vertex') for (const i of sel) out.add(i);
  else if (mode === 'edge') for (const k of sel) { const [a, b] = String(k).split(',').map(Number); out.add(a); out.add(b); }
  else if (mode === 'face') for (const fi of sel) for (const i of m.f[fi] || []) out.add(i);
  else m.v.forEach((_, i) => out.add(i));
  return out;
}

// ---- changes (return new meshes; the input is not touched) ---------------------------------------------------
export function transform(m, verts, fn) {
  const out = clone(m);
  for (const i of verts) out.v[i] = fn(out.v[i].slice());
  return out;
}

export function compact(m) {
  const used = new Map(), v = [];
  const f = [];
  for (const face of m.f) {
    const nf = [];
    for (const i of face) {
      if (!used.has(i)) { used.set(i, v.length); v.push(m.v[i].slice()); }
      const ni = used.get(i);
      if (nf[nf.length - 1] !== ni) nf.push(ni);
    }
    while (nf.length > 1 && nf[0] === nf[nf.length - 1]) nf.pop();
    if (new Set(nf).size >= 3) f.push(nf);
  }
  return { v, f };
}

export function deleteFaces(m, faces) {
  const drop = new Set(faces);
  return compact({ v: m.v, f: m.f.filter((_, i) => !drop.has(i)) });
}
export function deleteVerts(m, verts) {
  const drop = new Set(verts);
  return compact({ v: m.v, f: m.f.filter(f => !f.some(i => drop.has(i))) });
}
export function flip(m, faces = null) {
  const out = clone(m);
  const which = faces ? new Set(faces) : null;
  out.f = out.f.map((f, i) => (!which || which.has(i) ? f.slice().reverse() : f));
  return out;
}

// pull the faces out of the surface as a block (region extrude). Returns { mesh, faces: the moved faces }
export function extrude(m, faces, dist) {
  const out = clone(m);
  const S = new Set(faces);
  if (!S.size) return { mesh: out, faces: [] };
  const count = new Map();
  for (const fi of S) {
    const f = out.f[fi];
    for (let i = 0; i < f.length; i++) {
      const a = f[i], b = f[(i + 1) % f.length];
      const k = a < b ? a + ',' + b : b + ',' + a;
      count.set(k, (count.get(k) || 0) + 1);
    }
  }
  const n = [0, 0, 0];
  for (const fi of S) { const fn = faceNormal(out, out.f[fi]); n[0] += fn[0]; n[1] += fn[1]; n[2] += fn[2]; }
  const dir = norm(n);
  const dup = new Map();
  const nv = i => { if (!dup.has(i)) { dup.set(i, out.v.length); out.v.push(add(out.v[i], mul(dir, dist))); } return dup.get(i); };
  const sides = [];
  for (const fi of S) {
    const f = out.f[fi];
    for (let i = 0; i < f.length; i++) {
      const a = f[i], b = f[(i + 1) % f.length];
      const k = a < b ? a + ',' + b : b + ',' + a;
      if (count.get(k) === 1) sides.push([a, b, nv(b), nv(a)]);
    }
  }
  for (const fi of S) out.f[fi] = out.f[fi].map(nv);
  out.f.push(...sides);
  return { mesh: out, faces: [...S] };
}

// shrink each face into itself, leaving a ring of new faces around it
export function inset(m, faces, t = 0.2) {
  const out = clone(m);
  for (const fi of faces) {
    const f = out.f[fi];
    const c = faceCenter(out, f);
    const inner = f.map(i => { out.v.push(add(out.v[i], mul(sub(c, out.v[i]), t))); return out.v.length - 1; });
    for (let i = 0; i < f.length; i++) {
      const j = (i + 1) % f.length;
      out.f.push([f[i], f[j], inner[j], inner[i]]);
    }
    out.f[fi] = inner;
  }
  return { mesh: out, faces: [...faces] };
}

// split faces into smaller ones (quads into 4, triangles into 4, others into a fan of quads)
export function subdivide(m, faces = null) {
  const out = clone(m);
  const which = faces ? new Set(faces) : new Set(out.f.map((_, i) => i));
  const mid = new Map();
  const midOf = (a, b) => {
    const k = a < b ? a + ',' + b : b + ',' + a;
    if (!mid.has(k)) { mid.set(k, out.v.length); out.v.push(mul(add(out.v[a], out.v[b]), 0.5)); }
    return mid.get(k);
  };
  const result = [];
  for (let fi = 0; fi < out.f.length; fi++) {
    const f = out.f[fi];
    if (!which.has(fi)) continue;
    const ms = f.map((a, i) => midOf(a, f[(i + 1) % f.length]));
    if (f.length === 3) {
      result.push([f[0], ms[0], ms[2]], [ms[0], f[1], ms[1]], [ms[2], ms[1], f[2]], [ms[0], ms[1], ms[2]]);
    } else {
      out.v.push(faceCenter(out, f));
      const c = out.v.length - 1;
      for (let i = 0; i < f.length; i++) result.push([f[i], ms[i], c, ms[(i - 1 + f.length) % f.length]]);
    }
  }
  // faces next to the split ones get the new edge points too, so there are no cracks
  for (let fi = 0; fi < out.f.length; fi++) {
    if (which.has(fi)) continue;
    const f = out.f[fi];
    const nf = [];
    for (let i = 0; i < f.length; i++) {
      const a = f[i], b = f[(i + 1) % f.length];
      nf.push(a);
      const k = a < b ? a + ',' + b : b + ',' + a;
      if (mid.has(k)) nf.push(mid.get(k));
    }
    result.push(nf);
  }
  out.f = result;
  return out;
}

// merge points closer than tol
export function weld(m, tol = 1e-4) {
  const cell = Math.max(tol, 1e-9);
  const grid = new Map();
  const map = new Array(m.v.length);
  const v = [];
  m.v.forEach((p, i) => {
    const c = p.map(x => Math.floor(x / cell));
    let hit = -1;
    for (let a = -1; a <= 1 && hit < 0; a++) for (let b = -1; b <= 1 && hit < 0; b++) for (let d = -1; d <= 1 && hit < 0; d++) {
      for (const j of grid.get((c[0] + a) + ',' + (c[1] + b) + ',' + (c[2] + d)) || []) {
        if (len(sub(v[j], p)) <= tol) { hit = j; break; }
      }
    }
    if (hit < 0) {
      hit = v.length;
      v.push(p.slice());
      const k = c.join(',');
      (grid.get(k) || grid.set(k, []).get(k)).push(hit);
    }
    map[i] = hit;
  });
  return compact({ v, f: m.f.map(f => f.map(i => map[i])) });
}

// make the left side (+x) a mirror of the right, or add a mirrored copy: the result is symmetric about x = 0
export function mirrorX(m, { tol = 1e-4 } = {}) {
  // faces lying on the centre plane would end up inside: drop them
  const onPlane = f => f.every(i => Math.abs(m.v[i][0]) <= tol);
  const base = compact({ v: m.v.map(p => (Math.abs(p[0]) <= tol ? [0, p[1], p[2]] : p.slice())), f: m.f.filter(f => !onPlane(f)) });
  const n = base.v.length;
  const v = base.v.concat(base.v.map(p => [-p[0], p[1], p[2]]));
  const f = base.f.concat(base.f.map(face => face.map(i => i + n).reverse()));
  return weld({ v, f }, tol * 2);
}

export function join(a, b) {
  const n = a.v.length;
  return { v: a.v.map(p => p.slice()).concat(b.v.map(p => p.slice())), f: a.f.map(f => f.slice()).concat(b.f.map(f => f.map(i => i + n))) };
}

// ---- triangles ---------------------------------------------------------------------------------------------------
function earClip(m, face) {
  const n = norm(faceNormal(m, face));
  // a 2D frame on the face's plane
  const ax = Math.abs(n[0]) < 0.9 ? [1, 0, 0] : [0, 1, 0];
  const u = norm(cross(ax, n)), w = cross(n, u);
  const pts = face.map(i => [dot(m.v[i], u), dot(m.v[i], w)]);
  const idx = face.map((_, i) => i);
  const out = [];
  const area2 = (a, b, c) => (pts[b][0] - pts[a][0]) * (pts[c][1] - pts[a][1]) - (pts[c][0] - pts[a][0]) * (pts[b][1] - pts[a][1]);
  const inside = (p, a, b, c) => area2(a, b, p) >= 0 && area2(b, c, p) >= 0 && area2(c, a, p) >= 0;
  let guard = 0;
  while (idx.length > 3 && guard++ < 1000) {
    let cut = false;
    for (let i = 0; i < idx.length; i++) {
      const a = idx[(i - 1 + idx.length) % idx.length], b = idx[i], c = idx[(i + 1) % idx.length];
      if (area2(a, b, c) <= 1e-14) continue;
      if (idx.some(p => p !== a && p !== b && p !== c && inside(p, a, b, c))) continue;
      out.push([face[a], face[b], face[c]]);
      idx.splice(i, 1);
      cut = true;
      break;
    }
    if (!cut) break;
  }
  if (idx.length === 3) out.push(idx.map(i => face[i]));
  else for (let i = 1; i + 1 < idx.length; i++) out.push([face[idx[0]], face[idx[i]], face[idx[i + 1]]]);
  return out;
}

export function triangulate(m) {
  const tris = [], faceOf = [];
  m.f.forEach((f, fi) => {
    let list;
    if (f.length === 3) list = [f];
    else if (f.length === 4) {
      const d02 = len(sub(m.v[f[0]], m.v[f[2]])), d13 = len(sub(m.v[f[1]], m.v[f[3]]));
      list = d02 <= d13 ? [[f[0], f[1], f[2]], [f[0], f[2], f[3]]] : [[f[0], f[1], f[3]], [f[1], f[2], f[3]]];
    } else list = earClip(m, f);
    for (const t of list) { tris.push(t); faceOf.push(fi); }
  });
  const pos = new Float32Array(m.v.length * 3);
  m.v.forEach((p, i) => pos.set(p, i * 3));
  return { pos, idx: Uint32Array.from(tris.flat()), faceOf: Uint32Array.from(faceOf) };
}

// planar UVs from the largest side of each face (good enough for solid colours and simple textures)
export function boxUVs(m, scale = 1) {
  const uv = new Float32Array(m.v.length * 2);
  const best = new Array(m.v.length).fill(null);
  m.f.forEach(f => {
    const n = faceNormal(m, f), a = n.map(Math.abs);
    const k = a[0] >= a[1] && a[0] >= a[2] ? 0 : a[1] >= a[2] ? 1 : 2;
    for (const i of f) if (best[i] === null) best[i] = k;
  });
  m.v.forEach((p, i) => {
    const k = best[i] ?? 2;
    const [s, t] = k === 0 ? [p[1], p[2]] : k === 1 ? [p[0], p[2]] : [p[0], p[1]];
    uv[i * 2] = s * scale; uv[i * 2 + 1] = t * scale;
  });
  return uv;
}

// a mesh is closed (watertight) when every edge is used by exactly two faces, once each way
export function isClosed(m) {
  const dir = new Map();
  for (const f of m.f) for (let i = 0; i < f.length; i++) {
    const k = f[i] + ',' + f[(i + 1) % f.length];
    dir.set(k, (dir.get(k) || 0) + 1);
  }
  for (const [k, c] of dir) {
    if (c !== 1) return false;
    const [a, b] = k.split(',');
    if (dir.get(b + ',' + a) !== 1) return false;
  }
  return true;
}

export function volume(m) {
  const { pos, idx } = triangulate(m);
  let vol = 0;
  for (let t = 0; t < idx.length; t += 3) {
    const a = [pos[idx[t] * 3], pos[idx[t] * 3 + 1], pos[idx[t] * 3 + 2]];
    const b = [pos[idx[t + 1] * 3], pos[idx[t + 1] * 3 + 1], pos[idx[t + 1] * 3 + 2]];
    const c = [pos[idx[t + 2] * 3], pos[idx[t + 2] * 3 + 1], pos[idx[t + 2] * 3 + 2]];
    vol += dot(a, cross(b, c)) / 6;
  }
  return vol;
}
