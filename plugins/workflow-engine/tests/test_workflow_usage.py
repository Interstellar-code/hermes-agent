"""LB1: token usage per node/run, loop iteration rows, metadata merge,
input shapes, approval_message persistence."""
from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace

import pytest

from engine.core.executor_shared import add_usage, usage_payload
from engine.db.client import open_db
from engine.db.migrate import ensure_schema
from engine.runtime.runner import _resolve_inputs
from engine.wiring import create_engine


def _engine(tmp_path):
    return create_engine(
        db_path=str(tmp_path / "wf.db"), seed_bundled=False,
        write_manifest=False, crash_recovery=False,
    )


async def _settle(eng, run_id, statuses=("completed", "failed", "cancelled", "paused"), t=10.0):
    end = time.monotonic() + t
    while time.monotonic() < end:
        run = await eng.get_run(run_id)
        if run["status"] in statuses:
            return run
        await asyncio.sleep(0.02)
    raise AssertionError(f"run {run_id} stuck: {run}")


class FakeLlm:
    """ctx.llm stand-in returning PluginLlmCompleteResult-shaped objects."""

    def __init__(self, text="ok", model="gpt-4o", provider="openai", inp=1000, out=500):
        self.text, self.model, self.provider, self.inp, self.out = text, model, provider, inp, out

    def complete(self, messages, **kw):
        return SimpleNamespace(
            text=self.text, model=self.model, provider=self.provider,
            usage=SimpleNamespace(
                input_tokens=self.inp, output_tokens=self.out,
                total_tokens=self.inp + self.out, cache_read_tokens=0,
                cache_write_tokens=0, cost_usd=None,
            ),
        )


def test_migration_007_columns():
    with open_db(":memory:") as conn:
        ensure_schema(conn)
        assert int(conn.execute(
            "SELECT value FROM schema_meta WHERE key='schema_version'").fetchone()[0]) >= 7
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(node_runs)")}
        assert {"input_tokens", "output_tokens", "total_tokens",
                "cost_usd", "model", "provider"} <= cols


def test_usage_payload_estimates_cost_or_null():
    u = usage_payload(FakeLlm().complete([]))
    assert u["input_tokens"] == 1000 and u["output_tokens"] == 500 and u["total_tokens"] == 1500
    assert u["model"] == "gpt-4o" and u["provider"] == "openai"
    assert u["cost_usd"] == pytest.approx(0.0075)
    assert usage_payload(FakeLlm(model="auto", provider="manifest").complete([]))["cost_usd"] is None
    assert usage_payload(SimpleNamespace(text="x")) is None  # no usage reported
    total = add_usage(u, u)
    assert total["total_tokens"] == 3000 and total["cost_usd"] == pytest.approx(0.015)
    assert add_usage(u, {**u, "cost_usd": None})["cost_usd"] is None


PROMPT_YAML = """
name: usage-wf
description: d
nodes:
  - id: a
    prompt: "hello"
  - id: b
    depends_on: [a]
    prompt: "again"
"""

LOOP_YAML = """
name: loop-usage
description: d
nodes:
  - id: l
    loop:
      prompt: "iter $LOOP_INDEX"
      max_iterations: 3
      until: DONE
"""


@pytest.mark.asyncio
async def test_prompt_usage_on_node_and_run(tmp_path):
    eng = _engine(tmp_path)
    eng.set_llm(FakeLlm())
    try:
        await eng.upsert_definition("usage-wf", PROMPT_YAML)
        run = await _settle(eng, (await eng.start_run("usage-wf", {}, {}))["id"])
        assert run["status"] == "completed"
        for nr in await eng.list_node_runs(run["id"]):
            assert (nr["input_tokens"], nr["output_tokens"], nr["total_tokens"]) == (1000, 500, 1500)
            assert nr["model"] == "gpt-4o" and nr["provider"] == "openai"
            assert nr["cost_usd"] == pytest.approx(0.0075)
        assert run["usage"]["total_tokens"] == 3000
        assert run["usage"]["input_tokens"] == 2000
        assert run["usage"]["output_tokens"] == 1000
        assert run["usage"]["cost_usd"] == pytest.approx(0.015)
        listed = await eng.list_runs()
        assert listed[0]["usage"] == run["usage"]
    finally:
        await eng.shutdown()


@pytest.mark.asyncio
async def test_loop_usage_sums_iterations_and_reports_on_failure(tmp_path):
    eng = _engine(tmp_path)
    eng.set_llm(FakeLlm(text="not yet", model="auto", provider="manifest", inp=10, out=5))
    try:
        await eng.upsert_definition("loop-usage", LOOP_YAML)
        run = await _settle(eng, (await eng.start_run("loop-usage", {}, {}))["id"])
        assert run["status"] == "failed"  # never saw DONE
        nrs = await eng.list_node_runs(run["id"])
        wrapper = next(n for n in nrs if n["loop_iteration"] is None)
        iters = sorted((n for n in nrs if n["loop_iteration"]), key=lambda n: n["loop_iteration"])
        assert wrapper["status"] == "failed" and wrapper["total_tokens"] == 45
        assert wrapper["cost_usd"] is None  # unknown pricing stays null
        assert [n["loop_iteration"] for n in iters] == [1, 2, 3]
        assert all(n["status"] == "completed" and n["total_tokens"] == 15 for n in iters)
        assert all(n["loop_parent_node_run_id"] == wrapper["id"] for n in iters)
        # run sum counts the wrapper only, not wrapper + iterations
        assert run["usage"] == {"input_tokens": 30, "output_tokens": 15,
                                "total_tokens": 45, "cost_usd": None}
    finally:
        await eng.shutdown()


