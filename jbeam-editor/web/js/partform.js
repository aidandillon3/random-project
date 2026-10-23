// "Make it a part": the choices for turning a placed mesh into a BeamNG part, with a
// live preview of the structure in the 3D view. Shared by the importer and the Workshop.
import { S, A } from './state.js';
import { showStructure } from './view3d.js';
import { partEditable } from './scene.js';
import { rootPicker, slotSelect, freePrefix } from './dialogs.js';
import { attachBeams, createPart, makeStructure, meshOnPart, meshBounds, STIFFNESS } from './partmaker.js';
import { assignWeights } from './smartfit.js';
import { icon } from './icons.js';
import { $, h, fmt, toast, debounce, setLoading } from './util.js';

const DETAILS = [['coarse', 'Coarse'], ['medium', 'Medium'], ['fine', 'Fine']];

// container: element to fill. getMesh() -> { pos, idx, uv } in vehicle coordinates (or null)
export function partForm({ container, baseName, title, getMesh, onDone, onCancel, mass = 25, kind = 'smart', colours = true }) {
  const st = {
    kind, detail: 'medium', stiffness: 'panel', mass, collision: true, breakable: true,
    attach: S.sel.nodes.size ? 'sel' : 'near', fit: null, at: null,
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
  breakable.addEventListener('change', () => { st.breakable = breakable.checked; });
  const attachSel = h('select', {},
    h('option', { value: 'sel' }, 'To the nodes I selected'),
    h('option', { value: 'near' }, 'To the nearest vehicle nodes'),
    h('option', { value: 'none' }, 'Not attached (it falls off)'));
  attachSel.value = st.attach;
  attachSel.addEventListener('change', () => { st.attach = attachSel.value; update(); });
  const activeOk = S.activePart && partEditable(S.activePart);

  const structBox = h('div', {},
    h('div', { class: 'form', style: { gridTemplateColumns: '120px 1fr' } },
      h('span', { class: 'k' }, 'Detail'), detailSeg,
      h('span', { class: 'k' }, 'Stiffness'), stiffSel,
      h('span', { class: 'k' }, 'Mass (kg)'), massIn,
      h('span', { class: 'k' }, 'Collision'), h('label', { class: 'chk' }, collision, 'Other things bump into it'),
      h('span', { class: 'k' }, 'Mounted'), attachSel,
      h('span', { class: 'k' }, ''), h('label', { class: 'chk' }, breakable, 'Can break off in a crash'),
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
      st.at = st.attach === 'none' ? { beams: [] } : attachBeams(st.fit, st.attach, anchors());
      const s = STIFFNESS[st.stiffness];
      const w = assignWeights(st.fit, st.mass, { spring: s.spring, damp: s.damp, attach: st.at.beams });
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
      if (st.attach === 'sel' && !anchors().length) stats.append(h('div', { class: 'warn' }, 'Select the vehicle nodes it bolts to (in the 3D view), or pick "nearest".'));
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
        showStructure(null);
        onDone?.();
        toast(`Added the mesh to ${S.activePart}. It bends with the "${grp}" nodes.`, 'ok', 7000);
        return;
      }
      compute();
      const slot = slotSel.getSlot();
      if (!slot) throw new Error('Pick a slot for the part.');
      const rootId = await root.getRoot();
      const r = await createPart({ pos: m.pos, idx: m.idx, uv: m.uv, name, title: titleIn.value.trim() || name, slot, rootId,
        fit: st.fit, attach: st.at.beams, mass: st.mass, stiffness: st.stiffness, collision: st.collision, breakable: st.breakable,
        color: rgb, paint: paint.checked, groups: m.groups });
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
      onCancel ? h('button', { onclick: () => { showStructure(null); onCancel(); } }, 'Cancel') : null,
      h('button', { class: 'primary', onclick: go }, icon('check', 15), 'Make part')));
  showFor();
  checkSlot();
  compute();
  return { update, refresh: compute, clear: () => showStructure(null) };
}

function centreOf() {
  const sc = S.scene;
  let n = 0;
  const c = [0, 0, 0];
  for (const x of sc.nodes) if (!x.del) { for (let k = 0; k < 3; k++) c[k] += x.p[k]; n++; }
  return n ? [0, c[1] / n, c[2] / n] : null;
}
