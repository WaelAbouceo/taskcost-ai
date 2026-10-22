"""Calibrate Agent 1: does the profiler pick the cheapest tier that actually passes?

For each task in the library:
1. Jev profiles the task and picks a tier.
2. Ground truth: tiers run cheapest first, `trials` times each, until one passes every trial. That tier
   is the optimal one. Running cheapest first means expensive tiers only run where cheaper ones failed.
3. If Jev's pick was not run in step 2, it is run too, so every verdict is measured.
"""

from __future__ import annotations

import asyncio
import json
import sys
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, TextIO

from .config import BenchmarkConfig
from .judges import build_panel
from .profiler import Profile, profile_task, tier_for_class
from .runner import Backend, FatalRunError, first_error, load_attempts, new_run_dir, pricing_snapshot, run_job
from .tasks import Task


async def calibrate(
    config: BenchmarkConfig,
    tasks: list[Task],
    backend: Backend,
    profiler_model: str,
    trials: int = 3,
    full: bool = False,
    log: TextIO = sys.stderr,
    run_dir: Path | None = None,
) -> Path:
    tiers = list(config.tiers)
    if not tiers:
        raise ValueError("Configure tiers in benchmark.yaml")
    catalog = await backend.list_models()
    needed = [profiler_model, *config.judge_models, *(model for _, model in tiers)]
    missing = [m for m in needed if config.model_spec(m).id not in catalog]
    if missing:
        raise ValueError(f"Models not in the catalog: {missing}")
    panel = build_panel(backend, [catalog[j] for j in config.judge_models])
    run_config = replace(config, max_attempts=1)
    run_dir = run_dir or new_run_dir(config, "calibration")
    run_dir.mkdir(parents=True, exist_ok=True)

    print(f"Profiling {len(tasks)} tasks with {profiler_model}...", file=log)
    profiled = await asyncio.gather(*(profile_task(backend, catalog[profiler_model], t.prompt) for t in tasks))
    profiles: dict[str, Profile] = {t.id: p for t, p in zip(tasks, profiled)}
    picks = {t.id: tier_for_class(profiles[t.id].tier_class, tiers)[0] for t in tasks}

    semaphore = asyncio.Semaphore(config.concurrency)
    results: dict[str, dict[str, int]] = {t.id: {} for t in tasks}

    with (run_dir / "attempts.jsonl").open("w", encoding="utf-8") as sink:

        async def run_tier(tier: str, model: str, batch: list[Task]) -> None:
            spec = config.model_spec(model)

            async def one(task: Task, trial: int) -> bool:
                async with semaphore:
                    result = await run_job(run_config, backend, panel, spec, catalog[spec.id], task, trial, sink, {"tier": tier})
                return result.passed

            outcomes = await asyncio.gather(*(one(t, n) for t in batch for n in range(1, trials + 1)))
            for index, task in enumerate(batch):
                results[task.id][tier] = sum(outcomes[index * trials:(index + 1) * trials])
            print(f"  {tier} ({model}): " + ", ".join(f"{t.id} {results[t.id][tier]}/{trials}" for t in batch), file=log)

        try:
            remaining = list(tasks)
            for tier, model in tiers:
                batch = remaining if not full else list(tasks)
                if not batch:
                    break
                await run_tier(tier, model, batch)
                remaining = [t for t in remaining if results[t.id].get(tier, 0) < trials]
            for tier, model in tiers:
                unchecked = [t for t in tasks if picks[t.id] == tier and tier not in results[t.id]]
                if unchecked:
                    print("Checking Jev's pick where it wasn't run yet:", file=log)
                    await run_tier(tier, model, unchecked)
        except* FatalRunError as errors:
            raise first_error(errors) from None

    summary = summarize_calibration(tasks, tiers, profiles, picks, results, load_attempts(run_dir), trials)
    summary.update({
        "profiler_model": profiler_model,
        "judge_models": list(config.judge_models),
        "trials": trials,
        "full": full,
        "started_at": datetime.now(timezone.utc).isoformat(),
        "pricing_egp": pricing_snapshot([catalog[config.model_spec(m).id] for m in dict.fromkeys(needed)]),
    })
    (run_dir / "calibration.json").write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")
    (run_dir / "report.md").write_text(render_calibration(summary), encoding="utf-8")
    return run_dir


