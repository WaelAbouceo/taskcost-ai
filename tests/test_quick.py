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
