"""The hosting agent: where each model really runs, refreshed daily from sources it can prove.

1. SovereignEG's catalog is the authority on routing: models with `source: sovereign` run on its own GPUs
   in Egypt, and every other model's `engines` name the providers SovereignEG may send it to.
2. Each provider's official pages (knowledge/hosting_sources.yaml) are fetched, and a chat model reads the
   passages about locations. It must quote the page for every location it reports.
3. A location is accepted only if its quote appears word for word on a fetched page and names the place.
   Anything else is dropped; a provider with no verified location today keeps its last verified record.
   Pages that have not changed since the last verified check are not read again, so a quiet day is free.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import sys
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any, TextIO

import httpx
import yaml

from .client import ModelInfo
from .graders import extract_json
from .jsonchat import ChatBackend, chat_json

ROOT = Path(__file__).resolve().parent.parent
SOURCES_FILE = ROOT / "knowledge" / "hosting_sources.yaml"
JURISDICTIONS_FILE = ROOT / "knowledge" / "jurisdictions.yaml"
SNAPSHOT_FILE = ROOT / ".cache" / "hosting.json"
HISTORY_DIR = ROOT / ".cache" / "hosting-history"
UNKNOWN = "*"  # the provider forwards requests to companies it doesn't name, so the place can't be verified
PASSAGE_CHARS = 9000
MIN_QUOTE_CHARS = 20

EXTRACT_SYSTEM = """You read pages published by an AI provider and report where it processes or stores customers'
API requests. Use only the page text you are given.
- "default_locations": where requests are processed or stored by default.
- "optional_residency": locations a customer can choose or request instead (data residency options).
Use ISO country codes (US, GB, CA, IN, JP, SG, AU, CH, AE, SA, EG), or EU for the European Union or EEA.
If the text says that using regions other than a place needs approval or a special setting, or that data
is transferred to or processed in a place, that place is a default location.
For every location, copy one exact quote from the text (a full sentence, word for word) that names it.
If the text does not say where requests are processed, return empty lists. Never guess.
Reply with JSON only:
{"default_locations": [{"location": "US", "quote": "..."}], "optional_residency": [{"location": "EU", "quote": "..."}]}"""

_LOCATION_TERMS = re.compile(
    r"(united states|u\.s\.|\busa?\b|europe|\beu\b|\beea\b|united kingdom|\buk\b|region|data cent|located|"
    r"residency|stored|store|process|server|countr|transfer|jurisdiction|hosted|infrastructure)", re.I)


@lru_cache(maxsize=1)
def sources() -> dict[str, Any]:
    return yaml.safe_load(SOURCES_FILE.read_text(encoding="utf-8"))


@lru_cache(maxsize=1)
def jurisdictions() -> dict[str, Any]:
    return yaml.safe_load(JURISDICTIONS_FILE.read_text(encoding="utf-8"))


def page_text(html: str) -> str:
    html = re.sub(r"(?is)<(script|style|noscript|svg|head)[^>]*>.*?</\1>", " ", html)
    text = re.sub(r"(?s)<[^>]+>", " ", html)
    for entity, char in (("&nbsp;", " "), ("&amp;", "&"), ("&#39;", "'"), ("&quot;", '"'), ("&rsquo;", "'")):
        text = text.replace(entity, char)
    return re.sub(r"\s+", " ", text).strip()


def relevant_passages(text: str, limit: int = PASSAGE_CHARS) -> str:
    """The sentences about locations, each with one sentence either side, within `limit` characters.
    Sentences that name a place come first, so a long page's key passage isn't cut off."""
    sentences = re.split(r"(?<=[.!?])\s+", text)
    places = [w.strip() for words in jurisdictions()["location_words"].values() for w in words if len(w.strip()) > 3]

    def score(sentence: str) -> int:
        lowered = sentence.lower()
        return 2 * any(p in lowered for p in places) + bool(_LOCATION_TERMS.search(sentence))

    chosen: set[int] = set()
    size = 0
    for i in sorted((i for i, s in enumerate(sentences) if score(s)), key=lambda i: -score(sentences[i])):
        window = [j for j in (i - 1, i, i + 1) if 0 <= j < len(sentences) and j not in chosen]
        extra = sum(len(sentences[j]) + 1 for j in window)
        if size + extra > limit:
            continue
        chosen.update(window)
        size += extra
    return " ".join(sentences[i] for i in sorted(chosen))


def _norm(text: str) -> str:
    text = text.replace("\u2019", "'").replace("\u201c", '"').replace("\u201d", '"')
    return re.sub(r"\s+", " ", text).strip().lower()


def verify_fact(fact: Any, pages: dict[str, str]) -> dict[str, str] | None:
    """Accept a reported location only if its quote is on a fetched page and actually names the place."""
    if not isinstance(fact, dict):
        return None
    location = str(fact.get("location", "")).strip().upper()
    quote = str(fact.get("quote", "")).strip().strip('"')
    words = jurisdictions()["location_words"].get(location)
    if not words or len(quote) < MIN_QUOTE_CHARS:
        return None
    needle = _norm(quote)
    padded = f" {needle} "
    if not any(word in padded for word in words):
        return None
    url = next((u for u, text in pages.items() if needle in _norm(text)), None)
    return {"location": location, "quote": quote, "url": url} if url else None


async def fetch_pages(urls: list[str], timeout_s: float = 20.0) -> tuple[dict[str, str], list[dict[str, Any]]]:
    texts: dict[str, str] = {}
    report = []
    headers = {"User-Agent": "Mozilla/5.0 (compatible; TaskCost hosting agent)"}
    async with httpx.AsyncClient(follow_redirects=True, timeout=timeout_s, headers=headers) as http:
        responses = await asyncio.gather(*(http.get(u) for u in urls), return_exceptions=True)
    for url, response in zip(urls, responses):
        if isinstance(response, BaseException) or response.status_code >= 400:
            status = response.__class__.__name__ if isinstance(response, BaseException) else response.status_code
            report.append({"url": url, "ok": False, "status": status})
            continue
        text = page_text(response.text)
        texts[url] = text
        report.append({"url": url, "ok": True, "status": response.status_code, "chars": len(text)})
    return texts, report


async def research_provider(
    backend: ChatBackend, reader: ModelInfo, key: str, entry: dict[str, Any], previous: dict[str, Any] | None,
) -> dict[str, Any]:
    now = datetime.now(timezone.utc).isoformat()
    base = {"name": entry.get("name", key), "routes_onward": bool(entry.get("routes_onward")), "checked_at": now}
    if entry.get("catalog_location"):
        location = entry["catalog_location"]
        return {**base, "locations": [location], "optional_residency": [], "verified": True, "status": "catalog",
                "facts": [{"location": location, "quote": "Models with source \"sovereign\" in SovereignEG's catalog.",
                           "url": "https://backend.sovereigneg.com/v1/models"}], "pages": [], "cost_egp": 0.0}

    pages, report = await fetch_pages(entry.get("pages", []))
    digest = hashlib.sha256("".join(pages[u] for u in sorted(pages)).encode()).hexdigest()
    if previous and previous.get("verified") and previous.get("pages_hash") == digest:
        return {**previous, "checked_at": now, "status": "unchanged", "pages": report, "cost_egp": 0.0}

    found: dict[str, list[dict[str, str]]] = {"default_locations": [], "optional_residency": []}
    cost = 0.0
    if pages:
        material = "\n\n".join(f"## Page: {url}\n{relevant_passages(text)}" for url, text in pages.items())
        messages = [{"role": "system", "content": EXTRACT_SYSTEM},
                    {"role": "user", "content": f"Provider: {base['name']}\n\n{material}"}]

        def parse(text: str) -> dict[str, Any]:
            data = extract_json(text)
            if not isinstance(data, dict):
                raise ValueError("the reply must be a JSON object")
            return data

        try:
            reply = await chat_json(backend, reader, messages, parse, max_tokens=4000,
                                    fail=lambda problem: ValueError(f"unreadable reply: {problem}"))
            cost = reply.cost_egp
            for kind in found:
                facts = reply.value.get(kind)
                found[kind] = [f for f in map(lambda fact: verify_fact(fact, pages), facts if isinstance(facts, list) else []) if f]
        except ValueError as exc:
            base["error"] = str(exc)

    locations = sorted({f["location"] for f in found["default_locations"]})
    record = {
        **base, "pages": report, "pages_hash": digest, "cost_egp": round(cost, 6),
        "optional_residency": sorted({f["location"] for f in found["optional_residency"]}),
        "facts": [{"kind": kind, **f} for kind, facts in found.items() for f in facts],
    }
    if locations:
        return {**record, "locations": locations, "verified": True, "status": "verified"}
    if previous and previous.get("verified"):
        # Today's pages gave no checkable location: keep the last verified one, and say so.
        return {**previous, "checked_at": now, "status": "kept", "pages": report, "cost_egp": round(cost, 6),
                "note": f"No verified location on today's pages; kept the record from {previous.get('verified_at') or previous['checked_at']}."}
    return {**record, "locations": [], "verified": False, "status": "unverified"}


def model_residency(info: ModelInfo, providers: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """Every place the model may run: SovereignEG can route it to any of its engines."""
    if info.source == "sovereign":
        return {"locations": ["EG"], "verified": True, "providers": ["vllm"], "egypt_hosted": True}
    locations: set[str] = set()
    verified = bool(info.engines)
    for engine in info.engines:
        provider = providers.get(engine)
        if not provider or not provider.get("verified"):
            verified = False
            continue
        locations.update(provider["locations"])
        if provider.get("routes_onward"):
            locations.add(UNKNOWN)
            verified = False
    return {"locations": sorted(locations), "verified": verified and bool(locations), "providers": list(info.engines),
            "egypt_hosted": False}


def diff(previous: dict[str, Any] | None, current: dict[str, Any]) -> list[str]:
    if not previous:
        return ["First hosting check."]
    changes = []
    old_models, new_models = previous.get("models", {}), current["models"]
    gained = sorted(m for m, r in new_models.items() if r["egypt_hosted"] and not old_models.get(m, {}).get("egypt_hosted"))
    lost = sorted(m for m, r in old_models.items() if r.get("egypt_hosted") and not new_models.get(m, {}).get("egypt_hosted"))
    if gained:
        changes.append(f"Now hosted in Egypt: {', '.join(gained)}.")
    if lost:
        changes.append(f"No longer hosted in Egypt: {', '.join(lost)}.")
    for key, provider in current["providers"].items():
        before = previous.get("providers", {}).get(key)
        if before and before.get("locations") != provider.get("locations"):
            changes.append(f"{provider['name']}: locations changed from {before.get('locations') or 'unknown'} "
                           f"to {provider.get('locations') or 'unknown'}.")
        if before and before.get("verified") and not provider.get("verified"):
            changes.append(f"{provider['name']}: location can no longer be verified.")
    added = len(set(new_models) - set(old_models))
    if added:
        changes.append(f"{added} new models in the catalog.")
    return changes


async def refresh_hosting(backend: Any, reader_id: str, log: TextIO = sys.stderr) -> dict[str, Any]:
    """Run the hosting agent once and save the snapshot (and a dated copy for history)."""
    catalog: dict[str, ModelInfo] = await backend.list_models()
    if reader_id not in catalog:
        raise ValueError(f"hosting reader model {reader_id} is not in the catalog")
    previous = load_snapshot()
    chat_models = {m: info for m, info in catalog.items() if info.modality == "chat"}
    engines = sorted({e for info in chat_models.values() for e in info.engines})
    configured = sources()["providers"]
    print(f"Checking where {len(engines)} providers run: {', '.join(engines)}", file=log)
    records = await asyncio.gather(*(
        research_provider(backend, catalog[reader_id], key, configured.get(key, {"name": key}),
                          (previous or {}).get("providers", {}).get(key))
        for key in engines
    ))
    providers = dict(zip(engines, records))
    for key, record in providers.items():
        if record.get("status") == "verified":
            record["verified_at"] = record["checked_at"]
        print(f"  {record['name']}: {record['status']}, locations {record.get('locations') or 'unknown'}", file=log)
    snapshot = {
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "reader_model": reader_id,
        "providers": providers,
        "models": {m: model_residency(info, providers) for m, info in chat_models.items()},
        "cost_egp": round(sum(r.get("cost_egp", 0.0) for r in records), 6),
    }
    snapshot["changes"] = diff(previous, snapshot)
    SNAPSHOT_FILE.parent.mkdir(parents=True, exist_ok=True)
    SNAPSHOT_FILE.write_text(json.dumps(snapshot, indent=1, ensure_ascii=False), encoding="utf-8")
    HISTORY_DIR.mkdir(parents=True, exist_ok=True)
    (HISTORY_DIR / f"{snapshot['updated_at'][:10]}.json").write_text(json.dumps(snapshot, ensure_ascii=False), encoding="utf-8")
    _snapshot_cache.clear()
    return snapshot


_snapshot_cache: dict[float, dict[str, Any]] = {}


def load_snapshot() -> dict[str, Any] | None:
    try:
        mtime = SNAPSHOT_FILE.stat().st_mtime
    except OSError:
        return None
    if mtime not in _snapshot_cache:
        _snapshot_cache.clear()
        _snapshot_cache[mtime] = json.loads(SNAPSHOT_FILE.read_text(encoding="utf-8"))
    return _snapshot_cache[mtime]


def snapshot_age_hours() -> float | None:
    snapshot = load_snapshot()
    if not snapshot:
        return None
    updated = datetime.fromisoformat(snapshot["updated_at"])
    return (datetime.now(timezone.utc) - updated).total_seconds() / 3600


def residency_of(model_id: str) -> dict[str, Any]:
    """Where a model may run, from the latest snapshot (before the first check: Egypt-hosted or unverified)."""
    snapshot = load_snapshot()
    if snapshot and model_id in snapshot["models"]:
        return snapshot["models"][model_id]
    from .licenses import is_egypt_hosted

    if is_egypt_hosted(model_id):
        return {"locations": ["EG"], "verified": True, "providers": ["vllm"], "egypt_hosted": True}
    return {"locations": [], "verified": False, "providers": [], "egypt_hosted": False}
