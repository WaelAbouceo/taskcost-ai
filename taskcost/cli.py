"""Command-line entry point.

Jobs (the main flow):   taskcost plan -> taskcost show -> taskcost execute
Standard task suite:    taskcost validate / run
Utilities:              taskcost models / report
"""

from __future__ import annotations

import argparse
import asyncio
import re
import sys
from dataclasses import replace
from pathlib import Path

from .client import SovereignClient, client_from_env
from .config import load_config
from .design import design_plan
from .execute import execute_plan
from .graders import Assessment, GradeContext, grade
from .mock import MockBackend, mock_config
from .plan import Plan, describe
from .report import finalize_run
from .runner import FatalRunError, resolve_panel, run_benchmark
from .tasks import load_tasks


def _client(timeout_s: float = 90.0) -> SovereignClient:
    client = client_from_env(timeout_s)
    if client is None:
        sys.exit("LLM_API_KEY is not set. Add it to the environment or a .env file (see .env.example).")
    return client


async def _cmd_models(args: argparse.Namespace) -> int:
    client = _client()
    try:
        catalog = await client.list_models()
    finally:
        await client.aclose()
    rows = [m for m in catalog.values() if not args.modality or m.modality == args.modality]
    print(f"{'model':40} {'modality':10} {'in/1M':>9} {'out/1M':>9} {'cache/1M':>9}  status")
    for m in sorted(rows, key=lambda m: (m.modality, m.id)):
        cache = "-" if m.pricing.cache_read_per_1m is None else f"{m.pricing.cache_read_per_1m:.2f}"
        print(
            f"{m.id:40} {m.modality:10} {m.pricing.input_per_1m:9.2f} {m.pricing.output_per_1m:9.2f} "
            f"{cache:>9}  {m.status or ''}"
        )
    print("\nPrices in EGP per 1M tokens.")
    return 0


class _AlwaysPassJudge:
    async def assess(self, *, criteria: list[str], **_: object) -> Assessment:
        return Assessment([{"stub": 1.0} for _ in criteria], [""] * len(criteria), 0.0)


async def _cmd_validate(args: argparse.Namespace) -> int:
    if args.live:
        return await _validate_live(args)
    tasks = load_tasks(args.tasks)
    failures = 0
    for task in tasks:
        if not task.reference:
            print(f"WARN  {task.id}: no reference answer")
            continue
        if task.needs_judge and not task.counterexamples:
            print(f"WARN  {task.id}: no counterexamples, so judge strictness can't be calibrated")
        result = await grade(task.grader, task.reference, GradeContext(task=task, judge=_AlwaysPassJudge()))
        if result.passed:
            note = " (judge criteria checked only with --live)" if task.needs_judge else ""
            print(f"OK    {task.id}{note}")
        else:
            failures += 1
            print(f"FAIL  {task.id}: reference answer fails its own grader: {result.detail}")
    print(f"\n{len(tasks)} tasks, {failures} failing.")
    return 1 if failures else 0


async def _validate_live(args: argparse.Namespace) -> int:
    """The judge panel must pass every reference answer and fail every counterexample."""
    config = load_config(args.config)
    source = args.tasks if args.tasks != "tasks" else config.tasks_path
    tasks = [t for t in load_tasks(source) if t.needs_judge and t.reference]
    client = _client(config.timeout_s)
    try:
        panel = resolve_panel(config, await client.list_models(), client)
        semaphore = asyncio.Semaphore(config.concurrency)

        async def check(task, label: str, answer: str, should_pass: bool):
            async with semaphore:
                result = await grade(
                    task.grader, answer,
                    GradeContext(task=task, judge=panel, judge_threshold=config.judge_threshold),
                )
            return task.id, label, should_pass, result

        checks = [check(t, "reference", t.reference, True) for t in tasks] + [
            check(t, f"counterexample {i}", answer, False)
            for t in tasks for i, answer in enumerate(t.counterexamples, start=1)
        ]
        results = await asyncio.gather(*checks)
    finally:
        await client.aclose()

    wrong = 0
    for task_id, label, should_pass, result in sorted(results, key=lambda r: (r[0], r[1])):
        correct = result.passed == should_pass
        wrong += not correct
        verdict = "PASS" if result.passed else "FAIL"
        status = "OK   " if correct else "WRONG"
        print(f"{status} {task_id:40} {label:18} judged {verdict}  score={result.score:.2f}  {result.detail[:110]}")
    cost = sum(r[3].cost_egp for r in results)
    print(f"\nPanel {', '.join(panel.names)}: {len(results) - wrong}/{len(results)} verdicts correct, "
          f"grading cost {cost:.4f} EGP.")
    return 1 if wrong else 0


