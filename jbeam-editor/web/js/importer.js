// Import a 3D model (.obj .glb .gltf .stl .dae), place it, and turn it into a
// part: the mesh is written into the mod as a .dae, and a braced node cage
// with collision triangles, stable weights and attachment beams holds it.
import * as THREE from 'three';
import { OBJLoader } from 'three/addons/loaders/OBJLoader.js';
import { GLTFLoader } from 'three/addons/loaders/GLTFLoader.js';
import { STLLoader } from 'three/addons/loaders/STLLoader.js';
import { ColladaLoader } from 'three/addons/loaders/ColladaLoader.js';
import { mergeGeometries, mergeVertices } from 'three/addons/utils/BufferGeometryUtils.js';
import { S, A } from './state.js';
import { api } from './api.js';
import { DT, partEditable, partFileKey } from './scene.js';
import { V, redraw, updateGizmo } from './view3d.js';
import { freePrefix, info, placeCentre, rootPicker, slotSelect } from './dialogs.js';
import { $, closeModal, fmt, h, numInput, openModal, setLoading, toast } from './util.js';

const UNITS = { m: 1, cm: 0.01, mm: 0.001, in: 0.0254 };
const I = { outer: null, mesh: null, geo: null, file: '', up: 'Y', unit: 'm', scale: 1, rot: [0, 0, 0], keepPos: false, centre: null };

export function startImport() {
  if (!S.scene) return toast('Open a vehicle first.');
  $('#fileInput').value = '';
  $('#fileInput').click();
}

// a card in the 3D view to drop a file on or click (a file dialog needs a click to open)
export function showImportPrompt() {
  $('#importPrompt')?.remove();
  const card = h('div', { id: 'importPrompt', class: 'dropcard' },
    h('h3', {}, 'Import a 3D model'),
    h('p', {}, 'Drop an .obj, .glb, .gltf, .stl or .dae file here, or choose one.'),
    h('div', { class: 'btnrow', style: { justifyContent: 'center' } },
      h('button', { class: 'primary', onclick: () => { card.remove(); startImport(); } }, 'Choose a file…'),
      h('button', { onclick: () => card.remove() }, 'Not now')));
  $('#center').append(card);
}

export function initImporter() {
  $('#fileInput').addEventListener('change', e => { const f = e.target.files[0]; if (f) loadFile(f); });
  const vp = $('#viewport');
  vp.addEventListener('dragover', e => { e.preventDefault(); });
  vp.addEventListener('drop', e => {
    e.preventDefault();
    const f = e.dataTransfer.files[0];
    if (f && S.scene) loadFile(f);
  });
  V.gizmo.addEventListener('objectChange', () => { if (I.outer) syncInputs(); });
}

export function importActive() { return !!I.outer; }

