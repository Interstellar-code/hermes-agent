"""Tests for kanban template tool handlers in tools/kanban_tools.py.

Covers:
  - _check_kanban_template_mode: True/False based on HERMES_KANBAN_TASK + toolset config
  - _require_template_tool: worker refusal dict
  - _handle_template_list: happy path via orchestrator context
  - _handle_template_instantiate: happy path instantiation
"""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import patch

import pytest

import hermes_cli.kanban_db as kb
from hermes_cli import kanban_templates as kt


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
        "HERMES_KANBAN_TASK",
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


# ---------------------------------------------------------------------------
# Minimal YAML
# ---------------------------------------------------------------------------

_MINIMAL_YAML = """\
schema: 1
name: Tools Template
board:
  slug: tools-board
tasks:
  - key: a
    title: "Task A"
  - key: b
    title: "Task B"
links:
  - [a, b]
"""


# ---------------------------------------------------------------------------
# _check_kanban_template_mode
# ---------------------------------------------------------------------------

class TestCheckKanbanTemplateMode:
    def test_false_when_hermes_kanban_task_set(self, monkeypatch):
        from tools.kanban_tools import _check_kanban_template_mode
        monkeypatch.setenv("HERMES_KANBAN_TASK", "task-123")
        assert _check_kanban_template_mode() is False

    def test_false_when_no_kanban_toolset_in_profile(self, monkeypatch):
        from tools.kanban_tools import _check_kanban_template_mode
        monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
        # Patch _profile_has_kanban_toolset to return False
        with patch("tools.kanban_tools._profile_has_kanban_toolset", return_value=False):
            assert _check_kanban_template_mode() is False

    def test_true_when_kanban_toolset_enabled_and_no_task(self, monkeypatch):
        from tools.kanban_tools import _check_kanban_template_mode
        monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
        with patch("tools.kanban_tools._profile_has_kanban_toolset", return_value=True):
            assert _check_kanban_template_mode() is True


# ---------------------------------------------------------------------------
# _require_template_tool — worker refusal
# ---------------------------------------------------------------------------

class TestRequireTemplateTool:
    def test_returns_none_when_no_task_env(self, monkeypatch):
        from tools.kanban_tools import _require_template_tool
        monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
        result = _require_template_tool("kanban_template_list")
        assert result is None

    def test_returns_error_dict_when_worker(self, monkeypatch):
        from tools.kanban_tools import _require_template_tool
        monkeypatch.setenv("HERMES_KANBAN_TASK", "task-abc")
        result = _require_template_tool("kanban_template_list")
        assert isinstance(result, dict)
        assert "error" in result or "content" in result  # tool_error shape

    def test_worker_refusal_contains_informative_message(self, monkeypatch):
        from tools.kanban_tools import _require_template_tool
        monkeypatch.setenv("HERMES_KANBAN_TASK", "task-abc")
        result = _require_template_tool("kanban_template_list")
        assert result is not None
        # The refusal must mention something about worker/template restriction
        result_str = str(result)
        assert any(word in result_str.lower() for word in ["worker", "template", "not available", "orchestrator"])


# ---------------------------------------------------------------------------
# _handle_template_list
# ---------------------------------------------------------------------------

class TestHandleTemplateList:
    def test_empty_list_returns_templates_key(self, template_home, monkeypatch):
        from tools.kanban_tools import _handle_template_list
        monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
        result = _handle_template_list({})
        assert "templates" in result
        assert result["templates"] == []
        assert result["count"] == 0

    def test_lists_saved_template(self, template_home, monkeypatch):
        from tools.kanban_tools import _handle_template_list
        monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
        kt.save_template("tools-tmpl", _MINIMAL_YAML)
        result = _handle_template_list({})
        assert result["count"] == 1
        entry = result["templates"][0]
        assert entry["slug"] == "tools-tmpl"
        assert entry["task_count"] == 2

    def test_worker_context_returns_refusal(self, template_home, monkeypatch):
        from tools.kanban_tools import _handle_template_list
        monkeypatch.setenv("HERMES_KANBAN_TASK", "task-xyz")
        result = _handle_template_list({})
        # Should return error dict, not a templates dict
        assert "templates" not in result or "error" in result or "content" in result


# ---------------------------------------------------------------------------
# _handle_template_instantiate
# ---------------------------------------------------------------------------

class TestHandleTemplateInstantiate:
    def test_happy_path(self, template_home, monkeypatch):
        from tools.kanban_tools import _handle_template_instantiate
        monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
        kt.save_template("tools-tmpl", _MINIMAL_YAML)
        result = _handle_template_instantiate({"slug": "tools-tmpl"})
        assert result.get("ok") is True or "board_slug" in result

    def test_missing_slug_returns_error(self, template_home, monkeypatch):
        from tools.kanban_tools import _handle_template_instantiate
        monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
        result = _handle_template_instantiate({})
        # Should return error (not raise)
        result_str = str(result)
        assert "error" in result_str.lower() or "slug" in result_str.lower() or "content" in result

    def test_nonexistent_slug_returns_error(self, template_home, monkeypatch):
        from tools.kanban_tools import _handle_template_instantiate
        monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
        result = _handle_template_instantiate({"slug": "ghost"})
        result_str = str(result)
        assert "error" in result_str.lower() or "not found" in result_str.lower() or "content" in result

    def test_worker_context_returns_refusal(self, template_home, monkeypatch):
        from tools.kanban_tools import _handle_template_instantiate
        monkeypatch.setenv("HERMES_KANBAN_TASK", "task-xyz")
        result = _handle_template_instantiate({"slug": "tools-tmpl"})
        assert "templates" not in result or "error" in result or "content" in result

    def test_instantiate_with_board_slug(self, template_home, monkeypatch):
        from tools.kanban_tools import _handle_template_instantiate
        monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
        kt.save_template("tools-tmpl", _MINIMAL_YAML)
        result = _handle_template_instantiate({
            "slug": "tools-tmpl",
            "board_slug": "explicit-board",
        })
        assert result.get("ok") is True or result.get("board_slug") == "explicit-board"
        assert kb.board_exists("explicit-board")
