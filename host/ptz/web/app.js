// PTZ web UI - plain JS, no build step.
// Inputs (pad, zoom rocker, keyboard, gamepad) are merged into one jog
// vector which is sent at JOG_HZ while any input is active.
"use strict";

const JOG_HZ = 25;
const NUM_PRESETS = 12;
const MAX_LOG = 1500;
let ws = null, config = null, presets = {}, status = null, recordings = [];
let logEntries = [], myId = null, clients = [];

const $ = (id) => document.getElementById(id);
// per-browser identity (blocking mode is shared by every tab of this browser)
const TOKEN = (() => {
  let t = null;
  try { t = localStorage.getItem("ptz-token"); } catch (e) { /* private mode */ }
  if (!t) {
    t = Array.from(crypto.getRandomValues(new Uint8Array(12)), (b) => b.toString(16).padStart(2, "0")).join("");
    try { localStorage.setItem("ptz-token", t); } catch (e) { /* ignore */ }
  }
  return t;
})();
const input = { pad: { x: 0, y: 0 }, zoom: 0, keys: { x: 0, y: 0, z: 0 }, gp: { x: 0, y: 0, z: 0 } };

// ------------------------------------------------------------ websocket
function connect() {
  ws = new WebSocket(`${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/ws?token=${TOKEN}`);
  ws.onopen = () => { if ($("debug-mode").checked) send({ cmd: "logs", on: true }); };
  ws.onmessage = (e) => {
    const m = JSON.parse(e.data);
    switch (m.type) {
      case "status": renderStatus(m); break;
      case "config": config = m; onConfig(); break;
      case "presets": presets = m.presets; renderPresets(); break;
      case "recordings": recordings = m.recordings; renderRecordings(); renderTrackSelect(); break;
      case "clients": myId = m.you; clients = m.clients; renderClients(); break;
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
  if ((m.cmd === "record_stop" || m.cmd === "track_build") && m.recording && m.recording.mode === "track") {
    showBuild(m.recording);
  }
  if (m.cmd === "diag") {
    addLog({ t: Date.now() / 1000, level: "INFO", name: "diag", msg: JSON.stringify(m.diag, null, 2) });
  }
}
function showError(msg) {
  $("error").textContent = msg || "";
  if (msg) setTimeout(() => { if ($("error").textContent === msg) $("error").textContent = ""; }, 5000);
}
function setConn(ok) { $("conn").textContent = ok ? "online" : "offline"; $("conn").classList.toggle("off", !ok); }
function escapeHtml(s) { return String(s).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c])); }

// ------------------------------------------------------------ units helpers
const axisCfg = (name) => config.axes.find((a) => a.name === name);
function commonUnits() {
  const u = [...new Set(config.axes.map((a) => a.units))];
  return u.length === 1 ? u[0] : "u";
}
const fmtNum = (v) => (v >= 100 ? v.toFixed(0) : v >= 10 ? v.toFixed(1) : v.toFixed(2));
const fmtSpeed = (v, u) => `${fmtNum(v)} ${u}/s`;
const fmtAccel = (v, u) => `${fmtNum(v)} ${u}/s²`;
function fmtPos(v, u) {
  const sign = v < 0 ? "−" : "+";
  return `${sign}${Math.abs(v).toFixed(2)}${u === "°" ? "°" : " " + u}`;
}

// ------------------------------------------------------------ config-driven UI
function onConfig() {
  buildAxesTable();
  buildReadout();
  buildAxisSliders();
  const vmax = Math.max(...config.axes.map((a) => a.max_velocity));
  const amax = Math.max(...config.axes.map((a) => a.max_accel));
  $("speed").max = vmax; $("speed").step = vmax / 200;
  $("accel").max = amax; $("accel").step = amax / 200;
}

function buildReadout() {
  $("readout").innerHTML = config.axes.map((a) => `
    <div class="ax" id="ro-${a.name}"><div class="name"><span>${a.name}</span><span class="tag" id="ro-tag-${a.name}"></span></div>
    <div class="value" id="ro-val-${a.name}">-</div></div>`).join("");
}

function buildAxesTable() {
  const tb = $("axes").querySelector("tbody");
  tb.innerHTML = "";
  for (const a of config.axes) {
    const tr = document.createElement("tr");
    tr.id = `row-${a.name}`;
    tr.innerHTML = `<td><input type="checkbox" id="active-${a.name}" title="Axis in use (off: unpowered, left out of Home all, presets and replays)"></td>
      <td>${a.name}</td><td class="num" id="pos-${a.name}">-</td>
      <td class="num" id="vel-${a.name}">-</td><td id="st-${a.name}"></td>
      <td><input id="goto-${a.name}" type="number" step="any" min="${a.min}" max="${a.max}" title="${a.min}..${a.max} ${a.units}"></td>
      <td><button class="small" title="Home this axis only">Home</button></td>`;
    tr.querySelector("button").onclick = () => send({ cmd: "home", axes: [a.name], id: "ha" });
    tr.querySelector(`#active-${a.name}`).onchange = (e) => setAxisActive(a.name, e.target.checked);
    tb.appendChild(tr);
  }
  const homed = config.axes.filter((a) => a.home_with_all).map((a) => a.name);
  $("btn-home").title = `Homes: ${homed.join(", ") || "none"} (home_with_all in ptz.cfg)`;
}

