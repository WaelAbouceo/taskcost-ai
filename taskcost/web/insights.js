"use strict";

// Insights: the log reviewer's recommendations, each shown with the real usage facts it rests on.

const AREA_LABELS = {
  models: "Models", agent1: "Agent 1", user_experience: "Experience", sovereignty: "Sovereignty", cost: "Cost",
  speed: "Speed", reliability: "Reliability", task_library: "Task library",
};

// Rates (0..1) as percentages; counts and EGP as numbers; small maps as "a 3 · b 2".
function metricValue(key, value) {
  if (value && typeof value === "object") {
    return Array.isArray(value) ? value.map(esc).join(", ")
      : Object.entries(value).map(([k, v]) => `${esc(k)} ${esc(v)}`).join(" · ");
  }
  if (typeof value === "number" && /(rate|share)/.test(key)) return `${Math.round(value * 100)}%`;
  if (typeof value === "number" && key.endsWith("_egp")) return `${value.toFixed(2)} EGP`;
  if (typeof value === "number" && key.endsWith("_s")) return `${value.toFixed(0)} s`;
  return esc(value);
}

const KEY_FACTS = [
  ["activity.comparisons", "Comparisons"], ["activity.blueprints", "Plans"], ["agent1.best_rate", "Agent 1 right"],
  ["use_case.share_chat", "Chatbot tasks"], ["outcome.proprietary_fallback_rate", "Proprietary fallback"],
  ["cost.avg_per_comparison_egp", "Cost per comparison"], ["time.median_comparison_s", "Typical wait"],
  ["sovereignty.strict_refusals", "Strict refusals"],
];

const EFFECT_LABELS = {
  "outcome.chat_proprietary_rate": "Chatbot tasks ending on a proprietary model",
  "outcome.proprietary_fallback_rate": "All tasks ending on a proprietary model",
  "outcome.no_winner_rate": "No model qualified",
  "agent1.best_rate": "Agent 1 right",
  "cost.avg_per_comparison_egp": "Cost per comparison",
  "time.median_comparison_s": "Typical wait",
  "use_case.override_rate": "Use case corrected by users",
  "blueprint.measured_share": "Plans measured",
  "errors.failed_jobs": "Failed jobs",
};

// Each recent change, and the key facts before and after it (computed by code, not the reviewer).
function changesTable(changes) {
  if (!changes?.length) return "";
  return `<h2>What changed, and did it work?</h2>
    <div class="table-wrap"><table>
      <tr><th>Change</th><th>Before → after</th></tr>
      ${changes.map((c) => {
        // Only facts with enough usage since the change (5 comparisons, or 3 plans for plan facts).
        const enough = (key) => key.startsWith("blueprint.") ? (c.blueprints_after || 0) >= 3
          : key.startsWith("errors.") || c.comparisons_after >= 5;
        const rows = Object.entries(c.metrics).filter(([key, v]) => v.after != null && enough(key));
        const plural = (n, word) => `${n} ${word}${n === 1 ? "" : "s"}`;
        const since = c.comparisons_after + (c.blueprints_after || 0);
        return `<tr><td><strong>${esc(c.title)}</strong><div class="hint">${esc(timeAgo(c.at))} ·
            ${plural(c.comparisons_after, "comparison")} and ${plural(c.blueprints_after || 0, "plan")} since${since < 5 ? " (too few to tell yet)" : ""}</div></td>
          <td>${rows.length ? rows.map(([key, v]) => `<div>${esc(EFFECT_LABELS[key] || key)}:
            ${v.before == null ? "—" : metricValue(key, v.before)} → <strong>${metricValue(key, v.after)}</strong></div>`).join("")
            : `<span class="hint">Too few comparisons or plans since this change to tell yet.</span>`}</td></tr>`;
      }).join("")}
    </table></div>`;
}

