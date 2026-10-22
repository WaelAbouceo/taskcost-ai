import asyncio

import pytest

from taskcost.graders import GradeContext, extract_json, grade, resolve_path
from taskcost.judges import JudgePanel, JudgeVerdicts
from taskcost.tasks import Task

TASK = Task(id="t", category="c", prompt="p", grader={}, reference="ref")


def run(spec, output, judge=None):
    return asyncio.run(grade(spec, output, GradeContext(task=TASK, judge=judge)))


class FakeJudge:
    """Returns fixed per-criterion probabilities."""

    def __init__(self, name, probabilities, cost=0.001, fail=False):
        self.name = name
        self.probabilities = probabilities
        self.cost = cost
        self.fail = fail
        self.seen_reference = None

    async def evaluate(self, task_prompt, response, reference, criteria):
        if self.fail:
            raise RuntimeError("judge down")
        self.seen_reference = reference
        return JudgeVerdicts(self.probabilities[: len(criteria)], ["r"] * len(criteria), self.cost)


def panel(*judges):
    return JudgePanel(list(judges))


RUBRIC = {"type": "rubric", "criteria": ["first", "second", {"text": "nice to have", "required": False}]}


def test_rubric_passes_when_all_required_criteria_pass():
    judge = FakeJudge("a", [0.9, 0.95, 0.1])
    result = run(RUBRIC, "answer", panel(judge))
    assert result.passed
    assert judge.seen_reference == "ref"
    assert [c.passed for c in result.criteria] == [True, True, False]
    assert result.score == pytest.approx((0.9 + 0.95 + 0.1) / 3)


def test_rubric_fails_on_a_required_criterion():
    result = run(RUBRIC, "answer", panel(FakeJudge("a", [0.9, 0.3, 0.9])))
    assert not result.passed and "second" in result.detail


def test_panel_majority_vote_and_agreement():
    result = run(
        RUBRIC,
        "answer",
        panel(FakeJudge("a", [0.9, 0.9, 0.9]), FakeJudge("b", [0.9, 0.1, 0.9]), FakeJudge("c", [0.2, 0.9, 0.9])),
    )
    assert result.passed
    assert [c.unanimous for c in result.criteria] == [False, False, True]
    assert result.cost_egp == pytest.approx(0.003)


def test_two_judge_tie_fails_the_criterion():
    result = run(RUBRIC, "answer", panel(FakeJudge("a", [0.9, 0.9, 0.9]), FakeJudge("b", [0.1, 0.9, 0.9])))
    assert not result.passed


def test_panel_skips_a_failing_judge():
    result = run(RUBRIC, "answer", panel(FakeJudge("a", [0.9, 0.9, 0.9]), FakeJudge("b", [], fail=True)))
    assert result.passed and "judges unavailable" in result.detail


def test_abstaining_judge_does_not_vote():
    result = run(
        RUBRIC,
        "answer",
        panel(FakeJudge("a", [0.9, 0.9, 0.9]), FakeJudge("b", [0.9, 0.9, 0.9]), FakeJudge("c", [None, None, None])),
    )
    assert result.passed
    assert all(set(c.by_judge) == {"a", "b"} for c in result.criteria)


def test_criterion_fails_when_every_judge_abstains():
    result = run(RUBRIC, "answer", panel(FakeJudge("a", [None, 0.9, 0.9])))
    assert not result.passed and result.criteria[0].probability == 0.0


def test_panel_raises_when_every_judge_fails():
    with pytest.raises(RuntimeError):
        run(RUBRIC, "answer", panel(FakeJudge("a", [], fail=True)))


def test_llm_judge_is_a_single_criterion_rubric_without_reference():
    judge = FakeJudge("a", [0.85])
    result = run({"type": "llm_judge", "rubric": "r"}, "x", panel(judge))
    assert result.passed and judge.seen_reference is None


def test_all_of_skips_judges_after_cheap_failure():
    judge = FakeJudge("a", [1.0])
    spec = {"type": "all_of", "graders": [{"type": "max_words", "limit": 2}, {"type": "rubric", "criteria": ["r"]}]}
    result = run(spec, "one two three", panel(judge))
    assert not result.passed and result.cost_egp == 0.0 and judge.seen_reference is None


# Deterministic graders remain available for hard requirements.

def test_extract_json_from_fenced_block_with_prose():
    assert extract_json('Here you go:\n```json\n{"a": 1}\n```\nThanks') == {"a": 1}


def test_resolve_path_with_wildcard():
    assert resolve_path({"items": [{"owner": "A"}, {"owner": "B"}]}, "items[*].owner") == ["A", "B"]


def test_json_fields_numbers_strings_and_alternatives():
    spec = {
        "type": "json_fields",
        "fields": {"total": 18582, "id": "INV-2026-0417", "vendor": {"any_of": ["Nile", "Nile Co"]}},
    }
    assert run(spec, '{"total": "EGP 18,582.00", "id": "inv 2026 0417", "vendor": "nile co"}').passed


def test_json_list_is_order_insensitive_but_exact_size():
    spec = {"type": "json_fields", "fields": {"v": ["C2", "C4"]}}
    assert run(spec, '{"v": ["C4", "C2"]}').passed
    assert not run(spec, '{"v": ["C4", "C2", "C5"]}').passed


def test_contains_all_with_alternatives_and_forbidden():
    spec = {"type": "contains_all", "terms": ["4517", ["7", "seven"]], "forbidden": ["late fee"]}
    assert run(spec, "Invoice 4517 due in seven days").passed
    assert not run(spec, "Invoice 4517 due in seven days plus a late fee").passed
