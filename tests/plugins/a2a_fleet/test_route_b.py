"""Route B tests — inbound A2A → real Hermes agent bridge (no live gateway/agent).

Covers:
- Adapter connect/disconnect lifecycle (sets / clears get_agent_bridge())
- bridge_sync happy path (stub message_handler on real asyncio loop)
- Per-context busy lock (second concurrent bridge_sync raises A2ABusyError)
- Bridge not ready — server agent-mode POST returns -32000
- Server agent-mode happy path (monkeypatched bridge.bridge_sync)
- Echo + llm regression (existing modes unchanged)
"""
from __future__ import annotations

import asyncio
import threading
from pathlib import Path
from typing import Optional
from unittest.mock import MagicMock

import pytest
import yaml

pytest.importorskip("fastapi", reason="a2a_fleet server tests require hermes-agent[web]")
pytest.importorskip("uvicorn", reason="a2a_fleet server tests require hermes-agent[web]")

from fastapi.testclient import TestClient


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _send_body(text: str = "hello", context_id: Optional[str] = None):
    msg: dict = {"role": "user", "parts": [{"text": text}]}
    if context_id:
        msg["contextId"] = context_id
    return {
        "jsonrpc": "2.0",
        "id": "r-1",
        "method": "SendMessage",
        "params": {"message": msg},
    }


def _fleet_yaml_with_handler(home: Path, handler: str) -> None:
    """Rewrite fleet.yaml in the active profile directory to use ``handler``."""
    # HERMES_HOME is already set to the profile_dir by the fleet_home fixture.
    fleet_path = home / "fleet.yaml"
    if not fleet_path.is_file():
        # Fallback: look under profiles/switch/
        fleet_path = home / "profiles" / "switch" / "fleet.yaml"
    data = yaml.safe_load(fleet_path.read_text())
    data["fleet"]["response_handler"] = handler
    fleet_path.write_text(yaml.safe_dump(data))


# ---------------------------------------------------------------------------
# Adapter lifecycle tests
# ---------------------------------------------------------------------------

class TestAdapterLifecycle:
    """connect() registers the bridge; disconnect() clears it."""

    def test_connect_sets_bridge(self):
        from a2a_fleet.adapter import A2AFleetAdapter
        from a2a_fleet.agent_bridge import get_agent_bridge, set_agent_bridge

        # Start clean.
        set_agent_bridge(None)

        adapter = A2AFleetAdapter(config=MagicMock())

        loop = asyncio.new_event_loop()
        try:
            result = loop.run_until_complete(adapter.connect())
        finally:
            loop.close()

        assert result is True
        assert get_agent_bridge() is adapter

        # Cleanup.
        set_agent_bridge(None)

    def test_disconnect_clears_bridge(self):
        from a2a_fleet.adapter import A2AFleetAdapter
        from a2a_fleet.agent_bridge import get_agent_bridge, set_agent_bridge

        set_agent_bridge(None)
        adapter = A2AFleetAdapter(config=MagicMock())

        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(adapter.connect())
            assert get_agent_bridge() is adapter
            loop.run_until_complete(adapter.disconnect())
        finally:
            loop.close()

        assert get_agent_bridge() is None


# ---------------------------------------------------------------------------
# bridge_sync happy path
# ---------------------------------------------------------------------------

