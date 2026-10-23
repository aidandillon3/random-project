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

function startServer(install, port) {
  const data = path.join(install, 'data');
  fs.mkdirSync(data, { recursive: true });
  fs.writeFileSync(path.join(data, 'config.json'), JSON.stringify({
    user_folder: path.join(install, 'user'), game_folder: path.join(install, 'game'),
    sm_folder: path.join(install, 'sm'), sm_user_folder: path.join(install, 'smuser'),
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

scenario('garage-lift', async page => {
  await page.click('#modeTabs [data-mode=garage]');
  await page.waitForSelector('#garagePane .slider', { timeout: 15000 });
  const z0 = await nodeZ(page, 'fh1r');
  await setRange(page, 'Lift', 4);
  await page.waitForFunction(z0 => { const S = window.jbe.S; const n = S.scene.nodes[S.scene.nodeById.get('fh1r')]; return Math.abs(n.p[2] - (z0 - 0.1016)) < 1e-4 && !S.building; }, z0, { timeout: 15000 });
  const changes = await page.$$eval('#garageSide .changes li', l => l.map(x => x.textContent));
  if (!changes.some(c => /lifted by 4 in/.test(c))) throw new Error('changes list: ' + changes.join(' | '));
});

scenario('garage-tires', async page => {
  await page.click('#garageCats .cat:nth-child(2)');
  await page.waitForSelector('#garageBody .optrow');
  const rows = await page.$$('#garageBody .gsec:first-child .optrow');
  await rows[1].click();
  await page.waitForFunction(() => { const S = window.jbe.S; return !S.building && S.scene.wheels.find(w => w.name === 'FR').radius > 0.32; }, null, { timeout: 15000 });
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

// --------------------------------------------------------------------------------------------
(async () => {
  const install = fs.mkdtempSync(path.join(os.tmpdir(), 'jbe-ui-install-'));
  execFileSync('python3', [path.join(EDITOR, 'tests', 'fixture.py'), install]);
  const port = 8790 + Math.floor(Math.random() * 100);
  const srv = await startServer(install, port);
  const browser = await chromium.launch({ executablePath: fs.existsSync('/opt/pw-browsers/chromium') ? undefined : undefined });
  const page = await browser.newPage({ viewport: { width: 1500, height: 900 } });
  const errors = [];
  page.on('console', m => { if (m.type() === 'error') errors.push(m.text()); });
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
