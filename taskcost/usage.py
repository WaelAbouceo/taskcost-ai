"""Usage log: one JSON line per meaningful event, for the log reviewer agent.

Events never store the user's raw request text: only sizes, choices, outcomes, costs and durations, plus
the short title of the concrete task. Files are monthly (`.cache/usage/events-YYYY-MM.jsonl`).
"""

from __future__ import annotations

import json
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
USAGE_DIR = ROOT / ".cache" / "usage"
_lock = threading.Lock()


def log_event(kind: str, /, **fields: Any) -> None:
    """Append one event. Logging must never break a request, so errors are swallowed."""
    event = {**fields, "at": datetime.now(timezone.utc).isoformat(), "kind": kind}
    try:
        USAGE_DIR.mkdir(parents=True, exist_ok=True)
        path = USAGE_DIR / f"events-{event['at'][:7]}.jsonl"
        with _lock, path.open("a", encoding="utf-8") as sink:
            sink.write(json.dumps(event, ensure_ascii=False, default=str) + "\n")
    except OSError:
        pass


def has_events() -> bool:
    return any(USAGE_DIR.glob("events-*.jsonl"))


def load_events(days: float | None = None) -> list[dict[str, Any]]:
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat() if days else ""
    events = []
    for path in sorted(USAGE_DIR.glob("events-*.jsonl")):
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                event = json.loads(line)
                if event["at"] >= since:
                    events.append(event)
    return events


def comparison_event(summary: dict[str, Any], seconds: float | None = None, rerun: bool = False) -> dict[str, Any]:
    """The facts about one finished comparison (from quick.quick_summary)."""
    tiers = summary.get("tiers", [])
    recommended = next((t for t in tiers if summary.get("recommended") and t["model"] == summary["recommended"]["model"]), None)
    sovereignty = summary.get("sovereignty") or {}
    return {
        "run": summary["name"], "title": summary.get("title"), "task_type": summary.get("task_type"),
        "use_case": summary.get("use_case"), "use_case_guessed": bool(summary.get("use_case_guess")),
        "country": sovereignty.get("country"), "sovereignty": sovereignty.get("level"),
        "helper_mode": summary.get("helper_mode"), "trials": summary.get("trials"), "rerun": rerun,
        "documents": len(summary.get("documents") or []),
        "models": [{"tier": t["tier"], "model": t["model"], "passed": t["passed"], "runs": t["runs"], "errors": t["errors"],
                    "fast_enough": t["fast_enough"], "latency_s": t["latency_s"], "cost_per_1000_egp": t["cost_per_1000_egp"],
                    "open_weights": t["open_weights"], "fit": (t.get("residency") or {}).get("fit")} for t in tiers],
        "skipped_models": [t["model"] for t in sovereignty.get("skipped_tiers") or []],
        "recommended": recommended and {"model": recommended["model"], "tier": recommended["tier"],
                                        "open_weights": recommended["open_weights"],
                                        "fit": (recommended.get("residency") or {}).get("fit"),
                                        "cost_per_1000_egp": recommended["cost_per_1000_egp"]},
        "no_winner": recommended is None,
        "proprietary_fallback": bool(summary.get("proprietary_fallback")),
        "local_fallback": bool(summary.get("local_fallback")),
        "agent1_verdict": (summary.get("advice") or {}).get("verdict"),
        "design_cost_egp": summary.get("design_cost_egp") or 0.0,
        "judge_cost_egp": summary.get("judge_cost_egp") or 0.0,
        "model_cost_egp": sum((a.get("cost_egp") or 0.0) for t in tiers for a in t.get("answers", [])),
        "seconds": None if seconds is None else round(seconds, 1),
    }


def blueprint_event(blueprint: dict[str, Any], name: str, seconds: float | None = None) -> dict[str, Any]:
    steps = [s for w in blueprint.get("workflows", []) for s in w["steps"] if s["actor"] == "ai"]
    policy = blueprint.get("sovereignty") or {}
    return {
        "run": name, "title": blueprint.get("title"), "level": blueprint.get("level"),
        "workflows": len(blueprint.get("workflows", [])), "ai_steps": len(steps),
        "use_cases": [s.get("use_case") for s in steps], "models": [s.get("model") for s in steps],
        "monthly_cost_egp": blueprint.get("monthly_cost_egp"), "unpriced_steps": len(blueprint.get("unpriced_steps") or []),
        "one_time_cost_egp": blueprint.get("one_time_cost_egp") or 0.0,
        "build_steps": sum(1 for s in steps if s.get("phase") == "build"),
        "country": policy.get("country"), "sovereignty": policy.get("level"), "helper_mode": blueprint.get("helper_mode"),
        "design_cost_egp": (blueprint.get("design") or {}).get("cost_egp") or 0.0,
        "agent1_cost_egp": (blueprint.get("estimate") or {}).get("profiler_cost_egp") or 0.0,
        "seconds": None if seconds is None else round(seconds, 1),
    }


def backfill_from_results(results_dir: Path) -> int:
    """Rebuild events from runs saved before the usage log existed, so the first review has data."""
    from .quick import quick_summary

    written = 0
    for config_path in sorted(results_dir.glob("quick-*/config.json")):
        try:
            summary = quick_summary(config_path.parent)
        except Exception:
            continue
        event = comparison_event(summary)
        _write_backfilled("comparison_done", event, summary.get("started_at"))
        written += 1
    for path in sorted(results_dir.glob("blueprint-*/blueprint.json")):
        blueprint = json.loads(path.read_text(encoding="utf-8"))
        _write_backfilled("blueprint_done", blueprint_event(blueprint, path.parent.name),
                          (blueprint.get("design") or {}).get("created_at"))
        written += 1
        if blueprint.get("verification"):
            v = blueprint["verification"]
            _write_backfilled("measured", {"run": path.parent.name, "steps": v.get("ai_steps"),
                                           "measured_steps": v.get("measured_steps"),
                                           "cost_egp": sum((v.get("cost_egp") or {}).values())}, v.get("finished_at"))
            written += 1
    return written


def _write_backfilled(kind: str, fields: dict[str, Any], at: str | None) -> None:
    event = {"at": at or datetime.now(timezone.utc).isoformat(), "kind": kind, "backfilled": True, **fields}
    USAGE_DIR.mkdir(parents=True, exist_ok=True)
    with _lock, (USAGE_DIR / f"events-{event['at'][:7]}.jsonl").open("a", encoding="utf-8") as sink:
        sink.write(json.dumps(event, ensure_ascii=False, default=str) + "\n")
