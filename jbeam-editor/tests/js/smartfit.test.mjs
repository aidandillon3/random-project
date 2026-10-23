import { test } from 'node:test';
import assert from 'node:assert/strict';
import { smartFit, assignWeights, isSymmetric, STIFFNESS, DT } from '../../web/js/smartfit.js';
import { box, sphere, plane } from './meshes.mjs';

const sub = (a, b) => [a[0] - b[0], a[1] - b[1], a[2] - b[2]];
const cross = (a, b) => [a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0]];
const dot = (a, b) => a[0] * b[0] + a[1] * b[1] + a[2] * b[2];

function components(fit) {
  const ids = [...fit.pos.keys()];
  const parent = new Map(ids.map(i => [i, i]));
  const find = i => { while (parent.get(i) !== i) i = parent.get(i); return i; };
  for (const [a, b] of [...fit.beams.main, ...fit.beams.rigid]) parent.set(find(a), find(b));
  return new Set(ids.map(find)).size;
}
function degrees(fit) {
  const d = new Map([...fit.pos.keys()].map(i => [i, 0]));
  for (const [a, b] of [...fit.beams.main, ...fit.beams.rigid]) { d.set(a, d.get(a) + 1); d.set(b, d.get(b) + 1); }
  return d;
}
function outwardShare(fit, centre) {
  let out = 0;
  for (const t of fit.tris) {
    const [a, b, c] = t.map(id => fit.pos.get(id));
    const n = cross(sub(b, a), sub(c, a));
    const m = [(a[0] + b[0] + c[0]) / 3, (a[1] + b[1] + c[1]) / 3, (a[2] + b[2] + c[2]) / 3];
    if (dot(n, sub(m, centre)) > 0) out++;
  }
  return out / fit.tris.length;
}

test('box: symmetric, braced, outward collision, unique names', () => {
  const [p, i] = box(0, 0.2, 0.6, 1.2, 0.6, 0.4, 6);
  assert.ok(isSymmetric(p, 0.01));
  const fit = smartFit(p, i, { detail: 'medium', prefix: 'bx' });
  assert.equal(fit.symmetric, true);
  assert.ok(fit.nodes.length >= 20 && fit.nodes.length <= 200, 'nodes ' + fit.nodes.length);
  assert.equal(new Set(fit.nodes.map(n => n[0])).size, fit.nodes.length);
  assert.equal(components(fit), 1);
  for (const [id, d] of degrees(fit)) assert.ok(d >= 3, id + ' has ' + d + ' beams');
  assert.ok(outwardShare(fit, [0, 0.2, 0.6]) > 0.97);
  // every left node has its right twin at the mirrored place
  for (const [id, q] of fit.pos) {
    if (!/l$/.test(id)) continue;
    const twin = fit.pos.get(id.replace(/l$/, 'r'));
    assert.ok(twin, 'twin of ' + id);
    assert.ok(Math.abs(twin[0] + q[0]) < 1e-6 && Math.abs(twin[1] - q[1]) < 1e-6 && Math.abs(twin[2] - q[2]) < 1e-6);
  }
  // corners of the box are kept (sharp features)
  const far = Math.max(...fit.nodes.map(n => Math.hypot(n[1], n[2] - 0.2, n[3] - 0.6)));
  assert.ok(far > 0.66, 'corner distance ' + far);
});

test('sphere: nodes sit on the surface', () => {
  const [p, i] = sphere(0.5, 48, 32, [0.3, 0, 1]);
  const fit = smartFit(p, i, { detail: 'medium' });
  for (const n of fit.nodes) {
    const r = Math.hypot(n[1] - 0.3, n[2], n[3] - 1);
    assert.ok(r > 0.4 && r < 0.53, 'radius ' + r);
  }
  assert.ok(outwardShare(fit, [0.3, 0, 1]) > 0.97);
  assert.equal(fit.symmetric, false);
  assert.ok(fit.nodes.every(n => !/[lr]$/.test(n[0])));
});

test('flat panel gets a rigidifier and stays one piece', () => {
  const [p, i] = plane(1.0, 0.8, 1.2, 8);
  const fit = smartFit(p, i, { detail: 'medium', inside: [0, 0, 0] });
  assert.equal(fit.rigidifiers.length, 1);
  const rig = fit.pos.get(fit.rigidifiers[0]);
  assert.ok(rig[2] < 1.2 - 0.1, 'rigidifier goes toward the inside');
  assert.equal(rig[0], 0);
  assert.equal(components(fit), 1);
});

test('two separate boxes are tied together', () => {
  const [p1, i1] = box(-0.5, 0, 0.5, 0.3, 0.3, 0.3, 3);
  const [p2, i2] = box(0.6, 0, 0.5, 0.3, 0.3, 0.3, 3);
  const p = Float32Array.from([...p1, ...p2]);
  const i = Uint32Array.from([...i1, ...[...i2].map(x => x + p1.length / 3)]);
  const fit = smartFit(p, i, { spacing: 0.1 });
  assert.equal(components(fit), 1);
});

test('weights: total near the mass and every node stable', () => {
  const [p, i] = box(0, 0, 0.5, 1, 1, 0.5, 4);
  const fit = smartFit(p, i, { detail: 'medium' });
  const st = STIFFNESS.panel;
  const { weights, total, needed, K, C } = assignWeights(fit, 30, { spring: st.spring, damp: st.damp });
  for (const [id, w] of weights) {
    const stiff = Math.sqrt(K.get(id) / w) * DT, damp = C.get(id) / w * DT;
    assert.ok(stiff < 2.05 && damp < 2.45, `${id}: ${w} kg, stiffness ${stiff.toFixed(2)}, damping ${damp.toFixed(2)}`);
  }
  // the part weighs what was asked for, or the least its beams need, whichever is more
  assert.ok(total >= Math.max(30, needed) * 0.99 && total <= Math.max(30, needed) * 1.6, `total ${total}, needed ${needed}`);
  const beamsPerNode = 2 * (fit.beams.main.length + fit.beams.rigid.length) / fit.nodes.length;
  assert.ok(beamsPerNode < 12.5, 'beams per node ' + beamsPerNode);
});

test('large meshes are simplified quickly and to a sensible size', () => {
  const [p, i] = sphere(1.0, 400, 250);
  const t0 = Date.now();
  const fit = smartFit(p, i, { detail: 'fine', maxNodes: 300 });
  assert.ok(Date.now() - t0 < 6000, 'took ' + (Date.now() - t0) + ' ms');
  assert.ok(fit.nodes.length <= 300 && fit.nodes.length > 40, 'nodes ' + fit.nodes.length);
});
