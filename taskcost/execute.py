"""Executes a plan: every worker model completes every simple task, in dependency order, per trial."""

from __future__ import annotations

import asyncio
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import TextIO

from .client import ModelInfo
from .config import BenchmarkConfig, ModelSpec
from .judges import build_panel
from .naming import slug
from .plan import Plan, PlanNode, build_task
from .runner import (
    Backend, FatalRunError, JobResult, base_record, first_error, new_run_dir, pricing_snapshot,
    resolve_models, run_job, warn_on_judge_overlap, write_record,
)


async def execute_plan(
    config: BenchmarkConfig,
    plan: Plan,
    backend: Backend,
    run_dir: Path | None = None,
    log: TextIO = sys.stderr,
    metadata: dict | None = None,
    run_name: str | None = None,
) -> Path:
    catalog = await backend.list_models()
    models, judge_infos = resolve_models(config, catalog, needs_judge=True)
    judge = build_panel(backend, judge_infos)
    warn_on_judge_overlap(models, judge_infos, log)
    documents = plan.load_documents()
    ordered = plan.ordered_tasks()
    parents = {child.id: node.id for node in plan.root.walk() for child in node.children}

    if run_dir is None:
        run_dir = new_run_dir(config, run_name or f"job-{slug(plan.root.title, 'plan')}")
    run_dir.mkdir(parents=True, exist_ok=True)
    plan.save(run_dir / "plan.yaml")
    (run_dir / "config.json").write_text(
        json.dumps(
            {
                **config.to_dict(),
                "kind": "plan",
                "name": plan.root.title,
                "request": plan.request,
                "task_ids": [node.id for node in ordered],
                "design": plan.design,
                "started_at": datetime.now(timezone.utc).isoformat(),
                "pricing_egp": pricing_snapshot([info for _, info in models] + judge_infos),
                **(metadata or {}),
            },
            indent=2,
        ),
        encoding="utf-8",
    )

    semaphore = asyncio.Semaphore(config.concurrency)
    total = len(models) * config.trials * len(ordered)
    done = 0

    with (run_dir / "attempts.jsonl").open("w", encoding="utf-8") as sink:

        async def plan_run(spec: ModelSpec, info: ModelInfo, trial: int) -> None:
            futures: dict[str, asyncio.Task[JobResult]] = {}

            async def simple_task(node: PlanNode) -> JobResult:
                nonlocal done
                dependencies = {dep: await futures[dep] for dep in node.depends_on}
                extra = {"task_type": node.task_type, "parent_id": parents.get(node.id), "title": node.title}
                failed = [dep for dep, result in dependencies.items() if not result.passed]
                if failed:
                    task = build_task(plan, node, documents, {dep: "" for dep in node.depends_on})
                    record = base_record(spec, task, trial, 1, extra)
                    record.update(error=f"blocked: depends on failed {failed}", error_kind="blocked")
                    write_record(sink, record)
                    result = JobResult(False, 0)
                    status = "BLOCKED"
                else:
                    task = build_task(plan, node, documents, {dep: r.output for dep, r in dependencies.items()})
                    async with semaphore:
                        result = await run_job(config, backend, judge, spec, info, task, trial, sink, extra)
                    status = "PASS" if result.passed else "FAIL"
                done += 1
                print(f"[{done}/{total}] {spec.label} | trial {trial} | {node.id} {node.title}: {status}", file=log)
                return result

            async with asyncio.TaskGroup() as group:
                for node in ordered:
                    futures[node.id] = group.create_task(simple_task(node))

        try:
            async with asyncio.TaskGroup() as group:
                for spec, info in models:
                    for trial in range(1, config.trials + 1):
                        group.create_task(plan_run(spec, info, trial))
        except* FatalRunError as errors:
            raise first_error(errors) from None

    return run_dir
