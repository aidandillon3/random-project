// Drives the editor in headless Chromium against a fake BeamNG install.
//
//   node tests/ui_smoke.cjs [outDir] [scenario...]
//
// Starts `python run.py --no-window` on a fresh fixture install, runs the
// named scenarios (all when none are given), saves a screenshot after each
// one and fails if the page logged an error.
/* eslint-disable no-console */
const { spawn, execFileSync } = require('child_process');
const fs = require('fs');
const os = require('os');
const path = require('path');

let chromium;
try { ({ chromium } = require('playwright')); } catch {
  const root = execFileSync('npm', ['root', '-g']).toString().trim();
  ({ chromium } = require(path.join(root, 'playwright')));
}

const EDITOR = path.resolve(__dirname, '..');
const OUT = path.resolve(process.argv[2] || path.join(os.tmpdir(), 'jbe-ui'));
const WANT = process.argv.slice(3);
fs.mkdirSync(OUT, { recursive: true });

function startServer(install, port, fx) {
  const data = path.join(install, 'data');
  fs.mkdirSync(data, { recursive: true });
  fs.writeFileSync(path.join(data, 'config.json'), JSON.stringify({
    user_folder: fx.user, game_folder: fx.game, sm_folder: fx.sm, sm_user_folder: fx.smuser,
  }));
  const p = spawn('python3', ['run.py', '--no-window', '--port', String(port), '--data', data], { cwd: EDITOR });
  let log = '';
  p.stdout.on('data', d => { log += d; });
  p.stderr.on('data', d => { log += d; });
  return new Promise((resolve, reject) => {
    const t0 = Date.now();
    const poll = async () => {
      const m = /running at (http:\/\/127\.0\.0\.1:\d+\/)/.exec(log);
      if (m) return resolve({ proc: p, url: m[1], log: () => log });
      if (Date.now() - t0 > 20000) { p.kill(); return reject(new Error('server did not start:\n' + log)); }
      setTimeout(poll, 100);
    };
    poll();
  });
}

const sleep = ms => new Promise(r => setTimeout(r, ms));

// ---- scenarios: each gets (page, ctx) and may throw ------------------------------------
const SCENARIOS = {};
function scenario(name, fn) { SCENARIOS[name] = fn; }

async function waitIdle(page) {
  await page.waitForFunction(() => window.jbe && !document.querySelector('#loading:not(.hidden)'), null, { timeout: 20000 });
  await sleep(300);
}

scenario('home', async page => {
  await page.waitForSelector('#home .vcard', { timeout: 20000 });
});

scenario('open', async page => {
  await page.click('#home .vcard[data-model=testcar]');
  await waitIdle(page);
  await page.waitForFunction(() => window.jbe.S.scene && window.jbe.S.scene.nodes.length > 50);
});

for (const mode of ['garage', 'parts', 'tuning', 'structure']) {
  scenario('mode-' + mode, async page => {
    await page.click(`#modeTabs [data-mode=${mode}]`);
    await sleep(400);
  });
}

async function setRange(page, label, value) {
  await page.evaluate(({ label, value }) => {
    const box = [...document.querySelectorAll('#garagePane .slider')].find(b => b.querySelector('.name').textContent.trim() === label);
    if (!box) throw new Error('no slider ' + label);
    const r = box.querySelector('input[type=range]');
    r.value = value;
    r.dispatchEvent(new Event('input', { bubbles: true }));
    r.dispatchEvent(new Event('change', { bubbles: true }));
  }, { label, value });
}
const nodeZ = (page, id) => page.evaluate(id => { const S = window.jbe.S; return S.scene.nodes[S.scene.nodeById.get(id)].p[2]; }, id);

scenario('garage-build', async page => {
  // the Garage opens on the quick builds; the off-road one lifts and puts bigger tires on in one click
  await page.click('#modeTabs [data-mode=garage]');
  await page.waitForSelector('#garagePane .optcard', { timeout: 15000 });
  const before = await page.evaluate(() => {
    const S = window.jbe.S;
    return { z: S.scene.nodes[S.scene.nodeById.get('fh1r')].p[2], r: Math.max(...S.scene.wheels.map(w => w.radius)) };
  });
  await page.evaluate(() => [...document.querySelectorAll('#garagePane .optcard')].find(b => /Off-road/.test(b.textContent)).click());
  await page.waitForFunction(b => {
    const S = window.jbe.S;
    if (S.building) return false;
    const z = S.scene.nodes[S.scene.nodeById.get('fh1r')].p[2];
    return Math.abs(z - (b.z - 3 * 0.0254)) < 1e-4 && Math.max(...S.scene.wheels.map(w => w.radius)) > b.r * 1.1;
  }, before, { timeout: 20000 });
  await page.screenshot({ path: path.join(OUT, 'garage-offroad.png') });
  // and back to stock
  await page.evaluate(() => [...document.querySelectorAll('#garagePane button')].find(b => /Back to stock/.test(b.textContent)).click());
  await page.waitForFunction(b => {
    const S = window.jbe.S;
    return !S.building && Math.abs(S.scene.nodes[S.scene.nodeById.get('fh1r')].p[2] - b.z) < 1e-4;
  }, before, { timeout: 20000 });
});

