"""
Regression tests for #85B: workflow_approve/workflow_cancel must default-deny
when the caller has no session key, instead of silently allowing it.
"""
from __future__ import annotations

import pytest
from unittest.mock import AsyncMock, MagicMock


@pytest.fixture()
def fake_engine(monkeypatch):
    engine = MagicMock()
    engine.get_run = AsyncMock(return_value={
        "status": "running",
        "owner_session": "session-owner-123",
    })
    engine.approve = AsyncMock()
    engine.cancel_run = AsyncMock()
    monkeypatch.setattr("plugins.workflow_engine._shared._engine", engine)
    return engine


@pytest.fixture(autouse=True)
def no_approve_any(monkeypatch):
    monkeypatch.setattr(
        "plugins.workflow_engine.tools.approve_workflow._approve_any", lambda: False
    )
    monkeypatch.setattr(
        "plugins.workflow_engine.tools.cancel_workflow._approve_any", lambda: False
    )


@pytest.mark.asyncio
async def test_approve_denied_when_no_session_key(fake_engine):
    """#85B: _session_key=None, approve_any=False, owner_session set -> DENIED (was allowed before)."""
    from plugins.workflow_engine.tools.approve_workflow import _handler_impl

    result = await _handler_impl(
        {"run_id": "run-1", "node_id": "n1", "decision": "approve"},
        _session_key=None,
    )
    assert result["ok"] is False
    assert "approve_any" in result["error"]
    fake_engine.approve.assert_not_called()


@pytest.mark.asyncio
async def test_cancel_denied_when_no_session_key(fake_engine):
    """Same default-deny inversion applied to workflow_cancel."""
    from plugins.workflow_engine.tools.cancel_workflow import _handler_impl

    result = await _handler_impl({"run_id": "run-1"}, _session_key=None)
    assert result["ok"] is False
    assert "approve_any" in result["error"]
    fake_engine.cancel_run.assert_not_called()


@pytest.mark.asyncio
async def test_approve_allowed_when_owner_matches(fake_engine):
    """Sanity: legitimate owner session is still allowed through."""
    from plugins.workflow_engine.tools.approve_workflow import _handler_impl

    result = await _handler_impl(
        {"run_id": "run-1", "node_id": "n1", "decision": "approve"},
        _session_key="session-owner-123",
    )
    assert result["ok"] is True
    fake_engine.approve.assert_called_once()
