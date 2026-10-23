// The 3D Model Workshop: model a part right on the vehicle, then make it a BeamNG
// part with a fitted structure. Shapes, object/vertex/edge/face selection, move,
// rotate and scale with the gizmo, extrude, inset, subdivide, mirror, colours,
// undo, projects and .obj export. Geometry is in polymesh.js.
import * as THREE from 'three';
import { TransformControls } from 'three/addons/controls/TransformControls.js';
import { S, A } from './state.js';
import { api } from './api.js';
import { V, frame } from './view3d.js';
import * as P from './polymesh.js';
import { partForm } from './partform.js';
import { icon } from './icons.js';
import { $, h, fmt, toast, debounce, numInput, openModal, closeModal, askText } from './util.js';

const W = {
  objects: [],        // { id, name, mesh: {v, f}, color, paint, visible, shape: {type, params, center} | null }
  selObjs: new Set(), // selected object ids (object mode); the first is the active one
  mode: 'object',     // object | vertex | edge | face
  sel: new Set(),     // selected elements of the active object
  tool: 'translate',  // translate | rotate | scale
  snap: 0.01,
  undo: [], redo: [],
  project: null, dirty: false,
  group: null, gizmo: null, pivot: null,
  draw: new Map(),    // id -> { mesh, lines }
  extrudeBy: 0.1,
  making: false,
};
let nextId = 1;

const SHAPE_DEFS = {
  box: { title: 'Box', icon: 'box', params: { w: 0.6, l: 0.6, h: 0.3, segX: 1, segY: 1, segZ: 1 } },
  cylinder: { title: 'Cylinder', icon: 'cylinder', params: { r: 0.15, h: 0.6, sides: 20, axis: 'x' } },
  sphere: { title: 'Sphere', icon: 'sphere', params: { r: 0.2, seg: 20, rings: 12 } },
  cone: { title: 'Cone', icon: 'cone', params: { r1: 0.2, r2: 0.05, h: 0.4, sides: 20, axis: 'z' } },
  wedge: { title: 'Wedge', icon: 'wedge', params: { w: 0.6, l: 0.6, h: 0.3 } },
  tube: { title: 'Tube', icon: 'tube', params: { r: 0.03, t: 0.006, h: 1.0, sides: 12, axis: 'x' } },
  plane: { title: 'Panel', icon: 'plane', params: { w: 0.8, l: 0.8, segX: 2, segY: 2 } },
  torus: { title: 'Ring', icon: 'target', params: { R: 0.2, r: 0.04, seg: 24, sides: 10, axis: 'y' } },
};
const PARAM_INFO = {
  w: ['Width (left-right)', 0.01, 4, 0.01], l: ['Length (front-back)', 0.01, 6, 0.01], h: ['Height', 0.01, 4, 0.01],
  r: ['Radius', 0.005, 2, 0.005], r1: ['Bottom radius', 0, 2, 0.005], r2: ['Top radius', 0, 2, 0.005], R: ['Ring radius', 0.02, 2, 0.005],
  t: ['Wall thickness', 0.001, 0.5, 0.001], sides: ['Sides', 3, 64, 1], seg: ['Segments', 3, 64, 1], rings: ['Rings', 2, 32, 1],
  segX: ['Cuts across', 1, 12, 1], segY: ['Cuts along', 1, 12, 1], segZ: ['Cuts up', 1, 12, 1],
};
const COLOURS = ['#9aa0a8', '#2b2e33', '#d8d8d8', '#c0392b', '#e67e22', '#f1c40f', '#27ae60', '#2980b9', '#8e44ad', '#7f5539'];

// ---- registration -----------------------------------------------------------------------------------
export function register(MODES) {
  MODES.model = { enter, leave, render, key, frame: frameObjects };
}

function ensureScene() {
  if (W.group) return;
  W.group = new THREE.Group();
  V.scene.add(W.group);
  W.pivot = new THREE.Object3D();
  V.scene.add(W.pivot);
  W.gizmo = new TransformControls(V.camera, V.renderer.domElement);
  W.gizmo.setSize(0.85);
  W.gizmo.addEventListener('dragging-changed', e => { V.controls.enabled = !e.value; if (e.value) dragStart(); else dragEnd(); });
  W.gizmo.addEventListener('objectChange', dragMove);
  W.gizmo.addEventListener('change', () => { V.dirty = true; });
  V.scene.add(W.gizmo.getHelper ? W.gizmo.getHelper() : W.gizmo);
  const el = V.renderer.domElement;
  el.addEventListener('pointerdown', onDown);
  el.addEventListener('pointermove', onMove);
  el.addEventListener('pointerup', onUp);
  try {
    const saved = JSON.parse(localStorage.getItem('jbe.workshop.draft') || 'null');
    if (saved?.objects?.length && !W.objects.length) loadData(saved, false);
  } catch { /* no draft */ }
}

function enter() {
  ensureScene();
  W.group.visible = true;
  S.modeHint = 'Workshop: left-click to select, drag for a box, right-drag to turn the view. Keys: 1 object, 2 vertex, 3 edge, 4 face, G move, R rotate, S scale, E extrude, Del delete.';
  rebuildAll();
}

function leave() {
  if (W.making) cancelMake();
  W.gizmo?.detach();
  S.modeHint = null;
  if (W.group) W.group.visible = !!W.objects.length;   // keep showing your model in the other modes
  V.dirty = true;
}

// ---- undo -------------------------------------------------------------------------------------------------
const snap = () => JSON.stringify({ objects: W.objects, sel: [...W.selObjs] });
function remember(label) {
  W.undo.push({ label, state: snap() });
  if (W.undo.length > 80) W.undo.shift();
  W.redo = [];
  W.dirty = true;
  saveDraftSoon();
}
function restore(state) {
  const s = JSON.parse(state);
  W.objects = s.objects;
  W.selObjs = new Set(s.sel);
  W.sel.clear();
  rebuildAll();
}
function undo() {
  const r = W.undo.pop();
  if (!r) return toast('Nothing to undo');
  W.redo.push({ label: r.label, state: snap() });
  restore(r.state);
  toast('Undid: ' + r.label);
}
function redo() {
  const r = W.redo.pop();
  if (!r) return toast('Nothing to redo');
  W.undo.push({ label: r.label, state: snap() });
  restore(r.state);
  toast('Redid: ' + r.label);
}
const saveDraftSoon = debounce(() => {
  try { localStorage.setItem('jbe.workshop.draft', JSON.stringify(projectData())); } catch { /* full or private */ }
}, 800);