function buildAxisSliders() {
  const tb = $("axis-sliders").querySelector("tbody");
  tb.innerHTML = "";
  for (const a of config.axes) {
    for (const [kind, max, fmt] of [["speed", a.max_velocity, fmtSpeed], ["accel", a.max_accel, fmtAccel]]) {
      const id = `ax-${kind}-${a.name}`;
      const tr = document.createElement("tr");
      tr.innerHTML = `<td>${a.name} ${kind === "speed" ? "speed" : "accel."}</td>
        <td><input id="${id}" type="range" min="0" max="${max}" step="${max / 200}"></td>
        <td class="val" id="${id}-val"></td>`;
      tb.appendChild(tr);
      let t = null;
      tr.querySelector("input").oninput = (e) => {
        const v = Math.max(parseFloat(e.target.value), max / 200);
        $(`${id}-val`).textContent = fmt(v, a.units);
        clearTimeout(t);
        t = setTimeout(() => send({ cmd: "set_motion", [`axis_${kind}`]: { [a.name]: v } }), 80);
      };
    }
  }
}

// ------------------------------------------------------------ axis on / off
function setAxisActive(name, on) {
  send({ cmd: "axis_active", axis: name, on, id: "act" });
  if (on) showError(`${name} switched on: home it before use`);
}

// ------------------------------------------------------------ status
function flag(text, on, cls = "on") { return `<span class="flag ${on ? cls : ""}">${text}</span>`; }

function syncSlider(id, value, text, transform = (v) => v) {
  const el = $(id);
  if (!el || value === undefined) return;
  if (document.activeElement !== el) el.value = transform(value);
  if (document.activeElement !== el) $(`${id}-val`).textContent = text;
}
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
    p.textContent = fmtPos(a.pos, a.units);
    $(`vel-${name}`).textContent = `${a.vel.toFixed(1)} ${a.units}/s`;
    $(`st-${name}`).innerHTML = flag("homed", a.homed) + flag("on", a.enabled)
      + flag("endstop", a.endstop, "warn") + (a.homing ? flag("homing", true, "warn") : "")
      + (a.at_limit ? flag("limit", true, "warn") : "");
    const act = $(`active-${name}`);
    if (act && document.activeElement !== act) act.checked = a.active !== false;
    $(`row-${name}`).classList.toggle("inactive", a.active === false);
    if (name === "zoom") {
      const z = $("zoom-active");
      if (document.activeElement !== z) z.checked = a.active !== false;
      $("zoom").disabled = a.active === false;
    }
    const ro = $(`ro-${name}`);
    if (ro) {
      ro.classList.toggle("inactive", a.active === false);
      $(`ro-val-${name}`).textContent = fmtPos(a.pos, a.units);
      ro.classList.toggle("unhomed", !a.homed);
      ro.classList.toggle("moving", a.moving);
      $(`ro-tag-${name}`).textContent = a.active === false ? "off" : a.homing ? "homing" : !a.homed ? "not homed"
        : a.at_limit ? "limit" : a.endstop ? "endstop" : "";
    }
  }
  if (config && s.settings) renderSettings(s.settings);
  renderLock(s.lock);
  renderClients();
  renderRecorder(s.recording || {}, s.playback);
  renderTracking(s.tracking || {}, s.recording || {});
  $("config-banner").classList.toggle("hidden", !s.config_error);
  $("config-banner").textContent = s.config_error ? `Configuration: ${s.config_error}` : "";
}

function renderSettings(st) {
  const u = commonUnits();
  $("advanced").checked = st.advanced;
  $("simple-sliders").classList.toggle("hidden", st.advanced);
  $("axis-sliders").classList.toggle("hidden", !st.advanced);
  syncSlider("speed", st.speed_all, fmtSpeed(st.speed_all, u));
  syncSlider("accel", st.accel_all, fmtAccel(st.accel_all, u));
  for (const a of config.axes) {
    syncSlider(`ax-speed-${a.name}`, st.speed[a.name], fmtSpeed(st.speed[a.name], a.units));
    syncSlider(`ax-accel-${a.name}`, st.accel[a.name], fmtAccel(st.accel[a.name], a.units));
  }
  // in simple mode, show where an axis is capped by its own maximum
  const capped = config.axes.filter((a) => st.effective_speed[a.name] < st.speed_all - 1e-6)
    .map((a) => `${a.name} ${fmtSpeed(st.effective_speed[a.name], a.units)}`);
  $("speed").title = capped.length ? `Capped: ${capped.join(", ")}` : "";
  syncSlider("smoothing", st.smoothing, st.smoothing > 0 ? `${st.ease_s.toFixed(2)} s` : "off");
  syncSlider("play-speed", st.play_speed, times(st.play_speed), Math.log2);
  if (document.activeElement !== $("play-loop")) $("play-loop").checked = !!st.play_loop;
}

