// Scrap Mechanic import: pick one of your creations, see it as a BeamNG vehicle at once, adjust how it
// drives, and write it as a mod. The wheels become real wheels on suspension, the driver's seat steers
// the wheels it steers in Scrap Mechanic and the engines drive the wheels they drive.
import { S, A } from './state.js';
import { api } from './api.js';
import { icon } from './icons.js';
import { V, buildMeshes, clearView as clearDrawn, frame, rebuild, setMeshes, setPalette } from './view3d.js';
import { reindex } from './scene.js';
import { hideHome } from './home.js';
import { applies, batteryRows, curveChart, fieldControl, gearsText, section, torqueText, valueText } from './widgets.js';
import { closePanel as closeDropTest, runDropTest } from './droptest.js';
import { $, h, fmt, toast, debounce, openModal, closeModal, setLoading, shortPath, confirmBox } from './util.js';

const M = {
  list: null,          // {game, creations} from the server
  query: '',
  filter: 'all',
  shown: 60,
  creation: null,      // the creation being converted: {id, name, source, icon}
  settings: {},        // what the user changed (the server fills in the rest)
  result: null,        // last preview
  cat: 'fit',
  more: {},
  source: null,        // vehicle id when re-importing one made before
  token: 0,
  busy: false,
  fresh: true,
  loadingList: false,
};

const store = {
  get(k, d) { try { const v = localStorage.getItem('jbe.' + k); return v === null ? d : JSON.parse(v); } catch { return d; } },
  set(k, v) { try { localStorage.setItem('jbe.' + k, JSON.stringify(v)); } catch { /* private window */ } },
};
const imperial = () => store.get('builder.imperial', false);
const show = (f, v) => valueText(f, v, imperial());

const GROUP_TEXT = {
  fit: 'The creation is turned so its wheels roll forward, with the steered wheels at the front. Change the size to make a model car or a giant.',
  wheels: 'Every wheel on a bearing becomes a real wheel on a coil-over. Scrap Mechanic off-road suspension starts soft and long, sport suspension firm and short, and wheels with none get a short, firm ride.',
  engine: 'Power starts at what your engines give in Scrap Mechanic, and electric engines become an electric motor with a battery. The gearing is worked out from the top speed.',
  weight: 'The weight is spread over the structure the way the blocks are, so it balances like your creation.',
};

// ---- registration ------------------------------------------------------------------------------------
export function register(MODES) {
  MODES.scrap = { open, enter, leave, render, frame: frameAll };
}

async function open(model = null) {
  if (S.model && S.pending.length && !(await A.save({ quiet: true }))) return;
  hideHome();
  A.showWorkspace(false);
  Object.assign(S, { model: null, summary: null, scene: null, config: null, undo: [], redo: [], pending: [],
    activePart: null, garageSpec: null });
  clearView();
  A.setMode('scrap', { quiet: true });
  M.result = null;
  if (model) {
    try {
      setLoading(true, 'Reading how ' + model + ' was imported…');
      const r = await api.scrapLoad(model);
      if (!r.found) {
        toast(`The creation ${model} was made from is not on this computer any more (${shortPath(r.source.path || '')}). Pick it again.`, 'err', 10000);
        M.creation = null;
      } else {
        M.creation = { id: 'file:' + r.source.path, name: r.source.name, source: 'file', icon: false };
        M.settings = { ...r.settings };
        M.source = model;
      }
    } catch (e) { toast(e.message, 'err', 9000); } finally { setLoading(false); }
  } else {
    M.source = null;
  }
  render();
  renderSide();
  if (M.creation) { M.fresh = true; preview(); } else loadList();
}

function enter() {
  S.modeHint = 'Scrap Mechanic import: pick a creation on the left, then adjust it. Right-drag to turn the view.';
  const t = $('#builderTitle span');
  if (t) t.textContent = 'Scrap Mechanic import';
  $('#builderTitle svg')?.replaceWith(icon('scrap', 18));
}

function leave() {
  S.modeHint = null;
}

function frameAll() {
  if (!S.scene) return false;
  frame(S.scene.nodes.map(n => n.p));
  return true;
}

