// Axis debug page: raw step moves for checking a motor's wiring.
"use strict";

const $ = (id) => document.getElementById(id);
const TOKEN = (() => {               // same identity as the control page (blocking mode)
  let t = null;
  try { t = localStorage.getItem("ptz-token"); } catch (e) { /* private mode */ }
  if (!t) {
    t = Array.from(crypto.getRandomValues(new Uint8Array(12)), (b) => b.toString(16).padStart(2, "0")).join("");
    try { localStorage.setItem("ptz-token", t); } catch (e) { /* ignore */ }
  }
  return t;
})();
const store = {
  get(k, d) { try { const v = localStorage.getItem("ptz-dbg-" + k); return v == null ? d : v; } catch (e) { return d; } },
  set(k, v) { try { localStorage.setItem("ptz-dbg-" + k, v); } catch (e) { /* ignore */ } },
};

let ws = null, config = null, status = null, axis = store.get("axis", "zoom");
let zero = {}, diagTimer = null;
try { zero = JSON.parse(store.get("zero", "{}")) || {}; } catch (e) { zero = {}; }

function connect() {
  ws = new WebSocket(`${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/ws?token=${TOKEN}`);
  ws.onopen = () => setConn(true);
  ws.onmessage = (e) => {
    const m = JSON.parse(e.data);
    if (m.type === "status") { status = m; render(); }
    else if (m.type === "config") { config = m; onConfig(); }
    else if (m.type === "reply") onReply(m);
  };
  ws.onclose = () => { setConn(false); setTimeout(connect, 1000); };
}
function send(obj) { if (ws && ws.readyState === 1) ws.send(JSON.stringify(obj)); }
function setConn(on) {
  $("conn").textContent = on ? "online" : "offline";
  $("conn").classList.toggle("off", !on);
}
function showError(msg) { $("error").textContent = msg || ""; }

const cfgAxis = () => (config ? config.axes.find((a) => a.name === axis) : null);
const revSteps = (a) => a.full_steps * a.microsteps;          // microsteps per motor turn
const SERVO_STEPS_PER_US = 2;                                  // servo: 1 step = 0.5 us of pulse
const fmt = (v, d = 2) => (Math.round(v * 10 ** d) / 10 ** d).toLocaleString("en-US");

function onConfig() {
  const sel = $("axis");
  sel.innerHTML = config.axes.map((a) => `<option value="${a.name}">${a.name}</option>`).join("");
  if (!config.axes.some((a) => a.name === axis)) axis = config.axes[0] ? config.axes[0].name : null;
  sel.value = axis;
  onAxis();
}

function onAxis() {
  const a = cfgAxis();
  if (!a) return;
  if (a.servo) { onServoAxis(a); return; }
  $("in-steps").step = 1;
  const rev = revSteps(a);
  $("cfg").innerHTML = [
    ["Microsteps", a.microsteps],
    ["Full steps / motor turn", a.full_steps],
    ["Microsteps / motor turn", rev.toLocaleString("en-US")],
    ["Gear ratio", fmt(a.gear_ratio, 4)],
    ["Rotation distance", `${a.rotation_distance} ${a.units}`],
    ["Steps / " + a.units, fmt(a.steps_per_unit, 3)],
    ["Max speed", `${Math.round(a.max_velocity_steps).toLocaleString("en-US")} steps/s (${a.max_velocity} ${a.units}/s, ${fmt(a.max_velocity_steps / rev)} turn/s)`],
    ["Max accel", `${Math.round(a.max_accel_steps).toLocaleString("en-US")} steps/s²`],
    ["Run current", a.run_current == null ? "-" : `${a.run_current} A`],
    ["Endstop", a.has_endstop ? `yes, homing toward ${a.homing_positive_dir ? "+" : "−"}` : "none"],
    ["Endstop stops raw moves", a.endstop_guard ? `yes, moving ${a.homing_positive_dir ? "+" : "−"}` : "no"],
  ].map(([k, v]) => `<span>${k}</span><span>${v}</span>`).join("");
  $("quick-steps").innerHTML = [
    ["1 full step", a.microsteps], ["¼ turn", rev / 4], ["1 turn", rev], ["5 turns", rev * 5], ["20 turns", rev * 20],
  ].map(([l, v]) => `<button class="small" data-steps="${Math.round(v)}">${l}</button>`).join("");
  $("quick-steps").querySelectorAll("button").forEach((b) => {
    b.onclick = () => { $("in-steps").value = b.dataset.steps; updateEq(); };
  });
  const vmax = a.max_velocity_steps;
  $("quick-speed").innerHTML = [
    ["¼ turn/s", rev / 4], ["1 turn/s", rev], ["2 turns/s", rev * 2], ["max", vmax],
  ].filter(([l, v]) => l === "max" || v < vmax)
    .map(([l, v]) => `<button class="small" data-speed="${Math.round(v)}">${l}</button>`).join("");
  $("quick-speed").querySelectorAll("button").forEach((b) => {
    b.onclick = () => { $("in-speed").value = b.dataset.speed; updateEq(); };
  });
  const s = parseFloat(store.get(`steps-${axis}`, "")), v = parseFloat(store.get(`speed-${axis}`, ""));
  $("in-steps").value = s > 0 ? s : rev;
  $("in-speed").value = v > 0 ? v : Math.min(Math.round(rev / 2), Math.round(vmax));
  const side = a.homing_positive_dir ? "+" : "−";
  $("dir-hint").textContent = !a.has_endstop ? "No endstop on this axis: only Stop ends a raw move early."
    : a.endstop_guard ? `The endstop is on the ${side} side (homing moves ${side}): it halts a ${side} raw move.`
      : `Homing moves ${side}. The endstop is inside the travel: it does not stop raw moves, only Stop does.`;
  updateEq();
  render();
}

