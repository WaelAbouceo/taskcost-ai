"""Runs every (model, task, trial) and writes one JSON line per attempt."""

from __future__ import annotations

import asyncio
import json
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol, TextIO

from .client import APIError, Completion, Decision, ModelInfo
from .config import BenchmarkConfig, ModelSpec
from .graders import GradeContext, GraderUnavailable, grade
from .judges import JudgePanel, build_panel
from .naming import unique_dir
from .tasks import Task

MAX_STORED_OUTPUT_CHARS = 4000


class Backend(Protocol):
    async def list_models(self) -> dict[str, ModelInfo]: ...

    async def chat(
        self, model: ModelInfo, messages: list[dict[str, Any]], max_tokens: int,
        temperature: float | None = None, extra: dict[str, Any] | None = None,
    ) -> Completion: ...

    async def decide(self, model: ModelInfo, state: Any, questions: dict[str, Any]) -> Decision: ...


class FatalRunError(Exception):
    """An error (bad key, no credit) that would make every remaining request fail."""


def first_error(group: BaseException) -> BaseException:
    """Unwrap (possibly nested) task-group exception groups to the first real error."""
    while isinstance(group, BaseExceptionGroup):
        group = group.exceptions[0]
    return group


@dataclass(frozen=True)
class JobResult:
    passed: bool
    attempts: int
    output: str = ""


def resolve_models(
    config: BenchmarkConfig, catalog: dict[str, ModelInfo], needs_judge: bool
) -> tuple[list[tuple[ModelSpec, ModelInfo]], list[ModelInfo]]:
    missing = [spec.id for spec in config.models if spec.id not in catalog]
    wrong_modality = [
        spec.id for spec in config.models if spec.id in catalog and catalog[spec.id].modality != "chat"
    ]
    judges: list[ModelInfo] = []
    if needs_judge:
        if not config.judge_models:
            raise ValueError("Some tasks are graded by judges; set judge_models in the benchmark config")
        missing += [j for j in config.judge_models if j not in catalog]
        judges = [catalog[j] for j in config.judge_models if j in catalog]
    if missing:
        raise ValueError(f"Models not in the catalog: {missing}. Run `taskcost models` for valid ids.")
    if wrong_modality:
        raise ValueError(f"Models under test must be chat models: {wrong_modality}")
    return [(spec, catalog[spec.id]) for spec in config.models], judges


def resolve_panel(config: BenchmarkConfig, catalog: dict[str, ModelInfo], backend: Backend) -> JudgePanel:
    missing = [j for j in config.judge_models if j not in catalog]
    if not config.judge_models or missing:
        raise ValueError(f"judge_models must be set and exist in the catalog (missing: {missing})")
    return build_panel(backend, [catalog[j] for j in config.judge_models])


def warn_on_judge_overlap(models: list[tuple[ModelSpec, ModelInfo]], judges: list[ModelInfo], log: TextIO) -> None:
    overlap = sorted({spec.id for spec, _ in models} & {j.id for j in judges})
    if overlap:
        print(f"Warning: {overlap} both answer and judge; judges tend to favour their own answers.", file=log)


def pricing_snapshot(models: list[ModelInfo]) -> dict[str, Any]:
    return {
        m.id: {
            "modality": m.modality,
            "input_per_1m_egp": m.pricing.input_per_1m,
            "output_per_1m_egp": m.pricing.output_per_1m,
            "cache_read_per_1m_egp": m.pricing.cache_read_per_1m,
        }
        for m in models
    }


def new_run_dir(config: BenchmarkConfig, name: str) -> Path:
    """A new, never-reused results folder (runs started in the same second get a numeric suffix)."""
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return unique_dir(config.output_path, f"{name}-{stamp}")


