"""Tests for the API server bind-address startup guard.

Validates that is_network_accessible() correctly classifies addresses and
that connect() refuses to start without API_SERVER_KEY.
"""

import asyncio
import socket
import time
from unittest.mock import patch

import pytest

from gateway.config import PlatformConfig
from gateway.platforms import api_server as api_server_module
from gateway.platforms.api_server import APIServerAdapter
from gateway.platforms.base import is_network_accessible


@pytest.fixture
def no_bind_backoff(monkeypatch):
    """Skip the EADDRINUSE retry backoff so conflict tests stay fast."""
    monkeypatch.setattr(api_server_module, "BIND_RETRY_DELAYS", ())


# ---------------------------------------------------------------------------
# Unit tests: is_network_accessible()
# ---------------------------------------------------------------------------


class TestIsNetworkAccessible:
    """Direct tests for the address classification helper."""

    # -- Loopback (safe, should return False) --

    def test_ipv4_loopback(self):
        assert is_network_accessible("127.0.0.1") is False

    def test_ipv6_loopback(self):
        assert is_network_accessible("::1") is False

    def test_ipv4_mapped_loopback(self):
        # ::ffff:127.0.0.1 — Python's is_loopback returns False for mapped
        # addresses; the helper must unwrap and check ipv4_mapped.
        assert is_network_accessible("::ffff:127.0.0.1") is False

    # -- Network-accessible (should return True) --

    def test_ipv4_wildcard(self):
        assert is_network_accessible("0.0.0.0") is True

    def test_ipv6_wildcard(self):
        # This is the bypass vector that the string-based check missed.
        assert is_network_accessible("::") is True

    def test_ipv4_mapped_unspecified(self):
        assert is_network_accessible("::ffff:0.0.0.0") is True

    def test_private_ipv4(self):
        assert is_network_accessible("10.0.0.1") is True

    def test_private_ipv4_class_c(self):
        assert is_network_accessible("192.168.1.1") is True

    def test_public_ipv4(self):
        assert is_network_accessible("8.8.8.8") is True

    # -- Hostname resolution --

    def test_localhost_resolves_to_loopback(self):
        loopback_result = [
            (socket.AF_INET, socket.SOCK_STREAM, 0, "", ("127.0.0.1", 0)),
        ]
        with patch("gateway.platforms.base._socket.getaddrinfo", return_value=loopback_result):
            assert is_network_accessible("localhost") is False

    def test_hostname_resolving_to_non_loopback(self):
        non_loopback_result = [
            (socket.AF_INET, socket.SOCK_STREAM, 0, "", ("10.0.0.1", 0)),
        ]
        with patch("gateway.platforms.base._socket.getaddrinfo", return_value=non_loopback_result):
            assert is_network_accessible("my-server.local") is True

    def test_hostname_mixed_resolution(self):
        """If a hostname resolves to both loopback and non-loopback, it's
        network-accessible (any non-loopback address is enough)."""
        mixed_result = [
            (socket.AF_INET, socket.SOCK_STREAM, 0, "", ("127.0.0.1", 0)),
            (socket.AF_INET, socket.SOCK_STREAM, 0, "", ("10.0.0.1", 0)),
        ]
        with patch("gateway.platforms.base._socket.getaddrinfo", return_value=mixed_result):
            assert is_network_accessible("dual-host.local") is True

    def test_dns_failure_fails_closed(self):
        """Unresolvable hostnames should require an API key (fail closed)."""
        with patch(
            "gateway.platforms.base._socket.getaddrinfo",
            side_effect=socket.gaierror("Name resolution failed"),
        ):
            assert is_network_accessible("nonexistent.invalid") is True


# ---------------------------------------------------------------------------
# Integration tests: connect() startup guard
# ---------------------------------------------------------------------------


