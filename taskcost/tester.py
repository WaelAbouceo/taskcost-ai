"""Testing agent: try the requests people actually budget, and record what the system returns.

People ask TaskCost to price four kinds of work:

- task: one job with one answer, such as writing one email
- workflow: one recurring process, such as supplier invoices from receipt to payment
- project: a business function made of several processes, such as running customer service
- system: software to implement, replace or migrate, such as an ERP or a CRM

The product has three sizes. A task should be read as simple. A workflow should stay a workflow.
A project and a system should both be read as a project: a system is budgeted as the processes it runs,
not as a fourth size.

Each round draws a fresh mix, using requests not yet tried in this run first. `size` only asks how the
request was read (cheap). `budget` also returns the figure the product would show: a library cost per
1,000 tasks for a simple request, or the plan's monthly, setup and first-year cost for anything bigger.
Plans are not measured on a new example here; that is the separate verification step.
"""

from __future__ import annotations

import asyncio
import json
import random
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, TextIO

from .blueprint import (
    Backend, ai_steps, cost_per_correct, design_blueprint, estimate_blueprint, size_request,
)
from .client import APIError, ModelInfo
from .config import BenchmarkConfig
from .naming import unique_dir
from .recommender import recommend_hybrid
from .registry import ModelCard
from .sovereignty import accepted, residency_ranks
from .user_profile import stated_monthly_volume

SCOPES = ("task", "workflow", "project", "system")

# What a person would type. The scope is the kind of work they mean to budget, which is what the
# agent checks the system's reading against.
CASES: tuple[dict[str, str], ...] = (
    {"id": "t1", "scope": "task", "request":
     "Write a short Arabic email to Nour in Cairo. Her order 18442 is delayed until 28 October. "
     "Apologise and tell her she can cancel for a full refund."},
    {"id": "t2", "scope": "task", "request":
     "Extract the supplier, invoice number, date and total from this invoice:\n"
     "Nile Supplies\nInvoice NS-441\n12 March 2026\nTotal 18,640 EGP"},
    {"id": "t3", "scope": "task", "request":
     "Classify this ticket as billing, shipping or product, and give the reason in one sentence: "
     "The customer was charged twice for order 9021."},
    {"id": "t4", "scope": "task", "request":
     "Turn these meeting notes into action items with an owner for each: Hany will send the price list "
     "by Thursday. Mona will check the warehouse count. We still need someone to call the client."},
    {"id": "t5", "scope": "task", "request":
     "A customer on live chat asks whether the 14-day return covers opened headphones. Our policy allows "
     "returns within 14 days if the item is unused. Write the reply."},
    {"id": "t6", "scope": "task", "request":
     "Translate this into Egyptian Arabic, keeping the numbers: Payment is due within 15 days. "
     "Late invoices incur 2% per month."},
    {"id": "w1", "scope": "workflow", "request":
     "Handle supplier invoices from the moment they arrive until we pay them. We get about 800 invoices "
     "a month. Read each invoice, match it to the purchase order, and draft a message to the supplier "
     "when they don't match."},
    {"id": "w2", "scope": "workflow", "request":
     "Answer customer emails for our online shop. About 2,000 messages a month, mixed Arabic and English. "
     "Classify each one and draft a reply from our return and delivery policy."},
    {"id": "w3", "scope": "workflow", "request":
     "Screen CVs for the roles we have open, about 250 applications a month. Compare each CV with the "
     "job description and list who should be interviewed."},
    {"id": "w4", "scope": "workflow", "request":
     "Every time a shipment slips, tell the customer. About 400 delayed shipments a month. Draft the "
     "update from the tracking note."},
    {"id": "w5", "scope": "workflow", "request":
     "Review incoming customer contracts, about 30 a month. Pull the end date, the liability cap and "
     "any clause that differs from our standard terms."},
    {"id": "w6", "scope": "workflow", "request":
     "نراجع فواتير الموردين من ساعة ما توصل لحد ما ندفعها. حوالي 800 فاتورة في الشهر. نقرأ الفاتورة "
     "ونطابقها مع أمر الشراء ونكتب للمورد لو في فرق."},
    {"id": "p1", "scope": "project", "request":
     "We are a food distributor in Giza with about 400 people. I need the cost of AI across purchasing, "
     "warehouse receiving and collecting what customers owe us."},
    {"id": "p2", "scope": "project", "request":
     "A 150-bed hospital in Alexandria wants AI for appointments, insurance claims and the messages "
     "patients send after a visit. What would that cost to run?"},
    {"id": "p3", "scope": "project", "request":
     "Take customer operations for a mobile operator: complaints, billing questions, SIM swaps and "
     "outage texts. Roughly 50,000 subscriber contacts a month."},
    {"id": "p4", "scope": "project", "request":
     "Run hiring through to the first month on the job for a factory of 1,200 people: job posts, "
     "CV screening, offer letters and the questions new hires ask HR."},
    {"id": "p5", "scope": "project", "request":
     "We sell industrial spare parts. Budget AI for the whole sales cycle: new leads, quotes, order "
     "emails and chasing late payments. About 80 staff."},
    {"id": "p6", "scope": "project", "request":
     "وزارة محلية عايزة تقدر تكلفة الذكاء الاصطناعي على خدمة المواطنين: استخراج بيانات الطلبات، "
     "الرد على الاستفسارات، وتلخيص الشكاوى. حوالي 20 ألف معاملة في الشهر."},
    {"id": "s1", "scope": "system", "request":
     "Implement an ERP for a mid-sized distributor in Egypt, about 500 employees. Replace the spreadsheets "
     "for purchasing, sales, inventory and accounts."},
    {"id": "s2", "scope": "system", "request":
     "Replace our CRM. Migrate 15,000 old tickets and run leads plus customer support on the new system."},
    {"id": "s3", "scope": "system", "request":
     "Launch an IT helpdesk. Build the knowledge base from our current wiki, about 400 articles, then "
     "handle incidents and password resets."},
    {"id": "s4", "scope": "system", "request":
     "We are buying a warehouse system. Migrate 3 years of stock history and run receiving, pick "
     "exceptions and delivery updates."},
    {"id": "s5", "scope": "system", "request":
     "Put in a contract system: load about 2,000 existing contracts, then review, approve and renew them on it."},
    {"id": "s6", "scope": "system", "request":
     "عايزين نظام ERP كامل لشركة توزيع في مصر، من المشتريات للمبيعات للمخازن والحسابات، وننقل بيانات "
     "سنتين من الإكسيل."},
)


