"""Offline backend with simulated models, for trying the harness without an API key or credit.

Mock results only exercise the pipeline; they say nothing about real models.
"""

from __future__ import annotations

import random
from collections import defaultdict
from dataclasses import dataclass, replace
from typing import Any

from .client import Completion, Decision, ModelInfo, Pricing
from .config import BenchmarkConfig, ModelSpec
from .graders import normalize
from .plan import Plan
from .tasks import Task


@dataclass(frozen=True)
class MockProfile:
    accuracy: float
    latency_s: float
    reasoning_multiplier: float


MOCK_MODELS: dict[str, tuple[ModelInfo, MockProfile]] = {
    "mock-frontier": (
        ModelInfo("mock-frontier", "chat", Pricing(60.0, 240.0, 6.0)),
        MockProfile(accuracy=0.95, latency_s=3.0, reasoning_multiplier=4.0),
    ),
    "mock-balanced": (
        ModelInfo("mock-balanced", "chat", Pricing(8.0, 32.0, 0.8)),
        MockProfile(accuracy=0.85, latency_s=1.5, reasoning_multiplier=1.5),
    ),
    "mock-budget": (
        ModelInfo("mock-budget", "chat", Pricing(1.0, 4.0, 0.1)),
        MockProfile(accuracy=0.45, latency_s=0.8, reasoning_multiplier=1.0),
    ),
}
MOCK_JUDGE = ModelInfo("mock-judge", "decision", Pricing(2.4, 0.0))


def _tokens(text: str) -> int:
    return len(text) // 4 + 1


class MockBackend:
    """Answers a prompt with the reference answer of the task whose key text appears in it.

    For the standard suite the key is the whole prompt; for plans it is the simple task's
    instruction, because plan prompts also contain documents and earlier results.
    """

    def __init__(self, tasks: list[Task], seed: int = 7):
        self._answers = [(task.prompt, task.id, task.reference) for task in tasks]
        self._references = {normalize(t.reference) for t in tasks if t.reference}
        self._seed = seed
        self._calls: dict[tuple[str, str], int] = defaultdict(int)

    @classmethod
    def for_plan(cls, plan: Plan, seed: int = 7) -> MockBackend:
        backend = cls([], seed)
        nodes = sorted(plan.simple_tasks(), key=lambda n: -len(n.instruction))
        backend._answers = [(n.instruction, n.id, n.expected_answer) for n in nodes]
        backend._references = {normalize(n.expected_answer) for n in nodes if n.expected_answer}
        return backend

    def _match(self, prompt: str) -> tuple[str, str | None]:
        for key, task_id, reference in self._answers:
            if key == prompt or key in prompt:
                return task_id, reference
        raise ValueError("MockBackend only answers benchmark task prompts")

    async def list_models(self) -> dict[str, ModelInfo]:
        catalog = {model_id: info for model_id, (info, _) in MOCK_MODELS.items()}
        catalog[MOCK_JUDGE.id] = MOCK_JUDGE
        return catalog

    async def chat(
        self, model: ModelInfo, messages: list[dict[str, Any]], max_tokens: int,
        temperature: float | None = None, extra: dict[str, Any] | None = None,
    ) -> Completion:
        task_id, reference = self._match(messages[-1]["content"])
        profile = MOCK_MODELS[model.id][1]
        key = (model.id, task_id)
        self._calls[key] += 1
        rng = random.Random(f"{self._seed}|{model.id}|{task_id}|{self._calls[key]}")

        text = reference if reference and rng.random() < profile.accuracy else "I am not sure."
        prompt_text = "".join(m["content"] for m in messages)
        return Completion(
            text=text,
            prompt_tokens=_tokens(prompt_text),
            completion_tokens=int(_tokens(text) * profile.reasoning_multiplier),
            cached_tokens=0,
            latency_s=profile.latency_s * rng.uniform(0.6, 1.8),
        )

    async def decide(self, model: ModelInfo, state: Any, questions: dict[str, Any]) -> Decision:
        response = normalize(str(state.get("response", ""))) if isinstance(state, dict) else ""
        probability = 0.95 if response in self._references else 0.1
        return Decision(
            answers={name: {"noul": probability} for name in questions},
            input_tokens=_tokens(str(state) + str(questions)),
            latency_s=0.8,
        )

    async def aclose(self) -> None:
        pass


def mock_config(config: BenchmarkConfig) -> BenchmarkConfig:
    models = [ModelSpec(id=model_id, label=model_id) for model_id in MOCK_MODELS]
    return replace(config.with_models(models, [MOCK_JUDGE.id]), name=f"{config.name}-mock", test_mode=True)
