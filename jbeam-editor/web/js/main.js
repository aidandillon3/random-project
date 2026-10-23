// Starts the editor: loads vehicles, builds the chosen config, saves edits,
// and wires the toolbar, menus and keyboard.
import { S, A, on, emit, clearSelection } from './state.js';
import { api } from './api.js';
import { partEditable, reindex, totalMass } from './scene.js';
import { V, buildMeshes, frame, initView, rebuild, redraw, refreshColors, setMeshOpacity, setMeshes, setNodeSize,
  setVisible, updateGizmo, updateMeshVisibility, viewPreset } from './view3d.js';
import { connectSelected, deleteSelection, escape, flipSelectedTris, frameSelection, initTools, makeTriangle, setTool,
  updateHint } from './tools.js';
import { compactOps, redo, undo } from './edit.js';
import { renderParts, renderProblems, renderProps, renderTuning, renderWeights } from './panels.js';
import { checkDialog, exportDialog, helpDialog, saveConfigDialog, settingsDialog } from './dialogs.js';
import { clearImport, importActive, initImporter } from './importer.js';
import { $, $$, closeModal, debounce, fmt, h, modalOpen, openModal, setLoading, toast } from './util.js';

const store = {
  get(k) { try { return localStorage.getItem('jbe.' + k); } catch { return null; } },
  set(k, v) { try { localStorage.setItem('jbe.' + k, v); } catch { /* private window */ } },
};

// ---- loading vehicles and configs ------------------------------------------------------
async function loadState(st) {
  st = st || await api.state();
  S.appConfig = st.config;
  S.dataDir = st.dataDir;
  S.vehicles = st.vehicles;
  const sel = $('#vehicleSelect');
  sel.innerHTML = '';
  const list = st.vehicles.slice().sort((a, b) => (b.editable - a.editable) || String(a.name).localeCompare(String(b.name)));
  let group = null;
  for (const v of list) {
    const g = v.editable ? 'Your mods (editable)' : 'Read-only (game and zips)';
    if (g !== group) { group = g; sel.append(h('optgroup', { label: g })); }
    const label = (v.name && !String(v.name).startsWith('vehiclesData') ? v.name : v.model) + `  (${v.model})`;
    sel.lastChild.append(h('option', { value: v.model }, label));
  }
  if (!list.length) {
    $('#hint').textContent = 'No vehicles found. Open Settings and point the editor at your BeamNG folders.';
    if (!st.userFound || !st.gameFound) settingsDialog(true);
    return;
  }
  const last = store.get('model');
  const pick = list.find(v => v.model === last) || list.find(v => v.editable) || list[0];
  sel.value = pick.model;
  await openVehicle(pick.model);
  if (!st.userFound) toast('Your BeamNG user folder wasn\'t found. Set it in Settings to edit your mods.', 'err', 9000);
}

async function openVehicle(model) {
  if (S.model && S.pending.length && !(await save({ quiet: true }))) return;
  clearImport();
  Object.assign(S, { model, scene: null, undo: [], redo: [], pending: [], activePart: null, pinActive: false });
  S.hiddenParts.clear();
  clearSelection();
  $('#optPin').checked = false;
  for (const g of V.meshCache.values()) g.dispose();
  V.meshCache.clear();
  store.set('model', model);
  setLoading(true, 'Reading ' + model + '…');
  try {
    S.summary = await api.open(model);
    const names = S.summary.configs.map(c => c.name);
    const last = store.get('config.' + model);
    S.configName = last !== null && (last === '' || names.includes(last)) ? last : (S.summary.defaultConfig || '');
    fillConfigs();
    await buildScene({ configName: S.configName, fresh: true });
  } catch (e) {
    toast(e.message, 'err', 9000);
  } finally { setLoading(false); }
  updateBanner();
}

function fillConfigs() {
  const sel = $('#configSelect');
  sel.innerHTML = '';
  sel.append(h('option', { value: '' }, '(default parts)'));
  for (const c of S.summary?.configs || []) sel.append(h('option', { value: c.name }, c.name + (c.editable ? '' : '  🔒')));
  sel.value = S.configName || '';
}

