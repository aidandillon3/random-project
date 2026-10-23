// Starts the editor: the home screen, opening vehicles, the modes (Garage, Parts,
// Tuning, Structure, Model, Builder), saving, undo and the keyboard.
import { S, A, on, emit, clearSelection } from './state.js';
import { api } from './api.js';
import { partEditable, reindex, totalMass } from './scene.js';
import { V, buildMeshes, frame, initView, rebuild, redraw, refreshColors, setGround, setMeshOpacity, setMeshes,
  setNodeSize, setVehicleOffset, setVisible, updateGizmo, updateMeshVisibility, viewPreset } from './view3d.js';
import { connectSelected, deleteSelection, escape, flipSelectedTris, frameSelection, initTools, makeTriangle, setTool,
  updateHint } from './tools.js';
import { compactOps, redo, undo } from './edit.js';
import { renderParts, renderProblems, renderProps, renderTuning, renderWeights, renderVehicleSide, problemCount } from './panels.js';
import { checkDialog, exportDialog, helpDialog, saveConfigDialog, settingsDialog } from './dialogs.js';
import { clearImport, importActive, initImporter, showImportPrompt, startImport } from './importer.js';
import { hideHome, homeOpen, initHome, remember, renderGallery, showHome } from './home.js';
import { hydrateIcons, icon } from './icons.js';
import { closePanel as closeDropTest, runDropTest } from './droptest.js';
import { $, $$, closeModal, h, modalOpen, openModal, popupMenu, setLoading, toast, fmt, debounce } from './util.js';

const store = {
  get(k) { try { return localStorage.getItem('jbe.' + k); } catch { return null; } },
  set(k, v) { try { localStorage.setItem('jbe.' + k, v); } catch { /* private window */ } },
};

// modes that only look at the vehicle (no editing tools in the 3D view)
const LOOK_MODES = new Set(['garage', 'parts', 'tuning', 'model', 'builder']);
// mode modules register here: { enter(), leave(), render() }
export const MODES = {};

// ---- loading vehicles and configs ------------------------------------------------------
async function loadState(st) {
  st = st || await api.state();
  S.appState = st;
  S.appConfig = st.config;
  S.dataDir = st.dataDir;
  S.vehicles = st.vehicles;
  renderGallery();
  if (!S.model) showHome();
  if (!st.userFound || !st.gameFound) {
    if (!st.vehicles.length) setTimeout(() => settingsDialog(true), 300);
  }
}

async function openVehicle(model, { intent = null, mode = null, configName } = {}) {
  const want = mode || ({ garage: 'garage', model: 'model', import: 'structure', structure: 'structure' }[intent]) ||
    (S.mode && S.mode !== 'builder' ? S.mode : 'garage');
  if (S.model === model && S.scene && configName === undefined) {
    hideHome();
    setMode(want);
    if (intent === 'import') showImportPrompt();
    return;
  }
  if (S.model && S.pending.length && !(await save({ quiet: true }))) return;
  MODES.builder?.leave?.();
  clearImport();
  Object.assign(S, { model, scene: null, undo: [], redo: [], pending: [], activePart: null, pinActive: false, mode: null,
    garageSpec: null });
  emit('vehicleChanged', model);
  S.hiddenParts.clear();
  clearSelection();
  $('#optPin').checked = false;
  for (const g of V.meshCache.values()) g.dispose();
  V.meshCache.clear();
  store.set('model', model);
  remember(model);
  hideHome();
  showWorkspace(true);
  setLoading(true, 'Reading ' + model + '…');
  try {
    S.summary = await api.open(model);
    const names = S.summary.configs.map(c => c.name);
    const last = store.get('config.' + model);
    S.configName = configName !== undefined ? configName :
      last !== null && (last === '' || names.includes(last)) ? last : (S.summary.defaultConfig || '');
    fillConfigs();
    $('#vehName').textContent = S.summary.name || model;
    setMode(want, { quiet: true });
    await buildScene({ configName: S.configName, fresh: true });
  } catch (e) {
    toast(e.message, 'err', 9000);
  } finally { setLoading(false); }
  updateBanner();
  if (intent === 'import') showImportPrompt();
}

