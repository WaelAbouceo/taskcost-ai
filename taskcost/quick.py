"""Quick compare: one simple enterprise task, one model per tier (Fast, Balanced, Thinking, Frontier...).

The request is made concrete by the quick designer, every tier answers the same task, the judge panel
checks each answer, and the result is the cost per correct answer, projected to 1,000 tasks.
"""

from __future__ import annotations

import json
import sys
import time
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from typing import Any, TextIO

from .cache import Cache, CachedDecisions
from .config import BenchmarkConfig
from .design import design_simple_task
from .execute import execute_plan
from .plan import Plan, build_task
from .recommender import recommend_hybrid
from .licenses import is_egypt_hosted, is_open_weights
from .naming import slug
from .registry import MIN_SHORT_RUNS, USE_CASES, registry_from_results
from .report import finalize_run
from .sovereignty import FIT_RANK, LEVELS, accepted, country_name, model_fit, residency_ranks
from .runner import Backend, load_attempts


async def quick_compare(
    config: BenchmarkConfig,
    request: str,
    tiers: list[tuple[str, str]],
    backend: Backend,
    plans_dir: Path,
    documents: dict[str, Path] | None = None,
    trials: int = 1,
    log: TextIO = sys.stderr,
    progress: Any = None,
    use_case: str = "batch",
    plan: Plan | None = None,
    cache: Cache | None = None,
    use_case_guess: dict[str, float] | None = None,
    personalization: dict[str, Any] | None = None,
    size: dict[str, Any] | None = None,
) -> Path:
    """Run the whole quick flow and return the results folder.

    Pass `plan` to reuse an earlier concrete task (for "run again"), so the results stay comparable.
    `use_case_guess` holds Agent 1's probabilities when it chose the use case, rather than the user.
    """
    if use_case not in USE_CASES:
        raise ValueError(f"use_case must be one of {list(USE_CASES)}")
    tiers = list(dict(tiers).items()) if tiers else list(config.tiers)
    tiers = [(tier, model) for tier, model in tiers if model]
    if not tiers:
        raise ValueError("Choose at least one model")
    seen: dict[str, str] = {}
    skipped = []
    for tier, model in tiers:
        if not accepted(config, model):
            # Strict mode: the request is never sent to a model outside the places the user's law accepts.
            skipped.append({"tier": tier, "model": model, **model_fit(model, config.country)})
            continue
        seen.setdefault(model, tier)
    if not seen:
        raise ValueError(f"Strict mode: none of the chosen models runs in {country_name(config.country)} or a place "
                         "your law allows, so nothing was sent anywhere.")
    designer_id = config.quick_design_model or config.design_model
    if not designer_id:
        raise ValueError("Set quick_design_model (or design_model) in benchmark.yaml")

    catalog = await backend.list_models()
    missing = [m for m in [designer_id, *(config.model_spec(m).id for m in seen)] if m not in catalog]
    if missing:
        raise ValueError(f"Models not in the catalog: {missing}")

    if progress:
        progress("design")
    if plan is None:
        print(f"Understanding the task with {designer_id}...", file=log)
        plans_dir.mkdir(parents=True, exist_ok=True)
        plan = await design_simple_task(backend, catalog[designer_id], request, documents or {}, plans_dir.resolve(),
                                        context=(personalization or {}).get("context", ""))
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        plan.reviewed = True
        plan.save(plans_dir / f"quick-{slug(plan.root.title, 'task')}-{stamp}.yaml")
    else:
        print("Reusing the task from the earlier comparison.", file=log)
    task = plan.simple_tasks()[0]
    print(f"Task: {task.title}", file=log)

    if progress:
        progress("recommend")
    recommend_started = time.monotonic()
    recommendation, registry = await _recommend(config, plan, catalog, backend, use_case, cache, log)
    recommend_seconds = time.monotonic() - recommend_started
    picked = (recommendation or {}).get("model")
    if picked and picked not in seen and config.model_spec(picked).id in catalog and accepted(config, picked):
        # Always test Agent 1's own pick, so its prediction can be confirmed or refuted.
        seen[picked] = "Agent 1 pick"
        print(f"Adding Agent 1's pick ({picked}) to the comparison.", file=log)

    if progress:
        progress("run")
    run_started = time.monotonic()
    run_config = replace(
        config,
        models=tuple(config.model_spec(model) for model in seen),
        trials=max(1, min(trials, 5)),
        max_attempts=1,
    )
    run_dir = await execute_plan(
        run_config, plan, backend, log=log,
        run_name=f"quick-{slug(plan.root.title, 'task')}",
        metadata={
            "quick": True,
            "tiers": [{"tier": seen[m], "model": m} for m in seen],
            "use_case": use_case,
            "use_case_guess": use_case_guess,
            "personalization": personalization,
            "recommendation": recommendation,
            "registry": registry,
            "skipped_tiers": skipped,
            "residency": {m: model_fit(m, config.country) for m in seen},
            "size": size,
            "timeline": {"recommend_s": round(recommend_seconds, 2)},
        },
    )
    timeline_file = run_dir / "config.json"
    saved = json.loads(timeline_file.read_text(encoding="utf-8"))
    saved["timeline"]["run_s"] = round(time.monotonic() - run_started, 2)
    timeline_file.write_text(json.dumps(saved, indent=2), encoding="utf-8")
    finalize_run(run_dir)
    return run_dir


