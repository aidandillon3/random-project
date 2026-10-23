// The home screen: what you want to do, recent vehicles and the vehicle gallery.
import { S, A } from './state.js';
import { icon } from './icons.js';
import { $, $$, h, toast } from './util.js';

const ACTIONS = [
  { id: 'garage', icon: 'wrench', title: 'Modify a vehicle', needs: 'vehicle',
    text: 'Lift kits, bigger wheels and tires, wider track, engine power and paint. Saved as your own version of the car.' },
  { id: 'builder', icon: 'build', title: 'Build a new vehicle', cls: 'blue',
    text: 'Pick a body style and shape it with sliders: length, wheelbase, track width, ride height, suspension and engine.' },
  { id: 'scrap', icon: 'scrap', title: 'Import from Scrap Mechanic', cls: 'blue',
    text: 'Turn a Scrap Mechanic creation into a drivable BeamNG vehicle, with its wheels, suspension and engine.' },
  { id: 'model', icon: 'cube', title: '3D Model Workshop', cls: 'green', needs: 'vehicle',
    text: 'Model a bumper, roof rack, light bar or body kit right here, then bolt it on with a fitted node structure.' },
  { id: 'import', icon: 'import', title: 'Import a 3D model', cls: 'green', needs: 'vehicle',
    text: 'Use an .obj, .glb, .stl or .dae file as a new part. The physics structure is shaped to fit the model.' },
  { id: 'structure', icon: 'nodes', title: 'Advanced JBeam editing', needs: 'vehicle',
    text: 'Edit nodes, beams, collision triangles and weights directly, with stability checks and mirror editing.' },
];
const GALLERY_TITLE = {
  garage: 'Pick a vehicle to modify', model: 'Pick the vehicle the part is for', import: 'Pick the vehicle to put the model on',
  structure: 'Pick a vehicle to edit',
};

let intent = 'garage';
let filter = 'all';

const store = {
  get(k, d = null) { try { const v = localStorage.getItem('jbe.' + k); return v === null ? d : JSON.parse(v); } catch { return d; } },
  set(k, v) { try { localStorage.setItem('jbe.' + k, JSON.stringify(v)); } catch { /* private window */ } },
};

export function remember(model) {
  const v = S.vehicles.find(x => x.model === model);
  const list = store.get('recent', []).filter(r => r.model !== model);
  list.unshift({ model, name: v?.name || model, t: Date.now() });
  store.set('recent', list.slice(0, 8));
}

export function initHome() {
  $('#vehSearch').addEventListener('input', renderGallery);
  $('#vehSearch').addEventListener('keydown', e => e.stopPropagation());
  for (const b of $$('#vehFilter button')) b.addEventListener('click', () => {
    filter = b.dataset.f;
    for (const x of $$('#vehFilter button')) x.classList.toggle('active', x === b);
    renderGallery();
  });
  $('#homeSettings').addEventListener('click', () => A.settings());
  $('#homeHelp').addEventListener('click', () => A.help());
  renderActions();
}

export function showHome(want) {
  if (want) intent = want;
  $('#home').classList.remove('hidden');
  document.body.classList.add('at-home');
  renderActions();
  renderStatus();
  renderRecent();
  renderGallery();
  setTimeout(() => $('#vehSearch').focus({ preventScroll: true }), 50);
}
export function hideHome() {
  $('#home').classList.add('hidden');
  document.body.classList.remove('at-home');
}
export function homeOpen() { return !$('#home').classList.contains('hidden'); }

function renderActions() {
  const box = $('#homeActions');
  box.innerHTML = '';
  for (const a of ACTIONS) {
    const b = h('button', { class: 'action ' + (a.cls || '') + (a.needs && a.id === intent ? ' chosen' : '') },
      h('span', { class: 'ic' }, icon(a.icon, 22)),
      h('span', {}, h('span', { class: 't' }, a.title), h('span', { class: 'd' }, a.text)));
    b.addEventListener('click', () => act(a));
    box.append(b);
  }
}

