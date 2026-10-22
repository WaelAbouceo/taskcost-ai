import asyncio
from dataclasses import replace

from taskcost.client import Decision, ModelInfo, Pricing
from taskcost.recommender import recommend, recommend_hybrid, rule_pick
from taskcost.registry import ModelCard, best_model, build_registry, task_results

CARDS = {
    "small": ModelCard("small", "Fast", 30, 0.93, 7.0, 13.8, 0.005),
    "mini": ModelCard("mini", "Balanced", 30, 0.97, 1.4, 2.9, 0.046),
    "big": ModelCard("big", "Frontier", 3, 1.0, 27.0, 34.0, 3.4),
}
JEV = ModelInfo("jev-1.13", "decision", Pricing(2.47, 0.0))


class FakeJev:
    def __init__(self, answers):
        self.answers = answers

    async def decide(self, model, state, questions):
        return Decision(self.answers, 400, 0.5)


def test_latency_limits_per_use_case():
    assert not CARDS["small"].fits("chat") and CARDS["small"].fits("realtime") and CARDS["big"].fits("batch")
    assert rule_pick(CARDS, "chat") == "mini" and rule_pick(CARDS, "batch") == "small"


def test_speed_check_replaces_a_choice_that_is_too_slow():
    jev = FakeJev({"model": {"choice": "big", "probabilities": {"big": 0.6, "small": 0.3, "mini": 0.1}, "confidence": 0.3}})
    rec = asyncio.run(recommend(jev, JEV, "task", "chat", CARDS))
    assert rec.jev_choice == "big" and rec.model == "mini" and rec.guard_applied


def test_hybrid_moves_to_the_most_reliable_model_only_when_jev_says_the_task_is_hard():
    easy = asyncio.run(recommend_hybrid(FakeJev({"needs_stronger": {"noul": 0.2}}), JEV, "task", "batch", CARDS))
    hard = asyncio.run(recommend_hybrid(FakeJev({"needs_stronger": {"noul": 0.9}}), JEV, "task", "batch", CARDS))
    assert easy.model == "small" and hard.model == "big"  # big has the best pass record
    chat_hard = asyncio.run(recommend_hybrid(FakeJev({"needs_stronger": {"noul": 0.9}}), JEV, "task", "chat", CARDS))
    assert chat_hard.model == "mini"  # the only model fast enough for a chatbot


OPEN_CARDS = {
    "open-small": ModelCard("open-small", "Fast", 30, 0.95, 6.0, 14.0, 0.005, open_weights=True),
    "open-weak": ModelCard("open-weak", "Fast", 30, 0.60, 2.0, 3.0, 0.001, open_weights=True),
    "open-strong": ModelCard("open-strong", "Thinking", 30, 1.0, 5.0, 12.0, 0.04, open_weights=True),
    "closed-fast": ModelCard("closed-fast", "Balanced", 30, 0.97, 1.5, 3.0, 0.002),
}


def test_open_source_first_with_a_reliability_floor_and_proprietary_fallback():
    jev = FakeJev({"needs_stronger": {"noul": 0.1}})
    batch = asyncio.run(recommend_hybrid(jev, JEV, "t", "batch", OPEN_CARDS, prefer_open=True, min_pass_rate=0.85))
    assert batch.model == "open-small"  # open beats the cheaper proprietary model; the weak open one is excluded
    chat = asyncio.run(recommend_hybrid(jev, JEV, "t", "chat", OPEN_CARDS, prefer_open=True, min_pass_rate=0.85))
    assert chat.model == "closed-fast" and "proprietary model is the fallback" in chat.note
    cheapest = asyncio.run(recommend_hybrid(jev, JEV, "t", "batch", OPEN_CARDS, prefer_open=False, min_pass_rate=0.85))
    assert cheapest.model == "closed-fast"
    hard = asyncio.run(recommend_hybrid(FakeJev({"needs_stronger": {"noul": 0.9}}), JEV, "t", "batch", OPEN_CARDS,
                                        prefer_open=True, min_pass_rate=0.85))
    assert hard.model == "open-strong"


EGYPT_CARDS = {
    **OPEN_CARDS,
    "egypt": ModelCard("egypt", "Egypt-hosted", 20, 0.95, 5.0, 12.5, 0.058, open_weights=True, egypt_hosted=True),
}


def test_egypt_hosted_open_source_first_then_open_abroad_then_proprietary():
    easy = FakeJev({"needs_stronger": {"noul": 0.1}})
    batch = asyncio.run(recommend_hybrid(easy, JEV, "t", "batch", EGYPT_CARDS, min_pass_rate=0.85, prefer_egypt=True))
    assert batch.model == "egypt" and "Egypt-hosted open-source" in batch.note  # beats cheaper open models abroad
    hard = asyncio.run(recommend_hybrid(FakeJev({"needs_stronger": {"noul": 0.9}}), JEV, "t", "batch", EGYPT_CARDS,
                                        min_pass_rate=0.85, prefer_egypt=True))
    assert hard.model == "egypt"  # the most reliable model among the preferred kind
    chat = asyncio.run(recommend_hybrid(easy, JEV, "t", "chat", EGYPT_CARDS, min_pass_rate=0.85, prefer_egypt=True))
    assert chat.model == "closed-fast"  # nothing open is fast enough for a chatbot
    off = asyncio.run(recommend_hybrid(easy, JEV, "t", "batch", EGYPT_CARDS, min_pass_rate=0.85))
    assert off.model == "open-small"
    assert rule_pick(EGYPT_CARDS, "realtime", prefer_egypt=True) == "egypt"


