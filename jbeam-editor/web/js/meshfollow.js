// How a model's vertices follow its nodes: each vertex is tied to its nearest nodes
// with moving-least-squares weights, so moving, turning or stretching all of them
// moves the model exactly the same way, and one moved node bends the model around
// it. No three.js here, so node can test it.

// the weights tying each vertex to its nearest nodes
export function bind(rest, pts, K = 12) {
  const n = rest.length / 3, k = Math.min(K, pts.length / 3);
  const nb = new Int32Array(n * k), coef = new Float32Array(n * k);
  const np = pts.length / 3;
  const dist = new Float64Array(k), order = new Int32Array(k);
  const w = new Float64Array(k), ph = new Float64Array(k * 3);
  for (let v = 0; v < n; v++) {
    const x = rest[v * 3], y = rest[v * 3 + 1], z = rest[v * 3 + 2];
    // the k nearest nodes, kept sorted as they are found
    let m = 0;
    for (let j = 0; j < np; j++) {
      const dx = pts[j * 3] - x, dy = pts[j * 3 + 1] - y, dz = pts[j * 3 + 2] - z;
      const d2 = dx * dx + dy * dy + dz * dz;
      if (m === k && d2 >= dist[k - 1]) continue;
      let a = m < k ? m++ : k - 1;
      while (a > 0 && dist[a - 1] > d2) { dist[a] = dist[a - 1]; order[a] = order[a - 1]; a--; }
      dist[a] = d2; order[a] = j;
    }
    let W = 0;
    const c = [0, 0, 0];
    for (let a = 0; a < k; a++) {
      const j = order[a];
      w[a] = 1 / (dist[a] + 1e-4);
      W += w[a];
      for (let q = 0; q < 3; q++) c[q] += w[a] * pts[j * 3 + q];
    }
    for (let q = 0; q < 3; q++) c[q] /= W;
    // M = sum w p^ p^T (+ a little so flat groups of nodes still work)
    const M = [0, 0, 0, 0, 0, 0, 0, 0, 0];
    for (let a = 0; a < k; a++) {
      const j = order[a];
      for (let q = 0; q < 3; q++) ph[a * 3 + q] = pts[j * 3 + q] - c[q];
      for (let r = 0; r < 3; r++) for (let q = 0; q < 3; q++) M[r * 3 + q] += w[a] * ph[a * 3 + r] * ph[a * 3 + q];
    }
    const tr = M[0] + M[4] + M[8];
    const reg = tr * 1e-3 + 1e-9;
    M[0] += reg; M[4] += reg; M[8] += reg;
    const Mi = inv3(M);
    const d = [x - c[0], y - c[1], z - c[2]];
    const dM = [d[0] * Mi[0] + d[1] * Mi[3] + d[2] * Mi[6], d[0] * Mi[1] + d[1] * Mi[4] + d[2] * Mi[7], d[0] * Mi[2] + d[1] * Mi[5] + d[2] * Mi[8]];
    for (let a = 0; a < k; a++) {
      nb[v * k + a] = order[a];
      coef[v * k + a] = w[a] / W + w[a] * (dM[0] * ph[a * 3] + dM[1] * ph[a * 3 + 1] + dM[2] * ph[a * 3 + 2]);
    }
  }
  return { nb, coef, k };
}

function inv3(m) {
  const [a, b, c, d, e, f, g, h, i] = m;
  const A = e * i - f * h, B = -(d * i - f * g), C = d * h - e * g;
  const det = a * A + b * B + c * C || 1e-30;
  return [A / det, -(b * i - c * h) / det, (b * f - c * e) / det,
    B / det, (a * i - c * g) / det, -(a * f - c * d) / det,
    C / det, -(a * h - b * g) / det, (a * e - b * d) / det];
}

// moved positions of the vertices: rest + the weighted moves of their nodes
export function apply(rest, b, moves, out) {
  const n = rest.length / 3, k = b.k;
  for (let v = 0; v < n; v++) {
    let x = rest[v * 3], y = rest[v * 3 + 1], z = rest[v * 3 + 2];
    for (let a = 0; a < k; a++) {
      const j = b.nb[v * k + a], c = b.coef[v * k + a];
      x += c * moves[j * 3]; y += c * moves[j * 3 + 1]; z += c * moves[j * 3 + 2];
    }
    out[v * 3] = x; out[v * 3 + 1] = y; out[v * 3 + 2] = z;
  }
  return out;
}