function showWorkspace(vehicle) {
  $('#vehicleBox').classList.toggle('hidden', !vehicle);
  $('#modeTabs').classList.toggle('hidden', !vehicle);
  $('#editBox').classList.toggle('hidden', !vehicle);
  $('#builderTitle').classList.toggle('hidden', vehicle);
}

function fillConfigs() {
  const sel = $('#configSelect');
  sel.innerHTML = '';
  sel.append(h('option', { value: '' }, 'Default parts'));
  for (const c of S.summary?.configs || []) sel.append(h('option', { value: c.name }, c.name + (c.editable ? '  (mine)' : '')));
  sel.value = S.configName || '';
}

let buildToken = 0;
async function buildScene({ configName, fresh = false, quiet = false } = {}) {
  const token = ++buildToken;
  if (!quiet) setLoading(true, 'Building the vehicle…');
  S.building = true;
  emit('building', true);
  try {
    const garage = S.garageSpec || undefined;
    const sc = configName !== undefined ? await api.build(S.model, null, configName, garage) : await api.build(S.model, S.config, undefined, garage);
    if (token !== buildToken) return;   // a newer build is on its way
    setScene(sc, { fresh });
  } catch (e) {
    toast(e.message, 'err', 9000);
  } finally {
    if (token === buildToken) { S.building = false; emit('building', false); }
    if (!quiet) setLoading(false);
  }
}

function setScene(sc, { fresh = false } = {}) {
  closeDropTest();
  const old = S.scene;
  const keep = old && !fresh ? [...S.sel.nodes].map(i => old.nodes[i]?.id).filter(Boolean) : [];
  S.scene = sc;
  S.config = sc.config;
  reindex();
  clearSelection();
  for (const id of keep) { const i = sc.nodeById.get(id); if (i !== undefined) S.sel.nodes.add(i); }
  if (!S.activePart || !sc.parts[S.activePart] || !partEditable(S.activePart)) S.activePart = pickActive();
  fillActivePart();
  rebuild();
  updateGroundOffset();
  if (fresh) frame(sc.nodes.filter(n => !n.del).map(n => n.p), false);
  renderAll();
  updateDirty();
  updateHint();
  loadMeshes();
  updateBanner();
  emit('scene');
}

function pickActive() {
  const sc = S.scene;
  const count = {};
  for (const n of sc.nodes) if (!n.del) count[n.part] = (count[n.part] || 0) + 1;
  const ed = Object.keys(sc.parts).filter(partEditable);
  ed.sort((a, b) => (count[b] || 0) - (count[a] || 0));
  return ed[0] || null;
}

function fillActivePart() {
  const sel = $('#activePart');
  sel.innerHTML = '';
  const sc = S.scene;
  const ed = Object.keys(sc.parts).filter(partEditable);
  if (!ed.length) sel.append(h('option', { value: '' }, '(no part you can edit)'));
  for (const p of ed) sel.append(h('option', { value: p, title: p }, (sc.parts[p].title || p) + '  ·  ' + p));
  sel.value = S.activePart || '';
}

let meshToken = 0;
async function loadMeshes() {
  const sc = S.scene;
  const token = ++meshToken;
  const want = [...new Set(sc.flexbodies.map(f => f.mesh))].filter(n => !V.meshCache.has(n));
  if (want.length) {
    $('#statusRight').textContent = `Loading ${want.length} meshes…`;
    try {
      const r = await api.meshes(S.model, want);
      if (token !== meshToken) return;
      setMeshes(r.meshes);
      S.missingMeshes = r.missing;
    } catch (e) {
      toast('Could not load the 3D model: ' + e.message, 'err');
    }
  }
  if (token !== meshToken) return;
  buildMeshes();
  applyDisplay();
  updateStatus();
}

function renderAll() {
  renderParts();
  renderTuning();
  renderProblems();
  renderProps();
  renderWeights();
  renderVehicleSide();
  MODES[S.mode]?.render?.();
  updateStatus();
}

