// Path editor: fine-tune recorded paths and race tracks on site.
// Overlay of the control page (the controller, keyboard jog and E-STOP keep
// working). Uses globals from app.js: $, send, status, config, TOKEN,
// showError, escapeHtml, fmtPos.
//
// A recording holds a point every 50 ms. The editor works on a reduced set of
// control points; the mount replays a monotone cubic (PCHIP) through them, the
// same curve drawn here, so what is shown is what the head will do.
"use strict";

const ED = {
  open: false, name: null, rec: null, mode: "continuous", natural: false,
  orig: [], pts: [], axes: [], sel: -1, undo: [], redo: [], dirty: false,
  splines: {}, play: 0, ghosts: [], live: true,
  map: null, tl: null, drag: null, raf: false,
};
const ED_COLORS = { pan: "#3d8bfd", tilt: "#2fb66f", zoom: "#d29922" };

// ============================================================ math
function edEndSlope(h0, h1, d0, d1) {
  if (h0 + h1 <= 0) return d0;
  const m = ((2 * h0 + h1) * d0 - h0 * d1) / (h0 + h1);
  if (m * d0 <= 0) return 0;
  if (d0 * d1 <= 0 && Math.abs(m) > Math.abs(3 * d0)) return 3 * d0;
  return m;
}

// Same algorithm as host/ptz/trajectory.py (Pchip): returns t -> [y, dy/dt]
function edPchip(ts, ys, natural) {
  const n = ts.length, m = new Array(n).fill(0);
  if (n >= 3) {
    const h = [], d = [];
    for (let k = 0; k < n - 1; k++) { h.push(ts[k + 1] - ts[k]); d.push(h[k] > 0 ? (ys[k + 1] - ys[k]) / h[k] : 0); }
    for (let k = 1; k < n - 1; k++) {
      if (d[k - 1] * d[k] <= 0) continue;
      const w1 = 2 * h[k] + h[k - 1], w2 = h[k] + 2 * h[k - 1];
      m[k] = (w1 + w2) / (w1 / d[k - 1] + w2 / d[k]);
    }
    if (natural) { m[0] = edEndSlope(h[0], h[1], d[0], d[1]); m[n - 1] = edEndSlope(h[n - 2], h[n - 3], d[n - 2], d[n - 3]); }
  }
  return (t) => {
    if (n === 1 || t <= ts[0]) return [ys[0], 0];
    if (t >= ts[n - 1]) return [ys[n - 1], 0];
    let lo = 0, hi = n - 1;
    while (hi - lo > 1) { const mid = (lo + hi) >> 1; if (ts[mid] <= t) lo = mid; else hi = mid; }
    const k = lo, h = ts[k + 1] - ts[k];
    if (h <= 0) return [ys[k + 1], 0];
    const s = (t - ts[k]) / h, s2 = s * s, s3 = s2 * s;
    const y = (2 * s3 - 3 * s2 + 1) * ys[k] + (s3 - 2 * s2 + s) * h * m[k]
      + (-2 * s3 + 3 * s2) * ys[k + 1] + (s3 - s2) * h * m[k + 1];
    const dy = ((6 * s2 - 6 * s) * ys[k] + (3 * s2 - 4 * s + 1) * h * m[k]
      + (-6 * s2 + 6 * s) * ys[k + 1] + (3 * s2 - 2 * s) * h * m[k + 1]) / h;
    return [y, dy];
  };
}

const edClone = (p) => ({ t: p.t, pos: { ...p.pos } });
const edDuration = () => (ED.pts.length ? ED.pts[ED.pts.length - 1].t : 0);
const edClamp = (v, lo, hi) => Math.max(lo, Math.min(hi, v));
const edRound = (v, d = 3) => Math.round(v * 10 ** d) / 10 ** d;

function edRebuildSplines() {
  const ts = ED.pts.map((p) => p.t);
  ED.splines = {};
  for (const a of ED.axes) ED.splines[a] = edPchip(ts, ED.pts.map((p) => p.pos[a]), ED.natural);
}
const edAt = (t) => Object.fromEntries(ED.axes.map((a) => [a, ED.splines[a](t)[0]]));

// Control points from a dense recording: Douglas-Peucker in time, then add the
// worst original point until the replayed curve stays within the tolerance.
function edSimplify(points, axes, tol) {
  const n = points.length;
  if (n <= 3) return points.map(edClone);
  const keep = new Uint8Array(n); keep[0] = keep[n - 1] = 1;
  const stack = [[0, n - 1]];
  while (stack.length) {
    const [a, b] = stack.pop();
    let worst = -1, wd = tol;
    const ta = points[a].t, tb = points[b].t;
    for (let k = a + 1; k < b; k++) {
      const u = (points[k].t - ta) / (tb - ta);
      let dmax = 0;
      for (const ax of axes) {
        const v = points[a].pos[ax] + u * (points[b].pos[ax] - points[a].pos[ax]);
        dmax = Math.max(dmax, Math.abs(points[k].pos[ax] - v));
      }
      if (dmax > wd) { wd = dmax; worst = k; }
    }
    if (worst > 0) { keep[worst] = 1; stack.push([a, worst], [worst, b]); }
  }
  for (let iter = 0; iter < 60; iter++) {        // refine against the real (cubic) curve
    const sel = points.filter((_, i) => keep[i]);
    const ts = sel.map((p) => p.t);
    const sp = Object.fromEntries(axes.map((a) => [a, edPchip(ts, sel.map((p) => p.pos[a]), ED.natural)]));
    let worst = -1, wd = tol * 1.5;
    points.forEach((p, i) => {
      if (keep[i]) return;
      for (const a of axes) { const d = Math.abs(sp[a](p.t)[0] - p.pos[a]); if (d > wd) { wd = d; worst = i; } }
    });
    if (worst < 0) break;
    keep[worst] = 1;
  }
  return points.filter((_, i) => keep[i]).map(edClone);
}

