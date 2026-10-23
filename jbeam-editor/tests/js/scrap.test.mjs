// Scrap Mechanic import: converted creations stand on their wheels at the height they were built,
// whatever the layout (car, off-road truck on suspension, trike, six-wheeler).
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { execFileSync } from 'node:child_process';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { axleOf, dropTest } from '../../web/js/physics.js';

const EDITOR = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..', '..');

function scenes(kinds) {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'jbe-sm-'));
  const code = `
import sys, os, json
sys.path.insert(0, ${JSON.stringify(EDITOR)})
sys.path.insert(0, ${JSON.stringify(path.join(EDITOR, 'tests'))})
import smfixture as F
from editor import smimport, build, model
root = ${JSON.stringify(root)}
gf = F.make_game(root)
make = {"car": lambda: F.car()[0], "truck": lambda: F.car(suspension="offroad", wheel=F.BIG_WHEEL)[0],
        "trike": F.trike, "six": F.six_wheeler, "solid": F.solid_axles, "solid_off": lambda: F.solid_axles(1),
        "electric": lambda: F.car(engine=F.ELECTRIC_ENGINE)[0]}
imp = smimport.Importer()
out = {}
for k in ${JSON.stringify(kinds)}:
    path = F.save_creation(os.path.join(root, k), make[k](), k)
    g = imp.generate(path, gf, {})
    out[k] = build.build(model.Model.from_parts(None, g.P.model, g.parts), {})
print(json.dumps(out))
`;
  try {
    return JSON.parse(execFileSync('python3', ['-c', code], { maxBuffer: 1 << 28 }).toString());
  } finally {
    fs.rmSync(root, { recursive: true, force: true });
  }
}

test('imported creations settle at the height they were built', async () => {
  const all = scenes(['car', 'truck', 'trike', 'six', 'solid', 'solid_off', 'electric']);
  for (const [name, sc] of Object.entries(all)) {
    assert.deepEqual(sc.warnings, [], name);
    const { summary: s, sim } = await dropTest(sc, { seconds: 3 });
    assert.ok(s.stable, name + ' exploded');
    assert.ok(s.settled, name + ' still moving: ' + s.endSpeed);
    for (const [id, ax] of Object.entries(s.axles)) {
      const wheels = sim.wheels.filter(w => axleOf(w.name) === id);
      const wheelDrop = wheels.reduce((t, w) => t + (sim.pos[w.n1 * 3 + 2] - sim.start[w.n1 * 3 + 2] + sim.pos[w.n2 * 3 + 2] - sim.start[w.n2 * 3 + 2]) / 2, 0) / wheels.length;
      const sag = ax.drop - wheelDrop;
      assert.ok(Math.abs(sag) < 0.025, `${name} ${id} suspension sag ${sag}`);
      assert.ok(wheelDrop < 0 && wheelDrop > -0.06, `${name} ${id} tire squash ${wheelDrop}`);
    }
    assert.ok(s.maxStrain < 0.01, name + ' body bends ' + s.maxStrain);
  }
});

test('extra axles are told apart', () => {
  assert.equal(axleOf('FL'), 'F');
  assert.equal(axleOf('RR'), 'R');
  assert.equal(axleOf('M1L'), 'M1');
  assert.equal(axleOf('F2R'), 'F2');
  assert.equal(axleOf('RR1'), 'R');
});