function act(a) {
  if (a.id === 'builder') return A.openBuilder();
  if (a.id === 'scrap') return A.openScrap();
  intent = a.id;
  $('#galleryTitle').textContent = GALLERY_TITLE[intent] || 'Pick a vehicle';
  renderActions();
  if (S.model && S.scene) {
    // a vehicle is already open: go straight there
    return A.openVehicle(S.model, { intent });
  }
  $('#galleryTitle').scrollIntoView({ behavior: 'smooth', block: 'start' });
  toast(GALLERY_TITLE[intent] + ' below.');
}

function renderStatus() {
  const box = $('#homeStatus');
  box.innerHTML = '';
  const st = S.appState || {};
  const pill = (ok, label, title) => h('span', { class: 'chip small ' + (ok ? 'good' : 'warn'), title },
    icon(ok ? 'check' : 'warning', 14), label);
  box.append(pill(st.gameFound, st.gameFound ? 'BeamNG found' : 'BeamNG not found',
    st.config?.game_folder || 'Set the game folder in Settings'));
  box.append(pill(st.userFound, st.userFound ? 'Mods folder found' : 'Mods folder not found',
    st.config?.user_folder || 'Set your BeamNG user folder in Settings'));
  if (!st.gameFound || !st.userFound) {
    box.append(h('button', { class: 'small secondary', onclick: () => A.settings() }, 'Set up folders'));
  }
}

function renderRecent() {
  const box = $('#homeRecent');
  box.innerHTML = '';
  const list = store.get('recent', []).filter(r => S.vehicles.some(v => v.model === r.model)).slice(0, 6);
  if (!list.length) return;
  const row = h('div', { class: 'recent-row' });
  for (const r of list) {
    row.append(h('button', { class: 'chip', title: 'Open ' + r.name, onclick: () => A.openVehicle(r.model, { intent }) },
      icon('car', 14), r.name));
  }
  box.append(h('h2', { class: 'home-q' }, 'Recent'), row);
}

function vehicleMatches(v, q) {
  if (filter === 'mine' && !v.editable) return false;
  if (filter === 'game' && v.editable) return false;
  if (!q) return true;
  return [v.name, v.brand, v.model, v.type].some(x => String(x || '').toLowerCase().includes(q));
}

export function renderGallery() {
  const grid = $('#vehGrid');
  grid.innerHTML = '';
  const q = $('#vehSearch').value.trim().toLowerCase();
  const list = (S.vehicles || []).filter(v => vehicleMatches(v, q));
  list.sort((a, b) => (b.editable - a.editable) || String(a.brand + a.name).localeCompare(String(b.brand + b.name), undefined, { sensitivity: 'base' }));
  if (!S.vehicles?.length) {
    grid.append(h('div', { class: 'empty', style: { gridColumn: '1 / -1' } },
      h('p', {}, 'No vehicles found yet.'),
      h('p', { class: 'note' }, 'Point the editor at your BeamNG.drive game folder and user folder, and every vehicle shows up here.'),
      h('button', { class: 'primary', onclick: () => A.settings() }, icon('settings', 16), 'Open Settings'),
      ' ',
      h('button', { onclick: () => A.openBuilder() }, icon('build', 16), 'Or build a new vehicle')));
    return;
  }
  if (!list.length) {
    grid.append(h('div', { class: 'empty', style: { gridColumn: '1 / -1' } }, 'Nothing matches.'));
    return;
  }
  for (const v of list) {
    const img = h('img', { loading: 'lazy', alt: '', src: 'api/thumb?model=' + encodeURIComponent(v.model),
      style: { width: '100%', height: '100%', objectFit: 'cover', display: 'block' } });
    const thumb = h('div', { class: 'thumb' }, img);
    img.addEventListener('error', () => { img.remove(); thumb.append(icon('car', 42)); });
    const card = h('button', { class: 'vcard', 'data-model': v.model, title: `${v.brand ? v.brand + ' ' : ''}${v.name} (${v.model})` },
      thumb,
      h('span', { class: 'meta' },
        h('span', { class: 'name' }, v.name || v.model),
        h('span', { class: 'sub' }, v.brand || v.type || v.model,
          v.builder ? h('span', { class: 'badge mine' }, 'Built') : v.scrap ? h('span', { class: 'badge mine' }, 'Scrap Mechanic')
            : v.editable ? h('span', { class: 'badge mine' }, 'My mod') : null)));
    card.addEventListener('click', () => A.openVehicle(v.model, { intent }));
    grid.append(card);
  }
}

export function homeIntent() { return intent; }