function edDeviation() {
  let worst = 0;
  for (const p of ED.orig) for (const a of ED.axes) {
    if (p.pos[a] === undefined) continue;
    worst = Math.max(worst, Math.abs(ED.splines[a](p.t)[0] - p.pos[a]));
  }
  return worst;
}

// ============================================================ open / close
async function openEditor(name) {
  let rec;
  try {
    const r = await fetch(`/api/recordings/${encodeURIComponent(name)}`);
    rec = await r.json();
    if (!rec.points) throw new Error(rec.error || "not found");
  } catch (e) { showError(`Cannot open "${name}": ${e.message}`); return; }
  Object.assign(ED, {
    open: true, name: rec.name, rec, mode: rec.mode || "continuous",
    natural: rec.mode === "track", orig: rec.points.map(edClone),
    axes: Object.keys(rec.points[0].pos), sel: 0, undo: [], redo: [], dirty: false,
    play: 0, ghosts: [],
  });
  ED.pts = ED.orig.length > 60 ? edSimplify(ED.orig, ED.axes, parseFloat($("ed-tol").value) || 0.3)
    : ED.orig.map(edClone);
  edRebuildSplines();
  $("ed-title").textContent = `Path editor: "${rec.name}"`;
  $("editor").style.top = `${document.querySelector("header").offsetHeight}px`;
  $("editor").classList.remove("hidden");
  document.body.classList.add("editing");
  if (ED.mode === "track" && rec.source) edLoadGhosts(rec.source);
  edBuildSide();
  edFitViews();
  edRender();
}

function closeEditor() {
  if (ED.dirty && !confirm("Close without saving your changes?")) return;
  ED.open = false;
  $("editor").classList.add("hidden");
  document.body.classList.remove("editing");
}

async function edLoadGhosts(source) {
  try {
    const raw = await (await fetch(`/api/recordings/${encodeURIComponent(source)}`)).json();
    const marks = (raw.markers || []).slice().sort((a, b) => a - b);
    const laps = [];
    for (let i = 0; i + 1 < marks.length; i++) {
      const a = marks[i], b = marks[i + 1];
      if (b - a < 2) continue;
      const pts = raw.points.filter((p) => p.t >= a && p.t < b)
        .map((p) => ({ phase: (p.t - a) / (b - a), pos: p.pos }));
      if (pts.length > 3) laps.push(pts);
    }
    ED.ghosts = laps;
    edRender();
  } catch (e) { /* ghosts are optional */ }
}

// ============================================================ edits (with undo)
function edSnapshot() {
  ED.undo.push(JSON.stringify(ED.pts));
  if (ED.undo.length > 200) ED.undo.shift();
  ED.redo = [];
}
function edChanged(follow = false) {
  ED.dirty = true;
  edRebuildSplines();
  edUpdateSide();
  edRender();
  if (follow) edFollow();
}
function edRestore(from, to) {
  if (!from.length) return;
  to.push(JSON.stringify(ED.pts));
  ED.pts = JSON.parse(from.pop());
  ED.sel = Math.min(ED.sel, ED.pts.length - 1);
  const T = edDuration();
  if (ED.tl.t1 > T * 1.2 || ED.tl.t1 - ED.tl.t0 < T * 0.5) { ED.tl.t0 = 0; ED.tl.t1 = Math.max(T, 0.5); }
  ED.play = Math.min(ED.play, T);
  edChanged();
}

function edSetPoint(i, axis, value, follow = true) {
  const p = ED.pts[i];
  if (axis === "t") {
    const lo = i > 0 ? ED.pts[i - 1].t + 0.02 : 0, hi = i < ED.pts.length - 1 ? ED.pts[i + 1].t - 0.02 : 1e9;
    if (i === 0) return;                                   // the path starts at t = 0
    p.t = edRound(edClamp(value, lo, hi));
  } else {
    const c = axisCfg(axis);
    p.pos[axis] = edRound(edClamp(value, c.min, c.max));
  }
  edChanged(follow);
}

function edInsertAfter(i) {
  const a = ED.pts[i], b = ED.pts[i + 1];
  const t = b ? (a.t + b.t) / 2 : a.t + 1;
  edSnapshot();
  ED.pts.splice(i + 1, 0, { t: edRound(t), pos: Object.fromEntries(ED.axes.map((ax) => [ax, edRound(b ? ED.splines[ax](t)[0] : a.pos[ax])])) });
  ED.sel = i + 1;
  edChanged(true);
}
function edInsertAt(t) {
  let i = ED.pts.findIndex((p) => p.t > t);
  if (i <= 0) return;
  edSnapshot();
  ED.pts.splice(i, 0, { t: edRound(t), pos: Object.fromEntries(ED.axes.map((ax) => [ax, edRound(ED.splines[ax](t)[0])])) });
  ED.sel = i;
  edChanged(true);
}
function edDelete(i) {
  if (ED.pts.length <= 2) { showError("a path needs at least 2 points"); return; }
  edSnapshot();
  ED.pts.splice(i, 1);
  if (i === 0) { const t0 = ED.pts[0].t; ED.pts.forEach((p) => { p.t = edRound(p.t - t0); }); }
  ED.sel = Math.min(i, ED.pts.length - 1);
  edChanged();
}

