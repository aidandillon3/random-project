// A small soft-body simulation of a built vehicle, in the spirit of the game's physics (much
// simplified): nodes with mass, beams as springs and dampers (with precompression, bump stops
// and support beams), gravity, the ground, and each wheel as a tire spring under its axle.
// The drop test uses it to show whether a vehicle settles at the height it was built for and
// whether any part of its structure shakes itself apart. No DOM here, so tests can run it too.

export const DT = 0.0005;            // the game's 2000 Hz physics step
const G = 9.81;

function num(v, d) {
  return typeof v === 'number' && Number.isFinite(v) ? v : d;
}

// scene: the editor's built vehicle (nodes, beams, wheels, optsets)
export function prepare(scene) {
  const nodes = scene.nodes;
  const n = nodes.length;
  const pos = new Float64Array(n * 3), vel = new Float64Array(n * 3), frc = new Float64Array(n * 3);
  const mass = new Float64Array(n), coll = new Uint8Array(n), live = new Uint8Array(n);
  for (let i = 0; i < n; i++) {
    const nd = nodes[i];
    pos.set(nd.p, i * 3);
    live[i] = nd.del ? 0 : 1;
    mass[i] = nd.del ? 1 : Math.max(num(nd.w, 25), 0.001);
    coll[i] = nd.col === false ? 0 : 1;
  }
  const opts = (b) => Object.assign({}, scene.optsets?.[b.bo] || {}, b.io || {});
  const beams = [];
  for (const b of scene.beams) {
    if (b.del || !live[b.a] || !live[b.b]) continue;
    const o = opts(b);
    const dx = pos[b.b * 3] - pos[b.a * 3], dy = pos[b.b * 3 + 1] - pos[b.a * 3 + 1], dz = pos[b.b * 3 + 2] - pos[b.a * 3 + 2];
    const L = Math.hypot(dx, dy, dz);
    if (L < 1e-6) continue;
    const range = num(o.precompressionRange, null);
    const L0 = range !== null ? L + range : L * num(o.beamPrecompression, 1);
    const type = String(b.t || o.beamType || 'NORMAL').replace('|', '').toUpperCase();
    const c = num(b.c, num(o.beamDamp, 0));
    const beam = {
      a: b.a, b: b.b, L, L0, type,
      k: num(b.k, num(o.beamSpring, 0)), c, cReb: num(o.beamDampRebound, c),
    };
    if (type === 'BOUNDED') {
      beam.longR = num(o.longBoundRange, num(o.beamLongBound, 1) * L);
      beam.shortR = num(o.shortBoundRange, num(o.beamShortBound, 1) * L);
      beam.kLim = num(o.beamLimitSpring, 0);
      beam.cLim = num(o.beamLimitDamp, 0);
      beam.cLimReb = num(o.beamLimitDampRebound, beam.cLim);
      beam.zone = Math.max(num(o.boundZone, 1), 1e-4);
    }
    beams.push(beam);
  }
  // wheels: the generated hub and tire nodes' mass goes to the axle nodes
  const wheels = [];
  for (const w of scene.wheels || []) {
    if (!live[w.n1] || !live[w.n2]) continue;
    const m = num(w.mass, 20);
    mass[w.n1] += m / 2;
    mass[w.n2] += m / 2;
    const psi = num(w.pressurePSI, 30);
    const width = num(w.width, 0.2);
    const k = 190000 * Math.pow(Math.max(psi, 5) / 30, 0.6) * Math.min(Math.max(width / 0.215, 0.6), 2.4);
    wheels.push({ n1: w.n1, n2: w.n2, r: num(w.radius, 0.3), k, name: w.name });
  }
  let ground = Infinity;
  for (const w of wheels) {
    const cz = (pos[w.n1 * 3 + 2] + pos[w.n2 * 3 + 2]) / 2;
    ground = Math.min(ground, cz - w.r);
  }
  if (!Number.isFinite(ground)) {
    for (let i = 0; i < n; i++) if (live[i]) ground = Math.min(ground, pos[i * 3 + 2]);
  }
  // tire damping from the load it carries (about a fifth of critical)
  const total = mass.reduce((s, m, i) => s + (live[i] ? m : 0), 0);
  for (const w of wheels) {
    const load = total / Math.max(wheels.length, 1);
    w.c = 0.4 * Math.sqrt(w.k * load);
  }
  return { n, pos, vel, frc, mass, coll, live, beams, wheels, ground, start: pos.slice(), total, t: 0, broken: false, maxSpeed: 0 };
}

