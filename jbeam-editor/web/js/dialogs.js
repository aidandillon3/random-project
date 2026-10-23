// Dialogs: part text editor, new part wizard, add slot, settings, help,
// save config, export and check.
import { S, A, emit } from './state.js';
import { api } from './api.js';
import { DT, partFileKey, partEditable } from './scene.js';
import { V } from './view3d.js';
import { $, h, closeModal, openModal, setLoading, shortPath, toast, fmt } from './util.js';

// ---- shared bits ------------------------------------------------------------------
export function allSlots() {
  const out = [];
  const walk = (s, depth) => {
    if (s.path !== '/') out.push({ slot: s, depth });
    for (const c of s.children || []) walk(c, depth + 1);
  };
  walk(S.scene.tree, 0);
  return out;
}

export function slotSelect(current, { preferEmpty = false } = {}) {
  const sel = h('select');
  const list = allSlots();
  let pick = current;
  if (!pick && preferEmpty) {
    const empty = list.filter(x => !x.slot.chosen);
    const by = re => empty.find(x => re.test(x.slot.description || x.slot.id));
    pick = (by(/upfit/i) || by(/modification|\bmod\b|_mod$/i) || by(/accessor|extra|cargo|bed|rack/i) || empty[0] || list[0])?.slot;
  }
  list.forEach((x, k) => {
    const s = x.slot;
    const title = s.chosen ? (S.scene.partTitles[s.chosen] || s.chosen) : 'empty';
    sel.append(h('option', { value: k, selected: s === pick }, '\u00a0\u00a0'.repeat(Math.max(0, x.depth - 1)) + `${s.description || s.id}  (${title})`));
  });
  sel.getSlot = () => list[+sel.value]?.slot;
  return sel;
}

// a mod-folder picker that can also make a new mod folder
export function rootPicker() {
  const roots = S.summary?.editableRoots || [];
  const sel = h('select');
  for (const r of roots) sel.append(h('option', { value: r.id, title: r.path, selected: r.id === S.lastRoot }, r.label));
  sel.append(h('option', { value: '__new' }, '＋ New mod folder…'));
  const name = h('input', { type: 'text', placeholder: 'my_' + (S.model || 'vehicle') + '_parts', class: roots.length ? 'hidden' : '' });
  name.addEventListener('keydown', e => e.stopPropagation());
  sel.addEventListener('change', () => name.classList.toggle('hidden', sel.value !== '__new'));
  const el = h('span', { style: { display: 'flex', gap: '6px' } }, sel, name);
  el.getRoot = async () => {
    if (sel.value !== '__new') return (S.lastRoot = sel.value);
    const n = name.value.trim() || name.placeholder;
    const r = await api.newMod(S.model, n);
    S.summary = r.summary;
    toast(`Made mod folder ${shortPath(r.path)}`, 'ok');
    return (S.lastRoot = r.root);
  };
  return el;
}

function uniquePartName(base) {
  const taken = n => S.scene.partTitles[n] !== undefined || S.scene.parts[n] !== undefined;
  base = base.replace(/[^A-Za-z0-9_\-.]/g, '_');
  if (!taken(base)) return base;
  for (let k = 2; ; k++) if (!taken(base + k)) return base + k;
}

// a node-name prefix nobody uses yet
export function freePrefix(seed) {
  const sc = S.scene;
  const letters = (seed || 'x').replace(/[^a-z]/gi, '').toLowerCase() || 'x';
  const cands = [];
  for (let n = 2; n <= Math.min(5, letters.length); n++) cands.push(letters.slice(0, n));
  cands.push('je', 'jx', 'jz');
  const used = p => sc.nodes.some(n => n.id.startsWith(p) && /^\d/.test(n.id.slice(p.length)));
  for (const c of cands) if (!used(c)) return c;
  for (let k = 0; ; k++) if (!used('j' + k + 'n')) return 'j' + k + 'n';
}

export function info(title) {
  return { authors: S.appConfig.author || 'JBeam Editor', name: title, value: 500 };
}