async function buildScene({ configName, fresh = false } = {}) {
  setLoading(true, 'Building the vehicle…');
  try {
    const sc = configName !== undefined ? await api.build(S.model, null, configName) : await api.build(S.model, S.config);
    setScene(sc, { fresh });
  } catch (e) {
    toast(e.message, 'err', 9000);
  } finally { setLoading(false); }
}

function setScene(sc, { fresh = false } = {}) {
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
  if (fresh) frame(sc.nodes.filter(n => !n.del).map(n => n.p), false);
  renderAll();
  updateDirty();
  updateHint();
  loadMeshes();
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
  updateStatus();
}

function renderAll() {
  renderParts();
  renderTuning();
  renderProblems();
  renderProps();
  renderWeights();
  updateStatus();
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

Object.assign(A, { save, saveOps, rebuild: rebuildConfig, reloadModel, check: checkDialog, loadState });

function updateDirty() {
  const n = S.pending.length;
  const b = $('#btnSave');
  b.classList.toggle('dirty', n > 0);
  b.textContent = n ? `Save (${n})` : 'Save';
  $('#btnUndo').disabled = !S.undo.length;
  $('#btnRedo').disabled = !S.redo.length;
  $('#btnUndo').title = S.undo.length ? `Undo ${S.undo[S.undo.length - 1].label} (Ctrl+Z)` : 'Undo (Ctrl+Z)';
  $('#btnRedo').title = S.redo.length ? `Redo ${S.redo[S.redo.length - 1].label} (Ctrl+Y)` : 'Redo (Ctrl+Y)';
}

function updateStatus() {
  const sc = S.scene;
  if (!sc) return;
  let n = 0, b = 0, t = 0;
  for (const x of sc.nodes) if (!x.del) n++;
  for (const x of sc.beams) if (!x.del) b++;
  for (const x of sc.tris) if (!x.del) t++;
  $('#statusLeft').textContent = `${S.summary?.name || S.model} · ${n.toLocaleString()} nodes · ${b.toLocaleString()} beams · ${t.toLocaleString()} triangles · ${sc.wheels.length} wheels · ${fmt(totalMass(), 0)} kg`;
  const miss = S.missingMeshes?.length ? ` · ${S.missingMeshes.length} meshes not found` : '';
  $('#statusRight').textContent = (S.pending.length ? `${S.pending.length} unsaved edit(s)` : 'All saved') + miss;
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
      h('button', { class: 'primary', onclick: async () => {
        try {
          setLoading(true, 'Unpacking…');
          const r = await api.unpack(modZip.id);
          toast(`Unpacked to ${r.path}. Move the zip out of your mods folder so the game doesn't load both.`, 'ok', 12000);
          await loadState();
        } catch (e) { toast(e.message, 'err'); } finally { setLoading(false); }
      } }, 'Unpack it so I can edit'));
  } else {
    b.append(h('span', {}, 'Game vehicles are read-only. To change a part, use ⋯ next to its slot > Make an editable copy (it goes into a new mod folder). Save config makes a new config.'));
  }
  b.append(h('button', { class: 'small', title: 'Hide', onclick: () => b.classList.add('hidden') }, '✕'));
  b.classList.remove('hidden');
}

