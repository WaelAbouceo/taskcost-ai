import json
import shutil

import pytest
import sys
import time
from pathlib import Path

import yaml
from fastapi.testclient import TestClient

import taskcost.server as server
from taskcost.plan import Plan

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).parent))


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "client_from_env", lambda *a, **k: None)
    monkeypatch.setattr(server, "load_api_key", lambda *a, **k: None)
    shutil.copytree(ROOT / "examples", tmp_path / "examples")
    # The fake backends below know the global helper models, whatever the real default is.
    config = yaml.safe_load((ROOT / "benchmark.yaml").read_text(encoding="utf-8"))
    config.update(helper_mode="global", sovereignty="open")
    (tmp_path / "benchmark.yaml").write_text(yaml.safe_dump(config), encoding="utf-8")
    plan = Plan.load(ROOT / "tests" / "fixtures" / "contract_plan.yaml")
    plan.documents = {"contract": str(tmp_path / "examples/contracts/msa-delta-nilecloud.txt")}
    plan.base_dir = tmp_path
    plan.save(tmp_path / "plans" / "contract.yaml")
    with TestClient(server.create_app(tmp_path, tmp_path / "benchmark.yaml")) as test_client:
        yield test_client


def wait_for(client, job):
    for _ in range(200):
        state = client.get(f"/api/jobs/{job['id']}").json()
        if state["status"] != "running":
            return state
        time.sleep(0.05)
    raise AssertionError("job did not finish")


def test_page_and_config(client):
    assert "TaskCost AI" in client.get("/").text
    config = client.get("/api/config").json()
    assert config["has_key"] is False and config["judges"]
    assert any("msa-delta-nilecloud" in s for s in config["samples"])
    modes = {m["id"]: m for m in config["helper_modes"]}
    assert config["helper_mode"] == "global" and set(modes) == {"egypt", "global"}
    assert modes["egypt"]["models"]["profiler_model"] == "SovereignEG/Qwen3.8-27B-FP8"


def test_helper_modes_switch_every_helper_model():
    from taskcost.config import HELPER_FIELDS, load_config

    cfg = load_config(ROOT / "benchmark.yaml")
    egypt, world = cfg.with_helper_mode("egypt"), cfg.with_helper_mode("global")
    assert {getattr(egypt, f) for f in HELPER_FIELDS} == {"SovereignEG/Qwen3.8-27B-FP8"}
    assert world.profiler_model == "jev-1.13" and world.blueprint_model == "claude-sonnet-5.5"
    assert world.to_dict()["helper_mode"] == "global" and world.tiers == egypt.tiers  # the compared models stay
    with pytest.raises(ValueError):
        cfg.with_helper_mode("moon")


def test_unknown_helper_mode_is_rejected(client):
    response = client.post("/api/start", data={"task": "x", "tiers": '[{"tier": "Fast", "model": "small"}]',
                                               "helpers": "moon"})
    assert response.status_code == 400 and "unknown helper mode" in response.json()["detail"]


def test_edit_plan(client):
    plan = client.get("/api/plans/contract").json()
    assert plan["root"]["children"][0]["id"] == "T1"
    edit = {
        "reviewed": False,
        "tasks": {"T1": {"instruction": "Name the parties.", "expected_answer": "A and B",
                          "criteria": ["Names A", "(optional) Names B", ""]}},
    }
    saved = client.put("/api/plans/contract", json=edit).json()
    t1 = saved["root"]["children"][0]
    assert t1["instruction"] == "Name the parties."
    assert t1["criteria"] == ["Names A", {"text": "Names B", "required": False}]
    assert client.get("/api/plans").json()[0]["reviewed"] is False


def test_rejects_unsafe_names(client):
    assert client.get("/api/plans/..%2F..%2Fsecret").status_code in (400, 404)