// ---- objects ------------------------------------------------------------------------------------------------
const byId = id => W.objects.find(o => o.id === id);
const active = () => byId([...W.selObjs][0]);

function addShape(type) {
  const def = SHAPE_DEFS[type];
  const t = V.controls.target;
  const center = [0, +t.y.toFixed(3), +Math.max(0.2, t.z).toFixed(3)];
  remember('Add ' + def.title);
  const o = { id: nextId++, name: def.title + ' ' + (W.objects.length + 1), mesh: null, color: COLOURS[W.objects.length % COLOURS.length],
    paint: false, visible: true, shape: { type, params: { ...def.params }, center } };
  o.mesh = shapeMesh(o.shape);
  W.objects.push(o);
  W.selObjs = new Set([o.id]);
  W.mode = 'object';
  W.sel.clear();
  rebuildAll();
}

function shapeMesh(sh) {
  const p = sh.params;
  const center = sh.center;
  switch (sh.type) {
    case 'box': return P.box({ w: p.w, l: p.l, h: p.h, seg: [p.segX, p.segY, p.segZ], center });
    case 'plane': return P.plane({ w: p.w, l: p.l, seg: [p.segX, p.segY], center });
    case 'cylinder': return P.cylinder({ r: p.r, h: p.h, sides: p.sides, axis: p.axis, center });
    case 'cone': return P.cone({ r1: p.r1, r2: p.r2, h: p.h, sides: p.sides, axis: p.axis, center });
    case 'sphere': return P.sphere({ r: p.r, seg: p.seg, rings: p.rings, center });
    case 'wedge': return P.wedge({ w: p.w, l: p.l, h: p.h, center });
    case 'tube': return P.tube({ r: p.r, t: p.t, h: p.h, sides: p.sides, axis: p.axis, center });
    case 'torus': return P.torus({ R: p.R, r: p.r, seg: p.seg, sides: p.sides, axis: p.axis, center });
    default: return P.box({ center });
  }
}

// change an object's mesh (it stops being an editable shape)
function setMesh(o, mesh, keepShape = false) {
  o.mesh = mesh;
  if (!keepShape) o.shape = null;
}

// ---- drawing ------------------------------------------------------------------------------------------------
const MAT_CACHE = new Map();
function matFor(o, selected) {
  const key = o.color + (selected ? 's' : '');
  if (!MAT_CACHE.has(key)) {
    MAT_CACHE.set(key, new THREE.MeshStandardMaterial({ color: new THREE.Color(o.color), roughness: 0.55, metalness: 0.15,
      flatShading: true, side: THREE.DoubleSide, emissive: selected ? new THREE.Color(0x3a2408) : new THREE.Color(0x000000),
      polygonOffset: true, polygonOffsetFactor: 1, polygonOffsetUnits: 1 }));
  }
  return MAT_CACHE.get(key);
}
const EDGE_MAT = new THREE.LineBasicMaterial({ color: 0x15171c, transparent: true, opacity: 0.6 });
const EDGE_ACTIVE = new THREE.LineBasicMaterial({ color: 0xffc27a, transparent: true, opacity: 0.85 });

function rebuildObject(o) {
  const old = W.draw.get(o.id);
  if (old) { W.group.remove(old.mesh, old.lines); old.mesh.geometry.dispose(); old.lines.geometry.dispose(); }
  const t = P.triangulate(o.mesh);
  const g = new THREE.BufferGeometry();
  g.setAttribute('position', new THREE.BufferAttribute(t.pos, 3));
  g.setIndex(new THREE.BufferAttribute(t.idx, 1));
  g.computeVertexNormals();
  const sel = W.selObjs.has(o.id);
  const mesh = new THREE.Mesh(g, matFor(o, sel && W.mode === 'object'));
  mesh.userData = { id: o.id, faceOf: t.faceOf };
  mesh.visible = o.visible;
  const e = P.edges(o.mesh);
  const lp = new Float32Array(e.length * 6);
  e.forEach(([a, b], k) => { lp.set(o.mesh.v[a], k * 6); lp.set(o.mesh.v[b], k * 6 + 3); });
  const lg = new THREE.BufferGeometry();
  lg.setAttribute('position', new THREE.BufferAttribute(lp, 3));
  const lines = new THREE.LineSegments(lg, sel && W.mode !== 'object' ? EDGE_ACTIVE : EDGE_MAT);
  lines.visible = o.visible;
  W.group.add(mesh, lines);
  W.draw.set(o.id, { mesh, lines });
}

function rebuildAll() {
  for (const [id, d] of W.draw) if (!byId(id)) { W.group.remove(d.mesh, d.lines); d.mesh.geometry.dispose(); d.lines.geometry.dispose(); W.draw.delete(id); }
  for (const o of W.objects) rebuildObject(o);
  drawSelection();
  placeGizmo();
  render();
  V.dirty = true;
}

