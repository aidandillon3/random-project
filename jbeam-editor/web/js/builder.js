// The Vehicle Builder: pick a body style, shape it with sliders and see it in 3D at once.
// "Create vehicle" writes a complete mod (body, suspension, wheels, engine, gearbox) that
// drives in the game and opens in the rest of the editor like any other vehicle.
import { S, A } from './state.js';
import { api } from './api.js';
import { icon } from './icons.js';
import { V, buildMeshes, frame, rebuild, setMeshes, setPalette } from './view3d.js';
import { reindex } from './scene.js';
import { hideHome } from './home.js';
import { curveChart, section, slider, valueText } from './widgets.js';
import { closePanel as closeDropTest, runDropTest } from './droptest.js';
import { $, h, fmt, toast, debounce, openModal, closeModal, setLoading, shortPath, confirmBox } from './util.js';

const B = {
  schema: null,
  spec: null,          // the design being edited (plain values, see editor/vehiclegen/spec.py)
  cat: 'size',
  more: {},            // group id -> show the extra settings
  result: null,        // last preview: stats, notes, materials, model id, name clash
  source: null,        // the vehicle id when editing a vehicle made with the Builder
  token: 0,
  fresh: true,
  busy: false,
};

const store = {
  get(k, d) { try { const v = localStorage.getItem('jbe.builder.' + k); return v === null ? d : JSON.parse(v); } catch { return d; } },
  set(k, v) { try { localStorage.setItem('jbe.builder.' + k, JSON.stringify(v)); } catch { /* private window */ } },
};
let imperial = store.get('imperial', false);

const STYLE_ICONS = { pickup: 'box', van: 'box', offroad: 'wheel', roadster: 'sparkle', supercar: 'sparkle' };
const PAINTS = [
  ['#e8e8e8', 'White'], ['#111214', 'Black'], ['#8a8f96', 'Silver'], ['#3b3f45', 'Gunmetal'], ['#b3121b', 'Red'],
  ['#e0601c', 'Orange'], ['#f2c21b', 'Yellow'], ['#2f7d32', 'Green'], ['#1d5fbf', 'Blue'], ['#0d2a5c', 'Navy'],
  ['#6a2c91', 'Purple'], ['#5b6b3a', 'Olive'], ['#c9b48f', 'Sand'], ['#8c1c22', 'Maroon'],
];

// ---- units ---------------------------------------------------------------------------------------
const show = (f, v) => valueText(f, v, imperial);
const lenText = m => show({ unit: 'm' }, m);

// ---- registration ----------------------------------------------------------------------------------------
export function register(MODES) {
  MODES.builder = { open, enter, leave, render, frame: frameAll };
}

async function open(model = null) {
  if (S.model && S.pending.length && !(await A.save({ quiet: true }))) return;
  hideHome();
  A.showWorkspace(false);
  Object.assign(S, { model: null, summary: null, scene: null, config: null, undo: [], redo: [], pending: [],
    activePart: null, garageSpec: null });
  A.setMode('builder', { quiet: true });
  try {
    setLoading(true, 'Opening the Vehicle Builder…');
    if (!B.schema) B.schema = await api.builderSchema();
    if (model) {
      const r = await api.builderLoad(model);
      B.spec = r.spec;
      B.source = model;
    } else {
      B.spec = B.source ? null : (B.spec || store.get('draft', null));
      B.source = null;
      if (!B.spec) B.spec = styleValues('sedan');
    }
  } catch (e) {
    toast(e.message, 'err', 9000);
    if (!B.spec) B.spec = B.schema ? styleValues('sedan') : null;
  } finally { setLoading(false); }
  B.fresh = true;
  render();
  preview();
}

function enter() {
  S.modeHint = 'Vehicle Builder: change the sliders on the left; right-drag to turn the view.';
  const t = $('#builderTitle span');
  if (t) t.textContent = 'Vehicle Builder';
  $('#builderTitle svg')?.replaceWith(icon('build', 18));
}

