"""The rules that decide passes, fails and winners."""

import asyncio
import json

from taskcost.client import Completion, ModelInfo, Pricing
from taskcost.graders import GradeContext, grade
from taskcost.judges import ChatJudge, FactJudge, JudgePanel, JudgeVerdicts
from taskcost.quick import speed_verdict
from taskcost.registry import MIN_SHORT_RUNS
from taskcost.tasks import Task

TASK = Task(id="t", category="email", prompt="Write the email.", grader={"type": "rubric", "criteria": ["a", "b"]})


class Fixed:
    def __init__(self, name, probabilities):
        self.name, self.probabilities = name, probabilities

    async def evaluate(self, task_prompt, response, reference, criteria):
        return JudgeVerdicts(self.probabilities, [""] * len(criteria), 0.0)


def graded(*judges):
    ctx = GradeContext(task=TASK, judge=JudgePanel(list(judges)), judge_threshold=0.8)
    return asyncio.run(grade(TASK.grader, "answer", ctx))


def test_a_missing_verdict_is_not_reviewed_not_failed():
    result = graded(Fixed("only", [None, None]))
    assert not result.passed and result.unreviewed == ("a", "b")
    assert all(not c.reviewed for c in result.criteria) and "not reviewed" in result.detail and "failed:" not in result.detail


def test_a_reviewed_fail_still_fails_even_if_another_check_is_unreviewed():
    result = graded(Fixed("only", [0.1, None]))
    assert not result.passed and result.unreviewed == ("b",) and "failed: a" in result.detail


def test_a_tie_between_two_reviewers_is_not_a_majority():
    result = graded(Fixed("model", [1.0, 1.0]), Fixed("fact-check", [0.0, None]))
    a, b = result.criteria
    assert not a.passed and b.passed  # 1 of 2 isn't a majority; on b the fact check abstained, so the model decides


def test_speed_uses_the_same_rule_for_every_model():
    many = {"short_runs": MIN_SHORT_RUNS + 5, "latency_p90_short_s": 6.6, "runs": 80, "latency_p90_s": 14.0}
    few = {"short_runs": 2, "latency_p90_short_s": 2.0, "runs": 3, "latency_p90_s": 3.0}
    slow_history = speed_verdict(many, [4.1], "chat")
    assert slow_history["basis"] == "history" and slow_history["p90_s"] == 6.6 and not slow_history["fast_enough"]
    assert slow_history["this_run_s"] == 4.1  # shown next to the history figure
    unknown = speed_verdict(few, [4.4], "chat")
    assert unknown["basis"] == "this run" and unknown["fast_enough"] and unknown["answers"] == 0
    assert speed_verdict({}, [30.0], "batch")["fast_enough"]  # batch has no limit


def test_strict_single_reviewer_is_backed_by_the_fact_check():
    model_says_pass = Fixed("SovereignEG/Qwen", [1.0, 1.0])
    fact = FactJudge()
    task = Task(id="t", category="email", prompt="p",
                grader={"type": "rubric", "criteria": ["States order #MS-2024-08847", "Is polite"]})
    ctx = GradeContext(task=task, judge=JudgePanel([model_says_pass, fact]), judge_threshold=0.8)
    wrong = asyncio.run(grade(task.grader, "Sorry about order MS-2024-99999.", ctx))
    right = asyncio.run(grade(task.grader, "Sorry about order MS-2024-08847.", ctx))
    assert not wrong.passed and right.passed  # the self-graded pass on a wrong order number is caught


def test_a_reviewer_that_returns_no_verdict_is_asked_again_without_reasoning():
    calls = []

    class Backend:
        async def chat(self, model, messages, max_tokens, temperature=None, extra=None):
            calls.append(extra)
            if extra is None:
                return Completion("", 100, 4000, 0, 20.0)  # reasoned, then wrote nothing
            return Completion(json.dumps({"verdicts": [{"id": 1, "pass": True, "reason": "ok"}]}), 100, 50, 0, 1.0)

    verdicts = asyncio.run(ChatJudge(Backend(), ModelInfo("qwen", "chat", Pricing(1, 1))).evaluate("t", "r", None, ["a"]))
    assert calls == [None, {"reasoning_effort": "none"}] and verdicts.probabilities == [1.0]