def test_abroad_note_when_the_egypt_hosted_model_is_too_slow():
    rec = asyncio.run(recommend_hybrid(FakeJev({"needs_stronger": {"noul": 0.1}}), JEV, "t", "batch",
                                       {**EGYPT_CARDS, "egypt": replace(EGYPT_CARDS["egypt"], pass_rate=0.5)},
                                       min_pass_rate=0.85, prefer_egypt=True))
    assert rec.model == "open-small" and "runs abroad" in rec.note


def test_egypt_hosting_comes_from_the_catalog_source(monkeypatch, tmp_path):
    import taskcost.licenses as licenses

    monkeypatch.setattr(licenses, "HOSTING_FILE", tmp_path / "hosted.json")
    monkeypatch.setattr(licenses, "CATALOG_FILE", tmp_path / "weights.json")
    monkeypatch.setattr(licenses, "_egypt", None)
    monkeypatch.setattr(licenses, "_catalog", None)
    open_ = lambda source: replace(ModelInfo("m", "chat", Pricing(1, 1)), weights_type="open_weights", source=source)
    licenses.remember_catalog({"local": replace(open_("sovereign"), id="local"), "abroad": replace(open_("serverless"), id="abroad")})
    assert licenses.is_egypt_hosted("local") and not licenses.is_egypt_hosted("abroad")
    monkeypatch.setattr(licenses, "_egypt", None)
    assert licenses.is_egypt_hosted("local")  # saved, so it also works offline


def test_small_context_models_get_a_smaller_output_limit():
    from taskcost.client import _context_room

    message = ("this model's maximum context length is 4096 tokens. however, you requested 4000 output tokens "
               "and your prompt contains at least 97 input tokens, for a total of at least 4097 tokens")
    assert _context_room(message) == 4096 - 145 - 64
    assert _context_room("some other error") is None


def test_a_chat_model_answers_decision_questions_in_the_same_shapes():
    import json

    from taskcost.blueprint import SIZE_QUESTION
    from taskcost.client import Completion, SovereignClient
    from taskcost.recommender import HARDNESS_QUESTION

    questions = {**SIZE_QUESTION, **HARDNESS_QUESTION,
                 "effort": {"type": "score", "instructions": "How much?", "criteria": ["none", "some", "a lot"]}}
    replies = ["not json", "Thinking... " + json.dumps({
        "level": {"probabilities": {"project": 3, "workflow": 1}},
        "needs_stronger": {"probability_true": 0.7},
        "effort": {"probabilities": {"0": 0.0, "1": 0.5, "2": 0.5}},
    })]

    class Client(SovereignClient):
        async def chat(self, model, messages, max_tokens, temperature=None, extra=None):
            assert "an ERP" in messages[1]["content"] and "workflow" in messages[1]["content"]
            return Completion(replies.pop(0), 300, 100, 0, 2.0)

    client = Client("key")
    qwen = ModelInfo("qwen-local", "chat", Pricing(10.0, 60.0))
    decision = asyncio.run(client.decide(qwen, {"request": "an ERP"}, questions))
    asyncio.run(client.aclose())
    assert decision.answers["level"]["choice"] == "project" and decision.answers["level"]["probabilities"]["project"] == 0.75
    assert decision.answers["needs_stronger"] == {"noul": 0.7}
    assert decision.answers["effort"]["score"] == 1.5
    assert decision.cost(qwen) == qwen.pricing.cost(600, 200)  # both tries are billed, output included


def test_open_weight_labels_fall_back_to_the_family_list(monkeypatch):
    import taskcost.licenses as licenses

    monkeypatch.setattr(licenses, "_catalog", {})
    assert licenses.is_open_weights("gpt-oss-20b") and licenses.is_open_weights("qwen3.8-27b")
    assert not licenses.is_open_weights("gpt-5.4-mini") and not licenses.is_open_weights("claude-opus-5.5")
    assert not licenses.is_open_weights("qwen3.8-max") and not licenses.is_open_weights("qwen3-coder-flash")


def test_sovereigneg_catalog_label_wins(monkeypatch):
    import taskcost.licenses as licenses

    monkeypatch.setattr(licenses, "_catalog", {"gemma-4-31b-it-ultra": "proprietary", "new-open-model": "open_weights"})
    assert not licenses.is_open_weights("gemma-4-31b-it-ultra")  # the family list alone would say open
    assert licenses.is_open_weights("new-open-model")
    assert licenses.label_source("new-open-model") == "SovereignEG catalog"


def record(model, task, passed, cost, latency):
    return {"model": model, "task_id": task, "passed": passed, "cost_egp": cost, "latency_s": latency,
            "error_kind": None, "tier": "x"}


def test_registry_and_ground_truth():
    records = [record("a", "t1", True, 0.01, 8.0), record("a", "t1", True, 0.01, 9.0),
               record("b", "t1", True, 0.05, 1.0), record("b", "t1", True, 0.05, 1.2),
               record("a", "t2", False, 0.01, 8.0), record("b", "t2", True, 0.05, 1.1)]
    cards = build_registry(records)
    assert list(cards) == ["a", "b"] and cards["a"].pass_rate == 2 / 3
    results = task_results(records)
    assert best_model("t1", "batch", cards, results) == "a"
    assert best_model("t1", "chat", cards, results) == "b"
    assert best_model("t2", "batch", cards, results) == "b"
