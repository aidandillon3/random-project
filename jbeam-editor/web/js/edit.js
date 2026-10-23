// Edits: each user action becomes a record of file operations plus their
// inverse. Records change the local scene right away (so the 3D view updates)
// and queue the operations; Save writes the queue into the .jbeam files.
import { S, emit } from './state.js';
import { effOpts, fileIndexByKey, fileKey, partFileKey, reindex, toWorld } from './scene.js';
import { num, toast } from './util.js';

// ---- finding elements by the keys the operations use -----------------------
function findNode(op) {
  const sc = S.scene, fi = fileIndexByKey(op.f);
  return sc.nodes.findIndex(n => n.f === fi && n.part === op.part && n.rid === op.id);
}
function sameCells(a, b) {
  if (a.length !== b.length) return false;
  for (let i = 0; i < a.length; i++) if (a[i] !== b[i]) return false;
  return true;
}
function findRow(list, op, sec) {
  const fi = fileIndexByKey(op.f);
  return list.findIndex(e => e.f === fi && e.part === op.part && (e.sec || sec) === (op.sec || sec) && e.rk && sameCells(e.rk, op.rk));
}

function deriveNode(n) {
  const o = effOpts(n);
  n.w = num(o.nodeWeight, 25);
  if (!(n.w > 0)) n.w = 25;
  n.col = o.collision !== false;
  n.sc = !!o.selfCollision;
  n.fixed = !!o.fixed;
  const g = o.group;
  n.grp = typeof g === 'string' ? (g ? [g] : []) : Array.isArray(g) ? g.filter(x => typeof x === 'string' && x) : [];
}
function deriveBeam(b) {
  const o = effOpts(b);
  b.k = o.beamSpring ?? 4300000; b.c = o.beamDamp ?? 580; b.d = o.beamDeform ?? 220000; b.s = o.beamStrength ?? 'FLT_MAX';
  if (b.sec !== 'hydros') {
    const t = o.beamType;
    b.t = 'NORMAL';
    if (typeof t === 'string') {
      for (const x of t.split('|')) if (['BOUNDED', 'SUPPORT', 'HYDRO', 'ANISOTROPIC', 'PRESSURED', 'LBEAM'].includes(x)) b.t = x;
      if (t.includes('TIRESIDE')) b.t = 'ANISOTROPIC';
    }
  }
}
function deriveTri(t) {
  const tt = effOpts(t).triangleType;
  t.nc = typeof tt === 'string' && tt.includes('NONCOLLIDABLE');
}

function setInline(el, opts) {
  el.io = Object.assign({}, el.io || {});
  for (const [k, v] of Object.entries(opts)) {
    if (v === null || v === undefined) delete el.io[k]; else el.io[k] = v;
  }
  if (!Object.keys(el.io).length) el.io = null;
}

