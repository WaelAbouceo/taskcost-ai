"""Blueprints: turn a request that is bigger than one task into how it runs in a real enterprise.

1. Jev sizes the request: a simple task, a workflow (one recurring process) or a project (a system or
   function made of several workflows).
2. For workflows and projects, the designer maps the request onto standard enterprise processes
   (knowledge/enterprise_processes.yaml), states its assumptions (company, systems, languages,
   monthly volumes) and marks which steps a person, a system or AI does.
3. For every AI step, Agent 1 picks a model and the monthly cost is estimated from measured prices.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol, TextIO

import yaml

from .client import Completion, Decision, ModelInfo
from .config import BenchmarkConfig
from .graders import GradeContext, extract_json, grade
from .jsonchat import chat_json
from .judges import build_panel
from .naming import slug, unique_dir
from .plan import TASK_TYPES
from .recommender import recommend_hybrid
from .registry import USE_CASES, ModelCard, rank_candidates
from .sovereignty import accepted, residency_ranks
from .runner import FatalRunError, first_error, load_attempts, run_job
from .tasks import Task

KNOWLEDGE_FILE = Path(__file__).resolve().parent.parent / "knowledge" / "enterprise_processes.yaml"
MAX_WORKFLOWS = 5
MAX_AI_STEPS = 4

SIZE_QUESTION = {
    "level": {
        "type": "choice",
        "instructions": "How big is the work described in the `request`?",
        "criteria": {
            "simple": "One clear job with one answer that an AI can do in a single step, such as writing one email or extracting fields from one document",
            "workflow": "One recurring business process made of several steps, such as handling supplier invoices from receipt to payment",
            "project": "A whole system or business function made of several processes, such as an ERP, a CRM or running a department",
            "unclear": "Too vague to tell what work is wanted",
        },
    },
    "use_case": {
        "type": "choice",
        "instructions": "How will the result of the work in the `request` be used? If the request does not say, "
                        "choose how this kind of work usually runs in a company. Choose chat only when the request "
                        "mentions a conversation with someone waiting, such as a chatbot, live chat or messaging app; "
                        "writing an email, reading documents or summarising is batch unless the request says otherwise.",
        "criteria": {
            "chat": "Answers in a live conversation while a person waits, such as a chatbot, live chat, WhatsApp or an assistant",
            "realtime": "An application waits for the result before it continues, such as routing items or checking a form as they arrive",
            "batch": "Runs in the background and is needed within minutes or hours, such as drafting emails, reading documents, summarising or reports",
        },
    },
}
DEFAULT_USE_CASE = "realtime"


class Backend(Protocol):
    async def chat(
        self, model: ModelInfo, messages: list[dict[str, Any]], max_tokens: int,
        temperature: float | None = None, extra: dict[str, Any] | None = None,
    ) -> Completion: ...

    async def decide(self, model: ModelInfo, state: Any, questions: dict[str, Any]) -> Decision: ...


async def size_request(backend: Backend, jev: ModelInfo, request: str) -> dict[str, Any]:
    """Agent 1's view of the request: its size (anything other than a confident 'simple' becomes a blueprint)
    and how the result will be used (chat, realtime or batch), in one decision call."""
    decision = await backend.decide(jev, {"request": request[:4000]}, SIZE_QUESTION)
    answer = decision.answers.get("level") or {}
    probabilities = {k: float(v) for k, v in (answer.get("probabilities") or {}).items()}
    choice = str(answer.get("choice", "unclear"))
    use = decision.answers.get("use_case") or {}
    use_case = str(use.get("choice") or DEFAULT_USE_CASE)
    return {
        "level": choice,
        "probabilities": probabilities,
        "is_simple": choice == "simple" and probabilities.get("simple", 0.0) >= 0.6,
        "use_case": use_case if use_case in USE_CASES else DEFAULT_USE_CASE,
        "use_case_probabilities": {k: float(v) for k, v in (use.get("probabilities") or {}).items()},
        "cached": decision.cached,
    }


def load_knowledge(path: Path = KNOWLEDGE_FILE) -> dict[str, Any]:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def knowledge_summary(knowledge: dict[str, Any]) -> str:
    lines = ["Systems and the standard processes they usually cover:"]
    lines += [f"- {system}: {', '.join(names)}" for system, names in knowledge["systems"].items()]
    lines.append("")
    for key, process in knowledge["processes"].items():
        lines.append(f"{process['name']} [{key}], {process['area']}:")
        for step in process["steps"]:
            volume = f", about {step['volume']}/month" if step.get("volume") else ""
            lines.append(f"  - {step['name']} ({step['actor']}{volume})")
    return "\n".join(lines)


BLUEPRINT_SYSTEM = f"""You are the solution designer of TaskCost AI. The user's request is bigger than one task.
Describe how this work really runs in an enterprise and where AI helps, using the reference of standard
enterprise processes below and your own knowledge of how such systems run in practice.

