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


def test_register_from_sync_context_starts_server(fleet_home: Path) -> None:
    """Calling register() with no running asyncio loop must still start the server."""
    import a2a_fleet
    from a2a_fleet import server as server_mod

    # Confirm there is no running loop in this thread.
    try:
        asyncio.get_running_loop()
        pytest.skip("test must run outside an active event loop")
    except RuntimeError:
        pass

    port = _free_port()
    _rewrite_port(fleet_home, port)

    # Reset module-level thread state so this test is isolated.
    import a2a_fleet
    a2a_fleet._server_thread = None

    ctx = _StubCtx()
    a2a_fleet.register(ctx)

    # Give the daemon thread time to bind and start serving.
    deadline = time.monotonic() + 8.0
    reachable = False
    while time.monotonic() < deadline:
        try:
            resp = httpx.get(f"http://127.0.0.1:{port}/health", timeout=1.0)
            if resp.status_code == 200:
                reachable = True
                break
        except Exception:
            time.sleep(0.1)

    # Clean up — signal server to stop.
    server_mod.stop_server_sync()

    assert reachable, (
        "Server must be reachable after register() is called from a plain sync context "
        "(regression guard for issue #33)"
    )
