"""Async client for the SovereignEG OpenAI-compatible gateway."""

from __future__ import annotations

import asyncio
import json
import random
import re
import time
from dataclasses import dataclass
from typing import Any

import httpx

BASE_URL = "https://backend.sovereigneg.com/v1"
RETRYABLE_STATUSES = {429, 500, 503}
# 401 and 402 affect every request in the run, so continuing would only produce noise.
FATAL_STATUSES = {401, 402}

_CITATION_MARKUP = re.compile(r"【[^】]*】")


class APIError(Exception):
    def __init__(self, status: int, code: str | None, message: str, request_id: str | None = None):
        super().__init__(f"{status} {code or 'error'}: {message}")
        self.status = status
        self.code = code
        self.message = message
        self.request_id = request_id

    @property
    def retryable(self) -> bool:
        return self.status == 0 or self.status in RETRYABLE_STATUSES

    @property
    def fatal(self) -> bool:
        return self.status in FATAL_STATUSES


@dataclass(frozen=True)
class Pricing:
    """EGP per 1M tokens."""

    input_per_1m: float
    output_per_1m: float
    cache_read_per_1m: float | None = None

    def cost(self, prompt_tokens: int, completion_tokens: int, cached_tokens: int = 0) -> float:
        cache_rate = self.input_per_1m if self.cache_read_per_1m is None else self.cache_read_per_1m
        uncached = max(prompt_tokens - cached_tokens, 0)
        return (
            uncached * self.input_per_1m
            + cached_tokens * cache_rate
            + completion_tokens * self.output_per_1m
        ) / 1_000_000


@dataclass(frozen=True)
class ModelInfo:
    id: str
    modality: str
    pricing: Pricing
    supports_temperature: bool = True
    max_completion_tokens_only: bool = False
    status: str | None = None
    weights_type: str | None = None  # "open_weights" or "proprietary", as published in SovereignEG's catalog
    owned_by: str | None = None
    source: str | None = None  # "sovereign" = served from SovereignEG's own GPUs in Egypt; "serverless" = upstream
    engines: tuple[str, ...] = ()  # the providers SovereignEG may route this model to

    @classmethod
    def from_catalog(cls, entry: dict[str, Any]) -> ModelInfo:
        pricing = entry.get("pricing") or {}
        caps = entry.get("capabilities") or {}
        cache_read = pricing.get("cache_read_per_1m_egp")
        return cls(
            id=entry["id"],
            modality=entry.get("modality") or "chat",
            pricing=Pricing(
                input_per_1m=float(pricing.get("input_per_1m_egp") or 0),
                output_per_1m=float(pricing.get("output_per_1m_egp") or 0),
                cache_read_per_1m=None if cache_read is None else float(cache_read),
            ),
            supports_temperature=bool(caps.get("supports_temperature", True)),
            max_completion_tokens_only=bool(caps.get("requires_max_completion_tokens_only", False)),
            status=entry.get("status"),
            weights_type=entry.get("weights_type"),
            owned_by=entry.get("owned_by"),
            source=entry.get("source"),
            engines=tuple(entry.get("engines") or ()),
        )


@dataclass(frozen=True)
class Completion:
    text: str
    prompt_tokens: int
    completion_tokens: int
    cached_tokens: int
    latency_s: float
    request_id: str | None = None

    def cost(self, model: ModelInfo) -> float:
        return model.pricing.cost(self.prompt_tokens, self.completion_tokens, self.cached_tokens)


@dataclass(frozen=True)
class Decision:
    answers: dict[str, Any]
    input_tokens: int
    latency_s: float
    request_id: str | None = None
    cached: bool = False
    output_tokens: int = 0  # only when a chat model answered the questions

    def cost(self, model: ModelInfo) -> float:
        return model.pricing.cost(self.input_tokens, self.output_tokens)


DECIDE_BY_CHAT_SYSTEM = """You answer structured decision questions about the `state` you are given.
Give calibrated probabilities: use values close to 0 or 1 only when the answer is clear, and spread
probability across options when it is not. Reply with JSON only, in exactly the format requested."""