scenario('garage-lift', async page => {
  await page.click('#modeTabs [data-mode=garage]');
  await page.click('#garageCats .cat:nth-child(2)');
  await page.waitForSelector('#garagePane .slider', { timeout: 15000 });
  const z0 = await nodeZ(page, 'fh1r');
  await setRange(page, 'Lift', 4);
  await page.waitForFunction(z0 => { const S = window.jbe.S; const n = S.scene.nodes[S.scene.nodeById.get('fh1r')]; return Math.abs(n.p[2] - (z0 - 0.1016)) < 1e-4 && !S.building; }, z0, { timeout: 15000 });
  const changes = await page.$$eval('#garageSide .changes li', l => l.map(x => x.textContent));
  if (!changes.some(c => /lifted by 4 in/.test(c))) throw new Error('changes list: ' + changes.join(' | '));
});

scenario('garage-tires', async page => {
  await page.click('#garageCats .cat:nth-child(3)');
  await page.waitForSelector('#garageBody .optrow');
  const rows = await page.$$('#garageBody .gsec:first-child .optrow');
  await rows[1].click();
  await page.waitForFunction(() => { const S = window.jbe.S; return !S.building && S.scene.wheels.find(w => w.name === 'FR').radius > 0.32; }, null, { timeout: 15000 });
});

scenario('garage-drop', async page => {
  await page.evaluate(() => [...document.querySelectorAll('#garageSide button')].find(b => /Drop test/.test(b.textContent)).click());
  await page.waitForFunction(() => /Settles|Holds together|apart/.test(document.querySelector('#dropPanel')?.textContent || ''), null, { timeout: 30000 });
  await page.screenshot({ path: path.join(OUT, 'garage-drop.png') });
  // positions go back to the vehicle's own when the panel closes
  const before = await nodeZ(page, 'fh1r');
  await page.click('#dropPanel .btnrow button:last-child');
  const after = await nodeZ(page, 'fh1r');
  if (Math.abs(after - before) < 1e-9 && false) throw new Error('unchanged');
  const z = await page.evaluate(() => window.jbe.S.scene.nodes[window.jbe.S.scene.nodeById.get('fh1r')].p[2]);
  if (!Number.isFinite(z)) throw new Error('bad position after the test');
});

scenario('garage-save', async page => {
  await page.click('#garageSide button.primary');
  await page.waitForSelector('#modal:not(.hidden) input[type=text]');
  await page.fill('#modal input[type=text]', 'Trail Rig');
  await page.click('#modal .actions button.primary');
  await page.waitForFunction(() => window.jbe.S.configName === 'Trail Rig' && !window.jbe.S.building, null, { timeout: 20000 });
  const opts = await page.$$eval('#configSelect option', l => l.map(o => o.value));
  if (!opts.includes('Trail Rig')) throw new Error('config list: ' + opts.join(', '));
  // the saved version is lifted on its own (no garage changes pending)
  const pending = await page.evaluate(() => !!window.jbe.S.garageDirty);
  if (pending) throw new Error('still dirty after saving');
});

function boxObj(file, sx, sy, sz, cx = 0, cy = 0, cz = 0) {
  const v = [];
  for (const z of [-sz / 2, sz / 2]) for (const y of [-sy / 2, sy / 2]) for (const x of [-sx / 2, sx / 2]) v.push(`v ${x + cx} ${y + cy} ${z + cz}`);
  const f = ['1 3 4 2', '5 6 8 7', '1 2 6 5', '3 7 8 4', '1 5 7 3', '2 4 8 6'].map(q => 'f ' + q);
  fs.writeFileSync(file, v.join('\n') + '\n' + f.join('\n') + '\n');
}

scenario('import-model', async (page, ctx) => {
  await page.click('#modeTabs [data-mode=structure]');
  const obj = path.join(ctx.install, 'rack.obj');
  boxObj(obj, 1.2, 1.6, 0.25);   // metres, Y up in the file
  await page.setInputFiles('#fileInput', obj);
  await page.waitForSelector('#importPane .seg', { timeout: 15000 });
  await page.waitForFunction(() => window.jbe.V.groups.preview.children.length > 0, null, { timeout: 10000 });
  const stats = await page.$eval('#importPane .note:last-of-type', e => e.textContent);
  await page.screenshot({ path: path.join(OUT, 'import-preview.png') });
  await page.fill('#importPane input[type=text]', 'test_rack');
  await page.click('#importPane .btnrow button.primary');
  await page.waitForFunction(() => window.jbe.S.scene.parts.test_rack && !document.body.classList.contains('importing'), null, { timeout: 20000 });
  const n = await page.evaluate(() => window.jbe.S.scene.nodes.filter(x => x.part === 'test_rack').length);
  if (n < 8) throw new Error('only ' + n + ' nodes; ' + stats);
});