// ---- part text ---------------------------------------------------------------------------
export async function partTextDialog(part) {
  try {
    await A.save({ quiet: true });
    const r = await api.partText(S.model, part);
    const ta = h('textarea', { class: 'code', spellcheck: 'false' }, r.text);
    ta.readOnly = !r.editable;
    ta.addEventListener('keydown', e => {
      e.stopPropagation();
      if (e.key === 'Tab') { e.preventDefault(); ta.setRangeText('    ', ta.selectionStart, ta.selectionEnd, 'end'); }
      if (e.key === 's' && (e.ctrlKey || e.metaKey)) { e.preventDefault(); saveIt(); }
    });
    const err = h('div', { class: 'err' });
    const saveIt = async () => {
      err.textContent = '';
      const ok = await A.saveOps([{ op: 'part.replace', f: r.file.key, part, text: ta.value }], 'Edited ' + part);
      if (ok === true) closeModal(); else if (ok) err.textContent = ok;
    };
    const body = h('div', {},
      h('p', { class: 'note' }, `${r.file.root}: ${r.file.rel}, line ${r.line}. `,
        r.editable ? 'Changes are checked before they are written; a backup of the file is kept.' :
          'This part is read-only (from the game or a zip). Make an editable copy to change it.'),
      ta, err,
      h('div', { class: 'actions' },
        h('button', { onclick: () => api.reveal(r.file.path).catch(e => toast(e.message, 'err')) }, 'Show file'),
        !r.editable ? h('button', { class: 'primary', onclick: () => { closeModal(); newPartDialog({ copyFrom: part }); } }, 'Make an editable copy…') : null,
        h('button', { onclick: closeModal }, r.editable ? 'Cancel' : 'Close'),
        r.editable ? h('button', { class: 'primary', onclick: saveIt }, 'Save') : null));
    openModal(`Part ${part}`, body, { wide: true });
    setTimeout(() => ta.focus(), 50);
  } catch (e) { toast(e.message, 'err'); }
}