// ============================================================ head link
function edHeadAxes() {        // path axes the head can move now
  return ED.axes.filter((a) => status && status.axes[a] && status.axes[a].active !== false && status.axes[a].homed);
}
function edGoto(pos) {
  const msg = { cmd: "goto", speed: 0.5, id: "edgo" };
  for (const a of edHeadAxes()) msg[a] = pos[a];
  if (Object.keys(msg).length > 3) send(msg);
  else showError("home the axes to move the head");
}
let edFollowTimer = null;
function edFollow() {           // live mode: keep the head on the selected point
  if (!$("ed-live").checked || ED.sel < 0) return;
  clearTimeout(edFollowTimer);
  edFollowTimer = setTimeout(() => edGoto(ED.pts[ED.sel].pos), 120);
}

// ============================================================ side panel
function edBuildSide() {
  const unit = (a) => (axisCfg(a) || {}).units || "";
  $("ed-point").innerHTML = `<span>Time</span><input id="ed-f-t" type="number" step="0.05">`
    + ED.axes.map((a) => `<span style="color:${ED_COLORS[a] || "inherit"}">${a}</span>
      <div><input id="ed-f-${a}" type="number" step="0.05" title="${unit(a)}">
      <div class="ed-nudge">${[-1, -0.1, 0.1, 1].map((d) => `<button data-nudge="${a}" data-d="${d}">${d > 0 ? "+" : ""}${d}</button>`).join("")}</div></div>`).join("");
  $("ed-offsets").innerHTML = ED.axes.map((a) => `<span>${a}</span><input id="ed-o-${a}" type="number" step="0.1" value="0" title="added to every point (${unit(a)})">`).join("");
  $("ed-f-t").onchange = (e) => { edSnapshot(); edSetPoint(ED.sel, "t", parseFloat(e.target.value), false); };
  for (const a of ED.axes) {
    $(`ed-f-${a}`).onchange = (e) => { edSnapshot(); edSetPoint(ED.sel, a, parseFloat(e.target.value)); };
  }
  document.querySelectorAll("[data-nudge]").forEach((b) => {
    b.onclick = () => {
      const a = b.dataset.nudge;
      edSnapshot();
      edSetPoint(ED.sel, a, ED.pts[ED.sel].pos[a] + parseFloat(b.dataset.d));
    };
  });
  const hasZoom = config.axes.some((a) => a.name === "zoom");
  $("ed-zoom-add-row").classList.toggle("hidden", !hasZoom || ED.axes.includes("zoom"));
  $("ed-ghost-row").classList.toggle("hidden", ED.mode !== "track");
  $("ed-duration").value = edDuration().toFixed(2);
  edUpdateSide();
}

function edUpdateSide() {
  const p = ED.pts[ED.sel];
  $("ed-pt-index").textContent = p ? `${ED.sel + 1} / ${ED.pts.length}` : "";
  if (p) {
    if (document.activeElement !== $("ed-f-t")) $("ed-f-t").value = p.t.toFixed(2);
    $("ed-f-t").disabled = ED.sel === 0;
    for (const a of ED.axes) if (document.activeElement !== $(`ed-f-${a}`)) $(`ed-f-${a}`).value = p.pos[a].toFixed(2);
  }
  if (document.activeElement !== $("ed-duration")) $("ed-duration").value = edDuration().toFixed(2);
  $("ed-info").textContent = `${ED.mode} · ${edDuration().toFixed(2)} s · ${ED.pts.length} points`
    + (ED.dirty ? " · unsaved changes" : "");
  const sameTiming = ED.orig.length && Math.abs(ED.orig[ED.orig.length - 1].t - edDuration()) < 0.01;
  $("ed-dev").textContent = sameTiming ? `Max difference from the recording: ${edDeviation().toFixed(2)}`
    : "Timing changed: the grey line is the recording at its original timing.";
  $("ed-undo").disabled = !ED.undo.length;
  $("ed-redo").disabled = !ED.redo.length;
  edUpdateHead();
}

function edUpdateHead() {
  if (!ED.open || !status) return;
  const p = ED.pts[ED.sel];
  $("ed-head").innerHTML = ED.axes.map((a) => {
    const s = status.axes[a];
    if (!s) return "";
    const txt = s.active === false ? "off" : !s.homed ? "not homed" : fmtPos(s.pos, s.units)
      + (p ? ` <span class="muted">(Δ ${(s.pos - p.pos[a]).toFixed(2)})</span>` : "");
    return `<span style="color:${ED_COLORS[a] || "inherit"}">${a}</span><span class="num">${txt}</span>`;
  }).join("");
  $("ed-play-t").textContent = `playhead ${ED.play.toFixed(2)} s`;
}

// ============================================================ views
function edCanvas(id) {
  const c = $(id), r = c.getBoundingClientRect(), dpr = window.devicePixelRatio || 1;
  if (c.width !== Math.round(r.width * dpr) || c.height !== Math.round(r.height * dpr)) {
    c.width = Math.round(r.width * dpr); c.height = Math.round(r.height * dpr);
  }
  const g = c.getContext("2d");
  g.setTransform(dpr, 0, 0, dpr, 0, 0);
  return { c, g, w: r.width, h: r.height };
}