let overlay = null;
function drawSelection() {
  if (overlay) { W.group.remove(overlay); overlay.traverse(c => c.geometry?.dispose()); overlay = null; }
  const o = active();
  if (!o || W.mode === 'object') { V.dirty = true; return; }
  overlay = new THREE.Group();
  const m = o.mesh;
  if (W.mode === 'vertex') {
    const pos = new Float32Array(m.v.flat()), col = new Float32Array(m.v.length * 3);
    m.v.forEach((_, i) => { const c = W.sel.has(i) ? [1, 0.65, 0.2] : [0.9, 0.92, 0.96]; col.set(c, i * 3); });
    const g = new THREE.BufferGeometry();
    g.setAttribute('position', new THREE.BufferAttribute(pos, 3));
    g.setAttribute('color', new THREE.BufferAttribute(col, 3));
    overlay.add(new THREE.Points(g, new THREE.PointsMaterial({ size: 8, sizeAttenuation: false, vertexColors: true, depthTest: false })));
  }
  if (W.mode === 'edge' && W.sel.size) {
    const list = [...W.sel].map(k => k.split(',').map(Number));
    const lp = new Float32Array(list.length * 6);
    list.forEach(([a, b], k) => { lp.set(m.v[a], k * 6); lp.set(m.v[b], k * 6 + 3); });
    const g = new THREE.BufferGeometry();
    g.setAttribute('position', new THREE.BufferAttribute(lp, 3));
    overlay.add(new THREE.LineSegments(g, new THREE.LineBasicMaterial({ color: 0xffa733, depthTest: false })));
  }
  if (W.mode === 'face' && W.sel.size) {
    const sub = { v: m.v, f: [...W.sel].map(i => m.f[i]).filter(Boolean) };
    const t = P.triangulate(sub);
    const g = new THREE.BufferGeometry();
    g.setAttribute('position', new THREE.BufferAttribute(t.pos, 3));
    g.setIndex(new THREE.BufferAttribute(t.idx, 1));
    overlay.add(new THREE.Mesh(g, new THREE.MeshBasicMaterial({ color: 0xffa733, transparent: true, opacity: 0.45, side: THREE.DoubleSide, depthTest: false })));
  }
  overlay.renderOrder = 20;
  W.group.add(overlay);
  V.dirty = true;
}

// ---- selection centre and the gizmo -----------------------------------------------------------------------------
function selectedVerts() {
  const o = active();
  if (!o) return null;
  if (W.mode === 'object') return null;
  return P.vertsOf(o.mesh, W.mode, W.sel);
}

function selectionCenter() {
  const pts = [];
  if (W.mode === 'object') { for (const id of W.selObjs) { const o = byId(id); if (o) pts.push(P.bounds(o.mesh).center); } }
  else { const o = active(); const vs = selectedVerts(); if (o && vs) for (const i of vs) pts.push(o.mesh.v[i]); }
  if (!pts.length) return null;
  const c = [0, 0, 0];
  for (const p of pts) for (let k = 0; k < 3; k++) c[k] += p[k] / pts.length;
  return c;
}

function placeGizmo() {
  if (!W.gizmo) return;
  const c = selectionCenter();
  if (!c || S.mode !== 'model' || W.making) { W.gizmo.detach(); V.dirty = true; return; }
  if (!W.gizmo.dragging) { W.pivot.position.set(...c); W.pivot.quaternion.identity(); W.pivot.scale.set(1, 1, 1); }
  W.gizmo.setMode(W.tool);
  W.gizmo.setTranslationSnap(W.snap || null);
  W.gizmo.setRotationSnap(THREE.MathUtils.degToRad(5));
  W.gizmo.attach(W.pivot);
  V.dirty = true;
}

let drag = null;
function dragStart() {
  const targets = W.mode === 'object' ? [...W.selObjs].map(byId).filter(Boolean) : [active()].filter(Boolean);
  if (!targets.length) return;
  drag = {
    before: snap(),
    start: W.pivot.matrixWorld.clone(),
    items: targets.map(o => ({ o, v: o.mesh.v.map(p => p.slice()), verts: W.mode === 'object' ? null : selectedVerts() })),
  };
}
function dragMove() {
  if (!drag) return;
  W.pivot.updateMatrixWorld(true);
  const T = W.pivot.matrixWorld.clone().multiply(drag.start.clone().invert());
  const v = new THREE.Vector3();
  for (const it of drag.items) {
    const idx = it.verts ? it.verts : it.v.map((_, i) => i);
    for (const i of idx) { v.set(...it.v[i]).applyMatrix4(T); it.o.mesh.v[i] = [v.x, v.y, v.z]; }
    if (it.o.shape && W.mode === 'object' && W.tool === 'translate') {
      it.o.shape.center = P.bounds(it.o.mesh).center;
    } else if (it.o.shape) it.o.shape = null;
    rebuildObject(it.o);
  }
  drawSelection();
  renderSide();
}
function dragEnd() {
  if (!drag) return;
  W.undo.push({ label: W.tool === 'translate' ? 'Move' : W.tool === 'rotate' ? 'Rotate' : 'Scale', state: drag.before });
  W.redo = [];
  W.dirty = true;
  drag = null;
  saveDraftSoon();
  placeGizmo();
  render();
}

// ---- picking ---------------------------------------------------------------------------------------------------------
const ray = new THREE.Raycaster();
let down = null;
function local(e) { const r = V.renderer.domElement.getBoundingClientRect(); return [e.clientX - r.left, e.clientY - r.top]; }
function ndc(x, y) { const r = V.renderer.domElement.getBoundingClientRect(); return new THREE.Vector2(x / r.width * 2 - 1, -(y / r.height) * 2 + 1); }
function screen(p) {
  const v = new THREE.Vector3(...p).project(V.camera);
  const r = V.renderer.domElement.getBoundingClientRect();
  return [(v.x + 1) / 2 * r.width, (1 - v.y) / 2 * r.height, v.z];
}

function onDown(e) {
  if (S.mode !== 'model' || W.making || e.button !== 0 || e.altKey) return;
  if (W.gizmo.axis) return;
  const [x, y] = local(e);
  down = { x, y, shift: e.shiftKey, ctrl: e.ctrlKey || e.metaKey, moved: false };
}
function onMove(e) {
  if (!down || S.mode !== 'model') return;
  const [x, y] = local(e);
  if (Math.hypot(x - down.x, y - down.y) > 5) down.moved = true;
  if (down.moved) {
    const b = $('#boxsel');
    b.classList.remove('hidden');
    Object.assign(b.style, { left: Math.min(x, down.x) + 'px', top: Math.min(y, down.y) + 'px', width: Math.abs(x - down.x) + 'px', height: Math.abs(y - down.y) + 'px' });
  }
}
function onUp(e) {
  if (!down || S.mode !== 'model') return;
  const [x, y] = local(e);
  const d = down;
  down = null;
  $('#boxsel').classList.add('hidden');
  if (d.moved) boxSelect(d, x, y); else clickSelect(d, x, y);
}

