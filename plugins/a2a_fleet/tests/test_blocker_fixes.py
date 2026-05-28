"""Regression tests for the four post-review blocker fixes.

Covers:
* CRIT-1: start_server() raises when uvicorn cannot bind (port collision).
* CRIT-2: register() captures + logs start_server() exceptions.
* MAJ-6 : fleet.enabled=false skips tool registration and server start.
* MAJ-7 : auth_required=true + token_env unset returns a JSON-RPC error envelope.
"""
from __future__ import annotations

import asyncio
import logging
import socket
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient


def _rewrite(fleet_home: Path, mutate) -> None:
    path = fleet_home / "profiles" / "switch" / "fleet.yaml"
    data = yaml.safe_load(path.read_text())
    mutate(data)
    path.write_text(yaml.safe_dump(data))


# CRIT-1 ----------------------------------------------------------------


def test_start_server_raises_when_port_busy(fleet_home: Path) -> None:
    from a2a_fleet.server import A2AServerStartError, start_server, stop_server

    # Occupy the configured port so uvicorn cannot bind.
    blocker = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    blocker.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    blocker.bind(("127.0.0.1", 9319))
    blocker.listen(1)
    try:
        with pytest.raises(A2AServerStartError):
            asyncio.run(start_server(timeout=1.5))
    finally:
        blocker.close()
        # Ensure we are not leaking a half-started server instance.
        asyncio.run(stop_server())


# CRIT-2 ----------------------------------------------------------------


class _StubCtx:
    def __init__(self) -> None:
        self.calls: list = []

    def register_tool(self, **kwargs) -> None:
        self.calls.append(kwargs)


def test_register_logs_when_start_server_fails(
    fleet_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    from a2a_fleet import _start_server_safe, register
    from a2a_fleet import server as server_module

    async def boom(*_args, **_kwargs):
        raise RuntimeError("simulated bind failure")

    monkeypatch.setattr(server_module, "start_server", boom)

    async def go() -> None:
        ctx = _StubCtx()
        with caplog.at_level(logging.ERROR, logger="a2a_fleet.plugin"):
            register(ctx)
            # Allow the scheduled task to run + log.
            await asyncio.sleep(0.05)
        assert len(ctx.calls) == 1, "tool should still register before the failure surfaces"
        assert any(
            "server failed to start" in rec.message and "simulated bind failure" in rec.message
            for rec in caplog.records
        ), "register() must log the swallowed start_server exception"

    asyncio.run(go())


# MAJ-6 -----------------------------------------------------------------


def test_register_skips_when_fleet_disabled(fleet_home: Path) -> None:
    from a2a_fleet import register

    _rewrite(fleet_home, lambda d: d["fleet"].__setitem__("enabled", False))

    ctx = _StubCtx()
    register(ctx)
    assert ctx.calls == [], "no tool registration should happen when fleet.enabled=false"


# MAJ-7 -----------------------------------------------------------------


def test_auth_required_without_token_env_returns_jsonrpc_envelope(
    fleet_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from a2a_fleet.server import build_app

    _rewrite(fleet_home, lambda d: d["fleet"]["server"].__setitem__("auth_required", True))
    # Wipe the token env so token resolves to None.
    monkeypatch.delenv("SWITCH_A2A_TOKEN", raising=False)

    with TestClient(build_app()) as client:
        response = client.post(
            "/jsonrpc",
            json={"jsonrpc": "2.0", "id": 1, "method": "SendMessage", "params": {}},
            headers={"authorization": "Bearer something"},
        )
    assert response.status_code == 500
    body = response.json()
    assert body["jsonrpc"] == "2.0"
    assert body["error"]["code"] == -32603
    assert "token_env" in body["error"]["message"]
