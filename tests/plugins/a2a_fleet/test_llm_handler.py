"""Tests for llm_handler — no live API; monkeypatches resolve_provider_client."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, List
from unittest.mock import AsyncMock, MagicMock

import pytest

pytest.importorskip("fastapi", reason="a2a_fleet server tests require hermes-agent[web]")
pytest.importorskip("uvicorn", reason="a2a_fleet server tests require hermes-agent[web]")

from pathlib import Path

import yaml
from fastapi.testclient import TestClient


# ---------------------------------------------------------------------------
# Fake async client helpers
# ---------------------------------------------------------------------------

def _make_fake_client(reply: str = "fake-reply"):
    """Return a fake async client whose chat.completions.create returns canned content."""
    message = MagicMock()
    message.content = reply

    choice = MagicMock()
    choice.message = message

    response = MagicMock()
    response.choices = [choice]

    client = MagicMock()
    client.chat = MagicMock()
    client.chat.completions = MagicMock()
    client.chat.completions.create = AsyncMock(return_value=response)
    return client


def _make_failing_client(exc: Exception):
    """Return a fake async client whose create() raises exc."""
    client = MagicMock()
    client.chat = MagicMock()
    client.chat.completions = MagicMock()
    client.chat.completions.create = AsyncMock(side_effect=exc)
    return client


# ---------------------------------------------------------------------------
# Unit tests — llm_handler directly
# ---------------------------------------------------------------------------

@pytest.fixture
def base_cfg():
    return {
        "response_handler": "llm",
        "llm": {"max_tokens": 512, "temperature": 0.5},
    }


def test_prompt_assembly_includes_history(monkeypatch, base_cfg):
    """Prior history turns must appear in the messages sent to the model."""
    from a2a_fleet import context_store as cs
    from a2a_fleet.llm_handler import llm_handler

    ctx_id = "ctx-hist-test"
    # Pre-seed context store with a prior turn.
    cs.append(ctx_id, "user", "hello")
    cs.append(ctx_id, "assistant", "hi there")

    fake_client = _make_fake_client("response")
    monkeypatch.setattr(
        "agent.auxiliary_client.resolve_provider_client",
        lambda *a, **kw: (fake_client, "test-model"),
    )

    asyncio.get_event_loop().run_until_complete(
        llm_handler("follow-up", ctx_id, base_cfg)
    )

    call_args = fake_client.chat.completions.create.call_args
    messages: List[dict] = call_args.kwargs.get("messages") or call_args.args[0] if call_args.args else call_args.kwargs["messages"]
    # system + 2 history + new user = 4
    roles = [m["role"] for m in messages]
    assert roles[0] == "system"
    # history turns present
    assert any(m["content"] == "hello" for m in messages)
    assert any(m["content"] == "hi there" for m in messages)
    # new user turn at end
    assert messages[-1] == {"role": "user", "content": "follow-up"}


def test_successful_call_returns_handler_result_and_appends(monkeypatch, base_cfg):
    """On success, llm_handler returns HandlerResult and appends both turns."""
    from a2a_fleet import context_store as cs
    from a2a_fleet.llm_handler import llm_handler
    from a2a_fleet.response_handler import HandlerResult

    ctx_id = "ctx-success-test"
    fake_client = _make_fake_client("the-reply")
    monkeypatch.setattr(
        "agent.auxiliary_client.resolve_provider_client",
        lambda *a, **kw: (fake_client, "test-model"),
    )

    result = asyncio.get_event_loop().run_until_complete(
        llm_handler("my question", ctx_id, base_cfg)
    )

    assert isinstance(result, HandlerResult)
    assert result.text == "the-reply"
    assert result.context_id == ctx_id

    turns = cs.history(ctx_id)
    assert any(t["role"] == "user" and t["text"] == "my question" for t in turns)
    assert any(t["role"] == "assistant" and t["text"] == "the-reply" for t in turns)


def test_sequential_calls_include_first_turn(monkeypatch, base_cfg):
    """Second call on same context_id must see the first turn in outgoing messages."""
    from a2a_fleet.llm_handler import llm_handler

    ctx_id = "ctx-sequential-test"
    fake_client = _make_fake_client("reply-1")
    monkeypatch.setattr(
        "agent.auxiliary_client.resolve_provider_client",
        lambda *a, **kw: (fake_client, "test-model"),
    )

    loop = asyncio.new_event_loop()
    loop.run_until_complete(llm_handler("first-turn", ctx_id, base_cfg))

    # Second call — make the client return a different reply.
    fake_client2 = _make_fake_client("reply-2")
    monkeypatch.setattr(
        "agent.auxiliary_client.resolve_provider_client",
        lambda *a, **kw: (fake_client2, "test-model"),
    )
    loop.run_until_complete(llm_handler("second-turn", ctx_id, base_cfg))
    loop.close()

    call_args = fake_client2.chat.completions.create.call_args
    messages = call_args.kwargs.get("messages") or call_args.args[0]
    contents = [m["content"] for m in messages]
    assert "first-turn" in contents
    assert "reply-1" in contents
    assert messages[-1]["content"] == "second-turn"


def test_model_error_raises_a2a_handler_error(monkeypatch, base_cfg):
    """A failing model call must raise A2AHandlerError, not return an error result."""
    from a2a_fleet.llm_handler import A2AHandlerError, llm_handler

    ctx_id = "ctx-error-test"
    failing_client = _make_failing_client(RuntimeError("network timeout"))
    monkeypatch.setattr(
        "agent.auxiliary_client.resolve_provider_client",
        lambda *a, **kw: (failing_client, "test-model"),
    )

    with pytest.raises(A2AHandlerError):
        asyncio.get_event_loop().run_until_complete(
            llm_handler("hello", ctx_id, base_cfg)
        )


def test_unavailable_provider_raises_a2a_handler_error(monkeypatch, base_cfg):
    """resolve_provider_client returning (None, None) must raise A2AHandlerError."""
    from a2a_fleet.llm_handler import A2AHandlerError, llm_handler

    monkeypatch.setattr(
        "agent.auxiliary_client.resolve_provider_client",
        lambda *a, **kw: (None, None),
    )

    with pytest.raises(A2AHandlerError, match="unavailable"):
        asyncio.get_event_loop().run_until_complete(
            llm_handler("hello", "ctx-noauth", base_cfg)
        )


# ---------------------------------------------------------------------------
# Server-level integration tests
# ---------------------------------------------------------------------------

def _llm_fleet_yaml(response_handler: str = "llm") -> dict:
    return {
        "fleet": {
            "enabled": True,
            "self": {"name": "switch"},
            "server": {
                "bind_host": "127.0.0.1",
                "bind_port": 9319,
                "auth_required": False,
                "token_env": "SWITCH_A2A_TOKEN",
            },
            "response_handler": response_handler,
            "agents": {
                "construct": {
                    "url": "http://127.0.0.1:9320",
                    "agent_card_url": "http://127.0.0.1:9320/.well-known/agent-card.json",
                    "token_env": "CONSTRUCT_A2A_TOKEN",
                    "description": "Test peer",
                },
            },
        },
    }


@pytest.fixture
def llm_fleet_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Temp HERMES_HOME with response_handler: llm."""
    profile_dir = tmp_path / "profiles" / "switch"
    profile_dir.mkdir(parents=True)
    (profile_dir / "fleet.yaml").write_text(yaml.safe_dump(_llm_fleet_yaml("llm")))
    (tmp_path / "active_profile").write_text("switch")
    monkeypatch.setenv("HERMES_HOME", str(profile_dir))
    monkeypatch.setenv("SWITCH_A2A_TOKEN", "tok-switch")
    monkeypatch.setenv("CONSTRUCT_A2A_TOKEN", "tok-construct")
    return tmp_path