// ---- new part wizard ------------------------------------------------------------------------
export function newPartDialog({ slot = null, copyFrom = null } = {}) {
  const sc = S.scene;
  if (copyFrom && !slot) slot = allSlots().map(x => x.slot).find(s => s.chosen === copyFrom) || null;
  const slotSel = slotSelect(slot, { preferEmpty: !slot });
  const selected = [...S.sel.nodes].filter(i => !sc.nodes[i].del);
  const sourceOf = () => copyFrom || slotSel.getSlot()?.chosen || '';

  const nameIn = h('input', { type: 'text' });
  const titleIn = h('input', { type: 'text' });
  const suggest = () => {
    const s = slotSel.getSlot();
    const base = sourceOf() || (s?.allowTypes?.[0] || s?.id || 'part');
    nameIn.value = uniquePartName(base + '_custom');
    const t = sc.partTitles[sourceOf()] || s?.description || 'Part';
    titleIn.value = t + ' (custom)';
  };
  suggest();
  for (const i of [nameIn, titleIn]) i.addEventListener('keydown', e => e.stopPropagation());

  const mode = h('div', { class: 'choice' });
  const modeOpt = (v, label, small, checked) => {
    const r = h('input', { type: 'radio', name: 'npmode', value: v, checked });
    r.addEventListener('change', updateMode);
    return h('label', {}, r, h('span', {}, label, h('small', {}, small)));
  };
  mode.append(
    modeOpt('copy', 'Copy the current part', 'Start from the part in this slot and change it', !!sourceOf()),
    modeOpt('box', 'A braced box', 'A ready-made, stable block of 8 nodes with collision. Good start for boxes, bumpers, racks', !sourceOf()),
    modeOpt('empty', 'Empty part', 'No nodes yet; add them with the Node tool'));
  const boxOpts = h('div', { class: 'form', style: { marginTop: '8px' } });
  const dims = [0.6, 0.6, 0.4].map(v => h('input', { type: 'number', step: '0.05', value: v, style: { width: '70px' } }));
  const massIn = h('input', { type: 'number', step: '1', value: 20, style: { width: '90px' } });
  const attach = h('input', { type: 'checkbox', checked: selected.length > 0, disabled: !selected.length });
  boxOpts.append(
    h('span', { class: 'k' }, 'Size W × L × H (m)'), h('span', { style: { display: 'flex', gap: '4px' } }, ...dims),
    h('span', { class: 'k' }, 'Mass (kg)'), massIn,
    h('span', { class: 'k' }, 'Attach'), h('label', { class: 'chk' }, attach,
      selected.length ? `to the ${selected.length} selected node(s)` : 'select nodes first to attach the box to them'));
  function updateMode() {
    const m = mode.querySelector('input:checked')?.value;
    boxOpts.classList.toggle('hidden', m !== 'box');
  }
  slotSel.addEventListener('change', () => {
    suggest();
    const copy = mode.querySelector('input[value=copy]');
    copy.disabled = !sourceOf();
    if (copy.disabled && copy.checked) mode.querySelector('input[value=box]').checked = true;
    updateMode();
  });
  mode.querySelector('input[value=copy]').disabled = !sourceOf();
  updateMode();

  const root = rootPicker();
  const putOn = h('input', { type: 'checkbox', checked: true });
  const err = h('div', { class: 'err' });
  const go = async () => {
    err.textContent = '';
    const s = slotSel.getSlot();
    const m = mode.querySelector('input:checked')?.value;
    const name = nameIn.value.trim();
    if (!s) return (err.textContent = 'Pick a slot.');
    if (!/^[A-Za-z0-9_\-.]+$/.test(name)) return (err.textContent = 'Part ids may only use letters, digits, _ - and .');
    const slotType = s.allowTypes?.[0] || s.id;
    try {
      setLoading(true, 'Making the part…');
      await A.save({ quiet: true });
      const rootId = await root.getRoot();
      const body = { model: S.model, root: rootId, name, title: titleIn.value.trim() || name, slotType };
      if (m === 'copy') Object.assign(body, { mode: 'copy', source: sourceOf() });
      else {
        body.mode = 'data';
        body.data = m === 'box'
          ? boxPart(name, body.title, slotType, dims.map(d => +d.value || 0.5), +massIn.value || 20, attach.checked ? selected : [])
          : emptyPart(name, body.title, slotType);
      }
      const r = await api.newPart(body);
      S.summary = r.summary;
      if (putOn.checked) { S.config.parts[s.id] = name; S.activePart = name; S.pinActive = true; }
      closeModal();
      await A.reloadModel();
      toast(`Made part ${name} in ${r.written.map(shortPath).join(', ')}`, 'ok', 7000);
    } catch (e) {
      err.textContent = e.message;
    } finally { setLoading(false); }
  };
  openModal('New part', h('div', {},
    h('div', { class: 'form' },
      h('span', { class: 'k' }, 'Slot'), slotSel,
      h('span', { class: 'k' }, 'Part id'), nameIn,
      h('span', { class: 'k' }, 'Name in the parts menu'), titleIn,
      h('span', { class: 'k' }, 'Save into mod'), root),
    h('h4', {}, 'Start from'), mode, boxOpts,
    h('label', { class: 'chk', style: { marginTop: '10px' } }, putOn, 'Put it on the vehicle now and make it the part I\'m editing'),
    h('p', { class: 'note' }, 'Tip: to add an upfit where there is no free slot, add a slot to a part first (⋯ > Add a slot).'),
    err,
    h('div', { class: 'actions' }, h('button', { onclick: closeModal }, 'Cancel'), h('button', { class: 'primary', onclick: go }, 'Make part'))));
}

function emptyPart(name, title, slotType) {
  return {
    information: info(title), slotType,
    nodes: [['id', 'posX', 'posY', 'posZ'], { nodeWeight: 2 }, { collision: true }, { selfCollision: true }, { frictionCoef: 0.7 },
      { nodeMaterial: '|NM_METAL' }, { group: name }],
    beams: [['id1:', 'id2:'], { beamType: '|NORMAL', beamSpring: 1500000, beamDamp: 100, beamDeform: 40000, beamStrength: 'FLT_MAX' }],
    triangles: [['id1:', 'id2:', 'id3:'], { groundModel: 'metal' }],
  };
}

