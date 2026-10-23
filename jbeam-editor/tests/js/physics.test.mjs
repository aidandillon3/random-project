// Drop test physics: generated vehicles settle at their design height; a structure that is too
// light for its beams is caught as unstable.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { execFileSync } from 'node:child_process';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { dropTest, prepare, step } from '../../web/js/physics.js';

const EDITOR = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..', '..');

function scenes(styles, extra = {}) {
  const code = `
import sys, json
sys.path.insert(0, ${JSON.stringify(EDITOR)})
from editor import vehiclegen, build, model
out = {}
for st in ${JSON.stringify(styles)}:
    spec = {"style": st}
    spec.update(json.loads(${JSON.stringify(JSON.stringify(extra))}))
    g = vehiclegen.generate(spec)
    out[st] = build.build(model.Model.from_parts(None, g.P.model, g.parts), {})
print(json.dumps(out))
`;
  return JSON.parse(execFileSync('python3', ['-c', code], { maxBuffer: 1 << 28 }).toString());
}

test('generated vehicles settle at the height they were built for', async () => {
  const all = scenes(['sedan', 'offroad', 'pickup', 'supercar']);
  for (const [name, sc] of Object.entries(all)) {
    const { summary: s, sim } = await dropTest(sc, { seconds: 3 });
    assert.ok(s.stable, name + ' exploded');
    assert.ok(s.settled, name + ' still moving: ' + s.endSpeed);
    for (const id of ['F', 'R']) {
      const wheels = sim.wheels.filter(w => w.name[0] === id);
      const wheelDrop = wheels.reduce((t, w) => t + (sim.pos[w.n1 * 3 + 2] - sim.start[w.n1 * 3 + 2] + sim.pos[w.n2 * 3 + 2] - sim.start[w.n2 * 3 + 2]) / 2, 0) / wheels.length;
      const sag = s.axles[id].drop - wheelDrop;   // how much the springs compressed beyond the design
      assert.ok(Math.abs(sag) < 0.015, `${name} ${id} suspension sag ${sag}`);
      assert.ok(wheelDrop < 0 && wheelDrop > -0.05, `${name} ${id} tire squash ${wheelDrop}`);
    }
    assert.ok(s.maxStrain < 0.01, name + ' body bends ' + s.maxStrain);
  }
});

test('a structure too light for its beams is unstable', () => {
  const sc = scenes(['hatchback']).hatchback;
  for (const n of sc.nodes) n.w *= 0.004;
  for (const w of sc.wheels) w.mass *= 0.004;
  const sim = prepare(sc);
  let ok = true;
  for (let i = 0; i < 4000 && ok; i++) ok = step(sim, 1);
  assert.equal(ok, false);
  assert.equal(sim.broken, true);
});
