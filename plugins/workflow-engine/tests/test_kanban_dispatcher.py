"""
Tests for engine/dispatcher/kanban.py

Mocks the event bus and httpx to verify the dispatcher POSTs the correct
payload to the kanban endpoint and patches the node_run output.
"""
from __future__ import annotations

import asyncio
import json
from typing import Any, AsyncIterator, Dict, List, Optional
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from engine.dispatcher.kanban import KanbanDispatcher


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _FakeBus:
    """Fake EventBus that yields a pre-configured list of events."""

    def __init__(self, events: List[Dict[str, Any]]) -> None:
        self._events = events

    async def subscribe(self) -> AsyncIterator[Dict[str, Any]]:
        for event in self._events:
            yield event


def _make_engine(bus: _FakeBus, run_store: Optional[MagicMock] = None) -> MagicMock:
    engine = MagicMock()
    engine._bus = bus
    engine._run_store = run_store or MagicMock()
    return engine


def _node_completed_event(
    run_id: str = "run-1",
    node_run_id: str = "nr-1",
    output: Optional[Dict] = None,
) -> Dict[str, Any]:
    return {
        "run_id": run_id,
        "event_type": "node_completed",
        "node_run_id": node_run_id,
        "data": {
            "node_id": "triage-node",
            "output": output or {},
        },
    }


def _kanban_request(title: str = "Do something", **kwargs: Any) -> Dict[str, Any]:
    req: Dict[str, Any] = {"title": title}
    req.update(kwargs)
    return req


# ---------------------------------------------------------------------------
# Unit tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dispatcher_posts_kanban_task():
    """Dispatcher POSTs to kanban URL when node output has kanban_task_request."""
    req = _kanban_request(title="Triage PR", body="Please triage this PR")
    event = _node_completed_event(output={"kanban_task_request": req})

    run_store = MagicMock()
    engine = _make_engine(_FakeBus([event]), run_store)
    dispatcher = KanbanDispatcher(engine, kanban_url="http://fake/api/plugins/kanban/tasks")

    fake_resp = MagicMock()
    fake_resp.raise_for_status = MagicMock()
    fake_resp.json = MagicMock(return_value={"task": {"id": "kt-42"}})

    fake_client = AsyncMock()
    fake_client.__aenter__ = AsyncMock(return_value=fake_client)
    fake_client.__aexit__ = AsyncMock(return_value=False)
    fake_client.post = AsyncMock(return_value=fake_resp)

    with patch("engine.dispatcher.kanban.httpx.AsyncClient", return_value=fake_client):
        await dispatcher.run_forever()

    fake_client.post.assert_awaited_once()
    call_kwargs = fake_client.post.await_args
    posted_url = call_kwargs[0][0]
    posted_json = call_kwargs[1]["json"]

    assert posted_url == "http://fake/api/plugins/kanban/tasks"
    assert posted_json["title"] == "Triage PR"
    assert "run-1" in posted_json["body"]  # run_id appended


@pytest.mark.asyncio
async def test_dispatcher_patches_node_run():
    """Dispatcher calls update_node_run with the returned kanban_task_id."""
    req = _kanban_request(title="Fix bug")
    event = _node_completed_event(run_id="run-2", node_run_id="nr-99", output={"kanban_task_request": req})

    run_store = MagicMock()
    engine = _make_engine(_FakeBus([event]), run_store)
    dispatcher = KanbanDispatcher(engine, kanban_url="http://fake/api/plugins/kanban/tasks")

    fake_resp = MagicMock()
    fake_resp.raise_for_status = MagicMock()
    fake_resp.json = MagicMock(return_value={"task": {"id": "kt-99"}})

    fake_client = AsyncMock()
    fake_client.__aenter__ = AsyncMock(return_value=fake_client)
    fake_client.__aexit__ = AsyncMock(return_value=False)
    fake_client.post = AsyncMock(return_value=fake_resp)

    with patch("engine.dispatcher.kanban.httpx.AsyncClient", return_value=fake_client):
        await dispatcher.run_forever()

    run_store.update_node_run.assert_called_once_with("nr-99", {"kanban_task_id": "kt-99"})