def test_design_needs_a_key(client):
    response = client.post("/api/plans", data={"request": "Extract the data"})
    assert response.status_code == 400 and "LLM_API_KEY" in response.json()["detail"]


def test_quick_compare_end_to_end(client, monkeypatch):
    from test_quick import FakeBackend

    monkeypatch.setattr(server, "client_from_env", lambda *a, **k: FakeBackend())
    tiers = json.dumps([{"tier": "Fast", "model": "cheap-good"}, {"tier": "Frontier", "model": "pricey-good"}])
    job = client.post("/api/quick", data={"task": "write an email", "tiers": tiers, "trials": "1"}).json()
    state = wait_for(client, job)
    assert state["status"] == "done", state
    q = client.get(f"/api/quick/{state['result']}").json()
    assert q["recommended"]["tier"] == "Fast" and len(q["tiers"]) == 2
    assert q["use_case"] == "chat" and q["advice"] is None  # no measured registry in this test
    assert client.get("/api/results").json()[0]["quick"] is True

    form = {"task": "write an email", "tiers": tiers, "trials": "1"}
    again = client.post("/api/quick", data=form).json()
    assert again["status"] == "cached" and again["result"] == state["result"]

    designs = []
    original_chat = FakeBackend.chat

    async def counting_chat(self, model, messages, *args, **kwargs):
        if messages[0]["content"].startswith("You turn a user's request"):
            designs.append(model.id)
        return await original_chat(self, model, messages, *args, **kwargs)

    monkeypatch.setattr(FakeBackend, "chat", counting_chat)
    rerun = client.post("/api/quick", data={**form, "force": "true", "rerun_of": state["result"]}).json()
    rerun_state = wait_for(client, rerun)
    assert rerun_state["status"] == "done" and rerun_state["result"] != state["result"]
    assert designs == []  # the earlier concrete task was reused
    rerun_q = client.get(f"/api/quick/{rerun_state['result']}").json()
    assert rerun_q["instruction"] == q["instruction"]


def test_start_sends_a_project_to_the_blueprint_flow(client, monkeypatch, tmp_path):
    import taskcost.server as srv
    from test_blueprint import CARDS, FakeBackend as BlueprintBackend
    from taskcost.client import ModelInfo, Pricing

    monkeypatch.setattr(srv, "client_from_env", lambda *a, **k: BlueprintBackend("project"))
    monkeypatch.setattr(srv, "registry_from_results", lambda *a, **k: (CARDS, {}))
    models = {"jev-1.13": ModelInfo("jev-1.13", "decision", Pricing(2.47, 0.0)),
              "claude-sonnet-5.5": ModelInfo("claude-sonnet-5.5", "chat", Pricing(117.0, 588.0))}

    async def fake_list_models(self):
        return models

    monkeypatch.setattr(BlueprintBackend, "list_models", fake_list_models)
    tiers = json.dumps([{"tier": "Fast", "model": "small"}])
    job = client.post("/api/start", data={"task": "an ERP system", "tiers": tiers}).json()
    assert job["kind"] == "blueprint"
    state = wait_for(client, job)
    assert state["status"] == "done", state
    blueprint = client.get(f"/api/blueprints/{state['result']}").json()
    assert blueprint["monthly_cost_egp"] > 0 and len(blueprint["workflows"]) == 2
    history = client.get("/api/results").json()
    assert history[0]["kind"] == "blueprint"

    again = client.post("/api/start", data={"task": "an ERP system", "tiers": tiers}).json()
    assert again["kind"] == "blueprint" and again["status"] == "cached" and again["result"] == state["result"]


