"""Judge models for rubric grading, and the panel that combines them.

Decision models (System One) return a pass probability per criterion in one cheap call. Chat models
return a pass/fail verdict with a reason per criterion. A panel mixes both to reduce single-judge bias.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, Protocol

from .client import APIError, Completion, Decision, ModelInfo
from .graders import Assessment, extract_json

GRADING_PRINCIPLE = (
    "Judge meaning, not wording or formatting: a criterion is met if a careful business reviewer "
    "would accept the response on that point. The reference answer, when given, is one correct "
    "answer; other correct phrasings, orders and formats are equally acceptable. For length limits, "
    "estimate roughly and fail only if the response is clearly outside the range."
)

CHAT_JUDGE_SYSTEM = (
    "You are a strict, fair grader for an enterprise AI benchmark. "
    + GRADING_PRINCIPLE
    + ' Reply with JSON only: {"verdicts": [{"id": 1, "pass": true, "reason": "short reason"}]}, '
    "one entry per criterion, in order."
)


class JudgeBackend(Protocol):
    async def chat(
        self, model: ModelInfo, messages: list[dict[str, Any]], max_tokens: int,
        temperature: float | None = None, extra: dict[str, Any] | None = None,
    ) -> Completion: ...

    async def decide(self, model: ModelInfo, state: Any, questions: dict[str, Any]) -> Decision: ...


@dataclass(frozen=True)
class JudgeVerdicts:
    """`None` means the judge abstained on that criterion (for example, its reply was cut off)."""

    probabilities: list[float | None]
    reasons: list[str]
    cost_egp: float


class DecisionJudge:
    def __init__(self, backend: JudgeBackend, model: ModelInfo):
        self.backend = backend
        self.model = model
        self.name = model.id

    async def evaluate(
        self, task_prompt: str, response: str, reference: str | None, criteria: list[str]
    ) -> JudgeVerdicts:
        state: dict[str, str] = {"task": task_prompt, "response": response}
        if reference:
            state["reference_answer"] = reference
        questions = {
            f"c{i}": {
                "type": "noul",
                "instructions": f"Does the `response` to the `task` meet this criterion? {text} {GRADING_PRINCIPLE}",
            }
            for i, text in enumerate(criteria, start=1)
        }
        decision = await self.backend.decide(self.model, state, questions)
        probabilities = [
            float((decision.answers.get(f"c{i}") or {}).get("noul", 0.0)) for i in range(1, len(criteria) + 1)
        ]
        return JudgeVerdicts(probabilities, [""] * len(criteria), decision.cost(self.model))


class ChatJudge:
    def __init__(self, backend: JudgeBackend, model: ModelInfo):
        self.backend = backend
        self.model = model
        self.name = model.id

    async def evaluate(
        self, task_prompt: str, response: str, reference: str | None, criteria: list[str]
    ) -> JudgeVerdicts:
        numbered = "\n".join(f"{i}. {text}" for i, text in enumerate(criteria, start=1))
        parts = [f"## Task\n{task_prompt}", f"## Response to grade\n{response}"]
        if reference:
            parts.append(f"## Reference answer\n{reference}")
        parts.append(f"## Criteria\n{numbered}")
        completion = await self.backend.chat(
            self.model,
            [{"role": "system", "content": CHAT_JUDGE_SYSTEM}, {"role": "user", "content": "\n\n".join(parts)}],
            # Reasoning judges spend hidden tokens before the verdict; a tight cap truncates it.
            max_tokens=2000 + 150 * len(criteria),
            temperature=0.0,
        )
        cost = completion.cost(self.model)

        verdicts: dict[int, dict[str, Any]] = {}
        try:
            data = extract_json(completion.text)
            items = data.get("verdicts", []) if isinstance(data, dict) else data
            for position, item in enumerate(items, start=1):
                if isinstance(item, dict):
                    verdicts[int(item.get("id", position))] = item
        except (ValueError, TypeError):
            pass
        probabilities: list[float | None] = []
        reasons = []
        for i in range(1, len(criteria) + 1):
            verdict = verdicts.get(i, {}).get("pass")
            probabilities.append(None if not isinstance(verdict, bool) else float(verdict))
            reasons.append(str(verdicts.get(i, {}).get("reason", "abstained: no verdict returned")))
        return JudgeVerdicts(probabilities, reasons, cost)


class JudgePanel:
    """Runs the judges and combines their votes. A judge that errors is skipped; all failing raises.

    With a pass threshold, the first majority of judges (list the cheap ones first) run before the rest:
    if they all pass every criterion, the majority is already decided and the rest are skipped. Anything
    less clear-cut runs the whole panel, so failures always get every judge's verdict and reasons.
    """

    def __init__(self, judges: list[DecisionJudge | ChatJudge]):
        if not judges:
            raise ValueError("a judge panel needs at least one judge")
        self.judges = judges

    @property
    def names(self) -> list[str]:
        return [judge.name for judge in self.judges]

    async def assess(
        self, *, task_prompt: str, response: str, reference: str | None, criteria: list[str],
        threshold: float | None = None,
    ) -> Assessment:
        async def run(judges: list[DecisionJudge | ChatJudge]) -> list[Any]:
            outcomes = await asyncio.gather(
                *(judge.evaluate(task_prompt, response, reference, criteria) for judge in judges), return_exceptions=True)
            for outcome in outcomes:
                if isinstance(outcome, APIError) and outcome.fatal:
                    raise outcome
            return outcomes

        majority = len(self.judges) // 2 + 1
        first, rest = self.judges[:majority], self.judges[majority:]
        if threshold is None or not rest:
            judges, outcomes = self.judges, await run(self.judges)
        else:
            judges, outcomes = first, await run(first)
            settled = all(isinstance(o, JudgeVerdicts) and all(p is not None and p >= threshold for p in o.probabilities)
                          for o in outcomes)
            if not settled:
                judges, outcomes = self.judges, outcomes + await run(rest)
        succeeded = [(j, o) for j, o in zip(judges, outcomes) if isinstance(o, JudgeVerdicts)]
        failed = [j.name for j, o in zip(judges, outcomes) if not isinstance(o, JudgeVerdicts)]
        if not succeeded:
            raise next(o for o in outcomes if isinstance(o, BaseException))

        by_judge = [
            {j.name: v.probabilities[i] for j, v in succeeded if v.probabilities[i] is not None}
            for i in range(len(criteria))
        ]
        reasons = [
            " | ".join(f"{j.name}: {v.reasons[i]}" for j, v in succeeded if v.reasons[i])
            for i in range(len(criteria))
        ]
        return Assessment(by_judge, reasons, sum(v.cost_egp for _, v in succeeded), failed)


def build_panel(backend: JudgeBackend, models: list[ModelInfo]) -> JudgePanel:
    return JudgePanel(
        [DecisionJudge(backend, m) if m.modality == "decision" else ChatJudge(backend, m) for m in models]
    )
