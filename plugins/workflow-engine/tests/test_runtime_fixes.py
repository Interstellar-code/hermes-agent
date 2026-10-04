"""Real-engine regression tests for the F2 runtime fixes (L2-01/06/07/08/09/13/17)."""
from __future__ import annotations

import asyncio
import os
import shutil
import threading
import time
from pathlib import Path

import pytest

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


INPUTS_YAML = """
name: inputs-wf
description: d
inputs:
  - name: repo_path
    required: true
  - name: flavour
    default: vanilla
nodes:
  - id: show
    bash: 'echo "$repo_path|$flavour|$(pwd -P)"'
"""


@pytest.mark.asyncio
async def test_inputs_and_cwd_reach_bash_node(tmp_path):
    """L2-01: declared inputs → env, defaults applied, subprocess cwd = working_path,
    run log not written into the user's dir."""
    work = tmp_path / "repo"
    work.mkdir()
    eng = _engine(tmp_path)
    try:
        await eng.upsert_definition("inputs-wf", INPUTS_YAML)
        run = await eng.start_run(
            "inputs-wf", {"repo_path": "/x y", "PATH": "/evil"},
            {"working_path": str(work)},
        )
        run = await _settle(eng, run["id"])
        assert run["status"] == "completed", run
        nr = (await eng.list_node_runs(run["id"]))[0]
        assert nr["summary"] == f"/x y|vanilla|{work.resolve()}"
        assert list(work.iterdir()) == []  # no <run_id>.log in the user's dir
    finally:
        await eng.shutdown()


@pytest.mark.asyncio
@pytest.mark.skipif(shutil.which("uv") is None, reason="uv not installed")
async def test_script_node_output_passed_via_env_not_source(tmp_path):
    """L2-08: $node.output in a script body is refused; NODE_<ID>_OUTPUT env carries it."""
    from engine.core.dag_executor import DagRunContext
    from engine.nodes.script import execute_script_node
    from engine.schemas.dag_node import validate_dag_node
    from engine.schemas.workflow_run import make_node_output
    from unittest.mock import AsyncMock

    ctx = DagRunContext(
        run_id="r", emit_event=lambda *a: None, get_run_status=AsyncMock(),
        pause_run=AsyncMock(), cancel_run=AsyncMock(), send_message=AsyncMock(),
        get_subgraph_yaml=lambda r: None,
    )
    payload = 'x"""; import os; print(os.environ)'
    outs = {"produce": make_node_output("completed", payload)}
    bad, _ = validate_dag_node({"id": "bad", "runtime": "uv", "script": 'v = """$produce.output"""'}, 0)
    r = await execute_script_node(bad, outs, ctx)
    assert r.state == "failed" and "NODE_PRODUCE_OUTPUT" in r.error
    good, _ = validate_dag_node({
        "id": "good", "runtime": "uv",
        "script": 'import os\nprint(os.environ["NODE_PRODUCE_OUTPUT"] == %r)' % payload,
    }, 0)
    r = await execute_script_node(good, outs, ctx)
    assert (r.state, r.output) == ("completed", "True")


def test_bundled_scripts_do_not_splice_node_outputs():
    import yaml
    from engine.core.executor_shared import NODE_OUTPUT_REF_RE
    for f in (Path(__file__).parent.parent / "defaults").glob("*.yaml"):
        for n in yaml.safe_load(f.read_text()).get("nodes") or []:
            if "script" in n:
                assert not NODE_OUTPUT_REF_RE.search(n["script"]), (f.name, n["id"])


SLOW_YAML = """
name: slow
description: d
nodes:
  - id: a
    bash: sleep 0.3; echo a
  - id: b
    depends_on: [a]
    bash: echo b
"""


def test_run_outlives_tool_call_loop(tmp_path):
    """L2-07: a run started from a throwaway per-call loop keeps progressing
    after that loop is gone."""
    eng = _engine(tmp_path)
    try:
        holder = {}

        def tool_call():  # mimics model_tools._run_async on a worker thread
            async def go():
                await eng.upsert_definition("slow", SLOW_YAML)
                return await eng.start_run("slow", {}, {})
            holder["run"] = asyncio.run(go())

        t = threading.Thread(target=tool_call)
        t.start()
        t.join()
        run_id = holder["run"]["id"]
        run = asyncio.run(_settle(eng, run_id, ("completed", "failed", "cancelled")))
        assert run["status"] == "completed"
    finally:
        asyncio.run(eng.shutdown())


LOOP_YAML = """
name: iloop
description: d
nodes:
  - id: l
    loop:
      prompt: "iter $LOOP_INDEX prev=$LOOP_PREV_OUTPUT"
      max_iterations: 3
      interactive: true
      gate_message: "ok?"
  - id: after
    depends_on: [l]
    bash: 'echo done'
"""


