// Talking to the local Python server.

async function post(path, body) {
  const r = await fetch(path.replace(/^\//, ''), {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body || {}),
  });
  const ct = r.headers.get('Content-Type') || '';
  if (ct.includes('application/json')) {
    const data = await r.json();
    if (!r.ok) throw new Error(data.error || ('HTTP ' + r.status));
    return data;
  }
  if (!r.ok) throw new Error('HTTP ' + r.status);
  return r.arrayBuffer();
}

export const api = {
  state: () => post('/api/state'),
  config: cfg => post('/api/config', cfg),
  open: (model, reload = false) => post('/api/open', { model, reload }),
  build: (model, config, configName, garage) => post('/api/build', { model, config, configName, garage }),
  garageSave: body => post('/api/garage_save', body),
  builderSchema: () => post('/api/builder_schema'),
  builderPreview: (spec, have) => post('/api/builder_preview', { spec, have }),
  builderCreate: body => post('/api/builder_create', body),
  builderLoad: model => post('/api/builder_load', { model }),
  projects: () => post('/api/projects'),
  projectSave: (name, data) => post('/api/project_save', { name, data }),
  projectLoad: name => post('/api/project_load', { name }),
  projectDelete: name => post('/api/project_delete', { name }),
  save: (model, ops, config) => post('/api/save', { model, ops, config }),
  partText: (model, part) => post('/api/part_text', { model, part }),
  pcSave: (model, root, name, config) => post('/api/pc_save', { model, root, name, config }),
  check: (model, root) => post('/api/check', { model, root }),
  importMesh: body => post('/api/import_mesh', body),
  exportMod: (root, name) => post('/api/export', { root, name }),
  unpack: root => post('/api/unpack', { root }),
  reveal: path => post('/api/reveal', { path }),
  newPart: body => post('/api/new_part', body),
  newMod: (model, name) => post('/api/new_mod', { model, name }),
  shortcut: () => post('/api/shortcut'),
  installNative: start => post('/api/install_native', { start }),
  log: () => post('/api/log'),
  async meshes(model, names) {
    const buf = await post('/api/meshes', { model, names });
    const dv = new DataView(buf);
    const n = dv.getUint32(0, true);
    const head = JSON.parse(new TextDecoder().decode(new Uint8Array(buf, 4, n)));
    const base = 4 + n;
    const out = {};
    for (const [name, [po, pc, io, ic, file]] of Object.entries(head.meshes)) {
      out[name] = {
        pos: new Float32Array(buf, base + po, pc),
        idx: new Uint32Array(buf, base + io, ic),
        file,
      };
    }
    return { meshes: out, missing: head.missing };
  },
};
