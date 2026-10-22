"""The design agent: breaks a project or complex task down into simple tasks."""

from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol

from .client import Completion, ModelInfo
from .graders import extract_json
from .jsonchat import chat_json
from .plan import TASK_TYPES, Plan, PlanError, PlanNode, read_document

DESIGN_SYSTEM = f"""You are the design agent of TaskCost AI. You break work down into simple tasks, the unit of economics.

Definitions
- Simple task: one clear job with one answer, done in a single AI call, that a reviewer can check on its own against the source documents. Examples: "find the effective date", "calculate the annual fee", "classify this ticket".
- Complex task: a piece of work made of several simple tasks, for example "extract the key data from this contract".
- Project: a larger goal made of several complex tasks.

Rules
1. Decide the level of the request: project, complex or simple.
2. Break it down until every leaf is a simple task: one fact, one calculation, one decision or one short piece of writing. A complex task usually has 3 to 12 simple tasks.
3. When a value must be derived (a date from a rule, a total from instalments), say so explicitly in the instruction.
4. When the request needs one combined deliverable, end each complex task with an "assembly" simple task that depends on the others and combines their results without changing them. Set needs_documents to false for assembly tasks.
5. For every simple task write 2 to 5 acceptance criteria. Each criterion checks one fact, can be verified against the documents, and is worded so that any correct phrasing or format passes.
6. Give the expected answer for every simple task whose answer is determined by the documents. Work it out carefully from the text; a human will review it.
7. Use depends_on only when a task needs another task's result. Do not add tasks the request doesn't need.
8. task_type is one of: {", ".join(TASK_TYPES)}.
9. Use short ids: C1, C2 for complex tasks; T1, T2 for simple tasks (unique across the whole plan).

Reply with JSON only, in this shape:
{{
  "level": "project | complex | simple",
  "title": "short title for the whole request",
  "deliverable": "what the final output is",
  "children": [
    {{"id": "C1", "kind": "complex", "title": "...", "children": [
      {{"id": "T1", "kind": "simple", "title": "...", "task_type": "extraction",
        "instruction": "...", "depends_on": [], "needs_documents": true,
        "criteria": ["..."], "expected_answer": "..."}}
    ]}}
  ]
}}
For a complex request, "children" holds its simple tasks directly. For a simple request, "children" holds one simple task.
"""


QUICK_SYSTEM = f"""You turn a user's request into ONE concrete simple task, used to compare AI models on cost and quality.

Rules
1. If the request is vague (for example "write an email"), make it concrete with a realistic enterprise scenario: who it is for, the purpose and the key facts. Invent plausible details (names, dates, amounts, order numbers) and put them in the instruction, so every model gets exactly the same facts.
2. If documents are provided, base the task on them; the documents will be given to the models with your instruction.
3. Write the instruction exactly as it will be given to the models. It must be self-contained.
4. Write 3 to 6 acceptance criteria. Each checks exactly ONE thing (a required fact, a constraint or a quality such as tone), can be verified by a reviewer, and is worded so that any correct phrasing passes. Never join two requirements with "and" in one criterion: split them.
5. Reviewers cannot count words precisely. If length matters, ask for an approximate length in the instruction and make it a separate, optional criterion: {{"text": "Is roughly 120 to 180 words", "required": false}}.
6. Give expected_answer only when the answer is determined (extraction, calculation, classification); otherwise null.
7. task_type is one of: {", ".join(TASK_TYPES)}.

Reply with JSON only:
{{"title": "short title, e.g. Delayed-order email", "task_type": "generation",
  "instruction": "...", "criteria": ["...", {{"text": "...", "required": false}}], "expected_answer": null,
  "assumptions": "one sentence on what you made concrete, or empty"}}
"""


class DesignBackend(Protocol):
    async def chat(
        self, model: ModelInfo, messages: list[dict[str, Any]], max_tokens: int,
        temperature: float | None = None, extra: dict[str, Any] | None = None,
    ) -> Completion: ...