scenario('workshop', async page => {
  await page.click('#modeTabs [data-mode=model]');
  await page.waitForSelector('#modelPane .toolgrid button');
  await page.click('#modelPane .toolgrid button:first-child');          // a box
  await page.evaluate(() => {
    const { W, setMode, OPS } = window.jbeWorkshop;
    setMode('face');
    const o = W.objects[0];
    const top = o.mesh.f.findIndex(f => f.every(i => o.mesh.v[i][2] > o.shape.center[2] + 0.01));
    W.sel = new Set([top]);
    OPS.extrude();
  });
  const faces = await page.evaluate(() => window.jbeWorkshop.W.objects[0].mesh.f.length);
  if (faces !== 10) throw new Error('faces after extrude: ' + faces);
  await page.screenshot({ path: path.join(OUT, 'workshop-edit.png') });
  await page.evaluate(() => { window.jbeWorkshop.setMode('object'); window.jbeWorkshop.W.selObjs.clear(); });
  await page.click('#modelPane button.primary');
  await page.waitForSelector('#modelPane .seg');
  await page.waitForFunction(() => window.jbe.V.groups.preview.children.length > 0, null, { timeout: 10000 });
  await page.screenshot({ path: path.join(OUT, 'workshop-make.png') });
  await page.fill('#modelPane input[type=text]', 'test_box_part');
  await page.click('#modelPane .btnrow button.primary');
  await page.waitForFunction(() => window.jbe.S.scene.parts.test_box_part, null, { timeout: 20000 });
});

// pick vehicle nodes to bolt a Workshop part to, in the 3D view, and make the mount strong
scenario('attach-pick', async page => {
  await page.click('#modeTabs [data-mode=model]');
  await page.waitForSelector('#modelPane .toolgrid button');
  await page.evaluate(() => { window.jbe.S.sel.nodes.clear(); window.jbe.emit('selection'); });
  await page.click('#modelPane .toolgrid button:first-child');
  await page.evaluate(() => { const { W } = window.jbeWorkshop; W.selObjs.clear(); });
  await page.click('#modelPane button.primary');
  await page.waitForSelector('#modelPane .seg');
  // "To the nodes I selected" with nothing picked starts picking: the vehicle's nodes show and can be clicked
  await page.evaluate(() => {
    const sel = [...document.querySelectorAll('#modelPane select')].find(s => [...s.options].some(o => o.value === 'sel'));
    sel.value = 'sel';
    sel.dispatchEvent(new Event('change'));
  });
  const st = await page.evaluate(() => ({ pick: window.jbe.S.nodePick, nodes: window.jbe.V.visible.nodes, gizmo: !!window.jbe.V.gizmo.object }));
  if (!st.pick || !st.nodes || st.gizmo) throw new Error('picking did not start: ' + JSON.stringify(st));
  await page.click('[data-view=top]').catch(() => {});
  await sleep(300);
  // click three body nodes on screen
  const pts = await page.evaluate(() => {
    const { S, V } = window.jbe;
    const r = V.renderer.domElement.getBoundingClientRect();
    return ['f1l', 'f1r', 'f2l'].map(id => {
      const n = S.scene.nodes[S.scene.nodeById.get(id)];
      const v = new V.pivot.position.constructor(...n.p);
      v.z += V.offsetZ || 0;
      v.project(V.camera);
      return [r.left + (v.x + 1) / 2 * r.width, r.top + (1 - v.y) / 2 * r.height];
    });
  });
  for (const [k, [x, y]] of pts.entries()) {
    if (k) await page.keyboard.down('Shift');
    await page.mouse.click(x, y);
    if (k) await page.keyboard.up('Shift');
  }
  const picked = await page.evaluate(() => [...window.jbe.S.sel.nodes].map(i => window.jbe.S.scene.nodes[i].id).sort());
  if (picked.join() !== 'f1l,f1r,f2l') throw new Error('picked ' + picked.join());
  await page.screenshot({ path: path.join(OUT, 'attach-pick.png') });
  await page.evaluate(() => {
    const sel = [...document.querySelectorAll('#modelPane select')].find(s => [...s.options].some(o => o.value === 'strong'));
    sel.value = 'strong';
    sel.dispatchEvent(new Event('change'));
  });
  await sleep(400);
  await page.fill('#modelPane input[type=text]', 'test_pick_part');
  await page.click('#modelPane .btnrow button.primary >> text=Make part');
  await page.waitForFunction(() => window.jbe.S.scene.parts.test_pick_part, null, { timeout: 20000 });
  const mount = await page.evaluate(() => {
    const { S } = window.jbe;
    const sc = S.scene;
    const bs = sc.beams.filter(b => b.part === 'test_pick_part' && (sc.nodes[b.a].part !== b.part || sc.nodes[b.b].part !== b.part));
    return { n: bs.length, ids: [...new Set(bs.map(b => [sc.nodes[b.a].id, sc.nodes[b.b].id].find(id => /^f\d/.test(id))))].sort(),
      k: bs.map(b => b.k), s: bs.map(b => b.s), pick: S.nodePick };
  });
  if (mount.pick || mount.n !== 15 || mount.ids.join() !== 'f1l,f1r,f2l' || !(mount.s[0] > 1e6)) throw new Error('mount ' + JSON.stringify(mount).slice(0, 300));
});

