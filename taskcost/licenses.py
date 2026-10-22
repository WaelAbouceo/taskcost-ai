"""Open-weight or proprietary, and hosted in Egypt or not: used to prefer Egypt-hosted open-source models,
then other open-source ones, and fall back to proprietary ones.

The source of truth is SovereignEG's catalog on GET /v1/models: `weights_type`, and `source` ("sovereign"
means served from SovereignEG's own GPUs in Egypt). Both are saved whenever the catalog is fetched, so labels
also work offline. Models the catalog does not label fall back to the hand-maintained family list in
knowledge/model_licenses.yaml; unknown models count as proprietary and not hosted in Egypt.
"""

from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parent.parent
LICENSES_FILE = ROOT / "knowledge" / "model_licenses.yaml"
CATALOG_FILE = ROOT / ".cache" / "catalog_weights.json"
HOSTING_FILE = ROOT / ".cache" / "catalog_egypt_hosted.json"
_QWEN_SIZE = re.compile(r"\d+(\.\d+)?b\b|a\d+b")
_catalog: dict[str, str] | None = None
_egypt: set[str] | None = None


@lru_cache(maxsize=1)
def _rules() -> tuple[tuple[str, ...], tuple[str, ...]]:
    data = yaml.safe_load(LICENSES_FILE.read_text(encoding="utf-8"))
    return tuple(data["open_weight_families"]), tuple(data.get("proprietary_exceptions", []))


def _catalog_labels() -> dict[str, str]:
    global _catalog
    if _catalog is None:
        try:
            _catalog = json.loads(CATALOG_FILE.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            _catalog = {}
    return _catalog


def _egypt_hosted() -> set[str]:
    global _egypt
    if _egypt is None:
        try:
            _egypt = set(json.loads(HOSTING_FILE.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            _egypt = set()
    return _egypt


def remember_catalog(models: dict[str, Any]) -> None:
    """Store the catalog's weights_type and Egypt-hosting labels (called whenever the catalog is fetched)."""
    global _egypt
    hosted = {model_id for model_id, info in models.items() if getattr(info, "source", None) == "sovereign"}
    if any(getattr(info, "source", None) for info in models.values()) and hosted != _egypt_hosted():
        HOSTING_FILE.parent.mkdir(parents=True, exist_ok=True)
        HOSTING_FILE.write_text(json.dumps(sorted(hosted), indent=1), encoding="utf-8")
        _egypt = hosted
    labels = {model_id: info.weights_type for model_id, info in models.items()
              if getattr(info, "weights_type", None) in ("open_weights", "proprietary")}
    if not labels:
        return
    merged = {**_catalog_labels(), **labels}
    if merged != _catalog_labels():
        CATALOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        CATALOG_FILE.write_text(json.dumps(merged, indent=1, sort_keys=True), encoding="utf-8")
    _catalog_labels().update(labels)


def label_source(model_id: str) -> str:
    return "SovereignEG catalog" if model_id in _catalog_labels() else "family list"


def is_egypt_hosted(model_id: str) -> bool:
    """True if SovereignEG serves the model from its own GPUs in Egypt ("Sovereign · Hosted in Egypt")."""
    return model_id in _egypt_hosted()


def is_open_weights(model_id: str) -> bool:
    """True if the model's weights are published; unknown models count as proprietary."""
    label = _catalog_labels().get(model_id)
    if label:
        return label == "open_weights"
    name = model_id.lower()
    families, exceptions = _rules()
    if any(name.startswith(e) for e in exceptions):
        return False
    if name.startswith("qwen"):
        return bool(_QWEN_SIZE.search(name))
    return any(name.startswith(f) for f in families)
