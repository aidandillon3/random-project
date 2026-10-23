// Helpers over the scene data the server sends: lookups, effective options,
// stability numbers, mirror partners and new node names.
import { S } from './state.js';
import { num } from './util.js';

export const DT = 1 / 2000;            // BeamNG physics step
export const STIFF_YELLOW = 1.9;       // about as stiff as the stiffest 1% of official nodes
export const STIFF_RED = 2.6;          // stiffer than any node in the official cars we measured
export const DAMP_YELLOW = 2.5;
export const DAMP_RED = 4.0;
export const STIFF_TARGET = 2.0;       // what auto-weight aims for
export const DAMP_TARGET = 2.4;

export function fileKey(el) { return S.scene.files[el.f]?.key; }
export function editableFile(f) { return !!S.scene.files[f]?.editable; }
export function partEditable(part) {
  const p = S.scene.parts[part];
  return !!(p && S.scene.files[p.f]?.editable);
}
export function partFileKey(part) {
  const p = S.scene.parts[part];
  return p ? S.scene.files[p.f]?.key : null;
}
export function fileIndexByKey(key) {
  return S.scene.files.findIndex(f => f.key === key);
}

export function effOpts(el) {
  const base = S.scene.optsets[el.bo] || {};
  return el.io ? Object.assign({}, base, el.io) : base;
}

// called after every scene load and edit: rebuild lookups and derived values
export function reindex() {
  const sc = S.scene;
  sc.nodeById = new Map();
  sc.nodes.forEach((n, i) => { if (!n.del) sc.nodeById.set(n.id, i); });
  sc.beamsOf = sc.nodes.map(() => []);
  sc.beams.forEach((b, i) => {
    if (b.del || sc.nodes[b.a].del || sc.nodes[b.b].del) return;
    sc.beamsOf[b.a].push(i); sc.beamsOf[b.b].push(i);
  });
  computeStability();
}

export function nodeVisible(n) {
  if (n.del) return false;
  if (S.hiddenParts.has(n.part)) return false;
  if (S.onlyActive && S.activePart && n.part !== S.activePart && !n._linked) return false;
  return true;
}
export function elemVisible(e) {
  if (e.del) return false;
  if (S.hiddenParts.has(e.part)) return false;
  if (S.onlyActive && S.activePart && e.part !== S.activePart) return false;
  return true;
}
export function markLinked() {
  // nodes of other parts that the active part's beams/triangles use stay visible in "only this part"
  const sc = S.scene;
  for (const n of sc.nodes) n._linked = false;
  if (!S.onlyActive || !S.activePart) return;
  for (const b of sc.beams) if (!b.del && b.part === S.activePart) { sc.nodes[b.a]._linked = true; sc.nodes[b.b]._linked = true; }
  for (const t of sc.tris) if (!t.del && t.part === S.activePart) for (const i of t.n) sc.nodes[i]._linked = true;
}

export function computeStability() {
  const sc = S.scene;
  const K = new Float64Array(sc.nodes.length), C = new Float64Array(sc.nodes.length);
  for (const b of sc.beams) {
    if (b.del) continue;
    const k = num(b.k, 0), c = num(b.c, 0);
    if (isFinite(k)) { K[b.a] += k; K[b.b] += k; }
    if (isFinite(c)) { C[b.a] += c; C[b.b] += c; }
  }
  sc.nodes.forEach((n, i) => {
    const w = Math.max(n.w, 1e-6);
    n.K = K[i]; n.C = C[i];
    n.stiff = Math.sqrt(K[i] / w) * DT;
    n.damp = C[i] / w * DT;
    n.risk = Math.max(n.stiff / STIFF_RED, n.damp / DAMP_RED);
    n.level = (n.stiff >= STIFF_RED || n.damp >= DAMP_RED) ? 2 : (n.stiff >= STIFF_YELLOW || n.damp >= DAMP_YELLOW) ? 1 : 0;
  });
}

// smallest weight that keeps a node under the auto-weight targets
export function minStableWeight(n) {
  return Math.max(n.K * DT * DT / (STIFF_TARGET * STIFF_TARGET), n.C * DT / DAMP_TARGET);
}