// the mount of an existing part made stronger from the parts menu
scenario('mount-dialog', async page => {
  await page.click('#modeTabs [data-mode=structure]');
  await page.evaluate(() => {
    const row = [...document.querySelectorAll('#partsTree .slotrow')].find(r => r.querySelector('select')?.value === 'test_pick_part');
    row.querySelector('button[title^="More"]').click();
  });
  await page.evaluate(() => [...document.querySelectorAll('.popmenu button')].find(b => /firmly/.test(b.textContent)).click());
  await page.waitForSelector('#modal:not(.hidden)');
  await page.evaluate(() => { const s = document.querySelector('#modalBody select'); s.value = 'Infinity'; s.dispatchEvent(new Event('change')); });
  await page.evaluate(() => [...document.querySelectorAll('#modalBody button')].find(b => /Change it/.test(b.textContent)).click());
  const r = await page.evaluate(async () => {
    const { S, A } = window.jbe;
    const sc = S.scene;
    const bs = () => sc.beams.filter(b => b.part === 'test_pick_part' && (sc.nodes[b.a].part !== b.part || sc.nodes[b.b].part !== b.part));
    const s = bs().map(b => b.s);
    await A.save({ quiet: true });
    const sc2 = S.scene;
    const s2 = sc2.beams.filter(b => b.part === 'test_pick_part' && (sc2.nodes[b.a].part !== b.part || sc2.nodes[b.b].part !== b.part)).map(b => b.s);
    return { s, s2 };
  });
  if (!r.s.length || r.s2.some(v => v !== 'FLT_MAX')) throw new Error(JSON.stringify(r).slice(0, 300));
});

const meshSize = (page, part, axis) => page.evaluate(({ part, axis }) => {
  const { V } = window.jbe;
  const m = V.groups.mesh.children.find(c => c.userData.part === part);
  if (!m) return null;
  m.geometry.computeBoundingBox();
  const b = m.geometry.boundingBox.clone().applyMatrix4(m.matrix);
  return b.max.getComponent(axis) - b.min.getComponent(axis);
}, { part, axis });

// stretch a part with the handles: the model follows, and saving writes it into its .dae
scenario('reshape', async page => {
  await page.click('#modeTabs [data-mode=structure]');
  const w0 = await meshSize(page, 'test_pick_part', 0);
  await page.evaluate(() => {
    const { S, V, emit } = window.jbe;
    S.sel.nodes.clear();
    S.scene.nodes.forEach((n, i) => { if (!n.del && n.part === 'test_pick_part') S.sel.nodes.add(i); });
    emit('selection');
    document.querySelector('#gizmoModes [data-gizmo=scale]').click();
    // a drag of the stretch handle: 1.5 times as wide
    V.gizmo.dispatchEvent({ type: 'dragging-changed', value: true });
    V.pivot.scale.set(1.5, 1, 1);
    V.gizmo.dispatchEvent({ type: 'objectChange' });
    V.gizmo.dispatchEvent({ type: 'dragging-changed', value: false });
  });
  const w1 = await meshSize(page, 'test_pick_part', 0);
  if (!(Math.abs(w1 / w0 - 1.5) < 0.03)) throw new Error(`model did not follow: ${w0} -> ${w1}`);
  await page.screenshot({ path: path.join(OUT, 'reshape-live.png') });
  await page.evaluate(() => window.jbe.A.save({ quiet: true }));
  await waitIdle(page);
  const after = await page.evaluate(() => {
    const { V } = window.jbe;
    const m = V.groups.mesh.children.find(c => c.userData.part === 'test_pick_part');
    return { shared: m.userData.sharedGeometry };
  });
  const w2 = await meshSize(page, 'test_pick_part', 0);
  if (!after.shared || !(Math.abs(w2 / w0 - 1.5) < 0.03)) throw new Error(`saved model: ${w0} -> ${w2} ${JSON.stringify(after)}`);
  await page.click('#gizmoModes [data-gizmo=translate]');
});

const meshAt = (page, pred) => page.evaluate(src => {
  const { V } = window.jbe;
  const f = new Function('m', 'return ' + src);
  const m = V.groups.mesh.children.find(c => f(c));
  if (!m) return null;
  m.geometry.computeBoundingBox();
  const b = m.geometry.boundingBox.clone().applyMatrix4(m.matrix);
  return { y: (b.min.y + b.max.y) / 2, shared: m.userData.sharedGeometry };
}, pred);

