# Agent 1 calibration: does Jev pick the right model?

Jev picked the cheapest tier that passes for **1 of 10 tasks**. Too cheap (the pick would fail): 0. Too expensive (works, but costs more): 0. No tier passed every run: 9.

Jev's pick passed every run on 1 of 10 tasks. Following Jev cost 0.0066 EGP per set of answers, against 0.0066 EGP for the best possible picks.

Tiers: Egypt-hosted small = `Qwen/Qwen3-0.6B`. Each tier ran 2 times per task.

| Task | Jev's pick | Why | Cheapest tier that passed | Verdict | Egypt-hosted small |
|---|---|---|---|---|---|
| customer-reply-arabic | **Egypt-hosted small** | medium, reasoning, Arabic dialect; reasoning 1.2 | none | No tier passed every run | 0/2 · 0.0047 |
| ticket-routing | **Egypt-hosted small** | medium, instant, English; reasoning 1.2 | Egypt-hosted small | Right | 2/2 · 0.0066 |
| invoice-extraction | **Egypt-hosted small** | medium, reasoning, English; reasoning 1.9 | none | No tier passed every run | 0/2 · 0.0127 |
| contract-review | **Egypt-hosted small** | large, reasoning, English; reasoning 1.9 | none | No tier passed every run | 0/2 · 0.0087 |
| meeting-actions | **Egypt-hosted small** | medium, instant, English; reasoning 1.3 | none | No tier passed every run | 0/2 · 0.0058 |
| payment-reminder-email | **Egypt-hosted small** | medium, instant, English; reasoning 1.1 | none | No tier passed every run | 1/2 · 0.0060 |
| executive-summary | **Egypt-hosted small** | medium, instant, English; reasoning 1.1 | none | No tier passed every run | 1/2 · 0.0069 |
| sales-data-question | **Egypt-hosted small** | medium, reasoning, English; reasoning 2.0 | none | No tier passed every run | 0/2 · 0.0231 |
| translation-to-arabic | **Egypt-hosted small** | medium, instant, Arabic; reasoning 0.3 | none | No tier passed every run | 0/2 · 0.0051 |
| hr-policy-question | **Egypt-hosted small** | medium, reasoning, English; reasoning 1.7 | none | No tier passed every run | 0/2 · 0.0069 |

Cells show runs passed and EGP per answer. Tiers above the cheapest passing one are not run unless they were Jev's pick.

Costs: Jev 0.0288 EGP for all profiles; worker models 0.1729 EGP; reviewers 0.8939 EGP.