function hitObject(x, y) {
  ray.setFromCamera(ndc(x, y), V.camera);
  const meshes = [...W.draw.values()].map(d => d.mesh).filter(m => m.visible);
  const hits = ray.intersectObjects(meshes, false);
  return hits[0] || null;
}

function clickSelect(d, x, y) {
  const add = d.shift || d.ctrl;
  if (W.mode === 'object' || !active()) {
    const hit = hitObject(x, y);
    if (!add) W.selObjs.clear();
    if (hit) {
      const id = hit.object.userData.id;
      if (d.ctrl && W.selObjs.has(id)) W.selObjs.delete(id); else { W.selObjs.delete(id); W.selObjs = new Set([id, ...W.selObjs]); }
    }
    W.sel.clear();
    return rebuildAll();
  }
  const o = active();
  let picked = null;
  if (W.mode === 'face') {
    const hit = hitObject(x, y);
    if (hit && hit.object.userData.id === o.id) picked = hit.object.userData.faceOf[hit.faceIndex];
  } else if (W.mode === 'vertex') {
    let best = 100;
    o.mesh.v.forEach((p, i) => { const [sx, sy, sz] = screen(p); const dd = (sx - x) ** 2 + (sy - y) ** 2; if (sz < 1 && dd < best) { best = dd; picked = i; } });
  } else if (W.mode === 'edge') {
    let best = 7;
    for (const [a, b] of P.edges(o.mesh)) {
      const A = screen(o.mesh.v[a]), B = screen(o.mesh.v[b]);
      if (A[2] > 1 || B[2] > 1) continue;
      const dx = B[0] - A[0], dy = B[1] - A[1], L = dx * dx + dy * dy;
      const t = L ? Math.max(0, Math.min(1, ((x - A[0]) * dx + (y - A[1]) * dy) / L)) : 0;
      const dd = Math.hypot(A[0] + t * dx - x, A[1] + t * dy - y);
      if (dd < best) { best = dd; picked = a + ',' + b; }
    }
  }
  if (!add) W.sel.clear();
  if (picked !== null) { if (d.ctrl && W.sel.has(picked)) W.sel.delete(picked); else W.sel.add(picked); }
  drawSelection(); placeGizmo(); render();
}

function boxSelect(d, x, y) {
  const [ax, bx] = [Math.min(d.x, x), Math.max(d.x, x)], [ay, by] = [Math.min(d.y, y), Math.max(d.y, y)];
  const inBox = p => { const [sx, sy, sz] = screen(p); return sz <= 1 && sx >= ax && sx <= bx && sy >= ay && sy <= by; };
  const add = d.shift || d.ctrl;
  if (W.mode === 'object' || !active()) {
    if (!add) W.selObjs.clear();
    for (const o of W.objects) if (o.visible && inBox(P.bounds(o.mesh).center)) W.selObjs.add(o.id);
    W.sel.clear();
    return rebuildAll();
  }
  const o = active();
  if (!add) W.sel.clear();
  if (W.mode === 'vertex') o.mesh.v.forEach((p, i) => { if (inBox(p)) W.sel.add(i); });
  if (W.mode === 'face') o.mesh.f.forEach((f, i) => { if (inBox(P.faceCenter(o.mesh, f))) W.sel.add(i); });
  if (W.mode === 'edge') for (const [a, b] of P.edges(o.mesh)) { if (inBox(o.mesh.v[a]) && inBox(o.mesh.v[b])) W.sel.add(a + ',' + b); }
  drawSelection(); placeGizmo(); render();
}

// ---- operations ------------------------------------------------------------------------------------------------------
function setMode(m) {
  if (m !== 'object' && !active()) { toast('Select a shape first, then edit its ' + m + 's.'); return; }
  W.mode = m;
  W.sel.clear();
  rebuildAll();
}
function setTool(t) { W.tool = t; placeGizmo(); render(); }

function needFaces() {
  if (W.mode !== 'face' || !W.sel.size) { toast('Switch to Face (4) and select faces first.'); return false; }
  return true;
}