def _plan_from_design(data: dict[str, Any], request: str, documents: dict[str, str], base_dir: Path) -> Plan:
    level = str(data.get("level", "complex")).strip().lower()
    children = [PlanNode.from_dict(child) for child in data.get("children", [])]
    # The root is the whole job; a simple request becomes a job with one simple task.
    root = PlanNode(
        id="J", title=str(data.get("title", request)),
        kind="project" if level == "project" else "complex", children=children,
    )
    plan = Plan(
        request=request,
        level=level,
        deliverable=str(data.get("deliverable", "")),
        root=root,
        documents=documents,
        base_dir=base_dir,
    )
    # Documents were supplied by the user, not the model, so only the plan structure is checked here.
    plan.validate(check_documents=False)
    return plan


async def design_plan(
    backend: DesignBackend,
    model: ModelInfo,
    request: str,
    documents: dict[str, Path],
    base_dir: Path,
) -> Plan:
    """Ask the design model for a plan; if it is invalid, show it the problems and ask once more."""
    texts = {doc_id: read_document(path) for doc_id, path in documents.items()}
    doc_paths = {doc_id: os.path.relpath(path.resolve(), base_dir.resolve()) for doc_id, path in documents.items()}

    user_parts = [f"## Request\n{request}"]
    if texts:
        user_parts.append("## Source documents")
        user_parts += [f"### {doc_id}\n{text}" for doc_id, text in texts.items()]
    messages = [{"role": "system", "content": DESIGN_SYSTEM}, {"role": "user", "content": "\n\n".join(user_parts)}]

    def parse(text: str) -> Plan:
        data = extract_json(text)
        if not isinstance(data, dict):
            raise PlanError("the reply must be a JSON object")
        return _plan_from_design(data, request, doc_paths, base_dir)

    reply = await chat_json(
        backend, model, messages, parse, max_tokens=16000,
        fail=lambda problem: PlanError(f"The design agent did not produce a valid plan: {problem}"),
        retry_hint="That plan has problems: {problem}. Reply with the corrected full plan as JSON only.",
    )
    plan = reply.value
    plan.design = {
        "model": model.id,
        "cost_egp": round(reply.cost_egp, 6),
        "prompt_tokens": reply.prompt_tokens,
        "completion_tokens": reply.completion_tokens,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    return plan


async def design_simple_task(
    backend: DesignBackend,
    model: ModelInfo,
    request: str,
    documents: dict[str, Path],
    base_dir: Path,
    context: str = "",
) -> Plan:
    """Make a single concrete, checkable simple task from a (possibly vague) request. `context` describes the
    user (inferred from their earlier use) so the scenario looks like their work."""
    texts = {doc_id: read_document(path) for doc_id, path in documents.items()}
    user_parts = [f"## Request\n{request}"]
    if context:
        user_parts.append(f"## About the user (inferred from their earlier requests)\n{context}")
    if texts:
        user_parts.append("## Documents")
        user_parts += [f"### {doc_id}\n{text}" for doc_id, text in texts.items()]
    messages = [{"role": "system", "content": QUICK_SYSTEM}, {"role": "user", "content": "\n\n".join(user_parts)}]

    def parse(text: str) -> tuple[PlanNode, dict[str, Any]]:
        data = extract_json(text)
        if not isinstance(data, dict) or not data.get("instruction") or not data.get("criteria"):
            raise PlanError("the reply needs instruction and criteria")
        task = PlanNode.from_dict({
            "id": "T1", "kind": "simple", "title": data.get("title") or request[:60],
            "task_type": data.get("task_type", "other"), "instruction": data["instruction"],
            "criteria": data["criteria"], "expected_answer": data.get("expected_answer"),
            "needs_documents": bool(texts),
        })
        return task, data

    reply = await chat_json(
        backend, model, messages, parse, max_tokens=4000,
        fail=lambda problem: PlanError(f"Could not turn the request into a task: {problem}"),
    )
    task, data = reply.value
    plan = Plan(
        request=request,
        level="simple",
        deliverable=task.title,
        root=PlanNode(id="J", title=task.title, kind="complex", children=[task]),
        documents={doc_id: os.path.relpath(path.resolve(), base_dir.resolve()) for doc_id, path in documents.items()},
        base_dir=base_dir,
    )
    plan.validate(check_documents=False)
    plan.design = {
        "model": model.id,
        "cost_egp": round(reply.cost_egp, 6),
        "assumptions": str(data.get("assumptions") or ""),
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    return plan