// ------------------------------------------------------------ motion settings
function setupSimpleSlider(id, key, fmt) {
  let t = null;
  $(id).oninput = () => {
    const el = $(id), v = Math.max(parseFloat(el.value), parseFloat(el.max) / 200);
    $(`${id}-val`).textContent = fmt(v, commonUnits());
    clearTimeout(t);
    t = setTimeout(() => send({ cmd: "set_motion", [key]: v }), 80);
  };
}
setupSimpleSlider("speed", "speed", fmtSpeed);
setupSimpleSlider("accel", "accel", fmtAccel);
let smoothTimer = null;
$("smoothing").oninput = () => {
  const v = parseFloat($("smoothing").value);
  $("smoothing-val").textContent = v > 0 && config ? `${(v * config.ease_time).toFixed(2)} s` : "off";
  clearTimeout(smoothTimer);
  smoothTimer = setTimeout(() => send({ cmd: "set_motion", smoothing: v }), 80);
};
$("advanced").onchange = () => send({ cmd: "set_motion", advanced: $("advanced").checked, id: "adv" });

// ------------------------------------------------------------ clients + blocking mode
function lockedOut() { return !!(status && status.lock && status.lock.owner !== myId); }

function renderLock(lock) {
  const btn = $("btn-lock"), banner = $("lock-banner");
  const mine = !!lock && lock.owner === myId, other = !!lock && !mine;
  btn.textContent = mine ? "🔓 Release control" : other ? "🔒 Locked" : "🔒 Take control";
  btn.classList.toggle("owner", mine);
  btn.classList.toggle("other", other);
  btn.title = other ? `Locked by ${lock.label}` : mine
    ? "You have exclusive control. Click to release it."
    : "Blocking mode: only you can move the head";
  document.body.classList.toggle("locked-out", other);
  banner.classList.toggle("hidden", !lock);
  banner.classList.toggle("owner", mine);
  banner.textContent = mine
    ? "Blocking mode: you have exclusive control of the head. Nobody else can move it."
    : other ? `Control locked by ${lock.label}. Only Stop and E-STOP are available.` : "";
}
$("btn-lock").onclick = () => {
  if (!status) return;
  if (!status.lock) send({ cmd: "lock", id: "lk" });
  else if (status.lock.owner === myId) send({ cmd: "unlock", id: "ul" });
  else showError(`Control locked by ${status.lock.label}`);
};

function renderClients() {
  $("client-count").textContent = clients.length;
  const sources = (status && status.sources) || [];
  const rows = clients.map((c) => `<div class="${c.id === myId ? "me" : ""}">${c.owner ? "🔒 " : ""}`
    + `${escapeHtml(c.ip)} - ${escapeHtml(c.agent)}${c.id === myId ? " (this page)" : ""}</div>`).join("");
  const other = sources.map((s) => `<div>${s.kind.toUpperCase()} ${escapeHtml(s.ip)} - ${s.age.toFixed(0)} s ago</div>`).join("");
  $("clients-pop").innerHTML = `<h3>Web pages (${clients.length})</h3>${rows || "<div>none</div>"}`
    + (other ? `<h3>Network controllers (last 10 s)</h3>${other}` : "");
  $("btn-clients").title = `${clients.length} web page(s) connected`
    + (sources.length ? `, ${sources.length} network controller(s) active` : "");
}
$("btn-clients").onclick = (e) => { e.stopPropagation(); $("clients-pop").classList.toggle("hidden"); };
document.addEventListener("click", (e) => {
  if (!$("clients-pop").contains(e.target)) $("clients-pop").classList.add("hidden");
});

