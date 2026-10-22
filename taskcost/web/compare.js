"use strict";

// The simple experience: describe a task, say how it will be used, get the cheapest model that does it right.

const USE_CASES_SHORT = {
  chat: ["Chatbot", "A person is waiting, so answers must come within 5 seconds."],
  realtime: ["Workflow", "An app waits for the result, so answers must come within 15 seconds."],
  batch: ["Batch", "It runs in the background, so speed doesn't matter."],
};

const EXAMPLES = [
  ["Customer email", "Write an email to a customer about a delayed order"],
  ["Sort tickets", "Sort customer support tickets into billing, delivery, technical and account"],
  ["Meeting notes", "Turn a meeting transcript into action items with owners and deadlines"],
  ["ERP system", "An ERP system for a mid-sized distributor"],
];

const money = (v) => (v == null ? "—" : v >= 100 ? v.toFixed(0) : v >= 1 ? v.toFixed(2) : v.toFixed(3));

function timeAgo(iso) {
  if (!iso) return "";
  const minutes = Math.round((Date.now() - new Date(iso)) / 60000);
  if (minutes < 1) return "just now";
  if (minutes < 60) return `${minutes} min ago`;
  if (minutes < 1440) return `${Math.round(minutes / 60)} h ago`;
  return `${Math.round(minutes / 1440)} days ago`;
}

async function renderHome() {
  const [cfg, runs] = await Promise.all([getConfig(), api("/api/results")]);
  const recent = runs.filter((r) => r.quick || r.kind === "blueprint").slice(0, 5);
  app.innerHTML = `
    <section class="simple">
      <div class="hero-head">
      <h1>Which AI model <span class="accent">should you use?</span></h1>
      <p class="lead">Describe a task, a workflow or a whole system. For a task, we test it on ${cfg.tiers.length} models and
        tell you the cheapest one that does it right, fast enough for how you'll use it (we work that out from your words).
        For a workflow or system, we map how it runs and what the AI part costs per month.
        ${cfg.prefer_open_weights
          ? cfg.prefer_egypt_hosted
            ? "Open-source models hosted in Egypt come first; proprietary ones are the fallback."
            : "Open-source models come first; proprietary ones are the fallback."
          : ""}</p>
      </div>
      ${cfg.has_key ? "" : `<div class="notice bad">LLM_API_KEY is not set, so comparisons can't run.</div>`}
      <form id="quick">
        <textarea name="task" rows="3" required placeholder="e.g. Write an email to a customer about a delayed order"></textarea>
        <div class="examples">Try:
          ${EXAMPLES.map(([label, text]) => `<button type="button" class="link" data-example="${esc(text)}">${esc(label)}</button>`).join(" · ")}
        </div>

        ${helperSwitch(cfg)}

        <button type="submit" class="big-button" ${cfg.has_key ? "" : "disabled"}>Compare</button>
        <div id="quick-progress"></div>

        <details class="options">
          <summary>Options</summary>
          <label class="field">Attach a document <span class="hint">.txt, .md or .pdf</span></label>
          <input type="file" name="files" multiple accept=".txt,.md,.pdf">
          <label class="field">Models <span class="hint">clear a box to skip that model</span></label>
          <div class="tiers">
            ${cfg.tiers.map((t) => `<label class="tier-pick"><span>${esc(t.tier)} ${sourceBadge(t.open_weights, t.egypt_hosted)}</span>
              <input type="text" class="tier-model" data-tier="${esc(t.tier)}" value="${esc(t.model)}" list="catalog"></label>`).join("")}
          </div>
          <datalist id="catalog">${cfg.catalog.map((id) => `<option value="${esc(id)}">`).join("")}</datalist>
          <label class="check"><input type="checkbox" name="reliable"> Test each model 3 times (slower, more reliable)</label>
        </details>
      </form>

      ${recent.length ? `<div class="recent"><h3>Recent</h3>
        ${recent.map((r) => `<a href="${historyLink(r)}"><span>${esc(r.title)}</span>
          <span class="hint">${esc(timeAgo(r.started_at))}</span></a>`).join("")}</div>` : ""}
    </section>`;

  const form = document.getElementById("quick");
  form.querySelectorAll("[data-example]").forEach((b) =>
    b.addEventListener("click", () => { form.task.value = b.dataset.example; form.task.focus(); }));
  const toggle = form.querySelector("input[name=in_egypt]");
  if (toggle) {
    const showMode = () => {
      const mode = toggle.checked ? toggle.dataset.on : toggle.dataset.off;
      localStorage.setItem(HELPERS_KEY, mode);
      document.getElementById("helpers-hint").textContent = cfg.helper_modes.find((m) => m.id === mode).description;
    };
    toggle.addEventListener("change", showMode);
    showMode();
  }

  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const tiers = [...form.querySelectorAll(".tier-model")]
      .map((input) => ({ tier: input.dataset.tier, model: input.value.trim() }))
      .filter((t) => t.model);
    const data = new FormData();
    data.set("task", form.task.value);
    data.set("use_case", "auto");
    data.set("tiers", JSON.stringify(tiers));
    data.set("trials", form.reliable.checked ? "3" : "1");
    data.set("helpers", chosenHelpers(cfg));
    [...form.files.files].forEach((file) => data.append("files", file));
    await startComparison(data, tiers.length, form.querySelector("button[type=submit]"), document.getElementById("quick-progress"));
  });
}

// On: every step that reads the request runs in Egypt. Off: the global helpers.
function helperSwitch(cfg) {
  const modes = cfg.helper_modes || [];
  const egypt = modes.find((m) => m.egypt_hosted);
  const other = modes.find((m) => !m.egypt_hosted);
  if (!egypt || !other) return "";
  return `<label class="switch-row">
      <input type="checkbox" name="in_egypt" role="switch" data-on="${esc(egypt.id)}" data-off="${esc(other.id)}"
        ${chosenHelpers(cfg) === egypt.id ? "checked" : ""}>
      <span class="switch" aria-hidden="true"></span>
      <span><strong>${esc(egypt.label)}</strong><span class="hint" id="helpers-hint"></span></span>
    </label>`;
}

function helperNote(q) {
  if (!q.helpers) return "";
  const where = q.helpers_egypt_hosted ? "in Egypt" : "with the global helpers";
  const used = [...new Set([q.helpers.quick_design_model || q.helpers.design_model, q.helpers.profiler_model].filter(Boolean))];
  return `Your request was analysed ${where} (${esc(used.join(", "))}).`;
}

function historyLink(r) {
  const name = encodeURIComponent(r.name);
  if (r.kind === "blueprint") return `#/blueprint/${name}`;
  return r.quick ? `#/compare/${name}` : `#/results/${name}`;
}