async function loadFile(file) {
  $('#importPrompt')?.remove();
  const ext = file.name.split('.').pop().toLowerCase();
  setLoading(true, 'Reading ' + file.name + '…');
  try {
    const buf = await file.arrayBuffer();
    let root;
    if (ext === 'obj') root = new OBJLoader().parse(new TextDecoder().decode(buf));
    else if (ext === 'stl') root = new THREE.Mesh(new STLLoader().parse(buf));
    else if (ext === 'glb' || ext === 'gltf') {
      try { root = (await new GLTFLoader().parseAsync(buf, '')).scene; } catch (e) {
        throw new Error('Could not read it' + (ext === 'gltf' ? ' (a .gltf with separate .bin files can\'t be loaded here; export a single .glb instead)' : '') + ': ' + e.message);
      }
    } else if (ext === 'dae') root = new ColladaLoader().parse(new TextDecoder().decode(buf), '').scene;
    else throw new Error('Use an .obj, .glb, .gltf, .stl or .dae file.');
    root.updateMatrixWorld(true);
    const geos = [];
    root.traverse(o => {
      if (!o.isMesh || !o.geometry?.attributes?.position) return;
      let g = o.geometry.clone();
      g.applyMatrix4(o.matrixWorld);
      if (g.index) g = g.toNonIndexed();
      geos.push(g);
    });
    if (!geos.length) throw new Error('No meshes found in that file.');
    const withUv = geos.every(g => g.attributes.uv);
    for (const g of geos) for (const k of Object.keys(g.attributes)) if (k !== 'position' && !(k === 'uv' && withUv)) g.deleteAttribute(k);
    for (const g of geos) { g.morphAttributes = {}; g.clearGroups(); }
    let geo = geos.length > 1 ? mergeGeometries(geos) : geos[0];
    if (!geo) throw new Error('Could not combine the meshes in that file.');
    geo = mergeVertices(geo, 1e-6);
    geo.computeBoundingBox();
    const size = geo.boundingBox.getSize(new THREE.Vector3());
    const maxDim = Math.max(size.x, size.y, size.z);
    clearImport();
    I.geo = geo;
    I.file = file.name;
    I.up = ext === 'stl' ? 'Z' : 'Y';
    I.unit = maxDim > 1000 ? 'mm' : maxDim > 30 ? 'cm' : 'm';
    I.scale = 1;
    I.rot = [0, 0, 0];
    I.keepPos = ext === 'dae';
    I.centre = geo.boundingBox.getCenter(new THREE.Vector3());
    const mat = new THREE.MeshStandardMaterial({ color: 0xffa94d, roughness: 0.6, metalness: 0.05, transparent: true, opacity: 0.75, side: THREE.DoubleSide });
    I.mesh = new THREE.Mesh(geo, mat);
    I.outer = new THREE.Object3D();
    I.outer.add(I.mesh);
    V.groups.imports.add(I.outer);
    if (!I.keepPos) I.outer.position.set(...placeCentre());
    V.importObj = I.outer;
    applyTransform();
    updateGizmo();
    showPanel();
    const tris = (geo.index ? geo.index.count : geo.attributes.position.count) / 3;
    toast(`Loaded ${file.name}: ${Math.round(tris).toLocaleString()} triangles. Place it, then press Make part.`, 'ok', 6000);
  } catch (e) {
    toast(e.message, 'err', 8000);
  } finally { setLoading(false); }
}

function applyTransform() {
  const m = I.mesh;
  const s = UNITS[I.unit] * I.scale;
  m.position.set(0, 0, 0);
  m.rotation.set(I.up === 'Y' ? Math.PI / 2 : 0, 0, 0);
  m.scale.set(s, s, s);
  if (!I.keepPos) {
    // keep the model's centre at the handle
    m.updateMatrix();
    const c = I.centre.clone().applyMatrix4(m.matrix);
    m.position.sub(c);
  }
  I.outer.rotation.set(...I.rot.map(d => d * Math.PI / 180), 'ZYX');
  I.outer.updateMatrixWorld(true);
  redraw();
}

function worldBox() {
  I.outer.updateMatrixWorld(true);
  return new THREE.Box3().setFromObject(I.mesh);
}

let inputs = null;
function syncInputs() {
  if (!inputs) return;
  const p = I.outer.position;
  [p.x, p.y, p.z].forEach((v, k) => { inputs.pos[k].value = +v.toFixed(4); });
  I.rot.forEach((v, k) => { inputs.rot[k].value = +v.toFixed(2); });
  const sz = worldBox().getSize(new THREE.Vector3());
  inputs.size.textContent = `${fmt(sz.x, 3)} × ${fmt(sz.y, 3)} × ${fmt(sz.z, 3)} m`;
}

