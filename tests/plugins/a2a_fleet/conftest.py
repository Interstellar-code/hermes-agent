"""Shared pytest fixtures for a2a_fleet tests."""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest
import yaml

# Make `a2a_fleet` importable when pytest is invoked from anywhere.
# File is at tests/plugins/a2a_fleet/conftest.py; plugins/ is 3 levels up then down.
REPO_ROOT = Path(__file__).resolve().parents[3]
PLUGINS_DIR = REPO_ROOT / "plugins"
if str(PLUGINS_DIR) not in sys.path:
    sys.path.insert(0, str(PLUGINS_DIR))


@pytest.fixture
def fleet_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Create a temp ``HERMES_HOME`` with a default switch profile + fleet.yaml."""
    profile_dir = tmp_path / "profiles" / "switch"
    profile_dir.mkdir(parents=True)
    fleet_yaml = {
        "fleet": {
            "enabled": True,
            "self": {"name": "switch"},
            "server": {
                "bind_host": "127.0.0.1",
                "bind_port": 9319,
                "auth_required": False,
                "token_env": "SWITCH_A2A_TOKEN",
            },
            "response_handler": "echo",
            "agents": {
                "construct": {
                    "url": "http://127.0.0.1:9320",
                    "agent_card_url": "http://127.0.0.1:9320/.well-known/agent-card.json",
                    "token_env": "CONSTRUCT_A2A_TOKEN",
                    "description": "Test peer",
                },
            },
        },
    }
    (profile_dir / "fleet.yaml").write_text(yaml.safe_dump(fleet_yaml))
    (tmp_path / "active_profile").write_text("switch")
    monkeypatch.setenv("HERMES_HOME", str(profile_dir))
    monkeypatch.setenv("SWITCH_A2A_TOKEN", "tok-switch")
    monkeypatch.setenv("CONSTRUCT_A2A_TOKEN", "tok-construct")
    return tmp_path
