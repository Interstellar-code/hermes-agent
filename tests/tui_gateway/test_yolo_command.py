"""Tests for /yolo handling via slash.exec in tui_gateway.

#219: /yolo previously had no live-output entry, so slash.exec fell through
to session["slash_worker"] -- a separate subprocess that toggled
tools.approval._session_yolo in ITS OWN process (module-level, unpersisted)
then exited. The banner reported the safety-bypass change, but the live
agent's enforcement -- running in the serving process -- never saw it.
_live_yolo_toggle in methods_slash.py fixes this by answering /yolo
in-process, the same way /model, /personality, etc. already do.
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
    from tools.approval import disable_session_yolo

    sid = "sid-test"
    session_key = "tui-yolo-session-1"
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
    yield sid, session_key, s
    disable_session_yolo(session_key)


def test_slash_exec_yolo_toggles_in_process(server, session):
    """The bug this guards against: without a live-output entry for "yolo",
    slash.exec falls through to session["slash_worker"] (lazily spawning a
    _SlashWorker subprocess if absent) -- a different process than the one
    enforcing tool approvals. Proof: the toggle is visible via
    is_session_yolo_enabled() immediately after the call, in this process,
    and no slash_worker was ever constructed."""
    from tools.approval import is_session_yolo_enabled

    sid, session_key, s = session
    assert s.get("slash_worker") is None
    assert is_session_yolo_enabled(session_key) is False

    r = server.handle_request(
        {"id": 1, "method": "slash.exec", "params": {"command": "yolo on", "session_id": sid}}
    )
    assert "result" in r
    assert "enabled" in r["result"]["output"]
    assert is_session_yolo_enabled(session_key) is True
    assert s.get("slash_worker") is None

    r = server.handle_request(
        {"id": 2, "method": "slash.exec", "params": {"command": "yolo off", "session_id": sid}}
    )
    assert "disabled" in r["result"]["output"]
    assert is_session_yolo_enabled(session_key) is False
    assert s.get("slash_worker") is None


def test_live_slash_output_has_yolo_entry(server):
    """Guard: _LIVE_SLASH_OUTPUT must list "yolo" -- removing it would
    silently re-route /yolo to the slash worker (#219)."""
    from tui_gateway import methods_slash

    assert "yolo" in methods_slash._LIVE_SLASH_OUTPUT