// in the look modes the vehicle stands on the ground grid (the lowest tire touches it)
function updateGroundOffset() {
  const sc = S.scene;
  let z = 0;
  if (sc && S.mode !== 'structure' && S.mode !== 'model' && sc.wheels.length) {
    let low = Infinity;
    for (const w of sc.wheels) {
      const a = sc.nodes[w.n1].p, b = sc.nodes[w.n2].p;
      low = Math.min(low, (a[2] + b[2]) / 2 - (w.radius || 0.3));
    }
    if (isFinite(low)) z = -low;
  }
  setVehicleOffset(z);
}

// ---- modes ------------------------------------------------------------------------------------
function setMode(mode, { quiet = false } = {}) {
  const prev = S.mode;
  if (prev && prev !== mode) MODES[prev]?.leave?.();
  S.mode = mode;
  for (const b of $$('#modeTabs button')) b.classList.toggle('active', b.dataset.mode === mode);
  for (const p of $$('.pane')) p.classList.toggle('active', p.dataset.pane.split(' ').includes(mode));
  const look = LOOK_MODES.has(mode);
  $('#toolbar').classList.toggle('show', mode === 'structure');
  if (look) {
    if (S.tool !== 'select') setTool('select');
    if (mode !== 'model' && mode !== 'builder') { clearSelection(); emit('selection'); }
  }
  S.lookMode = look;
  updateGroundOffset();
  applyDisplay();
  updateHint();
  updateGizmo();
  if (prev !== mode) MODES[mode]?.enter?.();
  MODES[mode]?.render?.();
  if (!quiet && S.scene) store.set('mode', mode);
}

// what the 3D view shows: solid model in the "look" modes, the skeleton in Structure
const DISPLAY_DEFAULTS = {
  look: { mesh: true, nodes: false, beams: false, tris: false, wheels: false, opacity: 1, ground: true },
  model: { mesh: true, nodes: false, beams: false, tris: false, wheels: false, opacity: 0.4, ground: true },
  structure: { mesh: true, nodes: true, beams: true, tris: true, wheels: true, opacity: 0.35, ground: true },
};
function displayKey() { return S.mode === 'structure' ? 'structure' : S.mode === 'model' ? 'model' : 'look'; }
function displayPrefs() {
  const k = displayKey();
  let saved = null;
  try { saved = JSON.parse(store.get('display.' + k) || 'null'); } catch { saved = null; }
  return Object.assign({}, DISPLAY_DEFAULTS[k], saved || {});
}
function saveDisplay(patch) {
  const k = displayKey();
  store.set('display.' + k, JSON.stringify(Object.assign(displayPrefs(), patch)));
  applyDisplay();
}
function applyDisplay() {
  const d = displayPrefs();
  // no meshes loaded (e.g. compiled .cdae files): show the wheels so there is something to see
  const noMesh = S.scene && S.scene.flexbodies.length && !V.groups.mesh.children.length;
  setVisible('mesh', d.mesh);
  setVisible('nodes', d.nodes);
  setVisible('beams', d.beams || (noMesh && S.mode !== 'structure'));
  setVisible('tris', d.tris);
  setVisible('wheels', d.wheels || (S.mode !== 'structure' && (noMesh || !wheelMeshes())));
  setMeshOpacity(d.opacity);
  setGround(d.ground);
  $('#vMesh').checked = d.mesh; $('#vNodes').checked = d.nodes; $('#vBeams').checked = d.beams;
  $('#vTris').checked = d.tris; $('#vWheels').checked = d.wheels; $('#vGround').checked = d.ground;
  $('#vMeshOpacity').value = d.opacity;
  redraw();
}
function wheelMeshes() {
  // are the tires drawn by their own meshes?
  const sc = S.scene;
  if (!sc) return false;
  return sc.flexbodies.some(f => f.groups.some(g => /^(tire|wheel)_/i.test(g)) && V.meshCache.has(f.mesh));
}

