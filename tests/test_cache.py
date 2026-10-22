import asyncio
import time

from taskcost.cache import Cache, CachedDecisions
from taskcost.client import Decision, ModelInfo, Pricing

JEV = ModelInfo("jev-1.13", "decision", Pricing(2.47, 0.0))


def test_get_put_and_expiry(tmp_path, monkeypatch):
    cache = Cache(tmp_path / "c.sqlite")
    cache.put("x", {"a": 1}, "value")
    value, age = cache.get("x", {"a": 1}, 60)
    assert value == "value" and age < 5
    assert cache.get("x", {"a": 2}, 60) is None
    assert cache.get("x", {"a": 1}, 0) is None

    now = time.time()
    monkeypatch.setattr(time, "time", lambda: now + 3601)
    assert cache.get("x", {"a": 1}, 3600) is None
    assert cache.clear() == 1


class CountingJev:
    def __init__(self):
        self.calls = 0

    async def decide(self, model, state, questions):
        self.calls += 1
        return Decision({"q": {"noul": 0.3}}, 500, 0.9)

    async def list_models(self):
        return {}


def test_cached_decisions_answer_repeats_for_free(tmp_path):
    backend = CountingJev()
    decider = CachedDecisions(backend, Cache(tmp_path / "c.sqlite"), ttl_s=3600)
    first = asyncio.run(decider.decide(JEV, {"task": "t"}, {"q": {"type": "noul"}}))
    second = asyncio.run(decider.decide(JEV, {"task": "t"}, {"q": {"type": "noul"}}))
    other = asyncio.run(decider.decide(JEV, {"task": "other"}, {"q": {"type": "noul"}}))
    assert backend.calls == 2 and decider.hits == 1 and decider.misses == 2
    assert second.cached and second.answers == first.answers and second.input_tokens == 0
    assert not other.cached
    assert asyncio.run(decider.list_models()) == {}  # other calls pass through
