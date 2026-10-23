import asyncio
import io
import json
import random
from pathlib import Path

import pytest

from taskcost.client import APIError, Completion, Decision, ModelInfo, Pricing
from taskcost.config import load_config
from taskcost.registry import ModelCard
from taskcost.tester import (
    CASES, SCOPES, build_summary, draw_cases, expected_level, format_report, is_match, parse_scopes, probe_case,
    run_probe,
)

CONFIG = load_config(Path(__file__).resolve().parent.parent / "benchmark.yaml")
JEV = ModelInfo("jev-1.13", "decision", Pricing(2.0, 0.0))
DESIGNER = ModelInfo("designer", "chat", Pricing(10.0, 20.0))
CARDS = {"small": ModelCard("small", "Fast", 30, 1.0, 1.0, 2.0, 0.01, open_weights=True)}
DESIGN = {
    "level": "project",
    "title": "Distributor operations",
    "summary": "Purchasing.",
    "assumptions": [{"topic": "Company", "assumption": "Distributor in Egypt"}],
    "key_assumptions": ["800 invoices a month"],
    "workflows": [
        {"name": "Procure-to-Pay", "description": "Invoices", "steps": [
            {"name": "Approve", "actor": "person"},
            {"name": "Read invoice", "actor": "ai", "task_type": "extraction", "use_case": "batch",
             "phase": "run", "monthly_volume": 800, "instruction": "Extract the total.", "criteria": ["Gets the total"]},
        ]},
    ],
}
FOUR = [
    {"id": "t", "scope": "task", "request": "simple write one email"},
    {"id": "w", "scope": "workflow", "request": "workflow handle invoices"},
    {"id": "p", "scope": "project", "request": "project run purchasing and sales"},
    {"id": "s", "scope": "system", "request": "project implement an ERP"},
]


class Backend:
    """The first word of the request is the size it reports. Plans come back as one priced workflow."""

    def __init__(self, level=None, simple_p=0.85, boom=None):
        self.level = level
        self.simple_p = simple_p
        self.boom = boom
        self.chats = 0

    async def decide(self, model, state, questions):
        if self.boom and "level" in questions:
            raise self.boom
        if "level" in questions:
            level = self.level or str(state["request"]).split()[0]
            if level not in questions["level"]["criteria"]:
                level = "unclear"
            probabilities = {name: 0.05 for name in questions["level"]["criteria"]}
            probabilities[level] = self.simple_p
            return Decision({
                "level": {"choice": level, "probabilities": probabilities},
                "use_case": {"choice": "batch", "probabilities": {"batch": 0.8}},
                "domain": {"choice": "logistics", "probabilities": {"logistics": 0.6}},
            }, 100, 0.2)
        return Decision({"needs_stronger": {"noul": 0.1}}, 50, 0.1)

    async def chat(self, model, messages, max_tokens, temperature=None, extra=None):
        self.chats += 1
        return Completion(json.dumps(DESIGN), 100, 40, 0, 1.0)


def test_catalog_has_a_request_for_every_scope():
    assert [c["id"] for c in CASES] == list(dict.fromkeys(c["id"] for c in CASES))
    assert len({c["request"] for c in CASES}) == len(CASES)
    for scope in SCOPES:
        assert sum(c["scope"] == scope for c in CASES) >= 4
    assert expected_level("system") == "project"
    assert parse_scopes("task, task, system") == ("task", "system")
    with pytest.raises(ValueError):
        parse_scopes("department")


def test_a_system_matches_only_when_read_as_a_project():
    assert is_match("system", {"level": "project", "is_simple": False})
    assert not is_match("system", {"level": "workflow", "is_simple": False})
    assert is_match("task", {"level": "simple", "is_simple": True})
    assert not is_match("task", {"level": "simple", "is_simple": False})


def test_draws_are_spread_across_scopes_and_repeatable():
    cases = [{"scope": scope, "request": f"{scope}-{i}"} for scope in ("task", "workflow") for i in range(3)]
    first = draw_cases(cases, 4, random.Random(1), ("task", "workflow"), set())
    assert [c["request"] for c in first] == [c["request"] for c in draw_cases(cases, 4, random.Random(1), ("task", "workflow"), set())]
    assert sum(c["scope"] == "task" for c in first) == 2
    assert len({c["request"] for c in first}) == 4
    used = {c["request"] for c in first}
    more = draw_cases(cases, 4, random.Random(2), ("task", "workflow"), used)
    assert sum(c["request"] not in used for c in more) == 2