def expected_level(scope: str) -> str:
    """The product size this scope should be read as. A system is a project."""
    if scope == "task":
        return "simple"
    if scope == "workflow":
        return "workflow"
    if scope in ("project", "system"):
        return "project"
    raise ValueError(f"scope must be one of {', '.join(SCOPES)}")


def is_match(scope: str, size: dict[str, Any]) -> bool:
    """A task matches only when it is sent down the simple path (confidently simple)."""
    if scope == "task":
        return bool(size.get("is_simple"))
    return size.get("level") == expected_level(scope)


def parse_scopes(text: str) -> tuple[str, ...]:
    scopes = tuple(part.strip().lower() for part in text.split(",") if part.strip())
    unknown = [s for s in scopes if s not in SCOPES]
    if unknown or not scopes:
        raise ValueError(f"scope must list one or more of {', '.join(SCOPES)}")
    return tuple(dict.fromkeys(scopes))


def draw_cases(
    cases: list[dict[str, str]], count: int, rng: random.Random, scopes: tuple[str, ...], used: set[str],
) -> list[dict[str, str]]:
    """Spread `count` across the scopes. Prefer requests this run has not tried yet; no duplicates in one draw."""
    available = tuple(s for s in scopes if any(c["scope"] == s for c in cases))
    if not available or count < 1:
        return []
    base, extra = divmod(count, len(available))
    picked: list[dict[str, str]] = []
    for index, scope in enumerate(available):
        quota = base + (1 if index < extra else 0)
        group = [c for c in cases if c["scope"] == scope]
        fresh = [c for c in group if c["request"] not in used]
        pool = list(fresh or group)
        rng.shuffle(pool)
        take = pool[:quota]
        if len(take) < quota and fresh:
            taken = {c["request"] for c in take}
            rest = [c for c in group if c["request"] not in taken]
            rng.shuffle(rest)
            take.extend(rest[: quota - len(take)])
        picked.extend(take)
    rng.shuffle(picked)
    return picked


def _sure(size: dict[str, Any]) -> str:
    level = size.get("level")
    probability = (size.get("probabilities") or {}).get(level)
    return f"{round(100 * float(probability))}%" if probability is not None else "n/a"


