import asyncio
import io
import json
from dataclasses import replace
from pathlib import Path


from taskcost.calibrate import calibrate
from taskcost.client import Completion, Decision, ModelInfo, Pricing
from taskcost.config import load_config
from taskcost.graders import parse_criteria
from taskcost.profiler import QUESTIONS, decide_requirements, measure, tier_for_class
from taskcost.tasks import load_tasks

ROOT = Path(__file__).resolve().parent.parent
LIBRARY = load_tasks(ROOT / "library")
TIERS = [("Fast", "m-fast"), ("Balanced", "m-balanced"), ("Thinking", "m-thinking"), ("Frontier", "m-frontier")]


def answers(reasoning=0.5, error_cost=0.5, writing=0.5, expertise=0.5, constraints=0.5, arabic_output=0.1, dialect=0.1):
    score = lambda v: {"score": v}
    return {
        "work_type": {"choice": "extraction"}, "reasoning": score(reasoning), "error_cost": score(error_cost),
        "writing_quality": score(writing), "expertise": score(expertise), "constraints": score(constraints),
        "arabic_output": {"noul": arabic_output}, "dialect": {"noul": dialect},
    }


ENGLISH = measure("Route these tickets to the right team.")


def test_simple_task_gets_fast():
    rules = decide_requirements(answers(), ENGLISH)
    assert rules["tier_class"] == "Fast" and rules["size"] == "small" and rules["model_type"] == "instant"


def test_moderate_writing_gets_balanced():
    assert decide_requirements(answers(writing=1.6, constraints=1.4), ENGLISH)["tier_class"] == "Balanced"


def test_reasoning_gets_thinking_and_high_stakes_lowers_the_bar():
    assert decide_requirements(answers(reasoning=1.8), ENGLISH)["tier_class"] == "Thinking"
    assert decide_requirements(answers(reasoning=1.3), ENGLISH)["tier_class"] != "Thinking"
    assert decide_requirements(answers(reasoning=1.3, error_cost=1.8), ENGLISH)["tier_class"] == "Thinking"


def test_specialist_high_stakes_reasoning_gets_frontier():
    rules = decide_requirements(answers(reasoning=2.0, error_cost=1.8, expertise=1.8), ENGLISH)
    assert rules["tier_class"] == "Frontier" and rules["size"] == "large"


def test_arabic_is_measured_in_code_and_lifts_small_to_medium():
    arabic = measure("طلبي رقم 77120 وصل ناقص شاحن اللابتوب")
    assert arabic["arabic_share"] > 0.9
    rules = decide_requirements(answers(dialect=0.9), arabic)
    assert rules["language"] == "Arabic dialect" and rules["tier_class"] == "Balanced"


def test_borderline_scores_are_flagged():
    assert decide_requirements(answers(reasoning=1.55), ENGLISH)["borderline"]


def test_tier_mapping_by_position():
    assert tier_for_class("Thinking", TIERS) == ("Thinking", "m-thinking")
    assert tier_for_class("Frontier", TIERS[:2]) == ("Balanced", "m-balanced")


def test_library_has_thirteen_well_formed_tasks_three_of_them_hard():
    assert len(LIBRARY) == 13 and sum("hard" in task.tags for task in LIBRARY) == 3
    for task in LIBRARY:
        assert task.grader["type"] == "rubric" and parse_criteria(task.grader["criteria"])
        assert task.reference and len(task.counterexamples) >= 2


class AnyCatalog(dict):
    def __contains__(self, key):
        return True

    def __missing__(self, key):
        modality = "decision" if key in ("jev-1.13", "fake-judge") else "chat"
        price = {"m-fast": 1.0, "m-balanced": 5.0, "m-thinking": 20.0, "m-frontier": 100.0}.get(key, 1.0)
        return ModelInfo(key, modality, Pricing(price, price * 4))


class FakeBackend:
    """Jev says every task is simple (so it picks Fast); Fast fails the meeting task, Balanced passes it."""

    async def list_models(self):
        return AnyCatalog()

    async def chat(self, model, messages, max_tokens, temperature=None, extra=None):
        bad = model.id == "m-fast" and "meeting" in messages[-1]["content"]
        return Completion("BAD" if bad else "GOOD", 300, 100, 0, 1.0)

    async def decide(self, model, state, questions):
        if "work_type" in questions:
            return Decision(answers(), 500, 0.5)
        p = 0.95 if "GOOD" in state["response"] else 0.05
        return Decision({name: {"noul": p} for name in questions}, 300, 0.4)


def test_calibration_finds_the_cheapest_passing_tier(tmp_path):
    config = replace(load_config(ROOT / "benchmark.yaml"), tiers=tuple(TIERS), judge_models=("fake-judge",),
                     output_dir=str(tmp_path))
    tasks = [t for t in LIBRARY if t.id in ("ticket-routing", "meeting-actions")]
    run_dir = asyncio.run(calibrate(config, tasks, FakeBackend(), "jev-1.13", trials=2, log=io.StringIO(),
                                    run_dir=tmp_path / "cal"))
    summary = json.loads((run_dir / "calibration.json").read_text())
    rows = {r["task_id"]: r for r in summary["tasks"]}
    assert rows["ticket-routing"]["verdict"] == "right" and rows["ticket-routing"]["optimal"] == "Fast"
    assert rows["meeting-actions"]["verdict"] == "too_cheap" and rows["meeting-actions"]["optimal"] == "Balanced"
    assert "Thinking" not in rows["ticket-routing"]["results"]  # cheapest-first stops early
    assert summary["right"] == 1 and summary["too_cheap"] == 1
    assert "Agent 1 calibration" in (run_dir / "report.md").read_text()


def test_questions_cover_every_rule_input():
    assert {"reasoning", "error_cost", "writing_quality", "expertise", "constraints", "arabic_output", "dialect"} <= set(QUESTIONS)
