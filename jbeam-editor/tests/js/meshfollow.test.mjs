import { test } from 'node:test';
import assert from 'node:assert/strict';
import { apply, bind } from '../../web/js/meshfollow.js';

// nodes on a 3x3x3 lattice around a "bed", vertices scattered inside and around it
function setup() {
  const pts = [];
  for (const x of [-1, 0, 1]) for (const y of [-1.5, 0, 1.5]) for (const z of [0, 0.4, 0.8]) pts.push(x, y, z);
  const rest = [];
  let s = 7;
  const rnd = () => { s = (s * 16807) % 2147483647; return s / 2147483647; };
  for (let i = 0; i < 300; i++) rest.push(rnd() * 2.4 - 1.2, rnd() * 3.4 - 1.7, rnd() * 1.0 - 0.1);
  return { pts: Float32Array.from(pts), rest: Float32Array.from(rest) };
}
const moved = (pts, fn) => {
  const m = new Float32Array(pts.length);
  for (let j = 0; j < pts.length; j += 3) { const q = fn([pts[j], pts[j + 1], pts[j + 2]]); for (let k = 0; k < 3; k++) m[j + k] = q[k] - pts[j + k]; }
  return m;
};
const maxErr = (out, rest, fn) => {
  let e = 0;
  for (let v = 0; v < rest.length; v += 3) {
    const q = fn([rest[v], rest[v + 1], rest[v + 2]]);
    for (let k = 0; k < 3; k++) e = Math.max(e, Math.abs(out[v + k] - q[k]));
  }
  return e;
};

test('moving every node moves the model the same way', () => {
  const { pts, rest } = setup();
  const b = bind(rest, pts);
  const fn = p => [p[0] + 0.3, p[1] - 0.2, p[2] + 0.5];
  assert.ok(maxErr(apply(rest, b, moved(pts, fn), new Float32Array(rest.length)), rest, fn) < 1e-4);
});

test('stretching the nodes stretches the model (a wider bed)', () => {
  const { pts, rest } = setup();
  const b = bind(rest, pts);
  const fn = p => [p[0] * 1.3, p[1], p[2]];
  assert.ok(maxErr(apply(rest, b, moved(pts, fn), new Float32Array(rest.length)), rest, fn) < 0.01);
});

test('turning the nodes turns the model', () => {
  const { pts, rest } = setup();
  const b = bind(rest, pts);
  const a = 0.3, c = Math.cos(a), s = Math.sin(a);
  const fn = p => [c * p[0] - s * p[1], s * p[0] + c * p[1], p[2]];
  assert.ok(maxErr(apply(rest, b, moved(pts, fn), new Float32Array(rest.length)), rest, fn) < 0.01);
});

test('one moved node only bends the model near it', () => {
  const { pts, rest } = setup();
  const b = bind(rest, pts);
  const m = new Float32Array(pts.length);
  m[0 * 3 + 2] = 0.5;     // node (-1, -1.5, 0) up by half a metre
  const out = apply(rest, b, m, new Float32Array(rest.length));
  let near = 0, far = 0;
  for (let v = 0; v < rest.length; v += 3) {
    const d = Math.hypot(rest[v] + 1, rest[v + 1] + 1.5, rest[v + 2]);
    const dz = out[v + 2] - rest[v + 2];
    if (d < 0.3) near = Math.max(near, dz);
    if (d > 2) far = Math.max(far, Math.abs(dz));
  }
  assert.ok(near > 0.25, 'near ' + near);
  assert.ok(far < 0.05, 'far ' + far);
});

test('a flat group of nodes still works', () => {
  const pts = Float32Array.from([-1, -1, 0, 1, -1, 0, -1, 1, 0, 1, 1, 0, 0, 0, 0]);
  const rest = Float32Array.from([0.5, 0.5, 0.0, -0.2, 0.3, 0.05]);
  const b = bind(rest, pts);
  const fn = p => [p[0] * 1.5, p[1], p[2]];
  const out = apply(rest, b, moved(pts, fn), new Float32Array(rest.length));
  assert.ok(out.every(Number.isFinite));
  assert.ok(Math.abs(out[0] - 0.75) < 0.02 && Math.abs(out[3] + 0.3) < 0.02);
});
