"""The log reviewer agent: reads the usage log and recommends enhancements, backed by measured facts.

1. Code computes the facts (metrics) from the usage log: what people ask for, how often Agent 1 is right,
   how often users correct the guessed use case, fallbacks, errors, costs, waiting times, Strict refusals.
2. Code flags signals: metrics past a threshold that usually call for a change.
3. A chat model (hosted in Egypt by default) writes recommendations. Each must cite the metrics it rests on;
   a recommendation citing a metric that doesn't exist, or stating a number the metrics don't support, is
   dropped. The page shows the cited metrics' real values next to each recommendation.
"""

from __future__ import annotations

import json
import re
import statistics
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, TextIO

from .client import ModelInfo
from .graders import extract_json
from .jsonchat import ChatBackend, chat_json
from .usage import load_events

ROOT = Path(__file__).resolve().parent.parent
REVIEWS_DIR = ROOT / ".cache" / "reviews"
MIN_ACTIVITY = 3
AREAS = ("models", "agent1", "user_experience", "sovereignty", "cost", "speed", "reliability", "task_library")

REVIEW_SYSTEM = f"""You review how people use TaskCost AI, a tool that tells enterprises which AI model to use for a task
by testing models and reporting the cost per correct answer, with sovereignty settings (where data may go).
You get measured metrics from the usage log and signals that code flagged. Recommend up to 6 concrete
enhancements to the product, its default models, Agent 1 (the model picker), its task library or its settings.
Rules
- Base every recommendation only on the metrics given, read as metric_meanings explains, and consistent with
  product_facts. Cite the metric keys you rely on in "evidence".
- Never recommend a model for a use case it is too slow for.
- Do not write numbers in "why" or "action"; the page shows the cited metrics' values next to your text.
- "action" is one concrete change someone can make, such as a setting, a default model, a task to add,
  a page change or a measurement to run.
- If the metrics are too thin to support a recommendation, leave it out.
- area is one of: {", ".join(AREAS)}. priority is high, medium or low.
Reply with JSON only:
{{"summary": "two sentences on how the tool is being used", "recommendations": [{{"title": "...", "area": "...",
  "priority": "high", "why": "...", "action": "...", "evidence": ["metric.key"]}}]}}"""


def _rate(part: int, whole: int) -> float | None:
    return round(part / whole, 3) if whole else None


