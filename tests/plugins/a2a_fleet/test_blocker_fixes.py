"""Regression tests for the four post-review blocker fixes.

Covers:
* CRIT-1: start_server() raises when uvicorn cannot bind (port collision).
* CRIT-2: register() captures + logs start_server() exceptions.
* MAJ-6 : fleet.enabled=false skips tool registration and server start.
* MAJ-7 : auth_required=true + token_env unset returns a JSON-RPC error envelope.
"""
from __future__ import annotations

import asyncio
import importlib.util
import logging
import socket
from pathlib import Path

import pytest

pytest.importorskip("fastapi", reason="a2a_fleet server tests require hermes-agent[web]")
pytest.importorskip("uvicorn", reason="a2a_fleet server tests require hermes-agent[web]")

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


def _expected_registered_tool_names() -> set[str]:
    names = {
        "fleet_send",
        "deploy_cc_receiver",
        "cc_receiver_status",
        "cc_receiver_stop",
    }
    if importlib.util.find_spec("a2a_fleet.oc_deploy") is not None:
        names.update({
            "deploy_oc_receiver",
            "oc_receiver_status",
            "oc_receiver_stop",
        })
    if importlib.util.find_spec("a2a_fleet.codex_deploy") is not None:
        names.update({
            "deploy_codex_receiver",
            "codex_receiver_status",
            "codex_receiver_stop",
        })
    if importlib.util.find_spec("a2a_fleet.agy_deploy") is not None:
        names.update({
            "deploy_agy_receiver",
            "agy_receiver_status",
            "agy_receiver_stop",
        })
    return names


def test_register_logs_when_start_server_fails(
    fleet_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    import a2a_fleet
    from a2a_fleet import register
    from a2a_fleet import server as server_module

    def boom(*_args, **_kwargs):
        raise RuntimeError("simulated bind failure")

    # Patch start_server so the daemon thread logs the failure.
    monkeypatch.setattr(server_module, "start_server", boom)
    # Reset thread state so register() always spawns a fresh thread.
    monkeypatch.setattr(a2a_fleet, "_server_thread", None)

    ctx = _StubCtx()
    with caplog.at_level(logging.ERROR, logger="a2a_fleet.plugin"):
        register(ctx)
        import time; time.sleep(0.2)  # let the daemon thread log

    # fleet_send + the mode-specific deploy/status/stop tools register before the
    # server thread is spawned, so a server-start failure must not prevent any of
    # them from being registered.
    expected = _expected_registered_tool_names()
    actual = {call["name"] for call in ctx.calls}
    assert actual == expected, "tool registration should keep fleet/deploy parity even when the server thread fails"
    assert any(
        "server failed to start" in rec.message
        for rec in caplog.records
    ), "register() must log the swallowed start_server exception"


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
    # Must return 503 with a generic message — must NOT leak token_env name or
    # internal config details to the caller (fix for issue #34).
    assert response.status_code == 503
    body = response.json()
    assert "error" in body
    assert "token_env" not in body["error"]


# Issue #72 — deploy tool schemas must include repo_path in properties + required
# ---------------------------------------------------------------------------


def test_deploy_receiver_tools_have_repo_path_in_schema(
    fleet_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Each deploy_*_receiver tool must expose repo_path in its schema properties
    AND in its required list.  Guards against #72 regression."""
    import a2a_fleet
    from a2a_fleet import register
    from a2a_fleet import server as server_module

    # We only need the captured register_tool calls — do NOT spawn the embedded
    # A2A server thread (it would leak a running server and break
    # test_server_lifecycle, which then sees a server already up).
    monkeypatch.setattr(server_module, "start_server", lambda *a, **k: None)
    monkeypatch.setattr(a2a_fleet, "_server_thread", None)

    ctx = _StubCtx()
    register(ctx)

    deploy_tools = {
        call["name"]: call
        for call in ctx.calls
        if call["name"].startswith("deploy_") and call["name"].endswith("_receiver")
    }

    # At minimum the cc receiver must always be present.
    assert "deploy_cc_receiver" in deploy_tools, "deploy_cc_receiver must be registered"

    for name, call in deploy_tools.items():
        schema = call.get("schema", {})
        props = schema.get("properties", {})
        required = schema.get("required", [])
        assert "repo_path" in props, (
            f"{name}: 'repo_path' missing from schema properties (issue #72 regression)"
        )
        assert "repo_path" in required, (
            f"{name}: 'repo_path' missing from schema required list (issue #72 regression)"
        )