def _send_body(text: str = "hello", method: str = "SendMessage") -> dict:
    return {
        "jsonrpc": "2.0",
        "id": "t1",
        "method": method,
        "params": {"message": {"role": "user", "parts": [{"text": text}]}},
    }


def test_llm_handler_failing_client_returns_jsonrpc_error(
    llm_fleet_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When LLM client fails, /jsonrpc must return a JSON-RPC error object."""
    failing_client = _make_failing_client(RuntimeError("api down"))
    monkeypatch.setattr(
        "agent.auxiliary_client.resolve_provider_client",
        lambda *a, **kw: (failing_client, "test-model"),
    )

    from a2a_fleet.server import build_app

    with TestClient(build_app()) as client:
        resp = client.post("/jsonrpc", json=_send_body("hello"))

    assert resp.status_code == 200
    body = resp.json()
    assert "error" in body
    assert body["error"]["code"] == -32000
    assert "result" not in body


def test_unknown_handler_falls_back_to_echo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """An unknown response_handler in cfg falls back to echo without crashing."""
    profile_dir = tmp_path / "profiles" / "switch"
    profile_dir.mkdir(parents=True)
    # Write a fleet.yaml with unknown handler; load_fleet would raise FleetConfigError,
    # so we test the server dispatch by directly patching load_fleet.
    (profile_dir / "fleet.yaml").write_text(yaml.safe_dump(_llm_fleet_yaml("echo")))
    (tmp_path / "active_profile").write_text("switch")
    monkeypatch.setenv("HERMES_HOME", str(profile_dir))
    monkeypatch.setenv("SWITCH_A2A_TOKEN", "tok-switch")
    monkeypatch.setenv("CONSTRUCT_A2A_TOKEN", "tok-construct")

    from a2a_fleet import server as srv
    from a2a_fleet.fleet_config import load_fleet as real_load_fleet

    def patched_load_fleet(*a, **kw):
        cfg = real_load_fleet(*a, **kw)
        cfg["response_handler"] = "bogus-unknown"
        return cfg

    monkeypatch.setattr(srv, "load_fleet", patched_load_fleet)

    from a2a_fleet.server import build_app

    with TestClient(build_app()) as client:
        resp = client.post("/jsonrpc", json=_send_body("ping"))

    body = resp.json()
    assert body["result"]["message"]["parts"][0]["text"] == "pong"


def test_config_hotswap_echo_then_llm(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Two requests on the same app — first with echo, second with llm — both work."""
    profile_dir = tmp_path / "profiles" / "switch"
    profile_dir.mkdir(parents=True)
    fleet_yaml_path = profile_dir / "fleet.yaml"
    fleet_yaml_path.write_text(yaml.safe_dump(_llm_fleet_yaml("echo")))
    (tmp_path / "active_profile").write_text("switch")
    monkeypatch.setenv("HERMES_HOME", str(profile_dir))
    monkeypatch.setenv("SWITCH_A2A_TOKEN", "tok-switch")
    monkeypatch.setenv("CONSTRUCT_A2A_TOKEN", "tok-construct")

    from a2a_fleet.server import build_app

    with TestClient(build_app()) as client:
        # First request: echo handler.
        r1 = client.post("/jsonrpc", json=_send_body("ping"))
        assert r1.json()["result"]["message"]["parts"][0]["text"] == "pong"

        # Hot-swap to llm.
        fleet_yaml_path.write_text(yaml.safe_dump(_llm_fleet_yaml("llm")))
        fake_client = _make_fake_client("llm-response")
        monkeypatch.setattr(
            "agent.auxiliary_client.resolve_provider_client",
            lambda *a, **kw: (fake_client, "test-model"),
        )

        r2 = client.post("/jsonrpc", json=_send_body("ask something"))
        assert r2.json()["result"]["message"]["parts"][0]["text"] == "llm-response"
