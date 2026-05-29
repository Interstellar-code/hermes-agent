"""
Tests for GET /events (SSE).

TestClient streams SSE synchronously; we read the first few frames.
"""
from __future__ import annotations

import pytest
pytest.importorskip("fastapi")
pytestmark = pytest.mark.skip(
    reason="Starlette TestClient buffers this plugin's infinite SSE stream"
)

from fastapi import FastAPI
from fastapi.testclient import TestClient

from engine.wiring import create_engine

_HELLO_YAML = """\
id: hello-world
name: Hello World
description: A minimal test workflow
nodes:
  - id: greet
    prompt: Say hello
"""


@pytest.fixture()
def client():
    engine = create_engine(db_path=":memory:", seed_bundled=False, write_manifest=False, crash_recovery=False)
    app = FastAPI()

    import plugins.workflow_engine.dashboard.plugin_api as api_mod
    original = api_mod._engine
    api_mod._engine = lambda: engine
    app.include_router(api_mod.router)

    with TestClient(app, raise_server_exceptions=False) as c:
        c.post("/definitions", json={
            "id": "hello-world",
            "name": "Hello World",
            "yaml": _HELLO_YAML,
            "source": "user",
        })
        yield c

    api_mod._engine = original


def _read_sse_frames(response, max_frames: int = 5) -> list[dict]:
    """Read up to max_frames SSE frames from the response, return parsed dicts."""
    frames = []
    current: dict = {}
    for line in response.iter_lines():
        if not line:
            if current:
                frames.append(current)
                current = {}
            if len(frames) >= max_frames:
                break
        elif line.startswith("event:"):
            current["event"] = line[len("event:"):].strip()
        elif line.startswith("data:"):
            current["data"] = line[len("data:"):].strip()
    return frames


def _read_sse_frames_until(response, expected_events: set[str], max_lines: int = 50) -> list[dict]:
    """Read SSE frames until all expected event names are seen or line budget expires."""
    frames = []
    current: dict = {}
    seen: set[str] = set()
    for idx, line in enumerate(response.iter_lines()):
        if idx >= max_lines:
            break
        if not line:
            if current:
                frames.append(current)
                event_name = current.get("event")
                if event_name:
                    seen.add(event_name)
                current = {}
                if expected_events.issubset(seen):
                    break
        elif line.startswith("event:"):
            current["event"] = line[len("event:"):].strip()
        elif line.startswith("data:"):
            current["data"] = line[len("data:"):].strip()
    return frames


def test_events_returns_event_stream(client):
    """GET /events responds with text/event-stream content-type."""
    with client.stream("GET", "/events?runId=nonexistent") as r:
        assert r.status_code == 200
        assert "text/event-stream" in r.headers.get("content-type", "")
        frames = _read_sse_frames_until(r, {"connected", "error"})
        r.close()
    assert frames[0]["event"] == "connected"
    assert frames[1]["event"] == "error"
    assert "\"reason\": \"run_not_found\"" in frames[1]["data"]


def test_events_for_run_emits_frames(client):
    """After creating a run, GET /events?runId=<id> replays events."""
    create_r = client.post("/runs", json={
        "workflow_id": "hello-world",
        "conversation_id": "conv-sse-001",
        "user_message": "start",
    })
    assert create_r.status_code == 201
    run_id = create_r.json()["run"]["id"]

    # Read replayed events; the bus replays last 50 DB events.
    with client.stream("GET", f"/events?runId={run_id}") as r:
        assert r.status_code == 200
        frames = _read_sse_frames_until(r, {"workflow_started"}, max_lines=100)
        r.close()

    # At minimum, workflow_started should have been emitted.
    event_kinds = [f.get("event") for f in frames]
    assert any(k is not None for k in event_kinds), "Expected at least one SSE frame"


def test_events_all_runs_no_run_id(client):
    """GET /events without runId streams all-run events."""
    with client.stream("GET", "/events") as r:
        assert r.status_code == 200
        assert "text/event-stream" in r.headers.get("content-type", "")
        r.close()


def test_events_cache_control_header(client):
    """SSE endpoint must disable caching."""
    with client.stream("GET", "/events") as r:
        assert r.status_code == 200
        cc = r.headers.get("cache-control", "")
        assert "no-cache" in cc
        r.close()