def compute_metrics(events: list[dict[str, Any]]) -> dict[str, Any]:
    """Flat metric key -> value, computed from the usage log."""
    by_kind: dict[str, list[dict[str, Any]]] = {}
    for event in events:
        by_kind.setdefault(event["kind"], []).append(event)
    comparisons = by_kind.get("comparison_done", [])
    blueprints = by_kind.get("blueprint_done", [])
    requests = by_kind.get("request", [])
    m: dict[str, Any] = {
        "activity.comparisons": len(comparisons),
        "activity.blueprints": len(blueprints),
        "activity.measurements": len(by_kind.get("measured", [])),
        "activity.own_examples": len(by_kind.get("own_example", [])),
        "activity.reruns": sum(1 for c in comparisons if c.get("rerun")),
        "activity.requests": len(requests),
        "activity.cached_share": _rate(sum(1 for r in requests if r.get("cached")), len(requests)),
        "users.distinct": len({e.get("client") for e in events if e.get("client")}),
        "users.tailored_share": _rate(sum(1 for r in requests if r.get("tailored")),
                                      sum(1 for r in requests if r.get("via") == "start")),
        "errors.failed_jobs": len(by_kind.get("job_failed", [])),
        "errors.failed_by_kind": dict(Counter(e.get("job") for e in by_kind.get("job_failed", []))),
        "sovereignty.strict_refusals": len(by_kind.get("strict_refused", [])),
        "sovereignty.refusals_by_country": dict(Counter(e.get("country") for e in by_kind.get("strict_refused", []))),
    }
    if comparisons:
        n = len(comparisons)
        uses = Counter(c.get("use_case") for c in comparisons)
        for use in ("chat", "realtime", "batch"):
            m[f"use_case.share_{use}"] = _rate(uses.get(use, 0), n)
        guessed = [c for c in comparisons if c.get("use_case_guessed")]
        m["use_case.guessed_share"] = _rate(len(guessed), n)
        m["use_case.override_rate"] = _rate(len(by_kind.get("use_case_changed", [])), len(guessed))
        m["outcome.no_winner_rate"] = _rate(sum(c["no_winner"] for c in comparisons), n)
        m["outcome.proprietary_fallback_rate"] = _rate(sum(c["proprietary_fallback"] for c in comparisons), n)
        m["outcome.local_fallback_rate"] = _rate(sum(c["local_fallback"] for c in comparisons), n)
        chat = [c for c in comparisons if c.get("use_case") == "chat"]
        m["outcome.chat_proprietary_rate"] = _rate(sum(c["proprietary_fallback"] for c in chat), len(chat))
        verdicts = Counter(c.get("agent1_verdict") for c in comparisons if c.get("agent1_verdict"))
        judged = sum(verdicts.values())
        m["agent1.predictions"] = judged
        m["agent1.best_rate"] = _rate(verdicts.get("best", 0), judged)
        m["agent1.failed_rate"] = _rate(verdicts.get("failed", 0), judged)
        m["tasks.by_type"] = dict(Counter(c.get("task_type") for c in comparisons).most_common(8))
        m["tasks.titles"] = [t for t, _ in Counter(c.get("title") for c in comparisons if c.get("title")).most_common(10)]
        m["tasks.with_documents_share"] = _rate(sum(1 for c in comparisons if c.get("documents")), n)
        m["sovereignty.levels"] = dict(Counter(c.get("sovereignty") or "unknown" for c in comparisons))
        m["sovereignty.countries"] = dict(Counter(c.get("country") or "unknown" for c in comparisons))
        recommended = Counter(c["recommended"]["model"] for c in comparisons if c.get("recommended"))
        m["models.recommended"] = dict(recommended.most_common(6))
        runs: dict[str, list[dict[str, Any]]] = {}
        for c in comparisons:
            for model in c.get("models", []):
                runs.setdefault(model["model"], []).append(model)
        use_of = {id(model): c.get("use_case") for c in comparisons for model in c.get("models", [])}
        for model, rows in runs.items():
            if len(rows) >= 3:
                total = sum(r["runs"] for r in rows)
                m[f"model.{model}.tested"] = len(rows)
                m[f"model.{model}.pass_rate"] = _rate(sum(r["passed"] for r in rows), total)
                m[f"model.{model}.error_rate"] = _rate(sum(r["errors"] for r in rows), total)
                for use in ("chat", "realtime"):
                    timed = [r for r in rows if use_of[id(r)] == use]
                    if timed:
                        m[f"model.{model}.too_slow_rate_{use}"] = _rate(sum(1 for r in timed if not r["fast_enough"]), len(timed))
        costs = [c["design_cost_egp"] + c["judge_cost_egp"] + c["model_cost_egp"] for c in comparisons]
        helper = sum(c["design_cost_egp"] + c["judge_cost_egp"] for c in comparisons)
        m["cost.avg_per_comparison_egp"] = round(statistics.mean(costs), 4)
        m["cost.helpers_and_reviewers_share"] = _rate(helper, sum(costs)) if sum(costs) else None
        timed = sorted(c["seconds"] for c in comparisons if c.get("seconds"))
        if timed:
            m["time.median_comparison_s"] = round(statistics.median(timed), 1)
            m["time.slowest_comparison_s"] = timed[-1]
    if blueprints:
        m["blueprint.avg_ai_steps"] = round(statistics.mean(b["ai_steps"] for b in blueprints), 1)
        monthly = [b["monthly_cost_egp"] for b in blueprints if b.get("monthly_cost_egp") is not None]
        m["blueprint.median_monthly_cost_egp"] = round(statistics.median(monthly), 1) if monthly else None
        m["blueprint.unpriced_step_rate"] = _rate(sum(b["unpriced_steps"] for b in blueprints),
                                                  sum(b["ai_steps"] for b in blueprints))
        m["blueprint.measured_share"] = _rate(len({e["run"] for e in by_kind.get("measured", [])}), len(blueprints))
        timed = [b["seconds"] for b in blueprints if b.get("seconds")]
        if timed:
            m["time.median_blueprint_s"] = round(statistics.median(timed), 1)
    return {k: v for k, v in m.items() if v is not None}