function limitForce(e, k, zone) {
  // the limit stiffness comes in progressively over the bound zone
  return e < zone ? k * e * e / (2 * zone) : k * (e - zone / 2);
}

export function step(sim, steps = 1) {
  const { n, pos, vel, frc, mass, coll, live, beams, wheels, ground } = sim;
  for (let s = 0; s < steps; s++) {
    frc.fill(0);
    for (let i = 0; i < n; i++) frc[i * 3 + 2] = -mass[i] * G;
    for (let j = 0; j < beams.length; j++) {
      const b = beams[j];
      const ia = b.a * 3, ib = b.b * 3;
      const dx = pos[ib] - pos[ia], dy = pos[ib + 1] - pos[ia + 1], dz = pos[ib + 2] - pos[ia + 2];
      const L = Math.sqrt(dx * dx + dy * dy + dz * dz);
      if (L < 1e-9) continue;
      const ux = dx / L, uy = dy / L, uz = dz / L;
      const vr = (vel[ib] - vel[ia]) * ux + (vel[ib + 1] - vel[ia + 1]) * uy + (vel[ib + 2] - vel[ia + 2]) * uz;
      let F = 0;   // positive pulls the nodes together
      if (b.type === 'SUPPORT') {
        if (L < b.L0) F = b.k * (L - b.L0) + b.c * vr;
        if (F > 0) F = 0;
      } else {
        F = b.k * (L - b.L0) + (vr > 0 ? b.cReb : b.c) * vr;
        if (b.type === 'BOUNDED') {
          const d = L - b.L;
          if (d > b.longR) F += limitForce(d - b.longR, b.kLim, b.zone) + (vr > 0 ? b.cLimReb : b.cLim) * vr;
          else if (d < -b.shortR) F += -limitForce(-b.shortR - d, b.kLim, b.zone) + (vr > 0 ? b.cLimReb : b.cLim) * vr;
        }
      }
      frc[ia] += F * ux; frc[ia + 1] += F * uy; frc[ia + 2] += F * uz;
      frc[ib] -= F * ux; frc[ib + 1] -= F * uy; frc[ib + 2] -= F * uz;
    }
    for (const w of wheels) {
      const i1 = w.n1 * 3, i2 = w.n2 * 3;
      const cz = (pos[i1 + 2] + pos[i2 + 2]) / 2;
      const pen = w.r - (cz - ground);
      if (pen <= 0) continue;
      const vz = (vel[i1 + 2] + vel[i2 + 2]) / 2;
      let fz = w.k * pen - w.c * vz;
      if (fz < 0) fz = 0;
      // the tread holds on to the ground (a parked car)
      const fx = -w.c * 2 * (vel[i1] + vel[i2]) / 2, fy = -w.c * 2 * (vel[i1 + 1] + vel[i2 + 1]) / 2;
      frc[i1] += fx / 2; frc[i1 + 1] += fy / 2; frc[i1 + 2] += fz / 2;
      frc[i2] += fx / 2; frc[i2 + 1] += fy / 2; frc[i2 + 2] += fz / 2;
    }
    let vmax = 0;
    for (let i = 0; i < n; i++) {
      if (!live[i]) continue;
      const k = i * 3;
      if (coll[i] && pos[k + 2] < ground) {
        // nodes that touch the ground: a stiff, damped floor with friction
        const m = mass[i];
        const pen = ground - pos[k + 2];
        frc[k + 2] += m * 40000 * pen - m * 280 * Math.min(vel[k + 2], 0);
        frc[k] -= m * 60 * vel[k];
        frc[k + 1] -= m * 60 * vel[k + 1];
      }
      const im = DT / mass[i];
      vel[k] += frc[k] * im; vel[k + 1] += frc[k + 1] * im; vel[k + 2] += frc[k + 2] * im;
      pos[k] += vel[k] * DT; pos[k + 1] += vel[k + 1] * DT; pos[k + 2] += vel[k + 2] * DT;
      const sp = vel[k] * vel[k] + vel[k + 1] * vel[k + 1] + vel[k + 2] * vel[k + 2];
      if (sp > vmax) vmax = sp;
    }
    sim.t += DT;
    vmax = Math.sqrt(vmax);
    if (vmax > sim.maxSpeed) sim.maxSpeed = vmax;
    if (!(vmax < 60)) { sim.broken = true; return false; }
  }
  return true;
}