// ---- saving -------------------------------------------------------------------------------
let saving = null;
async function save({ quiet = false } = {}) {
  while (saving) await saving;
  if (!S.pending.length) { if (!quiet) toast('No changes to save.'); return true; }
  const n = S.pending.length;
  const ops = compactOps(S.pending);
  saving = (async () => {
    setLoading(true, 'Saving…');
    try {
      const r = await api.save(S.model, ops, S.config);
      if (!r.ok) { saveError(r.error); return false; }
      S.pending.splice(0, n);
      S.summary = r.summary;
      setScene(r.scene);
      toast(`Saved ${ops.length} change(s) to ${r.written.map(p => p.split(/[\\/]/).pop()).join(', ')}`, 'ok');
      return true;
    } catch (e) {
      saveError(e.message);
      return false;
    } finally { setLoading(false); }
  })();
  try { return await saving; } finally { saving = null; }
}

function saveError(msg) {
  openModal('Could not save', h('div', {},
    h('p', {}, 'Nothing was written: the files are as they were. The problem:'),
    h('pre', { class: 'code err', style: { whiteSpace: 'pre-wrap' } }, msg),
    h('p', { class: 'note' }, 'This can happen if the file was changed outside the editor. You can keep editing and try again, or throw away the unsaved edits and reload the files.'),
    h('div', { class: 'actions' },
      h('button', { class: 'danger', onclick: async () => {
        closeModal();
        S.pending = []; S.undo = []; S.redo = [];
        S.summary = await api.open(S.model, true);
        await buildScene({});
      } }, 'Throw away unsaved edits'),
      h('button', { class: 'primary', onclick: closeModal }, 'Keep editing'))));
}

// save the queue plus extra file edits in one go (part text, new slot, added mesh)
async function saveOps(extra, label) {
  while (saving) await saving;
  const n = S.pending.length;
  setLoading(true, 'Saving…');
  try {
    const r = await api.save(S.model, compactOps(S.pending).concat(extra), S.config);
    if (!r.ok) return r.error;
    S.pending.splice(0, n);
    S.undo = []; S.redo = [];
    S.summary = r.summary;
    setScene(r.scene);
    toast(label + ': saved', 'ok');
    return true;
  } catch (e) {
    return e.message;
  } finally { setLoading(false); }
}

async function rebuildConfig() {
  if (S.pending.length) { await save({ quiet: true }); return; }
  await buildScene({});
}

async function reloadModel() {
  S.summary = await api.open(S.model, true);
  fillConfigs();
  updateBanner();
  await buildScene({});
}

async function refreshVehicles() {
  const st = await api.state();
  S.appState = st;
  S.appConfig = st.config;
  S.vehicles = st.vehicles;
  renderGallery();
  return st;
}

async function switchConfig(name) {
  if (!(await save({ quiet: true }))) return;
  if (S.garageSpec) S.garageSpec = {};
  emit('configChanged', name);
  S.configName = name;
  store.set('config.' + S.model, name);
  fillConfigs();
  await buildScene({ configName: name });
}

function goHome(intent) {
  clearImport();
  showHome(intent);
}

Object.assign(A, { save, saveOps, rebuild: rebuildConfig, reloadModel, check: checkDialog, loadState, openVehicle,
  setMode, setScene, buildScene, refreshVehicles, switchConfig, goHome, showWorkspace, fillConfigs, updateStatus,
  applyDisplay, updateGroundOffset, renderAll,
  settings: () => settingsDialog(), help: helpDialog,
  openBuilder: () => (MODES.builder ? MODES.builder.open() : toast('The Vehicle Builder is not available.')),
  openScrap: () => (MODES.scrap ? MODES.scrap.open() : toast('The Scrap Mechanic importer is not available.')) });

function updateDirty() {
  const n = S.pending.length;
  const b = $('#btnSave');
  b.classList.toggle('dirty', n > 0);
  b.querySelector('span').textContent = n ? `Save (${n})` : 'Save';
  $('#btnUndo').disabled = !S.undo.length;
  $('#btnRedo').disabled = !S.redo.length;
  $('#btnUndo').title = S.undo.length ? `Undo ${S.undo[S.undo.length - 1].label} (Ctrl+Z)` : 'Undo (Ctrl+Z)';
  $('#btnRedo').title = S.redo.length ? `Redo ${S.redo[S.redo.length - 1].label} (Ctrl+Y)` : 'Redo (Ctrl+Y)';
}