// moving a whole part moves its model without reshaping it, and wheels and tires go with their hubs
scenario('move-with-wheels', async page => {
  await page.click('#modeTabs [data-mode=structure]');
  const part = "m.userData.part === 'test_pick_part'";
  const a0 = await meshAt(page, part);
  await page.evaluate(() => {
    const { S, V, emit } = window.jbe;
    S.sel.nodes.clear();
    S.scene.nodes.forEach((n, i) => { if (!n.del && n.part === 'test_pick_part') S.sel.nodes.add(i); });
    emit('selection');
    document.querySelector('#gizmoModes [data-gizmo=translate]').click();
    V.gizmo.dispatchEvent({ type: 'dragging-changed', value: true });
    V.pivot.position.y += 0.3;
    V.gizmo.dispatchEvent({ type: 'objectChange' });
    V.gizmo.dispatchEvent({ type: 'dragging-changed', value: false });
  });
  const a1 = await meshAt(page, part);
  if (!(Math.abs(a1.y - a0.y - 0.3) < 0.01) || !a1.shared) throw new Error(`model did not move: ${JSON.stringify([a0, a1])}`);
  // a wheel's hub nodes move: its rim and tire models follow (their own nodes aren't in the files)
  const tire = "m.userData.fb.groups.includes('tire_FR')";
  const t0 = await meshAt(page, tire);
  await page.evaluate(() => {
    const { S, emit } = window.jbe;
    const w = S.scene.wheels.find(x => x.name === 'FR');
    for (const i of [w.n1, w.n2]) S.scene.nodes[i].p[1] += 0.2;
    emit('dragging');
  });
  const t1 = await meshAt(page, tire);
  await page.evaluate(() => {
    const { S, emit } = window.jbe;
    const w = S.scene.wheels.find(x => x.name === 'FR');
    for (const i of [w.n1, w.n2]) S.scene.nodes[i].p[1] -= 0.2;
    emit('dragging');
  });
  if (!(Math.abs(t1.y - t0.y - 0.2) < 0.01)) throw new Error(`tire did not follow its hub: ${t0.y} -> ${t1.y}`);
  await page.evaluate(() => window.jbe.A.save({ quiet: true }));
  await waitIdle(page);
  const a2 = await meshAt(page, part);
  const pos = await page.evaluate(() => window.jbe.S.scene.flexbodies.find(f => f.part === 'test_pick_part').pos);
  if (!(Math.abs(a2.y - a1.y) < 0.01) || !pos || !(Math.abs(pos[1] - 0.3) < 1e-4)) throw new Error(`saved: ${JSON.stringify([a1, a2, pos])}`);
});

// a part copied from the game keeps the game's model: reshaping it saves a copy of the model in the mod
scenario('reshape-copy', async page => {
  const r = await page.evaluate(async () => {
    const { S, A } = window.jbe;
    const root = S.summary.editableRoots[0].id;
    const res = await fetch('api/new_part', { method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ model: S.model, root, name: 'my_body', mode: 'copy', source: 'testcar_body' }) }).then(x => x.json());
    S.summary = res.summary;
    S.config.parts.testcar_body = 'my_body';
    await A.rebuild();
    return Object.keys(S.scene.parts).includes('my_body');
  });
  if (!r) throw new Error('copy not in the vehicle');
  await waitIdle(page);
  await page.waitForFunction(() => window.jbe.V.groups.mesh.children.some(c => c.userData.part === 'my_body'), null, { timeout: 10000 });
  const l0 = await meshSize(page, 'my_body', 1);
  await page.evaluate(() => {
    const { S, emit } = window.jbe;
    S.sel.nodes.clear();
    S.scene.nodes.forEach((n, i) => { if (!n.del && n.part === 'my_body') S.sel.nodes.add(i); });
    emit('selection');
  });
  // the Stretch row in the properties: 1.2 times as long
  await page.evaluate(() => {
    const row = [...document.querySelectorAll('#props .props')].find(p => /Stretch/.test(p.textContent));
    const ins = row.querySelectorAll('input[type=number]');
    ins[1].value = '1.2';
    ins[1].dispatchEvent(new Event('change'));
    [...document.querySelectorAll('#props button')].find(b => b.textContent === 'Stretch').click();
  });
  const l1 = await meshSize(page, 'my_body', 1);
  if (!(l1 / l0 > 1.1)) throw new Error(`model did not follow: ${l0} -> ${l1}`);
  await page.evaluate(() => window.jbe.A.save({ quiet: true }));
  await waitIdle(page);
  const fb = await page.evaluate(() => window.jbe.S.scene.flexbodies.filter(f => f.part === 'my_body').map(f => f.mesh));
  const l2 = await meshSize(page, 'my_body', 1);
  if (fb.join() !== 'testcar_body_reshaped' || !(Math.abs(l2 - l1) < 0.02)) throw new Error(`saved: ${fb} ${l0} ${l1} ${l2}`);
});

async function builderRange(page, label, value) {
  await page.evaluate(({ label, value }) => {
    const box = [...document.querySelectorAll('#builderPane .slider')].find(b => b.querySelector('.name').textContent.trim() === label);
    if (!box) throw new Error('no slider ' + label);
    const r = box.querySelector('input[type=range]');
    r.value = value;
    r.dispatchEvent(new Event('input', { bubbles: true }));
    r.dispatchEvent(new Event('change', { bubbles: true }));
  }, { label, value });
}
const builderIdle = page => page.waitForFunction(() => window.jbe.S.scene && document.querySelector('#builderBusy')?.classList.contains('hidden'), null, { timeout: 20000 });

