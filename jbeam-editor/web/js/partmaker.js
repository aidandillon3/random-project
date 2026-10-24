// Turns a mesh (already placed on the vehicle) into a BeamNG part: the mesh is
// written as a .dae, and a node structure is built to carry it — either shaped
// to the mesh (smart fit) or a braced box — with collision, stable weights and
// beams that bolt it to the vehicle. Used by the importer and the 3D Workshop.
import { S, A } from './state.js';
import { api } from './api.js';
import { DT, STIFF_TARGET, partFileKey } from './scene.js';
import { smartFit, assignWeights, STIFFNESS, spacingFor } from './smartfit.js';
import { freePrefix, info } from './dialogs.js';

export { STIFFNESS };

export function b64(arr) {
  const bytes = new Uint8Array(arr.buffer, arr.byteOffset, arr.byteLength);
  let s = '';
  for (let i = 0; i < bytes.length; i += 0x8000) s += String.fromCharCode.apply(null, bytes.subarray(i, i + 0x8000));
  return btoa(s);
}

export function meshBounds(pos) {
  const lo = [Infinity, Infinity, Infinity], hi = [-Infinity, -Infinity, -Infinity];
  for (let i = 0; i < pos.length; i += 3) for (let k = 0; k < 3; k++) { lo[k] = Math.min(lo[k], pos[i + k]); hi[k] = Math.max(hi[k], pos[i + k]); }
  return { lo, hi, size: [hi[0] - lo[0], hi[1] - lo[1], hi[2] - lo[2]] };
}

// a braced box around the mesh (the old, simple structure), in the same shape smartFit returns
export function boxFit(pos, prefix, spacing) {
  const { lo, hi } = meshBounds(pos);
  const min = lo.slice(), max = hi.slice();
  for (let k = 0; k < 3; k++) if (max[k] - min[k] < 0.06) { const c = (max[k] + min[k]) / 2; min[k] = c - 0.03; max[k] = c + 0.03; }
  const cnt = [0, 1, 2].map(k => Math.min(7, Math.max(2, Math.round((max[k] - min[k]) / spacing) + 1)));
  const symmetric = Math.abs(min[0] + max[0]) < 0.04;
  if (symmetric) { const hw = (max[0] - min[0]) / 2; min[0] = -hw; max[0] = hw; }
  const at = (i, j, k) => [min[0] + (max[0] - min[0]) * i / (cnt[0] - 1), min[1] + (max[1] - min[1]) * j / (cnt[1] - 1), min[2] + (max[2] - min[2]) * k / (cnt[2] - 1)];
  const onSurface = (i, j, k) => i === 0 || j === 0 || k === 0 || i === cnt[0] - 1 || j === cnt[1] - 1 || k === cnt[2] - 1;
  const ids = new Map(), pos2 = new Map(), nodes = [], numbers = new Map();
  let next = 1;
  for (let k = 0; k < cnt[2]; k++) for (let j = 0; j < cnt[1]; j++) for (let i = 0; i < cnt[0]; i++) {
    if (!onSurface(i, j, k)) continue;
    let id;
    if (symmetric) {
      const im = Math.min(i, cnt[0] - 1 - i), key = im + ',' + j + ',' + k;
      if (!numbers.has(key)) numbers.set(key, next++);
      const mid = (cnt[0] - 1) / 2;
      id = prefix + numbers.get(key) + (i < mid ? 'r' : i > mid ? 'l' : '');
    } else id = prefix + next++;
    const p = at(i, j, k).map(v => +v.toFixed(4));
    ids.set(i + ',' + j + ',' + k, id);
    pos2.set(id, p);
    nodes.push([id, ...p]);
  }
  const key = (i, j, k) => ids.get(i + ',' + j + ',' + k);
  const beams = [], seen = new Set();
  const addBeam = (a, b) => { if (!a || !b || a === b) return; const s = a < b ? a + '|' + b : b + '|' + a; if (!seen.has(s)) { seen.add(s); beams.push([a, b]); } };
  for (const [s, a] of ids) {
    const [i, j, k] = s.split(',').map(Number);
    for (let di = -1; di <= 1; di++) for (let dj = -1; dj <= 1; dj++) for (let dk = -1; dk <= 1; dk++) {
      if (!di && !dj && !dk) continue;
      addBeam(a, key(i + di, j + dj, k + dk));
    }
  }
  const L = cnt.map(c => c - 1);
  addBeam(key(0, 0, 0), key(L[0], L[1], L[2])); addBeam(key(L[0], 0, 0), key(0, L[1], L[2]));
  addBeam(key(0, L[1], 0), key(L[0], 0, L[2])); addBeam(key(0, 0, L[2]), key(L[0], L[1], 0));
  const centre = [(min[0] + max[0]) / 2, (min[1] + max[1]) / 2, (min[2] + max[2]) / 2];
  const tris = [];
  const tri = (a, b, c) => {
    const A = pos2.get(a), B = pos2.get(b), C = pos2.get(c);
    const u = [B[0] - A[0], B[1] - A[1], B[2] - A[2]], v = [C[0] - A[0], C[1] - A[1], C[2] - A[2]];
    const n = [u[1] * v[2] - u[2] * v[1], u[2] * v[0] - u[0] * v[2], u[0] * v[1] - u[1] * v[0]];
    const m = [(A[0] + B[0] + C[0]) / 3 - centre[0], (A[1] + B[1] + C[1]) / 3 - centre[1], (A[2] + B[2] + C[2]) / 3 - centre[2]];
    tris.push(n[0] * m[0] + n[1] * m[1] + n[2] * m[2] >= 0 ? [a, b, c] : [a, c, b]);
  };
  for (let ax = 0; ax < 3; ax++) for (const fixed of [0, L[ax]]) {
    const [u, v] = [0, 1, 2].filter(x => x !== ax);
    for (let a = 0; a < L[u]; a++) for (let b = 0; b < L[v]; b++) {
      const c = (da, db) => { const ix = [0, 0, 0]; ix[ax] = fixed; ix[u] = a + da; ix[v] = b + db; return key(...ix); };
      tri(c(0, 0), c(1, 0), c(1, 1));
      tri(c(1, 1), c(0, 1), c(0, 0));
    }
  }
  const area = new Map(nodes.map(n => [n[0], 1]));
  return { nodes, beams: { main: beams, rigid: [] }, tris, rigidifiers: [], pos: pos2, area, spacing, symmetric };
}