// --- quaternions (same euler convention as the game's node offsets) ---------
export function quatFromEuler(x, y, z) {
  x *= 0.5; y *= 0.5; z *= 0.5;
  const sx = Math.sin(x), cx = Math.cos(x), sy = Math.sin(y), cy = Math.cos(y), sz = Math.sin(z), cz = Math.cos(z);
  return [cz * sx * cy - sz * cx * sy, cz * cx * sy + sz * sx * cy, sz * cx * cy + cz * sx * sy, cz * cx * cy - sz * sx * sy];
}
export function qrot(q, v) {
  const [x, y, z, w] = q, [vx, vy, vz] = v;
  const tx = 2 * (y * vz - z * vy), ty = 2 * (z * vx - x * vz), tz = 2 * (x * vy - y * vx);
  return [vx + w * tx + (y * tz - z * ty), vy + w * ty + (z * tx - x * tz), vz + w * tz + (x * ty - y * tx)];
}
export function toWorld(xfIndex, raw) {
  const xf = S.scene.xforms[xfIndex];
  if (!xf) return raw.slice();
  const p = qrot(xf.q, raw);
  return [p[0] + xf.t[0], p[1] + xf.t[1], p[2] + xf.t[2]];
}
export function toRaw(xfIndex, world) {
  const xf = S.scene.xforms[xfIndex];
  if (!xf) return world.slice();
  const inv = [-xf.q[0], -xf.q[1], -xf.q[2], xf.q[3]];
  return qrot(inv, [world[0] - xf.t[0], world[1] - xf.t[1], world[2] - xf.t[2]]);
}

// --- mirror partners ------------------------------------------------------------
export function mirrorName(id) {
  const m = /^(.*?)(l+|r+)$/.exec(id);
  if (!m || !m[1]) return null;
  const side = m[2];
  return m[1] + (side[0] === 'l' ? 'r' : 'l').repeat(side.length);
}
export function mirrorPartner(i) {
  const sc = S.scene, n = sc.nodes[i];
  const name = mirrorName(n.id);
  if (name) {
    const j = sc.nodeById.get(name);
    if (j !== undefined) {
      const q = sc.nodes[j];
      if (Math.abs(n.p[0] + q.p[0]) < 0.03 && Math.abs(n.p[1] - q.p[1]) < 0.03 && Math.abs(n.p[2] - q.p[2]) < 0.03) return j;
    }
  }
  return null;
}

// --- names for new nodes --------------------------------------------------------
export function namePrefix(part) {
  const counts = {};
  for (const n of S.scene.nodes) {
    if (n.del || n.part !== part) continue;
    const m = /^([A-Za-z_]+?)\d/.exec(n.id);
    if (m) counts[m[1]] = (counts[m[1]] || 0) + 1;
  }
  let best = null;
  for (const [k, v] of Object.entries(counts)) if (!best || v > counts[best]) best = k;
  return best || 'n';
}
export function newNodeNames(part, mirrored) {
  const prefix = namePrefix(part);
  let maxNum = 0;
  const re = new RegExp('^' + prefix.replace(/[^A-Za-z0-9_]/g, '') + '(\\d+)');
  for (const n of S.scene.nodes) {
    const m = re.exec(n.id);
    if (m) maxNum = Math.max(maxNum, parseInt(m[1], 10));
  }
  for (let k = maxNum + 1; ; k++) {
    const base = prefix + k;
    const names = mirrored ? [base + 'r', base + 'l'] : [base];
    if (names.every(nm => !S.scene.nodeById.has(nm) && !S.scene.nodes.some(n => n.id === nm))) return names;
  }
}

export function partMass(part) {
  let m = 0;
  for (const n of S.scene.nodes) if (!n.del && n.part === part) m += n.w;
  return m;
}
export function totalMass() {
  let m = 0;
  for (const n of S.scene.nodes) if (!n.del) m += n.w;
  return m;
}

export const BEAM_COLORS = {
  NORMAL: 0x7f93b0, BOUNDED: 0xf0c040, SUPPORT: 0xff8a3d, HYDRO: 0x3fd0e0, ANISOTROPIC: 0xb07cff,
  PRESSURED: 0xff6fb0, LBEAM: 0x6fdc7a,
};
export const BEAM_TYPE_INFO = {
  NORMAL: 'normal beam',
  BOUNDED: 'bounded (suspension spring / damper with travel limits)',
  SUPPORT: 'support (only pushes, never pulls; used to stop parts passing through each other)',
  HYDRO: 'hydro (steering rack or a powered ram)',
  ANISOTROPIC: 'anisotropic (different stiffness when stretched)',
  PRESSURED: 'pressured (tire air)',
  LBEAM: 'L-beam (bending stiffness)',
};
