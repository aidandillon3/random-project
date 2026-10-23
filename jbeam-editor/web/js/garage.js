// The Garage: lift kits, wheels and tires, spacers, engine power, brakes and paint,
// shown live in 3D and saved as your own version (a config plus generated parts).
import { S, A, on, emit } from './state.js';
import { api } from './api.js';
import { icon } from './icons.js';
import { V, setMeshColor } from './view3d.js';
import { vehicleDims, statTile } from './panels.js';
import { rootPicker } from './dialogs.js';
import { $, h, fmt, toast, debounce, openModal, closeModal, setLoading, num, shortPath, confirmBox } from './util.js';
import { curveChart, section, slider } from './widgets.js';
import { runDropTest } from './droptest.js';

const G = {
  spec: {},          // what the person changed (sent with every build)
  info: null,        // the server's view of the vehicle (axles, engine, brakes, body parts, variables)
  changes: [], notes: [],
  cat: 'builds',
  linked: true,      // same lift front and rear
  customOpen: {},    // axle id -> custom tire sliders shown
};

const CATS = [
  { id: 'builds', icon: 'sparkle', title: 'Quick builds' },
  { id: 'lift', icon: 'lift', title: 'Lift & suspension' },
  { id: 'wheels', icon: 'wheel', title: 'Wheels & tires' },
  { id: 'engine', icon: 'engine', title: 'Engine' },
  { id: 'brakes', icon: 'brake', title: 'Brakes' },
  { id: 'paint', icon: 'paint', title: 'Paint' },
  { id: 'tune', icon: 'sliders', title: 'Fine tuning' },
];

const PAINT_PRESETS = [
  ['#e8e8e8', 'White'], ['#111214', 'Black'], ['#8a8f96', 'Silver'], ['#3b3f45', 'Gunmetal'], ['#b3121b', 'Red'],
  ['#e0601c', 'Orange'], ['#f2c21b', 'Yellow'], ['#2f7d32', 'Green'], ['#1d5fbf', 'Blue'], ['#0d2a5c', 'Navy'],
  ['#6a2c91', 'Purple'], ['#7a5a3a', 'Brown'], ['#c9b48f', 'Sand'], ['#5d6b4a', 'Olive'],
];

const store = {
  get(k, d) { try { const v = localStorage.getItem('jbe.garage.' + k); return v === null ? d : JSON.parse(v); } catch { return d; } },
  set(k, v) { try { localStorage.setItem('jbe.garage.' + k, JSON.stringify(v)); } catch { /* private */ } },
};
let metric = store.get('metric', false);

// ---- units ----------------------------------------------------------------------------------
const len = {
  get unit() { return metric ? 'cm' : 'in'; },
  to(m) { return metric ? m * 100 : m / 0.0254; },
  from(v) { return metric ? v / 100 : v * 0.0254; },
  fmt(m, d = 1) { return fmt(this.to(m), d) + ' ' + this.unit; },
  signed(m, d = 1) { const v = this.to(m); return (v > 0.04 ? '+' : '') + fmt(v, d) + ' ' + this.unit; },
};
const kw2hp = kw => kw * 1.341;

// ---- registration -------------------------------------------------------------------------------
export function register(MODES) {
  MODES.garage = { enter, leave, render };
  on('scene', () => {
    const g = S.scene?.garage;
    if (g) { G.info = g.info; G.changes = g.changes; G.notes = g.notes; }
    if (S.mode === 'garage') render();
  });
  on('vehicleChanged', () => { G.spec = {}; G.info = null; G.customOpen = {}; setDirty(false); setMeshColor(null); });
  on('configChanged', () => { G.spec = {}; G.customOpen = {}; setDirty(false); setMeshColor(null); });
  on('building', () => { const s = $('#garageBusy'); if (s) s.classList.toggle('hidden', !S.building); });
}

function enter() {
  S.modeHint = 'Garage: change things on the left, the vehicle updates here. Right-drag to turn the view.';
  if (!S.garageSpec) {
    S.garageSpec = G.spec;
    A.buildScene({ quiet: true });   // fetch the Garage's view of the vehicle
  }
  paintPreview();
}

function leave() {
  // keep showing the changes in the other modes while they are not saved
  if (!S.garageDirty) S.garageSpec = null;
  S.modeHint = null;
}

function setDirty(d) {
  S.garageDirty = d;
  const tab = document.querySelector('#modeTabs [data-mode=garage]');
  if (tab) tab.classList.toggle('dirty', d);
}

const rebuildSoon = debounce(() => A.buildScene({ quiet: true }), 220);
function changed(rebuild = true) {
  G.spec.reset = false;
  delete G.spec.reset;
  S.garageSpec = G.spec;
  setDirty(true);
  if (rebuild) rebuildSoon();
  renderSide();
  renderCats();
}

function axleSpec(id) {
  G.spec.axles = G.spec.axles || {};
  return (G.spec.axles[id] = G.spec.axles[id] || {});
}
function axleValue(ax, key) {
  const s = G.spec.axles?.[ax.id];
  if (G.spec.reset) return key === 'customTire' ? null : 0;
  return s && key in s ? s[key] : ax[key];
}

