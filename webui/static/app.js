"use strict";

/* ── API klient + toast ──────────────────────────────────────────────────── */

async function apiGet(path) {
  const r = await fetch(path);
  if (!r.ok) throw await apiError(r);
  return r.json();
}
async function apiPost(path, body) {
  const r = await fetch(path, {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body || {}),
  });
  if (!r.ok) throw await apiError(r);
  return r.json();
}
async function apiUpload(path, file) {
  const fd = new FormData();
  fd.append("file", file, file.name);
  const r = await fetch(path, { method: "POST", body: fd });
  const data = await r.json().catch(() => null);
  if (!r.ok) { const e = new Error("upload"); e.status = r.status; e.detail = data && data.detail; throw e; }
  return data;
}
async function apiError(r) {
  let detail = null;
  try { const d = await r.json(); detail = d.detail; } catch {}
  const e = new Error("api");
  e.status = r.status; e.detail = detail;
  return e;
}
function detailText(err) {
  const d = err && err.detail;
  if (d == null) return `Chyba serveru (HTTP ${err && err.status || "?"})`;
  if (typeof d === "string") return d;
  if (d.message) return d.message;
  return JSON.stringify(d);
}
let toastTimer = null;
function toast(msg) {
  const el = document.getElementById("toast");
  el.textContent = msg;
  el.classList.remove("hidden");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => el.classList.add("hidden"), 6000);
}

/* ── Utility ─────────────────────────────────────────────────────────────── */