def _egp(value: float | None) -> str:
    if value is None:
        return "n/a"
    if abs(value) >= 100:
        return f"{value:,.0f} EGP"
    return f"{value:,.2f} EGP"


def budget_line(budget: dict[str, Any] | None) -> str:
    if not budget:
        return ""
    if budget["kind"] == "simple":
        monthly = (f", {_egp(budget['monthly_cost_egp'])}/month at {budget['monthly_volume']:,}"
                   if budget.get("monthly_volume") and budget.get("monthly_cost_egp") is not None else "")
        return f"{budget.get('model') or 'no model'} · {_egp(budget.get('cost_per_1000_egp'))} per 1,000{monthly}"
    names = ", ".join(w["name"] for w in budget.get("workflows", []))
    once = f" + {_egp(budget['one_time_cost_egp'])} setup" if budget.get("one_time_cost_egp") else ""
    return (f"{budget.get('title') or 'plan'} · {_egp(budget.get('monthly_cost_egp'))}/month{once}, "
            f"{_egp(budget.get('first_year_cost_egp'))} first year"
            + (f" ({names})" if names else ""))


def build_summary(records: list[dict[str, Any]]) -> dict[str, Any]:
    by_scope: dict[str, dict[str, int]] = {}
    confusion: dict[str, dict[str, int]] = {}
    for record in records:
        scope = record["scope"]
        scope_row = by_scope.setdefault(scope, {"matched": 0, "total": 0})
        scope_row["total"] += 1
        scope_row["matched"] += int(bool(record.get("matched")))
        read_as = (record.get("size") or {}).get("level") or "failed"
        counts = confusion.setdefault(scope, {})
        counts[read_as] = counts.get(read_as, 0) + 1
    return {
        "matched": sum(row["matched"] for row in by_scope.values()),
        "total": len(records),
        "by_scope": by_scope,
        "confusion": confusion,
        "errors": sum(1 for r in records if r.get("error")),
        "agent_cost_egp": round(sum(r.get("agent_cost_egp") or 0.0 for r in records), 4),
    }


def format_report(records: list[dict[str, Any]], meta: dict[str, Any], summary: dict[str, Any]) -> str:
    lines = [
        "# Testing agent",
        "",
        f"{summary['total']} requests, depth {meta['depth']}, seed {meta['seed']}.",
        "A system is supposed to be read as a project. A task matches only when it is confidently simple.",
        "",
        f"Matched {summary['matched']}/{summary['total']}.",
    ]
    for scope, row in summary["by_scope"].items():
        lines.append(f"- {scope}: {row['matched']}/{row['total']}")
    lines += ["", "How they were read (rows are what the person meant, columns are what the system said):", ""]
    columns = ("simple", "workflow", "project", "unclear", "failed")
    lines.append("| meant | " + " | ".join(columns) + " |")
    lines.append("| --- | " + " | ".join("---" for _ in columns) + " |")
    for scope, counts in summary["confusion"].items():
        lines.append("| " + scope + " | " + " | ".join(str(counts.get(col, 0)) for col in columns) + " |")
    lines += ["", "## Requests", ""]
    for index, record in enumerate(records, start=1):
        size = record.get("size") or {}
        verdict = "match" if record.get("matched") else "miss"
        if size:
            reading = (f"Read as {size.get('level')} ({_sure(size)} sure), "
                       f"{size.get('use_case', 'n/a')}, {size.get('domain', 'n/a')}.")
        else:
            reading = "Not read."
        lines.append(f"{index}. **{record['scope']}** — {verdict}. {reading}")
        lines.append(f"   {record['request']}")
        if record.get("budget"):
            basis = "library estimate" if record["budget"]["basis"] == "library" else "plan estimate, not measured on a new example"
            lines.append(f"   Budget ({basis}): {budget_line(record['budget'])}")
        if record.get("error"):
            lines.append(f"   Error: {record['error']}")
        lines.append("")
    lines.append(f"Agent cost {summary['agent_cost_egp']:.4f} EGP. "
                 f"Profiler {meta.get('profiler_model') or 'n/a'}.")
    return "\n".join(lines).rstrip() + "\n"