function clearView() {
  closeDropTest();
  S.scene = null;
  clearDrawn();
}

// ---- the list of creations ----------------------------------------------------------------------------
async function loadList(refresh = false) {
  M.loadingList = true;
  render();
  try {
    M.list = await api.scrapList(refresh);
  } catch (e) {
    toast('Could not look for Scrap Mechanic creations: ' + e.message, 'err', 9000);
    M.list = { game: { found: false }, creations: [] };
  } finally {
    M.loadingList = false;
    render();
  }
}

function pick(c) {
  const same = M.creation && M.creation.id === c.id;
  M.creation = { id: c.id, name: c.name, source: c.source, icon: c.icon };
  if (!same) {
    M.settings = {};
    M.source = null;
    M.cat = 'fit';
  }
  M.fresh = true;
  M.result = null;
  clearView();
  render();
  renderSide();
  preview();
}

function chooseAnother() {
  M.creation = null;
  M.result = null;
  M.source = null;
  clearView();
  $('#statusLeft').textContent = 'Scrap Mechanic import';
  $('#statusRight').textContent = '';
  render();
  renderSide();
  if (!M.list) loadList();
}

function whenText(t) {
  if (!t) return '';
  const d = new Date(t * 1000);
  return d.toLocaleDateString(undefined, { year: 'numeric', month: 'short', day: 'numeric' });
}

function listView(pane) {
  pane.append(h('div', { class: 'pane-head' },
    h('h2', {}, 'Import from Scrap Mechanic'),
    h('p', { class: 'note' }, 'Pick a creation. Its wheels, suspension, steering and engine become a vehicle that drives in BeamNG, and it keeps its blocks and colours.')));
  const L = M.list;
  if (M.loadingList && !L) {
    pane.append(h('p', { class: 'note' }, h('span', { class: 'spinner', style: { width: '14px', height: '14px', borderWidth: '2px', display: 'inline-block', verticalAlign: '-2px', marginRight: '6px' } }), 'Looking for your creations…'));
    return;
  }
  if (!L) return;
  const g = L.game || {};
  pane.append(h('div', { class: 'card' },
    h('div', { style: { display: 'flex', alignItems: 'center', gap: '8px' } },
      h('span', { class: 'chip small ' + (g.found ? 'good' : 'warn') }, icon(g.found ? 'check' : 'warning', 14), g.found ? 'Scrap Mechanic found' : 'Scrap Mechanic not found'),
      h('span', { class: 'grow' }),
      h('button', { class: 'small ghost', title: 'Look again', onclick: () => loadList(true) }, icon('refresh', 14), 'Refresh')),
    g.found ? null : h('p', { class: 'note' }, 'Without the game the editor guesses part sizes. Set its folder in ',
      h('a', { href: '#', onclick: e => { e.preventDefault(); A.settings(); } }, 'Settings'), ' (it is the one with Data and Survival in it).')));
  // search and filter
  const search = h('input', { type: 'search', placeholder: 'Find a creation…', value: M.query });
  search.addEventListener('input', () => { M.query = search.value; M.shown = 60; renderItems(); });
  search.addEventListener('keydown', e => e.stopPropagation());
  const seg = h('div', { class: 'seg', style: { marginTop: '6px' } });
  for (const [f, label] of [['all', 'All'], ['mine', 'Mine'], ['workshop', 'Workshop']]) {
    seg.append(h('button', { class: M.filter === f ? 'active' : '', onclick: () => { M.filter = f; M.shown = 60; render(); } }, label));
  }
  pane.append(h('div', { class: 'search' }, icon('search', 16), search), seg);
  const items = h('div', { class: 'sm-list', id: 'smList' });
  pane.append(items);
  renderItems();
  // a blueprint file anywhere
  const pathIn = h('input', { type: 'text', placeholder: '…\\Blueprints\\<id>\\blueprint.json' });
  pathIn.addEventListener('keydown', e => { e.stopPropagation(); if (e.key === 'Enter') openPath(); });
  async function openPath() {
    const p = pathIn.value.trim();
    if (!p) return;
    try {
      const c = await api.scrapEntry({ path: p });
      pick(c);
    } catch (e) { toast(e.message, 'err', 8000); }
  }
  pane.append(section('Open a blueprint file', 'folder', 'A creation from somewhere else: paste the path of its blueprint.json, or of the folder it is in.',
    h('div', { style: { display: 'flex', gap: '6px', marginBottom: '8px' } }, pathIn, h('button', { onclick: openPath }, 'Open'))));
}

