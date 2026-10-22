"""Agent 1 (v2): Jev recommends a model from the registry, for a task and a use case.

Jev makes the judgement (which model fits this task and use case). Code then enforces the hard
limit it should not have to calculate: the model must be fast enough for the use case.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Protocol

from .client import Decision, ModelInfo
from .registry import USE_CASES, ModelCard, preference, rank_candidates

MAX_TASK_CHARS = 8_000

INSTRUCTIONS = (
    "Recommend which model in `models` should do the `task` for the `use_case`. The best choice is the "
    "cheapest model that will do this task correctly and respond fast enough for the use case. Cheaper "
    "models are usually good enough for routine work; choose a more expensive model only if the task "
    "clearly needs it. A model that is too slow for the use case is never the right choice."
)


class DecisionBackend(Protocol):
    async def decide(self, model: ModelInfo, state: Any, questions: dict[str, Any]) -> Decision: ...


@dataclass
class Recommendation:
    use_case: str
    model: str | None
    jev_choice: str
    probabilities: dict[str, float]
    confidence: float
    guard_applied: bool
    note: str
    cost_egp: float
    latency_s: float
    candidates: list[str] = field(default_factory=list)
    cached: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


async def recommend(
    backend: DecisionBackend,
    jev: ModelInfo,
    task_text: str,
    use_case: str,
    cards: dict[str, ModelCard],
) -> Recommendation:
    if use_case not in USE_CASES:
        raise ValueError(f"use_case must be one of {list(USE_CASES)}")
    budget = USE_CASES[use_case]["max_p90_s"]
    task = task_text if len(task_text) <= MAX_TASK_CHARS else task_text[:MAX_TASK_CHARS] + "\n[...]"
    state = {
        "task": task,
        "use_case": USE_CASES[use_case]["label"]
        + (f". 1 in 10 answers may take at most {budget:.0f} seconds." if budget else ". Speed does not matter."),
        "models": "\n".join(f"{card.id}: {card.describe()}" for card in cards.values()),
    }
    questions = {
        "model": {"type": "choice", "instructions": INSTRUCTIONS,
                  "criteria": {card.id: card.describe() for card in cards.values()}},
    }
    decision = await backend.decide(jev, state, questions)
    answer = decision.answers.get("model") or {}
    probabilities = {k: float(v) for k, v in (answer.get("probabilities") or {}).items()}
    jev_choice = str(answer.get("choice", ""))

    ranked = sorted(cards, key=lambda m: -probabilities.get(m, 0.0))
    fitting = [m for m in ranked if cards[m].fits(use_case)]
    chosen = jev_choice if jev_choice in cards and cards[jev_choice].fits(use_case) else (fitting[0] if fitting else None)
    guard = chosen != jev_choice
    if not fitting:
        note = "No model in the registry is fast enough for this use case."
    elif guard:
        note = f"Jev chose {jev_choice}, which is too slow for this use case, so the next most likely fast-enough model was used."
    else:
        note = ""
    return Recommendation(
        use_case=use_case,
        model=chosen,
        jev_choice=jev_choice,
        probabilities=probabilities,
        confidence=float(answer.get("confidence", 0.0)),
        guard_applied=guard,
        note=note,
        cost_egp=decision.cost(jev),
        latency_s=round(decision.latency_s, 2),
        candidates=fitting,
        cached=decision.cached,
    )


HARDNESS_QUESTION = {
    "needs_stronger": {
        "type": "noul",
        "instructions": (
            "Would a small, inexpensive language model be likely to get the `task` wrong, so that a stronger "
            "model is needed? Small models handle routine work well when the instructions and inputs are clear. "
            "A stronger model is needed when the task combines several dependent steps with expert judgement, "
            "or when getting it right depends on subtle interpretation that is not spelled out."
        ),
        "criteria": {"true": "A stronger model is needed", "false": "A small model will do"},
    }
}
ESCALATE_AT = 0.5


async def recommend_hybrid(
    backend: DecisionBackend,
    jev: ModelInfo,
    task_text: str,
    use_case: str,
    cards: dict[str, ModelCard],
    escalate_at: float = ESCALATE_AT,
    prefer_open: bool = True,
    min_pass_rate: float = 0.0,
    prefer_egypt: bool = False,
) -> Recommendation:
    """Code ranks fast-enough, reliable models (open-source and Egypt-hosted first, then cheapest); Jev only judges
    whether the task needs a stronger one, in which case the most reliable model of the preferred kind is used."""
    fitting = rank_candidates(cards, use_case, prefer_open, min_pass_rate, prefer_egypt)
    task = task_text if len(task_text) <= MAX_TASK_CHARS else task_text[:MAX_TASK_CHARS] + "\n[...]"
    decision = await backend.decide(jev, {"task": task}, HARDNESS_QUESTION)
    hard = float((decision.answers.get("needs_stronger") or {}).get("noul", 0.0))
    best_kind = preference(fitting[0], prefer_open, prefer_egypt) if fitting else None
    preferred = [c for c in fitting if preference(c, prefer_open, prefer_egypt) == best_kind]
    top = preferred[0] if preferred else None
    kind = ("Egypt-hosted " if prefer_egypt and top and top.egypt_hosted else "") + (
        "open-source " if prefer_open and top and top.open_weights else "")
    if not fitting:
        chosen, note = None, "No reliable model in the registry is fast enough for this use case."
    elif hard >= escalate_at:
        strongest = max(preferred, key=lambda c: (c.pass_rate, -c.cost_per_answer_egp))
        chosen = strongest.id
        note = (f"Agent 1 judged the task hard (p={hard:.2f}), so the most reliable {kind}model that is fast enough was chosen."
                if strongest is not preferred[0] else
                f"Agent 1 judged the task hard (p={hard:.2f}); the cheapest {kind}model is also the most reliable fast-enough one.")
    else:
        chosen, note = preferred[0].id, f"Agent 1 judged a small model will do (p={hard:.2f}): the cheapest reliable {kind}model."
    if chosen and prefer_open and not cards[chosen].open_weights:
        note += " No open-source model is fast and reliable enough here, so a proprietary model is the fallback."
    elif chosen and prefer_egypt and not cards[chosen].egypt_hosted and any(c.egypt_hosted for c in cards.values()):
        note += " No Egypt-hosted model is fast and reliable enough here, so this open-source model runs abroad."
    return Recommendation(
        use_case=use_case,
        model=chosen,
        jev_choice=chosen or "",
        probabilities={"needs_stronger": hard},
        confidence=abs(hard - 0.5) * 2,
        guard_applied=False,
        note=note,
        cost_egp=decision.cost(jev),
        latency_s=round(decision.latency_s, 2),
        candidates=[c.id for c in fitting],
        cached=decision.cached,
    )


def rule_pick(cards: dict[str, ModelCard], use_case: str, min_pass_rate: float = 0.85, prefer_open: bool = True,
              prefer_egypt: bool = False) -> str | None:
    """Baseline without Jev: the first fast-enough model with a good pass record (open-source and Egypt-hosted
    first, then cheapest)."""
    ranked = rank_candidates(cards, use_case, prefer_open, min_pass_rate, prefer_egypt)
    return ranked[0].id if ranked else None