function edFitViews() {
  // aim map: path + recording + soft limits, same scale on both axes
  const xs = [], ys = [];
  for (const p of ED.orig.concat(ED.pts)) { if (p.pos.pan !== undefined) xs.push(p.pos.pan); if (p.pos.tilt !== undefined) ys.push(p.pos.tilt); }
  const lim = status && status.limits;
  if (lim && lim.enabled) {
    for (const [a, arr] of [["pan", xs], ["tilt", ys]]) {
      const u = lim.axes[a] || {};
      if (u.min != null) arr.push(u.min);
      if (u.max != null) arr.push(u.max);
    }
  }
  if (!xs.length) xs.push(-10, 10);
  if (!ys.length) ys.push(-10, 10);
  let x0 = Math.min(...xs), x1 = Math.max(...xs), y0 = Math.min(...ys), y1 = Math.max(...ys);
  ED.map = { cx: (x0 + x1) / 2, cy: (y0 + y1) / 2, xspan: Math.max(x1 - x0, 4) * 1.12,
             yspan: Math.max(y1 - y0, 4) * 1.25, zoom: 1 };
  // timeline
  const lanes = {};
  for (const a of ED.axes) {
    const v = ED.orig.concat(ED.pts).map((p) => p.pos[a]).filter((x) => x !== undefined);
    let lo = Math.min(...v), hi = Math.max(...v);
    const pad = Math.max((hi - lo) * 0.12, 1);
    lanes[a] = [lo - pad, hi + pad];
  }
  ED.tl = { t0: 0, t1: Math.max(edDuration(), 0.5), lanes };
}

function edMapXf(w, h) {
  const s = Math.min(w / ED.map.xspan, h / ED.map.yspan) * ED.map.zoom;   // same scale on both axes
  return { s, x: (pan) => w / 2 + (pan - ED.map.cx) * s, y: (tilt) => h / 2 - (tilt - ED.map.cy) * s,
           pan: (x) => ED.map.cx + (x - w / 2) / s, tilt: (y) => ED.map.cy - (y - h / 2) / s };
}

function edRender() {
  if (ED.raf) return;
  ED.raf = true;
  requestAnimationFrame(() => { ED.raf = false; if (ED.open) { edDrawMap(); edDrawTimeline(); edUpdateHead(); } });
}

function edDrawMap() {
  const { g, w, h } = edCanvas("ed-map");
  g.clearRect(0, 0, w, h);
  if (!ED.axes.includes("pan") || !ED.axes.includes("tilt")) {
    g.fillStyle = "#8a919c"; g.fillText("This path has no pan / tilt.", 20, 30); return;
  }
  const X = edMapXf(w, h);
  // grid every 10 units
  g.strokeStyle = "#262a31"; g.lineWidth = 1;
  const vis = Math.max(w, h) / X.s;
  const step = vis > 240 ? 30 : vis > 80 ? 10 : vis > 20 ? 5 : 1;
  for (let v = Math.ceil(X.pan(0) / step) * step; v < X.pan(w); v += step) { g.beginPath(); g.moveTo(X.x(v), 0); g.lineTo(X.x(v), h); g.stroke(); }
  for (let v = Math.ceil(X.tilt(h) / step) * step; v < X.tilt(0); v += step) { g.beginPath(); g.moveTo(0, X.y(v)); g.lineTo(w, X.y(v)); g.stroke(); }
  g.fillStyle = "#5c6470"; g.font = "11px system-ui";
  g.fillText(`grid ${step}°`, w - 70, h - 8);
  // configured travel and soft limits
  const pc = axisCfg("pan"), tc = axisCfg("tilt");
  g.setLineDash([6, 4]); g.strokeStyle = "#555";
  g.strokeRect(X.x(pc.min), X.y(tc.max), (pc.max - pc.min) * X.s, (tc.max - tc.min) * X.s);
  const lim = status && status.limits;
  if (lim) {
    const e = lim.effective;
    g.strokeStyle = lim.enabled ? "#e5484d" : "#7a4a1c";
    g.strokeRect(X.x(e.pan[0]), X.y(e.tilt[1]), (e.pan[1] - e.pan[0]) * X.s, (e.tilt[1] - e.tilt[0]) * X.s);
  }
  g.setLineDash([]);
  // presets
  g.fillStyle = "#8a919c";
  for (const [id, p] of Object.entries(presets || {})) {
    if (p.positions.pan === undefined || p.positions.tilt === undefined) continue;
    const x = X.x(p.positions.pan), y = X.y(p.positions.tilt);
    g.beginPath(); g.moveTo(x, y - 5); g.lineTo(x + 5, y); g.lineTo(x, y + 5); g.lineTo(x - 5, y); g.fill();
    g.fillText(id, x + 7, y + 4);
  }
  // learned laps (ghosts) and the recording before editing
  if ($("ed-ghosts").checked) {
    g.strokeStyle = "rgba(255,255,255,0.10)";
    for (const lap of ED.ghosts) { g.beginPath(); lap.forEach((p, i) => (i ? g.lineTo : g.moveTo).call(g, X.x(p.pos.pan), X.y(p.pos.tilt))); g.stroke(); }
  }
  g.strokeStyle = "rgba(138,145,156,0.45)"; g.lineWidth = 1;
  g.beginPath(); ED.orig.forEach((p, i) => (i ? g.lineTo : g.moveTo).call(g, X.x(p.pos.pan), X.y(p.pos.tilt))); g.stroke();
  // edited path
  const T = edDuration(), N = 600;
  g.strokeStyle = "#3d8bfd"; g.lineWidth = 2; g.beginPath();
  for (let i = 0; i <= N; i++) { const q = edAt(T * i / N); (i ? g.lineTo : g.moveTo).call(g, X.x(q.pan), X.y(q.tilt)); }
  g.stroke();
  // points
  ED.pts.forEach((p, i) => {
    g.beginPath(); g.arc(X.x(p.pos.pan), X.y(p.pos.tilt), i === ED.sel ? 7 : 4, 0, Math.PI * 2);
    g.fillStyle = i === ED.sel ? "#fff" : i === 0 ? "#2fb66f" : "#3d8bfd"; g.fill();
  });
  g.fillStyle = "#2fb66f"; g.fillText("start", X.x(ED.pts[0].pos.pan) + 8, X.y(ED.pts[0].pos.tilt) - 8);
  // playhead and live head
  const ph = edAt(ED.play);
  g.strokeStyle = "#f0b429"; g.lineWidth = 2; g.beginPath(); g.arc(X.x(ph.pan), X.y(ph.tilt), 9, 0, Math.PI * 2); g.stroke();
  if (status && status.axes.pan && status.axes.pan.homed) {
    const hx = X.x(status.axes.pan.pos), hy = X.y(status.axes.tilt.pos);
    g.strokeStyle = "#e5484d"; g.lineWidth = 2;
    g.beginPath(); g.moveTo(hx - 10, hy); g.lineTo(hx + 10, hy); g.moveTo(hx, hy - 10); g.lineTo(hx, hy + 10); g.stroke();
  }
}