Rules
1. Decide whether the request is a "workflow" (one recurring process) or a "project" (a system or business
   function made of several workflows).
2. Do not ask questions. Make sensible, explicit assumptions instead: the company (industry, size, country;
   if nothing is given, assume a mid-sized company in Egypt with about 500 employees), the systems in use,
   the languages (Arabic and English are common), and the monthly volumes. Start from the reference volumes
   and scale them to the company you assume.
3. List at most {MAX_WORKFLOWS} workflows, most relevant first, using standard names where they exist.
   A workflow request has exactly one workflow.
4. For each workflow, list its main steps in order (4 to 8) and say who does each: "person", "system" or "ai".
   Mark a step "ai" only where AI genuinely helps today: reading documents, classifying, matching, drafting,
   summarising, answering questions or translating. At most {MAX_AI_STEPS} AI steps per workflow.
5. For each AI step give: task_type (one of {", ".join(TASK_TYPES)}), use_case ("chat" if a person waits for
   the answer, "realtime" if an application waits, "batch" if it can run in the background),
   monthly_volume (a number), instruction (one sentence, as it would be given to the AI model) and
   2 to 4 criteria that a reviewer could check.

Reply with JSON only:
{{"level": "workflow or project", "title": "short title", "summary": "two sentences",
  "assumptions": [{{"topic": "Company", "assumption": "..."}}],
  "workflows": [{{"id": "W1", "name": "...", "description": "one sentence",
    "steps": [{{"name": "...", "actor": "person"}},
              {{"name": "...", "actor": "ai", "task_type": "extraction", "use_case": "batch",
                "monthly_volume": 1200, "instruction": "...", "criteria": ["..."]}}]}}]}}
