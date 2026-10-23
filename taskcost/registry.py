"""Model registry: measured cards for each model (cost per answer, latency, pass rate), built from runs.

Use cases set the latency a model must meet. Latency is judged on the slowest 1 in 10 answers (p90),
because a chatbot user notices the slow answers, not the average one.
"""

from __future__ import annotations

import json
import statistics
from collections import defaultdict
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from .licenses import is_egypt_hosted, is_open_weights
from .metrics import percentile

# Chatbot speed is judged on chatbot-length answers: a long report is slow from any model, and a chatbot
# rarely writes one. Used once a model has enough such answers measured; otherwise all answers count.
SHORT_ANSWER_TOKENS = 600
MIN_SHORT_RUNS = 8

USE_CASES: dict[str, dict[str, Any]] = {
    "chat": {"label": "Chatbot: a person is waiting for the answer", "max_p90_s": 5.0},
    "realtime": {"label": "Real-time workflow: an application waits for the result", "max_p90_s": 15.0},
    "batch": {"label": "Batch: results are needed within hours", "max_p90_s": None},
}


@dataclass
class ModelCard:
    id: str
    tier: str
    runs: int
    pass_rate: float
    latency_median_s: float
    latency_p90_s: float
    cost_per_answer_egp: float
    open_weights: bool = False
    egypt_hosted: bool = False
    latency_p90_short_s: float | None = None  # slowest 1 in 10 among answers of chatbot length
    short_runs: int = 0
    hard_pass_rate: float | None = None  # on library tasks tagged "hard" (multi-step reasoning with traps)
    hard_runs: int = 0

    def speed_for(self, use_case: str) -> float:
        """The slowest-1-in-10 latency that counts for this use case."""
        if use_case == "chat" and self.short_runs >= MIN_SHORT_RUNS and self.latency_p90_short_s is not None:
            return self.latency_p90_short_s
        return self.latency_p90_s

    def fits(self, use_case: str) -> bool:
        limit = USE_CASES[use_case]["max_p90_s"]
        return limit is None or self.speed_for(use_case) <= limit

    def describe(self) -> str:
        return (
            f"{self.tier} tier, {'open-source' if self.open_weights else 'proprietary'}"
            f"{', hosted in Egypt' if self.egypt_hosted else ''}. "
            f"About {self.cost_per_answer_egp:.4f} EGP per answer. "
            f"Typical response {self.latency_median_s:.1f} s; 1 in 10 answers takes over {self.latency_p90_s:.1f} s. "
            f"Passed {self.pass_rate:.0%} of {self.runs} benchmark answers."
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def load_runs(run_dirs: list[Path]) -> list[dict[str, Any]]:
    records = []
    for run_dir in run_dirs:
        path = run_dir / "attempts.jsonl"
        if path.is_file():
            records += [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    return records


def build_registry(records: list[dict[str, Any]], hard_tasks: set[str] | frozenset[str] = frozenset()) -> dict[str, ModelCard]:
    by_model: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in records:
        if r.get("error_kind") is None:
            by_model[r["model"]].append(r)
    cards = {}
    for model, runs in by_model.items():
        latencies = [r["latency_s"] for r in runs if r.get("latency_s") is not None]
        short = [r["latency_s"] for r in runs if r.get("latency_s") is not None
                 and (r.get("completion_tokens") or 0) <= SHORT_ANSWER_TOKENS]
        hard = [r for r in runs if r["task_id"] in hard_tasks]
        # Reliability comes from the reviewed task library. Plan measurements use one-off generated tests, some of
        # them flawed (every model fails them), so they add speed and cost data but don't lower a model's record.
        graded = [r for r in runs if not r.get("verification")] or runs
        graded = [r for r in graded if not r.get("unreviewed")] or graded  # no verdict is not evidence either way
        cards[model] = ModelCard(
            id=model,
            tier=str(runs[0].get("tier", "")),
            runs=len(runs),
            pass_rate=sum(r["passed"] for r in graded) / len(graded),
            latency_median_s=statistics.median(latencies),
            latency_p90_s=percentile(latencies, 90),
            cost_per_answer_egp=statistics.median(r["cost_egp"] for r in runs),
            open_weights=is_open_weights(model),
            egypt_hosted=is_egypt_hosted(model),
            latency_p90_short_s=percentile(short, 90),
            short_runs=len(short),
            hard_pass_rate=sum(r["passed"] for r in hard) / len(hard) if hard else None,
            hard_runs=len(hard),
        )
    return dict(sorted(cards.items(), key=lambda item: item[1].cost_per_answer_egp))


def preference(card: ModelCard, prefer_open: bool, residency: dict[str, int] | None = None) -> tuple[int, bool]:
    """Sort key for the kind of model, lower is preferred: closest to the user first (when location matters,
    see sovereignty.residency_ranks), then open-source."""
    return (residency.get(card.id, 9) if residency else 0, (not card.open_weights) if prefer_open else False)


def rank_candidates(
    cards: dict[str, ModelCard], use_case: str, prefer_open: bool = True, min_pass_rate: float = 0.0,
    residency: dict[str, int] | None = None,
) -> list[ModelCard]:
    """Fast-enough, reliable-enough models in the order to try them: closest to the user first (if location
    matters), open-source before proprietary (if preferred), then cheapest first."""
    fitting = [c for c in cards.values() if c.fits(use_case) and c.pass_rate >= min_pass_rate]
    return sorted(fitting, key=lambda c: (preference(c, prefer_open, residency), c.cost_per_answer_egp))


def task_results(records: list[dict[str, Any]]) -> dict[tuple[str, str], dict[str, float]]:
    """Per (model, task): runs, passes and mean cost, for working out the right answer."""
    results: dict[tuple[str, str], dict[str, float]] = defaultdict(lambda: {"runs": 0, "passed": 0, "cost": 0.0})
    for r in records:
        if r.get("error_kind") is None and not r.get("unreviewed"):
            entry = results[(r["model"], r["task_id"])]
            entry["runs"] += 1
            entry["passed"] += int(r["passed"])
            entry["cost"] += r["cost_egp"]
    return dict(results)


def best_model(
    task_id: str, use_case: str, cards: dict[str, ModelCard], results: dict[tuple[str, str], dict[str, float]],
    prefer_open: bool = False,
    residency: dict[str, int] | None = None,
) -> str | None:
    """Ground truth: the cheapest model that passed every run of this task and is fast enough for the use case
    (of the preferred kind, closest to the user and open-source, if any qualifies)."""
    candidates = []
    for model, card in cards.items():
        entry = results.get((model, task_id))
        if entry and entry["runs"] and entry["passed"] == entry["runs"] and card.fits(use_case):
            candidates.append((preference(card, prefer_open, residency), entry["cost"] / entry["runs"], model))
    return min(candidates)[2] if candidates else None


def registry_from_results(results_dir: Path) -> tuple[dict[str, ModelCard], dict[tuple[str, str], dict[str, float]]]:
    """Build the registry from every calibration run and blueprint measurement saved under results_dir.

    Every measured blueprint adds data, so the registry (and Agent 1's picks) improve with use.
    """
    dirs = sorted(results_dir.glob("calibration*")) + sorted(results_dir.glob("blueprint-*/verification"))
    records = load_runs(dirs)
    return build_registry(records, hard_task_ids(results_dir.parent / "library")), task_results(records)


def hard_task_ids(library: Path) -> frozenset[str]:
    """Ids of library tasks tagged "hard", whose results show which models handle difficult work."""
    if not library.is_dir():
        return frozenset()
    from .tasks import load_tasks

    return frozenset(t.id for t in load_tasks(library) if "hard" in t.tags)


def save_registry(cards: dict[str, ModelCard], path: Path, sources: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"sources": sources, "use_cases": USE_CASES, "models": [c.to_dict() for c in cards.values()]}
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def load_registry(path: Path) -> dict[str, ModelCard]:
    data = json.loads(path.read_text(encoding="utf-8"))
    return {m["id"]: ModelCard(**m) for m in data["models"]}
