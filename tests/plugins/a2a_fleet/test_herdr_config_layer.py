"""Phase 2.5 — config-layer prerequisites for Herdr session-manager peers.

Covers the sibling-set invariant in managed_peers (herdr_session must NOT
join SUPPORTED_MANAGED_MODES), the fleet_config.py url exemption / required
fields / fleet.herdr schema for herdr peers, and the client.py portless
transport seam. Pure config/routing tests — no herdr_client/herdr_capability
imports (those modules belong to a different concurrent change).
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, Dict

import pytest
import yaml


def _fleet_yaml_path(fleet_home: Path) -> Path:
    return fleet_home / "profiles" / "switch" / "fleet.yaml"


def _load_data(fleet_home: Path) -> Dict[str, Any]:
    return yaml.safe_load(_fleet_yaml_path(fleet_home).read_text())


def _write_data(fleet_home: Path, data: Dict[str, Any]) -> None:
    _fleet_yaml_path(fleet_home).write_text(yaml.safe_dump(data))


def _add_herdr_host(
    data: Dict[str, Any],
    alias: str = "mac-mini",
    *,
    transport: str = "local_socket",
    allowed_workspaces: list[str] | None = None,
    ssh_target: str | None = None,
) -> None:
    herdr_block = data["fleet"].setdefault("herdr", {})
    hosts = herdr_block.setdefault("hosts", {})
    host_entry: Dict[str, Any] = {
        "transport": transport,
        "allowed_workspaces": allowed_workspaces or ["/Users/rohits/hermes"],
    }
    if ssh_target is not None:
        host_entry["ssh_target"] = ssh_target
    hosts[alias] = host_entry


def _add_herdr_peer(
    data: Dict[str, Any],
    name: str = "herdr-mac-mini",
    *,
    host_alias: str = "mac-mini",
    workspace: str = "/Users/rohits/hermes",
    agent_kind: str = "claude_code",
    transport: str = "local_socket",
    extra: Dict[str, Any] | None = None,
) -> None:
    peer: Dict[str, Any] = {
        "managed": True,
        "mode": "herdr_session",
        "host_alias": host_alias,
        "workspace": workspace,
        "agent_kind": agent_kind,
        "transport": transport,
    }
    if extra:
        peer.update(extra)
    data["fleet"]["agents"][name] = peer


# ---------------------------------------------------------------------------
# managed_peers.py — sibling-set invariant
# ---------------------------------------------------------------------------


def test_supported_managed_modes_unchanged() -> None:
    from a2a_fleet.managed_peers import SUPPORTED_MANAGED_MODES

    assert SUPPORTED_MANAGED_MODES == frozenset({"claude_code", "opencode", "codex", "agy"})


def test_supports_managed_mode_false_for_herdr_session() -> None:
    from a2a_fleet.managed_peers import supports_managed_mode

    assert supports_managed_mode("herdr_session") is False


def test_supports_herdr_mode_true_for_herdr_session() -> None:
    from a2a_fleet.managed_peers import supports_herdr_mode

    assert supports_herdr_mode("herdr_session") is True
    assert supports_herdr_mode("claude_code") is False
    assert supports_herdr_mode(None) is False


def test_port_band_for_raises_for_herdr_session() -> None:
    from a2a_fleet.managed_peers import port_band_for

    with pytest.raises(ValueError):
        port_band_for("herdr_session")


def test_allocate_band_port_raises_for_herdr_session() -> None:
    from a2a_fleet.managed_peers import allocate_band_port

    with pytest.raises(ValueError):
        allocate_band_port("herdr_session")


def test_transcript_filename_for_herdr_session_unchanged_fallback() -> None:
    """herdr_session is unknown to _MODE_SPECS, same as any other unrecognised
    mode: transcript_filename_for degrades to the claude_code filename rather
    than raising (existing, documented behaviour — untouched by this change)."""
    from a2a_fleet.managed_peers import transcript_filename_for

    assert transcript_filename_for("herdr_session") == transcript_filename_for("bogus_mode")


def test_stable_token_env_name_raises_for_herdr_session(tmp_path: Path) -> None:
    from a2a_fleet.managed_peers import stable_token_env_name

    with pytest.raises(ValueError):
        stable_token_env_name("herdr_session", tmp_path)


# ---------------------------------------------------------------------------
# fleet_config.py — non-herdr regression guards (url check must be unaffected)
# ---------------------------------------------------------------------------


def test_non_herdr_peer_missing_url_still_raises(fleet_home: Path) -> None:
    from a2a_fleet.fleet_config import FleetConfigError, load_fleet

    data = _load_data(fleet_home)
    del data["fleet"]["agents"]["construct"]["url"]
    _write_data(fleet_home, data)
    with pytest.raises(FleetConfigError, match="url"):
        load_fleet()


def test_non_herdr_peer_file_scheme_url_still_raises(fleet_home: Path) -> None:
    from a2a_fleet.fleet_config import FleetConfigError, load_fleet

    data = _load_data(fleet_home)
    data["fleet"]["agents"]["construct"]["url"] = "file:///etc/passwd"
    _write_data(fleet_home, data)
    with pytest.raises(FleetConfigError, match="http or https"):
        load_fleet()


# ---------------------------------------------------------------------------
# fleet_config.py — fleet.herdr block (feature-off default)
# ---------------------------------------------------------------------------


def test_absent_herdr_block_loads_fine_hosts_empty(fleet_home: Path) -> None:
    from a2a_fleet.fleet_config import load_fleet

    cfg = load_fleet()
    assert cfg["herdr"]["hosts"] == {}
    assert cfg["herdr"]["read_only_default"] is True
    assert cfg["herdr"]["require_exact_session"] is True
    assert cfg["herdr"]["require_confirmation_for_mutations"] is True
    assert cfg["herdr"]["deny_raw_input"] is True
    assert cfg["herdr"]["deny_wildcard_operations"] is True


# ---------------------------------------------------------------------------
# fleet_config.py — valid herdr peer + fleet.herdr
# ---------------------------------------------------------------------------


def test_valid_herdr_peer_loads_with_no_url_and_no_token(fleet_home: Path) -> None:
    from a2a_fleet.fleet_config import load_fleet

    data = _load_data(fleet_home)
    _add_herdr_host(data)
    _add_herdr_peer(data)
    _write_data(fleet_home, data)

    cfg = load_fleet()
    peer = cfg["agents"]["herdr-mac-mini"]
    assert peer["url"] is None
    assert peer["agent_card_url"] is None
    assert peer["token"] is None
    assert peer["token_env"] is None
    assert peer["repo_path"] is None
    assert peer["managed"] is True
    assert peer["mode"] == "herdr_session"
    assert peer["host_alias"] == "mac-mini"
    assert peer["workspace"] == "/Users/rohits/hermes"
    assert peer["agent_kind"] == "claude_code"
    assert peer["transport"] == "local_socket"
    # Existing non-herdr peer is unaffected.
    assert cfg["agents"]["construct"]["url"] == "http://127.0.0.1:9320"


# ---------------------------------------------------------------------------
# fleet_config.py — herdr peer field rejection
# ---------------------------------------------------------------------------


def test_herdr_peer_declaring_token_env_raises(fleet_home: Path) -> None:
    from a2a_fleet.fleet_config import FleetConfigError, load_fleet

    data = _load_data(fleet_home)
    _add_herdr_host(data)
    _add_herdr_peer(data, extra={"token_env": "SOME_TOKEN"})
    _write_data(fleet_home, data)
    with pytest.raises(FleetConfigError, match="token_env"):
        load_fleet()


def test_herdr_peer_declaring_repo_path_raises(fleet_home: Path) -> None:
    from a2a_fleet.fleet_config import FleetConfigError, load_fleet

    data = _load_data(fleet_home)
    _add_herdr_host(data)
    _add_herdr_peer(data, extra={"repo_path": "/some/repo"})
    _write_data(fleet_home, data)
    with pytest.raises(FleetConfigError, match="repo_path"):
        load_fleet()


def test_herdr_peer_unknown_host_alias_raises(fleet_home: Path) -> None:
    from a2a_fleet.fleet_config import FleetConfigError, load_fleet

    data = _load_data(fleet_home)
    _add_herdr_host(data)
    _add_herdr_peer(data, host_alias="ghost-host")
    _write_data(fleet_home, data)
    with pytest.raises(FleetConfigError, match="host_alias"):
        load_fleet()


def test_herdr_peer_workspace_outside_allowed_raises(fleet_home: Path) -> None:
    from a2a_fleet.fleet_config import FleetConfigError, load_fleet

    data = _load_data(fleet_home)
    _add_herdr_host(data, allowed_workspaces=["/Users/rohits/hermes"])
    _add_herdr_peer(data, workspace="/Users/rohits/other-project")
    _write_data(fleet_home, data)
    with pytest.raises(FleetConfigError, match="allowed_workspaces"):
        load_fleet()


# ---------------------------------------------------------------------------
# fleet_config.py — fleet.herdr.hosts transport/ssh_target validation
# ---------------------------------------------------------------------------


def test_ssh_bridge_host_without_ssh_target_raises(fleet_home: Path) -> None:
    from a2a_fleet.fleet_config import FleetConfigError, load_fleet

    data = _load_data(fleet_home)
    _add_herdr_host(data, alias="build-host", transport="ssh_bridge")
    _write_data(fleet_home, data)
    with pytest.raises(FleetConfigError, match="ssh_target"):
        load_fleet()


def test_local_socket_host_with_ssh_target_raises(fleet_home: Path) -> None:
    from a2a_fleet.fleet_config import FleetConfigError, load_fleet

    data = _load_data(fleet_home)
    _add_herdr_host(data, transport="local_socket", ssh_target="mac-mini.local")
    _write_data(fleet_home, data)
    with pytest.raises(FleetConfigError, match="ssh_target"):
        load_fleet()


# ---------------------------------------------------------------------------
# client.py — portless transport seam
# ---------------------------------------------------------------------------


def test_fleet_send_to_herdr_peer_with_no_registered_handler_returns_error(
    fleet_home: Path, request: pytest.FixtureRequest
) -> None:
    """No handler registered for herdr_session -> FleetClientError, converted
    by fleet_send_handler into {"error": ...} rather than an unhandled raise.

    The registry is cleared explicitly because it is process-global and
    ``register()`` installs the real Phase 3 route: any earlier test that
    registers the plugin would otherwise make this precondition unreachable
    and quietly turn the assertion into a test of the happy path.
    """
    import a2a_fleet.client as client_mod
    import a2a_fleet.fleet_tools as fleet_tools

    saved = dict(client_mod._PORTLESS_HANDLERS)
    client_mod._PORTLESS_HANDLERS.clear()
    request.addfinalizer(
        lambda: (
            client_mod._PORTLESS_HANDLERS.clear(),
            client_mod._PORTLESS_HANDLERS.update(saved),
        )
    )

    data = _load_data(fleet_home)
    _add_herdr_host(data)
    _add_herdr_peer(data)
    _write_data(fleet_home, data)

    result = asyncio.get_event_loop().run_until_complete(
        fleet_tools.fleet_send_handler(agent="herdr-mac-mini", message="status?")
    )
    assert "error" in result
    assert "herdr" in result["error"]


def test_send_message_routes_herdr_peer_to_registered_handler(fleet_home: Path) -> None:
    """A registered handler for herdr_session is invoked instead of httpx, and
    the non-herdr httpx path is unaffected by registering one."""
    from a2a_fleet.client import register_portless_handler, send_message

    data = _load_data(fleet_home)
    _add_herdr_host(data)
    _add_herdr_peer(data)
    _write_data(fleet_home, data)

    captured: Dict[str, Any] = {}

    async def fake_handler(agent_name, text, *, context_id=None, timeout=30.0, client=None):
        captured["agent_name"] = agent_name
        captured["text"] = text
        return {"reply": "herdr-ack", "context_id": context_id or "ctx-herdr"}

    register_portless_handler("herdr_session", fake_handler)
    try:
        result = asyncio.get_event_loop().run_until_complete(
            send_message("herdr-mac-mini", "hello")
        )
    finally:
        from a2a_fleet.client import _PORTLESS_HANDLERS

        _PORTLESS_HANDLERS.pop("herdr_session", None)

    assert result == {"reply": "herdr-ack", "context_id": "ctx-herdr"}
    assert captured == {"agent_name": "herdr-mac-mini", "text": "hello"}