// Servo axis (servo_pin): position = pulse width, 0.5 us per step, no
// endstop, raw moves stay within the configured pulse range.
function onServoAxis(a) {
  const us = SERVO_STEPS_PER_US;
  $("cfg").innerHTML = [
    ["Type", "hobby servo (PWM, 50 Hz)"],
    ["Pulse range", `${a.pulse_min}..${a.pulse_max} µs = ${a.min}..${a.max} ${a.units}`],
    ["Steps", "0.5 µs of pulse each"],
    ["Steps / " + a.units, fmt(a.steps_per_unit, 3)],
    ["Max speed", `${Math.round(a.max_velocity_steps / us).toLocaleString("en-US")} µs/s (${a.max_velocity} ${a.units}/s)`],
    ["Max accel", `${Math.round(a.max_accel_steps / us).toLocaleString("en-US")} µs/s²`],
    ["Endstop", "none (the servo holds its own position)"],
  ].map(([k, v]) => `<span>${k}</span><span>${v}</span>`).join("");
  $("quick-steps").innerHTML = [["1 µs", 1], ["10 µs", 10], ["50 µs", 50], ["100 µs", 100], ["500 µs", 500]]
    .map(([l, v]) => `<button class="small" data-steps="${v * us}">${l}</button>`).join("");
  $("quick-steps").querySelectorAll("button").forEach((b) => {
    b.onclick = () => { $("in-steps").value = b.dataset.steps; updateEq(); };
  });
  const vmax = a.max_velocity_steps;
  $("quick-speed").innerHTML = [["50 µs/s", 50 * us], ["200 µs/s", 200 * us], ["500 µs/s", 500 * us], ["max", vmax]]
    .filter(([l, v]) => l === "max" || v < vmax)
    .map(([l, v]) => `<button class="small" data-speed="${Math.round(v)}">${l}</button>`).join("");
  $("quick-speed").querySelectorAll("button").forEach((b) => {
    b.onclick = () => { $("in-speed").value = b.dataset.speed; updateEq(); };
  });
  const s = parseFloat(store.get(`steps-${axis}`, "")), v = parseFloat(store.get(`speed-${axis}`, ""));
  $("in-steps").value = s > 0 ? s : 100 * us;
  $("in-speed").value = v > 0 ? v : Math.min(200 * us, Math.round(vmax));
  $("dir-hint").textContent = `+ lengthens the pulse. A move stops at the end of the pulse range `
    + `(${a.pulse_min}..${a.pulse_max} µs in the config): widen it there only if the servo allows it.`;
  updateEq();
  render();
}

function updateEq() {
  const a = cfgAxis();
  if (!a) return;
  if (a.servo) {
    const s = parseFloat($("in-steps").value), v = parseFloat($("in-speed").value), us = SERVO_STEPS_PER_US;
    $("eq-steps").textContent = s > 0 ? `= ${fmt(s / us, 1)} µs of pulse, ${fmt(s / a.steps_per_unit, 2)} ${a.units}` : "";
    let t = v > 0 ? `= ${fmt(v / us, 1)} µs/s, ${fmt(v / a.steps_per_unit, 2)} ${a.units}/s` : "";
    if (v > a.max_velocity_steps) t += ` — capped at ${Math.round(a.max_velocity_steps)} steps/s (max_velocity)`;
    $("eq-speed").textContent = t;
    if (s > 0) store.set(`steps-${axis}`, s);
    if (v > 0) store.set(`speed-${axis}`, v);
    return;
  }
  const rev = revSteps(a);
  const s = parseFloat($("in-steps").value), v = parseFloat($("in-speed").value);
  $("eq-steps").textContent = s > 0
    ? `= ${fmt(s / rev, 3)} motor turn, ${fmt(s / a.steps_per_unit, 3)} ${a.units}` : "";
  let t = "";
  if (v > 0) {
    t = `= ${fmt(v / rev, 3)} turn/s, ${fmt(v / a.steps_per_unit, 3)} ${a.units}/s`;
    if (v > a.max_velocity_steps) t += ` — capped at ${Math.round(a.max_velocity_steps)} steps/s (max_velocity)`;
    if (s > 0) t += `, about ${fmt(s / Math.min(v, a.max_velocity_steps), 1)} s`;
  }
  $("eq-speed").textContent = t;
  if (s > 0) store.set(`steps-${axis}`, s);
  if (v > 0) store.set(`speed-${axis}`, v);
}

