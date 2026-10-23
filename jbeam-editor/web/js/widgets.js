// Controls shared by the Garage and the Vehicle Builder: labelled sliders, sections and the engine chart.
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