// ---- applying one operation to the local scene ------------------------------
function applyLocal(op) {
  const sc = S.scene;
  switch (op.op) {
    case 'node.move': {
      const i = findNode(op);
      if (i < 0) return;
      const n = sc.nodes[i];
      n.raw = op.pos.slice();
      n.rawCells = op.pos.slice();
      n.p = toWorld(n.xf, n.raw);
      break;
    }
    case 'node.set': {
      const i = findNode(op);
      if (i < 0) return;
      setInline(sc.nodes[i], op.opts);
      deriveNode(sc.nodes[i]);
      break;
    }
    case 'node.add': {
      const i = findNode({ f: op.f, part: op.part, id: op.row[0] });
      if (i >= 0 && sc.nodes[i].del) { sc.nodes[i].del = false; break; }
      const raw = op.row.slice(1, 4);
      const n = {
        id: op.row[0], rid: op.row[0], raw, rawCells: raw.slice(), xf: op._xf || 0, p: null,
        part: op.part, f: fileIndexByKey(op.f), row: -1, bo: op._bo ?? 0,
        io: op.row[4] ? Object.assign({}, op.row[4]) : null, isNew: true,
      };
      n.p = toWorld(n.xf, raw);
      deriveNode(n);
      sc.nodes.push(n);
      break;
    }
    case 'node.del': {
      const i = findNode(op);
      if (i >= 0) sc.nodes[i].del = true;
      break;
    }
    case 'beam.add': {
      const sec = op.sec || 'beams';
      const rk = op.row.slice(0, 2).concat([op._occ || 0]);
      const i = findRow(sc.beams, { f: op.f, part: op.part, rk, sec }, 'beams');
      if (i >= 0 && sc.beams[i].del) { sc.beams[i].del = false; sc.beams[i].ord = ordNext++; renumber(sc.beams, sc.beams[i].f, op.part, sec); break; }
      const a = sc.nodeById.get(op.row[0]), b = sc.nodeById.get(op.row[1]);
      if (a === undefined || b === undefined) return;
      const beam = { a, b, t: 'NORMAL', sec, part: op.part, f: fileIndexByKey(op.f), row: -1, rk, bo: op._bo ?? 0,
        io: op.row[2] ? Object.assign({}, op.row[2]) : null, isNew: true, ord: ordNext++ };
      deriveBeam(beam);
      sc.beams.push(beam);
      renumber(sc.beams, beam.f, op.part, sec);
      break;
    }
    case 'beam.del': {
      const i = findRow(sc.beams, op, 'beams');
      if (i >= 0) { sc.beams[i].del = true; renumber(sc.beams, sc.beams[i].f, op.part, sc.beams[i].sec); }
      break;
    }
    case 'beam.set': {
      const i = findRow(sc.beams, op, 'beams');
      if (i < 0) return;
      setInline(sc.beams[i], op.opts);
      deriveBeam(sc.beams[i]);
      break;
    }
    case 'tri.add': {
      const sec = op.sec || 'triangles';
      const cnt = sec === 'quads' ? 4 : 3;
      const rk = op.row.slice(0, cnt).concat([op._occ || 0]);
      const found = sc.tris.filter(t => t.f === fileIndexByKey(op.f) && t.part === op.part && t.sec === sec && t.rk && sameCells(t.rk, rk));
      if (found.length && found.every(t => t.del)) {
        const o = ordNext++;
        found.forEach(t => { t.del = false; t.ord = o; });
        renumber(sc.tris, found[0].f, op.part, sec);
        break;
      }
      const n = op.row.slice(0, cnt).map(id => sc.nodeById.get(id));
      if (n.some(x => x === undefined)) return;
      const base = { sec, part: op.part, f: fileIndexByKey(op.f), row: -1, rk, bo: op._bo ?? 0,
        io: op.row[cnt] ? Object.assign({}, op.row[cnt]) : null, isNew: true, ord: ordNext++ };
      const halves = cnt === 4 ? [[n[0], n[1], n[2]], [n[2], n[3], n[0]]] : [n];
      halves.forEach((tn, h) => {
        const t = Object.assign({}, base, { n: tn, rk: rk.slice() });
        if (cnt === 4) t.half = h;
        deriveTri(t);
        sc.tris.push(t);
      });
      renumber(sc.tris, base.f, op.part, sec);
      break;
    }
    case 'tri.del': {
      const i = findRow(sc.tris, op, 'triangles');
      if (i >= 0) {
        const t0 = sc.tris[i];
        sc.tris.filter(t => sameKey(t, t0)).forEach(t => { t.del = true; });
        renumber(sc.tris, t0.f, op.part, t0.sec);
      }
      break;
    }
    case 'tri.set': {
      const i = findRow(sc.tris, op, 'triangles');
      if (i < 0) return;
      for (const t of sc.tris.filter(t => sameKey(t, sc.tris[i]))) { setInline(t, op.opts); deriveTri(t); }
      break;
    }
    case 'tri.flip': {
      const i = findRow(sc.tris, op, 'triangles');
      if (i < 0) return;
      const t0 = sc.tris[i];
      const group = sc.tris.filter(t => sameKey(t, t0));
      const rk = t0.rk.slice();
      if (t0.sec === 'quads') {
        const h0 = group.find(t => t.half === 0), h1 = group.find(t => t.half === 1);
        if (h0 && h1) {
          const [A, B, C] = h0.n, D = h1.n[1];
          h0.n = [A, D, C];
          h1.n = [C, B, A];
        }
        [rk[1], rk[3]] = [rk[3], rk[1]];
      } else {
        for (const t of group) t.n = [t.n[0], t.n[2], t.n[1]];
        [rk[1], rk[2]] = [rk[2], rk[1]];
      }
      for (const t of group) t.rk = rk.slice();
      renumber(sc.tris, t0.f, op.part, t0.sec);
      break;
    }
    default:
      break;  // other operations only change files; the scene reloads after saving
  }
}
function sameKey(a, b) {
  return a.f === b.f && a.part === b.part && a.sec === b.sec && a.rk && b.rk && sameCells(a.rk, b.rk);
}

