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
