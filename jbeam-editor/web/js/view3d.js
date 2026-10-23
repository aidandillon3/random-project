// The 3D viewport: draws nodes, beams, collision triangles, wheels and the
// vehicle's meshes, and handles picking, box selection and the move gizmo.
import * as THREE from 'three';
import { OrbitControls } from 'three/addons/controls/OrbitControls.js';
import { TransformControls } from 'three/addons/controls/TransformControls.js';
import { S, emit } from './state.js';
import { BEAM_COLORS, elemVisible, markLinked, nodeVisible, quatFromEuler } from './scene.js';
import { $ } from './util.js';

export const V = {
  renderer: null, scene: null, camera: null, controls: null, gizmo: null, pivot: null,
  groups: {}, meshCache: new Map(), meshMaterial: null, palette: new Map(),
  visible: { nodes: true, beams: true, tris: true, wheels: true, mesh: true, labels: true },
  nodeSize: 7, meshOpacity: 0.35,
  drawNodes: [], drawBeams: [], drawTris: [],
  hover: null, dirty: true, importObj: null,
};

const COL_SEL = new THREE.Color(0xffa733);
const COL_NODE = new THREE.Color(0xdfe7f5);
const COL_NODE_RO = new THREE.Color(0x7d8696);
const COL_FIXED = new THREE.Color(0xc77dff);
const COL_STAB = [new THREE.Color(0x4cc38a), new THREE.Color(0xe8c14a), new THREE.Color(0xef5d5d)];
const COL_HOVER = new THREE.Color(0xffffff);

export function initView() {
  const el = $('#viewport');
  const renderer = new THREE.WebGLRenderer({ antialias: true, preserveDrawingBuffer: true, alpha: true });
  renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
  renderer.setClearColor(0x000000, 0);
  renderer.toneMapping = THREE.ACESFilmicToneMapping;
  renderer.toneMappingExposure = 1.05;
  el.append(renderer.domElement);
  const scene = new THREE.Scene();
  const camera = new THREE.PerspectiveCamera(45, 1, 0.02, 2000);
  camera.up.set(0, 0, 1);
  camera.position.set(6, -8, 4);
  const controls = new OrbitControls(camera, renderer.domElement);
  controls.mouseButtons = { LEFT: -1, MIDDLE: THREE.MOUSE.PAN, RIGHT: THREE.MOUSE.ROTATE };
  controls.enableDamping = false;
  controls.zoomToCursor = true;
  controls.addEventListener('change', () => { V.dirty = true; });

  scene.add(new THREE.HemisphereLight(0xdde6ff, 0x2c2a26, 1.5));
  const sun = new THREE.DirectionalLight(0xffffff, 1.6);
  sun.position.set(4, -6, 10);
  scene.add(sun);
  const rim = new THREE.DirectionalLight(0x9ec4ff, 0.6);
  rim.position.set(-6, 8, 4);
  scene.add(rim);

  const ground = new THREE.Group();
  const grid = new THREE.GridHelper(40, 40, 0x3a4150, 0x232831);
  grid.rotation.x = Math.PI / 2;
  ground.add(grid);
  const fine = new THREE.GridHelper(8, 80, 0x2a303a, 0x1d2129);
  fine.rotation.x = Math.PI / 2;
  fine.position.z = -0.001;
  ground.add(fine);
  const axes = new THREE.AxesHelper(0.6);
  ground.add(axes);
  scene.add(ground);
  V.ground = ground;

  for (const g of ['mesh', 'wheels', 'tris', 'beams', 'nodes', 'overlay', 'imports', 'preview']) {
    V.groups[g] = new THREE.Group();
    scene.add(V.groups[g]);
  }

  V.meshMaterial = new THREE.MeshStandardMaterial({
    color: 0xaab3c2, roughness: 0.55, metalness: 0.1, transparent: true, opacity: V.meshOpacity,
    depthWrite: false, side: THREE.DoubleSide,
  });

  const pivot = new THREE.Object3D();
  scene.add(pivot);
  const gizmo = new TransformControls(camera, renderer.domElement);
  gizmo.setSpace('world');
  gizmo.setSize(0.9);
  gizmo.addEventListener('dragging-changed', e => { controls.enabled = !e.value; emit('gizmoDrag', e.value); });
  gizmo.addEventListener('objectChange', () => emit('gizmoMove', pivot.position.clone()));
  gizmo.addEventListener('change', () => { V.dirty = true; });
  scene.add(gizmo.getHelper ? gizmo.getHelper() : gizmo);

  Object.assign(V, { renderer, scene, camera, controls, gizmo, pivot, el });
  new ResizeObserver(resize).observe(el);
  resize();
  const loop = () => {
    requestAnimationFrame(loop);
    if (V.dirty) {
      V.dirty = false;
      renderer.render(scene, camera);
      updateLabels();
    }
  };
  loop();
}

