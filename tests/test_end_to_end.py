import asyncio
import io
import json
from pathlib import Path

import pytest

from taskcost.config import load_config
from taskcost.graders import Assessment, GradeContext, grade, parse_criteria
from taskcost.metrics import common_failures, summarize
from taskcost.mock import MockBackend, mock_config
from taskcost.report import write_reports
from taskcost.runner import load_attempts, run_benchmark
from taskcost.tasks import load_tasks

ROOT = Path(__file__).resolve().parent.parent
TASKS = load_tasks(ROOT / "tasks")


class PassingJudge:
    async def assess(self, *, criteria, **_):
        return Assessment([{"stub": 1.0} for _ in criteria], [""] * len(criteria), 0.0)


@pytest.mark.parametrize("task", TASKS, ids=[t.id for t in TASKS])
def test_task_is_well_formed(task):
    assert task.reference, "every bundled task needs a reference answer"
    assert task.counterexamples, "every bundled task needs counterexamples to calibrate the judges"
    if task.grader["type"] == "rubric":
        assert parse_criteria(task.grader["criteria"])
    result = asyncio.run(grade(task.grader, task.reference, GradeContext(task=task, judge=PassingJudge())))
    assert result.passed, result.detail


def test_mock_run_produces_reports(tmp_path):
    config = mock_config(load_config(ROOT / "benchmark.yaml"))
    run_dir = asyncio.run(
        run_benchmark(config, TASKS, MockBackend(TASKS), run_dir=tmp_path / "run", log=io.StringIO())
    )
    records = load_attempts(run_dir)
    assert {r["model"] for r in records} == {"mock-frontier", "mock-balanced", "mock-budget"}
    assert all(r["criteria"] for r in records if r["error"] is None)

    summaries = summarize(records, config.min_success_rate)
    assert all(s.runs == len(TASKS) * config.trials for s in summaries)
    assert all(s.grading_cost_egp > 0 for s in summaries)
    assert all(s.judge_agreement == 1.0 for s in summaries)

    failures = common_failures(records)
    assert failures and failures[0]["runs"] >= failures[-1]["runs"]

    config_dict = json.loads((run_dir / "config.json").read_text())
    paths = write_reports(run_dir, summaries, config_dict, failures)
    report = paths["markdown"].read_text()
    assert "EGP per 1k successful tasks" in report and "Most common failures" in report
