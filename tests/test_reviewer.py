import asyncio
import json

from taskcost.client import Completion, ModelInfo, Pricing
from taskcost.reviewer import check_recommendations, compute_metrics, latest_review, review_usage, signals
from taskcost.usage import load_events, log_event


def comparison(use_case="batch", guessed=True, recommended="local", proprietary=False, verdict="best", slow=False):
    return {
        "kind": "comparison_done", "at": "2026-10-22T10:00:00+00:00", "use_case": use_case, "use_case_guessed": guessed,
        "title": "Delayed-order email", "task_type": "generation", "country": "EG", "sovereignty": "prefer_local",
        "documents": 0, "rerun": False, "no_winner": False, "proprietary_fallback": proprietary, "local_fallback": False,
        "agent1_verdict": verdict, "design_cost_egp": 0.1, "judge_cost_egp": 0.1, "model_cost_egp": 0.2, "seconds": 30,
        "recommended": {"model": recommended}, "skipped_models": [],
        "models": [{"model": "local", "tier": "Egypt-hosted", "passed": 1, "runs": 1, "errors": 0, "fast_enough": not slow,
                    "latency_s": 6.0, "cost_per_1000_egp": 50, "open_weights": True, "fit": "in_country"}],
    }


def test_metrics_and_signals_from_the_usage_log():
    events = [comparison(), comparison(use_case="chat", recommended="mini", proprietary=True, slow=True),
              comparison(use_case="chat", recommended="mini", proprietary=True, slow=True, verdict="failed"),
              {"kind": "use_case_changed", "at": "2026-10-22T11:00:00+00:00", "guessed": True}]
    m = compute_metrics(events)
    assert m["activity.comparisons"] == 3 and m["use_case.share_chat"] == 0.667
    assert m["use_case.override_rate"] == 0.333 and m["outcome.chat_proprietary_rate"] == 1.0
    assert m["model.local.too_slow_rate_chat"] == 1.0 and m["agent1.failed_rate"] == 0.333
    assert m["cost.helpers_and_reviewers_share"] == 0.5
    flagged = {s["metric"] for s in signals(m)}
    assert {"use_case.override_rate", "outcome.chat_proprietary_rate", "agent1.failed_rate"} <= flagged


def test_recommendations_must_cite_real_metrics_and_real_numbers():
    metrics = {"use_case.override_rate": 0.333, "activity.comparisons": 3}
    raw = [
        {"title": "Ask before guessing", "priority": "high", "area": "user_experience", "why": "A third of guesses are corrected.",
         "action": "Show the guess first.", "evidence": ["use_case.override_rate"]},
        {"title": "Invented metric", "why": "x", "action": "y", "evidence": ["made.up"]},
        {"title": "Wrong number", "why": "Users corrected 80% of guesses.", "action": "y", "evidence": ["use_case.override_rate"]},
        {"title": "Right number", "why": "Users corrected 33% of guesses on gpt-5.4-mini.", "action": "y",
         "evidence": ["use_case.override_rate"]},
        {"title": "No evidence", "why": "x", "action": "y", "evidence": []},
    ]
    kept, dropped = check_recommendations(raw, metrics)
    assert [r["title"] for r in kept] == ["Ask before guessing", "Right number"] and dropped == 3
    assert kept[0]["evidence"] == {"use_case.override_rate": 0.333}


def test_review_needs_enough_usage_and_saves_the_review():
    class Reviewer:
        async def chat(self, model, messages, max_tokens, temperature=None, extra=None):
            payload = json.loads(messages[1]["content"])
            assert "metric_meanings" in payload and "product_facts" in payload
            return Completion(json.dumps({"summary": "Mostly batch emails.", "recommendations": [
                {"title": "Faster local model", "area": "models", "priority": "high", "why": "Chat is slow locally.",
                 "action": "Host a smaller model.", "evidence": ["model.local.too_slow_rate_chat"]}]}), 500, 200, 0, 3.0)

    model = ModelInfo("reviewer", "chat", Pricing(10, 60))
    thin = asyncio.run(review_usage(Reviewer(), model, days=7))
    assert thin["recommendations"] == [] and "Not enough usage" in thin["summary"]
    for event in [comparison(use_case="chat", slow=True) for _ in range(3)]:
        log_event("comparison_done", **{k: v for k, v in event.items() if k not in ("kind", "at")})
    assert len(load_events(7)) == 3
    review = asyncio.run(review_usage(Reviewer(), model, days=7))
    assert review["recommendations"][0]["evidence"] == {"model.local.too_slow_rate_chat": 1.0}
    assert latest_review()["created_at"] == review["created_at"] and review["cost_egp"] > 0


def test_changes_are_judged_on_usage_before_and_after_them():
    from taskcost.reviewer import change_effects, effect_metrics

    events = [{**comparison(use_case="chat", recommended="mini", proprietary=True, slow=True), "at": "2026-10-22T10:00:00+00:00"},
              {**comparison(use_case="chat"), "at": "2026-10-22T14:00:00+00:00"},
              {**comparison(use_case="chat"), "at": "2026-10-22T15:00:00+00:00", "backfilled": True}]
    effects = change_effects(events, [{"at": "2026-10-22T12:00:00+00:00", "title": "Fast local chat model"}])
    chat = effects[0]["metrics"]["outcome.chat_proprietary_rate"]
    assert chat == {"before": 1.0, "after": 0.0} and effects[0]["comparisons_after"] == 1  # backfilled events don't count
    assert effect_metrics(effects)["after:2026-10-22T12:00:outcome.chat_proprietary_rate"] == 0.0