def test_blueprint_volumes_and_own_example(client, monkeypatch):
    import taskcost.server as srv
    from test_blueprint import CARDS, FakeVerifyBackend
    from taskcost.client import ModelInfo, Pricing

    decision = lambda m: ModelInfo(m, "decision", Pricing(2.0, 0.0))
    models = {
        "jev-1.13": decision("jev-1.13"), "gpt-6-luna-decisions": decision("gpt-6-luna-decisions"),
        "claude-haiku-5.5": ModelInfo("claude-haiku-5.5", "chat", Pricing(5.0, 25.0)),
        "claude-sonnet-5.5": ModelInfo("claude-sonnet-5.5", "chat", Pricing(117.0, 588.0)),
        "deepseek-v4-pro": ModelInfo("deepseek-v4-pro", "chat", Pricing(76.0, 153.0)),
        **{c.id: ModelInfo(c.id, "chat", Pricing(1.0, 4.0)) for c in CARDS.values()},
    }

    class Backend(FakeVerifyBackend):
        async def list_models(self):
            return models

        async def chat(self, model, messages, max_tokens, temperature=None, extra=None):
            from taskcost.blueprint import BLUEPRINT_SYSTEM
            from test_blueprint import DESIGN
            from taskcost.client import Completion
            if messages[0]["content"] == BLUEPRINT_SYSTEM:
                return Completion(json.dumps(DESIGN), 3000, 2000, 0, 5.0)
            return await super().chat(model, messages, max_tokens, temperature, extra)

    monkeypatch.setattr(srv, "client_from_env", lambda *a, **k: Backend())
    monkeypatch.setattr(srv, "registry_from_results", lambda *a, **k: ({k: CARDS[k] for k in ("small", "mini")}, {}))
    tiers = json.dumps([{"tier": "Fast", "model": "small"}])
    state = wait_for(client, client.post("/api/start", data={"task": "an ERP system", "tiers": tiers}).json())
    name = state["result"]
    blueprint = client.get(f"/api/blueprints/{name}").json()
    invoice = next(s for w in blueprint["workflows"] for s in w["steps"] if s["name"] == "Read supplier invoice")

    saved = client.put(f"/api/blueprints/{name}/volumes", json={invoice["id"]: 2400}).json()
    step = next(s for w in saved["workflows"] for s in w["steps"] if s["id"] == invoice["id"])
    assert step["monthly_volume"] == 2400
    assert step["monthly_cost_egp"] == pytest.approx(invoice["monthly_cost_egp"] * 2)

    job = client.post(f"/api/blueprints/{name}/steps/{invoice['id']}/example", data={"text": "MY INVOICE 123 total 9,000"}).json()
    assert wait_for(client, job)["status"] == "done"
    detail = client.get(f"/api/blueprints/{name}/steps/{invoice['id']}").json()
    assert detail["step"]["sample"]["input"] == "MY INVOICE 123 total 9,000"
    assert detail["step"]["sample"]["source"] == "user"
    assert detail["step"]["measured"]["model"] == "small" and detail["attempts"]


def test_mock_run_then_results(client):
    job = client.post("/api/plans/contract/run", json={"models": [], "trials": 2, "mock": True}).json()
    state = wait_for(client, job)
    assert state["status"] == "done", state
    run_name = state["result"]

    runs = client.get("/api/results").json()
    assert runs[0]["name"] == run_name and runs[0]["kind"] == "plan" and runs[0]["test_mode"] is True
    detail = client.get(f"/api/results/{run_name}").json()
    assert set(detail["economics"]["jobs"]) == {"mock-frontier", "mock-balanced", "mock-budget"}
    assert "What the whole job costs" in detail["report_md"] and "Test mode" in detail["report_md"]

    attempts = client.get(f"/api/results/{run_name}/attempts", params={"task_id": "T1", "model": "mock-frontier"}).json()
    assert attempts and all(a["task_id"] == "T1" and a["model"] == "mock-frontier" for a in attempts)


def test_page_files_are_versioned_and_revalidated(client):
    page = client.get("/")
    assert page.headers["cache-control"] == "no-cache"
    assert "/static/app.js?v=" in page.text and "/static/style.css?v=" in page.text
    assert client.get("/static/app.js").headers["cache-control"] == "no-cache"
