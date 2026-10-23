// Drop test: lower the vehicle onto the ground in a quick physics simulation, play it back in
// the 3D view, and say in plain words whether it sits where it was built to and holds together.
import * as THREE from 'three';
import { S } from './state.js';
import { V, buildWheels, refreshPositions, setVisible } from './view3d.js';
import { axleOf, dropTest } from './physics.js';
import { icon } from './icons.js';
import { $, h, fmt } from './util.js';

let running = null;

export function dropTestRunning() { return !!running; }

export async function runDropTest() {
  const sc = S.scene;
  if (!sc || running) return;
  if (!sc.wheels.length) { panel(h('p', {}, 'This vehicle has no wheels, so there is nothing to stand on.')); return; }
  const me = running = { stop: false };
  const prog = h('div', { class: 'progress' }, h('div', { class: 'bar' }));
  panel(h('p', {}, 'Simulating the first seconds after it touches the ground…'), prog);
  let res;
  try {
    res = await dropTest(sc, { seconds: 3, onProgress: f => { prog.firstChild.style.width = (f * 100).toFixed(0) + '%'; } });
  } catch (e) {
    running = null;
    panel(h('p', { class: 'err' }, 'The test failed: ' + e.message));
    return;
  }
  if (me.stop) { running = null; return; }
  await play(res, me);
  showResult(res);
}

// ---- playback ----------------------------------------------------------------------------------------------
async function play(res, me) {
  const sc = S.scene;
  const orig = sc.nodes.map(n => n.p);
  const start = res.sim.start;
  const anchors = meshAnchors(sc);
  const shown = { nodes: V.visible.nodes, beams: V.visible.beams, wheels: V.visible.wheels };
  setVisible('beams', true);
  setVisible('wheels', !V.groups.mesh.children.length);
  const frameAt = k => {
    const f = res.frames[k];
    sc.nodes.forEach((n, i) => { n.p = [f[i * 3], f[i * 3 + 1], f[i * 3 + 2]]; });
    refreshPositions();
    if (V.visible.wheels) buildWheels();
    for (const { mesh, idx, base } of anchors) {
      let dx = 0, dy = 0, dz = 0;
      for (const i of idx) { dx += f[i * 3] - start[i * 3]; dy += f[i * 3 + 1] - start[i * 3 + 1]; dz += f[i * 3 + 2] - start[i * 3 + 2]; }
      const m = idx.length || 1;
      mesh.matrix.copy(base).premultiply(new THREE.Matrix4().makeTranslation(dx / m, dy / m, dz / m));
    }
    V.dirty = true;
  };
  const restore = () => {
    sc.nodes.forEach((n, i) => { n.p = orig[i]; });
    refreshPositions();
    for (const { mesh, base } of anchors) mesh.matrix.copy(base);
    setVisible('beams', shown.beams);
    setVisible('wheels', shown.wheels);
    buildWheels();
    V.dirty = true;
  };
  me.restore = restore;
  me.frameAt = frameAt;
  me.frames = res.frames.length;
  const t0 = performance.now();
  await new Promise(resolve => {
    const tick = () => {
      if (me.stop) return resolve();
      const k = Math.min(res.frames.length - 1, Math.floor((performance.now() - t0) / 1000 * res.fps));
      frameAt(k);
      if (k >= res.frames.length - 1) return resolve();
      requestAnimationFrame(tick);
    };
    tick();
  });
}

// each mesh follows the nodes it is attached to (moved, not bent: enough to see it settle)
function meshAnchors(sc) {
  const byGroup = new Map();
  sc.nodes.forEach((n, i) => { for (const g of n.grp || []) { if (!byGroup.has(g)) byGroup.set(g, []); byGroup.get(g).push(i); } });
  const wheelNodes = {};
  for (const w of sc.wheels) wheelNodes[w.name] = [w.n1, w.n2];
  const out = [];
  for (const mesh of V.groups.mesh.children) {
    const fb = mesh.userData.fb;
    if (!fb) continue;
    let idx = [];
    for (const g of fb.groups) if (byGroup.has(g)) idx = idx.concat(byGroup.get(g));
    if (!idx.length) {
      for (const g of fb.groups) {
        const m = /^(?:wheel|tire|wheelhub)_(\w+)$/.exec(g);
        if (m && wheelNodes[m[1]]) { idx = wheelNodes[m[1]]; break; }
      }
    }
    out.push({ mesh, idx, base: mesh.matrix.clone() });
  }
  return out;
}

