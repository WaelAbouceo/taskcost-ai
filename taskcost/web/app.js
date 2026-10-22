"use strict";

const app = document.getElementById("app");

const esc = (value) =>
  String(value ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const egp = (value) => (value == null ? "n/a" : Number(value).toFixed(4));
const pct = (value) => (value == null ? "n/a" : `${Math.round(value * 100)}%`);
const when = (iso) => (iso ? new Date(iso).toLocaleString() : "");
// An anonymous id for this browser, so the tool can learn this user's industry, language, usual use case and
// scale from their own requests (see /api/profile). No name, email or login.
const CLIENT_KEY = "taskcost.client";
function clientId() {
  let id = localStorage.getItem(CLIENT_KEY);
  if (!id) {
    id = (crypto.randomUUID ? crypto.randomUUID() : `${Date.now()}-${Math.random().toString(16).slice(2)}`).replace(/[^A-Za-z0-9-]/g, "");
    localStorage.setItem(CLIENT_KEY, id);
  }
  return id;
}
const getProfile = () => api(`/api/profile?client=${encodeURIComponent(clientId())}`).catch(() => ({ requests: 0 }));

// ---------- sovereignty: the user's country and how strictly their data must stay close ----------

const COUNTRY_KEY = "taskcost.country";
const LEVEL_KEY = "taskcost.sovereignty";
const TIMEZONE_COUNTRY = {
  "Africa/Cairo": "EG", "Asia/Riyadh": "SA", "Asia/Dubai": "AE", "Asia/Qatar": "QA", "Asia/Kuwait": "KW",
  "Asia/Bahrain": "BH", "Asia/Muscat": "OM", "Asia/Amman": "JO", "Asia/Beirut": "LB", "Asia/Baghdad": "IQ",
  "Africa/Casablanca": "MA", "Africa/Tunis": "TN", "Africa/Algiers": "DZ", "Europe/Vienna": "AT",
  "Europe/Brussels": "BE", "Europe/Berlin": "DE", "Europe/Copenhagen": "DK", "Europe/Madrid": "ES",
  "Europe/Helsinki": "FI", "Europe/Paris": "FR", "Europe/Athens": "GR", "Europe/Dublin": "IE", "Europe/Rome": "IT",
  "Europe/Amsterdam": "NL", "Europe/Warsaw": "PL", "Europe/Lisbon": "PT", "Europe/Stockholm": "SE",
  "Europe/Oslo": "NO", "Europe/London": "GB", "Europe/Zurich": "CH", "America/Toronto": "CA", "America/Vancouver": "CA",
};

// The country from the browser's time zone, then its language (ar-EG), else the configured default. Nothing leaves the browser.
function detectCountry(cfg) {
  const known = new Set(cfg.countries.map((c) => c.code));
  const zone = Intl.DateTimeFormat().resolvedOptions().timeZone || "";
  const fromZone = TIMEZONE_COUNTRY[zone] || (zone.startsWith("America/") ? "US" : null);
  const fromLanguage = (navigator.languages || [navigator.language]).map((l) => (l.split("-")[1] || "").toUpperCase());
  return [fromZone, ...fromLanguage].find((code) => code && known.has(code)) || cfg.country;
}

const chosenCountry = (cfg) => {
  const saved = localStorage.getItem(COUNTRY_KEY);
  return cfg.countries.some((c) => c.code === saved) ? saved : detectCountry(cfg);
};
const chosenLevel = (cfg) => {
  const saved = localStorage.getItem(LEVEL_KEY);
  return cfg.levels.some((l) => l.id === saved) ? saved : cfg.sovereignty;
};
const countryOf = (cfg, code) => cfg.countries.find((c) => c.code === code) || { code, name: code, law: "" };

const FIT_TEXT = {
  in_country: ["egypt", "In your country"], allowed: ["open", "Allowed by your law"], region: ["warn", "In your region"],
  abroad: ["closed", "Abroad"], unverified: ["muted", "Location unverified"],
};
// A badge for where a model runs, relative to the user's country (fit from sovereignty.py).
function residencyBadge(fit, locations = [], countryName = "") {
  if (!fit || !FIT_TEXT[fit]) return "";
  const [cls, label] = FIT_TEXT[fit];
  const places = locations.filter((l) => l !== "*").join(", ");
  const text = fit === "in_country" && countryName ? `In ${countryName}` : fit === "abroad" && places ? `Abroad · ${places}` : label;
  const title = fit === "unverified"
    ? "The hosting agent could not verify where this model runs (or its provider forwards requests to other companies)."
    : `Runs in: ${places || "unknown"} (verified daily by the hosting agent from the providers' own pages)`;
  return `<span class="badge ${cls}" title="${esc(title)}">${esc(text)}</span>`;
}
const sourceBadge = (open) => (open == null ? "" : open
  ? `<span class="badge open" title="Open-weight model (per SovereignEG's catalog): anyone can download and self-host it">Open source</span>`
  : `<span class="badge closed" title="Proprietary model (per SovereignEG's catalog), used as the fallback">Proprietary</span>`);

async function api(path, options = {}) {
  const response = await fetch(path, options);
  const body = await response.json().catch(() => ({}));
  if (!response.ok) {
    const detail = typeof body.detail === "string" ? body.detail : JSON.stringify(body.detail || response.statusText);
    throw new Error(detail);
  }
  return body;
}

let configCache = null;
const getConfig = async () => (configCache ??= await api("/api/config"));

function simpleTasks(node) {
  return node.kind === "simple" ? [node] : (node.children || []).flatMap(simpleTasks);
}

function criterionText(criterion) {
  return typeof criterion === "string" ? criterion : `(optional) ${criterion.text}`;
}

// ---------- under the hood: every step behind a result ----------

const secs = (s) => (s == null ? "" : s < 10 ? `${s.toFixed(1)} s` : `${Math.round(s)} s`);
const fitText = (fit) => (fit ? residencyBadge(fit.fit, fit.locations, "") : "");

// "How we got this answer": the steps, the model behind each (and where it runs), time and cost.
function renderTrace(trace, open = false) {
  if (!trace?.steps?.length) return "";
  const data = trace.data || {};
  const where = data.total ? ` · your task reached ${data.local} of ${data.total} models in your country or places your law allows` : "";
  return `<details class="options trace" ${open ? "open" : ""}>
      <summary>How we got this answer <span class="hint">${trace.steps.length} steps · ${secs(trace.seconds)} · ${egp(trace.cost_egp)} EGP${where}</span></summary>
      <ol class="trace-steps">${trace.steps.map((s) => `<li>
        <div class="trace-head"><strong>${esc(s.title)}</strong>
          <span class="hint">${[secs(s.seconds), s.cost_egp != null ? `${egp(s.cost_egp)} EGP` : "", s.cached ? "from cache" : ""].filter(Boolean).join(" · ")}</span></div>
        ${s.model ? `<div class="trace-model"><code>${esc(s.model)}</code> ${fitText(s.fit)}</div>` : ""}
        <p>${esc(s.detail)}</p>
        ${s.items?.length ? `<ul class="trace-items">${s.items.map((i) => `<li><code>${esc(i.model)}</code> ${fitText(i.fit)}
          <span class="hint">${[i.detail, secs(i.seconds), i.cost_egp != null ? `${egp(i.cost_egp)} EGP` : ""].filter(Boolean).join(" · ")}</span></li>`).join("")}</ul>` : ""}
      </li>`).join("")}</ol>
    </details>`;
}

// The steps a comparison goes through, shown live while it runs (job phases from the server).
const LIVE_STEPS = {
  quick: [["size", "Understanding your request"], ["design", "Writing a concrete test task"],
          ["recommend", "Agent 1 predicts the best model"], ["run", "Testing models and reviewing answers"]],
  blueprint: [["size", "Understanding your request"], ["design", "Mapping it onto standard enterprise processes"],
              ["estimate", "Agent 1 chooses a model for every AI step"]],
};
function liveSteps(kind, phase, extra = "") {
  const steps = LIVE_STEPS[kind] || LIVE_STEPS.quick;
  const at = Math.max(0, steps.findIndex(([id]) => id === phase));
  return `<ol class="live-steps">${steps.map(([id, label], i) => `<li class="${i < at ? "done" : i === at ? "now" : ""}">
      ${i < at ? "✓" : i === at ? `<span class="spinner"></span>` : "○"} ${esc(label)}${i === at && extra ? ` <span class="hint">${esc(extra)}</span>` : ""}</li>`).join("")}</ol>`;
}

// ---------- routing ----------

async function route() {
  const [, view, name] = location.hash.split("/");
  const section = view === "plan" ? "plans" : ["compare", "blueprint"].includes(view) ? "home" : view || "home";
  document.querySelectorAll("[data-nav]").forEach((a) => a.classList.toggle("active", a.dataset.nav === section));
  app.innerHTML = `<div class="empty">Loading…</div>`;
  try {
    if (view === "plan" && name) await renderPlan(decodeURIComponent(name));
    else if (view === "plans") await renderPlans();
    else if (view === "results") await renderResults(name ? decodeURIComponent(name) : null);
    else if (view === "new") await renderNew();
    else if (view === "compare" && name) await renderAnswer(decodeURIComponent(name));
    else if (view === "blueprint" && name) await renderBlueprint(decodeURIComponent(name));
    else if (view === "hosting") await renderHosting();
    else if (view === "insights") await renderInsights();
    else await renderHome();
  } catch (error) {
    app.innerHTML = `<div class="notice bad">${esc(error.message)}</div>`;
  }
}

window.addEventListener("hashchange", route);
window.addEventListener("DOMContentLoaded", route);

// ---------- background jobs ----------

// Polls a background job until it ends: render(state) while it runs, then onDone(state) or onError(state).
function followJob(job, { render, onDone, onError }) {
  const tick = async () => {
    const state = await api(`/api/jobs/${job.id}`).catch((error) => ({ status: "error", error: error.message }));
    if (state.status === "error") return onError(state);
    if (state.status === "done") return onDone(state);
    render(state);
    setTimeout(tick, 1200);
  };
  tick();
}

// The advanced pages' job box: progress bar and the job's log.
function pollJob(job, box, onDone) {
  const show = (state) => {
    const share = state.total ? Math.round((state.done / state.total) * 100) : null;
    const title = state.kind === "design" ? "The design agent is working" : "Running the job";
    box.innerHTML = `
      ${state.status === "running" ? `<p><strong>${title}</strong> · ${state.elapsed_s}s${
        state.total ? ` · ${state.done} of ${state.total} simple-task runs done` : ""}</p>
        <div class="progress"><div style="width:${share ?? 100}%;${share == null ? "opacity:.3" : ""}"></div></div>` : ""}
      ${state.status === "error" ? `<div class="notice bad">${esc(state.error)}</div>` : ""}
      <pre class="log">${esc((state.lines || []).join("\n"))}</pre>`;
  };
  followJob(job, { render: show, onError: show, onDone: (state) => { show(state); onDone(state); } });
}

// The simple compare flow (home page and answer page) lives in compare.js.

// ---------- new job ----------

async function renderNew() {
  const cfg = await getConfig();
  app.innerHTML = `
    <h1>New job</h1>
    <p class="lead">Describe the work. The design agent breaks it down into simple tasks, the unit we measure and price.</p>
    ${cfg.has_key ? "" : `<div class="notice bad">LLM_API_KEY is not set, so the design agent can't run.
      You can still open saved plans and run them in test mode.</div>`}
    <form id="new-job" class="card">
      <label class="field">What do you need done? <span class="hint">A project, a complex task or a simple task.</span></label>
      <textarea name="request" rows="3">Extract the key data from this contract</textarea>
      <label class="field">Documents <span class="hint">.txt, .md or .pdf</span></label>
      <input type="file" name="files" multiple accept=".txt,.md,.pdf">
      ${cfg.samples.length ? `<p class="hint" style="margin:12px 0 0">Or use a sample document:</p>
        ${cfg.samples.map((s) => `<label class="check"><input type="checkbox" name="samples" value="${esc(s)}"> ${esc(s)}</label>`).join("")}` : ""}
      <div class="actions">
        <button type="submit" ${cfg.has_key ? "" : "disabled"}>Break it down</button>
        <span class="hint">Usually takes about a minute · follows your country and sovereignty choice on the home page</span>
      </div>
      <div id="progress"></div>
    </form>`;

  const form = document.getElementById("new-job");
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const data = new FormData(form);
    if (!form.files.files.length) data.delete("files");
    data.set("client", clientId());
    data.set("country", chosenCountry(cfg));
    data.set("sovereignty", chosenLevel(cfg));
    const button = form.querySelector("button");
    const progress = document.getElementById("progress");
    button.disabled = true;
    try {
      const job = await api("/api/plans", { method: "POST", body: data });
      pollJob(job, progress, (done) => (location.hash = `#/plan/${done.result}`));
    } catch (error) {
      progress.innerHTML = `<div class="notice bad">${esc(error.message)}</div>`;
      button.disabled = false;
    }
  });
}