class TestConnectBindGuard:
    """Verify that connect() refuses dangerous configurations."""

    @pytest.mark.asyncio
    async def test_refuses_ipv4_wildcard_without_key(self):
        adapter = APIServerAdapter(PlatformConfig(enabled=True, extra={"host": "0.0.0.0"}))
        result = await adapter.connect()
        assert result is False

    @pytest.mark.asyncio
    async def test_refuses_ipv6_wildcard_without_key(self):
        adapter = APIServerAdapter(PlatformConfig(enabled=True, extra={"host": "::"}))
        result = await adapter.connect()
        assert result is False

    @pytest.mark.asyncio
    async def test_refuses_loopback_without_key(self):
        """Loopback binds are still an auth boundary and require API_SERVER_KEY."""
        adapter = APIServerAdapter(PlatformConfig(enabled=True, extra={"host": "127.0.0.1"}))
        assert adapter._api_key == ""
        assert is_network_accessible(adapter._host) is False
        result = await adapter.connect()
        assert result is False
        assert adapter._app is None
        assert adapter._background_tasks == set()

    @pytest.mark.asyncio
    async def test_refuses_weak_key_without_partial_startup(self):
        """Weak API_SERVER_KEY rejection must not create app or background tasks."""
        adapter = APIServerAdapter(
            PlatformConfig(enabled=True, extra={"host": "127.0.0.1", "key": "short"}),
        )
        result = await adapter.connect()
        assert result is False
        assert adapter._app is None
        assert adapter._background_tasks == set()

    @pytest.mark.asyncio
    async def test_allows_wildcard_with_key(self):
        """Non-loopback with a key should pass the guard."""
        adapter = APIServerAdapter(
            PlatformConfig(enabled=True, extra={"host": "0.0.0.0", "key": "sk-test"})
        )
        # The guard checks: is_network_accessible(host) AND NOT api_key
        # With a key set, the guard should not block.
        assert adapter._api_key == "sk-test"
        assert is_network_accessible("0.0.0.0") is True
        # Combined: the guard condition is False (key is set), so it passes


# ---------------------------------------------------------------------------
# Integration tests: bind mechanics (direct bind, no pre-probe — #10297)
# ---------------------------------------------------------------------------


