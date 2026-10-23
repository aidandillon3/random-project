// Import a 3D model (.obj .glb .gltf .stl .dae), place it on the vehicle and turn
// it into a part: the mesh is written into the mod as a .dae, and a node structure
// fitted to its shape (see smartfit.js) with collision, stable weights and mounting
// beams carries it. The placing and the part options live in the left panel.
import * as THREE from 'three';
import { OBJLoader } from 'three/addons/loaders/OBJLoader.js';
import { GLTFLoader } from 'three/addons/loaders/GLTFLoader.js';
import { STLLoader } from 'three/addons/loaders/STLLoader.js';
import { ColladaLoader } from 'three/addons/loaders/ColladaLoader.js';
import { mergeGeometries, mergeVertices } from 'three/addons/utils/BufferGeometryUtils.js';
import { S, A, on } from './state.js';
import { V, redraw, updateGizmo } from './view3d.js';
import { placeCentre } from './dialogs.js';
import { partForm } from './partform.js';
import { icon } from './icons.js';
import { $, fmt, h, numInput, setLoading, toast } from './util.js';

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
  V.gizmo.addEventListener('objectChange', () => { if (I.outer) { syncInputs(); form?.update(); } });
  on('selection', () => form?.update());
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
    A.setMode('structure');
    I.geo = geo;
    I.file = file.name;
    I.up = ext === 'stl' ? 'Z' : 'Y';
    I.unit = maxDim > 1000 ? 'mm' : maxDim > 30 ? 'cm' : 'm';
    I.scale = 1;
    I.rot = [0, 0, 0];
    I.keepPos = ext === 'dae';
    I.centre = geo.boundingBox.getCenter(new THREE.Vector3());
    const mat = new THREE.MeshStandardMaterial({ color: 0xffa94d, roughness: 0.6, metalness: 0.05, transparent: true, opacity: 0.6, side: THREE.DoubleSide, depthWrite: false });
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
    toast(`Loaded ${file.name}: ${Math.round(tris).toLocaleString()} triangles. Place it, then make it a part.`, 'ok', 6000);
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
let form = null;
function syncInputs() {
  if (!inputs) return;
  const p = I.outer.position;
  [p.x, p.y, p.z].forEach((v, k) => { inputs.pos[k].value = +v.toFixed(4); });
  I.rot.forEach((v, k) => { inputs.rot[k].value = +v.toFixed(2); });
  const sz = worldBox().getSize(new THREE.Vector3());
  inputs.size.textContent = `${fmt(sz.x, 3)} × ${fmt(sz.y, 3)} × ${fmt(sz.z, 3)} m`;
}

function changedPlacement() { applyTransform(); syncInputs(); form?.update(); }

function showPanel() {
  const pane = $('#importPane');
  pane.innerHTML = '';
  document.body.classList.add('importing');
  const sel = (opts, cur, fn) => {
    const s = h('select', {}, ...opts.map(([v, t]) => h('option', { value: v, selected: v === cur }, t)));
    s.addEventListener('change', () => { fn(s.value); changedPlacement(); });
    return s;
  };
  const pos = [0, 1, 2].map(k => numInput(I.outer.position.getComponent(k), v => { I.outer.position.setComponent(k, v); changedPlacement(); }, { digits: 4 }));
  const rot = [0, 1, 2].map(k => numInput(I.rot[k], v => { I.rot[k] = v; changedPlacement(); }, { digits: 2 }));
  const scale = numInput(I.scale, v => { I.scale = v > 0 ? v : 1; changedPlacement(); }, { digits: 4 });
  const size = h('span');
  inputs = { pos, rot, size };
  const turn = (k, d) => () => { I.rot[k] = ((I.rot[k] + d) % 360 + 360) % 360; changedPlacement(); };
  const keep = h('input', { type: 'checkbox', checked: I.keepPos });
  keep.addEventListener('change', () => { I.keepPos = keep.checked; if (keep.checked) I.outer.position.set(0, 0, 0); changedPlacement(); });
  pane.append(
    h('div', { class: 'pane-head' }, h('h2', {}, 'Import: ' + I.file),
      h('p', { class: 'note' }, 'Drag the arrows in the 3D view or type values. The vehicle faces the front view (−Y).')),
    h('h3', { class: 'sect' }, 'Place it'),
    h('div', { class: 'props' },
      h('span', { class: 'k' }, 'Up axis'), sel([['Y', 'Y up (most apps)'], ['Z', 'Z up (Blender, BeamNG)']], I.up, v => { I.up = v; }),
      h('span', { class: 'k' }, 'Units'), sel([['m', 'metres'], ['cm', 'centimetres'], ['mm', 'millimetres'], ['in', 'inches']], I.unit, v => { I.unit = v; }),
      h('span', { class: 'k' }, 'Scale ×'), scale,
      h('span', { class: 'k' }, 'Size'), size,
      h('span', { class: 'k' }, 'Position'), h('span', { class: 'xyz' }, ...pos),
      h('span', { class: 'k' }, 'Rotation °'), h('span', { class: 'xyz' }, ...rot),
      h('span', { class: 'k', title: 'For models already made in vehicle coordinates (like BeamNG .dae files)' }, 'Keep file position'), keep),
    h('div', { class: 'btnrow' },
      h('button', { class: 'small', onclick: turn(2, 90) }, icon('rotate', 13), 'Z 90°'), h('button', { class: 'small', onclick: turn(0, 90) }, icon('rotate', 13), 'X 90°'),
      h('button', { class: 'small', onclick: turn(1, 90) }, icon('rotate', 13), 'Y 90°'), h('button', { class: 'small', onclick: turn(2, 180) }, 'Turn around')),
    h('div', { class: 'btnrow' },
      h('button', { class: 'small', title: 'Move it to the middle of the selected nodes', onclick: () => { I.outer.position.set(...placeCentre()); changedPlacement(); } }, 'To selection'),
      h('button', { class: 'small', title: 'Put its bottom on top of the selected nodes', onclick: sitOnSelection }, 'Sit on selection'),
      h('button', { class: 'small', title: 'Centre it left/right on the vehicle', onclick: () => { I.outer.position.x = 0; changedPlacement(); } }, 'Centre left/right')));
  const base = (I.file.replace(/\.[^.]+$/, '') || 'imported').replace(/[^A-Za-z0-9_]/g, '_').toLowerCase();
  form = partForm({ container: pane, baseName: base, title: I.file.replace(/\.[^.]+$/, ''), getMesh: baked,
    onDone: () => clearImport(), onCancel: () => clearImport() });
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
  changedPlacement();
}

export function clearImport() {
  form?.clear();
  form = null;
  const pane = $('#importPane');
  if (pane) pane.innerHTML = '';
  document.body.classList.remove('importing');
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

// the placed mesh in vehicle coordinates
function baked() {
  if (!I.outer) return null;
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