// the structure for a mesh, by the person's choices
export function makeStructure(pos, idx, { kind = 'smart', detail = 'medium', spacing = null, prefix = 'sf', inside = null } = {}) {
  const b = meshBounds(pos);
  const s = spacing || spacingFor(b.size, detail);
  if (kind === 'box') return boxFit(pos, prefix, s * 1.6);
  return smartFit(pos, idx, { spacing: s, prefix, inside });
}

// How firmly a new part is fixed to the vehicle: the springs, when its mounting beams start to
// bend and when they break (as a share of the part's own stiffness), and how many of them there are.
export const MOUNT = {
  light: { label: 'Light: breaks off easily', k: 1, deform: 1, strength: 2.5, perAnchor: 3, near: [12, 2] },
  normal: { label: 'Normal: stays on in bumps', k: 2, deform: 4, strength: 10, perAnchor: 4, near: [16, 3] },
  strong: { label: 'Strong: bolted on, survives crashes', k: 4, deform: 15, strength: 40, perAnchor: 5, near: [24, 3] },
  welded: { label: 'Welded: never comes off', k: 6, deform: null, strength: null, perAnchor: 6, near: [32, 3] },
};

// the mounting beam settings for a stiffness and a mount level ('FLT_MAX' = never)
export function mountSettings(stiffness, level, breakable = true) {
  const st = STIFFNESS[stiffness] || STIFFNESS.panel, m = MOUNT[level] || MOUNT.light;
  return {
    spring: Math.round(st.spring * m.k), damp: Math.round(st.damp * Math.sqrt(m.k)),
    deform: m.deform ? Math.round(st.deform * m.deform) : 'FLT_MAX',
    strength: m.strength && breakable ? Math.round(st.deform * m.strength) : 'FLT_MAX',
  };
}