"""


async def design_blueprint(
    backend: Backend, designer: ModelInfo, request: str, documents: dict[str, str] | None = None,
) -> dict[str, Any]:
    parts = [f"## Request\n{request}", f"## Reference: standard enterprise processes\n{knowledge_summary(load_knowledge())}"]
    if documents:
        parts.append("## Documents provided by the user")
        parts += [f"### {name}\n{text[:6000]}" for name, text in documents.items()]
    messages = [{"role": "system", "content": BLUEPRINT_SYSTEM}, {"role": "user", "content": "\n\n".join(parts)}]
    reply = await chat_json(backend, designer, messages, lambda text: _clean(extract_json(text)), max_tokens=12000,
                            fail=lambda problem: ValueError(f"Could not design a blueprint: {problem}"))
    blueprint = reply.value
    blueprint["design"] = {"model": designer.id, "cost_egp": round(reply.cost_egp, 6),
                           "created_at": datetime.now(timezone.utc).isoformat()}
    blueprint["request"] = request
    return blueprint


def _clean(data: Any) -> dict[str, Any]:
    if not isinstance(data, dict) or not data.get("workflows"):
        raise ValueError("the reply needs at least one workflow")
    workflows = []
    for w_index, workflow in enumerate(data["workflows"][:MAX_WORKFLOWS], start=1):
        steps = []
        ai_count = 0
        for s_index, step in enumerate(workflow.get("steps", []), start=1):
            actor = str(step.get("actor", "person")).lower()
            entry = {"id": f"W{w_index}.S{s_index}", "name": str(step.get("name", "")).strip(), "actor": actor}
            if actor == "ai":
                ai_count += 1
                if ai_count > MAX_AI_STEPS:
                    entry["actor"] = "person"
                else:
                    use_case = str(step.get("use_case", "batch"))
                    task_type = str(step.get("task_type", "other"))
                    entry.update({
                        "task_type": task_type if task_type in TASK_TYPES else "other",
                        "use_case": use_case if use_case in USE_CASES else "batch",
                        "monthly_volume": max(0, int(float(step.get("monthly_volume") or 0))),
                        "instruction": str(step.get("instruction", "")).strip(),
                        "criteria": [str(c) for c in step.get("criteria", [])][:4],
                    })
                    if not entry["instruction"]:
                        raise ValueError(f"AI step {entry['id']} has no instruction")
            steps.append(entry)
        if not steps:
            raise ValueError(f"workflow {w_index} has no steps")
        workflows.append({
            "id": f"W{w_index}", "name": str(workflow.get("name", f"Workflow {w_index}")),
            "description": str(workflow.get("description", "")), "steps": steps,
        })
    level = str(data.get("level", "project")).lower()
    return {
        "level": level if level in ("workflow", "project") else "project",
        "title": str(data.get("title", "")),
        "summary": str(data.get("summary", "")),
        "assumptions": [
            {"topic": str(a.get("topic", "")), "assumption": str(a.get("assumption", ""))}
            for a in data.get("assumptions", []) if isinstance(a, dict)
        ],
        "workflows": workflows,
    }


async def estimate_blueprint(
    blueprint: dict[str, Any], backend: Backend, jev: ModelInfo, cards: dict[str, ModelCard], escalate_at: float,
    prefer_open: bool = True, min_pass_rate: float = 0.0, residency: dict[str, int] | None = None,
) -> dict[str, Any]:
    """Pick a model for every AI step (Agent 1, all steps at once) and estimate monthly cost from measured prices."""
    if not cards:
        raise ValueError("No measured model registry yet; run `taskcost calibrate` first")
    steps = ai_steps(blueprint)
    recommendations = await asyncio.gather(*(
        recommend_hybrid(backend, jev, f"{step['instruction']}\n\nThe result must: " + "; ".join(step.get("criteria", [])),
                         step["use_case"], cards, escalate_at, prefer_open, min_pass_rate, residency)
        for step in steps
    ))
    for step, rec in zip(steps, recommendations):
        card = cards.get(rec.model) if rec.model else None
        step["model"] = rec.model
        step["model_tier"] = card.tier if card else None
        step["model_note"] = rec.note
        step["cost_per_answer_egp"] = cost_per_correct(card.to_dict()) if card else None
        step["monthly_cost_egp"] = None
        step["latency_s"] = card.latency_median_s if card else None
    blueprint["strongest_model"] = max(cards.values(), key=lambda c: c.cost_per_answer_egp).id
    blueprint["unpriced_steps"] = [s["id"] for s in steps if s["cost_per_answer_egp"] is None]
    blueprint["estimate"] = {
        "basis": "measured median cost per answer on the task library, divided by the model's pass rate",
        "registry": {model: card.to_dict() for model, card in cards.items()},
        "profiler_cost_egp": sum(r.cost_egp for r in recommendations),
    }
    return recompute_totals(blueprint)


SAMPLE_SYSTEM = """You create realistic test material for one AI step in an enterprise workflow.
Given the company assumptions and the step, write:
1. "input": one realistic example of exactly what this step receives in this company, in full: for example
   the complete text of a supplier invoice, a customer email, a bank statement extract or a CV. Use plausible
   names, numbers and dates, and the languages in the assumptions. Keep it realistic but answerable: every
   field the instruction asks for must be present and legible. Keep it under 500 words.
2. "expected_answer": the correct result of the step for this input.
3. "alternative_answer": a second, equally correct answer for the same input, written in a clearly different
   format or structure (for example prose instead of JSON, a table instead of a list, or shared fields given
   once instead of on every line).
4. "criteria": 3 to 5 checks a reviewer can verify against the input. Both answers must pass every check.
   - Each check tests exactly ONE fact or requirement. Never join two requirements with "and".
   - Only check what the instruction asks for. Do not check fields, formats, layout or behaviour it does not mention.
   - Word each check so that any correct phrasing, format or structure passes.
