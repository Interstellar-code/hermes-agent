"""
test_api_routes.py — FastAPI TestClient tests for dashboard/plugin_api.py.

Mounts the router directly without going through web_server.
Uses a temp DB via KARPATHY_DB_PATH env override.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

# Ensure plugin root is on sys.path before importing plugin_api.
_PLUGIN_DIR = Path(__file__).resolve().parent.parent
if str(_PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_DIR))

# Also ensure dashboard/ dir is findable for the spec-loaded flat-module path.
_DASHBOARD_DIR = _PLUGIN_DIR / "dashboard"
if str(_DASHBOARD_DIR) not in sys.path:
    sys.path.insert(0, str(_DASHBOARD_DIR))


@pytest.fixture(autouse=True)
def isolated_db(tmp_path, monkeypatch):
    """Point all DB operations at a fresh temp file and reset the singleton."""
    db_file = str(tmp_path / "api-test-karpathy.db")
    monkeypatch.setenv("KARPATHY_DB_PATH", db_file)

    import _db as db_mod
    monkeypatch.setattr(db_mod, "_conn", None)
    yield db_file


@pytest.fixture()
def client():
    """Return a TestClient wrapping the karpathy plugin router."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    # Import plugin_api via spec_from_file_location to mirror how web_server does it.
    import importlib.util

    api_path = _DASHBOARD_DIR / "plugin_api.py"
    spec = importlib.util.spec_from_file_location("_karpathy_plugin_api_test", api_path)
    mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    spec.loader.exec_module(mod)  # type: ignore[union-attr]

    app = FastAPI()
    app.include_router(mod.router, prefix="/api/plugins/karpathy-self-improve")
    return TestClient(app)


# ---------------------------------------------------------------------------
# /health
# ---------------------------------------------------------------------------

def test_health(client) -> None:
    r = client.get("/api/plugins/karpathy-self-improve/health")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["plugin"] == "karpathy-self-improve"
    assert "version" in body


# ---------------------------------------------------------------------------
# /metrics
# ---------------------------------------------------------------------------

def test_metrics_empty(client) -> None:
    r = client.get("/api/plugins/karpathy-self-improve/metrics")
    assert r.status_code == 200
    assert r.json()["metrics"] == []


def test_metrics_after_insert(client, tmp_path) -> None:
    from datetime import datetime, timezone
    from _db import open_db

    db_file = Path(os.environ["KARPATHY_DB_PATH"])
    db = open_db(db_file)
    db.insert_metrics_snapshot(
        profile="test",
        captured_at=datetime.now(timezone.utc).isoformat(),
        sessions_count=3,
    )

    r = client.get("/api/plugins/karpathy-self-improve/metrics")
    assert r.status_code == 200
    rows = r.json()["metrics"]
    assert len(rows) == 1
    assert rows[0]["profile"] == "test"
    assert rows[0]["sessions_count"] == 3


def test_metrics_profile_filter(client) -> None:
    from datetime import datetime, timezone
    from _db import open_db

    db_file = Path(os.environ["KARPATHY_DB_PATH"])
    db = open_db(db_file)
    ts = datetime.now(timezone.utc).isoformat()
    db.insert_metrics_snapshot(profile="a", captured_at=ts)
    db.insert_metrics_snapshot(profile="b", captured_at=ts)

    r = client.get("/api/plugins/karpathy-self-improve/metrics?profile=a")
    assert r.status_code == 200
    rows = r.json()["metrics"]
    assert len(rows) == 1
    assert rows[0]["profile"] == "a"


def test_metrics_latest(client) -> None:
    from datetime import datetime, timezone
    from _db import open_db

    db_file = Path(os.environ["KARPATHY_DB_PATH"])
    db = open_db(db_file)
    ts = datetime.now(timezone.utc).isoformat()
    db.insert_metrics_snapshot(profile="x", captured_at=ts, sessions_count=1)
    db.insert_metrics_snapshot(profile="x", captured_at=ts, sessions_count=2)
    db.insert_metrics_snapshot(profile="y", captured_at=ts, sessions_count=5)

    r = client.get("/api/plugins/karpathy-self-improve/metrics/latest")
    assert r.status_code == 200
    rows = r.json()["metrics"]
    profiles = {row["profile"] for row in rows}
    assert profiles == {"x", "y"}


# ---------------------------------------------------------------------------
# /experiments
# ---------------------------------------------------------------------------

def test_experiments_empty(client) -> None:
    r = client.get("/api/plugins/karpathy-self-improve/experiments")
    assert r.status_code == 200
    assert r.json()["experiments"] == []


def test_experiment_not_found(client) -> None:
    r = client.get("/api/plugins/karpathy-self-improve/experiments/9999")
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# /propose — profile_root resolution via get_profile_dir (PR #137 fix)
# ---------------------------------------------------------------------------

