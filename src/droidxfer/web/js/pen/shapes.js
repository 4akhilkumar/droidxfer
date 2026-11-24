// Hold the pen still at the end of a stroke to straighten it: line, rectangle or ellipse.

const dist = (a, b) => Math.hypot(a[0] - b[0], a[1] - b[1]);

function lerpPts(a, b, n, p) {
  const out = [];
  for (let i = 0; i <= n; i += 1) out.push([a[0] + ((b[0] - a[0]) * i) / n, a[1] + ((b[1] - a[1]) * i) / n, p]);
  return out;
}

export function recognizeShape(pts) {
  const n = pts.length;
  if (n < 6) return null;
  let len = 0;
  let x0 = Infinity;
  let y0 = Infinity;
  let x1 = -Infinity;
  let y1 = -Infinity;
  let psum = 0;
  for (let i = 0; i < n; i += 1) {
    const p = pts[i];
    if (i) len += dist(pts[i - 1], p);
    x0 = Math.min(x0, p[0]);
    y0 = Math.min(y0, p[1]);
    x1 = Math.max(x1, p[0]);
    y1 = Math.max(y1, p[1]);
    psum += p[2];
  }
  const pressure = Math.min(1, Math.max(0.45, psum / n));
  const diag = Math.hypot(x1 - x0, y1 - y0);
  if (diag < 25) return null;
  const first = pts[0];
  const last = pts[n - 1];
  const chord = dist(first, last);

  // straight line: every point close to the chord
  if (chord > 0.8 * diag) {
    let worst = 0;
    const dx = last[0] - first[0];
    const dy = last[1] - first[1];
    for (const p of pts) worst = Math.max(worst, Math.abs(dy * p[0] - dx * p[1] + last[0] * first[1] - last[1] * first[0]) / chord);
    if (worst / chord < 0.07) return { kind: 'line', pts: lerpPts(first, last, 24, pressure) };
    return null;
  }
  // closed shapes: the end comes back near the start
  if (chord > 0.25 * diag || len < 2 * diag) return null;
  const cx = (x0 + x1) / 2;
  const cy = (y0 + y1) / 2;
  const rx = (x1 - x0) / 2;
  const ry = (y1 - y0) / 2;
  if (rx < 8 || ry < 8) return null;
  let ellipseErr = 0;
  let rectErr = 0;
  for (const p of pts) {
    const v = Math.hypot((p[0] - cx) / rx, (p[1] - cy) / ry);
    ellipseErr += Math.abs(v - 1);
    const edge = Math.min(Math.abs(p[0] - x0), Math.abs(p[0] - x1), Math.abs(p[1] - y0), Math.abs(p[1] - y1));
    rectErr += edge / Math.min(rx, ry);
  }
  ellipseErr /= n;
  rectErr /= n;
  if (rectErr < 0.14 && rectErr < ellipseErr) {
    const corners = [[x0, y0], [x1, y0], [x1, y1], [x0, y1], [x0, y0]];
    const out = [];
    for (let i = 0; i < 4; i += 1) out.push(...lerpPts(corners[i], corners[i + 1], 12, pressure).slice(i ? 1 : 0));
    return { kind: 'rectangle', pts: out };
  }
  if (ellipseErr < 0.2) {
    const out = [];
    for (let i = 0; i <= 72; i += 1) {
      const a = (i / 72) * Math.PI * 2;
      out.push([cx + rx * Math.cos(a), cy + ry * Math.sin(a), pressure]);
    }
    return { kind: 'ellipse', pts: out };
  }
  return null;
}
