"""Sovereignty: rank and filter models by where they run, for the user's country and chosen level.

Levels
- open: where a model runs doesn't matter; the cheapest correct model wins.
- prefer_local: models in the user's country first, then places their law allows, then their region,
  then anywhere; each fallback is explained.
- strict: only models in the user's country or places their law allows. Helper models and reviewers must
  qualify too, so the request never leaves those places.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any, Iterable

from .config import BenchmarkConfig
from .hosting import UNKNOWN, jurisdictions, residency_of

LEVELS = {"open": "Open", "prefer_local": "Prefer local", "strict": "Strict"}
FIT_RANK = {"in_country": 0, "allowed": 1, "region": 2, "abroad": 3, "unverified": 4}
ACCEPTED = {"in_country", "allowed"}


def countries() -> dict[str, dict[str, Any]]:
    return jurisdictions()["countries"]


def _region_of(location: str) -> str | None:
    info = countries().get(location)
    return info["region"] if info else (location if location in jurisdictions()["regions"] else None)


def fit(residency: dict[str, Any], country: str) -> str:
    """How a model's locations fit a user in `country`: in_country, allowed, region, abroad or unverified."""
    locations = residency.get("locations") or []
    if not residency.get("verified") or not locations or UNKNOWN in locations:
        return "unverified"
    if all(loc == country for loc in locations):
        return "in_country"
    info = countries().get(country) or {"region": None, "allowed": []}
    allowed = set(info.get("allowed", []))
    if all(loc == country or loc in allowed or _region_of(loc) in allowed for loc in locations):
        return "allowed"
    if info.get("region") and all(loc == country or _region_of(loc) == info["region"] for loc in locations):
        return "region"
    return "abroad"


def model_fit(model_id: str, country: str) -> dict[str, Any]:
    residency = residency_of(model_id)
    name = fit(residency, country)
    return {"fit": name, "rank": FIT_RANK[name], "locations": residency.get("locations", []),
            "verified": bool(residency.get("verified")), "egypt_hosted": bool(residency.get("egypt_hosted"))}


def residency_ranks(config: BenchmarkConfig, model_ids: Iterable[str]) -> dict[str, int] | None:
    """Ranking by location (lower is better), or None when location doesn't matter."""
    if config.sovereignty == "open":
        return None
    return {m: FIT_RANK[fit(residency_of(m), config.country)] for m in model_ids}


def accepted(config: BenchmarkConfig, model_id: str) -> bool:
    return config.sovereignty != "strict" or fit(residency_of(model_id), config.country) in ACCEPTED


def country_name(code: str) -> str:
    return (countries().get(code) or {}).get("name", code)


def apply_policy(config: BenchmarkConfig, helpers: str = "") -> BenchmarkConfig:
    """Choose helper models and reviewers that respect the user's country and level.

    `helpers` forces a helper mode; otherwise Prefer local and Strict use a mode whose models all run in
    places the user's law accepts, and Open uses the first mode that isn't local (the cheaper helpers).
    """
    if config.sovereignty not in LEVELS:
        raise ValueError(f"sovereignty must be one of {list(LEVELS)}")
    country = country_name(config.country)
    notes: list[str] = []
    local = lambda models: all(fit(residency_of(m), config.country) in ACCEPTED for m in models)
    if helpers:
        config = config.with_helper_mode(helpers)
    elif config.helper_modes:
        local_modes = [m for m in config.helper_modes if local(m.models.values())]
        if config.sovereignty == "open":
            chosen = next((m for m in config.helper_modes if m not in local_modes), config.helper_modes[0])
        elif local_modes:
            chosen = local_modes[0]
        elif config.sovereignty == "strict":
            raise ValueError(f"Strict mode: no model that can analyse your request runs in {country} or a place "
                             "your law allows, so nothing was sent anywhere.")
        else:
            chosen = next((m for m in config.helper_modes if m.id == config.helper_mode), config.helper_modes[0])
            notes.append(f"No helper model runs in {country}, so your request was analysed abroad.")
        config = config.with_helper_mode(chosen.id)

    judges = list(config.judge_models)
    if config.sovereignty == "strict":
        kept = [j for j in judges if fit(residency_of(j), config.country) in ACCEPTED]
        if not kept:
            helper_models = {getattr(config, f) for f in ("quick_design_model", "profiler_model") if getattr(config, f)}
            kept = sorted(m for m in helper_models if fit(residency_of(m), config.country) in ACCEPTED)[:1]
            if not kept:
                raise ValueError(f"Strict mode: no reviewer runs in {country} or a place your law allows.")
            notes.append(f"Strict mode: the only model reviewer is {kept[0]}, because no other reviewer runs in {country}. "
                         "It is also one of the compared models, so a code-based fact check (exact numbers, dates and "
                         "codes) reviews every answer too.")
        judges = kept
        config = replace(config, fact_check=len(judges) < 3)
    elif config.sovereignty == "prefer_local":
        abroad = [j for j in judges if fit(residency_of(j), config.country) not in ACCEPTED]
        if abroad:
            notes.append(f"The reviewers ({', '.join(abroad)}) run outside {country}; choose Strict to keep them local.")
    return replace(config, judge_models=tuple(judges), sovereignty_notes=tuple(notes),
                   fact_check=config.fact_check if config.sovereignty == "strict" else False)
