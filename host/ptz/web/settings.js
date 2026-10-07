// PTZ configuration editor
"use strict";

const $ = (id) => document.getElementById(id);
let saved = "", info = null;
// same per-browser identity as the control page (blocking mode)
let clientId = "";
try { clientId = localStorage.getItem("ptz-token") || ""; } catch (e) { /* ignore */ }

function escapeHtml(s) { return String(s).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c])); }
function showError(msg) {
  $("error").textContent = msg || "";
  if (msg) setTimeout(() => { if ($("error").textContent === msg) $("error").textContent = ""; }, 6000);
}
const dirty = () => $("editor").value !== saved;
function updateDirty() {
  $("dirty").classList.toggle("hidden", !dirty());
  $("editor").classList.toggle("dirty", dirty());
}

async function api(method, url, body) {
  const r = await fetch(url, {
    method, body: body === undefined ? undefined : JSON.stringify(body),
    headers: { "Content-Type": "application/json", "X-PTZ-Client": clientId },
  });
  return r.json();
}

async function load() {
  const r = await api("GET", "/api/config/file");
  if (!r.ok) { showError(r.error); return; }
  info = r;
  saved = r.text;
  $("editor").value = r.text;
  updateDirty();
  renderInfo();
}

function renderInfo() {
  $("paths").innerHTML = (info.is_edited
    ? `Editing <b>${escapeHtml(info.edited)}</b> (your copy). The repository default
       ${escapeHtml(info.default)} is not modified, so updates from GitHub keep working.`
    : `Showing the repository default <b>${escapeHtml(info.default)}</b>. Saving creates your own
       copy at ${escapeHtml(info.edited)}, which is then used instead.`)
    + `<br>Running with: ${escapeHtml(info.running)}`
    + (info.running !== info.active ? " - <b>restart to apply the saved file</b>" : "");
  $("load-error").classList.toggle("hidden", !info.load_error);
  $("load-error").textContent = info.load_error || "";
  $("btn-reset").disabled = !info.is_edited;
  $("backups").innerHTML = `<option value="">Load a backup… (${info.backups.length})</option>`
    + info.backups.map((b) => `<option value="${b}">${b.replace(/^ptz-(\d{4})(\d\d)(\d\d)-(\d\d)(\d\d)(\d\d)\.cfg$/, "$1-$2-$3 $4:$5:$6")}</option>`).join("");
}

function showResult(r) {
  const el = $("result");
  if (!r.ok) {
    el.className = "result bad";
    el.textContent = `✖ ${r.error}`;
    return false;
  }
  el.className = "result ok";
  el.innerHTML = `✔ Configuration is valid`
    + (r.axes ? ` - axes: ${r.axes.join(", ")}` : "")
    + (r.disabled_axes && r.disabled_axes.length ? ` (disabled: ${r.disabled_axes.join(", ")})` : "")
    + (r.warnings && r.warnings.length
      ? r.warnings.map((w) => `<div class="warn">⚠ ${escapeHtml(w)}</div>`).join("") : "");
  return true;
}

async function validate() {
  return showResult(await api("POST", "/api/config/validate", { text: $("editor").value }));
}

async function save() {
  if (!(await validate())) return false;
  const r = await api("POST", "/api/config/file", { text: $("editor").value });
  if (!r.ok) { showResult(r); return false; }
  saved = $("editor").value;
  updateDirty();
  info = { ...info, ...r };
  renderInfo();
  $("result").innerHTML += `<div>Saved. Restart the daemon to apply.</div>`;
  return true;
}

async function restart() {
  if (!confirm("Restart the daemon now?\n\nEvery axis will have to be homed again.")) return;
  const r = await api("POST", "/api/restart");
  if (!r.ok) { showError(r.error); return; }
  $("result").className = "result";
  $("result").textContent = "Restarting…";
  // wait for the daemon to go down and come back
  await new Promise((res) => setTimeout(res, 1500));
  for (let i = 0; i < 60; i++) {
    try {
      const s = await (await fetch("/api/status")).json();
      if (s.ready) break;
    } catch (e) { /* still restarting */ }
    await new Promise((res) => setTimeout(res, 500));
  }
  await load();
  $("result").className = "result ok";
  $("result").textContent = "✔ Restarted with the new configuration. Home the axes before moving.";
}

$("editor").addEventListener("input", updateDirty);
$("editor").addEventListener("keydown", (e) => {          // Tab inserts spaces
  if (e.key === "Tab") {
    e.preventDefault();
    const t = e.target, a = t.selectionStart;
    t.setRangeText("    ", a, t.selectionEnd, "end");
    updateDirty();
  }
  if ((e.ctrlKey || e.metaKey) && e.key === "s") { e.preventDefault(); save(); }
});
$("btn-validate").onclick = validate;
$("btn-save").onclick = save;
$("btn-save-restart").onclick = async () => { if (await save()) await restart(); };
$("btn-revert").onclick = () => { $("editor").value = saved; updateDirty(); $("result").textContent = ""; };
$("btn-restart").onclick = restart;
$("btn-reset").onclick = async () => {
  if (!confirm("Delete your edited configuration and use the repository default again?\n(A backup is kept.)")) return;
  const r = await api("POST", "/api/config/reset");
  if (!r.ok) { showError(r.error); return; }
  await load();
  $("result").className = "result ok";
  $("result").textContent = "Back to the repository default. Restart the daemon to apply.";
};
$("backups").onchange = async () => {
  const name = $("backups").value;
  if (!name) return;
  if (dirty() && !confirm("Replace your unsaved changes with this backup?")) { $("backups").value = ""; return; }
  const r = await api("GET", `/api/config/backups/${encodeURIComponent(name)}`);
  $("backups").value = "";
  if (!r.ok) { showError(r.error); return; }
  $("editor").value = r.text;
  updateDirty();
  $("result").className = "result";
  $("result").textContent = `Loaded backup ${name}. Check, then Save to use it.`;
};
window.addEventListener("beforeunload", (e) => { if (dirty()) { e.preventDefault(); e.returnValue = ""; } });

load();
