// Side panels: the parts tree (like the game's parts menu), tuning sliders,
// problems, the properties of what's selected, and weights / auto-weight.
import { S, A, emit, clearSelection } from './state.js';
import { DAMP_RED, DAMP_YELLOW, DT, STIFF_RED, STIFF_TARGET, STIFF_YELLOW, effOpts, minStableWeight, mirrorPartner, partEditable,
  partMass, toRaw, totalMass, BEAM_TYPE_INFO } from './scene.js';
import { commit, opNodeMove, opNodeSet, opRowSet } from './edit.js';
import { deleteSelection, flipSelectedTris, connectSelected, frameSelection } from './tools.js';
import { frame } from './view3d.js';
import { $, closeModal, debounce, fmt, h, num, numInput, openModal, popupMenu, textInput, toast } from './util.js';
import { addSlotDialog, newPartDialog, partTextDialog } from './dialogs.js';
import { icon } from './icons.js';

const editable = el => !!S.scene.files[el.f]?.editable;

// =====================================================================================
// Parts tree
// =====================================================================================
const expanded = new Set(['/']);
let expandedFor = null;   // the vehicle the open/closed state belongs to

export function renderParts() {
  const box = $('#partsTree');
  box.innerHTML = '';
  const sc = S.scene;
  if (!sc) return;
  if (expandedFor !== S.model) {
    expanded.clear();
    expanded.add('/');
    for (const c of sc.tree.children || []) expanded.add(c.path);
    expandedFor = S.model;
  }
  const q = $('#partSearch').value.trim().toLowerCase();
  const el = slotNode(sc.tree, 0, q);
  if (el) box.append(el); else box.append(h('p', { class: 'muted' }, 'Nothing matches.'));
}

function slotMatches(slot, q) {
  const titles = S.scene.partTitles;
  if ((slot.description || '').toLowerCase().includes(q) || slot.id.toLowerCase().includes(q)) return true;
  if (slot.chosen && (slot.chosen.toLowerCase().includes(q) || (titles[slot.chosen] || '').toLowerCase().includes(q))) return true;
  return slot.options.some(o => o.toLowerCase().includes(q) || (titles[o] || '').toLowerCase().includes(q));
}
function subtreeMatches(slot, q) {
  return slotMatches(slot, q) || (slot.children || []).some(c => subtreeMatches(c, q));
}

function slotNode(slot, depth, q) {
  if (q && !subtreeMatches(slot, q)) return null;
  const sc = S.scene;
  const kids = slot.children || [];
  const open = q ? true : expanded.has(slot.path);
  const chosen = slot.chosen || '';
  const canEdit = chosen && partEditable(chosen);

  const sel = h('select', { title: chosen ? `${sc.partTitles[chosen] || chosen}\n(${chosen})` : 'Empty' });
  if (!slot.coreSlot || !chosen) sel.append(h('option', { value: '' }, 'Empty'));
  const opts = slot.options.slice();
  if (chosen && !opts.includes(chosen)) opts.unshift(chosen);
  for (const o of opts) sel.append(h('option', { value: o, selected: o === chosen }, sc.partTitles[o] || sc.parts[o]?.title || o));
  sel.addEventListener('change', () => setSlot(slot, sel.value));

  const icons = h('span', { class: 'icons' });
  if (chosen) {
    const hidden = S.hiddenParts.has(chosen);
    icons.append(h('button', { title: hidden ? 'Show this part' : 'Hide this part in the 3D view', onclick: () => {
      if (hidden) S.hiddenParts.delete(chosen); else S.hiddenParts.add(chosen);
      emit('visibility');
      renderParts();
    } }, icon(hidden ? 'eyeOff' : 'eye', 15)));
  }
  if (slot.path !== '/' || chosen) {
    const more = h('button', { title: 'More: edit, new part, add slot' }, icon('more', 15));
    more.addEventListener('click', () => popupMenu(more, [
      chosen ? [canEdit ? 'Edit JBeam text…' : 'View JBeam text (read-only)…', () => partTextDialog(chosen)] : null,
      chosen ? ['Select its nodes', () => focusPart(chosen)] : null,
      slot.path !== '/' ? [chosen && !canEdit ? 'Make an editable copy…' : 'New part for this slot…', () => newPartDialog({ slot })] : null,
      chosen ? ['Add a slot to this part (for upfits)…', () => addSlotDialog(chosen), !canEdit] : null,
      chosen ? ['How firmly it is mounted…', () => mountDialog(chosen), !canEdit] : null,
    ].filter(Boolean)));
    icons.append(more);
  }

  const label = h('span', { class: 'label', title: `${slot.description || slot.id}\nslot: ${slot.id}\nClick to select this part's nodes` },
    slot.description || slot.id);
  label.addEventListener('click', () => { if (chosen) focusPart(chosen); });
  label.style.cursor = chosen ? 'pointer' : '';

  const row = h('div', { class: 'slotrow' + (chosen && chosen === S.activePart ? ' active' : '') +
      (q && slotMatches(slot, q) ? ' match' : '') + (S.hiddenParts.has(chosen) ? ' hiddenpart' : '') },
    h('span', { class: 'twisty', onclick: () => { if (open) expanded.delete(slot.path); else expanded.add(slot.path); renderParts(); } },
      kids.length ? (open ? '▾' : '▸') : ''),
    label, sel,
    chosen && !canEdit ? h('span', { class: 'lock', title: 'Read-only (from the game or a zip). Use ⋯ > Make an editable copy.' }, icon('file', 13)) : null,
    icons);

  const box = h('div', { class: 'slot' + (depth === 0 ? ' root' : '') }, row);
  if (open) for (const c of kids) { const e = slotNode(c, depth + 1, q); if (e) box.append(e); }
  return box;
}

function setSlot(slot, value) {
  S.config.parts = S.config.parts || {};
  S.config.parts[slot.id] = value;
  if (slot.path in S.config.parts) S.config.parts[slot.path] = value;
  A.rebuild();
}

// How firmly a part is held on: the beams of the part that reach nodes of other parts, made
// stiffer and harder to break (or weaker), as far as the nodes at their ends stay stable.
const HOLD = [[0.5, 'Weaker'], [1, 'As it is now'], [2, 'Stronger (×2)'], [5, 'Much stronger (×5)'], [20, 'Very strong (×20)'],
  [Infinity, 'Never breaks off']];
