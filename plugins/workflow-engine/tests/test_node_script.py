"""Tests for engine/nodes/script.py."""

from __future__ import annotations

import pytest
from unittest.mock import AsyncMock

from engine.nodes.script import execute_script_node
from engine.core.dag_executor import DagRunContext
from engine.schemas.dag_node import validate_dag_node


def _parse_node(data):
    node, errors = validate_dag_node(data, 0)
    if errors:
        raise ValueError(f"Invalid node: {errors}")
    return node


def _make_ctx():
    events = []
    ctx = DagRunContext(
        run_id="test-run",
        emit_event=lambda t, p: events.append((t, p)),
        get_run_status=AsyncMock(return_value="running"),
        pause_run=AsyncMock(),
        cancel_run=AsyncMock(),
        send_message=AsyncMock(),
        get_subgraph_yaml=lambda ref: None,
    )
    return ctx, events


@pytest.mark.asyncio
async def test_script_node_rejects_user_message_guard():
    """A script node containing $USER_MESSAGE must fail via guard."""
    node = _parse_node({
        "id": "bad-script-node",
        "script": "import os\nprint('$USER_MESSAGE')\n",
        "runtime": "uv",
    })
    ctx, events = _make_ctx()
    ctx.workflow_vars = {"user_message": "hello"}
    result = await execute_script_node(node, {}, ctx)
    assert result.state == "failed"
    assert "bad-script-node" in (result.error or "")
    assert "$USER_MESSAGE" in (result.error or "")
    assert 'os.environ["USER_MESSAGE"]' in (result.error or "")
    assert "node_failed" in [e[0] for e in events]


@pytest.mark.asyncio
async def test_script_node_user_message_env_injection():
    """A script node reading os.environ['USER_MESSAGE'] receives the environment value."""
    node = _parse_node({
        "id": "env-script-node",
        "script": "import os\nprint(os.environ.get('USER_MESSAGE'))\n",
        "runtime": "uv",
    })
    ctx, events = _make_ctx()
    ctx.workflow_vars = {"user_message": "secret_user_input_42"}
    result = await execute_script_node(node, {}, ctx)
    assert result.state == "completed"
    assert result.output == "secret_user_input_42"
    assert "node_completed" in [e[0] for e in events]


@pytest.mark.asyncio
async def test_script_node_artifacts_dir_substitution():
    """$ARTIFACTS_DIR in a script body still substitutes normally."""
    node = _parse_node({
        "id": "artifacts-script-node",
        "script": "print('$ARTIFACTS_DIR')\n",
        "runtime": "uv",
    })
    ctx, events = _make_ctx()
    ctx.workflow_vars = {"artifacts_dir": "/tmp/test_artifacts_dir_path"}
    result = await execute_script_node(node, {}, ctx)
    assert result.state == "completed"
    assert result.output == "/tmp/test_artifacts_dir_path"
    assert "node_completed" in [e[0] for e in events]