// the stiffest mounting spring the vehicle's nodes take and stay stable (null: no limit needed)
export function mountSpringLimit(beams) {
  const sc = S.scene;
  const count = new Map();
  for (const [vid] of beams) count.set(vid, (count.get(vid) || 0) + 1);
  let lim = Infinity;
  for (const [vid, c] of count) {
    const n = sc.nodes[sc.nodeById.get(vid)];
    if (!n) continue;
    const room = Math.max(0, n.w * (STIFF_TARGET / DT) ** 2 - (n.K || 0));
    lim = Math.min(lim, room / c);
  }
  return isFinite(lim) ? Math.max(100000, Math.floor(lim / 1000) * 1000) : null;
}

// beams from the structure to the vehicle: to picked nodes, or to the nearest ones
export function attachBeams(fit, mode, anchors, level = 'light') {
  const m = MOUNT[level] || MOUNT.light;
  const sc = S.scene;
  const d2 = (a, b) => (a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2 + (a[2] - b[2]) ** 2;
  const out = [], seen = new Set();
  const add = (vehId, cageId) => { const k = vehId + '|' + cageId; if (!seen.has(k)) { seen.add(k); out.push([vehId, cageId]); } };
  const ids = [...fit.pos.keys()].filter(id => !fit.rigidifiers.includes(id));
  if (mode === 'sel') {
    for (const i of anchors) {
      const p = sc.nodes[i].p;
      ids.slice().sort((a, b) => d2(fit.pos.get(a), p) - d2(fit.pos.get(b), p)).slice(0, m.perAnchor).forEach(c => add(sc.nodes[i].id, c));
    }
  } else if (mode === 'near') {
    const veh = sc.nodes.filter(n => !n.del);
    const nearest = ids.map(c => {
      const p = fit.pos.get(c);
      const list = veh.map(n => [d2(n.p, p), n.id]).sort((a, b) => a[0] - b[0]).slice(0, m.near[1]);
      return { c, list, d: Math.sqrt(list[0]?.[0] ?? Infinity) };
    }).sort((a, b) => a.d - b.d);
    if (!nearest.length || nearest[0].d > 1.0) return { beams: [], warn: 'Nothing on the vehicle is within 1 m of the model, so it was not attached.' };
    const lim = nearest[0].d + 0.12;
    for (const x of nearest.filter(x => x.d <= lim).slice(0, m.near[0])) for (const [dd, id] of x.list) if (Math.sqrt(dd) <= lim + 0.2) add(id, x.c);
  }
  const veh = new Set(out.map(b => b[0]));
  const warn = out.length && veh.size < 3
    ? `It is held by only ${veh.size} vehicle node${veh.size === 1 ? '' : 's'}, so it can swing around ${veh.size === 1 ? 'it' : 'them'}. Pick at least 3 that aren't in a line.`
    : null;
  return { beams: out, warn };
}

// the JBeam data of the part
export function partData({ name, title, slotType, mesh, fit, attach, mass, stiffness = 'panel', collision = true, breakable = true,
  mount = null }) {
  const st = STIFFNESS[stiffness] || STIFFNESS.panel;
  const mt = mount || mountSettings(stiffness, 'light', breakable);
  const { weights, total, needed } = assignWeights(fit, mass, { spring: st.spring, damp: st.damp, attach, attachSpring: mt.spring });
  const rig = new Set(fit.rigidifiers);
  const nodeRows = [];
  let lastW = null;
  for (const row of fit.nodes) {
    if (rig.has(row[0])) continue;
    const w = weights.get(row[0]);
    if (w !== lastW) { nodeRows.push({ nodeWeight: w }); lastW = w; }
    nodeRows.push(row);
  }
  const rigRows = fit.nodes.filter(r => rig.has(r[0])).flatMap(r => [{ nodeWeight: weights.get(r[0]) }, r]);
  const deform = st.deform;
  const data = {
    information: info(title),
    slotType,
    flexbodies: [['mesh', '[group]:', 'nonFlexMaterials'], [mesh, [name], []]],
    nodes: [['id', 'posX', 'posY', 'posZ'], { collision: true }, { selfCollision: true }, { frictionCoef: 0.7 },
      { nodeMaterial: '|NM_METAL' }, { group: name }, ...nodeRows,
      ...(rigRows.length ? [{ collision: false }, { selfCollision: false }, ...rigRows, { collision: true }, { selfCollision: true }] : []),
      { group: '' }],
    beams: [['id1:', 'id2:'], { beamPrecompression: 1, beamType: '|NORMAL', beamLongBound: 1, beamShortBound: 1 },
      { beamSpring: st.spring, beamDamp: st.damp }, { beamDeform: deform, beamStrength: 'FLT_MAX' },
      ...fit.beams.main,
      ...(fit.beams.rigid.length ? [{ beamSpring: Math.round(st.spring * 0.5), beamDamp: Math.round(st.damp * 0.5) }, { beamDeform: Math.round(deform * 0.4) }, ...fit.beams.rigid] : []),
      ...(attach.length ? [{ beamSpring: mt.spring, beamDamp: mt.damp },
        { beamDeform: mt.deform, beamStrength: mt.strength },
        ...(mt.strength !== 'FLT_MAX' ? [{ breakGroup: name + '_mount' }] : []), ...attach,
        ...(mt.strength !== 'FLT_MAX' ? [{ breakGroup: '' }] : [])] : []),
      { beamDeform: deform },
      { beamStrength: 'FLT_MAX' }],
  };
  if (collision && fit.tris.length) data.triangles = [['id1:', 'id2:', 'id3:'], { groundModel: 'metal' }, { dragCoef: 5 }, ...fit.tris];
  return { data, total, needed };
}

// write the mesh and the part, put it on the vehicle; returns a summary
export async function createPart({ pos, idx, uv = null, name, title, slot, rootId, fit, attach, mass, stiffness, collision,
  breakable, mount = null, color = [0.6, 0.6, 0.6], paint = false, groups = null }) {
  const mr = await api.importMesh({ model: S.model, root: rootId, name, positions: b64(pos), indices: b64(idx),
    uvs: uv ? b64(uv) : null, color, paint,
    groups: groups ? groups.map(g => ({ indices: b64(g.indices), color: g.color, paint: !!g.paint })) : null });
  const slotType = slot.allowTypes?.[0] || slot.id;
  const { data, total, needed } = partData({ name, title, slotType, mesh: mr.mesh, fit, attach, mass, stiffness, collision, breakable, mount });
  const r = await api.newPart({ model: S.model, root: rootId, name, mode: 'data', data, title: data.information.name, slotType });
  S.summary = r.summary;
  S.config.parts[slot.id] = name;
  S.activePart = name;
  S.pinActive = true;
  await A.reloadModel();
  return { nodes: fit.nodes.length, beams: fit.beams.main.length + fit.beams.rigid.length + attach.length,
    tris: fit.tris.length, mass: total, needed };
}

// put the mesh on an existing part instead (it bends with that part's nodes)
export async function meshOnPart({ pos, idx, uv = null, name, part, color, paint, groups: colourGroups = null }) {
  const sc = S.scene;
  const groups = {};
  for (const n of sc.nodes) if (!n.del && n.part === part) for (const g of n.grp) groups[g] = (groups[g] || 0) + 1;
  const grp = Object.entries(groups).sort((a, b) => b[1] - a[1])[0]?.[0];
  if (!grp) throw new Error('The nodes of that part have no group, so a mesh can\'t follow them. Select its nodes and set a Group first.');
  const proot = sc.files[sc.parts[part].f];
  const rootId = S.summary.editableRoots.find(r => r.label === proot.root)?.id;
  const mr = await api.importMesh({ model: S.model, root: rootId, name, positions: b64(pos), indices: b64(idx), uvs: uv ? b64(uv) : null, color, paint,
    groups: colourGroups ? colourGroups.map(g => ({ indices: b64(g.indices), color: g.color, paint: !!g.paint })) : null });
  const ok = await A.saveOps([{ op: 'row.add', f: partFileKey(part), part, sec: 'flexbodies', row: [mr.mesh, [grp], []] }], 'Add mesh ' + mr.mesh);
  if (ok !== true) throw new Error(ok || 'could not add the mesh');
  return grp;
}

export function newPrefix(seed) { return freePrefix(seed); }