function mountDialog(part) {
  const sc = S.scene;
  const els = sc.beams.filter(b => !b.del && b.part === part && editable(b) && (sc.nodes[b.a].part !== part || sc.nodes[b.b].part !== part));
  if (!els.length) return toast('This part has no beams to the rest of the vehicle, so there is nothing to make stronger. It may share nodes with another part instead.');
  const med = a => a.slice().sort((x, y) => x - y)[a.length >> 1];
  const never = v => v === 'FLT_MAX' || !isFinite(num(v, Infinity));
  const k0 = med(els.map(b => num(b.k, 0))), c0 = med(els.map(b => num(b.c, 0)));
  const d0 = els.every(b => never(b.d)) ? Infinity : med(els.filter(b => !never(b.d)).map(b => num(b.d, 0)));
  const s0 = els.every(b => never(b.s)) ? Infinity : med(els.filter(b => !never(b.s)).map(b => num(b.s, 0)));
  // the stiffest spring the nodes at the ends take: each node's room under the stability target
  const count = new Map();
  for (const b of els) for (const i of [b.a, b.b]) count.set(i, (count.get(i) || 0) + 1);
  let lim = Infinity;
  for (const [i, c] of count) {
    const n = sc.nodes[i];
    lim = Math.min(lim, (n.w * (STIFF_TARGET / DT) ** 2 - (n.K || 0)) / c + k0);
  }
  lim = Math.max(k0, Math.floor(lim / 1000) * 1000);
  const kN = v => (isFinite(v) ? fmt(v / 1000, 1) + ' kN' : 'never');
  const holdSel = h('select', {}, ...HOLD.map(([f, t]) => h('option', { value: String(f), selected: f === 1 }, t)));
  const springIn = h('input', { type: 'number', step: '100000', value: k0 });
  const breakIn = h('input', { type: 'number', step: '1', placeholder: 'never', value: isFinite(s0) ? +(s0 / 1000).toFixed(1) : '' });
  for (const i of [springIn, breakIn]) i.addEventListener('keydown', e => e.stopPropagation());
  const note = h('p', { class: 'note' });
  const fill = () => {
    const f = +holdSel.value;
    const k = isFinite(f) ? Math.min(lim, Math.round(k0 * Math.max(1, Math.sqrt(f)))) : lim;
    springIn.value = f < 1 ? Math.round(k0 * f) : k;
    const s = isFinite(f) && isFinite(s0) ? s0 * f : Infinity;
    breakIn.value = isFinite(s) ? +(s / 1000).toFixed(1) : '';
    note.textContent = k < k0 * Math.sqrt(f) && isFinite(f) || (!isFinite(f) && lim < k0 * 4)
      ? 'The springs are as stiff as the nodes at their ends take and stay stable; it still holds by not breaking.' : '';
  };
  holdSel.addEventListener('change', fill);
  const apply = () => {
    const k = Math.max(1000, +springIn.value || k0);
    const s = breakIn.value === '' ? Infinity : Math.max(1, +breakIn.value) * 1000;
    const d = !isFinite(s) ? Infinity : Math.min(s, isFinite(d0) ? d0 * Math.max(1, s / (isFinite(s0) ? s0 : s)) : s);
    setOpts('beams', els, {
      beamSpring: k, beamDamp: Math.round(c0 * Math.sqrt(k / (k0 || k))),
      beamDeform: isFinite(d) ? Math.round(d) : 'FLT_MAX', beamStrength: isFinite(s) ? Math.round(s) : 'FLT_MAX',
    }, 'Mount strength of ' + part);
    closeModal();
    toast(`Changed the ${els.length} mounting beams. Save (Ctrl+S) to keep it.`, 'ok');
  };
  openModal('How firmly it is mounted', h('div', {},
    h('p', { class: 'note' }, `${sc.parts[part]?.title || part} is held on by ${els.length} beams to the rest of the vehicle. ` +
      `Now each has a spring of ${fmt(k0, 0)} N/m and breaks at ${kN(s0)}.`),
    h('div', { class: 'form' },
      h('span', { class: 'k' }, 'Make it'), holdSel,
      h('span', { class: 'k', title: 'Stiffness of each mounting beam in N/m' }, 'Spring'), springIn,
      h('span', { class: 'k', title: 'The pull in kilonewtons that breaks a mounting beam. Empty: never breaks.' }, 'Breaks at (kN)'), breakIn),
    note,
    h('div', { class: 'actions' }, h('button', { onclick: closeModal }, 'Cancel'), h('button', { class: 'primary', onclick: apply }, 'Change it'))));
}

export function focusPart(name) {
  const sc = S.scene;
  if (partEditable(name)) {
    S.activePart = name;
    emit('activePart');
  }
  clearSelection();
  sc.nodes.forEach((n, i) => { if (!n.del && n.part === name) S.sel.nodes.add(i); });
  emit('selection');
  const pts = [...S.sel.nodes].map(i => sc.nodes[i].p);
  if (pts.length) frame(pts);
  else toast(`${name} has no nodes of its own.`);
}

// =====================================================================================
// Tuning (variables)
// =====================================================================================
const rebuildSoon = debounce(() => A.rebuild(), 350);

export function renderTuning() {
  const box = $('#tuning');
  box.innerHTML = '';
  const sc = S.scene;
  if (!sc) return;
  const vars = sc.variables.filter(v => !v.hidden && typeof v.min === 'number' && typeof v.max === 'number');
  if (!vars.length) { box.append(h('p', { class: 'muted' }, 'The parts you picked have no tuning settings.')); return; }
  box.append(h('div', { class: 'btnrow' },
    h('button', { onclick: () => { S.config.vars = {}; A.rebuild(); } }, 'Reset all to default')));
  let cat = null;
  for (const v of vars) {
    const c = [v.category, v.subCategory].filter(Boolean).join(' › ') || 'Other';
    if (c !== cat) { cat = c; box.append(h('div', { class: 'vcat' }, c)); }
    const dmin = num(v.minDis, v.min), dmax = num(v.maxDis, v.max);
    const toDis = x => v.max === v.min ? dmin : dmin + (x - v.min) / (v.max - v.min) * (dmax - dmin);
    const fromDis = d => dmax === dmin ? v.min : v.min + (d - dmin) / (dmax - dmin) * (v.max - v.min);
    const cur = num(S.config.vars?.[v.name], num(v.val, v.default));
    const out = h('span', {}, fmt(toDis(cur), 2) + (v.unit ? ' ' + v.unit : ''));
    const range = h('input', { type: 'range', min: dmin, max: dmax, step: num(v.stepDis, (dmax - dmin) / 100) || 'any', value: toDis(cur) });
    range.addEventListener('input', () => { out.textContent = fmt(+range.value, 2) + (v.unit ? ' ' + v.unit : ''); });
    range.addEventListener('change', () => {
      S.config.vars = S.config.vars || {};
      S.config.vars[v.name] = +fromDis(+range.value).toPrecision(8);
      rebuildSoon();
    });
    box.append(h('div', { class: 'vrow', title: (v.description || '') + '\n' + v.name },
      h('div', { class: 'vt' }, h('span', {}, v.title || v.name), out), range));
  }
}

