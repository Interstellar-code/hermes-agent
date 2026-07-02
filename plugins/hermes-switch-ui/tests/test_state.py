"""test_state.py — tests for hermes-switch-ui _state.py.

Covers:
 - manifest/settings roundtrip
 - atomic write leaves no .tmp file
 - TTL boundary: fresh -> running True, stale -> running False
 - validate_manifest rejects unknown nested blobs/garbage (ValueError)
 - validate_settings strips token/password/secret keys
 - SWITCHUI_STATE_PATH env override is honored
"""
from __future__ import annotations

import importlib.util
import json
import os
import sys
import time
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Load _state.py via spec_from_file_location
# ---------------------------------------------------------------------------

_PLUGIN_DIR = Path(__file__).resolve().parent.parent


def _load_state(monkeypatch, tmp_path: Path):
    """Load a fresh _state module with SWITCHUI_STATE_PATH pointed at tmp_path."""
    state_file = tmp_path / "state.json"
    monkeypatch.setenv("SWITCHUI_STATE_PATH", str(state_file))

    state_path = _PLUGIN_DIR / "_state.py"
    spec = importlib.util.spec_from_file_location("_state_fresh", state_path)
    mod = importlib.util.module_from_spec(spec)

    # Remove any cached _state_fresh so each call is truly fresh
    sys.modules.pop("_state_fresh", None)
    spec.loader.exec_module(mod)
    return mod, state_file


# ---------------------------------------------------------------------------
# Roundtrip tests
# ---------------------------------------------------------------------------

def test_manifest_roundtrip(monkeypatch, tmp_path):
    """save_manifest then get_status returns the manifest back."""
    st, state_file = _load_state(monkeypatch, tmp_path)

    manifest = {
        "version": "1.2.3",
        "url": "http://localhost:3002",
        "port": 3002,
        "enabled_features": ["chat", "terminal"],
    }
    st.save_manifest(manifest)

    status = st.get_status()
    assert status["manifest"] is not None
    assert status["manifest"]["version"] == "1.2.3"
    assert status["manifest"]["url"] == "http://localhost:3002"
    assert status["manifest"]["port"] == 3002


def test_settings_roundtrip(monkeypatch, tmp_path):
    """save_settings then get_status returns reported_settings back."""
    st, _ = _load_state(monkeypatch, tmp_path)

    settings = {"theme": "dark", "language": "en"}
    st.save_settings(settings)

    status = st.get_status()
    assert status["reported_settings"] is not None
    assert status["reported_settings"]["theme"] == "dark"