function updateStatus() {
  const sc = S.scene;
  const chip = $('#healthChip');
  if (!sc) { $('#statusLeft').textContent = ''; chip.textContent = ''; return; }
  let n = 0, b = 0, t = 0;
  for (const x of sc.nodes) if (!x.del) n++;
  for (const x of sc.beams) if (!x.del) b++;
  for (const x of sc.tris) if (!x.del) t++;
  $('#statusLeft').textContent = `${S.summary?.name || S.model} · ${n.toLocaleString()} nodes · ${b.toLocaleString()} beams · ${t.toLocaleString()} triangles · ${sc.wheels.length} wheels · ${fmt(totalMass(), 0)} kg`;
  const miss = S.missingMeshes?.length ? ` · ${S.missingMeshes.length} meshes not found` : '';
  $('#statusRight').textContent = (S.pending.length ? `${S.pending.length} unsaved edit(s)` : 'All saved') + miss;
  const pc = problemCount();
  chip.innerHTML = '';
  chip.className = 'chip small ' + (pc.errors ? 'bad' : pc.warnings ? 'warn' : 'good');
  chip.append(icon(pc.errors ? 'error' : pc.warnings ? 'warning' : 'check', 13),
    pc.errors || pc.warnings ? `${pc.errors + pc.warnings} to look at` : 'No problems');
}

function updateBanner() {
  let b = $('#banner');
  if (!b) { b = h('div', { id: 'banner', class: 'hidden' }); $('#center').append(b); }
  b.innerHTML = '';
  const sm = S.summary;
  const off = (sm?.editableRoots || []).filter(r => r.offInGame);
  if (sm && sm.editableRoots.length && off.length) {
    b.append(h('span', {}, `${off.map(r => r.label).join(', ')} is switched off in the game's mod manager. You can edit it here; turn it on in the game to drive your changes.`),
      h('button', { class: 'small', title: 'Hide', onclick: () => b.classList.add('hidden') }, '✕'));
    b.classList.remove('hidden');
    return;
  }
  if (!sm || sm.editableRoots.length) { b.classList.add('hidden'); return; }
  const modZip = sm.zipRoots.find(r => r.origin === 'mod zip');
  if (modZip) {
    b.append(h('span', {}, `This vehicle is inside the zip "${modZip.label}", so it can't be changed in place.`),
      h('button', { class: 'primary small', onclick: async () => {
        try {
          setLoading(true, 'Unpacking…');
          const r = await api.unpack(modZip.id);
          toast(`Unpacked to ${r.path}. Move the zip out of your mods folder so the game doesn't load both.`, 'ok', 12000);
          await refreshVehicles();
          await reloadModel();
        } catch (e) { toast(e.message, 'err'); } finally { setLoading(false); }
      } }, 'Unpack it so I can edit'));
  } else {
    b.append(h('span', {}, S.mode === 'structure'
      ? 'This is a game vehicle, so its files are read-only. To change a part, use ⋯ next to its slot in Parts > Make an editable copy.'
      : 'Game vehicle: your changes are saved as your own version in a new mod, the original stays untouched.'));
  }
  b.append(h('button', { class: 'small ghost', title: 'Hide', onclick: () => b.classList.add('hidden') }, '✕'));
  b.classList.remove('hidden');
}

// ---- events from the other modules -----------------------------------------------------------
on('edited', () => {
  rebuild();
  renderProps();
  renderWeights();
  renderProblems();
  renderVehicleSide();
  updateDirty();
  updateStatus();
});
on('selection', () => {
  refreshColors();
  updateGizmo();
  renderProps();
  redraw();
});
on('activePart', () => {
  $('#activePart').value = S.activePart || '';
  renderParts();
  renderWeights();
  if (S.onlyActive) { rebuild(); updateMeshVisibility(); }
  if (!S.sel.nodes.size && !S.sel.beams.size && !S.sel.tris.size) renderProps();
  updateHint();
});
on('visibility', () => { rebuild(); updateMeshVisibility(); });
on('configs', () => fillConfigs());