// =====================================================================================
// Problems
// =====================================================================================
export function renderProblems() {
  const box = $('#problems');
  box.innerHTML = '';
  const sc = S.scene, sm = S.summary;
  if (!sc) return;
  const unstable = [], close = [];
  sc.nodes.forEach((n, i) => { if (!n.del) { if (n.level === 2) unstable.push(i); else if (n.level === 1) close.push(i); } });
  box.append(h('div', { class: 'btnrow' },
    h('button', { onclick: () => A.check() }, 'Run a full check of my mod')));
  const add = (cls, text, btn) => box.append(h('div', { class: 'prob ' + cls }, text, btn ? h('div', {}, btn) : null));
  if (unstable.length) add('err', `${unstable.length} node(s) are too light for their beams and may shake or explode in game.`,
    h('button', { class: 'small', onclick: () => selectNodes(unstable) }, 'Select them'));
  if (close.length) add('wrn', `${close.length} node(s) are close to the stability limit.`,
    h('button', { class: 'small', onclick: () => selectNodes(close) }, 'Select them'));
  for (const e of sm?.errors || []) add('err', `${e.root}: ${e.file}: ${e.message}`);
  for (const d of sm?.duplicates || []) add('wrn', `Part "${d.part}" is in two files. The game uses ${d.kept} and ignores ${d.ignored}.`);
  for (const w of sc.warnings) add('wrn', w);
  if (box.children.length === 1) box.append(h('p', { class: 'good' }, 'No problems found.'));
}

// for the status bar: how many things need a look
export function problemCount() {
  const sc = S.scene, sm = S.summary;
  if (!sc) return { errors: 0, warnings: 0 };
  let unstable = 0;
  for (const n of sc.nodes) if (!n.del && n.level === 2) unstable++;
  return { errors: (sm?.errors?.length || 0) + unstable, warnings: (sm?.duplicates?.length || 0) + sc.warnings.length };
}

// =====================================================================================
// Vehicle summary (right side in Parts and Tuning)
// =====================================================================================
export function vehicleDims() {
  const sc = S.scene;
  if (!sc) return null;
  const lo = [Infinity, Infinity, Infinity], hi = [-Infinity, -Infinity, -Infinity];
  for (const n of sc.nodes) if (!n.del) for (let k = 0; k < 3; k++) { lo[k] = Math.min(lo[k], n.p[k]); hi[k] = Math.max(hi[k], n.p[k]); }
  const wheels = sc.wheels.map(w => {
    const a = sc.nodes[w.n1].p, b = sc.nodes[w.n2].p;
    const c = [(a[0] + b[0]) / 2, (a[1] + b[1]) / 2, (a[2] + b[2]) / 2];
    const o = sc.optsets[w.o] || {};
    const r = num(w.radius, num(o.radius, 0.3)) || 0.3;
    for (let k = 0; k < 3; k++) { lo[k] = Math.min(lo[k], c[k] - (k === 1 || k === 2 ? r : 0)); hi[k] = Math.max(hi[k], c[k] + (k === 1 || k === 2 ? r : 0)); }
    return { name: w.name, c, r, width: num(w.width, num(o.tireWidth, null)), psi: num(w.pressurePSI, num(o.pressurePSI, null)) };
  });
  const front = wheels.filter(w => w.c[1] < 0), rear = wheels.filter(w => w.c[1] >= 0);
  const avg = (list, f) => list.length ? list.reduce((s, w) => s + f(w), 0) / list.length : null;
  const track = list => list.length >= 2 ? Math.max(...list.map(w => w.c[0])) - Math.min(...list.map(w => w.c[0])) : null;
  const yF = avg(front, w => w.c[1]), yR = avg(rear, w => w.c[1]);
  // ground: the lowest tire contact; ride height is from there to the lowest body node
  const groundZ = wheels.length ? Math.min(...wheels.map(w => w.c[2] - w.r)) : lo[2];
  return {
    length: hi[1] - lo[1], width: hi[0] - lo[0], height: hi[2] - groundZ,
    wheelbase: yF !== null && yR !== null ? yR - yF : null,
    trackF: track(front), trackR: track(rear),
    tireF: avg(front, w => w.r * 2), tireR: avg(rear, w => w.r * 2),
    widthF: avg(front.filter(w => w.width), w => w.width), widthR: avg(rear.filter(w => w.width), w => w.width),
    groundZ, wheels,
  };
}

export function statTile(label, value, unit = '', extra = null) {
  return h('div', { class: 'stat' }, h('div', { class: 'k' }, label),
    h('div', { class: 'v' }, value, unit ? h('small', {}, ' ' + unit) : null), extra);
}

export function renderVehicleSide() {
  const box = $('#vehicleSide');
  if (!box) return;
  box.innerHTML = '';
  const sc = S.scene;
  if (!sc) return;
  const d = vehicleDims();
  const m = v => v === null || v === undefined ? '–' : fmt(v, 2);
  box.append(h('h3', {}, S.summary?.name || S.model),
    h('div', { class: 'stat-grid' },
      statTile('Mass', fmt(totalMass(), 0), 'kg'),
      statTile('Wheels', String(sc.wheels.length)),
      statTile('Length', m(d.length), 'm'), statTile('Width', m(d.width), 'm'),
      statTile('Height', m(d.height), 'm'), statTile('Wheelbase', m(d.wheelbase), 'm'),
      statTile('Track F / R', d.trackF ? `${fmt(d.trackF, 2)} / ${fmt(d.trackR ?? 0, 2)}` : '–', 'm'),
      statTile('Tires F / R', d.tireF ? `${fmt(d.tireF * 39.37, 1)}" / ${fmt((d.tireR ?? d.tireF) * 39.37, 1)}"` : '–', '')),
    h('p', { class: 'note' }, 'Sizes are measured from the nodes and wheels; tire size is the outside diameter.'));
  const active = Object.keys(sc.parts).length;
  box.append(h('div', { class: 'card' },
    h('b', {}, 'Parts on this vehicle'),
    h('p', { class: 'note' }, `${active} parts are in use. Parts from the game are read-only; changes are saved as your own copies in a mod folder.`),
    h('div', { class: 'btnrow' },
      h('button', { onclick: () => A.setMode('garage') }, icon('wrench', 15), 'Garage'),
      h('button', { onclick: () => A.setMode('structure') }, icon('nodes', 15), 'Structure'))));
}

export function selectNodes(list) {
  clearSelection();
  for (const i of list) S.sel.nodes.add(i);
  emit('selection');
  frameSelection();
}