// Run a drop test: settle for `seconds`, keeping `fps` frames of node positions for playback.
// Returns frames and a summary; onProgress(fraction) lets the page show progress.
export async function dropTest(scene, { seconds = 2.5, fps = 50, chunk = 400, onProgress = null, drop = 0.03 } = {}) {
  const sim = prepare(scene);
  // start a little above the ground, like spawning
  for (let i = 0; i < sim.n; i++) sim.pos[i * 3 + 2] += drop;
  const frames = [sim.pos.slice()];
  const every = Math.max(1, Math.round(1 / fps / DT));
  const total = Math.round(seconds / DT);
  let done = 0;
  while (done < total && !sim.broken) {
    const todo = Math.min(chunk, total - done);
    for (let s = 0; s < todo; s++) {
      if (!step(sim, 1)) break;
      done++;
      if (done % every === 0) frames.push(sim.pos.slice());
    }
    if (onProgress) { onProgress(done / total); await new Promise(r => setTimeout(r, 0)); }
  }
  return { frames, fps, summary: summarize(scene, sim), sim };
}

// the axle a wheel belongs to: F and R, or the Scrap Mechanic import's extra ones (M1L -> M1, F2R -> F2)
export function axleOf(name) {
  const n = name || '?';
  const m = /^([A-Z]+\d+)[LR]$/i.exec(n);
  if (m) return m[1].toUpperCase();
  return /^F/i.test(n) ? 'F' : /^R/i.test(n) ? 'R' : n;
}

// how the vehicle sits after the test compared to how it was built
export function summarize(scene, sim) {
  const { pos, start, live } = sim;
  const wheelNodes = new Set();
  for (const w of sim.wheels) { wheelNodes.add(w.n1); wheelNodes.add(w.n2); }
  // body: the nodes that are not wheels, suspension or engine ("hub" groups are suspension)
  const isBody = i => live[i] && !wheelNodes.has(i) && !(scene.nodes[i].grp || []).some(g => /hub|arm|axle|link|coil|tie|engine|wheel|tire/i.test(g));
  const axles = {};
  for (const w of sim.wheels) {
    const id = axleOf(w.name);
    const y = (start[w.n1 * 3 + 1] + start[w.n2 * 3 + 1]) / 2;
    (axles[id] = axles[id] || { ys: [] }).ys.push(y);
  }
  const out = { stable: !sim.broken, time: sim.t, maxSpeed: sim.maxSpeed, axles: {} };
  if (sim.broken) return out;
  let mean = 0, cnt = 0;
  for (let i = 0; i < sim.n; i++) if (isBody(i)) { mean += pos[i * 3 + 2] - start[i * 3 + 2]; cnt++; }
  out.bodyDrop = cnt ? mean / cnt : 0;
  for (const [id, a] of Object.entries(axles)) {
    const y = a.ys.reduce((s, v) => s + v, 0) / a.ys.length;
    let d = 0, c = 0;
    for (let i = 0; i < sim.n; i++) {
      if (!isBody(i) || Math.abs(start[i * 3 + 1] - y) > 0.45) continue;
      d += pos[i * 3 + 2] - start[i * 3 + 2]; c++;
    }
    out.axles[id] = { drop: c ? d / c : 0, nodes: c, y };
  }
  // how much the structure itself bends: the largest stretch of a body beam
  let maxStrain = 0, worst = null;
  for (const b of sim.beams) {
    if (!isBody(b.a) || !isBody(b.b) || b.type !== 'NORMAL') continue;
    const ia = b.a * 3, ib = b.b * 3;
    const L = Math.hypot(pos[ib] - pos[ia], pos[ib + 1] - pos[ia + 1], pos[ib + 2] - pos[ia + 2]);
    const strain = Math.abs(L - b.L) / b.L;
    if (strain > maxStrain) { maxStrain = strain; worst = [b.a, b.b]; }
  }
  out.maxStrain = maxStrain;
  out.worstBeam = worst;
  // still moving at the end? (the body as a whole: a light node buzzing in place doesn't count)
  let v = 0, vx = 0, vy = 0, vz = 0, m = 0;
  for (let i = 0; i < sim.n; i++) {
    if (!isBody(i)) continue;
    vx += sim.vel[i * 3] * sim.mass[i]; vy += sim.vel[i * 3 + 1] * sim.mass[i]; vz += sim.vel[i * 3 + 2] * sim.mass[i]; m += sim.mass[i];
  }
  if (m) v = Math.hypot(vx, vy, vz) / m;
  let buzz = 0;
  for (let i = 0; i < sim.n; i++) if (live[i]) buzz = Math.max(buzz, Math.hypot(sim.vel[i * 3], sim.vel[i * 3 + 1], sim.vel[i * 3 + 2]));
  out.endSpeed = v;
  out.maxEndSpeed = buzz;
  out.settled = v < 0.015;
  return out;
}