def write_probe(folder: Path, records: list[dict[str, Any]], meta: dict[str, Any], summary: dict[str, Any]) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "cases.jsonl").write_text(
        "".join(json.dumps(record, ensure_ascii=False) + "\n" for record in records), encoding="utf-8")
    (folder / "summary.json").write_text(
        json.dumps({"meta": meta, "summary": summary}, indent=2, ensure_ascii=False), encoding="utf-8")
    (folder / "report.md").write_text(format_report(records, meta, summary), encoding="utf-8")


async def _budget_simple(
    backend: Backend, jev: ModelInfo, request: str, size: dict[str, Any], cards: dict[str, ModelCard],
    escalate_at: float, prefer_open: bool, min_pass_rate: float, residency: dict[str, int] | None,
) -> dict[str, Any]:
    use_case = size["use_case"]
    recommendation = await recommend_hybrid(
        backend, jev, request, use_case, cards, escalate_at, prefer_open, min_pass_rate, residency)
    card = cards.get(recommendation.model) if recommendation.model else None
    per = cost_per_correct(card.to_dict()) if card else None
    volume = stated_monthly_volume(request)
    return {
        "kind": "simple",
        "basis": "library",
        "model": recommendation.model,
        "model_tier": card.tier if card else None,
        "note": recommendation.note,
        "use_case": use_case,
        "cost_per_answer_egp": round(per, 6) if per is not None else None,
        "cost_per_1000_egp": round(per * 1000, 2) if per is not None else None,
        "monthly_volume": volume,
        "monthly_cost_egp": round(per * volume, 2) if per is not None and volume else None,
        "agent_cost_egp": round(recommendation.cost_egp, 6),
    }


async def _budget_plan(
    backend: Backend, jev: ModelInfo, designer: ModelInfo, request: str, cards: dict[str, ModelCard],
    escalate_at: float, prefer_open: bool, min_pass_rate: float, residency: dict[str, int] | None,
) -> dict[str, Any]:
    blueprint = await design_blueprint(backend, designer, request)
    blueprint = await estimate_blueprint(
        blueprint, backend, jev, cards, escalate_at, prefer_open, min_pass_rate, residency)
    return {
        "kind": "blueprint",
        "basis": "plan",
        "level": blueprint["level"],
        "title": blueprint["title"],
        "summary": blueprint["summary"],
        "key_assumptions": blueprint.get("key_assumptions") or [],
        "workflows": [
            {"name": workflow["name"], "steps": len(workflow["steps"]),
             "ai_steps": sum(1 for step in workflow["steps"] if step["actor"] == "ai"),
             "monthly_cost_egp": round(workflow["monthly_cost_egp"], 2),
             "one_time_cost_egp": round(workflow["one_time_cost_egp"], 2)}
            for workflow in blueprint["workflows"]
        ],
        "ai_steps": len(ai_steps(blueprint)),
        "monthly_cost_egp": round(blueprint["monthly_cost_egp"], 2),
        "one_time_cost_egp": round(blueprint["one_time_cost_egp"], 2),
        "first_year_cost_egp": round(blueprint["first_year_cost_egp"], 2),
        "agent_cost_egp": round(blueprint["design"]["cost_egp"] + blueprint["estimate"]["profiler_cost_egp"], 6),
    }


async def probe_case(
    backend: Backend, jev: ModelInfo, case: dict[str, str], *, depth: str,
    designer: ModelInfo | None = None, cards: dict[str, ModelCard] | None = None,
    escalate_at: float = 0.5, prefer_open: bool = True, min_pass_rate: float = 0.0,
    residency: dict[str, int] | None = None, repeat: bool = False,
) -> dict[str, Any]:
    """Size one request and, at budget depth, price it the way the product would."""
    record: dict[str, Any] = {
        "id": case["id"], "scope": case["scope"], "request": case["request"], "repeat": repeat,
        "expected_level": expected_level(case["scope"]), "size": None, "matched": False,
        "budget": None, "error": None, "agent_cost_egp": 0.0,
    }
    try:
        size = await size_request(backend, jev, case["request"])
    except APIError:
        raise
    except Exception as exc:
        record["error"] = f"sizing failed: {exc}"
        return record
    record["size"] = size
    record["matched"] = is_match(case["scope"], size)
    record["agent_cost_egp"] = round(size.get("cost_egp") or 0.0, 6)
    if depth == "size":
        return record
    try:
        if size["is_simple"]:
            budget = await _budget_simple(
                backend, jev, case["request"], size, cards or {}, escalate_at, prefer_open, min_pass_rate, residency)
        else:
            if designer is None:
                raise ValueError("blueprint_model is not set")
            budget = await _budget_plan(
                backend, jev, designer, case["request"], cards or {}, escalate_at, prefer_open, min_pass_rate, residency)
        record["budget"] = budget
        record["agent_cost_egp"] = round(record["agent_cost_egp"] + budget["agent_cost_egp"], 6)
    except APIError:
        raise
    except Exception as exc:
        record["error"] = str(exc)
    return record


