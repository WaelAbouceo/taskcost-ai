"""Task plans: work broken down into a tree whose leaves are simple tasks, the unit of economics.

A project contains complex tasks, a complex task contains simple tasks, and a simple task is one
clear job with one answer, done in one AI call and checkable on its own.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from .tasks import DEFAULT_MAX_TOKENS, Task

LEVELS = ("project", "complex", "simple")
TASK_TYPES = (
    "extraction", "calculation", "classification", "reasoning", "summarization",
    "translation", "generation", "assembly", "other",
)


class PlanError(ValueError):
    pass


@dataclass
class PlanNode:
    id: str
    title: str
    kind: str
    children: list[PlanNode] = field(default_factory=list)
    task_type: str = "other"
    instruction: str = ""
    depends_on: list[str] = field(default_factory=list)
    criteria: list[Any] = field(default_factory=list)
    expected_answer: str | None = None
    needs_documents: bool = True
    max_tokens: int = DEFAULT_MAX_TOKENS

    def walk(self) -> list[PlanNode]:
        return [self] + [node for child in self.children for node in child.walk()]

    def simple_tasks(self) -> list[PlanNode]:
        return [node for node in self.walk() if node.kind == "simple"]

    def to_dict(self) -> dict[str, Any]:
        if self.kind != "simple":
            return {"id": self.id, "kind": self.kind, "title": self.title,
                    "children": [child.to_dict() for child in self.children]}
        data: dict[str, Any] = {
            "id": self.id, "kind": "simple", "title": self.title, "task_type": self.task_type,
            "instruction": self.instruction, "depends_on": self.depends_on, "criteria": self.criteria,
            "expected_answer": self.expected_answer, "needs_documents": self.needs_documents,
        }
        if self.max_tokens != DEFAULT_MAX_TOKENS:
            data["max_tokens"] = self.max_tokens
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> PlanNode:
        kind = str(data.get("kind", "simple"))
        if kind != "simple":
            return cls(
                id=str(data["id"]), title=str(data.get("title", data["id"])), kind=kind,
                children=[cls.from_dict(child) for child in data.get("children", [])],
            )
        task_type = str(data.get("task_type", "other"))
        return cls(
            id=str(data["id"]),
            title=str(data.get("title", data["id"])),
            kind="simple",
            task_type=task_type if task_type in TASK_TYPES else "other",
            instruction=str(data.get("instruction", "")).strip(),
            depends_on=[str(d) for d in data.get("depends_on", [])],
            criteria=list(data.get("criteria", [])),
            expected_answer=None if data.get("expected_answer") in (None, "") else str(data["expected_answer"]).strip(),
            needs_documents=bool(data.get("needs_documents", True)),
            max_tokens=int(data.get("max_tokens", DEFAULT_MAX_TOKENS)),
        )


@dataclass
class Plan:
    request: str
    level: str
    deliverable: str
    root: PlanNode
    documents: dict[str, str] = field(default_factory=dict)
    design: dict[str, Any] = field(default_factory=dict)
    reviewed: bool = False
    base_dir: Path = Path(".")

    def simple_tasks(self) -> list[PlanNode]:
        return self.root.simple_tasks()

    def complex_tasks(self) -> list[PlanNode]:
        return [node for node in self.root.walk() if node.kind == "complex"]

    def node(self, node_id: str) -> PlanNode:
        for node in self.root.walk():
            if node.id == node_id:
                return node
        raise KeyError(node_id)

    def ordered_tasks(self) -> list[PlanNode]:
        """Simple tasks in dependency order (each task after everything it depends on)."""
        tasks = {node.id: node for node in self.simple_tasks()}
        ordered: list[PlanNode] = []
        state: dict[str, str] = {}

        def visit(node_id: str, path: list[str]) -> None:
            if state.get(node_id) == "done":
                return
            if state.get(node_id) == "visiting":
                raise PlanError(f"Dependency cycle: {' -> '.join(path + [node_id])}")
            state[node_id] = "visiting"
            for dep in tasks[node_id].depends_on:
                visit(dep, path + [node_id])
            state[node_id] = "done"
            ordered.append(tasks[node_id])

        for node_id in tasks:
            visit(node_id, [])
        return ordered

    def document_path(self, path: str) -> Path:
        return Path(os.path.normpath(self.base_dir / path))

    def validate(self, check_documents: bool = True) -> None:
        problems = []
        if self.level not in LEVELS:
            problems.append(f"level must be one of {LEVELS}, got {self.level!r}")
        nodes = self.root.walk()
        ids = [node.id for node in nodes]
        duplicates = sorted({i for i in ids if ids.count(i) > 1})
        if duplicates:
            problems.append(f"duplicate ids: {duplicates}")
        simple = {node.id: node for node in self.simple_tasks()}
        if not simple:
            problems.append("the plan has no simple tasks")
        for node in nodes:
            if node.kind not in LEVELS:
                problems.append(f"{node.id}: kind must be one of {LEVELS}")
            if node.kind != "simple" and not node.children:
                problems.append(f"{node.id}: a {node.kind} task needs children")
        for node in simple.values():
            if not node.instruction:
                problems.append(f"{node.id}: missing instruction")
            if not node.criteria:
                problems.append(f"{node.id}: missing criteria")
            for dep in node.depends_on:
                if dep not in simple:
                    problems.append(f"{node.id}: depends on unknown simple task {dep!r}")
        for doc_id, path in self.documents.items() if check_documents else ():
            if not self.document_path(path).is_file():
                problems.append(f"document {doc_id!r} not found at {path}")
        if not problems:
            try:
                self.ordered_tasks()
            except PlanError as exc:
                problems.append(str(exc))
        if problems:
            raise PlanError("; ".join(problems))

    def load_documents(self) -> dict[str, str]:
        return {doc_id: read_document(self.document_path(path)) for doc_id, path in self.documents.items()}

    def to_dict(self, relative_to: Path | None = None) -> dict[str, Any]:
        base = relative_to or self.base_dir
        documents = {
            doc_id: os.path.relpath(self.document_path(path), os.path.normpath(base.absolute()))
            for doc_id, path in self.documents.items()
        }
        return {
            "request": self.request,
            "level": self.level,
            "deliverable": self.deliverable,
            "reviewed": self.reviewed,
            "documents": documents,
            "design": self.design,
            "root": self.root.to_dict(),
        }

    def save(self, path: str | Path) -> Path:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        text = yaml.safe_dump(self.to_dict(relative_to=path.parent), sort_keys=False, allow_unicode=True, width=100)
        path.write_text(text, encoding="utf-8")
        return path

    @classmethod
    def from_dict(cls, data: dict[str, Any], base_dir: Path) -> Plan:
        return cls(
            request=str(data["request"]),
            level=str(data.get("level", "complex")),
            deliverable=str(data.get("deliverable", "")),
            root=PlanNode.from_dict(data["root"]),
            documents={str(k): str(v) for k, v in (data.get("documents") or {}).items()},
            design=dict(data.get("design") or {}),
            reviewed=bool(data.get("reviewed", False)),
            base_dir=base_dir,
        )

    @classmethod
    def load(cls, path: str | Path) -> Plan:
        path = Path(path)
        plan = cls.from_dict(yaml.safe_load(path.read_text(encoding="utf-8")), path.parent.resolve())
        plan.validate()
        return plan


def read_document(path: Path) -> str:
    if path.suffix.lower() == ".pdf":
        from pypdf import PdfReader

        return "\n".join(page.extract_text() or "" for page in PdfReader(str(path)).pages).strip()
    return path.read_text(encoding="utf-8").strip()


def build_task(
    plan: Plan,
    node: PlanNode,
    documents: dict[str, str],
    dependency_outputs: dict[str, str],
) -> Task:
    """Turn a simple task into the prompt a worker model receives, with sources and earlier results."""
    parts = [
        "You are completing one step of a larger piece of work.",
        f"Overall request: {plan.request}",
        f"Your step ({node.id}, {node.title}): {node.instruction}",
        "Answer this step only, clearly and completely. If a value must be calculated, show the calculation briefly.",
    ]
    if node.needs_documents and documents:
        parts.append("## Source documents")
        parts += [f"### {doc_id}\n{text}" for doc_id, text in documents.items()]
    if node.depends_on:
        parts.append("## Results of earlier steps")
        parts += [f"### {dep} ({plan.node(dep).title})\n{dependency_outputs[dep]}" for dep in node.depends_on]
    return Task(
        id=node.id,
        category=node.task_type,
        prompt="\n\n".join(parts),
        grader={"type": "rubric", "criteria": node.criteria},
        reference=node.expected_answer,
        description=node.title,
        max_tokens=node.max_tokens,
    )


def describe(plan: Plan) -> str:
    """Readable outline of a plan for human review."""
    design = plan.design
    lines = [
        f"Request:     {plan.request}",
        f"Level:       {plan.level}",
        f"Deliverable: {plan.deliverable}",
        f"Documents:   {', '.join(f'{k} ({v})' for k, v in plan.documents.items()) or 'none'}",
        f"Designed by: {design.get('model', '?')} for {design.get('cost_egp', 0):.4f} EGP"
        f"   Reviewed: {'yes' if plan.reviewed else 'no'}",
        f"Simple tasks: {len(plan.simple_tasks())}",
        "",
    ]

    def render(node: PlanNode, depth: int) -> None:
        pad = "  " * depth
        if node.kind != "simple":
            lines.append(f"{pad}[{node.kind}] {node.id}: {node.title}")
            for child in node.children:
                render(child, depth + 1)
            return
        needs = f"  (uses {', '.join(node.depends_on)})" if node.depends_on else ""
        lines.append(f"{pad}[{node.task_type}] {node.id}: {node.title}{needs}")
        lines.append(f"{pad}    do:       {node.instruction}")
        if node.expected_answer:
            expected = node.expected_answer.replace("\n", " ")
            lines.append(f"{pad}    expected: {expected[:220]}")
        for criterion in node.criteria:
            text = criterion if isinstance(criterion, str) else criterion.get("text", "")
            lines.append(f"{pad}    check:    {text}")

    render(plan.root, 0)
    return "\n".join(lines)