// ---- left pane ---------------------------------------------------------------------------------------
export function render() {
  const pane = $('#garagePane');
  if (!pane) return;
  pane.innerHTML = '';
  pane.append(h('div', { class: 'pane-head' },
    h('h2', {}, 'Garage', h('span', { id: 'garageBusy', class: 'spinner' + (S.building ? '' : ' hidden'),
      style: { width: '14px', height: '14px', borderWidth: '2px', display: 'inline-block', marginLeft: '8px', verticalAlign: '-2px' } })),
    h('p', { class: 'note' }, 'Make it yours. Changes show in 3D straight away; "Save my version" keeps them as your own config of this vehicle. The original is never changed.'),
    h('div', { class: 'seg', style: { marginTop: '4px' } },
      h('button', { class: metric ? '' : 'active', onclick: () => { metric = false; store.set('metric', false); render(); renderSide(); } }, 'Inches'),
      h('button', { class: metric ? 'active' : '', onclick: () => { metric = true; store.set('metric', true); render(); renderSide(); } }, 'Centimetres'))));
  const made = (S.vehicles || []).find(v => v.model === S.model);
  if (made?.builder || made?.scrap) {
    pane.append(h('div', { class: 'card', style: { margin: '0 0 12px' } },
      h('b', {}, made.builder ? 'Made with the Vehicle Builder' : 'Imported from Scrap Mechanic'),
      h('p', { class: 'note' }, made.builder
        ? 'To change its size, body, suspension or engine, open its design in the Builder and update it.'
        : 'To change its size, ride or power, or bring in changes you made in Scrap Mechanic, import it again.'),
      h('button', { class: 'small', onclick: () => (made.builder ? A.openBuilder(S.model) : A.openScrap(S.model)) },
        icon(made.builder ? 'build' : 'scrap', 14), made.builder ? 'Edit in the Builder' : 'Import again')));
  }
  pane.append(h('div', { class: 'catlist', id: 'garageCats' }));
  renderCats();
  const body = h('div', { id: 'garageBody' });
  pane.append(body);
  if (!G.info) {
    body.append(h('p', { class: 'note' }, S.scene ? 'Looking at the vehicle…' : 'Open a vehicle first.'));
  } else {
    ({ builds: buildsPanel, lift: liftPanel, wheels: wheelsPanel, engine: enginePanel, brakes: brakesPanel, paint: paintPanel,
      tune: tunePanel })[G.cat](body);
  }
  renderSide();
}

function catHasChange(id) {
  const s = G.spec;
  if (id === 'builds') return false;
  if (id === 'lift') return Object.values(s.axles || {}).some(a => 'lift' in a) || 'bodyLift' in s;
  if (id === 'wheels') return Object.values(s.axles || {}).some(a => 'spacer' in a || 'customTire' in a);
  if (id === 'engine') return !!s.engine;
  if (id === 'brakes') return !!s.brakes;
  if (id === 'paint') return !!s.paints;
  return false;
}

function renderCats() {
  const box = $('#garageCats');
  if (!box) return;
  box.innerHTML = '';
  for (const c of CATS) {
    const b = h('button', { class: 'cat' + (G.cat === c.id ? ' active' : '') }, h('span', { class: 'ic' }, icon(c.icon, 18)), c.title,
      catHasChange(c.id) ? h('span', { class: 'dot', title: 'Changed' }) : null);
    b.addEventListener('click', () => { G.cat = c.id; render(); });
    box.append(b);
  }
}

// ---- quick builds ---------------------------------------------------------------------------------------
// each one sets up the other categories in one go (sizes relative to the vehicle's own), all still adjustable
const IN = 0.0254;
const BUILDS = [
  { id: 'offroad', icon: 'wheel', title: 'Off-road', text: '3" lift, taller and wider tires, a wider stance and a little more power.',
    lift: 3 * IN, tire: [1.12, 1.18], spacer: 1 * IN, power: 1.1, brakes: 1.15 },
  { id: 'monster', icon: 'lift', title: 'Monster truck', text: '10" lift on giant tires, a much wider stance and lots more power.',
    lift: 10 * IN, tire: [1.6, 1.7], spacer: 5 * IN, power: 1.6, brakes: 1.6 },
  { id: 'lowered', icon: 'car', title: 'Lowered street', text: '1.5" lower on wider tires, a slightly wider stance and a tune.',
    lift: -1.5 * IN, tire: [1.0, 1.12], spacer: 0.5 * IN, power: 1.15, brakes: 1.1 },
  { id: 'rally', icon: 'sparkle', title: 'Rally', text: '1.5" lift for more suspension room, slightly taller tires and a tune.',
    lift: 1.5 * IN, tire: [1.04, 1.0], spacer: 0.5 * IN, power: 1.2, brakes: 1.1 },
  { id: 'drag', icon: 'engine', title: 'Drag', text: '60% more power, wide rear tires and stronger brakes.',
    lift: 0, tire: [1.0, 1.0], rearTire: [1.04, 1.35], spacer: 0, power: 1.6, brakes: 1.3 },
];

