// Mouse and keyboard tools in the 3D view: select, move, add node, connect, triangle.
import { S, emit, clearSelection } from './state.js';
import { V, frame, nodesInRect, pickBeam, pickNode, pickTri, pointOnViewPlane, refreshColors, refreshPositions,
  setOverlay, updateGizmo } from './view3d.js';
import { mirrorName, mirrorPartner, newNodeNames, partEditable, toRaw } from './scene.js';
import { commit, opBeamAdd, opBeamDel, opNodeAdd, opNodeDel, opNodeMove, opTriAdd, opTriDel, opTriFlip } from './edit.js';
import { $, toast } from './util.js';

const HINTS = {
  select: 'Click a node, beam or triangle to select it. Shift+click adds, Ctrl+click toggles, drag on empty space for a box. Drag the arrows to move. Right-drag orbits, middle-drag pans, wheel zooms.',
  node: 'Click to place a node. It is joined to the nodes you had selected, then becomes selected, so you can click a chain. Click an existing node to start from it.',
  beam: 'Click a node, then another: a beam joins them. Keep clicking to chain. Esc or a click on empty space starts over.',
  tri: 'Click three nodes to make a collision triangle. The green side is the outside; red means it faces inward (press I to flip).',
};

let down = null;          // pointer down info
let chain = [];           // nodes clicked by the beam/triangle tools
let dragStart = null;     // gizmo drag bookkeeping

export function setTool(t) {
  S.tool = t === 'move' ? 'select' : t;
  chain = [];
  setOverlay([]);
  for (const b of document.querySelectorAll('#tools button')) b.classList.toggle('active', b.dataset.tool === S.tool || (S.tool === 'select' && b.dataset.tool === 'move'));
  updateHint();
  updateGizmo();
}

const LOOK_HINT = 'Right-drag to turn the view, middle-drag to pan, wheel to zoom. Home shows the whole vehicle.';

export function updateHint(extra) {
  if (S.lookMode && !extra) { $('#hint').textContent = S.scene ? (S.modeHint || LOOK_HINT) : ''; return; }
  let txt = HINTS[S.tool] || '';
  if (S.tool !== 'select' && !partEditable(S.activePart)) txt = 'Pick a part you can edit in "Adding to" first. ' + txt;
  $('#hint').textContent = extra || txt;
}

export function initTools() {
  const el = V.renderer.domElement;
  el.addEventListener('pointerdown', onDown);
  el.addEventListener('pointermove', onMove);
  el.addEventListener('pointerup', onUp);
  el.addEventListener('contextmenu', e => e.preventDefault());
  el.addEventListener('pointerleave', () => { if (V.hover) { V.hover = null; refreshColors(); } });
  window.addEventListener('keydown', e => { if (e.key === 'Alt') { V.controls.mouseButtons.LEFT = 0; } });
  window.addEventListener('keyup', e => { if (e.key === 'Alt') { V.controls.mouseButtons.LEFT = -1; } });
  window.addEventListener('blur', () => { V.controls.mouseButtons.LEFT = -1; });
  V.gizmo.addEventListener('dragging-changed', e => (e.value ? gizmoStart() : gizmoEnd()));
  V.gizmo.addEventListener('objectChange', gizmoMoved);
}

function local(e) {
  const r = V.renderer.domElement.getBoundingClientRect();
  return [e.clientX - r.left, e.clientY - r.top];
}

function onDown(e) {
  if (e.button !== 0 || e.altKey || !S.scene) return;
  if (S.lookMode && S.mode !== 'model') return;   // the look-only modes don't select
  if (V.gizmo.axis) return;   // the gizmo handles this drag
  const [x, y] = local(e);
  down = { x, y, shift: e.shiftKey, ctrl: e.ctrlKey || e.metaKey, moved: false };
}

