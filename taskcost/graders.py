"""Graders decide whether a model output completes a task.

The default is `rubric`: a panel of judge models checks each plain-language criterion, judging meaning
rather than wording or format. Deterministic graders (`json_fields`, `contains_all`, ...) remain
available for hard requirements such as a strict output schema.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Protocol

from .tasks import Task


class GraderUnavailable(Exception):
    """Raised when a grader needs a resource (such as a judge model) that isn't configured."""


@dataclass(frozen=True)
class CriterionResult:
    criterion: str
    required: bool
    passed: bool
    probability: float
    by_judge: dict[str, float]
    unanimous: bool
    reason: str = ""
    reviewed: bool = True  # False when no reviewer returned a verdict: neither a pass nor a fail


@dataclass(frozen=True)
class GradeResult:
    passed: bool
    score: float
    detail: str = ""
    cost_egp: float = 0.0
    criteria: tuple[CriterionResult, ...] = ()
    unreviewed: tuple[str, ...] = ()  # required checks no reviewer gave a verdict on


@dataclass(frozen=True)
class Assessment:
    """A panel's view of one response: for each criterion, every judge's pass probability."""

    by_judge: list[dict[str, float]]
    reasons: list[str]
    cost_egp: float
    failed_judges: list[str] = field(default_factory=list)


class Judge(Protocol):
    async def assess(
        self, *, task_prompt: str, response: str, reference: str | None, criteria: list[str],
        threshold: float | None = None,
    ) -> Assessment: ...


@dataclass(frozen=True)
class GradeContext:
    task: Task
    judge: Judge | None = None
    judge_threshold: float = 0.8


GraderFn = Callable[[dict[str, Any], str, GradeContext], Awaitable[GradeResult]]
_GRADERS: dict[str, GraderFn] = {}


def grader(name: str) -> Callable[[GraderFn], GraderFn]:
    def register(fn: GraderFn) -> GraderFn:
        _GRADERS[name] = fn
        return fn

    return register


async def grade(spec: dict[str, Any], output: str, ctx: GradeContext) -> GradeResult:
    grader_type = spec.get("type")
    if grader_type not in _GRADERS:
        raise ValueError(f"Unknown grader type {grader_type!r} in task {ctx.task.id}")
    return await _GRADERS[grader_type](spec, output, ctx)


def normalize(text: str) -> str:
    return re.sub(r"[\W_]+", "", text.casefold())


def _to_number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        cleaned = re.sub(r"[^\d.\-]", "", value)
        try:
            return float(cleaned)
        except ValueError:
            return None
    return None


@grader("exact")
async def _exact(spec: dict[str, Any], output: str, ctx: GradeContext) -> GradeResult:
    expected = spec["expected"]
    accepted = expected if isinstance(expected, list) else [expected]
    passed = normalize(output) in {normalize(str(value)) for value in accepted}
    return GradeResult(passed, float(passed), "" if passed else f"expected one of {accepted}")


@grader("contains_all")
async def _contains_all(spec: dict[str, Any], output: str, ctx: GradeContext) -> GradeResult:
    """`terms` must all appear (a nested list means any one of them); `forbidden` must not appear."""
    text = output.casefold()
    terms = spec.get("terms", [])
    missing = [
        term
        for term in terms
        if not any(option.casefold() in text for option in (term if isinstance(term, list) else [term]))
    ]
    present_forbidden = [term for term in spec.get("forbidden", []) if term.casefold() in text]
    score = 1.0 if not terms else (len(terms) - len(missing)) / len(terms)
    passed = not missing and not present_forbidden
    problems = []
    if missing:
        problems.append(f"missing {missing}")
    if present_forbidden:
        problems.append(f"contains forbidden {present_forbidden}")
    return GradeResult(passed, score if not present_forbidden else 0.0, "; ".join(problems))