function renderItems() {
  const box = $('#smList');
  if (!box || !M.list) return;
  box.innerHTML = '';
  const q = M.query.trim().toLowerCase();
  const all = (M.list.creations || []).filter(c => (M.filter === 'all' || c.source === M.filter) &&
    (!q || c.name.toLowerCase().includes(q) || (c.description || '').toLowerCase().includes(q)));
  if (!all.length) {
    const none = !(M.list.creations || []).length;
    box.append(h('div', { class: 'empty' }, none
      ? h('div', {}, h('p', {}, 'No creations found.'), h('p', { class: 'note' }, 'Save a creation with the lift in Scrap Mechanic, or subscribe to one on the Steam Workshop, then press Refresh.'))
      : 'Nothing matches.'));
    return;
  }
  for (const c of all.slice(0, M.shown)) {
    const img = c.icon ? h('img', { loading: 'lazy', alt: '', src: 'api/scrap_icon?id=' + encodeURIComponent(c.id) }) : null;
    const thumb = h('span', { class: 'sm-thumb' }, img || icon('scrap', 26));
    img?.addEventListener('error', () => { img.remove(); thumb.append(icon('scrap', 26)); });
    const b = h('button', { class: 'sm-item' + (M.creation?.id === c.id ? ' active' : ''), title: c.description || c.name },
      thumb,
      h('span', { class: 'meta' }, h('span', { class: 'name' }, c.name),
        h('span', { class: 'sub' }, c.source === 'mine' ? 'My creation' : 'Workshop', c.mtime ? ' · ' + whenText(c.mtime) : '')));
    b.addEventListener('click', () => pick(c));
    box.append(b);
  }
  if (all.length > M.shown) {
    box.append(h('button', { class: 'small ghost', onclick: () => { M.shown += 100; renderItems(); } }, `Show more (${all.length - M.shown})`));
  }
}

// ---- live preview ---------------------------------------------------------------------------------------
const previewSoon = debounce(() => preview(), 150);
const previewLater = debounce(() => preview(), 600);

function changed(slow = false) {
  (slow ? previewLater : previewSoon)();
}