@pytest.mark.asyncio
async def test_retry_usage_is_additive(tmp_path):
    eng = _engine(tmp_path)
    try:
        await eng.upsert_definition("usage-wf", PROMPT_YAML)
        run = await eng.start_run("usage-wf", {}, {})
        run = await _settle(eng, run["id"])
        store = eng._run_store
        nr = store.find_node_run(run["id"], "a")
        store.add_node_usage(nr["id"], {"input_tokens": 1, "output_tokens": 2, "total_tokens": 3,
                                        "cost_usd": None, "model": "m", "provider": "p"})
        store.add_node_usage(nr["id"], {"input_tokens": 1, "output_tokens": 2, "total_tokens": 3,
                                        "cost_usd": 0.5, "model": None, "provider": None})
        nr = store.get_node_run(nr["id"])
        assert (nr["total_tokens"], nr["cost_usd"], nr["model"]) == (6, 0.5, "m")
    finally:
        await eng.shutdown()


APPROVAL_YAML = """
name: gate
description: d
nodes:
  - id: gate
    approval:
      message: "Ship it?"
"""


@pytest.mark.asyncio
async def test_approval_pause_keeps_metadata_and_stores_message(tmp_path):
    eng = _engine(tmp_path)
    try:
        await eng.upsert_definition("gate", APPROVAL_YAML)
        await eng.upsert_definition("usage-wf", PROMPT_YAML)
        trigger = {"type": "manual", "source": "test"}
        run = await eng.start_run("gate", {"x": 1}, trigger)
        run = await _settle(eng, run["id"])
        assert run["status"] == "paused"
        assert run["metadata"]["trigger"] == trigger
        assert run["metadata"]["inputs"] == {"x": 1}
        assert run["metadata"]["pause"]["message"] == "Ship it?"
        nr = eng._run_store.find_node_run(run["id"], "gate")
        assert nr["status"] == "paused" and nr["approval_message"] == "Ship it?"
        # update_workflow_run merges metadata instead of replacing it
        eng._run_store.update_workflow_run(run["id"], metadata={"note": "n"})
        meta = (await eng.get_run(run["id"]))["metadata"]
        assert meta["trigger"] == trigger and meta["note"] == "n" and "pause" in meta
    finally:
        await eng.shutdown()


def test_resolve_inputs_three_shapes():
    as_list = "inputs:\n  - name: a\n    default: 1\n  - name: b\n"
    assert _resolve_inputs(as_list, {"b": "x"}) == {"a": 1, "b": "x"}
    as_map = "inputs:\n  a:\n    type: string\n    default: 1\n  b:\n    required: true\n"
    assert _resolve_inputs(as_map, {"b": "x", "evil": 1}) == {"a": 1, "b": "x"}
    top = "required_inputs: [a]\noptional_inputs: [b]\n"
    assert _resolve_inputs(top, {"a": "x", "c": 2}) == {"a": "x"}
    mixed = "required_inputs: [a]\ninputs:\n  b:\n    default: 2\n"
    assert _resolve_inputs(mixed, {"a": "x"}) == {"a": "x", "b": 2}
    with pytest.raises(ValueError, match="reserved"):
        _resolve_inputs("inputs:\n  PATH: {}\n", {})


def test_run_cost_unknown_if_any_token_row_lacks_cost():
    from engine.store.run_store import RunStore
    with open_db(":memory:") as conn:
        ensure_schema(conn)
        conn.execute("INSERT INTO workflow_definitions (id,name,source,yaml,checksum,created_at,updated_at)"
                     " VALUES ('w','w','user','x','x',1,1)")
        conn.execute("INSERT INTO workflow_runs (id,workflow_id,conversation_id,working_path,user_message,"
                     "status,current_phase,started_at,last_heartbeat) VALUES ('r','w','c','/','m','running','p',1,1)")
        for i, cost in enumerate((0.5, None)):
            conn.execute("INSERT INTO node_runs (id,workflow_run_id,dag_node_id,node_type,status,started_at,"
                         "input_tokens,output_tokens,total_tokens,cost_usd) VALUES (?,?,?,?,?,?,1,1,2,?)",
                         (f"n{i}", "r", f"d{i}", "prompt", "completed", 1, cost))
        conn.commit()
        store = RunStore(conn)
        assert store.get_workflow_run("r")["usage"]["cost_usd"] is None
        conn.execute("UPDATE node_runs SET cost_usd = 0.25 WHERE id = 'n1'")
        conn.commit()
        assert store.get_workflow_run("r")["usage"]["cost_usd"] == pytest.approx(0.75)


def test_migration_007_half_applied_converges():
    with open_db(":memory:") as conn:
        ensure_schema(conn)
        conn.execute("ALTER TABLE node_runs DROP COLUMN provider")
        conn.execute("ALTER TABLE node_runs DROP COLUMN model")
        conn.execute("UPDATE schema_meta SET value='6' WHERE key='schema_version'")
        conn.commit()
        # simulate half-applied: some 007 columns present, version still 6
        ensure_schema(conn)
        assert int(conn.execute("SELECT value FROM schema_meta WHERE key='schema_version'").fetchone()[0]) >= 7
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(node_runs)")}
        assert {"input_tokens", "model", "provider"} <= cols