def _reload_plugin_api(monkeypatch, get_profile_dir_mock):
    """Reload plugin_api with get_profile_dir monkeypatched."""
    import importlib
    import importlib.util
    import sys

    # Patch hermes_cli.profiles.get_profile_dir before loading the module.
    import types
    profiles_mod = types.ModuleType("hermes_cli.profiles")
    profiles_mod.get_profile_dir = get_profile_dir_mock
    monkeypatch.setitem(sys.modules, "hermes_cli.profiles", profiles_mod)
    # Also ensure hermes_cli namespace exists.
    if "hermes_cli" not in sys.modules:
        hermes_cli_mod = types.ModuleType("hermes_cli")
        monkeypatch.setitem(sys.modules, "hermes_cli", hermes_cli_mod)

    # Remove any prior load of the plugin api from sys.modules so it re-imports.
    monkeypatch.delitem(sys.modules, "_karpathy_plugin_api_test", raising=False)

    api_path = _DASHBOARD_DIR / "plugin_api.py"
    spec = importlib.util.spec_from_file_location("_karpathy_plugin_api_test_propose", api_path)
    mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


def test_propose_uses_get_profile_dir_named(tmp_path, monkeypatch) -> None:
    """trigger_propose resolves profile_root via get_profile_dir for a named profile."""
    import sys
    import types

    expected_dir = tmp_path / "profiles" / "coder"
    expected_dir.mkdir(parents=True)

    captured = {}

    def fake_get_profile_dir(name):
        captured["name"] = name
        return expected_dir

    mod = _reload_plugin_api(monkeypatch, fake_get_profile_dir)

    # Stub out propose_for_profile so no real LLM call is made. Use ONLY
    # monkeypatch.setitem (not patch.dict) so sys.modules["_proposer"] is
    # restored to its true prior value on teardown — mixing the two leaks the
    # fake module into later test_proposer.py tests.
    fake_result = type("R", (), {"skipped": True, "skip_reason": "no_changes", "ok": True})()
    proposer_mod = types.ModuleType("_proposer")
    proposer_mod.propose_for_profile = lambda **kw: (
        captured.update({"profile_root": kw.get("profile_root")}),
        fake_result,
    )[1]
    monkeypatch.setitem(sys.modules, "_proposer", proposer_mod)

    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    app = FastAPI()
    app.include_router(mod.router, prefix="/api/plugins/karpathy-self-improve")
    client = TestClient(app)

    client.post(
        "/api/plugins/karpathy-self-improve/propose",
        json={"profile": "coder"},
        headers={"Authorization": "Bearer test"},
    )

    assert captured.get("name") == "coder"
    assert captured.get("profile_root") == str(expected_dir)


def test_propose_uses_get_profile_dir_default(tmp_path, monkeypatch) -> None:
    """trigger_propose resolves profile_root to default home for 'default' profile."""
    from pathlib import Path as _Path
    import types, sys

    default_home = tmp_path / "hermes_home"
    default_home.mkdir()

    captured = {}

    def fake_get_profile_dir(name):
        captured["name"] = name
        return default_home

    mod = _reload_plugin_api(monkeypatch, fake_get_profile_dir)

    fake_result = type("R", (), {"skipped": True, "skip_reason": "no_changes", "ok": True})()

    proposer_mod = types.ModuleType("_proposer")
    proposer_mod.propose_for_profile = lambda **kw: (captured.update({"profile_root": kw.get("profile_root")}), fake_result)[1]
    monkeypatch.setitem(sys.modules, "_proposer", proposer_mod)

    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    app = FastAPI()
    app.include_router(mod.router, prefix="/api/plugins/karpathy-self-improve")
    client = TestClient(app)

    r = client.post(
        "/api/plugins/karpathy-self-improve/propose",
        json={"profile": "default"},
        headers={"Authorization": "Bearer test"},
    )

    assert captured.get("name") == "default"
    assert captured.get("profile_root") == str(default_home)


def test_propose_body_override_wins(tmp_path, monkeypatch) -> None:
    """Explicit body profile_root overrides get_profile_dir resolution."""
    import types, sys

    resolver_dir = tmp_path / "profiles" / "coder"
    resolver_dir.mkdir(parents=True)
    override_dir = str(tmp_path / "custom_root")

    captured = {}

    def fake_get_profile_dir(name):
        return resolver_dir

    mod = _reload_plugin_api(monkeypatch, fake_get_profile_dir)

    fake_result = type("R", (), {"skipped": True, "skip_reason": "no_changes", "ok": True})()

    proposer_mod = types.ModuleType("_proposer")
    proposer_mod.propose_for_profile = lambda **kw: (captured.update({"profile_root": kw.get("profile_root")}), fake_result)[1]
    monkeypatch.setitem(sys.modules, "_proposer", proposer_mod)

    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    app = FastAPI()
    app.include_router(mod.router, prefix="/api/plugins/karpathy-self-improve")
    client = TestClient(app)

    r = client.post(
        "/api/plugins/karpathy-self-improve/propose",
        json={"profile": "coder", "profile_root": override_dir},
        headers={"Authorization": "Bearer test"},
    )

    assert captured.get("profile_root") == override_dir