async def _recommend(
    config: BenchmarkConfig, plan: Plan, catalog: dict[str, Any], backend: Backend,
    use_case: str, cache: Cache | None, log: TextIO,
) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    """Agent 1's recommendation before any model runs; skipped if there is no measured registry yet."""
    cards, _ = registry_from_results(config.output_path)
    registry = {model: card.to_dict() for model, card in cards.items()}
    cards = {m: c for m, c in cards.items() if accepted(config, m)}
    jev_id = config.profiler_model
    if not cards or not jev_id or jev_id not in catalog:
        return None, registry
    decider = CachedDecisions(backend, cache, config.cache_decisions_hours * 3600) if cache else backend
    task = plan.simple_tasks()[0]
    prompt = build_task(plan, task, plan.load_documents(), {}).prompt
    try:
        rec = await recommend_hybrid(decider, catalog[jev_id], prompt, use_case, cards, config.escalate_at,
                                     config.prefer_open_weights, config.min_model_pass_rate,
                                     residency_ranks(config, cards))
    except Exception as exc:  # the comparison still runs without a recommendation
        print(f"Recommendation unavailable: {exc}", file=log)
        return None, registry
    print(f"Agent 1 recommends {rec.model} for {use_case}{' (cached)' if rec.cached else ''}.", file=log)
    return rec.to_dict(), registry


def speed_verdict(card: dict[str, Any], latencies: list[float], use_case: str) -> dict[str, Any]:
    """The same rule for every model: with enough earlier answers (MIN_SHORT_RUNS; chatbot-length ones for a
    chatbot), the slowest 1 in 10 of them decides, because one run says little about the slow answers; otherwise
    this run's slowest answer decides. Both figures are returned so the page can show them for every model."""
    chat_speed = use_case == "chat" and card.get("short_runs", 0) >= MIN_SHORT_RUNS
    p90 = card.get("latency_p90_short_s") if chat_speed else card.get("latency_p90_s")
    answers = card.get("short_runs", 0) if chat_speed else card.get("runs", 0)
    if answers < MIN_SHORT_RUNS:
        p90, answers = None, 0
    this_run = max(latencies) if latencies else None
    budget = USE_CASES.get(use_case, USE_CASES["batch"])["max_p90_s"]
    slowest = p90 if p90 is not None else this_run
    return {"this_run_s": this_run, "runs": len(latencies), "p90_s": p90, "answers": answers,
            "basis": "history" if p90 is not None else "this run", "limit_s": budget, "slowest_s": slowest,
            "fast_enough": budget is None or (slowest is not None and slowest <= budget)}