@pytest.mark.asyncio
async def test_dispatcher_ignores_events_without_kanban_request():
    """Dispatcher does not POST if node output lacks kanban_task_request."""
    event = _node_completed_event(output={"some_other_field": "value"})

    run_store = MagicMock()
    engine = _make_engine(_FakeBus([event]), run_store)
    dispatcher = KanbanDispatcher(engine, kanban_url="http://fake/api/plugins/kanban/tasks")

    with patch("engine.dispatcher.kanban.httpx.AsyncClient") as mock_client_cls:
        await dispatcher.run_forever()

    mock_client_cls.assert_not_called()
    run_store.update_node_run.assert_not_called()


@pytest.mark.asyncio
async def test_dispatcher_ignores_non_node_completed_events():
    """Dispatcher skips events that are not node_completed."""
    events = [
        {"run_id": "run-1", "event_type": "node_started", "node_run_id": "nr-1", "data": {"output": {"kanban_task_request": {"title": "x"}}}},
        {"run_id": "run-1", "event_type": "run_completed", "node_run_id": None, "data": {}},
    ]
    run_store = MagicMock()
    engine = _make_engine(_FakeBus(events), run_store)
    dispatcher = KanbanDispatcher(engine, kanban_url="http://fake/api/plugins/kanban/tasks")

    with patch("engine.dispatcher.kanban.httpx.AsyncClient") as mock_client_cls:
        await dispatcher.run_forever()

    mock_client_cls.assert_not_called()


@pytest.mark.asyncio
async def test_dispatcher_continues_after_http_error():
    """HTTP error on one event does not crash the dispatcher loop."""
    req = _kanban_request(title="Task A")
    events = [
        _node_completed_event(run_id="run-1", node_run_id="nr-1", output={"kanban_task_request": req}),
        _node_completed_event(run_id="run-2", node_run_id="nr-2", output={"kanban_task_request": {"title": "Task B"}}),
    ]

    run_store = MagicMock()
    engine = _make_engine(_FakeBus(events), run_store)
    dispatcher = KanbanDispatcher(engine, kanban_url="http://fake/api/plugins/kanban/tasks")

    call_count = 0

    async def _fake_post(*args: Any, **kwargs: Any) -> MagicMock:
        nonlocal call_count
        call_count += 1
        resp = MagicMock()
        if call_count == 1:
            resp.raise_for_status = MagicMock(side_effect=RuntimeError("HTTP 503"))
        else:
            resp.raise_for_status = MagicMock()
            resp.json = MagicMock(return_value={"task": {"id": "kt-2"}})
        return resp

    fake_client = AsyncMock()
    fake_client.__aenter__ = AsyncMock(return_value=fake_client)
    fake_client.__aexit__ = AsyncMock(return_value=False)
    fake_client.post = _fake_post

    with patch("engine.dispatcher.kanban.httpx.AsyncClient", return_value=fake_client):
        await dispatcher.run_forever()

    # Second event was still processed
    run_store.update_node_run.assert_called_once_with("nr-2", {"kanban_task_id": "kt-2"})


@pytest.mark.asyncio
async def test_dispatcher_forwards_optional_fields():
    """Fields like assignee, triage, skills are forwarded from the request."""
    req = _kanban_request(
        title="Triage",
        body="body text",
        assignee="worker-1",
        triage=True,
        skills=["python"],
        priority=1,
    )
    event = _node_completed_event(output={"kanban_task_request": req})

    run_store = MagicMock()
    engine = _make_engine(_FakeBus([event]), run_store)
    dispatcher = KanbanDispatcher(engine, kanban_url="http://fake/api/plugins/kanban/tasks")

    fake_resp = MagicMock()
    fake_resp.raise_for_status = MagicMock()
    fake_resp.json = MagicMock(return_value={"task": {"id": "kt-5"}})

    fake_client = AsyncMock()
    fake_client.__aenter__ = AsyncMock(return_value=fake_client)
    fake_client.__aexit__ = AsyncMock(return_value=False)
    fake_client.post = AsyncMock(return_value=fake_resp)

    with patch("engine.dispatcher.kanban.httpx.AsyncClient", return_value=fake_client):
        await dispatcher.run_forever()

    posted_json = fake_client.post.await_args[1]["json"]
    assert posted_json["assignee"] == "worker-1"
    assert posted_json["triage"] is True
    assert posted_json["skills"] == ["python"]
    assert posted_json["priority"] == 1
