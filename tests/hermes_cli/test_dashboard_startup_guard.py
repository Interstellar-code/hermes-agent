"""Dashboard single-instance and port-conflict startup guards."""

from __future__ import annotations

import socket

import pytest

from hermes_cli.dashboard_lifecycle import (
    DASHBOARD_CONFLICT_EXIT_CODE,
    DashboardStartupConflict,
    acquire_dashboard_startup_guard,
)


def test_lock_is_profile_scoped(tmp_path, monkeypatch):
    first_home = tmp_path / "profile-one"
    second_home = tmp_path / "profile-two"
    monkeypatch.setenv("HERMES_HOME", str(first_home))

    first = acquire_dashboard_startup_guard("127.0.0.1", 0)
    try:
        assert first.path == first_home / "dashboard.lock"
        monkeypatch.setenv("HERMES_HOME", str(second_home))
        second = acquire_dashboard_startup_guard("127.0.0.1", 0)
        try:
            assert second.path == second_home / "dashboard.lock"
        finally:
            second.release()
    finally:
        first.release()


def test_second_dashboard_for_same_profile_is_rejected(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "profile-home"))

    first = acquire_dashboard_startup_guard("127.0.0.1", 0)
    try:
        with pytest.raises(DashboardStartupConflict) as exc:
            acquire_dashboard_startup_guard("127.0.0.1", 0)
        assert exc.value.exit_code == DASHBOARD_CONFLICT_EXIT_CODE
        assert "already starting or running" in str(exc.value)
    finally:
        first.release()


def test_occupied_port_is_rejected_once_with_owner_details(
    tmp_path, monkeypatch,
):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "profile-home"))
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    port = listener.getsockname()[1]

    try:
        with pytest.raises(DashboardStartupConflict) as exc:
            acquire_dashboard_startup_guard("127.0.0.1", port)
        message = str(exc.value)
        assert exc.value.exit_code == DASHBOARD_CONFLICT_EXIT_CODE
        assert f"127.0.0.1:{port}" in message
        assert "already in use" in message
    finally:
        listener.close()


def test_released_lock_can_be_acquired_again(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "profile-home"))

    first = acquire_dashboard_startup_guard("127.0.0.1", 0)
    first.release()
    second = acquire_dashboard_startup_guard("127.0.0.1", 0)
    second.release()


def test_port_probe_enables_address_reuse(tmp_path, monkeypatch):
    """A just-stopped dashboard port must be reusable on macOS."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "profile-home"))
    calls = []
    real_socket = socket.socket

    class RecordingSocket:
        def __init__(self, *args):
            self._socket = real_socket(*args)

        def setsockopt(self, level, option, value):
            calls.append((level, option, value))
            return self._socket.setsockopt(level, option, value)

        def bind(self, address):
            return self._socket.bind(address)

        def close(self):
            return self._socket.close()

    monkeypatch.setattr(socket, "socket", RecordingSocket)
    lease = acquire_dashboard_startup_guard("127.0.0.1", 0)
    lease.release()

    # Port 0 skips the probe, so exercise a concrete free port.
    holder = real_socket(socket.AF_INET, socket.SOCK_STREAM)
    holder.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    holder.bind(("127.0.0.1", 0))
    port = holder.getsockname()[1]
    holder.close()

    lease = acquire_dashboard_startup_guard("127.0.0.1", port)
    lease.release()
    assert (socket.SOL_SOCKET, socket.SO_REUSEADDR, 1) in calls
