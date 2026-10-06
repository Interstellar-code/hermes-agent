"""POST /definitions/validate — read-only, positioned lint for the editor (B1)."""
from __future__ import annotations

import time
from pathlib import Path

import pytest
pytest.importorskip("fastapi")

from fastapi import FastAPI
from fastapi.testclient import TestClient

from engine.wiring import create_engine

_DEFAULTS = Path(__file__).resolve().parent.parent / "defaults"

_OK = """\
name: Demo
description: demo
inputs:
  - name: repo
nodes:
  - id: a
    prompt: Look at $INPUTS.repo
  - id: b
    prompt: Summarise $a.output
    depends_on: [a]
"""


@pytest.fixture()
def engine():
    return create_engine(db_path=":memory:", seed_bundled=False,
                         write_manifest=False, crash_recovery=False)


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


def _validate(client, yaml_text, **extra):
    r = client.post("/definitions/validate", json={"yaml": yaml_text, **extra})
    assert r.status_code == 200, r.text
    return r.json()


def _codes(items):
    return [d["code"] for d in items]


def test_valid_definition(client):
    body = _validate(client, _OK)
    assert body == {"ok": True, "errors": [], "warnings": [], "id_available": None}


def test_yaml_parse_has_line(client):
    body = _validate(client, "name: x\ndescription: y\nnodes:\n  - id: a\n    prompt: [unclosed\n")
    assert not body["ok"]
    (err,) = body["errors"]
    assert err["code"] == "yaml_parse"
    assert err["line"] == 6 and err["col"] is not None
    (err,) = _validate(client, "name: x\ndescription: y\n\tnodes: []\n")["errors"]
    assert (err["code"], err["line"]) == ("yaml_parse", 3)


def test_schema_node_error_located(client):
    body = _validate(client, "name: x\ndescription: y\nnodes:\n  - id: a\n    prompt: hi\n  - id: b\n")
    (err,) = body["errors"]
    assert err["code"] == "schema" and err["node_id"] == "b" and err["line"] == 6


def test_schema_top_level(client):
    body = _validate(client, "name: x\nnodes:\n  - id: a\n    prompt: hi\n")
    assert _codes(body["errors"]) == ["schema"]
    assert "description" in body["errors"][0]["message"]


def test_duplicate_id(client):
    body = _validate(client, "name: x\ndescription: y\nnodes:\n"
                             "  - id: a\n    prompt: hi\n  - id: a\n    prompt: again\n")
    (err,) = body["errors"]
    assert err["code"] == "duplicate_id" and err["node_id"] == "a" and err["line"] == 6


def test_unknown_dependency_block_list_line(client):
    body = _validate(client, "name: x\ndescription: y\nnodes:\n"
                             "  - id: a\n    prompt: hi\n"
                             "  - id: b\n    prompt: hi\n    depends_on:\n      - a\n      - ghost\n")
    (err,) = body["errors"]
    assert err["code"] == "unknown_dependency"
    assert (err["line"], err["col"], err["node_id"]) == (10, 9, "b")


def test_unknown_dependency_and_unreachable(client):
    body = _validate(client, "name: x\ndescription: y\nnodes:\n"
                             "  - id: a\n    prompt: hi\n    depends_on: [ghost]\n"
                             "  - id: b\n    prompt: hi\n    depends_on: [a]\n")
    assert _codes(body["errors"]) == ["unknown_dependency", "unreachable_node"]
    unknown, unreachable = body["errors"]
    assert unknown["line"] == 6 and unknown["node_id"] == "a"
    assert unreachable["node_id"] == "b" and unreachable["line"] == 7


def test_cycle_has_node_ids_and_null_position(client):
    body = _validate(client, "name: x\ndescription: y\nnodes:\n"
                             "  - id: a\n    prompt: hi\n    depends_on: [b]\n"
                             "  - id: b\n    prompt: hi\n    depends_on: [a]\n"
                             "  - id: c\n    prompt: hi\n    depends_on: [b]\n")
    cycles = [e for e in body["errors"] if e["code"] == "cycle"]
    assert {e["node_id"] for e in cycles} == {"a", "b"}
    assert all(e["line"] is None and e["col"] is None for e in cycles)
    (tail,) = [e for e in body["errors"] if e["code"] == "unreachable_node"]
    assert tail["node_id"] == "c"


def test_undeclared_input(client):
    body = _validate(client, "name: x\ndescription: y\ninputs:\n  - name: repo\nnodes:\n"
                             "  - id: a\n    prompt: |\n      first line\n      use $INPUTS.branch\n")
    (err,) = body["errors"]
    assert err["code"] == "undeclared_input" and err["node_id"] == "a"
    assert "branch" in err["message"] and err["line"] == 9
    assert body["warnings"] == []


def test_lowercase_inputs_ref_warns_and_still_checks_declared(client):
    head = "name: x\ndescription: y\ninputs:\n  - name: repo\nnodes:\n  - id: a\n"
    body = _validate(client, head + "    prompt: use $inputs.repo\n")
    assert body["ok"]
    assert [(w["code"], w["line"]) for w in body["warnings"]] == [("inputs_ref_syntax", 7)]
    body = _validate(client, head + "    prompt: use $inputs.branch\n")
    assert _codes(body["errors"]) == ["undeclared_input"]
    assert _codes(body["warnings"]) == ["inputs_ref_syntax"]


