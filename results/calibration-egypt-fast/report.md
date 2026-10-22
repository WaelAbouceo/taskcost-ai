# Agent 1 calibration: does Jev pick the right model?

Jev picked the cheapest tier that passes for **0 of 10 tasks**. Too cheap (the pick would fail): 0. Too expensive (works, but costs more): 10.

Jev's pick passed every run on 10 of 10 tasks. Following Jev cost 0.2964 EGP per set of answers, against 0.5554 EGP for the best possible picks.

Tiers: Egypt-hosted fast = `SovereignEG/Qwen3.8-27B-FP8@fast`, Egypt-hosted instant = `SovereignEG/Qwen3.8-27B-FP8@instant`. Each tier ran 2 times per task.

| Task | Jev's pick | Why | Cheapest tier that passed | Verdict | Egypt-hosted fast | Egypt-hosted instant |
|---|---|---|---|---|---|---|
| customer-reply-arabic | **Egypt-hosted instant** | medium, instant, Arabic dialect; reasoning 1.2 | Egypt-hosted fast | Too expensive: works but wastes money | 2/2 · 0.0300 | 2/2 · 0.0089 |
| ticket-routing | **Egypt-hosted instant** | medium, instant, English; reasoning 1.2 | Egypt-hosted fast | Too expensive: works but wastes money | 2/2 · 0.0291 | 2/2 · 0.0264 |
| invoice-extraction | **Egypt-hosted instant** | medium, reasoning, English; reasoning 1.9 | Egypt-hosted fast | Too expensive: works but wastes money | 2/2 · 0.0396 | 2/2 · 0.0154 |
| contract-review | **Egypt-hosted instant** | large, reasoning, English; reasoning 1.9 | Egypt-hosted fast | Too expensive: works but wastes money | 2/2 · 0.1362 | 2/2 · 0.0933 |
| meeting-actions | **Egypt-hosted instant** | medium, instant, English; reasoning 1.3 | Egypt-hosted fast | Too expensive: works but wastes money | 2/2 · 0.0189 | 2/2 · 0.0103 |
| payment-reminder-email | **Egypt-hosted instant** | medium, instant, English; reasoning 1.2 | Egypt-hosted fast | Too expensive: works but wastes money | 2/2 · 0.0721 | 2/2 · 0.0190 |
| executive-summary | **Egypt-hosted instant** | medium, instant, English; reasoning 1.1 | Egypt-hosted fast | Too expensive: works but wastes money | 2/2 · 0.0419 | 2/2 · 0.0170 |
| sales-data-question | **Egypt-hosted instant** | medium, reasoning, English; reasoning 2.0 | Egypt-hosted fast | Too expensive: works but wastes money | 2/2 · 0.0749 | 2/2 · 0.0766 |
| translation-to-arabic | **Egypt-hosted instant** | medium, instant, Arabic; reasoning 0.3 | Egypt-hosted fast | Too expensive: works but wastes money | 2/2 · 0.0680 | 2/2 · 0.0095 |
| hr-policy-question | **Egypt-hosted instant** | medium, reasoning, English; reasoning 1.6 | Egypt-hosted fast | Too expensive: works but wastes money | 2/2 · 0.0448 | 2/2 · 0.0201 |

Cells show runs passed and EGP per answer. Tiers above the cheapest passing one are not run unless they were Jev's pick.

Costs: Jev 0.0288 EGP for all profiles; worker models 1.7037 EGP; reviewers 1.0021 EGP.