@grader("regex")
async def _regex(spec: dict[str, Any], output: str, ctx: GradeContext) -> GradeResult:
    flags = re.IGNORECASE if spec.get("ignore_case", True) else 0
    matched = re.search(spec["pattern"], output, flags | re.MULTILINE) is not None
    passed = matched == spec.get("must_match", True)
    return GradeResult(passed, float(passed), "" if passed else f"regex check failed: {spec['pattern']}")


@grader("max_words")
async def _max_words(spec: dict[str, Any], output: str, ctx: GradeContext) -> GradeResult:
    count = len(output.split())
    passed = count <= spec["limit"]
    return GradeResult(passed, float(passed), "" if passed else f"{count} words > {spec['limit']}")


def extract_json(output: str) -> Any:
    text = output.strip()
    fenced = re.search(r"```(?:json)?\s*(.*?)```", text, re.DOTALL)
    candidates = [text]
    if fenced:
        candidates.append(fenced.group(1).strip())
    for open_char, close_char in ("{}", "[]"):
        start, end = text.find(open_char), text.rfind(close_char)
        if 0 <= start < end:
            candidates.append(text[start : end + 1])
    for candidate in candidates:
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            continue
    raise ValueError("no JSON object found in output")


def resolve_path(data: Any, path: str) -> Any:
    """Resolve `a.b` or `items[*].owner` (the `[*]` suffix maps over a list)."""
    current, fan_out = [data], False
    for part in path.split("."):
        star = part.endswith("[*]")
        key = part[:-3] if star else part
        next_values = []
        for item in current:
            if not isinstance(item, dict) or key not in item:
                raise KeyError(path)
            value = item[key]
            if star:
                if not isinstance(value, list):
                    raise KeyError(path)
                next_values.extend(value)
                fan_out = True
            else:
                next_values.append(value)
        current = next_values
    return current if fan_out else current[0]


def _values_equal(actual: Any, expected: Any, tolerance: float) -> bool:
    if isinstance(expected, dict):
        if "any_of" in expected:
            return any(_values_equal(actual, option, tolerance) for option in expected["any_of"])
        if "superset" in expected:
            return isinstance(actual, list) and _match_list(actual, expected["superset"], tolerance, exact_size=False)
        raise ValueError(f"Unsupported expected value {expected}")
    if isinstance(expected, bool):
        return str(actual).casefold() == str(expected).casefold()
    if isinstance(expected, (int, float)):
        number = _to_number(actual)
        return number is not None and abs(number - expected) <= tolerance
    if isinstance(expected, str):
        return isinstance(actual, (str, int, float)) and normalize(str(actual)) == normalize(expected)
    if isinstance(expected, list):
        return isinstance(actual, list) and _match_list(actual, expected, tolerance, exact_size=True)
    return actual == expected


def _match_list(actual: list[Any], expected: list[Any], tolerance: float, exact_size: bool) -> bool:
    """Order-insensitive match: each expected item must pair with a distinct actual item."""
    if exact_size and len(actual) != len(expected):
        return False
    unused = list(actual)
    for want in expected:
        match = next((i for i, have in enumerate(unused) if _values_equal(have, want, tolerance)), None)
        if match is None:
            return False
        unused.pop(match)
    return True


@grader("json_fields")
async def _json_fields(spec: dict[str, Any], output: str, ctx: GradeContext) -> GradeResult:
    try:
        data = extract_json(output)
    except ValueError as exc:
        return GradeResult(False, 0.0, str(exc))

    tolerance = float(spec.get("numeric_tolerance", 0.01))
    fields: dict[str, Any] = spec["fields"]
    wrong = []
    for path, expected in fields.items():
        try:
            actual = resolve_path(data, path)
        except KeyError:
            wrong.append(f"{path}: missing")
            continue
        if not _values_equal(actual, expected, tolerance):
            wrong.append(f"{path}: got {json.dumps(actual, ensure_ascii=False)[:80]}")
    score = (len(fields) - len(wrong)) / len(fields)
    return GradeResult(not wrong, score, "; ".join(wrong))