// centre for new things: the selected nodes or the point the camera orbits
export function placeCentre() {
  const sc = S.scene;
  const sel = [...S.sel.nodes].filter(i => !sc.nodes[i].del);
  if (sel.length) {
    const c = [0, 0, 0];
    for (const i of sel) for (let k = 0; k < 3; k++) c[k] += sc.nodes[i].p[k] / sel.length;
    return c;
  }
  const t = V.controls.target;
  return [t.x, t.y, t.z];
}

function boxPart(name, title, slotType, [w, l, ht], mass, anchors) {
  const sc = S.scene;
  const c = placeCentre();
  if (anchors.length) c[2] += ht / 2 + 0.05;   // sit on top of what it attaches to
  const p = freePrefix(name);
  const nodes = [], ids = [];
  // corners: l = +X (driver's left), r = -X; 1/2 front/back bottom, 3/4 front/back top
  const corner = (k, side) => {
    const x = c[0] + (side === 'l' ? w / 2 : -w / 2);
    const y = c[1] + (k % 2 === 1 ? -l / 2 : l / 2);
    const z = c[2] + (k <= 2 ? -ht / 2 : ht / 2);
    const id = p + k + side;
    ids.push(id);
    nodes.push([id, +x.toFixed(4), +y.toFixed(4), +z.toFixed(4)]);
    return [x, y, z];
  };
  const pos = {};
  for (const k of [1, 2, 3, 4]) for (const s of ['r', 'l']) pos[p + k + s] = corner(k, s);
  const spring = 1500000, damp = 100;
  const beams = [];
  for (let a = 0; a < ids.length; a++) for (let b = a + 1; b < ids.length; b++) beams.push([ids[a], ids[b]]);
  const attachBeams = [];
  if (anchors.length) {
    const seen = new Set();
    const add = (cid, nid) => { const k = cid + '|' + nid; if (!seen.has(k)) { seen.add(k); attachBeams.push([nid, cid]); } };
    const d2 = (a, b) => (a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2 + (a[2] - b[2]) ** 2;
    for (const i of anchors) {
      const n = sc.nodes[i];
      ids.slice().sort((x, y) => d2(pos[x], n.p) - d2(pos[y], n.p)).slice(0, 3).forEach(cid => add(cid, n.id));
    }
    for (const cid of ids.slice(0, 4)) {   // bottom corners
      const best = anchors.slice().sort((x, y) => d2(sc.nodes[x].p, pos[cid]) - d2(sc.nodes[y].p, pos[cid]))[0];
      add(cid, sc.nodes[best].id);
    }
  }
  // outward-facing collision triangles (counter-clockwise seen from outside)
  const q = (a, b, cc, d) => [[a, b, cc], [cc, d, a]];
  const P = s => p + s;
  const faces = [
    ...q(P('1r'), P('1l'), P('3l'), P('3r')),   // front (-Y)
    ...q(P('2l'), P('2r'), P('4r'), P('4l')),   // back (+Y)
    ...q(P('1l'), P('2l'), P('4l'), P('3l')),   // left (+X)
    ...q(P('2r'), P('1r'), P('3r'), P('4r')),   // right (-X)
    ...q(P('3r'), P('3l'), P('4l'), P('4r')),   // top
    ...q(P('2r'), P('2l'), P('1l'), P('1r')),   // bottom
  ];
  // weights: share the mass, then make sure each corner is heavy enough for its beams
  const K = {};
  for (const id of ids) K[id] = 0;
  for (const [a, b] of beams) { K[a] += spring; K[b] += spring; }
  for (const [, b] of attachBeams) K[b] += spring;
  const per = mass / 8;
  const minW = Math.max(...ids.map(id => Math.max(K[id] * DT * DT / 4, (7 * damp) * DT / 2.4)));
  const wt = +Math.max(per, minW * 1.05).toFixed(2);
  return {
    information: info(title), slotType,
    nodes: [['id', 'posX', 'posY', 'posZ'], { nodeWeight: wt }, { collision: true }, { selfCollision: true }, { frictionCoef: 0.7 },
      { nodeMaterial: '|NM_METAL' }, { group: name }, ...nodes, { group: '' }],
    beams: [['id1:', 'id2:'], { beamType: '|NORMAL', beamSpring: spring, beamDamp: damp, beamDeform: 60000, beamStrength: 'FLT_MAX' },
      ...beams, ...attachBeams],
    triangles: [['id1:', 'id2:', 'id3:'], { groundModel: 'metal' }, ...faces],
  };
}

// ---- add slot ----------------------------------------------------------------------------------
export function addSlotDialog(part) {
  if (!partEditable(part)) return toast('That part is read-only. Make an editable copy first (⋯ next to its slot).', 'err');
  const typeIn = h('input', { type: 'text', value: part + '_upfit' });
  const descIn = h('input', { type: 'text', value: 'Upfit' });
  for (const i of [typeIn, descIn]) i.addEventListener('keydown', e => e.stopPropagation());
  const err = h('div', { class: 'err' });
  const go = async () => {
    const type = typeIn.value.trim();
    if (!/^[A-Za-z0-9_\-.]+$/.test(type)) return (err.textContent = 'Slot names may only use letters, digits, _ - and .');
    const ok = await A.saveOps([{ op: 'slot.add', f: partFileKey(part), part, type, description: descIn.value.trim() || type, default: '' }],
      'Add slot ' + type);
    if (ok === true) { closeModal(); toast(`Added slot "${descIn.value}". Use ⋯ > New part on it to make a part for it.`, 'ok'); }
    else if (ok) err.textContent = ok;
  };
  openModal('Add a slot', h('div', {},
    h('p', { class: 'note' }, `Adds a slot to ${S.scene.parts[part]?.title || part}. Parts whose slotType is the slot name can then be picked in it, like in the game's parts menu.`),
    h('div', { class: 'form' },
      h('span', { class: 'k' }, 'Slot name (type)'), typeIn,
      h('span', { class: 'k' }, 'Shown as'), descIn),
    err,
    h('div', { class: 'actions' }, h('button', { onclick: closeModal }, 'Cancel'), h('button', { class: 'primary', onclick: go }, 'Add slot'))));
}

// ---- settings -----------------------------------------------------------------------------------
export function settingsDialog(first = false) {
  const c = S.appConfig;
  const user = h('input', { type: 'text', value: c.user_folder || '', placeholder: 'C:\\Users\\you\\AppData\\Local\\BeamNG\\BeamNG.drive\\current' });
  const game = h('input', { type: 'text', value: c.game_folder || '', placeholder: 'C:\\Program Files (x86)\\Steam\\steamapps\\common\\BeamNG.drive' });
  const extra = h('textarea', { rows: 3, placeholder: 'One folder per line (each holding a vehicles folder)' }, (c.extra_folders || []).join('\n'));
  const author = h('input', { type: 'text', value: c.author || '', placeholder: 'Your name (written into new parts)' });
  for (const i of [user, game, extra, author]) i.addEventListener('keydown', e => e.stopPropagation());
  const err = h('div', { class: 'err' });
  const save = async () => {
    try {
      setLoading(true, 'Looking for vehicles…');
      const st = await api.config({ user_folder: user.value, game_folder: game.value, author: author.value,
        extra_folders: extra.value.split('\n') });
      closeModal();
      await A.loadState(st);
    } catch (e) { err.textContent = e.message; } finally { setLoading(false); }
  };
  openModal('Settings', h('div', {},
    first ? h('p', {}, 'Tell the editor where BeamNG keeps your mods and where the game is installed. It found what it could.') : null,
    h('div', { class: 'form' },
      h('span', { class: 'k' }, 'BeamNG user folder'), user,
      h('span', { class: 'k' }, 'Game folder (read-only)'), game,
      h('span', { class: 'k' }, 'Extra folders'), extra,
      h('span', { class: 'k' }, 'Author name'), author),
    h('p', { class: 'note' }, 'Your editable mods live in the user folder under mods\\unpacked. The game folder is only read, never changed. Backups of every file the editor changes go to ', h('code', {}, (S.dataDir || 'data') + '\\backups'), '.'),
    err,
    h('div', { class: 'actions' }, h('button', { onclick: closeModal }, 'Cancel'), h('button', { class: 'primary', onclick: save }, 'Save'))));
}

// ---- save config ------------------------------------------------------------------------------------
export function saveConfigDialog() {
  const name = h('input', { type: 'text', value: (S.configName && !S.configName.startsWith('(')) ? S.configName : 'my_config' });
  name.addEventListener('keydown', e => e.stopPropagation());
  const root = rootPicker();
  const err = h('div', { class: 'err' });
  const go = async () => {
    try {
      const rootId = await root.getRoot();
      const r = await api.pcSave(S.model, rootId, name.value.trim(), S.config);
      S.summary = r.summary;
      S.configName = name.value.trim();
      emit('configs');
      closeModal();
      toast('Saved ' + shortPath(r.path), 'ok', 7000);
    } catch (e) { err.textContent = e.message; }
  };
  openModal('Save config', h('div', {},
    h('p', { class: 'note' }, 'Saves the parts and tuning you picked as a .pc config. It shows up in the game\'s vehicle selector.'),
    h('div', { class: 'form' }, h('span', { class: 'k' }, 'Config name'), name, h('span', { class: 'k' }, 'Save into mod'), root),
    err,
    h('div', { class: 'actions' }, h('button', { onclick: closeModal }, 'Cancel'), h('button', { class: 'primary', onclick: go }, 'Save config'))));
}

// ---- export ------------------------------------------------------------------------------------------
export function exportDialog() {
  const roots = S.summary?.editableRoots || [];
  if (!roots.length) return toast('This vehicle has no mod folder of yours to export yet.', 'err');
  const sel = h('select', {}, ...roots.map(r => h('option', { value: r.id }, r.label)));
  const name = h('input', { type: 'text', value: roots[0].label });
  name.addEventListener('keydown', e => e.stopPropagation());
  sel.addEventListener('change', () => { name.value = roots.find(r => r.id === sel.value)?.label || name.value; });
  const out = h('div');
  const go = async () => {
    try {
      await A.save({ quiet: true });
      setLoading(true, 'Packing…');
      const r = await api.exportMod(sel.value, name.value.trim());
      out.innerHTML = '';
      out.append(h('p', { class: 'good' }, `Packed ${r.files} files into ${r.path}`),
        h('button', { onclick: () => api.reveal(r.path) }, 'Show in Explorer'));
    } catch (e) { out.textContent = e.message; out.className = 'err'; } finally { setLoading(false); }
  };
  openModal('Export mod', h('div', {},
    h('p', { class: 'note' }, 'Your unpacked mod already works in the game as it is. Export packs it into a zip to share or keep. ' +
      'Don\'t put the zip in your own mods folder while the unpacked copy is there too, or the game loads both.'),
    h('div', { class: 'form' }, h('span', { class: 'k' }, 'Mod folder'), sel, h('span', { class: 'k' }, 'Zip name'), name),
    out,
    h('div', { class: 'actions' }, h('button', { onclick: closeModal }, 'Close'), h('button', { class: 'primary', onclick: go }, 'Export'))));
}

// ---- check ----------------------------------------------------------------------------------------------
export async function checkDialog() {
  try {
    await A.save({ quiet: true });
    setLoading(true, 'Checking…');
    const r = await api.check(S.model);
    const list = h('div');
    for (const e of r.errors) list.append(h('div', { class: 'prob err' }, e));
    for (const w of r.warnings) list.append(h('div', { class: 'prob wrn' }, w));
    if (!r.errors.length && !r.warnings.length) list.append(h('p', { class: 'good' }, 'No problems found.'));
    openModal(`Check: ${r.root}`, h('div', {},
      h('p', { class: 'note' }, `Checked ${r.parts} parts: ${r.errors.length} errors, ${r.warnings.length} warnings.`), list,
      h('div', { class: 'actions' }, h('button', { onclick: closeModal }, 'Close'))), { wide: true });
  } catch (e) { toast(e.message, 'err'); } finally { setLoading(false); }
}

// ---- help ----------------------------------------------------------------------------------------------------
export function helpDialog() {
  openModal('How to use the JBeam Editor', h('div', { class: 'help', html: `
<h3>Getting around</h3>
<p><kbd>Right-drag</kbd> orbit, <kbd>Middle-drag</kbd> pan, <kbd>Wheel</kbd> zoom (toward the mouse). <kbd>Alt</kbd>+<kbd>Left-drag</kbd> also orbits.
<kbd>F</kbd> zooms to what's selected, <kbd>Home</kbd> shows everything, <kbd>1</kbd> front, <kbd>3</kbd> side, <kbd>7</kbd> top.</p>
<h3>Picking parts</h3>
<p>The Parts tab works like the game's parts menu: pick a part for each slot and the vehicle rebuilds. Click a slot's name to select that part's nodes.
👁 hides a part. ⋯ opens the part's menu: edit its JBeam text, make a new part for the slot (a copy, a braced box, or empty),
or add a slot to it for upfits. Parts from the game or a zip are read-only (🔒); "Make an editable copy" puts your own copy in a mod folder.</p>
<h3>Editing</h3>
<ul>
<li><kbd>Q</kbd> Select: click, <kbd>Shift</kbd>+click adds, <kbd>Ctrl</kbd>+click toggles, drag a box. Drag the arrows to move nodes.</li>
<li><kbd>N</kbd> Node: click to place a node. It is joined to the nodes you had selected, so you can click out a chain.</li>
<li><kbd>B</kbd> Beam: click two nodes. With 2+ nodes selected, <kbd>B</kbd> joins them in order and <kbd>Shift</kbd>+<kbd>B</kbd> braces them all together.</li>
<li><kbd>T</kbd> Triangle: click three nodes for a collision triangle (green side faces out; <kbd>I</kbd> flips).</li>
<li><kbd>M</kbd> Mirror: left and right sides change together (names ending in l / r, X flipped).</li>
<li><kbd>Delete</kbd> removes the selection (beams and triangles on deleted nodes go too).</li>
<li><kbd>Ctrl</kbd>+<kbd>Z</kbd> / <kbd>Ctrl</kbd>+<kbd>Y</kbd> undo and redo, <kbd>Ctrl</kbd>+<kbd>S</kbd> saves into the .jbeam files.</li>
</ul>
<p>"Adding to" is the part new nodes, beams and triangles go into. Clicking a node switches it to that node's part unless "Stay" is ticked.</p>
<h3>Weights and stability</h3>
<p>BeamNG simulates 2000 steps a second. A node that is too light for the stiff beams holding it shakes or explodes. The editor works this out for every node:
green is fine, yellow is close, red is unstable (View ▸ Colour nodes by stability). Auto-weight on the right fixes it: either just the unstable nodes,
or spread a total mass so stiff spots get more weight.</p>
<h3>3D models</h3>
<p>Import 3D model loads an .obj, .glb/.gltf, .stl or .dae. Place it, then "Make part" writes the mesh into your mod, builds a braced node cage around it
with collision, sets stable weights and attaches it to the vehicle. The game bends the mesh with the cage.</p>
<h3>Saving</h3>
<p>Save writes only what you changed into the files and keeps their layout. Every file is checked before it is written and a backup is kept.
Save config writes a .pc config with your parts and tuning. Export mod packs your mod folder into a zip.</p>
<p>In the game, press <kbd>Ctrl</kbd>+<kbd>R</kbd> to reload the vehicle after saving.</p>` }), { wide: true });
}

export function fmtMass(v) { return fmt(v, 1) + ' kg'; }
