"""Benchmark configuration and environment loading."""

from __future__ import annotations

import os
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

import yaml

# Models that read the user's request before or around the comparison; a helper mode sets all of them.
HELPER_FIELDS = ("design_model", "quick_design_model", "profiler_model", "blueprint_model", "sample_model")


@dataclass(frozen=True)
class HelperMode:
    id: str
    label: str
    description: str
    models: dict[str, str]


@dataclass(frozen=True)
class ModelSpec:
    """A model under test. The same model id can appear twice with different params and labels."""

    id: str
    label: str
    temperature: float | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def parse(cls, entry: str | dict[str, Any], default_temperature: float | None) -> ModelSpec:
        if isinstance(entry, str):
            return cls(id=entry, label=entry, temperature=default_temperature)
        entry = dict(entry)
        model_id = entry.pop("id")
        label = entry.pop("label", model_id)
        temperature = entry.pop("temperature", default_temperature)
        return cls(id=model_id, label=label, temperature=temperature, extra=entry)


@dataclass(frozen=True)
class BenchmarkConfig:
    name: str
    tasks: str
    models: tuple[ModelSpec, ...]
    trials: int = 3
    max_attempts: int = 2
    concurrency: int = 4
    timeout_s: float = 90.0
    judge_models: tuple[str, ...] = ()
    judge_threshold: float = 0.8
    design_model: str | None = None
    default_temperature: float | None = None
    test_mode: bool = False
    tiers: tuple[tuple[str, str], ...] = ()
    quick_design_model: str | None = None
    profiler_model: str | None = None
    blueprint_model: str | None = None
    sample_model: str | None = None
    escalate_at: float = 0.5
    prefer_open_weights: bool = True
    country: str = "EG"               # the user's country (ISO code); the web page sends the detected one
    sovereignty: str = "prefer_local"  # open, prefer_local or strict (see sovereignty.py)
    sovereignty_notes: tuple[str, ...] = ()
    hosting_model: str | None = None  # reads provider pages for the daily hosting agent
    hosting_refresh_hours: float = 24.0
    min_model_pass_rate: float = 0.85
    cache_decisions_hours: float = 24.0
    cache_comparisons_hours: float = 24.0
    min_success_rate: float = 0.8
    output_dir: str = "results"
    base_dir: Path = Path(".")
    helper_modes: tuple[HelperMode, ...] = ()
    helper_mode: str | None = None

    def with_helper_mode(self, mode: str) -> BenchmarkConfig:
        chosen = next((m for m in self.helper_modes if m.id == mode), None)
        if chosen is None:
            raise ValueError(f"unknown helper mode {mode!r}; choose one of {[m.id for m in self.helper_modes]}")
        return replace(self, helper_mode=mode, **chosen.models)

    @property
    def tasks_path(self) -> Path:
        return (self.base_dir / self.tasks).resolve()

    @property
    def output_path(self) -> Path:
        return (self.base_dir / self.output_dir).resolve()

    @property
    def cache_path(self) -> Path:
        return (self.base_dir / ".cache" / "taskcost.sqlite").resolve()

    def with_models(self, models: list[ModelSpec], judge_models: list[str]) -> BenchmarkConfig:
        return replace(self, models=tuple(models), judge_models=tuple(judge_models))

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "tasks": str(self.tasks_path),
            "models": [
                {"id": m.id, "label": m.label, "temperature": m.temperature, **m.extra}
                for m in self.models
            ],
            "trials": self.trials,
            "max_attempts": self.max_attempts,
            "concurrency": self.concurrency,
            "timeout_s": self.timeout_s,
            "judge_models": list(self.judge_models),
            "judge_threshold": self.judge_threshold,
            "design_model": self.design_model,
            "test_mode": self.test_mode,
            "prefer_open_weights": self.prefer_open_weights,
            "country": self.country,
            "sovereignty": self.sovereignty,
            "sovereignty_notes": list(self.sovereignty_notes),
            "helper_mode": self.helper_mode,
            "helpers": {name: getattr(self, name) for name in HELPER_FIELDS},
            "min_success_rate": self.min_success_rate,
        }


