"use strict";

const $ = (sel, el = document) => el.querySelector(sel);
const view = $("#view");
const modal = $("#modal");
const modalBody = $("#modal-body");

let S = null;               // latest /api/state
let route = { name: "drives" };
let dirty = false;          // a form on the current view has unsaved edits
const dismissed = new Set(); // serials whose "new drive" prompt was closed
let lastRender = "";

// ------------------------------------------------------------ helpers

const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

function bytes(n) {
  n = Number(n || 0);
  const u = ["B", "KB", "MB", "GB", "TB", "PB"];
  let i = 0;
  while (Math.abs(n) >= 1000 && i < u.length - 1) { n /= 1000; i++; }
  return i === 0 ? `${n} B` : `${n.toFixed(n >= 100 ? 0 : 1)} ${u[i]}`;
}

function dur(sec) {
  if (sec == null || !isFinite(sec)) return "—";
  sec = Math.max(0, Math.round(sec));
  const h = Math.floor(sec / 3600), m = Math.floor((sec % 3600) / 60), s = sec % 60;
  if (h) return `${h} h ${m} min`;
  if (m) return `${m} min`;
  return `${s} s`;
}

function daysSince(iso) {
  if (!iso) return null;
  return Math.floor((Date.now() - new Date(iso).getTime()) / 86400000);
}

function fmtDate(iso, withTime = false) {
  if (!iso) return "—";
  const d = new Date(iso);
  const opts = { year: "numeric", month: "short", day: "numeric" };
  if (withTime) Object.assign(opts, { hour: "numeric", minute: "2-digit" });
  return d.toLocaleString(undefined, opts);
}

function speedText(mbps) {
  if (!mbps) return "unknown speed";
  if (mbps >= 5000) return `${mbps / 1000} Gb/s (USB 3)`;
  if (mbps >= 480) return "480 Mb/s (USB 2)";
  return `${mbps} Mb/s`;
}

function portLabel(key, name) {
  if (name) return name;
  if (!key) return "an unknown port";
  const i = key.lastIndexOf(":");
  return `unnamed port ${key.slice(i + 1)} on controller ${key.slice(0, i).replace(/^0000:/, "")}`;
}

function toast(msg) {
  const t = $("#toast");
  t.textContent = msg;
  t.classList.add("show");
  clearTimeout(toast._t);
  toast._t = setTimeout(() => t.classList.remove("show"), 3800);
}