function renderHistory(runs) {
  const label = (r) => r.kind === "blueprint" ? "Project" : r.quick ? "Task" : r.kind === "plan" ? "Big job" : "Benchmark";
  app.innerHTML = `
    <section class="simple">
      <h1>History</h1>
      ${runs.length ? `<div class="recent">${runs.map((r) => `
        <a href="${historyLink(r)}"><span>${esc(r.title || r.name)} <span class="badge muted">${label(r)}</span></span>
          <span class="hint">${esc(timeAgo(r.started_at))}</span></a>`).join("")}</div>`
        : `<p class="lead">Nothing yet. <a href="#/">Compare your first task</a>.</p>`}
    </section>`;
}

async function startComparison(data, modelCount, button, box, endpoint = "/api/start") {
  button.disabled = true;
  try {
    const job = await api(endpoint, { method: "POST", body: data });
    const target = job.kind === "blueprint" ? "#/blueprint/" : "#/compare/";
    if (job.status === "cached") {
      location.hash = target + job.result;
      return;
    }
    const intro = job.kind === "blueprint"
      ? `<p class="hint">This looks like a ${esc(job.size?.level === "workflow" ? "workflow" : "project")}, not a single task, so we're mapping how it usually runs in an enterprise.</p>` : "";
    followJob(job, {
      render: (state) => {
        const step = {
          run: `Testing ${modelCount} models${state.total ? ` (${state.done} of ${state.total} checked)` : ""}…`,
          estimate: "Choosing a model for each AI step and estimating the monthly cost…",
        }[state.phase] || (job.kind === "blueprint" ? "Mapping it to standard enterprise processes…" : "Understanding your task…");
        box.innerHTML = `${intro}<p class="working"><span class="spinner"></span>${esc(step)} <span class="hint">${state.elapsed_s ?? 0}s</span></p>`;
      },
      onDone: (state) => { location.hash = target + state.result; },
      onError: (state) => {
        box.innerHTML = `<div class="notice bad">${esc(state.error)}</div>`;
        button.disabled = false;
      },
    });
  } catch (error) {
    box.innerHTML = `<div class="notice bad">${esc(error.message)}</div>`;
    button.disabled = false;
  }
}