function edLaneLayout(h) {
  const lanes = ED.axes.concat(["speed"]), top = 22, gap = 8;
  const lh = (h - top - 24 - gap * (lanes.length - 1)) / lanes.length;   // bottom: room for the legend
  return lanes.map((name, i) => ({ name, y0: top + i * (lh + gap), y1: top + i * (lh + gap) + lh }));
}
function edTlX(w) {
  const L = 60, R = 12;
  return { L, R, x: (t) => L + (t - ED.tl.t0) / (ED.tl.t1 - ED.tl.t0) * (w - L - R),
           t: (x) => ED.tl.t0 + (x - L) / (w - L - R) * (ED.tl.t1 - ED.tl.t0) };
}

function edDrawTimeline() {
  const { g, w, h } = edCanvas("ed-tl");
  g.clearRect(0, 0, w, h);
  const X = edTlX(w), T = edDuration(), lanes = edLaneLayout(h);
  g.font = "11px system-ui";
  // time ruler
  const span = ED.tl.t1 - ED.tl.t0, step = span > 120 ? 10 : span > 40 ? 5 : span > 12 ? 1 : 0.5;
  g.fillStyle = "#8a919c"; g.strokeStyle = "#262a31";
  for (let t = Math.ceil(ED.tl.t0 / step) * step; t <= ED.tl.t1; t += step) {
    const x = X.x(t); g.beginPath(); g.moveTo(x, 16); g.lineTo(x, h - 12); g.stroke(); g.fillText(`${+t.toFixed(1)}s`, x + 2, 12);
  }
  for (const lane of lanes) {
    const a = lane.name;
    g.fillStyle = "#15171a"; g.fillRect(X.L, lane.y0, w - X.L - X.R, lane.y1 - lane.y0);
    g.fillStyle = ED_COLORS[a] || "#8a919c"; g.fillText(a === "speed" ? "speed °/s" : a, 6, lane.y0 + 12);
    if (a === "speed") { edDrawSpeedLane(g, X, lane, T); continue; }
    const [lo, hi] = ED.tl.lanes[a], Y = (v) => lane.y1 - (v - lo) / (hi - lo) * (lane.y1 - lane.y0);
    g.save(); g.beginPath(); g.rect(0, lane.y0, w, lane.y1 - lane.y0); g.clip();
    g.fillStyle = "#5c6470"; g.fillText(hi.toFixed(0), 34, lane.y0 + 24); g.fillText(lo.toFixed(0), 34, lane.y1 - 2);
    // soft limits of this axis
    const lim = status && status.limits;
    if (lim && lim.enabled) {
      g.strokeStyle = "rgba(229,72,77,0.6)"; g.setLineDash([5, 4]);
      for (const v of lim.effective[a] || []) { if (v > lo && v < hi) { g.beginPath(); g.moveTo(X.L, Y(v)); g.lineTo(w - X.R, Y(v)); g.stroke(); } }
      g.setLineDash([]);
    }
    if ($("ed-ghosts").checked && ED.ghosts.length) {
      g.strokeStyle = "rgba(255,255,255,0.10)";
      for (const lap of ED.ghosts) { g.beginPath(); lap.forEach((p, i) => (i ? g.lineTo : g.moveTo).call(g, X.x(p.phase * T), Y(p.pos[a]))); g.stroke(); }
    }
    g.strokeStyle = "rgba(138,145,156,0.45)"; g.lineWidth = 1; g.beginPath();
    ED.orig.forEach((p, i) => { if (p.pos[a] !== undefined) (i ? g.lineTo : g.moveTo).call(g, X.x(p.t), Y(p.pos[a])); }); g.stroke();
    g.strokeStyle = ED_COLORS[a] || "#3d8bfd"; g.lineWidth = 2; g.beginPath();
    const N = 800;
    for (let i = 0; i <= N; i++) { const t = ED.tl.t0 + span * i / N; if (t > T) break; (i ? g.lineTo : g.moveTo).call(g, X.x(t), Y(ED.splines[a](t)[0])); }
    g.stroke();
    ED.pts.forEach((p, i) => {
      const x = X.x(p.t); if (x < X.L - 5 || x > w - X.R + 5) return;
      g.beginPath(); g.arc(x, Y(p.pos[a]), i === ED.sel ? 6 : 3.5, 0, Math.PI * 2);
      g.fillStyle = i === ED.sel ? "#fff" : ED_COLORS[a] || "#3d8bfd"; g.fill();
    });
    if (status && status.axes[a] && status.axes[a].homed) {      // live head value
      g.strokeStyle = "rgba(240,240,240,0.55)"; g.setLineDash([2, 3]);
      g.beginPath(); g.moveTo(X.L, Y(status.axes[a].pos)); g.lineTo(w - X.R, Y(status.axes[a].pos)); g.stroke(); g.setLineDash([]);
    }
    g.restore();
  }
  // playhead + end of path
  g.strokeStyle = "#f0b429"; g.lineWidth = 2;
  g.beginPath(); g.moveTo(X.x(ED.play), 16); g.lineTo(X.x(ED.play), h - 12); g.stroke();
  g.strokeStyle = "#555"; g.setLineDash([4, 4]); g.beginPath(); g.moveTo(X.x(T), 16); g.lineTo(X.x(T), h - 12); g.stroke(); g.setLineDash([]);
}

