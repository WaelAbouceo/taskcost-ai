import asyncio
import io
import json
from dataclasses import replace
from pathlib import Path

import pytest

from taskcost.client import Completion, Decision, ModelInfo, Pricing
from taskcost.config import load_config
from taskcost.design import QUICK_SYSTEM
from taskcost.quick import quick_compare, quick_summary

ROOT = Path(__file__).resolve().parent.parent

DESIGN = {
    "title": "Delayed-order email",
    "task_type": "generation",
    "instruction": "Write an email to Mona Adel saying order #4471 will arrive 2 days late.",
    "criteria": ["Mentions order #4471", "Says the delay is 2 days", "Apologises"],
    "expected_answer": None,
    "assumptions": "Invented the customer, order number and delay.",
}


class AnyCatalog(dict):
    """Every model id exists; prices make 'cheap-*' cheap and 'pricey-*' expensive."""

    def __contains__(self, key):
        return True

    def __missing__(self, key):
        price = 1.0 if key.startswith("cheap") else 100.0
        modality = "decision" if key in ("jev-1.13", "gpt-6-luna-decisions", "fake-judge") else "chat"
        return ModelInfo(key, modality, Pricing(price, price * 4))


class FakeBackend:
    """Designer returns DESIGN; workers answer 'GOOD' unless their id contains 'bad'; judges pass 'GOOD'."""

    def __init__(self):
        self.closed = False

    async def list_models(self):
        return AnyCatalog()

    async def chat(self, model, messages, max_tokens, temperature=None, extra=None):
        if messages[0]["content"] == QUICK_SYSTEM:
            return Completion(json.dumps(DESIGN), 500, 300, 0, 1.0)
        if messages[0]["role"] == "system":  # chat judge
            ok = "GOOD" in messages[-1]["content"]
            verdicts = [{"id": i, "pass": ok, "reason": "r"} for i in range(1, 6)]
            return Completion(json.dumps({"verdicts": verdicts}), 200, 50, 0, 0.5)
        text = "BAD answer" if "bad" in model.id else "GOOD answer about order #4471"
        return Completion(text, 400, 200, 0, 1.5)

    async def decide(self, model, state, questions):
        p = 0.95 if "GOOD" in state["response"] else 0.05
        return Decision({name: {"noul": p} for name in questions}, 300, 0.4)

    async def aclose(self):
        self.closed = True


def config(tmp_path):
    return replace(load_config(ROOT / "benchmark.yaml"), output_dir=str(tmp_path / "results"),
                   judge_models=("fake-judge",))


def test_quick_compare_recommends_the_cheapest_correct_tier(tmp_path):
    tiers = [("Fast", "cheap-bad"), ("Balanced", "cheap-good"), ("Frontier", "pricey-good")]
    run_dir = asyncio.run(quick_compare(config(tmp_path), "write an email", tiers, FakeBackend(),
                                        tmp_path / "plans", trials=2, log=io.StringIO()))
    q = quick_summary(run_dir)
    assert q["title"] == "Delayed-order email" and "Invented" in q["assumptions"]
    assert [t["tier"] for t in q["tiers"]] == ["Fast", "Balanced", "Frontier"]
    fast, balanced, frontier = q["tiers"]
    assert fast["passed"] == 0 and fast["cost_per_1000_egp"] is None and fast["failed_checks"]
    assert balanced["passed"] == balanced["runs"] == 2
    assert q["recommended"] == {"tier": "Balanced", "model": "cheap-good"}
    assert q["savings_ratio"] == pytest.approx(frontier["cost_per_success_egp"] / balanced["cost_per_success_egp"])
    assert list((tmp_path / "plans").glob("quick-*.yaml"))


def test_quick_compare_needs_a_model(tmp_path):
    with pytest.raises(ValueError):
        asyncio.run(quick_compare(config(tmp_path), "x", [("Fast", "")], FakeBackend(), tmp_path / "plans",
                                  log=io.StringIO()))


def test_result_explains_every_step_with_models_time_and_cost(tmp_path):
    from taskcost.trace import quick_trace

    config = {
        "country": "EG", "sovereignty": "prefer_local", "use_case": "chat", "use_case_guess": {"chat": 0.8}, "trials": 1,
        "size": {"model": "qwen-local", "level": "simple", "probabilities": {"simple": 0.95}, "use_case_probabilities": {"chat": 0.8},
                 "domain": "logistics", "seconds": 2.0, "cost_egp": 0.01},
        "helpers": {"quick_design_model": "qwen-local", "profiler_model": "qwen-local"},
        "design": {"model": "qwen-local", "seconds": 3.0, "cost_egp": 0.05, "assumptions": "Invented an order."},
        "recommendation": {"model": "a", "note": "Agent 1 judged a small model will do.", "latency_s": 1.0, "cost_egp": 0.02},
        "judge_models": ["j1", "j2", "j3"], "timeline": {"run_s": 4.0},
    }
    tiers = [{"model": "a", "latency_s": 1.2, "cost_per_answer_egp": 0.01, "runs": 1, "passed": 1, "fast_enough": True}]
    records = [{"cost_egp": 0.01, "grading_cost_egp": 0.004,
                "criteria": [{"by_judge": {"j1": 1.0, "j2": 0.9}}]}]
    trace = quick_trace(config, tiers, records, {"model": "a"})
    titles = [s["title"] for s in trace["steps"]]
    assert titles[0] == "Understood your request" and titles[-1] == "Chose the recommendation"
    assert "logistics" in trace["steps"][0]["detail"] and "95% sure" in trace["steps"][0]["detail"]
    reviewed = next(s for s in trace["steps"] if s["title"].startswith("Reviewed"))
    assert "last one was skipped" in reviewed["detail"] and [i["model"] for i in reviewed["items"]] == ["j1", "j2", "j3"]
    assert trace["seconds"] == 10.0 and trace["cost_egp"] == pytest.approx(0.094)
    assert trace["data"]["total"] == 3 + 1 + 3  # understanding, test writing, Agent 1; the tested model; reviewers
