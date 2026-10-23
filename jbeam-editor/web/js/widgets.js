// Controls shared by the Garage, the Vehicle Builder and the Scrap Mechanic import: labelled sliders,
// settings fields, sections and the engine chart.
import { icon } from './icons.js';
import { h, fmt } from './util.js';

export function slider({ label, min, max, step, value, show, onInput, help, scale = null }) {
  const valEl = h('span', { class: 'val' }, show(value));
  const range = h('input', { type: 'range', min, max, step, value });
  const box = h('div', { class: 'slider' },
    h('div', { class: 'top' }, h('span', { class: 'name' }, label), valEl),
    range,
    scale ? h('div', { class: 'scale' }, h('span', {}, scale[0]), h('span', {}, scale[1])) : null,
    help ? h('div', { class: 'help' }, help) : null);
  range.addEventListener('input', () => { valEl.textContent = show(+range.value); onInput(+range.value, false); });
  range.addEventListener('change', () => onInput(+range.value, true));
  range.addEventListener('keydown', e => e.stopPropagation());
  return box;
}

export function section(title, ic, text, ...body) {
  return h('div', { class: 'gsec' }, h('h3', {}, ic ? icon(ic, 16) : null, title), text ? h('p', {}, text) : null, ...body);
}

// torque (solid) and power (dashed) curves, stock in grey and tuned in orange
export function curveChart(curve, power, rpm, maxRPM) {
  const W = 300, H = 140, P = 26;
  const ns = 'http://www.w3.org/2000/svg';
  const svg = document.createElementNS(ns, 'svg');
  svg.setAttribute('viewBox', `0 0 ${W} ${H}`);
  svg.setAttribute('width', '100%');
  svg.style.background = '#0f1217';
  svg.style.borderRadius = '8px';
  svg.style.marginTop = '6px';
  if (!curve.length) return svg;
  const maxR = Math.max(curve[curve.length - 1][0], (maxRPM || 0) + Math.max(0, rpm));
  const tq = (r, list, f) => { for (let i = 1; i < list.length; i++) if (list[i][0] >= r) { const [r0, t0] = list[i - 1], [r1, t1] = list[i]; return (t0 + (t1 - t0) * (r - r0) / Math.max(1, r1 - r0)) * f; } return list[list.length - 1][1] * f; };
  const maxT = Math.max(...curve.map(c => c[1])) * Math.max(1, power) * 1.08;
  const maxP = Math.max(...curve.map(c => c[1] * c[0])) * Math.max(1, power) * 1.08;
  const X = r => P + (W - P - 6) * r / maxR, Yt = t => H - 16 - (H - 26) * t / maxT, Yp = p => H - 16 - (H - 26) * p / maxP;
  const path = (f, lim, kind) => {
    let d = '';
    for (let i = 0; i <= 60; i++) {
      const r = lim * i / 60;
      const t = tq(r, curve, f);
      d += (i ? 'L' : 'M') + X(r).toFixed(1) + ',' + (kind === 't' ? Yt(t) : Yp(t * r)).toFixed(1);
    }
    return d;
  };
  const line = (d, color, dash) => {
    const p = document.createElementNS(ns, 'path');
    p.setAttribute('d', d); p.setAttribute('fill', 'none'); p.setAttribute('stroke', color); p.setAttribute('stroke-width', '2');
    if (dash) p.setAttribute('stroke-dasharray', '4 3');
    svg.append(p);
  };
  const lim0 = Math.min(maxR, maxRPM || maxR), lim1 = Math.min(maxR, (maxRPM || maxR) + rpm);
  line(path(1, lim0, 't'), '#5f6878'); line(path(1, lim0, 'p'), '#5f6878', true);
  line(path(power, lim1, 't'), '#f29a2e'); line(path(power, lim1, 'p'), '#f29a2e', true);
  const txt = (x, y, s, c = '#8d97a8') => { const t = document.createElementNS(ns, 'text'); t.setAttribute('x', x); t.setAttribute('y', y); t.setAttribute('fill', c); t.setAttribute('font-size', '9'); t.textContent = s; svg.append(t); };
  txt(P, H - 4, '0'); txt(W - 46, H - 4, fmt(maxR, 0) + ' rpm');
  txt(P + 4, 12, '— torque   - - power', '#8d97a8');
  return svg;
}