function resize() {
  const w = V.el.clientWidth, h = V.el.clientHeight;
  if (!w || !h) return;
  V.renderer.setSize(w, h);
  V.camera.aspect = w / h;
  V.camera.updateProjectionMatrix();
  V.dirty = true;
}

function clearGroup(g) {
  for (const c of [...g.children]) {
    g.remove(c);
    if (c.geometry && !c.userData.sharedGeometry) c.geometry.dispose();
    if (c.material && !c.userData.sharedMaterial) {
      if (Array.isArray(c.material)) c.material.forEach(m => m.dispose()); else c.material.dispose();
    }
  }
}

// nothing drawn: for a page that has no vehicle to show yet
export function clearView() {
  for (const k of ['nodes', 'beams', 'tris', 'wheels', 'mesh', 'overlay', 'preview']) if (V.groups[k]) clearGroup(V.groups[k]);
  V.dirty = true;
}

// ---- building the drawable geometry from the scene --------------------------
const circleTex = (() => {
  const c = document.createElement('canvas');
  c.width = c.height = 64;
  const g = c.getContext('2d');
  g.beginPath(); g.arc(32, 32, 28, 0, Math.PI * 2);
  g.fillStyle = '#fff'; g.fill();
  g.lineWidth = 6; g.strokeStyle = '#000'; g.stroke();
  const t = new THREE.CanvasTexture(c);
  t.colorSpace = THREE.SRGBColorSpace;
  return t;
})();

export function rebuild() {
  const sc = S.scene;
  if (!sc) return;
  markLinked();
  buildNodes();
  buildBeams();
  buildTris();
  buildWheels();
  updateMeshVisibility();
  updateGizmo();
  V.dirty = true;
}

function nodeColor(n, i) {
  if (S.sel.nodes.has(i)) return COL_SEL;
  if (V.hover && V.hover.kind === 'node' && V.hover.i === i) return COL_HOVER;
  if (S.showStability) return COL_STAB[n.level || 0];
  if (n.fixed) return COL_FIXED;
  return S.scene.files[n.f]?.editable ? COL_NODE : COL_NODE_RO;
}

function buildNodes() {
  clearGroup(V.groups.nodes);
  const sc = S.scene;
  const list = [];
  sc.nodes.forEach((n, i) => { if (nodeVisible(n)) list.push(i); });
  V.drawNodes = list;
  const pos = new Float32Array(list.length * 3), col = new Float32Array(list.length * 3);
  list.forEach((i, k) => {
    const n = sc.nodes[i];
    pos.set(n.p, k * 3);
    nodeColor(n, i).toArray(col, k * 3);
  });
  const g = new THREE.BufferGeometry();
  g.setAttribute('position', new THREE.BufferAttribute(pos, 3));
  g.setAttribute('color', new THREE.BufferAttribute(col, 3));
  const m = new THREE.PointsMaterial({ size: V.nodeSize, sizeAttenuation: false, vertexColors: true, map: circleTex,
    alphaTest: 0.5, transparent: false, depthTest: true });
  const pts = new THREE.Points(g, m);
  pts.renderOrder = 3;
  pts.visible = V.visible.nodes;
  V.groups.nodes.add(pts);
  V.nodePoints = pts;
}

function beamColor(b, i) {
  if (S.sel.beams.has(i)) return COL_SEL;
  if (V.hover && V.hover.kind === 'beam' && V.hover.i === i) return COL_HOVER;
  return new THREE.Color(BEAM_COLORS[b.t] ?? 0x7f93b0);
}

