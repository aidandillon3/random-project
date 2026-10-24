// App state shared by every module, plus a tiny event bus.

export const S = {
  vehicles: [],
  model: null,          // model folder name, e.g. "offroadsemi"
  summary: null,        // /api/open result
  scene: null,          // /api/build result (mutated locally by edits)
  config: null,         // current config {mainPartName, parts, vars, ...}
  configName: null,
  sel: { nodes: new Set(), beams: new Set(), tris: new Set() },
  tool: 'select',
  gizmoMode: 'translate', // what the handles on selected nodes do: translate, scale or rotate
  nodePick: false,      // picking vehicle nodes for something else (what a new part bolts to)
  mirror: false,
  snap: 0,
  activePart: null,     // part name new rows go into
  pinActive: false,     // keep activePart when clicking nodes of other parts
  hiddenParts: new Set(),
  onlyActive: false,
  showStability: false,
  undo: [],
  redo: [],
  pending: [],          // ops not yet written to disk
  appConfig: {},
  appState: null,       // /api/state result
  mode: null,           // garage, parts, tuning, structure, model or builder
  lookMode: true,       // a mode that only looks at the vehicle (no structure tools)
};

// actions main.js provides to the other modules (save, rebuild, reload)
export const A = {};

const listeners = {};
export function on(ev, fn) { (listeners[ev] = listeners[ev] || []).push(fn); }
export function off(ev, fn) { listeners[ev] = (listeners[ev] || []).filter(f => f !== fn); }
export function emit(ev, ...args) { for (const fn of listeners[ev] || []) fn(...args); }

export function clearSelection() {
  S.sel.nodes.clear(); S.sel.beams.clear(); S.sel.tris.clear();
}
export function selectionEmpty() {
  return !S.sel.nodes.size && !S.sel.beams.size && !S.sel.tris.size;
}
