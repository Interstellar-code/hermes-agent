"""Tests for fleet_yaml_io — first-enable scaffold + comment-preserving peer upsert.

Covers the v0.4 onboarding fixes:
  * ensure_example_fleet_yaml(): scaffolds a loadable fleet.yaml on a fresh
    profile (no more silent-idle), idempotent.
  * upsert_cc_peer(): surgically wires a managed claude_code peer while preserving
    the operator's comments; idempotent; no_auth -> plain url peer; name-collision
    across repos gets a distinct peer name.
"""
from __future__ import annotations

from pathlib import Path

import pytest

import a2a_fleet.cc_deploy as cc_deploy
import a2a_fleet.fleet_config as fleet_config
import a2a_fleet.fleet_yaml_io as fyio


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Empty HERMES_HOME with no fleet.yaml (a fresh profile)."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.delenv("HERMES_PROFILE", raising=False)
    return tmp_path


def _stable_env(repo: Path) -> str:
    canon, _ = cc_deploy.canonicalize_repo_path(str(repo))
    return cc_deploy.stable_token_env_name(canon)


# --------------------------------------------------------------------------- #
# Scaffold
# --------------------------------------------------------------------------- #

def test_scaffold_creates_loadable_fleet_yaml(home: Path):
    path, created = fyio.ensure_example_fleet_yaml()
    assert created is True
    assert path == home / "fleet.yaml"
    assert path.is_file()

    cfg = fleet_config.load_fleet()
    assert cfg["enabled"] is True
    assert cfg["response_handler"] == "agent"
    assert cfg["self"]["bind_port"] == 9219
    assert cfg["agents"] == {}  # empty peers map, ready for auto-wiring


def test_scaffold_is_idempotent(home: Path):
    path, created1 = fyio.ensure_example_fleet_yaml()
    body1 = path.read_text()
    path2, created2 = fyio.ensure_example_fleet_yaml()
    assert created1 is True and created2 is False
    assert path2 == path
    assert path.read_text() == body1  # untouched


def test_scaffold_keeps_existing_file(home: Path):
    path = home / "fleet.yaml"
    path.write_text("fleet:\n  enabled: false\n")
    _, created = fyio.ensure_example_fleet_yaml()
    assert created is False
    assert path.read_text() == "fleet:\n  enabled: false\n"


# --------------------------------------------------------------------------- #
# Managed peer upsert
# --------------------------------------------------------------------------- #

def test_upsert_managed_peer_into_scaffold(home: Path, tmp_path: Path):
    repo = tmp_path / "myrepo"
    repo.mkdir()
    token_env = _stable_env(repo)

    res = fyio.upsert_cc_peer(
        repo_path=str(repo), url="http://127.0.0.1:9300", token_env=token_env,
    )
    assert res["action"] == "created"
    assert res["name"] == "claude-code"

    cfg = fleet_config.load_fleet()
    peer = cfg["agents"]["claude-code"]
    assert peer["url"] == "http://127.0.0.1:9300"
    assert peer["managed"] is True
    assert peer["mode"] == "claude_code"
    assert peer["token_env"] == token_env
    # repo_path is canonicalized on load; compare to the realpath the deploy uses.
    canon, _ = cc_deploy.canonicalize_repo_path(str(repo))
    assert peer["repo_path"] == str(canon)


def test_upsert_preserves_comments(home: Path, tmp_path: Path):
    path, _ = fyio.ensure_example_fleet_yaml()
    repo = tmp_path / "myrepo"
    repo.mkdir()
    fyio.upsert_cc_peer(
        repo_path=str(repo), url="http://127.0.0.1:9300", token_env=_stable_env(repo),
    )
    body = path.read_text()
    # A distinctive comment from the scaffold survives the round-trip write.
    assert "Scaffolded by the a2a_fleet plugin" in body
    assert "dispatch into the REAL Hermes agent" in body


def test_upsert_is_idempotent(home: Path, tmp_path: Path):
    repo = tmp_path / "myrepo"
    repo.mkdir()
    token_env = _stable_env(repo)
    fyio.upsert_cc_peer(repo_path=str(repo), url="http://127.0.0.1:9300", token_env=token_env)
    res2 = fyio.upsert_cc_peer(repo_path=str(repo), url="http://127.0.0.1:9300", token_env=token_env)
    assert res2["action"] == "unchanged"


def test_upsert_updates_changed_url(home: Path, tmp_path: Path):
    repo = tmp_path / "myrepo"
    repo.mkdir()
    token_env = _stable_env(repo)
    fyio.upsert_cc_peer(repo_path=str(repo), url="http://127.0.0.1:9300", token_env=token_env)
    res = fyio.upsert_cc_peer(repo_path=str(repo), url="http://127.0.0.1:9999", token_env=token_env)
    assert res["action"] == "updated"
    cfg = fleet_config.load_fleet()
    assert cfg["agents"]["claude-code"]["url"] == "http://127.0.0.1:9999"


def test_upsert_no_auth_writes_plain_peer(home: Path, tmp_path: Path):
    repo = tmp_path / "myrepo"
    repo.mkdir()
    res = fyio.upsert_cc_peer(repo_path=str(repo), url="http://127.0.0.1:9300", token_env="")
    assert res["action"] == "created"
    cfg = fleet_config.load_fleet()
    peer = cfg["agents"]["claude-code"]
    assert peer["url"] == "http://127.0.0.1:9300"
    assert peer["managed"] is False
    assert peer["token_env"] is None
    assert peer["repo_path"] is None  # no_auth peer carries no managed markers


def test_upsert_idempotent_across_disk_reload(home: Path, tmp_path: Path):
    """Each upsert re-reads fleet.yaml from disk; a re-upsert of identical managed
    data must report 'unchanged' (ruamel round-trip types compare equal to the
    freshly-assigned plain values) so the file mtime/comments don't churn."""
    repo = tmp_path / "myrepo"
    repo.mkdir()
    token_env = _stable_env(repo)
    fyio.upsert_cc_peer(repo_path=str(repo), url="http://127.0.0.1:9300", token_env=token_env)
    body_after_first = (home / "fleet.yaml").read_text()
    res2 = fyio.upsert_cc_peer(repo_path=str(repo), url="http://127.0.0.1:9300", token_env=token_env)
    assert res2["action"] == "unchanged"
    assert (home / "fleet.yaml").read_text() == body_after_first  # byte-identical


def test_upsert_distinct_name_for_second_repo(home: Path, tmp_path: Path):
    repo_a = tmp_path / "repo-a"
    repo_a.mkdir()
    repo_b = tmp_path / "repo-b"
    repo_b.mkdir()
    fyio.upsert_cc_peer(repo_path=str(repo_a), url="http://127.0.0.1:9300", token_env=_stable_env(repo_a))
    res_b = fyio.upsert_cc_peer(repo_path=str(repo_b), url="http://127.0.0.1:9301", token_env=_stable_env(repo_b))
    assert res_b["name"] == "claude-code-repo-b"

    cfg = fleet_config.load_fleet()
    assert "claude-code" in cfg["agents"]
    assert "claude-code-repo-b" in cfg["agents"]


def test_upsert_creates_file_when_absent(home: Path, tmp_path: Path):
    # No prior scaffold call — upsert must bootstrap the file itself.
    repo = tmp_path / "myrepo"
    repo.mkdir()
    assert not (home / "fleet.yaml").exists()
    res = fyio.upsert_cc_peer(
        repo_path=str(repo), url="http://127.0.0.1:9300", token_env=_stable_env(repo),
    )
    assert res["action"] == "created"
    assert (home / "fleet.yaml").is_file()