# What each metric means, given to the reviewer so it reads them correctly (matched by key prefix).
METRIC_HELP = {
    "activity.": "counts of what users did in the period",
    "activity.cached_share": "share of requests answered from the 24-hour cache instead of running again",
    "use_case.share_": "share of comparisons per use case: chat (a person waits, answers within 5 s), realtime (an app waits, within 15 s), batch (no limit)",
    "use_case.guessed_share": "share of comparisons where the system guessed the use case from the user's words (the user didn't pick it)",
    "use_case.override_rate": "share of guessed use cases that the user then changed on the result page (a wrong guess)",
    "outcome.no_winner_rate": "share of comparisons where no model both passed and was fast enough",
    "outcome.proprietary_fallback_rate": "share of comparisons where the recommended model is proprietary because no open-source one passed and was fast enough",
    "outcome.chat_proprietary_rate": "the same, for chatbot comparisons only",
    "outcome.local_fallback_rate": "share of comparisons where a model closer to the user was tested but didn't pass or wasn't fast enough",
    "agent1.": "Agent 1 predicts the best model before testing; best_rate = it was the recommended one, failed_rate = it failed the test",
    "model.": "per model, over the comparisons it was tested in: pass_rate, error_rate, and too_slow_rate_chat / too_slow_rate_realtime = share of chat or realtime comparisons where it was too slow for that use",
    "models.recommended": "how often each model was the final recommendation",
    "tasks.": "what users asked for: task types, titles of the concrete tasks, share that included a document",
    "sovereignty.": "sovereignty levels and countries chosen, and Strict refusals (no model allowed where the user is)",
    "cost.": "EGP per comparison, and the share spent on helpers (understanding the task) and reviewers rather than the models tested",
    "time.": "seconds from pressing Compare to the result",
    "blueprint.": "plans for workflows and systems: AI steps per plan, median monthly cost, share of steps with no model fast enough, share of plans measured on examples rather than estimated",
    "errors.": "jobs that failed with an error",
    "users.": "distinct anonymous users (browsers), and the share of their requests tailored to what was learned about them (industry, language, usual use case)",
}
PRODUCT_FACTS = (
    "Only models hosted on SovereignEG's GPUs in Egypt are local for Egyptian users; there are no local models elsewhere. "
    "The Egypt-hosted model is too slow for chatbots (its slowest answers take about 12 seconds; chat allows 5). "
    "Agent 1 recommends from measured models only; users can rerun a comparison with a different use case."
)


def metric_help(metrics: dict[str, Any]) -> dict[str, str]:
    help_text = {}
    for key in metrics:
        match = max((p for p in METRIC_HELP if key.startswith(p)), key=len, default=None)
        if match:
            help_text.setdefault(match, METRIC_HELP[match])
    return help_text


# (metric, threshold, direction, what it usually means) - flagged for the reviewer, not decisions.
SIGNALS = [
    ("use_case.override_rate", 0.2, ">", "users often correct the guessed use case"),
    ("outcome.no_winner_rate", 0.15, ">", "many comparisons end without a model that passed and was fast enough"),
    ("outcome.chat_proprietary_rate", 0.5, ">", "chatbot tasks mostly fall back to proprietary models"),
    ("outcome.local_fallback_rate", 0.3, ">", "the closest models often don't qualify"),
    ("agent1.failed_rate", 0.2, ">", "Agent 1's prediction often fails the test"),
    ("agent1.best_rate", 0.6, "<", "Agent 1 often misses the best model"),
    ("cost.helpers_and_reviewers_share", 0.5, ">", "helpers and reviewers cost more than the models tested"),
    ("time.median_comparison_s", 45, ">", "comparisons are slow"),
    ("errors.failed_jobs", 2, ">", "jobs fail"),
    ("sovereignty.strict_refusals", 0, ">", "Strict mode refused requests, so there is demand in countries without local models"),
    ("blueprint.unpriced_step_rate", 0.1, ">", "plan steps often have no model fast enough"),
    ("blueprint.measured_share", 0.2, "<", "plans are rarely measured, so their costs stay estimates"),
]


def signals(metrics: dict[str, Any]) -> list[dict[str, Any]]:
    flagged = [{"metric": key, "value": metrics[key], "meaning": meaning}
               for key, limit, op, meaning in SIGNALS
               if isinstance(metrics.get(key), (int, float)) and (metrics[key] > limit if op == ">" else metrics[key] < limit)]
    for key, value in metrics.items():
        if key.endswith(".error_rate") and value > 0.1:
            flagged.append({"metric": key, "value": value, "meaning": "this model often errors"})
        if key.endswith(".pass_rate") and value < 0.7:
            flagged.append({"metric": key, "value": value, "meaning": "this model often fails users' tasks"})
    return flagged


_NUMBER = re.compile(r"(?<![\w.\-/])\d+(?:\.\d+)?%?(?![\w\-/])")


