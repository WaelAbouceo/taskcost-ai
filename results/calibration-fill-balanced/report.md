# Agent 1 calibration: does Jev pick the right model?

Jev picked the cheapest tier that passes for **2 of 3 tasks**. Too cheap (the pick would fail): 0. Too expensive (works, but costs more): 0. No tier passed every run: 1.

Jev's pick passed every run on 2 of 3 tasks. Following Jev cost 0.1934 EGP per set of answers, against 0.1934 EGP for the best possible picks.

Tiers: Balanced = `gpt-5.4-mini`. Each tier ran 3 times per task.

| Task | Jev's pick | Why | Cheapest tier that passed | Verdict | Balanced |
|---|---|---|---|---|---|
| invoice-extraction | **Balanced** | medium, reasoning, English; reasoning 1.9 | Balanced | Right | 3/3 · 0.0481 |
| sales-data-question | **Balanced** | medium, reasoning, English; reasoning 2.0 | Balanced | Right | 3/3 · 0.1453 |
| hr-policy-question | **Balanced** | medium, reasoning, English; reasoning 1.6 | none | No tier passed every run | 2/3 · 0.0471 |

Cells show runs passed and EGP per answer. Tiers above the cheapest passing one are not run unless they were Jev's pick.

Costs: Jev 0.0087 EGP for all profiles; worker models 0.7214 EGP; reviewers 0.3875 EGP.
