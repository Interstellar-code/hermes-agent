"""test_api_routes.py — personas REST router: list / get / promote-stub.

Loads plugin_api flat (spec_from_file_location) and drives the router via
FastAPI TestClient. Auth no-ops in standalone test context (_require_auth swallows
ImportError when web_server is absent).
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

import pytest

_PLUGIN_DIR = Path(__file__).resolve().parent.parent  # plugins/personas/


def _load_api() -> Any:
    if str(_PLUGIN_DIR) not in sys.path:
        sys.path.insert(0, str(_PLUGIN_DIR))
    spec = importlib.util.spec_from_file_location(
        "personas_plugin_api", _PLUGIN_DIR / "dashboard" / "plugin_api.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _client():
    fastapi = pytest.importorskip("fastapi")
    pytest.importorskip("starlette")
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    api = _load_api()
    app = FastAPI()
    app.include_router(api.router, prefix="/api/plugins/personas")
    return TestClient(app)


def test_list_returns_all():
    c = _client()
    r = c.get("/api/plugins/personas/list")
    assert r.status_code == 200
    data = r.json()
    assert data["count"] == 20 and len(data["personas"]) == 20


def test_list_metadata_only():
    c = _client()
    data = c.get("/api/plugins/personas/list").json()
    for p in data["personas"]:
        assert "system_prompt" not in p
        assert "system_prompt_preview" in p


def test_list_category_filter():
    c = _client()
    data = c.get("/api/plugins/personas/list", params={"category": "engineering"}).json()
    assert data["count"] == 4
    assert all(p["category"] == "engineering" for p in data["personas"])


def test_get_returns_full_prompt():
    c = _client()
    r = c.get("/api/plugins/personas/get", params={"id": "engineering-security-engineer"})
    assert r.status_code == 200
    persona = r.json()["persona"]
    assert persona["system_prompt"].strip()


def test_get_unknown_404():
    c = _client()
    r = c.get("/api/plugins/personas/get", params={"id": "does-not-exist"})
    assert r.status_code == 404


def test_promote_stub_501():
    c = _client()
    r = c.post("/api/plugins/personas/promote", json={"persona_id": "x"})
    assert r.status_code == 501


def test_body_cap_constant():
    api = _load_api()
    assert api.MAX_BODY_BYTES == 32 * 1024
