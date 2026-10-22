"""Aggregate attempt records into per-model results.

A *run* is one (model, task, trial). It succeeds if any of its attempts passes, and its cost is the
sum of all attempts, so retries and failures are paid for by the successes they produce:

    cost per successful task = total model spend / number of successful runs
"""

from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass
class RunOutcome:
    model: str
    task_id: str
    category: str
    trial: int
    passed: bool
    attempts: int
    cost_egp: float
    grading_cost_egp: float
    latencies: list[float]
    errors: int
    ungraded: bool
    blocked: bool = False


@dataclass
class CategoryStats:
    runs: int
    passed: int
    cost_egp: float

    @property
    def success_rate(self) -> float:
        return self.passed / self.runs if self.runs else 0.0

    @property
    def cost_per_success_egp(self) -> float | None:
        return self.cost_egp / self.passed if self.passed else None


@dataclass
class ModelSummary:
    model: str
    model_id: str
    runs: int
    passed_runs: int
    success_rate: float
    reliability: float
    total_cost_egp: float
    cost_per_success_egp: float | None
    cost_per_run_egp: float
    attempts_per_success: float | None
    latency_p50_s: float | None
    latency_p95_s: float | None
    error_rate: float
    ungraded_runs: int
    blocked_runs: int
    grading_cost_egp: float
    qualifies: bool
    judge_agreement: float | None = None
    by_category: dict[str, dict[str, Any]] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def percentile(values: list[float], pct: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    rank = max(math.ceil(pct / 100 * len(ordered)) - 1, 0)
    return ordered[rank]


def group_runs(records: list[dict[str, Any]]) -> list[RunOutcome]:
    grouped: dict[tuple[str, str, int], list[dict[str, Any]]] = defaultdict(list)
    for record in records:
        grouped[(record["model"], record["task_id"], record["trial"])].append(record)

    runs = []
    for (model, task_id, trial), attempts in grouped.items():
        attempts.sort(key=lambda r: r["attempt"])
        runs.append(
            RunOutcome(
                model=model,
                task_id=task_id,
                category=attempts[0]["category"],
                trial=trial,
                passed=any(a["passed"] for a in attempts),
                attempts=len(attempts),
                cost_egp=sum(a["cost_egp"] for a in attempts),
                grading_cost_egp=sum(a.get("grading_cost_egp", 0.0) for a in attempts),
                latencies=[a["latency_s"] for a in attempts if a.get("latency_s") is not None],
                errors=sum(1 for a in attempts if a.get("error_kind") == "model"),
                ungraded=any(a.get("error_kind") == "grader" for a in attempts),
                blocked=any(a.get("error_kind") == "blocked" for a in attempts),
            )
        )
    return runs


def common_failures(records: list[dict[str, Any]], limit: int = 8) -> list[dict[str, Any]]:
    """Required criteria that failed most often, counted once per run."""
    failed_runs: dict[tuple[str, str], set[tuple[str, str, int]]] = defaultdict(set)
    for record in records:
        for c in record.get("criteria", []):
            if c["required"] and not c["passed"]:
                failed_runs[(record["task_id"], c["criterion"])].add(
                    (record["model"], record["task_id"], record["trial"])
                )
    ranked = sorted(failed_runs.items(), key=lambda item: (-len(item[1]), item[0]))
    return [
        {"task_id": task_id, "criterion": criterion, "runs": len(runs), "models": sorted({r[0] for r in runs})}
        for (task_id, criterion), runs in ranked[:limit]
    ]


def summarize(records: list[dict[str, Any]], min_success_rate: float = 0.8) -> list[ModelSummary]:
    """Rank qualifying models (success rate >= min_success_rate) by cost per success, others after."""
    model_ids = {r["model"]: r.get("model_id", r["model"]) for r in records}
    by_model: dict[str, list[RunOutcome]] = defaultdict(list)
    for run in group_runs(records):
        by_model[run.model].append(run)

    summaries = []
    for model, all_runs in by_model.items():
        # Ungraded runs (grader failed) and blocked runs (an earlier step failed) were never judged on
        # this task, so they are excluded here and reported; blocked runs still fail the whole job.
        runs = [r for r in all_runs if not r.ungraded and not r.blocked]
        passed = [r for r in runs if r.passed]
        total_cost = sum(r.cost_egp for r in runs)
        total_attempts = sum(r.attempts for r in runs)

        trials_by_task: dict[str, list[bool]] = defaultdict(list)
        categories: dict[str, CategoryStats] = defaultdict(lambda: CategoryStats(0, 0, 0.0))
        for run in runs:
            trials_by_task[run.task_id].append(run.passed)
            stats = categories[run.category]
            stats.runs += 1
            stats.passed += int(run.passed)
            stats.cost_egp += run.cost_egp

        latencies = [lat for r in runs for lat in r.latencies]
        verdicts = [c for rec in records if rec["model"] == model for c in rec.get("criteria", [])]
        success_rate = len(passed) / len(runs) if runs else 0.0
        summaries.append(
            ModelSummary(
                model=model,
                model_id=model_ids[model],
                runs=len(runs),
                passed_runs=len(passed),
                success_rate=success_rate,
                reliability=(
                    sum(all(results) for results in trials_by_task.values()) / len(trials_by_task)
                    if trials_by_task else 0.0
                ),
                total_cost_egp=total_cost,
                cost_per_success_egp=total_cost / len(passed) if passed else None,
                cost_per_run_egp=total_cost / len(runs) if runs else 0.0,
                attempts_per_success=sum(r.attempts for r in passed) / len(passed) if passed else None,
                latency_p50_s=percentile(latencies, 50),
                latency_p95_s=percentile(latencies, 95),
                error_rate=sum(r.errors for r in runs) / total_attempts if total_attempts else 0.0,
                ungraded_runs=sum(r.ungraded for r in all_runs),
                blocked_runs=sum(r.blocked for r in all_runs),
                grading_cost_egp=sum(r.grading_cost_egp for r in all_runs),
                qualifies=bool(passed) and success_rate >= min_success_rate,
                judge_agreement=(
                    sum(c["unanimous"] for c in verdicts) / len(verdicts) if verdicts else None
                ),
                by_category={
                    name: {
                        "runs": s.runs,
                        "passed": s.passed,
                        "success_rate": s.success_rate,
                        "cost_per_success_egp": s.cost_per_success_egp,
                    }
                    for name, s in sorted(categories.items())
                },
            )
        )

    return sorted(
        summaries,
        key=lambda s: (
            not s.qualifies,
            s.cost_per_success_egp if s.qualifies else -s.success_rate,
            s.model,
        ),
    )
