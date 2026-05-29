"""Hardening tests for a2a_fleet — issue #36/#37/#38.

Covers:
- auth_required defaults to False (issue #34 / #38)
- peer URL scheme validation rejects non-http(s) URLs (SSRF, issue #37 / #38)
- /health endpoint does not leak self name or peer names (issue #37)
"""
from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("fastapi", reason="a2a_fleet server tests require hermes-agent[web]")
pytest.importorskip("uvicorn", reason="a2a_fleet server tests require hermes-agent[web]")

import yaml


# ---------------------------------------------------------------------------
# Auth-default behaviour (#34 / #38)
# ---------------------------------------------------------------------------

def test_auth_required_defaults_true(fleet_home: Path) -> None:
    """When auth_required is omitted from fleet.yaml it must default to True (secure default)."""
    from a2a_fleet.fleet_config import load_fleet

    fleet_yaml = fleet_home / "profiles" / "switch" / "fleet.yaml"
    data = yaml.safe_load(fleet_yaml.read_text())
    # Remove auth_required so the default kicks in.
    data["fleet"]["server"].pop("auth_required", None)
    fleet_yaml.write_text(yaml.safe_dump(data))

    cfg = load_fleet()
    assert cfg["self"]["auth_required"] is True, (
        "auth_required must default to True when omitted from fleet.yaml"
    )


# ---------------------------------------------------------------------------
# Peer URL scheme validation — SSRF guard (#37 / #38)
# ---------------------------------------------------------------------------

def _write_fleet_with_peer_url(fleet_home: Path, url: str) -> None:
    fleet_yaml = fleet_home / "profiles" / "switch" / "fleet.yaml"
    data = yaml.safe_load(fleet_yaml.read_text())
    data["fleet"]["agents"]["construct"]["url"] = url
    fleet_yaml.write_text(yaml.safe_dump(data))


def test_peer_url_http_accepted(fleet_home: Path) -> None:
    from a2a_fleet.fleet_config import load_fleet

    _write_fleet_with_peer_url(fleet_home, "http://peer.internal:9320")
    cfg = load_fleet()
    assert cfg["agents"]["construct"]["url"] == "http://peer.internal:9320"


def test_peer_url_https_accepted(fleet_home: Path) -> None:
    from a2a_fleet.fleet_config import load_fleet

    _write_fleet_with_peer_url(fleet_home, "https://peer.example.com:443")
    cfg = load_fleet()
    assert cfg["agents"]["construct"]["url"] == "https://peer.example.com:443"


@pytest.mark.parametrize("bad_url", [
    "file:///etc/passwd",
    "ftp://internal/secret",
    "gopher://evil.example.com",
    "javascript:alert(1)",
    "",
])
def test_peer_url_bad_scheme_rejected(fleet_home: Path, bad_url: str) -> None:
    from a2a_fleet.fleet_config import FleetConfigError, load_fleet

    _write_fleet_with_peer_url(fleet_home, bad_url)
    with pytest.raises(FleetConfigError):
        load_fleet()


def test_peer_agent_card_url_bad_scheme_rejected(fleet_home: Path) -> None:
    from a2a_fleet.fleet_config import FleetConfigError, load_fleet

    fleet_yaml = fleet_home / "profiles" / "switch" / "fleet.yaml"
    data = yaml.safe_load(fleet_yaml.read_text())
    data["fleet"]["agents"]["construct"]["agent_card_url"] = "file:///etc/hosts"
    fleet_yaml.write_text(yaml.safe_dump(data))
    with pytest.raises(FleetConfigError):
        load_fleet()


# ---------------------------------------------------------------------------
# /health does not leak self name or peer names (#37)
# ---------------------------------------------------------------------------

def test_health_does_not_leak_names(fleet_home: Path) -> None:
    """/health must not expose self name or enumerate peer names."""
    import asyncio
    from a2a_fleet.server import build_app
    from httpx import AsyncClient, ASGITransport

    app = build_app()

    async def _get_health() -> dict:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get("/health")
            assert resp.status_code == 200
            return resp.json()

    body = asyncio.run(_get_health())
    assert "self" not in body, "/health must not expose self name"
    assert "peers" not in body, "/health must not enumerate peer names"
    assert "ok" in body
    assert "peer_count" in body