function flag(text, cls) { return `<span class="flag ${cls || ""}">${text}</span>`; }

function render() {
  if (!status || !config) return;
  const a = cfgAxis(), st = status.axes[axis];
  if (!a || !st) return;
  $("steps").textContent = st.steps.toLocaleString("en-US");
  const z = zero[axis];
  if (a.servo) {
    const us = SERVO_STEPS_PER_US;
    $("steps-sub").textContent = `pulse ${fmt(st.pulse, 1)} µs · ${fmt(st.pos, 2)} ${a.units}`
      + ` · ${fmt(st.vel * a.steps_per_unit / us, 0)} µs/s`;
    $("rel").textContent = z == null ? "" : `since zero: ${fmt((st.steps - z) / us, 1)} µs`;
  } else {
    const rev = revSteps(a);
    $("steps-sub").textContent = `${fmt(st.steps / rev, 3)} motor turns · ${fmt(st.pos, 3)} ${a.units}`
      + ` · ${fmt(st.vel * a.steps_per_unit, 0)} steps/s`;
    $("rel").textContent = z == null ? "" : `since zero: ${(st.steps - z).toLocaleString("en-US")} steps`
      + ` (${fmt((st.steps - z) / rev, 3)} turns)`;
  }
  $("flags").innerHTML = [
    st.active ? flag("on", "on") : flag("off", "bad"),
    st.enabled ? flag("powered", "on") : flag("unpowered", "warn"),
    st.moving ? flag("moving", "on") : flag("still"),
    a.has_endstop ? (st.endstop ? flag("endstop TRIGGERED", "warn") : flag("endstop open")) : "",
    st.halted ? flag("halted on endstop", "warn") : "",
    a.servo ? (st.available === false ? flag("needs new firmware", "bad") : flag("servo", "on"))
      : st.homed ? flag("homed", "on") : flag("not homed"),
    st.raw ? flag("raw move", "on") : "",
    status.estop ? flag("E-STOP", "bad") : "",
    status.ready ? "" : flag("MCU not ready", "bad"),
  ].join("");
  $("chk-active").checked = st.active;
  $("chk-enabled").checked = st.enabled;
  $("chk-enabled").disabled = !st.active;
  $("power-hint").textContent = a.servo
    ? (st.available === false ? status.mcu_warning
      : !st.active || !st.enabled ? "No pulse: the servo is released (a digital servo may still hold)."
        : "Pulses on: the servo holds the position shown.")
    : !st.active ? "Axis off: its driver is unpowered (safe to plug the motor)."
      : !st.enabled ? "Driver unpowered: the shaft turns freely by hand. Power it to move."
        : "";
  const can = st.active && st.enabled && status.ready && !status.estop;
  $("btn-plus").disabled = $("btn-minus").disabled = !can;
  const e = $("btn-estop");
  e.textContent = status.estop ? "Reset E-STOP" : "E-STOP";
  e.classList.toggle("reset", status.estop);
}

function move(sign) {
  const steps = Math.round(parseFloat($("in-steps").value)), speed = parseFloat($("in-speed").value);
  const accel = parseFloat($("in-accel").value);
  if (!(steps > 0)) { showError("Steps must be a whole number above 0"); return; }
  if (!(speed > 0)) { showError("Speed must be above 0 steps/s"); return; }
  showError("");
  const msg = { cmd: "raw_move", axis, steps: sign * steps, speed, id: "raw" };
  if (accel > 0) msg.accel = accel;
  send(msg);
}

function onReply(m) {
  if (!m.ok) {
    showError(m.error);
    if (m.cmd === "raw_move") setResult(m.error, false);
    return;
  }
  showError("");
  if (m.cmd === "raw_move" && m.raw) {
    const r = m.raw;
    setResult(`Moving to ${r.target.toLocaleString("en-US")} steps at ${r.speed} steps/s`
      + (r.capped ? " (capped by max_velocity)" : ""), true);
  } else if (m.cmd === "diag" && m.diag) renderDiag(m.diag[axis]);
}
function setResult(text, ok) {
  $("result").textContent = text;
  $("result").className = "result " + (ok ? "ok" : "bad");
}

