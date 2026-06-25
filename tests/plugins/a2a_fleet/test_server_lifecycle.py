"""US-005 / lifecycle: start_server() / stop_server() boot and shutdown cleanly."""
from __future__ import annotations

import asyncio
import socket
from pathlib import Path

import httpx
import pytest

pytest.importorskip("fastapi", reason="a2a_fleet server tests require hermes-agent[web]")
pytest.importorskip("uvicorn", reason="a2a_fleet server tests require hermes-agent[web]")



def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _rewrite_bind_port(fleet_home: Path, port: int) -> None:
    import yaml

    fleet_yaml = fleet_home / "profiles" / "switch" / "fleet.yaml"
    data = yaml.safe_load(fleet_yaml.read_text())
    data["fleet"]["server"]["bind_port"] = port
    fleet_yaml.write_text(yaml.safe_dump(data))


def test_start_and_stop_server(fleet_home: Path) -> None:
    port = _free_port()
    _rewrite_bind_port(fleet_home, port)

    from a2a_fleet.server import is_running, start_server, stop_server

    async def go() -> None:
        info = await start_server()
        assert info["started"] is True
        assert info["port"] == port
        assert is_running() is True

        # Ensure the live server actually answers a request on the bound port.
        await asyncio.sleep(0.3)
        async with httpx.AsyncClient(timeout=5.0) as client:
            response = await client.get(f"http://127.0.0.1:{port}/health")
        assert response.status_code == 200
        body = response.json()
        assert body["ok"] is True
        assert "peer_count" in body
        assert "self" not in body
        assert "peers" not in body

        info = await stop_server()
        assert info["stopped"] is True
        assert is_running() is False

    asyncio.run(go())


def test_double_start_is_idempotent(fleet_home: Path) -> None:
    port = _free_port()
    _rewrite_bind_port(fleet_home, port)

    from a2a_fleet.server import start_server, stop_server

    async def go() -> None:
        try:
            r1 = await start_server()
            r2 = await start_server()
            assert r1["started"] is True
            assert r2["started"] is False, "second start_server() must be a no-op"
        finally:
            await stop_server()

    asyncio.run(go())


@pytest.mark.timeout(15)
def test_stop_before_start_is_safe(fleet_home: Path) -> None:
    from a2a_fleet.server import stop_server

    info = asyncio.run(stop_server())
    assert info["stopped"] is False