// ---- events from the other modules -----------------------------------------------------------
on('edited', () => {
  rebuild();
  renderProps();
  renderWeights();
  renderProblems();
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
function wireUI() {
  $('#vehicleSelect').addEventListener('change', e => openVehicle(e.target.value));
  $('#configSelect').addEventListener('change', async e => {
    if (!(await save({ quiet: true }))) { e.target.value = S.configName || ''; return; }
    S.configName = e.target.value;
    store.set('config.' + S.model, S.configName);
    await buildScene({ configName: S.configName });
  });
  $('#btnSaveConfig').addEventListener('click', () => S.scene && saveConfigDialog());
  $('#btnUndo').addEventListener('click', undo);
  $('#btnRedo').addEventListener('click', redo);
  $('#btnSave').addEventListener('click', () => save());
  $('#btnCheck').addEventListener('click', () => S.model && checkDialog());
  $('#btnExport').addEventListener('click', () => S.model && exportDialog());
  $('#btnSettings').addEventListener('click', () => settingsDialog());
  $('#btnHelp').addEventListener('click', helpDialog);
  $('#modalClose').addEventListener('click', closeModal);
  $('#modal').addEventListener('mousedown', e => { if (e.target.id === 'modal') closeModal(); });

  for (const b of $$('#leftTabs button')) b.addEventListener('click', () => {
    for (const x of $$('#leftTabs button')) x.classList.toggle('active', x === b);
    for (const t of $$('#left .tab')) t.classList.toggle('hidden', t.id !== 'tab-' + b.dataset.tab);
  });
  $('#partSearch').addEventListener('input', debounce(renderParts, 150));
  $('#partSearch').addEventListener('keydown', e => e.stopPropagation());

  for (const b of $$('#tools button')) b.addEventListener('click', () => setTool(b.dataset.tool));
  $('#optMirror').addEventListener('change', e => { S.mirror = e.target.checked; renderProps(); });
  $('#optSnap').addEventListener('change', e => { S.snap = +e.target.value; updateGizmo(); });
  $('#activePart').addEventListener('change', e => { S.activePart = e.target.value || null; emit('activePart'); });
  $('#optPin').addEventListener('change', e => { S.pinActive = e.target.checked; });

  $('#vNodes').addEventListener('change', e => setVisible('nodes', e.target.checked));
  $('#vBeams').addEventListener('change', e => setVisible('beams', e.target.checked));
  $('#vTris').addEventListener('change', e => setVisible('tris', e.target.checked));
  $('#vWheels').addEventListener('change', e => setVisible('wheels', e.target.checked));
  $('#vMesh').addEventListener('change', e => setVisible('mesh', e.target.checked));
  $('#vMeshOpacity').addEventListener('input', e => setMeshOpacity(+e.target.value));
  $('#vLabels').addEventListener('change', e => { V.visible.labels = e.target.checked; redraw(); });
  $('#vStability').addEventListener('change', e => { S.showStability = e.target.checked; refreshColors(); });
  $('#vOnlyActive').addEventListener('change', e => { S.onlyActive = e.target.checked; rebuild(); updateMeshVisibility(); });
  $('#vNodeSize').addEventListener('input', e => setNodeSize(+e.target.value));
  for (const b of $$('[data-view]')) b.addEventListener('click', () => {
    if (!S.scene) return;
    if (b.dataset.view === 'all') frameAll(); else viewPreset(b.dataset.view);
  });
  document.addEventListener('click', e => {
    const d = $('.viewmenu');
    if (d.open && !d.contains(e.target)) d.open = false;
  });

  window.addEventListener('keydown', onKey);
  window.addEventListener('beforeunload', e => { if (S.pending.length) { e.preventDefault(); e.returnValue = ''; } });
}

function frameAll() { frame(S.scene.nodes.filter(n => !n.del).map(n => n.p)); }

function onKey(e) {
  if (modalOpen()) { if (e.key === 'Escape') closeModal(); return; }
  const k = e.key.toLowerCase();
  const ctrl = e.ctrlKey || e.metaKey;
  if (e.target.matches && e.target.matches('input, textarea, select') && !(ctrl && k === 's')) return;
  if (ctrl) {
    if (k === 'z' && !e.shiftKey) undo();
    else if (k === 'y' || (k === 'z' && e.shiftKey)) redo();
    else if (k === 's') save();
    else if (k === 'a' && S.scene) {
      clearSelection();
      S.scene.nodes.forEach((n, i) => { if (!n.del && (!S.activePart || n.part === S.activePart)) S.sel.nodes.add(i); });
      emit('selection');
    } else return;
    e.preventDefault();
    return;
  }
  if (!S.scene) return;
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
    case 'home': frameAll(); break;
    case '1': viewPreset('front'); break;
    case '3': viewPreset('side'); break;
    case '7': viewPreset('top'); break;
    case 'm': $('#optMirror').checked = S.mirror = !S.mirror; toast(S.mirror ? 'Mirror on: left and right change together' : 'Mirror off'); renderProps(); break;
    case 'escape': if (importActive()) clearImport(); else escape(); break;
    default: return;
  }
  e.preventDefault();
}

// for debugging from the browser console
window.jbe = { S, V, A, emit };

// ---- start ------------------------------------------------------------------------------------------
initView();
initTools();
initImporter();
wireUI();
setTool('select');
updateDirty();
loadState().catch(e => toast('Could not reach the editor\'s server: ' + e.message, 'err', 15000));