def parse_criteria(raw: list[Any]) -> list[tuple[str, bool]]:
    """Criteria are strings (required) or `{text, required}` mappings."""
    parsed = []
    for item in raw:
        if isinstance(item, str):
            parsed.append((item, True))
        else:
            parsed.append((str(item["text"]), bool(item.get("required", True))))
    if not parsed:
        raise ValueError("rubric needs at least one criterion")
    return parsed


@grader("rubric")
async def _rubric(spec: dict[str, Any], output: str, ctx: GradeContext) -> GradeResult:
    """Each criterion passes on a strict majority of judges; the task passes if every required one does."""
    if ctx.judge is None:
        raise GraderUnavailable("rubric grading needs judge_models in the benchmark config")
    criteria = parse_criteria(spec["criteria"])
    threshold = float(spec.get("threshold", ctx.judge_threshold))
    assessment = await ctx.judge.assess(
        task_prompt=ctx.task.prompt,
        response=output,
        reference=ctx.task.reference if spec.get("include_reference", True) else None,
        criteria=[text for text, _ in criteria],
        threshold=threshold,
    )

    results = []
    for (text, required), by_judge, reason in zip(criteria, assessment.by_judge, assessment.reasons):
        # Judges that abstained are absent from by_judge; if every judge abstained, the check is not reviewed.
        votes = [p >= threshold for p in by_judge.values()]
        results.append(
            CriterionResult(
                criterion=text,
                required=required,
                passed=bool(votes) and sum(votes) * 2 > len(votes),
                probability=sum(by_judge.values()) / len(by_judge) if by_judge else 0.0,
                by_judge=by_judge,
                unanimous=len(set(votes)) == 1,
                reason=reason,
                reviewed=bool(votes),
            )
        )

    failed = [r for r in results if r.required and r.reviewed and not r.passed]
    unreviewed = tuple(r.criterion for r in results if r.required and not r.reviewed)
    detail = "; ".join(f"failed: {r.criterion[:80]} (p={r.probability:.2f})" for r in failed)
    if unreviewed:
        detail = "; ".join(filter(None, [detail, f"not reviewed (no verdict returned): {len(unreviewed)} check(s)"]))
    if assessment.failed_judges:
        detail = "; ".join(filter(None, [detail, f"judges unavailable: {assessment.failed_judges}"]))
    reviewed = [r for r in results if r.reviewed]
    score = sum(r.probability for r in reviewed) / len(reviewed) if reviewed else 0.0
    return GradeResult(not failed and not unreviewed, score, detail, assessment.cost_egp, tuple(results), unreviewed)


@grader("llm_judge")
async def _llm_judge(spec: dict[str, Any], output: str, ctx: GradeContext) -> GradeResult:
    """Single-criterion rubric, kept for task files that use one overall rubric."""
    rubric_spec = {
        "criteria": [spec["rubric"]],
        "include_reference": spec.get("include_reference", False),
        **({"threshold": spec["threshold"]} if "threshold" in spec else {}),
    }
    return await _rubric(rubric_spec, output, ctx)


@grader("all_of")
async def _all_of(spec: dict[str, Any], output: str, ctx: GradeContext) -> GradeResult:
    """Runs cheap graders first and skips the rest (including paid judges) after the first failure."""
    results: list[GradeResult] = []
    for child in spec["graders"]:
        result = await grade(child, output, ctx)
        results.append(result)
        if not result.passed:
            break
    total = len(spec["graders"])
    score = sum(r.score for r in results) / total
    cost = sum(r.cost_egp for r in results)
    failed = [r.detail for r in results if not r.passed]
    criteria = tuple(c for r in results for c in r.criteria)
    unreviewed = tuple(u for r in results for u in r.unreviewed)
    return GradeResult(not failed and len(results) == total, score, "; ".join(failed), cost, criteria, unreviewed)