const STATUS_CZ = {
  queued: "Ve frontě", running: "Běží", success: "Hotovo", failed: "Chyba",
  cancelled: "Zrušeno", interrupted: "Přerušeno", pending: "Čeká", skipped: "Přeskočeno",
};
function statusPill(status) {
  const label = STATUS_CZ[status] || status;
  return `<span class="status-pill st-${status}">${label}</span>`;
}
function esc(s) {
  return String(s == null ? "" : s).replace(/[&<>"]/g, c =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
}
function fmtBytes(n) {
  if (n == null) return "";
  if (n < 1024) return n + " B";
  if (n < 1024 * 1024) return (n / 1024).toFixed(1) + " kB";
  return (n / 1024 / 1024).toFixed(1) + " MB";
}
const TERMINAL = ["success", "failed", "cancelled", "interrupted"];

/* ── Job detail + živý log ───────────────────────────────────────────────── */

function ensureJobSkeleton(container, jobId) {
  if (container._jobId === jobId) return;
  container._jobId = jobId;
  container._logOffset = 0;
  container.classList.remove("hidden");
  container.innerHTML = `
    <div class="row" style="justify-content:space-between">
      <div><strong class="jd-title"></strong> <span class="jd-status"></span></div>
      <button class="btn-secondary jd-cancel hidden">Zrušit</button>
    </div>
    <ul class="steps jd-steps"></ul>
    <div class="jd-error"></div>
    <div class="jd-extra"></div>
    <pre class="job-log jd-log"></pre>`;
  container.querySelector(".jd-cancel").onclick = async () => {
    if (!confirm("Opravdu zrušit tuto úlohu (a celý strom procesů)?")) return;
    try { await apiPost(`/api/jobs/${jobId}/cancel`, {}); }
    catch (e) { toast(detailText(e)); }
  };
}

function renderJobFields(container, job) {
  container.querySelector(".jd-title").textContent = job.title || job.id;
  container.querySelector(".jd-status").innerHTML = statusPill(job.status);
  container.querySelector(".jd-cancel").classList.toggle(
    "hidden", !["running", "queued"].includes(job.status));
  container.querySelector(".jd-steps").innerHTML = (job.steps || []).map(s =>
    `<li>${statusPill(s.status)} <strong>${esc(s.name)}</strong>
       <span class="step-cmd">${esc(s.cmdline)}</span>
       ${s.exit_code != null ? `<span class="hint">exit ${s.exit_code}</span>` : ""}</li>`
  ).join("");
  const errBox = container.querySelector(".jd-error");
  if (job.error_lines && job.error_lines.length) {
    errBox.className = "error-box"; errBox.textContent = job.error_lines.join("\n");
  } else { errBox.className = ""; errBox.textContent = ""; }
  const extra = container.querySelector(".jd-extra");
  if (job.status === "success" && job.params && job.params.session_dir_detected) {
    extra.innerHTML = `<div class="hint">Session: <code>${esc(job.params.session_dir_detected)}</code></div>`;
  } else if (job.type === "daily" && job.status === "success") {
    const p = job.params || {};
    extra.innerHTML = `<a href="#vysledky">Otevřít výsledky →</a>`;
  } else { extra.innerHTML = ""; }
}

async function watchJob(jobId, container, onDone) {
  ensureJobSkeleton(container, jobId);
  const logEl = container.querySelector(".jd-log");
  let onDoneCalled = false;
  // Robustní smyčka: žádná výjimka (fetch/render/log) nesmí zabít polling.
  // finally VŽDY naplánuje další cyklus, dokud není job v terminálním stavu.
  const poll = async () => {
    if (container._jobId !== jobId) return;   // panel přepnut na jiný job
    let terminal = false;
    try {
      const job = await apiGet(`/api/jobs/${jobId}`);
      try { renderJobFields(container, job); } catch (e) { /* render nezabije smyčku */ }
      try {
        const lg = await apiGet(`/api/jobs/${jobId}/log?offset=${container._logOffset}`);
        if (lg && lg.content) {
          const nearBottom = logEl.scrollHeight - logEl.scrollTop - logEl.clientHeight < 40;
          logEl.textContent += lg.content;
          container._logOffset = lg.offset;
          if (nearBottom) logEl.scrollTop = logEl.scrollHeight;
        }
      } catch (e) { /* log fetch může selhat přechodně */ }
      terminal = TERMINAL.includes(job.status);
      if (terminal && onDone && !onDoneCalled) { onDoneCalled = true; onDone(job); }
    } catch (e) {
      /* job fetch selhal — zkusíme znovu příště */
    } finally {
      if (!terminal && container._jobId === jobId) setTimeout(poll, 1500);
    }
  };
  poll();
}

/* ── Taby ────────────────────────────────────────────────────────────────── */

const TABS = ["denni", "benchmarky", "vysledky", "uzavirky", "ulohy"];
function activateTab(name) {
  if (!TABS.includes(name)) name = "denni";
  document.querySelectorAll(".tabs a").forEach(a =>
    a.classList.toggle("active", a.dataset.tab === name));
  document.querySelectorAll(".tab-panel").forEach(p =>
    p.classList.toggle("active", p.id === "tab-" + name));
  if (name === "denni") Daily.onShow();
  if (name === "benchmarky") Bench.onShow();
  if (name === "vysledky") Results.onShow();
  if (name === "uzavirky") Closures.onShow();
  if (name === "ulohy") Jobs.onShow();
}
window.addEventListener("hashchange", () => activateTab(location.hash.slice(1)));

/* ── Health badge ────────────────────────────────────────────────────────── */

async function pollHealth() {
  const el = document.getElementById("health-badge");
  try {
    const h = await apiGet("/api/health");
    el.className = "badge badge-ok";
    el.textContent = "● server ok";
    el.title = `repo: ${h.repo_root} · Python ${h.python_version}`;
  } catch {
    el.className = "badge badge-err";
    el.textContent = "● server nedostupný";
  }
  setTimeout(pollHealth, 5000);
}

/* ── DENNÍ BĚH ───────────────────────────────────────────────────────────── */

const Daily = {
  _depots: [],
  async init() {
    this._depots = await apiGet("/api/depots").catch(() => []);
    const sel = document.getElementById("daily-depot");
    sel.innerHTML = this._depots.map(d => `<option value="${d.code}">${d.code} — ${esc(d.name)}</option>`).join("");
    sel.onchange = () => this.refreshInput();
    document.getElementById("daily-refresh-input").onclick = () => this.refreshInput();
    document.getElementById("daily-upload-btn").onclick = () => this.upload();
    document.getElementById("daily-show-cmd").onclick = () => this.run(true);
    document.getElementById("daily-run").onclick = () => this.run(false);
  },
  onShow() { if (this._depots.length) this.refreshInput(); },
  depot() { return document.getElementById("daily-depot").value; },
  async refreshInput() {
    const el = document.getElementById("daily-input-info");
    const depot = this.depot();
    try {
      const files = await apiGet(`/api/input/${depot}`);
      if (!files.length) { el.innerHTML = `<span class="muted">Prázdné — nahraj RiRo soubor.</span>`; return; }
      el.innerHTML = files.map(f =>
        `<div>📄 <strong>${esc(f.name)}</strong> <span class="hint">(${fmtBytes(f.size)}, datum ${esc(f.date || "?")})</span></div>`
      ).join("");
    } catch (e) { el.textContent = detailText(e); }
  },
  async upload() {
    const inp = document.getElementById("daily-upload-file");
    if (!inp.files.length) { toast("Vyber soubor k nahrání."); return; }
    const file = inp.files[0];
    const depot = this.depot();
    try {
      await apiUpload(`/api/input/${depot}/upload`, file);
      toast("Nahráno.");
      inp.value = ""; this.refreshInput();
    } catch (e) {
      if (e.status === 409) {
        const existing = (e.detail && e.detail.existing || []).join(", ");
        if (confirm(`V aktivni/ už je soubor: ${existing}.\nPřesunout do archivu a nahradit?`)) {
          try {
            await apiUpload(`/api/input/${depot}/upload?force=true`, file);
            toast("Starý soubor archivován, nový nahrán.");
            inp.value = ""; this.refreshInput();
          } catch (e2) { toast(detailText(e2)); }
        }
      } else { toast(detailText(e)); }
    }
  },
  body(dry) {
    const budget = document.getElementById("daily-budget").value;
    return {
      depot: this.depot(),
      budget_min: budget ? parseFloat(budget) : null,
      force_matrix: document.getElementById("daily-force-matrix").checked,
      fresh_osm: document.getElementById("daily-fresh-osm").checked,
      allow_profile_fallback: document.getElementById("daily-allow-fallback").checked,
      skip_startup_tests: document.getElementById("daily-skip-tests").checked,
      visualize: document.getElementById("daily-visualize").checked,
      dry: dry,
    };
  },
  async run(dry) {
    try {
      const job = await apiPost("/api/jobs/daily", this.body(dry));
      if (dry) {
        const pre = document.getElementById("daily-cmd-preview");
        pre.classList.remove("hidden");
        pre.textContent = job.steps.map(s => "$ " + s.cmdline).join("\n");
      } else {
        watchJob(job.id, document.getElementById("daily-job"));
      }
    } catch (e) { toast(detailText(e)); }
  },
};

/* ── BENCHMARKY ──────────────────────────────────────────────────────────── */

const Bench = {
  _inited: false,
  init() {
    document.getElementById("ad-show-cmd").onclick = () => this.runAll(true);
    document.getElementById("ad-run").onclick = () => this.runAll(false);
    document.getElementById("bm-show-cmd").onclick = () => this.runBench(true);
    document.getElementById("bm-run").onclick = () => this.runBench(false);
    document.getElementById("bench-session-refresh").onclick = () => this.loadSessions();
    document.getElementById("bench-session-select").onchange = (e) => this.loadSession(e.target.value);
    this._inited = true;
  },
  onShow() { this.loadSessions(); },
  val(id) { const v = document.getElementById(id).value.trim(); return v || null; },
  num(id) { const v = document.getElementById(id).value.trim(); return v ? parseFloat(v) : null; },
  chk(id) { return document.getElementById(id).checked; },
  allBody(dry) {
    return {
      date: this.val("ad-date"), depots: this.val("ad-depots"),
      budget_min: this.num("ad-budget"), budget_ratios: this.val("ad-ratios"),
      clusters: this.val("ad-clusters"), workers: this.num("ad-workers"),
      force_matrix: this.chk("ad-force-matrix"), fresh_osm: this.chk("ad-fresh-osm"),
      dry_run: this.chk("ad-dry-run"), skip_startup_tests: this.chk("ad-skip-tests"),
      dry: dry,
    };
  },
  benchBody(dry) {
    return {
      budget_min: this.num("bm-budget"), preset: this.val("bm-preset"),
      date: this.val("bm-date"), depots: this.val("bm-depots"),
      cluster_factors: this.val("bm-factors"), budget_profiles: this.val("bm-profiles"),
      list_only: this.chk("bm-list-only"), force_matrix: this.chk("bm-force-matrix"),
      fresh_osm: this.chk("bm-fresh-osm"), skip_startup_tests: this.chk("bm-skip-tests"),
      dry: dry,
    };
  },
  async runAll(dry) {
    try {
      const job = await apiPost("/api/jobs/all-depots", this.allBody(dry));
      this._afterSubmit(job, dry, "ad-cmd-preview");
    } catch (e) { toast(detailText(e)); }
  },
  async runBench(dry) {
    try {
      const job = await apiPost("/api/jobs/benchmark", this.benchBody(dry));
      this._afterSubmit(job, dry, "bm-cmd-preview");
    } catch (e) { toast(detailText(e)); }
  },
  _afterSubmit(job, dry, previewId) {
    if (dry) {
      const pre = document.getElementById(previewId);
      pre.classList.remove("hidden");
      pre.textContent = job.steps.map(s => "$ " + s.cmdline).join("\n");
    } else {
      watchJob(job.id, document.getElementById("bench-job"), () => this.loadSessions());
    }
  },
  async loadSessions() {
    try {
      const data = await apiGet("/api/results");
      const sel = document.getElementById("bench-session-select");
      const cur = sel.value;
      sel.innerHTML = `<option value="">— vyber session —</option>` +
        (data.benchmark_sessions || []).map(s => `<option value="${esc(s.path)}">${esc(s.name)}</option>`).join("");
      sel.value = cur;
    } catch (e) { toast(detailText(e)); }
  },
  async loadSession(path) {
    const box = document.getElementById("bench-session-table");
    if (!path) { box.innerHTML = ""; return; }
    try {
      const s = await apiGet(`/api/benchmark/session?path=${encodeURIComponent(path)}`);
      const runs = s.runs || [];
      if (!runs.length) { box.innerHTML = `<p class="muted">Žádné běhy (session se možná spouští).</p>`; return; }
      const cols = ["variant_id", "status", "budget_min", "clusters", "total_cost_kc", "total_km", "lines_count", "wall_elapsed_min"];
      const have = cols.filter(c => c in runs[0]);
      box.innerHTML = `<table><thead><tr>${have.map(c => `<th>${esc(c)}</th>`).join("")}</tr></thead>
        <tbody>${runs.map(r => `<tr>${have.map(c => `<td>${esc(r[c])}</td>`).join("")}</tr>`).join("")}</tbody></table>`;
    } catch (e) { toast(detailText(e)); }
  },
};

/* ── VÝSLEDKY ────────────────────────────────────────────────────────────── */

const Results = {
  _selected: null,
  init() {
    document.getElementById("results-refresh").onclick = () => this.loadList();
    document.getElementById("runlog-refresh").onclick = () => this.loadRunlog();
    document.getElementById("runlog-zone").onchange = () => this.loadRunlog();
  },
  onShow() { this.loadList(); this.loadRunlog(); },
  async loadList() {
    const el = document.getElementById("results-list");
    try {
      const data = await apiGet("/api/results");
      let html = "";
      const group = (title, runs) => {
        if (!runs || !runs.length) return "";
        return `<div class="results-group"><h4>${esc(title)}</h4>` +
          runs.map(r => `<div class="result-item" data-path="${esc(r.path)}" data-map="${r.has_map}">
            ${esc(r.label)} ${r.zone_summary ? `<span class="hint">${r.zone_summary.total_cost_kc || "?"} Kč</span>` : ""}</div>`).join("") +
          `</div>`;
      };
      for (const d of ["CB", "HK", "MO", "PR"]) html += group(d, data.depots[d]);
      html += group("ALL", data.all);
      el.innerHTML = html || `<span class="muted">Žádné běhy.</span>`;
      el.querySelectorAll(".result-item").forEach(it =>
        it.onclick = () => { el.querySelectorAll(".result-item").forEach(x => x.classList.remove("selected"));
          it.classList.add("selected"); this.loadDetail(it.dataset.path); });
    } catch (e) { el.textContent = detailText(e); }
  },
  async loadDetail(path) {
    this._selected = path;
    const el = document.getElementById("results-detail");
    try {
      const d = await apiGet(`/api/results/detail?path=${encodeURIComponent(path)}`);
      const zs = d.zone_summary || {};
      const card = (v, l) => `<div class="summary-card"><div class="val">${esc(v == null ? "—" : v)}</div><div class="lbl">${esc(l)}</div></div>`;
      const mix = zs.vehicle_type_mix ? Object.entries(zs.vehicle_type_mix).map(([k, v]) => `${k}: ${v}`).join(", ") : "";
      const files = (d.files || []).filter(f => /\.(csv|xlsx|json)$/i.test(f.name));
      el.innerHTML = `
        <div class="row" style="justify-content:space-between">
          <strong>${esc(path)}</strong>
        </div>
        <div class="summary-cards">
          ${card(zs.lines_count, "Linky")}${card(zs.total_cost_kc, "Cena Kč")}
          ${card(zs.total_km, "km")}${card(zs.total_hours, "hodiny")}
          ${card(zs.elapsed_min, "výpočet min")}
        </div>
        ${mix ? `<div class="hint">Vozidla: ${esc(mix)}</div>` : ""}
        <div class="row" style="margin-top:10px">
          ${files.map(f => `<a class="btn-secondary" href="/api/results/file?path=${encodeURIComponent(path + "/" + f.name)}" download>${esc(f.name)}</a>`).join(" ")}
        </div>
        <div class="row">
          <button class="btn-secondary" id="rd-regen">Přegenerovat mapu</button>
          <a class="btn-secondary" href="/api/results/map?path=${encodeURIComponent(path)}" target="_blank">Otevřít mapu v novém okně</a>
        </div>
        <div id="rd-mapwrap"></div>
        <div id="rd-job" class="job-detail hidden"></div>`;
      const mapf = (d.files || []).some(f => f.name === "routes_map.html");
      if (mapf) document.getElementById("rd-mapwrap").innerHTML =
        `<iframe class="map" src="/api/results/map?path=${encodeURIComponent(path)}"></iframe>`;
      else document.getElementById("rd-mapwrap").innerHTML = `<p class="hint">Mapa zatím není — použij „Přegenerovat mapu".</p>`;
      document.getElementById("rd-regen").onclick = async () => {
        if (!confirm("Přegenerovat mapu? Přepíše routes_map.html v této složce.")) return;
        try {
          const job = await apiPost("/api/jobs/visualize", { path: path });
          watchJob(job.id, document.getElementById("rd-job"), () => this.loadDetail(path));
        } catch (e) { toast(detailText(e)); }
      };
    } catch (e) { el.textContent = detailText(e); }
  },
  async loadRunlog() {
    const zone = document.getElementById("runlog-zone").value;
    const box = document.getElementById("runlog-table");
    try {
      const recs = await apiGet(`/api/runlog?zone=${encodeURIComponent(zone)}&limit=100`);
      // naplň zone select jednou
      const zsel = document.getElementById("runlog-zone");
      if (zsel.dataset.filled !== "1") {
        // necháme jen statické; doplníme z dat
      }
      const cols = [["delivery_date", "Datum"], ["zone", "Zóna"], ["budget_min", "Budget"],
        ["lines_count", "Linky"], ["total_cost_kc", "Cena Kč"], ["total_km", "km"],
        ["total_hours", "h"], ["elapsed_min", "výpočet"]];
      box.innerHTML = `<table><thead><tr><th></th>${cols.map(c => `<th>${c[1]}</th>`).join("")}</tr></thead>
        <tbody>${recs.map((r, i) => `<tr>
          <td><input type="checkbox" class="runlog-cmp" data-i="${i}"></td>
          ${cols.map(c => `<td>${esc(r[c[0]])}</td>`).join("")}</tr>`).join("")}</tbody></table>`;
      this._recs = recs;
      box.querySelectorAll(".runlog-cmp").forEach(cb => cb.onchange = () => this.compare());
      // zone options z dat
      const zones = [...new Set(recs.map(r => r.zone).filter(Boolean))];
      const zc = document.getElementById("runlog-zone");
      const cur = zc.value;
      const opts = `<option value="">— všechny —</option>` + zones.map(z => `<option value="${esc(z)}">${esc(z)}</option>`).join("");
      if (zc.dataset.zones !== zones.join(",")) { zc.innerHTML = opts; zc.value = cur; zc.dataset.zones = zones.join(","); }
    } catch (e) { box.textContent = detailText(e); }
  },
  compare() {
    const checked = [...document.querySelectorAll(".runlog-cmp:checked")].map(cb => this._recs[+cb.dataset.i]);
    const box = document.getElementById("runlog-compare");
    if (checked.length !== 2) { box.innerHTML = checked.length ? `<p class="hint">Zaškrtni přesně 2 běhy.</p>` : ""; return; }
    const [a, b] = checked;
    const rows = [["Cena Kč", "total_cost_kc"], ["Linky", "lines_count"], ["km", "total_km"], ["Hodiny", "total_hours"]];
    box.innerHTML = `<table><thead><tr><th>Metrika</th><th>${esc(a.run_id || a.delivery_date)}</th>
      <th>${esc(b.run_id || b.delivery_date)}</th><th>Δ</th></tr></thead><tbody>${
      rows.map(([lbl, k]) => {
        const av = +a[k], bv = +b[k];
        const delta = (isFinite(av) && isFinite(bv)) ? (bv - av).toFixed(1) : "";
        return `<tr><td>${lbl}</td><td>${esc(a[k])}</td><td>${esc(b[k])}</td><td>${delta}</td></tr>`;
      }).join("")}</tbody></table>`;
  },
};

/* ── ÚLOHY ───────────────────────────────────────────────────────────────── */

const Jobs = {
  init() {
    document.getElementById("jobs-refresh").onclick = () => this.load();
    document.getElementById("jobs-selftest").onclick = () => this.selftest();
  },
  onShow() { this.load(); },
  async load() {
    const box = document.getElementById("jobs-table");
    try {
      const jobs = await apiGet("/api/jobs?limit=50");
      box.innerHTML = `<table><thead><tr><th>Vytvořeno</th><th>Typ</th><th>Název</th><th>Stav</th></tr></thead>
        <tbody>${jobs.map(j => `<tr class="clickable" data-id="${esc(j.id)}">
          <td>${esc(j.created_at)}</td><td>${esc(j.type)}</td><td>${esc(j.title)}</td>
          <td>${statusPill(j.status)}</td></tr>`).join("")}</tbody></table>`;
      box.querySelectorAll("tr.clickable").forEach(tr =>
        tr.onclick = () => watchJob(tr.dataset.id, document.getElementById("jobs-job")));
    } catch (e) { box.textContent = detailText(e); }
  },
  async selftest() {
    try {
      const job = await apiPost("/api/jobs/selftest", {});
      watchJob(job.id, document.getElementById("jobs-job"), () => this.load());
      this.load();
    } catch (e) { toast(detailText(e)); }
  },
};

/* ── UZAVÍRKY ────────────────────────────────────────────────────────────── */

const Closures = {
  init() {
    document.getElementById("clo-refresh").onclick = () => { this.load(); this.reloadMap(); };
    document.getElementById("clo-editor-start").onclick = () => this.startEditor();
    document.getElementById("clo-test-run").onclick = () => this.runTest();
  },
  onShow() { this.load(); this.reloadMap(); },
  reloadMap() {
    const m = document.getElementById("clo-map");
    if (m) m.src = "/api/closures/map?_=" + Date.now();   // bust cache
  },
  async load() {
    const el = document.getElementById("clo-list");
    try {
      const d = await apiGet("/api/closures");
      const cs = d.closures || [];
      if (!cs.length) { el.innerHTML = `<p class="muted">Žádné uzavírky.</p>`; return; }
      el.innerHTML = `<table><thead><tr><th>ID</th><th>Název</th><th>Aktivní</th>
          <th>Platnost</th><th>Buffer</th><th></th></tr></thead><tbody>${
        cs.map(c => `<tr>
          <td>${esc(c.id)}</td><td>${esc(c.name)}</td>
          <td>${c.active
            ? '<span class="status-pill st-success">ano</span>'
            : '<span class="status-pill st-skipped">ne</span>'}</td>
          <td class="hint">${esc(c.valid_from || "")}${c.valid_to ? " – " + esc(c.valid_to) : " – ∞"}</td>
          <td>${c.buffer_km != null ? esc(c.buffer_km) + " km" : ""}</td>
          <td>
            <button class="btn-secondary clo-toggle" data-id="${esc(c.id)}">${c.active ? "Deaktivovat" : "Aktivovat"}</button>
            <button class="btn-secondary clo-remove" data-id="${esc(c.id)}">Smazat</button>
          </td></tr>`).join("")}</tbody></table>`;
      el.querySelectorAll(".clo-toggle").forEach(b => b.onclick = () => this.toggle(b.dataset.id));
      el.querySelectorAll(".clo-remove").forEach(b => b.onclick = () => this.remove(b.dataset.id));
    } catch (e) { el.textContent = detailText(e); }
  },
  async toggle(id) {
    try {
      const d = await apiPost(`/api/closures/${id}/toggle`, {});
      if (d.returncode !== 0) toast(d.output || "Toggle selhal.");
      this.load(); this.reloadMap();
    } catch (e) { toast(detailText(e)); }
  },
  async remove(id) {
    if (!confirm(`Opravdu TRVALE smazat uzavírku ${id}?`)) return;
    try {
      const d = await apiPost(`/api/closures/${id}/remove`, {});
      if (d.returncode !== 0) toast(d.output || "Smazání selhalo.");
      this.load(); this.reloadMap();
    } catch (e) { toast(detailText(e)); }
  },
  async startEditor() {
    try {
      const d = await apiPost("/api/closures/editor/start", {});
      // Editor si sám otevře okno (webbrowser.open); tady jen ukážeme odkaz,
      // ať neotvíráme tab dvakrát a nezávodíme se startem serveru na :8765.
      document.getElementById("clo-editor-link").innerHTML =
        (d.already_running ? "Editor už běží: " : "Editor se spouští: ") +
        `<a href="${d.url}" target="_blank">${d.url}</a>` +
        " — po uložení uzavírky klikni na Obnovit.";
    } catch (e) { toast(detailText(e)); }
  },
  async runTest() {
    const orders = document.getElementById("clo-test-orders").value.trim();
    if (!orders) { toast("Zadej cestu k orders souboru."); return; }
    const fresh = document.getElementById("clo-test-fresh").checked;
    try {
      const job = await apiPost("/api/closures/test", {
        orders_file: orders,
        osrm_url: fresh ? "http://localhost:5001" : null,
        skip_startup_tests: true,
      });
      watchJob(job.id, document.getElementById("clo-job"));
    } catch (e) { toast(detailText(e)); }
  },
};

/* ── Init ────────────────────────────────────────────────────────────────── */

(async function init() {
  await Daily.init();
  Bench.init();
  Results.init();
  Closures.init();
  Jobs.init();
  pollHealth();
  activateTab(location.hash.slice(1) || "denni");
})();
