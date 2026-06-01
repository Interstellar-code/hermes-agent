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


def test_plain_peer_gets_repo_aware_defaults(fleet_home: Path) -> None:
    """Existing url/token peers (no v0.3 fields) parse with inert defaults."""
    from a2a_fleet.fleet_config import load_fleet

    cfg = load_fleet()
    construct = cfg["agents"]["construct"]
    # Pre-existing behaviour unchanged.
    assert construct["url"] == "http://127.0.0.1:9320"
    assert construct["token"] == "tok-construct"
    # New repo-aware fields default to the inert (non-managed) values.
    assert construct["repo_path"] is None
    assert construct["managed"] is False
    assert construct["mode"] is None


def test_repo_aware_peer_fields_parsed(tmp_path: Path, fleet_home: Path) -> None:
    """A managed claude_code peer surfaces repo_path / managed / mode.

    token_env MUST be the stable per-repo name, so use a real on-disk repo dir and
    derive the expected name via stable_token_env_name (single source of truth).
    """
    import os

    from a2a_fleet.cc_deploy import stable_token_env_name
    from a2a_fleet.fleet_config import load_fleet

    repo = tmp_path / "some-repo"
    repo.mkdir()
    canonical = Path(os.path.realpath(str(repo)))
    stable = stable_token_env_name(canonical)

    fleet_yaml = fleet_home / "profiles" / "switch" / "fleet.yaml"
    data = yaml.safe_load(fleet_yaml.read_text())
    data["fleet"]["agents"]["claude-code"] = {
        "url": "http://127.0.0.1:9300",
        "repo_path": str(repo),
        "managed": True,
        "mode": "claude_code",
        "token_env": stable,
    }
    fleet_yaml.write_text(yaml.safe_dump(data))

    cfg = load_fleet()
    cc = cfg["agents"]["claude-code"]
    assert cc["url"] == "http://127.0.0.1:9300"
    assert cc["repo_path"] == str(repo)
    assert cc["managed"] is True
    assert cc["mode"] == "claude_code"
    assert cc["token_env"] == stable
    # Adding a repo-aware peer never disturbs an existing plain peer.
    assert cfg["agents"]["construct"]["managed"] is False


def test_managed_cc_peer_token_env_mismatch_raises(tmp_path: Path, fleet_home: Path) -> None:
    """A managed claude_code peer whose token_env != stable name is rejected (#3/H3)."""
    from a2a_fleet.fleet_config import FleetConfigError, load_fleet

    repo = tmp_path / "myrepo"
    repo.mkdir()
    fleet_yaml = fleet_home / "profiles" / "switch" / "fleet.yaml"
    data = yaml.safe_load(fleet_yaml.read_text())
    data["fleet"]["agents"]["claude-code"] = {
        "url": "http://127.0.0.1:9300",
        "repo_path": str(repo),
        "managed": True,
        "mode": "claude_code",
        "token_env": "A2A_CC_TOKEN_WRONG_NAME",
    }
    fleet_yaml.write_text(yaml.safe_dump(data))
    with pytest.raises(FleetConfigError) as exc:
        load_fleet()
    assert "token_env must be" in str(exc.value)


def test_managed_cc_peer_token_env_matching_ok(tmp_path: Path, fleet_home: Path) -> None:
    """The stable token_env name passes validation."""
    import os

    from a2a_fleet.cc_deploy import stable_token_env_name
    from a2a_fleet.fleet_config import load_fleet

    repo = tmp_path / "myrepo"
    repo.mkdir()
    stable = stable_token_env_name(Path(os.path.realpath(str(repo))))
    fleet_yaml = fleet_home / "profiles" / "switch" / "fleet.yaml"
    data = yaml.safe_load(fleet_yaml.read_text())
    data["fleet"]["agents"]["claude-code"] = {
        "url": "http://127.0.0.1:9300",
        "repo_path": str(repo),
        "managed": True,
        "mode": "claude_code",
        "token_env": stable,
    }
    fleet_yaml.write_text(yaml.safe_dump(data))
    cfg = load_fleet()
    assert cfg["agents"]["claude-code"]["token_env"] == stable


def test_managed_string_false_raises(fleet_home: Path) -> None:
    """managed: "false" (string) must raise, not bool()-coerce to True (#5)."""
    from a2a_fleet.fleet_config import FleetConfigError, load_fleet

    fleet_yaml = fleet_home / "profiles" / "switch" / "fleet.yaml"
    data = yaml.safe_load(fleet_yaml.read_text())
    # Force a STRING value (quoted) past yaml round-trip.
    data["fleet"]["agents"]["construct"]["managed"] = "false"
    fleet_yaml.write_text(yaml.safe_dump(data))
    with pytest.raises(FleetConfigError) as exc:
        load_fleet()
    assert "managed must be a boolean" in str(exc.value)


def test_managed_bool_false_yields_false(fleet_home: Path) -> None:
    """managed: false (real bool) parses to False."""
    from a2a_fleet.fleet_config import load_fleet

    fleet_yaml = fleet_home / "profiles" / "switch" / "fleet.yaml"
    data = yaml.safe_load(fleet_yaml.read_text())
    data["fleet"]["agents"]["construct"]["managed"] = False
    fleet_yaml.write_text(yaml.safe_dump(data))
    cfg = load_fleet()
    assert cfg["agents"]["construct"]["managed"] is False


def test_managed_absent_defaults_false(fleet_home: Path) -> None:
    """Absent managed defaults to False (no key in the peer)."""
    from a2a_fleet.fleet_config import load_fleet

    cfg = load_fleet()
    assert cfg["agents"]["construct"]["managed"] is False


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



def test_managed_oc_peer_token_env_mismatch_raises(tmp_path: Path, fleet_home: Path) -> None:
    from a2a_fleet.fleet_config import FleetConfigError, load_fleet

    repo = tmp_path / "oc-repo"
    repo.mkdir()
    fleet_yaml = fleet_home / "profiles" / "switch" / "fleet.yaml"
    data = yaml.safe_load(fleet_yaml.read_text())
    data["fleet"]["agents"]["opencode"] = {
        "url": "http://127.0.0.1:9310",
        "repo_path": str(repo),
        "managed": True,
        "mode": "opencode",
        "token_env": "A2A_OC_TOKEN_WRONG_NAME",
    }
    fleet_yaml.write_text(yaml.safe_dump(data))
    with pytest.raises(FleetConfigError) as exc:
        load_fleet()
    assert "token_env must be" in str(exc.value)



def test_managed_oc_peer_token_env_matching_ok(tmp_path: Path, fleet_home: Path) -> None:
    from a2a_fleet.oc_deploy import stable_token_env_name
    from a2a_fleet.fleet_config import load_fleet

    repo = tmp_path / "oc-repo"
    repo.mkdir()
    stable = stable_token_env_name(repo.resolve())
    fleet_yaml = fleet_home / "profiles" / "switch" / "fleet.yaml"
    data = yaml.safe_load(fleet_yaml.read_text())
    data["fleet"]["agents"]["opencode"] = {
        "url": "http://127.0.0.1:9310",
        "repo_path": str(repo),
        "managed": True,
        "mode": "opencode",
        "token_env": stable,
    }
    fleet_yaml.write_text(yaml.safe_dump(data))
    cfg = load_fleet()
    peer = cfg["agents"]["opencode"]
    assert peer["managed"] is True
    assert peer["mode"] == "opencode"
    assert peer["token_env"] == stable