function showPanel() {
  $('#importPanel')?.remove();
  const sel = (opts, cur, fn) => {
    const s = h('select', {}, ...opts.map(([v, t]) => h('option', { value: v, selected: v === cur }, t)));
    s.addEventListener('change', () => { fn(s.value); applyTransform(); syncInputs(); });
    return s;
  };
  const pos = [0, 1, 2].map(k => numInput(I.outer.position.getComponent(k), v => { I.outer.position.setComponent(k, v); applyTransform(); syncInputs(); }, { digits: 4 }));
  const rot = [0, 1, 2].map(k => numInput(I.rot[k], v => { I.rot[k] = v; applyTransform(); syncInputs(); }, { digits: 2 }));
  const scale = numInput(I.scale, v => { I.scale = v > 0 ? v : 1; applyTransform(); syncInputs(); }, { digits: 4 });
  const size = h('span');
  inputs = { pos, rot, size };
  const turn = (k, d) => () => { I.rot[k] = ((I.rot[k] + d) % 360 + 360) % 360; applyTransform(); syncInputs(); };
  const keep = h('input', { type: 'checkbox', checked: I.keepPos });
  keep.addEventListener('change', () => { I.keepPos = keep.checked; if (keep.checked) I.outer.position.set(0, 0, 0); applyTransform(); syncInputs(); });
  const panel = h('div', { id: 'importPanel' },
    h('h3', {}, 'Place ' + I.file),
    h('p', { class: 'note' }, 'Drag the arrows or type values. The vehicle faces -Y (towards the front view).'),
    h('div', { class: 'props' },
      h('span', { class: 'k' }, 'Up axis'), sel([['Y', 'Y up (most apps)'], ['Z', 'Z up (Blender, BeamNG)']], I.up, v => { I.up = v; }),
      h('span', { class: 'k' }, 'Units'), sel([['m', 'metres'], ['cm', 'centimetres'], ['mm', 'millimetres'], ['in', 'inches']], I.unit, v => { I.unit = v; }),
      h('span', { class: 'k' }, 'Scale ×'), scale,
      h('span', { class: 'k' }, 'Size'), size,
      h('span', { class: 'k' }, 'Position'), h('span', { class: 'xyz' }, ...pos),
      h('span', { class: 'k' }, 'Rotation °'), h('span', { class: 'xyz' }, ...rot),
      h('span', { class: 'k', title: 'For models already made in vehicle coordinates (like BeamNG .dae files)' }, 'Keep file position'), keep),
    h('div', { class: 'btnrow' },
      h('button', { class: 'small', onclick: turn(2, 90) }, '⟳ Z 90°'), h('button', { class: 'small', onclick: turn(0, 90) }, '⟳ X 90°'),
      h('button', { class: 'small', onclick: turn(1, 90) }, '⟳ Y 90°'), h('button', { class: 'small', onclick: turn(2, 180) }, 'Turn around')),
    h('div', { class: 'btnrow' },
      h('button', { class: 'small', title: 'Move it to the middle of the selected nodes', onclick: () => { I.outer.position.set(...placeCentre()); applyTransform(); syncInputs(); } }, 'To selection'),
      h('button', { class: 'small', title: 'Put its bottom on top of the selected nodes', onclick: sitOnSelection }, 'Sit on selection'),
      h('button', { class: 'small', title: 'Centre it left/right on the vehicle', onclick: () => { I.outer.position.x = 0; applyTransform(); syncInputs(); } }, 'Centre X')),
    h('div', { class: 'actions' },
      h('button', { onclick: clearImport }, 'Cancel'),
      h('button', { class: 'primary', onclick: makePartDialog }, 'Make part…')));
  panel.addEventListener('keydown', e => e.stopPropagation());
  $('#center').append(panel);
  syncInputs();
}

function sitOnSelection() {
  const sc = S.scene;
  const sel = [...S.sel.nodes].filter(i => !sc.nodes[i].del);
  if (!sel.length) return toast('Select the nodes it should sit on first.');
  const top = Math.max(...sel.map(i => sc.nodes[i].p[2]));
  const c = placeCentre();
  const b = worldBox();
  I.outer.position.x += c[0] - (b.min.x + b.max.x) / 2;
  I.outer.position.y += c[1] - (b.min.y + b.max.y) / 2;
  I.outer.position.z += top + 0.01 - b.min.z;
  applyTransform();
  syncInputs();
}

export function clearImport() {
  $('#importPanel')?.remove();
  inputs = null;
  if (I.outer) {
    V.groups.imports.remove(I.outer);
    I.mesh.material.dispose();
    I.geo.dispose();
  }
  I.outer = I.mesh = I.geo = null;
  V.importObj = null;
  updateGizmo();
  redraw();
}

// ---- baking the placed mesh ---------------------------------------------------------------
function baked() {
  I.outer.updateMatrixWorld(true);
  const m = I.mesh.matrixWorld;
  const src = I.geo.attributes.position;
  const pos = new Float32Array(src.count * 3);
  const v = new THREE.Vector3();
  for (let i = 0; i < src.count; i++) { v.fromBufferAttribute(src, i).applyMatrix4(m); pos[i * 3] = v.x; pos[i * 3 + 1] = v.y; pos[i * 3 + 2] = v.z; }
  let idx;
  if (I.geo.index) idx = Uint32Array.from(I.geo.index.array);
  else { idx = new Uint32Array(src.count); for (let i = 0; i < src.count; i++) idx[i] = i; }
  if (m.determinant() < 0) for (let i = 0; i < idx.length; i += 3) { const t = idx[i + 1]; idx[i + 1] = idx[i + 2]; idx[i + 2] = t; }
  const uv = I.geo.attributes.uv ? Float32Array.from(I.geo.attributes.uv.array) : null;
  return { pos, idx, uv };
}

