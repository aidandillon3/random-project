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
  const c = S.appConfig || {};
  const st = S.appState || {};
  const user = h('input', { type: 'text', value: c.user_folder || '', placeholder: 'C:\\Users\\you\\AppData\\Local\\BeamNG\\BeamNG.drive\\current' });
  const game = h('input', { type: 'text', value: c.game_folder || '', placeholder: 'C:\\Program Files (x86)\\Steam\\steamapps\\common\\BeamNG.drive' });
  const extra = h('textarea', { rows: 3, placeholder: 'One folder per line (each holding a vehicles folder)' }, (c.extra_folders || []).join('\n'));
  const author = h('input', { type: 'text', value: c.author || '', placeholder: 'Your name (written into new parts)' });
  const sm = h('input', { type: 'text', value: c.sm_folder || '', placeholder: 'Found automatically if Scrap Mechanic is installed with Steam' });
  const smUser = h('input', { type: 'text', value: c.sm_user_folder || '', placeholder: 'Found automatically (AppData\\Roaming\\Axolot Games\\Scrap Mechanic\\User)' });
  for (const i of [user, game, extra, author, sm, smUser]) i.addEventListener('keydown', e => e.stopPropagation());
  const win = st.window || {};
  const winSel = h('select', {},
    h('option', { value: 'auto' }, 'Automatic (best available)'),
    h('option', { value: 'native', disabled: !win.native }, 'Native window' + (win.native ? '' : ' (not installed)')),
    h('option', { value: 'app', disabled: !win.app }, 'App window (Edge/Chrome, no tabs)' + (win.app ? '' : ' (not found)')),
    h('option', { value: 'browser' }, 'A tab in my web browser'));
  winSel.value = c.window || 'auto';
  const winNote = h('span', { class: 'note' }, win.mode ? `Right now it is open as: ${({ native: 'a native window', app: 'an app window', browser: 'a browser tab' })[win.mode] || win.mode}.` : '');
  const installOut = h('div', { class: 'note' });
  const installBtn = h('button', { class: 'small', disabled: win.native, onclick: async () => {
    installBtn.disabled = true;
    installOut.textContent = 'Installing pywebview with pip… this can take a minute.';
    try {
      await api.installNative(true);
      for (let t = 0; t < 600; t++) {
        await new Promise(r => setTimeout(r, 1500));
        const r = await api.installNative(false);
        if (r.state === 'done') { installOut.textContent = 'Installed. Restart the editor to use the native window.'; break; }
        if (r.state === 'failed') { installOut.textContent = 'It did not install: ' + r.log.slice(-400); installBtn.disabled = false; break; }
      }
    } catch (e) { installOut.textContent = e.message; installBtn.disabled = false; }
  } }, win.native ? 'Native window installed' : 'Install native window');
  const shortcutBtn = h('button', { class: 'small', disabled: st.platform && !st.platform.startsWith('win'), onclick: async () => {
    try {
      const r = await api.shortcut();
      toast('Made shortcuts: ' + r.made.map(shortPath).join(', '), 'ok', 8000);
    } catch (e) { toast(e.message, 'err', 8000); }
  } }, 'Make desktop and Start menu shortcuts');
  const err = h('div', { class: 'err' });
  const save = async () => {
    try {
      setLoading(true, 'Looking for vehicles…');
      const st2 = await api.config({ user_folder: user.value, game_folder: game.value, author: author.value,
        extra_folders: extra.value.split('\n'), window: winSel.value, sm_folder: sm.value, sm_user_folder: smUser.value });
      closeModal();
      await A.loadState(st2);
    } catch (e) { err.textContent = e.message; } finally { setLoading(false); }
  };
  const sect = t => h('h4', { style: { margin: '18px 0 8px', color: 'var(--muted)', fontSize: '11px', textTransform: 'uppercase', letterSpacing: '.06em' } }, t);
  openModal('Settings', h('div', {},
    first ? h('p', {}, 'Tell the editor where BeamNG keeps your mods and where the game is installed. It filled in what it could find.') : null,
    sect('BeamNG.drive'),
    h('div', { class: 'form' },
      h('span', { class: 'k' }, 'BeamNG user folder'), user,
      h('span', { class: 'k' }, 'Game folder (read-only)'), game,
      h('span', { class: 'k' }, 'Extra folders'), extra,
      h('span', { class: 'k' }, 'Author name'), author),
    h('p', { class: 'note' }, 'Your editable mods live in the user folder under mods\\unpacked. The game folder is only read, never changed. Backups of every file the editor changes go to ', h('code', {}, (S.dataDir || 'data') + '\\backups'), '.'),
    sect('Scrap Mechanic'),
    h('div', { class: 'form' }, h('span', { class: 'k' }, 'Game folder'), sm, h('span', { class: 'k' }, 'Creations folder'), smUser),
    sect('Window'),
    h('div', { class: 'form' },
      h('span', { class: 'k' }, 'Show the editor in'), h('span', {}, winSel, ' ', winNote),
      h('span', { class: 'k' }, 'Shortcuts'), h('span', {}, shortcutBtn),
      h('span', { class: 'k' }, 'Native window'), h('span', {}, installBtn, installOut),
      h('span', { class: 'k' }, 'Log'), h('span', {}, h('button', { class: 'small', onclick: showLog }, 'Show the editor\'s log'))),
    err,
    h('div', { class: 'actions' }, h('button', { onclick: closeModal }, 'Cancel'), h('button', { class: 'primary', onclick: save }, 'Save'))));
}

