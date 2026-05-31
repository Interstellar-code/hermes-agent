"""US-001: fleet_config.py loader behaviour."""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml


def test_load_fleet_happy_path(fleet_home: Path) -> None:
    from a2a_fleet.fleet_config import load_fleet

    cfg = load_fleet()
    assert cfg["enabled"] is True
    assert cfg["response_handler"] == "echo"
    assert cfg["self"]["name"] == "switch"
    assert cfg["self"]["bind_host"] == "127.0.0.1"
    assert cfg["self"]["bind_port"] == 9319
    assert cfg["self"]["token"] == "tok-switch"
    assert "url" not in cfg["self"], "self.url MUST NOT be cached at import time"
    construct = cfg["agents"]["construct"]
    assert construct["url"] == "http://127.0.0.1:9320"
    assert construct["agent_card_url"].endswith("/.well-known/agent-card.json")
    assert construct["token"] == "tok-construct"


def test_response_handler_fail_fast(fleet_home: Path) -> None:
    """Unsupported handlers still raise; 'llm' is now supported (v0.2)."""
    from a2a_fleet.fleet_config import FleetConfigError, load_fleet

    fleet_yaml = fleet_home / "profiles" / "switch" / "fleet.yaml"
    data = yaml.safe_load(fleet_yaml.read_text())
    data["fleet"]["response_handler"] = "not-a-real-handler"
    fleet_yaml.write_text(yaml.safe_dump(data))
    with pytest.raises((FleetConfigError, ValueError)) as exc:
        load_fleet()
    assert "not-a-real-handler" in str(exc.value)


def test_missing_bind_port_raises(fleet_home: Path) -> None:
    from a2a_fleet.fleet_config import FleetConfigError, load_fleet

    fleet_yaml = fleet_home / "profiles" / "switch" / "fleet.yaml"
    data = yaml.safe_load(fleet_yaml.read_text())
    del data["fleet"]["server"]["bind_port"]
    fleet_yaml.write_text(yaml.safe_dump(data))
    with pytest.raises(FleetConfigError):
        load_fleet()


def test_missing_fleet_yaml_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from a2a_fleet.fleet_config import FleetConfigError, load_fleet

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_PROFILE", "ghost")
    with pytest.raises(FleetConfigError):
        load_fleet()


def test_get_agent_unknown_raises(fleet_home: Path) -> None:
    from a2a_fleet.fleet_config import get_agent

    with pytest.raises(KeyError):
        get_agent("nonexistent")


def test_profile_home_reads_fleet_yaml_without_nested_profiles(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Profile-mode HERMES_HOME already points at the active profile directory."""
    from a2a_fleet.fleet_config import load_fleet

    profile_home = tmp_path / ".hermes" / "profiles" / "switch"
    profile_home.mkdir(parents=True)
    fleet_yaml = {
        "fleet": {
            "enabled": True,
            "self": {"name": "switch"},
            "server": {"bind_host": "127.0.0.1", "bind_port": 9319},
            "response_handler": "echo",
            "agents": {},
        }
    }
    (profile_home / "fleet.yaml").write_text(yaml.safe_dump(fleet_yaml))
    monkeypatch.setenv("HERMES_HOME", str(profile_home))
    monkeypatch.setenv("HERMES_PROFILE", "switch")

    cfg = load_fleet()

    assert cfg["self"]["name"] == "switch"
    assert cfg["self"]["bind_port"] == 9319