function leave() {
  S.modeHint = null;
  if (B.spec && !B.source) store.set('draft', B.spec);
}

function frameAll() {
  if (!S.scene) return false;
  frame(S.scene.nodes.map(n => n.p));
  return true;
}

function styleValues(id) {
  const st = B.schema.styles.find(s => s.id === id) || B.schema.styles[0];
  return { ...st.values, name: 'My ' + st.title };
}

// ---- live preview --------------------------------------------------------------------------------------
const previewSoon = debounce(() => preview(), 120);
const previewLater = debounce(() => preview(), 600);

function changed(slow = false) {
  if (!B.source) store.set('draft', B.spec);
  (slow ? previewLater : previewSoon)();
}

async function preview() {
  if (!B.spec) return;
  const token = ++B.token;
  B.busy = true;
  busy(true);
  try {
    const have = [...V.meshCache.keys()].filter(k => k.includes('@'));
    const r = await api.builderPreview(B.spec, have);
    if (token !== B.token) return;
    apply(r);
  } catch (e) {
    if (token === B.token) toast('Could not build the design: ' + e.message, 'err', 9000);
  } finally {
    if (token === B.token) { B.busy = false; busy(false); }
  }
}

function b64(s, Type) {
  const bin = atob(s);
  const buf = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) buf[i] = bin.charCodeAt(i);
  return new Type(buf.buffer);
}

function apply(r) {
  closeDropTest();
  setPalette(r.materials);
  const meshes = {};
  for (const [k, m] of Object.entries(r.meshes)) meshes[k] = { pos: b64(m.pos, Float32Array), idx: b64(m.idx, Uint32Array), groups: m.groups };
  setMeshes(meshes);
  S.scene = r.scene;
  S.config = r.scene.config;
  reindex();
  // forget meshes of earlier designs
  const used = new Set(r.scene.flexbodies.map(f => f.mesh));
  for (const [k, g] of V.meshCache) if (k.includes('@') && !used.has(k)) { g.dispose(); V.meshCache.delete(k); }
  rebuild();
  buildMeshes();
  A.updateGroundOffset();
  A.applyDisplay();
  if (B.fresh) { frameAll(); B.fresh = false; }
  B.result = r;
  B.spec = { ...B.spec, model: r.model };
  const st = r.stats;
  A.updateStatus();   // the problems chip
  $('#statusLeft').textContent = `Vehicle Builder · ${r.spec.name} · ${st.nodes} nodes · ${st.beams} beams · ${st.triangles} triangles · ${fmt(st.mass, 0)} kg`;
  $('#statusRight').textContent = B.source ? `Editing ${B.source}` : 'Not created yet';
  renderSide();
}

function busy(on) {
  const s = $('#builderBusy');
  if (s) s.classList.toggle('hidden', !on);
}

// ---- left pane: style, categories, sliders ----------------------------------------------------------------
export function render() {
  const pane = $('#builderPane');
  if (!pane) return;
  pane.innerHTML = '';
  if (!B.schema || !B.spec) { pane.append(h('p', { class: 'note' }, 'Loading…')); return; }
  pane.append(h('div', { class: 'pane-head' },
    h('h2', {}, 'Vehicle Builder', h('span', { id: 'builderBusy', class: 'spinner' + (B.busy ? '' : ' hidden'),
      style: { width: '14px', height: '14px', borderWidth: '2px', display: 'inline-block', marginLeft: '8px', verticalAlign: '-2px' } })),
    h('p', { class: 'note' }, B.source
      ? `Editing ${B.spec.name || B.source}. "Update vehicle" rewrites it with your changes (the old files are backed up).`
      : 'Start from a body style, then shape it. Everything is generated for you: the body, the physics structure, suspension, wheels, engine and gearbox.'),
    h('div', { class: 'seg', style: { marginTop: '4px' } },
      h('button', { class: imperial ? '' : 'active', onclick: () => setUnits(false) }, 'Metric'),
      h('button', { class: imperial ? 'active' : '', onclick: () => setUnits(true) }, 'Imperial'))));
  pane.append(stylePicker());
  const cats = h('div', { class: 'catlist' });
  for (const g of B.schema.groups) {
    const b = h('button', { class: 'cat' + (B.cat === g.id ? ' active' : '') }, h('span', { class: 'ic' }, icon(g.icon, 18)), g.title);
    b.addEventListener('click', () => { B.cat = g.id; render(); });
    cats.append(b);
  }
  pane.append(cats);
  const body = h('div', { id: 'builderBody' });
  pane.append(body);
  groupPanel(body, B.cat);
}

