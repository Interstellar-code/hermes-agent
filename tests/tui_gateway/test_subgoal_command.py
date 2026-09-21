"""Tests for /subgoal handling in tui_gateway.

/subgoal was registered in COMMAND_REGISTRY (so the palette offered it) but
had no branch in ``command.dispatch``'s ``_SLASH_BUILTINS`` table and was
absent from ``_PENDING_INPUT_COMMANDS``, so every non-CLI surface got a 4018
"not a ... command" error, and ``slash.exec`` fell through to the separate
slash-worker subprocess. Routing it to the worker would be actively wrong:
the post-turn judge (running in this serving process) writes
turns_used/status/last_verdict to the same goal state a worker-side write
would race and clobber. Both must answer in-process, like /goal and /loop.
"""

from __future__ import annotations

import importlib
import threading
from pathlib import Path
from unittest.mock import MagicMock

import pytest


@pytest.fixture()
def hermes_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(home))

    from hermes_cli import goals

    goals._DB_CACHE.clear()
    yield home
    goals._DB_CACHE.clear()


@pytest.fixture()
def server(hermes_home, monkeypatch):
    with __import__("unittest.mock", fromlist=["patch"]).patch.dict(
        "sys.modules",
        {
            "hermes_cli.env_loader": MagicMock(),
            "hermes_cli.banner": MagicMock(),
        },
    ):
        mod = importlib.import_module("tui_gateway.server")
    monkeypatch.setattr(mod, "_hermes_home", hermes_home)
    monkeypatch.setattr(mod, "_cfg_cache", None)
    monkeypatch.setattr(mod, "_cfg_mtime", None)
    monkeypatch.setattr(mod, "_cfg_path", None)
    yield mod
    mod._sessions.clear()
    __import__("tui_gateway.server_requests", fromlist=["x"]).reset_for_tests()


@pytest.fixture()
def session(server):
    sid = "sid-test"
    session_key = "tui-subgoal-session-1"
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


def test_subgoal_bare_shows_status_when_no_goal(server, session):
    sid, _, _ = session
    r = _call(server, "command.dispatch", name="subgoal", arg="", session_id=sid)
    assert r["result"]["type"] == "exec"
    assert "No active goal" in r["result"]["output"]


def test_subgoal_add_and_list_round_trip(server, session):
    from hermes_cli.goals import GoalManager

    sid, session_key, _ = session
    GoalManager(session_key).set("finish the benchmark", max_turns=10)

    r = _call(
        server, "command.dispatch", name="subgoal", arg="tests pass", session_id=sid
    )
    assert r["result"]["type"] == "exec"
    assert "Added subgoal 1: tests pass" in r["result"]["output"]

    r = _call(server, "command.dispatch", name="subgoal", arg="", session_id=sid)
    assert "tests pass" in r["result"]["output"]


def test_subgoal_remove_and_clear(server, session):
    from hermes_cli.goals import GoalManager

    sid, session_key, _ = session
    GoalManager(session_key).set("finish the benchmark", max_turns=10)
    _call(server, "command.dispatch", name="subgoal", arg="tests pass", session_id=sid)

    r = _call(
        server, "command.dispatch", name="subgoal", arg="remove 1", session_id=sid
    )
    assert "Removed subgoal 1" in r["result"]["output"]

    _call(server, "command.dispatch", name="subgoal", arg="another one", session_id=sid)
    r = _call(server, "command.dispatch", name="subgoal", arg="clear", session_id=sid)
    assert "Cleared 1 subgoal" in r["result"]["output"]


def test_slash_exec_routes_subgoal_to_command_dispatch(server, session):
    """The bug this guards against: without the fix, slash.exec falls through
    to session["slash_worker"] (lazily spawning a _SlashWorker subprocess if
    absent) — a separate process whose write would race the post-turn judge's
    write to the same goal state. Because "subgoal" is in
    _PENDING_INPUT_COMMANDS, slash.exec's `target = base if base in
    _PENDING_INPUT_COMMANDS else _bundle_key_for(base)` resolves target and
    returns via command.dispatch before ever reaching `session.get(
    "slash_worker")` (tui_gateway/methods_tools.py:900-942). Proof: the
    session has no slash_worker before OR after the call — it was never
    lazily constructed."""
    sid, _, s = session
    assert s.get("slash_worker") is None

    r = _call(server, "slash.exec", command="subgoal", session_id=sid)
    assert "result" in r
    assert r["result"]["type"] == "exec"
    assert "No active goal" in r["result"]["output"]

    assert s.get("slash_worker") is None


def test_pending_input_commands_includes_subgoal(server):
    """Guard: _PENDING_INPUT_COMMANDS must list 'subgoal' — removing it
    would silently re-route /subgoal to the slash worker (#222)."""
    assert "subgoal" in server._PENDING_INPUT_COMMANDS


def test_slash_builtins_includes_subgoal(server):
    """Guard: _SLASH_BUILTINS must list 'subgoal' — removing it would
    silently re-break command.dispatch with a 4018 error."""
    from tui_gateway import methods_tools

    assert "subgoal" in methods_tools._SLASH_BUILTINS
