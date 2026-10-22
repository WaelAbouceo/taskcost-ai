"""File and folder names for plans, runs and blueprints."""

from __future__ import annotations

import re
from pathlib import Path


def slug(text: str, fallback: str = "job", limit: int = 40) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:limit] or fallback


def unique_dir(parent: Path, name: str) -> Path:
    """Create and return parent/name, or parent/name-2, -3... if it exists (never reuses a folder)."""
    parent.mkdir(parents=True, exist_ok=True)
    for n in range(1, 1000):
        folder = parent / (name if n == 1 else f"{name}-{n}")
        try:
            folder.mkdir()
            return folder
        except FileExistsError:
            continue
    raise RuntimeError(f"Could not create a folder for {name}")
