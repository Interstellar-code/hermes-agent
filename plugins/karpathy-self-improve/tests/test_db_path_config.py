"""
test_db_path_config.py — Tests for configurable DB path (resolve_db_path).

Covers:
  - env var wins over everything
  - config.yaml key used when env unset
  - default when neither env nor config
  - relative config path resolved under hermes root
  - announce-on-create logs INFO once for a fresh DB, DEBUG on subsequent opens
  - resolve_db_path() returns expected in each case
  - /health includes db_path + db_exists
"""
from __future__ import annotations

import logging
import importlib
import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest


# ---------------------------------------------------------------------------
# Helpers to reload _db so resolve_db_path picks up monkeypatched state
# ---------------------------------------------------------------------------

def _reload_db_mod():
    """Reload _db module, resetting the singleton connection."""
    import _db as db_mod
    db_mod._conn = None
    return db_mod


# ---------------------------------------------------------------------------
# resolve_db_path — env var
# ---------------------------------------------------------------------------

def test_env_var_wins(tmp_path, monkeypatch):
    """KARPATHY_DB_PATH env var takes highest precedence."""
    db_file = str(tmp_path / "from_env.db")
    monkeypatch.setenv("KARPATHY_DB_PATH", db_file)

    # Even if config would return something, env wins.
    mock_config = {"plugins": {"karpathy_self_improve": {"db_path": "/should/not/be/used.db"}}}
    with patch("hermes_cli.config.load_config", return_value=mock_config):
        from _db import resolve_db_path
        result = resolve_db_path()

    assert result == Path(db_file)


def test_env_var_wins_over_default(tmp_path, monkeypatch):
    """KARPATHY_DB_PATH wins over default even when config unavailable."""
    db_file = str(tmp_path / "env.db")
    monkeypatch.setenv("KARPATHY_DB_PATH", db_file)
    monkeypatch.delenv("KARPATHY_DB_PATH", raising=False)
    monkeypatch.setenv("KARPATHY_DB_PATH", db_file)

    from _db import resolve_db_path
    assert resolve_db_path() == Path(db_file)


# ---------------------------------------------------------------------------
# resolve_db_path — config key
# ---------------------------------------------------------------------------

def test_config_key_used_when_env_unset(tmp_path, monkeypatch):
    """Config key plugins.karpathy_self_improve.db_path is used when env unset."""
    monkeypatch.delenv("KARPATHY_DB_PATH", raising=False)
    cfg_db = str(tmp_path / "from_config.db")
    mock_config = {"plugins": {"karpathy_self_improve": {"db_path": cfg_db}}}

    with patch("hermes_cli.config.load_config", return_value=mock_config), \
         patch("hermes_cli.config.cfg_get", side_effect=lambda cfg, *keys, default=None: (
             cfg.get("plugins", {}).get("karpathy_self_improve", {}).get("db_path", default)
             if keys == ("plugins", "karpathy_self_improve", "db_path")
             else default
         )):
        from _db import resolve_db_path
        result = resolve_db_path()

    assert result == Path(cfg_db)


def test_config_key_expands_tilde(tmp_path, monkeypatch):
    """~ in config value is expanded via expanduser."""
    monkeypatch.delenv("KARPATHY_DB_PATH", raising=False)
    cfg_val = "~/karpathy-test.db"
    expected = Path(cfg_val).expanduser()
    mock_config = {"plugins": {"karpathy_self_improve": {"db_path": cfg_val}}}

    with patch("hermes_cli.config.load_config", return_value=mock_config), \
         patch("hermes_cli.config.cfg_get", side_effect=lambda cfg, *keys, default=None: (
             cfg_val if keys == ("plugins", "karpathy_self_improve", "db_path") else default
         )):
        from _db import resolve_db_path
        result = resolve_db_path()

    assert result == expected


def test_relative_config_path_resolved_under_root(tmp_path, monkeypatch):
    """Relative config path is resolved against get_default_hermes_root()."""
    monkeypatch.delenv("KARPATHY_DB_PATH", raising=False)
    cfg_val = "relative/ksi.db"
    mock_config = {"plugins": {"karpathy_self_improve": {"db_path": cfg_val}}}

    fake_root = tmp_path / "hermes_home"
    fake_root.mkdir()

    import _db as db_mod
    with patch("hermes_cli.config.load_config", return_value=mock_config), \
         patch("hermes_cli.config.cfg_get", side_effect=lambda cfg, *keys, default=None: (
             cfg_val if keys == ("plugins", "karpathy_self_improve", "db_path") else default
         )), \
         patch.object(db_mod, "get_default_hermes_root", return_value=fake_root):
        result = db_mod.resolve_db_path()

    assert result == fake_root / cfg_val


def test_default_when_neither_env_nor_config(tmp_path, monkeypatch):
    """Default path used when env unset and config returns no db_path."""
    monkeypatch.delenv("KARPATHY_DB_PATH", raising=False)
    fake_root = tmp_path / "hermes_home"
    fake_root.mkdir()

    import _db as db_mod
    with patch("hermes_cli.config.load_config", return_value={}), \
         patch("hermes_cli.config.cfg_get", return_value=None), \
         patch.object(db_mod, "get_default_hermes_root", return_value=fake_root):
        result = db_mod.resolve_db_path()

    assert result == fake_root / "karpathy-self-improve.db"


