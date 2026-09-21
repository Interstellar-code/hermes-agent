"""Tests for /handoff routing via slash.exec in tui_gateway.

#221: /handoff marks the row pending and then poll-blocks for up to 60s --
longer than the slash worker's own deadline -- so over slash.exec the worker
was always killed first and the CLI's cleanup never ran. _slash_exec_handoff
in methods_tools.py fixes this by answering /handoff in-process via the
non-blocking handoff.request RPC (already used by the desktop client), the
same way /yolo and /subgoal are answered in-process instead of falling
through to session["slash_worker"].
"""

from __future__ import annotations

import importlib
import threading
from unittest.mock import MagicMock

import pytest


@pytest.fixture()
def server(monkeypatch):
    with __import__("unittest.mock", fromlist=["patch"]).patch.dict(
        "sys.modules",
        {
            "hermes_cli.env_loader": MagicMock(),
            "hermes_cli.banner": MagicMock(),
        },
    ):
        mod = importlib.import_module("tui_gateway.server")
    yield mod
    mod._sessions.clear()
    __import__("tui_gateway.server_requests", fromlist=["x"]).reset_for_tests()


@pytest.fixture()
def session(server):
    sid = "sid-test"
    session_key = "tui-handoff-session-1"
    s = {
        "session_key": session_key,
        "history": [],
        "history_lock": threading.Lock(),
        "history_version": 0,
        "running": False,
        "attached_images": [],
        "cols": 120,
    }
    server._sessions[sid] = s
    return sid, session_key, s


def _call(server, method, **params):
    handler = server._methods[method]
    return handler(1, params)


def test_slash_exec_handoff_routes_to_handoff_request_in_process(
    server, session, monkeypatch
):
    """The bug this guards against: without the fix, slash.exec falls through
    to session["slash_worker"] (lazily spawning a _SlashWorker subprocess if
    absent) -- a separate process that always loses the race against the
    CLI's 60s poll-block (#221). Proof: handoff.request is invoked directly
    and no slash_worker is ever constructed."""
    sid, _, s = session
    assert s.get("slash_worker") is None

    calls = []

    def fake_handoff_request(rid, params):
        calls.append(params)
        return {"id": rid, "result": {"queued": True, "home_name": "telegram-home"}}

    monkeypatch.setitem(server._methods, "handoff.request", fake_handoff_request)

    r = _call(server, "slash.exec", command="handoff telegram", session_id=sid)
    assert "result" in r
    assert "Queued handoff" in r["result"]["output"]
    assert "telegram-home" in r["result"]["output"]
    assert calls == [{"session_id": sid, "platform": "telegram"}]
    assert s.get("slash_worker") is None


def test_slash_exec_handoff_no_arg_shows_usage(server, session, monkeypatch):
    sid, _, s = session
    calls = []
    monkeypatch.setitem(
        server._methods,
        "handoff.request",
        lambda rid, params: calls.append(params) or {"id": rid, "result": {}},
    )

    r = _call(server, "slash.exec", command="handoff", session_id=sid)
    assert "Usage: /handoff <platform>" in r["result"]["output"]
    assert calls == []
    assert s.get("slash_worker") is None


def test_slash_exec_handoff_propagates_rpc_error(server, session, monkeypatch):
    """handoff.request's own error (e.g. bad platform, already running) must
    pass through unchanged rather than being swallowed."""
    sid, _, _ = session
    monkeypatch.setitem(
        server._methods,
        "handoff.request",
        lambda rid, params: {"id": rid, "error": {"code": 4024, "message": "unknown platform"}},
    )

    r = _call(server, "slash.exec", command="handoff bogus", session_id=sid)
    assert "error" in r
    assert r["error"]["code"] == 4024


def test_rpc_routed_commands_includes_handoff(server):
    """Guard: _RPC_ROUTED_COMMANDS must list 'handoff' -- removing it would
    silently re-route /handoff to the slash worker (#221)."""
    assert "handoff" in server._RPC_ROUTED_COMMANDS


def test_dispatch_routed_commands_includes_subgoal(server):
    """Guard: _DISPATCH_ROUTED_COMMANDS must list 'subgoal' for parity with
    the fork's routing set, even though _PENDING_INPUT_COMMANDS already
    covers subgoal's routing today."""
    assert "subgoal" in server._DISPATCH_ROUTED_COMMANDS