// Rows with identical cells are told apart by their occurrence number (the last
// rk entry). Adding, deleting or flipping a row shifts the numbers of its twins,
// so recount them in file order: original rows by line, new rows at the end.
let ordNext = 1e7;
function renumber(list, f, part, sec) {
  const rows = list.filter(e => !e.del && e.f === f && e.part === part && e.sec === sec && e.rk && e.half !== 1);
  rows.sort((a, b) => (a.ord ?? a.row) - (b.ord ?? b.row));
  const seen = new Map(), remap = new Map();
  for (const e of rows) {
    const cells = e.rk.slice(0, -1).join('\u0001');
    const n = seen.get(cells) || 0;
    seen.set(cells, n + 1);
    if (e.rk[e.rk.length - 1] !== n) remap.set(e.rk.join('\u0001'), n);
  }
  if (!remap.size) return;
  for (const e of list) {
    if (e.del || e.f !== f || e.part !== part || e.sec !== sec || !e.rk) continue;
    const n = remap.get(e.rk.join('\u0001'));
    if (n !== undefined) { e.rk = e.rk.slice(); e.rk[e.rk.length - 1] = n; }
  }
}

// ---- records, undo, redo ----------------------------------------------------------
export function commit(label, ops, inv, { applied = false, needsReload = false } = {}) {
  if (!ops.length) return;
  if (!applied) for (const op of ops) applyLocal(op);
  S.undo.push({ label, ops, inv, needsReload });
  if (S.undo.length > 300) S.undo.shift();
  S.redo = [];
  S.pending.push(...ops);
  reindex();
  emit('edited', label);
}

export function undo() {
  const r = S.undo.pop();
  if (!r) return toast('Nothing to undo');
  for (const op of r.inv.slice().reverse()) applyLocal(op);
  S.pending.push(...r.inv.slice().reverse());
  S.redo.push(r);
  reindex();
  emit('edited', 'Undo ' + r.label);
  toast('Undid: ' + r.label);
}
export function redo() {
  const r = S.redo.pop();
  if (!r) return toast('Nothing to redo');
  for (const op of r.ops) applyLocal(op);
  S.pending.push(...r.ops);
  S.undo.push(r);
  reindex();
  emit('edited', 'Redo ' + r.label);
  toast('Redid: ' + r.label);
}

// ---- operation builders --------------------------------------------------------------
export function opNodeMove(n, newRaw) {
  const k = { f: fileKey(n), part: n.part, id: n.rid };
  return [{ op: 'node.move', ...k, pos: newRaw.map(v => +v.toFixed(5)) },
          { op: 'node.move', ...k, pos: n.raw.slice() }];
}

export function opNodeSet(n, opts) {
  const k = { f: fileKey(n), part: n.part, id: n.rid };
  const back = {};
  for (const key of Object.keys(opts)) back[key] = n.io && key in n.io ? n.io[key] : null;
  return [{ op: 'node.set', ...k, opts }, { op: 'node.set', ...k, opts: back }];
}

export function opRowSet(el, opts) {
  const kind = el.n ? 'tri.set' : 'beam.set';
  const k = { f: fileKey(el), part: el.part, sec: el.sec, rk: el.rk.slice() };
  const back = {};
  for (const key of Object.keys(opts)) back[key] = el.io && key in el.io ? el.io[key] : null;
  return [{ op: kind, ...k, opts }, { op: kind, ...k, opts: back }];
}

function occFor(list, f, part, sec, cells) {
  let n = 0;
  for (const e of list) if (!e.del && e.f === f && e.part === part && e.sec === sec && e.rk && sameCells(e.rk.slice(0, -1), cells)) n++;
  return n;
}

function baseFor(part, list, sec) {
  // modifier options a new row inherits: take them from the last row of that part
  for (let i = list.length - 1; i >= 0; i--) {
    const e = list[i];
    if (e.part === part && (!sec || e.sec === sec) && e.bo !== undefined && !e.isNew) return e.bo;
  }
  for (let i = list.length - 1; i >= 0; i--) if (list[i].part === part && list[i].bo !== undefined) return list[i].bo;
  return 0;
}

