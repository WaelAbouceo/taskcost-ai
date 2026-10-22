"use strict";

// Where models run: the hosting agent's latest check, with the quotes that prove each location.

const STATUS_TEXT = {
  catalog: ["good", "From SovereignEG's catalog"], verified: ["good", "Verified today"], unchanged: ["good", "Verified, pages unchanged"],
  kept: ["warn", "Kept from an earlier check"], unverified: ["muted", "Could not verify"],
};

async function renderHosting() {
  const h = await api("/api/hosting");
  if (!h.updated_at) {
    app.innerHTML = `<section class="simple"><h1>Where models run</h1>
      <p class="lead">The hosting agent hasn't run yet. It runs daily while the app is up, or now with <code>taskcost hosting</code>.</p></section>`;
    return;
  }
  const providers = Object.entries(h.providers).sort(([a], [b]) => (a === "vllm" ? -1 : b === "vllm" ? 1 : a.localeCompare(b)));
  app.innerHTML = `
    <section class="simple wide">
      <h1>Where models run</h1>
      <p class="lead">Every day the hosting agent reads SovereignEG's model list and each provider's own pages, and keeps a
        location only when it can quote the page that states it. ${esc(h.verified_models)} of ${esc(h.model_count)} models have a
        verified location; ${esc(h.egypt_hosted.length)} run in Egypt.</p>
      <p class="hint">Last checked ${esc(timeAgo(h.updated_at))} by ${esc(h.reader_model)}, for ${egp(h.cost_egp)} EGP.
        ${h.changes?.length ? `Changes: ${h.changes.map(esc).join(" ")}` : ""}</p>
      ${providers.map(([key, p]) => {
        const [cls, status] = STATUS_TEXT[p.status] || ["muted", p.status];
        const facts = p.facts || [];
        return `<div class="card provider">
          <div class="provider-head"><h3>${esc(p.name)}</h3> <span class="badge ${cls}">${esc(status)}</span>
            <span class="hint">runs in ${esc((p.locations || []).join(", ") || "unknown")}${
              p.optional_residency?.length ? ` · optional residency: ${esc(p.optional_residency.join(", "))}` : ""}</span></div>
          ${p.routes_onward ? `<p class="hint">Forwards requests to other companies it doesn't name, so models routed through it can't be verified.</p>` : ""}
          ${p.note ? `<p class="hint">${esc(p.note)}</p>` : ""}
          ${facts.length ? `<ul class="quotes">${facts.map((f) => `<li><strong>${esc(f.location)}</strong>${f.kind === "optional_residency" ? " (optional)" : ""}:
            “${esc(f.quote)}” <a href="${esc(f.url)}" target="_blank" rel="noopener">source</a></li>`).join("")}</ul>`
            : `<p class="hint">No page we could read states where it processes requests.</p>`}
        </div>`;
      }).join("")}
      ${h.egypt_hosted.length ? `<p class="hint">Hosted in Egypt: ${h.egypt_hosted.map(esc).join(", ")}.</p>` : ""}
    </section>`;
}
