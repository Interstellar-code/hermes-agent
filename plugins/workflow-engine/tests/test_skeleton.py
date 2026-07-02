"""Phase 1 skeleton tests — import + /health only."""

from __future__ import annotations

import importlib

import pytest
pytest.importorskip("fastapi")

from fastapi import FastAPI
from fastapi.testclient import TestClient

from plugins.workflow_engine.dashboard.plugin_api import router


def _make_client() -> TestClient:
    app = FastAPI()
    app.include_router(router, prefix="/api/plugins/workflow-engine")
    return TestClient(app, raise_server_exceptions=True)


# ---------------------------------------------------------------------------
# Import smoke test
# ---------------------------------------------------------------------------


def test_plugin_module_importable():
    mod = importlib.import_module("plugins.workflow_engine")
    assert hasattr(mod, "register"), "plugins.workflow_engine must expose register()"


def test_router_importable():
    from plugins.workflow_engine.dashboard.plugin_api import router as r  # noqa: PLC0415
    assert r is not None


def test_engine_package_importable():
    importlib.import_module("plugins.workflow_engine.engine")


# ---------------------------------------------------------------------------
# /health — must return 200 + ok=True
# ---------------------------------------------------------------------------


def test_health():
    client = _make_client()
    r = client.get("/api/plugins/workflow-engine/health")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert "version" in body


# Phase 3 implemented all 9 endpoints — stub-returns-501 test removed.
# Real endpoint coverage lives in test_api_definitions.py / test_api_runs.py /
# test_api_approve.py / test_api_events_sse.py.