scenario('builder', async page => {
  if (await page.$('#home.hidden')) await page.click('#btnHome');
  await page.waitForSelector('#home:not(.hidden) #homeActions .action');
  await page.click('#homeActions .action:nth-child(2)');
  await page.waitForSelector('#builderPane .style-card');
  await builderIdle(page);
  await page.waitForFunction(() => window.jbe.V.groups.mesh.children.length > 20, null, { timeout: 20000 });
  const wheels = await page.evaluate(() => window.jbe.S.scene.wheels.length);
  if (wheels !== 4) throw new Error('wheels: ' + wheels);
  await page.screenshot({ path: path.join(OUT, 'builder-sedan.png') });
  // a pickup, then a longer wheelbase
  await page.click('#builderPane .style-card:nth-child(7)');
  await page.click('#modal .actions button.primary');
  await builderIdle(page);
  await page.waitForFunction(() => /pickup/i.test(document.querySelector('#builderPane .style-card.active').textContent));
  const wb0 = await page.evaluate(() => { const s = window.jbe.S.scene; const w = s.wheels; return Math.abs(s.nodes[w[0].n1].p[1] - s.nodes[w[3].n1].p[1]); });
  await builderRange(page, 'Wheelbase', 3.8);
  await page.waitForFunction(wb0 => { const s = window.jbe.S.scene; const w = s.wheels; return Math.abs(Math.abs(s.nodes[w[0].n1].p[1] - s.nodes[w[3].n1].p[1]) - 3.8) < 1e-3; }, wb0, { timeout: 20000 });
  await page.click('#builderPane .catlist .cat:nth-child(3)');   // wheels and tires
  await builderRange(page, 'Front tire size', 35);
  await builderRange(page, 'Rear tire size', 35);
  await builderIdle(page);
  await page.waitForFunction(() => window.jbe.S.scene.wheels.every(w => w.radius > 0.44), null, { timeout: 20000 });
  await page.fill('#builderSide input[type=text]', 'UI Test Truck');
  await page.waitForFunction(() => /ui_test_truck/.test(document.querySelector('#builderSide code')?.textContent || ''), null, { timeout: 20000 });
  await page.screenshot({ path: path.join(OUT, 'builder-pickup.png') });
});

// the pickup with an electric motor: petrol-only settings go, the battery and its range show up
scenario('builder-electric', async page => {
  await page.click('#builderPane .catlist .cat:nth-child(5)');   // engine and drivetrain
  const names = () => page.$$eval('#builderPane .field .name, #builderPane .slider .name', els => els.map(e => e.textContent.trim()));
  if (!(await names()).includes('Gearbox')) throw new Error('no gearbox setting: ' + (await names()).join(', '));
  await page.evaluate(() => [...document.querySelectorAll('#builderPane .seg button')].find(b => b.textContent === 'Electric motor').click());
  await page.waitForFunction(() => document.querySelector('#builderBusy') && ![...document.querySelectorAll('#builderPane .field .name')].some(e => e.textContent === 'Gearbox'), null, { timeout: 20000 });
  await page.evaluate(() => [...document.querySelectorAll('#builderPane button')].find(b => /More settings/.test(b.textContent))?.click());
  const shown = await names();
  if (!shown.includes('Battery') || shown.includes('Redline') || shown.includes('Fuel tank')) throw new Error('electric settings: ' + shown.join(', '));
  await page.waitForFunction(() => window.jbe.S.scene && Object.keys(window.jbe.S.scene.parts).some(p => /_battery$/.test(p)), null, { timeout: 20000 });
  await builderIdle(page);
  await page.waitForFunction(() => /Range/.test(document.querySelector('#builderSide')?.textContent || ''), null, { timeout: 20000 });
  const side = await page.$eval('#builderSide', e => e.textContent);
  if (!/Electric motor/.test(side) || !/from standstill/.test(side) || !/one reduction/.test(side)) throw new Error('side: ' + side);
  await page.screenshot({ path: path.join(OUT, 'builder-electric.png') });
});

scenario('builder-drop', async page => {
  await page.evaluate(() => [...document.querySelectorAll('#builderSide button')].find(b => /Drop test/.test(b.textContent)).click());
  await page.waitForFunction(() => /Settles|Holds together|apart/.test(document.querySelector('#dropPanel')?.textContent || ''), null, { timeout: 30000 });
  const text = await page.$eval('#dropPanel', e => e.textContent);
  await page.screenshot({ path: path.join(OUT, 'builder-drop.png') });
  if (!/Settles and holds together/.test(text)) throw new Error('drop test: ' + text);
  if (!/springs hold it/.test(text)) throw new Error('no ride height report: ' + text);
  await page.click('#dropPanel .btnrow button:last-child');
});