// ---------- plans ----------

async function renderPlans() {
  const plans = await api("/api/plans");
  app.innerHTML = `
    <h1>Plans</h1>
    <p class="lead">Work that the design agent has broken down into simple tasks. Open one to check it and run it.</p>
    <div class="card list">
      ${plans.length ? plans.map((p) => `
        <a class="list-item" href="#/plan/${encodeURIComponent(p.name)}">
          <div><strong>${esc(p.title)}</strong><div class="hint">${esc(p.request)}</div></div>
          <div class="meta">${p.tasks} simple tasks<br>
            ${p.reviewed ? `<span class="badge good">checked</span>` : `<span class="badge muted">not checked</span>`}</div>
        </a>`).join("") : `<div class="empty">No plans yet. <a href="#/new">Start a new job</a>.</div>`}
    </div>`;
}

async function renderPlan(name) {
  const [plan, cfg] = await Promise.all([api(`/api/plans/${encodeURIComponent(name)}`), getConfig()]);
  const tasks = simpleTasks(plan.root);
  const design = plan.design || {};

  app.innerHTML = `
    <p><a href="#/plans">← All plans</a></p>
    <h1>${esc(plan.root.title)}</h1>
    <p class="lead">${esc(plan.request)}</p>
    <div class="grid stats">
      <div class="card stat"><div class="label">Simple tasks</div><div class="value">${tasks.length}</div></div>
      <div class="card stat"><div class="label">Designed by</div><div class="value" style="font-size:16px">${esc(design.model || "?")}</div></div>
      <div class="card stat"><div class="label">Design cost</div><div class="value">${egp(design.cost_egp)} <small>EGP</small></div></div>
      <div class="card stat"><div class="label">Status</div><div class="value" id="status-badge"></div></div>
    </div>

    <h2>1. Check the plan</h2>
    <p class="hint">Check every expected answer against the document: the reviewers compare each model's answer with it.
      One check per line; put <code>(optional)</code> in front of a check that isn't required.</p>
    <div id="tasks">
      ${tasks.map((t, i) => `
        <details class="task" data-id="${esc(t.id)}" ${i === 0 ? "open" : ""}>
          <summary>
            <span class="id">${esc(t.id)}</span><span class="badge">${esc(t.task_type)}</span>
            <h3>${esc(t.title)}</h3>
            ${t.depends_on?.length ? `<span class="uses">uses ${esc(t.depends_on.join(", "))}</span>` : ""}
          </summary>
          <div class="body">
            <label class="field">What the model must do</label>
            <textarea data-field="instruction" rows="2">${esc(t.instruction)}</textarea>
            <label class="field">Expected answer</label>
            <textarea data-field="expected_answer" rows="2">${esc(t.expected_answer || "")}</textarea>
            <label class="field">Checks the reviewers apply</label>
            <textarea data-field="criteria" rows="${Math.max(2, t.criteria.length)}">${esc(t.criteria.map(criterionText).join("\n"))}</textarea>
          </div>
        </details>`).join("")}
    </div>
    <div class="actions">
      <label class="check"><input type="checkbox" id="reviewed" ${plan.reviewed ? "checked" : ""}> I checked this plan</label>
      <button class="secondary" id="save">Save changes</button>
      <span class="hint" id="save-message"></span>
    </div>

    <h2>2. Run it</h2>
    <div class="card" id="run-card">
      <label class="field">Models to compare</label>
      <div id="models">
        ${cfg.models.map((m) => `<label class="check"><input type="checkbox" value="${esc(m.label)}" checked> ${esc(m.label)}</label>`).join("")}
      </div>
      <div style="display:flex;gap:8px;margin-top:8px;max-width:520px">
        <input type="text" id="add-model" list="catalog" placeholder="Add another model from the catalog">
        <button type="button" class="secondary" id="add">Add</button>
      </div>
      <datalist id="catalog">${cfg.catalog.map((id) => `<option value="${esc(id)}">`).join("")}</datalist>
      <label class="field">How many times to run the whole job <span class="hint">more runs give more reliable results</span></label>
      <input type="number" id="trials" min="1" max="10" value="${cfg.trials}">
      <label class="check"><input type="checkbox" id="mock"> Test mode: simulated models, no API calls, no cost</label>
      <p class="hint" id="estimate"></p>
      <div class="actions"><button id="run">Run</button></div>
      <div id="run-progress"></div>
    </div>`;

  const reviewed = document.getElementById("reviewed");
  const showStatus = () => {
    document.getElementById("status-badge").innerHTML = reviewed.checked
      ? `<span class="badge good">Checked</span>` : `<span class="badge muted">Not checked</span>`;
  };
  showStatus();
  reviewed.addEventListener("change", showStatus);

  const selectedModels = () => [...document.querySelectorAll("#models input:checked")].map((box) => box.value);
  const updateEstimate = () => {
    const models = selectedModels().length;
    const trials = Number(document.getElementById("trials").value) || 1;
    const mock = document.getElementById("mock").checked;
    const answers = tasks.length * models * trials;
    const judged = cfg.judges.filter((j) => selectedModels().includes(j));
    document.getElementById("estimate").innerHTML = mock
      ? "Test mode: simulated models and reviewers, nothing is charged."
      : `About <strong>${answers}</strong> answers (up to ${answers * cfg.max_attempts} with retries), each checked by
         ${cfg.judges.length} reviewers (${cfg.judges.map(esc).join(", ")}).
         ${judged.length ? `<br><span class="badge bad">Warning</span> ${judged.map(esc).join(", ")} is also a reviewer.` : ""}`;
  };
  updateEstimate();
  document.getElementById("run-card").addEventListener("change", updateEstimate);

  document.getElementById("add").addEventListener("click", () => {
    const input = document.getElementById("add-model");
    const id = input.value.trim();
    if (!id || selectedModels().includes(id)) return;
    document.getElementById("models").insertAdjacentHTML(
      "beforeend", `<label class="check"><input type="checkbox" value="${esc(id)}" checked> ${esc(id)}</label>`);
    input.value = "";
    updateEstimate();
  });

  const save = async () => {
    const edits = {};
    document.querySelectorAll("#tasks .task").forEach((card) => {
      const field = (key) => card.querySelector(`[data-field="${key}"]`).value;
      edits[card.dataset.id] = {
        instruction: field("instruction"),
        expected_answer: field("expected_answer"),
        criteria: field("criteria").split("\n"),
      };
    });
    await api(`/api/plans/${encodeURIComponent(name)}`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ reviewed: reviewed.checked, tasks: edits }),
    });
  };

  document.getElementById("save").addEventListener("click", async () => {
    const message = document.getElementById("save-message");
    try {
      await save();
      message.textContent = `Saved at ${new Date().toLocaleTimeString()}`;
    } catch (error) {
      message.innerHTML = `<span class="badge bad">Not saved</span> ${esc(error.message)}`;
    }
  });

  document.getElementById("run").addEventListener("click", async (event) => {
    const mock = document.getElementById("mock").checked;
    const progress = document.getElementById("run-progress");
    if (!mock && !reviewed.checked && !confirm("This plan isn't marked as checked. Run it anyway?")) return;
    event.target.disabled = true;
    try {
      await save();
      const job = await api(`/api/plans/${encodeURIComponent(name)}/run`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ models: selectedModels(), trials: Number(document.getElementById("trials").value), mock }),
      });
      pollJob(job, progress, (done) => (location.hash = `#/results/${done.result}`));
    } catch (error) {
      progress.innerHTML = `<div class="notice bad">${esc(error.message)}</div>`;
      event.target.disabled = false;
    }
  });
}