async function showLog() {
  try {
    const r = await api.log();
    openModal('Editor log', h('div', {},
      h('p', { class: 'note' }, r.path),
      h('pre', { class: 'code', style: { maxHeight: '60vh', overflow: 'auto', whiteSpace: 'pre-wrap' } }, r.text || '(empty: the editor writes a log only when it runs without a console)')), { wide: true });
  } catch (e) { toast(e.message, 'err'); }
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
<h3>The modes</h3>
<ul>
<li><b>Garage</b>: the easy way to change a vehicle. Lift or lower it, pick bigger wheels and tires, widen the track,
add engine power, paint it. Everything shows in 3D straight away; <b>Save my version</b> writes it as your own config of the car
(the original stays as it was).</li>
<li><b>Parts</b>: pick a part for every slot, like the game's parts menu.</li>
<li><b>Tuning</b>: the sliders the vehicle's parts offer (springs, gearing, tire pressure and so on).</li>
<li><b>Structure</b>: the physics skeleton for detailed work: nodes, beams, collision triangles and weights.</li>
<li><b>Model</b>: model a part (bumper, rack, light bar…) in the editor and fit it to the vehicle.</li>
</ul>
<h3>Making vehicles</h3>
<ul>
<li><b>Vehicle Builder</b> (home screen > <i>Build a new vehicle</i>): pick a body style and shape it with sliders. The body, suspension,
wheels, engine and gearbox are generated and work in the game. <b>Create vehicle</b> writes it as a mod; reopen it from the Garage with
<i>Edit in the Builder</i> to change it later.</li>
<li><b>Scrap Mechanic import</b> (home screen > <i>Import from Scrap Mechanic</i>): pick a creation saved with the lift. Wheels on bearings
become real wheels on suspension; the bearings your driver's seat is connected to steer, the ones your engines are connected to are driven.
If the preview faces the wrong way, set <i>Front of the vehicle</i> to <i>Turned around</i>.</li>
<li><b>Drop test</b> (Garage, Builder and import): drops the vehicle onto the ground in a quick physics test and tells you whether it sits
at the height it was built for and holds together.</li>
</ul>
<h3>Getting around the 3D view</h3>
<p><kbd>Right-drag</kbd> orbit, <kbd>Middle-drag</kbd> pan, <kbd>Wheel</kbd> zoom (toward the mouse). <kbd>Alt</kbd>+<kbd>Left-drag</kbd> also orbits.
<kbd>Home</kbd> shows everything, <kbd>1</kbd> front, <kbd>3</kbd> side, <kbd>7</kbd> top, <kbd>F</kbd> zooms to the selection.
The layers button at the top right picks what is shown (model, nodes, beams…).</p>
<h3>Structure editing</h3>
<ul>
<li><kbd>Q</kbd> Select: click, <kbd>Shift</kbd>+click adds, <kbd>Ctrl</kbd>+click toggles, drag a box. Then drag the handles:
<kbd>W</kbd> move, <kbd>R</kbd> stretch (wider, longer, taller), <kbd>E</kbd> turn. With <b>Model follows</b> on, the 3D model
bends and stretches with the nodes and is saved that way. To reshape a whole part, use ⋯ &gt; Select its nodes in Parts first.</li>
<li><kbd>N</kbd> Node: click to place a node. It is joined to the nodes you had selected, so you can click out a chain.</li>
<li><kbd>B</kbd> Beam: click two nodes. With 2+ nodes selected, <kbd>B</kbd> joins them in order and <kbd>Shift</kbd>+<kbd>B</kbd> braces them all together.</li>
<li><kbd>T</kbd> Triangle: click three nodes for a collision triangle (green side faces out; <kbd>I</kbd> flips).</li>
<li><kbd>M</kbd> Mirror: left and right sides change together. <kbd>Delete</kbd> removes the selection.</li>
<li><kbd>Ctrl</kbd>+<kbd>Z</kbd> / <kbd>Ctrl</kbd>+<kbd>Y</kbd> undo and redo, <kbd>Ctrl</kbd>+<kbd>S</kbd> saves.</li>
</ul>
<p>Game parts are read-only: in Parts, ⋯ next to a slot > <i>Make an editable copy</i> puts your own copy in a mod folder.</p>
<h3>Weights and stability</h3>
<p>BeamNG simulates 2000 steps a second. A node that is too light for the stiff beams holding it shakes or explodes. The editor checks every node:
green is fine, yellow is close, red is unstable. Auto-weight fixes it, or spreads a total mass so stiff spots get more weight.</p>
<h3>Saving and the game</h3>
<p>Your changes go into mods\\unpacked in your BeamNG user folder, so the game loads them as a mod. Only what you changed is written, the files keep their layout,
and a backup is kept. In the game press <kbd>Ctrl</kbd>+<kbd>R</kbd> to reload the vehicle after saving.</p>` }), { wide: true });
}

export function fmtMass(v) { return fmt(v, 1) + ' kg'; }
