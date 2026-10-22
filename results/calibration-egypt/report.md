# Agent 1 calibration: does Jev pick the right model?

Jev picked the cheapest tier that passes for **9 of 10 tasks**. Too cheap (the pick would fail): 0. Too expensive (works, but costs more): 0. No tier passed every run: 1.

Jev's pick passed every run on 9 of 10 tasks. Following Jev cost 0.7017 EGP per set of answers, against 0.7017 EGP for the best possible picks.

Tiers: Egypt-hosted small = `Qwen/Qwen3-0.6B`, Egypt-hosted = `SovereignEG/Qwen3.8-27B-FP8`. Each tier ran 2 times per task.

| Task | Jev's pick | Why | Cheapest tier that passed | Verdict | Egypt-hosted small | Egypt-hosted |
|---|---|---|---|---|---|---|
| customer-reply-arabic | **Egypt-hosted** | medium, reasoning, Arabic dialect; reasoning 1.2 | Egypt-hosted | Right | 0/2 · 0.0000 | 2/2 · 0.0526 |
| ticket-routing | **Egypt-hosted** | medium, instant, English; reasoning 1.2 | Egypt-hosted | Right | 0/2 · 0.0000 | 2/2 · 0.0529 |
| invoice-extraction | **Egypt-hosted** | medium, reasoning, English; reasoning 1.9 | Egypt-hosted | Right | 0/2 · 0.0000 | 2/2 · 0.0502 |
| contract-review | **Egypt-hosted** | large, reasoning, English; reasoning 1.9 | Egypt-hosted | Right | 0/2 · 0.0000 | 2/2 · 0.1725 |
| meeting-actions | **Egypt-hosted** | medium, instant, English; reasoning 1.3 | Egypt-hosted | Right | 0/2 · 0.0000 | 2/2 · 0.0316 |
| payment-reminder-email | **Egypt-hosted** | medium, instant, English; reasoning 1.2 | Egypt-hosted | Right | 0/2 · 0.0000 | 2/2 · 0.1475 |
| executive-summary | **Egypt-hosted** | medium, instant, English; reasoning 1.1 | Egypt-hosted | Right | 0/2 · 0.0000 | 2/2 · 0.0440 |
| sales-data-question | **Egypt-hosted** | medium, reasoning, English; reasoning 2.0 | Egypt-hosted | Right | 0/2 · 0.0000 | 2/2 · 0.0887 |
| translation-to-arabic | **Egypt-hosted** | medium, instant, Arabic; reasoning 0.3 | none | No tier passed every run | 0/2 · 0.0000 | 1/2 · 0.1089 |
| hr-policy-question | **Egypt-hosted** | medium, reasoning, English; reasoning 1.6 | Egypt-hosted | Right | 0/2 · 0.0000 | 2/2 · 0.0616 |

Cells show runs passed and EGP per answer. Tiers above the cheapest passing one are not run unless they were Jev's pick.

Costs: Jev 0.0288 EGP for all profiles; worker models 1.6211 EGP; reviewers 0.8778 EGP.
