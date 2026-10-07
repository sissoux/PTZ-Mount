// PTZ web UI - plain JS, no build step.
// Inputs (pad, zoom rocker, keyboard, gamepad) are merged into one jog
// vector which is sent at JOG_HZ while any input is active.
"use strict";

const JOG_HZ = 25;
const NUM_PRESETS = 12;
const GAMEPAD_DEADBAND = 0.12;      // ignore stick drift
const MAX_LOG = 1500;
let ws = null, config = null, presets = {}, status = null, recordings = [];
let logEntries = [];

const $ = (id) => document.getElementById(id);
const input = { pad: { x: 0, y: 0 }, zoom: 0, keys: { x: 0, y: 0, z: 0 }, gp: { x: 0, y: 0, z: 0 } };

// ------------------------------------------------------------ websocket
function connect() {
  ws = new WebSocket(`${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/ws`);
  ws.onopen = () => { if ($("debug-mode").checked) send({ cmd: "logs", on: true }); };
  ws.onmessage = (e) => {
    const m = JSON.parse(e.data);
    switch (m.type) {
      case "status": renderStatus(m); break;
      case "config": config = m; buildAxesTable(); break;
      case "presets": presets = m.presets; renderPresets(); break;
      case "recordings": recordings = m.recordings; renderRecordings(); break;
      case "debug": setDebugUi(m.on); break;
      case "log": addLog(m); break;
      case "log_backlog": logEntries = []; m.entries.forEach((x) => addLog(x, false)); renderLog(); break;
      case "reply": onReply(m); break;
    }
  };
  ws.onclose = () => { setConn(false); setTimeout(connect, 1000); };
}
function send(obj) { if (ws && ws.readyState === 1) ws.send(JSON.stringify(obj)); }
function onReply(m) {
  if (!m.ok) { showError(m.error); return; }
  if (m.cmd === "diag") {
    addLog({ t: Date.now() / 1000, level: "INFO", name: "diag", msg: JSON.stringify(m.diag, null, 2) });
  }
}
function showError(msg) {
  $("error").textContent = msg || "";
  if (msg) setTimeout(() => { if ($("error").textContent === msg) $("error").textContent = ""; }, 5000);
}
function setConn(ok) { $("conn").textContent = ok ? "online" : "offline"; $("conn").classList.toggle("off", !ok); }

// ------------------------------------------------------------ status
function buildAxesTable() {
  const tb = $("axes").querySelector("tbody");
  tb.innerHTML = "";
  for (const a of config.axes) {
    const tr = document.createElement("tr");
    tr.innerHTML = `<td>${a.name}</td><td class="num" id="pos-${a.name}">-</td>
      <td class="num" id="vel-${a.name}">-</td><td id="st-${a.name}"></td>
      <td><input id="goto-${a.name}" type="number" step="any" min="${a.min}" max="${a.max}"></td>
      <td><button class="small" title="Home this axis only">Home</button></td>`;
    tr.querySelector("button").onclick = () => send({ cmd: "home", axes: [a.name], id: "ha" });
    tb.appendChild(tr);
  }
  const homed = config.axes.filter((a) => a.home_with_all).map((a) => a.name);
  $("btn-home").title = `Homes: ${homed.join(", ") || "none"} (home_with_all in ptz.cfg)`;
}
function flag(text, on, cls = "on") { return `<span class="flag ${on ? cls : ""}">${text}</span>`; }

function syncSlider(id, value, fmt, transform = (v) => v) {
  const el = $(id);
  if (document.activeElement === el || value === undefined) return;   // user is dragging it
  el.value = transform(value);
  $(`${id}-val`).textContent = fmt(value);
}
const pct = (v) => `${Math.round(v * 100)}%`;
const times = (v) => `${v.toFixed(2)}×`;

