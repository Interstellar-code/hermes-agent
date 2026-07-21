"""
test_api_lifecycle.py — FastAPI TestClient lifecycle tests for plugin_api.router.

Uses a tmp git repo as fake profile_root with a committed SOUL.md and a tmp DB.

Tests:
- POST /experiments → approve → apply (file changed + commit added, apply_commit_sha stored)
  → verify (baseline row created)
- Revert path: POST /experiments → approve → apply → revert
- Scenario CRUD: holdout hidden from default GET, shown with ?include_holdout=1
- GET /experiments/{id}/history returns transitions + eval runs
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# Path injection so plugin_api can import _db etc. without __init__.py
# ---------------------------------------------------------------------------

_PLUGIN_DIR = Path(__file__).resolve().parent.parent
if str(_PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_DIR))


# ---------------------------------------------------------------------------
# Git helpers
# ---------------------------------------------------------------------------

def _git(args, cwd):
    return subprocess.run(
        ["git"] + args, cwd=str(cwd), capture_output=True, text=True, check=True
    )


@pytest.fixture()
def git_repo(tmp_path, patch_profiles_root):
    """Minimal git repo with a committed SOUL.md, placed under patched profiles root.

    The directory name is 'test-profile' so it matches the profile name used in
    lifecycle tests when creating experiments via POST /experiments.
    """
    repo = patch_profiles_root / "test-profile"
    repo.mkdir()
    _git(["init", "-b", "main"], repo)
    _git(["config", "user.email", "test@example.com"], repo)
    _git(["config", "user.name", "Test"], repo)
    soul = repo / "SOUL.md"
    soul.write_text("You are a helpful assistant. Always be concise.\n")
    _git(["add", "."], repo)
    _git(["commit", "-m", "initial commit"], repo)
    return repo


# ---------------------------------------------------------------------------
# TestClient fixture (isolated DB per test)
# ---------------------------------------------------------------------------

@pytest.fixture()
def client(tmp_path, monkeypatch, git_repo, patch_profiles_root):
    """Return a TestClient with a fresh DB and the plugin router mounted."""
    db_file = str(tmp_path / "api-test.db")
    monkeypatch.setenv("KARPATHY_DB_PATH", db_file)

    # Pre-create profile directories used by lifecycle tests so the H-4
    # existence check in POST /experiments passes.
    for _prof in ("revert-profile", "hist-profile", "scen-profile", "my-profile"):
        (patch_profiles_root / _prof).mkdir(exist_ok=True)

    # Reset the DB singleton so each test gets a fresh connection
    import _db as db_mod
    monkeypatch.setattr(db_mod, "_conn", None)

    # Force plugin_api to re-import _db fresh (it lazily calls get_db())
    # by reloading the relevant modules
    for mod_name in list(sys.modules.keys()):
        if mod_name.startswith("_db"):
            del sys.modules[mod_name]

    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    # Import router fresh
    if "dashboard.plugin_api" in sys.modules:
        del sys.modules["dashboard.plugin_api"]
    # plugin_api lives at PLUGIN_DIR/dashboard/plugin_api.py
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "dashboard.plugin_api",
        _PLUGIN_DIR / "dashboard" / "plugin_api.py",
    )
    api_mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(api_mod)

    app = FastAPI()
    app.include_router(api_mod.router)

    return TestClient(app), git_repo, db_file


def _now():
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _get_db(db_file):
    from _db import open_db
    return open_db(Path(db_file))


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------

def test_health(client):
    tc, repo, db_file = client
    resp = tc.get("/health")
    assert resp.status_code == 200
    assert resp.json()["ok"] is True


# ---------------------------------------------------------------------------
# Full lifecycle: create → approve → apply → verify
# ---------------------------------------------------------------------------

def test_full_lifecycle_create_approve_apply_verify(client):
    tc, repo, db_file = client

    soul_path = repo / "SOUL.md"
    original_content = soul_path.read_text()

    # 1. POST /experiments — create a proposed experiment with a diff
    diff = (
        "--- a/SOUL.md\n"
        "+++ b/SOUL.md\n"
        "@@ -1,1 +1,1 @@\n"
        "-You are a helpful assistant. Always be concise.\n"
        "+You are a helpful assistant. Always be concise and accurate.\n"
    )
    resp = tc.post("/experiments", json={
        "profile": "test-profile",
        "file": "SOUL.md",
        "diff": diff,
        "rationale": "Add accuracy",
    })
    assert resp.status_code == 201
    exp_id = resp.json()["experiment_id"]
    assert isinstance(exp_id, int)

    # Store profile_root on the experiment so /apply can find it
    db = _get_db(db_file)
    db.update_experiment_fields(
        exp_id,
        target_profile_root=str(repo),
        target_relpath="SOUL.md",
        updated_at=_now(),
    )

    # 2. POST /experiments/{id}/approve
    resp = tc.post(f"/experiments/{exp_id}/approve", json={"actor": "human"})
    assert resp.status_code == 200
    assert resp.json()["state"] == "approved"

    # 3. POST /experiments/{id}/apply — file must change + one commit added
    resp = tc.post(f"/experiments/{exp_id}/apply")
    assert resp.status_code == 200, resp.json()
    apply_data = resp.json()
    assert apply_data["state"] == "live"
    apply_commit_sha = apply_data["apply_commit_sha"]
    assert apply_commit_sha  # non-empty

    # File content should have changed
    new_content = soul_path.read_text()
    assert new_content != original_content
    assert "accurate" in new_content

    # Git log should show the extra commit
    log = subprocess.run(
        ["git", "-C", str(repo), "log", "--oneline"],
        capture_output=True, text=True,
    )
    assert len(log.stdout.strip().splitlines()) >= 2

    # DB: apply_commit_sha stored
    db = _get_db(db_file)
    exp = db.get_experiment(exp_id)
    assert exp["apply_commit_sha"] == apply_commit_sha

    # 4. POST /experiments/{id}/verify — baseline row created
    resp = tc.post(f"/experiments/{exp_id}/verify")
    assert resp.status_code == 200
    assert resp.json()["state"] == "verified"

    baselines = db.list_baselines("test-profile")
    assert len(baselines) >= 1


# ---------------------------------------------------------------------------
# Revert path
# ---------------------------------------------------------------------------

def test_lifecycle_revert(client):
    tc, repo, db_file = client

    diff = (
        "--- a/SOUL.md\n"
        "+++ b/SOUL.md\n"
        "@@ -1,1 +1,1 @@\n"
        "-You are a helpful assistant. Always be concise.\n"
        "+You are a helpful assistant. Always be concise and accurate.\n"
    )
    resp = tc.post("/experiments", json={
        "profile": "revert-profile",
        "file": "SOUL.md",
        "diff": diff,
        "rationale": "test",
    })
    assert resp.status_code == 201
    exp_id = resp.json()["experiment_id"]

    db = _get_db(db_file)
    db.update_experiment_fields(
        exp_id,
        target_profile_root=str(repo),
        target_relpath="SOUL.md",
        updated_at=_now(),
    )

    tc.post(f"/experiments/{exp_id}/approve", json={"actor": "human"})
    apply_resp = tc.post(f"/experiments/{exp_id}/apply")
    assert apply_resp.status_code == 200

    # Revert
    resp = tc.post(f"/experiments/{exp_id}/revert", json={"reason": "score dropped"})
    assert resp.status_code == 200
    assert resp.json()["state"] == "reverted"

    exp = db.get_experiment(exp_id)
    assert exp["state"] == "reverted"


# ---------------------------------------------------------------------------
# Scenario CRUD + holdout filtering
# ---------------------------------------------------------------------------

def test_scenario_crud_holdout_hidden_by_default(client):
    tc, repo, db_file = client

    # Create train scenario
    resp = tc.post("/scenarios", json={
        "profile": "scen-profile",
        "name": "train-scenario",
        "input": "say hello",
        "checks": [{"type": "must_contain", "value": "hello"}],
        "holdout": 0,
    })
    assert resp.status_code == 201
    train_id = resp.json()["scenario_id"]

    # Create holdout scenario
    resp = tc.post("/scenarios", json={
        "profile": "scen-profile",
        "name": "holdout-scenario",
        "input": "say goodbye",
        "checks": [{"type": "must_contain", "value": "goodbye"}],
        "holdout": 1,
    })
    assert resp.status_code == 201
    holdout_id = resp.json()["scenario_id"]

    # Default GET: holdout excluded
    resp = tc.get("/scenarios?profile=scen-profile")
    assert resp.status_code == 200
    names = [s["name"] for s in resp.json()["scenarios"]]
    assert "train-scenario" in names
    assert "holdout-scenario" not in names

    # With include_holdout=1: holdout visible
    resp = tc.get("/scenarios?profile=scen-profile&include_holdout=1")
    assert resp.status_code == 200
    names = [s["name"] for s in resp.json()["scenarios"]]
    assert "train-scenario" in names
    assert "holdout-scenario" in names

    # DELETE train scenario
    resp = tc.delete(f"/scenarios/{train_id}")
    assert resp.status_code == 200
    assert resp.json()["ok"] is True

    # Confirm deleted
    resp = tc.get("/scenarios?profile=scen-profile")
    names = [s["name"] for s in resp.json()["scenarios"]]
    assert "train-scenario" not in names

    # DELETE non-existent
    resp = tc.delete("/scenarios/99999")
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# GET /experiments/{id}/history
# ---------------------------------------------------------------------------

def test_history_returns_transitions_and_eval_runs(client):
    tc, repo, db_file = client

    # Create experiment
    resp = tc.post("/experiments", json={
        "profile": "hist-profile",
        "file": "SOUL.md",
        "diff": "",
        "rationale": "test history",
    })
    exp_id = resp.json()["experiment_id"]

    # Approve it (adds a state transition)
    tc.post(f"/experiments/{exp_id}/approve", json={"actor": "tester"})

    # Add an eval run manually
    db = _get_db(db_file)
    db.insert_eval_run(
        experiment_id=exp_id,
        kind="offline",
        proposer_model="a",
        judge_model="b",
        aggregate_score=0.75,
        created_at=_now(),
    )

    resp = tc.get(f"/experiments/{exp_id}/history")
    assert resp.status_code == 200
    data = resp.json()

    assert "experiment" in data
    assert "transitions" in data
    assert "eval_runs" in data
    assert "scenario_results" in data

    # Should have at least one transition (proposed → approved)
    assert len(data["transitions"]) >= 1
    t = data["transitions"][0]
    assert t["from_state"] == "proposed"
    assert t["to_state"] == "approved"
    assert t["actor"] == "tester"

    # Should have one eval run
    assert len(data["eval_runs"]) >= 1
    assert data["eval_runs"][0]["aggregate_score"] == pytest.approx(0.75)


def test_history_404_for_missing_experiment(client):
    tc, repo, db_file = client
    resp = tc.get("/experiments/99999/history")
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# POST /experiments — missing profile returns 400
# ---------------------------------------------------------------------------

def test_create_experiment_missing_profile(client):
    tc, repo, db_file = client
    resp = tc.post("/experiments", json={"diff": "x", "rationale": "y"})
    assert resp.status_code == 400


# ---------------------------------------------------------------------------
# Pause / Resume
# ---------------------------------------------------------------------------

def test_pause_and_resume_profile(client):
    tc, repo, db_file = client

    resp = tc.post("/profiles/my-profile/pause")
    assert resp.status_code == 200
    assert resp.json()["paused"] is True

    db = _get_db(db_file)
    assert db.is_paused("my-profile") is True

    resp = tc.post("/profiles/my-profile/resume")
    assert resp.status_code == 200
    assert resp.json()["paused"] is False
    assert db.is_paused("my-profile") is False


# ---------------------------------------------------------------------------
# GET /profiles/{profile} — full config + status surface
# ---------------------------------------------------------------------------

def test_get_profile_status_surface(client):
    tc, repo, db_file = client

    # Seed one train + one holdout scenario for the profile.
    tc.post("/scenarios", json={"profile": "scen-profile", "name": "t", "holdout": 0})
    tc.post("/scenarios", json={"profile": "scen-profile", "name": "h", "holdout": 1})

    resp = tc.get("/profiles/scen-profile")
    assert resp.status_code == 200
    data = resp.json()

    # Every contract key is present.
    for key in (
        "profile", "paused", "configured", "target_relpath", "profile_root",
        "proposer_model", "judge_model", "live_sessions_target", "scenario_counts",
        "experiment_counts", "latest_baseline_score", "last_collection_at",
        "last_proposal_at", "last_verification_at",
    ):
        assert key in data, f"missing key {key!r}"

    assert data["profile"] == "scen-profile"
    assert data["paused"] is False
    assert data["target_relpath"] == "system_prompt.md"
    assert data["scenario_counts"] == {"train": 1, "holdout": 1}
    assert set(data["experiment_counts"]) == {
        "proposed", "approved", "live", "verified", "reverted", "rejected"
    }
    # No experiments/baselines/metrics seeded → optional fields are null.
    assert data["last_proposal_at"] is None
    assert data["latest_baseline_score"] is None
    assert data["last_collection_at"] is None

    # Pause is reflected in the surface.
    tc.post("/profiles/scen-profile/pause")
    assert tc.get("/profiles/scen-profile").json()["paused"] is True
