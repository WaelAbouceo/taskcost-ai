"use strict";

// Workflows and projects: how the work runs, who does each step, and what the AI part costs per month.

const perMonth = (v) => (v == null ? "—" : v >= 100 ? Math.round(v).toLocaleString() : v >= 1 ? v.toFixed(1) : v.toFixed(2));
// Measuring a 16-step plan cost 17.9 EGP (example writing, models and reviewers), so about 1.1 EGP per step.
const MEASURE_EGP_PER_STEP = 1.1;
const WHO = { ai: ["good", "AI"], ml: ["warn", "ML model"], person: ["muted", "Person"], system: ["muted", "System"] };
const UNRESOLVED = {
  error: ["bad", "Couldn't be measured", "measuring this step failed; re-measure it, or use your own example"],
  needs_frontier: ["warn", "Needs Frontier", "no cheaper model passed; Frontier wasn't tried, to keep costs down"],
  no_reliable_test: ["muted", "No reliable test", "the test itself failed its own checks, so it wasn't used"],
  no_model_passed: ["bad", "No model passed", "every fast-enough model failed"],
};

// Older examples stored structured answers as Python-style text; show them as readable JSON when possible.
function prettyAnswer(text) {
  try {
    return JSON.stringify(JSON.parse(text.replace(/'/g, '"').replace(/\bNone\b/g, "null")), null, 2);
  } catch {
    return text;
  }
}

function perAnswer(step) {
  return step.measured ? step.measured.cost_per_answer_egp : step.cost_per_answer_egp;
}

let openWeights = {};
let residency = {};
let policy = null;
const placeBadge = (model) => (!policy || policy.level === "open" || !residency[model] ? ""
  : residencyBadge(residency[model].fit, residency[model].locations, policy.country_name));

function modelCell(s) {
  const tried = s.tried?.length ? `<div class="hint">tried ${esc(s.tried.map((t) => `${t.model} ${t.passed}/${t.runs}`).join(", "))}</div>` : "";
  if (s.measured) {
    const m = s.measured;
    return `${esc(m.model)} <span class="badge good">Measured</span> ${sourceBadge(openWeights[m.model])} ${placeBadge(m.model)}
      <div class="hint">${esc(m.tier)}${m.latency_s == null ? "" : ` · ${m.latency_s.toFixed(1)} s`}${s.sample?.source === "user" ? " · your example" : ""}</div>
      ${s.dropped_checks?.length ? `<div class="hint" title="${esc(s.dropped_checks.join("; "))}">One check was dropped: every model failed it, so the check was too strict.</div>` : ""}`;
  }
  if (UNRESOLVED[s.verdict]) {
    const [cls, label, why] = UNRESOLVED[s.verdict];
    return `<span class="badge ${cls}">${label}</span><div class="hint">${why}</div>${tried}`;
  }
  return `${esc(s.model || "none fast enough")} <span class="badge muted">Estimate</span> ${s.model ? sourceBadge(openWeights[s.model]) + ' ' + placeBadge(s.model) : ""}${s.model_tier ? `<div class="hint">${esc(s.model_tier)}</div>` : ""}`;
}

function watchJob(job, box, fallbackTotal, onDone, onError) {
  followJob(job, {
    render: (state) => {
      const last = (state.lines || []).slice(-1)[0] || "";
      box.innerHTML = `<p class="working"><span class="spinner"></span>Measuring ${state.done || 0} of ${state.total || fallbackTotal} step(s)…
        <span class="hint">${state.elapsed_s ?? 0}s</span></p><p class="hint">${esc(last.replace(/^\[\d+\/\d+\]\s*/, ""))}</p>`;
    },
    onDone: () => onDone(),
    onError: (state) => {
      box.innerHTML = `<div class="notice bad">${esc(state.error)}</div>`;
      onError?.();
    },
  });
}

async function startMeasuring(name, stepIds, button, box, total, onDone) {
  button.disabled = true;
  try {
    const query = stepIds?.length ? `?steps=${encodeURIComponent(stepIds.join(","))}` : "";
    const job = await api(`/api/blueprints/${encodeURIComponent(name)}/verify${query}`, { method: "POST" });
    watchJob(job, box, total, onDone, () => { button.disabled = false; });
  } catch (error) {
    box.innerHTML = `<div class="notice bad">${esc(error.message)}</div>`;
    button.disabled = false;
  }
}

async function renderBlueprint(name) {
  const b = await api(`/api/blueprints/${encodeURIComponent(name)}`);
  openWeights = b.open_weights || {};
  residency = b.residency || {};
  policy = b.sovereignty || null;
  const registry = b.estimate?.registry || {};
  const strongest = registry[b.strongest_model];
  const strongestPer = strongest ? strongest.cost_per_answer_egp / Math.max(strongest.pass_rate, 0.5) : 0;
  const aiSteps = b.workflows.flatMap((w) => w.steps.filter((s) => s.actor === "ai"));
  const mlSteps = b.workflows.flatMap((w) => w.steps.filter((s) => s.actor === "ml"));
  const reviewed = aiSteps.filter((s) => s.review === "person");
  const isBuild = (s) => s.phase === "build";
  const hasBuild = aiSteps.some(isBuild);
  const stepVolume = (s) => (isBuild(s) ? s.total_volume : s.monthly_volume) || 0;
  const v = b.verification;
  const spent = v ? Object.values(v.cost_egp).reduce((a, c) => a + c, 0) : 0;
  const unresolved = v ? aiSteps.filter((s) => UNRESOLVED[s.verdict]) : [];

  app.innerHTML = `
    <section class="simple wide">
      <a href="#/" class="back">← New task</a>
      <p class="asked">“${esc(b.request)}”</p>
      <div class="answer ${v ? "good" : "estimate"}">
        <div class="label">This is a ${b.level === "workflow" ? "workflow" : "project"}, not a single task</div>
        <div class="pick">${esc(b.title)}</div>
        <div class="facts">${hasBuild
          ? `About <strong><span id="bp-setup"></span> EGP to set up</strong>, then <strong><span id="bp-total"></span> EGP per month</strong>
             to run · first year about <span id="bp-year"></span> EGP`
          : `About <strong><span id="bp-total"></span> EGP per month</strong> for the AI work`} ·
          ${aiSteps.length} AI steps in ${b.workflows.length} workflow${b.workflows.length === 1 ? "" : "s"}</div>
        ${hasBuild ? `<div class="hint">Setup is one-time work (${aiSteps.filter(isBuild).length} steps, such as migrating old records or
          preparing a knowledge base); running is the work that repeats every month.</div>` : ""}
        ${v ? `<div class="hint"><strong>Measured</strong> on a realistic example for ${v.measured_steps} of ${v.ai_steps} AI steps,
              cheapest model first. Measuring has cost ${egp(spent)} EGP so far.</div>`
            : `<div class="hint"><strong>Estimate only.</strong> Volumes are assumed and no step has been tested yet; real
                outputs are often longer than the estimate assumes, so measured costs can be several times higher.
                ${b.measuring ? "Measuring the costliest steps now…" : `<button type="button" class="link" id="measure-top">Measure it for real (about ${Math.round(aiSteps.length * MEASURE_EGP_PER_STEP)} EGP) →</button>`}</div>`}
        <div class="hint">Using ${esc(b.strongest_model)} for every AI step would cost about <span id="bp-strongest"></span>.</div>
      </div>

      ${b.key_assumptions?.length ? `<div class="card key-assumptions"><strong>What drives this cost</strong>
          <ul>${b.key_assumptions.map((a) => `<li>${esc(a)}</li>`).join("")}</ul>
          <p class="hint">Assumed from your request. If yours differ, change the volumes in the tables below and the cost updates.</p></div>` : ""}

      ${mlSteps.length || reviewed.length ? `<div class="card nudge">
          ${mlSteps.length ? `<p><strong>${mlSteps.length} step${mlSteps.length === 1 ? " needs" : "s need"} a specialised model, not a language model</strong>
            (${esc([...new Set(mlSteps.map((s) => s.ml_kind))].join(", "))}). They are not priced here: they need their own
            data and training, usually by a data-science team.</p>` : ""}
          ${reviewed.length ? `<p><strong>${reviewed.length} AI step${reviewed.length === 1 ? "" : "s"} should be checked by a person</strong>
            before use, because a wrong result could harm people, the environment, money or compliance. Agent 1 picked
            the model with the best record on hard tasks for them.</p>` : ""}
        </div>` : ""}

      ${b.measuring ? `<div class="card measure" id="measuring-card">
          <strong>${b.measuring.auto ? `Measuring the ${b.measuring.steps.length} costliest steps for real` : "Measuring"}</strong>
          <p class="hint">We write a realistic example for each, check the test itself, then try the cheapest models first.
            The plan is usable meanwhile; the numbers update when it finishes.</p>
          <div id="measuring-progress"></div>
        </div>` : ""}

      ${v || b.measuring ? "" : `<div class="card measure">
          <strong>Make it real</strong>
          <p class="hint">For each AI step we write a realistic example (a full invoice, a customer email…), check that the
            test itself is sound, then try the cheapest models first. About ${Math.max(3, Math.round(aiSteps.length / 2))} minutes;
            usually about ${Math.round(aiSteps.length * MEASURE_EGP_PER_STEP)} EGP, at most about ${Math.round(aiSteps.length * 2)} EGP.</p>
          <button type="button" id="measure">Measure it for real</button>
          <div id="measure-progress"></div>
        </div>`}

      ${unresolved.length ? `<div class="card unresolved">
          <strong>${unresolved.length} step${unresolved.length === 1 ? " isn't" : "s aren't"} measured yet</strong>
          <ul>${unresolved.map((s) => `<li>${esc(s.name)} <span class="badge ${UNRESOLVED[s.verdict][0]}">${UNRESOLVED[s.verdict][1]}</span></li>`).join("")}</ul>
          <p class="hint">Their cost below is the estimate. Re-measure with a new example, or open a step to use your own example.</p>
          <button type="button" class="secondary" id="remeasure-unresolved">Re-measure ${unresolved.length === 1 ? "it" : "them"}</button>
          <div id="unresolved-progress"></div>
        </div>` : ""}

      <div class="card">
        <strong>Where the money goes</strong>
        <div id="bp-breakdown"></div>
        <p class="hint" id="bp-insight"></p>
      </div>

      <p>${esc(b.summary)}</p>
      ${renderTrace(b.trace)}

      <details class="options">
        <summary>What we assumed</summary>
        <ul>${b.assumptions.map((a) => `<li><strong>${esc(a.topic)}:</strong> ${esc(a.assumption)}</li>`).join("")}</ul>
        <p class="hint">Volumes are assumptions: change them in the tables below to match your company.</p>
      </details>

      ${b.workflows.map((w) => `
        <h2>${esc(w.name)}</h2>
        <p class="hint">${esc(w.description)}</p>
        <div class="table-wrap"><table class="models blueprint">
          <tr><th>Step</th><th>Who</th><th>Model</th><th class="num">How many</th><th class="num">EGP</th></tr>
          ${w.steps.map((s) => {
            const [cls, who] = WHO[s.actor] || WHO.person;
            if (s.actor === "ml") {
              return `<tr class="manual"><td>${esc(s.name)}<div class="hint">Needs ${esc(s.ml_kind || "a specialised model")}</div></td>
                <td><span class="badge ${cls}">${who}</span></td><td class="hint">Not a language model</td><td></td><td class="num hint">not priced</td></tr>`;
            }
            if (s.actor !== "ai") {
              return `<tr class="manual"><td>${esc(s.name)}</td><td><span class="badge ${cls}">${who}</span></td><td></td><td></td><td></td></tr>`;
            }
            return `<tr class="clickable" data-step-row="${esc(s.id)}">
              <td>${esc(s.name)}<div class="hint">${isBuild(s) ? "Setup, once" : "Running"} · ${esc((USE_CASES_SHORT[s.use_case] || [s.use_case])[0])} · ${esc(s.task_type)}</div></td>
              <td><span class="badge ${cls}">${who}</span>${s.review === "person" ? ` <span class="badge warn" title="A wrong result could harm people, money or compliance">A person reviews</span>` : ""}</td>
              <td>${modelCell(s)}</td>
              <td class="num"><input type="number" min="0" class="volume" data-step="${esc(s.id)}" value="${stepVolume(s)}">
                <div class="hint">${isBuild(s) ? "in total" : "a month"}</div></td>
              <td class="num" data-cost="${esc(s.id)}"></td></tr>`;
          }).join("")}
        </table></div>`).join("")}
      <p class="hint">Tap an AI step to see the example it was tested on, re-measure it, or measure it on your own example.</p>

      <div class="savebar" id="savebar" hidden>
        <span>You changed some volumes.</span>
        <button type="button" id="save-volumes">Save volumes</button>
      </div>

      <p class="hint">Measured costs come from realistic examples, cheapest model first; estimates use each model's average cost.
        Long real documents cost more than short examples. Costs cover AI model use only, not integration or hosting.
        Mapping this project cost ${egp(b.design?.cost_egp)} EGP.
        ${b.helpers ? `Your request was analysed ${b.helpers.local ? `in ${esc(policy?.country_name || "your country")}` : "outside your country"} (${esc(b.helpers.models.join(", "))}); re-measuring uses the same helpers.` : ""}
        ${policy ? `Sovereignty: ${esc(policy.level_label)} · ${esc(policy.country_name)}. ${(policy.notes || []).map(esc).join(" ")}` : ""}</p>
      <a href="#/"><button type="button" class="big-button">Compare another task</button></a>
    </section>`;

  const volumeOf = (s) => Number(app.querySelector(`input[data-step="${s.id}"]`).value) || 0;
  const update = () => {
    // A step's cost: once for setup steps, per month for running steps. "Year" puts both on one scale.
    const costs = aiSteps.map((s) => {
      const cost = perAnswer(s) == null ? null : perAnswer(s) * volumeOf(s);
      return { step: s, cost, year: cost == null ? null : isBuild(s) ? cost : cost * 12 };
    });
    costs.forEach(({ step, cost }) => {
      app.querySelector(`[data-cost="${step.id}"]`).textContent = cost == null ? "—" : `${perMonth(cost)} ${isBuild(step) ? "once" : "/ month"}`;
    });
    const monthly = costs.filter((c) => !isBuild(c.step)).reduce((sum, c) => sum + (c.cost || 0), 0);
    const setup = costs.filter((c) => isBuild(c.step)).reduce((sum, c) => sum + (c.cost || 0), 0);
    document.getElementById("bp-total").textContent = perMonth(monthly);
    if (hasBuild) {
      document.getElementById("bp-setup").textContent = perMonth(setup);
      document.getElementById("bp-year").textContent = perMonth(setup + 12 * monthly);
    }
    const strongMonthly = aiSteps.filter((s) => !isBuild(s)).reduce((sum, s) => sum + strongestPer * volumeOf(s), 0);
    const strongSetup = aiSteps.filter(isBuild).reduce((sum, s) => sum + strongestPer * volumeOf(s), 0);
    document.getElementById("bp-strongest").textContent = hasBuild
      ? `${perMonth(strongSetup)} EGP to set up and ${perMonth(strongMonthly)} EGP per month` : `${perMonth(strongMonthly)} EGP per month`;

    const total = costs.reduce((sum, c) => sum + (c.year || 0), 0);
    const top = costs.filter((c) => c.year).sort((a, c) => c.year - a.year).slice(0, 3);
    document.getElementById("bp-breakdown").innerHTML = top.map(({ step, cost, year }) => `
      <div class="bar-row"><span>${esc(step.name)}</span>
        <div class="bar-track"><div class="bar-fill" style="width:${total ? Math.max((year / total) * 100, 1) : 0}%"></div></div>
        <span class="num">${perMonth(cost)} EGP ${isBuild(step) ? "once" : "/ mo"}</span></div>`).join("")
      + (hasBuild ? `<p class="hint">Ranked by first-year cost: setup once plus 12 months of running.</p>` : "");
    const chatShare = total ? costs.filter((c) => c.step.use_case === "chat").reduce((sum, c) => sum + (c.year || 0), 0) / total : 0;
    const topShare = total && top[0] ? top[0].year / total : 0;
    document.getElementById("bp-insight").textContent = chatShare >= 0.5
      ? `Chatbot steps make up ${Math.round(chatShare * 100)}% of the cost: a person is waiting, so they need a fast model, and the cheapest model is too slow for them.`
      : topShare >= 0.5
        ? `"${top[0].step.name}" alone is ${Math.round(topShare * 100)}% of the cost, so its volume and model matter most.`
        : `The top ${top.length} steps make up ${total ? Math.round((top.reduce((sum, c) => sum + c.year, 0) / total) * 100) : 0}% of the cost.`;
  };
  update();

  const savebar = document.getElementById("savebar");
  app.querySelectorAll("input.volume").forEach((input) => input.addEventListener("input", () => { update(); savebar.hidden = false; }));
  document.getElementById("save-volumes").addEventListener("click", async (event) => {
    event.target.disabled = true;
    const volumes = Object.fromEntries(aiSteps.map((s) => [s.id, volumeOf(s)]));
    await api(`/api/blueprints/${encodeURIComponent(name)}/volumes?client=${encodeURIComponent(clientId())}`, {
      method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify(volumes),
    });
    renderBlueprint(name);
  });

  app.querySelectorAll("tr[data-step-row]").forEach((row) => row.addEventListener("click", (event) => {
    if (!event.target.closest("input")) openStep(name, row.dataset.stepRow);
  }));

  if (b.measuring) {
    const box = document.getElementById("measuring-progress");
    watchJob({ id: b.measuring.job }, box, b.measuring.steps.length || aiSteps.length, () => renderBlueprint(name), () => {
      box.insertAdjacentHTML("beforeend", `<button type="button" class="secondary" id="measure-again">Measure again</button>`);
      document.getElementById("measure-again").addEventListener("click", (event) =>
        startMeasuring(name, b.measuring.steps, event.target, box, b.measuring.steps.length || aiSteps.length, () => renderBlueprint(name)));
    });
  }
  const measure = document.getElementById("measure");
  if (measure) measure.addEventListener("click", () =>
    startMeasuring(name, null, measure, document.getElementById("measure-progress"), aiSteps.length, () => renderBlueprint(name)));
  document.getElementById("measure-top")?.addEventListener("click", () => {
    if (!measure) return;
    measure.scrollIntoView({ behavior: "smooth", block: "center" });
    measure.click();
  });
  const again = document.getElementById("remeasure-unresolved");
  if (again) again.addEventListener("click", () =>
    startMeasuring(name, unresolved.map((s) => s.id), again, document.getElementById("unresolved-progress"),
      unresolved.length, () => renderBlueprint(name)));
}

async function openStep(name, stepId) {
  const { step, attempts } = await api(`/api/blueprints/${encodeURIComponent(name)}/steps/${encodeURIComponent(stepId)}`);
  const sample = step.sample;
  const check = step.sample_check;
  const checkText = !check ? "" : check.passed
    ? `<p class="hint">✓ The test was checked first: its correct answer passes every check, uses only facts in the example,
        and a differently written correct answer passes too.${check.dropped?.length ? ` ${check.dropped.length} unsound check(s) were dropped.` : ""}</p>`
    : `<p class="hint">✗ The test failed its own check, so it wasn't used: ${esc(check.failed.join("; "))}</p>`;

  const intro = `<div class="card step-panel">
      <div>${modelCell(step)}</div>
      ${sample ? `
        <strong>The example we tested</strong> <span class="hint">${sample.source === "user" ? "(your example)" : "(written for this test)"}</span>
        <pre class="answer" dir="auto">${esc(sample.input)}</pre>
        ${sample.expected_answer ? `<strong>Correct answer</strong><pre class="answer" dir="auto">${esc(prettyAnswer(sample.expected_answer))}</pre>` : ""}
        ${checkText}
        ${step.tried?.length ? `<p class="hint">Models tried, cheapest first: ${step.tried.map((t) =>
          `${esc(t.model)} ${t.passed}/${t.runs}${t.cost_per_answer_egp == null ? "" : ` (${egp(t.cost_per_answer_egp)} EGP per answer)`}`).join(" → ")}</p>` : ""}`
        : `<p class="hint">This step hasn't been measured yet.</p>`}
      <div class="actions">
        <button type="button" class="secondary" data-act="remeasure">${sample ? "Re-measure with a new example" : "Measure this step"}</button>
      </div>
      <details class="own">
        <summary>Use my own example</summary>
        <p class="hint">Paste a real example (an invoice, an email, a report…) or upload a .txt, .md or .pdf file.
          We write the correct answer and checks for it, test the test, then measure the models on it.</p>
        <textarea rows="6" data-own-text placeholder="Paste your example here"></textarea>
        <input type="file" data-own-file accept=".txt,.md,.pdf">
        <div class="actions"><button type="button" data-act="own">Measure on my example</button></div>
      </details>
      <div data-step-progress></div>
    </div>`;

  const panel = showAnswersPanel(step.name, step.instruction, attempts, intro);
  const box = panel.querySelector("[data-step-progress]");
  const finish = () => { panel.remove(); renderBlueprint(name); };

  panel.querySelector('[data-act="remeasure"]').addEventListener("click", (event) =>
    startMeasuring(name, [step.id], event.target, box, 1, finish));

  panel.querySelector('[data-act="own"]').addEventListener("click", async (event) => {
    const data = new FormData();
    data.set("text", panel.querySelector("[data-own-text]").value);
    const file = panel.querySelector("[data-own-file]").files[0];
    if (file) data.append("files", file);
    event.target.disabled = true;
    try {
      const job = await api(`/api/blueprints/${encodeURIComponent(name)}/steps/${encodeURIComponent(step.id)}/example`, {
        method: "POST", body: data,
      });
      watchJob(job, box, 1, finish, () => { event.target.disabled = false; });
    } catch (error) {
      box.innerHTML = `<div class="notice bad">${esc(error.message)}</div>`;
      event.target.disabled = false;
    }
  });
}