function buildBeams() {
  clearGroup(V.groups.beams);
  const sc = S.scene;
  const list = [];
  sc.beams.forEach((b, i) => {
    if (elemVisible(b) && nodeVisible(sc.nodes[b.a]) && nodeVisible(sc.nodes[b.b])) list.push(i);
  });
  V.drawBeams = list;
  const pos = new Float32Array(list.length * 6), col = new Float32Array(list.length * 6);
  list.forEach((i, k) => {
    const b = sc.beams[i];
    pos.set(sc.nodes[b.a].p, k * 6);
    pos.set(sc.nodes[b.b].p, k * 6 + 3);
    const c = beamColor(b, i);
    const dim = sc.files[b.f]?.editable ? 1 : 0.6;
    col[k * 6] = col[k * 6 + 3] = c.r * dim;
    col[k * 6 + 1] = col[k * 6 + 4] = c.g * dim;
    col[k * 6 + 2] = col[k * 6 + 5] = c.b * dim;
  });
  const g = new THREE.BufferGeometry();
  g.setAttribute('position', new THREE.BufferAttribute(pos, 3));
  g.setAttribute('color', new THREE.BufferAttribute(col, 3));
  const lines = new THREE.LineSegments(g, new THREE.LineBasicMaterial({ vertexColors: true, transparent: true, opacity: 0.85 }));
  lines.renderOrder = 2;
  lines.visible = V.visible.beams;
  V.groups.beams.add(lines);
  V.beamLines = lines;
}

function buildTris() {
  clearGroup(V.groups.tris);
  const sc = S.scene;
  const list = [];
  sc.tris.forEach((t, i) => {
    if (elemVisible(t) && t.n.every(j => nodeVisible(sc.nodes[j]))) list.push(i);
  });
  V.drawTris = list;
  const pos = new Float32Array(list.length * 9), col = new Float32Array(list.length * 9);
  const colFront = new THREE.Color(0x2fbf9a), colNc = new THREE.Color(0x6b7280);
  list.forEach((i, k) => {
    const t = sc.tris[i];
    for (let v = 0; v < 3; v++) pos.set(sc.nodes[t.n[v]].p, k * 9 + v * 3);
    const c = S.sel.tris.has(i) ? COL_SEL : (V.hover && V.hover.kind === 'tri' && V.hover.i === i) ? COL_HOVER : t.nc ? colNc : colFront;
    for (let v = 0; v < 3; v++) c.toArray(col, k * 9 + v * 3);
  });
  const g = new THREE.BufferGeometry();
  g.setAttribute('position', new THREE.BufferAttribute(pos, 3));
  g.setAttribute('color', new THREE.BufferAttribute(col, 3));
  // outward side green, inward side red: a red triangle facing you points the wrong way
  const front = new THREE.Mesh(g, new THREE.MeshBasicMaterial({ vertexColors: true, transparent: true, opacity: 0.32,
    side: THREE.FrontSide, depthWrite: false, polygonOffset: true, polygonOffsetFactor: 1, polygonOffsetUnits: 1 }));
  const back = new THREE.Mesh(g, new THREE.MeshBasicMaterial({ color: 0xd04848, transparent: true, opacity: 0.32,
    side: THREE.BackSide, depthWrite: false, polygonOffset: true, polygonOffsetFactor: 1, polygonOffsetUnits: 1 }));
  back.userData.sharedGeometry = true;
  front.renderOrder = back.renderOrder = 1;
  V.groups.tris.add(front, back);
  V.groups.tris.visible = V.visible.tris;
  V.triMesh = front;
}