// =====================================================================================
// Properties of the selection
// =====================================================================================
const NODE_MATERIALS = ['', '|NM_METAL', '|NM_PLASTIC', '|NM_RUBBER', '|NM_WOOD', '|NM_GLASS'];
const BEAM_TYPES = [['|NORMAL', 'Normal'], ['|BOUNDED', 'Bounded (spring/damper)'], ['|SUPPORT', 'Support (push only)'],
  ['|ANISOTROPIC', 'Anisotropic']];
const NODE_DEFAULTS = { nodeWeight: 25, collision: true, selfCollision: false, fixed: false, frictionCoef: 1 };
const BEAM_DEFAULTS = { beamSpring: 4300000, beamDamp: 580, beamDeform: 220000, beamStrength: 'FLT_MAX' };

function parseVal(s) {
  const t = s.trim();
  if (t === '') return null;
  if (t === 'true') return true;
  if (t === 'false') return false;
  if (/^-?(\d+\.?\d*|\.\d+)(e-?\d+)?$/i.test(t)) return parseFloat(t);
  if (/^[[{]/.test(t)) { try { return JSON.parse(t); } catch { /* keep as text */ } }
  return t;
}
function showVal(v) {
  if (v === undefined || v === null) return '';
  return typeof v === 'string' ? v : JSON.stringify(v);
}
function common(els, fn) {
  let first, set = false;
  for (const e of els) {
    const v = fn(e);
    if (!set) { first = v; set = true; } else if (JSON.stringify(v) !== JSON.stringify(first)) return { mixed: true };
  }
  return { value: first };
}

// nodes the user means: the selection plus mirror partners when mirror mode is on
function withMirror(idx) {
  const set = new Set(idx);
  if (S.mirror) for (const i of idx) { const j = mirrorPartner(i); if (j !== null) set.add(j); }
  return [...set];
}

function uniqueRows(els) {
  // quads are drawn as two triangles; edit each file row once
  const seen = new Set(), out = [];
  for (const e of els) {
    const k = e.part + '|' + e.sec + '|' + (e.rk || []).join('|');
    if (seen.has(k)) continue;
    seen.add(k); out.push(e);
  }
  return out;
}

export function setOpts(kind, els, opts, label) {
  const ops = [], inv = [];
  let skipped = 0;
  for (const el of kind === 'tris' ? uniqueRows(els) : els) {
    if (!editable(el)) { skipped++; continue; }
    const [o, i] = kind === 'nodes' ? opNodeSet(el, opts) : opRowSet(el, opts);
    ops.push(o); inv.unshift(i);
  }
  if (skipped) toast(`${skipped} selected item(s) are in read-only parts and were left alone.`);
  if (ops.length) commit(label, ops, inv);
}

function field(label, kind, els, key, type, { def, choices, help, digits = 4 } = {}) {
  const c = common(els, e => effOpts(e)[key] ?? def);
  const commitVal = v => setOpts(kind, els, { [key]: v }, `Set ${label.toLowerCase()}`);
  let input;
  if (type === 'bool') {
    input = h('input', { type: 'checkbox' });
    input.checked = !c.mixed && !!c.value;
    input.indeterminate = !!c.mixed;
    input.addEventListener('change', () => commitVal(input.checked));
  } else if (type === 'num') {
    input = numInput(c.mixed ? null : num(c.value, null), commitVal, { digits });
    if (c.mixed) input.placeholder = 'mixed';
  } else if (type === 'select') {
    input = h('select');
    const list = choices.slice();
    if (!c.mixed && c.value !== undefined && !list.some(x => (Array.isArray(x) ? x[0] : x) === c.value)) list.push(c.value);
    if (c.mixed) input.append(h('option', { value: '\u0000', selected: true }, 'mixed'));
    for (const x of list) {
      const [v, t] = Array.isArray(x) ? x : [x, x || '(none)'];
      input.append(h('option', { value: v, selected: !c.mixed && v === (c.value ?? '') }, t));
    }
    input.addEventListener('change', () => { if (input.value !== '\u0000') commitVal(input.value === '' ? null : input.value); });
  } else {
    const isGroup = key === 'group';
    const shown = isGroup && Array.isArray(c.value) ? c.value.join(', ') : showVal(c.value);
    input = textInput(c.mixed ? '' : shown, s => {
      if (!isGroup) return commitVal(parseVal(s));
      const g = s.split(',').map(x => x.trim()).filter(Boolean);
      commitVal(g.length > 1 ? g : (g[0] || ''));
    });
    if (c.mixed) input.placeholder = 'mixed';
  }
  return [h('span', { class: 'k', title: (help ? help + '\n' : '') + key }, label), h('span', { class: 'v' }, input)];
}

// every option on the rows, editable; inline overrides can be removed
function allOptions(kind, els, shown) {
  const keys = new Set();
  for (const e of els.slice(0, 400)) for (const k of Object.keys(effOpts(e))) if (!shown.includes(k)) keys.add(k);
  const grid = h('div', { class: 'props' });
  for (const k of [...keys].sort()) {
    const c = common(els, e => effOpts(e)[k]);
    const inl = els.some(e => e.io && k in e.io);
    const inp = textInput(c.mixed ? '' : showVal(c.value), s => setOpts(kind, els, { [k]: parseVal(s) }, `Set ${k}`));
    if (c.mixed) inp.placeholder = 'mixed';
    grid.append(h('span', { class: 'k', title: inl ? 'Set on this row' : 'Inherited from the part\'s settings above this row' },
      k + (inl ? ' •' : '')),
    h('span', { class: 'v', style: { display: 'flex', gap: '3px' } }, inp,
      inl ? h('button', { class: 'small', title: 'Remove the setting from this row (go back to the inherited value)',
        onclick: () => setOpts(kind, els, { [k]: null }, `Remove ${k}`) }, '×') : null));
  }
  const kIn = h('input', { type: 'text', placeholder: 'option name' });
  const vIn = h('input', { type: 'text', placeholder: 'value' });
  for (const i of [kIn, vIn]) i.addEventListener('keydown', e => e.stopPropagation());
  const add = () => {
    const k = kIn.value.trim();
    if (!k) return;
    setOpts(kind, els, { [k]: parseVal(vIn.value) }, `Set ${k}`);
  };
  grid.append(kIn, h('span', { style: { display: 'flex', gap: '3px' } }, vIn, h('button', { class: 'small', onclick: add }, 'Add')));
  return h('details', { class: 'box' }, h('summary', {}, `All options (${keys.size})`),
    h('p', { class: 'note' }, '• = set on the row itself. Others come from the part\'s settings rows above.'), grid);
}

function stabilityText(n) {
  const col = ['good', 'warn', 'bad'][n.level];
  const word = ['Stable', 'Close to the limit', 'Unstable: may shake or explode'][n.level];
  return h('span', { class: col, title: `Stiffness ${n.stiff.toFixed(2)} (yellow ${STIFF_YELLOW}, red ${STIFF_RED})\nDamping ${n.damp.toFixed(2)} (yellow ${DAMP_YELLOW}, red ${DAMP_RED})` },
    h('span', { class: 'stab', style: { background: `var(--${col})` } }), word);
}

export function renderProps() {
  const box = $('#props');
  box.innerHTML = '';
  const sc = S.scene;
  if (!sc) return;
  const nodes = [...S.sel.nodes].filter(i => sc.nodes[i] && !sc.nodes[i].del);
  const beams = [...S.sel.beams].filter(i => sc.beams[i] && !sc.beams[i].del);
  const tris = [...S.sel.tris].filter(i => sc.tris[i] && !sc.tris[i].del);
  if (!nodes.length && !beams.length && !tris.length) return renderPartInfo(box);
  if (nodes.length) box.append(nodeProps(nodes));
  if (beams.length) box.append(beamProps(beams));
  if (tris.length) box.append(triProps(tris));
  box.append(h('div', { class: 'btnrow' },
    h('button', { onclick: frameSelection, title: 'F' }, 'Zoom to selection'),
    h('button', { class: 'danger', onclick: deleteSelection, title: 'Delete' }, 'Delete')));
}

function renderPartInfo(box) {
  const sc = S.scene, name = S.activePart;
  box.append(h('h3', {}, 'Nothing selected'));
  box.append(h('p', { class: 'note' }, 'Click a node, beam or collision triangle in the 3D view, or a slot name on the left to select a whole part.'));
  if (sc.wheels.length) box.append(wheelList());
  if (!name || !sc.parts[name]) return;
  const p = sc.parts[name], f = sc.files[p.f];
  const cnt = { n: 0, b: 0, t: 0 };
  for (const n of sc.nodes) if (!n.del && n.part === name) cnt.n++;
  for (const b of sc.beams) if (!b.del && b.part === name) cnt.b++;
  for (const t of sc.tris) if (!t.del && t.part === name) cnt.t++;
  box.append(h('h4', {}, 'Part I\'m editing'),
    h('div', { class: 'props' },
      h('span', { class: 'k' }, 'Name'), h('span', { class: 'v', title: name }, p.title || name),
      h('span', { class: 'k' }, 'Part id'), h('span', { class: 'v', title: name }, name),
      h('span', { class: 'k' }, 'Slot type'), h('span', { class: 'v' }, String(p.slotType)),
      h('span', { class: 'k' }, 'File'), h('span', { class: 'v', title: f?.path }, f ? `${f.root}: ${f.rel}` : '?'),
      h('span', { class: 'k' }, 'Contents'), h('span', { class: 'v' }, `${cnt.n} nodes, ${cnt.b} beams, ${cnt.t} triangles`),
      h('span', { class: 'k' }, 'Mass'), h('span', { class: 'v' }, fmt(partMass(name), 1) + ' kg')),
    h('div', { class: 'btnrow' },
      h('button', { onclick: () => focusPart(name) }, 'Select its nodes'),
      h('button', { onclick: () => partTextDialog(name) }, 'Edit JBeam text…'),
      h('button', { onclick: () => addSlotDialog(name) }, 'Add slot…')));
}

// the wheels on the vehicle; their size and pressure live in the wheel and tire parts
function wheelList() {
  const sc = S.scene;
  const rows = sc.wheels.map(w => {
    const o = S.scene.optsets[w.o] || {};
    const r = num(w.radius, num(o.radius, null)), wd = num(w.width, num(o.tireWidth, null)), psi = num(w.pressurePSI, num(o.pressurePSI, null));
    return h('div', { class: 'prob', style: { display: 'flex', alignItems: 'center', gap: '6px' } },
      h('b', { style: { minWidth: '44px' } }, w.name),
      h('span', { class: 'grow', title: w.part }, [r !== null ? `⌀ ${fmt(r * 2, 3)} m` : null, wd !== null ? `${fmt(wd, 3)} m wide` : null,
        psi !== null ? `${fmt(psi, 1)} psi` : null].filter(Boolean).join(' · ') || w.sec),
      h('button', { class: 'small', title: `Edit ${w.part}`, onclick: () => partTextDialog(w.part) }, '✎'));
  });
  return h('details', { class: 'box' }, h('summary', {}, `Wheels (${sc.wheels.length})`),
    h('p', { class: 'note' }, 'Swap wheels, tires and suspension in the Parts list (Wheels, Tires, Springs, Shocks slots). ✎ opens the part that sets a wheel\'s size; tire pressure is also on the Tuning tab.'),
    ...rows);
}

function nodeProps(idx) {
  const sc = S.scene;
  const els = withMirror(idx).map(i => sc.nodes[i]);
  const one = idx.length === 1 ? sc.nodes[idx[0]] : null;
  const wrap = h('div');
  if (one) {
    wrap.append(h('h3', {}, 'Node ', h('span', { style: { color: 'var(--accent)' } }, one.id)));
    const posIn = [0, 1, 2].map(k => numInput(one.p[k], v => {
      const p = one.p.slice(); p[k] = v;
      if (!editable(one)) return toast('This node is in a read-only part.', 'err');
      const ops = [], inv = [];
      const [o, i] = opNodeMove(one, toRaw(one.xf, p));
      ops.push(o); inv.push(i);
      const j = S.mirror ? mirrorPartner(idx[0]) : null;
      if (j !== null && editable(sc.nodes[j])) {
        const q = sc.nodes[j].p.slice(); q[k] = k === 0 ? -v : v;
        const [o2, i2] = opNodeMove(sc.nodes[j], toRaw(sc.nodes[j].xf, q));
        ops.push(o2); inv.push(i2);
      }
      commit('Move node ' + one.id, ops, inv);
    }, { digits: 4 }));
    wrap.append(h('div', { class: 'props' },
      h('span', { class: 'k' }, 'Part'), h('span', { class: 'v', title: one.part }, sc.parts[one.part]?.title || one.part),
      h('span', { class: 'k', title: 'X = left/right (negative is the right side), Y = front/back (negative is the front), Z = up' }, 'Position X Y Z'),
      h('span', { class: 'v xyz' }, ...posIn),
      h('span', { class: 'k' }, 'Beams'), h('span', { class: 'v' }, String(sc.beamsOf[idx[0]].length)),
      h('span', { class: 'k' }, 'Stability'), h('span', { class: 'v' }, stabilityText(one))));
    if (one.level > 0) {
      const need = Math.ceil(minStableWeight(one) * 100) / 100;
      wrap.append(h('div', { class: 'note' }, `Needs at least ${need} kg for its beams. `,
        h('button', { class: 'small', onclick: () => setOpts('nodes', els, { nodeWeight: need }, 'Make node stable') }, `Use ${need} kg`)));
    }
  } else {
    const w = els.reduce((s, n) => s + n.w, 0);
    const bad = els.filter(n => n.level === 2).length;
    wrap.append(h('h3', {}, `${idx.length} nodes`), h('div', { class: 'props' },
      h('span', { class: 'k' }, 'Total weight'), h('span', { class: 'v' }, fmt(w, 2) + ' kg'),
      h('span', { class: 'k' }, 'Unstable'), h('span', { class: 'v ' + (bad ? 'bad' : 'good') }, String(bad))));
  }
  if (S.mirror && els.length > idx.length) wrap.append(h('p', { class: 'note' }, `Mirror is on: changes also go to ${els.length - idx.length} mirrored node(s).`));
  wrap.append(h('h4', {}, 'Settings'), h('div', { class: 'props' },
    ...field('Weight (kg)', 'nodes', els, 'nodeWeight', 'num', { def: 25, digits: 3, help: 'Mass of this node' }),
    ...field('Collides', 'nodes', els, 'collision', 'bool', { def: true, help: 'Hits the ground and other vehicles' }),
    ...field('Self collision', 'nodes', els, 'selfCollision', 'bool', { def: false, help: 'Hits this vehicle\'s own collision triangles' }),
    ...field('Fixed in place', 'nodes', els, 'fixed', 'bool', { def: false, help: 'Pinned in the world (for test rigs)' }),
    ...field('Friction', 'nodes', els, 'frictionCoef', 'num', { def: 1, help: 'Grip against the ground' }),
    ...field('Material', 'nodes', els, 'nodeMaterial', 'select', { choices: NODE_MATERIALS, help: 'Sound and sparks when scraping' }),
    ...field('Group', 'nodes', els, 'group', 'text', { help: 'Flexbody group(s): the 3D mesh in this group bends with these nodes. Comma list = ["a","b"]' })));
  if (idx.length > 1) {
    const d = [0, 0, 0];
    const dIn = [0, 1, 2].map(k => numInput(0, v => { d[k] = v; }, { digits: 4 }));
    wrap.append(h('h4', {}, 'Move, stretch and line up'),
      h('div', { class: 'props' }, h('span', { class: 'k' }, 'Move by X Y Z'), h('span', { class: 'v xyz' }, ...dIn)),
      h('div', { class: 'btnrow' },
        h('button', { onclick: () => moveNodes(idx, n => [n.p[0] + d[0], n.p[1] + d[1], n.p[2] + d[2]], 'Move nodes') }, 'Move'),
        ...['X', 'Y', 'Z'].map((a, k) => h('button', { title: `Give every selected node the same ${a} (the average)`,
          onclick: () => { const avg = idx.reduce((s, i) => s + sc.nodes[i].p[k], 0) / idx.length; moveNodes(idx, n => { const p = n.p.slice(); p[k] = avg; return p; }, 'Line up ' + a); } }, 'Line up ' + a))),
      stretchRow(idx),
      h('div', { class: 'btnrow' },
        h('button', { title: 'B: join the selected nodes in the order you picked them', onclick: () => connectSelected(false) }, 'Connect in a chain'),
        h('button', { title: 'Shift+B: join every selected node to every other one (a braced, rigid block)', onclick: () => connectSelected(true) }, 'Connect all (brace)')));
  }
  if (els.length <= 400) wrap.append(allOptions('nodes', els, ['nodeWeight', 'collision', 'selfCollision', 'fixed', 'frictionCoef', 'nodeMaterial', 'group']));
  return wrap;
}

// make the selection wider, longer or taller by a factor (the 3D model follows when "Model follows" is on)
function stretchRow(idx) {
  const sc = S.scene;
  const f = [1, 1, 1];
  const fIn = [0, 1, 2].map(k => numInput(1, v => { f[k] = v > 0 ? v : 1; }, { digits: 3 }));
  const around = h('select', {}, h('option', { value: 'sel' }, 'the middle of the selection'),
    h('option', { value: 'axis' }, 'the middle of the vehicle (left-right)'));
  const go = () => {
    const pts = idx.map(i => sc.nodes[i].p);
    const c = [0, 1, 2].map(k => (Math.min(...pts.map(p => p[k])) + Math.max(...pts.map(p => p[k]))) / 2);
    if (around.value === 'axis') c[0] = 0;
    if (f.every(v => v === 1)) return toast('Type how many times bigger, like 1.2 for 20% wider.');
    moveNodes(idx, n => n.p.map((v, k) => c[k] + (v - c[k]) * f[k]), 'Stretch nodes');
  };
  return h('div', {},
    h('div', { class: 'props' },
      h('span', { class: 'k', title: 'Width (X), length (Y) and height (Z): 1.2 is 20% bigger, 0.9 is 10% smaller' }, 'Stretch × X Y Z'),
      h('span', { class: 'v xyz' }, ...fIn),
      h('span', { class: 'k' }, 'Around'), around),
    h('div', { class: 'btnrow' }, h('button', { onclick: go }, 'Stretch')));
}

function moveNodes(idx, fn, label) {
  const sc = S.scene;
  const ops = [], inv = [];
  for (const i of idx) {
    const n = sc.nodes[i];
    if (!editable(n)) continue;
    const [o, iv] = opNodeMove(n, toRaw(n.xf, fn(n)));
    ops.push(o); inv.push(iv);
  }
  if (!ops.length) return toast('No editable nodes selected.', 'err');
  commit(label, ops, inv);
}

function beamProps(idx) {
  const sc = S.scene;
  const els = idx.map(i => sc.beams[i]);
  const one = els.length === 1 ? els[0] : null;
  const wrap = h('div');
  if (one) {
    const a = sc.nodes[one.a], b = sc.nodes[one.b];
    const len = Math.hypot(a.p[0] - b.p[0], a.p[1] - b.p[1], a.p[2] - b.p[2]);
    wrap.append(h('h3', {}, 'Beam ', h('span', { style: { color: 'var(--accent)' } }, `${a.id} – ${b.id}`)),
      h('div', { class: 'props' },
        h('span', { class: 'k' }, 'Part'), h('span', { class: 'v', title: one.part }, sc.parts[one.part]?.title || one.part),
        h('span', { class: 'k' }, 'Length'), h('span', { class: 'v' }, fmt(len, 3) + ' m'),
        h('span', { class: 'k' }, 'Kind'), h('span', { class: 'v', title: BEAM_TYPE_INFO[one.t] }, one.sec === 'hydros' ? 'hydro' : (BEAM_TYPE_INFO[one.t] || one.t).split(' (')[0])));
  } else {
    wrap.append(h('h3', {}, `${els.length} beams`));
  }
  const grid = h('div', { class: 'props' },
    ...field('Spring', 'beams', els, 'beamSpring', 'num', { def: BEAM_DEFAULTS.beamSpring, digits: 0, help: 'Stiffness in N/m. Frames ~1e7, body panels ~1e6' }),
    ...field('Damping', 'beams', els, 'beamDamp', 'num', { def: BEAM_DEFAULTS.beamDamp, digits: 2, help: 'Stops wobbling. Too high makes nodes unstable' }),
    ...field('Bends at', 'beams', els, 'beamDeform', 'text', { def: BEAM_DEFAULTS.beamDeform, help: 'Force (N) at which it bends for good. FLT_MAX = never' }),
    ...field('Breaks at', 'beams', els, 'beamStrength', 'text', { def: BEAM_DEFAULTS.beamStrength, help: 'Force (N) at which it snaps. FLT_MAX = never' }));
  if (!els.some(e => e.sec === 'hydros')) grid.append(...field('Type', 'beams', els, 'beamType', 'select', { def: '|NORMAL', choices: BEAM_TYPES }));
  const types = new Set(els.map(e => e.t));
  if (types.has('BOUNDED')) grid.append(
    ...field('Short bound', 'beams', els, 'beamShortBound', 'num', { help: 'How far it may shorten (fraction of length)' }),
    ...field('Long bound', 'beams', els, 'beamLongBound', 'num', { help: 'How far it may lengthen (fraction of length)' }),
    ...field('Limit spring', 'beams', els, 'beamLimitSpring', 'num', { digits: 0, help: 'Bump stop stiffness' }),
    ...field('Limit damp', 'beams', els, 'beamLimitDamp', 'num', { help: 'Bump stop damping' }),
    ...field('Precompression', 'beams', els, 'beamPrecompression', 'num', { help: 'Length multiplier at spawn (ride height for springs)' }),
    ...field('Rebound damp', 'beams', els, 'beamDampRebound', 'num', { help: 'Damping when extending' }));
  if (types.has('SUPPORT')) grid.append(...field('Long bound', 'beams', els, 'beamLongBound', 'num', { help: 'Stretch allowed before it stops acting' }));
  if (els.some(e => e.sec === 'hydros')) grid.append(
    ...field('Input', 'beams', els, 'inputSource', 'text', { help: 'e.g. steering' }),
    ...field('In factor', 'beams', els, 'inFactor', 'num'), ...field('Out factor', 'beams', els, 'outFactor', 'num'));
  wrap.append(h('h4', {}, 'Settings'), grid);
  wrap.append(h('div', { class: 'btnrow' },
    h('button', { title: 'Copy the spring, damping and strength most beams around these use', onclick: () => matchNeighbours(els) }, 'Match nearby beams')));
  if (els.length <= 400) wrap.append(allOptions('beams', els, ['beamSpring', 'beamDamp', 'beamDeform', 'beamStrength', 'beamType',
    'beamShortBound', 'beamLongBound', 'beamLimitSpring', 'beamLimitDamp', 'beamPrecompression', 'beamDampRebound', 'inputSource', 'inFactor', 'outFactor']));
  return wrap;
}

function matchNeighbours(els) {
  const sc = S.scene;
  const mine = new Set(els);
  const near = [];
  for (const b of els) for (const i of [b.a, b.b]) for (const k of sc.beamsOf[i]) {
    const x = sc.beams[k];
    if (!mine.has(x) && x.t === 'NORMAL' && x.sec === 'beams') near.push(x);
  }
  if (!near.length) return toast('No other beams touch these nodes.');
  const med = key => { const v = near.map(x => x[key]).filter(x => typeof x === 'number' && isFinite(x)).sort((a, b) => a - b); return v.length ? v[v.length >> 1] : undefined; };
  const opts = { beamSpring: med('k'), beamDamp: med('c'), beamDeform: med('d') };
  const s = near.map(x => x.s).filter(x => typeof x === 'number').sort((a, b) => a - b);
  opts.beamStrength = s.length ? s[s.length >> 1] : 'FLT_MAX';
  for (const k of Object.keys(opts)) if (opts[k] === undefined) delete opts[k];
  setOpts('beams', els, opts, 'Match nearby beams');
}

function triProps(idx) {
  const sc = S.scene;
  const els = idx.map(i => sc.tris[i]);
  const rows = uniqueRows(els);
  const wrap = h('div');
  const one = rows.length === 1 ? rows[0] : null;
  wrap.append(h('h3', {}, one ? 'Collision triangle' : `${rows.length} collision triangles`));
  if (one) wrap.append(h('div', { class: 'props' },
    h('span', { class: 'k' }, 'Nodes'), h('span', { class: 'v' }, one.rk.slice(0, -1).join(', ')),
    h('span', { class: 'k' }, 'Part'), h('span', { class: 'v', title: one.part }, sc.parts[one.part]?.title || one.part)));
  const c = common(rows, t => t.nc);
  const chk = h('input', { type: 'checkbox' });
  chk.checked = !c.mixed && !c.value;
  chk.indeterminate = !!c.mixed;
  chk.addEventListener('change', () => setOpts('tris', rows, { triangleType: chk.checked ? '|NORMALTYPE' : '|NONCOLLIDABLE' },
    chk.checked ? 'Make triangles collide' : 'Make triangles non-colliding'));
  wrap.append(h('div', { class: 'props' },
    h('span', { class: 'k', title: 'Other vehicles and this vehicle\'s nodes bump into it' }, 'Collides'), h('span', { class: 'v' }, chk),
    ...field('Drag', 'tris', rows, 'dragCoef', 'num', { help: 'Air drag of this face (percent)' }),
    ...field('Lift', 'tris', rows, 'liftCoef', 'num', { help: 'Aerodynamic lift of this face' })),
    h('p', { class: 'note' }, 'Green side = outside. If red faces outward, flip it.'),
    h('div', { class: 'btnrow' }, h('button', { onclick: flipSelectedTris, title: 'I' }, 'Flip (I)')));
  if (rows.length <= 400) wrap.append(allOptions('tris', rows, ['triangleType', 'dragCoef', 'liftCoef']));
  return wrap;
}

// =====================================================================================
// Weights and auto-weight
// =====================================================================================
const AW = { scope: 'part', mode: 'fix', total: null, minW: 0.5, preview: null };

export function renderWeights() {
  const box = $('#weights');
  box.innerHTML = '';
  const sc = S.scene;
  if (!sc) return;
  let red = 0, yellow = 0;
  for (const n of sc.nodes) if (!n.del) { if (n.level === 2) red++; else if (n.level === 1) yellow++; }
  const unstable = [];
  sc.nodes.forEach((n, i) => { if (!n.del && n.level === 2) unstable.push(i); });
  box.append(h('h4', {}, 'Weight and stability'), h('div', { class: 'props' },
    h('span', { class: 'k' }, 'Vehicle'), h('span', { class: 'v', title: 'Sum of node weights (wheels\' generated nodes not counted)' },
      fmt(totalMass(), 0) + ' kg'),
    S.activePart ? h('span', { class: 'k', title: S.activePart }, 'This part') : null,
    S.activePart ? h('span', { class: 'v' }, fmt(partMass(S.activePart), 1) + ' kg') : null,
    h('span', { class: 'k' }, 'Stability'),
    h('span', { class: 'v' },
      h('span', { class: red ? 'bad' : 'good' }, h('span', { class: 'stab', style: { background: 'var(--bad)' } }), `${red} unstable `),
      h('span', { class: 'warn' }, h('span', { class: 'stab', style: { background: 'var(--warn)' } }), `${yellow} close`))),
  red ? h('div', { class: 'btnrow' }, h('button', { onclick: () => selectNodes(unstable) }, 'Select unstable nodes')) : '');

  // auto-weight
  const radio = (name, value, label, small) => {
    const r = h('input', { type: 'radio', name, value, checked: AW[name] === value });
    r.addEventListener('change', () => { AW[name] = value; AW.preview = null; renderWeights(); });
    return h('label', {}, r, h('span', {}, label, small ? h('small', {}, small) : null));
  };
  const totalIn = numInput(AW.total, v => { AW.total = v; AW.preview = null; renderWeights(); }, { digits: 2 });
  totalIn.placeholder = 'kg';
  const minIn = numInput(AW.minW, v => { AW.minW = Math.max(0.01, v); AW.preview = null; renderWeights(); }, { digits: 3, width: '70px' });
  const aw = h('div', { class: 'box' }, h('b', {}, 'Auto-weight'),
    h('p', { class: 'note' }, 'Sets node weights so the game runs them smoothly: nodes held by many stiff beams need more weight.'),
    h('div', { class: 'choice' },
      radio('scope', 'sel', 'Selected nodes'),
      radio('scope', 'part', 'The part I\'m editing'),
      radio('scope', 'all', 'Every part I can edit')),
    h('div', { class: 'choice', style: { marginTop: '6px' } },
      radio('mode', 'fix', 'Fix unstable nodes only', 'Adds just enough weight where needed'),
      radio('mode', 'spread', 'Spread a total mass', 'Stiff spots get more, keeping all stable')),
    AW.mode === 'spread' ? h('div', { class: 'props', style: { marginTop: '6px' } },
      h('span', { class: 'k' }, 'Total mass (kg)'), totalIn) : null,
    h('div', { class: 'props', style: { marginTop: '6px' } }, h('span', { class: 'k', title: 'No node gets less than this' }, 'Lightest node (kg)'), minIn),
    h('div', { class: 'btnrow' },
      h('button', { onclick: () => { AW.preview = autoWeight(); renderWeights(); } }, 'Preview'),
      AW.preview && AW.preview.changes?.length ? h('button', { class: 'primary', onclick: applyAutoWeight }, 'Apply') : null));
  if (AW.preview) {
    const p = AW.preview;
    aw.append(h('p', { class: p.error ? 'note err' : 'note' }, p.error || p.text));
  }
  box.append(aw);
}

function scopeNodes() {
  const sc = S.scene;
  let list = [];
  if (AW.scope === 'sel') list = [...S.sel.nodes];
  else sc.nodes.forEach((n, i) => { if (AW.scope === 'all' || n.part === S.activePart) list.push(i); });
  return list.filter(i => !sc.nodes[i].del && editable(sc.nodes[i]));
}

function autoWeight() {
  const sc = S.scene;
  const list = scopeNodes();
  if (!list.length) return { error: AW.scope === 'sel' ? 'Select some nodes in a part you can edit first.' : 'No editable nodes in that choice.' };
  const minW = AW.minW || 0.5;
  const floor = i => Math.max(minW, minStableWeight(sc.nodes[i]) * 1.02);
  const before = list.reduce((s, i) => s + sc.nodes[i].w, 0);
  const target = new Map();
  if (AW.mode === 'fix') {
    for (const i of list) {
      const n = sc.nodes[i];
      if (n.level > 0 || n.w < minW) target.set(i, Math.ceil(Math.max(n.w, floor(i)) * 100) / 100);
    }
  } else {
    const M = AW.total;
    if (!(M > 0)) return { error: 'Type the total mass you want first.' };
    const need = list.reduce((s, i) => s + floor(i), 0);
    if (need > M) return { error: `These ${list.length} nodes need at least ${fmt(need, 1)} kg to be stable with their beams. Raise the total or soften the beams.` };
    // share in proportion to beam stiffness: equal stiffness-to-weight everywhere
    const fixed = new Map();
    for (let it = 0; it < 30; it++) {
      const free = list.filter(i => !fixed.has(i));
      const rest = M - [...fixed.values()].reduce((s, v) => s + v, 0);
      const sumK = free.reduce((s, i) => s + sc.nodes[i].K + 1, 0);
      let changed = false;
      for (const i of free) {
        const w = rest * (sc.nodes[i].K + 1) / sumK;
        if (w < floor(i)) { fixed.set(i, floor(i)); changed = true; }
      }
      if (!changed) {
        for (const i of free) target.set(i, rest * (sc.nodes[i].K + 1) / sumK);
        break;
      }
    }
    for (const [i, w] of fixed) target.set(i, w);
    for (const [i, w] of target) target.set(i, Math.round(w * 100) / 100);
  }
  const changes = [...target].filter(([i, w]) => Math.abs(w - sc.nodes[i].w) > 0.005);
  const after = before + changes.reduce((s, [i, w]) => s + w - sc.nodes[i].w, 0);
  const sign = after >= before ? '+' : '';
  return { changes, text: changes.length ?
    `Changes ${changes.length} of ${list.length} nodes: ${fmt(before, 1)} kg → ${fmt(after, 1)} kg (${sign}${fmt(after - before, 1)} kg).` :
    'Nothing to change: those nodes are already fine.' };
}

function applyAutoWeight() {
  const sc = S.scene;
  const p = AW.preview;
  if (!p?.changes?.length) return;
  const ops = [], inv = [];
  for (const [i, w] of p.changes) {
    const [o, iv] = opNodeSet(sc.nodes[i], { nodeWeight: w });
    ops.push(o); inv.unshift(iv);
  }
  AW.preview = null;
  commit(`Auto-weight ${ops.length} nodes`, ops, inv);
  toast(`Weights set on ${ops.length} nodes. Press Save to write them.`, 'ok');
}