function renderStatus(s) {
  status = s;
  setConn(s.connected);
  $("estop-badge").classList.toggle("hidden", !s.estop);
  const b = $("btn-estop");
  b.textContent = s.estop ? "RESET E-STOP" : "E-STOP";
  b.classList.toggle("reset", s.estop);
  if (s.error) showError(s.error);
  for (const [name, a] of Object.entries(s.axes)) {
    const p = $(`pos-${name}`); if (!p) continue;
    p.textContent = a.pos.toFixed(2);
    $(`vel-${name}`).textContent = a.vel.toFixed(1);
    $(`st-${name}`).innerHTML = flag("homed", a.homed) + flag("on", a.enabled)
      + flag("endstop", a.endstop, "warn") + (a.homing ? flag("homing", true, "warn") : "")
      + (a.at_limit ? flag("limit", true, "warn") : "");
  }
  syncSlider("speed", s.speed, pct);
  syncSlider("accel", s.accel, pct);
  syncSlider("smoothing", s.smoothing, pct);
  syncSlider("play-speed", s.play_speed, times, Math.log2);
  if (document.activeElement !== $("play-loop")) $("play-loop").checked = !!s.play_loop;
  renderRecorder(s.recording || {}, s.playback);
}

// ------------------------------------------------------------ presets
function renderPresets() {
  const box = $("presets");
  box.innerHTML = "";
  const saving = $("save-mode").checked;
  for (let i = 1; i <= NUM_PRESETS; i++) {
    const p = presets[String(i)];
    const b = document.createElement("button");
    b.textContent = p ? p.name : `${i}`;
    b.title = p ? JSON.stringify(p.positions) : "empty";
    if (!p) b.classList.add("empty");
    if (saving) b.classList.add("saving");
    b.onclick = () => {
      if ($("save-mode").checked) {
        const name = prompt(`Name for preset ${i}`, p ? p.name : `Preset ${i}`);
        if (name !== null) send({ cmd: "preset_save", preset: i, name, id: "ps" });
        $("save-mode").checked = false; renderPresets();
      } else if (p) {
        send({ cmd: "preset_recall", preset: i, id: "pr" });
      }
    };
    box.appendChild(b);
  }
}
$("save-mode").onchange = renderPresets;

// ------------------------------------------------------------ recorder
function renderRecorder(rec, play) {
  const recording = !!rec.active;
  $("btn-rec").textContent = recording ? "■ Stop recording" : "● Record";
  $("btn-rec").classList.toggle("recording", recording);
  $("btn-keypoint").disabled = !(recording && rec.mode === "keypoints");
  $("btn-rec-cancel").classList.toggle("hidden", !recording);
  $("rec-mode").disabled = recording;
  $("rec-badge").classList.toggle("hidden", !recording);
  $("rec-status").textContent = recording
    ? `Recording (${rec.mode}) - ${rec.elapsed.toFixed(1)} s, ${rec.points} ${rec.mode === "keypoints" ? "keypoints" : "samples"}`
      + (rec.mode === "keypoints" ? " - move, then press + Keypoint" : " - move the head, then press Stop")
    : "";

  $("play-badge").classList.toggle("hidden", !play);
  $("btn-play-stop").disabled = !play;
  $("play-progress").style.width = play ? `${Math.round((play.progress || 0) * 100)}%` : "0";
  $("play-status").textContent = play
    ? `${play.phase === "positioning" ? "Moving to start of" : "Playing"} "${play.name}"`
      + (play.loop ? ` - loop, pass ${play.pass}` : "")
    : "";
  document.querySelectorAll("#recordings tr").forEach((tr) => {
    tr.classList.toggle("playing", !!play && tr.dataset.name === play.name);
  });
}

function renderRecordings() {
  const tb = $("recordings").querySelector("tbody");
  tb.innerHTML = "";
  if (!recordings.length) {
    tb.innerHTML = `<tr><td class="muted">No recording yet.</td></tr>`;
    return;
  }
  for (const r of recordings) {
    const tr = document.createElement("tr");
    tr.dataset.name = r.name;
    tr.innerHTML = `<td>${escapeHtml(r.name)}</td>
      <td class="muted">${r.mode === "keypoints" ? `${r.points} keypoints` : "path"}</td>
      <td class="num">${r.duration.toFixed(1)} s</td>
      <td class="actions">
        <button class="small" data-a="play" title="Replay">▶</button>
        <button class="small" data-a="rename" title="Rename">✎</button>
        <button class="small" data-a="delete" title="Delete">🗑</button></td>`;
    tr.querySelector('[data-a="play"]').onclick = () =>
      send({ cmd: "play", name: r.name, speed: playSpeed(), loop: $("play-loop").checked, id: "pl" });
    tr.querySelector('[data-a="rename"]').onclick = () => {
      const n = prompt("New name", r.name);
      if (n && n !== r.name) send({ cmd: "recording_rename", name: r.name, new: n, id: "rn" });
    };
    tr.querySelector('[data-a="delete"]').onclick = () => {
      if (confirm(`Delete recording "${r.name}"?`)) send({ cmd: "recording_delete", name: r.name, id: "rd" });
    };
    tb.appendChild(tr);
  }
}
function escapeHtml(s) { return s.replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c])); }
const playSpeed = () => Math.pow(2, parseFloat($("play-speed").value));