// ---- toolbar, menus, keyboard ------------------------------------------------------------------
function moreMenu(anchor) {
  popupMenu(anchor, [
    ['Save as a config…', () => S.scene && saveConfigDialog()],
    ['Import a 3D model…', () => S.scene && startImport()],
    null,
    ['Drop test (does it sit right?)', () => S.scene && runDropTest()],
    ['Check my mod for mistakes', () => S.model && checkDialog()],
    ['Export my mod as a zip…', () => S.model && exportDialog()],
    null,
    ['Reload the files from disk', () => S.model && reloadModel()],
  ]);
}

function wireUI() {
  $('#btnHome').addEventListener('click', () => goHome());
  $('#btnVehicle').addEventListener('click', () => goHome());
  $('#configSelect').addEventListener('change', e => switchConfig(e.target.value));
  $('#btnUndo').addEventListener('click', undo);
  $('#btnRedo').addEventListener('click', redo);
  $('#btnSave').addEventListener('click', () => save());
  $('#btnMore').addEventListener('click', e => moreMenu(e.currentTarget));
  $('#btnSettings').addEventListener('click', () => settingsDialog());
  $('#btnHelp').addEventListener('click', helpDialog);
  $('#modalClose').addEventListener('click', closeModal);
  $('#modal').addEventListener('mousedown', e => { if (e.target.id === 'modal') closeModal(); });
  $('#healthChip').addEventListener('click', () => setMode('structure'));
  for (const b of $$('#modeTabs button')) b.addEventListener('click', () => { setMode(b.dataset.mode); updateBanner(); });

  $('#partSearch').addEventListener('input', debounce(renderParts, 150));
  $('#partSearch').addEventListener('keydown', e => e.stopPropagation());

  for (const b of $$('#tools button')) b.addEventListener('click', () => setTool(b.dataset.tool));
  $('#optMirror').addEventListener('change', e => { S.mirror = e.target.checked; renderProps(); });
  $('#optSnap').addEventListener('change', e => { S.snap = +e.target.value; updateGizmo(); });
  $('#activePart').addEventListener('change', e => { S.activePart = e.target.value || null; emit('activePart'); });
  $('#optPin').addEventListener('change', e => { S.pinActive = e.target.checked; });

  $('#btnDisplay').addEventListener('click', e => { e.stopPropagation(); $('#displayMenu').classList.toggle('hidden'); });
  document.addEventListener('mousedown', e => {
    const m = $('#displayMenu');
    if (!m.classList.contains('hidden') && !m.contains(e.target) && !$('#btnDisplay').contains(e.target)) m.classList.add('hidden');
  });
  $('#vNodes').addEventListener('change', e => saveDisplay({ nodes: e.target.checked }));
  $('#vBeams').addEventListener('change', e => saveDisplay({ beams: e.target.checked }));
  $('#vTris').addEventListener('change', e => saveDisplay({ tris: e.target.checked }));
  $('#vWheels').addEventListener('change', e => saveDisplay({ wheels: e.target.checked }));
  $('#vMesh').addEventListener('change', e => saveDisplay({ mesh: e.target.checked }));
  $('#vGround').addEventListener('change', e => saveDisplay({ ground: e.target.checked }));
  $('#vMeshOpacity').addEventListener('input', e => setMeshOpacity(+e.target.value));
  $('#vMeshOpacity').addEventListener('change', e => saveDisplay({ opacity: +e.target.value }));
  $('#vLabels').addEventListener('change', e => { V.visible.labels = e.target.checked; redraw(); });
  $('#vStability').addEventListener('change', e => { S.showStability = e.target.checked; refreshColors(); });
  $('#vOnlyActive').addEventListener('change', e => { S.onlyActive = e.target.checked; rebuild(); updateMeshVisibility(); });
  $('#vNodeSize').addEventListener('input', e => setNodeSize(+e.target.value));
  for (const b of $$('[data-view]')) b.addEventListener('click', () => {
    if (!S.scene && !V.previewing) return;
    if (b.dataset.view === 'all') frameAll(); else viewPreset(b.dataset.view);
  });

  window.addEventListener('keydown', onKey);
  window.addEventListener('beforeunload', e => {
    if (S.pending.length || S.garageDirty) { e.preventDefault(); e.returnValue = ''; }
  });
}