const OPS = {
  extrude() {
    if (!needFaces()) return;
    const o = active();
    remember('Extrude');
    const { mesh, faces } = P.extrude(o.mesh, [...W.sel], W.extrudeBy);
    setMesh(o, mesh);
    W.sel = new Set(faces);
    setTool('translate');
    rebuildAll();
    toast('Extruded. Drag the arrows to pull it further.');
  },
  inset() {
    if (!needFaces()) return;
    const o = active();
    remember('Inset');
    const { mesh, faces } = P.inset(o.mesh, [...W.sel], 0.2);
    setMesh(o, mesh);
    W.sel = new Set(faces);
    rebuildAll();
  },
  subdivide() {
    const o = active();
    if (!o) return toast('Select a shape first.');
    remember('Subdivide');
    setMesh(o, P.subdivide(o.mesh, W.mode === 'face' && W.sel.size ? [...W.sel] : null));
    W.sel.clear();
    rebuildAll();
  },
  remove() {
    if (W.mode === 'object') {
      if (!W.selObjs.size) return;
      remember('Delete');
      W.objects = W.objects.filter(o => !W.selObjs.has(o.id));
      W.selObjs.clear();
      return rebuildAll();
    }
    const o = active();
    if (!o || !W.sel.size) return;
    remember('Delete');
    if (W.mode === 'face') setMesh(o, P.deleteFaces(o.mesh, [...W.sel]));
    else if (W.mode === 'vertex') setMesh(o, P.deleteVerts(o.mesh, [...W.sel]));
    else {
      const keys = new Set(W.sel);
      const drop = o.mesh.f.map((f, i) => [f, i]).filter(([f]) => f.some((a, k) => { const b = f[(k + 1) % f.length]; return keys.has(Math.min(a, b) + ',' + Math.max(a, b)); })).map(([, i]) => i);
      setMesh(o, P.deleteFaces(o.mesh, drop));
    }
    W.sel.clear();
    rebuildAll();
  },
  duplicate() {
    const list = W.mode === 'object' ? [...W.selObjs].map(byId) : [active()];
    if (!list.filter(Boolean).length) return;
    remember('Duplicate');
    const made = [];
    for (const o of list.filter(Boolean)) {
      const c = JSON.parse(JSON.stringify(o));
      c.id = nextId++;
      c.name = o.name + ' copy';
      c.mesh = P.transform(c.mesh, c.mesh.v.map((_, i) => i), p => [p[0], p[1] + 0.1, p[2]]);
      if (c.shape) c.shape.center = P.bounds(c.mesh).center;
      W.objects.push(c);
      made.push(c.id);
    }
    W.mode = 'object';
    W.selObjs = new Set(made);
    W.sel.clear();
    rebuildAll();
  },
  mirrorCopy() {
    const list = [...W.selObjs].map(byId).filter(Boolean);
    if (!list.length) return toast('Select shapes to mirror to the other side.');
    remember('Mirror copy');
    for (const o of list) {
      const c = JSON.parse(JSON.stringify(o));
      c.id = nextId++;
      c.name = o.name + ' (other side)';
      c.mesh = P.flip(P.transform(c.mesh, c.mesh.v.map((_, i) => i), p => [-p[0], p[1], p[2]]));
      if (c.shape) c.shape = null;
      W.objects.push(c);
    }
    rebuildAll();
  },
  symmetrize() {
    const o = active();
    if (!o) return toast('Select a shape first.');
    remember('Make symmetric');
    const keep = P.deleteFaces(o.mesh, o.mesh.f.map((f, i) => [i, P.faceCenter(o.mesh, f)[0]]).filter(([, x]) => x < -1e-6).map(([i]) => i));
    setMesh(o, P.mirrorX(keep));
    W.sel.clear();
    rebuildAll();
    toast('The right side (−X) is now a mirror of the left.');
  },
  flip() {
    const o = active();
    if (!o) return;
    remember('Flip');
    setMesh(o, P.flip(o.mesh, W.mode === 'face' && W.sel.size ? [...W.sel] : null), true);
    rebuildAll();
  },
  weld() {
    const o = active();
    if (!o) return;
    remember('Weld');
    const n = o.mesh.v.length;
    setMesh(o, P.weld(o.mesh, 0.002));
    W.sel.clear();
    rebuildAll();
    toast(`Merged ${n - o.mesh.v.length} points.`);
  },
  join() {
    const list = [...W.selObjs].map(byId).filter(Boolean);
    if (list.length < 2) return toast('Select two or more shapes to join (Shift+click).');
    remember('Join');
    let m = list[0].mesh;
    for (const o of list.slice(1)) m = P.join(m, o.mesh);
    list[0].mesh = m;
    list[0].shape = null;
    W.objects = W.objects.filter(o => !list.slice(1).includes(o));
    W.selObjs = new Set([list[0].id]);
    rebuildAll();
  },
  centre() {
    const list = W.mode === 'object' ? [...W.selObjs].map(byId).filter(Boolean) : [];
    if (!list.length) return toast('Select shapes to centre.');
    remember('Centre');
    for (const o of list) {
      const c = P.bounds(o.mesh).center;
      o.mesh = P.transform(o.mesh, o.mesh.v.map((_, i) => i), p => [p[0] - c[0], p[1], p[2]]);
      if (o.shape) o.shape.center = P.bounds(o.mesh).center;
    }
    rebuildAll();
  },
  selectAll() {
    if (W.mode === 'object') W.selObjs = new Set(W.objects.map(o => o.id));
    else {
      const o = active();
      if (!o) return;
      W.sel = new Set(W.mode === 'vertex' ? o.mesh.v.map((_, i) => i) : W.mode === 'face' ? o.mesh.f.map((_, i) => i) : P.edges(o.mesh).map(e => e.join(',')));
    }
    rebuildAll();
  },
};

// ---- keyboard ----------------------------------------------------------------------------------------------------------
function key(e) {
  const k = e.key.toLowerCase();
  const ctrl = e.ctrlKey || e.metaKey;
  if (W.making) return false;
  if (ctrl && k === 'z' && !e.shiftKey) { undo(); return true; }
  if (ctrl && (k === 'y' || (k === 'z' && e.shiftKey))) { redo(); return true; }
  if (ctrl) return false;
  if (e.shiftKey && k === 'd') { OPS.duplicate(); return true; }
  switch (k) {
    case '1': setMode('object'); return true;
    case '2': setMode('vertex'); return true;
    case '3': setMode('edge'); return true;
    case '4': setMode('face'); return true;
    case 'tab': setMode(W.mode === 'object' ? 'face' : 'object'); return true;
    case 'g': setTool('translate'); return true;
    case 'r': setTool('rotate'); return true;
    case 's': setTool('scale'); return true;
    case 'e': OPS.extrude(); return true;
    case 'i': OPS.inset(); return true;
    case 'a': OPS.selectAll(); return true;
    case 'delete': case 'backspace': case 'x': OPS.remove(); return true;
    case 'escape': W.sel.clear(); if (W.mode === 'object') W.selObjs.clear(); rebuildAll(); return true;
    case 'f': frameSelection(); return true;
    default: return false;
  }
}

function frameObjects() {
  const pts = W.objects.filter(o => o.visible).flatMap(o => { const b = P.bounds(o.mesh); return [b.lo, b.hi]; });
  if (!pts.length) return false;
  frame(pts);
  return true;
}
function frameSelection() {
  const c = selectionCenter();
  if (!c) return frameObjects();
  const pts = W.mode === 'object' ? [...W.selObjs].map(byId).filter(Boolean).flatMap(o => { const b = P.bounds(o.mesh); return [b.lo, b.hi]; }) : [...selectedVerts()].map(i => active().mesh.v[i]);
  frame(pts.length > 1 ? pts : [c, c.map(v => v + 0.2)]);
}

