"""A small on-disk cache with expiry, so repeated analyses don't pay or wait twice.

Keys are hashes of everything that affects the answer (model, inputs, question wording), so changing
any of them simply misses the cache instead of returning a stale answer.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

from .client import Decision, ModelInfo


def cache_key(namespace: str, parts: Any) -> str:
    payload = json.dumps(parts, sort_keys=True, ensure_ascii=False, default=str)
    return f"{namespace}:{hashlib.sha256(payload.encode('utf-8')).hexdigest()}"


class Cache:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(path), check_same_thread=False)
        self._db.execute("CREATE TABLE IF NOT EXISTS entries (key TEXT PRIMARY KEY, value TEXT, created REAL)")
        self._lock = threading.Lock()

    def get(self, namespace: str, parts: Any, max_age_s: float) -> tuple[Any, float] | None:
        """Return (value, age in seconds), or None if missing or older than max_age_s."""
        if max_age_s <= 0:
            return None
        with self._lock:
            row = self._db.execute("SELECT value, created FROM entries WHERE key = ?",
                                   (cache_key(namespace, parts),)).fetchone()
        if row is None:
            return None
        age = time.time() - row[1]
        return (json.loads(row[0]), age) if age <= max_age_s else None

    def put(self, namespace: str, parts: Any, value: Any) -> None:
        with self._lock:
            self._db.execute(
                "INSERT OR REPLACE INTO entries (key, value, created) VALUES (?, ?, ?)",
                (cache_key(namespace, parts), json.dumps(value, ensure_ascii=False), time.time()),
            )
            self._db.commit()

    def clear(self, namespace: str | None = None) -> int:
        with self._lock:
            if namespace is None:
                cursor = self._db.execute("DELETE FROM entries")
            else:
                cursor = self._db.execute("DELETE FROM entries WHERE key LIKE ?", (f"{namespace}:%",))
            self._db.commit()
            return cursor.rowcount


class CachedDecisions:
    """Wraps a backend so identical decision-model calls within the TTL come from the cache, at no cost.

    Only decision calls (Jev, judges) are cached. Chat calls are never cached, because measuring their
    real cost and latency is the point of the benchmark.
    """

    def __init__(self, backend: Any, cache: Cache, ttl_s: float):
        self._backend = backend
        self._cache = cache
        self._ttl_s = ttl_s
        self.hits = 0
        self.misses = 0

    async def decide(self, model: ModelInfo, state: Any, questions: dict[str, Any]) -> Decision:
        parts = {"model": model.id, "state": state, "questions": questions}
        hit = self._cache.get("decision", parts, self._ttl_s)
        if hit is not None:
            self.hits += 1
            return Decision(answers=hit[0]["answers"], input_tokens=0, latency_s=0.0, request_id="cache", cached=True)
        self.misses += 1
        decision = await self._backend.decide(model, state, questions)
        self._cache.put("decision", parts, {"answers": decision.answers, "input_tokens": decision.input_tokens})
        return decision

    def __getattr__(self, name: str) -> Any:
        return getattr(self._backend, name)