async function renderInsights() {
  const { latest: r, history } = await api("/api/insights");
  const facts = r ? KEY_FACTS.filter(([key]) => r.metrics[key] != null) : [];
  app.innerHTML = `
    <section class="simple wide">
      <h1>Insights</h1>
      <p class="lead">Every day the log reviewer reads how the tool was used and recommends improvements. Each one cites the
        usage facts it rests on, shown next to it; recommendations that cite facts that don't exist are dropped.</p>
      <div class="actions" style="margin-top:0"><button type="button" class="secondary" id="run-review">Review now</button>
        <span class="hint">${r ? `Last review ${esc(timeAgo(r.created_at))} · last ${esc(r.days)} days · ${esc(r.events)} events ·
          ${esc(r.reviewer_model)} · ${egp(r.cost_egp)} EGP` : "No review yet."}</span></div>
      <div id="review-progress"></div>
      ${r ? `
        ${r.summary ? `<div class="card insight-summary"><p>${esc(r.summary)}</p></div>` : ""}
        ${facts.length ? `<div class="facts-grid">${facts.map(([key, label]) => `<div class="card stat">
          <div class="label">${esc(label)}</div><div class="value">${metricValue(key, r.metrics[key])}</div></div>`).join("")}</div>` : ""}
        ${changesTable(r.changes)}
        <h2>Recommendations</h2>
        ${r.recommendations.length ? r.recommendations.map((rec, i) => `<div class="card recommendation">
            <div class="provider-head"><h3>${i + 1}. ${esc(rec.title)}</h3>
              <span class="badge ${rec.priority === "high" ? "bad" : rec.priority === "medium" ? "warn" : "muted"}">${esc(rec.priority)}</span>
              <span class="badge muted">${esc(AREA_LABELS[rec.area] || rec.area)}</span></div>
            <p>${esc(rec.why)}</p>
            <p><strong>Do:</strong> ${esc(rec.action)}</p>
            <div class="evidence">${Object.entries(rec.evidence).map(([key, value]) =>
              `<span class="chip" title="${esc(key)}">${esc(key)} = <strong>${metricValue(key, value)}</strong></span>`).join("")}</div>
          </div>`).join("")
          : `<p class="hint">${esc(r.summary || "No recommendations.")}</p>`}
        ${r.dropped ? `<p class="hint">${esc(r.dropped)} recommendation(s) dropped because they cited facts or numbers the usage log doesn't support.</p>` : ""}
        ${r.signals.length ? `<h2>Signals</h2><ul class="quotes">${r.signals.map((s) =>
          `<li><strong>${esc(s.meaning)}</strong>: ${esc(s.metric)} = ${metricValue(s.metric, s.value)}</li>`).join("")}</ul>` : ""}
        <details class="options"><summary>All usage facts</summary>
          <table><tr><th>Fact</th><th>Value</th></tr>${Object.entries(r.metrics).map(([key, value]) =>
            `<tr><td>${esc(key)}</td><td>${metricValue(key, value)}</td></tr>`).join("")}</table>
        </details>
        ${history.length > 1 ? `<p class="hint">Earlier reviews: ${history.slice(1).map((h) =>
          `${esc(timeAgo(h.created_at))} (${esc(h.recommendations)})`).join(" · ")}</p>` : ""}` : ""}
    </section>`;

  document.getElementById("run-review").addEventListener("click", async (event) => {
    const button = event.target;
    const box = document.getElementById("review-progress");
    button.disabled = true;
    try {
      const job = await api("/api/insights/run", { method: "POST" });
      followJob(job, {
        render: (state) => { box.innerHTML = `<p class="working"><span class="spinner"></span>Reviewing the usage log… <span class="hint">${state.elapsed_s ?? 0}s</span></p>`; },
        onDone: () => renderInsights(),
        onError: (state) => { box.innerHTML = `<div class="notice bad">${esc(state.error)}</div>`; button.disabled = false; },
      });
    } catch (error) {
      box.innerHTML = `<div class="notice bad">${esc(error.message)}</div>`;
      button.disabled = false;
    }
  });
}
