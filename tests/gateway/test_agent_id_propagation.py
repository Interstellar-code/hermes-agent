#!/usr/bin/env python3
"""Persistence + API surface tests for explicit agent_id propagation (#194).

agent_id is persisted as a ``_agent_id`` key inside the ``model_config`` JSON
blob (same pattern as ``_delegate_from``/``_branched_from`` — no new column)
and projected back out as ``agent_id`` by the rich session readers.

Lives in its own file (not test_api_server.py) deliberately.
"""

import asyncio
from types import SimpleNamespace

import pytest

from hermes_state import SessionDB


@pytest.fixture
def db(tmp_path):
    return SessionDB(tmp_path / "state.db")


class TestAgentIdPersistence:
    def test_persist_and_reload_via_list_sessions_rich(self, db):
        db.create_session("s-neo", "cli", model_config={"_agent_id": "neo"})
        rows = db.list_sessions_rich()
        assert len(rows) == 1
        assert rows[0]["agent_id"] == "neo"

    def test_order_by_last_active_path_projects_agent_id(self, db):
        # The gateway API always uses order_by_last_active=True — cover that
        # SELECT variant too.
        db.create_session("s-neo", "cli", model_config={"_agent_id": "neo"})
        rows = db.list_sessions_rich(order_by_last_active=True)
        assert rows[0]["agent_id"] == "neo"

    def test_pre_194_row_without_marker_is_none(self, db):
        # Simulates rows created before #194: model_config NULL or present
        # without the _agent_id key. Must read back as None, no crash.
        db.create_session("s-null-config", "cli")
        db.create_session("s-no-key", "cli", model_config={"temperature": 0.5})
        rows = {r["id"]: r for r in db.list_sessions_rich()}
        assert rows["s-null-config"]["agent_id"] is None
        assert rows["s-no-key"]["agent_id"] is None

    def test_get_session_rich_row_projects_agent_id(self, db):
        db.create_session("s-neo", "cli", model_config={"_agent_id": "neo"})
        db.create_session("s-legacy", "cli")
        assert db._get_session_rich_row("s-neo")["agent_id"] == "neo"
        assert db._get_session_rich_row("s-legacy")["agent_id"] is None


class TestParentSessionIdFilter:
    def _seed_family(self, db):
        db.create_session("parent-1", "cli")
        db.create_session(
            "child-a", "cli", parent_session_id="parent-1",
            model_config={"_agent_id": "neo", "_delegate_from": "parent-1"},
        )
        db.create_session(
            "child-b", "cli", parent_session_id="parent-1",
            model_config={"_agent_id": "trinity", "_delegate_from": "parent-1"},
        )
        db.create_session("unrelated-root", "cli")

    def test_filter_returns_only_children(self, db):
        self._seed_family(db)
        rows = db.list_sessions_rich(parent_session_id="parent-1")
        assert {r["id"] for r in rows} == {"child-a", "child-b"}
        by_id = {r["id"]: r for r in rows}
        assert by_id["child-a"]["agent_id"] == "neo"
        assert by_id["child-b"]["agent_id"] == "trinity"

    def test_filter_on_order_by_last_active_path(self, db):
        self._seed_family(db)
        rows = db.list_sessions_rich(
            parent_session_id="parent-1", order_by_last_active=True
        )
        assert {r["id"] for r in rows} == {"child-a", "child-b"}

    def test_no_filter_still_hides_delegate_children(self, db):
        # The default listing contract is unchanged: delegate children stay
        # hidden unless the caller filters by parent or opts into children.
        self._seed_family(db)
        rows = db.list_sessions_rich()
        assert {r["id"] for r in rows} == {"parent-1", "unrelated-root"}

    def test_filter_with_no_children(self, db):
        self._seed_family(db)
        assert db.list_sessions_rich(parent_session_id="unrelated-root") == []


class TestSessionResponse:
    def test_session_response_includes_agent_id(self):
        from gateway.platforms.api_server import APIServerAdapter

        payload = APIServerAdapter._session_response(
            {"id": "s1", "agent_id": "neo", "model_config": "{}"}
        )
        assert payload["agent_id"] == "neo"
        # model_config itself must NOT leak through the client API.
        assert "model_config" not in payload

    def test_session_response_passes_none_through(self):
        from gateway.platforms.api_server import APIServerAdapter

        payload = APIServerAdapter._session_response({"id": "s1", "agent_id": None})
        assert payload["agent_id"] is None


class TestListSessionsHandlerThreading:
    def test_parent_session_id_query_param_threaded(self, db):
        """GET /api/sessions?parent_session_id=... must reach
        list_sessions_rich and surface only that session's children."""
        from gateway.platforms.api_server import APIServerAdapter

        db.create_session("parent-1", "cli")
        db.create_session(
            "child-a", "cli", parent_session_id="parent-1",
            model_config={"_agent_id": "neo", "_delegate_from": "parent-1"},
        )
        db.create_session("unrelated-root", "cli")

        stub = SimpleNamespace(
            _check_auth=lambda request: None,
            _ensure_session_db=lambda: db,
            _parse_nonnegative_int=lambda value, default, maximum: default,
            _session_response=APIServerAdapter._session_response,
        )
        request = SimpleNamespace(query={"parent_session_id": "parent-1"})

        resp = asyncio.run(
            APIServerAdapter._handle_list_sessions(stub, request)
        )
        assert resp.status == 200
        import json as _json

        body = _json.loads(resp.body.decode("utf-8"))
        ids = [s["id"] for s in body["data"]]
        assert ids == ["child-a"]
        assert body["data"][0]["agent_id"] == "neo"