function buildsPanel(body) {
  const grid = h('div', { class: 'optcards' });
  for (const b of BUILDS) {
    const btn = h('button', { class: 'optcard' }, h('span', { class: 't' }, icon(b.icon, 14), ' ', b.title), h('span', { class: 'd' }, b.text));
    btn.addEventListener('click', () => applyBuild(b));
    grid.append(btn);
  }
  body.append(section('Quick builds', 'sparkle',
    'One click sets up a whole look. Everything stays adjustable in the other categories, and nothing is kept until you press "Save my version".',
    grid, h('button', { class: 'small', onclick: resetAll, disabled: !S.garageDirty && !Object.keys(G.spec).length }, 'Back to stock')));
}

async function applyBuild(b) {
  const info = G.info;
  const mine = Object.keys(G.spec).filter(k => k !== 'paints');
  if (mine.length && !(await confirmBox('Use the ' + b.title.toLowerCase() + ' build?',
    'It replaces the Garage changes you made so far (your paint stays).', 'Use it'))) return;
  const spec = { axles: {} };
  const last = info.axles[info.axles.length - 1];
  for (const ax of info.axles) {
    const a = spec.axles[ax.id] = {};
    if (ax.slots.length) a.lift = +b.lift.toFixed(4);
    if (ax.canSpacer) a.spacer = +b.spacer.toFixed(4);
    const [fd, fw] = b.rearTire && ax === last && info.axles.length > 1 ? b.rearTire : b.tire;
    if (ax.tire && (Math.abs(fd - 1) > 0.001 || Math.abs(fw - 1) > 0.001)) {
      a.customTire = { diameter: +(ax.radius * 2 * fd).toFixed(4), width: +((ax.width || 0.2) * fw).toFixed(4) };
    }
  }
  if (info.engine?.slot) spec.engine = { power: b.power };
  if (info.brakes.length) spec.brakes = { torque: b.brakes };
  if (G.spec.paints) spec.paints = G.spec.paints;
  G.spec = spec;
  G.linked = true;
  G.customOpen = {};
  changed();
  render();
  toast(`${b.title} build set up. Look through the categories to adjust it, then save your version.`, 'ok', 6000);
}

// ---- lift ------------------------------------------------------------------------------------------------
function liftPanel(body) {
  const info = G.info;
  const axles = info.axles;
  if (!axles.length) { body.append(h('p', { class: 'note' }, 'This vehicle has no wheels the Garage can find.')); return; }
  const liftable = axles.filter(a => a.slots.length);
  const range = { min: metric ? -10 : -4, max: metric ? 30 : 12, step: metric ? 0.5 : 0.25 };
  const linkBox = h('label', { class: 'toggle-row chk' }, h('input', { type: 'checkbox', checked: G.linked,
    onchange: e => { G.linked = e.target.checked; if (G.linked && axles.length > 1) { const v = axleValue(axles[0], 'lift'); for (const a of axles) axleSpec(a.id).lift = v; changed(); } render(); } }),
  'Same lift front and rear');
  const sliders = [];
  const lifts = G.linked ? [axles[0]] : axles;
  for (const ax of lifts) {
    const cur = axleValue(ax, 'lift') || 0;
    sliders.push(slider({
      label: G.linked && axles.length > 1 ? 'Lift' : ax.label + ' lift',
      min: range.min, max: range.max, step: range.step, value: +len.to(cur).toFixed(2),
      show: v => (v > 0 ? '+' : '') + fmt(v, 2) + ' ' + len.unit,
      scale: [len.fmt(len.from(range.min), 0) + ' (lower)', '+' + len.fmt(len.from(range.max), 0)],
      onInput: (v, done) => {
        const m = +len.from(v).toFixed(4);
        for (const a of (G.linked ? axles : [ax])) axleSpec(a.id).lift = m;
        changed(true);
        if (done) render();
      },
    }));
  }
  body.append(section('Lift kit', 'lift',
    'Moves the whole suspension down on longer brackets, the way a real lift kit does, so the body sits higher. Springs, shocks and steering keep working. Negative values lower the vehicle.',
    axles.length > 1 ? linkBox : null, ...sliders,
    liftable.length < axles.length ? h('p', { class: 'note warn' }, axles.filter(a => !a.slots.length).map(a => a.label).join(' and ') +
      ' suspension is part of the main frame part, so the Garage can\'t lift it. You can still move its nodes in Structure mode.') : null,
    h('p', { class: 'note' }, 'Parts that move: ', liftable.map(a => a.slots.map(s => s.title).join(', ')).join(' · ') || 'none')));
  if (info.body.length) {
    const cur = 'bodyLift' in G.spec ? G.spec.bodyLift : info.bodyLift || 0;
    const chosen = new Set(G.spec.bodySlots || info.body.filter(b => b.bodyLift || b.checked).map(b => b.path));
    const list = h('div', { class: 'optlist2' }, ...info.body.map(b => {
      const cb = h('input', { type: 'checkbox', checked: chosen.has(b.path) });
      cb.addEventListener('change', () => {
        if (cb.checked) chosen.add(b.path); else chosen.delete(b.path);
        G.spec.bodySlots = [...chosen];
        changed();
      });
      return h('label', { class: 'optrow' }, cb, h('span', { class: 't' }, b.description), h('span', { class: 'd' }, b.title));
    }));
    body.append(section('Body lift', 'box',
      'Raises the body on its frame (for body-on-frame trucks). Pick the parts that sit on the frame.',
      slider({ label: 'Body lift', min: 0, max: metric ? 12 : 5, step: range.step, value: +len.to(cur).toFixed(2),
        show: v => '+' + fmt(v, 2) + ' ' + len.unit,
        onInput: (v, done) => { G.spec.bodyLift = +len.from(v).toFixed(4); if (!G.spec.bodySlots) G.spec.bodySlots = [...chosen]; changed(); if (done) renderCats(); } }),
      h('details', { class: 'box' }, h('summary', {}, `Parts that move up (${chosen.size})`), list)));
  }
  const groups = ['rideHeight', 'springs', 'dampers', 'swaybars'].filter(g => info.vars[g]?.length);
  if (groups.length) {
    body.append(section('Built-in adjustments', 'sliders', 'Settings this vehicle\'s own parts offer (also on the Tuning tab).',
      ...groups.flatMap(g => info.vars[g].map(varSlider))));
  }
}