function b64(arr) {
  const bytes = new Uint8Array(arr.buffer, arr.byteOffset, arr.byteLength);
  let s = '';
  for (let i = 0; i < bytes.length; i += 0x8000) s += String.fromCharCode.apply(null, bytes.subarray(i, i + 0x8000));
  return btoa(s);
}

// ---- the node cage ---------------------------------------------------------------------------------
const STIFFNESS = {
  soft: { spring: 400000, damp: 40, deform: 15000, label: 'Soft (plastic trim, light panels)' },
  panel: { spring: 1500000, damp: 100, deform: 40000, label: 'Body panel (most parts)' },
  stiff: { spring: 6000000, damp: 200, deform: 150000, label: 'Stiff (bumpers, racks, steel boxes)' },
};

export function buildCage(box, { spacing, prefix, spring, damp, collision }) {
  const min = box.min.toArray(), max = box.max.toArray();
  for (let k = 0; k < 3; k++) if (max[k] - min[k] < 0.06) { const c = (max[k] + min[k]) / 2; min[k] = c - 0.03; max[k] = c + 0.03; }
  const cnt = [0, 1, 2].map(k => Math.min(7, Math.max(2, Math.round((max[k] - min[k]) / spacing) + 1)));
  const symmetric = Math.abs(min[0] + max[0]) < 0.04;
  if (symmetric) { const hw = (max[0] - min[0]) / 2; min[0] = -hw; max[0] = hw; }   // exact mirror pairs
  const at = (i, j, k) => [min[0] + (max[0] - min[0]) * i / (cnt[0] - 1), min[1] + (max[1] - min[1]) * j / (cnt[1] - 1), min[2] + (max[2] - min[2]) * k / (cnt[2] - 1)];
  const onSurface = (i, j, k) => i === 0 || j === 0 || k === 0 || i === cnt[0] - 1 || j === cnt[1] - 1 || k === cnt[2] - 1;
  const ids = new Map(), pos = new Map(), nodes = [];
  const numbers = new Map();
  let next = 1;
  for (let k = 0; k < cnt[2]; k++) for (let j = 0; j < cnt[1]; j++) for (let i = 0; i < cnt[0]; i++) {
    if (!onSurface(i, j, k)) continue;
    let id;
    if (symmetric) {
      const im = Math.min(i, cnt[0] - 1 - i);
      const key = im + ',' + j + ',' + k;
      if (!numbers.has(key)) numbers.set(key, next++);
      const mid = (cnt[0] - 1) / 2;
      id = prefix + numbers.get(key) + (i < mid ? 'r' : i > mid ? 'l' : '');
    } else id = prefix + next++;
    const p = at(i, j, k).map(v => +v.toFixed(4));
    ids.set(i + ',' + j + ',' + k, id);
    pos.set(id, p);
    nodes.push([id, ...p]);
  }
  const key = (i, j, k) => ids.get(i + ',' + j + ',' + k);
  const beams = [], seen = new Set();
  const addBeam = (a, b) => { if (!a || !b || a === b) return; const s = a < b ? a + '|' + b : b + '|' + a; if (!seen.has(s)) { seen.add(s); beams.push([a, b]); } };
  for (const [s, a] of ids) {
    const [i, j, k] = s.split(',').map(Number);
    for (let di = -1; di <= 1; di++) for (let dj = -1; dj <= 1; dj++) for (let dk = -1; dk <= 1; dk++) {
      if (!di && !dj && !dk) continue;
      const b = key(i + di, j + dj, k + dk);
      if (b) addBeam(a, b);
    }
  }
  const L = cnt.map(c => c - 1);
  addBeam(key(0, 0, 0), key(L[0], L[1], L[2])); addBeam(key(L[0], 0, 0), key(0, L[1], L[2]));
  addBeam(key(0, L[1], 0), key(L[0], 0, L[2])); addBeam(key(0, 0, L[2]), key(L[0], L[1], 0));
  // collision triangles on the six faces, facing out
  const tris = [];
  if (collision) {
    const centre = [(min[0] + max[0]) / 2, (min[1] + max[1]) / 2, (min[2] + max[2]) / 2];
    const tri = (a, b, c) => {
      const A = pos.get(a), B = pos.get(b), C = pos.get(c);
      const u = [B[0] - A[0], B[1] - A[1], B[2] - A[2]], v = [C[0] - A[0], C[1] - A[1], C[2] - A[2]];
      const n = [u[1] * v[2] - u[2] * v[1], u[2] * v[0] - u[0] * v[2], u[0] * v[1] - u[1] * v[0]];
      const m = [(A[0] + B[0] + C[0]) / 3 - centre[0], (A[1] + B[1] + C[1]) / 3 - centre[1], (A[2] + B[2] + C[2]) / 3 - centre[2]];
      tris.push(n[0] * m[0] + n[1] * m[1] + n[2] * m[2] >= 0 ? [a, b, c] : [a, c, b]);
    };
    const face = (axis, fixedIdx) => {
      const [u, v] = [0, 1, 2].filter(x => x !== axis);
      for (let a = 0; a < L[u]; a++) for (let b = 0; b < L[v]; b++) {
        const c = (da, db) => { const ix = [0, 0, 0]; ix[axis] = fixedIdx; ix[u] = a + da; ix[v] = b + db; return key(...ix); };
        tri(c(0, 0), c(1, 0), c(1, 1));
        tri(c(1, 1), c(0, 1), c(0, 0));
      }
    };
    for (let ax = 0; ax < 3; ax++) { face(ax, 0); face(ax, L[ax]); }
  }
  // stiffness each node carries, for the weights
  const K = new Map(), C = new Map();
  for (const id of pos.keys()) { K.set(id, 0); C.set(id, 0); }
  for (const [a, b] of beams) { K.set(a, K.get(a) + spring); K.set(b, K.get(b) + spring); C.set(a, C.get(a) + damp); C.set(b, C.get(b) + damp); }
  return { nodes, beams, tris, pos, K, C };
}