let hoverT = 0;
function onMove(e) {
  if (!S.scene) return;
  const [x, y] = local(e);
  if (down && S.tool === 'select') {
    if (Math.hypot(x - down.x, y - down.y) > 5) down.moved = true;
    if (down.moved) {
      const b = $('#boxsel');
      b.classList.remove('hidden');
      Object.assign(b.style, { left: Math.min(x, down.x) + 'px', top: (Math.min(y, down.y) + V.el.offsetTop) + 'px',
        width: Math.abs(x - down.x) + 'px', height: Math.abs(y - down.y) + 'px' });
    }
    return;
  }
  if (down) return;
  const now = performance.now();
  if (now - hoverT < 30) return;
  hoverT = now;
  // hover highlight
  let h = null;
  const n = pickNode(x, y);
  if (n >= 0) h = { kind: 'node', i: n };
  else if (S.tool === 'select') {
    const b = pickBeam(x, y);
    if (b >= 0) h = { kind: 'beam', i: b };
  }
  const changed = JSON.stringify(h) !== JSON.stringify(V.hover);
  V.hover = h;
  if (changed) refreshColors();
  if (S.tool === 'node') previewNode(x, y);
  if ((S.tool === 'beam' || S.tool === 'tri') && chain.length) {
    const p = h && h.kind === 'node' ? S.scene.nodes[h.i].p : pointOnViewPlane(x, y, S.scene.nodes[chain[chain.length - 1]].p);
    if (p) setOverlay(chain.map(i => [S.scene.nodes[i].p, p]), [p]);
  }
}

function onUp(e) {
  if (e.button !== 0 || !down) return;
  const [x, y] = local(e);
  const d = down;
  down = null;
  $('#boxsel').classList.add('hidden');
  if (S.tool === 'select') {
    if (d.moved) return boxSelect(d, x, y);
    return clickSelect(x, y, d);
  }
  if (S.tool === 'node') return clickNode(x, y, d);
  if (S.tool === 'beam') return clickBeam(x, y);
  if (S.tool === 'tri') return clickTri(x, y);
}

function followPart(i) {
  const n = S.scene.nodes[i];
  if (!S.pinActive && n && partEditable(n.part) && S.activePart !== n.part) {
    S.activePart = n.part;
    emit('activePart');
  }
}

function clickSelect(x, y, d) {
  const sc = S.scene;
  let hit = null;
  const n = pickNode(x, y);
  if (n >= 0) hit = ['nodes', n];
  else {
    const b = pickBeam(x, y);
    if (b >= 0) hit = ['beams', b];
    else {
      const t = pickTri(x, y);
      if (t >= 0) hit = ['tris', t];
    }
  }
  if (!d.shift && !d.ctrl) clearSelection();
  if (hit) {
    const set = S.sel[hit[0]];
    if (d.ctrl && set.has(hit[1])) set.delete(hit[1]); else set.add(hit[1]);
    if (hit[0] === 'nodes') followPart(hit[1]);
    else {
      const el = hit[0] === 'beams' ? sc.beams[hit[1]] : sc.tris[hit[1]];
      if (!S.pinActive && partEditable(el.part) && S.activePart !== el.part) { S.activePart = el.part; emit('activePart'); }
    }
  }
  emit('selection');
}

function boxSelect(d, x, y) {
  const ids = nodesInRect(d.x, d.y, x, y);
  if (!d.shift && !d.ctrl) clearSelection();
  for (const i of ids) {
    if (d.ctrl) S.sel.nodes.delete(i); else S.sel.nodes.add(i);
  }
  emit('selection');
}

// ---- add node -------------------------------------------------------------------
function anchorPoint() {
  const sc = S.scene;
  const sel = [...S.sel.nodes].filter(i => !sc.nodes[i].del);
  if (sel.length) {
    const c = [0, 0, 0];
    for (const i of sel) for (let k = 0; k < 3; k++) c[k] += sc.nodes[i].p[k] / sel.length;
    return c;
  }
  const t = V.controls.target;
  return [t.x, t.y, t.z];
}

function placement(x, y) {
  let p = pointOnViewPlane(x, y, anchorPoint());
  if (!p) return null;
  if (S.snap) p = p.map(v => Math.round(v / S.snap) * S.snap);
  return p.map(v => +v.toFixed(4));
}