class TestBridgeSync:
    """bridge_sync dispatches into the gateway loop and returns the reply."""

    def test_bridge_sync_returns_agent_reply(self):
        from a2a_fleet.adapter import A2AFleetAdapter
        from a2a_fleet.agent_bridge import set_agent_bridge

        # Run a real asyncio loop in a background thread — mirrors the gateway.
        bg_loop = asyncio.new_event_loop()

        async def _stub_handler(event):
            return "AGENT_REPLY"

        def _run_loop():
            asyncio.set_event_loop(bg_loop)
            bg_loop.run_forever()

        t = threading.Thread(target=_run_loop, daemon=True)
        t.start()

        try:
            adapter = A2AFleetAdapter(config=MagicMock())
            adapter._gateway_loop = bg_loop
            adapter._message_handler = _stub_handler
            set_agent_bridge(adapter)

            reply = adapter.bridge_sync("hello", "ctx-1", "peer-x", timeout=5.0)
            assert reply == "AGENT_REPLY"
        finally:
            bg_loop.call_soon_threadsafe(bg_loop.stop)
            t.join(timeout=2)
            set_agent_bridge(None)

    def test_bridge_sync_none_reply_returns_empty_string(self):
        from a2a_fleet.adapter import A2AFleetAdapter
        from a2a_fleet.agent_bridge import set_agent_bridge

        bg_loop = asyncio.new_event_loop()

        async def _none_handler(event):
            return None

        def _run_loop():
            asyncio.set_event_loop(bg_loop)
            bg_loop.run_forever()

        t = threading.Thread(target=_run_loop, daemon=True)
        t.start()

        try:
            adapter = A2AFleetAdapter(config=MagicMock())
            adapter._gateway_loop = bg_loop
            adapter._message_handler = _none_handler
            set_agent_bridge(adapter)

            reply = adapter.bridge_sync("anything", "ctx-2", "peer-y", timeout=5.0)
            assert reply == ""
        finally:
            bg_loop.call_soon_threadsafe(bg_loop.stop)
            t.join(timeout=2)
            set_agent_bridge(None)


# ---------------------------------------------------------------------------
# Per-context busy lock
# ---------------------------------------------------------------------------

class TestPerContextBusyLock:
    """Second concurrent bridge_sync on the same context raises A2ABusyError."""

    def test_busy_raises_when_context_locked(self):
        import time

        from a2a_fleet.adapter import A2AFleetAdapter
        from a2a_fleet.agent_bridge import A2ABusyError, set_agent_bridge

        bg_loop = asyncio.new_event_loop()
        # Slow handler — gives us time to fire a concurrent call.
        gate = threading.Event()

        async def _slow_handler(event):
            await asyncio.sleep(0.5)
            return "SLOW_REPLY"

        def _run_loop():
            asyncio.set_event_loop(bg_loop)
            bg_loop.run_forever()

        t = threading.Thread(target=_run_loop, daemon=True)
        t.start()

        try:
            adapter = A2AFleetAdapter(config=MagicMock())
            adapter._gateway_loop = bg_loop
            adapter._message_handler = _slow_handler
            set_agent_bridge(adapter)

            results = {}

            def _call_first():
                results["first"] = adapter.bridge_sync("msg", "ctx-busy", "p1", timeout=5.0)

            def _call_second():
                # Let the first call get the lock before we try.
                time.sleep(0.05)
                try:
                    adapter.bridge_sync("msg2", "ctx-busy", "p2", timeout=5.0)
                    results["second"] = "no_error"
                except A2ABusyError as exc:
                    results["second"] = f"busy:{exc}"

            th1 = threading.Thread(target=_call_first)
            th2 = threading.Thread(target=_call_second)
            th1.start()
            th2.start()
            th1.join(timeout=5)
            th2.join(timeout=5)

            assert results.get("first") == "SLOW_REPLY"
            assert results.get("second", "").startswith("busy:")
        finally:
            bg_loop.call_soon_threadsafe(bg_loop.stop)
            t.join(timeout=2)
            set_agent_bridge(None)


# ---------------------------------------------------------------------------
# Bridge not ready — server returns -32000
# ---------------------------------------------------------------------------

class TestBridgeNotReady:
    """When get_agent_bridge() is None, POST agent mode returns -32000."""

    def test_server_agent_mode_no_bridge(self, fleet_home: Path, monkeypatch) -> None:
        import os
        import a2a_fleet.agent_bridge as _ab

        # Ensure bridge is None.
        _ab.set_agent_bridge(None)

        # Write agent handler to fleet.yaml in HERMES_HOME.
        hermes_home = Path(os.environ["HERMES_HOME"])
        _fleet_yaml_with_handler(hermes_home, "agent")

        from a2a_fleet.server import build_app

        with TestClient(build_app()) as client:
            response = client.post("/jsonrpc", json=_send_body("hello"))

        assert response.status_code == 200
        body = response.json()
        assert "error" in body
        assert body["error"]["code"] == -32000
        assert "bridge not ready" in body["error"]["message"]