// The blueprint page (workflows and projects) lives in blueprint.js.

function modelStatus(t) {
  if (t.errors && !t.passed) return ["bad", "✗ Error", "The model returned an error."];
  if (t.passed < t.runs) {
    const missed = t.failed_checks[0] ? `Missed: ${t.failed_checks[0]}${t.failed_checks.length > 1 ? ` (+${t.failed_checks.length - 1} more)` : ""}` : "";
    return ["bad", t.runs > 1 ? `✗ Failed ${t.runs - t.passed} of ${t.runs}` : "✗ Failed", missed];
  }
  if (!t.fast_enough) return ["warn", "⏱ Too slow", `1 in 10 answers takes over ${t.slowest_s?.toFixed(1)} s`];
  return ["good", "✓ Passed", ""];
}

async function renderAnswer(name) {
  const q = await api(`/api/quick/${encodeURIComponent(name)}`);
  const best = q.recommended ? q.tiers.find((t) => t.tier === q.recommended.tier && t.model === q.recommended.model) : null;
  const useName = (USE_CASES_SHORT[q.use_case] || [q.use_case_label])[0];

  app.innerHTML = `
    <section class="simple">
      <a href="#/" class="back">← New task</a>
      <p class="asked">“${esc(q.request)}”</p>
      <p class="use-line">${q.use_case_guess ? "We assumed" : "Used as"} <strong>${esc(useName.toLowerCase())}</strong>:
        ${esc((USE_CASES_SHORT[q.use_case] || ["", ""])[1].toLowerCase())}
        <span class="hint">Not right? ${Object.keys(USE_CASES_SHORT).filter((u) => u !== q.use_case)
          .map((u) => `<button type="button" class="link" data-use-case="${esc(u)}">${esc(USE_CASES_SHORT[u][0])}</button>`).join(" · ")}</span></p>
      <div id="use-progress"></div>
      ${q.test_mode ? `<div class="notice">Test mode: simulated models, not real results.</div>` : ""}

      ${best ? `<div class="answer good">
          <div class="label">Use</div>
          <div class="pick">${esc(best.model)} <span class="badge">${esc(best.tier)}</span> ${sourceBadge(best.open_weights, best.egypt_hosted)}</div>
          <div class="facts"><strong>${money(best.cost_per_1000_egp)} EGP</strong> per 1,000 tasks ·
            about ${best.latency_s == null ? "?" : best.latency_s.toFixed(1)} s per answer · passed every check</div>
          ${q.savings_ratio && q.savings_ratio >= 1.5
            ? `<div class="hint">${esc(q.most_expensive.tier)} would cost ${Math.round(q.savings_ratio)}× more for the same result.</div>` : ""}
          ${q.proprietary_fallback ? `<div class="hint">No open-source model both passed and was fast enough for this use, so this is the proprietary fallback.</div>`
            : q.abroad_fallback ? `<div class="hint">No Egypt-hosted model both passed and was fast enough for this use, so this open-source model runs abroad.</div>`
            : q.prefer_egypt_hosted && best.egypt_hosted ? `<div class="hint">Hosted in Egypt: this open-source model did the task right and runs on SovereignEG's GPUs in Egypt.</div>`
            : q.prefer_open_weights && best.open_weights ? `<div class="hint">Open source preferred: this is the cheapest open-source model that did the task right.</div>` : ""}
          ${q.cheapest_correct && q.cheapest_correct.cost_per_1000_egp < best.cost_per_1000_egp
            ? `<div class="hint">Cheapest correct option: ${esc(q.cheapest_correct.model)}${q.cheapest_correct.open_weights && !q.cheapest_correct.egypt_hosted ? " (open source, runs abroad)" : q.cheapest_correct.open_weights ? "" : " (proprietary)"}
               at ${money(q.cheapest_correct.cost_per_1000_egp)} EGP per 1,000 tasks.</div>` : ""}
          ${q.advice?.verdict === "best" ? `<div class="hint">✓ Agent 1 predicted this before testing.</div>` : ""}
        </div>`
        : `<div class="answer bad">
          <div class="label">No clear winner</div>
          <div>No model both did the task right and was fast enough for ${esc(useName.toLowerCase())} use. Tap a model below to see why.</div>
        </div>`}

      <table class="models">
        <tr><th>Model</th><th>Result</th><th class="num">EGP per 1,000 tasks</th></tr>
        ${q.tiers.map((t, i) => {
          const [cls, label, why] = modelStatus(t);
          return `<tr data-answer="${i}" class="${best === t ? "best" : ""}">
            <td><strong>${esc(t.tier)}</strong> ${sourceBadge(t.open_weights, t.egypt_hosted)}<div class="hint">${esc(t.model)}</div></td>
            <td><span class="badge ${cls}">${esc(label)}</span>${why ? `<div class="hint">${esc(why)}</div>` : ""}</td>
            <td class="num">${money(t.cost_per_1000_egp)}</td></tr>`;
        }).join("")}
      </table>
      <p class="hint">Tap a model to read its answer and what the reviewers checked.</p>

      <details class="options">
        <summary>How we tested</summary>
        <p>${esc(q.instruction)}</p>
        ${q.assumptions ? `<p class="hint">Your request was made concrete so every model got the same facts: ${esc(q.assumptions)}</p>` : ""}
        <strong>What the reviewers checked</strong>
        <ul>${q.criteria.map((c) => `<li>${esc(c)}</li>`).join("")}</ul>
        ${q.advice ? `<p class="hint">Agent 1's prediction: ${esc(q.advice.model || "none")}. ${esc(q.advice.note)}</p>` : ""}
        <p class="hint">Each model answered ${q.trials > 1 ? `${q.trials} times` : "once"}; an independent panel of AI reviewers checked every answer.
          Checking cost ${egp(q.judge_cost_egp)} EGP and understanding the task ${egp(q.design_cost_egp)} EGP, not included above.
          Tested ${esc(timeAgo(q.started_at))}. ${helperNote(q)}</p>
        <button type="button" class="secondary" id="run-again">Run again</button>
        <div id="rerun-progress"></div>
      </details>

      <a href="#/"><button type="button" class="big-button">Compare another task</button></a>
    </section>`;

  app.querySelectorAll("tr[data-answer]").forEach((row) => row.addEventListener("click", () => {
    const t = q.tiers[Number(row.dataset.answer)];
    const attempts = t.answers.map((a) => ({ ...a, attempt: 1, error_kind: a.error ? "model" : null }));
    showAnswersPanel(`${t.tier} · ${t.model}`, q.title, attempts);
  }));

  // Same concrete task, so only the use case (or the run) differs.
  const rerun = (useCase, force) => {
    const data = new FormData();
    data.set("task", q.request);
    data.set("tiers", JSON.stringify(q.tiers.filter((t) => t.tier !== "Agent 1 pick").map((t) => ({ tier: t.tier, model: t.model }))));
    data.set("trials", String(q.trials || 1));
    data.set("use_case", useCase);
    data.set("force", force ? "true" : "false");
    data.set("rerun_of", q.name);
    if (q.helper_mode) data.set("helpers", q.helper_mode);
    return data;
  };
  document.getElementById("run-again").addEventListener("click", (event) => {
    startComparison(rerun(q.use_case, true), q.tiers.length, event.target, document.getElementById("rerun-progress"), "/api/quick");
  });
  app.querySelectorAll("[data-use-case]").forEach((button) => button.addEventListener("click", () => {
    startComparison(rerun(button.dataset.useCase, false), q.tiers.length, button, document.getElementById("use-progress"), "/api/quick");
  }));
}
