"""B4: definition save rejects hostile YAML fast (400-class, not hang/500);
runs started by the agent tool carry trigger.kind 'agent'."""
from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

import pytest
pytest.importorskip("fastapi")

import yaml
from fastapi import FastAPI
from fastapi.testclient import TestClient

from engine.discovery.validator import _load_bounded, validate_workflow_yaml
from engine.wiring import create_engine

_DEFAULTS = Path(__file__).resolve().parent.parent / "defaults"

_ONE = """\
name: one
description: d
nodes:
  - id: a
    bash: echo a
"""

# 9 levels x 9 aliases: ~387M nodes if expanded.
_BOMB = 'a: &a ["x","x","x","x","x","x","x","x","x"]\n' + "".join(
    f"{chr(98 + i)}: &{chr(98 + i)} [{','.join(['*' + chr(97 + i)] * 9)}]\n" for i in range(8)
)
# kind -> (yaml, seconds allowed). Deep flow nesting costs a flat ~0.6 s at any
# size: PyYAML's scanner looks ahead up to 1024 chars per open bracket until the
# composer hits the recursion limit; the bound is 2 s so a loaded runner passes.
_HOSTILE = {
    "alias_bomb": (_BOMB, 1.0),
    "deep_flow": ("[" * 5000 + "]" * 5000, 2.0),
    "deep_block": ("- " * 3000 + "x\n", 1.0),
    "recursive_alias": ("name: x\ndescription: y\nnodes: &n [*n]\n", 1.0),
    # YAML 1.1 base-60 int: PyYAML's construction is quadratic (23 s at 600 KB)
    "sexagesimal_int": ("n: " + ":".join(["1"] * 100_000) + "\n", 1.0),
    "sexagesimal_float": ("n: " + ":".join(["1"] * 400) + ".5\n", 1.0),  # OverflowError
    "long_decimal": ("n: " + "9" * 5000 + "\n", 1.0),  # Python int-digit limit
    "bad_tagged_value": ("n: !!int ''\nm: !!bool maybe\n", 1.0),  # constructor IndexError
}

_LINT_FANOUT = (  # one big anchored scalar aliased from every node
    "name: x\ndescription: y\nnodes:\n  - id: n0\n    prompt: &p \""
    + "$inputs.a " * 40_000 + "\"\n"
    + "".join(f"  - id: n{i}\n    prompt: *p\n" for i in range(1, 2000))
)


def _arun(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


@pytest.fixture()
def engine():
    e = create_engine(db_path=":memory:", seed_bundled=False,
                      write_manifest=False, crash_recovery=False)
    yield e
    _arun(e.shutdown())


@pytest.fixture()
def client(engine):
    import plugins.workflow_engine.dashboard.plugin_api as api_mod
    original = api_mod._engine
    api_mod._engine = lambda: engine
    app = FastAPI()
    app.include_router(api_mod.router)
    with TestClient(app, raise_server_exceptions=True) as c:
        yield c
    api_mod._engine = original


@pytest.mark.parametrize("kind", sorted(_HOSTILE))
def test_hostile_yaml_save_rejected_fast(client, kind):
    text, limit = _HOSTILE[kind]
    t = time.monotonic()
    r = client.post("/definitions", json={"id": "evil", "name": "evil", "yaml": text})
    assert time.monotonic() - t < limit
    assert r.status_code == 422, r.text
    assert "YAML parse error" in r.json()["error"]
    assert client.get("/definitions/evil").status_code == 404


def test_validate_route_hostile_yaml_is_200_parse_error(client):
    for kind, (text, limit) in _HOSTILE.items():
        t = time.monotonic()
        r = client.post("/definitions/validate", json={"yaml": text})
        assert time.monotonic() - t < limit, kind
        assert r.status_code == 200, (kind, r.text)
        assert [e["code"] for e in r.json()["errors"]] == ["yaml_parse"], kind


def test_lint_scans_aliased_scalar_once(client):
    t = time.monotonic()
    r = client.post("/definitions/validate", json={"yaml": _LINT_FANOUT})
    assert time.monotonic() - t < 5.0
    assert r.status_code == 200, r.text


def test_lone_surrogate_is_400(client):
    body = '{"id": "s", "name": "s", "yaml": "name: \\ud800"}'
    for path in ("/definitions", "/definitions/validate"):
        r = client.post(path, content=body, headers={"content-type": "application/json"})
        assert r.status_code == 400, (path, r.text)


def test_valid_yaml_parses_like_safe_load():
    files = sorted(_DEFAULTS.glob("*.yaml"))
    assert files
    for p in files:
        text = p.read_text(encoding="utf-8")
        assert _load_bounded(text)[1] == yaml.safe_load(text), p.name
        assert validate_workflow_yaml(text, p.name)[1] is None, p.name
    anchors = "a: &x {k: 1}\nb: *x\nc: [*x, *x]\n"
    assert _load_bounded(anchors)[1] == yaml.safe_load(anchors)


def test_plain_parse_error_message_unchanged():
    bad = "name: x\ndescription: y\nnodes: [unclosed\n"
    with pytest.raises(yaml.YAMLError) as exc:
        yaml.safe_load(bad)
    _wf, err = validate_workflow_yaml(bad, "f")
    assert err is not None
    assert err.error == f"YAML parse error: {exc.value}" and err.errorType == "parse_error"


def _start_via_tool(engine, monkeypatch):
    import plugins.workflow_engine._shared as shared
    import plugins.workflow_engine.tools.run_workflow as rw
    monkeypatch.setattr(shared, "get_engine", lambda: engine)
    monkeypatch.setattr(rw, "_wait_timeout_s", lambda: None)
    return json.loads(_arun(rw.handler({"id": "one"}, session_id="s1")))


def test_agent_tool_run_has_agent_kind(engine, client, monkeypatch):
    assert client.post("/definitions", json={"id": "one", "name": "one", "yaml": _ONE}).status_code == 201
    out = _start_via_tool(engine, monkeypatch)
    run = _arun(engine.get_run(out["run_id"]))
    assert run["metadata"]["trigger"]["kind"] == "agent"


def test_http_run_kind_unchanged(engine, client):
    assert client.post("/definitions", json={"id": "one", "name": "one", "yaml": _ONE}).status_code == 201
    r = client.post("/runs", json={"workflow_id": "one", "conversation_id": "c",
                                   "user_message": "go"})
    assert r.status_code in (200, 201, 202), r.text
    run_id = r.json().get("run", r.json()).get("id") or r.json()["run_id"]
    run = _arun(engine.get_run(run_id))
    assert run["metadata"]["trigger"]["kind"] == "manual"