function attachBeams(cage, mode, anchors) {
  const sc = S.scene;
  const d2 = (a, b) => (a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2 + (a[2] - b[2]) ** 2;
  const out = [], seen = new Set();
  const add = (vehId, cageId) => { const k = vehId + '|' + cageId; if (!seen.has(k)) { seen.add(k); out.push([vehId, cageId]); } };
  const cageIds = [...cage.pos.keys()];
  if (mode === 'sel') {
    for (const i of anchors) {
      const p = sc.nodes[i].p;
      cageIds.slice().sort((a, b) => d2(cage.pos.get(a), p) - d2(cage.pos.get(b), p)).slice(0, 3).forEach(c => add(sc.nodes[i].id, c));
    }
  } else if (mode === 'near') {
    const veh = sc.nodes.filter(n => !n.del);
    const nearest = cageIds.map(c => {
      const p = cage.pos.get(c);
      const list = veh.map(n => [d2(n.p, p), n.id]).sort((a, b) => a[0] - b[0]).slice(0, 2);
      return { c, list, d: Math.sqrt(list[0]?.[0] ?? Infinity) };
    }).sort((a, b) => a.d - b.d);
    if (!nearest.length || nearest[0].d > 1.0) return { beams: [], warn: 'Nothing on the vehicle is within 1 m of the model, so it was not attached.' };
    const lim = nearest[0].d + 0.12;
    for (const x of nearest.filter(x => x.d <= lim).slice(0, 12)) for (const [dd, id] of x.list) if (Math.sqrt(dd) <= lim + 0.2) add(id, x.c);
  }
  return { beams: out };
}

function makePartDialog() {
  const sc = S.scene;
  const base = (I.file.replace(/\.[^.]+$/, '') || 'imported').replace(/[^A-Za-z0-9_]/g, '_').toLowerCase();
  const selected = [...S.sel.nodes].filter(i => !sc.nodes[i].del);
  const how = h('div', { class: 'choice' });
  const howOpt = (v, label, small, checked, disabled) => {
    const r = h('input', { type: 'radio', name: 'imhow', value: v, checked, disabled });
    r.addEventListener('change', update);
    return h('label', {}, r, h('span', {}, label, h('small', {}, small)));
  };
  const activeOk = S.activePart && partEditable(S.activePart);
  how.append(
    howOpt('cage', 'New part with its own nodes', 'Builds a braced node cage around the model with collision, so it can bend, break off and hit things', true),
    howOpt('mesh', 'Just the model, on the part I\'m editing', activeOk ? `Adds the mesh to ${sc.parts[S.activePart]?.title || S.activePart}; it bends with that part's nodes` :
      'Pick a part you can edit in "Adding to" first', false, !activeOk));
  const nameIn = h('input', { type: 'text', value: base });
  const titleIn = h('input', { type: 'text', value: I.file.replace(/\.[^.]+$/, '') });
  const slotSel = slotSelect(null, { preferEmpty: true });
  const root = rootPicker();
  const massIn = h('input', { type: 'number', step: '1', value: 25, style: { width: '90px' } });
  const detail = h('select', {}, h('option', { value: 0.6 }, 'Coarse (fewest nodes)'), h('option', { value: 0.35, selected: true }, 'Medium'), h('option', { value: 0.2 }, 'Fine'));
  const stiff = h('select', {}, ...Object.entries(STIFFNESS).map(([k, v]) => h('option', { value: k, selected: k === 'panel' }, v.label)));
  const coll = h('input', { type: 'checkbox', checked: true });
  const attach = h('select', {},
    h('option', { value: 'sel', disabled: !selected.length, selected: !!selected.length }, selected.length ? `To the ${selected.length} selected node(s)` : 'To selected nodes (select some first)'),
    h('option', { value: 'near', selected: !selected.length }, 'To the nearest vehicle nodes'),
    h('option', { value: 'none' }, 'Don\'t attach (it falls off)'));
  const paint = h('input', { type: 'checkbox' });
  const colour = h('input', { type: 'color', value: '#9aa0a8' });
  for (const i of [nameIn, titleIn, massIn]) i.addEventListener('keydown', e => e.stopPropagation());
  const cageBox = h('div', { class: 'form', style: { marginTop: '8px' } },
    h('span', { class: 'k' }, 'Slot'), slotSel,
    h('span', { class: 'k' }, 'Name in the parts menu'), titleIn,
    h('span', { class: 'k' }, 'Mass (kg)'), massIn,
    h('span', { class: 'k' }, 'Cage detail'), detail,
    h('span', { class: 'k' }, 'Stiffness'), stiff,
    h('span', { class: 'k' }, 'Collision'), h('label', { class: 'chk' }, coll, 'Other things bump into it'),
    h('span', { class: 'k' }, 'Attach'), attach);
  function update() {
    cageBox.classList.toggle('hidden', how.querySelector('input:checked').value !== 'cage');
  }
  update();
  const err = h('div', { class: 'err' });
  const go = async () => {
    err.textContent = '';
    const mode = how.querySelector('input:checked').value;
    const name = nameIn.value.trim();
    if (!/^[A-Za-z0-9_\-.]+$/.test(name)) return (err.textContent = 'Use only letters, digits, _ - and . in the id.');
    try {
      setLoading(true, 'Writing the model…');
      await A.save({ quiet: true });
      const { pos, idx, uv } = baked();
      const c = colour.value;
      const rgb = [1, 3, 5].map(k => parseInt(c.slice(k, k + 2), 16) / 255);
      if (mode === 'mesh') {
        const part = S.activePart;
        const groups = {};
        for (const n of sc.nodes) if (!n.del && n.part === part) for (const g of n.grp) groups[g] = (groups[g] || 0) + 1;
        const grp = Object.entries(groups).sort((a, b) => b[1] - a[1])[0]?.[0];
        if (!grp) throw new Error('The nodes of that part have no group, so a mesh can\'t follow them. Select its nodes and set a Group first.');
        const proot = S.scene.files[S.scene.parts[part].f];
        const rootId = S.summary.editableRoots.find(r => r.label === proot.root)?.id;
        const mr = await api.importMesh({ model: S.model, root: rootId, name, positions: b64(pos), indices: b64(idx), uvs: uv ? b64(uv) : null, color: rgb, paint: paint.checked });
        clearImport();
        closeModal();
        const ok = await A.saveOps([{ op: 'row.add', f: partFileKey(part), part, sec: 'flexbodies', row: [mr.mesh, [grp], []] }], 'Add mesh ' + mr.mesh);
        if (ok !== true) throw new Error(ok || 'could not add the mesh');
        toast(`Added the model to ${part}. It bends with the "${grp}" nodes.`, 'ok', 7000);
        return;
      }
      const s = slotSel.getSlot();
      if (!s) throw new Error('Pick a slot.');
      const rootId = await root.getRoot();
      const mr = await api.importMesh({ model: S.model, root: rootId, name, positions: b64(pos), indices: b64(idx), uvs: uv ? b64(uv) : null, color: rgb, paint: paint.checked });
      const st = STIFFNESS[stiff.value];
      const cage = buildCage(worldBox(), { spacing: +detail.value, prefix: freePrefix(name), spring: st.spring, damp: st.damp, collision: coll.checked });
      const at = attachBeams(cage, attach.value, selected);
      for (const [, cid] of at.beams) { cage.K.set(cid, cage.K.get(cid) + st.spring); cage.C.set(cid, cage.C.get(cid) + st.damp); }
      const mass = +massIn.value > 0 ? +massIn.value : 25;
      const per = mass / cage.nodes.length;
      const w = new Map();
      for (const id of cage.pos.keys()) {
        const need = Math.max(cage.K.get(id) * DT * DT / 4, cage.C.get(id) * DT / 2.4) * 1.05;
        w.set(id, +Math.max(per, need, 0.3).toFixed(2));
      }
      // group nodes by weight so each weight is written once
      const nodeRows = [];
      let lastW = null;
      for (const row of cage.nodes) {
        const ww = w.get(row[0]);
        if (ww !== lastW) { nodeRows.push({ nodeWeight: ww }); lastW = ww; }
        nodeRows.push(row);
      }
      const data = {
        information: info(titleIn.value.trim() || name),
        slotType: s.allowTypes?.[0] || s.id,
        flexbodies: [['mesh', '[group]:', 'nonFlexMaterials'], [mr.mesh, [name], []]],
        nodes: [['id', 'posX', 'posY', 'posZ'], { collision: true }, { selfCollision: true }, { frictionCoef: 0.7 },
          { nodeMaterial: '|NM_METAL' }, { group: name }, ...nodeRows, { group: '' }],
        beams: [['id1:', 'id2:'], { beamType: '|NORMAL', beamSpring: st.spring, beamDamp: st.damp, beamDeform: st.deform, beamStrength: 'FLT_MAX' },
          ...cage.beams, ...at.beams],
        triangles: [['id1:', 'id2:', 'id3:'], { groundModel: 'metal' }, ...cage.tris],
      };
      const r = await api.newPart({ model: S.model, root: rootId, name, mode: 'data', data, title: data.information.name, slotType: data.slotType });
      S.summary = r.summary;
      S.config.parts[s.id] = name;
      S.activePart = name;
      S.pinActive = true;
      clearImport();
      closeModal();
      await A.reloadModel();
      const total = [...w.values()].reduce((a, b) => a + b, 0);
      toast(`Made part ${name}: ${cage.nodes.length} nodes, ${cage.beams.length + at.beams.length} beams, ${cage.tris.length} triangles, ${fmt(total, 1)} kg.` +
        (at.warn ? ' ' + at.warn : ''), at.warn ? 'err' : 'ok', 9000);
    } catch (e) {
      err.textContent = e.message;
    } finally { setLoading(false); }
  };
  openModal('Make a part from ' + I.file, h('div', {},
    how,
    h('div', { class: 'form', style: { marginTop: '8px' } },
      h('span', { class: 'k' }, 'Part / mesh id'), nameIn,
      h('span', { class: 'k' }, 'Save into mod'), root,
      h('span', { class: 'k' }, 'Colour'), h('span', { style: { display: 'flex', gap: '8px', alignItems: 'center' } }, colour,
        h('label', { class: 'chk' }, paint, 'Use the vehicle\'s paint'))),
    cageBox,
    err,
    h('div', { class: 'actions' }, h('button', { onclick: closeModal }, 'Cancel'), h('button', { class: 'primary', onclick: go }, 'Make part'))));
}