def quick_summary(run_dir: Path) -> dict[str, Any]:
    """Everything the quick-compare page shows, per tier."""
    config = json.loads((run_dir / "config.json").read_text(encoding="utf-8"))
    plan = Plan.load(run_dir / "plan.yaml")
    task = plan.simple_tasks()[0]
    records = load_attempts(run_dir)
    use_case = config.get("use_case", "batch")
    budget = USE_CASES.get(use_case, USE_CASES["batch"])["max_p90_s"]
    registry = config.get("registry") or {}
    country = config.get("country", "EG")
    level = config.get("sovereignty") or ("prefer_local" if config.get("prefer_egypt_hosted") else "open")
    stored = config.get("residency") or {}

    tiers = []
    for entry in config.get("tiers", []):
        runs = sorted((r for r in records if r["model"] == entry["model"]), key=lambda r: r["trial"])
        graded = [r for r in runs if r.get("error_kind") is None]
        passed = [r for r in graded if r["passed"]]
        # An answer with no failed check but some checks without a verdict is "not reviewed": neither a pass nor a fail.
        reviewed_fail = lambda r: any(c["required"] and c.get("reviewed", True) and not c["passed"] for c in r.get("criteria", []))
        unreviewed = [r for r in graded if r.get("unreviewed") and not reviewed_fail(r)]
        cost = sum(r["cost_egp"] for r in runs)
        latencies = [r["latency_s"] for r in runs if r.get("latency_s") is not None]
        failed_checks = list(dict.fromkeys(
            c["criterion"] for r in graded for c in r.get("criteria", [])
            if c["required"] and c.get("reviewed", True) and not c["passed"]
        ))
        cost_per_success = cost / len(passed) if passed else None
        speed = speed_verdict(registry.get(entry["model"]) or {}, latencies, use_case)
        slowest, measured_p90 = speed["slowest_s"], speed["p90_s"]
        tiers.append({
            "open_weights": is_open_weights(entry["model"]),
            "egypt_hosted": is_egypt_hosted(entry["model"]),
            "residency": stored.get(entry["model"]) or model_fit(entry["model"], country),
            "fast_enough": speed["fast_enough"],
            "slowest_s": slowest,
            "speed_source": "registry" if measured_p90 is not None else "this run",
            "speed": {k: v for k, v in speed.items() if k not in ("slowest_s", "fast_enough")},
            "tier": entry["tier"],
            "model": entry["model"],
            "runs": len(runs),
            "passed": len(passed),
            "errors": len(runs) - len(graded),
            "unreviewed": len(unreviewed),
            "success_rate": len(passed) / len(runs) if runs else 0.0,
            "quality": sum(r["score"] for r in graded) / len(graded) if graded else None,
            "cost_per_answer_egp": cost / len(runs) if runs else None,
            "cost_per_success_egp": cost_per_success,
            "cost_per_1000_egp": cost_per_success * 1000 if cost_per_success is not None else None,
            "latency_s": sum(latencies) / len(latencies) if latencies else None,
            "failed_checks": failed_checks,
            "answers": [
                {
                    "trial": r["trial"], "passed": r["passed"], "score": r["score"], "output": r["output"],
                    "cost_egp": r["cost_egp"], "latency_s": r.get("latency_s"), "error": r.get("error"),
                    "criteria": r.get("criteria", []),
                }
                for r in runs
            ],
        })

    reliable = [t for t in tiers if t["runs"] and t["passed"] == t["runs"] and t["fast_enough"]]
    prefer_open = config.get("prefer_open_weights", True)
    local_first = level != "open"
    recommended = min(reliable, key=lambda t: (
        t["residency"]["rank"] if local_first else 0,
        (not t["open_weights"]) if prefer_open else False,
        t["cost_per_success_egp"],
    )) if reliable else None
    cheapest = min(reliable, key=lambda t: t["cost_per_success_egp"]) if reliable else None
    priced = [t for t in reliable if t["cost_per_success_egp"]]  # "the same result" means it also passed and was fast enough
    most_expensive = max(priced, key=lambda t: t["cost_per_success_egp"]) if priced else None
    judge_cost = sum(r.get("grading_cost_egp", 0.0) for r in records)

    advice = config.get("recommendation")
    if advice and advice.get("model"):
        ran = next((t for t in tiers if t["model"] == advice["model"]), None)
        if ran is None:
            advice["verdict"] = "not_run"
        elif ran["unreviewed"] and ran["passed"] + ran["unreviewed"] == ran["runs"]:
            advice["verdict"] = "not_reviewed"
        elif ran["passed"] == ran["runs"] and recommended is not None and recommended["model"] == ran["model"]:
            advice["verdict"] = "best"
        elif ran["passed"] == ran["runs"]:
            advice["verdict"] = "worked"
        else:
            advice["verdict"] = "failed"

    from .trace import quick_trace

    return {
        "name": run_dir.name,
        "trace": quick_trace(config, tiers, records, recommended),
        "use_case": use_case,
        "use_case_label": USE_CASES.get(use_case, USE_CASES["batch"])["label"],
        "use_case_guess": config.get("use_case_guess"),
        "personalization": config.get("personalization"),
        "latency_budget_s": budget,
        "advice": advice,
        "request": config.get("request"),
        "title": task.title,
        "task_type": task.task_type,
        "instruction": task.instruction,
        "criteria": [c if isinstance(c, str) else c.get("text", "") for c in task.criteria],
        "expected_answer": task.expected_answer,
        "assumptions": (config.get("design") or {}).get("assumptions", ""),
        "recurring": (config.get("design") or {}).get("recurring", True),
        "documents": list(plan.documents),
        "trials": config.get("trials"),
        "test_mode": bool(config.get("test_mode")),
        "design_cost_egp": (config.get("design") or {}).get("cost_egp"),
        "size_cost_egp": (config.get("size") or {}).get("cost_egp"),
        "judge_cost_egp": judge_cost,
        "started_at": config.get("started_at"),
        "tiers": tiers,
        "recommended": recommended and {"tier": recommended["tier"], "model": recommended["model"]},
        "unreviewed_tiers": sum(1 for t in tiers if t["unreviewed"]),
        "cheapest_correct": cheapest and cheapest is not recommended and {
            "tier": cheapest["tier"], "model": cheapest["model"], "cost_per_1000_egp": cheapest["cost_per_1000_egp"],
            "egypt_hosted": cheapest["egypt_hosted"], "open_weights": cheapest["open_weights"],
            "residency": cheapest["residency"],
        } or None,
        "prefer_open_weights": prefer_open,
        "proprietary_fallback": bool(prefer_open and recommended and not recommended["open_weights"]),
        "helper_mode": config.get("helper_mode"),
        "helpers": config.get("helpers"),
        "helpers_local": bool(config.get("helpers")) and all(
            model_fit(m, country)["rank"] <= FIT_RANK["allowed"] for m in (config.get("helpers") or {}).values() if m),
        "sovereignty": {
            "country": country, "country_name": country_name(country), "level": level, "level_label": LEVELS.get(level, level),
            "notes": config.get("sovereignty_notes") or [], "skipped_tiers": config.get("skipped_tiers") or [],
            "judges": config.get("judge_models") or [],
        },
        # A closer model was compared but didn't both pass and stay fast enough, so the pick runs further away.
        "local_fallback": bool(local_first and recommended and tiers
                               and recommended["residency"]["rank"] > min(t["residency"]["rank"] for t in tiers)),
        "savings_ratio": (
            most_expensive["cost_per_success_egp"] / recommended["cost_per_success_egp"]
            if recommended and most_expensive and most_expensive is not recommended else None
        ),
        "most_expensive": most_expensive and {"tier": most_expensive["tier"], "model": most_expensive["model"]},
    }
