// PTZ web UI - plain JS, no build step.
// Inputs (pad, zoom rocker, keyboard, gamepad) are merged into one jog
// vector which is sent at JOG_HZ while any input is active.
"use strict";

const JOG_HZ = 25;
const NUM_PRESETS = 12;
let ws = null, config = null, presets = {}, status = null;

const $ = (id) => document.getElementById(id);
const input = { pad: { x: 0, y: 0 }, zoom: 0, keys: { x: 0, y: 0, z: 0 }, pad_gp: { x: 0, y: 0, z: 0 } };

// ------------------------------------------------------------ websocket
function connect() {
  ws = new WebSocket(`${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/ws`);
  ws.onmessage = (e) => {
    const m = JSON.parse(e.data);
    if (m.type === "status") renderStatus(m);
    else if (m.type === "config") { config = m; buildAxesTable(); }
    else if (m.type === "presets") { presets = m.presets; renderPresets(); }
    else if (m.type === "reply" && !m.ok) showError(m.error);
  };
  ws.onclose = () => { setConn(false); setTimeout(connect, 1000); };
}
function send(obj) { if (ws && ws.readyState === 1) ws.send(JSON.stringify(obj)); }
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
      <td><input id="goto-${a.name}" type="number" step="any" min="${a.min}" max="${a.max}"></td>`;
    tb.appendChild(tr);
  }
}
function flag(text, on, cls = "on") { return `<span class="flag ${on ? cls : ""}">${text}</span>`; }
function renderStatus(s) {
  status = s;
  setConn(s.connected);
  $("estop-badge").classList.toggle("hidden", !s.estop);
  if (s.error) showError(s.error);
  for (const [name, a] of Object.entries(s.axes)) {
    const p = $(`pos-${name}`); if (!p) continue;
    p.textContent = a.pos.toFixed(2);
    $(`vel-${name}`).textContent = a.vel.toFixed(1);
    $(`st-${name}`).innerHTML = flag("homed", a.homed) + flag("on", a.enabled)
      + flag("endstop", a.endstop, "warn") + (a.homing ? flag("homing", true, "warn") : "")
      + (a.at_limit ? flag("limit", true, "warn") : "");
  }
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
  if (e.target.tagName === "INPUT" && e.target.type === "number") return;
  if (e.code === "Space") { send({ cmd: "stop" }); e.preventDefault(); return; }
  const k = KEYS[e.key]; if (!k) return;
  input.keys[k[0]] = k[1] * (e.shiftKey ? 0.3 : 1); e.preventDefault();
});
document.addEventListener("keyup", (e) => { const k = KEYS[e.key]; if (k) input.keys[k[0]] = 0; });

// ------------------------------------------------------------ gamepad (browser Gamepad API)
function pollGamepad() {
  const gp = [...(navigator.getGamepads ? navigator.getGamepads() : [])].find((g) => g);
  if (!gp) { input.pad_gp = { x: 0, y: 0, z: 0 }; return; }
  const ax = (i) => (gp.axes[i] || 0);
  // Left stick = pan/tilt, right stick vertical = zoom (standard mapping)
  input.pad_gp = { x: ax(0), y: -ax(1), z: -ax(3) };
}

// ------------------------------------------------------------ jog loop
let wasActive = false;
setInterval(() => {
  pollGamepad();
  const pick = (...v) => v.reduce((a, b) => (Math.abs(b) > Math.abs(a) ? b : a), 0);
  const pan = pick(input.pad.x, input.keys.x, input.pad_gp.x);
  const tilt = pick(input.pad.y, input.keys.y, input.pad_gp.y);
  const z = pick(input.zoom, input.keys.z, input.pad_gp.z);
  const active = Math.abs(pan) > 0.01 || Math.abs(tilt) > 0.01 || Math.abs(z) > 0.01;
  if (active || wasActive) send({ cmd: "jog", pan, tilt, zoom: z });  // last frame sends zeros
  wasActive = active;
}, 1000 / JOG_HZ);

// ------------------------------------------------------------ buttons
$("speed").oninput = () => {
  const v = parseFloat($("speed").value);
  $("speed-val").textContent = `${Math.round(v * 100)}%`;
  send({ cmd: "set_speed", value: v });
};
$("btn-estop").onclick = () => send({ cmd: "estop", id: "es" });
$("btn-clear").onclick = () => send({ cmd: "clear_estop", id: "ce" });
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

renderPresets();
connect();