function previewNode(x, y) {
  if (V.hover && V.hover.kind === 'node') { setOverlay([]); return; }
  const p = placement(x, y);
  if (!p) return;
  const sc = S.scene;
  const lines = [...S.sel.nodes].slice(0, 12).map(i => [sc.nodes[i].p, p]);
  const pts = [p];
  if (S.mirror && Math.abs(p[0]) > 0.01) {
    const q = [-p[0], p[1], p[2]];
    pts.push(q);
    for (const i of [...S.sel.nodes].slice(0, 12)) {
      const j = mirrorPartner(i);
      lines.push([sc.nodes[j ?? i].p, q]);
    }
  }
  setOverlay(lines, pts);
}

function requireEditable() {
  if (!S.activePart || !partEditable(S.activePart)) {
    toast('Choose a part you can edit in "Adding to" (parts from the game or a zip are read-only).', 'err');
    return false;
  }
  return true;
}

function clickNode(x, y, d) {
  const sc = S.scene;
  const hit = pickNode(x, y);
  if (hit >= 0) {
    if (!d.shift) clearSelection();
    S.sel.nodes.add(hit);
    emit('selection');
    return;
  }
  if (!requireEditable()) return;
  const p = placement(x, y);
  if (!p) return;
  const part = S.activePart;
  const mirrored = S.mirror && Math.abs(p[0]) > 0.01;
  const names = newNodeNames(part, mirrored);
  const ops = [], inv = [];
  const add = (pair) => { ops.push(pair[0]); if (pair[1]) inv.unshift(pair[1]); };
  const anchors = [...S.sel.nodes].filter(i => !sc.nodes[i].del);
  // right side (-X) gets the "r" name
  let primary = p, secondary = null;
  let primaryName = names[0], secondaryName = names[1];
  if (mirrored) {
    secondary = [-p[0], p[1], p[2]];
    if (p[0] > 0) { primaryName = names[1]; secondaryName = names[0]; }
  }
  add(opNodeAdd(part, primaryName, primary));
  if (mirrored) add(opNodeAdd(part, secondaryName, secondary));
  for (const i of anchors) {
    add(opBeamAdd(part, sc.nodes[i].id, primaryName));
    if (mirrored) {
      const j = mirrorPartner(i);
      add(opBeamAdd(part, sc.nodes[j ?? i].id, secondaryName));
    }
  }
  commit(mirrored ? 'Add 2 nodes' : 'Add node ' + primaryName, ops, inv);
  clearSelection();
  const ni = sc.nodeById.get(primaryName);
  if (ni !== undefined) S.sel.nodes.add(ni);
  emit('selection');
}

// ---- connect --------------------------------------------------------------------------
function beamExists(a, b) {
  const sc = S.scene;
  return sc.beamsOf[a].some(k => { const x = sc.beams[k]; return (x.a === a && x.b === b) || (x.a === b && x.b === a); });
}

export function connect(pairs, label) {
  if (!requireEditable()) return 0;
  const sc = S.scene, part = S.activePart;
  const ops = [], inv = [];
  const seen = new Set();
  const want = [];
  for (const [a, b] of pairs) {
    want.push([a, b]);
    if (S.mirror) {
      const ma = mirrorPartner(a) ?? (Math.abs(sc.nodes[a].p[0]) < 0.01 ? a : null);
      const mb = mirrorPartner(b) ?? (Math.abs(sc.nodes[b].p[0]) < 0.01 ? b : null);
      if (ma !== null && mb !== null) want.push([ma, mb]);
    }
  }
  for (const [a, b] of want) {
    const key = a < b ? a + ':' + b : b + ':' + a;
    if (a === b || seen.has(key) || beamExists(a, b)) continue;
    seen.add(key);
    const [o, i] = opBeamAdd(part, sc.nodes[a].id, sc.nodes[b].id);
    ops.push(o); inv.unshift(i);
  }
  if (!ops.length) { toast('Those nodes are already connected.'); return 0; }
  commit(label || (ops.length === 1 ? 'Add beam' : `Add ${ops.length} beams`), ops, inv);
  return ops.length;
}

function clickBeam(x, y) {
  const hit = pickNode(x, y);
  if (hit < 0) { chain = []; setOverlay([]); updateHint(); return; }
  if (chain.length && chain[0] !== hit) {
    connect([[chain[0], hit]]);
  }
  chain = [hit];
  clearSelection();
  S.sel.nodes.add(hit);
  emit('selection');
  updateHint('Now click the node to join it to. Esc to stop.');
}

