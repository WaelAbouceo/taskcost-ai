# Agent 1 calibration: does Jev pick the right model?

Jev picked the cheapest tier that passes for **0 of 3 tasks**. Too cheap (the pick would fail): 0. Too expensive (works, but costs more): 3.

Jev's pick passed every run on 3 of 3 tasks. Following Jev cost 0.1323 EGP per set of answers, against 0.2306 EGP for the best possible picks.

Tiers: Egypt instant = `SovereignEG/Qwen3.8-27B-FP8@instant`, Egypt fast = `SovereignEG/Qwen3.8-27B-FP8@fast`, Egypt reasoning = `SovereignEG/Qwen3.8-27B-FP8`, Fast = `gpt-oss-20b`, Balanced = `nvidia-nemotron-3-super-120b-a12b`, Strong = `deepseek-v4-pro`, Proprietary = `gpt-5.4-mini`. Each tier ran 2 times per task.

| Task | Jev's pick | Why | Cheapest tier that passed | Verdict | Egypt instant | Egypt fast | Egypt reasoning | Fast | Balanced | Strong | Proprietary |
|---|---|---|---|---|---|---|---|---|---|---|---|
| quality-root-cause | **Fast** | large, reasoning, English; reasoning 2.2 | Egypt instant | Too expensive: works but wastes money | 2/2 · 0.1149 | 2/2 · 0.2039 | 1/2 · 0.2613 | 2/2 · 0.0208 | 2/2 · 0.0539 | 2/2 · 0.1759 | 2/2 · 0.2363 |
| refund-policy-rules | **Egypt reasoning** | medium, reasoning, English; reasoning 2.3 | Egypt instant | Too expensive: works but wastes money | 2/2 · 0.0698 | 2/2 · 0.0822 | 2/2 · 0.1033 | 2/2 · 0.0092 | 1/2 · 0.0394 | 2/2 · 0.0880 | 0/2 · 0.0812 |
| safety-incident-classification | **Fast** | large, reasoning, English; reasoning 1.9 | Egypt instant | Too expensive: works but wastes money | 2/2 · 0.0459 | 2/2 · 0.0640 | 2/2 · 0.0899 | 2/2 · 0.0081 | 2/2 · 0.0207 | 2/2 · 0.0609 | 2/2 · 0.0758 |

Cells show runs passed and EGP per answer. Tiers above the cheapest passing one are not run unless they were Jev's pick.

Costs: Jev 0.0094 EGP for all profiles; worker models 3.8110 EGP; reviewers 0.9676 EGP.

**Warning:** some answers hit the token limit and were cut off, so those results are not reliable: SovereignEG/Qwen3.8-27B-FP8 on quality-root-cause.
