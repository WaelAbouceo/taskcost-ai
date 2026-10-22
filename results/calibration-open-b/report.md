# Agent 1 calibration: does Jev pick the right model?

Jev picked the cheapest tier that passes for **9 of 10 tasks**. Too cheap (the pick would fail): 0. Too expensive (works, but costs more): 0. No tier passed every run: 1.

Jev's pick passed every run on 9 of 10 tasks. Following Jev cost 0.1425 EGP per set of answers, against 0.1425 EGP for the best possible picks.

Tiers: Balanced = `nvidia-nemotron-3-super-120b-a12b`. Each tier ran 2 times per task.

| Task | Jev's pick | Why | Cheapest tier that passed | Verdict | Balanced |
|---|---|---|---|---|---|
| customer-reply-arabic | **Balanced** | medium, reasoning, Arabic dialect; reasoning 1.2 | Balanced | Right | 2/2 · 0.0091 |
| ticket-routing | **Balanced** | medium, instant, English; reasoning 1.2 | Balanced | Right | 2/2 · 0.0081 |
| invoice-extraction | **Balanced** | medium, reasoning, English; reasoning 1.9 | Balanced | Right | 2/2 · 0.0181 |
| contract-review | **Balanced** | large, reasoning, English; reasoning 1.9 | none | No tier passed every run | 1/2 · 0.0362 |
| meeting-actions | **Balanced** | medium, instant, English; reasoning 1.3 | Balanced | Right | 2/2 · 0.0095 |
| payment-reminder-email | **Balanced** | medium, instant, English; reasoning 1.2 | Balanced | Right | 2/2 · 0.0237 |
| executive-summary | **Balanced** | medium, instant, English; reasoning 1.0 | Balanced | Right | 2/2 · 0.0193 |
| sales-data-question | **Balanced** | medium, reasoning, English; reasoning 2.0 | Balanced | Right | 2/2 · 0.0249 |
| translation-to-arabic | **Balanced** | medium, instant, Arabic; reasoning 0.3 | Balanced | Right | 2/2 · 0.0089 |
| hr-policy-question | **Balanced** | medium, reasoning, English; reasoning 1.6 | Balanced | Right | 2/2 · 0.0210 |

Cells show runs passed and EGP per answer. Tiers above the cheapest passing one are not run unless they were Jev's pick.

Costs: Jev 0.0288 EGP for all profiles; worker models 0.3574 EGP; reviewers 0.8389 EGP.