async function api(method, path, body) {
  const r = await fetch(path, {
    method, headers: { "Content-Type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  let data = {};
  try { data = await r.json(); } catch (_) { /* empty */ }
  if (!r.ok) throw new Error(data.detail || data.error || `request failed (${r.status})`);
  return data;
}

function staleness(days) {
  const warn = Number(S.settings.warn_days), stale = Number(S.settings.stale_days);
  if (days == null) return "never";
  if (days >= stale) return "stale";
  if (days >= warn) return "warn";
  return "fresh";
}

const KIND_NAMES = { sync: "Backup", verify: "Verify", init: "Setup", eject: "Eject" };
const MODE_NAMES = { full: "full copy", incremental: "incremental", resume: "resumed", current: "already current" };

// ------------------------------------------------------------ routing

function parseRoute() {
  const h = location.hash.replace(/^#\/?/, "");
  const [a, b] = h.split("/");
  if (a === "drive" && b) return { name: "drive", id: b };
  if (a === "history") return { name: "history" };
  if (a === "settings") return { name: "settings" };
  return { name: "drives" };
}

window.addEventListener("hashchange", () => {
  route = parseRoute();
  dirty = false;
  lastRender = "";
  render(true);
});

// ------------------------------------------------------------ polling

async function poll() {
  try {
    S = await api("GET", "/api/state");
    renderChrome();
    maybePromptNewDrive();
    render(false);
  } catch (e) {
    $("#banner").hidden = false;
    $("#banner").textContent = `The dashboard server is not responding: ${e.message}`;
  }
  setTimeout(poll, 3000);
}

function renderChrome() {
  $("#host").textContent = S.host ? `Backing up ${S.source?.name || "?"} on ${S.host}` : "Host agent offline";
  const b = $("#banner");
  if (!S.agent_ok) {
    b.hidden = false;
    b.textContent = `${S.agent_error}. Check that the vaultsync agent is running on the host and that AGENT_URL and AGENT_TOKEN are set.`;
  } else b.hidden = true;
  const src = S.source;
  $("#source").innerHTML = src
    ? `<strong>${bytes(src.backup_bytes)} to back up</strong>${esc(src.name)} has ${bytes(src.avail)} free`
    : "";
  document.querySelectorAll(".tabs a").forEach((a) => {
    const on = a.dataset.tab === (route.name === "drive" ? "drives" : route.name);
    on ? a.setAttribute("aria-current", "page") : a.removeAttribute("aria-current");
  });
}

function render(force) {
  if (!S) { view.innerHTML = `<div class="empty"><h2>Connecting…</h2></div>`; return; }
  if (route.name === "drives") return renderDrives();
  if (route.name === "history") return renderHistory(force);
  if (route.name === "settings") return renderSettings(force);
  if (route.name === "drive") return renderDetail(force);
}

function setView(html) {
  if (html === lastRender) return;
  lastRender = html;
  const y = window.scrollY;
  view.innerHTML = html;
  window.scrollTo(0, y);
}

// ------------------------------------------------------------ drives view

function gauge(d) {
  const days = daysSince(d.last_sync);
  const cls = staleness(days);
  const stale = Number(S.settings.stale_days), warn = Number(S.settings.warn_days);
  const max = stale * 1.15;
  const fill = days == null ? 0 : Math.min(100, (days / max) * 100);
  const num = days == null
    ? `<div class="gauge-num never">Never<small>backed up</small></div>`
    : `<div class="gauge-num ${cls}">${days}<small>${days === 1 ? "day" : "days"} ago</small></div>`;
  return `<div class="gauge" title="Warning at ${warn} days, stale at ${stale} days">${num}
    <div class="gauge-bar" aria-hidden="true">
      <div class="gauge-fill ${cls}" style="width:${fill}%"></div>
      <div class="gauge-tick" style="left:${(warn / max) * 100}%"><span>${warn}</span></div>
      <div class="gauge-tick" style="left:${(stale / max) * 100}%"><span>${stale}</span></div>
    </div></div>`;
}

function lastResult(d) {
  const j = d.last_job;
  if (!j) return `<div class="last">No backups yet</div>`;
  const when = fmtDate(j.finished);
  if (j.status === "success") {
    return `<div class="last"><b>${KIND_NAMES[j.kind]} OK</b> ${esc(when)}${d.last_sync_bytes != null && j.kind === "sync" ? `<br>${bytes(d.last_sync_bytes)} copied` : ""}</div>`;
  }
  if (j.status === "running" || j.status === "queued") return `<div class="last"><b>${KIND_NAMES[j.kind]} running</b></div>`;
  return `<div class="last"><b class="fail">${KIND_NAMES[j.kind]} ${esc(j.status)}</b> ${esc(when)}</div>`;
}

function jobBlock(j) {
  const pct = j.percent ?? (j.bytes_total ? (100 * j.bytes_done) / j.bytes_total : null);
  const indeterminate = j.status === "queued" || pct == null || (j.kind === "sync" && !j.bytes_total);
  const what = { sync: "Backing up", verify: "Verifying", init: "Setting up", eject: "Ejecting" }[j.kind];
  const right = j.kind === "sync" || j.kind === "verify"
    ? `${bytes(j.bytes_done)} of ${bytes(j.bytes_total)}${j.rate ? `, ${bytes(j.rate)}/s` : ""}${j.eta ? `, about ${dur(j.eta)} left` : ""}`
    : "";
  return `<div class="job">
    <div class="job-line"><span><b>${what}</b> <span class="muted">${esc(j.phase || j.status)}</span></span>
      <span class="muted">${right}</span></div>
    <div class="progress ${indeterminate ? "indeterminate" : ""}"><div style="width:${pct ?? 0}%"></div></div>
    <div class="dock-actions"><span class="muted">${pct != null && !indeterminate ? `${pct.toFixed(1)}%` : "Starting…"}</span>
      <span class="spacer"></span>
      <button class="quiet" data-act="log" data-job="${esc(j.id)}">View log</button>
      ${j.kind !== "eject" ? `<button data-act="cancel" data-job="${esc(j.id)}">Stop</button>` : ""}</div>
  </div>`;
}

function dock(d) {
  const a = d.attached;
  const notes = [];
  if (a.port_mode === "slow" || (a.speed && a.speed < 5000)) {
    notes.push(`<div class="note">Connected at ${speedText(a.speed)}. A full copy will take much longer than on a USB 3 port.</div>`);
  }
  if (a.port_mode === "unconfigured") {
    notes.push(`<div class="note">This port isn't set up yet. You can name it and mark it active in <a href="#/settings">Settings</a>.</div>`);
  }
  const src = S.source?.backup_bytes || 0;
  if (d.capacity && src > d.capacity * 0.8) {
    notes.push(`<div class="note ${src > d.capacity * 0.97 ? "bad" : ""}">The data to back up (${bytes(src)}) is ${src > d.capacity * 0.97 ? "larger than" : "close to"} this drive's capacity (${bytes(d.capacity)}).</div>`);
  }
  const busy = d.active_job;
  const info = `<div class="dock-info">
      <span>Plugged into <b>${esc(portLabel(a.port, a.port_name))}</b></span>
      <span>Link <b>${speedText(a.speed)}</b></span>
      <span>Disk <b>${esc(a.model)}</b> <span class="muted">${esc(a.serial)}</span></span>
    </div>`;
  const actions = busy ? jobBlock(busy) : `<div class="dock-actions">
      <button class="primary" data-act="sync" data-id="${d.id}">Back up now</button>
      <button data-act="verify" data-id="${d.id}">Verify drive</button>
      <button data-act="custody" data-id="${d.id}">Hand off</button>
      <span class="spacer"></span>
      <button data-act="eject" data-id="${d.id}">Eject</button>
    </div>`;
  return `<div class="dock">${info}${notes.join("")}${actions}</div>`;
}

function driveRow(d) {
  const who = d.custodian
    ? `With <b>${esc(d.custodian)}</b>${d.location ? `, ${esc(d.location)}` : ""}`
    : `<span class="muted">No custodian set</span>`;
  const cls = ["drive", d.attached ? "attached" : "", d.retired_at ? "retired" : ""].join(" ");
  return `<div class="${cls}">
    <div class="drive-main" data-open="${d.id}" tabindex="0" role="link" aria-label="Open ${esc(d.label)}">
      <div><div class="tag-label">${esc(d.label)}</div><div class="tag-who">${d.retired_at ? "Retired" : who}</div></div>
      ${gauge(d)}
      ${lastResult(d)}
      <div class="cap"><b>${d.capacity ? bytes(d.capacity) : "—"}</b>${d.attached ? "connected" : "not connected"}</div>
    </div>
    ${d.attached ? dock(d) : ""}
  </div>`;
}

function unknownStrip(disk) {
  if (disk.active_job) {
    return `<div class="strip" style="display:block">${jobBlock(disk.active_job)}</div>`;
  }
  const isVs = disk.vaultsync;
  const title = isVs ? "A backup drive that isn't in the list is plugged in" : "A new drive is plugged in";
  const desc = `${esc(disk.model || "USB disk")}, ${bytes(disk.size)}, on ${esc(portLabel(disk.port, disk.port_name))}`;
  return `<div class="strip"><div><h3>${title}</h3><p>${desc}</p></div>
    <button class="primary" data-act="${isVs ? "adopt" : "setup"}" data-serial="${esc(disk.serial)}">${isVs ? "Add to list" : "Set up as backup drive"}</button></div>`;
}

function renderDrives() {
  const active = S.drives.filter((d) => !d.retired_at);
  const retired = S.drives.filter((d) => d.retired_at);
  const strips = S.unknown_disks.map(unknownStrip).join("");
  const head = `<div class="section-head"><div><h1>Backup drives</h1>
    <p class="lede">Days since each drive last received a backup. Plug a drive into the server to update it.</p></div></div>`;
  let body;
  if (!S.drives.length) {
    body = `<div class="ledger"><div class="empty"><h2>No backup drives yet</h2>
      <p>Plug a USB drive into the server. It will show up here so you can set it up.</p></div></div>`;
  } else {
    body = `<div class="ledger" role="list">
      <div class="ledger-head"><span>Drive and custodian</span><span>Since last backup</span><span>Last result</span><span>Capacity</span></div>
      ${active.map(driveRow).join("")}
      ${retired.length ? retired.map(driveRow).join("") : ""}
    </div>`;
  }
  setView(head + strips + body);
}

// ------------------------------------------------------------ drive detail

let detail = null;

async function loadDetail() {
  detail = await api("GET", `/api/drives/${route.id}`);
}

async function renderDetail(force) {
  if (force || !detail || detail.id !== route.id) {
    try { await loadDetail(); } catch (e) { setView(`<div class="empty"><h2>${esc(e.message)}</h2><a href="#/">Back to drives</a></div>`); return; }
  } else if (!dirty) {
    try { await loadDetail(); } catch (_) { /* keep old */ }
  }
  if (dirty) return;
  const live = S.drives.find((x) => x.id === detail.id) || {};
  const d = { ...live, ...detail, attached: live.attached, active_job: live.active_job };
  const cur = d.custody.find((c) => !c.ended_at);
  const custody = d.custody.length
    ? `<ul class="timeline">${d.custody.map((c) => `<li class="${c === cur ? "current" : ""}">
        <div class="who">${esc(c.custodian || "No one")}${c.location ? `<span class="muted">, ${esc(c.location)}</span>` : ""}</div>
        <div class="when">${fmtDate(c.started_at)} – ${c.ended_at ? fmtDate(c.ended_at) : "now"}</div>
        ${c.notes ? `<div class="muted">${esc(c.notes)}</div>` : ""}</li>`).join("")}</ul>`
    : `<p class="muted">No custody records yet.</p>`;
  const jobs = d.jobs.length
    ? `<table><thead><tr><th>When</th><th>What</th><th>Result</th><th>Copied</th></tr></thead><tbody>
      ${d.jobs.map((j) => `<tr class="clickable" data-act="log" data-job="${esc(j.id)}">
        <td>${fmtDate(j.finished || j.started, true)}</td>
        <td>${KIND_NAMES[j.kind] || esc(j.kind)}${j.mode && j.kind === "sync" ? `<div class="muted">${MODE_NAMES[j.mode] || esc(j.mode)}</div>` : ""}</td>
        <td><span class="status ${esc(j.status)}">${esc(j.status)}</span><div class="msg">${esc(j.message || "")}</div></td>
        <td>${j.kind === "sync" && j.bytes != null ? bytes(j.bytes) : ""}</td></tr>`).join("")}
      </tbody></table>`
    : `<div class="empty">No jobs recorded for this drive.</div>`;
  setView(`<a class="back" href="#/">← All drives</a>
    <div class="section-head"><div><h1>${esc(d.label)}</h1>
      <p class="lede">${d.attached ? "Connected now." : "Not connected."} ${d.last_sync ? `Last backup ${fmtDate(d.last_sync)} (${daysSince(d.last_sync)} days ago).` : "Never backed up."}</p></div>
      <div class="dock-actions"><button data-act="custody" data-id="${d.id}">Hand off</button>
      <button class="quiet" data-act="edit" data-id="${d.id}">Edit</button></div></div>
    ${d.attached ? `<div class="ledger"><div class="drive attached" style="padding-top:18px">${dock(d)}</div></div>` : ""}
    <div class="detail-grid">
      <div class="panel card-pad"><h2>Custody</h2>${custody}
        <dl class="facts">
          <dt>Pool</dt><dd class="mono-ish">${esc(d.pool)}</dd>
          <dt>Disk</dt><dd>${esc(d.model || "—")}</dd>
          <dt>Serial</dt><dd class="mono-ish">${esc(d.serial || "—")}</dd>
          <dt>Capacity</dt><dd>${d.capacity ? bytes(d.capacity) : "—"}</dd>
          <dt>Added</dt><dd>${fmtDate(d.created_at)}</dd>
          ${d.last_verify ? `<dt>Last verify</dt><dd>${fmtDate(d.last_verify.finished)} <span class="status ${esc(d.last_verify.status)}">${esc(d.last_verify.status)}</span></dd>` : ""}
          ${d.notes ? `<dt>Notes</dt><dd>${esc(d.notes)}</dd>` : ""}
        </dl></div>
      <div class="panel"><div class="card-pad" style="padding-bottom:4px"><h2>History</h2></div>${jobs}</div>
    </div>`);
}

// ------------------------------------------------------------ history

let historyData = null;
async function renderHistory(force) {
  try { historyData = await api("GET", "/api/history"); } catch (e) { if (!historyData) { setView(`<div class="empty">${esc(e.message)}</div>`); return; } }
  const rows = historyData.jobs.map((j) => {
    const took = j.started && j.finished ? (new Date(j.finished) - new Date(j.started)) / 1000 : null;
    return `<tr class="clickable" data-act="log" data-job="${esc(j.id)}">
      <td>${fmtDate(j.started || j.finished, true)}</td>
      <td>${j.drive_id ? `<a href="#/drive/${esc(j.drive_id)}">${esc(j.label || j.pool)}</a>` : `<span class="muted">${esc(j.pool || "—")}</span>`}</td>
      <td>${KIND_NAMES[j.kind] || esc(j.kind)}${j.mode && j.kind === "sync" ? ` <span class="muted">${MODE_NAMES[j.mode] || esc(j.mode)}</span>` : ""}</td>
      <td><span class="status ${esc(j.status)}">${esc(j.status)}</span></td>
      <td>${j.kind === "sync" && j.bytes != null ? bytes(j.bytes) : ""}</td>
      <td>${took != null ? dur(took) : ""}</td>
      <td class="msg">${esc(j.message || "")}</td></tr>`;
  }).join("");
  setView(`<div class="section-head"><div><h1>History</h1><p class="lede">Every backup, verify, setup and eject, newest first. Select a row to read its log.</p></div></div>
    <div class="panel">${rows ? `<table><thead><tr><th>Started</th><th>Drive</th><th>Job</th><th>Result</th><th>Copied</th><th>Took</th><th>Message</th></tr></thead><tbody>${rows}</tbody></table>`
      : `<div class="empty">Nothing has run yet.</div>`}</div>`);
}

// ------------------------------------------------------------ settings

function renderSettings(force) {
  if (dirty && !force) return;
  const st = S.settings, cfg = S.config || {};
  const groups = {};
  for (const p of S.ports) (groups[p.controller] ||= []).push(p);
  const portRows = Object.entries(groups).map(([ctrl, ports]) => {
    const fast = Math.max(...ports.map((p) => p.max_speed)) >= 5000;
    const head = `<tr class="ctrl-group"><td colspan="5">Controller ${esc(ctrl)} <span class="muted" style="font-weight:400">${fast ? "USB 3, ports share this controller's bandwidth" : "USB 2 only"}</span></td></tr>`;
    return head + ports.map((p) => {
      const dev = p.device ? `${esc((p.device.manufacturer + " " + p.device.product).trim() || p.device.usb_id)}<div class="muted">${speedText(p.device.speed)}</div>` : `<span class="muted">empty</span>`;
      return `<tr data-port="${esc(p.key)}">
        <td class="mono-ish">${esc(p.chain)}</td>
        <td><span class="speed ${p.max_speed < 5000 ? "usb2" : ""}">${p.max_speed >= 5000 ? "USB 3" : "USB 2"}</span></td>
        <td><input data-f="name" value="${esc(p.name)}" placeholder="e.g. Rear USB 3 left" maxlength="40" aria-label="Name for port ${esc(p.key)}"></td>
        <td><select data-f="mode" aria-label="Use of port ${esc(p.key)}">
          ${["active", "slow", "ignored", "unconfigured"].map((m) => `<option value="${m}" ${p.mode === m ? "selected" : ""}>${{ active: "Active", slow: "Active, slow", ignored: "Ignore", unconfigured: "Not set" }[m]}</option>`).join("")}
        </select></td>
        <td>${dev}</td></tr>`;
    }).join("");
  }).join("");
  const ds = (S.source?.datasets || []).map((d) => `<tr><td class="mono-ish">${esc(d.name)}</td><td>${bytes(d.refer)}</td><td>${d.excluded ? "excluded" : "backed up"}</td></tr>`).join("");
  setView(`<div class="section-head"><div><h1>Settings</h1></div></div>
  <div class="settings-grid">
    <section class="settings-block"><h2>Staleness</h2>
      <p>How many days after its last backup a drive turns amber, then red, on the drives page.</p>
      <div class="row-fields">
        <label class="field">Warning after (days)<input type="number" min="1" id="warn_days" value="${esc(st.warn_days)}"></label>
        <label class="field">Stale after (days)<input type="number" min="2" id="stale_days" value="${esc(st.stale_days)}"></label>
        <button class="primary" data-act="save-stale">Save staleness</button>
      </div></section>
    <section class="settings-block"><h2>USB ports</h2>
      <p>Drives on ignored ports never show up here. Mark USB 2 ports as slow so a warning appears before a long copy. To find a port, plug a drive into it and watch which row fills in.</p>
      <div class="panel"><table><thead><tr><th>Port</th><th>Type</th><th>Name</th><th>Use</th><th>Plugged in</th></tr></thead><tbody>${portRows || `<tr><td colspan="5" class="muted">No USB ports reported by the host.</td></tr>`}</tbody></table></div>
      <div class="row-fields" style="margin-top:14px"><button class="primary" data-act="save-ports">Save ports</button></div></section>
    <section class="settings-block"><h2>Jobs</h2>
      <p>Two backups can run at once on different drives. On this server they only both run at full speed when the drives are on different USB 3 controllers.</p>
      <div class="row-fields">
        <label class="field">Jobs at once<input type="number" min="1" max="8" id="max_jobs" value="${esc(cfg.max_jobs ?? 2)}"></label>
        <label class="field">Discard unfinished backups after (days)<input type="number" min="1" id="stale_snapshot_days" value="${esc(cfg.stale_snapshot_days ?? 14)}"></label>
        <label class="check"><input type="checkbox" id="verify_after_sync" ${cfg.verify_after_sync ? "checked" : ""}> Verify the whole drive after every backup (slow)</label>
        <button class="primary" data-act="save-jobs">Save jobs</button>
      </div>
      <p style="margin-top:14px">An interrupted backup keeps a snapshot on ${esc(S.source?.name || "the source")} so it can resume. If that drive doesn't come back, the snapshot is removed after the number of days above.</p>
      <button data-act="cleanup">Remove old snapshots now</button></section>
    <section class="settings-block"><h2>Host</h2>
      <dl class="facts"><dt>Agent</dt><dd class="mono-ish">${esc(S.agent_url)}</dd><dt>Host</dt><dd>${esc(S.host || "—")}</dd>
        <dt>Source</dt><dd class="mono-ish">${esc(S.source?.name || "—")}</dd></dl>
      ${ds ? `<div class="panel" style="margin-top:14px"><table><thead><tr><th>Dataset</th><th>Size</th><th></th></tr></thead><tbody>${ds}</tbody></table></div>` : ""}
    </section>
  </div>`);
}

view.addEventListener("input", (e) => {
  if (e.target.matches("input, select, textarea")) dirty = true;
});

// ------------------------------------------------------------ modals

function openModal(html, onReady) {
  modalBody.innerHTML = html;
  if (!modal.open) modal.showModal();
  onReady && onReady(modalBody);
}
function closeModal() { if (modal.open) modal.close(); }
modal.addEventListener("click", (e) => { if (e.target === modal) closeModal(); });
modal.addEventListener("close", () => { modalBody.innerHTML = ""; });

function findUnknown(serial) { return S.unknown_disks.find((d) => d.serial === serial); }

function maybePromptNewDrive() {
  if (modal.open) return;
  const disk = S.unknown_disks.find((d) => !d.active_job && !dismissed.has(d.serial));
  if (!disk) return;
  dismissed.add(disk.serial);
  disk.vaultsync ? adoptModal(disk) : setupModal(disk);
}

function setupModal(disk) {
  const parts = (disk.partitions || []).filter((p) => p.fstype || p.label);
  const existing = parts.length
    ? `<div>It currently holds: ${parts.map((p) => `${esc(p.fstype || "data")}${p.label ? ` “${esc(p.label)}”` : ""} (${bytes(p.size)})`).join(", ")}.</div>` : "";
  const foreign = disk.foreign_zfs ? `<div><strong>This disk belongs to another ZFS pool (${esc(disk.pool)}).</strong> Make sure it isn't part of a server pool.</div>` : "";
  const slow = disk.speed && disk.speed < 5000 ? `<div class="note">This drive is connected at ${speedText(disk.speed)}. The first full copy of ${bytes(S.source?.backup_bytes)} will be slow; a USB 3 port is better.</div>` : "";
  const small = S.source && disk.size < S.source.backup_bytes ? `<div class="note bad">This drive (${bytes(disk.size)}) is smaller than the data to back up (${bytes(S.source.backup_bytes)}).</div>` : "";
  openModal(`<form id="f-setup">
    <div class="modal-head"><h2>Set up a new backup drive</h2><p>A drive the dashboard hasn't seen before was plugged in.</p></div>
    <div class="modal-body">
      <div class="disk-id"><b>${esc(disk.model || "USB disk")}</b><span>${bytes(disk.size)}, serial ${esc(disk.serial)}</span>
        <span class="muted">On ${esc(portLabel(disk.port, disk.port_name))}, ${speedText(disk.speed)}</span></div>
      ${slow}${small}
      <div class="two">
        <label class="field">Drive name<input name="label" required maxlength="40" pattern="[A-Za-z0-9][A-Za-z0-9 _.\\-]{0,39}" placeholder="e.g. Charlie" autocomplete="off"></label>
        <label class="field">Who will keep it<input name="custodian" placeholder="Name" autocomplete="off"></label>
      </div>
      <label class="field">Where it will be kept<input name="location" placeholder="e.g. Mom's house, office safe" autocomplete="off"></label>
      <label class="field">Notes<textarea name="notes"></textarea></label>
      <label class="check"><input type="checkbox" name="start_sync" checked> Start the first backup right after setup</label>
      <div class="erase"><strong>Everything on this drive will be erased.</strong>${existing}${foreign}
        <label class="field">Type the drive name to confirm<input name="confirm" required autocomplete="off"></label></div>
      <div class="err" id="f-err"></div>
    </div>
    <div class="modal-foot"><button type="button" data-close>Not now</button><button class="danger" type="submit">Erase and set up</button></div>
  </form>`, (root) => {
    root.querySelector("[name=label]").focus();
    root.querySelector("[data-close]").onclick = closeModal;
    root.querySelector("form").onsubmit = async (e) => {
      e.preventDefault();
      const f = new FormData(e.target);
      const err = root.querySelector("#f-err");
      if (f.get("confirm").trim() !== f.get("label").trim()) { err.textContent = "The confirmation doesn't match the drive name."; return; }
      try {
        e.submitter.disabled = true;
        await api("POST", "/api/drives", {
          device: disk.device, serial: disk.serial, label: f.get("label").trim(), confirm: f.get("confirm").trim(),
          custodian: f.get("custodian"), location: f.get("location"), notes: f.get("notes"), start_sync: !!f.get("start_sync"),
        });
        closeModal();
        toast(`Setting up ${f.get("label").trim()}…`);
        poll.now();
      } catch (ex) { err.textContent = ex.message; e.submitter.disabled = false; }
    };
  });
}

function adoptModal(disk) {
  const label = S.known?.[disk.pool]?.label || disk.label || "";
  openModal(`<form id="f-adopt">
    <div class="modal-head"><h2>Add an existing backup drive</h2><p>This drive was set up by vaultsync but isn't in this dashboard's list. Adding it keeps its data.</p></div>
    <div class="modal-body">
      <div class="disk-id"><b>${esc(label || disk.pool)}</b><span>${esc(disk.model)}, ${bytes(disk.size)}, serial ${esc(disk.serial)}</span><span class="muted mono-ish">${esc(disk.pool)}</span></div>
      <div class="two"><label class="field">Drive name<input name="label" required value="${esc(label)}" maxlength="40"></label>
        <label class="field">Who keeps it<input name="custodian"></label></div>
      <label class="field">Where it is kept<input name="location"></label>
      <div class="err" id="f-err"></div>
    </div>
    <div class="modal-foot"><button type="button" data-close>Not now</button><button class="primary" type="submit">Add drive</button></div>
  </form>`, (root) => {
    root.querySelector("[data-close]").onclick = closeModal;
    root.querySelector("form").onsubmit = async (e) => {
      e.preventDefault();
      const f = new FormData(e.target);
      try {
        await api("POST", "/api/drives/adopt", { pool: disk.pool, label: f.get("label"), custodian: f.get("custodian"), location: f.get("location") });
        closeModal(); toast("Drive added"); poll.now();
      } catch (ex) { root.querySelector("#f-err").textContent = ex.message; }
    };
  });
}

async function syncModal(id) {
  const d = S.drives.find((x) => x.id === id);
  openModal(`<div class="modal-head"><h2>Back up to ${esc(d.label)}</h2><p>Checking what needs to be copied…</p></div>
    <div class="modal-body"><div class="progress indeterminate"><div></div></div></div>`);
  let plan;
  try { plan = await api("POST", `/api/drives/${id}/plan`); }
  catch (e) {
    openModal(`<div class="modal-head"><h2>Can't back up ${esc(d.label)}</h2></div><div class="modal-body"><div class="note bad">${esc(e.message)}</div></div>
      <div class="modal-foot"><button data-close>Close</button></div>`, (r) => { r.querySelector("[data-close]").onclick = closeModal; });
    return;
  }
  const speed = d.attached?.speed >= 5000 ? 150e6 : 35e6;
  const eta = plan.bytes / speed;
  const sum = {
    full: `Full copy, about ${bytes(plan.bytes)}`,
    incremental: `Incremental, about ${bytes(plan.bytes)} of changes`,
    resume: `Resume the interrupted backup, then copy new changes`,
  }[plan.mode] || `About ${bytes(plan.bytes)}`;
  const what = plan.mode === "full"
    ? (d.last_sync ? "There's no shared snapshot with this drive, so its current copy will be replaced from scratch." : "This is the first backup to this drive.")
    : `The drive's copy from ${fmtDate(d.last_sync)} will be updated to match ${esc(S.source?.name)} as it is now.`;
  openModal(`<div class="modal-head"><h2>Back up to ${esc(d.label)}</h2></div>
    <div class="modal-body">
      <div class="plan-sum">${sum}</div>
      <div>${what} The drive keeps only the newest copy.</div>
      <div class="muted">Estimated time: ${dur(eta)} at ${speedText(d.attached?.speed)}. You can close this page; the backup keeps running on the server.</div>
      ${plan.fits ? "" : `<div class="note bad">This needs ${bytes(plan.bytes)} but the drive has ${bytes(plan.drive_free)} available. It won't fit.</div>`}
      <div class="err" id="f-err"></div>
    </div>
    <div class="modal-foot"><button data-close>Cancel</button><button class="primary" id="go" ${plan.fits ? "" : "disabled"}>Start backup</button></div>`, (root) => {
    root.querySelector("[data-close]").onclick = closeModal;
    root.querySelector("#go").onclick = async (e) => {
      e.target.disabled = true;
      try { await api("POST", `/api/drives/${id}/sync`); closeModal(); toast(`Backup to ${d.label} started`); poll.now(); }
      catch (ex) { root.querySelector("#f-err").textContent = ex.message; e.target.disabled = false; }
    };
  });
}

function confirmModal(title, text, okLabel, fn, danger = false) {
  openModal(`<div class="modal-head"><h2>${esc(title)}</h2></div><div class="modal-body"><div>${text}</div><div class="err" id="f-err"></div></div>
    <div class="modal-foot"><button data-close>Cancel</button><button class="${danger ? "danger" : "primary"}" id="go">${esc(okLabel)}</button></div>`, (root) => {
    root.querySelector("[data-close]").onclick = closeModal;
    root.querySelector("#go").onclick = async (e) => {
      e.target.disabled = true;
      try { await fn(); closeModal(); poll.now(); }
      catch (ex) { root.querySelector("#f-err").textContent = ex.message; e.target.disabled = false; }
    };
  });
}

function custodyModal(id) {
  const d = S.drives.find((x) => x.id === id) || detail;
  openModal(`<form><div class="modal-head"><h2>Hand off ${esc(d.label)}</h2><p>Record who has the drive now. The previous holder's record is closed with today's date.</p></div>
    <div class="modal-body">
      <div class="two"><label class="field">Now kept by<input name="custodian" required value=""></label>
      <label class="field">Kept at<input name="location" value=""></label></div>
      <label class="field">Notes<textarea name="notes" placeholder="Optional"></textarea></label>
      <div class="muted">Currently with ${esc(d.custodian || "no one")}${d.location ? `, ${esc(d.location)}` : ""}.</div>
      <div class="err" id="f-err"></div></div>
    <div class="modal-foot"><button type="button" data-close>Cancel</button><button class="primary" type="submit">Save hand-off</button></div></form>`, (root) => {
    root.querySelector("[name=custodian]").focus();
    root.querySelector("[data-close]").onclick = closeModal;
    root.querySelector("form").onsubmit = async (e) => {
      e.preventDefault();
      const f = new FormData(e.target);
      try {
        await api("POST", `/api/drives/${id}/custody`, { custodian: f.get("custodian"), location: f.get("location"), notes: f.get("notes") });
        closeModal(); toast(`${d.label} is now with ${f.get("custodian")}`); detail = null; poll.now();
      } catch (ex) { root.querySelector("#f-err").textContent = ex.message; }
    };
  });
}

function editModal(id) {
  const d = detail && detail.id === id ? detail : S.drives.find((x) => x.id === id);
  openModal(`<form><div class="modal-head"><h2>Edit ${esc(d.label)}</h2></div>
    <div class="modal-body">
      <label class="field">Drive name<input name="label" required value="${esc(d.label)}" maxlength="40"></label>
      <label class="field">Notes<textarea name="notes">${esc(d.notes || "")}</textarea></label>
      <label class="check"><input type="checkbox" name="retired" ${d.retired_at ? "checked" : ""}> Retired (keep its history but move it to the bottom of the list)</label>
      <div class="err" id="f-err"></div></div>
    <div class="modal-foot"><button type="button" data-close>Cancel</button><button class="primary" type="submit">Save changes</button></div></form>`, (root) => {
    root.querySelector("[data-close]").onclick = closeModal;
    root.querySelector("form").onsubmit = async (e) => {
      e.preventDefault();
      const f = new FormData(e.target);
      try {
        await api("PATCH", `/api/drives/${id}`, { label: f.get("label"), notes: f.get("notes"), retired: !!f.get("retired") });
        closeModal(); toast("Changes saved"); detail = null; poll.now();
      } catch (ex) { root.querySelector("#f-err").textContent = ex.message; }
    };
  });
}

async function logModal(jobId) {
  openModal(`<div class="modal-head"><h2>Job log</h2><p class="mono-ish">${esc(jobId)}</p></div>
    <div class="modal-body"><pre class="log" id="log">Loading…</pre></div>
    <div class="modal-foot"><button data-close>Close</button></div>`, (root) => { root.querySelector("[data-close]").onclick = closeModal; });
  const load = async () => {
    if (!modal.open || !$("#log", modalBody)) return;
    try {
      const r = await api("GET", `/api/jobs/${jobId}/log`);
      const el = $("#log", modalBody);
      if (!el) return;
      const atBottom = el.scrollTop + el.clientHeight >= el.scrollHeight - 20;
      el.textContent = r.lines.join("\n") || "(empty)";
      if (atBottom) el.scrollTop = el.scrollHeight;
    } catch (e) { const el = $("#log", modalBody); if (el) el.textContent = e.message; }
    setTimeout(load, 3000);
  };
  load();
}

// ------------------------------------------------------------ actions

document.addEventListener("click", async (e) => {
  const open = e.target.closest("[data-open]");
  const btn = e.target.closest("[data-act]");
  if (btn) {
    e.stopPropagation();
    const { act, id, job, serial } = btn.dataset;
    const d = id && S.drives.find((x) => x.id === id);
    if (act === "sync") return syncModal(id);
    if (act === "verify") return confirmModal(`Verify ${d.label}`, `Reads every block on the drive and checks it against its checksums. On a full drive this can take several hours. The drive can't be backed up while it runs.`, "Start verify", async () => { await api("POST", `/api/drives/${id}/verify`); toast(`Verifying ${d.label}`); });
    if (act === "eject") return confirmModal(`Eject ${d.label}`, `Closes the drive so it can be unplugged safely.`, "Eject", async () => { await api("POST", `/api/drives/${id}/eject`); toast(`Ejecting ${d.label}…`); });
    if (act === "custody") return custodyModal(id);
    if (act === "edit") return editModal(id);
    if (act === "cancel") return confirmModal("Stop this job?", "A stopped backup can be resumed: the next backup to this drive continues where this one stopped.", "Stop job", async () => { await api("POST", `/api/jobs/${job}/cancel`); toast("Stopping…"); }, true);
    if (act === "log") return logModal(job);
    if (act === "setup") { const disk = findUnknown(serial); return disk && setupModal(disk); }
    if (act === "adopt") { const disk = findUnknown(serial); return disk && adoptModal(disk); }
    if (act === "save-stale") return save({ warn_days: +$("#warn_days").value, stale_days: +$("#stale_days").value }, "Staleness saved");
    if (act === "save-jobs") return save({ max_jobs: +$("#max_jobs").value, stale_snapshot_days: +$("#stale_snapshot_days").value, verify_after_sync: $("#verify_after_sync").checked }, "Job settings saved");
    if (act === "save-ports") {
      const ports = {};
      document.querySelectorAll("tr[data-port]").forEach((tr) => {
        const name = tr.querySelector("[data-f=name]").value.trim();
        const mode = tr.querySelector("[data-f=mode]").value;
        if (name || mode !== "unconfigured") ports[tr.dataset.port] = { name, mode };
      });
      return save({ ports }, "Ports saved");
    }
    if (act === "cleanup") {
      try { const r = await api("POST", "/api/cleanup"); toast(r.removed?.length ? `Removed ${r.removed.length} snapshot(s)` : "Nothing to remove"); }
      catch (ex) { toast(ex.message); }
      return;
    }
  }
  if (open && !e.target.closest("a, button")) location.hash = `#/drive/${open.dataset.open}`;
});

document.addEventListener("keydown", (e) => {
  const open = e.target.closest && e.target.closest("[data-open]");
  if (open && (e.key === "Enter" || e.key === " ")) { e.preventDefault(); location.hash = `#/drive/${open.dataset.open}`; }
});

async function save(body, msg) {
  try {
    await api("PUT", "/api/settings", body);
    dirty = false; toast(msg); lastRender = ""; await poll.now();
  } catch (ex) { toast(ex.message); }
}

poll.now = async () => {
  try {
    S = await api("GET", "/api/state");
    renderChrome();
    lastRender = "";
    render(true);
  } catch (_) { /* next poll will report */ }
};

route = parseRoute();
render(true);
poll();