function setUnits(imp) {
  imperial = imp;
  store.set('imperial', imp);
  render();
  renderSide();
}

function stylePicker() {
  const grid = h('div', { class: 'style-grid' });
  for (const st of B.schema.styles) {
    const b = h('button', { class: 'style-card' + (B.spec.style === st.id ? ' active' : ''), title: st.text },
      icon(STYLE_ICONS[st.id] || 'car', 18), h('span', {}, st.title));
    b.addEventListener('click', async () => {
      if (st.id === B.spec.style) return;
      const ok = await confirmBox('Start a new ' + st.title.toLowerCase() + '?',
        'Every setting goes back to the ' + st.title.toLowerCase() + '\'s starting point. The name you gave stays.', 'Start over');
      if (!ok) return;
      const keep = { name: B.spec.name, brand: B.spec.brand };
      B.spec = { ...styleValues(st.id), ...keep };
      B.fresh = true;
      render();
      changed();
    });
    grid.append(b);
  }
  return h('div', { class: 'gsec' }, h('h3', {}, icon('car', 16), 'Body style'), grid);
}

const GROUP_TEXT = {
  size: 'The outside dimensions. Wheelbase and track width change how it drives: longer is calmer, wider is more stable in corners.',
  shape: 'The silhouette: how long the hood is, how steep the windshield and back window are, and where the windows start.',
  wheels: 'Tire and rim sizes are real-world numbers (diameter in inches, width in millimetres). Bigger tires lift the vehicle.',
  suspension: 'Springs are set by how fast they bounce: around 1.2 Hz is a comfortable car, 2 Hz and up is a race car. Rates in N/m are worked out from the weight.',
  engine: 'Power and character make the torque curve; the gearing is worked out from the top speed.',
  weight: 'The weight is spread over the structure so it lands where a real vehicle has it. Brake torque follows the weight and tire size.',
  looks: 'Paint and details. The colour can also be changed in the game.',
};

function groupPanel(body, gid) {
  const fields = B.schema.fields.filter(f => f.group === gid);
  const basic = fields.filter(f => f.level === 'basic');
  const extra = fields.filter(f => f.level !== 'basic');
  const g = B.schema.groups.find(x => x.id === gid);
  const kids = basic.map(fieldControl);
  if (extra.length) {
    const open = !!B.more[gid];
    kids.push(h('button', { class: 'small ghost', onclick: () => { B.more[gid] = !open; render(); } },
      icon(open ? 'chevronDown' : 'chevronRight', 14), open ? 'Fewer settings' : `More settings (${extra.length})`));
    if (open) kids.push(...extra.map(fieldControl));
  }
  body.append(section(g.title, g.icon, GROUP_TEXT[gid] || '', ...kids));
  if (gid === 'size') body.append(sizeCard());
}

