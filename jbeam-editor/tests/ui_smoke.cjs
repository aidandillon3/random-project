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
