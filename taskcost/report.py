"""Render run results as Markdown, CSV and JSON."""

from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any

from .metrics import ModelSummary


def _pct(value: float) -> str:
    return f"{value * 100:.0f}%"


def _egp_per_1k(value: float | None) -> str:
    return "n/a" if value is None else f"{value * 1000:,.2f}"


def _seconds(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.2f}s"


def render_markdown(
    summaries: list[ModelSummary], config: dict[str, Any], failures: list[dict[str, Any]] | None = None
) -> str:
    task_count = len(config.get("task_ids", []))
    judges = config.get("judge_models") or []
    lines = [
        f"# TaskCost AI results: {config.get('name', 'benchmark')}",
        "",
        "*Powered by SovereignEG. Costs in EGP at catalog prices captured when the run started.*",
        "",
        f"{task_count} tasks, {config.get('trials')} trials each, up to {config.get('max_attempts')} "
        f"attempt(s) per run. Models need a success rate of at least "
        f"{_pct(config.get('min_success_rate', 0.8))} to be ranked.",
    ]
    if judges:
        lines += [
            "",
            f"Answers are graded against plain-language criteria by a judge panel "
            f"({', '.join(f'`{j}`' for j in judges)}); each criterion passes on a majority vote.",
        ]
    lines += [
        "",
        "## Leaderboard",
        "",
        "| Rank | Model | EGP per 1k successful tasks | Success rate | Reliability | Attempts per success "
        "| Latency p50 | Latency p95 | Total spend (EGP) |",
        "|---|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    rank = 0
    for s in summaries:
        rank_label = "-"
        if s.qualifies:
            rank += 1
            rank_label = str(rank)
        attempts = "n/a" if s.attempts_per_success is None else f"{s.attempts_per_success:.2f}"
        lines.append(
            f"| {rank_label} | `{s.model}` | {_egp_per_1k(s.cost_per_success_egp)} | {_pct(s.success_rate)} "
            f"| {_pct(s.reliability)} | {attempts} | {_seconds(s.latency_p50_s)} | {_seconds(s.latency_p95_s)} "
            f"| {s.total_cost_egp:,.4f} |"
        )

    categories = sorted({name for s in summaries for name in s.by_category})
    if categories:
        lines += [
            "",
            "## Success rate by category",
            "",
            "| Model | " + " | ".join(categories) + " |",
            "|---|" + "---:|" * len(categories),
        ]
        for s in summaries:
            cells = [
                _pct(s.by_category[name]["success_rate"]) if name in s.by_category else "n/a"
                for name in categories
            ]
            lines.append(f"| `{s.model}` | " + " | ".join(cells) + " |")

    if failures:
        lines += [
            "",
            "## Most common failures",
            "",
            "| Task | Criterion the judges said was not met | Failed runs | Models |",
            "|---|---|---:|---|",
        ]
        for f in failures:
            models = ", ".join(f"`{m}`" for m in f["models"])
            lines.append(f"| `{f['task_id']}` | {f['criterion']} | {f['runs']} | {models} |")

    lines += [
        "",
        "## How to read this",
        "",
        "- **EGP per 1k successful tasks**: everything spent on the model, including failed attempts "
        "and retries, divided by the runs that passed, times 1,000. Lower is better.",
        "- **Success rate**: share of runs (task and trial) that passed within the attempt limit.",
        "- **Reliability**: share of tasks that passed in every trial.",
        "- **Latency**: per attempt, measured at the client.",
    ]

    notes = []
    agreements = [s.judge_agreement for s in summaries if s.judge_agreement is not None]
    if agreements and len(judges) > 1:
        notes.append(
            f"- Judge panel agreement: {_pct(sum(agreements) / len(agreements))} of criterion verdicts "
            "were unanimous. Low agreement means the criteria need sharper wording."
        )
    for s in summaries:
        if s.ungraded_runs:
            notes.append(f"- `{s.model}`: {s.ungraded_runs} run(s) excluded because grading failed.")
        if s.error_rate:
            notes.append(f"- `{s.model}`: {_pct(s.error_rate)} of attempts ended in an API error.")
    grading_cost = sum(s.grading_cost_egp for s in summaries)
    if grading_cost:
        notes.append(f"- Judge model spend (not charged to any model): {grading_cost:,.4f} EGP.")
    if notes:
        lines += ["", "## Notes", "", *notes]
    return "\n".join(lines) + "\n"


def _egp(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.4f}"


def render_plan_report(
    config: dict[str, Any],
    econ: dict[str, Any],
    summaries: list[ModelSummary],
    failures: list[dict[str, Any]] | None = None,
) -> str:
    design = config.get("design") or {}
    jobs: dict[str, dict[str, Any]] = econ["jobs"]
    models = list(jobs)
    min_rate = config.get("min_success_rate", 0.8)
    tasks = econ["tasks"]
    mix = econ["mix"]

    lines = [
        f"# TaskCost AI job report: {config.get('name', 'job')}",
        "",
        "*Powered by SovereignEG. Costs in EGP at catalog prices captured when the run started.*",
        "",
        f"**Request:** {config.get('request', '')}",
        "",
        *(["> **Test mode:** simulated models and reviewers. These numbers say nothing about real models.", ""]
          if config.get("test_mode") else []),
        f"The design agent (`{design.get('model', '?')}`) broke this into **{len(tasks)} simple tasks** "
        f"for {_egp(design.get('cost_egp'))} EGP. Each worker model did the whole job "
        f"{config.get('trials')} times, with up to {config.get('max_attempts')} attempt(s) per simple task. "
        f"A judge panel ({', '.join(f'`{j}`' for j in config.get('judge_models', []))}) checked every simple task.",
        "",
        "## What the whole job costs",
        "",
        "| Model | Job done correctly | EGP per correct job | EGP per job attempt | Simple tasks passed |",
        "|---|---:|---:|---:|---:|",
    ]
    by_model = {s.model: s for s in summaries}

    def job_key(model: str) -> tuple:
        job = jobs[model]
        ok = job["passed"] > 0 and job["success_rate"] >= min_rate
        return (not ok, job["cost_per_success_egp"] if ok else -job["success_rate"], model)

    for model in sorted(models, key=job_key):
        job = jobs[model]
        s = by_model.get(model)
        simple = f"{s.passed_runs}/{s.runs + s.blocked_runs}" if s else "n/a"
        lines.append(
            f"| `{model}` | {job['passed']}/{job['runs']} ({_pct(job['success_rate'])}) "
            f"| {_egp(job['cost_per_success_egp'])} | {_egp(job['cost_per_run_egp'])} | {simple} |"
        )
    if mix["cost_per_success_egp"] is not None:
        lines.append(
            f"| **Recommended mix** (best model per simple task) | est. {_pct(mix['estimated_success_rate'])} "
            f"| **{_egp(mix['cost_per_success_egp'])}** | | |"
        )

    lines += [
        "",
        "## Best model for each simple task",
        "",
        "Each cell shows *passed / tried* and EGP per correct answer. The recommended model is the cheapest "
        f"one that passed at least {_pct(min_rate)} of its tries.",
        "",
        "| Task | Type | " + " | ".join(f"`{m}`" for m in models) + " | Recommended |",
        "|---|---|" + "---:|" * len(models) + "---|",
    ]
    for t in tasks:
        cells = []
        for model in models:
            stats = t["models"][model]
            if not stats["attempted"]:
                cells.append("blocked")
            else:
                cells.append(f"{stats['passed']}/{stats['attempted']} · {_egp(stats['cost_per_success_egp'])}")
        recommended = f"`{t['recommended']}`" if t["recommended"] else "**none reliable**"
        lines.append(f"| {t['task_id']} {t['title']} | {t['task_type']} | " + " | ".join(cells) + f" | {recommended} |")
    if mix["uncovered_tasks"]:
        lines += ["", f"No model was reliable on {', '.join(mix['uncovered_tasks'])}, so no mix can be recommended yet."]

    complex_ids = sorted({cid for job in jobs.values() for cid in job["complex"]})
    if complex_ids:
        lines += [
            "",
            "## Complex tasks",
            "",
            "| Complex task | " + " | ".join(f"`{m}`" for m in models) + " |",
            "|---|" + "---:|" * len(models),
        ]
        for cid in complex_ids:
            title = next(job["complex"][cid]["title"] for job in jobs.values() if cid in job["complex"])
            cells = [
                f"{_pct(jobs[m]['complex'][cid]['success_rate'])} · {_egp(jobs[m]['complex'][cid]['cost_per_success_egp'])}"
                for m in models
            ]
            lines.append(f"| {cid} {title} | " + " | ".join(cells) + " |")

    lines += [
        "",
        "## By task type",
        "",
        "| Type | " + " | ".join(f"`{m}`" for m in models) + " |",
        "|---|" + "---:|" * len(models),
    ]
    for task_type, per_model in econ["task_types"].items():
        cells = []
        for model in models:
            stats = per_model.get(model, {})
            rate = stats.get("success_rate")
            cells.append("n/a" if rate is None else f"{_pct(rate)} · {_egp(stats.get('cost_per_success_egp'))}")
        lines.append(f"| {task_type} | " + " | ".join(cells) + " |")

    if failures:
        lines += [
            "",
            "## Most common failures",
            "",
            "| Task | What the judges said was missing or wrong | Failed runs | Models |",
            "|---|---|---:|---|",
        ]
        for f in failures:
            lines.append(f"| {f['task_id']} | {f['criterion']} | {f['runs']} | {', '.join(f'`{m}`' for m in f['models'])} |")

    judge_cost = sum(s.grading_cost_egp for s in summaries)
    blocked = sum(s.blocked_runs for s in summaries)
    lines += [
        "",
        "## Notes",
        "",
        f"- One-off design cost: {_egp(design.get('cost_egp'))} EGP. Judge cost: {_egp(judge_cost)} EGP. "
        "Neither is included in the model costs above.",
    ]
    if blocked:
        lines.append(f"- {blocked} simple task run(s) were blocked because a task they depend on failed.")
    agreements = [s.judge_agreement for s in summaries if s.judge_agreement is not None]
    if agreements:
        lines.append(f"- Judge panel agreement: {_pct(sum(agreements) / len(agreements))} of verdicts were unanimous.")
    return "\n".join(lines) + "\n"


CSV_FIELDS = [
    "model", "model_id", "qualifies", "cost_per_success_egp", "success_rate", "reliability",
    "attempts_per_success", "latency_p50_s", "latency_p95_s", "total_cost_egp", "cost_per_run_egp",
    "runs", "passed_runs", "error_rate", "ungraded_runs", "blocked_runs", "grading_cost_egp", "judge_agreement",
]


def finalize_run(run_dir: Path, min_success_rate: float | None = None) -> str:
    """Compute metrics for a finished run, write report.md / leaderboard.csv / summary.json, return the markdown."""
    from .economics import plan_economics
    from .metrics import common_failures, summarize
    from .plan import Plan
    from .runner import load_attempts

    config = json.loads((run_dir / "config.json").read_text(encoding="utf-8"))
    if min_success_rate is not None:
        config["min_success_rate"] = min_success_rate
    min_rate = config.get("min_success_rate", 0.8)
    records = load_attempts(run_dir)
    summaries = summarize(records, min_rate)
    failures = common_failures(records)

    economics = None
    if config.get("kind") == "plan":
        economics = plan_economics(Plan.load(run_dir / "plan.yaml").root, records, min_rate)
        markdown = render_plan_report(config, economics, summaries, failures)
    else:
        markdown = render_markdown(summaries, config, failures)
    write_reports(run_dir, summaries, config, failures, markdown, economics)
    return markdown


def write_reports(
    run_dir: Path,
    summaries: list[ModelSummary],
    config: dict[str, Any],
    failures: list[dict[str, Any]] | None = None,
    markdown: str | None = None,
    economics: dict[str, Any] | None = None,
) -> dict[str, Path]:
    paths = {
        "markdown": run_dir / "report.md",
        "csv": run_dir / "leaderboard.csv",
        "json": run_dir / "summary.json",
    }
    paths["markdown"].write_text(markdown or render_markdown(summaries, config, failures), encoding="utf-8")
    with paths["csv"].open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=CSV_FIELDS, extrasaction="ignore")
        writer.writeheader()
        for s in summaries:
            writer.writerow(s.to_dict())
    paths["json"].write_text(
        json.dumps(
            {
                "config": config,
                "models": [s.to_dict() for s in summaries],
                "common_failures": failures or [],
                **({"economics": economics} if economics else {}),
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    return paths