$("btn-rec").onclick = () => {
  if (status && status.recording && status.recording.active) send({ cmd: "record_stop", id: "rs" });
  else send({ cmd: "record_start", mode: $("rec-mode").value, name: $("rec-name").value.trim(), id: "rb" });
};
$("btn-keypoint").onclick = () => send({ cmd: "record_keypoint", id: "kp" });
$("btn-rec-cancel").onclick = () => send({ cmd: "record_cancel", id: "rc" });
$("btn-play-stop").onclick = () => send({ cmd: "play_stop", id: "ps" });
$("play-loop").onchange = () => send({ cmd: "play_set", loop: $("play-loop").checked });
let playSpeedTimer = null;
$("play-speed").oninput = () => {
  $("play-speed-val").textContent = times(playSpeed());
  clearTimeout(playSpeedTimer);
  playSpeedTimer = setTimeout(() => send({ cmd: "play_set", speed: playSpeed() }), 80);
};

// ------------------------------------------------------------ motion sliders
function motionSlider(id, key) {
  let t = null;
  $(id).oninput = () => {
    const v = parseFloat($(id).value);
    $(`${id}-val`).textContent = pct(v);
    clearTimeout(t);
    t = setTimeout(() => send({ cmd: "set_motion", [key]: v }), 80);
  };
}
motionSlider("speed", "speed");
motionSlider("accel", "accel");
motionSlider("smoothing", "smoothing");

// ------------------------------------------------------------ pad (pan/tilt)
const pad = $("pad"), knob = $("knob");
function padMove(e) {
  const r = pad.getBoundingClientRect();
  let x = ((e.clientX - r.left) / r.width) * 2 - 1;
  let y = -(((e.clientY - r.top) / r.height) * 2 - 1);
  const len = Math.hypot(x, y);
  if (len > 1) { x /= len; y /= len; }
  input.pad = { x, y };
  knob.style.left = `${39 + x * 39}%`;
  knob.style.top = `${39 - y * 39}%`;
}
function padRelease() { input.pad = { x: 0, y: 0 }; knob.style.left = knob.style.top = "39%"; }
pad.addEventListener("pointerdown", (e) => { pad.setPointerCapture(e.pointerId); padMove(e); });
pad.addEventListener("pointermove", (e) => { if (pad.hasPointerCapture(e.pointerId)) padMove(e); });
pad.addEventListener("pointerup", padRelease);
pad.addEventListener("pointercancel", padRelease);

// ------------------------------------------------------------ zoom rocker (springs back)
const zoom = $("zoom");
zoom.addEventListener("input", () => { input.zoom = parseFloat(zoom.value); });
const zoomRelease = () => { zoom.value = 0; input.zoom = 0; };
zoom.addEventListener("pointerup", zoomRelease);
zoom.addEventListener("pointercancel", zoomRelease);
zoom.addEventListener("change", zoomRelease);

// ------------------------------------------------------------ keyboard
const KEYS = { ArrowLeft: ["x", -1], ArrowRight: ["x", 1], ArrowUp: ["y", 1], ArrowDown: ["y", -1],
               PageUp: ["z", 1], PageDown: ["z", -1] };
document.addEventListener("keydown", (e) => {
  if (["INPUT", "SELECT", "TEXTAREA"].includes(e.target.tagName) && e.target.type !== "range"
      && e.target.type !== "checkbox") return;
  if (e.code === "Space") { send({ cmd: "stop" }); e.preventDefault(); return; }
  if (e.key === "Escape") { send({ cmd: "estop" }); e.preventDefault(); return; }
  const k = KEYS[e.key]; if (!k) return;
  input.keys[k[0]] = k[1] * (e.shiftKey ? 0.3 : 1); e.preventDefault();
});
document.addEventListener("keyup", (e) => { const k = KEYS[e.key]; if (k) input.keys[k[0]] = 0; });