// a setting's value in the units people read (metric, or imperial when asked)
export function valueText(f, v, imperial = false) {
  const u = f.unit;
  if (f.display === 'percent') return fmt(v * 100, 0) + ' %';
  if (u === 'm') {
    if (!imperial) return fmt(v, 2) + ' m';
    const inches = v / 0.0254;
    return inches >= 36 ? `${Math.floor(inches / 12)} ft ${fmt(inches % 12, 1)} in` : fmt(inches, 1) + ' in';
  }
  if (u === 'kg') return imperial ? fmt(v * 2.20462, 0) + ' lb' : fmt(v, 0) + ' kg';
  if (u === 'km/h') return imperial ? fmt(v / 1.609, 0) + ' mph' : fmt(v, 0) + ' km/h';
  if (u === 'km') return imperial ? fmt(v / 1.609, 0) + ' mi' : fmt(v, 0) + ' km';
  if (u === 'L') return imperial ? fmt(v / 3.785, 1) + ' gal' : fmt(v, 0) + ' L';
  if (u === 'in') return fmt(v, 1) + '"';
  if (u === 'hp') return fmt(v, 0) + ' hp' + (imperial ? '' : ` (${fmt(v * 0.7457, 0)} kW)`);
  if (u === '°') return fmt(v, f.step < 1 ? 1 : 0) + '°';
  if (u === 'Hz') return fmt(v, 2) + ' Hz';
  if (u === '×') return fmt(v, 2) + '×';
  if (!u) return fmt(v, 2);
  return fmt(v, f.step >= 1 ? 0 : 2) + ' ' + u;
}

// does a setting mean anything with these values? (fields with "when": {key: value} only do when they match)
export function applies(f, values) {
  return !f.when || Object.entries(f.when).every(([k, v]) => values[k] === v);
}

// what drives the wheels, in the words of a vehicle's numbers (stats from the Builder or the importer)
export function torqueText(st) {
  return st.electric ? `${fmt(st.torqueNm, 0)} Nm from standstill` : `${fmt(st.torqueNm, 0)} Nm at ${st.torqueRPM} rpm`;
}
export function gearsText(st) {
  return st.electric ? `one reduction, ${fmt(st.finalDrive, 2)}:1` : `${st.gears.length} + final drive ${fmt(st.finalDrive, 2)}`;
}
export function batteryRows(st, imperial = false) {
  if (!st.electric) return [];
  const kg = st.batteryMass ? ` (${valueText({ unit: 'kg' }, st.batteryMass, imperial)})` : '';
  return [['Battery', `${fmt(st.battery, 0)} kWh${kg}`], ['Range', `about ${valueText({ unit: 'km' }, st.range, imperial)} at ${valueText({ unit: 'km/h' }, 100, imperial)}`]];
}

// one setting from a schema field ({type: range|choice|bool|color, ...}); set(value, done) stores it
export function fieldControl(f, value, set, { imperial = false, show = null, rerender = null, swatches = null } = {}) {
  const text = show || (x => valueText(f, x, imperial));
  if (f.type === 'range') {
    return slider({
      label: f.label, min: f.min, max: f.max, step: f.step, value, show: text,
      scale: f.lowLabel ? [f.lowLabel, f.highLabel] : null, help: f.help,
      onInput: (x, done) => set(+x, done),
    });
  }
  if (f.type === 'choice') {
    const wrap = h('div', { class: 'field' }, h('div', { class: 'name' }, f.label));
    if (f.options.length <= 3) {
      const seg = h('div', { class: 'seg wrap' });
      for (const [val, label] of f.options) {
        seg.append(h('button', { class: value === val ? 'active' : '', onclick: () => { set(val, true); rerender?.(); } }, label));
      }
      wrap.append(seg);
    } else {
      const sel = h('select', {}, ...f.options.map(([val, label]) => h('option', { value: val, selected: value === val }, label)));
      sel.addEventListener('change', () => set(sel.value, true));
      wrap.append(sel);
    }
    if (f.help) wrap.append(h('div', { class: 'help' }, f.help));
    return wrap;
  }
  if (f.type === 'bool') {
    const cb = h('input', { type: 'checkbox', checked: !!value });
    cb.addEventListener('change', () => set(cb.checked, true));
    return h('label', { class: 'chk field' }, cb, f.label);
  }
  if (f.type === 'color') {
    const pick = h('input', { type: 'color', value });
    pick.addEventListener('input', () => set(pick.value, false));
    pick.addEventListener('change', () => { set(pick.value, true); rerender?.(); });
    const sw = h('div', { class: 'swatches' });
    for (const [c, name] of swatches || []) {
      sw.append(h('button', { class: 'swatch' + (value === c ? ' active' : ''), title: name, style: { background: c },
        onclick: () => { set(c, true); rerender?.(); } }));
    }
    return h('div', { class: 'field' }, h('div', { class: 'name' }, f.label), h('div', { class: 'toggle-row' }, pick, h('span', { class: 'muted' }, value)), sw);
  }
  return null;
}
