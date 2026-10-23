"""Judge models for rubric grading, and the panel that combines them.

Decision models (System One) return a pass probability per criterion in one cheap call. Chat models
return a pass/fail verdict with a reason per criterion. A panel mixes both to reduce single-judge bias.
"""

from __future__ import annotations

import asyncio
import re
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
        messages = [{"role": "system", "content": CHAT_JUDGE_SYSTEM}, {"role": "user", "content": "\n\n".join(parts)}]
        cost = 0.0
        verdicts: dict[int, dict[str, Any]] = {}
        # A reasoning judge can spend its budget thinking and return no verdict (or cut-off JSON): give it room,
        # and if nothing usable comes back, ask once more with reasoning off before abstaining.
        for extra in (None, {"reasoning_effort": "none"}):
            try:
                completion = await self.backend.chat(self.model, messages, max_tokens=6000 + 200 * len(criteria),
                                                     temperature=0.0, extra=extra)
            except APIError:
                if extra is None:
                    raise
                break
            cost += completion.cost(self.model)
            verdicts = _parse_verdicts(completion.text)
            if verdicts:
                break
        probabilities: list[float | None] = []
        reasons = []
        for i in range(1, len(criteria) + 1):
            verdict = verdicts.get(i, {}).get("pass")
            probabilities.append(None if not isinstance(verdict, bool) else float(verdict))
            reasons.append(str(verdicts.get(i, {}).get("reason", "abstained: no verdict returned")))
        return JudgeVerdicts(probabilities, reasons, cost)


def _parse_verdicts(text: str) -> dict[int, dict[str, Any]]:
    verdicts: dict[int, dict[str, Any]] = {}
    try:
        data = extract_json(text)
        items = data.get("verdicts", []) if isinstance(data, dict) else data
        for position, item in enumerate(items, start=1):
            if isinstance(item, dict) and isinstance(item.get("pass"), bool):
                verdicts[int(item.get("id", position))] = item
    except (ValueError, TypeError):
        pass
    return verdicts


# Numbers and codes a check names (an order number, an amount, a date, a percentage). Numbers that limit the
# answer's shape ("at most 3 sentences") are not facts to find in it.
_FACT = re.compile(r"#?[A-Za-z]{1,6}-?\d[\w-]*\d|\d[\d,.]*\d|\d")
_SHAPE = re.compile(r"(?:at most|at least|under|over|fewer than|less than|more than|up to|no more than|maximum|minimum)"
                    r"\s+\d+|\d+\s*(?:sentences?|words?|paragraphs?|bullets?|bullet points?|lines?|characters?|points?|items?)",
                    re.IGNORECASE)


def _norm_fact(value: str) -> str:
    """Comparable form: no thousands separators, # or %, no trailing decimal zeros ("1,275.00" -> "1275")."""
    return re.sub(r"\.0+\b", "", re.sub(r"(?<=\d),(?=\d)|[#%]", "", value)).lower().strip()


class FactJudge:
    """A deterministic reviewer: a check that names exact numbers or codes passes only if all of them appear in
    the answer. It abstains on checks without such facts. Free, and independent of any model, so it backs up
    small panels (such as Strict mode with a single local reviewer that is also being compared)."""

    name = "fact-check"

    async def evaluate(self, task_prompt: str, response: str, reference: str | None, criteria: list[str]) -> JudgeVerdicts:
        answer = _norm_fact(response)
        probabilities: list[float | None] = []
        reasons: list[str] = []
        for criterion in criteria:
            text = _SHAPE.sub(" ", criterion)
            facts = [f for f in (_norm_fact(m) for m in _FACT.findall(text)) if f]
            if not facts:
                probabilities.append(None)
                reasons.append("")
                continue
            missing = [f for f in facts if not re.search(rf"(?<![0-9a-z]){re.escape(f)}(?![0-9a-z])", answer)]
            probabilities.append(0.0 if missing else 1.0)
            reasons.append(f"missing {', '.join(missing)}" if missing else "all named numbers and codes found")
        return JudgeVerdicts(probabilities, reasons, 0.0)


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


def build_panel(backend: JudgeBackend, models: list[ModelInfo], fact_check: bool = False) -> JudgePanel:
    """Model reviewers, plus the deterministic fact check when asked (Strict mode's small local panel)."""
    judges: list[Any] = [DecisionJudge(backend, m) if m.modality == "decision" else ChatJudge(backend, m) for m in models]
    if fact_check:
        judges.append(FactJudge())
    return JudgePanel(judges)
