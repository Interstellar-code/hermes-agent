"""
Test dynamic HERMES_HOME profile path resolution across workflow-engine seams (#197).
"""
import os
from pathlib import Path

from engine.wiring import create_engine, _resolve_db_path, get_default_db_path
from engine.db.migrate import get_default_lock_path
from engine.discovery.loader import get_user_workflows_dir
from engine.runtime.manifest import get_manifest_path


def test_workflow_engine_paths_resolve_dynamically_with_hermes_home(tmp_path, monkeypatch):
    profile_home_1 = tmp_path / "profile1"
    profile_home_2 = tmp_path / "profile2"
    profile_home_1.mkdir()
    profile_home_2.mkdir()

    monkeypatch.setenv("HERMES_HOME", str(profile_home_1))
    assert get_default_db_path() == str(profile_home_1 / "switchui-workflows.db")
    assert _resolve_db_path(None) == str(profile_home_1 / "switchui-workflows.db")
    assert get_default_lock_path() == profile_home_1 / "switchui-workflows.db.migrate.lock"
    assert get_user_workflows_dir() == profile_home_1 / "workflows"
    assert get_manifest_path() == profile_home_1 / "workflows-manifest.json"

    # Switch HERMES_HOME at runtime
    monkeypatch.setenv("HERMES_HOME", str(profile_home_2))
    assert get_default_db_path() == str(profile_home_2 / "switchui-workflows.db")
    assert _resolve_db_path(None) == str(profile_home_2 / "switchui-workflows.db")
    assert get_default_lock_path() == profile_home_2 / "switchui-workflows.db.migrate.lock"
    assert get_user_workflows_dir() == profile_home_2 / "workflows"
    assert get_manifest_path() == profile_home_2 / "workflows-manifest.json"