// ---- projects and export ------------------------------------------------------------------------------------------------
function projectData() {
  return { version: 1, objects: W.objects, model: S.model || null };
}
function loadData(data, rememberIt = true) {
  if (rememberIt) remember('Open project');
  W.objects = (data.objects || []).map(o => ({ ...o, id: nextId++ }));
  W.selObjs.clear();
  W.sel.clear();
  W.mode = 'object';
  rebuildAll();
}

async function saveProject(asNew = false) {
  let name = W.project;
  if (!name || asNew) {
    name = await askText('Save project', 'Name for this Workshop project:', W.project || (S.model ? S.model + ' part' : 'my part'), 'Save');
    if (!name) return;
  }
  try {
    const r = await api.projectSave(name, projectData());
    W.project = r.name;
    W.dirty = false;
    toast('Saved project ' + r.name, 'ok');
    render();
  } catch (e) { toast(e.message, 'err'); }
}

async function openProject() {
  try {
    const r = await api.projects();
    if (!r.projects.length) return toast('No saved projects yet.');
    const list = h('div', { class: 'optlist2' }, ...r.projects.map(p => {
      const b = h('button', { class: 'optrow' }, h('span', { class: 't' }, p.name), h('span', { class: 'd' }, new Date(p.modified * 1000).toLocaleString()));
      b.addEventListener('click', async () => {
        const d = await api.projectLoad(p.name);
        loadData(d.data);
        W.project = d.name;
        closeModal();
        frameObjects();
      });
      return b;
    }));
    openModal('Open a Workshop project', h('div', {}, list, h('div', { class: 'actions' }, h('button', { onclick: closeModal }, 'Close'))));
  } catch (e) { toast(e.message, 'err'); }
}

function exportObj() {
  const lines = ['# JBeam Editor Workshop export (Z up, metres)'];
  let base = 1;
  for (const o of W.objects.filter(x => x.visible)) {
    lines.push('o ' + o.name.replace(/\s+/g, '_'));
    for (const p of o.mesh.v) lines.push(`v ${p[0].toFixed(5)} ${p[1].toFixed(5)} ${p[2].toFixed(5)}`);
    for (const f of o.mesh.f) lines.push('f ' + f.map(i => i + base).join(' '));
    base += o.mesh.v.length;
  }
  const blob = new Blob([lines.join('\n') + '\n'], { type: 'text/plain' });
  const a = h('a', { href: URL.createObjectURL(blob), download: (W.project || 'workshop') + '.obj' });
  document.body.append(a);
  a.click();
  setTimeout(() => { URL.revokeObjectURL(a.href); a.remove(); }, 1000);
}

// ---- making the part ------------------------------------------------------------------------------------------------------
function combined(onlySelected) {
  const list = W.objects.filter(o => o.visible && (!onlySelected || W.selObjs.has(o.id)));
  if (!list.length) return null;
  const pos = [], groups = new Map();
  let base = 0;
  for (const o of list) {
    const t = P.triangulate(o.mesh);
    pos.push(...t.pos);
    const key = o.color + (o.paint ? 'p' : '');
    if (!groups.has(key)) groups.set(key, { color: [1, 3, 5].map(k => parseInt(o.color.slice(k, k + 2), 16) / 255), paint: o.paint, idx: [] });
    groups.get(key).idx.push(...[...t.idx].map(i => i + base));
    base += o.mesh.v.length;
  }
  const g = [...groups.values()].map(x => ({ color: x.color, paint: x.paint, indices: Uint32Array.from(x.idx) }));
  const idx = Uint32Array.from(g.flatMap(x => [...x.indices]));
  const all = { v: [], f: [] };
  for (const o of list) { const n = all.v.length; all.v.push(...o.mesh.v); all.f.push(...o.mesh.f.map(f => f.map(i => i + n))); }
  return { pos: Float32Array.from(pos), idx, uv: P.boxUVs(all, 1), groups: g };
}

let form = null;
function startMake() {
  if (!W.objects.some(o => o.visible)) return toast('Add a shape first.');
  if (!S.scene) return toast('Open a vehicle to put the part on.');
  W.making = true;
  W.gizmo.detach();
  render();
}
function cancelMake() {
  W.making = false;
  form?.clear();
  form = null;
  render();
  placeGizmo();
}