def test_state_path_override(monkeypatch, tmp_path):
    """SWITCHUI_STATE_PATH env var controls where state is written."""
    custom = tmp_path / "custom" / "mystate.json"
    monkeypatch.setenv("SWITCHUI_STATE_PATH", str(custom))

    state_path = _PLUGIN_DIR / "_state.py"
    spec = importlib.util.spec_from_file_location("_state_override", state_path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules.pop("_state_override", None)
    spec.loader.exec_module(mod)

    mod.touch_heartbeat()
    assert custom.exists(), f"State file not created at custom path: {custom}"


# ---------------------------------------------------------------------------
# Atomic write test
# ---------------------------------------------------------------------------

def test_atomic_write_no_tmp_left(monkeypatch, tmp_path):
    """After save_manifest, no .tmp file should remain next to state.json."""
    st, state_file = _load_state(monkeypatch, tmp_path)

    manifest = {"version": "1.0.0", "url": "http://localhost:3002", "port": 3002, "enabled_features": []}
    st.save_manifest(manifest)

    tmp_file = state_file.with_suffix(".json.tmp")
    assert not tmp_file.exists(), f".tmp file was left behind: {tmp_file}"
    assert state_file.exists(), "state.json was not created"


# ---------------------------------------------------------------------------
# TTL / heartbeat boundary tests
# ---------------------------------------------------------------------------

def test_heartbeat_fresh_is_running(monkeypatch, tmp_path):
    """A just-stamped heartbeat means running=True."""
    st, _ = _load_state(monkeypatch, tmp_path)

    st.touch_heartbeat()
    status = st.get_status()
    assert status["running"] is True, "Fresh heartbeat should yield running=True"


def test_heartbeat_stale_not_running(monkeypatch, tmp_path):
    """A heartbeat backdated past TTL means running=False."""
    st, state_file = _load_state(monkeypatch, tmp_path)

    # Write a state file with a heartbeat far in the past
    past_ts = "2000-01-01T00:00:00Z"
    state_file.parent.mkdir(parents=True, exist_ok=True)
    state_file.write_text(
        json.dumps({"schema_version": 1, "last_heartbeat": past_ts}),
        encoding="utf-8",
    )

    status = st.get_status()
    assert status["running"] is False, "Stale heartbeat should yield running=False"
    assert status["last_heartbeat"] == past_ts


def test_no_heartbeat_not_running(monkeypatch, tmp_path):
    """No state file at all -> running=False."""
    st, _ = _load_state(monkeypatch, tmp_path)
    # State file does not exist yet
    status = st.get_status()
    assert status["running"] is False


def test_ttl_constant_exposed(monkeypatch, tmp_path):
    """HEARTBEAT_TTL must be a positive integer and match get_status ttl_seconds."""
    st, _ = _load_state(monkeypatch, tmp_path)

    assert isinstance(st.HEARTBEAT_TTL, int)
    assert st.HEARTBEAT_TTL > 0

    st.touch_heartbeat()
    status = st.get_status()
    assert status["ttl_seconds"] == st.HEARTBEAT_TTL


# ---------------------------------------------------------------------------
# validate_manifest tests
# ---------------------------------------------------------------------------

def test_validate_manifest_rejects_non_dict(monkeypatch, tmp_path):
    st, _ = _load_state(monkeypatch, tmp_path)
    with pytest.raises(ValueError, match="JSON object"):
        st.validate_manifest("not a dict")


def test_validate_manifest_rejects_list(monkeypatch, tmp_path):
    st, _ = _load_state(monkeypatch, tmp_path)
    with pytest.raises(ValueError):
        st.validate_manifest([{"version": "1.0.0"}])


def test_validate_manifest_rejects_unknown_nested_blob(monkeypatch, tmp_path):
    """Unknown keys with dict/list values must be rejected."""
    st, _ = _load_state(monkeypatch, tmp_path)
    with pytest.raises(ValueError, match="unknown nested"):
        st.validate_manifest({
            "version": "1.0.0",
            "port": 3002,
            "enabled_features": [],
            "evil_blob": {"nested": "object"},
        })


def test_validate_manifest_rejects_garbage(monkeypatch, tmp_path):
    """Completely garbage input (None) must be rejected."""
    st, _ = _load_state(monkeypatch, tmp_path)
    with pytest.raises((ValueError, TypeError)):
        st.validate_manifest(None)


def test_validate_manifest_valid_passes(monkeypatch, tmp_path):
    """Valid manifest dict passes validation and returns a dict."""
    st, _ = _load_state(monkeypatch, tmp_path)
    result = st.validate_manifest({
        "version": "1.2.3",
        "url": "http://localhost:3002",
        "port": 3002,
        "enabled_features": ["chat"],
    })
    assert isinstance(result, dict)
    assert result["version"] == "1.2.3"


# ---------------------------------------------------------------------------
# validate_settings tests
# ---------------------------------------------------------------------------

def test_validate_settings_strips_token(monkeypatch, tmp_path):
    """Keys containing 'token' are stripped."""
    st, _ = _load_state(monkeypatch, tmp_path)
    result = st.validate_settings({
        "theme": "dark",
        "HERMES_API_TOKEN": "secret123",
        "auth_token": "abc",
    })
    assert "HERMES_API_TOKEN" not in result
    assert "auth_token" not in result
    assert result.get("theme") == "dark"


def test_validate_settings_strips_password(monkeypatch, tmp_path):
    """Keys containing 'password' are stripped."""
    st, _ = _load_state(monkeypatch, tmp_path)
    result = st.validate_settings({
        "language": "en",
        "HERMES_PASSWORD": "hunter2",
        "user_password": "xyz",
    })
    assert "HERMES_PASSWORD" not in result
    assert "user_password" not in result
    assert result.get("language") == "en"


def test_validate_settings_strips_secret(monkeypatch, tmp_path):
    """Keys containing 'secret' are stripped."""
    st, _ = _load_state(monkeypatch, tmp_path)
    result = st.validate_settings({
        "ui_mode": "compact",
        "client_secret": "topsecret",
    })
    assert "client_secret" not in result
    assert result.get("ui_mode") == "compact"


def test_validate_settings_rejects_non_dict(monkeypatch, tmp_path):
    st, _ = _load_state(monkeypatch, tmp_path)
    with pytest.raises(ValueError):
        st.validate_settings("bad input")


def test_settings_secret_keys_not_persisted(monkeypatch, tmp_path):
    """Secret keys must not reach the state file."""
    st, state_file = _load_state(monkeypatch, tmp_path)

    settings = {"theme": "dark", "HERMES_API_TOKEN": "should-not-persist"}
    validated = st.validate_settings(settings)
    st.save_settings(validated)

    raw = json.loads(state_file.read_text(encoding="utf-8"))
    reported = raw.get("reported_settings", {})
    assert "HERMES_API_TOKEN" not in reported
    assert reported.get("theme") == "dark"