class TestBindMechanics:
    """connect() binds directly instead of pre-probing 127.0.0.1.

    The old ``_port_is_available()`` probe connected to 127.0.0.1 only and
    reported a lingering TIME_WAIT socket as "in use", failing gateway
    restarts for up to ~60s (#10297). The fix removes the probe: bind
    directly, keep SO_REUSEADDR default semantics on Linux (rebind past
    TIME_WAIT), and surface a real bind conflict as a clean ``False`` with
    the runner torn down.
    """

    _KEY = "sk-test-strong-key-0123456789"

    def _make_adapter(self, port: int) -> APIServerAdapter:
        return APIServerAdapter(
            PlatformConfig(
                enabled=True,
                extra={"host": "127.0.0.1", "port": port, "key": self._KEY},
            )
        )

    @staticmethod
    def _free_port() -> int:
        with socket.socket() as s:
            s.bind(("", 0))
            return s.getsockname()[1]

    @pytest.mark.asyncio
    async def test_immediate_rebind_after_disconnect(self):
        """A restarted adapter can rebind the same port immediately.

        This is the #10297 symptom: the old pre-probe (and disabled address
        reuse) made a quick gateway restart fail while the previous socket
        sat in TIME_WAIT.
        """
        port = self._free_port()
        first = self._make_adapter(port)
        assert await first.connect() is True
        await first.disconnect()

        second = self._make_adapter(port)
        try:
            assert await second.connect() is True
        finally:
            await second.disconnect()

    @pytest.mark.asyncio
    async def test_live_listener_conflict_returns_false_and_cleans_up(self, no_bind_backoff):
        """A second adapter on an occupied port fails cleanly, not with a raise."""
        port = self._free_port()
        first = self._make_adapter(port)
        assert await first.connect() is True
        second = self._make_adapter(port)
        try:
            result = await second.connect()
            assert result is False
            assert second._runner is None
            assert second._site is None
            assert second.is_connected is False
        finally:
            await first.disconnect()
            await second.disconnect()

    def test_pre_probe_helper_removed(self):
        """The racy single-family pre-probe must not come back."""
        assert not hasattr(APIServerAdapter, "_port_is_available")

    @pytest.mark.asyncio
    async def test_port_conflict_sets_non_retryable_fatal_error(self, no_bind_backoff):
        """A real port conflict (EADDRINUSE) must set a non-retryable fatal
        error so the reconnect watcher drops the platform from the retry
        queue instead of looping indefinitely.

        Previously connect() returned bare ``False``, which the reconnect
        watcher treated as retryable — retrying every 5 minutes forever,
        filling errors.log and leaking 2 fds per retry (#52132: 1568+
        retries over 5 days in a multi-profile setup).
        """
        port = self._free_port()
        first = self._make_adapter(port)
        assert await first.connect() is True
        second = self._make_adapter(port)
        try:
            result = await second.connect()
            assert result is False
            assert second.has_fatal_error is True
            assert second.fatal_error_retryable is False
            assert second.fatal_error_code == "api_server_port_in_use"
            assert str(port) in (second.fatal_error_message or "")
        finally:
            await first.disconnect()
            await second.disconnect()

    @pytest.mark.asyncio
    async def test_bind_retries_while_port_debris_clears(self, monkeypatch):
        """A restart race must not leave the gateway without an API server.

        With reuse_address off on macOS, TIME_WAIT sockets from the previous
        instance's *clients* keep the port unbindable for up to ~60s — well
        after that process is gone. Measured on 2026-07-29 12:21: the old
        listener closed 8s before the replacement started and the bind still
        failed for 15s more, leaving a gateway with every other platform up
        and no API server at all.

        A socket bound but never listen()ed reproduces that shape: the port
        is occupied, yet nothing accepts connections.
        """
        monkeypatch.setattr(api_server_module, "BIND_RETRY_DELAYS", (0.05, 0.05, 0.05, 0.05))
        port = self._free_port()
        debris = socket.socket()
        debris.bind(("127.0.0.1", port))  # bound, never listen()

        async def clear_debris():
            await asyncio.sleep(0.12)
            debris.close()

        clearer = asyncio.create_task(clear_debris())
        adapter = self._make_adapter(port)
        try:
            assert await adapter.connect() is True
            assert adapter.has_fatal_error is False
        finally:
            await clearer
            debris.close()
            await adapter.disconnect()

    def test_backoff_fits_inside_the_platform_connect_timeout(self):
        """The ladder must finish before gateway.run cancels connect().

        gateway.run wraps every platform connect() in a 30s timeout. A longer
        ladder is not "more patient" — it is silently cancelled mid-sleep, and
        the adapter never reaches its own failure classification. That is
        exactly what happened on 2026-07-29 14:52 with a 60s ladder:
        "api_server connect timed out after 30s".
        """
        from gateway.run import _PLATFORM_CONNECT_TIMEOUT_SECS_DEFAULT

        budget = sum(api_server_module.BIND_RETRY_DELAYS)
        assert budget < _PLATFORM_CONNECT_TIMEOUT_SECS_DEFAULT * 0.75, (
            f"bind backoff {budget}s leaves no room inside the "
            f"{_PLATFORM_CONNECT_TIMEOUT_SECS_DEFAULT}s connect timeout"
        )

    @pytest.mark.asyncio
    async def test_exhausted_debris_stays_retryable_so_the_watcher_heals_it(
        self, monkeypatch
    ):
        """Debris that outlasts the ladder must NOT be marked non-retryable.

        gateway.run's reconnect watcher drops non-retryable failures from its
        queue immediately. Marking TIME_WAIT debris non-retryable therefore
        converts a self-clearing ~60s condition into an API server that stays
        dead until a human restarts the gateway — which is what happened twice
        on 2026-07-29. The port conflict classification (#52132) must apply
        only when something is actually listening.
        """
        monkeypatch.setattr(api_server_module, "BIND_RETRY_DELAYS", (0.01, 0.01))
        port = self._free_port()
        debris = socket.socket()
        debris.bind(("127.0.0.1", port))  # bound, never listen(): nothing accepts

        adapter = self._make_adapter(port)
        try:
            assert await adapter.connect() is False
            assert adapter.has_fatal_error is False, (
                "debris must stay retryable — a fatal error drops it from the "
                "reconnect queue forever"
            )
        finally:
            debris.close()
            await adapter.disconnect()

    @pytest.mark.asyncio
    async def test_live_conflict_fails_fast_instead_of_waiting_out_the_backoff(self):
        """Retrying is for debris. A live listener is a real conflict.

        Without the listener probe, a genuinely occupied port would burn the
        full backoff before reporting — turning a clear config error into a
        minute of apparent hang at every startup.
        """
        port = self._free_port()
        first = self._make_adapter(port)
        assert await first.connect() is True
        second = self._make_adapter(port)
        try:
            started = time.monotonic()
            assert await second.connect() is False
            assert time.monotonic() - started < 2.0
            assert second.fatal_error_code == "api_server_port_in_use"
        finally:
            await first.disconnect()
            await second.disconnect()