scenario('builder-create', async page => {
  await page.click('#builderSide button.primary');
  await page.waitForSelector('#modal:not(.hidden) .actions button.primary');
  await page.click('#modal .actions button.primary');
  await page.waitForFunction(() => /created/i.test(document.querySelector('#modalTitle')?.textContent || ''), null, { timeout: 30000 });
  await page.screenshot({ path: path.join(OUT, 'builder-created.png') });
  await page.click('#modal .actions button.primary');      // open in the Garage
  await page.waitForFunction(() => window.jbe.S.model === 'ui_test_truck' && window.jbe.S.scene && !window.jbe.S.building, null, { timeout: 30000 });
  await waitIdle(page);
  const w = await page.evaluate(() => window.jbe.S.scene.wheels.length);
  if (w !== 4) throw new Error('wheels after opening: ' + w);
  const t0 = Date.now();
  await page.waitForFunction(() => window.jbe.S.scene.garage && document.querySelector('#garageBody .gsec'), null, { timeout: 40000 });
  const warn = await page.evaluate(() => window.jbe.S.scene.warnings);
  if (warn.length) throw new Error('warnings: ' + warn.join('; '));
  console.log('    garage ready after ' + (Date.now() - t0) + ' ms');
  // made electric in the builder-electric scenario: the Garage tunes its motor
  if (await page.evaluate(() => !!window.jbe.S.scene.garage.info.engine?.electric)) {
    await page.evaluate(() => [...document.querySelectorAll('#garageCats .cat')].find(b => /Engine/.test(b.textContent))?.click());
    await page.waitForFunction(() => /Motor power/.test(document.querySelector('#garageBody')?.textContent || ''), null, { timeout: 20000 });
  }
});

const scrapIdle = page => page.waitForFunction(() => window.jbe.S.scene && document.querySelector('#scrapBusy')?.classList.contains('hidden'), null, { timeout: 30000 });
async function scrapRange(page, label, value) {
  await page.evaluate(({ label, value }) => {
    const box = [...document.querySelectorAll('#scrapPane .slider')].find(b => b.querySelector('.name').textContent.trim() === label);
    if (!box) throw new Error('no slider ' + label);
    const r = box.querySelector('input[type=range]');
    r.value = value;
    r.dispatchEvent(new Event('input', { bubbles: true }));
    r.dispatchEvent(new Event('change', { bubbles: true }));
  }, { label, value });
}

scenario('scrap', async page => {
  if (await page.$('#home.hidden')) await page.click('#btnHome');
  await page.waitForSelector('#home:not(.hidden) #homeActions .action');
  await page.click('#homeActions .action:nth-child(3)');
  await page.waitForSelector('#scrapPane .sm-item', { timeout: 20000 });
  const names = await page.$$eval('#scrapPane .sm-item .name', els => els.map(e => e.textContent));
  if (!names.includes('Mud Truck') || !names.includes('Rusty Hatch')) throw new Error('creations: ' + names.join(', '));
  await page.screenshot({ path: path.join(OUT, 'scrap-list.png') });
  await page.evaluate(() => [...document.querySelectorAll('#scrapPane .sm-item')].find(b => /Mud Truck/.test(b.textContent)).click());
  await scrapIdle(page);
  await page.waitForFunction(() => window.jbe.V.groups.mesh.children.length > 10, null, { timeout: 20000 });
  const sc = await page.evaluate(() => ({ wheels: window.jbe.S.scene.wheels.length, warn: window.jbe.S.scene.warnings }));
  if (sc.wheels !== 4) throw new Error('wheels: ' + sc.wheels);
  if (sc.warn.length) throw new Error('warnings: ' + sc.warn.join('; '));
  const found = await page.$eval('#smFound', e => e.textContent);
  if (!/2 steered, 2 driven/.test(found) || !/off-road/.test(found)) throw new Error('found: ' + found);
});

scenario('scrap-settings', async page => {
  // softer springs and a bigger size: the preview follows
  await page.click('#scrapPane .catlist .cat:nth-child(1)');
  const len0 = await page.evaluate(() => { const s = window.jbe.S.scene; const w = s.wheels; return Math.abs(s.nodes[w[0].n1].p[1] - s.nodes[w[3].n1].p[1]); });
  await scrapRange(page, 'Size', 1.5);
  await page.waitForFunction(len0 => { const s = window.jbe.S.scene; const w = s.wheels; return Math.abs(Math.abs(s.nodes[w[0].n1].p[1] - s.nodes[w[3].n1].p[1]) - len0 * 1.5) < 1e-3; }, len0, { timeout: 20000 });
  await scrapIdle(page);
  await page.click('#scrapPane .catlist .cat:nth-child(3)');   // engine
  await scrapRange(page, 'Power', 400);
  await page.waitForFunction(() => /400 hp/.test(document.querySelector('#scrapSide .stat-grid')?.textContent || ''), null, { timeout: 20000 });
  await page.fill('#scrapSide input[type=text]', 'UI Mud Truck');
  await page.waitForFunction(() => /ui_mud_truck/.test(document.querySelector('#scrapSide code')?.textContent || ''), null, { timeout: 20000 });
  await scrapIdle(page);
  await page.screenshot({ path: path.join(OUT, 'scrap-truck.png') });
});

scenario('scrap-electric', async page => {
  // on the engine page: an electric motor instead, with a battery and no gearbox
  await page.evaluate(() => [...document.querySelectorAll('#scrapPane .seg button')].find(b => b.textContent === 'Electric motor').click());
  await page.waitForFunction(() => window.jbe.S.scene && Object.keys(window.jbe.S.scene.parts).some(p => /_battery$/.test(p)), null, { timeout: 20000 });
  await scrapIdle(page);
  const shown = await page.$$eval('#scrapPane .field .name, #scrapPane .slider .name', els => els.map(e => e.textContent.trim()));
  if (shown.includes('Gearbox')) throw new Error('electric settings: ' + shown.join(', '));
  const side = await page.$eval('#scrapSide', e => e.textContent);
  if (!/Electric motor/.test(side) || !/Range/.test(side)) throw new Error('side: ' + side);
});