def test_sizing_reads_each_scope_without_designing_a_plan(tmp_path):
    backend = Backend()
    result = asyncio.run(run_probe(
        backend, jev=JEV, config=CONFIG, count=4, rounds=1, scopes=SCOPES, depth="size", seed=3,
        out_dir=tmp_path, cases=FOUR, log=io.StringIO(),
    ))
    assert backend.chats == 0
    assert result["summary"]["matched"] == 4
    assert {r["scope"]: r["size"]["level"] for r in result["records"]} == {
        "task": "simple", "workflow": "workflow", "project": "project", "system": "project"}


def test_a_run_writes_the_report_and_prices_what_was_not_simple(tmp_path):
    backend = Backend()
    result = asyncio.run(run_probe(
        backend, jev=JEV, designer=DESIGNER, cards=CARDS, config=CONFIG, count=4, rounds=1,
        scopes=SCOPES, depth="budget", seed=3, out_dir=tmp_path, cases=FOUR, log=io.StringIO(),
    ))
    by_scope = {r["scope"]: r for r in result["records"]}
    assert by_scope["task"]["budget"]["kind"] == "simple"
    assert by_scope["task"]["budget"]["cost_per_1000_egp"] == pytest.approx(10)
    assert by_scope["task"]["budget"]["model"] == "small"
    for scope in ("workflow", "project", "system"):
        assert by_scope[scope]["budget"]["monthly_cost_egp"] == pytest.approx(8)
        assert by_scope[scope]["budget"]["first_year_cost_egp"] == pytest.approx(96)
        assert by_scope[scope]["matched"]
    assert backend.chats == 3
    report = (result["folder"] / "report.md").read_text(encoding="utf-8")
    assert "Matched 4/4" in report and "implement an ERP" in report
    saved = [json.loads(line) for line in (result["folder"] / "cases.jsonl").read_text(encoding="utf-8").splitlines()]
    assert len(saved) == 4


def test_an_unsure_task_is_a_miss_and_gets_a_plan_anyway():
    backend = Backend(level="simple", simple_p=0.4)
    record = asyncio.run(probe_case(
        backend, JEV, {"id": "t", "scope": "task", "request": "write one email"}, depth="budget",
        designer=DESIGNER, cards=CARDS,
    ))
    assert record["size"]["level"] == "simple" and not record["matched"]
    assert record["budget"]["kind"] == "blueprint" and backend.chats == 1


def test_a_system_read_as_a_workflow_is_a_miss():
    record = asyncio.run(probe_case(
        Backend(level="workflow"), JEV, {"id": "s", "scope": "system", "request": "install an ERP"},
        depth="size",
    ))
    assert not record["matched"] and record["expected_level"] == "project"
    summary = build_summary([record])
    assert summary["confusion"]["system"] == {"workflow": 1}
    assert "miss" in format_report([record], {"depth": "size", "seed": 1, "profiler_model": "jev"}, summary)


def test_a_failed_read_is_kept_and_a_fatal_api_error_stops_the_case():
    failed = asyncio.run(probe_case(
        Backend(boom=RuntimeError("nope")), JEV, FOUR[0], depth="size",
    ))
    assert failed["error"].startswith("sizing failed") and not failed["matched"]
    with pytest.raises(APIError):
        asyncio.run(probe_case(Backend(boom=APIError(401, "unauthorized", "no")), JEV, FOUR[0], depth="size"))


def test_later_rounds_reuse_a_request_only_after_the_catalog_is_used(tmp_path):
    backend = Backend()
    result = asyncio.run(run_probe(
        backend, jev=JEV, config=CONFIG, count=1, rounds=2, scopes=("task",), depth="size", seed=1,
        out_dir=tmp_path, cases=[{"id": "t", "scope": "task", "request": "simple hello"}], log=io.StringIO(),
    ))
    assert [r["repeat"] for r in result["records"]] == [False, True]


def test_budget_needs_a_measured_registry(tmp_path):
    with pytest.raises(ValueError, match="calibrate"):
        asyncio.run(run_probe(
            Backend(), jev=JEV, designer=DESIGNER, cards={}, config=CONFIG, count=1, rounds=1,
            scopes=("task",), depth="budget", seed=1, out_dir=tmp_path, cases=FOUR[:1], log=io.StringIO(),
        ))