def summarize_calibration(
    tasks: list[Task],
    tiers: list[tuple[str, str]],
    profiles: dict[str, Profile],
    picks: dict[str, str],
    results: dict[str, dict[str, int]],
    records: list[dict[str, Any]],
    trials: int,
) -> dict[str, Any]:
    order = [tier for tier, _ in tiers]
    cost: dict[tuple[str, str], float] = {}
    for r in records:
        key = (r["task_id"], r["tier"])
        cost[key] = cost.get(key, 0.0) + r["cost_egp"]

    rows = []
    for task in tasks:
        passed = results[task.id]
        optimal = next((tier for tier in order if passed.get(tier) == trials), None)
        pick = picks[task.id]
        if optimal is None:
            verdict = "none_passed"
        elif pick == optimal:
            verdict = "right"
        elif order.index(pick) < order.index(optimal):
            verdict = "too_cheap"
        else:
            verdict = "too_expensive"
        per_answer = {tier: cost.get((task.id, tier), 0.0) / trials for tier in passed}
        profile = profiles[task.id]
        rows.append({
            "task_id": task.id,
            "category": task.category,
            "pick": pick,
            "optimal": optimal,
            "verdict": verdict,
            "pick_works": passed.get(pick) == trials,
            "results": {tier: {"passed": passed[tier], "runs": trials, "cost_per_answer_egp": per_answer[tier]}
                        for tier in order if tier in passed},
            "pick_cost_egp": per_answer.get(pick),
            "optimal_cost_egp": per_answer.get(optimal) if optimal else None,
            "profile": profile.to_dict(),
        })

    counted = [r for r in rows if r["verdict"] != "none_passed"]
    both = [r for r in counted if r["pick_cost_egp"] is not None and r["optimal_cost_egp"] is not None]
    return {
        "tiers": [{"tier": t, "model": m} for t, m in tiers],
        "tasks": rows,
        "right": sum(r["verdict"] == "right" for r in rows),
        "too_cheap": sum(r["verdict"] == "too_cheap" for r in rows),
        "too_expensive": sum(r["verdict"] == "too_expensive" for r in rows),
        "none_passed": sum(r["verdict"] == "none_passed" for r in rows),
        "pick_works": sum(r["pick_works"] for r in rows),
        "pick_cost_egp": sum(r["pick_cost_egp"] for r in both),
        "optimal_cost_egp": sum(r["optimal_cost_egp"] for r in both),
        "profiler_cost_egp": sum(p.cost_egp for p in profiles.values()),
        "judge_cost_egp": sum(r.get("grading_cost_egp", 0.0) for r in records),
        "model_cost_egp": sum(r["cost_egp"] for r in records),
        "truncated": sorted({f"{r['model']} on {r['task_id']}" for r in records if r.get("truncated")}),
    }


VERDICT_TEXT = {
    "right": "Right",
    "too_cheap": "Too cheap: would fail",
    "too_expensive": "Too expensive: works but wastes money",
    "none_passed": "No tier passed every run",
}


def render_calibration(s: dict[str, Any]) -> str:
    n = len(s["tasks"])
    order = [t["tier"] for t in s["tiers"]]
    lines = [
        "# Agent 1 calibration: does Jev pick the right model?",
        "",
        f"Jev picked the cheapest tier that passes for **{s['right']} of {n} tasks**. "
        f"Too cheap (the pick would fail): {s['too_cheap']}. Too expensive (works, but costs more): {s['too_expensive']}."
        + (f" No tier passed every run: {s['none_passed']}." if s["none_passed"] else ""),
        "",
        f"Jev's pick passed every run on {s['pick_works']} of {n} tasks. Following Jev cost "
        f"{s['pick_cost_egp']:.4f} EGP per set of answers, against {s['optimal_cost_egp']:.4f} EGP for the best possible picks.",
        "",
        "Tiers: " + ", ".join(f"{t['tier']} = `{t['model']}`" for t in s["tiers"]) + f". Each tier ran {s['trials']} times per task.",
        "",
        "| Task | Jev's pick | Why | Cheapest tier that passed | Verdict | " + " | ".join(order) + " |",
        "|---|---|---|---|---|" + "---|" * len(order),
    ]
    for r in s["tasks"]:
        p = r["profile"]
        why = f"{p['size']}, {p['model_type']}, {p['language']}; reasoning {p['scores']['reasoning']:.1f}"
        cells = []
        for tier in order:
            res = r["results"].get(tier)
            cells.append("not run" if not res else f"{res['passed']}/{res['runs']} · {res['cost_per_answer_egp']:.4f}")
        lines.append(
            f"| {r['task_id']} | **{r['pick']}** | {why} | {r['optimal'] or 'none'} | {VERDICT_TEXT[r['verdict']]} | "
            + " | ".join(cells) + " |"
        )
    lines += [
        "",
        "Cells show runs passed and EGP per answer. Tiers above the cheapest passing one are not run unless they were Jev's pick.",
        "",
        f"Costs: Jev {s['profiler_cost_egp']:.4f} EGP for all profiles; worker models {s['model_cost_egp']:.4f} EGP; "
        f"reviewers {s['judge_cost_egp']:.4f} EGP.",
    ]
    if s.get("truncated"):
        lines += ["", "**Warning:** some answers hit the token limit and were cut off, so those results are not "
                  "reliable: " + ", ".join(s["truncated"]) + "."]
    return "\n".join(lines) + "\n"
