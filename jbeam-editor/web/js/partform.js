// "Make it a part": the choices for turning a placed mesh into a BeamNG part, with a
// live preview of the structure in the 3D view. Shared by the importer and the Workshop.
import { S, A, emit, on, off } from './state.js';
import { showStructure, updateGizmo } from './view3d.js';
import { updateHint } from './tools.js';
import { partEditable } from './scene.js';
import { rootPicker, slotSelect, freePrefix } from './dialogs.js';
import { attachBeams, createPart, makeStructure, meshOnPart, meshBounds, mountSettings, mountSpringLimit, MOUNT, STIFFNESS } from './partmaker.js';
import { assignWeights } from './smartfit.js';
import { icon } from './icons.js';
import { $, h, fmt, toast, debounce, setLoading } from './util.js';

const DETAILS = [['coarse', 'Coarse'], ['medium', 'Medium'], ['fine', 'Fine']];

// container: element to fill. getMesh() -> { pos, idx, uv } in vehicle coordinates (or null)
export function partForm({ container, baseName, title, getMesh, onDone, onCancel, mass = 25, kind = 'smart', colours = true }) {
  const st = {
    kind, detail: 'medium', stiffness: 'panel', mass, collision: true, breakable: true,
    attach: S.sel.nodes.size ? 'sel' : 'near', fit: null, at: null,
    mount: 'light', springOv: null, strengthOv: null, mt: null,
  };
  const anchors = () => [...S.sel.nodes].filter(i => !S.scene.nodes[i].del);
  const nameIn = h('input', { type: 'text', value: baseName });
  const titleIn = h('input', { type: 'text', value: title });
  for (const i of [nameIn, titleIn]) i.addEventListener('keydown', e => e.stopPropagation());
  const slotSel = slotSelect(null, { preferEmpty: true });
  const slotWarn = h('div', { class: 'note warn' });
  const checkSlot = () => {
    const sl = slotSel.getSlot();
    slotWarn.textContent = sl?.chosen ? `This slot has ${S.scene.partTitles[sl.chosen] || sl.chosen} in it now: your part takes its place. Pick an empty slot to add it instead.` : '';
  };
  slotSel.addEventListener('change', checkSlot);
  const root = rootPicker();
  const colour = h('input', { type: 'color', value: '#9aa0a8' });
  const paint = h('input', { type: 'checkbox' });
  const stats = h('div', { class: 'note' });
  const err = h('div', { class: 'err' });

  const seg = (opts, cur, fn) => {
    const box = h('div', { class: 'seg' });
    for (const [v, label] of opts) {
      const b = h('button', { class: v === cur() ? 'active' : '' }, label);
      b.addEventListener('click', () => { fn(v); for (const x of box.children) x.classList.toggle('active', x === b); update(); });
      box.append(b);
    }
    return box;
  };
  const kindSeg = seg([['smart', 'Fitted to the shape'], ['box', 'Simple box'], ['mesh', 'No structure']], () => st.kind, v => { st.kind = v; showFor(); });
  const detailSeg = seg(DETAILS, () => st.detail, v => { st.detail = v; });
  const stiffSel = h('select', {}, ...Object.entries(STIFFNESS).map(([k, v]) => h('option', { value: k, selected: k === st.stiffness }, v.label)));
  stiffSel.addEventListener('change', () => { st.stiffness = stiffSel.value; update(); });
  const massIn = h('input', { type: 'number', step: '0.5', min: '0.5', value: st.mass, style: { width: '90px' } });
  massIn.addEventListener('change', () => { st.mass = Math.max(0.5, +massIn.value || 25); update(); });
  massIn.addEventListener('keydown', e => e.stopPropagation());
  const collision = h('input', { type: 'checkbox', checked: true });
  collision.addEventListener('change', () => { st.collision = collision.checked; update(); });
  const breakable = h('input', { type: 'checkbox', checked: true });
  breakable.addEventListener('change', () => { st.breakable = breakable.checked; st.strengthOv = null; update(); });
  const attachSel = h('select', {},
    h('option', { value: 'sel' }, 'To the nodes I selected'),
    h('option', { value: 'near' }, 'To the nearest vehicle nodes'),
    h('option', { value: 'none' }, 'Not attached (it falls off)'));
  attachSel.value = st.attach;
  attachSel.addEventListener('change', () => {
    st.attach = attachSel.value;
    if (st.attach === 'sel' && !anchors().length) setPick(true);
    if (st.attach !== 'sel') setPick(false);
    showPickRow();
    update();
  });

  // picking the vehicle nodes it bolts to, right in the 3D view
  const pickBtn = h('button', { class: 'small', onclick: () => setPick(!S.nodePick) });
  const pickInfo = h('span', { class: 'muted', style: { fontSize: '12px' } });
  const pickRow = h('div', { class: 'btnrow', style: { marginTop: 0 } }, pickBtn, pickInfo,
    h('button', { class: 'small ghost', title: 'Unpick all', onclick: () => { S.sel.nodes.clear(); emit('selection'); } }, 'Clear'));
  function setPick(on) {
    S.nodePick = on;
    pickBtn.replaceChildren(icon(on ? 'check' : 'target', 13), on ? 'Done picking' : 'Pick nodes on the vehicle');
    pickBtn.classList.toggle('primary', on);
    A.applyDisplay();
    updateGizmo();
    updateHint(on ? 'Click the vehicle nodes the part bolts to (Shift+click for more, drag a box around several, Ctrl+click to unpick). Pick at least 3 that aren\'t in a line.' : undefined);
  }
  function showPickRow() {
    pickRow.classList.toggle('hidden', st.attach !== 'sel');
    const n = anchors().length;
    pickInfo.textContent = n ? `${n} vehicle node${n === 1 ? '' : 's'} picked` : 'none picked yet';
  }
  const onSel = () => { if (!container.isConnected) return stop(); showPickRow(); update(); };
  on('selection', onSel);

  // how firmly it is fixed on
  const mountSel = h('select', {}, ...Object.entries(MOUNT).map(([k, v]) => h('option', { value: k, selected: k === st.mount }, v.label)));
  mountSel.addEventListener('change', () => { st.mount = mountSel.value; st.springOv = st.strengthOv = null; update(); });
  const springIn = h('input', { type: 'number', step: '100000', min: '10000', style: { width: '120px' } });
  springIn.addEventListener('change', () => { const v = +springIn.value; st.springOv = v > 0 ? v : null; update(); });
  const strengthIn = h('input', { type: 'number', step: '1', min: '1', style: { width: '90px' }, placeholder: 'never' });
  strengthIn.addEventListener('change', () => { const v = +strengthIn.value; st.strengthOv = strengthIn.value === '' ? 'FLT_MAX' : v > 0 ? v * 1000 : null; update(); });
  for (const i of [springIn, strengthIn]) i.addEventListener('keydown', e => e.stopPropagation());
  const mountNote = h('div', { class: 'note' });
  const mountNow = () => {
    const m = mountSettings(st.stiffness, st.mount, st.breakable);
    if (st.springOv) m.spring = st.springOv;
    if (st.strengthOv) m.strength = st.strengthOv;
    const lim = st.at?.beams.length ? mountSpringLimit(st.at.beams) : null;
    m.limited = lim !== null && m.spring > lim;
    if (m.limited) m.spring = lim;
    return m;
  };
  const activeOk = S.activePart && partEditable(S.activePart);

  const structBox = h('div', {},
    h('div', { class: 'form', style: { gridTemplateColumns: '120px 1fr' } },
      h('span', { class: 'k' }, 'Detail'), detailSeg,
      h('span', { class: 'k' }, 'Stiffness'), stiffSel,
      h('span', { class: 'k' }, 'Mass (kg)'), massIn,
      h('span', { class: 'k' }, 'Collision'), h('label', { class: 'chk' }, collision, 'Other things bump into it'),
      h('span', { class: 'k' }, 'Mounted'), h('span', {}, attachSel, pickRow),
      h('span', { class: 'k', title: 'How firmly it is fixed to the vehicle' }, 'Mount strength'), mountSel,
      h('span', { class: 'k', title: 'Stiffness of each mounting beam in N/m. Higher holds it tighter.' }, 'Mount spring'), springIn,
      h('span', { class: 'k', title: 'The pull in kilonewtons that breaks a mounting beam. Empty: never breaks.' }, 'Breaks at (kN)'), strengthIn,
      h('span', { class: 'k' }, ''), h('label', { class: 'chk' }, breakable, 'Can break off in a crash'),
      h('span', { class: 'k' }, ''), mountNote,
      h('span', { class: 'k' }, 'Slot'), h('span', {}, slotSel, slotWarn),
      h('span', { class: 'k' }, 'Name in parts menu'), titleIn,
      h('span', { class: 'k' }, 'Save into mod'), root),
    stats);
  const meshBox = h('p', { class: 'note' }, activeOk
    ? `The mesh is added to ${S.scene.parts[S.activePart]?.title || S.activePart} and bends with its nodes. Nothing new to tune, but it can't break off on its own.`
    : 'Pick a part you can edit (Structure > Part you\'re editing) to put the mesh on it.');
  function showFor() {
    structBox.classList.toggle('hidden', st.kind === 'mesh');
    meshBox.classList.toggle('hidden', st.kind !== 'mesh');
  }

  const compute = () => {
    const m = getMesh();
    if (!m || st.kind === 'mesh') { st.fit = null; showStructure(null); stats.textContent = ''; return; }
    try {
      const prefix = freePrefix(nameIn.value || 'part');
      const vc = S.scene ? centreOf() : null;
      st.fit = makeStructure(m.pos, m.idx, { kind: st.kind, detail: st.detail, prefix, inside: vc });
      st.at = st.attach === 'none' ? { beams: [] } : attachBeams(st.fit, st.attach, anchors(), st.mount);
      st.mt = mountNow();
      springIn.value = st.mt.spring;
      strengthIn.value = st.mt.strength === 'FLT_MAX' ? '' : +(st.mt.strength / 1000).toFixed(1);
      mountNote.textContent = (st.at.beams.length ? `${st.at.beams.length} mounting beams` +
        (st.mt.strength === 'FLT_MAX' ? ', never break' : `, each breaks at ${fmt(st.mt.strength / 1000, 1)} kN`) + '.' : '') +
        (st.mt.limited ? ' The spring is as stiff as the vehicle\'s nodes take and stay stable; pick more nodes to hold it tighter.' : '');
      const s = STIFFNESS[st.stiffness];
      const w = assignWeights(st.fit, st.mass, { spring: s.spring, damp: s.damp, attach: st.at.beams, attachSpring: st.mt.spring });
      showStructure(st.collision ? st.fit : { ...st.fit, tris: [] }, { attach: st.at.beams });
      const size = meshBounds(m.pos).size;
      stats.innerHTML = '';
      stats.append(`${st.fit.nodes.length} nodes, ${st.fit.beams.main.length + st.fit.beams.rigid.length} beams, ` +
        `${st.collision ? st.fit.tris.length : 0} collision triangles, ${st.at.beams.length} mounting beams. ` +
        `Size ${fmt(size[0], 2)} × ${fmt(size[1], 2)} × ${fmt(size[2], 2)} m.`);
      if (w.needed > st.mass * 1.05) {
        stats.append(h('div', { class: 'warn' }, icon('warning', 13), ` To be stable it needs at least ${fmt(w.needed, 1)} kg. ` +
          'Use less detail or softer stiffness for a lighter part, or keep it: the weights are raised to stay stable.'));
      }
      if (st.at.warn) stats.append(h('div', { class: 'warn' }, st.at.warn));
      if (st.attach === 'sel' && !anchors().length) stats.append(h('div', { class: 'warn' }, 'Pick the vehicle nodes it bolts to (Pick nodes on the vehicle), or choose "nearest".'));
    } catch (e) {
      stats.textContent = 'Could not build the structure: ' + e.message;
    }
  };
  const update = debounce(compute, 180);

  const go = async () => {
    err.textContent = '';
    const name = nameIn.value.trim();
    if (!/^[A-Za-z0-9_\-.]+$/.test(name)) return (err.textContent = 'Use only letters, digits, _ - and . in the id.');
    const m = getMesh();
    if (!m) return (err.textContent = 'No mesh to make a part from.');
    const rgb = [1, 3, 5].map(k => parseInt(colour.value.slice(k, k + 2), 16) / 255);
    try {
      setLoading(true, 'Making the part…');
      await A.save({ quiet: true });
      if (st.kind === 'mesh') {
        if (!activeOk) throw new Error('Pick a part you can edit first (Structure > Part you\'re editing).');
        const grp = await meshOnPart({ pos: m.pos, idx: m.idx, uv: m.uv, name, part: S.activePart, color: rgb, paint: paint.checked, groups: m.groups });
        stop();
        showStructure(null);
        onDone?.();
        toast(`Added the mesh to ${S.activePart}. It bends with the "${grp}" nodes.`, 'ok', 7000);
        return;
      }
      compute();
      const slot = slotSel.getSlot();
      if (!slot) throw new Error('Pick a slot for the part.');
      const rootId = await root.getRoot();
      setPick(false);
      const r = await createPart({ pos: m.pos, idx: m.idx, uv: m.uv, name, title: titleIn.value.trim() || name, slot, rootId,
        fit: st.fit, attach: st.at.beams, mass: st.mass, stiffness: st.stiffness, collision: st.collision, breakable: st.breakable,
        mount: st.mt, color: rgb, paint: paint.checked, groups: m.groups });
      stop();
      showStructure(null);
      onDone?.();
      toast(`Made part ${name}: ${r.nodes} nodes, ${r.beams} beams, ${r.tris} triangles, ${fmt(r.mass, 1)} kg.` +
        (st.at.warn ? ' ' + st.at.warn : ''), st.at.warn ? 'err' : 'ok', 9000);
    } catch (e) {
      err.textContent = e.message;
    } finally { setLoading(false); }
  };

  container.append(
    h('h3', { class: 'sect' }, 'Make it a part'),
    h('div', { class: 'form', style: { gridTemplateColumns: '120px 1fr' } },
      h('span', { class: 'k' }, 'Part id'), nameIn,
      colours ? h('span', { class: 'k' }, 'Colour') : null, colours ? h('span', { style: { display: 'flex', gap: '8px', alignItems: 'center' } }, colour,
        h('label', { class: 'chk' }, paint, 'Vehicle paint')) : null),
    h('p', { class: 'note' }, 'Structure: the nodes and beams the game simulates. Fitted to the shape follows the model, so it collides and crumples where it really is.'),
    kindSeg, structBox, meshBox, err,
    h('div', { class: 'btnrow' },
      onCancel ? h('button', { onclick: () => { stop(); showStructure(null); onCancel(); } }, 'Cancel') : null,
      h('button', { class: 'primary', onclick: go }, icon('check', 15), 'Make part')));
  function stop() {
    off('selection', onSel);
    if (S.nodePick) setPick(false);
  }
  showFor();
  checkSlot();
  showPickRow();
  setPick(st.attach === 'sel' && !anchors().length);
  compute();
  return { update, refresh: compute, clear: () => { stop(); showStructure(null); } };
}

function centreOf() {
  const sc = S.scene;
  let n = 0;
  const c = [0, 0, 0];
  for (const x of sc.nodes) if (!x.del) { for (let k = 0; k < 3; k++) c[k] += x.p[k]; n++; }
  return n ? [0, c[1] / n, c[2] / n] : null;
}
