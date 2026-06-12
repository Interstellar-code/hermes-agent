"""CLI tests for kanban template subcommands (_dispatch_templates).

Tests _cmd_template_list, _cmd_template_create, _cmd_template_delete,
_cmd_template_instantiate, and _cmd_template_save_board via argparse Namespace.
"""

from __future__ import annotations

import argparse
import sys
from io import StringIO
from pathlib import Path

import pytest

import hermes_cli.kanban_db as kb
from hermes_cli import kanban_templates as kt
from hermes_cli.kanban import _dispatch_templates


# ---------------------------------------------------------------------------
# Fixture
# ---------------------------------------------------------------------------

@pytest.fixture()
def template_home(tmp_path, monkeypatch):
    """Isolated HERMES_HOME with empty kanban + templates directories."""
    home = tmp_path / "hermes_home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    for var in (
        "HERMES_KANBAN_DB",
        "HERMES_KANBAN_WORKSPACES_ROOT",
        "HERMES_KANBAN_HOME",
        "HERMES_KANBAN_BOARD",
    ):
        monkeypatch.delenv(var, raising=False)
    try:
        import hermes_constants
        hermes_constants._cached_default_hermes_root = None  # type: ignore[attr-defined]
    except Exception:
        pass
    kb._INITIALIZED_PATHS.clear()
    kb.init_db()
    return home


def _args(**kwargs) -> argparse.Namespace:
    """Build an argparse Namespace with defaults required by dispatch."""
    defaults = {
        "template_action": "list",
        "slug": None,
        "file": None,
        "yes": False,
        "vars": [],
        "template_board": None,
        "dispatch": False,
        "keep_status": False,
        "reset_status": True,
    }
    defaults.update(kwargs)
    return argparse.Namespace(**defaults)


# ---------------------------------------------------------------------------
# Minimal YAML
# ---------------------------------------------------------------------------

_MINIMAL_YAML = """\
schema: 1
name: CLI Template
tasks:
  - key: a
    title: "Task A"
  - key: b
    title: "Task B"
"""

_YAML_WITH_BOARD = """\
schema: 1
name: Board Template
board:
  slug: cli-board
tasks:
  - key: t1
    title: "Task 1"
  - key: t2
    title: "Task 2"
"""


# ---------------------------------------------------------------------------
# template list
# ---------------------------------------------------------------------------

class TestTemplateList:
    def test_empty_list_exits_zero(self, template_home, capsys):
        rc = _dispatch_templates(_args(template_action="list"))
        assert rc == 0
        out = capsys.readouterr().out
        assert "no templates" in out.lower() or out == "" or "(no templates" in out

    def test_lists_saved_template(self, template_home, capsys):
        kt.save_template("my-tmpl", _MINIMAL_YAML)
        rc = _dispatch_templates(_args(template_action="list"))
        assert rc == 0
        out = capsys.readouterr().out
        assert "my-tmpl" in out


# ---------------------------------------------------------------------------
# template create
# ---------------------------------------------------------------------------

class TestTemplateCreate:
    def test_create_from_yaml_file(self, template_home, tmp_path, capsys):
        yaml_file = tmp_path / "template.yaml"
        # Include slug in YAML
        yaml_file.write_text("slug: file-tmpl\n" + _MINIMAL_YAML, encoding="utf-8")
        rc = _dispatch_templates(_args(
            template_action="create",
            file=str(yaml_file),
        ))
        assert rc == 0
        out = capsys.readouterr().out
        assert "file-tmpl" in out

    def test_create_with_slug_override(self, template_home, tmp_path, capsys):
        yaml_file = tmp_path / "template.yaml"
        yaml_file.write_text(_MINIMAL_YAML, encoding="utf-8")
        rc = _dispatch_templates(_args(
            template_action="create",
            file=str(yaml_file),
            slug="override-slug",
        ))
        assert rc == 0
        # Verify it was actually saved
        assert any(t["slug"] == "override-slug" for t in kt.list_templates())

    def test_create_missing_file_exits_nonzero(self, template_home, capsys):
        rc = _dispatch_templates(_args(
            template_action="create",
            file="/nonexistent/path/template.yaml",
        ))
        assert rc != 0

    def test_create_no_slug_in_yaml_and_no_override_exits_nonzero(self, template_home, tmp_path, capsys):
        yaml_file = tmp_path / "template.yaml"
        # YAML with no slug field
        yaml_file.write_text(_MINIMAL_YAML, encoding="utf-8")
        rc = _dispatch_templates(_args(
            template_action="create",
            file=str(yaml_file),
            slug=None,
        ))
        assert rc != 0