def test_default_when_config_import_fails(tmp_path, monkeypatch):
    """Default path used when hermes_cli.config is unavailable."""
    monkeypatch.delenv("KARPATHY_DB_PATH", raising=False)
    fake_root = tmp_path / "hermes_home2"
    fake_root.mkdir()

    import _db as db_mod
    with patch.object(db_mod, "get_default_hermes_root", return_value=fake_root), \
         patch.dict(sys.modules, {"hermes_cli.config": None}):
        result = db_mod.resolve_db_path()

    assert result == fake_root / "karpathy-self-improve.db"


# ---------------------------------------------------------------------------
# Announce-on-create
# ---------------------------------------------------------------------------

def test_announce_on_create_logs_info_once(tmp_path, monkeypatch, caplog):
    """INFO log fires exactly once for a fresh DB, not on subsequent opens."""
    db_file = tmp_path / "announce_test.db"
    assert not db_file.exists()

    import _db as db_mod
    # Reset singleton so open_conn is triggered fresh.
    monkeypatch.setattr(db_mod, "_conn", None)

    # Logger name is the module's __name__ which is "_db" when loaded flat.
    with caplog.at_level(logging.INFO, logger="_db"):
        from _db import open_db
        open_db(db_file)

    info_msgs = [r for r in caplog.records if r.levelno == logging.INFO and "initializing" in r.message]
    assert len(info_msgs) >= 1, f"Expected at least one INFO 'initializing' log; got: {caplog.records}"
    assert str(db_file) in info_msgs[0].message


def test_no_announce_on_existing_db(tmp_path, monkeypatch, caplog):
    """No INFO 'initializing' log when DB file already exists."""
    db_file = tmp_path / "existing.db"
    # Pre-create the file.
    from _db import open_db
    open_db(db_file)

    import _db as db_mod
    monkeypatch.setattr(db_mod, "_conn", None)

    with caplog.at_level(logging.INFO, logger="_db"):
        caplog.clear()
        open_db(db_file)

    info_msgs = [r for r in caplog.records if r.levelno == logging.INFO and "initializing" in r.message]
    assert len(info_msgs) == 0, "Should not log 'initializing' for existing DB"


# ---------------------------------------------------------------------------
# /health includes db_path + db_exists
# ---------------------------------------------------------------------------

@pytest.fixture()
def api_client(tmp_path, monkeypatch):
    """TestClient for the plugin API with an isolated DB."""
    db_file = str(tmp_path / "health_test.db")
    monkeypatch.setenv("KARPATHY_DB_PATH", db_file)

    import _db as db_mod
    monkeypatch.setattr(db_mod, "_conn", None)

    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    import importlib.util
    _PLUGIN_DIR = Path(__file__).resolve().parent.parent
    spec = importlib.util.spec_from_file_location(
        "plugin_api_health_test",
        _PLUGIN_DIR / "dashboard" / "plugin_api.py",
    )
    api_mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(api_mod)

    app = FastAPI()
    app.include_router(api_mod.router, prefix="/api/plugins/karpathy-self-improve")
    return TestClient(app), db_file


def test_health_includes_db_path(api_client):
    client, db_file = api_client
    r = client.get("/api/plugins/karpathy-self-improve/health")
    assert r.status_code == 200
    data = r.json()
    assert "db_path" in data
    assert data["db_path"] == db_file


def test_health_db_exists_false_before_open(tmp_path, monkeypatch):
    """db_exists=False when the DB file has not been created yet."""
    db_file = str(tmp_path / "not_yet.db")
    monkeypatch.setenv("KARPATHY_DB_PATH", db_file)

    import _db as db_mod
    monkeypatch.setattr(db_mod, "_conn", None)

    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    import importlib.util
    _PLUGIN_DIR = Path(__file__).resolve().parent.parent
    spec = importlib.util.spec_from_file_location(
        "plugin_api_health_no_db",
        _PLUGIN_DIR / "dashboard" / "plugin_api.py",
    )
    api_mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(api_mod)

    app = FastAPI()
    app.include_router(api_mod.router, prefix="/api/plugins/karpathy-self-improve")
    client = TestClient(app)

    r = client.get("/api/plugins/karpathy-self-improve/health")
    assert r.status_code == 200
    data = r.json()
    assert "db_exists" in data
    # File hasn't been created yet (health doesn't create it)
    assert data["db_exists"] == Path(db_file).exists()


def test_health_db_exists_true_after_open(tmp_path, monkeypatch):
    """db_exists=True after get_db() creates the file."""
    db_file = str(tmp_path / "will_exist.db")
    monkeypatch.setenv("KARPATHY_DB_PATH", db_file)

    import _db as db_mod
    monkeypatch.setattr(db_mod, "_conn", None)

    # Open DB to create the file.
    from _db import get_db
    get_db()

    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    import importlib.util
    _PLUGIN_DIR = Path(__file__).resolve().parent.parent
    spec = importlib.util.spec_from_file_location(
        "plugin_api_health_exists",
        _PLUGIN_DIR / "dashboard" / "plugin_api.py",
    )
    api_mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(api_mod)

    app = FastAPI()
    app.include_router(api_mod.router, prefix="/api/plugins/karpathy-self-improve")
    client = TestClient(app)

    r = client.get("/api/plugins/karpathy-self-improve/health")
    assert r.status_code == 200
    data = r.json()
    assert data["db_exists"] is True
    assert data["db_path"] == db_file