// ---------- results ----------

async function renderResults(name) {
  const runs = await api("/api/results");
  if (!name) {
    renderHistory(runs);
    return;
  }
  const selected = name;
  const detail = await api(`/api/results/${encodeURIComponent(selected)}`);
  const full = runs.filter((r) => !r.quick && r.kind !== "blueprint");

  app.innerHTML = `
    <div class="layout">
      <div class="card list side">
        ${full.map((r) => `
          <a class="list-item ${r.name === selected ? "active" : ""}" href="${historyLink(r)}">
            <strong>${esc(r.title || r.name)}</strong>
            <small>${r.test_mode ? `<span class="badge muted">test mode</span> ` : ""}${r.kind === "plan" ? "Big job" : "Standard suite"} · ${esc(when(r.started_at))}</small>
            <small>${esc(r.models.join(", "))}</small>
          </a>`).join("")}
      </div>
      <div id="detail"></div>
    </div>`;
  const container = document.getElementById("detail");
  if (detail.config.kind === "plan" && detail.economics) renderJobResult(container, detail);
  else renderSuiteResult(container, detail);
}

function jobRanking(jobs, minRate) {
  return Object.keys(jobs).sort((a, b) => {
    const ok = (m) => jobs[m].passed > 0 && jobs[m].success_rate >= minRate;
    if (ok(a) !== ok(b)) return ok(a) ? -1 : 1;
    if (ok(a)) return jobs[a].cost_per_success_egp - jobs[b].cost_per_success_egp;
    return jobs[b].success_rate - jobs[a].success_rate;
  });
}