function fieldControl(f) {
  const v = B.spec[f.key];
  const set = (val, slow = false) => { B.spec = { ...B.spec, [f.key]: val }; changed(slow); };
  if (f.type === 'range') {
    return slider({
      label: f.label, min: f.min, max: f.max, step: f.step, value: v,
      show: x => show(f, x) + (f.key === 'frontOverhang' ? ` · rear ${lenText(Math.max(0, B.spec.length - B.spec.wheelbase - x))}` : ''),
      scale: f.lowLabel ? [f.lowLabel, f.highLabel] : null, help: f.help,
      onInput: (x, done) => { set(+x); if (done) renderSide(); },
    });
  }
  if (f.type === 'choice') {
    const wrap = h('div', { class: 'field' }, h('div', { class: 'name' }, f.label));
    if (f.options.length <= 3) {
      const seg = h('div', { class: 'seg wrap' });
      for (const [val, label] of f.options) {
        seg.append(h('button', { class: v === val ? 'active' : '', onclick: () => { set(val); render(); } }, label));
      }
      wrap.append(seg);
    } else {
      const sel = h('select', {}, ...f.options.map(([val, label]) => h('option', { value: val, selected: v === val }, label)));
      sel.addEventListener('change', () => set(sel.value));
      wrap.append(sel);
    }
    if (f.help) wrap.append(h('div', { class: 'help' }, f.help));
    return wrap;
  }
  if (f.type === 'bool') {
    const cb = h('input', { type: 'checkbox', checked: !!v });
    cb.addEventListener('change', () => set(cb.checked));
    return h('label', { class: 'chk field' }, cb, f.label);
  }
  if (f.type === 'color') {
    const pick = h('input', { type: 'color', value: v });
    pick.addEventListener('input', () => set(pick.value));
    pick.addEventListener('change', () => render());
    const sw = h('div', { class: 'swatches' });
    if (f.key === 'paint' || f.key === 'rimColor') {
      const list = f.key === 'paint' ? PAINTS : [['#c4c8cc', 'Silver'], ['#2b2d30', 'Black'], ['#8a8f96', 'Grey'], ['#c9a227', 'Gold'], ['#e8e8e8', 'White'], ['#b3121b', 'Red']];
      for (const [c, name] of list) {
        sw.append(h('button', { class: 'swatch' + (v === c ? ' active' : ''), title: name, style: { background: c },
          onclick: () => { set(c); render(); } }));
      }
    }
    return h('div', { class: 'field' }, h('div', { class: 'name' }, f.label), h('div', { class: 'toggle-row' }, pick, h('span', { class: 'muted' }, v)), sw);
  }
  return null;
}

function sizeCard() {
  const s = B.spec;
  const ro = Math.max(0, s.length - s.wheelbase - s.frontOverhang);
  return h('div', { class: 'card' }, h('b', {}, 'Proportions'),
    h('div', { class: 'compare', style: { gridTemplateColumns: 'auto 1fr' } },
      h('span', { class: 'k' }, 'Rear overhang'), h('span', {}, lenText(ro)),
      h('span', { class: 'k' }, 'Wheelbase / length'), h('span', {}, fmt(s.wheelbase / s.length * 100, 0) + ' %'),
      h('span', { class: 'k' }, 'Track / width'), h('span', {}, fmt(Math.max(s.trackF, s.trackR) / s.width * 100, 0) + ' %')));
}

