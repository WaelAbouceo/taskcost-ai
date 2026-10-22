import pytest

from taskcost.client import ModelInfo, Pricing
from taskcost.metrics import summarize


def attempt(model, task, trial, attempt, passed, cost, **extra):
    return {
        "model": model, "model_id": model, "task_id": task, "category": "cat", "trial": trial,
        "attempt": attempt, "passed": passed, "cost_egp": cost, "grading_cost_egp": 0.0,
        "latency_s": 1.0, "error_kind": None, **extra,
    }


def test_pricing_counts_cached_tokens_at_cache_rate():
    pricing = Pricing(input_per_1m=10.0, output_per_1m=40.0, cache_read_per_1m=1.0)
    assert pricing.cost(1_000_000, 500_000, cached_tokens=400_000) == pytest.approx(6 + 0.4 + 20)


def test_catalog_entry_parsing():
    info = ModelInfo.from_catalog({
        "id": "m", "modality": "chat",
        "pricing": {"input_per_1m_egp": 42.98, "output_per_1m_egp": 257.86},
        "capabilities": {"supports_temperature": False, "requires_max_completion_tokens_only": True},
    })
    assert info.pricing.cache_read_per_1m is None
    assert not info.supports_temperature and info.max_completion_tokens_only


def test_failed_attempts_are_charged_to_successes():
    records = [
        attempt("m", "t1", 1, 1, False, 1.0),
        attempt("m", "t1", 1, 2, True, 1.0),
        attempt("m", "t2", 1, 1, True, 1.0),
        attempt("m", "t3", 1, 1, False, 1.0),
        attempt("m", "t3", 1, 2, False, 1.0),
    ]
    [summary] = summarize(records)
    assert summary.runs == 3 and summary.passed_runs == 2
    assert summary.total_cost_egp == 5.0
    assert summary.cost_per_success_egp == 2.5
    assert summary.attempts_per_success == 1.5


def test_reliability_requires_every_trial_to_pass():
    records = [
        attempt("m", "t1", 1, 1, True, 1.0),
        attempt("m", "t1", 2, 1, True, 1.0),
        attempt("m", "t2", 1, 1, True, 1.0),
        attempt("m", "t2", 2, 1, False, 1.0),
    ]
    [summary] = summarize(records)
    assert summary.success_rate == 0.75
    assert summary.reliability == 0.5


def test_ungraded_runs_are_excluded():
    records = [
        attempt("m", "t1", 1, 1, True, 1.0),
        attempt("m", "t2", 1, 1, False, 1.0, error_kind="grader"),
    ]
    [summary] = summarize(records)
    assert summary.runs == 1 and summary.ungraded_runs == 1 and summary.success_rate == 1.0


def test_ranking_puts_cheap_but_unreliable_models_last():
    records = [
        attempt("cheap", "t1", 1, 1, True, 0.1),
        attempt("cheap", "t2", 1, 1, False, 0.1),
        attempt("cheap", "t3", 1, 1, False, 0.1),
        attempt("solid", "t1", 1, 1, True, 1.0),
        attempt("solid", "t2", 1, 1, True, 1.0),
        attempt("solid", "t3", 1, 1, False, 1.0),
    ]
    summaries = summarize(records, min_success_rate=0.5)
    assert [s.model for s in summaries] == ["solid", "cheap"]
    assert summaries[0].qualifies and not summaries[1].qualifies