# ---------------------------------------------------------------------------
# Server agent-mode happy path
# ---------------------------------------------------------------------------

class TestServerAgentModeHappyPath:
    """Monkeypatched bridge returns HELLO_FROM_AGENT; server wraps it in envelope."""

    def test_agent_mode_reply_in_message_envelope(self, fleet_home: Path, monkeypatch) -> None:
        import os
        import a2a_fleet.agent_bridge as _ab

        hermes_home = Path(os.environ["HERMES_HOME"])
        _fleet_yaml_with_handler(hermes_home, "agent")

        # Fake bridge whose bridge_sync returns canned text.
        fake_bridge = MagicMock()
        fake_bridge.bridge_sync.return_value = "HELLO_FROM_AGENT"
        _ab.set_agent_bridge(fake_bridge)

        try:
            from a2a_fleet.server import build_app

            with TestClient(build_app()) as client:
                response = client.post("/jsonrpc", json=_send_body("what is the answer?"))

            assert response.status_code == 200
            body = response.json()
            assert "result" in body, f"Expected result, got: {body}"
            result = body["result"]
            assert result["kind"] == "message"
            msg = result["message"]
            assert msg["role"] == "agent"
            assert msg["parts"][0]["text"] == "HELLO_FROM_AGENT"
            assert "contextId" in msg
        finally:
            _ab.set_agent_bridge(None)

    def test_agent_mode_busy_error_returns_minus_32000(self, fleet_home: Path) -> None:
        import os
        import a2a_fleet.agent_bridge as _ab
        from a2a_fleet.agent_bridge import A2ABusyError

        hermes_home = Path(os.environ["HERMES_HOME"])
        _fleet_yaml_with_handler(hermes_home, "agent")

        fake_bridge = MagicMock()
        fake_bridge.bridge_sync.side_effect = A2ABusyError("ctx busy")
        _ab.set_agent_bridge(fake_bridge)

        try:
            from a2a_fleet.server import build_app

            with TestClient(build_app()) as client:
                response = client.post("/jsonrpc", json=_send_body("hello"))

            assert response.status_code == 200
            body = response.json()
            assert body["error"]["code"] == -32000
            assert "busy" in body["error"]["message"]
        finally:
            _ab.set_agent_bridge(None)


# ---------------------------------------------------------------------------
# Regression: echo + llm modes unchanged
# ---------------------------------------------------------------------------

class TestEchoLlmRegression:
    """echo and llm modes must keep working exactly as before."""

    def test_echo_ping_pong_unchanged(self, fleet_home: Path) -> None:
        # fleet_home defaults to response_handler: echo
        from a2a_fleet.server import build_app

        with TestClient(build_app()) as client:
            response = client.post("/jsonrpc", json=_send_body("ping"))
        assert response.status_code == 200
        body = response.json()
        assert body["result"]["message"]["parts"][0]["text"] == "pong"

    def test_echo_verbatim_unchanged(self, fleet_home: Path) -> None:
        from a2a_fleet.server import build_app

        with TestClient(build_app()) as client:
            response = client.post("/jsonrpc", json=_send_body("hello fleet"))
        assert response.status_code == 200
        body = response.json()
        assert body["result"]["message"]["parts"][0]["text"] == "hello fleet"


# ---------------------------------------------------------------------------
# Reasoning preamble stripping (agent replies must not leak the display block)
# ---------------------------------------------------------------------------

class TestStripReasoningPreamble:
    def test_strips_reasoning_block(self) -> None:
        from a2a_fleet.adapter import _strip_reasoning_preamble

        raw = "💭 **Reasoning:**\n```\nthinking\nmore\n```\n\nFinal answer."
        assert _strip_reasoning_preamble(raw) == "Final answer."

    def test_noop_when_absent(self) -> None:
        from a2a_fleet.adapter import _strip_reasoning_preamble

        plain = "Just the answer, no reasoning block."
        assert _strip_reasoning_preamble(plain) == plain

    def test_empty_string(self) -> None:
        from a2a_fleet.adapter import _strip_reasoning_preamble

        assert _strip_reasoning_preamble("") == ""