Reply with JSON only: {"input": "...", "expected_answer": "...", "alternative_answer": "...", "criteria": ["..."]}"""


# Used only when testing the test: a correct answer that relies on facts missing from the example
# would make every model fail for the wrong reason.
GROUNDED_CHECK = "Every fact the response relies on can be found in the input given in the task."


def as_text(value: Any) -> str:
    """Models sometimes return structured answers; keep them as readable JSON rather than Python reprs."""
    if value is None:
        return ""
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, indent=2)
    return str(value).strip()


FORMAT_NOTE = "(any wording or format with the same meaning passes)"


def format_tolerant(criterion: str) -> str:
    """Decision-model judges read quoted values literally (a date written differently fails), so say it outright."""
    return criterion if FORMAT_NOTE in criterion else f"{criterion.rstrip('.')} {FORMAT_NOTE}."


async def generate_sample(
    backend: Backend, model: ModelInfo, blueprint: dict[str, Any], step: dict[str, Any], feedback: str = "",
) -> tuple[dict[str, Any], float]:
    """A realistic example input, its correct answer and checks, so the step can be measured for real.

    If the step has a user-provided example (`user_input`), that input is kept exactly and only the
    correct answer and checks are written for it.
    """
    assumptions = "\n".join(f"- {a['topic']}: {a['assumption']}" for a in blueprint.get("assumptions", []))
    given = step.get("user_input")
    user = (
        f"## Company assumptions\n{assumptions}\n\n## The AI step\n{step['name']} ({step['task_type']}).\n"
        f"Instruction given to the model: {step['instruction']}\n"
        f"Checks already planned: {'; '.join(step.get('criteria', []))}"
        + (f"\n\n## The input is given by the user. Use it exactly as \"input\" and do not change it:\n{given}" if given else "")
        + (f"\n\n## Problem with your previous attempt\n{feedback}" if feedback else "")
    )
    messages = [{"role": "system", "content": SAMPLE_SYSTEM}, {"role": "user", "content": user}]

    def parse(text: str) -> dict[str, Any]:
        data = extract_json(text)
        if not isinstance(data, dict) or not data.get("input") or not data.get("criteria"):
            raise ValueError("the reply needs input and criteria")
        return {
            "input": given or as_text(data["input"]),
            "source": "user" if given else "generated",
            "expected_answer": as_text(data.get("expected_answer")) or None,
            "alternative_answer": as_text(data.get("alternative_answer")) or None,
            "criteria": [format_tolerant(str(c)) for c in data["criteria"]][:5],
        }

    reply = await chat_json(backend, model, messages, parse, max_tokens=4000,
                            fail=lambda problem: ValueError(f"Could not write an example for {step['id']}: {problem}"))
    return reply.value, reply.cost_egp


def step_task(step: dict[str, Any], sample: dict[str, Any]) -> Task:
    return Task(
        id=step["id"],
        category=step["task_type"],
        prompt=f"{step['instruction']}\n\n## Input\n{sample['input']}",
        grader={"type": "rubric", "criteria": sample["criteria"]},
        reference=sample["expected_answer"],
        description=step["name"],
    )


async def verify_blueprint(
    config: BenchmarkConfig,
    blueprint: dict[str, Any],
    backend: Backend,
    catalog: dict[str, ModelInfo],
    cards: dict[str, ModelCard],
    run_dir: Path,
    log: TextIO,
    trials: int = 2,
    include_frontier: bool = False,
    only_steps: set[str] | None = None,
) -> dict[str, Any]:
    """Measure every AI step on a realistic example: cheapest fast-enough model first, stop at the first
    model that passes every trial. Results are saved as runs, so the registry learns from them.

    Each example is checked before any model is tried: its own correct answer must pass its own checks.
    The Frontier tier is skipped unless include_frontier is set, because it costs several EGP per answer.
    """
    sample_model = catalog[config.sample_model or config.blueprint_model or config.design_model]
    panel = build_panel(backend, [catalog[j] for j in config.judge_models])
    run_config = replace(config, max_attempts=1)
    steps = [(w, s) for w in blueprint["workflows"] for s in w["steps"] if s["actor"] == "ai"]
    to_run = [(w, s) for w, s in steps if only_steps is None or s["id"] in only_steps]
    semaphore = asyncio.Semaphore(config.concurrency)
    done = 0
    sample_cost = 0.0
    run_dir.mkdir(parents=True, exist_ok=True)
    attempts_file = run_dir / "attempts.jsonl"
    previous = blueprint.get("verification", {}).get("cost_egp", {}) if only_steps else {}
    if only_steps and attempts_file.is_file():
        # Re-measuring some steps: drop their old runs so old and new results never mix.
        kept = [line for line in attempts_file.read_text(encoding="utf-8").splitlines()
                if line.strip() and json.loads(line)["task_id"] not in only_steps]
        attempts_file.write_text("".join(f"{line}\n" for line in kept), encoding="utf-8")
    else:
        kept = []

    async def self_check(sample: dict[str, Any]) -> tuple[Task, dict[str, Any]]:
        """Test the test: the example's own correct answer must pass its own checks."""
        nonlocal sample_cost
        task = step_task({"id": "check", "task_type": "other", "instruction": "", "name": ""}, sample)
        if not sample.get("expected_answer"):
            return task, {"passed": True, "failed": [], "note": "no fixed answer to check"}
        ctx = GradeContext(task=task, judge=panel, judge_threshold=config.judge_threshold)
        result = await grade({"type": "rubric", "criteria": [*sample["criteria"], GROUNDED_CHECK]},
                             sample["expected_answer"], ctx)
        sample_cost += result.cost_egp
        failed = [c.criterion for c in result.criteria if c.required and not c.passed]
        if sample.get("alternative_answer"):
            # Checks that reject a correct answer written differently are too strict.
            alternative = await grade(task.grader, sample["alternative_answer"], ctx)
            sample_cost += alternative.cost_egp
            failed += [c.criterion for c in alternative.criteria if c.required and not c.passed and c.criterion not in failed]
        return task, {"passed": not failed, "failed": failed}

    with attempts_file.open("a" if only_steps else "w", encoding="utf-8") as sink:

        async def verify_step(workflow: dict[str, Any], step: dict[str, Any]) -> None:
            nonlocal done, sample_cost
            async with semaphore:
                sample, cost = await generate_sample(backend, sample_model, blueprint, step)
                sample_cost += cost
                task, check = await self_check(sample)
                if not check["passed"]:
                    feedback = ("A correct answer (yours, or your alternative written differently) failed these checks: "
                                + "; ".join(check["failed"])
                                + ". Make every check accept any correct answer, and make sure every fact the answer "
                                  "relies on is in the input.")
                    sample, cost = await generate_sample(backend, sample_model, blueprint, step, feedback)
                    sample_cost += cost
                    task, check = await self_check(sample)
                if not check["passed"] and GROUNDED_CHECK not in check["failed"]:
                    usable = [c for c in sample["criteria"] if c not in check["failed"]]
                    if len(usable) >= 2:
                        check["dropped"] = check["failed"]
                        sample = {**sample, "criteria": usable}
                        check["passed"] = True
                step["sample"] = sample
                step["sample_check"] = check
                task = step_task(step, sample)
                tried = []
                if not check["passed"]:
                    step["tried"] = tried
                    done += 1
                    print(f"[{done}/{len(to_run)}] {step['name']}: no reliable test could be built", file=log)
                    return
                candidates = [
                    c for c in rank_candidates(cards, step["use_case"], config.prefer_open_weights,
                                               config.min_model_pass_rate, residency_ranks(config, cards))
                    if config.model_spec(c.id).id in catalog and accepted(config, c.id) and (include_frontier or c.tier != "Frontier")
                ]
                for card in candidates:
                    spec = config.model_spec(card.id)
                    extra = {"tier": card.tier, "step_id": step["id"], "workflow_id": workflow["id"], "verification": True}
                    outcomes = await asyncio.gather(*(
                        run_job(run_config, backend, panel, spec, catalog[spec.id], task, trial, sink, extra)
                        for trial in range(1, trials + 1)
                    ))
                    passed = sum(o.passed for o in outcomes)
                    tried.append({"model": card.id, "tier": card.tier, "passed": passed, "runs": trials})
                    if passed == trials:
                        break
                step["tried"] = tried
            done += 1
            winner = next((t for t in tried if t["passed"] == t["runs"]), None)
            outcome = f"{winner['model']} passed" if winner else "no model passed"
            print(f"[{done}/{len(to_run)}] {step['name']}: {outcome}", file=log)

        try:
            async with asyncio.TaskGroup() as group:
                for workflow, step in to_run:
                    group.create_task(verify_step(workflow, step))
        except* FatalRunError as errors:
            raise first_error(errors) from None

    records = load_attempts(run_dir)
    for _, step in steps:
        for entry in step.get("tried", []):
            runs = [r for r in records if r["task_id"] == step["id"] and r["model"] == entry["model"]]
            entry["cost_per_answer_egp"] = sum(r["cost_egp"] for r in runs) / len(runs) if runs else None
            latencies = [r["latency_s"] for r in runs if r.get("latency_s") is not None]
            entry["latency_s"] = sum(latencies) / len(latencies) if latencies else None
            entry["failed_checks"] = list(dict.fromkeys(
                c["criterion"] for r in runs for c in r.get("criteria", []) if c["required"] and not c["passed"]
            ))
        winner = next((t for t in step.get("tried", []) if t["passed"] == t["runs"]), None)
        if not step.get("sample_check", {}).get("passed", True):
            step["verdict"] = "no_reliable_test"
        elif winner:
            step["verdict"] = "measured"
        elif not include_frontier and any(c.tier == "Frontier" and c.fits(step["use_case"]) for c in cards.values()):
            step["verdict"] = "needs_frontier"
        else:
            step["verdict"] = "no_model_passed"
        step["measured"] = winner and {
            "model": winner["model"], "tier": winner["tier"],
            "cost_per_answer_egp": winner["cost_per_answer_egp"],
            "latency_s": winner["latency_s"],
        }

    measured = [s for _, s in steps if s.get("measured")]
    blueprint["verification"] = {
        "run": run_dir.name,
        "trials": trials,
        "measured_steps": len(measured),
        "ai_steps": len(steps),
        "failed_steps": [s["id"] for _, s in steps if not s.get("measured")],
        "include_frontier": include_frontier,
        "sample_model": sample_model.id,
        # Everything spent on measuring so far, including earlier runs of re-measured steps.
        "cost_egp": {
            "examples": round(previous.get("examples", 0.0) + sample_cost, 4),
            "models": round(previous.get("models", 0.0) + sum(r["cost_egp"] for r in records[len(kept):]), 4),
            "reviewers": round(previous.get("reviewers", 0.0)
                               + sum(r.get("grading_cost_egp", 0.0) for r in records[len(kept):]), 4),
        },
        "finished_at": datetime.now(timezone.utc).isoformat(),
    }
    return recompute_totals(blueprint)


