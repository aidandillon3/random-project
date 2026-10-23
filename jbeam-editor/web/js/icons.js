// Line icons (24×24, drawn with the current text colour). icon('wrench') returns an <svg> element.
const P = {
  home: 'M3 11l9-8 9 8M5 9.5V21h5v-6h4v6h5V9.5',
  wrench: 'M14.7 6.3a4 4 0 0 0-5.4 5.4L3 18l3 3 6.3-6.3a4 4 0 0 0 5.4-5.4l-2.6 2.6-2.4-.6-.6-2.4z',
  car: 'M3 15l1.5-5A2 2 0 0 1 6.4 8.5h11.2a2 2 0 0 1 1.9 1.5L21 15v3h-2.5M5.5 18H3v-3h18M7 18a2 2 0 1 0 4 0 2 2 0 1 0-4 0M13 18a2 2 0 1 0 4 0 2 2 0 1 0-4 0',
  build: 'M2 20h20M5 20V9l7-5 7 5v11M9 20v-6h6v6',
  cube: 'M12 2l9 5v10l-9 5-9-5V7zM12 12l9-5M12 12L3 7M12 12v10',
  import: 'M12 3v12M7 10l5 5 5-5M4 17v3h16v-3',
  blocks: 'M3 13h8v8H3zM13 13h8v8h-8zM8 3h8v8H8z',
  nodes: 'M5 6a2 2 0 1 0 4 0 2 2 0 1 0-4 0M15 6a2 2 0 1 0 4 0 2 2 0 1 0-4 0M10 18a2 2 0 1 0 4 0 2 2 0 1 0-4 0M9 6h6M8.2 7.6l2.6 8.8M15.8 7.6l-2.6 8.8',
  parts: 'M4 4h7v7H4zM13 4h7v7h-7zM4 13h7v7H4zM16.5 13v7M13 16.5h7',
  sliders: 'M4 6h10M18 6h2M4 12h4M12 12h8M4 18h12M20 18h0M14 4v4M8 10v4M16 16v4',
  settings: 'M12 15a3 3 0 1 0 0-6 3 3 0 0 0 0 6zM19.4 15a1.7 1.7 0 0 0 .3 1.8l.1.1a2 2 0 1 1-2.8 2.8l-.1-.1a1.7 1.7 0 0 0-1.8-.3 1.7 1.7 0 0 0-1 1.5V21a2 2 0 1 1-4 0v-.1a1.7 1.7 0 0 0-1.1-1.5 1.7 1.7 0 0 0-1.8.3l-.1.1a2 2 0 1 1-2.8-2.8l.1-.1a1.7 1.7 0 0 0 .3-1.8 1.7 1.7 0 0 0-1.5-1H3a2 2 0 1 1 0-4h.1a1.7 1.7 0 0 0 1.5-1.1 1.7 1.7 0 0 0-.3-1.8l-.1-.1a2 2 0 1 1 2.8-2.8l.1.1a1.7 1.7 0 0 0 1.8.3H9a1.7 1.7 0 0 0 1-1.5V3a2 2 0 1 1 4 0v.1a1.7 1.7 0 0 0 1 1.5 1.7 1.7 0 0 0 1.8-.3l.1-.1a2 2 0 1 1 2.8 2.8l-.1.1a1.7 1.7 0 0 0-.3 1.8V9a1.7 1.7 0 0 0 1.5 1H21a2 2 0 1 1 0 4h-.1a1.7 1.7 0 0 0-1.5 1z',
  help: 'M12 22a10 10 0 1 0 0-20 10 10 0 0 0 0 20zM9.1 9a3 3 0 0 1 5.8 1c0 2-3 3-3 3M12 17h0',
  undo: 'M9 14L4 9l5-5M4 9h10.5a5.5 5.5 0 0 1 0 11H11',
  redo: 'M15 14l5-5-5-5M20 9H9.5a5.5 5.5 0 0 0 0 11H13',
  save: 'M19 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h11l5 5v11a2 2 0 0 1-2 2zM17 21v-8H7v8M7 3v5h8',
  eye: 'M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8zM12 15a3 3 0 1 0 0-6 3 3 0 0 0 0 6z',
  eyeOff: 'M17.9 17.9A10.1 10.1 0 0 1 12 20c-7 0-11-8-11-8a18.5 18.5 0 0 1 5.1-5.9M9.9 4.2A9.1 9.1 0 0 1 12 4c7 0 11 8 11 8a18.5 18.5 0 0 1-2.2 3.2M14.1 14.1a3 3 0 1 1-4.2-4.2M1 1l22 22',
  search: 'M11 19a8 8 0 1 0 0-16 8 8 0 0 0 0 16zM21 21l-4.3-4.3',
  plus: 'M12 5v14M5 12h14',
  minus: 'M5 12h14',
  trash: 'M3 6h18M8 6V4h8v2M19 6l-1 14H6L5 6M10 11v6M14 11v6',
  wheel: 'M12 22a10 10 0 1 0 0-20 10 10 0 0 0 0 20zM12 15a3 3 0 1 0 0-6 3 3 0 0 0 0 6zM12 2v7M12 15v7M2 12h7M15 12h7',
  lift: 'M7 21V8M7 8l-3 3M7 8l3 3M17 21V8M17 8l-3 3M17 8l3 3M3 3h18',
  engine: 'M7 7h7l2 2h3v3h2v4h-2v3h-4l-2 2H9v-3H6v-3H3v-4h3V9h1zM10 4h4M12 4v3',
  paint: 'M19 11l-8-8-8.5 8.5a2 2 0 0 0 0 2.8l5.2 5.2a2 2 0 0 0 2.8 0zM5 2l5 5M2 13h15M22 20a2 2 0 1 1-4 0c0-1.6 2-4 2-4s2 2.4 2 4z',
  weight: 'M6 8h12l2 13H4zM9 8a3 3 0 1 1 6 0',
  brake: 'M12 21a9 9 0 1 0 0-18 9 9 0 0 0 0 18zM12 15a3 3 0 1 0 0-6 3 3 0 0 0 0 6zM18.5 7.5l-3 2',
  gear: 'M12 16a4 4 0 1 0 0-8 4 4 0 0 0 0 8zM12 2v3M12 19v3M2 12h3M19 12h3M4.9 4.9l2.1 2.1M17 17l2.1 2.1M4.9 19.1L7 17M17 7l2.1-2.1',
  check: 'M20 6L9 17l-5-5',
  warning: 'M10.3 3.9L1.8 18a2 2 0 0 0 1.7 3h17a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0zM12 9v4M12 17h0',
  error: 'M12 22a10 10 0 1 0 0-20 10 10 0 0 0 0 20zM15 9l-6 6M9 9l6 6',
  export: 'M12 15V3M7 8l5-5 5 5M4 15v6h16v-6',
  folder: 'M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z',
  play: 'M6 4l14 8-14 8z',
  pause: 'M7 4h4v16H7zM13 4h4v16h-4z',
  magnet: 'M6 3v8a6 6 0 0 0 12 0V3h-4v8a2 2 0 0 1-4 0V3zM6 7h4M14 7h4',
  grid: 'M3 3h18v18H3zM3 9h18M3 15h18M9 3v18M15 3v18',
  mirror: 'M12 3v18M8 7L3 12l5 5M16 7l5 5-5 5',
  box: 'M21 8l-9-5-9 5v8l9 5 9-5zM3 8l9 5 9-5M12 13v8',
  cylinder: 'M4 6c0-1.7 3.6-3 8-3s8 1.3 8 3-3.6 3-8 3-8-1.3-8-3zM4 6v12c0 1.7 3.6 3 8 3s8-1.3 8-3V6',
  sphere: 'M12 22a10 10 0 1 0 0-20 10 10 0 0 0 0 20zM2 12c0 2.2 4.5 4 10 4s10-1.8 10-4M12 2c-2.5 2.7-3.8 6.3-3.8 10s1.3 7.3 3.8 10',
  cone: 'M12 3L4 18c0 1.7 3.6 3 8 3s8-1.3 8-3zM4 18c0-1.7 3.6-3 8-3s8 1.3 8 3',
  wedge: 'M3 19h18L3 7zM3 7v12',
  tube: 'M8 6c0-1.7 1.8-3 4-3s4 1.3 4 3-1.8 3-4 3-4-1.3-4-3zM8 6v12c0 1.7 1.8 3 4 3s4-1.3 4-3V6',
  plane: 'M2 16l6-8h14l-6 8z',
  move: 'M12 2v20M2 12h20M12 2l-3 3M12 2l3 3M12 22l-3-3M12 22l3-3M2 12l3-3M2 12l3 3M22 12l-3-3M22 12l-3 3',
  rotate: 'M21 12a9 9 0 1 1-3-6.7L21 8M21 3v5h-5',
  scale: 'M21 3l-7 7M21 3h-6M21 3v6M3 21l7-7M3 21h6M3 21v-6',
  extrude: 'M4 14h16v6H4zM12 12V3M8 7l4-4 4 4',
  select: 'M4 3l7 17 2-7 7-2z',
  node: 'M12 16a4 4 0 1 0 0-8 4 4 0 0 0 0 8zM12 2v4M12 18v4M2 12h4M18 12h4',
  beam: 'M5 19L19 5M5 21a2 2 0 1 0 0-4 2 2 0 0 0 0 4zM19 7a2 2 0 1 0 0-4 2 2 0 0 0 0 4z',
  triangle: 'M12 4l9 16H3z',
  layers: 'M12 2l10 5-10 5L2 7zM2 17l10 5 10-5M2 12l10 5 10-5',
  camera: 'M23 19a2 2 0 0 1-2 2H3a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h4l2-3h6l2 3h4a2 2 0 0 1 2 2zM12 17a4 4 0 1 0 0-8 4 4 0 0 0 0 8z',
  fit: 'M3 8V3h5M21 8V3h-5M3 16v5h5M21 16v5h-5',
  close: 'M18 6L6 18M6 6l12 12',
  chevronRight: 'M9 18l6-6-6-6',
  chevronDown: 'M6 9l6 6 6-6',
  chevronLeft: 'M15 18l-6-6 6-6',
  more: 'M5 12h0M12 12h0M19 12h0',
  info: 'M12 22a10 10 0 1 0 0-20 10 10 0 0 0 0 20zM12 16v-4M12 8h0',
  sparkle: 'M12 3l1.9 5.8L20 11l-6.1 2.2L12 19l-1.9-5.8L4 11l6.1-2.2zM19 3v4M17 5h4',
  copy: 'M9 9h11v11H9zM5 15H4V4h11v1',
  link: 'M10 13a5 5 0 0 0 7.5.5l3-3a5 5 0 0 0-7-7l-1.7 1.7M14 11a5 5 0 0 0-7.5-.5l-3 3a5 5 0 0 0 7 7l1.7-1.7',
  physics: 'M12 2v6M12 22v-4M5 12a7 7 0 0 0 14 0M8 5l4 3 4-3M3 22h18',
  scrap: 'M4 4h6v6H4zM14 4h6v6h-6zM4 14h6v6H4zM14 14h6v6h-6zM10 7h4M7 10v4M17 10v4M10 17h4',
  spring: 'M8 2h8M8 22h8M12 2v2l-5 2 10 3-10 3 10 3-10 3 5 2v2',
  ruler: 'M2 16L16 2l6 6L8 22zM6 12l2 2M9 9l2 2M12 6l2 2',
  arrowRight: 'M5 12h14M13 6l6 6-6 6',
  refresh: 'M23 4v6h-6M1 20v-6h6M3.5 9a9 9 0 0 1 14.9-3.4L23 10M1 14l4.6 4.4A9 9 0 0 0 20.5 15',
  window: 'M3 4h18v16H3zM3 9h18',
  file: 'M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8zM14 2v6h6',
  text: 'M4 7V4h16v3M9 20h6M12 4v16',
  target: 'M12 22a10 10 0 1 0 0-20 10 10 0 0 0 0 20zM12 18a6 6 0 1 0 0-12 6 6 0 0 0 0 12zM12 14a2 2 0 1 0 0-4 2 2 0 0 0 0 4z',
};

export function icon(name, size = 18, cls = '') {
  const ns = 'http://www.w3.org/2000/svg';
  const svg = document.createElementNS(ns, 'svg');
  svg.setAttribute('viewBox', '0 0 24 24');
  svg.setAttribute('width', size);
  svg.setAttribute('height', size);
  svg.setAttribute('fill', 'none');
  svg.setAttribute('stroke', 'currentColor');
  svg.setAttribute('stroke-width', '2');
  svg.setAttribute('stroke-linecap', 'round');
  svg.setAttribute('stroke-linejoin', 'round');
  svg.setAttribute('aria-hidden', 'true');
  svg.classList.add('ico');
  if (cls) svg.classList.add(...cls.split(' '));
  const path = document.createElementNS(ns, 'path');
  path.setAttribute('d', P[name] || P.info);
  svg.append(path);
  return svg;
}

// replace <i data-icon="name"></i> placeholders in static HTML
export function hydrateIcons(root = document) {
  for (const el of root.querySelectorAll('i[data-icon]')) {
    el.replaceWith(icon(el.dataset.icon, +(el.dataset.size || 18)));
  }
}

export const ICON_NAMES = Object.keys(P);
