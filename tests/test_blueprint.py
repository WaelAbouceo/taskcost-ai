import asyncio
import json

import pytest

from taskcost.blueprint import (
    BLUEPRINT_SYSTEM, SAMPLE_SYSTEM, design_blueprint, estimate_blueprint, knowledge_summary, load_knowledge,
    size_request, verify_blueprint,
)
from taskcost.client import Completion, Decision, ModelInfo, Pricing
from taskcost.registry import ModelCard

JEV = ModelInfo("jev-1.13", "decision", Pricing(2.47, 0.0))
DESIGNER = ModelInfo("designer", "chat", Pricing(100.0, 500.0))
CARDS = {
    "small": ModelCard("small", "Fast", 30, 1.0, 7.0, 13.8, 0.005),
    "mini": ModelCard("mini", "Balanced", 30, 1.0, 1.4, 2.9, 0.05),
    "big": ModelCard("big", "Frontier", 3, 1.0, 27.0, 34.0, 3.0),
}

DESIGN = {
    "level": "project",
    "title": "ERP for a mid-sized distributor",
    "summary": "Two workflows.",
    "assumptions": [{"topic": "Company", "assumption": "Mid-sized distributor in Egypt, 500 employees"}],
    "workflows": [
        {"name": "Procure-to-Pay", "description": "Buying and paying suppliers", "steps": [
            {"name": "Purchase request", "actor": "person"},
            {"name": "Read supplier invoice", "actor": "ai", "task_type": "extraction", "use_case": "batch",
             "monthly_volume": 1200, "instruction": "Extract the invoice fields.", "criteria": ["Gets the total"]},
            {"name": "Answer supplier", "actor": "ai", "task_type": "nonsense", "use_case": "chat",
             "monthly_volume": "300", "instruction": "Reply to the supplier query.", "criteria": ["Polite"]},
        ]},
        {"name": "Order-to-Cash", "description": "Selling and collecting", "steps": [
            {"name": "Ship goods", "actor": "person"},
            {"name": "Payment reminder", "actor": "ai", "task_type": "generation", "use_case": "batch",
             "monthly_volume": 250, "instruction": "Write a payment reminder.", "criteria": ["Has the amount"]},
        ]},
    ],
}


class FakeBackend:
    def __init__(self, level="project", replies=None):
        self.level = level
        self.replies = list(replies or [json.dumps(DESIGN)])

    async def list_models(self):
        return {}

    async def chat(self, model, messages, max_tokens, temperature=None, extra=None):
        assert messages[0]["content"] == BLUEPRINT_SYSTEM
        return Completion(self.replies.pop(0) if self.replies else json.dumps(DESIGN), 3000, 2000, 0, 5.0)

    async def decide(self, model, state, questions):
        if "level" in questions:
            probabilities = {k: (0.9 if k == self.level else 0.1 / 3) for k in questions["level"]["criteria"]}
            return Decision({"level": {"choice": self.level, "probabilities": probabilities}}, 200, 0.5)
        return Decision({"needs_stronger": {"noul": 0.1}}, 300, 0.4)

    async def aclose(self):
        pass


def test_sizing_also_guesses_the_use_case():
    class Backend(FakeBackend):
        async def decide(self, model, state, questions):
            assert set(questions) == {"level", "use_case", "domain"}
            return Decision({"level": {"choice": "simple", "probabilities": {"simple": 0.9}},
                             "use_case": {"choice": "chat", "probabilities": {"chat": 0.8, "batch": 0.2}}}, 200, 0.5)

    size = asyncio.run(size_request(Backend(), JEV, "a chatbot reply to a complaint"))
    assert size["use_case"] == "chat" and size["use_case_probabilities"]["chat"] == 0.8
    assert asyncio.run(size_request(FakeBackend("simple"), JEV, "x"))["use_case"] == "realtime"  # no answer: the default


def test_sizing_sends_only_confident_simple_requests_to_the_simple_flow():
    assert asyncio.run(size_request(FakeBackend("simple"), JEV, "write an email"))["is_simple"]
    assert not asyncio.run(size_request(FakeBackend("project"), JEV, "an ERP system"))["is_simple"]
    assert not asyncio.run(size_request(FakeBackend("unclear"), JEV, "stuff"))["is_simple"]


def test_knowledge_reference_covers_standard_processes():
    summary = knowledge_summary(load_knowledge())
    assert "Procure-to-Pay" in summary and "ERP:" in summary and "about 1200/month" in summary