def _decision_prompt(state: Any, questions: dict[str, Any]) -> str:
    lines = ["State:", "```json", json.dumps(state, ensure_ascii=False, indent=1), "```", "", "Questions:"]
    reply: dict[str, Any] = {}
    for key, q in questions.items():
        lines.append(f"\n`{key}`: {q.get('instructions', '')}")
        criteria = q.get("criteria")
        if q["type"] == "noul":
            if isinstance(criteria, dict):
                lines += [f"  true means: {criteria.get('true', 'yes')}", f"  false means: {criteria.get('false', 'no')}"]
            reply[key] = {"probability_true": "number from 0 to 1"}
        elif q["type"] == "choice":
            lines += [f"  - {name}" + (f": {text}" if text else "") for name, text in criteria.items()]
            reply[key] = {"probabilities": {name: "number" for name in criteria}}
        elif q["type"] == "score":
            lines += [f"  {level}: {text}" for level, text in enumerate(criteria)]
            reply[key] = {"probabilities": {str(level): "number" for level in range(len(criteria))}}
        else:
            raise ValueError(f"unsupported question type {q['type']!r}")
    lines += ["", "Probabilities for one question add up to 1. Reply with JSON in this shape:",
              json.dumps(reply, indent=1)]
    return "\n".join(lines)


def _parse_decision(text: str, questions: dict[str, Any]) -> dict[str, Any]:
    """Turn a chat model's JSON reply into the same answer shapes the decisions endpoint returns."""
    from .graders import extract_json

    data = extract_json(text)
    if not isinstance(data, dict):
        raise ValueError("the reply must be a JSON object")
    answers: dict[str, Any] = {}
    for key, q in questions.items():
        raw = data.get(key)
        raw = raw if isinstance(raw, dict) else {}
        if q["type"] == "noul":
            answers[key] = {"noul": min(max(float(raw.get("probability_true", 0.5)), 0.0), 1.0)}
            continue
        names = list(q["criteria"]) if q["type"] == "choice" else [str(i) for i in range(len(q["criteria"]))]
        given = raw.get("probabilities")
        given = given if isinstance(given, dict) else {}
        probabilities = {name: max(float(given.get(name, 0) or 0), 0.0) for name in names}
        total = sum(probabilities.values())
        probabilities = {n: (p / total if total else 1 / len(names)) for n, p in probabilities.items()}
        best = max(probabilities, key=probabilities.get)
        if q["type"] == "choice":
            answers[key] = {"choice": best, "probabilities": probabilities, "confidence": probabilities[best]}
        else:
            answers[key] = {"score": sum(int(n) * p for n, p in probabilities.items()), "legend": q["criteria"],
                            "probabilities": probabilities, "confidence": probabilities[best]}
    return answers


def _message_text(message: dict[str, Any]) -> str:
    content = message.get("content")
    if isinstance(content, list):
        content = "".join(part.get("text", "") for part in content if isinstance(part, dict))
    return _CITATION_MARKUP.sub("", content or "").strip()