export function makeTriangle(ids) {
  if (!requireEditable()) return;
  const sc = S.scene, part = S.activePart;
  const ops = [], inv = [];
  const add = pair => { ops.push(pair[0]); inv.unshift(pair[1]); };
  add(opTriAdd(part, ids.map(i => sc.nodes[i].id)));
  if (S.mirror) {
    const m = ids.map(i => mirrorPartner(i) ?? (Math.abs(sc.nodes[i].p[0]) < 0.01 ? i : null));
    if (m.every(x => x !== null) && m.some((x, k) => x !== ids[k])) {
      // mirroring flips the winding, so swap two corners to keep it facing out
      add(opTriAdd(part, [m[0], m[2], m[1]].map(i => sc.nodes[i].id)));
    }
  }
  commit(ops.length > 1 ? 'Add 2 triangles' : 'Add triangle', ops, inv);
}

function clickTri(x, y) {
  const hit = pickNode(x, y);
  if (hit < 0) { chain = []; setOverlay([]); updateHint(); return; }
  if (chain.includes(hit)) return;
  chain.push(hit);
  clearSelection();
  for (const i of chain) S.sel.nodes.add(i);
  emit('selection');
  if (chain.length === 3) {
    makeTriangle(chain);
    chain = [];
    setOverlay([]);
    updateHint();
  } else {
    updateHint(chain.length === 1 ? 'Click the second corner.' : 'Click the third corner.');
  }
}

// ---- gizmo dragging -----------------------------------------------------------------
function gizmoStart() {
  if (V.importObj) return;
  const sc = S.scene;
  const sel = [...S.sel.nodes].filter(i => !sc.nodes[i].del);
  const locked = sel.filter(i => !sc.files[sc.nodes[i].f]?.editable);
  const movable = sel.filter(i => sc.files[sc.nodes[i].f]?.editable);
  if (locked.length) toast(`${locked.length} selected node(s) are in read-only parts and won't move.`);
  const set = new Map();
  for (const i of movable) set.set(i, 1);
  if (S.mirror) {
    for (const i of movable) {
      const j = mirrorPartner(i);
      if (j !== null && !set.has(j) && sc.files[sc.nodes[j].f]?.editable) set.set(j, -1);
    }
  }
  dragStart = {
    pivot: V.pivot.position.clone(),
    nodes: [...set.entries()].map(([i, sx]) => ({ i, sx, p: sc.nodes[i].p.slice(), raw: sc.nodes[i].raw.slice(),
      center: S.mirror && Math.abs(sc.nodes[i].p[0]) < 0.005 && mirrorName(sc.nodes[i].id) === null })),
  };
}

function gizmoMoved() {
  if (!dragStart) return;
  const sc = S.scene;
  const d = V.pivot.position.clone().sub(dragStart.pivot);
  for (const n of dragStart.nodes) {
    const dx = n.center ? 0 : d.x * n.sx;
    const node = sc.nodes[n.i];
    node.p = [n.p[0] + dx, n.p[1] + d.y, n.p[2] + d.z];
    node.raw = toRaw(node.xf, node.p);
  }
  refreshPositions();
  emit('dragging');
}

function gizmoEnd() {
  if (!dragStart) return;
  const sc = S.scene;
  const ops = [], inv = [];
  for (const n of dragStart.nodes) {
    const node = sc.nodes[n.i];
    const moved = node.p.some((v, k) => Math.abs(v - n.p[k]) > 1e-6);
    if (!moved) continue;
    const newRaw = node.raw.slice();
    node.raw = n.raw;          // so the inverse remembers the old position
    const [o, i] = opNodeMove(node, newRaw);
    node.raw = newRaw;
    node.rawCells = newRaw.slice();
    ops.push(o); inv.push(i);
  }
  dragStart = null;
  if (ops.length) commit(ops.length === 1 ? 'Move node' : `Move ${ops.length} nodes`, ops, inv, { applied: true });
}