def save_blueprint(blueprint: dict[str, Any], results_dir: Path) -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    name = f"blueprint-{slug(blueprint.get('title') or blueprint['request'], 'blueprint')}-{stamp}"
    folder = unique_dir(results_dir, name)
    write_blueprint(folder, blueprint)
    return folder.name


def ai_steps(blueprint: dict[str, Any]) -> list[dict[str, Any]]:
    return [s for w in blueprint["workflows"] for s in w["steps"] if s["actor"] == "ai"]


def find_step(blueprint: dict[str, Any], step_id: str, ai_only: bool = False) -> dict[str, Any] | None:
    return next((s for w in blueprint["workflows"] for s in w["steps"]
                 if s["id"] == step_id and (not ai_only or s["actor"] == "ai")), None)


def cost_per_correct(card: dict[str, Any]) -> float:
    """Cost per answer divided by the pass rate (floored at 50%), so unreliable models aren't priced as cheap."""
    return card["cost_per_answer_egp"] / max(card["pass_rate"], 0.5)


def recompute_totals(blueprint: dict[str, Any]) -> dict[str, Any]:
    """Recalculate every monthly cost from per-answer costs and the current volumes."""
    registry = blueprint.get("estimate", {}).get("registry", {})
    strongest = registry.get(blueprint.get("strongest_model") or "")
    strongest_per = cost_per_correct(strongest) if strongest else 0.0
    for workflow in blueprint["workflows"]:
        steps = [s for s in workflow["steps"] if s["actor"] == "ai"]
        for step in steps:
            volume = step.get("monthly_volume", 0)
            if step.get("cost_per_answer_egp") is not None:
                step["monthly_cost_egp"] = step["cost_per_answer_egp"] * volume
            step["strongest_monthly_cost_egp"] = strongest_per * volume
            if step.get("measured"):
                step["measured"]["monthly_cost_egp"] = step["measured"]["cost_per_answer_egp"] * volume
        workflow["monthly_cost_egp"] = sum(s.get("monthly_cost_egp") or 0.0 for s in steps)
    steps = ai_steps(blueprint)
    blueprint["monthly_cost_egp"] = sum(w["monthly_cost_egp"] for w in blueprint["workflows"])
    blueprint["strongest_monthly_cost_egp"] = sum(s["strongest_monthly_cost_egp"] for s in steps)
    if blueprint.get("verification"):
        blueprint["verification"]["monthly_cost_egp"] = sum(
            s["measured"]["monthly_cost_egp"] if s.get("measured") else (s.get("monthly_cost_egp") or 0.0) for s in steps
        )
    return blueprint


def load_blueprint(folder: Path) -> dict[str, Any]:
    return json.loads((folder / "blueprint.json").read_text(encoding="utf-8"))


def write_blueprint(folder: Path, blueprint: dict[str, Any]) -> None:
    (folder / "blueprint.json").write_text(json.dumps(blueprint, indent=2, ensure_ascii=False), encoding="utf-8")