def test_design_cleans_the_blueprint():
    blueprint = asyncio.run(design_blueprint(FakeBackend(), DESIGNER, "an ERP system"))
    w1, w2 = blueprint["workflows"]
    assert [s["id"] for s in w1["steps"]] == ["W1.S1", "W1.S2", "W1.S3"]
    assert w1["steps"][2]["task_type"] == "other" and w1["steps"][2]["monthly_volume"] == 300
    assert blueprint["design"]["cost_egp"] == pytest.approx((3000 * 100 + 2000 * 500) / 1e6)


def test_design_retries_once_on_a_bad_reply():
    backend = FakeBackend(replies=["not json", json.dumps(DESIGN)])
    assert asyncio.run(design_blueprint(backend, DESIGNER, "an ERP system"))["workflows"]


class FakeVerifyBackend(FakeBackend):
    """Writes examples; the cheapest model fails the supplier reply, a stronger one passes everything."""

    broken_examples = False

    async def chat(self, model, messages, max_tokens, temperature=None, extra=None):
        if messages[0]["content"] == SAMPLE_SYSTEM:
            answer = "Total 5,000" if self.broken_examples else "GOOD total 5,000"
            sample = {"input": "INVOICE 77 ... total EGP 5,000", "expected_answer": answer,
                      "alternative_answer": None if self.broken_examples else "GOOD | total | 5,000",
                      "criteria": ["Gets the total", "Gets the invoice number"]}
            return Completion(json.dumps(sample), 500, 300, 0, 2.0)
        prompt = messages[-1]["content"]
        bad = model.id == "small" and "supplier query" in prompt
        return Completion("BAD" if bad else "GOOD answer", 400, 150, 0, 1.0)

    async def decide(self, model, state, questions):
        p = 0.95 if "GOOD" in str(state.get("response", "")) else 0.05
        return Decision({name: {"noul": p} for name in questions}, 300, 0.3)


def verification_setup():
    from dataclasses import replace
    from pathlib import Path

    from taskcost.config import load_config

    config = replace(load_config(Path(__file__).resolve().parent.parent / "benchmark.yaml"),
                     judge_models=("fake-judge",), concurrency=2, blueprint_model="designer", sample_model=None)
    catalog = {
        "designer": DESIGNER, "fake-judge": ModelInfo("fake-judge", "decision", Pricing(2.0, 0.0)),
        **{c.id: ModelInfo(c.id, "chat", Pricing(1.0 if c.id == "small" else 10.0, 4.0)) for c in CARDS.values()},
    }
    blueprint = asyncio.run(design_blueprint(FakeBackend(), DESIGNER, "an ERP system"))
    return config, catalog, blueprint


def test_verification_measures_cheapest_first(tmp_path):
    import io

    config, catalog, blueprint = verification_setup()
    measured = asyncio.run(verify_blueprint(config, blueprint, FakeVerifyBackend(), catalog,
                                            {k: CARDS[k] for k in ("small", "mini")}, tmp_path / "v", io.StringIO()))
    steps = {s["name"]: s for w in measured["workflows"] for s in w["steps"] if s["actor"] == "ai"}
    invoice, supplier = steps["Read supplier invoice"], steps["Answer supplier"]
    assert invoice["measured"]["model"] == "small" and [t["model"] for t in invoice["tried"]] == ["small"]
    assert supplier["use_case"] == "chat" and supplier["measured"]["model"] == "mini"  # small is too slow for chat
    assert invoice["sample"]["input"].startswith("INVOICE")
    v = measured["verification"]
    assert v["measured_steps"] == 3 and v["ai_steps"] == 3 and v["cost_egp"]["models"] > 0
    assert invoice["measured"]["monthly_cost_egp"] == pytest.approx(1200 * invoice["measured"]["cost_per_answer_egp"])
    assert invoice["sample_check"]["passed"] and invoice["verdict"] == "measured"
    assert invoice["sample"]["alternative_answer"] == "GOOD | total | 5,000"


def test_a_broken_example_is_not_used_to_judge_models(tmp_path):
    import io

    config, catalog, blueprint = verification_setup()
    backend = FakeVerifyBackend()
    backend.broken_examples = True  # the example's own correct answer fails its checks
    measured = asyncio.run(verify_blueprint(config, blueprint, backend, catalog, CARDS, tmp_path / "v", io.StringIO()))
    steps = [s for w in measured["workflows"] for s in w["steps"] if s["actor"] == "ai"]
    assert all(s["verdict"] == "no_reliable_test" and s["tried"] == [] for s in steps)
    assert measured["verification"]["measured_steps"] == 0