// a tuning variable as a slider (changes the config, like the Tuning tab)
function varSlider(name) {
  const v = S.scene.variables.find(x => x.name === name);
  if (!v) return null;
  const dmin = num(v.minDis, v.min), dmax = num(v.maxDis, v.max);
  const toDis = x => v.max === v.min ? dmin : dmin + (x - v.min) / (v.max - v.min) * (dmax - dmin);
  const fromDis = d => dmax === dmin ? v.min : v.min + (d - dmin) / (dmax - dmin) * (v.max - v.min);
  const cur = num(S.config.vars?.[v.name], num(v.val, v.default));
  return slider({
    label: v.title || v.name, min: dmin, max: dmax, step: num(v.stepDis, (dmax - dmin) / 100) || 'any', value: toDis(cur),
    show: d => fmt(d, 2) + (v.unit ? ' ' + v.unit : ''), help: v.description,
    onInput: (d, done) => {
      if (!done) return;
      S.config.vars = S.config.vars || {};
      S.config.vars[v.name] = +fromDis(d).toPrecision(8);
      A.buildScene({ quiet: true });
    },
  });
}

// ---- wheels and tires ---------------------------------------------------------------------------------------
function sizeText(o) {
  if (!o.diameter) return '';
  return (metric ? `${fmt(o.diameter * 100, 1)} cm` : `${fmt(o.diameter / 0.0254, 1)}"`) +
    (o.width ? (metric ? ` × ${fmt(o.width * 1000, 0)} mm` : ` × ${fmt(o.width / 0.0254, 1)}"`) : '');
}