function renderJobResult(container, detail) {
  const { economics: econ, config: cfg } = detail;
  const jobs = econ.jobs;
  const models = Object.keys(jobs);
  const ranked = jobRanking(jobs, cfg.min_success_rate ?? 0.8);
  const byModel = Object.fromEntries(detail.models.map((m) => [m.model, m]));
  const judgeCost = detail.models.reduce((sum, m) => sum + (m.grading_cost_egp || 0), 0);
  const mix = econ.mix;
  const bestSingle = ranked.find((m) => jobs[m].cost_per_success_egp != null && jobs[m].success_rate >= (cfg.min_success_rate ?? 0.8));
  const bars = [...ranked.filter((m) => jobs[m].cost_per_success_egp != null).map((m) => [m, jobs[m].cost_per_success_egp])];
  if (mix.cost_per_success_egp != null) bars.push(["Best mix", mix.cost_per_success_egp]);
  const maxCost = Math.max(...bars.map(([, v]) => v), 0.0001);

  const mixPlan = Object.entries(mix.assignment).reduce((groups, [task, model]) => {
    if (model) (groups[model] ||= []).push(task);
    return groups;
  }, {});

  container.innerHTML = `
    <h1>${esc(cfg.name)}</h1>
    <p class="lead">${esc(cfg.request)}</p>
    ${cfg.test_mode ? `<div class="notice"><strong>Test mode:</strong> simulated models and reviewers.
      These numbers only show how the system works; they say nothing about real models.</div>` : ""}
    <div class="grid stats">
      <div class="card stat"><div class="label">Simple tasks</div><div class="value">${econ.tasks.length}</div></div>
      <div class="card stat"><div class="label">Runs per model</div><div class="value">${cfg.trials}</div></div>
      <div class="card stat"><div class="label">Design cost</div><div class="value">${egp(cfg.design?.cost_egp)} <small>EGP</small></div></div>
      <div class="card stat"><div class="label">Reviewer cost</div><div class="value">${egp(judgeCost)} <small>EGP</small></div></div>
    </div>

    <h2>What the whole job costs</h2>
    <p class="hint">A job counts as done only if every simple task in it passed. Retries and failed attempts are included in the cost.</p>
    <div class="table-wrap"><table>
      <tr><th>Model</th><th class="num">Job done correctly</th><th class="num">EGP per correct job</th>
          <th class="num">EGP per job attempt</th><th class="num">Simple tasks passed</th></tr>
      ${ranked.map((m) => {
        const s = byModel[m];
        return `<tr><td class="nowrap">${esc(m)}</td><td class="num">${jobs[m].passed}/${jobs[m].runs} (${pct(jobs[m].success_rate)})</td>
          <td class="num">${egp(jobs[m].cost_per_success_egp)}</td><td class="num">${egp(jobs[m].cost_per_run_egp)}</td>
          <td class="num">${s ? `${s.passed_runs}/${s.runs + s.blocked_runs}` : "n/a"}</td></tr>`;
      }).join("")}
      ${mix.cost_per_success_egp != null ? `<tr class="highlight"><td>Best mix (best model for each task)</td>
        <td class="num">est. ${pct(mix.estimated_success_rate)}</td><td class="num">${egp(mix.cost_per_success_egp)}</td><td></td><td></td></tr>` : ""}
    </table></div>

    <div class="card" style="margin-top:14px">
      <h3 style="margin-bottom:8px">EGP per correct job</h3>
      ${bars.map(([label, value]) => `
        <div class="bar-row"><span>${esc(label)}</span>
          <div class="bar-track"><div class="bar-fill" style="width:${(value / maxCost) * 100}%"></div></div>
          <span class="num">${egp(value)}</span></div>`).join("")}
    </div>

    ${mix.cost_per_success_egp != null ? `<div class="notice good"><strong>Recommendation:</strong>
      ${Object.entries(mixPlan).map(([m, ts]) => `use <strong>${esc(m)}</strong> for ${esc(ts.join(", "))}`).join("; ")}.
      Estimated <strong>${egp(mix.cost_per_success_egp)} EGP</strong> per correct job${
        bestSingle ? `, compared with ${egp(jobs[bestSingle].cost_per_success_egp)} EGP using only ${esc(bestSingle)}` : ""}.</div>`
      : `<div class="notice">No model was reliable on ${esc(mix.uncovered_tasks.join(", "))}, so there's no recommended mix yet.</div>`}

    <h2>Best model for each simple task</h2>
    <p class="hint">Each cell shows <em>passed / tried</em> and EGP per correct answer. Click a cell to see the answer and what the reviewers said.</p>
    <div class="table-wrap"><table id="task-table">
      <tr><th>Task</th><th>Type</th>${models.map((m) => `<th class="num">${esc(m)}</th>`).join("")}<th>Recommended</th></tr>
      ${econ.tasks.map((t) => `<tr>
        <td><strong>${esc(t.task_id)}</strong> ${esc(t.title)}</td><td><span class="badge">${esc(t.task_type)}</span></td>
        ${models.map((m) => {
          const s = t.models[m];
          const cls = `num clickable ${t.recommended === m ? "best" : ""}`;
          const text = s.attempted ? `${s.passed}/${s.attempted} · ${egp(s.cost_per_success_egp)}` : "blocked";
          const badge = s.attempted && s.passed < s.attempted ? "bad" : s.attempted ? "good" : "muted";
          return `<td class="${cls}" data-task="${esc(t.task_id)}" data-model="${esc(m)}" data-title="${esc(t.title)}">
            <span class="badge ${badge}">${text}</span></td>`;
        }).join("")}
        <td>${t.recommended ? esc(t.recommended) : `<span class="badge bad">none reliable</span>`}</td></tr>`).join("")}
    </table></div>

    ${detail.common_failures?.length ? `
      <h2>Most common failures</h2>
      <div class="table-wrap"><table>
        <tr><th>Task</th><th>What the reviewers said was missing or wrong</th><th class="num">Failed runs</th><th>Models</th></tr>
        ${detail.common_failures.map((f) => `<tr><td>${esc(f.task_id)}</td><td>${esc(f.criterion)}</td>
          <td class="num">${f.runs}</td><td>${esc(f.models.join(", "))}</td></tr>`).join("")}
      </table></div>` : ""}

    <details style="margin-top:24px"><summary class="hint">Full report (markdown)</summary>
      <pre class="answer">${esc(detail.report_md)}</pre></details>`;

  container.querySelector("#task-table").addEventListener("click", (event) => {
    const cell = event.target.closest("td[data-task]");
    if (cell) openAnswers(detail.name, cell.dataset.task, cell.dataset.model, cell.dataset.title);
  });
}

function renderSuiteResult(container, detail) {
  const cfg = detail.config;
  container.innerHTML = `
    <h1>${esc(cfg.name)}</h1>
    <p class="lead">Standard task suite · ${cfg.task_ids?.length ?? "?"} tasks · ${cfg.trials} runs each</p>
    <div class="table-wrap"><table>
      <tr><th>Model</th><th class="num">EGP per 1k successful tasks</th><th class="num">Success rate</th>
          <th class="num">Reliability</th><th class="num">Latency p50</th><th class="num">Total spend</th></tr>
      ${detail.models.map((m) => `<tr><td>${esc(m.model)}</td>
        <td class="num">${m.cost_per_success_egp == null ? "n/a" : (m.cost_per_success_egp * 1000).toFixed(2)}</td>
        <td class="num">${pct(m.success_rate)}</td><td class="num">${pct(m.reliability)}</td>
        <td class="num">${m.latency_p50_s == null ? "n/a" : `${m.latency_p50_s.toFixed(2)}s`}</td>
        <td class="num">${egp(m.total_cost_egp)}</td></tr>`).join("")}
    </table></div>
    <details style="margin-top:24px" open><summary class="hint">Full report (markdown)</summary>
      <pre class="answer">${esc(detail.report_md)}</pre></details>`;
}

async function openAnswers(runName, taskId, model, title) {
  const attempts = await api(`/api/results/${encodeURIComponent(runName)}/attempts?task_id=${encodeURIComponent(taskId)}&model=${encodeURIComponent(model)}`);
  showAnswersPanel(`${taskId} · ${title}`, model, attempts);
}

function showAnswersPanel(title, subtitle, attempts, introHtml = "") {
  attempts.sort((a, b) => a.trial - b.trial || a.attempt - b.attempt);
  const judges = [...new Set(attempts.flatMap((a) => a.criteria.flatMap((c) => Object.keys(c.by_judge))))];
  const retries = attempts.some((a) => a.attempt > 1);

  const backdrop = document.createElement("div");
  backdrop.className = "panel-backdrop";
  backdrop.innerHTML = `
    <div class="panel">
      <button class="secondary close">Close</button>
      <h2 style="margin-top:0">${esc(title)}</h2>
      <p class="lead">${esc(subtitle)}</p>
      ${introHtml}
      ${attempts.map((a) => {
        const status = a.error_kind === "blocked" ? ["muted", "BLOCKED"] : a.passed ? ["good", "PASSED"] : ["bad", "FAILED"];
        return `<div class="card">
          <div style="display:flex;gap:10px;align-items:center;flex-wrap:wrap">
            <strong>${retries ? `Run ${a.trial}, attempt ${a.attempt}` : `Answer ${a.trial}`}${a.model && a.model !== subtitle ? ` · ${esc(a.model)}` : ""}</strong><span class="badge ${status[0]}">${status[1]}</span>
            <span class="hint">${egp(a.cost_egp)} EGP${a.latency_s != null ? ` · ${a.latency_s}s` : ""}</span>
          </div>
          ${a.error ? `<div class="notice ${a.error_kind === "blocked" ? "" : "bad"}">${esc(a.error)}</div>` : ""}
          ${a.output ? `<pre class="answer" dir="auto">${esc(a.output)}</pre>` : ""}
          ${a.criteria.length ? `<div class="table-wrap"><table style="margin-top:10px">
            <tr><th>Check</th><th>Result</th>${judges.map((j) => `<th class="num">${esc(j)}</th>`).join("")}</tr>
            ${a.criteria.map((c) => `<tr><td>${esc(c.criterion)}${c.reason ? `<div class="hint">${esc(c.reason)}</div>` : ""}</td>
              <td><span class="badge ${c.passed ? "good" : "bad"}">${c.passed ? "pass" : "fail"}</span></td>
              ${judges.map((j) => `<td class="num">${c.by_judge[j] == null ? "–" : Number(c.by_judge[j]).toFixed(2)}</td>`).join("")}</tr>`).join("")}
          </table></div>` : ""}
        </div>`;
      }).join("")}
    </div>`;
  const close = () => backdrop.remove();
  backdrop.addEventListener("click", (event) => { if (event.target === backdrop) close(); });
  backdrop.querySelector(".close").addEventListener("click", close);
  document.body.appendChild(backdrop);
  return backdrop;
}
