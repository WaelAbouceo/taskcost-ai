"""TaskCost AI web UI: a small FastAPI server plus a single-page frontend in taskcost/web.

Start it with `taskcost ui`. Designing and executing plans take minutes, so they run as background
jobs that the page polls for progress.
"""

from __future__ import annotations

import asyncio
import hashlib
import itertools
import json
import re
import sys
import time
from contextlib import aclosing, asynccontextmanager
from dataclasses import dataclass, field, replace
from datetime import datetime
from pathlib import Path
from typing import Any, Awaitable, Callable

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .blueprint import (
    DEFAULT_USE_CASE, ai_steps, design_blueprint, estimate_blueprint, find_step, load_blueprint, recompute_totals,
    save_blueprint, size_request, verify_blueprint, write_blueprint,
)
from .cache import Cache, CachedDecisions
from .client import ModelInfo, SovereignClient, client_from_env
from .config import ModelSpec, load_api_key, load_config
from .design import design_plan
from .execute import execute_plan
from .mock import MockBackend, mock_config
from .licenses import is_egypt_hosted, is_open_weights
from .naming import slug
from .plan import Plan, read_document
from .quick import quick_compare, quick_summary
from .trace import blueprint_trace
from .hosting import load_snapshot, refresh_hosting, snapshot_age_hours
from .registry import USE_CASES, registry_from_results
from .reviewer import latest_review, list_reviews, review_age_hours, review_usage
from .user_profile import arabic_share, build_profile, valid_client, design_context, forget, stated_monthly_volume, use_case_prior
from .usage import backfill_from_results, blueprint_event, comparison_event, has_events, log_event
from .sovereignty import LEVELS, accepted, apply_policy, countries, country_name, model_fit, residency_ranks
from .report import finalize_run
from .runner import load_attempts

WEB_DIR = Path(__file__).parent / "web"
SAFE_NAME = re.compile(r"^[A-Za-z0-9._-]+$")
DOCUMENT_TYPES = {".txt", ".md", ".pdf"}
CATALOG_TTL_S = 3600
HOSTING_CHECK_S = 3600


@dataclass
class Job:
    """A background job. It doubles as the log stream the runner prints progress to."""

    id: str
    kind: str
    status: str = "running"
    lines: list[str] = field(default_factory=list)
    done: int = 0
    total: int = 0
    result: str | None = None
    error: str | None = None
    phase: str = ""
    started: float = field(default_factory=time.time)

    def write(self, text: str) -> None:
        for line in text.splitlines():
            if not line.strip():
                continue
            self.lines.append(line)
            match = re.match(r"\[(\d+)/(\d+)\]", line)
            if match:
                self.done, self.total = int(match[1]), int(match[2])
        del self.lines[:-300]

    def flush(self) -> None:
        pass

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id, "kind": self.kind, "status": self.status, "lines": self.lines[-40:],
            "done": self.done, "total": self.total, "result": self.result, "error": self.error, "phase": self.phase,
            "elapsed_s": round(time.time() - self.started),
        }


class TaskEdit(BaseModel):
    instruction: str
    expected_answer: str | None = None
    criteria: list[str]


class PlanEdit(BaseModel):
    reviewed: bool
    tasks: dict[str, TaskEdit]


class RunRequest(BaseModel):
    models: list[str]
    trials: int = 3
    mock: bool = False


def _criteria_from_lines(lines: list[str]) -> list[Any]:
    criteria: list[Any] = []
    for line in (line.strip() for line in lines):
        if not line:
            continue
        if line.lower().startswith("(optional)"):
            criteria.append({"text": line[len("(optional)"):].strip(), "required": False})
        else:
            criteria.append(line)
    return criteria


