// Test meshes: [positions Float32Array, indices Uint32Array]
export function box(cx, cy, cz, sx, sy, sz, div = 1) {
  const pos = [], idx = [];
  const face = (o, u, v) => {
    const base = pos.length / 3;
    for (let j = 0; j <= div; j++) for (let i = 0; i <= div; i++) {
      pos.push(o[0] + u[0] * i / div + v[0] * j / div, o[1] + u[1] * i / div + v[1] * j / div, o[2] + u[2] * i / div + v[2] * j / div);
    }
    for (let j = 0; j < div; j++) for (let i = 0; i < div; i++) {
      const a = base + j * (div + 1) + i, b = a + 1, c = a + div + 1, d = c + 1;
      idx.push(a, b, d, a, d, c);
    }
  };
  const x0 = cx - sx / 2, x1 = cx + sx / 2, y0 = cy - sy / 2, y1 = cy + sy / 2, z0 = cz - sz / 2, z1 = cz + sz / 2;
  face([x0, y0, z0], [0, sy, 0], [sx, 0, 0]);   // bottom (-z)
  face([x0, y0, z1], [sx, 0, 0], [0, sy, 0]);   // top (+z)
  face([x0, y0, z0], [sx, 0, 0], [0, 0, sz]);   // front (-y)
  face([x0, y1, z0], [0, 0, sz], [sx, 0, 0]);   // back (+y)
  face([x0, y0, z0], [0, 0, sz], [0, sy, 0]);   // right (-x)
  face([x1, y0, z0], [0, sy, 0], [0, 0, sz]);   // left (+x)
  return [Float32Array.from(pos), Uint32Array.from(idx)];
}

export function sphere(r, seg = 24, rings = 16, c = [0, 0, 0]) {
  const pos = [], idx = [];
  for (let j = 0; j <= rings; j++) {
    const v = j / rings * Math.PI;
    for (let i = 0; i <= seg; i++) {
      const u = i / seg * Math.PI * 2;
      pos.push(c[0] + r * Math.sin(v) * Math.cos(u), c[1] + r * Math.sin(v) * Math.sin(u), c[2] + r * Math.cos(v));
    }
  }
  for (let j = 0; j < rings; j++) for (let i = 0; i < seg; i++) {
    const a = j * (seg + 1) + i, b = a + seg + 1;
    idx.push(a, b, a + 1, b, b + 1, a + 1);
  }
  return [Float32Array.from(pos), Uint32Array.from(idx)];
}

export function plane(sx, sy, z = 0.5, div = 6) {
  const pos = [], idx = [];
  for (let j = 0; j <= div; j++) for (let i = 0; i <= div; i++) pos.push(-sx / 2 + sx * i / div, -sy / 2 + sy * j / div, z);
  for (let j = 0; j < div; j++) for (let i = 0; i < div; i++) {
    const a = j * (div + 1) + i;
    idx.push(a, a + 1, a + div + 2, a, a + div + 2, a + div + 1);
  }
  return [Float32Array.from(pos), Uint32Array.from(idx)];
}