export function opNodeAdd(part, id, world, after = null, inline = null) {
  const sc = S.scene;
  const f = partFileKey(part);
  // a new node gets the same slot offset as the part's other nodes
  let xf = 0;
  for (const n of sc.nodes) if (n.part === part && !n.del) { xf = n.xf; break; }
  const xfObj = sc.xforms[xf];
  let raw = world;
  if (xfObj) {
    const inv = [-xfObj.q[0], -xfObj.q[1], -xfObj.q[2], xfObj.q[3]];
    const v = [world[0] - xfObj.t[0], world[1] - xfObj.t[1], world[2] - xfObj.t[2]];
    const x = inv[0], y = inv[1], z = inv[2], w = inv[3];
    const tx = 2 * (y * v[2] - z * v[1]), ty = 2 * (z * v[0] - x * v[2]), tz = 2 * (x * v[1] - y * v[0]);
    raw = [v[0] + w * tx + (y * tz - z * ty), v[1] + w * ty + (z * tx - x * tz), v[2] + w * tz + (x * ty - y * tx)];
  }
  raw = raw.map(v => +v.toFixed(4));
  const row = [id, ...raw];
  if (inline && Object.keys(inline).length) row.push(inline);
  const op = { op: 'node.add', f, part, row, after, _xf: xf, _bo: baseFor(part, sc.nodes) };
  return [op, { op: 'node.del', f, part, id }];
}

export function opNodeDel(n) {
  const sc = S.scene;
  // re-insert after the node that was above it in the file
  let after = null, bestRow = -1;
  for (const m of sc.nodes) {
    if (m !== n && m.f === n.f && m.part === n.part && m.row < n.row && m.row > bestRow && !m.del) { bestRow = m.row; after = m.rid; }
  }
  const row = [n.rid, ...n.raw.map(v => +(+v).toFixed(5))];
  if (n.io && Object.keys(n.io).length) row.push(Object.assign({}, n.io));
  const f = fileKey(n);
  return [{ op: 'node.del', f, part: n.part, id: n.rid },
          { op: 'node.add', f, part: n.part, row, after, _xf: n.xf, _bo: n.bo }];
}

export function opBeamAdd(part, idA, idB, inline = null, sec = 'beams') {
  const sc = S.scene;
  const f = partFileKey(part), fi = fileIndexByKey(f);
  const occ = occFor(sc.beams, fi, part, sec, [idA, idB]);
  const row = [idA, idB];
  if (inline && Object.keys(inline).length) row.push(inline);
  return [{ op: 'beam.add', f, part, sec, row, _occ: occ, _bo: baseFor(part, sc.beams, sec) },
          { op: 'beam.del', f, part, sec, rk: [idA, idB, occ] }];
}

export function opBeamDel(b) {
  const f = fileKey(b);
  const row = b.rk.slice(0, -1);
  if (b.io && Object.keys(b.io).length) row.push(Object.assign({}, b.io));
  return [{ op: 'beam.del', f, part: b.part, sec: b.sec, rk: b.rk.slice() },
          { op: 'beam.add', f, part: b.part, sec: b.sec, row, _occ: b.rk[b.rk.length - 1], _bo: b.bo }];
}

export function opTriAdd(part, ids, inline = null) {
  const sc = S.scene;
  const f = partFileKey(part), fi = fileIndexByKey(f);
  const occ = occFor(sc.tris, fi, part, 'triangles', ids);
  const row = ids.slice();
  if (inline && Object.keys(inline).length) row.push(inline);
  return [{ op: 'tri.add', f, part, sec: 'triangles', row, _occ: occ, _bo: baseFor(part, sc.tris, 'triangles') },
          { op: 'tri.del', f, part, sec: 'triangles', rk: ids.concat([occ]) }];
}

export function opTriDel(t) {
  const f = fileKey(t);
  const row = t.rk.slice(0, -1);
  if (t.io && Object.keys(t.io).length) row.push(Object.assign({}, t.io));
  return [{ op: 'tri.del', f, part: t.part, sec: t.sec, rk: t.rk.slice() },
          { op: 'tri.add', f, part: t.part, sec: t.sec, row, _occ: t.rk[t.rk.length - 1], _bo: t.bo }];
}

export function opTriFlip(t) {
  const f = fileKey(t);
  const rk2 = t.rk.slice();
  if (t.sec === 'quads') [rk2[1], rk2[3]] = [rk2[3], rk2[1]]; else [rk2[1], rk2[2]] = [rk2[2], rk2[1]];
  return [{ op: 'tri.flip', f, part: t.part, sec: t.sec, rk: t.rk.slice() },
          { op: 'tri.flip', f, part: t.part, sec: t.sec, rk: rk2 }];
}

// collapse a queue before sending: many drags of one node become one move
export function compactOps(ops) {
  const out = [];
  for (const op of ops) {
    const prev = out[out.length - 1];
    if (prev && op.op === 'node.move' && prev.op === 'node.move' && prev.f === op.f && prev.part === op.part && prev.id === op.id) {
      out[out.length - 1] = op;
      continue;
    }
    out.push(op);
  }
  return out;
}