def test_required_inputs_list_counts_as_declared(client):
    body = _validate(client, "name: x\ndescription: y\nrequired_inputs: [branch]\nnodes:\n"
                             "  - id: a\n    prompt: use $INPUTS.branch\n")
    assert body["ok"], body


def test_risky_shell_is_warning(client):
    body = _validate(client, "name: x\ndescription: y\nnodes:\n"
                             "  - id: sh\n    bash: rm -rf build\n"
                             "  - id: py\n    script: print(1)\n    runtime: uv\n"
                             "  - id: llm\n    command: review\n")
    assert body["ok"] and body["errors"] == []
    assert [(w["code"], w["node_id"]) for w in body["warnings"]] == [
        ("risky_shell", "sh"), ("risky_shell", "py"),
    ]
    assert body["warnings"][0]["line"] == 4


def test_id_available_true_false_null(client, engine):
    assert _validate(client, _OK)["id_available"] is None
    assert _validate(client, _OK, id="fresh-id")["id_available"] is True
    client.post("/definitions", json={"id": "taken", "name": "T", "yaml": _OK, "source": "user"})
    body = _validate(client, _OK, id="taken")
    assert body["id_available"] is False and not body["ok"]
    assert _codes(body["errors"]) == ["id_taken"]


def _db_snapshot(engine):
    conn = engine._def_store._conn
    tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")]
    snap = {t: conn.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0] for t in tables}
    snap["defs_max_updated_at"] = conn.execute(
        "SELECT MAX(updated_at) FROM workflow_definitions").fetchone()[0]
    snap["total_changes"] = conn.total_changes
    return snap


def test_validate_writes_nothing(client, engine):
    client.post("/definitions", json={"id": "taken", "name": "T", "yaml": _OK, "source": "user"})
    before = _db_snapshot(engine)
    assert before["workflow_definitions"] == 1
    _validate(client, _OK)
    _validate(client, _OK, id="never-saved")
    _validate(client, _OK, id="taken")
    _validate(client, "nodes: [")
    assert _db_snapshot(engine) == before
    assert client.get("/definitions/never-saved").status_code == 404


_ALIAS_BOMB = "\n".join(
    ["a: &a [x, x, x, x, x, x, x, x, x]"]
    + [f"{chr(98 + i)}: &{chr(98 + i)} [*{chr(97 + i)}, *{chr(97 + i)}, *{chr(97 + i)}, "
       f"*{chr(97 + i)}, *{chr(97 + i)}, *{chr(97 + i)}, *{chr(97 + i)}, *{chr(97 + i)}, "
       f"*{chr(97 + i)}]" for i in range(8)]
    + ["name: x", "description: y", "nodes:", "  - id: n", "    prompt: hi", "    when: *i", ""]
)


def test_alias_bomb_rejected_fast(client):
    t0 = time.monotonic()
    body = _validate(client, _ALIAS_BOMB)
    elapsed = time.monotonic() - t0
    assert elapsed < 2, elapsed
    (err,) = body["errors"]
    assert err["code"] == "yaml_parse" and "expands too large" in err["message"]


def test_recursive_alias_rejected(client):
    body = _validate(client, "a: &a [*a]\nname: x\ndescription: y\nnodes: []\n")
    assert _codes(body["errors"]) == ["yaml_parse"]


@pytest.mark.parametrize("doc", [
    "x: " + "[" * 5000 + "]" * 5000 + "\n",
    "name: x\ndescription: y\nnodes:\n  - id: a\n    prompt: hi\n    meta: "
    + "{k: " * 3000 + "v" + "}" * 3000 + "\n",
], ids=["deep-seq", "deep-map-in-node"])
def test_deep_nesting_is_yaml_parse_not_500(client, doc):
    body = _validate(client, doc)
    (err,) = body["errors"]
    assert err["code"] == "yaml_parse" and err["line"] is None


def test_legit_anchor_alias_validates(client):
    body = _validate(client, "name: x\ndescription: y\nnodes:\n"
                             "  - id: a\n    prompt: &p Say hi\n"
                             "  - id: b\n    prompt: *p\n    depends_on: [a]\n")
    assert body["ok"], body


def test_diagnostics_truncated(client):
    nodes = "".join(f"  - id: n{i}\n    prompt: hi\n    depends_on: [ghost]\n" for i in range(250))
    body = _validate(client, "name: x\ndescription: y\nnodes:\n" + nodes)
    assert len(body["errors"]) == 200
    assert body["warnings"][-1]["code"] == "truncated"
    assert "+50 more" in body["warnings"][-1]["message"]


def test_body_too_large_413(client):
    r = client.post("/definitions/validate", json={"yaml": "x" * (1024 * 1024 + 1)})
    assert r.status_code == 413


def test_bad_body_400(client):
    assert client.post("/definitions/validate", json={"yaml": 3}).status_code == 400
    assert client.post("/definitions/validate", content=b"nope").status_code == 400


def test_health_advertises_validate():
    import plugins.workflow_engine.dashboard.plugin_api as api_mod
    assert "validate" in api_mod.FEATURES


@pytest.mark.parametrize("path", sorted(_DEFAULTS.glob("*.yaml")), ids=lambda p: p.name)
def test_factory_yaml_validates_clean(client, path):
    body = _validate(client, path.read_text(encoding="utf-8"))
    assert body["errors"] == [], body["errors"]
