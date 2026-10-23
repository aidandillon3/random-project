// Small DOM helpers, dialogs and messages.

export function h(tag, attrs = {}, ...children) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === undefined || v === null || v === false) continue;
    if (k === 'class') el.className = v;
    else if (k === 'style' && typeof v === 'object') Object.assign(el.style, v);
    else if (k.startsWith('on') && typeof v === 'function') el.addEventListener(k.slice(2), v);
    else if (k === 'html') el.innerHTML = v;
    else if (v === true) el.setAttribute(k, '');
    else el.setAttribute(k, v);
  }
  for (const c of children.flat(Infinity)) {
    if (c === null || c === undefined || c === false) continue;
    el.append(c instanceof Node ? c : document.createTextNode(String(c)));
  }
  return el;
}

export const $ = (sel, root = document) => root.querySelector(sel);
export const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];

// a short message at the top of the 3D view: at most three at a time, the same one is not repeated,
// and a click puts it away
export function toast(msg, kind = '', ms = 4500) {
  const box = $('#toasts');
  const text = typeof msg === 'string' ? msg : null;
  const same = text && [...box.children].find(x => x.dataset.text === text);
  if (same) same.remove();
  const t = h('div', { class: 'toast ' + kind, title: 'Click to close' }, msg);
  if (text) t.dataset.text = text;
  t.addEventListener('click', () => t.remove());
  box.append(t);
  while (box.children.length > 3) box.firstElementChild.remove();
  setTimeout(() => t.remove(), ms);
  return t;
}

let modalOnClose = null;
export function openModal(title, body, { wide = false, onClose = null } = {}) {
  $('#modalTitle').textContent = title;
  const mb = $('#modalBody');
  mb.innerHTML = '';
  mb.append(body);
  $('#modal').classList.toggle('wide', wide);
  $('#modal').classList.remove('hidden');
  modalOnClose = onClose;
}
export function closeModal() {
  $('#modal').classList.add('hidden');
  const f = modalOnClose;
  modalOnClose = null;
  if (f) f();
}
export function modalOpen() { return !$('#modal').classList.contains('hidden'); }

export function confirmBox(title, text, okLabel = 'OK', danger = false) {
  return new Promise(resolve => {
    let done = false;
    const finish = v => { if (!done) { done = true; resolve(v); closeModal(); } };
    const body = h('div', {},
      h('p', {}, text),
      h('div', { class: 'actions' },
        h('button', { onclick: () => finish(false) }, 'Cancel'),
        h('button', { class: danger ? 'danger' : 'primary', onclick: () => finish(true) }, okLabel)));
    openModal(title, body, { onClose: () => { if (!done) { done = true; resolve(false); } } });
  });
}

export function setLoading(on, text = 'Loading…') {
  $('#loading').classList.toggle('hidden', !on);
  $('#loadingText').textContent = text;
}

export function fmt(v, digits = 3) {
  if (v === null || v === undefined) return '';
  if (typeof v === 'number') {
    if (!isFinite(v)) return v > 0 ? 'FLT_MAX' : '-FLT_MAX';
    if (Math.abs(v) >= 1e5) return Math.round(v).toLocaleString('en-US');
    return String(+v.toFixed(digits));
  }
  if (typeof v === 'object') return JSON.stringify(v);
  return String(v);
}

export function num(v, d = null) {
  if (typeof v === 'number') return v;
  if (v === 'FLT_MAX') return Infinity;
  if (v === 'MINUS_FLT_MAX') return -Infinity;
  return d;
}

export function debounce(fn, ms) {
  let t = null;
  return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); };
}

// numeric input that commits on Enter/blur; returns the element
export function numInput(value, onCommit, { step = 'any', digits = 4, width = null } = {}) {
  const el = h('input', { type: 'number', step, value: value === null || value === undefined ? '' : +(+value).toFixed(digits) });
  if (width) el.style.width = width;
  let last = el.value;
  const commit = () => {
    if (el.value === last) return;
    last = el.value;
    const v = parseFloat(el.value);
    if (!isNaN(v)) onCommit(v);
  };
  el.addEventListener('change', commit);
  el.addEventListener('keydown', e => { if (e.key === 'Enter') { commit(); el.blur(); } e.stopPropagation(); });
  return el;
}

export function textInput(value, onCommit, attrs = {}) {
  const el = h('input', Object.assign({ type: 'text', value: value ?? '' }, attrs));
  let last = el.value;
  const commit = () => { if (el.value !== last) { last = el.value; onCommit(el.value); } };
  el.addEventListener('change', commit);
  el.addEventListener('keydown', e => { if (e.key === 'Enter') { commit(); el.blur(); } e.stopPropagation(); });
  return el;
}

// a small drop-down menu under a button: items are [label, fn] or null for a line
export function popupMenu(anchor, items) {
  document.querySelector('.popmenu')?.remove();
  const m = h('div', { class: 'popmenu' });
  for (const it of items) {
    if (!it) { m.append(h('hr')); continue; }
    const [label, fn, disabled] = it;
    m.append(h('button', { disabled, onclick: () => { m.remove(); fn(); } }, label));
  }
  document.body.append(m);
  const r = anchor.getBoundingClientRect();
  m.style.left = Math.min(r.left, window.innerWidth - m.offsetWidth - 8) + 'px';
  m.style.top = (r.bottom + m.offsetHeight > window.innerHeight - 8 ? r.top - m.offsetHeight - 2 : r.bottom + 2) + 'px';
  const close = e => { if (!m.contains(e.target)) { m.remove(); document.removeEventListener('mousedown', close, true); } };
  setTimeout(() => document.addEventListener('mousedown', close, true), 0);
}

// a path short enough for a message: from the mods folder on, or the last few folders
export function shortPath(p) {
  const parts = String(p).split(/[\\/]/);
  const i = parts.lastIndexOf('mods');
  return (i >= 0 ? parts.slice(i) : parts.slice(-3)).join('\\');
}

// ask for a line of text in a small dialog; resolves to the text or null
export function askText(title, label, value = '', okLabel = 'OK') {
  return new Promise(resolve => {
    let done = false;
    const input = h('input', { type: 'text', value, style: { width: '100%' } });
    const finish = v => { if (!done) { done = true; resolve(v); closeModal(); } };
    input.addEventListener('keydown', e => {
      e.stopPropagation();
      if (e.key === 'Enter') finish(input.value.trim() || null);
      if (e.key === 'Escape') finish(null);
    });
    openModal(title, h('div', {}, h('p', { class: 'note' }, label), input,
      h('div', { class: 'actions' }, h('button', { onclick: () => finish(null) }, 'Cancel'),
        h('button', { class: 'primary', onclick: () => finish(input.value.trim() || null) }, okLabel))),
    { onClose: () => { if (!done) { done = true; resolve(null); } } });
    setTimeout(() => { input.focus(); input.select(); }, 30);
  });
}