async def run_benchmark(
    config: BenchmarkConfig,
    tasks: list[Task],
    backend: Backend,
    run_dir: Path | None = None,
    log: TextIO = sys.stderr,
) -> Path:
    catalog = await backend.list_models()
    models, judge_infos = resolve_models(config, catalog, any(task.needs_judge for task in tasks))
    judge = build_panel(backend, judge_infos) if judge_infos else None
    warn_on_judge_overlap(models, judge_infos, log)

    if run_dir is None:
        run_dir = new_run_dir(config, config.name)
    run_dir.mkdir(parents=True, exist_ok=True)

    (run_dir / "config.json").write_text(
        json.dumps(
            {
                **config.to_dict(),
                "kind": "suite",
                "task_ids": [t.id for t in tasks],
                "started_at": datetime.now(timezone.utc).isoformat(),
                "pricing_egp": pricing_snapshot([info for _, info in models] + judge_infos),
            },
            indent=2,
        ),
        encoding="utf-8",
    )

    jobs = [(spec, info, task, trial) for spec, info in models for task in tasks for trial in range(1, config.trials + 1)]
    semaphore = asyncio.Semaphore(config.concurrency)
    done = 0

    with (run_dir / "attempts.jsonl").open("w", encoding="utf-8") as sink:

        async def worker(spec: ModelSpec, info: ModelInfo, task: Task, trial: int) -> None:
            nonlocal done
            async with semaphore:
                result = await run_job(config, backend, judge, spec, info, task, trial, sink)
            done += 1
            status = "PASS" if result.passed else "FAIL"
            print(f"[{done}/{len(jobs)}] {spec.label} | {task.id} | trial {trial}: {status} ({result.attempts} attempt(s))", file=log)

        try:
            async with asyncio.TaskGroup() as group:
                for job in jobs:
                    group.create_task(worker(*job))
        except* FatalRunError as errors:
            raise first_error(errors) from None

    return run_dir


def base_record(spec: ModelSpec, task: Task, trial: int, attempt: int, extra: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "model": spec.label,
        "model_id": spec.id,
        "task_id": task.id,
        "category": task.category,
        "trial": trial,
        "attempt": attempt,
        "passed": False,
        "score": 0.0,
        "detail": "",
        "output": "",
        "cost_egp": 0.0,
        "grading_cost_egp": 0.0,
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "cached_tokens": 0,
        "latency_s": None,
        "error": None,
        "error_kind": None,
        "request_id": None,
        "criteria": [],
        "truncated": False,
        **(extra or {}),
    }


async def run_job(
    config: BenchmarkConfig,
    backend: Backend,
    judge: Any,
    spec: ModelSpec,
    info: ModelInfo,
    task: Task,
    trial: int,
    sink: TextIO,
    extra: dict[str, Any] | None = None,
) -> JobResult:
    """One run: up to max_attempts attempts at a task, each billed and logged, until one passes."""
    ctx = GradeContext(task=task, judge=judge, judge_threshold=config.judge_threshold)
    last_output = ""
    for attempt in range(1, config.max_attempts + 1):
        record = base_record(spec, task, trial, attempt, extra)

        try:
            completion = await backend.chat(
                info, task.messages(), max_tokens=task.max_tokens,
                temperature=spec.temperature, extra=spec.extra,
            )
        except APIError as exc:
            record.update(error=str(exc), error_kind="model", request_id=exc.request_id)
            write_record(sink, record)
            if exc.fatal:
                raise FatalRunError(str(exc)) from exc
            if not exc.retryable:
                return JobResult(False, attempt)
            continue

        last_output = completion.text
        record.update(
            output=completion.text[:MAX_STORED_OUTPUT_CHARS],
            cost_egp=completion.cost(info),
            prompt_tokens=completion.prompt_tokens,
            completion_tokens=completion.completion_tokens,
            cached_tokens=completion.cached_tokens,
            latency_s=round(completion.latency_s, 3),
            request_id=completion.request_id,
            truncated=completion.completion_tokens >= task.max_tokens,
        )

        try:
            result = await grade(task.grader, completion.text, ctx)
        except (APIError, GraderUnavailable) as exc:
            record.update(error=f"grader: {exc}", error_kind="grader")
            write_record(sink, record)
            if isinstance(exc, APIError) and exc.fatal:
                raise FatalRunError(str(exc)) from exc
            return JobResult(False, attempt, last_output)

        record.update(
            passed=result.passed, score=round(result.score, 4),
            detail=result.detail, grading_cost_egp=result.cost_egp,
            criteria=[
                {
                    "criterion": c.criterion, "required": c.required, "passed": c.passed,
                    "probability": round(c.probability, 4), "unanimous": c.unanimous,
                    "by_judge": {name: round(p, 4) for name, p in c.by_judge.items()},
                    "reason": c.reason[:300],
                }
                for c in result.criteria
            ],
        )
        write_record(sink, record)
        if result.passed:
            return JobResult(True, attempt, completion.text)
    return JobResult(False, config.max_attempts, last_output)


def write_record(sink: TextIO, record: dict[str, Any]) -> None:
    sink.write(json.dumps(record, ensure_ascii=False) + "\n")
    sink.flush()


def load_attempts(run_dir: Path) -> list[dict[str, Any]]:
    path = run_dir / "attempts.jsonl"
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