export function buildWheels() {
  clearGroup(V.groups.wheels);
  const sc = S.scene;
  const mat = new THREE.MeshStandardMaterial({ color: 0x2a2d33, roughness: 0.9, transparent: true, opacity: 0.55, depthWrite: false });
  const hubMat = new THREE.MeshStandardMaterial({ color: 0x8a8f99, roughness: 0.5, transparent: true, opacity: 0.5, depthWrite: false });
  for (const w of sc.wheels) {
    if (!elemVisible(w)) continue;
    const a = new THREE.Vector3(...sc.nodes[w.n1].p), b = new THREE.Vector3(...sc.nodes[w.n2].p);
    const axis = b.clone().sub(a);
    const len = Math.max(axis.length(), 0.05);
    const width = Math.max(w.width || len, 0.08);
    const mid = a.clone().add(b).multiplyScalar(0.5);
    const q = new THREE.Quaternion().setFromUnitVectors(new THREE.Vector3(0, 1, 0), axis.clone().normalize());
    const tire = new THREE.Mesh(new THREE.CylinderGeometry(w.radius, w.radius, width, 32, 1, false), mat);
    tire.position.copy(mid); tire.quaternion.copy(q);
    tire.userData.sharedMaterial = true;
    V.groups.wheels.add(tire);
    if (w.hubRadius) {
      const hub = new THREE.Mesh(new THREE.CylinderGeometry(w.hubRadius, w.hubRadius, width * 1.02, 24), hubMat);
      hub.position.copy(mid); hub.quaternion.copy(q);
      hub.userData.sharedMaterial = true;
      V.groups.wheels.add(hub);
    }
  }
  V.groups.wheels.visible = V.visible.wheels;
}

// fast path while dragging: only positions change
export function refreshPositions() {
  const sc = S.scene;
  if (!sc) return;
  if (V.nodePoints) {
    const a = V.nodePoints.geometry.attributes.position;
    V.drawNodes.forEach((i, k) => a.array.set(sc.nodes[i].p, k * 3));
    a.needsUpdate = true;
    V.nodePoints.geometry.computeBoundingSphere();
  }
  if (V.beamLines) {
    const a = V.beamLines.geometry.attributes.position;
    V.drawBeams.forEach((i, k) => { const b = sc.beams[i]; a.array.set(sc.nodes[b.a].p, k * 6); a.array.set(sc.nodes[b.b].p, k * 6 + 3); });
    a.needsUpdate = true;
    V.beamLines.geometry.computeBoundingSphere();
  }
  if (V.triMesh) {
    const a = V.triMesh.geometry.attributes.position;
    V.drawTris.forEach((i, k) => { const t = sc.tris[i]; for (let v = 0; v < 3; v++) a.array.set(sc.nodes[t.n[v]].p, k * 9 + v * 3); });
    a.needsUpdate = true;
    V.triMesh.geometry.computeBoundingSphere();
  }
  V.dirty = true;
}

export function refreshColors() {
  const sc = S.scene;
  if (!sc) return;   // between vehicles: the old buffers no longer match anything
  if (V.nodePoints) {
    const a = V.nodePoints.geometry.attributes.color;
    V.drawNodes.forEach((i, k) => nodeColor(sc.nodes[i], i).toArray(a.array, k * 3));
    a.needsUpdate = true;
  }
  if (V.beamLines) {
    const a = V.beamLines.geometry.attributes.color;
    V.drawBeams.forEach((i, k) => {
      const b = sc.beams[i], c = beamColor(b, i), dim = sc.files[b.f]?.editable ? 1 : 0.6;
      for (const o of [0, 3]) { a.array[k * 6 + o] = c.r * dim; a.array[k * 6 + o + 1] = c.g * dim; a.array[k * 6 + o + 2] = c.b * dim; }
    });
    a.needsUpdate = true;
  }
  if (V.triMesh) {
    const a = V.triMesh.geometry.attributes.color;
    const colFront = new THREE.Color(0x2fbf9a), colNc = new THREE.Color(0x6b7280);
    V.drawTris.forEach((i, k) => {
      const t = sc.tris[i];
      const c = S.sel.tris.has(i) ? COL_SEL : (V.hover && V.hover.kind === 'tri' && V.hover.i === i) ? COL_HOVER : t.nc ? colNc : colFront;
      for (let v = 0; v < 3; v++) c.toArray(a.array, k * 9 + v * 3);
    });
    a.needsUpdate = true;
  }
  V.dirty = true;
}

export function setNodeSize(px) {
  V.nodeSize = px;
  if (V.nodePoints) V.nodePoints.material.size = px;
  V.dirty = true;
}

export function setVisible(kind, on) {
  V.visible[kind] = on;
  if (kind === 'nodes' && V.nodePoints) V.nodePoints.visible = on;
  if (kind === 'beams' && V.beamLines) V.beamLines.visible = on;
  if (kind === 'tris') V.groups.tris.visible = on;
  if (kind === 'wheels') V.groups.wheels.visible = on;
  if (kind === 'mesh') V.groups.mesh.visible = on;
  V.dirty = true;
}