def create_app(root: Path, config_path: Path) -> FastAPI:
    @asynccontextmanager
    async def lifespan(_: FastAPI):
        if load_api_key():
            task = asyncio.create_task(hosting_loop())
            running.add(task)
            task.add_done_callback(running.discard)
        yield

    app = FastAPI(title="TaskCost AI", lifespan=lifespan)

    @app.middleware("http")
    async def revalidate_pages(request: Any, call_next: Any) -> Any:
        """Browsers must check the page and its files for updates on every visit (cheap, with ETags), so an
        old cached script never runs against a newer page."""
        response = await call_next(request)
        if request.url.path == "/" or request.url.path.startswith("/static/"):
            response.headers["Cache-Control"] = "no-cache"
        return response
    plans_dir = root / "plans"
    uploads_dir = root / "uploads"
    jobs: dict[str, Job] = {}
    running: set[asyncio.Task[None]] = set()
    ids = itertools.count(1)
    catalog_cache: dict[str, tuple[float, dict[str, ModelInfo]]] = {}
    cache = Cache(load_config(config_path).cache_path)

    def config():
        return load_config(config_path)

    def policy_config(helpers: str = "", country: str = "", sovereignty: str = ""):
        """The config for one request: the user's country and sovereignty level, with helper models and
        reviewers chosen to respect them (sovereignty.apply_policy)."""
        cfg = config()
        cfg = replace(cfg, country=(country or cfg.country).upper(), sovereignty=sovereignty or cfg.sovereignty)
        try:
            return apply_policy(cfg, helpers)
        except ValueError as exc:
            if cfg.sovereignty == "strict":
                log_event("strict_refused", country=cfg.country, reason=str(exc)[:200])
            raise HTTPException(400, str(exc)) from None

    def policy_key(cfg) -> dict[str, Any]:
        return {"helpers": cfg.helper_mode, "country": cfg.country, "sovereignty": cfg.sovereignty,
                "judges": list(cfg.judge_models)}

    def checked(name: str) -> str:
        if not SAFE_NAME.match(name):
            raise HTTPException(400, "invalid name")
        return name

    def plan_file(name: str) -> Path:
        path = plans_dir / f"{checked(name)}.yaml"
        if not path.is_file():
            raise HTTPException(404, f"plan {name} not found")
        return path

    def run_folder(name: str) -> Path:
        path = config().output_path / checked(name)
        if not (path / "config.json").is_file():
            raise HTTPException(404, f"result {name} not found")
        return path

    def open_client(timeout_s: float = 90.0) -> SovereignClient:
        client = client_from_env(timeout_s=timeout_s)
        if client is None:
            raise HTTPException(400, "LLM_API_KEY is not set.")
        return client

    def documents_key(documents: dict[str, Path]) -> list[str]:
        return sorted(hashlib.sha256(p.read_bytes()).hexdigest() for p in documents.values())

    async def fetch_catalog() -> dict[str, ModelInfo]:
        client = client_from_env()
        if client is None:
            return {}
        async with aclosing(client):
            models = await client.list_models()
        catalog_cache["models"] = (time.time(), models)
        return models

    async def catalog() -> dict[str, ModelInfo]:
        """SovereignEG's model list. Fetched once; after an hour it is refreshed in the background while the
        last copy keeps serving, so pages never wait on SovereignEG."""
        fetched_at, models = catalog_cache.get("models", (0.0, {}))
        if not models:
            return await fetch_catalog()
        if time.time() - fetched_at > CATALOG_TTL_S and "refreshing" not in catalog_cache:
            catalog_cache["refreshing"] = (time.time(), {})

            async def refresh() -> None:
                try:
                    await fetch_catalog()
                except Exception as exc:  # keep serving the last copy
                    print(f"Catalog refresh failed: {exc}")
                finally:
                    catalog_cache.pop("refreshing", None)

            task = asyncio.create_task(refresh())
            running.add(task)
            task.add_done_callback(running.discard)
        return models

    def start(kind: str, work: Callable[[Job], Awaitable[str]]) -> Job:
        job = Job(id=str(next(ids)), kind=kind)
        jobs[job.id] = job

        async def run() -> None:
            try:
                job.result = await work(job)
                job.status = "done"
            except Exception as exc:  # surfaced to the page, which shows the message
                job.status = "error"
                job.error = str(exc) or exc.__class__.__name__
                job.write(f"Error: {job.error}")
                if job.error.startswith("Strict mode"):
                    log_event("strict_refused", job=kind, reason=job.error[:200])
                else:
                    log_event("job_failed", job=kind, error=job.error[:200], seconds=round(time.time() - job.started))

        task = asyncio.create_task(run())
        running.add(task)
        task.add_done_callback(running.discard)
        return job

    async def collect_documents(samples: list[str], files: list[UploadFile]) -> dict[str, Path]:
        documents: dict[str, Path] = {}
        for sample in samples:
            path = (root / sample).resolve()
            if not path.is_file() or root.resolve() not in path.parents:
                raise HTTPException(400, f"unknown sample {sample}")
            documents[slug(path.stem)] = path
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        for upload in files:
            if not upload.filename:
                continue
            suffix = Path(upload.filename).suffix.lower()
            if suffix not in DOCUMENT_TYPES:
                raise HTTPException(400, f"{upload.filename}: use .txt, .md or .pdf")
            uploads_dir.mkdir(parents=True, exist_ok=True)
            path = uploads_dir / f"{stamp}-{slug(Path(upload.filename).stem)}{suffix}"
            path.write_bytes(await upload.read())
            documents[slug(Path(upload.filename).stem)] = path
        return documents

    @app.get("/")
    async def index() -> HTMLResponse:
        """The page, with each script and stylesheet address stamped with a hash of its contents, so a changed
        file always has a new address."""
        html = (WEB_DIR / "index.html").read_text(encoding="utf-8")

        def stamp(match: re.Match[str]) -> str:
            path = WEB_DIR / match.group(1)
            version = hashlib.sha256(path.read_bytes()).hexdigest()[:10] if path.is_file() else "0"
            return f"/static/{match.group(1)}?v={version}"

        return HTMLResponse(re.sub(r"/static/([\w.-]+\.(?:js|css))", stamp, html))

    @app.get("/api/config")
    async def get_config() -> dict[str, Any]:
        cfg = config()
        models = await catalog()
        samples = [
            str(p.relative_to(root)) for p in sorted((root / "examples").glob("**/*"))
            if p.suffix.lower() in DOCUMENT_TYPES
        ]
        return {
            "has_key": bool(load_api_key()),
            "design_model": cfg.design_model,
            "judges": list(cfg.judge_models),
            "trials": cfg.trials,
            "max_attempts": cfg.max_attempts,
            "models": [{"label": m.label, "id": m.id} for m in cfg.models],
            "tiers": [{"tier": tier, "model": model, "open_weights": is_open_weights(model),
                       "egypt_hosted": is_egypt_hosted(model)} for tier, model in cfg.tiers],
            "prefer_open_weights": cfg.prefer_open_weights,
            "country": cfg.country,
            "sovereignty": cfg.sovereignty,
            "levels": [{"id": k, "label": v} for k, v in LEVELS.items()],
            "countries": [{"code": code, "name": c["name"], "region": c["region"], "law": c["law"]}
                          for code, c in sorted(countries().items(), key=lambda item: item[1]["name"])],
            "hosting_updated_at": (load_snapshot() or {}).get("updated_at"),
            # How each default model fits every covered country, so the page can label them for the user's choice.
            "tier_fit": {model: {code: model_fit(model, code)["fit"] for code in countries()} for _, model in cfg.tiers},
            "tier_locations": {model: model_fit(model, cfg.country)["locations"] for _, model in cfg.tiers},
            "helper_mode": cfg.helper_mode,
            "helper_modes": [{
                "id": m.id, "label": m.label, "description": m.description, "models": m.models,
                "egypt_hosted": all(is_egypt_hosted(model) for model in m.models.values()),
            } for m in cfg.helper_modes],
            "use_cases": [{"id": key, "label": value["label"], "max_p90_s": value["max_p90_s"]}
                          for key, value in USE_CASES.items()],
            "cache_hours": cfg.cache_comparisons_hours,
            "quick_design_model": cfg.quick_design_model or cfg.design_model,
            "catalog": sorted(m.id for m in models.values() if m.modality == "chat"),
            "samples": samples,
        }

    @app.get("/api/plans")
    async def list_plans() -> list[dict[str, Any]]:
        items = []
        for path in sorted(plans_dir.glob("*.yaml"), key=lambda p: p.stat().st_mtime, reverse=True):
            try:
                plan = Plan.load(path)
            except Exception:
                continue
            items.append({
                "name": path.stem, "request": plan.request, "title": plan.root.title,
                "tasks": len(plan.simple_tasks()), "reviewed": plan.reviewed,
                "design_cost_egp": plan.design.get("cost_egp"),
            })
        return items

    @app.get("/api/plans/{name}")
    async def get_plan(name: str) -> dict[str, Any]:
        plan = Plan.load(plan_file(name))
        return {"name": name, **plan.to_dict()}

    @app.put("/api/plans/{name}")
    async def save_plan(name: str, edit: PlanEdit) -> dict[str, Any]:
        path = plan_file(name)
        plan = Plan.load(path)
        for task_id, change in edit.tasks.items():
            try:
                node = plan.node(task_id)
            except KeyError:
                raise HTTPException(400, f"unknown task {task_id}") from None
            node.instruction = change.instruction.strip()
            node.expected_answer = (change.expected_answer or "").strip() or None
            node.criteria = _criteria_from_lines(change.criteria)
        plan.reviewed = edit.reviewed
        try:
            plan.validate()
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        plan.save(path)
        return {"name": name, **plan.to_dict()}

    @app.post("/api/plans")
    async def create_plan(
        request: str = Form(...),
        samples: list[str] = Form(default=[]),
        files: list[UploadFile] = File(default=[]),
        helpers: str = Form(default=""),
        country: str = Form(default=""),
        sovereignty: str = Form(default=""),
    ) -> dict[str, Any]:
        cfg = policy_config(helpers, country, sovereignty)
        if not request.strip():
            raise HTTPException(400, "Describe the work you need done.")
        if not cfg.design_model:
            raise HTTPException(400, "Set design_model in benchmark.yaml.")
        if not load_api_key():
            raise HTTPException(400, "LLM_API_KEY is not set, so the design agent can't run.")

        documents = await collect_documents(samples, files)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        plans_dir.mkdir(parents=True, exist_ok=True)
        out = plans_dir / f"{slug(request)}-{stamp}.yaml"

        async def work(job: Job) -> str:
            models = await catalog()
            if cfg.design_model not in models:
                raise ValueError(f"design model {cfg.design_model} is not in the catalog")
            job.write(f"Design agent ({cfg.design_model}) is reading the documents and breaking the work into simple tasks...")
            async with aclosing(open_client(max(cfg.timeout_s, 600))) as client:
                plan = await design_plan(client, models[cfg.design_model], request.strip(), documents, plans_dir.resolve())
            plan.save(out)
            job.write(f"Done: {len(plan.simple_tasks())} simple tasks, design cost {plan.design['cost_egp']:.4f} EGP.")
            return out.stem

        return start("design", work).to_dict()

    @app.post("/api/plans/{name}/run")
    async def run_plan(name: str, body: RunRequest) -> dict[str, Any]:
        cfg = replace(config(), trials=max(1, min(body.trials, 10)))
        plan = Plan.load(plan_file(name))
        if body.mock:
            cfg = mock_config(cfg)
            backend: Any = MockBackend.for_plan(plan)
        else:
            if not body.models:
                raise HTTPException(400, "Choose at least one model.")
            by_label = {m.label: m for m in cfg.models}
            specs = [by_label.get(m) or ModelSpec.parse(m, cfg.default_temperature) for m in dict.fromkeys(body.models)]
            cfg = replace(cfg, models=tuple(specs))
            backend = open_client()

        async def work(job: Job) -> str:
            async with aclosing(backend):
                run_dir = await execute_plan(cfg, plan, backend, log=job)
            finalize_run(run_dir)
            job.write("Finished. Report ready.")
            return run_dir.name

        return start("execute", work).to_dict()

    @app.post("/api/quick")
    async def start_quick(
        task: str = Form(...),
        tiers: str = Form(default="[]"),
        trials: int = Form(default=1),
        use_case: str = Form(default="chat"),
        force: bool = Form(default=False),
        rerun_of: str = Form(default=""),
        samples: list[str] = Form(default=[]),
        files: list[UploadFile] = File(default=[]),
        helpers: str = Form(default=""),
        country: str = Form(default=""),
        sovereignty: str = Form(default=""),
        client: str = Form(default=""),
    ) -> dict[str, Any]:
        cfg = policy_config(helpers, country, sovereignty)
        client = valid_client(client)
        chosen = parse_request(task, use_case, tiers)
        documents = await collect_documents(samples, files)
        personalization = None
        if rerun_of:
            earlier = json.loads((run_folder(rerun_of) / "config.json").read_text(encoding="utf-8"))
            personalization = earlier.get("personalization")
            if earlier.get("use_case") != use_case:
                log_event("use_case_changed", client=client, run=rerun_of, guessed=bool(earlier.get("use_case_guess")),
                          from_use_case=earlier.get("use_case"), to_use_case=use_case)
        result = launch_quick(cfg, task, chosen, trials, use_case, documents, force, rerun_of,
                              personalization=personalization, client=client)
        log_event("request", client=client, via="rerun" if rerun_of else "quick", flow="simple",
                  cached=result.get("status") == "cached", use_case=use_case, country=cfg.country, sovereignty=cfg.sovereignty)
        return result

    @app.post("/api/start")
    async def start_anything(
        task: str = Form(...),
        tiers: str = Form(default="[]"),
        trials: int = Form(default=1),
        use_case: str = Form(default="auto"),
        files: list[UploadFile] = File(default=[]),
        helpers: str = Form(default=""),
        country: str = Form(default=""),
        sovereignty: str = Form(default=""),
        client: str = Form(default=""),
    ) -> dict[str, Any]:
        """The Compare button: Agent 1 sizes the request and, unless the user chose one, decides the use case;
        then it goes to the simple or the blueprint flow (where each AI step has its own use case).
        What was learned about this user (user_profile.py) tailors the test and an unclear use case."""
        cfg = policy_config(helpers, country, sovereignty)
        client = valid_client(client)
        auto = use_case in ("", "auto")
        chosen = parse_request(task, DEFAULT_USE_CASE if auto else use_case, tiers)
        documents = await collect_documents([], files)
        models = await catalog()
        jev = models.get(cfg.profiler_model or "")
        async with aclosing(open_client()) as sizer:
            try:
                decider = CachedDecisions(sizer, cache, cfg.cache_decisions_hours * 3600)
                size = await size_request(decider, jev, task) if jev else {"is_simple": True, "level": "simple"}
            except Exception:
                size = {"is_simple": True, "level": "simple"}
        profile = build_profile(client) if client else {}
        guess, use_case_note = None, None
        if auto:
            guess = size.get("use_case_probabilities") or {}
            use_case, use_case_note = use_case_prior(profile, size.get("use_case", DEFAULT_USE_CASE), guess)
        context = design_context(profile)
        personalization = {"context": context, "use_case_note": use_case_note,
                           "profile": {k: profile.get(k) for k in ("domain", "language", "usual_use_case", "monthly_volume")}
                           } if context or use_case_note else None
        request_facts = {"client": client, "via": "start", "level": size.get("level"), "use_case": use_case,
                         "use_case_auto": auto, "country": cfg.country, "sovereignty": cfg.sovereignty,
                         "documents": len(documents), "request_words": len(task.split()), "domain": size.get("domain"),
                         "domain_scores": size.get("domain_scores"),
                         "arabic_share": round(arabic_share(task), 2), "monthly_volume": stated_monthly_volume(task),
                         "tailored": bool(personalization)}
        if size["is_simple"]:
            result = launch_quick(cfg, task, chosen, trials, use_case, documents, False, "", guess,
                                  personalization=personalization, client=client, size=size)
            log_event("request", flow="simple", cached=result.get("status") == "cached", **request_facts)
            return {"kind": "simple", "size": size, **result}

        cache_parts = {"task": " ".join(task.split()).lower(), "documents": documents_key(documents), **policy_key(cfg),
                       "context": context}
        hit = cache.get("blueprint", cache_parts, cfg.cache_comparisons_hours * 3600)
        if hit and (cfg.output_path / hit[0] / "blueprint.json").is_file():
            log_event("request", flow="blueprint", cached=True, **request_facts)
            return {"kind": "blueprint", "size": size, "status": "cached", "result": hit[0]}
        log_event("request", flow="blueprint", cached=False, **request_facts)

        designer_id = cfg.blueprint_model or cfg.design_model
        if not designer_id or designer_id not in models:
            raise HTTPException(400, "Set blueprint_model in benchmark.yaml to a model in the catalog.")
        backend = open_client(max(cfg.timeout_s, 600))
        decider = CachedDecisions(backend, cache, cfg.cache_decisions_hours * 3600)

        async def work(job: Job) -> str:
            async with aclosing(backend):
                job.phase = "design"
                job.write(f"This looks like a {size['level']}; mapping it to standard enterprise processes...")
                texts = {name: read_document(path) for name, path in documents.items()}
                blueprint = await design_blueprint(decider, models[designer_id], task.strip(), texts, context=context)
                blueprint["size"] = size
                blueprint["personalization"] = personalization
                blueprint["helper_mode"] = cfg.helper_mode
                blueprint["sovereignty"] = {"country": cfg.country, "level": cfg.sovereignty,
                                            "notes": list(cfg.sovereignty_notes)}
                job.phase = "estimate"
                job.write("Choosing a model for each AI step and estimating the monthly cost...")
                cards, _ = registry_from_results(cfg.output_path)
                cards = {m: c for m, c in cards.items() if accepted(cfg, m)}
                blueprint = await estimate_blueprint(blueprint, decider, jev, cards, cfg.escalate_at,
                                                     cfg.prefer_open_weights, cfg.min_model_pass_rate,
                                                     residency_ranks(cfg, cards))
            name = save_blueprint(blueprint, cfg.output_path)
            cache.put("blueprint", cache_parts, name)
            log_event("blueprint_done", client=client, **blueprint_event(blueprint, name, time.time() - job.started))
            job.phase = "done"
            return name

        return {"kind": "blueprint", "size": size, **start("blueprint", work).to_dict()}

    def blueprint_folder(name: str) -> Path:
        folder = config().output_path / checked(name)
        if not (folder / "blueprint.json").is_file():
            raise HTTPException(404, f"blueprint {name} not found")
        return folder

    @app.get("/api/blueprints/{name}")
    async def get_blueprint(name: str) -> dict[str, Any]:
        blueprint = load_blueprint(blueprint_folder(name))
        models = {m for s in ai_steps(blueprint)
                  for m in [s.get("model"), (s.get("measured") or {}).get("model"), *[t["model"] for t in s.get("tried", [])]] if m}
        models.add(blueprint.get("strongest_model") or "")
        mode = next((m for m in config().helper_modes if m.id == blueprint.get("helper_mode")), None)
        policy = blueprint.get("sovereignty") or {"country": config().country, "level": config().sovereignty, "notes": []}
        residency = {m: model_fit(m, policy["country"]) for m in models if m}
        return {"name": name, **blueprint, "open_weights": {m: is_open_weights(m) for m in models if m},
                "egypt_hosted": {m: is_egypt_hosted(m) for m in models if m}, "residency": residency,
                "sovereignty": {**policy, "country_name": country_name(policy["country"]),
                                "level_label": LEVELS.get(policy["level"], policy["level"])},
                "helpers": mode and {"label": mode.label, "models": sorted(set(mode.models.values())),
                                     "local": all(model_fit(m, policy["country"])["rank"] <= 1 for m in mode.models.values())},
                "trace": blueprint_trace(blueprint, mode and {k: v for k, v in mode.models.items()
                                                             if k in ("blueprint_model", "profiler_model")},
                                         list(config().judge_models))}

    @app.post("/api/blueprints/{name}/verify")
    async def verify(name: str, steps: str = "", helpers: str = "") -> dict[str, Any]:
        """Measure AI steps on a realistic example, cheapest model first (all steps, or the comma-separated ids).
        Uses the country, level and helpers the blueprint was made with."""
        only = {s for s in steps.split(",") if s} or None
        folder = blueprint_folder(name)
        blueprint = load_blueprint(folder)
        policy = blueprint.get("sovereignty") or {}
        cfg = policy_config(helpers or blueprint.get("helper_mode") or "", policy.get("country", ""), policy.get("level", ""))
        models = await catalog()
        needed = [cfg.sample_model or cfg.blueprint_model or cfg.design_model, *cfg.judge_models]
        if any(m not in models for m in needed):
            raise HTTPException(400, f"These models must be in the catalog: {needed}")
        cards, _ = registry_from_results(cfg.output_path)
        if not cards:
            raise HTTPException(400, "No measured model registry yet; run `taskcost calibrate` first.")
        backend = open_client(max(cfg.timeout_s, 300))

        async def work(job: Job) -> str:
            job.phase = "verify"
            job.total = len(only) if only else len(ai_steps(blueprint))
            job.write("Writing a realistic example for each AI step and testing the cheapest models first...")
            async with aclosing(backend):
                measured = await verify_blueprint(cfg, blueprint, backend, models, cards, folder / "verification",
                                                  log=job, only_steps=only)
            write_blueprint(folder, measured)
            v = measured.get("verification") or {}
            log_event("measured", run=name, steps=len(only) if only else v.get("ai_steps"),
                      measured_steps=v.get("measured_steps"), own_example=bool(only and len(only) == 1 and any(
                          (find_step(measured, s) or {}).get("sample", {}).get("source") == "user" for s in only)),
                      seconds=round(time.time() - job.started, 1))
            job.phase = "done"
            return name

        return start("verify", work).to_dict()

    @app.put("/api/blueprints/{name}/volumes")
    async def save_volumes(name: str, volumes: dict[str, int], client: str = "") -> dict[str, Any]:
        folder = blueprint_folder(name)
        blueprint = load_blueprint(folder)
        for step in ai_steps(blueprint):
            if step["id"] in volumes:
                step["total_volume" if step.get("phase") == "build" else "monthly_volume"] = max(0, int(volumes[step["id"]]))
        log_event("volumes_saved", client=valid_client(client), run=name, volumes=[max(0, int(v)) for v in volumes.values()])
        write_blueprint(folder, recompute_totals(blueprint))
        return {"name": name, **blueprint}

    @app.post("/api/blueprints/{name}/steps/{step_id}/example")
    async def own_example(
        name: str, step_id: str, text: str = Form(default=""), files: list[UploadFile] = File(default=[]),
        helpers: str = Form(default=""),
    ) -> dict[str, Any]:
        """Measure one step on the user's own example (pasted text or an uploaded document)."""
        folder = blueprint_folder(name)
        blueprint = load_blueprint(folder)
        step = find_step(blueprint, step_id, ai_only=True)
        if step is None:
            raise HTTPException(404, f"AI step {step_id} not found")
        content = text.strip()
        if not content:
            documents = await collect_documents([], files)
            content = "\n\n".join(read_document(path) for path in documents.values()).strip()
        if not content:
            raise HTTPException(400, "Paste an example or upload a .txt, .md or .pdf file.")
        step["user_input"] = content[:20_000]
        write_blueprint(folder, blueprint)
        log_event("own_example", run=name, step=step_id, task_type=step.get("task_type"), chars=len(content))
        return await verify(name, steps=step_id, helpers=helpers)

    @app.get("/api/blueprints/{name}/steps/{step_id}")
    async def get_step(name: str, step_id: str) -> dict[str, Any]:
        folder = blueprint_folder(name)
        blueprint = load_blueprint(folder)
        step = find_step(blueprint, step_id)
        if step is None:
            raise HTTPException(404, f"step {step_id} not found")
        attempts = []
        if (folder / "verification" / "attempts.jsonl").is_file():
            attempts = [r for r in load_attempts(folder / "verification") if r["task_id"] == step_id]
        return {"step": step, "attempts": attempts}

    def parse_request(task: str, use_case: str, tiers: str) -> list[tuple[str, str]]:
        if not task.strip():
            raise HTTPException(400, "Describe the task you want to compare.")
        if use_case not in USE_CASES:
            raise HTTPException(400, f"use_case must be one of {list(USE_CASES)}")
        try:
            chosen = [(str(t["tier"]).strip(), str(t["model"]).strip()) for t in json.loads(tiers)]
        except (ValueError, KeyError, TypeError):
            raise HTTPException(400, "invalid tiers") from None
        chosen = [(tier, model) for tier, model in chosen if tier and model]
        if not chosen:
            raise HTTPException(400, "Choose at least one model.")
        return chosen

    def launch_quick(cfg, task: str, chosen, trials: int, use_case: str, documents, force: bool, rerun_of: str,
                     use_case_guess: dict[str, float] | None = None, personalization: dict[str, Any] | None = None,
                     client: str = "", size: dict[str, Any] | None = None) -> dict[str, Any]:
        cache_parts = {
            "context": (personalization or {}).get("context", ""),
            "task": " ".join(task.split()).lower(),
            "tiers": chosen,
            "trials": trials,
            "use_case": use_case,
            "documents": documents_key(documents),
            **policy_key(cfg),
        }
        if not force:
            hit = cache.get("comparison", cache_parts, cfg.cache_comparisons_hours * 3600)
            if hit and (cfg.output_path / hit[0] / "config.json").is_file():
                return {"status": "cached", "result": hit[0], "age_s": round(hit[1])}

        earlier_plan = Plan.load(run_folder(rerun_of) / "plan.yaml") if rerun_of else None
        backend = open_client(max(cfg.timeout_s, 180))

        async def work(job: Job) -> str:
            def progress(phase: str) -> None:
                job.phase = phase

            async with aclosing(backend):
                run_dir = await quick_compare(
                    cfg, task.strip(), chosen, backend, plans_dir,
                    documents=documents, trials=trials, log=job, progress=progress,
                    use_case=use_case, plan=earlier_plan, cache=cache, use_case_guess=use_case_guess,
                    personalization=personalization, size=size,
                )
            cache.put("comparison", cache_parts, run_dir.name)
            log_event("comparison_done", client=client,
                      **comparison_event(quick_summary(run_dir), time.time() - job.started, bool(rerun_of)))
            job.phase = "done"
            return run_dir.name

        return start("quick", work).to_dict()

    @app.get("/api/quick/{name}")
    async def get_quick(name: str) -> dict[str, Any]:
        folder = run_folder(name)
        if not json.loads((folder / "config.json").read_text(encoding="utf-8")).get("quick"):
            raise HTTPException(404, "not a quick comparison")
        return quick_summary(folder)

    @app.get("/api/jobs/{job_id}")
    async def get_job(job_id: str) -> dict[str, Any]:
        if job_id not in jobs:
            raise HTTPException(404, "job not found")
        return jobs[job_id].to_dict()

    @app.get("/api/results")
    async def list_results() -> list[dict[str, Any]]:
        out_dir = config().output_path
        found = [(p, "run") for p in out_dir.glob("*/summary.json")] + [(p, "blueprint") for p in out_dir.glob("*/blueprint.json")]
        items = []
        for path, kind in found:
            if kind == "blueprint":
                blueprint = json.loads(path.read_text(encoding="utf-8"))
                items.append({
                    "name": path.parent.name, "kind": "blueprint", "quick": False, "test_mode": False,
                    "title": blueprint.get("title"), "request": blueprint.get("request"),
                    "started_at": (blueprint.get("design") or {}).get("created_at"), "models": [],
                })
                continue
            run_config = json.loads((path.parent / "config.json").read_text(encoding="utf-8"))
            items.append({
                "name": path.parent.name,
                "kind": run_config.get("kind", "suite"),
                "test_mode": bool(run_config.get("test_mode")),
                "quick": bool(run_config.get("quick")),
                "title": run_config.get("name"),
                "request": run_config.get("request"),
                "started_at": run_config.get("started_at"),
                "models": [m["label"] for m in run_config.get("models", [])],
            })
        # Newest first by start time: saving edits to a blueprint must not move it to the top.
        return sorted(items, key=lambda item: item["started_at"] or "", reverse=True)

    @app.get("/api/results/{name}")
    async def get_result(name: str) -> dict[str, Any]:
        folder = run_folder(name)
        summary = json.loads((folder / "summary.json").read_text(encoding="utf-8"))
        report = folder / "report.md"
        return {"name": name, **summary, "report_md": report.read_text(encoding="utf-8") if report.is_file() else ""}

    @app.get("/api/results/{name}/attempts")
    async def get_attempts(name: str, task_id: str | None = None, model: str | None = None) -> list[dict[str, Any]]:
        records = load_attempts(run_folder(name))
        return [
            r for r in records
            if (task_id is None or r["task_id"] == task_id) and (model is None or r["model"] == model)
        ]

    @app.get("/api/hosting")
    async def get_hosting() -> dict[str, Any]:
        """The hosting agent's latest snapshot: providers with their verified locations and quotes, and changes."""
        snapshot = load_snapshot()
        if not snapshot:
            return {"updated_at": None, "providers": {}, "changes": [], "model_count": 0}
        models = snapshot["models"].values()
        return {
            "updated_at": snapshot["updated_at"], "reader_model": snapshot.get("reader_model"),
            "providers": snapshot["providers"], "changes": snapshot.get("changes", []),
            "model_count": len(snapshot["models"]),
            "egypt_hosted": sorted(m for m, r in snapshot["models"].items() if r["egypt_hosted"]),
            "verified_models": sum(1 for r in models if r["verified"]), "cost_egp": snapshot.get("cost_egp"),
        }

    @app.get("/api/profile")
    async def get_profile(client: str) -> dict[str, Any]:
        """What the tool has learned about this browser's user, from their own requests and choices."""
        client = valid_client(client)
        return build_profile(client) if client else {"requests": 0}

    @app.delete("/api/profile")
    async def delete_profile(client: str) -> dict[str, Any]:
        """Forget this user: delete their events from the usage log."""
        client = valid_client(client)
        return {"removed": forget(client) if client else 0}

    @app.get("/api/insights")
    async def get_insights() -> dict[str, Any]:
        """The log reviewer's latest review, and the dates of earlier ones."""
        return {"latest": latest_review(),
                "history": [{"name": p.stem, "created_at": json.loads(p.read_text(encoding="utf-8")).get("created_at"),
                             "recommendations": len(json.loads(p.read_text(encoding="utf-8")).get("recommendations", []))}
                            for p in list_reviews()[:10]]}

    @app.post("/api/insights/run")
    async def run_insights() -> dict[str, Any]:
        if not load_api_key():
            raise HTTPException(400, "LLM_API_KEY is not set.")
        return start("review", run_review_agent).to_dict()

    async def run_review_agent(log: Any = None) -> str:
        cfg = config()
        if not has_events():
            backfill_from_results(cfg.output_path)
        models = await catalog()
        reviewer_id = cfg.review_model or cfg.hosting_model or cfg.profiler_model
        if reviewer_id not in models:
            raise ValueError(f"review model {reviewer_id} is not in the catalog")
        async with aclosing(open_client(180)) as client:
            review = await review_usage(client, models[reviewer_id], cfg.review_days, log=log or sys.stderr)
        return review["created_at"]

    async def run_hosting_agent() -> None:
        cfg = config()
        reader = cfg.hosting_model or cfg.profiler_model
        client = client_from_env(timeout_s=180)
        if client is None or not reader:
            return
        async with aclosing(client):
            await refresh_hosting(client, reader)

    async def hosting_loop() -> None:
        """The daily agents, while the server is up: the hosting agent (hosting_refresh_hours) and the log
        reviewer (review_refresh_hours). Each keeps its last result if a run fails."""
        while True:
            cfg = config()
            age = snapshot_age_hours()
            if age is None or age >= cfg.hosting_refresh_hours:
                try:
                    await run_hosting_agent()
                except Exception as exc:
                    print(f"Hosting agent failed: {exc}")
            age = review_age_hours()
            if age is None or age >= cfg.review_refresh_hours:
                try:
                    await run_review_agent()
                except Exception as exc:
                    print(f"Log reviewer failed: {exc}")
            await asyncio.sleep(HOSTING_CHECK_S)

    app.mount("/static", StaticFiles(directory=WEB_DIR), name="static")
    return app