class SovereignClient:
    def __init__(
        self,
        api_key: str,
        base_url: str = BASE_URL,
        timeout_s: float = 90.0,
        max_retries: int = 3,
    ):
        self._http = httpx.AsyncClient(
            base_url=base_url,
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=timeout_s,
        )
        self._max_retries = max_retries
        self._no_temperature: set[str] = set()
        self._completion_tokens_only: set[str] = set()

    async def aclose(self) -> None:
        await self._http.aclose()

    async def list_models(self) -> dict[str, ModelInfo]:
        body, _, _ = await self._request("GET", "/models")
        models = {entry["id"]: ModelInfo.from_catalog(entry) for entry in body.get("data", [])}
        from .licenses import remember_catalog

        remember_catalog(models)
        return models

    async def chat(
        self,
        model: ModelInfo,
        messages: list[dict[str, Any]],
        max_tokens: int,
        temperature: float | None = None,
        extra: dict[str, Any] | None = None,
    ) -> Completion:
        payload: dict[str, Any] = {"model": model.id, "messages": messages}
        completion_only = model.max_completion_tokens_only or model.id in self._completion_tokens_only
        payload["max_completion_tokens" if completion_only else "max_tokens"] = max_tokens
        if temperature is not None and model.supports_temperature and model.id not in self._no_temperature:
            payload["temperature"] = temperature
        payload.update(extra or {})

        # Some upstream models reject parameters the catalog says they accept; adapt once and remember.
        for _ in range(5):
            try:
                body, headers, latency = await self._request("POST", "/chat/completions", payload)
                break
            except APIError as exc:
                message = exc.message.lower()
                if exc.status == 400 and "temperature" in payload and "temperature" in message:
                    self._no_temperature.add(model.id)
                    payload.pop("temperature")
                elif exc.status == 400 and "max_tokens" in payload and "max_completion_tokens" in message:
                    self._completion_tokens_only.add(model.id)
                    payload["max_completion_tokens"] = payload.pop("max_tokens")
                elif exc.status == 400 and (room := _context_room(message)):
                    limit_key = "max_completion_tokens" if "max_completion_tokens" in payload else "max_tokens"
                    if room >= payload[limit_key]:
                        raise
                    payload[limit_key] = room
                else:
                    raise
        else:
            raise APIError(400, "unsupported_parameters", f"{model.id} rejected the request parameters")
        usage = body.get("usage") or {}
        details = usage.get("prompt_tokens_details") or {}
        choices = body.get("choices") or [{}]
        return Completion(
            text=_message_text(choices[0].get("message") or {}),
            prompt_tokens=int(usage.get("prompt_tokens") or 0),
            completion_tokens=int(usage.get("completion_tokens") or 0),
            cached_tokens=int(details.get("cached_tokens") or 0),
            latency_s=latency,
            request_id=headers.get("x-request-id"),
        )

    async def decide(self, model: ModelInfo, state: Any, questions: dict[str, Any]) -> Decision:
        if model.modality == "chat":
            return await self._decide_by_chat(model, state, questions)
        payload = {"model": model.id, "state": state, "questions": questions}
        body, headers, latency = await self._request("POST", "/systemone", payload)
        usage = body.get("usage") or {}
        tokens = usage.get("prompt_tokens") or usage.get("input_tokens") or usage.get("total_tokens") or 0
        return Decision(
            answers=body.get("answers") or {},
            input_tokens=int(tokens),
            latency_s=latency,
            request_id=headers.get("x-request-id"),
        )

    async def _decide_by_chat(self, model: ModelInfo, state: Any, questions: dict[str, Any]) -> Decision:
        """Decision questions answered by a chat model (e.g. one hosted in Egypt), in the decision answer shapes."""
        from .jsonchat import chat_json

        messages = [{"role": "system", "content": DECIDE_BY_CHAT_SYSTEM},
                    {"role": "user", "content": _decision_prompt(state, questions)}]
        reply = await chat_json(
            self, model, messages, lambda text: _parse_decision(text, questions), max_tokens=4000,
            fail=lambda problem: APIError(502, "unreadable_decision", f"{model.id} did not reply with usable JSON: {problem}"),
        )
        return Decision(reply.value, reply.prompt_tokens, reply.latency_s, reply.request_id,
                        output_tokens=reply.completion_tokens)

    async def _request(
        self, method: str, path: str, payload: dict[str, Any] | None = None
    ) -> tuple[dict[str, Any], httpx.Headers, float]:
        for attempt in range(self._max_retries + 1):
            start = time.perf_counter()
            try:
                response = await self._http.request(method, path, json=payload)
            except httpx.HTTPError as exc:
                if attempt == self._max_retries:
                    raise APIError(0, "transport_error", repr(exc)) from exc
                await asyncio.sleep(_backoff(attempt))
                continue
            latency = time.perf_counter() - start

            if response.status_code < 400:
                return response.json(), response.headers, latency

            error = _parse_error(response)
            if error.retryable and attempt < self._max_retries:
                await asyncio.sleep(_retry_after(response) or _backoff(attempt))
                continue
            raise error
        raise AssertionError("unreachable")


def client_from_env(timeout_s: float = 90.0) -> SovereignClient | None:
    """Client configured from LLM_API_KEY / LLM_BASE_URL (environment or .env), or None without a key."""
    from .config import load_api_key, load_env_value

    key = load_api_key()
    if not key:
        return None
    return SovereignClient(key, base_url=load_env_value("LLM_BASE_URL") or BASE_URL, timeout_s=timeout_s)


_CONTEXT_LIMIT = re.compile(r"maximum context length is (\d+) tokens.*?contains (?:at least )?(\d+) input tokens")


def _context_room(message: str) -> int | None:
    """Output tokens that still fit when a small-context model rejects the requested max_tokens."""
    match = _CONTEXT_LIMIT.search(message)
    if not match:
        return None
    # The engine reports "at least N" input tokens, so leave headroom beyond N.
    room = int(match.group(1)) - int(int(match.group(2)) * 1.5) - 64
    return room if room > 0 else None


def _backoff(attempt: int) -> float:
    return min(2**attempt, 20) + random.uniform(0, 0.5)


def _retry_after(response: httpx.Response) -> float | None:
    value = response.headers.get("retry-after")
    try:
        return float(value) if value is not None else None
    except ValueError:
        return None


def _parse_error(response: httpx.Response) -> APIError:
    request_id = response.headers.get("x-request-id")
    try:
        error = response.json().get("error") or {}
    except ValueError:
        error = {}
    message = error.get("message") or response.text[:300] or response.reason_phrase
    return APIError(response.status_code, error.get("code"), message, request_id)
