"""Job economics: roll simple-task results up to complex tasks and the whole job, and pick the
cheapest reliable model for each simple task.

A job run (one model, one trial) succeeds only if every simple task in it passed. Its cost is the
sum of its simple-task runs, retries included. Blocked tasks cost nothing but fail the job.
"""

from __future__ import annotations

import math
from collections import defaultdict
from typing import Any

from .metrics import RunOutcome, group_runs
from .plan import PlanNode


def _rollup(model: str, trials: list[int], task_ids: list[str], index: dict[tuple[str, str, int], RunOutcome]) -> dict[str, Any]:
    runs = []
    for trial in trials:
        task_runs = [index.get((model, task_id, trial)) for task_id in task_ids]
        runs.append({
            "trial": trial,
            "passed": all(run is not None and run.passed for run in task_runs),
            "cost_egp": sum(run.cost_egp for run in task_runs if run is not None),
        })
    passed = sum(r["passed"] for r in runs)
    total_cost = sum(r["cost_egp"] for r in runs)
    return {
        "runs": len(runs),
        "passed": passed,
        "success_rate": passed / len(runs) if runs else 0.0,
        "total_cost_egp": total_cost,
        "cost_per_run_egp": total_cost / len(runs) if runs else 0.0,
        "cost_per_success_egp": total_cost / passed if passed else None,
    }


def plan_economics(root: PlanNode, records: list[dict[str, Any]], min_success_rate: float) -> dict[str, Any]:
    runs = group_runs(records)
    index = {(r.model, r.task_id, r.trial): r for r in runs}
    models = list(dict.fromkeys(r["model"] for r in records))
    trials = {m: sorted({r.trial for r in runs if r.model == m}) for m in models}
    simple = root.simple_tasks()
    simple_ids = [node.id for node in simple]
    complex_nodes = [node for node in root.walk() if node.kind == "complex" and node is not root]

    jobs = {}
    for model in models:
        job = _rollup(model, trials[model], simple_ids, index)
        job["complex"] = {
            node.id: {"title": node.title, **_rollup(model, trials[model], [n.id for n in node.simple_tasks()], index)}
            for node in complex_nodes
        }
        jobs[model] = job

    tasks = []
    for node in simple:
        per_model = {}
        for model in models:
            attempted = [index[(model, node.id, t)] for t in trials[model]
                         if (model, node.id, t) in index and not index[(model, node.id, t)].blocked
                         and not index[(model, node.id, t)].ungraded]
            passed = [r for r in attempted if r.passed]
            cost = sum(r.cost_egp for r in attempted)
            per_model[model] = {
                "attempted": len(attempted),
                "passed": len(passed),
                "success_rate": len(passed) / len(attempted) if attempted else None,
                "cost_per_success_egp": cost / len(passed) if passed else None,
                "mean_cost_egp": cost / len(attempted) if attempted else None,
            }
        eligible = [
            (stats["cost_per_success_egp"], model) for model, stats in per_model.items()
            if stats["success_rate"] is not None and stats["success_rate"] >= min_success_rate
        ]
        tasks.append({
            "task_id": node.id,
            "title": node.title,
            "task_type": node.task_type,
            "models": per_model,
            "recommended": min(eligible)[1] if eligible else None,
        })

    covered = [t for t in tasks if t["recommended"]]
    uncovered = [t["task_id"] for t in tasks if not t["recommended"]]
    mix = {
        "cost_per_success_egp": sum(t["models"][t["recommended"]]["cost_per_success_egp"] for t in covered) if not uncovered else None,
        "estimated_success_rate": math.prod(t["models"][t["recommended"]]["success_rate"] for t in covered) if not uncovered else None,
        "uncovered_tasks": uncovered,
        "assignment": {t["task_id"]: t["recommended"] for t in tasks},
    }

    by_type: dict[str, dict[str, dict[str, float]]] = defaultdict(lambda: defaultdict(lambda: {"attempted": 0, "passed": 0, "cost": 0.0}))
    for t in tasks:
        for model, stats in t["models"].items():
            bucket = by_type[t["task_type"]][model]
            bucket["attempted"] += stats["attempted"]
            bucket["passed"] += stats["passed"]
            bucket["cost"] += (stats["mean_cost_egp"] or 0.0) * stats["attempted"]
    task_types = {
        task_type: {
            model: {
                "success_rate": b["passed"] / b["attempted"] if b["attempted"] else None,
                "cost_per_success_egp": b["cost"] / b["passed"] if b["passed"] else None,
            }
            for model, b in per_model.items()
        }
        for task_type, per_model in sorted(by_type.items())
    }

    return {"jobs": jobs, "tasks": tasks, "mix": mix, "task_types": task_types}