// last raw move's end, from the status (raw is set while it runs)
let wasRaw = false;
function watchRaw() {
  if (!status || !status.axes[axis]) return;
  const st = status.axes[axis];
  if (st.raw) wasRaw = true;
  else if (wasRaw) {
    wasRaw = false;
    if (st.halted || st.endstop) setResult(`Stopped at ${st.steps.toLocaleString("en-US")} steps: endstop`, false);
    else setResult(`Done at ${st.steps.toLocaleString("en-US")} steps`, true);
  }
}

function renderDiag(d) {
  if (!d) { $("diag").innerHTML = ""; return; }
  const rows = [];
  const b = (bad, yes, no) => bad ? `<span class="bad">${yes}</span>` : `<span class="good">${no}</span>`;
  rows.push(["Endstop pin", d.endstop_pin
    ? `${d.endstop_pin}${d.endstop_pullup ? ", pull-up" : ""}${d.endstop_invert ? ", inverted" : ""}: `
      + (d.endstop_triggered ? "<b>TRIGGERED</b>" : "open") : "none"]);
  rows.push(["Driver enabled (MCU)", d.enabled ? "yes" : "no"]);
  if (!d.tmc_registers) rows.push(["UART", "no TMC driver configured for this axis"]);
  else {
    const answering = d.tmc_registers.IFCNT != null;
    rows.push(["UART", b(!answering, "driver not answering: check its UART address / jumpers", "answering")]);
    const s = d.tmc_status;
    if (s) {
      rows.push(["Coil A", b(s.ola, "open load (A1/A2 not connected?)", "connected")]);
      rows.push(["Coil B", b(s.olb, "open load (B1/B2 not connected?)", "connected")]);
      rows.push(["Short to ground", b(s.s2ga || s.s2gb, `${s.s2ga ? "coil A " : ""}${s.s2gb ? "coil B" : ""}`, "none")]);
      rows.push(["Temperature", s.ot ? '<span class="bad">overtemperature: shut down</span>'
        : s.otpw ? '<span class="bad">pre-warning</span>' : '<span class="good">ok</span>']);
      rows.push(["Current scale", `${s.cs_actual}/31`]);
      rows.push(["Mode", `${s.stealth ? "stealthChop" : "spreadCycle"}${s.standstill ? ", standstill" : ", moving"}`]);
    }
    if (d.tmc_gstat) {
      const g = d.tmc_gstat;
      rows.push(["Driver flags (GSTAT)", g.drv_err ? '<span class="bad">driver error (short / overtemp)</span>'
        : g.uv_cp ? '<span class="bad">charge pump undervoltage (motor power?)</span>'
          : g.reset ? "reset since last read" : '<span class="good">clear</span>']);
    }
    if (d.tmc_pins) {
      const p = d.tmc_pins;
      rows.push(["Pins seen by the driver", `ENN ${p.enn ? 1 : 0} (1: off) · DIR ${p.dir ? 1 : 0} · STEP ${p.step ? 1 : 0} · DIAG ${p.diag ? 1 : 0}`]);
    }
  }
  $("diag").innerHTML = rows.map(([k, v]) => `<tr><td>${k}</td><td>${v}</td></tr>`).join("");
}

// ------------------------------------------------------------ controls
$("axis").onchange = () => {
  axis = $("axis").value;
  store.set("axis", axis);
  $("diag").innerHTML = "";
  setResult("", true);
  onAxis();
};
$("in-steps").oninput = $("in-speed").oninput = updateEq;
$("btn-plus").onclick = () => move(1);
$("btn-minus").onclick = () => move(-1);
$("btn-stop").onclick = () => send({ cmd: "stop", axes: [axis], id: "st" });
$("btn-estop").onclick = () => send({ cmd: status && status.estop ? "clear_estop" : "estop", id: "es" });
$("btn-zero").onclick = () => {
  if (!status) return;
  zero[axis] = status.axes[axis].steps;
  store.set("zero", JSON.stringify(zero));
  render();
};
$("chk-active").onchange = (e) => send({ cmd: "axis_active", axis, on: e.target.checked, id: "act" });
$("chk-enabled").onchange = (e) => send({ cmd: "enable", on: e.target.checked, axes: [axis], id: "en" });
$("btn-diag").onclick = () => send({ cmd: "diag", id: "diag" });
$("chk-auto").onchange = (e) => {
  clearInterval(diagTimer);
  diagTimer = e.target.checked ? setInterval(() => send({ cmd: "diag", id: "diag" }), 1000) : null;
};
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape" || e.key === " ") {
    if (e.target.tagName === "INPUT" && e.key === " ") return;
    e.preventDefault();
    $("btn-stop").click();
  }
});
setInterval(watchRaw, 100);
connect();