// ---- flexbody meshes ----------------------------------------------------------------
export function setMeshes(meshData) {
  for (const [name, m] of Object.entries(meshData)) {
    if (V.meshCache.has(name)) continue;
    const g = new THREE.BufferGeometry();
    g.setAttribute('position', new THREE.BufferAttribute(m.pos, 3));
    g.setIndex(new THREE.BufferAttribute(m.idx, 1));
    if (m.groups) {
      // meshes with their own materials (the Vehicle Builder's): one group per material
      m.groups.forEach(([start, count], i) => g.addGroup(start, count, i));
      g.userData.mats = m.groups.map(gr => gr[2]);
    }
    g.computeVertexNormals();
    V.meshCache.set(name, g);
  }
}

// colours for meshes that name their materials; updated in place so a new paint shows at once
// materials by name for meshes with material groups: the Builder's and importer's own, or a vehicle's
// (read from its materials files; doubleSide for meshes made elsewhere, whose faces may point either way)
export function setPalette(materials, { doubleSide = false } = {}) {
  for (const [name, d] of Object.entries(materials || {})) {
    let m = V.palette.get(name);
    if (!m) {
      m = new THREE.MeshStandardMaterial({ side: doubleSide ? THREE.DoubleSide : THREE.FrontSide });
      V.palette.set(name, m);
    }
    m.side = doubleSide ? THREE.DoubleSide : THREE.FrontSide;
    m.userData.color = d.color;
    m.userData.paint = !!d.paint;
    m.color.setRGB(...(d.paint && V.paint ? V.paint : d.color), THREE.SRGBColorSpace);
    m.metalness = d.metallic ?? 0;
    m.roughness = d.roughness ?? 0.5;
    m.userData.opacity = d.opacity ?? 1;
    const em = d.glow && d.emissive ? d.emissive.map(c => c * 0.18) : [0, 0, 0];
    m.emissive.setRGB(...em);
    applyOpacity(m);
  }
  V.dirty = true;
}

function applyOpacity(m) {
  const op = (m.userData.opacity ?? 1) * (V.meshOpacity ?? 1);
  m.transparent = op < 0.999;
  m.opacity = op;
  m.depthWrite = op > 0.95;
  m.needsUpdate = true;
}

export function buildMeshes() {
  clearGroup(V.groups.mesh);
  const sc = S.scene;
  if (!sc) return;
  for (const fb of sc.flexbodies) {
    const geo = V.meshCache.get(fb.mesh);
    if (!geo) continue;
    const mat = geo.userData.mats ? geo.userData.mats.map(n => V.palette.get(n) || V.meshMaterial) : V.meshMaterial;
    const mesh = new THREE.Mesh(geo, mat);
    mesh.userData.sharedGeometry = true;
    mesh.userData.sharedMaterial = true;
    mesh.userData.part = fb.part;
    mesh.userData.fb = fb;
    const inner = new THREE.Matrix4();
    const pos = fb.pos ? new THREE.Vector3(...fb.pos) : new THREE.Vector3();
    const rot = fb.rot ? new THREE.Quaternion(...quatFromEuler(-fb.rot[0] * Math.PI / 180, -fb.rot[1] * Math.PI / 180, -fb.rot[2] * Math.PI / 180)) : new THREE.Quaternion();
    const scl = fb.scale ? new THREE.Vector3(...fb.scale) : new THREE.Vector3(1, 1, 1);
    inner.compose(pos, rot, scl);
    if (fb.xf) {
      const outer = new THREE.Matrix4().compose(new THREE.Vector3(...fb.xf.t), new THREE.Quaternion(...fb.xf.q), new THREE.Vector3(1, 1, 1));
      inner.premultiply(outer);
    }
    mesh.matrixAutoUpdate = false;
    mesh.matrix.copy(inner);
    mesh.renderOrder = 0;
    V.groups.mesh.add(mesh);
  }
  updateMeshVisibility();
  V.dirty = true;
}

export function updateMeshVisibility() {
  for (const m of V.groups.mesh.children) {
    const p = m.userData.part;
    m.visible = !S.hiddenParts.has(p) && !(S.onlyActive && S.activePart && p !== S.activePart);
  }
  V.groups.mesh.visible = V.visible.mesh;
  V.dirty = true;
}