// ---- result ----------------------------------------------------------------------------------------------------
function showResult(res) {
  const s = res.summary;
  const rows = [];
  const cm = v => fmt(Math.abs(v) * 100, 1) + ' cm';
  let verdict;
  if (!s.stable) {
    verdict = h('p', { class: 'bad' }, icon('error', 16), ` It shook itself apart after ${fmt(s.time, 2)} s.`);
    rows.push(h('p', { class: 'note' }, 'Some node is too light for the springs on it, so the simulation (and the game) cannot keep it still. ' +
      'In Structure mode, turn on "Colour nodes by stability" to find it, then make it heavier or its beams softer.'));
  } else {
    const sag = id => {
      const w = res.sim.wheels.filter(x => axleOf(x.name) === id);
      if (!w.length || !s.axles[id]) return null;
      const st = res.sim.start, p = res.sim.pos;
      const wheel = w.reduce((t, x) => t + (p[x.n1 * 3 + 2] - st[x.n1 * 3 + 2] + p[x.n2 * 3 + 2] - st[x.n2 * 3 + 2]) / 2, 0) / w.length;
      return { body: s.axles[id].drop, tire: wheel, springs: s.axles[id].drop - wheel };
    };
    const ids = Object.keys(s.axles).sort((a, b) => s.axles[a].y - s.axles[b].y);
    const many = ids.length > 2;
    const label = (id, i) => id === 'F' && !i ? 'Front' : id === 'R' && i === ids.length - 1 ? 'Rear'
      : /^M\d+$/.test(id) ? (many && ids.filter(x => /^M/.test(x)).length > 1 ? 'Middle ' + id.slice(1) : 'Middle') : id;
    const list = ids.map((id, i) => [label(id, i), sag(id)]);
    const soft = list.some(([, x]) => x && x.springs < -0.03);
    const high = list.some(([, x]) => x && x.springs > 0.03);
    verdict = h('p', { class: soft || high || !s.settled ? 'warn' : 'good' }, icon(soft || high || !s.settled ? 'warning' : 'check', 16),
      s.settled ? ' Settles and holds together.' : ' Holds together, but is still bouncing after 3 s.');
    for (const [name, x] of list) {
      if (!x) continue;
      const springs = Math.abs(x.springs) < 0.003 ? 'right at the height it was built for'
        : x.springs < 0 ? `${cm(x.springs)} lower than built` : `${cm(x.springs)} higher than built`;
      rows.push(h('div', { class: 'row' }, h('b', {}, name), h('span', {}, `springs hold it ${springs}; tires squash ${cm(x.tire)}`)));
    }
    if (soft) rows.push(h('p', { class: 'note' }, 'The springs are too soft for the weight: raise the spring rate, or the spring height (Tuning).'));
    if (high) rows.push(h('p', { class: 'note' }, 'The springs push it up above its built height: lower the spring height or the spring rate.'));
    if (s.maxStrain > 0.01) rows.push(h('p', { class: 'note warn' }, `The body bends ${fmt(s.maxStrain * 100, 1)} % under its own weight: it needs more bracing.`));
  }
  panel(verdict, ...rows,
    h('div', { class: 'btnrow' },
      h('button', { class: 'small', onclick: () => replay(res) }, icon('play', 14), 'Replay'),
      h('button', { class: 'small', onclick: () => closePanel() }, 'Close')));
}

async function replay(res) {
  const me = running;
  if (!me || !me.frameAt) return;
  const t0 = performance.now();
  me.stop = false;
  await new Promise(resolve => {
    const tick = () => {
      if (me.stop) return resolve();
      const k = Math.min(me.frames - 1, Math.floor((performance.now() - t0) / 1000 * res.fps));
      me.frameAt(k);
      if (k >= me.frames - 1) return resolve();
      requestAnimationFrame(tick);
    };
    tick();
  });
}

function panel(...kids) {
  let box = $('#dropPanel');
  if (!box) {
    box = h('div', { id: 'dropPanel', class: 'floatcard' });
    $('#center').append(box);
  }
  box.innerHTML = '';
  box.append(h('div', { class: 'head' }, icon('physics', 16), h('b', {}, 'Drop test'),
    h('button', { class: 'icon-btn small', title: 'Close', onclick: () => closePanel() }, icon('close', 14))), ...kids);
  box.classList.remove('hidden');
}

export function closePanel() {
  const me = running;
  if (me) {
    me.stop = true;
    me.restore?.();
  }
  running = null;
  $('#dropPanel')?.classList.add('hidden');
}