// ------------------------------------------------------------ presets
function renderPresets() {
  const box = $("presets");
  box.innerHTML = "";
  const saving = $("save-mode").checked;
  for (let i = 1; i <= NUM_PRESETS; i++) {
    const p = presets[String(i)];
    const b = document.createElement("button");
    b.textContent = p ? p.name : `${i}`;
    b.title = p ? Object.entries(p.positions).map(([k, v]) => `${k} ${v.toFixed(2)}`).join(", ") : "empty";
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
  $("btn-keypoint").disabled = !(recording && rec.mode === "keypoints" && !rec.armed);
  $("btn-rec-cancel").classList.toggle("hidden", !recording);
  $("rec-mode").disabled = recording;
  $("rec-on-move").disabled = recording;
  $("rec-badge").classList.toggle("hidden", !recording);
  $("rec-badge").textContent = rec.armed ? "● ARMED" : "● REC";
  let text = "";
  if (recording && rec.armed) {
    text = "Armed: recording starts as soon as the head moves.";
  } else if (recording) {
    text = `Recording (${rec.mode}) - ${rec.elapsed.toFixed(1)} s, ${rec.points} ${rec.mode === "keypoints" ? "keypoints" : "samples"}`
      + (rec.mode === "keypoints" ? " - move, then press + Keypoint" : " - move the head, then press Stop");
  }
  $("rec-status").textContent = text;

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
    const kind = { keypoints: `${r.points} keypoints`, track: `track ${r.laps ? r.laps.filter((l) => l.used).length + " laps" : ""}`,
                   "laps-raw": `learning ${r.marks ? r.marks - 1 + " laps" : ""}` }[r.mode] || "path";
    tr.innerHTML = `<td>${escapeHtml(r.name)}</td>
      <td class="muted">${kind}</td>
      <td class="num">${r.duration.toFixed(1)} s</td>
      <td class="actions">
        <button class="small" data-a="play" title="Replay">▶</button>
        <a class="small btn" href="/api/recordings/${encodeURIComponent(r.name)}" download title="Download (JSON)">⬇</a>
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
const playSpeed = () => Math.pow(2, parseFloat($("play-speed").value));

$("btn-rec").onclick = () => {
  if (status && status.recording && status.recording.active) send({ cmd: "record_stop", id: "rs" });
  else send({ cmd: "record_start", mode: $("rec-mode").value, name: $("rec-name").value.trim(),
              on_move: $("rec-on-move").checked, id: "rb" });
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

// upload a path (same JSON format as the download)
$("btn-upload").onclick = () => $("upload-file").click();
$("upload-file").onchange = async () => {
  const file = $("upload-file").files[0];
  $("upload-file").value = "";
  if (!file) return;
  let data;
  try { data = JSON.parse(await file.text()); } catch (e) { showError(`${file.name}: not a JSON file`); return; }
  const name = (data && data.name) || file.name.replace(/\.json$/i, "");
  try {
    const r = await fetch(`/api/recordings?name=${encodeURIComponent(name)}`, {
      method: "POST", body: JSON.stringify(data),
      headers: { "Content-Type": "application/json", "X-PTZ-Client": TOKEN },
    });
    const res = await r.json();
    if (!res.ok) showError(`Upload refused: ${res.error}`);
    else $("rec-status").textContent = `Uploaded "${res.recording.name}" (${res.recording.points} points, ${res.recording.duration.toFixed(1)} s)`;
  } catch (e) { showError(`Upload failed: ${e}`); }
};

// ------------------------------------------------------------ race tracking
const goAllowed = (phase) => ["armed", "arming", "running", "done"].includes(phase);
const learning = () => !!(status && status.recording && status.recording.active && status.recording.mode === "laps");
function lapMark() {
  send({ cmd: "lap_mark", id: "lap" });
  const b = $("btn-lap");
  b.classList.add("flash");
  setTimeout(() => b.classList.remove("flash"), 150);
}
$("btn-lap").onclick = lapMark;
$("btn-learn").onclick = () => {
  if (learning()) send({ cmd: "record_stop", id: "learn-stop" });
  else send({ cmd: "record_start", mode: "laps", name: $("learn-name").value.trim(), id: "learn" });
};
$("btn-learn-cancel").onclick = () => {
  if (confirm("Discard this learning session?")) send({ cmd: "record_cancel", id: "lc" });
};

let lastBuild = null;
function showBuild(track) {
  lastBuild = track;
  $("build-result").classList.remove("hidden");
  const tb = $("lap-table").querySelector("tbody");
  tb.innerHTML = "";
  for (const l of track.laps || []) {
    const tr = document.createElement("tr");
    tr.className = l.used ? "" : "unused";
    tr.innerHTML = `<td><input type="checkbox" data-lap="${l.lap}" ${l.used ? "checked" : ""}></td>
      <td>${l.lap}</td><td class="num">${l.duration.toFixed(2)} s</td><td class="num">${l.deviation.toFixed(2)}</td>`;
    tb.appendChild(tr);
  }
  $("build-info").textContent = `"${track.name}": lap time ${track.lap_time.toFixed(2)} s`;
  renderTrackSelect(track.name);
}
$("btn-rebuild").onclick = () => {
  if (!lastBuild) return;
  const exclude = [...document.querySelectorAll("#lap-table input[data-lap]")]
    .filter((c) => !c.checked).map((c) => parseInt(c.dataset.lap, 10));
  send({ cmd: "track_build", source: lastBuild.source, name: lastBuild.name, exclude, id: "rebuild" });
};

function renderTrackSelect(selectName) {
  const sel = $("track-select"), cur = selectName || sel.value;
  const tracks = recordings.filter((r) => r.mode === "track");
  sel.innerHTML = tracks.length
    ? tracks.map((r) => `<option value="${escapeHtml(r.name)}">${escapeHtml(r.name)} - ${(r.lap_time || r.duration).toFixed(1)} s</option>`).join("")
    : `<option value="">no learned track yet</option>`;
  if (tracks.some((r) => r.name === cur)) sel.value = cur;
}

function renderTracking(tr, rec) {
  const isLearning = rec.active && rec.mode === "laps";
  $("btn-learn").textContent = isLearning ? "■ Stop & build" : "● Start learning";
  $("btn-learn").classList.toggle("recording", isLearning);
  $("btn-learn-cancel").classList.toggle("hidden", !isLearning);
  $("learn-active").classList.toggle("hidden", !isLearning);
  if (isLearning) {
    $("learn-status").textContent = rec.marks === 0
      ? "Recording. Press LAP / Space when the car crosses the start line."
      : `${rec.laps} lap(s) done` + (rec.last_lap ? `, last ${rec.last_lap.toFixed(2)} s` : "")
        + ` - current lap ${rec.lap_elapsed.toFixed(1)} s`;
  }
  const phase = tr.phase || "idle";
  $("btn-go").disabled = !goAllowed(phase);
  $("btn-arm").disabled = phase === "arming" || phase === "running";
  $("btn-track-abort").disabled = phase === "idle" || phase === "done";
  $("track-progress").style.width = tr.progress != null ? `${Math.round(tr.progress * 100)}%` : "0";
  $("track-speed").textContent = tr.lap_time
    ? `Learned lap ${tr.lap_time.toFixed(2)} s - current lap ${tr.current_lap.toFixed(2)} s (×${tr.speed_eff.toFixed(2)})`
      + (tr.last_interval ? ` - last GO-to-GO ${tr.last_interval.toFixed(2)} s` : "")
      + (tr.adjust_note ? ` - ${tr.adjust_note}` : "")
    : "";
  if (document.activeElement !== $("auto-rearm")) $("auto-rearm").checked = tr.auto_rearm !== false;
  if (document.activeElement !== $("auto-adjust")) $("auto-adjust").checked = !!tr.auto_adjust;
  if (document.activeElement !== $("adjust-tol") && tr.adjust_tolerance)
    $("adjust-tol").value = Math.round(tr.adjust_tolerance * 100);
  if (document.activeElement !== $("target-lap")) $("target-lap").value = tr.target_lap || "";
  $("track-status").textContent = {
    idle: "", done: `Lap done (${tr.runs}). Press ARM for the next one.`,
    arming: `Moving to the start point of "${tr.track}"…`,
    armed: `ARMED on "${tr.track}": waiting for GO` + (tr.runs ? ` (${tr.runs} lap(s) done)` : ""),
    running: `Tracking lap ${tr.runs + 1}… (GO again = next lap)`,
  }[phase] || phase;
}
$("btn-arm").onclick = () => {
  const name = $("track-select").value;
  if (!name) { showError("learn a track first"); return; }
  send({ cmd: "track_arm", name, id: "arm" });
};
$("btn-go").onclick = () => send({ cmd: "track_go", id: "go" });
$("btn-track-abort").onclick = () => send({ cmd: "track_abort", id: "ta" });
$("auto-rearm").onchange = () => send({ cmd: "track_set", auto_rearm: $("auto-rearm").checked });
$("auto-adjust").onchange = () => send({ cmd: "track_set", auto_adjust: $("auto-adjust").checked, id: "aa" });
$("adjust-tol").onchange = () => {
  const v = parseFloat($("adjust-tol").value);
  if (!isNaN(v)) send({ cmd: "track_set", adjust_tolerance: v / 100, id: "at" });
};
$("target-lap").onchange = () => {
  const v = parseFloat($("target-lap").value);
  send({ cmd: "track_set", target_lap: isNaN(v) ? 0 : v, id: "tl" });
};

$("zoom-active").onchange = () => setAxisActive("zoom", $("zoom-active").checked);

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
  if (e.code === "Space") {
    if (learning()) lapMark(); else send({ cmd: "stop" });
    e.preventDefault(); return;
  }
  if (e.key === "Enter" && status && status.tracking && goAllowed(status.tracking.phase)) {
    send({ cmd: "track_go", id: "go" }); e.preventDefault(); return;
  }
  if (e.key === "Escape") { send({ cmd: "estop" }); e.preventDefault(); return; }
  const k = KEYS[e.key]; if (!k) return;
  input.keys[k[0]] = k[1] * (e.shiftKey ? 0.3 : 1); e.preventDefault();
});
document.addEventListener("keyup", (e) => {
  const k = KEYS[e.key]; if (k) input.keys[k[0]] = 0;
  // Space is our hotkey: never let it also "click" the focused button
  if (e.code === "Space" && e.target.tagName === "BUTTON") e.preventDefault();
});

// ------------------------------------------------------------ controller (browser Gamepad API)
// Works with any gamepad / HID joystick seen by the OS. The pad that moved
// last is used ("auto"), so other devices listed by Windows (virtual
// joysticks, headsets...) do not hide it. Mapping is stored per device in
// this browser. Button numbers are shown 1-based like Windows' joy.cpl.
const GP_ACTIONS = [["", "-"], ["lapgo", "Lap / GO (race)"], ["lap", "Lap mark"], ["go", "GO"],
  ["arm", "ARM track"], ["stop", "Stop"], ["estop", "E-STOP"], ["home", "Home all"],
  ["center", "Go to 0 / 0"],
  ["rec", "Record start / stop"]]
  .concat(Array.from({ length: NUM_PRESETS }, (_, i) => [`preset:${i + 1}`, `Preset ${i + 1}`]));
const gp = { id: null, map: null, prevAxes: {}, prevButtons: [], activity: {}, detect: null,
             baseline: null, sliderLast: null, zoomFollow: false, error: "", tick: 0, everSeen: false };
input.gpScale = 1;

function gpDefaults(pad) {
  const std = pad.mapping === "standard";
  const buttons = {};
  if (!std) {                       // custom controller: 1..5 = presets, 6 = Lap / GO
    for (let i = 0; i < Math.min(5, pad.buttons.length); i++) buttons[i] = `preset:${i + 1}`;
    if (pad.buttons.length > 5) buttons[5] = "lapgo";
  }
  return { pan: 0, panInv: false, tilt: 1, tiltInv: true,
           zoom: std && pad.axes.length > 3 ? 3 : -1, zoomInv: true,
           slider: !std && pad.axes.length > 2 ? 2 : -1, sliderInv: false, sliderMode: "speed",
           deadband: 0.08, buttons };
}
function gpLoadMap(pad) {
  try {
    const m = JSON.parse(localStorage.getItem(`ptz-gp:${pad.id}`));
    if (m) return { ...gpDefaults(pad), ...m };
  } catch (e) { /* defaults */ }
  return gpDefaults(pad);
}
function gpSaveMap() { try { localStorage.setItem(`ptz-gp:${gp.id}`, JSON.stringify(gp.map)); } catch (e) { /* */ } }
function gpChoice() { try { return localStorage.getItem("ptz-gp-choice") || "auto"; } catch (e) { return "auto"; } }

function gpList() {
  try {
    gp.error = "";
    return [...(navigator.getGamepads ? navigator.getGamepads() : [])].filter(Boolean);
  } catch (e) {                    // older Chrome: blocked on http:// LAN pages
    gp.error = `${e.name}: ${e.message}`;
    return [];
  }
}

function gpPick(pads) {
  const now = performance.now();
  for (const p of pads) {         // remember which device the operator touches
    const prev = gp.prevAxes[p.index];
    const moved = prev && p.axes.some((v, i) => Math.abs(v - (prev[i] || 0)) > 0.15);
    if (moved || p.buttons.some((b) => b.pressed)) gp.activity[p.id] = now;
    gp.prevAxes[p.index] = [...p.axes];
  }
  const choice = gpChoice();
  if (choice !== "auto") return pads.find((p) => p.id === choice) || null;
  return pads.slice().sort((a, b) => (gp.activity[b.id] || 0) - (gp.activity[a.id] || 0))[0] || null;
}

function gpAxis(pad, index, inv, deadband) {
  if (index == null || index < 0 || index >= pad.axes.length) return 0;
  let v = pad.axes[index] || 0;
  if (!(Math.abs(v) <= 1.5)) return 0;              // invalid axis value: ignore
  v = Math.max(-1, Math.min(1, v));
  if (inv) v = -v;
  if (deadband === undefined) return v;
  const a = Math.abs(v);
  return a <= deadband ? 0 : Math.sign(v) * (a - deadband) / (1 - deadband);
}

function gpAction(a) {
  if (!a) return;
  if (a.startsWith("preset:")) { send({ cmd: "preset_recall", preset: parseInt(a.slice(7), 10), id: "gp" }); return; }
  const tr = status && status.tracking;
  switch (a) {
    case "lapgo":                 // same button for learning and live tracking
      if (learning()) lapMark();
      else if (tr && goAllowed(tr.phase)) send({ cmd: "track_go", id: "go" });
      else showError("Lap / GO button: start learning or ARM a track first");
      break;
    case "lap": lapMark(); break;
    case "go": send({ cmd: "track_go", id: "go" }); break;
    case "arm": $("btn-arm").click(); break;
    case "stop": send({ cmd: "stop", id: "st" }); break;
    case "estop": send({ cmd: "estop", id: "es" }); break;
    case "home": send({ cmd: "home", id: "ho" }); break;
    case "center": goCenter(); break;
    case "rec": $("btn-rec").click(); break;
  }
}

function pollController() {
  const pads = gpList();
  const pad = gpPick(pads);
  if (pads.length) gp.everSeen = true;
  if (!pad) {
    input.gp = { x: 0, y: 0, z: 0 }; input.gpScale = 1;
    if (gp.id !== null) { gp.id = null; buildGpUi(null, pads); }
    if (gp.tick++ % 5 === 0) renderGpLive(null, pads);
    return;
  }
  if (pad.id !== gp.id) {         // new device: load its mapping, rebuild the panel
    gp.id = pad.id; gp.map = gpLoadMap(pad); gp.prevButtons = []; gp.sliderLast = null;
    buildGpUi(pad, pads);
  }
  const m = gp.map;
  if (gp.detect && !gp.baseline) gp.baseline = [...pad.axes];
  if (gp.detect) {                 // "detect": the axis that moves most gets assigned
    let best = -1, amount = 0.5;
    pad.axes.forEach((v, i) => {
      if (Math.abs(v) > 1.5 || Math.abs(gp.baseline[i] || 0) > 1.5) return;   // unused / invalid axis
      const d = Math.abs(v - (gp.baseline[i] || 0));
      if (d > amount) { amount = d; best = i; }
    });
    if (best >= 0) {
      const key = gp.detect.replace("gp-", "");
      m[key] = best; gpSaveMap(); $(gp.detect).value = best;
      document.querySelector(`[data-detect="${gp.detect}"]`).classList.remove("gp-detecting");
      gp.detect = null;
    }
  }
  let x = gpAxis(pad, m.pan, m.panInv, m.deadband);
  let y = gpAxis(pad, m.tilt, m.tiltInv, m.deadband);
  let z = gpAxis(pad, m.zoom, m.zoomInv, m.deadband);
  let scale = 1;
  if (m.slider >= 0 && m.slider < pad.axes.length) {
    const t = (gpAxis(pad, m.slider, m.sliderInv) + 1) / 2;          // 0..1
    if (m.sliderMode === "speed") {
      scale = 0.05 + 0.95 * t;
    } else if (m.sliderMode === "zoom_rate") {
      const r = 2 * t - 1;
      z = Math.abs(r) < 0.1 ? z : (Math.abs(r) > Math.abs(z) ? r : z);
    } else if (m.sliderMode === "zoom_pos" && config && status) {
      const zc = axisCfg("zoom"), zs = status.axes.zoom;
      if (zc && zs && zs.homed) {
        const range = zc.max - zc.min, target = zc.min + t * range;
        if (gp.sliderLast === null || Math.abs(t - gp.sliderLast) > 0.004) gp.zoomFollow = true;
        gp.sliderLast = t;
        const err = target - zs.pos;
        if (gp.zoomFollow && Math.abs(err) < 0.004 * range) gp.zoomFollow = false;
        if (gp.zoomFollow) {        // follow the slider; stops once reached
          const f = Math.max(-1, Math.min(1, err / (0.08 * range)));
          z = Math.abs(f) > Math.abs(z) ? f : z;
        }
      }
    }
  }
  input.gpScale = scale;              // applied on the Pi, after the deadband
  input.gp = { x, y, z };
  pad.buttons.forEach((b, i) => {  // actions on press (rising edge)
    const on = b.pressed || b.value > 0.5;
    if (on && !gp.prevButtons[i]) gpAction(m.buttons[i]);
    gp.prevButtons[i] = on;
  });
  if (gp.tick++ % 3 === 0) renderGpLive(pad, pads);
}

function buildGpUi(pad, pads) {
  const sel = $("gp-select"), choice = gpChoice();
  sel.innerHTML = `<option value="auto">Auto (the one you touch)</option>`
    + pads.map((p) => `<option value="${escapeHtml(p.id)}">${escapeHtml(p.id)}</option>`).join("");
  sel.value = pads.some((p) => p.id === choice) ? choice : "auto";
  const axisOpts = (n) => `<option value="-1">-</option>`
    + Array.from({ length: n }, (_, i) => `<option value="${i}">Axis ${i}</option>`).join("");
  for (const k of ["pan", "tilt", "zoom", "slider"]) {
    $(`gp-${k}`).innerHTML = axisOpts(pad ? pad.axes.length : 0);
    if (pad) { $(`gp-${k}`).value = gp.map[k]; $(`gp-${k}-inv`).checked = !!gp.map[`${k}Inv`]; }
  }
  if (!pad) { $("gp-buttons").innerHTML = ""; return; }
  $("gp-slider-mode").value = gp.map.sliderMode;
  $("gp-deadband").value = gp.map.deadband;
  const opts = GP_ACTIONS.map(([v, t]) => `<option value="${v}">${t}</option>`).join("");
  $("gp-buttons").innerHTML = pad.buttons.map((_, i) =>
    `<label>Button ${i + 1}<select data-btn="${i}">${opts}</select></label>`).join("");
  document.querySelectorAll("#gp-buttons select").forEach((s) => {
    s.value = gp.map.buttons[s.dataset.btn] || "";
    s.onchange = () => { gp.map.buttons[s.dataset.btn] = s.value; gpSaveMap(); };
  });
}

function renderGpLive(pad, pads) {
  const warn = $("gp-warning");
  let w = "";
  if (gp.error) {
    w = `This browser blocks game controllers on this page (${gp.error}). Use a current Chrome / Edge, `
      + `or allow it: chrome://flags/#unsafely-treat-insecure-origin-as-secure → add ${location.origin}`;
  } else if (!navigator.getGamepads) {
    w = "This browser has no Gamepad API.";
  } else if (document.hidden) {
    w = "The page is hidden: browsers stop reading controllers in background tabs.";
  }
  warn.textContent = w;
  warn.classList.toggle("hidden", !w);
  if (!pad) {
    $("gp-info").innerHTML = gp.everSeen ? "Controller disconnected."
      : "<b>No controller visible yet.</b> Browsers only show a controller after a button "
        + "press or a full stick movement on this page: press any button on it.";
    $("gp-live").innerHTML = "";
    return;
  }
  if (gp.detect) {
    $("gp-info").innerHTML = `<b>Detect:</b> move the axis for "${gp.detect.replace("gp-", "")}" fully.`;
  }
  const m = gp.map;
  const role = { [m.pan]: "pan", [m.tilt]: "tilt", [m.zoom]: "zoom", [m.slider]: "slider" };
  let info = `${pads.length} device(s) - using "${pad.id}" (${pad.mapping || "generic"} mapping, `
    + `${pad.axes.length} axes, ${pad.buttons.length} buttons)`;
  if (m.slider >= 0) {
    info += m.sliderMode === "speed" ? ` - joystick speed ${Math.round(input.gpScale * 100)} %`
      : m.sliderMode === "zoom_pos" ? " - slider = zoom position" : " - slider = zoom rate";
  }
  if (!gp.detect) $("gp-info").textContent = info;
  $("gp-live").innerHTML = pad.axes.map((raw, i) => {
    const bad = !(Math.abs(raw) <= 1.5), v = bad ? 0 : Math.max(-1, Math.min(1, raw));
    const left = v < 0 ? 50 + v * 50 : 50, width = Math.abs(v) * 50;
    return `<div class="gp-axisbar ${role[i] ? "used" : ""}"><span class="name">Axis ${i}${role[i] ? " · " + role[i] : ""}</span>
      <span class="bar"><span class="fill" style="left:${left}%;width:${width}%"></span></span>
      <span class="num">${bad ? "n/a" : v.toFixed(2)}</span></div>`;
  }).join("") + `<div style="grid-column:1/-1">${pad.buttons.map((b, i) =>
    `<span class="gp-btn ${b.pressed || b.value > 0.5 ? "on" : ""}" title="${escapeHtml((GP_ACTIONS.find(([v]) => v === (m.buttons[i] || "")) || ["", ""])[1])}">${i + 1}</span>`).join("")}</div>`;
}