// lift the drawn vehicle (not the ground) by z, so it can stand on the grid
export function setVehicleOffset(z) {
  V.offsetZ = z;
  for (const k of ['mesh', 'wheels', 'tris', 'beams', 'nodes', 'overlay', 'imports', 'preview']) if (V.groups[k]) V.groups[k].position.z = z;
  V.dirty = true;
}

export function setGround(on) {
  if (V.ground) V.ground.visible = on;
  V.dirty = true;
}

// paint colour on the model (Garage paint preview); null goes back to the vehicle's own. Materials that take
// the paint get it; a vehicle without any (or without readable materials) shows it on the whole model
export function setMeshColor(rgb) {
  V.paint = rgb || null;
  for (const m of V.palette.values()) {
    if (m.userData.paint) m.color.setRGB(...(rgb || m.userData.color), THREE.SRGBColorSpace);
  }
  V.meshMaterial.color.setRGB(...(rgb && !V.vehiclePaint ? rgb : [0.667, 0.702, 0.761]), THREE.SRGBColorSpace);
  V.dirty = true;
}

export function setMeshOpacity(v) {
  V.meshOpacity = v;
  V.meshMaterial.opacity = v;
  V.meshMaterial.depthWrite = v > 0.95;
  V.meshMaterial.transparent = v <= 0.95;
  V.meshMaterial.needsUpdate = true;
  for (const m of V.palette.values()) applyOpacity(m);
  V.dirty = true;
}

// ---- camera ----------------------------------------------------------------------------
export function frame(points, keepDir = true) {
  if (!points.length) return;
  const box = new THREE.Box3();
  for (const p of points) box.expandByPoint(new THREE.Vector3(...p));
  const c = box.getCenter(new THREE.Vector3());
  const r = Math.max(box.getSize(new THREE.Vector3()).length() / 2, 0.3);
  const dir = keepDir ? V.camera.position.clone().sub(V.controls.target).normalize() : new THREE.Vector3(0.6, -0.8, 0.45).normalize();
  const dist = r / Math.sin(V.camera.fov * Math.PI / 360) * 1.05;
  V.controls.target.copy(c);
  V.camera.position.copy(c.clone().add(dir.multiplyScalar(dist)));
  V.camera.near = Math.max(dist / 500, 0.005);
  V.camera.updateProjectionMatrix();
  V.controls.update();
  V.dirty = true;
}

export function viewPreset(name) {
  const t = V.controls.target.clone();
  const d = V.camera.position.distanceTo(t);
  const dirs = { front: [0, -1, 0], back: [0, 1, 0], side: [-1, 0, 0], left: [1, 0, 0], top: [0, -0.001, 1] };
  const v = new THREE.Vector3(...dirs[name]).normalize().multiplyScalar(d);
  V.camera.position.copy(t.clone().add(v));
  V.controls.update();
  V.dirty = true;
}

// ---- picking ---------------------------------------------------------------------------
const tmpV = new THREE.Vector3();
function toScreen(p) {
  tmpV.set(p[0], p[1], p[2]).project(V.camera);
  const r = V.renderer.domElement.getBoundingClientRect();
  return [(tmpV.x + 1) / 2 * r.width, (1 - tmpV.y) / 2 * r.height, tmpV.z];
}
export function screenOf(p) { return toScreen(p); }

export function pickNode(x, y, radius = 9) {
  if (!V.visible.nodes) return -1;
  const sc = S.scene;
  let best = -1, bestD = radius * radius, bestZ = 2;
  for (const i of V.drawNodes) {
    const [sx, sy, sz] = toScreen(sc.nodes[i].p);
    if (sz > 1 || sz < -1) continue;
    const d = (sx - x) ** 2 + (sy - y) ** 2;
    if (d < bestD - 4 || (d <= bestD + 4 && sz < bestZ)) { best = i; bestD = Math.min(d, bestD); bestZ = sz; }
  }
  return best;
}