// ---- left and right panels ---------------------------------------------------------------------------------------------------
export function render() {
  const pane = $('#modelPane');
  if (!pane || S.mode !== 'model') return;
  pane.innerHTML = '';
  if (W.making) {
    const onlySel = W.selObjs.size > 0 && W.selObjs.size < W.objects.filter(o => o.visible).length;
    pane.append(h('div', { class: 'pane-head' }, h('h2', {}, 'Make it a part'),
      h('p', { class: 'note' }, (onlySel ? 'The selected shapes' : 'All visible shapes') + ' become one part, each shape keeping its colour.')));
    const name = (W.project || W.objects[0]?.name || 'workshop_part').replace(/[^A-Za-z0-9_]+/g, '_').toLowerCase();
    form = partForm({ container: pane, baseName: name, title: W.project || 'My part', getMesh: () => combined(onlySel), colours: false,
      onDone: () => { W.making = false; form = null; render(); toast('Your part is on the vehicle. Look at it in Parts or Structure.', 'ok'); },
      onCancel: cancelMake });
    renderSide();
    return;
  }
  pane.append(h('div', { class: 'pane-head' },
    h('h2', {}, '3D Workshop', W.project ? h('span', { class: 'badge', style: { marginLeft: '8px' } }, W.project) : null),
    h('p', { class: 'note' }, 'Model a part right on the vehicle: add shapes, then shape them by moving their faces, edges and points.')));
  const shapes = h('div', { class: 'toolgrid' }, ...Object.entries(SHAPE_DEFS).map(([k, d]) =>
    h('button', { title: 'Add a ' + d.title.toLowerCase(), onclick: () => addShape(k) }, icon(d.icon, 20), d.title)));
  const modeSeg = h('div', { class: 'seg' }, ...[['object', 'Object', '1'], ['vertex', 'Points', '2'], ['edge', 'Edges', '3'], ['face', 'Faces', '4']].map(([m, t, kk]) =>
    h('button', { class: W.mode === m ? 'active' : '', title: `${t} (${kk})`, onclick: () => setMode(m) }, t)));
  const toolSeg = h('div', { class: 'seg' }, ...[['translate', 'Move', 'move', 'G'], ['rotate', 'Rotate', 'rotate', 'R'], ['scale', 'Scale', 'scale', 'S']].map(([t, label, ic, kk]) =>
    h('button', { class: W.tool === t ? 'active' : '', title: `${label} (${kk})`, onclick: () => setTool(t) }, icon(ic, 14), label)));
  const snapSel = h('select', {}, ...[[0, 'No snap'], [0.005, '5 mm'], [0.01, '1 cm'], [0.025, '2.5 cm'], [0.05, '5 cm'], [0.1, '10 cm']].map(([v, t]) =>
    h('option', { value: v, selected: v === W.snap }, t)));
  snapSel.addEventListener('change', () => { W.snap = +snapSel.value; placeGizmo(); });
  const ex = numInput(W.extrudeBy, v => { W.extrudeBy = v; }, { digits: 3, width: '70px' });
  const opBtn = (label, ic, fn, title) => h('button', { class: 'small', title, onclick: fn }, icon(ic, 14), label);
  pane.append(
    h('h3', { class: 'sect' }, 'Add a shape'), shapes,
    h('h3', { class: 'sect' }, 'Select'), modeSeg,
    h('h3', { class: 'sect' }, 'Change'), h('div', { class: 'btnrow', style: { marginTop: 0 } }, toolSeg, snapSel),
    h('div', { class: 'btnrow' },
      opBtn('Extrude', 'extrude', OPS.extrude, 'Pull the selected faces out (E)'),
      h('span', { class: 'muted', style: { fontSize: '12px' } }, 'by'), ex, h('span', { class: 'muted', style: { fontSize: '12px' } }, 'm'),
      opBtn('Inset', 'box', OPS.inset, 'Shrink the selected faces into themselves (I)'),
      opBtn('Subdivide', 'grid', OPS.subdivide, 'Split faces into smaller ones'),
      opBtn('Delete', 'trash', OPS.remove, 'Delete the selection (Del)')),
    h('div', { class: 'btnrow' },
      opBtn('Duplicate', 'copy', OPS.duplicate, 'Shift+D'),
      opBtn('Mirror copy', 'mirror', OPS.mirrorCopy, 'A copy on the other side of the vehicle'),
      opBtn('Make symmetric', 'mirror', OPS.symmetrize, 'Make the right side a mirror of the left'),
      opBtn('Centre', 'target', OPS.centre, 'Move the selected shapes to the middle (left-right)'),
      opBtn('Join', 'link', OPS.join, 'Join the selected shapes into one'),
      opBtn('Flip faces', 'refresh', OPS.flip, 'Turn faces inside out'),
      opBtn('Weld', 'magnet', OPS.weld, 'Merge points that touch')),
    h('div', { class: 'btnrow' },
      h('button', { class: 'small', disabled: !W.undo.length, onclick: undo }, icon('undo', 14), 'Undo'),
      h('button', { class: 'small', disabled: !W.redo.length, onclick: redo }, icon('redo', 14), 'Redo')));
  // objects list
  const list = h('div', { class: 'objlist' });
  for (const o of W.objects) {
    const row = h('button', { class: 'objrow' + (W.selObjs.has(o.id) ? ' active' : '') },
      h('span', { class: 'sw', style: { background: o.color } }), h('span', { class: 'grow', style: { textAlign: 'left' } }, o.name),
      h('span', { class: 'muted', style: { fontSize: '11px' } }, o.shape ? SHAPE_DEFS[o.shape.type]?.title : `${o.mesh.f.length} faces`));
    const eye = h('span', { class: 'icon-btn', style: { width: '24px', height: '24px' }, title: o.visible ? 'Hide' : 'Show' }, icon(o.visible ? 'eye' : 'eyeOff', 14));
    eye.addEventListener('click', ev => { ev.stopPropagation(); o.visible = !o.visible; rebuildAll(); });
    row.append(eye);
    row.addEventListener('click', ev => {
      if (ev.shiftKey) { if (W.selObjs.has(o.id)) W.selObjs.delete(o.id); else W.selObjs.add(o.id); }
      else W.selObjs = new Set([o.id]);
      if (W.mode !== 'object') { W.mode = 'object'; W.sel.clear(); }
      rebuildAll();
    });
    list.append(row);
  }
  pane.append(h('h3', { class: 'sect' }, `Shapes (${W.objects.length})`), W.objects.length ? list : h('p', { class: 'note' }, 'Nothing yet: add a shape above.'));
  pane.append(h('div', { class: 'btnrow' },
    h('button', { class: 'primary', disabled: !W.objects.length, onclick: startMake }, icon('check', 15), 'Make it a part…')),
  h('div', { class: 'btnrow' },
    h('button', { class: 'small', onclick: () => saveProject(false) }, icon('save', 14), 'Save project'),
    h('button', { class: 'small', onclick: () => saveProject(true) }, 'Save as…'),
    h('button', { class: 'small', onclick: openProject }, icon('folder', 14), 'Open…'),
    h('button', { class: 'small', disabled: !W.objects.length, onclick: exportObj }, icon('export', 14), 'Export .obj'),
    h('button', { class: 'small danger', disabled: !W.objects.length, onclick: () => { remember('Clear'); W.objects = []; W.selObjs.clear(); W.project = null; rebuildAll(); } }, 'Clear')));
  renderSide();
}