function edDrawSpeedLane(g, X, lane, T) {
  const axes = ED.axes.filter((a) => axisCfg(a));
  const vmax = Math.max(...axes.map((a) => axisCfg(a).max_velocity));
  const top = vmax * 1.15, Y = (v) => lane.y1 - v / top * (lane.y1 - lane.y0);
  const N = 500, span = ED.tl.t1 - ED.tl.t0;
  for (const a of axes) {
    const lim = axisCfg(a).max_velocity;
    g.strokeStyle = ED_COLORS[a] || "#8a919c"; g.lineWidth = 1.5; g.beginPath();
    for (let i = 0; i <= N; i++) {
      const t = ED.tl.t0 + span * i / N; if (t > T) break;
      const v = Math.abs(ED.splines[a](t)[1]);
      (i ? g.lineTo : g.moveTo).call(g, X.x(t), Y(Math.min(v, top)));
      if (v > lim) { g.save(); g.fillStyle = "rgba(229,72,77,0.5)"; g.fillRect(X.x(t), lane.y0, 2, lane.y1 - lane.y0); g.restore(); }
    }
    g.stroke();
    g.setLineDash([4, 4]); g.beginPath(); g.moveTo(X.L, Y(lim)); g.lineTo(X.x(T), Y(lim)); g.stroke(); g.setLineDash([]);
  }
  g.fillStyle = "#5c6470"; g.fillText(`${vmax.toFixed(0)} max`, 6, lane.y1 - 2);
}

// ============================================================ pointer handling
function edPointer(canvasId, handlers) {
  const c = $(canvasId);
  c.addEventListener("pointerdown", (e) => { c.setPointerCapture(e.pointerId); handlers.down(e, c.getBoundingClientRect()); });
  c.addEventListener("pointermove", (e) => { if (c.hasPointerCapture(e.pointerId)) handlers.move(e, c.getBoundingClientRect()); });
  c.addEventListener("pointerup", (e) => { if (c.hasPointerCapture(e.pointerId)) { c.releasePointerCapture(e.pointerId); handlers.up(e); } });
  c.addEventListener("wheel", (e) => { e.preventDefault(); handlers.wheel(e, c.getBoundingClientRect()); }, { passive: false });
  if (handlers.dbl) c.addEventListener("dblclick", (e) => handlers.dbl(e, c.getBoundingClientRect()));
}

edPointer("ed-map", {
  down(e, r) {
    if (!ED.axes.includes("pan")) return;
    const X = edMapXf(r.width, r.height), mx = e.clientX - r.left, my = e.clientY - r.top;
    let best = -1, bd = 12;
    ED.pts.forEach((p, i) => { const d = Math.hypot(X.x(p.pos.pan) - mx, X.y(p.pos.tilt) - my); if (d < bd) { bd = d; best = i; } });
    if (best >= 0) {
      ED.sel = best; edSnapshot(); ED.drag = { kind: "point", moved: false };
      edUpdateSide(); edRender(); edFollow();
    } else {
      ED.drag = { kind: "view", x: mx, y: my, cx: ED.map.cx, cy: ED.map.cy };
    }
  },
  move(e, r) {
    const X = edMapXf(r.width, r.height), mx = e.clientX - r.left, my = e.clientY - r.top;
    if (!ED.drag) return;
    if (ED.drag.kind === "point") {
      const p = ED.pts[ED.sel], pc = axisCfg("pan"), tc = axisCfg("tilt");
      p.pos.pan = edRound(edClamp(X.pan(mx), pc.min, pc.max));
      p.pos.tilt = edRound(edClamp(X.tilt(my), tc.min, tc.max));
      ED.drag.moved = true; edRebuildSplines(); edUpdateSide(); edRender();
    } else {
      ED.map.cx = ED.drag.cx - (mx - ED.drag.x) / X.s; ED.map.cy = ED.drag.cy + (my - ED.drag.y) / X.s; edRender();
    }
  },
  up() {
    if (ED.drag && ED.drag.kind === "point") { if (ED.drag.moved) edChanged(true); else ED.undo.pop(); }
    ED.drag = null;
  },
  wheel(e, r) {
    const X = edMapXf(r.width, r.height), mx = e.clientX - r.left, my = e.clientY - r.top;
    const px = X.pan(mx), py = X.tilt(my), k = e.deltaY > 0 ? 1.15 : 1 / 1.15;
    ED.map.zoom = edClamp(ED.map.zoom / k, 0.05, 200);
    const X2 = edMapXf(r.width, r.height);
    ED.map.cx += px - X2.pan(mx); ED.map.cy += py - X2.tilt(my); edRender();
  },
});