$("gp-select").onchange = () => {
  try { localStorage.setItem("ptz-gp-choice", $("gp-select").value); } catch (e) { /* */ }
  gp.id = null;                    // rebuild on next poll
};
for (const k of ["pan", "tilt", "zoom", "slider"]) {
  $(`gp-${k}`).onchange = () => { if (gp.map) { gp.map[k] = parseInt($(`gp-${k}`).value, 10); gpSaveMap(); } };
  $(`gp-${k}-inv`).onchange = () => { if (gp.map) { gp.map[`${k}Inv`] = $(`gp-${k}-inv`).checked; gpSaveMap(); } };
}
$("gp-slider-mode").onchange = () => { if (gp.map) { gp.map.sliderMode = $("gp-slider-mode").value; gp.sliderLast = null; gpSaveMap(); } };
$("gp-deadband").onchange = () => {
  const v = parseFloat($("gp-deadband").value);
  if (gp.map && !isNaN(v)) { gp.map.deadband = Math.max(0, Math.min(0.5, v)); gpSaveMap(); }
};
document.querySelectorAll("[data-detect]").forEach((b) => {
  b.onclick = () => {
    document.querySelectorAll("[data-detect]").forEach((o) => o.classList.remove("gp-detecting"));
    const pad = gpList().find((p) => p.id === gp.id);
    gp.detect = b.dataset.detect;
    gp.baseline = pad ? [...pad.axes] : null;   // no pad yet: taken when it appears
    b.classList.add("gp-detecting");
  };
});
$("gp-reset").onclick = () => {
  const pad = gpList().find((p) => p.id === gp.id);
  if (!pad) return;
  try { localStorage.removeItem(`ptz-gp:${pad.id}`); } catch (e) { /* */ }
  gp.id = null;
};
window.addEventListener("gamepadconnected", (e) => console.log("gamepad connected:", e.gamepad.id));
buildGpUi(null, []);