def test_remeasuring_one_step_keeps_the_others(tmp_path):
    import io

    config, catalog, blueprint = verification_setup()
    first = asyncio.run(verify_blueprint(config, blueprint, FakeVerifyBackend(), catalog, CARDS, tmp_path / "v", io.StringIO()))
    invoice_id = next(s["id"] for w in first["workflows"] for s in w["steps"] if s["name"] == "Read supplier invoice")
    lines_before = (tmp_path / "v" / "attempts.jsonl").read_text().splitlines()
    first_cost = first["verification"]["cost_egp"]["models"]
    again = asyncio.run(verify_blueprint(config, first, FakeVerifyBackend(), catalog, CARDS, tmp_path / "v", io.StringIO(),
                                         only_steps={invoice_id}))
    records = [json.loads(line) for line in (tmp_path / "v" / "attempts.jsonl").read_text().splitlines()]
    assert len(records) == len(lines_before)  # the step's old runs were replaced, not added to
    assert again["verification"]["measured_steps"] == 3
    assert again["verification"]["cost_egp"]["models"] > first_cost


def test_generated_checks_tolerate_other_formats():
    from taskcost.blueprint import FORMAT_NOTE, format_tolerant

    assert format_tolerant("The date is extracted as '2024-05-15'.").endswith(f"{FORMAT_NOTE}.")
    assert format_tolerant(format_tolerant("x")).count(FORMAT_NOTE) == 1


def test_frontier_is_skipped_unless_asked(tmp_path):
    import io

    config, catalog, blueprint = verification_setup()
    measured = asyncio.run(verify_blueprint(config, blueprint, FakeVerifyBackend(), catalog, CARDS, tmp_path / "v", io.StringIO()))
    tried = {t["model"] for w in measured["workflows"] for s in w["steps"] for t in s.get("tried", [])}
    assert "big" not in tried


def test_estimate_prices_every_ai_step_by_volume():
    blueprint = asyncio.run(design_blueprint(FakeBackend(), DESIGNER, "an ERP system"))
    estimated = asyncio.run(estimate_blueprint(blueprint, FakeBackend(), JEV, CARDS, escalate_at=0.5))
    invoice, supplier = [s for s in estimated["workflows"][0]["steps"] if s["actor"] == "ai"]
    assert invoice["model"] == "small" and invoice["monthly_cost_egp"] == pytest.approx(1200 * 0.005)
    assert supplier["model"] == "mini"  # a chatbot step needs a fast model
    assert estimated["monthly_cost_egp"] == pytest.approx(1200 * 0.005 + 300 * 0.05 + 250 * 0.005)
    assert estimated["strongest_model"] == "big"
    assert estimated["strongest_monthly_cost_egp"] == pytest.approx((1200 + 300 + 250) * 3.0)


def test_build_steps_cost_once_and_run_steps_every_month():
    from taskcost.blueprint import _clean, recompute_totals

    data = {"level": "project", "workflows": [
        {"name": "Setup and migration", "steps": [
            {"name": "Legacy invoices migrated", "actor": "ai", "task_type": "extraction", "use_case": "batch",
             "phase": "build", "total_volume": 60000, "instruction": "Extract the fields.", "criteria": ["a"]}]},
        {"name": "Run", "steps": [
            {"name": "New invoice read", "actor": "ai", "task_type": "extraction", "use_case": "batch",
             "monthly_volume": 1200, "instruction": "Extract the fields.", "criteria": ["a"]}]},
    ]}
    blueprint = _clean(data)
    build, run = blueprint["workflows"][0]["steps"][0], blueprint["workflows"][1]["steps"][0]
    assert (build["phase"], build["total_volume"], build["monthly_volume"]) == ("build", 60000, 0)
    assert (run["phase"], run["monthly_volume"]) == ("run", 1200) and "total_volume" not in run
    for step in (build, run):
        step["cost_per_answer_egp"] = 0.01
    blueprint["estimate"] = {"registry": {"big": {"cost_per_answer_egp": 0.1, "pass_rate": 1.0}}}
    blueprint["strongest_model"] = "big"
    totals = recompute_totals(blueprint)
    assert totals["one_time_cost_egp"] == pytest.approx(600) and totals["monthly_cost_egp"] == pytest.approx(12)
    assert totals["first_year_cost_egp"] == pytest.approx(600 + 12 * 12)
    assert totals["strongest_one_time_cost_egp"] == pytest.approx(6000) and build["monthly_cost_egp"] == 0.0
