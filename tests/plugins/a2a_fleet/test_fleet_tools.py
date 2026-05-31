"""Tests for fleet_send_handler — the agent-facing A2A send tool.

The critical coverage gap that let a TypeError reach a live Hermes agent:
``registry.dispatch()`` invokes ``handler(args, **kwargs)`` — it passes the
WHOLE args dict as the first positional and injects ``task_id`` as a kwarg. The
handler must unwrap that dict shape (not assume spread kwargs) or it raises
``TypeError: missing 1 required positional argument: 'message'`` before its own
logic runs.
"""
from __future__ import annotations

import asyncio
from typing import Any, Dict

import a2a_fleet.fleet_tools as fleet_tools


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


def _patch_send(monkeypatch, captured: Dict[str, Any]):
    async def fake_send(agent_name, text, *, context_id=None, **_kw):
        captured["agent"] = agent_name
        captured["text"] = text
        captured["context_id"] = context_id
        return {"reply": "pong", "context_id": context_id or "ctx-generated"}

    monkeypatch.setattr(fleet_tools, "send_message", fake_send)


# ---------------------------------------------------------------------------
# The live gateway dispatch shape: handler(args_dict, task_id=...)
# ---------------------------------------------------------------------------

def test_dispatch_shape_dict_first_positional_with_injected_task_id(monkeypatch):
    """registry.dispatch passes the whole args dict positionally + injects
    task_id. The handler must unwrap it and NOT raise TypeError."""
    captured: Dict[str, Any] = {}
    _patch_send(monkeypatch, captured)

    res = _run(
        fleet_tools.fleet_send_handler(
            {"agent": "claude-code", "message": "hello", "context_id": "ctx-1"},
            task_id="t-99",  # gateway-injected kwarg
        )
    )

    assert res == {"reply": "pong", "context_id": "ctx-1"}
    assert captured == {"agent": "claude-code", "text": "hello", "context_id": "ctx-1"}


def test_dispatch_shape_dict_without_context_id(monkeypatch):
    """Optional context_id absent from the dict → passed through as None so the
    server generates one and returns it."""
    captured: Dict[str, Any] = {}
    _patch_send(monkeypatch, captured)

    res = _run(
        fleet_tools.fleet_send_handler(
            {"agent": "claude-code", "message": "hi"}, task_id="t-1"
        )
    )

    assert res["reply"] == "pong"
    assert res["context_id"] == "ctx-generated"
    assert captured["context_id"] is None


# ---------------------------------------------------------------------------
# Backward-compat: direct kwarg-style calls (tests / internal callers)
# ---------------------------------------------------------------------------

def test_kwarg_style_still_works(monkeypatch):
    captured: Dict[str, Any] = {}
    _patch_send(monkeypatch, captured)

    res = _run(
        fleet_tools.fleet_send_handler(agent="peer", message="yo", context_id="c2")
    )

    assert res == {"reply": "pong", "context_id": "c2"}
    assert captured["agent"] == "peer" and captured["text"] == "yo"


# ---------------------------------------------------------------------------
# Validation: missing required fields return an error dict, never raise
# ---------------------------------------------------------------------------

def test_missing_message_returns_error_dict_not_raise(monkeypatch):
    captured: Dict[str, Any] = {}
    _patch_send(monkeypatch, captured)

    res = _run(fleet_tools.fleet_send_handler({"agent": "claude-code"}, task_id="t-1"))

    assert "error" in res and "message" in res["error"]
    assert captured == {}  # send_message never called


def test_missing_agent_returns_error_dict(monkeypatch):
    captured: Dict[str, Any] = {}
    _patch_send(monkeypatch, captured)

    res = _run(fleet_tools.fleet_send_handler({"message": "orphan"}, task_id="t-1"))

    assert "error" in res
    assert captured == {}


def test_non_str_agent_returns_error_dict(monkeypatch):
    """A doubly-wrapped / malformed agent value must not reach send_message as a
    non-string and blow up downstream — caught with a clean error dict."""
    captured: Dict[str, Any] = {}
    _patch_send(monkeypatch, captured)

    res = _run(
        fleet_tools.fleet_send_handler(
            {"agent": {"nested": "bad"}, "message": "hi"}, task_id="t-1"
        )
    )

    assert "error" in res and "string" in res["error"]
    assert captured == {}