async def _cmd_run(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    if args.trials:
        config = replace(config, trials=args.trials)
    tasks = load_tasks(config.tasks_path)
    if args.only:
        tasks = [t for t in tasks if args.only in t.id or args.only == t.category]
        if not tasks:
            sys.exit(f"No tasks match {args.only!r}")

    if args.mock:
        config = mock_config(config)
        backend = MockBackend(tasks)
    else:
        backend = _client(config.timeout_s)

    try:
        run_dir = await run_benchmark(config, tasks, backend)
    except FatalRunError as exc:
        print(f"Run stopped: {exc}", file=sys.stderr)
        return 2
    finally:
        await backend.aclose()

    return _report(run_dir, config.min_success_rate)


def _report(run_dir: Path, min_success_rate: float | None) -> int:
    print(finalize_run(run_dir, min_success_rate))
    print(f"Reports written to {run_dir}")
    return 0


def _parse_docs(values: list[str]) -> dict[str, Path]:
    documents = {}
    for value in values:
        name, sep, path = value.partition("=")
        doc_path = Path(path if sep else value)
        if not doc_path.is_file():
            sys.exit(f"Document not found: {doc_path}")
        documents[name if sep else doc_path.stem] = doc_path
    return documents


async def _cmd_plan(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    model_id = args.design_model or config.design_model
    if not model_id:
        sys.exit("Set design_model in the benchmark config or pass --design-model.")
    documents = _parse_docs(args.doc)
    out = Path(args.out) if args.out else Path("plans") / f"{re.sub(r'[^a-z0-9]+', '-', args.request.lower()).strip('-')[:50]}.yaml"

    out.parent.mkdir(parents=True, exist_ok=True)
    client = _client(timeout_s=max(config.timeout_s, 600))
    try:
        catalog = await client.list_models()
        if model_id not in catalog:
            sys.exit(f"Design model {model_id!r} is not in the catalog. Run `taskcost models`.")
        print(f"Design agent ({model_id}) is breaking the request down...", file=sys.stderr)
        plan = await design_plan(client, catalog[model_id], args.request, documents, out.parent.resolve())
    finally:
        await client.aclose()

    plan.save(out)
    print(describe(plan))
    print(f"\nPlan saved to {out}. Review it (edit the file if needed, then set `reviewed: true`), and run:")
    print(f"  taskcost execute {out}")
    return 0


def _print_profile(label: str, profile) -> None:
    print(f"\n{label}")
    print(f"  Pick:      {profile.tier_class}  ({profile.size}, {profile.model_type}, {profile.language}, {profile.context} context)")
    print(f"  Work type: {profile.work_type}")
    print("  Scores:    " + ", ".join(f"{k} {v:.2f}" for k, v in profile.scores.items()))
    for reason in profile.reasons:
        print(f"  - {reason}")
    for note in profile.borderline:
        print(f"  ! borderline: {note}")
    print(f"  Cost {profile.cost_egp:.5f} EGP, {profile.latency_s:.1f}s")


async def _cmd_profile(args: argparse.Namespace) -> int:
    from .profiler import profile_task

    config = load_config(args.config)
    model_id = config.profiler_model or "jev-1.13"
    if not args.text and not args.library:
        sys.exit("Give a task text, or --library DIR")
    client = _client()
    decider = _cached(client, config, args.fresh)
    try:
        catalog = await client.list_models()
        model = catalog[model_id]
        if args.text:
            _print_profile(args.text[:80], await profile_task(decider, model, args.text))
        if args.library:
            tasks = load_tasks(args.library)
            profiles = await asyncio.gather(*(profile_task(decider, model, t.prompt) for t in tasks))
            for task, profile in zip(tasks, profiles):
                _print_profile(task.id, profile)
    finally:
        await client.aclose()
    return 0


async def _cmd_calibrate(args: argparse.Namespace) -> int:
    from .calibrate import calibrate

    config = load_config(args.config)
    tasks = load_tasks(args.library)
    if args.only:
        tasks = [t for t in tasks if args.only in t.id]
    client = _client(config.timeout_s)
    try:
        run_dir = await calibrate(config, tasks, client, config.profiler_model or "jev-1.13",
                                  trials=args.trials, full=args.full)
    except FatalRunError as exc:
        print(f"Run stopped: {exc}", file=sys.stderr)
        return 2
    finally:
        await client.aclose()
    print((run_dir / "report.md").read_text(encoding="utf-8"))
    print(f"Saved to {run_dir}")
    return 0


def _registry_from_runs(config, sources: list[str] | None):
    from .registry import build_registry, load_runs, save_registry, task_results

    dirs = [Path(s) for s in sources] if sources else sorted(config.output_path.glob("calibration*"))
    records = load_runs(dirs)
    if not records:
        sys.exit("No measured runs found. Run `taskcost calibrate` first.")
    cards = build_registry(records)
    save_registry(cards, Path("registry") / "models.json", [str(d) for d in dirs])
    return cards, task_results(records)


async def _cmd_recommend(args: argparse.Namespace) -> int:
    from .recommender import recommend, recommend_hybrid, rule_pick
    from .registry import USE_CASES, best_model

    config = load_config(args.config)
    cards, results = _registry_from_runs(config, args.runs)
    print("Model registry (from measured runs):")
    for card in cards.values():
        print(f"  {card.id:18} {card.describe()}")
    use_cases = list(USE_CASES) if args.use_case == "all" else [args.use_case]

    client = _client()
    decider = _cached(client, config, args.fresh)
    try:
        jev = (await client.list_models())[config.profiler_model or "jev-1.13"]
        if args.text:
            for use_case in use_cases:
                rec = await recommend(decider, jev, args.text, use_case, cards)
                print(f"\n{USE_CASES[use_case]['label']}\n  Recommended: {rec.model}  (Jev chose {rec.jev_choice}, "
                      f"confidence {rec.confidence:.2f})  {rec.note}")
                print("  Probabilities: " + ", ".join(f"{m} {p:.2f}" for m, p in rec.probabilities.items()))
            return 0

        tasks = load_tasks(args.library)
        jobs = [(t, u) for t in tasks for u in use_cases]
        recs = await asyncio.gather(*(recommend(decider, jev, t.prompt, u, cards) for t, u in jobs))
        hybrid = await asyncio.gather(*(recommend_hybrid(decider, jev, t.prompt, u, cards, config.escalate_at,
                                                         config.prefer_open_weights, config.min_model_pass_rate,
                                                         config.prefer_egypt_hosted)
                                        for t, u in jobs))
    finally:
        await client.aclose()

    mark = lambda ok: "✓" if ok else "✗"

    def good(pick, truth, task_id, use_case) -> bool:
        """A pick is good if it passed this task every time, is fast enough, respects the open-source preference
        and costs within 25% of the best option (near-identical prices shouldn't count as wrong)."""
        if pick == truth:
            return True
        entry, best = results.get((pick, task_id)), results.get((truth, task_id))
        if not pick or not truth or not entry or not best or entry["passed"] < entry["runs"]:
            return False
        if not cards[pick].fits(use_case):
            return False
        if config.prefer_open_weights and cards[truth].open_weights and not cards[pick].open_weights:
            return False
        if config.prefer_egypt_hosted and cards[truth].egypt_hosted and not cards[pick].egypt_hosted:
            return False
        pick_cost, best_cost = entry["cost"] / entry["runs"], best["cost"] / best["runs"]
        return pick_cost <= max(1.25 * best_cost, best_cost + 0.005)

    score = {"jev": 0, "hybrid": 0, "rule": 0}
    print(f"\n{'task':24} {'use case':9} {'right answer':15} {'Jev picks':18} {'code + Jev check':22} {'code only'}")
    for (task, use_case), rec, hyb in zip(jobs, recs, hybrid):
        truth = best_model(task.id, use_case, cards, results, config.prefer_open_weights, config.prefer_egypt_hosted)
        rule = rule_pick(cards, use_case, prefer_egypt=config.prefer_egypt_hosted)
        oks = {"jev": good(rec.model, truth, task.id, use_case), "hybrid": good(hyb.model, truth, task.id, use_case),
               "rule": good(rule, truth, task.id, use_case)}
        for key, ok in oks.items():
            score[key] += ok
        hyb_text = f"{hyb.model} {mark(oks['hybrid'])} (p={hyb.probabilities['needs_stronger']:.2f})"
        jev_text = f"{rec.model} {mark(oks['jev'])}"
        print(f"{task.id:24} {use_case:9} {str(truth):15} {jev_text:18} {hyb_text:22} "
              f"{rule} {mark(oks['rule'])}")
    total = len(jobs)
    print(f"\nGood picks out of {total} (passed, fast enough, open-source if one qualifies, within 25% or 0.005 EGP of the best cost): "
          f"Jev picks {score['jev']}, code + Jev check {score['hybrid']}, code only {score['rule']}.")
    cost = sum(r.cost_egp for r in recs) + sum(r.cost_egp for r in hybrid)
    print(f"Jev cost {cost:.4f} EGP for {2 * total} decisions "
          f"({decider.hits} answered from the cache, {decider.misses} asked fresh).")
    return 0


def _cached(client, config, fresh: bool):
    from .cache import Cache, CachedDecisions

    ttl = 0 if fresh else config.cache_decisions_hours * 3600
    return CachedDecisions(client, Cache(config.cache_path), ttl)


async def _cmd_cache(args: argparse.Namespace) -> int:
    from .cache import Cache

    removed = Cache(load_config(args.config).cache_path).clear()
    print(f"Cleared {removed} cached entries.")
    return 0


async def _cmd_show(args: argparse.Namespace) -> int:
    print(describe(Plan.load(args.plan)))
    return 0


async def _cmd_execute(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    if args.trials:
        config = replace(config, trials=args.trials)
    plan = Plan.load(args.plan)
    if not plan.reviewed:
        print("Note: this plan is not marked `reviewed: true`; check the expected answers before trusting results.",
              file=sys.stderr)

    if args.mock:
        config = mock_config(config)
        backend = MockBackend.for_plan(plan)
    else:
        backend = _client(config.timeout_s)
    try:
        run_dir = await execute_plan(config, plan, backend)
    except FatalRunError as exc:
        print(f"Run stopped: {exc}", file=sys.stderr)
        return 2
    finally:
        await backend.aclose()
    return _report(run_dir, config.min_success_rate)


async def _cmd_report(args: argparse.Namespace) -> int:
    return _report(Path(args.run_dir), args.min_success_rate)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="taskcost",
        description="TaskCost AI: compare AI models by the cost of successfully completed tasks.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    plan = sub.add_parser("plan", help="Design agent: break a project or complex task down into simple tasks")
    plan.add_argument("request", help='What you need done, e.g. "Extract the key data from this contract"')
    plan.add_argument("--doc", action="append", default=[], help="Source document (repeatable); name=path or path")
    plan.add_argument("--out", help="Where to save the plan (default: plans/<request>.yaml)")
    plan.add_argument("--config", default="benchmark.yaml")
    plan.add_argument("--design-model", help="Override design_model from the config")
    plan.set_defaults(handler=_cmd_plan)

    show = sub.add_parser("show", help="Print a plan for review")
    show.add_argument("plan")
    show.set_defaults(handler=_cmd_show)

    execute = sub.add_parser("execute", help="Run a plan: every model does every simple task, then cost the job")
    execute.add_argument("plan")
    execute.add_argument("--config", default="benchmark.yaml")
    execute.add_argument("--trials", type=int, help="Override trials")
    execute.add_argument("--mock", action="store_true", help="Use simulated offline models (no API calls)")
    execute.set_defaults(handler=_cmd_execute)

    profile = sub.add_parser("profile", help="Agent 1: ask Jev what kind of model a task needs")
    profile.add_argument("text", nargs="?", help="The task, as you would give it to a model")
    profile.add_argument("--library", help="Profile every task in this folder instead")
    profile.add_argument("--config", default="benchmark.yaml")
    profile.add_argument("--fresh", action="store_true", help="Ask Jev again instead of using cached answers")
    profile.set_defaults(handler=_cmd_profile)

    calibrate = sub.add_parser("calibrate", help="Measure how often Agent 1 picks the cheapest tier that passes")
    calibrate.add_argument("--library", default="library")
    calibrate.add_argument("--trials", type=int, default=3)
    calibrate.add_argument("--full", action="store_true", help="Run every tier on every task (costs more)")
    calibrate.add_argument("--only", help="Only tasks whose id contains this text")
    calibrate.add_argument("--config", default="benchmark.yaml")
    calibrate.set_defaults(handler=_cmd_calibrate)

    recommend = sub.add_parser("recommend", help="Agent 1: Jev recommends a model for a task and use case")
    recommend.add_argument("text", nargs="?", help="The task; omit to evaluate on the library")
    recommend.add_argument("--use-case", default="all", choices=["chat", "realtime", "batch", "all"])
    recommend.add_argument("--library", default="library")
    recommend.add_argument("--runs", nargs="*", help="Measured run folders for the registry (default: results/calibration*)")
    recommend.add_argument("--config", default="benchmark.yaml")
    recommend.add_argument("--fresh", action="store_true", help="Ask Jev again instead of using cached answers")
    recommend.set_defaults(handler=_cmd_recommend)

    cache_cmd = sub.add_parser("cache", help="Manage the cache of Jev answers and comparisons")
    cache_cmd.add_argument("action", choices=["clear"])
    cache_cmd.add_argument("--config", default="benchmark.yaml")
    cache_cmd.set_defaults(handler=_cmd_cache)

    ui = sub.add_parser("ui", help="Start the web interface")
    ui.add_argument("--config", default="benchmark.yaml")
    ui.add_argument("--port", type=int, default=8000)
    ui.set_defaults(handler=None)

    models = sub.add_parser("models", help="List SovereignEG models with EGP pricing")
    models.add_argument("--modality", help="Filter, e.g. chat or decision")
    models.set_defaults(handler=_cmd_models)

    validate = sub.add_parser("validate", help="Check tasks; with --live, calibrate the judge panel")
    validate.add_argument("--tasks", default="tasks", help="Task file or directory (offline check)")
    validate.add_argument(
        "--live", action="store_true",
        help="Run the judge panel on reference answers (must pass) and counterexamples (must fail)",
    )
    validate.add_argument("--config", default="benchmark.yaml", help="Config naming the judge panel")
    validate.set_defaults(handler=_cmd_validate)

    run = sub.add_parser("run", help="Run the standard task suite in tasks/")
    run.add_argument("--config", default="benchmark.yaml")
    run.add_argument("--mock", action="store_true", help="Use simulated offline models (no API calls)")
    run.add_argument("--trials", type=int, help="Override trials per task")
    run.add_argument("--only", help="Run only tasks whose id contains this text, or this category")
    run.set_defaults(handler=_cmd_run)

    report = sub.add_parser("report", help="Rebuild reports from a results directory")
    report.add_argument("run_dir")
    report.add_argument("--min-success-rate", type=float)
    report.set_defaults(handler=_cmd_report)

    args = parser.parse_args(argv)
    if args.command == "ui":
        return _serve_ui(args)
    return asyncio.run(args.handler(args))


def _serve_ui(args: argparse.Namespace) -> int:
    import uvicorn

    from .server import create_app

    config_path = Path(args.config).resolve()
    load_config(config_path)
    print(f"TaskCost AI is running at http://127.0.0.1:{args.port}  (Ctrl+C to stop)")
    uvicorn.run(create_app(Path.cwd(), config_path), host="127.0.0.1", port=args.port, log_level="warning")
    return 0


if __name__ == "__main__":
    sys.exit(main())