edPointer("ed-tl", {
  down(e, r) {
    const X = edTlX(r.width), mx = e.clientX - r.left, my = e.clientY - r.top;
    const lane = edLaneLayout(r.height).find((l) => my >= l.y0 && my <= l.y1);
    if (lane && lane.name !== "speed") {
      const [lo, hi] = ED.tl.lanes[lane.name], Y = (v) => lane.y1 - (v - lo) / (hi - lo) * (lane.y1 - lane.y0);
      let best = -1, bd = 10;
      ED.pts.forEach((p, i) => { const d = Math.hypot(X.x(p.t) - mx, Y(p.pos[lane.name]) - my); if (d < bd) { bd = d; best = i; } });
      if (best >= 0) {
        ED.sel = best; edSnapshot();
        ED.drag = { kind: "tlpoint", lane, moved: false, time: e.shiftKey };
        edUpdateSide(); edRender(); edFollow();
        return;
      }
    }
    ED.play = edClamp(X.t(mx), 0, edDuration());
    ED.drag = { kind: "scrub" };
    edRender();
  },
  move(e, r) {
    if (!ED.drag) return;
    const X = edTlX(r.width), mx = e.clientX - r.left, my = e.clientY - r.top;
    if (ED.drag.kind === "scrub") { ED.play = edClamp(X.t(mx), 0, edDuration()); edRender(); return; }
    const lane = ED.drag.lane, a = lane.name, [lo, hi] = ED.tl.lanes[a];
    const i = ED.sel, p = ED.pts[i];
    if (ED.drag.time) {
      if (i === 0) return;
      const tlo = ED.pts[i - 1].t + 0.02, thi = i < ED.pts.length - 1 ? ED.pts[i + 1].t - 0.02 : 1e9;
      p.t = edRound(edClamp(X.t(mx), tlo, thi));
    } else {
      const c = axisCfg(a), v = lo + (lane.y1 - my) / (lane.y1 - lane.y0) * (hi - lo);
      p.pos[a] = edRound(edClamp(v, c.min, c.max));
    }
    ED.drag.moved = true; edRebuildSplines(); edUpdateSide(); edRender();
  },
  up() {
    if (ED.drag && ED.drag.kind === "tlpoint") { if (ED.drag.moved) edChanged(!ED.drag.time); else ED.undo.pop(); }
    ED.drag = null;
  },
  wheel(e, r) {
    const X = edTlX(r.width), t = X.t(e.clientX - r.left), k = e.deltaY > 0 ? 1.2 : 1 / 1.2;
    const span = edClamp((ED.tl.t1 - ED.tl.t0) * k, 0.5, Math.max(edDuration() * 1.2, 1));
    const f = (t - ED.tl.t0) / (ED.tl.t1 - ED.tl.t0);
    ED.tl.t0 = Math.max(0, t - f * span); ED.tl.t1 = ED.tl.t0 + span; edRender();
  },
  dbl(e, r) { const X = edTlX(r.width); edInsertAt(edClamp(X.t(e.clientX - r.left), 0.01, edDuration() - 0.01)); },
});

// ============================================================ whole-path tools
$("ed-offset-apply").onclick = () => {
  const off = Object.fromEntries(ED.axes.map((a) => [a, parseFloat($(`ed-o-${a}`).value) || 0]));
  if (!Object.values(off).some((v) => v)) return;
  edSnapshot();
  for (const p of ED.pts) for (const a of ED.axes) { const c = axisCfg(a); p.pos[a] = edRound(edClamp(p.pos[a] + off[a], c.min, c.max)); }
  ED.axes.forEach((a) => { $(`ed-o-${a}`).value = 0; });
  edChanged(true);
};
$("ed-duration-apply").onclick = () => {
  const d = parseFloat($("ed-duration").value), T = edDuration();
  if (!(d > 0.2) || !T) return;
  edSnapshot();
  const k = d / T;
  ED.pts.forEach((p) => { p.t = edRound(p.t * k); });
  ED.play *= k; ED.tl.t0 *= k; ED.tl.t1 *= k;
  edChanged();
};
function edTrim(keepAfter) {
  const t = ED.play, T = edDuration();
  if (t <= 0.05 || t >= T - 0.05) { showError("place the playhead inside the path first"); return; }
  edSnapshot();
  const cut = { t, pos: Object.fromEntries(ED.axes.map((a) => [a, edRound(ED.splines[a](t)[0])])) };
  if (keepAfter) {
    ED.pts = [cut].concat(ED.pts.filter((p) => p.t > t + 0.02)).map((p) => ({ t: edRound(p.t - t), pos: p.pos }));
    ED.play = 0;
  } else {
    ED.pts = ED.pts.filter((p) => p.t < t - 0.02).concat([cut]);
  }
  ED.sel = 0;
  ED.tl.t0 = 0; ED.tl.t1 = edDuration();
  edChanged();
}
$("ed-trim-start").onclick = () => edTrim(true);
$("ed-trim-end").onclick = () => edTrim(false);
$("ed-resimplify").onclick = () => {
  if (ED.dirty && !confirm("Rebuild the points from the recording? Your edits are lost (Undo brings them back).")) return;
  edSnapshot();
  ED.pts = edSimplify(ED.orig, ED.axes, parseFloat($("ed-tol").value) || 0.3);
  ED.sel = 0;
  edChanged();
};
$("ed-zoom-add").onclick = () => {
  const z = status && status.axes.zoom, c = axisCfg("zoom");
  const v = z && z.homed ? z.pos : c.min;
  edSnapshot();
  ED.pts.forEach((p) => { p.pos.zoom = edRound(v); });
  ED.orig.forEach((p) => { if (p.pos.zoom === undefined) p.pos.zoom = edRound(v); });
  ED.axes.push("zoom");
  edBuildSide(); edFitViews(); edChanged();
};
$("ed-ghosts").onchange = edRender;