function wheelsPanel(body) {
  const info = G.info;
  for (const ax of info.axles) {
    const tire = ax.tire, rim = ax.rim;
    const ct = axleValue(ax, 'customTire');
    const parts = [];
    if (tire) {
      const opts = tire.options.filter(o => !o.generated || o.name === tire.part);
      parts.push(h('div', { class: 'note' }, 'Tires (pick one, sizes are tall × wide):'));
      const list = h('div', { class: 'optlist2' });
      for (const o of opts) {
        const cur = o.name === tire.part || o.name === tire.source;
        const b = h('button', { class: 'optrow' + (cur && !ct ? ' active' : '') }, h('span', { class: 't' }, o.title), h('span', { class: 'd' }, sizeText(o)));
        b.addEventListener('click', () => pickPart(tire.id, o.name, ax, 'customTire'));
        list.append(b);
      }
      parts.push(list);
      const open = G.customOpen[ax.id] || !!ct;
      const curD = ct?.diameter || ax.radius * 2, curW = ct?.width || ax.width || 0.2;
      const customBox = h('div', { class: open ? 'card' : 'hidden' },
        h('b', {}, 'Custom tire size'),
        h('p', { class: 'note' }, 'Makes a copy of the current tire with a new size. The tire mesh is stretched to match.'),
        slider({ label: 'Diameter (tall)', min: metric ? 40 : 16, max: metric ? 140 : 55, step: metric ? 0.5 : 0.25, value: +len.to(curD).toFixed(2),
          show: v => fmt(v, 2) + ' ' + len.unit,
          onInput: v => { axleSpec(ax.id).customTire = { diameter: +len.from(v).toFixed(4), width: (axleSpec(ax.id).customTire || {}).width || +curW.toFixed(4) }; changed(); } }),
        slider({ label: 'Width', min: metric ? 10 : 4, max: metric ? 50 : 20, step: metric ? 0.5 : 0.25, value: +len.to(curW).toFixed(2),
          show: v => fmt(v, 2) + ' ' + len.unit,
          onInput: v => { axleSpec(ax.id).customTire = { diameter: (axleSpec(ax.id).customTire || {}).diameter || +curD.toFixed(4), width: +len.from(v).toFixed(4) }; changed(); } }),
        ct ? h('button', { class: 'small', onclick: () => { axleSpec(ax.id).customTire = null; G.customOpen[ax.id] = false; changed(); render(); } }, 'Back to a standard size') : null);
      parts.push(h('button', { class: 'small', onclick: () => { G.customOpen[ax.id] = !open; render(); } }, icon('ruler', 14), open ? 'Hide custom size' : 'Custom size…'), customBox);
    }
    if (rim && rim.options.length > 1) {
      const list = h('div', { class: 'optlist2' });
      for (const o of rim.options.filter(x => !x.generated || x.name === rim.part)) {
        const b = h('button', { class: 'optrow' + (o.name === rim.part ? ' active' : '') }, h('span', { class: 't' }, o.title), h('span', { class: 'd' }, sizeText(o)));
        b.addEventListener('click', () => pickPart(rim.id, o.name, ax));
        list.append(b);
      }
      parts.push(h('div', { class: 'note', style: { marginTop: '10px' } }, 'Rims:'), list);
    }
    if (ax.canSpacer) {
      const sp = axleValue(ax, 'spacer') || 0;
      parts.push(slider({ label: 'Wheel spacers (each side)', min: metric ? -2 : -1, max: metric ? 15 : 6, step: metric ? 0.1 : 0.05,
        value: +len.to(sp).toFixed(2), show: v => (v > 0 ? '+' : '') + fmt(v, 2) + ' ' + len.unit,
        help: `Pushes the wheels out for a wider stance. Track now ${len.fmt(ax.track + 2 * sp, 1)}.`,
        onInput: (v, done) => { axleSpec(ax.id).spacer = +len.from(v).toFixed(4); changed(); if (done) render(); } }));
    }
    const current = tire ? tire.options.find(o => o.name === tire.part) : null;
    body.append(section(ax.label + ' wheels', 'wheel',
      `Now: ${current ? current.title + ' — ' : ''}${len.fmt(ct?.diameter || ax.radius * 2, 1)} tall tires, track ${len.fmt(ax.track, 1)}.`, ...parts));
  }
  const rub = rubCheck();
  if (rub.length) body.append(h('div', { class: 'card' }, h('b', { class: 'warn' }, icon('warning', 15), ' Tires may rub'),
    ...rub.map(r => h('p', { class: 'note' }, r))));
  const pressure = G.info.vars.pressure || [];
  const track = G.info.vars.track || [];
  if (pressure.length || track.length) body.append(section('Built-in adjustments', 'sliders', null, ...[...pressure, ...track].map(varSlider)));
}

// pick an existing part for a slot (like the Parts tab)
function pickPart(slotId, name, ax, clearKey) {
  if (clearKey && ax) { axleSpec(ax.id)[clearKey] = null; G.customOpen[ax.id] = false; }
  S.config.parts = S.config.parts || {};
  S.config.parts[slotId] = name;
  setDirty(true);
  A.buildScene({ quiet: true });
}

// will the tires hit the body? look for body nodes inside each tire's space
function rubCheck() {
  const sc = S.scene;
  if (!sc || !sc.wheels.length) return [];
  const wheelParts = new Set();
  for (const ax of G.info.axles) for (const s of ax.slots) wheelParts.add(s.part);
  const out = [];
  const seen = new Set();
  for (const w of sc.wheels) {
    const a = sc.nodes[w.n1].p, b = sc.nodes[w.n2].p;
    const c = [(a[0] + b[0]) / 2, (a[1] + b[1]) / 2, (a[2] + b[2]) / 2];
    const r = w.radius || 0.3, half = Math.max(Math.abs(a[0] - b[0]) / 2, (w.width || 0.2) / 2);
    let worst = 0;
    for (const n of sc.nodes) {
      if (n.del || !n.col) continue;
      const part = sc.parts[n.part];
      if (!part) continue;
      const path = part.path || '';
      if (G.info.axles.some(ax => ax.slots.some(s => path.startsWith(s.path)))) continue;   // the suspension itself
      if (Math.abs(n.p[0] - c[0]) > half + 0.02) continue;
      const d = Math.hypot(n.p[1] - c[1], n.p[2] - c[2]);
      if (n.p[2] > c[2] && d < r + 0.015) worst = Math.max(worst, r + 0.015 - d);
    }
    const side = c[1] < 0 ? 'Front' : 'Rear';
    if (worst > 0.005 && !seen.has(side)) {
      seen.add(side);
      out.push(`${side} tires reach about ${len.fmt(worst, 1)} into the body. More lift or smaller tires would clear it.`);
    }
  }
  return out;
}