def _supported(text: str, values: list[Any]) -> bool:
    """Numbers written in the text must match a cited metric (as is, or as a percentage)."""
    allowed: set[float] = set()
    for value in values:
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            allowed |= {round(float(value), 1), round(float(value) * 100), round(float(value))}
    for token in _NUMBER.findall(text):
        number = float(token.rstrip("%"))
        if not any(abs(number - a) <= 1 for a in allowed):
            return False
    return True


def check_recommendations(raw: Any, metrics: dict[str, Any]) -> tuple[list[dict[str, Any]], int]:
    kept, dropped = [], 0
    for item in raw if isinstance(raw, list) else []:
        if not isinstance(item, dict):
            dropped += 1
            continue
        evidence = [e for e in item.get("evidence") or [] if isinstance(e, str)]
        if not evidence or any(e not in metrics for e in evidence):
            dropped += 1
            continue
        values = [metrics[e] for e in evidence]
        text = f"{item.get('why', '')} {item.get('action', '')} {item.get('title', '')}"
        if not _supported(text, values):
            dropped += 1
            continue
        kept.append({
            "title": str(item.get("title", "")).strip(), "area": item.get("area") if item.get("area") in AREAS else "user_experience",
            "priority": item.get("priority") if item.get("priority") in ("high", "medium", "low") else "medium",
            "why": str(item.get("why", "")).strip(), "action": str(item.get("action", "")).strip(),
            "evidence": {e: metrics[e] for e in evidence},
        })
    order = {"high": 0, "medium": 1, "low": 2}
    return sorted(kept, key=lambda r: order[r["priority"]]), dropped


async def review_usage(backend: ChatBackend, reviewer: ModelInfo, days: float = 7, log: TextIO = sys.stderr) -> dict[str, Any]:
    """Run the log reviewer once and save its review."""
    events = load_events(days)
    metrics = compute_metrics(events)
    flagged = signals(metrics)
    review: dict[str, Any] = {
        "created_at": datetime.now(timezone.utc).isoformat(), "days": days, "events": len(events),
        "reviewer_model": reviewer.id, "metrics": metrics, "signals": flagged,
        "summary": "", "recommendations": [], "dropped": 0, "cost_egp": 0.0,
    }
    activity = metrics.get("activity.comparisons", 0) + metrics.get("activity.blueprints", 0)
    if activity < MIN_ACTIVITY:
        review["summary"] = f"Not enough usage in the last {days:g} days to recommend changes yet."
    else:
        print(f"Reviewing {len(events)} events from the last {days:g} days with {reviewer.id}...", file=log)
        payload = {"product_facts": PRODUCT_FACTS, "metric_meanings": metric_help(metrics),
                   "metrics": metrics, "signals": flagged}
        messages = [{"role": "system", "content": REVIEW_SYSTEM},
                    {"role": "user", "content": json.dumps(payload, ensure_ascii=False, indent=1)}]

        def parse(text: str) -> dict[str, Any]:
            data = extract_json(text)
            if not isinstance(data, dict) or not isinstance(data.get("recommendations"), list):
                raise ValueError("the reply needs a recommendations list")
            return data

        reply = await chat_json(backend, reviewer, messages, parse, max_tokens=6000,
                                fail=lambda problem: ValueError(f"The reviewer's reply could not be used: {problem}"))
        kept, dropped = check_recommendations(reply.value["recommendations"], metrics)
        summary = str(reply.value.get("summary", "")).strip()
        review.update(summary=summary if _supported(summary, list(metrics.values())) else "",
                      recommendations=kept, dropped=dropped, cost_egp=round(reply.cost_egp, 6))
    REVIEWS_DIR.mkdir(parents=True, exist_ok=True)
    stamp = review["created_at"][:16].replace(":", "")
    (REVIEWS_DIR / f"review-{stamp}.json").write_text(json.dumps(review, indent=1, ensure_ascii=False), encoding="utf-8")
    return review


def list_reviews() -> list[Path]:
    return sorted(REVIEWS_DIR.glob("review-*.json"), reverse=True)


def latest_review() -> dict[str, Any] | None:
    reviews = list_reviews()
    return json.loads(reviews[0].read_text(encoding="utf-8")) if reviews else None


def review_age_hours() -> float | None:
    review = latest_review()
    if not review:
        return None
    return (datetime.now(timezone.utc) - datetime.fromisoformat(review["created_at"])).total_seconds() / 3600