// ---- right side: name, numbers, create ----------------------------------------------------------------------
function renderSide() {
  const box = $('#builderSide');
  if (!box) return;
  box.innerHTML = '';
  if (!B.spec) return;
  const r = B.result;
  const nameIn = h('input', { type: 'text', value: B.spec.name || '', placeholder: 'My vehicle' });
  nameIn.addEventListener('input', () => { B.spec = { ...B.spec, name: nameIn.value, model: B.source || '' }; changed(true); });
  nameIn.addEventListener('keydown', e => e.stopPropagation());
  const brandIn = h('input', { type: 'text', value: B.spec.brand || '', placeholder: 'Custom' });
  brandIn.addEventListener('input', () => { B.spec = { ...B.spec, brand: brandIn.value }; changed(true); });
  brandIn.addEventListener('keydown', e => e.stopPropagation());
  box.append(h('h3', {}, 'Your vehicle'),
    h('div', { class: 'form', style: { gridTemplateColumns: '64px 1fr' } },
      h('span', { class: 'k' }, 'Name'), nameIn, h('span', { class: 'k' }, 'Brand'), brandIn));
  if (r) {
    box.append(h('p', { class: 'note' }, 'Game id: ', h('code', {}, r.model),
      r.exists?.taken ? (r.exists.builder ? ' · already made with the Builder: creating it again updates it' : '') : ''));
    if (r.exists?.taken && !r.exists.builder) {
      box.append(h('p', { class: 'note warn' }, icon('warning', 13), ` A vehicle with the id "${r.model}" already exists (${r.exists.where}). Pick another name.`));
    }
    const st = r.stats;
    box.append(h('div', { class: 'stat-grid' },
      tile('Weight', show({ unit: 'kg' }, st.mass), `${fmt(st.frontShare * 100, 0)} / ${fmt(100 - st.frontShare * 100, 0)} front/rear`),
      tile('Power', fmt(st.powerHP, 0) + ' hp', `${fmt(st.torqueNm, 0)} Nm at ${st.torqueRPM} rpm`),
      tile('0–100 km/h', st.zeroTo100 ? fmt(st.zeroTo100, 1) + ' s' : '–', `${fmt(st.hpPerTon, 0)} hp per tonne`),
      tile('Top speed', show({ unit: 'km/h' }, st.topSpeed), 'estimated')));
    const chart = curveChart(st.curve, 1, 0, st.redline);
    box.append(h('h4', {}, 'Engine'), chart);
    box.append(h('h4', {}, 'Numbers'), h('div', { class: 'compare', style: { gridTemplateColumns: 'auto 1fr' } },
      ...row('Tires', st.tireF === st.tireR ? st.tireF : `${st.tireF} / ${st.tireR}`),
      ...row('Turning circle', lenText(st.turningCircle)),
      ...row('Ground clearance', lenText(B.spec.clearance)),
      ...row('Approach / departure', `${fmt(st.approach, 0)}° / ${fmt(st.departure, 0)}°`),
      ...row('Breakover', `${fmt(st.breakover, 0)}°`),
      ...row('Tips over at', `${fmt(st.rollG, 2)} g sideways`),
      ...row('Springs', `${fmt(st.springF / 1000, 1)} / ${fmt(st.springR / 1000, 1)} kN/m`),
      ...row('Gears', `${st.gears.length} + final drive ${fmt(st.finalDrive, 2)}`),
      ...row('Structure', `${st.nodes} nodes, ${st.beams} beams`)));
    if (st.rollRisk) box.append(h('p', { class: 'note warn' }, icon('warning', 13), ` It can roll over in hard corners (it tips at ${fmt(st.rollG, 2)} g, before the tires slide): make the track wider, or lower the ride height or the body.`));
    for (const n of r.notes || []) box.append(h('p', { class: 'note warn' }, icon('warning', 13), ' ', n));
  } else {
    box.append(h('p', { class: 'note' }, 'Building the preview…'));
  }
  const blocked = r?.exists?.taken && !r.exists.builder;
  box.append(h('div', { class: 'btnrow' },
    h('button', { class: 'primary big', disabled: !r || blocked, onclick: createDialog }, icon('build', 16),
      r?.exists?.builder ? 'Update vehicle…' : 'Create vehicle…'),
    h('button', { disabled: !r, onclick: () => runDropTest(), title: 'Drop it onto the ground in a quick physics test' }, icon('physics', 15), 'Drop test'),
    h('button', { onclick: startOver, title: 'Go back to this body style\'s starting point' }, 'Start over')),
  h('p', { class: 'note' }, 'Creating writes a mod into your BeamNG mods folder. Spawn it from the game\'s vehicle selector like any car; it also opens here for the Garage, parts, tuning and 3D tools.'));
}

function tile(k, v, d) {
  return h('div', { class: 'stat' }, h('div', { class: 'k' }, k), h('div', { class: 'v' }, v), d ? h('div', { class: 'd muted' }, d) : null);
}
function row(k, v) { return [h('span', { class: 'k' }, k), h('span', {}, v)]; }

