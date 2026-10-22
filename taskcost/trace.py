"""How we got this answer: every step behind a result, with the model that ran it, where that model runs,
how long it took and what it cost. Built from what each run already records, so it shows what happened."""

from __future__ import annotations

from typing import Any

from .registry import USE_CASES
from .sovereignty import LEVELS, country_name, model_fit
from .user_profile import DOMAIN_LABELS

LEVEL_NAMES = {"simple": "a single task", "workflow": "a workflow", "project": "a project", "unclear": "unclear"}
USE_NAMES = {"chat": "a chatbot", "realtime": "a workflow an app waits on", "batch": "a background (batch) job"}


def _step(title: str, detail: str, model: str | None = None, country: str = "EG", seconds: float | None = None,
          cost: float | None = None, cached: bool = False, items: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    return {"title": title, "detail": detail, "model": model, "fit": model_fit(model, country) if model else None,
            "seconds": None if seconds is None else round(seconds, 1), "cost_egp": cost, "cached": cached,
            "items": items or []}


def _pct(p: float | None) -> str:
    return f"{round(p * 100)}%" if p is not None else "?"


def _sovereignty_step(policy: dict[str, Any], helpers: dict[str, Any] | None, judges: list[str],
                      skipped: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    country = policy.get("country", "EG")
    level = policy.get("level", "open")
    used = sorted({m for m in (helpers or {}).values() if m})
    parts = [f"{LEVELS.get(level, level)} for {country_name(country)}."]
    if used:
        parts.append(f"Helpers that read your request: {', '.join(used)}.")
    if judges:
        parts.append(f"Reviewers: {', '.join(judges)}.")
    if skipped:
        parts.append("Skipped because they run outside the places your law allows: "
                     + ", ".join(t["model"] for t in skipped) + ".")
    parts += policy.get("notes") or []
    return _step("Applied your sovereignty choice", " ".join(parts))


def data_locations(steps: list[dict[str, Any]]) -> dict[str, int]:
    """How many steps sent your request to a model in your country (or a place your law allows), and how many didn't."""
    fits = [s["fit"]["fit"] for s in steps if s.get("fit")] + [i["fit"]["fit"] for s in steps for i in s["items"] if i.get("fit")]
    return {"local": sum(f in ("in_country", "allowed") for f in fits), "total": len(fits)}


def quick_trace(config: dict[str, Any], tiers: list[dict[str, Any]], records: list[dict[str, Any]],
                recommended: dict[str, Any] | None) -> dict[str, Any]:
    country = config.get("country", "EG")
    steps = []
    size = config.get("size")
    if size and size.get("model"):
        use_p = (size.get("use_case_probabilities") or {}).get(config.get("use_case"))
        detail = (f"Read it as {LEVEL_NAMES.get(size.get('level'), size.get('level'))} "
                  f"({_pct((size.get('probabilities') or {}).get(size.get('level')))} sure)")
        if config.get("use_case_guess"):
            detail += f", used as {USE_NAMES.get(config.get('use_case'), config.get('use_case'))} ({_pct(use_p)} sure)"
        if size.get("domain") and size["domain"] != "general":
            detail += f", from {DOMAIN_LABELS.get(size['domain'], size['domain'])}"
        steps.append(_step("Understood your request", detail + ".", size["model"], country, size.get("seconds"),
                           size.get("cost_egp"), size.get("cached")))
    personal = config.get("personalization")
    if personal and (personal.get("context") or personal.get("use_case_note")):
        steps.append(_step("Tailored it to you", " ".join(filter(None, [personal.get("context"), personal.get("use_case_note")]))))
    steps.append(_sovereignty_step({"country": country, "level": config.get("sovereignty", "open"),
                                    "notes": config.get("sovereignty_notes") or []},
                                   {k: v for k, v in (config.get("helpers") or {}).items() if k in ("quick_design_model", "profiler_model")},
                                   config.get("judge_models") or [], config.get("skipped_tiers")))
    design = config.get("design") or {}
    if design.get("model"):
        steps.append(_step("Wrote a concrete test task",
                           (design.get("assumptions") or "Turned your request into one task every model gets with the same facts."),
                           design["model"], country, design.get("seconds"), design.get("cost_egp")))
    advice = config.get("recommendation")
    if advice:
        steps.append(_step("Agent 1 predicted the best model before testing",
                           f"Predicted {advice.get('model') or 'none'}. {advice.get('note', '')}",
                           (config.get("helpers") or {}).get("profiler_model"), country, advice.get("latency_s"),
                           advice.get("cost_egp"), advice.get("cached")))
    budget = USE_CASES.get(config.get("use_case", "batch"), {}).get("max_p90_s")
    items = [{"model": t["model"], "fit": model_fit(t["model"], country), "seconds": t["latency_s"],
              "cost_egp": t["cost_per_answer_egp"],
              "detail": ("passed" if t["runs"] and t["passed"] == t["runs"] else f"failed {t['runs'] - t['passed']} of {t['runs']}")
                        + ("" if t["fast_enough"] else ", too slow")} for t in tiers]
    trials = config.get("trials") or 1
    steps.append(_step(f"Tested {len(tiers)} models on the same task",
                       f"Each model answered {'once' if trials == 1 else f'{trials} times'}, all at the same time"
                       + (f"; a {USE_NAMES.get(config.get('use_case'))} must answer within {budget:g} s." if budget else "."),
                       seconds=(config.get("timeline") or {}).get("run_s"),
                       cost=sum(r["cost_egp"] for r in records), items=items))
    judges = config.get("judge_models") or []
    graded = [r for r in records if r.get("criteria")]
    settled = sum(1 for r in graded if len({j for c in r["criteria"] for j in c.get("by_judge", {})}) < len(judges))
    steps.append(_step(f"Reviewed {len(graded)} answers against {len((graded[0]['criteria']) if graded else [])} checks",
                       f"{len(judges)} reviewers vote on each check; a check passes on a majority."
                       + (f" For {settled} answers the first {len(judges) // 2 + 1} reviewers already agreed everything passed, "
                          "so the last one was skipped to save cost." if settled else ""),
                       cost=sum(r.get("grading_cost_egp", 0.0) for r in records),
                       items=[{"model": j, "fit": model_fit(j, country), "seconds": None, "cost_egp": None, "detail": "reviewer"}
                              for j in judges]))
    rule = ("closest to you first, then " if config.get("sovereignty", "open") != "open" else "") + \
           ("open source before proprietary, then " if config.get("prefer_open_weights", True) else "") + "the cheapest"
    steps.append(_step("Chose the recommendation",
                       (f"{recommended['model']}: " if recommended else "No model qualified. ")
                       + f"among the models that passed every check and were fast enough, we pick {rule}."))
    total_s = sum(s["seconds"] or 0 for s in steps)
    total_cost = sum(s["cost_egp"] or 0 for s in steps)
    return {"steps": steps, "seconds": round(total_s, 1), "cost_egp": round(total_cost, 4), "data": data_locations(steps)}


def blueprint_trace(blueprint: dict[str, Any], helpers: dict[str, Any] | None, judges: list[str]) -> dict[str, Any]:
    policy = blueprint.get("sovereignty") or {"country": "EG", "level": "open"}
    country = policy.get("country", "EG")
    steps = []
    size = blueprint.get("size") or {}
    if size.get("model"):
        steps.append(_step("Understood your request",
                           f"Read it as {LEVEL_NAMES.get(size.get('level'), size.get('level'))} "
                           f"({_pct((size.get('probabilities') or {}).get(size.get('level')))} sure), so we mapped how it runs "
                           "instead of testing one task.", size["model"], country, size.get("seconds"), size.get("cost_egp"),
                           size.get("cached")))
    personal = blueprint.get("personalization")
    if personal and personal.get("context"):
        steps.append(_step("Tailored it to you", personal["context"]))
    steps.append(_sovereignty_step(policy, helpers, judges))
    design = blueprint.get("design") or {}
    ai = [s for w in blueprint["workflows"] for s in w["steps"] if s["actor"] == "ai"]
    build = sum(1 for s in ai if s.get("phase") == "build")
    steps.append(_step("Mapped it onto standard enterprise processes",
                       f"{len(blueprint['workflows'])} workflows, {len(ai)} AI steps ({build} one-time setup), with the "
                       f"assumptions listed below.", design.get("model"), country, design.get("seconds"), design.get("cost_egp")))
    estimate = blueprint.get("estimate") or {}
    steps.append(_step("Agent 1 chose a model for every AI step",
                       f"All {len(ai)} steps at once, from the measured model records: the cheapest model that is reliable and "
                       "fast enough for each step, closest to you first.",
                       (helpers or {}).get("profiler_model"), country, cost=estimate.get("profiler_cost_egp")))
    v = blueprint.get("verification")
    if v:
        spent = sum((v.get("cost_egp") or {}).values())
        steps.append(_step("Measured it for real",
                           f"Wrote a realistic example for each AI step, checked the test itself, then tried the cheapest models "
                           f"first: {v.get('measured_steps')} of {v.get('ai_steps')} steps measured.",
                           v.get("sample_model"), country, cost=spent))
    total_cost = sum(s["cost_egp"] or 0 for s in steps)
    return {"steps": steps, "seconds": round(sum(s["seconds"] or 0 for s in steps), 1), "cost_egp": round(total_cost, 4),
            "data": data_locations(steps)}
