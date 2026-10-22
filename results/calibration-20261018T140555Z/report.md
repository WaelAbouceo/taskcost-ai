# Agent 1 calibration: does Jev pick the right model?

Jev picked the cheapest tier that passes for **0 of 10 tasks**. Too cheap (the pick would fail): 0. Too expensive (works, but costs more): 10.

Jev's pick passed every run on 9 of 10 tasks. Following Jev cost 4.4230 EGP per set of answers, against 0.2681 EGP for the best possible picks.

Tiers: Fast = `gpt-oss-20b`, Balanced = `gpt-5.4-mini`, Thinking = `o4-mini`, Frontier = `claude-opus-5.5`. Each tier ran 3 times per task.

| Task | Jev's pick | Why | Cheapest tier that passed | Verdict | Fast | Balanced | Thinking | Frontier |
|---|---|---|---|---|---|---|---|---|
| customer-reply-arabic | **Balanced** | medium, instant, Arabic dialect; reasoning 1.2 | Fast | Too expensive: works but wastes money | 3/3 · 0.0030 | 3/3 · 0.0380 | not run | not run |
| ticket-routing | **Balanced** | medium, instant, English; reasoning 1.2 | Fast | Too expensive: works but wastes money | 3/3 · 0.0038 | 3/3 · 0.0224 | not run | not run |
| invoice-extraction | **Thinking** | medium, reasoning, English; reasoning 1.9 | Fast | Too expensive: works but wastes money | 3/3 · 0.0052 | not run | 3/3 · 0.2136 | not run |
| contract-review | **Frontier** | large, reasoning, English; reasoning 1.9 | Balanced | Too expensive: works but wastes money | 1/3 · 0.0119 | 3/3 · 0.2136 | not run | 3/3 · 3.5863 |
| meeting-actions | **Balanced** | medium, instant, English; reasoning 1.3 | Fast | Too expensive: works but wastes money | 3/3 · 0.0042 | 3/3 · 0.0280 | not run | not run |
| payment-reminder-email | **Balanced** | medium, instant, English; reasoning 1.2 | Fast | Too expensive: works but wastes money | 3/3 · 0.0118 | 3/3 · 0.0558 | not run | not run |
| executive-summary | **Balanced** | medium, instant, English; reasoning 1.1 | Fast | Too expensive: works but wastes money | 3/3 · 0.0050 | 3/3 · 0.0828 | not run | not run |
| sales-data-question | **Thinking** | medium, reasoning, English; reasoning 2.0 | Fast | Too expensive: works but wastes money | 3/3 · 0.0066 | not run | 3/3 · 0.2153 | not run |
| translation-to-arabic | **Balanced** | medium, instant, Arabic; reasoning 0.3 | Fast | Too expensive: works but wastes money | 3/3 · 0.0080 | 3/3 · 0.0368 | not run | not run |
| hr-policy-question | **Thinking** | medium, reasoning, English; reasoning 1.6 | Fast | Too expensive: works but wastes money | 3/3 · 0.0068 | not run | 2/3 · 0.1440 | not run |

Cells show runs passed and EGP per answer. Tiers above the cheapest passing one are not run unless they were Jev's pick.

Costs: Jev 0.0282 EGP for all profiles; worker models 14.1091 EGP; reviewers 2.6914 EGP.