async function preview() {
  if (!M.creation) return;
  const token = ++M.token;
  M.busy = true;
  busy(true);
  try {
    const have = [...V.meshCache.keys()].filter(k => k.includes('@'));
    const r = await api.scrapPreview({ id: M.creation.id, settings: M.settings, have });
    if (token !== M.token) return;
    apply(r);
  } catch (e) {
    if (token === M.token) {
      M.result = { error: e.message };
      render();
      renderSide();
    }
  } finally {
    if (token === M.token) { M.busy = false; busy(false); }
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
  const first = !M.result || M.result.error;
  setPalette(r.materials);
  const meshes = {};
  for (const [k, m] of Object.entries(r.meshes)) meshes[k] = { pos: b64(m.pos, Float32Array), idx: b64(m.idx, Uint32Array), groups: m.groups };
  setMeshes(meshes);
  S.scene = r.scene;
  S.config = r.scene.config;
  reindex();
  const used = new Set(r.scene.flexbodies.map(f => f.mesh));
  for (const [k, g] of V.meshCache) if (k.includes('@') && !used.has(k)) { g.dispose(); V.meshCache.delete(k); }
  rebuild();
  buildMeshes();
  A.updateGroundOffset();
  A.applyDisplay();
  if (M.fresh) { frameAll(); M.fresh = false; }
  M.result = r;
  M.creation = { ...M.creation, ...r.creation };
  const st = r.stats;
  A.updateStatus();
  $('#statusLeft').textContent = `Scrap Mechanic import · ${r.settings.name || r.creation.name} · ${st.nodes} nodes · ${st.beams} beams · ${fmt(st.mass, 0)} kg`;
  $('#statusRight').textContent = M.source ? `Re-importing ${M.source}` : 'Not created yet';
  if (first) render();
  else renderFound();
  renderSide();
}

function busy(on) {
  const s = $('#scrapBusy');
  if (s) s.classList.toggle('hidden', !on);
}

// ---- left pane -----------------------------------------------------------------------------------------
export function render() {
  const pane = $('#scrapPane');
  if (!pane) return;
  pane.innerHTML = '';
  if (!M.creation) { listView(pane); return; }
  const c = M.creation;
  const img = c.icon ? h('img', { alt: '', src: 'api/scrap_icon?id=' + encodeURIComponent(c.id) }) : null;
  const thumb = h('span', { class: 'sm-thumb' }, img || icon('scrap', 26));
  img?.addEventListener('error', () => { img.remove(); thumb.append(icon('scrap', 26)); });
  pane.append(h('div', { class: 'pane-head' },
    h('h2', {}, 'Scrap Mechanic import', h('span', { id: 'scrapBusy', class: 'spinner' + (M.busy ? '' : ' hidden'),
      style: { width: '14px', height: '14px', borderWidth: '2px', display: 'inline-block', marginLeft: '8px', verticalAlign: '-2px' } })),
    h('div', { class: 'sm-item active', style: { cursor: 'default' } }, thumb,
      h('span', { class: 'meta' }, h('span', { class: 'name' }, c.name || 'Creation'),
        h('span', { class: 'sub' }, M.source ? `Re-importing ${M.source}` : 'Converting it as you change the settings'))),
    h('div', { class: 'btnrow', style: { marginTop: '6px' } },
      h('button', { class: 'small', onclick: chooseAnother }, icon('chevronLeft', 14), 'Choose another creation'))));
  const r = M.result;
  if (r?.error) {
    pane.append(h('div', { class: 'card' }, h('p', { class: 'note warn' }, icon('warning', 13), ' ', r.error),
      h('p', { class: 'note' }, 'In Scrap Mechanic, a vehicle needs its wheels on bearings (directly or on suspension), the driver\'s seat connected to the front bearings to steer, and an engine connected to the bearings it should drive.')));
    return;
  }
  if (!r) { pane.append(h('p', { class: 'note' }, 'Converting…')); return; }
  pane.append(h('div', { id: 'smFound' }));
  renderFound();
  const cats = h('div', { class: 'catlist' });
  for (const g of r.schema.groups) {
    const b = h('button', { class: 'cat' + (M.cat === g.id ? ' active' : '') }, h('span', { class: 'ic' }, icon(g.icon, 18)), g.title);
    b.addEventListener('click', () => { M.cat = g.id; render(); });
    cats.append(b);
  }
  pane.append(cats);
  const body = h('div', {});
  pane.append(body);
  groupPanel(body, M.cat);
}

function renderFound() {
  const box = $('#smFound');
  const r = M.result;
  if (!box || !r || r.error) return;
  const s = r.summary;
  box.innerHTML = '';
  const susp = Object.entries(s.suspension || {}).map(([k, n]) => `${n}× ${({ offroad: 'off-road', sport: 'sport', generic: 'suspension', none: 'no suspension' })[k] || k}`).join(', ');
  const rows = [
    ['Blocks and parts', `${s.blocks} blocks, ${s.parts} parts`],
    ['Wheels', `${s.wheels} (${s.steered} steered, ${s.driven} driven)`],
    ['Suspension', susp || '–'],
    ['Engine', s.engines.length ? `${s.engines.length > 1 ? s.engines.length + '× ' : ''}${s.engines[0]}${s.enginePower ? ' ≈ ' + fmt(s.enginePower, 0) + ' hp' : ''}` : 'none (one is added)'],
    ['Driver\'s seat', s.seat ? 'yes' : 'none'],
    ['Weight in Scrap Mechanic', show({ unit: 'kg' }, s.massBuilt)],
  ];
  if (s.unknown) rows.push(['Unknown parts', `${s.unknown} (mods not installed)`]);
  box.append(h('div', { class: 'card' }, h('b', {}, 'What was found'),
    h('div', { class: 'compare', style: { gridTemplateColumns: 'auto 1fr', marginTop: '6px' } },
      ...rows.flatMap(([k, v]) => [h('span', { class: 'k' }, k), h('span', {}, v)]))));
}

// the settings as the page should show them: what the last preview used, with every change made since
function values() {
  return { ...(M.result?.settings || {}), ...M.settings };
}

function groupPanel(body, gid) {
  const r = M.result;
  const fields = r.schema.fields.filter(f => f.group === gid && applies(f, values()));
  const basic = fields.filter(f => f.level === 'basic');
  const extra = fields.filter(f => f.level !== 'basic');
  const g = r.schema.groups.find(x => x.id === gid);
  const kids = basic.map(control);
  if (extra.length) {
    const open = !!M.more[gid];
    kids.push(h('button', { class: 'small ghost', onclick: () => { M.more[gid] = !open; render(); } },
      icon(open ? 'chevronDown' : 'chevronRight', 14), open ? 'Fewer settings' : `More settings (${extra.length})`));
    if (open) kids.push(...extra.map(control));
  }
  body.append(section(g.title, g.icon, GROUP_TEXT[gid] || '', ...kids));
  if (gid === 'weight' && M.settings.massAuto === false) {
    body.append(h('button', { class: 'small', onclick: () => { delete M.settings.mass; M.settings.massAuto = true; render(); changed(); } },
      'Back to the weight it has in Scrap Mechanic'));
  }
}

function control(f) {
  const r = M.result;
  const v = values()[f.key];
  const set = (val, done) => {
    M.settings = { ...M.settings, [f.key]: val };
    if (f.key === 'mass') M.settings.massAuto = false;
    if (f.key === 'turn' || f.key === 'scale') M.fresh = true;
    changed(!done && f.type === 'range');
  };
  const builtHint = f.key === 'power' && r.summary.enginePower ? ` · creation ${fmt(r.summary.enginePower, 0)} hp` : '';
  return fieldControl(f, v, set, {
    imperial: imperial(), rerender: render,
    show: x => show(f, x) + builtHint,
    swatches: f.key === 'rimColor' ? [['#b8bcc0', 'Silver'], ['#2b2d30', 'Black'], ['#8a8f96', 'Grey'], ['#c9a227', 'Gold'], ['#e8e8e8', 'White'], ['#b3121b', 'Red']] : null,
  });
}

// ---- right side -----------------------------------------------------------------------------------------
function renderSide() {
  const box = $('#scrapSide');
  if (!box) return;
  box.innerHTML = '';
  const r = M.result;
  if (!M.creation) {
    box.append(h('h3', {}, 'How it works'),
      h('ol', { class: 'changes' },
        h('li', {}, 'Pick a creation you saved with the lift (or one from the Workshop).'),
        h('li', {}, 'It turns into a vehicle: every wheel on a bearing becomes a real tire on suspension, the driver\'s seat steers, the engines drive.'),
        h('li', {}, 'Adjust the size, ride, power and weight, and drop it onto the ground to test it.'),
        h('li', {}, 'Create it: it is written as a mod and shows up in BeamNG\'s vehicle selector.')),
      h('p', { class: 'note' }, 'Tip: in Scrap Mechanic, connect the driver\'s seat to the front wheel bearings and the engine to the bearings that should drive, so the import knows which wheels steer and which are driven.'));
    return;
  }
  if (!r || r.error) {
    box.append(h('p', { class: 'note' }, r?.error ? 'This creation could not be converted.' : 'Converting the creation…'));
    return;
  }
  const nameIn = h('input', { type: 'text', value: r.settings.name || '', placeholder: 'My vehicle' });
  nameIn.addEventListener('input', () => { M.settings = { ...M.settings, name: nameIn.value, model: M.source || '' }; changed(true); });
  nameIn.addEventListener('keydown', e => e.stopPropagation());
  const brandIn = h('input', { type: 'text', value: r.settings.brand || '', placeholder: 'Scrap Mechanic' });
  brandIn.addEventListener('input', () => { M.settings = { ...M.settings, brand: brandIn.value }; changed(true); });
  brandIn.addEventListener('keydown', e => e.stopPropagation());
  box.append(h('h3', {}, 'Your vehicle'),
    h('div', { class: 'form', style: { gridTemplateColumns: '64px 1fr' } },
      h('span', { class: 'k' }, 'Name'), nameIn, h('span', { class: 'k' }, 'Brand'), brandIn),
    h('p', { class: 'note' }, 'Game id: ', h('code', {}, r.model),
      r.exists?.taken && r.exists.scrap ? ' · imported before: creating it again updates it' : ''));
  if (r.exists?.taken && !r.exists.scrap) {
    box.append(h('p', { class: 'note warn' }, icon('warning', 13), ` A vehicle with the id "${r.model}" already exists (${r.exists.where}). Pick another name.`));
  }
  const st = r.stats;
  box.append(h('div', { class: 'stat-grid' },
    tile('Weight', show({ unit: 'kg' }, st.mass), `${fmt(st.frontShare * 100, 0)} / ${fmt(100 - st.frontShare * 100, 0)} front/rear`),
    tile('Power', fmt(st.powerHP, 0) + ' hp', torqueText(st)),
    tile('0–100 km/h', st.zeroTo100 ? fmt(st.zeroTo100, 1) + ' s' : '–', `${fmt(st.hpPerTon, 0)} hp per tonne`),
    tile('Top speed', show({ unit: 'km/h' }, st.topSpeed), 'estimated')));
  box.append(h('h4', {}, 'Axles'), h('div', { class: 'compare', style: { gridTemplateColumns: 'auto 1fr' } },
    ...st.axles.flatMap(a => [h('span', { class: 'k' }, a.name),
      h('span', {}, `${a.wheels === 2 ? 'pair' : a.wheels + ' wheel'} · ${a.tire}${a.steer ? ' · steers' : ''}${a.driven ? ' · driven' : ''}`)])));
  box.append(h('h4', {}, st.electric ? 'Electric motor' : 'Engine'), curveChart(st.curve, 1, 0, st.redline));
  const len = m => show({ unit: 'm' }, m);
  box.append(h('h4', {}, 'Numbers'), h('div', { class: 'compare', style: { gridTemplateColumns: 'auto 1fr' } },
    ...row('Size', `${len(st.length)} × ${len(st.width)} × ${len(st.height)}`),
    ...row('Wheelbase', len(st.wheelbase)),
    ...row('Ground clearance', len(st.clearance)),
    ...row('Turning circle', st.turningCircle ? len(st.turningCircle) : '–'),
    ...row('Tips over at', `${fmt(st.rollG, 2)} g sideways`),
    ...row('Gears', gearsText(st)),
    ...batteryRows(st, imperial()).flatMap(([k, v]) => row(k, v)),
    ...row('Structure', `${st.nodes} nodes, ${st.beams} beams (${fmt(st.lattice * 100, 0)} cm grid)`)));
  if (st.rollRisk) box.append(h('p', { class: 'note warn' }, icon('warning', 13), ` It can roll over in hard corners (it tips at ${fmt(st.rollG, 2)} g, before the tires slide): it is tall for how far apart its wheels are. In Scrap Mechanic, put the wheels further out or build it lower.`));
  for (const n of r.notes || []) box.append(h('p', { class: 'note warn' }, icon('warning', 13), ' ', n));
  const blocked = r.exists?.taken && !r.exists.scrap;
  box.append(h('div', { class: 'btnrow' },
    h('button', { class: 'primary big', disabled: blocked, onclick: createDialog }, icon('build', 16),
      r.exists?.scrap ? 'Update vehicle…' : 'Create vehicle…'),
    h('button', { onclick: () => runDropTest(), title: 'Drop it onto the ground in a quick physics test' }, icon('physics', 15), 'Drop test'),
    h('button', { onclick: resetSettings, title: 'Go back to what was found in the creation' }, 'Reset')),
  h('p', { class: 'note' }, 'Creating writes a mod into your BeamNG mods folder. Spawn it from the game\'s vehicle selector; it also opens here for the Garage, tuning and the 3D tools.'));
}

function tile(k, v, d) {
  return h('div', { class: 'stat' }, h('div', { class: 'k' }, k), h('div', { class: 'v' }, v), d ? h('div', { class: 'd muted' }, d) : null);
}
function row(k, v) { return [h('span', { class: 'k' }, k), h('span', {}, v)]; }

async function resetSettings() {
  const ok = await confirmBox('Reset the settings?', 'Everything goes back to what was found in the creation. The name you gave stays.', 'Reset');
  if (!ok) return;
  const keep = { name: M.settings.name, brand: M.settings.brand };
  M.settings = {};
  for (const [k, v] of Object.entries(keep)) if (v) M.settings[k] = v;
  M.fresh = true;
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
  const r = M.result;
  if (!r || r.error) return;
  const update = !!r.exists?.scrap;
  const modIn = h('input', { type: 'text', value: r.model });
  modIn.addEventListener('keydown', e => e.stopPropagation());
  const pic = snapshot();
  const usePic = h('input', { type: 'checkbox', checked: !!pic });
  const err = h('div', { class: 'err' });
  const go = async () => {
    err.textContent = '';
    try {
      setLoading(true, update ? 'Updating the vehicle…' : 'Creating the vehicle…');
      const res = await api.scrapCreate({ id: M.creation.id, settings: M.settings, modName: modIn.value.trim() || r.model,
        picture: usePic.checked ? pic : null });
      closeModal();
      M.source = res.model;
      await A.refreshVehicles();
      await preview();
      doneDialog(res, r.settings.name);
    } catch (e) { err.textContent = e.message; } finally { setLoading(false); }
  };
  const st = r.stats;
  openModal(update ? 'Update vehicle' : 'Create vehicle', h('div', {},
    h('div', { style: { display: 'flex', gap: '14px', alignItems: 'flex-start' } },
      pic ? h('img', { src: pic, style: { width: '220px', borderRadius: '10px', border: '1px solid var(--line)' } }) : null,
      h('div', { style: { flex: 1 } },
        h('p', { style: { marginTop: 0 } }, h('b', {}, r.settings.name), ` (${r.model})`),
        h('ul', { class: 'changes' },
          h('li', {}, `${r.scene.nodes.length} nodes, ${r.scene.beams.length} beams, ${r.scene.wheels.length} wheels`),
          h('li', {}, `${fmt(st.powerHP, 0)} hp, ${st.electric ? `electric, ${fmt(st.battery, 0)} kWh battery` : `${st.gears.length}-speed ${r.settings.gearbox}`}`),
          h('li', {}, `${show({ unit: 'kg' }, st.mass)}, ${st.axles.length} axle${st.axles.length > 1 ? 's' : ''}`)),
        update ? h('p', { class: 'note' }, 'This rewrites the vehicle you imported before. Its old files are backed up first.') : null)),
    h('div', { class: 'form', style: { marginTop: '12px' } },
      update ? null : h('span', { class: 'k' }, 'Mod folder'), update ? null : modIn,
      h('span', { class: 'k' }, 'Picture'), h('label', { class: 'chk' }, usePic, 'Use this view as the picture in the game\'s vehicle selector')),
    err,
    h('div', { class: 'actions' }, h('button', { onclick: closeModal }, 'Cancel'),
      h('button', { class: 'primary', onclick: go }, icon('build', 15), update ? 'Update' : 'Create'))));
}

function doneDialog(res, name) {
  openModal(res.updated ? 'Vehicle updated' : 'Vehicle created', h('div', {},
    h('p', {}, h('b', {}, name), ' is in ', h('code', {}, shortPath(res.path)), '.'),
    h('p', { class: 'note' }, 'In BeamNG.drive, open the vehicle selector and pick it (if the game was running, press Ctrl+L to reload the vehicle list, or Ctrl+R to reload a spawned one).'),
    h('div', { class: 'actions' },
      h('button', { onclick: () => api.reveal(res.path).catch(e => toast(e.message, 'err')) }, icon('folder', 15), 'Show the files'),
      h('button', { onclick: closeModal }, 'Keep adjusting'),
      h('button', { class: 'primary', onclick: () => { closeModal(); A.openVehicle(res.model, { mode: 'garage' }); } }, icon('wrench', 15), 'Open in the Garage'))));
}