function renderSide() {
  const box = $('#modelSide');
  if (!box) return;
  box.innerHTML = '';
  if (W.making) {
    box.append(h('h3', {}, 'Making a part'), h('p', { class: 'note' }, 'Orange: the nodes and beams the game will simulate. Green: the beams that bolt it to the vehicle. Select vehicle nodes in Structure mode first to choose where it is bolted on.'));
    return;
  }
  const o = active();
  if (!o) {
    box.append(h('h3', {}, 'Nothing selected'), h('p', { class: 'note' }, 'Click a shape to select it. Shift+click selects more. Drag on empty space for a box.'),
      h('div', { class: 'card' }, h('b', {}, 'Tips'),
        h('ul', { class: 'changes' },
          h('li', {}, 'The vehicle faces the front view: −Y is forward, +X is the left side, Z is up.'),
          h('li', {}, 'Build one side, then "Make symmetric" or "Mirror copy" for the other.'),
          h('li', {}, 'Face mode (4) + Extrude pulls a box out of a face: the quickest way to shape bumpers and racks.'))));
    return;
  }
  const b = P.bounds(o.mesh);
  const nameIn = h('input', { type: 'text', value: o.name });
  nameIn.addEventListener('keydown', e => e.stopPropagation());
  nameIn.addEventListener('change', () => { remember('Rename'); o.name = nameIn.value.trim() || o.name; render(); });
  const colour = h('input', { type: 'color', value: o.color });
  colour.addEventListener('input', () => { o.color = colour.value; rebuildObject(o); V.dirty = true; });
  colour.addEventListener('change', () => { remember('Colour'); render(); });
  const paint = h('input', { type: 'checkbox', checked: o.paint });
  paint.addEventListener('change', () => { remember('Paint'); o.paint = paint.checked; });
  const sw = h('div', { class: 'swatches' }, ...COLOURS.map(c => {
    const s = h('button', { class: 'swatch' + (o.color === c ? ' active' : ''), style: { background: c } });
    s.addEventListener('click', () => { remember('Colour'); o.color = c; rebuildAll(); });
    return s;
  }));
  const posIn = [0, 1, 2].map(k => numInput(b.center[k], v => {
    remember('Move');
    const d = v - P.bounds(o.mesh).center[k];
    o.mesh = P.transform(o.mesh, o.mesh.v.map((_, i) => i), p => { p[k] += d; return p; });
    if (o.shape) o.shape.center = P.bounds(o.mesh).center;
    rebuildAll();
  }, { digits: 3 }));
  const sizeIn = [0, 1, 2].map(k => numInput(b.size[k], v => {
    if (!(v > 0) || !(b.size[k] > 0)) return;
    remember('Resize');
    const bb = P.bounds(o.mesh), f = v / bb.size[k];
    o.mesh = P.transform(o.mesh, o.mesh.v.map((_, i) => i), p => { p[k] = bb.center[k] + (p[k] - bb.center[k]) * f; return p; });
    o.shape = null;
    rebuildAll();
  }, { digits: 3 }));
  box.append(h('h3', {}, o.name),
    h('div', { class: 'props' },
      h('span', { class: 'k' }, 'Name'), nameIn,
      h('span', { class: 'k' }, 'Colour'), h('span', { style: { display: 'flex', gap: '8px', alignItems: 'center' } }, colour, h('label', { class: 'chk' }, paint, 'Vehicle paint')),
      h('span', { class: 'k' }, 'Centre X Y Z'), h('span', { class: 'xyz' }, ...posIn),
      h('span', { class: 'k' }, 'Size X Y Z'), h('span', { class: 'xyz' }, ...sizeIn),
      h('span', { class: 'k' }, 'Faces'), h('span', {}, `${o.mesh.f.length} faces, ${o.mesh.v.length} points${P.isClosed(o.mesh) ? ', closed' : ''}`)),
    sw);
  if (o.shape) {
    const def = SHAPE_DEFS[o.shape.type];
    const card = h('div', { class: 'card' }, h('b', {}, def.title + ' settings'),
      h('p', { class: 'note' }, 'These stay adjustable until you edit its points, edges or faces.'));
    for (const [k, v] of Object.entries(o.shape.params)) {
      if (k === 'axis') {
        const sel = h('select', {}, ...['x', 'y', 'z'].map(a => h('option', { value: a, selected: v === a }, { x: 'Left-right (X)', y: 'Front-back (Y)', z: 'Up (Z)' }[a])));
        sel.addEventListener('change', () => { remember('Shape'); o.shape.params.axis = sel.value; o.mesh = shapeMesh(o.shape); rebuildAll(); });
        card.append(h('div', { class: 'props', style: { marginTop: '6px' } }, h('span', { class: 'k' }, 'Runs along'), sel));
        continue;
      }
      const [label, min, max, step] = PARAM_INFO[k] || [k, 0, 10, 0.01];
      const val = h('span', { class: 'val' }, fmt(v, 3));
      const r = h('input', { type: 'range', min, max, step, value: v });
      let first = true;
      r.addEventListener('input', () => {
        if (first) { remember('Shape'); first = false; }
        o.shape.params[k] = step >= 1 ? Math.round(+r.value) : +r.value;
        val.textContent = fmt(o.shape.params[k], 3);
        o.mesh = shapeMesh(o.shape);
        rebuildObject(o);
        drawSelection();
        placeGizmo();
        V.dirty = true;
      });
      r.addEventListener('change', () => { first = true; render(); });
      card.append(h('div', { class: 'slider' }, h('div', { class: 'top' }, h('span', { class: 'name' }, label), val), r));
    }
    box.append(card);
  }
  if (W.mode !== 'object') box.append(h('p', { class: 'note' }, `${W.sel.size} ${W.mode === 'vertex' ? 'points' : W.mode + 's'} selected.`));
}

// for the UI tests and the console
window.jbeWorkshop = { W, OPS, addShape, setMode, setTool, undo, redo, startMake };
