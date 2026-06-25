"""Regression test for issue #33: register() from a plain synchronous context.

Before the fix, _schedule_server_start() called asyncio.get_running_loop()
which raised RuntimeError when there was no running loop, causing the server to
never start.  After the fix, a daemon thread with its own event loop is spawned
regardless of loop state at call time.
"""
from __future__ import annotations

import asyncio
import socket
import time
from pathlib import Path

import httpx
import pytest

pytest.importorskip("fastapi", reason="a2a_fleet server tests require hermes-agent[web]")
pytest.importorskip("uvicorn", reason="a2a_fleet server tests require hermes-agent[web]")

import yaml


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _rewrite_port(fleet_home: Path, port: int) -> None:
    path = fleet_home / "profiles" / "switch" / "fleet.yaml"
    data = yaml.safe_load(path.read_text())
    data["fleet"]["server"]["bind_port"] = port
    # auth_required=False so health check works without a token.
    data["fleet"]["server"]["auth_required"] = False
    path.write_text(yaml.safe_dump(data))


class _StubCtx:
    def register_tool(self, **kwargs) -> None:
        pass

    # Gateway/agent context signal — register() starts the A2A listener only when
    # this is present (co-located with the Route B bridge, #120).
    def register_platform(self, **kwargs) -> None:
        pass


def test_register_does_not_bind_and_connect_starts_reachable_server(fleet_home: Path) -> None:
    """#120: register() must NOT bind the A2A port (it runs in every process that
    loads the plugin — racing them broke Route B). The listener now binds when
    A2AFleetAdapter.connect() runs in the gateway/agent process. This verifies
    both halves: register() alone leaves the port closed; connect() brings up a
    reachable server. (Supersedes the issue-#33 register()-starts-server guard.)
    """
    import a2a_fleet
    from a2a_fleet import server as server_mod
    from a2a_fleet.adapter import A2AFleetAdapter
    from a2a_fleet.agent_bridge import set_agent_bridge
    from gateway.config import PlatformConfig

    port = _free_port()
    _rewrite_port(fleet_home, port)
    a2a_fleet._server_thread = None

    # 1) register() alone must NOT bind the port.
    a2a_fleet.register(_StubCtx())
    time.sleep(0.3)
    try:
        httpx.get(f"http://127.0.0.1:{port}/health", timeout=0.5)
        bound_after_register = True
    except Exception:
        bound_after_register = False
    assert not bound_after_register, "register() must not start/bind the A2A server"

    # 2) connect() (gateway/agent process) must bring up a reachable server.
    adapter = A2AFleetAdapter(PlatformConfig())
    reachable = False
    try:
        asyncio.run(adapter.connect())
        deadline = time.monotonic() + 8.0
        while time.monotonic() < deadline:
            try:
                if httpx.get(f"http://127.0.0.1:{port}/health", timeout=1.0).status_code == 200:
                    reachable = True
                    break
            except Exception:
                time.sleep(0.1)
    finally:
        server_mod.stop_server_sync()
        set_agent_bridge(None)

    assert reachable, "A2A server must be reachable after A2AFleetAdapter.connect()"