// ------------------------------------------------------------ jog loop
let wasActive = false;
setInterval(() => {
  pollController();
  if (lockedOut()) { wasActive = false; return; }     // someone else has control
  const pick = (...v) => v.reduce((a, b) => (Math.abs(b) > Math.abs(a) ? b : a), 0);
  const pan = pick(input.pad.x, input.keys.x, input.gp.x);
  const tilt = pick(input.pad.y, input.keys.y, input.gp.y);
  const z = pick(input.zoom, input.keys.z, input.gp.z);
  const active = Math.abs(pan) > 0.01 || Math.abs(tilt) > 0.01 || Math.abs(z) > 0.01;
  if (active || wasActive) {               // last frame sends zeros
    const msg = { cmd: "jog", pan, tilt, zoom: z };
    if (input.gpScale < 1) {               // controller speed slider: only where it drives
      const scale = {};
      if (pan && pan === input.gp.x) scale.pan = input.gpScale;
      if (tilt && tilt === input.gp.y) scale.tilt = input.gpScale;
      if (Object.keys(scale).length) msg.scale = scale;
    }
    send(msg);
  }
  wasActive = active;
}, 1000 / JOG_HZ);

// ------------------------------------------------------------ buttons
$("btn-estop").onclick = () =>
  send(status && status.estop ? { cmd: "clear_estop", id: "ce" } : { cmd: "estop", id: "es" });
$("btn-stop").onclick = () => send({ cmd: "stop", id: "st" });
$("btn-home").onclick = () => send({ cmd: "home", id: "ho" });
function goCenter() {
  const msg = { cmd: "goto", id: "c0" };
  for (const n of ["pan", "tilt"]) if (status && status.axes[n] && status.axes[n].active !== false) msg[n] = 0;
  send(msg);
}
$("btn-center").onclick = goCenter;
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
renderTrackSelect();
connect();
