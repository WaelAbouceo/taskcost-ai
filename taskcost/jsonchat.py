"""Ask a chat model for JSON, and give it one chance to fix a reply that can't be used."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Generic, Protocol, TypeVar

from .client import Completion, ModelInfo

T = TypeVar("T")
RETRY_HINT = "That reply has problems: {problem}. Reply with the corrected JSON only."


class ChatBackend(Protocol):
    async def chat(
        self, model: ModelInfo, messages: list[dict[str, Any]], max_tokens: int,
        temperature: float | None = None, extra: dict[str, Any] | None = None,
    ) -> Completion: ...


@dataclass(frozen=True)
class JSONReply(Generic[T]):
    value: T
    cost_egp: float
    prompt_tokens: int
    completion_tokens: int
    latency_s: float
    request_id: str | None


async def chat_json(
    backend: ChatBackend,
    model: ModelInfo,
    messages: list[dict[str, Any]],
    parse: Callable[[str], T],
    max_tokens: int,
    fail: Callable[[str], Exception],
    retry_hint: str = RETRY_HINT,
    tries: int = 2,
) -> JSONReply[T]:
    """`parse` turns the reply text into a value, raising ValueError, KeyError or TypeError if it can't.
    Every attempt is billed; `fail(problem)` is raised when no attempt can be used."""
    messages = list(messages)
    cost = 0.0
    prompt_tokens = completion_tokens = 0
    latency = 0.0
    problem = ""
    for _ in range(tries):
        completion = await backend.chat(model, messages, max_tokens=max_tokens, temperature=0.0)
        cost += completion.cost(model)
        prompt_tokens += completion.prompt_tokens
        completion_tokens += completion.completion_tokens
        latency += completion.latency_s
        try:
            value = parse(completion.text)
        except (ValueError, KeyError, TypeError) as exc:
            problem = str(exc)
            messages += [{"role": "assistant", "content": completion.text},
                         {"role": "user", "content": retry_hint.format(problem=problem)}]
            continue
        return JSONReply(value, cost, prompt_tokens, completion_tokens, latency, completion.request_id)
    raise fail(problem)
