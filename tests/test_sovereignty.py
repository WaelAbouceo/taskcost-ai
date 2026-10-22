import asyncio
import json
from dataclasses import replace

import pytest

import taskcost.hosting as hosting
from taskcost.client import Completion, ModelInfo, Pricing
from taskcost.config import BenchmarkConfig, HelperMode
from taskcost.sovereignty import apply_policy, fit, residency_ranks

LOCAL = "local-model"
BASE = BenchmarkConfig(
    name="t", tasks="tasks", models=(), judge_models=("judge-us",), country="EG",
    helper_modes=(HelperMode("egypt", "Keep my request in Egypt", "", {"quick_design_model": LOCAL, "profiler_model": LOCAL}),
                  HelperMode("global", "Global helpers", "", {"quick_design_model": "helper-us", "profiler_model": "jev"})),
)


@pytest.fixture
def snapshot():
    models = {
        LOCAL: {"locations": ["EG"], "verified": True, "egypt_hosted": True},
        "judge-us": {"locations": ["US"], "verified": True, "egypt_hosted": False},
        "helper-us": {"locations": ["US"], "verified": True, "egypt_hosted": False},
        "jev": {"locations": ["US", "*"], "verified": False, "egypt_hosted": False},
        "eu-model": {"locations": ["EU"], "verified": True, "egypt_hosted": False},
    }
    hosting.SNAPSHOT_FILE.write_text(json.dumps({"updated_at": "2026-10-22T00:00:00+00:00", "models": models,
                                                 "providers": {}}), encoding="utf-8")
    hosting._snapshot_cache.clear()
    return models


def test_fit_by_country_and_law():
    eg = {"locations": ["EG"], "verified": True}
    us = {"locations": ["US"], "verified": True}
    eu = {"locations": ["EU"], "verified": True}
    assert fit(eg, "EG") == "in_country" and fit(us, "EG") == "abroad"
    assert fit(eu, "DE") == "allowed" and fit({"locations": ["DE"], "verified": True}, "FR") == "allowed"  # GDPR
    assert fit({"locations": ["AE"], "verified": True}, "SA") == "region"  # both in the Gulf
    assert fit({"locations": ["US", "*"], "verified": False}, "US") == "unverified"  # forwarded to unnamed companies


def test_prefer_local_picks_local_helpers_and_notes_reviewers_abroad(snapshot):
    cfg = apply_policy(BASE)
    assert cfg.helper_mode == "egypt" and cfg.profiler_model == LOCAL
    assert cfg.judge_models == ("judge-us",) and any("outside Egypt" in n for n in cfg.sovereignty_notes)
    assert residency_ranks(cfg, [LOCAL, "judge-us", "jev"]) == {LOCAL: 0, "judge-us": 3, "jev": 4}


def test_open_uses_the_global_helpers_and_ignores_location(snapshot):
    cfg = apply_policy(replace(BASE, sovereignty="open"))
    assert cfg.helper_mode == "global" and residency_ranks(cfg, [LOCAL]) is None


def test_strict_keeps_everything_local_or_refuses(snapshot):
    cfg = apply_policy(replace(BASE, sovereignty="strict"))
    assert cfg.profiler_model == LOCAL and cfg.judge_models == (LOCAL,)  # the only local reviewer, with a warning
    assert any("only reviewer" in n for n in cfg.sovereignty_notes)
    with pytest.raises(ValueError, match="Strict mode"):
        apply_policy(replace(BASE, sovereignty="strict", country="SA"))  # nothing runs in Saudi Arabia


def test_quotes_must_be_on_the_page_and_name_the_place():
    pages = {"https://p": "Your data may be transferred, stored, and processed specifically in the United States."}
    ok = {"location": "US", "quote": "stored, and processed specifically in the United States."}
    assert hosting.verify_fact(ok, pages)["url"] == "https://p"
    assert hosting.verify_fact({"location": "US", "quote": "We run all servers in the United States of America."}, pages) is None
    assert hosting.verify_fact({"location": "EU", "quote": ok["quote"]}, pages) is None  # quote doesn't name the EU
    assert hosting.verify_fact({"location": "XX", "quote": ok["quote"]}, pages) is None


def test_research_keeps_the_last_verified_record_when_pages_say_nothing(monkeypatch):
    async def fake_fetch(urls, timeout_s=20.0):
        return {"https://p": "We care about privacy."}, [{"url": "https://p", "ok": True}]

    class Reader:
        async def chat(self, model, messages, max_tokens, temperature=None, extra=None):
            return Completion(json.dumps({"default_locations": [{"location": "US", "quote": "Servers are in the United States."}]}),
                              100, 50, 0, 1.0)

    monkeypatch.setattr(hosting, "fetch_pages", fake_fetch)
    reader = ModelInfo("reader", "chat", Pricing(1, 1))
    entry = {"name": "Prov", "pages": ["https://p"]}
    fresh = asyncio.run(hosting.research_provider(Reader(), reader, "prov", entry, None))
    assert fresh["status"] == "unverified" and fresh["locations"] == []  # the invented quote was rejected
    previous = {"name": "Prov", "verified": True, "locations": ["US"], "checked_at": "2026-10-21", "pages_hash": "old"}
    kept = asyncio.run(hosting.research_provider(Reader(), reader, "prov", entry, previous))
    assert kept["status"] == "kept" and kept["locations"] == ["US"]


def test_model_residency_from_its_engines():
    providers = {"a": {"verified": True, "locations": ["US"]}, "b": {"verified": True, "locations": ["EU"]},
                 "r": {"verified": True, "locations": ["US"], "routes_onward": True}}
    model = lambda *engines: replace(ModelInfo("m", "chat", Pricing(1, 1)), source="serverless", engines=engines)
    assert hosting.model_residency(model("a", "b"), providers) == {
        "locations": ["EU", "US"], "verified": True, "providers": ["a", "b"], "egypt_hosted": False}
    assert not hosting.model_residency(model("a", "r"), providers)["verified"]
    assert not hosting.model_residency(model("a", "missing"), providers)["verified"]
    local = replace(ModelInfo("q", "chat", Pricing(1, 1)), source="sovereign")
    assert hosting.model_residency(local, providers)["locations"] == ["EG"]
