"""Task definitions loaded from YAML files."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

REQUIRED_KEYS = ("id", "category", "prompt", "grader")
# Reasoning models spend hidden tokens before answering, and only tokens actually used are billed,
# so a generous cap costs nothing extra but prevents cut-off answers.
DEFAULT_MAX_TOKENS = 4000


@dataclass(frozen=True)
class Task:
    id: str
    category: str
    prompt: str
    grader: dict[str, Any]
    system: str | None = None
    reference: str | None = None
    description: str = ""
    max_tokens: int = DEFAULT_MAX_TOKENS
    source: str = ""
    tags: tuple[str, ...] = field(default_factory=tuple)

    def messages(self) -> list[dict[str, str]]:
        messages = []
        if self.system:
            messages.append({"role": "system", "content": self.system})
        messages.append({"role": "user", "content": self.prompt})
        return messages

    counterexamples: tuple[str, ...] = field(default_factory=tuple)

    def uses_grader(self, grader_type: str) -> bool:
        return _uses_grader(self.grader, grader_type)

    @property
    def needs_judge(self) -> bool:
        return self.uses_grader("rubric") or self.uses_grader("llm_judge")


def _uses_grader(spec: dict[str, Any], grader_type: str) -> bool:
    if spec.get("type") == grader_type:
        return True
    return any(_uses_grader(child, grader_type) for child in spec.get("graders", []))


def load_tasks(path: str | Path) -> list[Task]:
    path = Path(path)
    files = sorted(path.glob("**/*.y*ml")) if path.is_dir() else [path]
    tasks: list[Task] = []
    for file in files:
        data = yaml.safe_load(file.read_text(encoding="utf-8"))
        for entry in data if isinstance(data, list) else [data]:
            tasks.append(_parse_task(entry, file))

    seen: set[str] = set()
    for task in tasks:
        if task.id in seen:
            raise ValueError(f"Duplicate task id: {task.id}")
        seen.add(task.id)
    if not tasks:
        raise ValueError(f"No tasks found in {path}")
    return tasks


def _parse_task(entry: dict[str, Any], file: Path) -> Task:
    missing = [key for key in REQUIRED_KEYS if key not in entry]
    if missing:
        raise ValueError(f"{file}: task {entry.get('id', '?')} is missing {', '.join(missing)}")
    return Task(
        id=str(entry["id"]),
        category=str(entry["category"]),
        prompt=str(entry["prompt"]).strip(),
        grader=entry["grader"],
        system=entry.get("system"),
        reference=None if entry.get("reference") is None else str(entry["reference"]).strip(),
        description=str(entry.get("description", "")).strip(),
        max_tokens=int(entry.get("max_tokens", DEFAULT_MAX_TOKENS)),
        source=str(file),
        tags=tuple(entry.get("tags", [])),
        counterexamples=tuple(str(c).strip() for c in entry.get("counterexamples", [])),
    )