export function pickBeam(x, y, radius = 5) {
  if (!V.visible.beams) return -1;
  const sc = S.scene;
  let best = -1, bestD = radius;
  for (const i of V.drawBeams) {
    const b = sc.beams[i];
    const A = toScreen(sc.nodes[b.a].p), B = toScreen(sc.nodes[b.b].p);
    if (A[2] > 1 || B[2] > 1) continue;
    const dx = B[0] - A[0], dy = B[1] - A[1];
    const L = dx * dx + dy * dy;
    let t = L ? ((x - A[0]) * dx + (y - A[1]) * dy) / L : 0;
    t = Math.max(0, Math.min(1, t));
    const d = Math.hypot(A[0] + t * dx - x, A[1] + t * dy - y);
    if (d < bestD) { bestD = d; best = i; }
  }
  return best;
}

const ray = new THREE.Raycaster();
export function pickTri(x, y) {
  if (!V.visible.tris || !V.triMesh) return -1;
  const r = V.renderer.domElement.getBoundingClientRect();
  ray.setFromCamera(new THREE.Vector2(x / r.width * 2 - 1, -(y / r.height) * 2 + 1), V.camera);
  const mat = V.triMesh.material;
  const side = mat.side;
  mat.side = THREE.DoubleSide;
  const hits = ray.intersectObject(V.triMesh);
  mat.side = side;
  if (!hits.length) return -1;
  return V.drawTris[hits[0].faceIndex];
}

export function nodesInRect(x0, y0, x1, y1) {
  const out = [];
  const [ax, bx] = [Math.min(x0, x1), Math.max(x0, x1)], [ay, by] = [Math.min(y0, y1), Math.max(y0, y1)];
  for (const i of V.drawNodes) {
    const [sx, sy, sz] = toScreen(S.scene.nodes[i].p);
    if (sz <= 1 && sx >= ax && sx <= bx && sy >= ay && sy <= by) out.push(i);
  }
  return out;
}

// a point under the mouse on the plane through `through` facing the camera
export function pointOnViewPlane(x, y, through) {
  const r = V.renderer.domElement.getBoundingClientRect();
  ray.setFromCamera(new THREE.Vector2(x / r.width * 2 - 1, -(y / r.height) * 2 + 1), V.camera);
  const n = V.camera.getWorldDirection(new THREE.Vector3());
  // snap the plane to the closest axis plane so new nodes line up with the view
  const ax = [Math.abs(n.x), Math.abs(n.y), Math.abs(n.z)];
  const k = ax.indexOf(Math.max(...ax));
  const normal = new THREE.Vector3(k === 0 ? 1 : 0, k === 1 ? 1 : 0, k === 2 ? 1 : 0);
  if (ax[k] < 0.92) normal.copy(n);
  const plane = new THREE.Plane().setFromNormalAndCoplanarPoint(normal, new THREE.Vector3(...through));
  const out = new THREE.Vector3();
  return ray.ray.intersectPlane(plane, out) ? [out.x, out.y, out.z] : null;
}

// ---- gizmo ------------------------------------------------------------------------------
export function updateGizmo() {
  if (V.importObj) {   // placing an imported model
    V.gizmo.attach(V.importObj);
    V.gizmo.setTranslationSnap(S.snap || null);
    V.dirty = true;
    return;
  }
  const sc = S.scene;
  const sel = [...S.sel.nodes].filter(i => sc && sc.nodes[i] && !sc.nodes[i].del);
  const show = sc && sel.length && S.tool === 'select';
  if (!show) {
    V.gizmo.detach();
    V.dirty = true;
    return;
  }
  const c = [0, 0, 0];
  for (const i of sel) for (let k = 0; k < 3; k++) c[k] += sc.nodes[i].p[k] / sel.length;
  if (!V.gizmo.dragging) V.pivot.position.set(...c);
  V.gizmo.attach(V.pivot);
  V.gizmo.setTranslationSnap(S.snap || null);
  V.dirty = true;
}