def load_config(path: str | Path) -> BenchmarkConfig:
    path = Path(path)
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    temperature = data.get("temperature")
    models = tuple(ModelSpec.parse(entry, temperature) for entry in data.get("models", []))
    if not models:
        raise ValueError(f"{path}: 'models' must list at least one model")
    labels = [m.label for m in models]
    if len(set(labels)) != len(labels):
        raise ValueError(f"{path}: model labels must be unique, got {labels}")

    config = BenchmarkConfig(
        name=data.get("name", path.stem),
        tasks=data.get("tasks", "tasks"),
        models=models,
        trials=int(data.get("trials", 3)),
        max_attempts=int(data.get("max_attempts", 2)),
        concurrency=int(data.get("concurrency", 4)),
        timeout_s=float(data.get("timeout_s", 90)),
        judge_models=_judge_models(data),
        judge_threshold=float(data.get("judge_threshold", 0.8)),
        design_model=data.get("design_model"),
        default_temperature=temperature,
        tiers=tuple((str(tier), str(model)) for tier, model in (data.get("tiers") or {}).items()),
        quick_design_model=data.get("quick_design_model"),
        profiler_model=data.get("profiler_model"),
        blueprint_model=data.get("blueprint_model"),
        sample_model=data.get("sample_model"),
        escalate_at=float(data.get("escalate_at", 0.5)),
        prefer_open_weights=bool(data.get("prefer_open_weights", True)),
        country=str(data.get("country", "EG")).upper(),
        sovereignty=str(data.get("sovereignty", "prefer_local")),
        hosting_model=data.get("hosting_model"),
        hosting_refresh_hours=float(data.get("hosting_refresh_hours", 24)),
        min_model_pass_rate=float(data.get("min_model_pass_rate", 0.85)),
        cache_decisions_hours=float((data.get("cache") or {}).get("decisions_hours", 24)),
        cache_comparisons_hours=float((data.get("cache") or {}).get("comparisons_hours", 24)),
        min_success_rate=float(data.get("min_success_rate", 0.8)),
        output_dir=data.get("output_dir", "results"),
        base_dir=path.parent.resolve(),
    )
    if config.trials < 1 or config.max_attempts < 1 or config.concurrency < 1:
        raise ValueError(f"{path}: trials, max_attempts and concurrency must be at least 1")
    modes = _helper_modes(data.get("helper_modes") or {}, path)
    if modes:
        config = replace(config, helper_modes=modes)
        config = config.with_helper_mode(str(data.get("helper_mode") or modes[0].id))
    return config


def _helper_modes(data: dict[str, Any], path: Path) -> tuple[HelperMode, ...]:
    modes = []
    for mode_id, entry in data.items():
        models = {name: str(entry[name]) for name in HELPER_FIELDS if entry.get(name)}
        unknown = set(entry) - set(HELPER_FIELDS) - {"label", "description"}
        if unknown:
            raise ValueError(f"{path}: helper mode {mode_id!r} has unknown keys {sorted(unknown)}")
        modes.append(HelperMode(str(mode_id), str(entry.get("label", mode_id)), str(entry.get("description", "")), models))
    return tuple(modes)


def _judge_models(data: dict[str, Any]) -> tuple[str, ...]:
    judges = data.get("judge_models", data.get("judge_model", []))
    return tuple([judges] if isinstance(judges, str) else judges)


def load_env_value(name: str, env_file: str | Path = ".env") -> str | None:
    """Return a variable from the environment, falling back to a local .env file."""
    if os.environ.get(name):
        return os.environ[name]
    env_path = Path(env_file)
    if not env_path.is_file():
        return None
    for line in env_path.read_text(encoding="utf-8").splitlines():
        key, sep, value = line.strip().partition("=")
        if sep and key.strip() == name:
            return value.strip().strip("'\"") or None
    return None


def load_api_key(env_file: str | Path = ".env") -> str | None:
    return load_env_value("LLM_API_KEY", env_file)
