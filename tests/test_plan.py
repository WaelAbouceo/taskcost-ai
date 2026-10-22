import asyncio
import io
import json
from pathlib import Path

import pytest

from taskcost.client import Completion, ModelInfo, Pricing
from taskcost.config import load_config
from taskcost.design import design_plan
from taskcost.economics import plan_economics
from taskcost.execute import execute_plan
from taskcost.metrics import common_failures, summarize
from taskcost.mock import MockBackend, mock_config
from taskcost.plan import Plan, PlanError, PlanNode, build_task
from taskcost.report import render_plan_report
from taskcost.runner import load_attempts

ROOT = Path(__file__).resolve().parent.parent
FIXTURE = ROOT / "tests" / "fixtures" / "contract_plan.yaml"
CONTRACT = ROOT / "examples" / "contracts" / "msa-delta-nilecloud.txt"


def simple(task_id, depends_on=(), **extra):
    return PlanNode(id=task_id, title=task_id, kind="simple", instruction=f"do {task_id}",
                    criteria=["c"], depends_on=list(depends_on), **extra)


def make_plan(*nodes):
    return Plan(request="r", level="complex", deliverable="d",
                root=PlanNode(id="J", title="job", kind="complex", children=list(nodes)))


def test_fixture_plan_loads_and_orders_dependencies():
    plan = Plan.load(FIXTURE)
    order = [n.id for n in plan.ordered_tasks()]
    assert order.index("T2") < order.index("T3") < order.index("T4")
    assert order.index("T1") < order.index("T4")


def test_validation_catches_cycles_and_unknown_dependencies():
    with pytest.raises(PlanError, match="cycle"):
        make_plan(simple("T1", ["T2"]), simple("T2", ["T1"])).validate()
    with pytest.raises(PlanError, match="unknown"):
        make_plan(simple("T1", ["T9"])).validate()


def test_build_task_includes_documents_and_earlier_results():
    plan = Plan.load(FIXTURE)
    documents = plan.load_documents()
    extraction = build_task(plan, plan.node("T2"), documents, {})
    assert "Nile Cloud Systems" in extraction.prompt and extraction.grader["type"] == "rubric"
    calculation = build_task(plan, plan.node("T3"), documents, {"T2": "EGP 145,000 per quarter"})
    assert "EGP 145,000 per quarter" in calculation.prompt
    assert "Nile Cloud Systems" not in calculation.prompt  # needs_documents: false


class FakeDesignModel:
    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = 0

    async def chat(self, model, messages, max_tokens, temperature=None, extra=None):
        self.calls += 1
        return Completion(self.replies.pop(0), 1000, 500, 0, 1.0)


DESIGN_MODEL = ModelInfo("designer", "chat", Pricing(100.0, 400.0))
VALID_DESIGN = {
    "level": "complex",
    "title": "Contract extraction",
    "deliverable": "data sheet",
    "children": [
        {"id": "T1", "kind": "simple", "title": "Parties", "task_type": "extraction",
         "instruction": "Find the parties", "criteria": ["Names both parties"], "expected_answer": "A and B"},
        {"id": "T2", "kind": "simple", "title": "Sheet", "task_type": "assembly", "depends_on": ["T1"],
         "needs_documents": False, "instruction": "Combine", "criteria": ["Includes T1"]},
    ],
}


def test_design_agent_builds_a_plan_and_counts_its_cost(tmp_path):
    backend = FakeDesignModel([json.dumps(VALID_DESIGN)])
    plan = asyncio.run(design_plan(backend, DESIGN_MODEL, "Extract", {"contract": CONTRACT}, tmp_path))
    assert [n.id for n in plan.simple_tasks()] == ["T1", "T2"]
    assert plan.design["cost_egp"] == pytest.approx((1000 * 100 + 500 * 400) / 1e6)
    saved = Plan.load(plan.save(tmp_path / "plan.yaml"))
    assert "Nile Cloud Systems" in saved.load_documents()["contract"]


def test_design_agent_retries_once_on_an_invalid_plan(tmp_path):
    broken = {**VALID_DESIGN, "children": [{**VALID_DESIGN["children"][1]}]}  # depends on missing T1
    backend = FakeDesignModel([json.dumps(broken), json.dumps(VALID_DESIGN)])
    plan = asyncio.run(design_plan(backend, DESIGN_MODEL, "Extract", {}, tmp_path))
    assert backend.calls == 2 and len(plan.simple_tasks()) == 2


def record(model, task, trial, passed, cost, error_kind=None):
    return {"model": model, "model_id": model, "task_id": task, "category": "extraction", "trial": trial,
            "attempt": 1, "passed": passed, "cost_egp": cost, "grading_cost_egp": 0.0, "latency_s": 1.0,
            "error_kind": error_kind, "criteria": []}


def test_economics_rolls_up_jobs_and_recommends_a_mix():
    plan = make_plan(simple("T1"), simple("T2", ["T1"]))
    records = [
        record("cheap", "T1", 1, True, 0.01), record("cheap", "T2", 1, False, 0.01),
        record("cheap", "T1", 2, True, 0.01), record("cheap", "T2", 2, False, 0.01),
        record("solid", "T1", 1, True, 0.10), record("solid", "T2", 1, True, 0.10),
        record("solid", "T1", 2, False, 0.10), record("solid", "T2", 2, False, 0.0, error_kind="blocked"),
    ]
    econ = plan_economics(plan.root, records, min_success_rate=0.8)
    assert econ["jobs"]["solid"]["passed"] == 1
    assert econ["jobs"]["solid"]["cost_per_success_egp"] == pytest.approx(0.30)
    assert econ["jobs"]["cheap"]["passed"] == 0
    t2 = next(t for t in econ["tasks"] if t["task_id"] == "T2")
    assert t2["models"]["solid"]["attempted"] == 1  # the blocked run is not counted as a try
    assert econ["mix"]["assignment"] == {"T1": "cheap", "T2": "solid"}
    assert econ["mix"]["cost_per_success_egp"] == pytest.approx(0.01 + 0.10)


def test_mock_plan_execution_end_to_end(tmp_path):
    plan = Plan.load(FIXTURE)
    config = mock_config(load_config(ROOT / "benchmark.yaml"))
    run_dir = asyncio.run(execute_plan(config, plan, MockBackend.for_plan(plan), tmp_path / "run", io.StringIO()))
    records = load_attempts(run_dir)
    assert {r["task_id"] for r in records} == {"T1", "T2", "T3", "T4"}
    assert all(r["parent_id"] == "J" for r in records)

    config_dict = json.loads((run_dir / "config.json").read_text())
    econ = plan_economics(Plan.load(run_dir / "plan.yaml").root, records, config.min_success_rate)
    assert set(econ["jobs"]) == {"mock-frontier", "mock-balanced", "mock-budget"}
    report = render_plan_report(config_dict, econ, summarize(records), common_failures(records))
    assert "What the whole job costs" in report and "Best model for each simple task" in report