// ============================================================ point buttons
$("ed-goto").onclick = () => edGoto(ED.pts[ED.sel].pos);
$("ed-from-head").onclick = () => {
  const axes = edHeadAxes();
  if (!axes.length) { showError("home the axes first"); return; }
  edSnapshot();
  for (const a of axes) ED.pts[ED.sel].pos[a] = edRound(status.axes[a].pos);
  edChanged();
};
$("ed-insert").onclick = () => edInsertAfter(ED.sel);
$("ed-delete").onclick = () => edDelete(ED.sel);
$("ed-prev-pt").onclick = () => { ED.sel = Math.max(0, ED.sel - 1); edUpdateSide(); edRender(); edFollow(); };
$("ed-next-pt").onclick = () => { ED.sel = Math.min(ED.pts.length - 1, ED.sel + 1); edUpdateSide(); edRender(); edFollow(); };
$("ed-goto-play").onclick = () => edGoto(edAt(ED.play));
$("ed-fit").onclick = () => { edFitViews(); edRender(); };
$("ed-undo").onclick = () => edRestore(ED.undo, ED.redo);
$("ed-redo").onclick = () => edRestore(ED.redo, ED.undo);

// ============================================================ preview / save
function edPayload() {
  const out = { mode: ED.mode, points: ED.pts.map(edClone) };
  for (const k of ["laps", "source"]) if (ED.rec[k] !== undefined) out[k] = ED.rec[k];
  return out;
}
async function edPut(name) {
  const r = await fetch(`/api/recordings/${encodeURIComponent(name)}`, {
    method: "PUT", body: JSON.stringify(edPayload()),
    headers: { "Content-Type": "application/json", "X-PTZ-Client": TOKEN },
  });
  const res = await r.json();
  if (!res.ok) throw new Error(res.error);
  return res.recording;
}
$("ed-preview").onclick = async () => {
  try {
    await edPut("~preview");
    send({ cmd: "play", name: "~preview", speed: parseFloat($("ed-prev-speed").value), loop: false, id: "edpl" });
  } catch (e) { showError(`Preview: ${e.message}`); }
};
$("ed-prev-stop").onclick = () => send({ cmd: "play_stop", id: "edps" });
$("ed-save").onclick = async () => {
  if (!confirm(`Replace "${ED.name}"? The previous version is kept as a backup.`)) return;
  try {
    await edPut(ED.name);
    ED.dirty = false; edUpdateSide();
    showError(`Saved "${ED.name}"` + (ED.mode === "track" ? " (re-ARM to use it)" : ""));
  } catch (e) { showError(`Save failed: ${e.message}`); }
};
$("ed-saveas").onclick = async () => {
  const name = prompt("Save as (new name)", `${ED.name} (edited)`);
  if (!name) return;
  if (recordings.some((r) => r.name === name) && !confirm(`"${name}" exists. Replace it (a backup is kept)?`)) return;
  try {
    const rec = await edPut(name);
    ED.name = rec.name; ED.rec.name = rec.name; ED.dirty = false;
    $("ed-title").textContent = `Path editor: "${rec.name}"`;
    edUpdateSide();
  } catch (e) { showError(`Save failed: ${e.message}`); }
};
$("ed-close").onclick = closeEditor;

// ============================================================ keyboard, resize, live updates
document.addEventListener("keydown", (e) => {
  if (!ED.open || ["INPUT", "SELECT", "TEXTAREA"].includes(e.target.tagName)) return;
  if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "z") { e.preventDefault(); edRestore(ED.undo, ED.redo); }
  else if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "y") { e.preventDefault(); edRestore(ED.redo, ED.undo); }
  else if (e.key === "Delete") { e.preventDefault(); edDelete(ED.sel); }
  else if (e.key === "Tab") { e.preventDefault(); $(e.shiftKey ? "ed-prev-pt" : "ed-next-pt").click(); }
});
window.addEventListener("resize", () => { if (ED.open) edRender(); });
window.addEventListener("beforeunload", (e) => { if (ED.open && ED.dirty) { e.preventDefault(); e.returnValue = ""; } });
setInterval(() => { if (ED.open) edRender(); }, 100);        // live head marker