async function startOver() {
  const ok = await confirmBox('Start over?', 'All settings go back to the body style\'s starting point.', 'Start over');
  if (!ok) return;
  const keep = { name: B.spec.name, brand: B.spec.brand };
  B.spec = { ...styleValues(B.spec.style), ...keep };
  B.fresh = true;
  render();
  changed();
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
    return c.toDataURL('image/jpeg', 0.88);
  } catch { return null; }
}

function createDialog() {
  const r = B.result;
  if (!r) return;
  const update = !!r.exists?.builder;
  const modIn = h('input', { type: 'text', value: r.model });
  modIn.addEventListener('keydown', e => e.stopPropagation());
  const pic = snapshot();
  const usePic = h('input', { type: 'checkbox', checked: !!pic });
  const err = h('div', { class: 'err' });
  const go = async () => {
    err.textContent = '';
    try {
      setLoading(true, update ? 'Updating the vehicle…' : 'Creating the vehicle…');
      const res = await api.builderCreate({ spec: B.spec, modName: modIn.value.trim() || r.model, picture: usePic.checked ? pic : null });
      closeModal();
      B.source = res.model;
      store.set('draft', null);
      await A.refreshVehicles();
      render();
      renderSide();
      doneDialog(res);
    } catch (e) { err.textContent = e.message; } finally { setLoading(false); }
  };
  openModal(update ? 'Update vehicle' : 'Create vehicle', h('div', {},
    h('div', { style: { display: 'flex', gap: '14px', alignItems: 'flex-start' } },
      pic ? h('img', { src: pic, style: { width: '220px', borderRadius: '10px', border: '1px solid var(--line)' } }) : null,
      h('div', { style: { flex: 1 } },
        h('p', { style: { marginTop: 0 } }, h('b', {}, B.spec.name), ` (${r.model})`),
        h('ul', { class: 'changes' },
          h('li', {}, `${r.scene.nodes.length} nodes, ${r.scene.beams.length} beams, ${r.scene.wheels.length} wheels`),
          h('li', {}, `${fmt(r.stats.powerHP, 0)} hp, ${r.stats.gears.length}-speed ${B.spec.gearbox}, ${B.spec.drive}`),
          h('li', {}, `${show({ unit: 'kg' }, r.stats.mass)}, tires ${r.stats.tireF}`)),
        update ? h('p', { class: 'note' }, 'This rewrites the vehicle you made before. Its old files are backed up first.') : null)),
    h('div', { class: 'form', style: { marginTop: '12px' } },
      update ? null : h('span', { class: 'k' }, 'Mod folder'), update ? null : modIn,
      h('span', { class: 'k' }, 'Picture'), h('label', { class: 'chk' }, usePic, 'Use this view as the picture in the game\'s vehicle selector')),
    err,
    h('div', { class: 'actions' }, h('button', { onclick: closeModal }, 'Cancel'),
      h('button', { class: 'primary', onclick: go }, icon('build', 15), update ? 'Update' : 'Create'))));
}

function doneDialog(res) {
  openModal(res.updated ? 'Vehicle updated' : 'Vehicle created', h('div', {},
    h('p', {}, h('b', {}, B.spec.name), ' is in ', h('code', {}, shortPath(res.path)), '.'),
    h('p', { class: 'note' }, 'In BeamNG.drive, open the vehicle selector and pick it (if the game was running, press Ctrl+L to reload the vehicle list, or Ctrl+R to reload a spawned one).'),
    h('div', { class: 'actions' },
      h('button', { onclick: () => api.reveal(res.path).catch(e => toast(e.message, 'err')) }, icon('folder', 15), 'Show the files'),
      h('button', { onclick: closeModal }, 'Keep building'),
      h('button', { class: 'primary', onclick: () => { closeModal(); A.openVehicle(res.model, { mode: 'garage' }); } }, icon('wrench', 15), 'Open in the Garage'))));
}
