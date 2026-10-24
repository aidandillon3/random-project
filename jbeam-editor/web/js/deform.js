// The 3D model follows moved nodes. In the game a flexbody's vertices are tied to
// the nodes of its groups where both are in the files, so moving nodes alone leaves
// the model behind; here every vertex is carried along by the nodes near it (a
// moving-least-squares fit, so a stretch, turn or move of a whole part is followed
// exactly and a single moved node bends the model around it). Saving writes the
// new shape into the model's .dae (see editor/daeedit.py).
import * as THREE from 'three';
import { S } from './state.js';
import { V } from './view3d.js';
import { apply, bind } from './meshfollow.js';

const bindings = new Map();  // mesh object -> { rest, nb, coef, nodes }

let follow = true;
try { follow = localStorage.getItem('jbe.modelFollows') !== '0'; } catch { /* storage off */ }

export function followOn() { return follow; }
export function setFollow(on) {
  follow = !!on;
  try { localStorage.setItem('jbe.modelFollows', follow ? '1' : '0'); } catch { /* storage off */ }
  updateDeform();
}

// node positions as they are in the files (and so where the models were made to fit)
export function markRest(sc) {
  for (const n of sc.nodes) n.p0 = n.p.slice();
  bindings.clear();
}

// the drawn models were made again (their bindings belong to the old ones)
export function forgetBindings() { bindings.clear(); }

function groupNodes(fb) {
  const sc = S.scene;
  const want = new Set(fb.groups);
  const out = [];
  sc.nodes.forEach((n, i) => { if (!n.del && n.p0 && n.grp.some(g => want.has(g))) out.push(i); });
  return out;
}

const usedTwice = () => {
  const count = new Map();
  for (const fb of S.scene.flexbodies) count.set(fb.mesh, (count.get(fb.mesh) || 0) + 1);
  return count;
};

function movesOf(nodes) {
  const sc = S.scene;
  const m = new Float32Array(nodes.length * 3);
  let any = false;
  nodes.forEach((i, j) => {
    const n = sc.nodes[i];
    for (let q = 0; q < 3; q++) {
      m[j * 3 + q] = n.p[q] - n.p0[q];
      if (Math.abs(m[j * 3 + q]) > 1e-6) any = true;
    }
  });
  return any ? m : null;
}

function restore(mesh) {
  if (mesh.userData.sharedGeometry) return;
  mesh.geometry.dispose();
  mesh.geometry = V.meshCache.get(mesh.userData.fb.mesh);
  mesh.userData.sharedGeometry = true;
}

// bend the drawn models to where their nodes are now
export function updateDeform() {
  const sc = S.scene;
  if (!sc || !V.groups.mesh) return;
  const twice = usedTwice();
  for (const mesh of V.groups.mesh.children) {
    const fb = mesh.userData.fb;
    const base = fb && V.meshCache.get(fb.mesh);
    if (!follow || !base || twice.get(fb.mesh) > 1) { restore(mesh); continue; }
    let bd = bindings.get(mesh);
    if (!bd) {
      const nodes = groupNodes(fb);
      if (!nodes.length || !movesOf(nodes)) { restore(mesh); continue; }
      mesh.updateMatrix();
      const src = base.attributes.position.array;
      const rest = new Float32Array(src.length);
      const v = new THREE.Vector3();
      for (let i = 0; i < src.length; i += 3) { v.set(src[i], src[i + 1], src[i + 2]).applyMatrix4(mesh.matrix); rest[i] = v.x; rest[i + 1] = v.y; rest[i + 2] = v.z; }
      const pts = new Float32Array(nodes.length * 3);
      nodes.forEach((i, j) => pts.set(sc.nodes[i].p0, j * 3));
      bd = { nodes, rest, inv: mesh.matrix.clone().invert(), ...bind(rest, pts) };
      bindings.set(mesh, bd);
    }
    const moves = movesOf(bd.nodes);
    if (!moves) { restore(mesh); continue; }
    if (mesh.userData.sharedGeometry) {
      const g = new THREE.BufferGeometry();
      g.setAttribute('position', new THREE.BufferAttribute(new Float32Array(base.attributes.position.array.length), 3));
      g.setIndex(base.index);
      for (const gr of base.groups) g.addGroup(gr.start, gr.count, gr.materialIndex);
      g.userData = base.userData;
      mesh.geometry = g;
      mesh.userData.sharedGeometry = false;
    }
    const pos = mesh.geometry.attributes.position;
    apply(bd.rest, bd, moves, pos.array);
    const v = new THREE.Vector3();
    for (let i = 0; i < pos.array.length; i += 3) {
      v.set(pos.array[i], pos.array[i + 1], pos.array[i + 2]).applyMatrix4(bd.inv);
      pos.array[i] = v.x; pos.array[i + 1] = v.y; pos.array[i + 2] = v.z;
    }
    pos.needsUpdate = true;
    mesh.geometry.computeVertexNormals();
    mesh.geometry.computeBoundingSphere();
  }
  V.dirty = true;
}

// the models whose shape changed, for saving: [{ mesh, pos (Float32Array, in the file's space), f, part, row }]
export function reshapedMeshes() {
  const out = [];
  if (!follow || !V.groups.mesh) return out;
  for (const mesh of V.groups.mesh.children) {
    if (mesh.userData.sharedGeometry) continue;
    const fb = mesh.userData.fb;
    out.push({ mesh: fb.mesh, pos: Float32Array.from(mesh.geometry.attributes.position.array), f: S.scene.files[fb.f]?.key, part: fb.part, row: fb.row });
  }
  return out;
}
