"""Focused regression tests for MCP registration deduplication."""

import asyncio
import threading
import time
from unittest.mock import patch


def _make_mock_server(name):
    from tools.mcp_tool import MCPServerTask
    return MCPServerTask(name)


def test_concurrent_registration_waits_for_inflight_server():
    from tools import mcp_tool

    fake_config = {
        "trek": {
            "command": "npx",
            "args": ["mcp-remote", "https://example.com/trek"],
            "connect_timeout": 0.5,
        }
    }
    started = threading.Event()
    release = threading.Event()
    call_count = {"connect": 0}
    results = []

    async def fake_connect(name, cfg):
        call_count["connect"] += 1
        started.set()
        await asyncio.get_running_loop().run_in_executor(None, release.wait)
        return _make_mock_server(name)

    def _register_once():
        results.append(mcp_tool.register_mcp_servers(fake_config))

    with patch("tools.mcp_tool._MCP_AVAILABLE", True), \
         patch("tools.mcp_tool._connect_server", side_effect=fake_connect), \
         patch("tools.mcp_tool._register_server_tools", return_value=["mcp_trek_tool"]), \
         patch("tools.mcp_tool._existing_tool_names", return_value=["mcp_trek_tool"]), \
         patch("gateway.status.acquire_scoped_lock", return_value=(True, None)) as mock_lock:
        mcp_tool._ensure_mcp_loop()
        t1 = threading.Thread(target=_register_once)
        t2 = threading.Thread(target=_register_once)
        t1.start()
        assert started.wait(timeout=1.0)
        t2.start()
        time.sleep(0.1)
        release.set()
        t1.join(timeout=2.0)
        t2.join(timeout=2.0)

    try:
        assert call_count["connect"] == 1
        assert mock_lock.call_count == 1
        assert results == [["mcp_trek_tool"], ["mcp_trek_tool"]]
    finally:
        with mcp_tool._lock:
            mcp_tool._servers.clear()
            mcp_tool._connecting_servers.clear()
            mcp_tool._server_lock_identities.clear()
        mcp_tool._stop_mcp_loop()


def test_skips_server_owned_by_other_process():
    from tools import mcp_tool

    fake_config = {
        "trek": {"command": "npx", "args": ["mcp-remote", "https://example.com/trek"]}
    }

    with patch("tools.mcp_tool._MCP_AVAILABLE", True), \
         patch("tools.mcp_tool._discover_and_register_server") as mock_register, \
         patch("tools.mcp_tool._existing_tool_names", return_value=[]), \
         patch("gateway.status.acquire_scoped_lock", return_value=(False, {"pid": 4242})):
        result = mcp_tool.register_mcp_servers(fake_config)

    assert result == []
    mock_register.assert_not_called()
