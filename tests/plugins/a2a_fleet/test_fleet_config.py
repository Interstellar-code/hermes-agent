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


# ---------------------------------------------------------------------------
# Issue #84 — system_prompt_file path-traversal guard
# ---------------------------------------------------------------------------


def _write_fleet_with_sp_file(fleet_yaml_path: Path, sp_file_value: str) -> None:
    """Rewrite fleet.yaml to set llm.system_prompt_file."""
    data = yaml.safe_load(fleet_yaml_path.read_text())
    data["fleet"]["response_handler"] = "llm"
    data["fleet"]["llm"] = {"system_prompt_file": sp_file_value}
    fleet_yaml_path.write_text(yaml.safe_dump(data))


def test_system_prompt_file_outside_hermes_home_raises(
    fleet_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """system_prompt_file pointing outside get_hermes_home() raises FleetConfigError (#84)."""
    from a2a_fleet.fleet_config import FleetConfigError, load_fleet

    fleet_yaml = fleet_home / "profiles" / "switch" / "fleet.yaml"
    _write_fleet_with_sp_file(fleet_yaml, "/etc/passwd")
    with pytest.raises(FleetConfigError, match="must be within"):
        load_fleet()


def test_system_prompt_file_dotdot_traversal_raises(
    fleet_home: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A relative path that escapes root via .. raises FleetConfigError (#84)."""
    from a2a_fleet.fleet_config import FleetConfigError, load_fleet

    # Create a file outside hermes_home to reference via traversal.
    outside_file = tmp_path / "secret.txt"
    outside_file.write_text("secret")

    # HERMES_HOME is profile_dir inside tmp_path; build a relative escape.
    # fleet_home fixture sets HERMES_HOME = profile_dir = tmp_path/profiles/switch.
    # A relative path "../../secret.txt" from that root would resolve to tmp_path/secret.txt.
    fleet_yaml = fleet_home / "profiles" / "switch" / "fleet.yaml"
    _write_fleet_with_sp_file(fleet_yaml, "../../secret.txt")
    with pytest.raises(FleetConfigError, match="must be within"):
        load_fleet()


def test_system_prompt_file_inside_hermes_home_loads_ok(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """system_prompt_file inside get_hermes_home() loads without error, path resolved (#84)."""
    from a2a_fleet.fleet_config import load_fleet

    # Build a fresh hermes_home so we fully control its contents.
    hermes_home = tmp_path / "hermes_home"
    hermes_home.mkdir()
    sp_file = hermes_home / "system_prompt.txt"
    sp_file.write_text("You are a helpful assistant.")

    fleet_yaml_data = {
        "fleet": {
            "enabled": True,
            "self": {"name": "switch"},
            "server": {"bind_host": "127.0.0.1", "bind_port": 9319},
            "response_handler": "llm",
            "agents": {},
            "llm": {"system_prompt_file": str(sp_file)},
        }
    }
    (hermes_home / "fleet.yaml").write_text(yaml.safe_dump(fleet_yaml_data))
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))

    cfg = load_fleet()
    # The stored path must be the resolved absolute path.
    assert cfg["llm"]["system_prompt_file"] == str(sp_file.resolve())


def test_system_prompt_file_absent_does_not_raise(fleet_home: Path) -> None:
    """When system_prompt_file is not set, load_fleet() succeeds normally (#84)."""
    from a2a_fleet.fleet_config import load_fleet

    cfg = load_fleet()
    assert cfg["llm"]["system_prompt_file"] is None



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


# Issue #104 — managed-peer token resolves from the persisted .token file when
# the canonical env var is unset in this process (no bearer -> 401 otherwise).
def test_managed_token_falls_back_to_token_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from a2a_fleet.fleet_config import _resolve_managed_token

    repo = tmp_path / "repo"
    (repo / ".hermes").mkdir(parents=True)
    (repo / ".hermes" / ".oc-token").write_text("FILE_TOKEN_123\n", encoding="utf-8")

    monkeypatch.delenv("A2A_OC_TOKEN_X", raising=False)
    # env unset -> read from <repo>/.hermes/.oc-token
    assert _resolve_managed_token("A2A_OC_TOKEN_X", "opencode", str(repo)) == "FILE_TOKEN_123"


def test_managed_token_file_takes_precedence_over_stale_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # P0-3: the persisted .token is authoritative (what the running receiver
    # requires; every deploy writes it last). A STALE env var from an
    # out-of-process redeploy must NOT shadow it (that sent the wrong bearer -> 401).
    from a2a_fleet.fleet_config import _resolve_managed_token

    repo = tmp_path / "repo"
    (repo / ".hermes").mkdir(parents=True)
    (repo / ".hermes" / ".codex-token").write_text("FRESH_FILE\n", encoding="utf-8")
    monkeypatch.setenv("A2A_CODEX_TOKEN_X", "STALE_ENV")
    assert _resolve_managed_token("A2A_CODEX_TOKEN_X", "codex", str(repo)) == "FRESH_FILE"


def test_managed_token_env_fallback_when_no_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # File absent (receiver not deployed on this host yet) -> env var is used.
    from a2a_fleet.fleet_config import _resolve_managed_token

    repo = tmp_path / "repo"
    (repo / ".hermes").mkdir(parents=True)
    monkeypatch.setenv("A2A_OC_TOKEN_X", "ENV_ONLY")
    assert _resolve_managed_token("A2A_OC_TOKEN_X", "opencode", str(repo)) == "ENV_ONLY"


def test_managed_token_none_for_unknown_mode_or_missing_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from a2a_fleet.fleet_config import _resolve_managed_token

    monkeypatch.delenv("A2A_X_TOKEN", raising=False)
    assert _resolve_managed_token("A2A_X_TOKEN", "bogus_mode", str(tmp_path)) is None
    assert _resolve_managed_token("A2A_OC_TOKEN_X", "opencode", None) is None