@pytest.mark.asyncio
async def test_interactive_loop_gate_pause_and_resume(tmp_path):
    """L2-06: gate marks node_run paused; approve resumes at the next iteration."""
    eng = _engine(tmp_path)
    try:
        await eng.upsert_definition("iloop", LOOP_YAML)
        run = await eng.start_run("iloop", {}, {})
        run = await _settle(eng, run["id"])
        assert run["status"] == "paused"
        nrs = await eng.list_node_runs(run["id"])
        assert [n["status"] for n in nrs if n["loop_iteration"] is None] == ["paused"]
        assert [(n["loop_iteration"], n["status"]) for n in nrs if n["loop_iteration"]] == [(1, "completed")]
        await eng.approve(run["id"], "l", "approve")
        run = await _settle(eng, run["id"])
        assert run["status"] == "paused"  # second gate, after iteration index 1
        assert run["metadata"]["pause"]["iteration"] == 2
        with pytest.raises(ValueError, match="approval"):  # L2-09: no gate bypass
            await eng.resume_run(run["id"])
        await eng.approve(run["id"], "l", "approve")
        run = await _settle(eng, run["id"])
        assert run["status"] == "completed", run
        nrs = await eng.list_node_runs(run["id"])
        loop_nr = next(n for n in nrs if n["dag_node_id"] == "l" and n["loop_iteration"] is None)
        assert loop_nr["summary"].startswith("iter 2 prev=iter 1")
        iters = sorted((n for n in nrs if n["loop_iteration"]), key=lambda n: n["loop_iteration"])
        assert [(n["loop_iteration"], n["status"]) for n in iters] == [(1, "completed"), (2, "completed"), (3, "completed")]
        assert all(n["loop_parent_node_run_id"] == loop_nr["id"] and n["completed_at"] for n in iters)
    finally:
        await eng.shutdown()


@pytest.mark.asyncio
async def test_resume_run_not_paused_raises_and_reject_is_cas(tmp_path):
    """L2-09 / L2-13."""
    eng = _engine(tmp_path)
    try:
        await eng.upsert_definition("slow", SLOW_YAML)
        run = await eng.start_run("slow", {}, {})
        run = await _settle(eng, run["id"])
        with pytest.raises(ValueError):
            await eng.resume_run(run["id"])
        # paused without a gate (e.g. external pause) → real resume runs the DAG
        eng._run_store._conn.execute(
            "UPDATE workflow_runs SET status='paused' WHERE id=?", (run["id"],))
        eng._run_store._conn.execute(
            "UPDATE node_runs SET status='failed' WHERE dag_node_id='b'")
        eng._run_store._conn.commit()
        assert (await eng.resume_run(run["id"]))["status"] == "running"
        run = await _settle(eng, run["id"])
        assert run["status"] == "completed"
        assert eng._fail_run(run["id"], "late reject", ("paused",)) is False
        assert (await eng.get_run(run["id"]))["status"] == "completed"
    finally:
        await eng.shutdown()


@pytest.mark.asyncio
async def test_bash_timeout_kills_process_group(tmp_path):
    """L2-17: grandchildren die with the timed-out node."""
    from unittest.mock import AsyncMock
    from engine.core.dag_executor import DagRunContext
    from engine.nodes.bash import execute_bash_node
    from engine.schemas.dag_node import validate_dag_node

    pidfile = tmp_path / "child.pid"
    node, _ = validate_dag_node(
        {"id": "h", "timeout": 300, "bash": f"sleep 30 & echo $! > {pidfile}; wait"}, 0,
    )
    ctx = DagRunContext(
        run_id="r", emit_event=lambda *a: None, get_run_status=AsyncMock(),
        pause_run=AsyncMock(), cancel_run=AsyncMock(), send_message=AsyncMock(),
        get_subgraph_yaml=lambda r: None,
    )
    r = await execute_bash_node(node, {}, ctx)
    assert r.state == "failed" and "timed out" in r.error
    pid = int(pidfile.read_text())
    time.sleep(0.1)
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)  # windows-footgun: ok — POSIX-only test


def test_engine_binds_register_home_not_call_time_home(tmp_path, monkeypatch):
    """Review #1: register() under profile A, get_engine() later with no
    override (process HERMES_HOME = B) must bind A's DB."""
    import plugins.workflow_engine._shared as shared
    from hermes_constants import set_hermes_home_override, reset_hermes_home_override
    from unittest.mock import MagicMock

    home_a, home_b = tmp_path / "a", tmp_path / "b"
    monkeypatch.delenv("WORKFLOW_DB_PATH", raising=False)
    monkeypatch.setenv("HERMES_HOME", str(home_b))
    monkeypatch.setattr(shared, "_engines", {})
    create = MagicMock()
    monkeypatch.setattr(shared, "create_engine", create)
    tok = set_hermes_home_override(home_a)
    try:
        shared.bind_register_home()
    finally:
        reset_hermes_home_override(tok)
    shared.get_engine()
    create.assert_called_once_with(str(home_a / "switchui-workflows.db"))


def test_reserved_input_names_and_ref_safe_substitution():
    """Review #7/#8."""
    from engine.core.executor_shared import substitute_inputs
    from engine.runtime.runner import _resolve_inputs
    with pytest.raises(ValueError, match="reserved"):
        _resolve_inputs("inputs:\n  - name: PATH\n", {"PATH": "/evil"})
    from engine.core.executor_shared import substitute_node_output_refs
    from engine.schemas.workflow_run import make_node_output
    outs = {"llm-review": make_node_output("completed", "REVIEW")}
    # node refs first, then inputs: an input value can't become a node ref,
    # and an input named like a node id can't rewrite `$x.output`.
    text = substitute_node_output_refs("$a / $llm-review.output / $llm", outs)
    out = substitute_inputs(text, {"a": "$llm-review.output", "llm": "X"})
    assert out == "$llm-review.output / REVIEW / X"
    assert substitute_inputs("$llm-review.output", {"llm": "X"}) == "$llm-review.output"