// ---- keyboard actions used by main.js ------------------------------------------------
export function deleteSelection() {
  const sc = S.scene;
  const ops = [], inv = [];
  const add = pair => { ops.push(pair[0]); inv.unshift(pair[1]); };
  const nodes = new Set([...S.sel.nodes].filter(i => !sc.nodes[i].del));
  if (S.mirror) for (const i of [...nodes]) { const j = mirrorPartner(i); if (j !== null) nodes.add(j); }
  const ro = [...nodes].filter(i => !sc.files[sc.nodes[i].f]?.editable);
  if (ro.length) return toast(`Can't delete: ${ro.length} node(s) are in read-only parts.`, 'err');
  // beams and triangles that use the nodes go too
  const beams = new Set([...S.sel.beams].filter(i => !sc.beams[i].del));
  const tris = new Set([...S.sel.tris].filter(i => !sc.tris[i].del));
  for (const i of nodes) for (const b of sc.beamsOf[i]) beams.add(b);
  sc.tris.forEach((t, k) => { if (!t.del && t.n.some(i => nodes.has(i))) tris.add(k); });
  const blocked = [...beams].filter(k => !sc.files[sc.beams[k].f]?.editable).length +
    [...tris].filter(k => !sc.files[sc.tris[k].f]?.editable).length;
  if (blocked && nodes.size) return toast(`Can't delete: read-only parts still use these nodes (${blocked} beams/triangles).`, 'err');
  const doneTris = new Set();
  for (const k of tris) {
    const t = sc.tris[k];
    if (!sc.files[t.f]?.editable) continue;
    const key = t.part + '|' + t.rk.join('|');
    if (doneTris.has(key)) continue;
    doneTris.add(key);
    add(opTriDel(t));
  }
  for (const k of beams) if (sc.files[sc.beams[k].f]?.editable) add(opBeamDel(sc.beams[k]));
  for (const i of nodes) add(opNodeDel(sc.nodes[i]));
  if (!ops.length) return toast('Nothing editable selected to delete.');
  commit(`Delete ${nodes.size ? nodes.size + ' nodes' : ''}${nodes.size && beams.size ? ', ' : ''}${beams.size ? beams.size + ' beams' : ''}${tris.size && !nodes.size ? (beams.size ? ', ' : '') + doneTris.size + ' triangles' : ''}`, ops, inv);
  clearSelection();
  emit('selection');
}

export function flipSelectedTris() {
  const sc = S.scene;
  const ops = [], inv = [];
  const done = new Set();
  for (const k of S.sel.tris) {
    const t = sc.tris[k];
    if (t.del || !sc.files[t.f]?.editable) continue;
    const key = t.part + '|' + t.rk.join('|');
    if (done.has(key)) continue;
    done.add(key);
    const [o, i] = opTriFlip(t);
    ops.push(o); inv.unshift(i);
  }
  if (!ops.length) return toast('Select triangles in a part you can edit, then press I to flip them.');
  commit(ops.length === 1 ? 'Flip triangle' : `Flip ${ops.length} triangles`, ops, inv);
}

export function connectSelected(all = false) {
  const sel = [...S.sel.nodes].filter(i => !S.scene.nodes[i].del);
  if (sel.length < 2) return false;
  const pairs = [];
  if (all) { for (let a = 0; a < sel.length; a++) for (let b = a + 1; b < sel.length; b++) pairs.push([sel[a], sel[b]]); }
  else for (let k = 1; k < sel.length; k++) pairs.push([sel[k - 1], sel[k]]);
  connect(pairs);
  return true;
}

export function escape() {
  chain = [];
  setOverlay([]);
  if (S.tool !== 'select') setTool('select');
  else { clearSelection(); emit('selection'); }
  updateHint();
}

export function frameSelection() {
  const sc = S.scene;
  const pts = [...S.sel.nodes].map(i => sc.nodes[i].p);
  for (const k of S.sel.beams) pts.push(sc.nodes[sc.beams[k].a].p, sc.nodes[sc.beams[k].b].p);
  for (const k of S.sel.tris) for (const i of sc.tris[k].n) pts.push(sc.nodes[i].p);
  if (pts.length) frame(pts);
  else frame(sc.nodes.filter(n => !n.del).map(n => n.p));
}