# ---------------------------------------------------------------------------
# template delete
# ---------------------------------------------------------------------------

class TestTemplateDelete:
    def test_delete_with_yes_flag(self, template_home, capsys):
        kt.save_template("del-me", _MINIMAL_YAML)
        rc = _dispatch_templates(_args(
            template_action="delete",
            slug="del-me",
            yes=True,
        ))
        assert rc == 0
        assert not any(t["slug"] == "del-me" for t in kt.list_templates())

    def test_delete_nonexistent_exits_nonzero(self, template_home, capsys):
        rc = _dispatch_templates(_args(
            template_action="delete",
            slug="no-such",
            yes=True,
        ))
        assert rc != 0

    def test_delete_aborted_when_not_yes(self, template_home, monkeypatch, capsys):
        kt.save_template("keep-me", _MINIMAL_YAML)
        # Simulate user typing "n"
        monkeypatch.setattr("builtins.input", lambda _: "n")
        rc = _dispatch_templates(_args(
            template_action="delete",
            slug="keep-me",
            yes=False,
        ))
        assert rc == 0
        # Template should still exist
        assert any(t["slug"] == "keep-me" for t in kt.list_templates())


# ---------------------------------------------------------------------------
# template instantiate
# ---------------------------------------------------------------------------

class TestTemplateInstantiate:
    def test_instantiate_happy_path(self, template_home, capsys):
        kt.save_template("board-tmpl", _YAML_WITH_BOARD)
        rc = _dispatch_templates(_args(
            template_action="instantiate",
            slug="board-tmpl",
            vars=[],
            template_board=None,
            dispatch=False,
        ))
        assert rc == 0
        out = capsys.readouterr().out
        assert "board" in out.lower() or "cli-board" in out

    def test_instantiate_with_var(self, template_home, tmp_path, capsys):
        yaml_with_var = """\
schema: 1
name: Var Template
board:
  slug: var-board
variables:
  - key: project
    default: default-proj
tasks:
  - key: a
    title: "{{project}} Task A"
"""
        kt.save_template("var-tmpl", yaml_with_var)
        rc = _dispatch_templates(_args(
            template_action="instantiate",
            slug="var-tmpl",
            vars=["project=myproj"],
            template_board="var-board",
            dispatch=False,
        ))
        assert rc == 0

    def test_instantiate_bad_var_format_exits_nonzero(self, template_home, capsys):
        kt.save_template("board-tmpl", _YAML_WITH_BOARD)
        rc = _dispatch_templates(_args(
            template_action="instantiate",
            slug="board-tmpl",
            vars=["no-equals-sign"],
        ))
        assert rc != 0

    def test_instantiate_nonexistent_template_exits_nonzero(self, template_home, capsys):
        rc = _dispatch_templates(_args(
            template_action="instantiate",
            slug="ghost",
            vars=[],
        ))
        assert rc != 0

    def test_instantiate_board_slug_override(self, template_home, capsys):
        kt.save_template("board-tmpl", _YAML_WITH_BOARD)
        rc = _dispatch_templates(_args(
            template_action="instantiate",
            slug="board-tmpl",
            vars=[],
            template_board="my-custom-board",
            dispatch=False,
        ))
        assert rc == 0
        assert kb.board_exists("my-custom-board")


# ---------------------------------------------------------------------------
# Unknown template action
# ---------------------------------------------------------------------------

class TestUnknownAction:
    def test_unknown_action_exits_nonzero(self, template_home):
        rc = _dispatch_templates(_args(template_action="nonexistent-action"))
        assert rc != 0
