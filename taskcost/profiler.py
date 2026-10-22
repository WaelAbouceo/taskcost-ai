"""Agent 1, the task profiler: decides what kind of model a simple task needs.

Jev (a decision model) answers judgement questions about the task. Facts that code can measure
(Arabic text, input size) are measured in code. Transparent rules then turn both into model
requirements (size, instant or reasoning, language, context) and a tier.

The thresholds below are starting points; `taskcost calibrate` measures how often they pick the
cheapest tier that actually passes, on the task library.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from typing import Any, Protocol

from .client import Decision, ModelInfo

# Tier classes in cost order; config tiers are matched to these by position.
TIER_CLASSES = ("Fast", "Balanced", "Thinking", "Frontier")

QUESTIONS: dict[str, dict[str, Any]] = {
    "work_type": {
        "type": "choice",
        "instructions": "What kind of work does the `task` ask for?",
        "criteria": {
            "extraction": "Find and copy facts or values that are stated in the given material",
            "classification": "Put items into given categories or labels",
            "calculation": "Compute numbers, totals, percentages or dates",
            "rule_application": "Apply a policy, contract or set of rules to specific cases",
            "summarization": "Condense given material into its key points",
            "translation": "Translate text into another language",
            "writing": "Write new text such as an email, a reply or an announcement",
            "question_answering": "Answer questions using given reference material",
        },
    },
    "reasoning": {
        "type": "score",
        "instructions": "How much step-by-step reasoning is needed to complete the `task` correctly?",
        "criteria": [
            "None: the answer is stated directly or only needs rewording",
            "Light: one simple inference or one obvious calculation",
            "Moderate: several dependent steps, such as date arithmetic, percentages or applying several rules",
            "Heavy: long chains of logic, many interacting rules, edge cases or traps",
        ],
    },
    "error_cost": {
        "type": "score",
        "instructions": "How costly would a mistake in the result of the `task` be for the business?",
        "criteria": [
            "Low: a draft that a person will edit before it is used",
            "Medium: it should be correct, and mistakes would be noticed and fixed",
            "High: exact values, money, legal or compliance consequences",
        ],
    },
    "writing_quality": {
        "type": "score",
        "instructions": "How much does the quality of the writing matter in the result of the `task`?",
        "criteria": [
            "None: structured or factual output where wording does not matter",
            "Basic: clear and correct plain text",
            "High: tone, empathy, persuasion or polish matter",
        ],
    },
    "expertise": {
        "type": "score",
        "instructions": "How much specialist knowledge does the `task` need?",
        "criteria": [
            "General: everyday knowledge",
            "Business: common business practice",
            "Specialist: legal, financial, medical or technical expertise",
        ],
    },
    "constraints": {
        "type": "score",
        "instructions": "How many separate requirements must the result of the `task` satisfy at the same time?",
        "criteria": [
            "Few: one or two simple instructions",
            "Several: a handful of requirements",
            "Many: many requirements that must all be met at once",
        ],
    },
    "arabic_output": {
        "type": "noul",
        "instructions": "Must the response to the `task` be written in Arabic?",
    },
    "dialect": {
        "type": "noul",
        "instructions": "Does the `task` contain Arabic written in a spoken dialect, such as Egyptian or Gulf Arabic, rather than formal Modern Standard Arabic?",
    },
}

THRESHOLDS = {
    "reasoning_model": 1.6,          # reasoning score at or above this needs a reasoning model
    "reasoning_if_high_stakes": 1.2,  # ...or at or above this when error_cost is high
    "high_error_cost": 1.5,
    "large_reasoning": 2.5,
    "specialist": 1.5,
    "high_writing": 1.5,
    "small_max": 1.2,                # a small model only if reasoning, writing, constraints and expertise are all below this
    "arabic_output": 0.7,
    "dialect": 0.7,
    "arabic_share": 0.05,
    "long_context_tokens": 30_000,
    "borderline": 0.15,
}

_ARABIC = re.compile(r"[\u0600-\u06FF]")
_LETTER = re.compile(r"[^\W\d_]")
MAX_STATE_CHARS = 12_000


class DecisionBackend(Protocol):
    async def decide(self, model: ModelInfo, state: Any, questions: dict[str, Any]) -> Decision: ...


@dataclass
class Profile:
    tier_class: str
    size: str
    model_type: str
    language: str
    context: str
    work_type: str
    scores: dict[str, float]
    reasons: list[str]
    borderline: list[str] = field(default_factory=list)
    measured: dict[str, Any] = field(default_factory=dict)
    answers: dict[str, Any] = field(default_factory=dict)
    cost_egp: float = 0.0
    latency_s: float = 0.0
    model: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def measure(text: str) -> dict[str, Any]:
    letters = _LETTER.findall(text)
    arabic = sum(1 for ch in letters if _ARABIC.match(ch))
    share = arabic / len(letters) if letters else 0.0
    # Arabic text uses roughly 2.5 characters per token, Latin text roughly 4.
    tokens = int((len(text) - arabic) / 4 + arabic / 2.5)
    return {"arabic_share": round(share, 3), "input_tokens_estimate": tokens, "characters": len(text)}


def _score(answer: dict[str, Any] | None) -> float:
    return float((answer or {}).get("score", 0.0))


def decide_requirements(answers: dict[str, Any], measured: dict[str, Any], t: dict[str, float] = THRESHOLDS) -> dict[str, Any]:
    """The rule layer: plain, inspectable rules from Jev's answers and measured facts to a tier."""
    scores = {name: round(_score(answers.get(name)), 2)
              for name in ("reasoning", "error_cost", "writing_quality", "expertise", "constraints")}
    reasoning, error_cost = scores["reasoning"], scores["error_cost"]
    writing, expertise, constraints = scores["writing_quality"], scores["expertise"], scores["constraints"]
    arabic_output = float((answers.get("arabic_output") or {}).get("noul", 0.0))
    dialect = float((answers.get("dialect") or {}).get("noul", 0.0))
    has_arabic = measured["arabic_share"] >= t["arabic_share"]
    reasons: list[str] = []
    borderline: list[str] = []

    def near(value: float, threshold: float, label: str) -> None:
        if abs(value - threshold) <= t["borderline"]:
            borderline.append(f"{label} is {value:.2f}, close to the {threshold} threshold")

    high_stakes = error_cost >= t["high_error_cost"]
    needs_reasoning = reasoning >= t["reasoning_model"] or (high_stakes and reasoning >= t["reasoning_if_high_stakes"])
    near(reasoning, t["reasoning_model"], "Reasoning")
    if high_stakes:
        near(reasoning, t["reasoning_if_high_stakes"], "Reasoning (high stakes)")
    if needs_reasoning:
        reasons.append(f"Reasoning {reasoning:.1f}/3{' with high error cost' if high_stakes else ''}: needs a reasoning model")
    else:
        reasons.append(f"Reasoning {reasoning:.1f}/3: an instant model is enough")

    if reasoning >= t["large_reasoning"] or (expertise >= t["specialist"] and (high_stakes or writing >= t["high_writing"])):
        size = "large"
        reasons.append("Heavy reasoning or specialist work with high stakes or demanding writing: needs a large model")
    elif max(reasoning, writing, constraints, expertise) < t["small_max"]:
        size = "small"
        reasons.append("Simple, low-demand task: a small model should do")
        near(max(reasoning, writing, constraints, expertise), t["small_max"], "The highest demand score")
    else:
        size = "medium"
        reasons.append("Moderate demands on writing, rules or knowledge: needs a medium model")

    arabic_needed = has_arabic or arabic_output >= t["arabic_output"]
    is_dialect = has_arabic and dialect >= t["dialect"]
    language = "Arabic dialect" if is_dialect else "Arabic" if arabic_needed else "English"
    if arabic_needed and size == "small":
        size = "medium"
        reasons.append("Arabic: small models are often weak in Arabic, so at least a medium model")

    context = "long" if measured["input_tokens_estimate"] >= t["long_context_tokens"] else "standard"
    if context == "long":
        reasons.append(f"About {measured['input_tokens_estimate']:,} input tokens: needs a long context window")

    if needs_reasoning:
        tier_class = "Frontier" if size == "large" else "Thinking"
    else:
        tier_class = {"small": "Fast", "medium": "Balanced", "large": "Frontier"}[size]

    return {
        "tier_class": tier_class,
        "size": size,
        "model_type": "reasoning" if needs_reasoning else "instant",
        "language": language,
        "context": context,
        "scores": scores,
        "reasons": reasons,
        "borderline": borderline,
    }


async def profile_task(backend: DecisionBackend, model: ModelInfo, task_text: str) -> Profile:
    measured = measure(task_text)
    state = task_text if len(task_text) <= MAX_STATE_CHARS else (
        task_text[:MAX_STATE_CHARS] + f"\n[... {len(task_text) - MAX_STATE_CHARS:,} more characters ...]"
    )
    decision = await backend.decide(model, {"task": state}, QUESTIONS)
    answers = decision.answers
    rules = decide_requirements(answers, measured)
    return Profile(
        work_type=str((answers.get("work_type") or {}).get("choice", "other")),
        measured=measured,
        answers=answers,
        cost_egp=decision.cost(model),
        latency_s=round(decision.latency_s, 2),
        model=model.id,
        **rules,
    )


def tier_for_class(tier_class: str, tiers: list[tuple[str, str]]) -> tuple[str, str]:
    """Map a tier class onto the configured tiers by position (Fast is the first, Frontier the fourth)."""
    if not tiers:
        raise ValueError("No tiers configured")
    index = min(TIER_CLASSES.index(tier_class), len(tiers) - 1)
    return tiers[index]