// ---- engine ---------------------------------------------------------------------------------------------------
function enginePanel(body) {
  const e = G.info.engine;
  if (!e) { body.append(h('p', { class: 'note' }, 'No engine with a torque curve was found on this vehicle.')); return; }
  const power = G.spec.reset ? 1 : (G.spec.engine && 'power' in G.spec.engine ? G.spec.engine.power : e.power || 1);
  const rpm = G.spec.reset ? 0 : (G.spec.engine && 'rpm' in G.spec.engine ? G.spec.engine.rpm : e.rpm || 0);
  const hp0 = kw2hp(e.peakKW);
  const chart = h('div');
  const draw = () => { chart.innerHTML = ''; chart.append(curveChart(e.curve, power, rpm, e.maxRPM)); };
  draw();
  const now = h('span', {});
  const say = (p, r) => { now.textContent = `${fmt(hp0 * p, 0)} hp, ${fmt(e.peakTorque * p, 0)} Nm (stock ${fmt(hp0, 0)} hp, ${fmt(e.peakTorque, 0)} Nm), rev limit ${fmt((e.maxRPM || 0) + r, 0)} rpm`; };
  say(power, rpm);
  const slot = e.slot;
  body.append(section('Engine power', 'engine',
    `${slot ? slot.title : e.part}. A tune scales its whole torque curve; the engine's strength limits are raised to match.`,
    h('p', { class: 'note' }, now),
    slider({ label: 'Power', min: 50, max: 300, step: 1, value: Math.round(power * 100), show: v => v + ' %',
      onInput: (v, done) => { G.spec.engine = Object.assign(G.spec.engine || {}, { power: v / 100 }); say(v / 100, (G.spec.engine.rpm ?? rpm)); draw2(); changed(); if (done) renderCats(); } }),
    slider({ label: 'Rev limit', min: -1500, max: 3000, step: 50, value: rpm, show: v => (v > 0 ? '+' : '') + v + ' rpm',
      help: 'More revs only help if the engine still makes power up there.',
      onInput: (v, done) => { G.spec.engine = Object.assign(G.spec.engine || {}, { rpm: v }); say(G.spec.engine.power ?? power, v); draw2(); changed(); if (done) renderCats(); } }),
    chart));
  function draw2() { chart.innerHTML = ''; chart.append(curveChart(e.curve, G.spec.engine?.power ?? power, G.spec.engine?.rpm ?? rpm, e.maxRPM)); }
  if (slot) {
    const entry = findSlot(slot.path);
    if (entry && entry.options.length > 1) {
      const list = h('div', { class: 'optlist2' });
      for (const o of entry.options) {
        if (S.scene.parts[o]?.virtual) continue;
        const cur = o === slot.source || o === slot.part;
        const b = h('button', { class: 'optrow' + (cur ? ' active' : '') }, h('span', { class: 't' }, S.scene.partTitles[o] || o));
        b.addEventListener('click', () => pickPart(slot.id, o));
        list.append(b);
      }
      body.append(section('Engine swap', 'refresh', 'Engines this vehicle can take.', list));
    }
  }
  const g = [...(G.info.vars.gearing || []), ...(G.info.vars.engine || [])];
  if (g.length) body.append(section('Built-in adjustments', 'sliders', null, ...g.map(varSlider)));
}

function findSlot(path) {
  const walk = s => { if (s.path === path) return s; for (const c of s.children || []) { const r = walk(c); if (r) return r; } return null; };
  return S.scene ? walk(S.scene.tree) : null;
}

// ---- brakes ---------------------------------------------------------------------------------------------------
function brakesPanel(body) {
  const info = G.info;
  const cur = G.spec.reset ? 1 : (G.spec.brakes?.torque ?? info.brakeFactor ?? 1);
  if (!info.brakes.length) body.append(h('p', { class: 'note' }, 'No brake parts were found to change.'));
  else body.append(section('Brake force', 'brake',
    `Scales the brake torque of ${info.brakes.map(b => b.title).join(', ')}. Bigger tires and more weight need more brake.`,
    slider({ label: 'Brake force', min: 50, max: 300, step: 5, value: Math.round(cur * 100), show: v => v + ' %',
      onInput: (v, done) => { G.spec.brakes = { torque: v / 100 }; changed(); if (done) renderCats(); } })));
  const b = info.vars.brakes || [];
  if (b.length) body.append(section('Built-in adjustments', 'sliders', null, ...b.map(varSlider)));
}

// ---- paint -------------------------------------------------------------------------------------------------------
function defaultPaints() {
  const p = S.config?.paints;
  if (Array.isArray(p) && p.length) return JSON.parse(JSON.stringify(p));
  const own = S.summary?.defaultPaint;
  return [0, 1, 2].map(() => ({ baseColor: own ? [...own, 1] : [0.85, 0.85, 0.85, 1], metallic: 0.2, roughness: 0.5, clearcoat: 1, clearcoatRoughness: 0.05 }));
}
const hex = c => '#' + c.slice(0, 3).map(v => Math.round(Math.max(0, Math.min(1, v)) * 255).toString(16).padStart(2, '0')).join('');
const rgb = s => [1, 3, 5].map(k => parseInt(s.slice(k, k + 2), 16) / 255);

function paintPreview() {
  const p = G.spec.paints || (S.config?.paints?.length ? S.config.paints : null);
  setMeshColor(p ? p[0].baseColor.slice(0, 3) : (S.summary?.defaultPaint || null));
}

