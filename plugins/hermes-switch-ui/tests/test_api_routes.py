"""test_api_routes.py — FastAPI route tests for hermes-switch-ui dashboard/plugin_api.py.

Loads plugin_api.py flat via spec_from_file_location, mounts router on TestClient.
Auth fallback no-ops (hermes_cli not importable in test context).
All writes go to a temp SWITCHUI_STATE_PATH.

Routes covered:
  GET  /connection
  POST /register   — valid, version warn, oversized (413), garbage JSON (422)
  POST /settings   — valid, secret key stripping, oversized (413), garbage JSON (422)
  GET  /status     — running True, stale -> False
  POST /heartbeat  — ok + refreshes running
"""
from __future__ import annotations

import importlib.util
import json
import os
import sys
import time
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

# ---------------------------------------------------------------------------
# Loader
# ---------------------------------------------------------------------------

_PLUGIN_DIR = Path(__file__).resolve().parent.parent
_API_PATH = _PLUGIN_DIR / "dashboard" / "plugin_api.py"

# Inject plugin dir into sys.path before any module load so _state/_knowledge/_version_compat resolve
if str(_PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_DIR))


def _load_api_module(state_path: Path):
    """Load plugin_api fresh with SWITCHUI_STATE_PATH set to state_path."""
    # Set env BEFORE module exec so _state picks it up at import time
    os.environ["SWITCHUI_STATE_PATH"] = str(state_path)

    # Evict cached flat-import modules so each load is fresh
    for key in list(sys.modules.keys()):
        if key in ("_state", "_state_fresh", "_knowledge", "_version_compat",
                   "plugin_api_test_module"):
            del sys.modules[key]

    spec = importlib.util.spec_from_file_location("plugin_api_test_module", _API_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _make_client(state_path: Path) -> TestClient:
    """Return a TestClient wrapping the plugin router mounted at /."""
    api_mod = _load_api_module(state_path)
    app = FastAPI()
    app.include_router(api_mod.router)
    return TestClient(app, raise_server_exceptions=False)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def client(tmp_path):
    state_file = tmp_path / "state.json"
    return _make_client(state_file)


@pytest.fixture()
def client_with_state(tmp_path):
    """Return (client, state_file) so tests can inspect / pre-populate the file."""
    state_file = tmp_path / "state.json"
    return _make_client(state_file), state_file


# ---------------------------------------------------------------------------
# /connection
# ---------------------------------------------------------------------------

def test_connection_shape(client):
    resp = client.get("/connection")
    assert resp.status_code == 200
    data = resp.json()
    # Must include at minimum gateway_port and dashboard_port
    assert "gateway_port" in data, f"/connection missing gateway_port: {data}"
    assert "dashboard_port" in data, f"/connection missing dashboard_port: {data}"


# ---------------------------------------------------------------------------
# /register
# ---------------------------------------------------------------------------

_VALID_MANIFEST = {
    "version": "1.2.3",
    "url": "http://localhost:3002",
    "port": 3002,
    "enabled_features": ["chat", "terminal"],
}


def test_register_valid_ok(client):
    resp = client.post("/register", json=_VALID_MANIFEST)
    assert resp.status_code == 200
    data = resp.json()
    assert data.get("ok") is True
    assert "compat" in data


def test_register_compat_in_range(client):
    resp = client.post("/register", json=_VALID_MANIFEST)
    compat = resp.json()["compat"]
    assert compat["compatible"] is True
    assert compat["warn"] is None


def test_register_version_warn_non_null(tmp_path):
    """Version 3.0.0 is outside >=1.0.0,<3.0.0 — compat.warn must be non-null."""
    state_file = tmp_path / "s.json"
    c = _make_client(state_file)
    payload = {**_VALID_MANIFEST, "version": "3.0.0"}
    resp = c.post("/register", json=payload)
    assert resp.status_code == 200
    compat = resp.json()["compat"]
    assert compat["compatible"] is False
    assert compat["warn"] is not None and compat["warn"].strip()


def test_register_oversized_413(tmp_path):
    """Body > 32 KB must return 413; state file must remain untouched."""
    state_file = tmp_path / "s.json"
    c = _make_client(state_file)

    big_body = ("x" * (32 * 1024 + 1)).encode()
    resp = c.post("/register", content=big_body,
                  headers={"content-type": "application/json"})
    assert resp.status_code == 413, f"Expected 413, got {resp.status_code}"
    assert not state_file.exists(), "State file must not be created on oversized body"


def test_register_garbage_json_422(client):
    resp = client.post("/register", content=b"not-json-{{{",
                       headers={"content-type": "application/json"})
    assert resp.status_code == 422


def test_register_persists_manifest(client_with_state):
    c, state_file = client_with_state
    c.post("/register", json=_VALID_MANIFEST)
    raw = json.loads(state_file.read_text())
    assert raw.get("manifest", {}).get("version") == "1.2.3"


# ---------------------------------------------------------------------------
# /settings
# ---------------------------------------------------------------------------

def test_settings_valid_ok(client):
    resp = client.post("/settings", json={"theme": "dark", "language": "en"})
    assert resp.status_code == 200
    assert resp.json().get("ok") is True


def test_settings_strips_token(client_with_state):
    c, state_file = client_with_state
    payload = {"theme": "dark", "HERMES_API_TOKEN": "secret", "auth_token": "abc"}
    resp = c.post("/settings", json=payload)
    assert resp.status_code == 200
    raw = json.loads(state_file.read_text())
    reported = raw.get("reported_settings", {})
    assert "HERMES_API_TOKEN" not in reported
    assert "auth_token" not in reported
    assert reported.get("theme") == "dark"


def test_settings_strips_password(client_with_state):
    c, state_file = client_with_state
    payload = {"lang": "en", "HERMES_PASSWORD": "hunter2"}
    resp = c.post("/settings", json=payload)
    assert resp.status_code == 200
    raw = json.loads(state_file.read_text())
    reported = raw.get("reported_settings", {})
    assert "HERMES_PASSWORD" not in reported
    assert reported.get("lang") == "en"


def test_settings_oversized_413(tmp_path):
    """Body > 32 KB must return 413; state file must remain untouched."""
    state_file = tmp_path / "s.json"
    c = _make_client(state_file)

    big_body = ("y" * (32 * 1024 + 1)).encode()
    resp = c.post("/settings", content=big_body,
                  headers={"content-type": "application/json"})
    assert resp.status_code == 413, f"Expected 413, got {resp.status_code}"
    assert not state_file.exists(), "State file must not be created on oversized body"


def test_settings_garbage_json_422(client):
    resp = client.post("/settings", content=b"}bad{",
                       headers={"content-type": "application/json"})
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# /status
# ---------------------------------------------------------------------------

def test_status_running_after_register(client):
    client.post("/register", json=_VALID_MANIFEST)
    resp = client.get("/status")
    assert resp.status_code == 200
    data = resp.json()
    assert data["running"] is True


def test_status_not_running_stale_heartbeat(tmp_path):
    """Backdated heartbeat -> running=False."""
    state_file = tmp_path / "s.json"
    state_file.parent.mkdir(parents=True, exist_ok=True)
    state_file.write_text(
        json.dumps({"schema_version": 1, "last_heartbeat": "2000-01-01T00:00:00Z"}),
        encoding="utf-8",
    )
    c = _make_client(state_file)
    resp = c.get("/status")
    assert resp.status_code == 200
    assert resp.json()["running"] is False


def test_status_shape(client):
    resp = client.get("/status")
    assert resp.status_code == 200
    data = resp.json()
    for key in ("running", "last_heartbeat", "ttl_seconds"):
        assert key in data, f"/status missing key {key!r}: {data}"


# ---------------------------------------------------------------------------
# /heartbeat
# ---------------------------------------------------------------------------

def test_heartbeat_ok(client):
    resp = client.post("/heartbeat")
    assert resp.status_code == 200
    assert resp.json().get("ok") is True


def test_heartbeat_refreshes_running(tmp_path):
    """After a stale state, posting /heartbeat must flip running back to True."""
    state_file = tmp_path / "s.json"
    state_file.parent.mkdir(parents=True, exist_ok=True)
    state_file.write_text(
        json.dumps({"schema_version": 1, "last_heartbeat": "2000-01-01T00:00:00Z"}),
        encoding="utf-8",
    )
    c = _make_client(state_file)

    # Confirm stale before
    assert c.get("/status").json()["running"] is False

    # Post heartbeat
    c.post("/heartbeat")

    # Now running must be True
    assert c.get("/status").json()["running"] is True