function frameAll() {
  if (MODES[S.mode]?.frame?.()) return;
  if (S.scene) frame(S.scene.nodes.filter(n => !n.del).map(n => n.p));
}

function onKey(e) {
  if (modalOpen()) { if (e.key === 'Escape') closeModal(); return; }
  if (homeOpen()) return;
  const k = e.key.toLowerCase();
  const ctrl = e.ctrlKey || e.metaKey;
  if (e.target.matches && e.target.matches('input, textarea, select') && !(ctrl && k === 's')) return;
  if (MODES[S.mode]?.key?.(e)) { e.preventDefault(); return; }
  if (ctrl) {
    if (k === 'z' && !e.shiftKey) undo();
    else if (k === 'y' || (k === 'z' && e.shiftKey)) redo();
    else if (k === 's') save();
    else if (k === 'a' && S.scene && S.mode === 'structure') {
      clearSelection();
      S.scene.nodes.forEach((n, i) => { if (!n.del && (!S.activePart || n.part === S.activePart)) S.sel.nodes.add(i); });
      emit('selection');
    } else return;
    e.preventDefault();
    return;
  }
  if (k === 'home') { frameAll(); e.preventDefault(); return; }
  if (k === '1' || k === '3' || k === '7') { viewPreset({ 1: 'front', 3: 'side', 7: 'top' }[k]); e.preventDefault(); return; }
  if (!S.scene || S.mode !== 'structure') return;
  switch (k) {
    case 'q': case 'w': setTool('select'); break;
    case 'n': setTool('node'); break;
    case 'b':
      if (e.shiftKey) { if (!connectSelected(true)) toast('Select 2 or more nodes to brace them together.'); }
      else if (S.tool === 'select' && S.sel.nodes.size >= 2) connectSelected(false);
      else setTool('beam');
      break;
    case 't':
      if (S.tool === 'select' && S.sel.nodes.size === 3) makeTriangle([...S.sel.nodes]); else setTool('tri');
      break;
    case 'i': flipSelectedTris(); break;
    case 'delete': case 'backspace': deleteSelection(); break;
    case 'f': frameSelection(); break;
    case 'm': $('#optMirror').checked = S.mirror = !S.mirror; toast(S.mirror ? 'Mirror on: left and right change together' : 'Mirror off'); renderProps(); break;
    case 'escape': if (importActive()) clearImport(); else escape(); break;
    default: return;
  }
  e.preventDefault();
}

// the window stays open while this page is open; the editor stops when it closes
function keepAlive() {
  try {
    const es = new EventSource('api/events');
    es.addEventListener('error', () => { /* the browser reconnects by itself */ });
  } catch { /* old browser: the editor then keeps running until closed */ }
}

// for debugging from the browser console and for the UI tests
window.jbe = { S, V, A, emit, MODES };

// ---- start ------------------------------------------------------------------------------------------
async function start() {
  hydrateIcons();
  initView();
  initTools();
  initImporter();
  initHome();
  wireUI();
  setTool('select');
  updateDirty();
  keepAlive();
  // optional modes load on their own so a problem in one never stops the editor
  for (const m of ['garage', 'builder', 'scrap', 'workshop']) {
    import('./' + m + '.js').then(mod => mod.register?.(MODES)).catch(e => console.warn('mode ' + m + ' not loaded:', e));
  }
  try {
    await loadState();
    const last = store.get('model');
    if (last && new URLSearchParams(location.search).has('open')) openVehicle(last, { mode: store.get('mode') || 'garage' });
  } catch (e) {
    toast('Could not reach the editor\'s server: ' + e.message, 'err', 15000);
  }
}
start();