// ------------------------------------------------------------ gamepad (browser Gamepad API)
function pollGamepad() {
  const gp = [...(navigator.getGamepads ? navigator.getGamepads() : [])].find((g) => g);
  if (!gp) { input.gp = { x: 0, y: 0, z: 0 }; return; }
  const ax = (i) => { const v = gp.axes[i] || 0; return Math.abs(v) < GAMEPAD_DEADBAND ? 0 : v; };
  // Left stick = pan/tilt, right stick vertical = zoom (standard mapping)
  input.gp = { x: ax(0), y: -ax(1), z: -ax(3) };
}

// ------------------------------------------------------------ jog loop
let wasActive = false;
setInterval(() => {
  pollGamepad();
  const pick = (...v) => v.reduce((a, b) => (Math.abs(b) > Math.abs(a) ? b : a), 0);
  const pan = pick(input.pad.x, input.keys.x, input.gp.x);
  const tilt = pick(input.pad.y, input.keys.y, input.gp.y);
  const z = pick(input.zoom, input.keys.z, input.gp.z);
  const active = Math.abs(pan) > 0.01 || Math.abs(tilt) > 0.01 || Math.abs(z) > 0.01;
  if (active || wasActive) send({ cmd: "jog", pan, tilt, zoom: z });  // last frame sends zeros
  wasActive = active;
}, 1000 / JOG_HZ);

// ------------------------------------------------------------ buttons
$("btn-estop").onclick = () =>
  send(status && status.estop ? { cmd: "clear_estop", id: "ce" } : { cmd: "estop", id: "es" });
$("btn-stop").onclick = () => send({ cmd: "stop", id: "st" });
$("btn-home").onclick = () => send({ cmd: "home", id: "ho" });
$("btn-enable").onclick = () => send({ cmd: "enable", on: true, id: "en" });
$("btn-disable").onclick = () => {
  if (confirm("Release motors? The tilt axis may drop and all axes lose their homing."))
    send({ cmd: "enable", on: false, id: "di" });
};
$("btn-goto").onclick = () => {
  const msg = { cmd: "goto", id: "go" };
  for (const a of config.axes) {
    const v = $(`goto-${a.name}`).value;
    if (v !== "") msg[a.name] = parseFloat(v);
  }
  send(msg);
};

// ------------------------------------------------------------ debug console
const LEVELS = { DEBUG: 10, INFO: 20, WARNING: 30, ERROR: 40, CRITICAL: 50 };
function setDebugUi(on) {
  $("debug-mode").checked = on;
  $("debug-card").classList.toggle("hidden", !on);
  send({ cmd: "logs", on });
}
$("debug-mode").onchange = () => {
  const on = $("debug-mode").checked;
  send({ cmd: "debug", on });
  setDebugUi(on);
};
function fmtTime(t) {
  const d = new Date(t * 1000);
  return d.toTimeString().slice(0, 8) + "." + String(d.getMilliseconds()).padStart(3, "0");
}
function logLine(e) {
  const div = document.createElement("div");
  div.className = `l-${e.level}`;
  div.innerHTML = `<span class="ts">${fmtTime(e.t)}</span> ${e.level.padEnd(7)} `
    + `<span class="src">${escapeHtml(e.name)}</span> ${escapeHtml(e.msg)}`;
  return div;
}
function addLog(e, render = true) {
  logEntries.push(e);
  if (logEntries.length > MAX_LOG) logEntries.splice(0, logEntries.length - MAX_LOG);
  if (!render) return;
  if ((LEVELS[e.level] || 0) < LEVELS[$("log-level").value]) return;
  const box = $("console");
  box.appendChild(logLine(e));
  while (box.childNodes.length > MAX_LOG) box.removeChild(box.firstChild);
  if ($("log-autoscroll").checked) box.scrollTop = box.scrollHeight;
}
function renderLog() {
  const box = $("console"), min = LEVELS[$("log-level").value];
  box.innerHTML = "";
  for (const e of logEntries) if ((LEVELS[e.level] || 0) >= min) box.appendChild(logLine(e));
  box.scrollTop = box.scrollHeight;
}
$("log-level").onchange = renderLog;
$("btn-log-clear").onclick = () => { logEntries = []; renderLog(); };
$("btn-diag").onclick = () => send({ cmd: "diag", id: "diag" });

renderPresets();
renderRecordings();
connect();
