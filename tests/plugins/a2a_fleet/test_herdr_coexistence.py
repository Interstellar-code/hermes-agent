"""Coexistence: a herdr_session peer registered alongside a normal HTTP peer
must not perturb the HTTP path, and a Herdr tool-registration fault must not
take down fleet_send / the managed-executor deploy tools.

This is the acceptance evidence for Phase 1's "existing behavior unchanged"
claim. Mirrors test_sync_register.py's importorskip guard (register() returns
early — no tools registered at all — unless fastapi/uvicorn are installed).
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Dict

import pytest
import yaml

pytest.importorskip("fastapi", reason="a2a_fleet.register requires fastapi")
pytest.importorskip("uvicorn", reason="a2a_fleet.register requires uvicorn")

import a2a_fleet
import a2a_fleet.client as client


def _fleet_yaml_path(fleet_home: Path) -> Path:
    return fleet_home / "profiles" / "switch" / "fleet.yaml"


def _load_data(fleet_home: Path) -> Dict[str, Any]:
    return yaml.safe_load(_fleet_yaml_path(fleet_home).read_text())


def _write_data(fleet_home: Path, data: Dict[str, Any]) -> None:
    _fleet_yaml_path(fleet_home).write_text(yaml.safe_dump(data))


def _add_herdr_peer_and_host(fleet_home: Path) -> None:
    """Extend the base fleet.yaml (HTTP 'construct' peer already present) with
    a herdr_session peer + matching fleet.herdr.hosts entry."""
    data = _load_data(fleet_home)
    workspace = "/srv/workspaces/project-a"
    herdr_block = data["fleet"].setdefault("herdr", {})
    hosts = herdr_block.setdefault("hosts", {})
    hosts["mac-mini"] = {
        "transport": "local_socket",
        "allowed_workspaces": [workspace],
    }
    data["fleet"]["agents"]["herdr-mac-mini"] = {
        "managed": True,
        "mode": "herdr_session",
        "host_alias": "mac-mini",
        "workspace": workspace,
        "agent_kind": "claude_code",
        "transport": "local_socket",
    }
    _write_data(fleet_home, data)


class _RecordingCtx:
    """Stub ctx that records every registered tool name (mirrors
    test_sync_register.py's _StubCtx, plus a name log for assertions)."""

    def __init__(self) -> None:
        self.registered: list[str] = []

    def register_tool(self, **kwargs: Any) -> None:
        self.registered.append(kwargs.get("name", ""))

    def register_platform(self, **kwargs: Any) -> None:
        pass


class _HerdrRaisingCtx(_RecordingCtx):
    """Same as _RecordingCtx, but register_tool raises for any tool name
    starting with 'herdr_' — simulating a fault in Herdr tool registration
    without touching fleet_send / the deploy tools registered earlier."""

    def register_tool(self, **kwargs: Any) -> None:
        name = kwargs.get("name", "")
        if name.startswith("herdr_"):
            raise RuntimeError(f"boom: herdr tool registration fault for {name!r}")
        super().register_tool(**kwargs)


class _StubResponse:
    def __init__(self, status_code: int, json_body: Dict[str, Any]) -> None:
        self.status_code = status_code
        self._json_body = json_body
        self.text = str(json_body)

    def json(self) -> Dict[str, Any]:
        return self._json_body


class _StubAsyncClient:
    """Replaces httpx.AsyncClient inside a2a_fleet.client for the HTTP peer
    send path, since fleet_send_handler -> send_message() never passes a
    client= override (no existing httpx.MockTransport injection point)."""

    last_request: Dict[str, Any] = {}

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        pass

    async def post(self, url: str, json: Dict[str, Any] | None = None, headers: Dict[str, Any] | None = None):
        _StubAsyncClient.last_request = {"url": url, "json": json, "headers": headers}
        message_id = (json or {}).get("id", "req-1")
        return _StubResponse(
            200,
            {
                "jsonrpc": "2.0",
                "id": message_id,
                "result": {
                    "kind": "message",
                    "message": {
                        "contextId": "ctx-http-peer",
                        "parts": [{"text": "pong"}],
                    },
                },
            },
        )

    async def aclose(self) -> None:
        pass


def test_fleet_send_to_http_peer_unaffected_when_herdr_registered(
    fleet_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _add_herdr_peer_and_host(fleet_home)

    a2a_fleet._server_thread = None
    ctx = _RecordingCtx()
    a2a_fleet.register(ctx)

    assert "fleet_send" in ctx.registered
    assert "herdr_status" in ctx.registered
    assert "herdr_list_sessions" in ctx.registered
    assert "herdr_inspect_session" in ctx.registered

    monkeypatch.setattr(client.httpx, "AsyncClient", _StubAsyncClient)

    import a2a_fleet.fleet_tools as fleet_tools
    import asyncio

    result = asyncio.run(fleet_tools.fleet_send_handler(agent="construct", message="ping"))

    assert result == {"reply": "pong", "context_id": "ctx-http-peer"}
    assert _StubAsyncClient.last_request["url"] == "http://127.0.0.1:9320/jsonrpc"


def test_herdr_registration_failure_does_not_break_fleet_send(fleet_home: Path) -> None:
    _add_herdr_peer_and_host(fleet_home)

    a2a_fleet._server_thread = None
    ctx = _HerdrRaisingCtx()

    # register() must not raise even though herdr tool registration does.
    a2a_fleet.register(ctx)

    assert "fleet_send" in ctx.registered
    assert "deploy_cc_receiver" in ctx.registered
    assert "deploy_oc_receiver" in ctx.registered
    assert "deploy_codex_receiver" in ctx.registered
    assert "deploy_agy_receiver" in ctx.registered

    # The fault fired (fail-safe caught it, not "never ran").
    assert "herdr_status" not in ctx.registered
    assert "herdr_list_sessions" not in ctx.registered
    assert "herdr_inspect_session" not in ctx.registered