function paintPanel(body) {
  const paints = G.spec.paints || defaultPaints();
  const set = () => { G.spec.paints = paints; paintPreview(); changed(false); };
  ['Main colour', 'Second colour', 'Third colour'].forEach((label, k) => {
    const p = paints[k] || (paints[k] = { baseColor: [0.85, 0.85, 0.85, 1], metallic: 0.2, roughness: 0.5, clearcoat: 1, clearcoatRoughness: 0.05 });
    const pick = h('input', { type: 'color', value: hex(p.baseColor) });
    pick.addEventListener('input', () => { p.baseColor = [...rgb(pick.value), 1]; set(); render(); });
    const sw = h('div', { class: 'swatches' }, ...PAINT_PRESETS.map(([c, name]) => {
      const b = h('button', { class: 'swatch' + (hex(p.baseColor) === c ? ' active' : ''), title: name, style: { background: c } });
      b.addEventListener('click', () => { p.baseColor = [...rgb(c), 1]; set(); render(); });
      return b;
    }));
    body.append(section(label, k ? null : 'paint', k ? null : 'Saved with your version. The 3D view shows the main colour; the game shows all three.',
      h('div', { class: 'toggle-row' }, pick, h('span', { class: 'muted' }, hex(p.baseColor))), sw,
      slider({ label: 'Metallic', min: 0, max: 1, step: 0.01, value: p.metallic ?? 0.2, show: v => fmt(v * 100, 0) + ' %', onInput: v => { p.metallic = v; set(); } }),
      slider({ label: 'Roughness', min: 0, max: 1, step: 0.01, value: p.roughness ?? 0.5, show: v => fmt(v * 100, 0) + ' %', onInput: v => { p.roughness = v; set(); } }),
      slider({ label: 'Clear coat', min: 0, max: 1, step: 0.01, value: p.clearcoat ?? 1, show: v => fmt(v * 100, 0) + ' %', onInput: v => { p.clearcoat = v; set(); } })));
  });
  if (G.spec.paints) body.append(h('button', { onclick: () => { delete G.spec.paints; paintPreview(); changed(false); render(); } }, 'Use the vehicle\'s own colours'));
}

// ---- fine tuning -----------------------------------------------------------------------------------------------
function tunePanel(body) {
  const v = G.info.vars;
  const names = { rideHeight: 'Ride height', springs: 'Springs', dampers: 'Dampers', swaybars: 'Anti-roll bars', track: 'Wheel offset',
    pressure: 'Tire pressure', brakes: 'Brakes', gearing: 'Gearing', engine: 'Engine', alignment: 'Wheel alignment' };
  let any = false;
  for (const [k, title] of Object.entries(names)) {
    if (!v[k]?.length) continue;
    any = true;
    body.append(section(title, null, null, ...v[k].map(varSlider)));
  }
  if (!any) body.append(h('p', { class: 'note' }, 'This vehicle\'s parts offer no tuning settings.'));
  body.append(h('button', { onclick: () => A.setMode('tuning') }, icon('sliders', 15), 'All tuning settings'));
}

// ---- right side: before / after and saving --------------------------------------------------------------------------
export function renderSide() {
  const box = $('#garageSide');
  if (!box) return;
  box.innerHTML = '';
  if (!S.scene || !G.info) return;
  const d = vehicleDims();
  const info = G.info;
  const F = info.axles.find(a => a.id === 'F'), R = info.axles.find(a => a.id === 'R');
  const row = (label, before, after) => [h('span', { class: 'k' }, label), h('span', {}, before),
    h('span', { class: before === after ? '' : 'new' }, after)];
  const ride = groundClearance();
  const e = info.engine;
  const power = e ? (G.spec.engine?.power ?? e.power ?? 1) : 1;
  const cmp = h('div', { class: 'compare' },
    h('span', { class: 'h' }, ''), h('span', { class: 'h' }, 'Now'), h('span', { class: 'h' }, 'New'),
    ...(ride ? row('Body height', len.fmt(ride.before, 1), len.fmt(ride.after, 1)) : []),
    ...(F ? row('Front track', len.fmt(F.track, 1), d.trackF ? len.fmt(d.trackF, 1) : '–') : []),
    ...(R ? row('Rear track', len.fmt(R.track, 1), d.trackR ? len.fmt(d.trackR, 1) : '–') : []),
    ...(F ? row('Front tires', len.fmt(F.radius * 2, 1), d.tireF ? len.fmt(d.tireF, 1) : '–') : []),
    ...(R ? row('Rear tires', len.fmt(R.radius * 2, 1), d.tireR ? len.fmt(d.tireR, 1) : '–') : []),
    ...(e ? row('Power', fmt(kw2hp(e.peakKW), 0) + ' hp', fmt(kw2hp(e.peakKW) * power, 0) + ' hp') : []));
  const list = G.changes.length ? h('ul', { class: 'changes' }, ...G.changes.map(c => h('li', {}, c)))
    : h('p', { class: 'note' }, 'No changes yet. Pick a category on the left.');
  box.append(h('h3', {}, 'Your version'), cmp,
    h('h4', {}, 'Changes'), list,
    ...G.notes.map(n => h('p', { class: 'note warn' }, icon('warning', 13), ' ', n)),
    h('div', { class: 'btnrow' },
      h('button', { class: 'primary big', disabled: !S.garageDirty && !G.changes.length && !G.spec.paints, onclick: saveDialog }, icon('save', 16), 'Save my version…'),
      h('button', { disabled: !S.garageDirty, onclick: resetAll, title: 'Undo all Garage changes since the last save' }, 'Reset'),
      h('button', { onclick: () => runDropTest(), title: 'Drop it onto the ground in a quick physics test: does it sit right?' }, icon('physics', 15), 'Drop test')),
    h('p', { class: 'note' }, 'Saving makes a new config of this vehicle in a mod of yours. In the game, pick it in the vehicle selector or press Ctrl+R if it is already spawned.'));
}

