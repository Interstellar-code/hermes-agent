"""Regression coverage for Projects as a first-class bundled plugin."""

from hermes_cli.plugins import PluginManager
from hermes_cli.plugins_cmd import _discover_all_plugins


def test_projects_is_discoverable_and_safe_to_enable(monkeypatch, tmp_path):
    """The dashboard API must not leave Projects orphaned from Hub inventory."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    (tmp_path / "config.yaml").write_text(
        "plugins:\n  enabled: [projects]\n", encoding="utf-8"
    )

    discovered = [
        row for row in _discover_all_plugins() if row[0] == "projects"
    ]
    assert len(discovered) == 1
    name, version, _description, source, _path, key = discovered[0]
    assert (name, version, source, key) == (
        "projects",
        "1.0.0",
        "bundled",
        "projects",
    )

    manager = PluginManager()
    manager.discover_and_load()
    loaded = manager._plugins["projects"]
    assert loaded.enabled is True
    assert loaded.error is None