// ---- helper lines for the tools --------------------------------------------------------------
export function setOverlay(lines, points = []) {
  clearGroup(V.groups.overlay);
  if (lines.length) {
    const pos = new Float32Array(lines.length * 6);
    lines.forEach(([a, b], k) => { pos.set(a, k * 6); pos.set(b, k * 6 + 3); });
    const g = new THREE.BufferGeometry();
    g.setAttribute('position', new THREE.BufferAttribute(pos, 3));
    const l = new THREE.LineSegments(g, new THREE.LineDashedMaterial({ color: 0xffa733, dashSize: 0.04, gapSize: 0.03, depthTest: false }));
    l.computeLineDistances();
    l.renderOrder = 10;
    V.groups.overlay.add(l);
  }
  if (points.length) {
    const g = new THREE.BufferGeometry();
    g.setAttribute('position', new THREE.BufferAttribute(new Float32Array(points.flat()), 3));
    const p = new THREE.Points(g, new THREE.PointsMaterial({ size: V.nodeSize + 4, sizeAttenuation: false, color: 0xffa733,
      map: circleTex, alphaTest: 0.5, depthTest: false, transparent: true, opacity: 0.8 }));
    p.renderOrder = 11;
    V.groups.overlay.add(p);
  }
  V.dirty = true;
}

// ---- a structure that is about to be made (import, workshop) --------------------------------------
// fit: { pos: Map(id -> [x,y,z]), beams: { main: [[a,b]], rigid: [[a,b]] }, tris: [[a,b,c]] }; null clears it
export function showStructure(fit, { attach = [], tris = true } = {}) {
  clearGroup(V.groups.preview);
  if (fit) {
    const P = id => fit.pos.get(id);
    const pts = [...fit.pos.values()];
    const g = new THREE.BufferGeometry();
    g.setAttribute('position', new THREE.BufferAttribute(new Float32Array(pts.flat()), 3));
    const nodes = new THREE.Points(g, new THREE.PointsMaterial({ size: V.nodeSize + 1, sizeAttenuation: false, color: 0xffb547,
      map: circleTex, alphaTest: 0.5, depthTest: false, transparent: true }));
    nodes.renderOrder = 12;
    const lines = (list, color, opacity) => {
      if (!list.length) return null;
      const arr = new Float32Array(list.length * 6);
      list.forEach(([a, b], k) => { const A = P(a) || a, B = P(b) || b; arr.set(A, k * 6); arr.set(B, k * 6 + 3); });
      const lg = new THREE.BufferGeometry();
      lg.setAttribute('position', new THREE.BufferAttribute(arr, 3));
      const l = new THREE.LineSegments(lg, new THREE.LineBasicMaterial({ color, transparent: true, opacity, depthTest: false }));
      l.renderOrder = 11;
      return l;
    };
    const main = lines(fit.beams.main, 0xf29a2e, 0.75);
    const rig = lines(fit.beams.rigid, 0x4ea1ff, 0.35);
    const veh = S.scene ? attach.map(([vid, cid]) => {
      const i = S.scene.nodeById.get(vid);
      return i === undefined ? null : [S.scene.nodes[i].p, cid];
    }).filter(Boolean) : [];
    const at = lines(veh, 0x3fbf7f, 0.9);
    for (const o of [main, rig, at, nodes]) if (o) V.groups.preview.add(o);
    if (tris && fit.tris.length) {
      const arr = new Float32Array(fit.tris.length * 9);
      fit.tris.forEach((t, k) => t.forEach((id, v) => arr.set(P(id), k * 9 + v * 3)));
      const tg = new THREE.BufferGeometry();
      tg.setAttribute('position', new THREE.BufferAttribute(arr, 3));
      const m = new THREE.Mesh(tg, new THREE.MeshBasicMaterial({ color: 0x2fbf9a, transparent: true, opacity: 0.16, side: THREE.FrontSide, depthWrite: false }));
      m.renderOrder = 10;
      V.groups.preview.add(m);
    }
  }
  V.dirty = true;
}

// ---- labels ---------------------------------------------------------------------------------
function updateLabels() {
  const box = $('#labels');
  if (!S.scene || !V.visible.labels) { box.innerHTML = ''; return; }
  const ids = [...S.sel.nodes].slice(0, 150);
  if (V.hover && V.hover.kind === 'node' && !S.sel.nodes.has(V.hover.i)) ids.push(V.hover.i);
  const parts = [];
  for (const i of ids) {
    const n = S.scene.nodes[i];
    if (!n || n.del) continue;
    const [x, y, z] = toScreen(n.p);
    if (z > 1) continue;
    parts.push(`<div style="left:${x.toFixed(0)}px;top:${y.toFixed(0)}px">${n.id.replace(/</g, '&lt;')}</div>`);
  }
  box.innerHTML = parts.join('');
}
export function redraw() { V.dirty = true; }