// lowest point of the body (not the suspension or wheels) above the ground, now and with the changes
function groundClearance() {
  const sc = S.scene;
  if (!sc || !sc.wheels.length || !G.info || G.info.clearance === null || G.info.clearance === undefined) return null;
  const suspPaths = G.info.axles.flatMap(a => a.slots.map(s => s.path));
  const axleNodes = new Set(sc.wheels.flatMap(w => [w.n1, w.n2]));
  let low = Infinity;
  sc.nodes.forEach((n, i) => {
    if (n.del || axleNodes.has(i)) return;
    const path = sc.parts[n.part]?.path || '';
    if (suspPaths.some(p => path.startsWith(p))) return;
    low = Math.min(low, n.p[2]);
  });
  if (!isFinite(low)) return null;
  return { before: G.info.clearance, after: low - vehicleDims().groundZ };
}

function resetAll() {
  G.spec = { reset: true };
  S.garageSpec = G.spec;
  G.customOpen = {};
  setDirty(false);
  paintPreview();
  A.buildScene({ quiet: true }).then(() => { G.spec = {}; S.garageSpec = G.spec; render(); });
}

function snapshot() {
  try {
    V.renderer.render(V.scene, V.camera);
    const src = V.renderer.domElement;
    const c = document.createElement('canvas');
    const w = 640, ht = Math.round(w * src.height / src.width);
    c.width = w; c.height = ht;
    const g = c.getContext('2d');
    g.fillStyle = '#1b1f27'; g.fillRect(0, 0, w, ht);
    g.drawImage(src, 0, 0, w, ht);
    return c.toDataURL('image/jpeg', 0.85);
  } catch { return null; }
}

function saveDialog() {
  const base = S.summary?.name || S.model;
  const roots = S.summary?.editableRoots || [];
  const titleIn = h('input', { type: 'text', value: (S.configName && roots.length ? S.configName : base + ' Custom') });
  titleIn.addEventListener('keydown', e => e.stopPropagation());
  const root = rootPicker();
  const pic = snapshot();
  const usePic = h('input', { type: 'checkbox', checked: !!pic });
  const err = h('div', { class: 'err' });
  const go = async () => {
    err.textContent = '';
    const title = titleIn.value.trim();
    if (!title) return (err.textContent = 'Give your version a name.');
    try {
      setLoading(true, 'Saving your version…');
      const sel = root.querySelector('select');
      const body = { model: S.model, configName: title, title, garage: G.spec.reset ? {} : G.spec, config: S.config,
        picture: usePic.checked ? pic : null };
      if (sel.value === '__new') body.newMod = root.querySelector('input').value.trim() || ('my_' + S.model + '_versions');
      else body.root = sel.value;
      const r = await api.garageSave(body);
      S.summary = r.summary;
      S.lastRoot = r.root;
      closeModal();
      G.spec = {};
      G.customOpen = {};
      setDirty(false);
      S.garageSpec = G.spec;
      await A.refreshVehicles();
      await A.switchConfig(r.configName);
      toast(`Saved "${r.configName}" in ${shortPath(r.folder)}. Find it in the game's vehicle selector under ${S.summary?.name || S.model}.`, 'ok', 10000);
      for (const n of r.notes || []) toast(n, '', 9000);
    } catch (e) { err.textContent = e.message; } finally { setLoading(false); }
  };
  openModal('Save my version', h('div', {},
    h('div', { style: { display: 'flex', gap: '14px', alignItems: 'flex-start' } },
      pic ? h('img', { src: pic, style: { width: '220px', borderRadius: '10px', border: '1px solid var(--line)' } }) : null,
      h('div', { style: { flex: 1 } },
        h('p', { class: 'note', style: { marginTop: 0 } }, 'Your version is saved as a config of this vehicle, with your parts made from copies of the original ones.'),
        h('ul', { class: 'changes' }, ...(G.changes.length ? G.changes : ['Parts and colours you picked']).map(c => h('li', {}, c))))),
    h('div', { class: 'form', style: { marginTop: '12px' } },
      h('span', { class: 'k' }, 'Name'), titleIn,
      h('span', { class: 'k' }, 'Save into mod'), root,
      h('span', { class: 'k' }, 'Picture'), h('label', { class: 'chk' }, usePic, 'Use this view as the picture in the game\'s vehicle selector')),
    err,
    h('div', { class: 'actions' }, h('button', { onclick: closeModal }, 'Cancel'), h('button', { class: 'primary', onclick: go }, icon('save', 15), 'Save'))));
}
