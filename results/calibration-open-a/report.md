# Agent 1 calibration: does Jev pick the right model?

Jev picked the cheapest tier that passes for **1 of 10 tasks**. Too cheap (the pick would fail): 1. Too expensive (works, but costs more): 8.

Jev's pick passed every run on 8 of 10 tasks. Following Jev cost 0.1945 EGP per set of answers, against 0.0894 EGP for the best possible picks.

Tiers: Fast = `gpt-oss-120b`, Balanced = `gemma-4-31b-it-turbo`, Strong = `qwen3-next-80b-a3b-instruct`, Thinking = `deepseek-v4-pro`. Each tier ran 2 times per task.

| Task | Jev's pick | Why | Cheapest tier that passed | Verdict | Fast | Balanced | Strong | Thinking |
|---|---|---|---|---|---|---|---|---|
| customer-reply-arabic | **Balanced** | medium, instant, Arabic dialect; reasoning 1.2 | Fast | Too expensive: works but wastes money | 2/2 · 0.0030 | 2/2 · 0.0037 | 2/2 · 0.0159 | 2/2 · 0.0373 |
| ticket-routing | **Balanced** | medium, instant, English; reasoning 1.2 | Fast | Too expensive: works but wastes money | 2/2 · 0.0036 | 2/2 · 0.0021 | 2/2 · 0.0192 | 2/2 · 0.0284 |
| invoice-extraction | **Strong** | medium, reasoning, English; reasoning 1.9 | Fast | Too expensive: works but wastes money | 2/2 · 0.0057 | 2/2 · 0.0040 | 0/2 · 0.0162 | 2/2 · 0.0370 |
| contract-review | **Thinking** | large, reasoning, English; reasoning 1.9 | Fast | Too expensive: works but wastes money | 2/2 · 0.0146 | 2/2 · 0.0107 | 0/2 · 0.1011 | 2/2 · 0.0733 |
| meeting-actions | **Balanced** | medium, instant, English; reasoning 1.3 | Fast | Too expensive: works but wastes money | 2/2 · 0.0033 | 2/2 · 0.0019 | 2/2 · 0.0078 | 2/2 · 0.0252 |
| payment-reminder-email | **Balanced** | medium, instant, English; reasoning 1.2 | Balanced | Right | 0/2 · 0.0041 | 2/2 · 0.0055 | 0/2 · 0.0163 | 2/2 · 0.0460 |
| executive-summary | **Balanced** | medium, instant, English; reasoning 1.0 | Fast | Too expensive: works but wastes money | 2/2 · 0.0051 | 2/2 · 0.0061 | 2/2 · 0.0152 | 2/2 · 0.0518 |
| sales-data-question | **Strong** | medium, reasoning, English; reasoning 2.0 | Fast | Too expensive: works but wastes money | 2/2 · 0.0102 | 2/2 · 0.0083 | 2/2 · 0.0606 | 2/2 · 0.1064 |
| translation-to-arabic | **Balanced** | medium, instant, Arabic; reasoning 0.3 | Thinking | Too cheap: would fail | 1/2 · 0.0039 | 0/2 · 0.0094 | 0/2 · 0.0124 | 2/2 · 0.0322 |
| hr-policy-question | **Strong** | medium, reasoning, English; reasoning 1.6 | Fast | Too expensive: works but wastes money | 2/2 · 0.0063 | 0/2 · 0.0030 | 2/2 · 0.0156 | 2/2 · 0.0457 |

Cells show runs passed and EGP per answer. Tiers above the cheapest passing one are not run unless they were Jev's pick.

Costs: Jev 0.0288 EGP for all profiles; worker models 1.7564 EGP; reviewers 3.4653 EGP.