async def run_probe(
    backend: Backend, *, jev: ModelInfo, config: BenchmarkConfig, count: int, rounds: int,
    scopes: tuple[str, ...], depth: str, seed: int, out_dir: Path,
    designer: ModelInfo | None = None, cards: dict[str, ModelCard] | None = None,
    cases: list[dict[str, str]] | None = None, concurrency: int = 4, log: TextIO = sys.stderr,
) -> dict[str, Any]:
    """Draw requests, probe each one, and write report.md, cases.jsonl and summary.json."""
    if depth not in ("size", "budget"):
        raise ValueError("depth must be size or budget")
    if count < 1 or rounds < 1:
        raise ValueError("count and rounds must be at least 1")
    catalog = list(cases if cases is not None else CASES)
    priced = {model: card for model, card in (cards or {}).items() if accepted(config, model)}
    if depth == "budget":
        if designer is None:
            raise ValueError("Set blueprint_model in benchmark.yaml to a model in the catalog.")
        if not priced:
            raise ValueError("No measured model registry yet; run `taskcost calibrate` first.")
    residency = residency_ranks(config, priced) if depth == "budget" else None
    rng = random.Random(seed)
    used: set[str] = set()
    drawn: list[tuple[int, dict[str, str], bool]] = []
    for round_no in range(1, rounds + 1):
        batch = draw_cases(catalog, count, rng, scopes, used)
        for case in batch:
            drawn.append((round_no, case, case["request"] in used))
            used.add(case["request"])
    if not drawn:
        raise ValueError("No requests match those scopes.")

    meta = {
        "created_at": datetime.now(timezone.utc).isoformat(), "seed": seed, "depth": depth, "rounds": rounds,
        "scopes": list(scopes), "profiler_model": jev.id, "blueprint_model": designer.id if designer else None,
    }
    records: list[dict[str, Any]] = []

    def finish() -> dict[str, Any]:
        summary = build_summary(records)
        folder = unique_dir(out_dir, f"probe-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}")
        write_probe(folder, records, meta, summary)
        return {"folder": folder, "records": records, "summary": summary, "meta": meta}

    async def once(item: tuple[int, dict[str, str], bool]) -> dict[str, Any]:
        round_no, case, repeat = item
        record = await probe_case(
            backend, jev, case, depth=depth, designer=designer, cards=priced,
            escalate_at=config.escalate_at, prefer_open=config.prefer_open_weights,
            min_pass_rate=config.min_model_pass_rate, residency=residency, repeat=repeat)
        record["round"] = round_no
        return record

    try:
        if depth == "size":
            print(f"Reading {len(drawn)} requests...", file=log)
            semaphore = asyncio.Semaphore(max(concurrency, 1))

            async def limited(item: tuple[int, dict[str, str], bool]) -> dict[str, Any]:
                async with semaphore:
                    return await once(item)

            records = list(await asyncio.gather(*(limited(item) for item in drawn)))
        else:
            for index, item in enumerate(drawn, start=1):
                _, case, _ = item
                print(f"[{index}/{len(drawn)}] {case['scope']}: {case['request'][:80]}", file=log, flush=True)
                records.append(await once(item))
                print(f"    {_line(records[-1])}", file=log, flush=True)
    except APIError:
        if records:
            finish()
        raise
    return finish()


def _line(record: dict[str, Any]) -> str:
    size = record.get("size") or {}
    verdict = "match" if record.get("matched") else "miss"
    reading = f"{verdict}, read as {size.get('level', 'failed')} ({_sure(size)} sure)" if size else "miss, not read"
    extra = budget_line(record.get("budget"))
    error = f"  error: {record['error']}" if record.get("error") else ""
    return reading + (f"  {extra}" if extra else "") + error


def format_run(result: dict[str, Any]) -> str:
    """The terminal summary after a run."""
    lines = [format_report(result["records"], result["meta"], result["summary"]).rstrip(),
             "", f"Saved to {result['folder']}"]
    return "\n".join(lines)
