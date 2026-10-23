import { test } from 'node:test';
import assert from 'node:assert/strict';
import * as P from '../../web/js/polymesh.js';

const close = (a, b, eps = 1e-6) => Math.abs(a - b) <= eps * Math.max(1, Math.abs(b));

test('shapes are closed and face outward', () => {
  const cases = {
    box: [P.box({ w: 2, l: 3, h: 1, seg: [2, 3, 1] }), 6],
    cylinder: [P.cylinder({ r: 1, h: 2, sides: 64 }), Math.PI * 2 * 0.998],
    cone: [P.cone({ r1: 1, r2: 0, h: 3, sides: 64 }), Math.PI * 0.998],
    frustum: [P.cone({ r1: 1, r2: 0.5, h: 1, sides: 64, axis: 'x' }), null],
    sphere: [P.sphere({ r: 1, seg: 48, rings: 24 }), 4 / 3 * Math.PI * 0.98],
    wedge: [P.wedge({ w: 2, l: 2, h: 1 }), 2],
    tube: [P.tube({ r: 1, t: 0.2, h: 1, sides: 64, axis: 'y' }), Math.PI * (1 - 0.64) * 0.998],
    torus: [P.torus({ R: 1, r: 0.25, seg: 48, sides: 24 }), 2 * Math.PI * Math.PI * 1 * 0.0625 * 0.98],
  };
  for (const [name, [m, vol]] of Object.entries(cases)) {
    assert.ok(P.isClosed(m), name + ' is closed');
    const v = P.volume(m);
    assert.ok(v > 0, name + ' faces outward (volume ' + v + ')');
    if (vol) assert.ok(v >= vol * 0.97 && v <= vol * 1.03, `${name}: volume ${v}, expected about ${vol}`);
  }
});

test('extrude adds a block and stays closed', () => {
  const m = P.box({ w: 1, l: 1, h: 1 });
  const top = m.f.findIndex(f => P.faceNormal(m, f)[2] > 0.5);
  const { mesh, faces } = P.extrude(m, [top], 0.5);
  assert.ok(P.isClosed(mesh));
  assert.ok(close(P.volume(mesh), 1.5));
  assert.ok(close(P.bounds(mesh).hi[2], 1.0));
  assert.deepEqual(faces, [top]);
});

test('extruding two neighbouring faces makes one block', () => {
  const m = P.box({ w: 2, l: 1, h: 1, seg: [2, 1, 1] });
  const tops = m.f.map((f, i) => [i, P.faceNormal(m, f)[2]]).filter(([, z]) => z > 0.5).map(([i]) => i);
  assert.equal(tops.length, 2);
  const { mesh } = P.extrude(m, tops, 1);
  assert.ok(P.isClosed(mesh));
  assert.ok(close(P.volume(mesh), 4));
  // 2 x 1 x 1 box (6 faces made of 10 quads) + 6 side walls, no wall between the two lifted faces
  assert.equal(mesh.f.length, m.f.length + 6);
});

test('inset and subdivide keep the shape', () => {
  const m = P.box({ w: 1, l: 2, h: 0.5 });
  const { mesh: a } = P.inset(m, [0, 1], 0.3);
  assert.ok(P.isClosed(a));
  assert.ok(close(P.volume(a), 1));
  const b = P.subdivide(m, [0]);
  assert.ok(P.isClosed(b));
  assert.ok(close(P.volume(b), 1));
  const c = P.subdivide(P.sphere({ r: 1, seg: 8, rings: 4 }));
  assert.ok(P.isClosed(c));
});

test('mirror makes a symmetric closed mesh from a half', () => {
  const half = P.box({ w: 1, l: 1, h: 1, center: [0.5, 0, 0] });
  const open = P.deleteFaces(half, half.f.map((f, i) => [i, P.faceCenter(half, f)[0]]).filter(([, x]) => x < 1e-9).map(([i]) => i));
  assert.ok(!P.isClosed(open));
  const full = P.mirrorX(open);
  assert.ok(P.isClosed(full));
  assert.ok(close(P.volume(full), 2));
  const b = P.bounds(full);
  assert.ok(close(b.lo[0], -1) && close(b.hi[0], 1));
});

test('weld, flip, triangulate', () => {
  const a = P.box({ w: 1, l: 1, h: 1 });
  const twice = P.join(a, P.box({ w: 1, l: 1, h: 1, center: [1, 0, 0] }));
  assert.equal(P.weld(twice).v.length, 12);
  const f = P.flip(a);
  assert.ok(P.volume(f) < 0);
  // a concave L-shaped face
  const L = { v: [[0, 0, 0], [2, 0, 0], [2, 1, 0], [1, 1, 0], [1, 2, 0], [0, 2, 0]], f: [[0, 1, 2, 3, 4, 5]] };
  const t = P.triangulate(L);
  assert.equal(t.idx.length, 12);
  let area = 0;
  for (let i = 0; i < t.idx.length; i += 3) {
    const p = k => [t.pos[t.idx[i + k] * 3], t.pos[t.idx[i + k] * 3 + 1]];
    const [A, B, C] = [p(0), p(1), p(2)];
    area += ((B[0] - A[0]) * (C[1] - A[1]) - (C[0] - A[0]) * (B[1] - A[1])) / 2;
  }
  assert.ok(close(area, 3));
});