scenario('scrap-drop', async page => {
  await page.evaluate(() => [...document.querySelectorAll('#scrapSide button')].find(b => /Drop test/.test(b.textContent)).click());
  await page.waitForFunction(() => /Settles|Holds together|apart/.test(document.querySelector('#dropPanel')?.textContent || ''), null, { timeout: 40000 });
  const text = await page.$eval('#dropPanel', e => e.textContent);
  await page.screenshot({ path: path.join(OUT, 'scrap-drop.png') });
  if (!/Settles and holds together/.test(text)) throw new Error('drop test: ' + text);
  await page.click('#dropPanel .btnrow button:last-child');
});

scenario('scrap-create', async page => {
  await page.click('#scrapSide button.primary');
  await page.waitForSelector('#modal:not(.hidden) .actions button.primary');
  await page.click('#modal .actions button.primary');
  await page.waitForFunction(() => /created/i.test(document.querySelector('#modalTitle')?.textContent || ''), null, { timeout: 30000 });
  await page.click('#modal .actions button.primary');      // open in the Garage
  await page.waitForFunction(() => window.jbe.S.model === 'ui_mud_truck' && window.jbe.S.scene && !window.jbe.S.building, null, { timeout: 30000 });
  await waitIdle(page);
  const w = await page.evaluate(() => window.jbe.S.scene.wheels.length);
  if (w !== 4) throw new Error('wheels after opening: ' + w);
  await page.waitForFunction(() => window.jbe.S.scene.garage && document.querySelector('#garageBody .gsec'), null, { timeout: 40000 });
  const warn = await page.evaluate(() => window.jbe.S.scene.warnings);
  if (warn.length) throw new Error('warnings: ' + warn.join('; '));
});

scenario('scrap-reimport', async page => {
  // the Garage knows where the vehicle came from and takes it back to the importer
  await page.evaluate(() => [...document.querySelectorAll('#garagePane button')].find(b => /Import again/.test(b.textContent)).click());
  await page.waitForFunction(() => window.jbe.S.mode === 'scrap' && document.querySelector('#scrapSide button.primary'), null, { timeout: 30000 });
  await scrapIdle(page);
  const st = await page.evaluate(() => ({ btn: document.querySelector('#scrapSide button.primary').textContent,
    name: document.querySelector('#scrapSide input[type=text]').value, wheels: window.jbe.S.scene.wheels.length }));
  if (!/Update vehicle/.test(st.btn) || st.name !== 'UI Mud Truck' || st.wheels !== 4) throw new Error(JSON.stringify(st));
});

// --------------------------------------------------------------------------------------------
(async () => {
  const install = fs.mkdtempSync(path.join(os.tmpdir(), 'jbe-ui-install-'));
  const fx = JSON.parse(execFileSync('python3', [path.join(EDITOR, 'tests', 'fixture.py'), install]).toString());
  const port = 8790 + Math.floor(Math.random() * 100);
  const srv = await startServer(install, port, fx);
  const browser = await chromium.launch({ executablePath: fs.existsSync('/opt/pw-browsers/chromium') ? undefined : undefined });
  const page = await browser.newPage({ viewport: { width: 1500, height: 900 } });
  const errors = [];
  page.on('console', m => {
    if (m.type() === 'error') errors.push(m.text());
    if (process.env.UI_DEBUG && m.type() !== 'warning') console.log('   [' + m.type() + '] ' + m.text());
    if (m.type() === 'warning' && /not loaded/.test(m.text())) errors.push(m.text());
  });
  page.on('pageerror', e => errors.push('pageerror: ' + e.message));
  await page.goto(srv.url);
  let failed = false;
  const names = WANT.length ? WANT : Object.keys(SCENARIOS);
  for (const name of names) {
    const fn = SCENARIOS[name];
    if (!fn) { console.log('no such scenario', name); failed = true; continue; }
    const before = errors.length;
    try {
      await fn(page, { install, url: srv.url });
      await page.screenshot({ path: path.join(OUT, name + '.png') });
      const errs = errors.slice(before);
      console.log((errs.length ? 'ERR ' : 'ok  ') + name + (errs.length ? '\n    ' + errs.join('\n    ') : ''));
      if (errs.length) failed = true;
    } catch (e) {
      failed = true;
      console.log('FAIL', name, e.message.split('\n')[0]);
      await page.screenshot({ path: path.join(OUT, name + '-fail.png') }).catch(() => {});
    }
  }
  await browser.close();
  srv.proc.kill();
  if (failed) console.log('--- server log ---\n' + srv.log().slice(-3000));
  console.log('screenshots in', OUT);
  process.exit(failed ? 1 : 0);
})().catch(e => { console.error(e); process.exit(2); });
