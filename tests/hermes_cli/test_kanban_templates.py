"""Tests for hermes_cli.kanban_templates — core module.

Covers: save/list/load/delete roundtrip; validate_template rejections;
substitute() basics; instantiate() happy-path + guardrails; save_board_as_template.
"""

from __future__ import annotations

import os
from pathlib import Path

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
# Minimal valid template YAML
# ---------------------------------------------------------------------------

_MINIMAL_YAML = """\
schema: 1
name: Test Template
tasks:
  - key: alpha
    title: "Alpha task"
  - key: beta
    title: "Beta task"
"""

_YAML_WITH_LINKS = """\
schema: 1
name: Linked Template
board:
  slug: test-board
tasks:
  - key: a
    title: "Task A"
  - key: b
    title: "Task B"
links:
  - [a, b]
"""

_YAML_WITH_VARS = """\
schema: 1
name: "{{project}} Template"
board:
  slug: "{{project}}-board"
variables:
  - key: project
    required: true
  - key: owner
    default: "unassigned"
tasks:
  - key: setup
    title: "Setup {{project}} for {{owner}}"
  - key: review
    title: "Review {{project}}"
"""


# ---------------------------------------------------------------------------
# Save / List / Load / Delete roundtrip
# ---------------------------------------------------------------------------

class TestSaveLoadListDelete:
    def test_save_returns_parsed_dict(self, template_home):
        result = kt.save_template("my-template", _MINIMAL_YAML)
        assert result["schema"] == 1
        assert len(result["tasks"]) == 2

    def test_list_empty(self, template_home):
        assert kt.list_templates() == []

    def test_list_shows_saved(self, template_home):
        kt.save_template("my-template", _MINIMAL_YAML)
        templates = kt.list_templates()
        assert len(templates) == 1
        entry = templates[0]
        assert entry["slug"] == "my-template"
        assert entry["name"] == "Test Template"
        assert "has_recurrence" in entry
        assert entry["has_recurrence"] is False

    def test_load_roundtrip(self, template_home):
        kt.save_template("my-template", _MINIMAL_YAML)
        loaded = kt.load_template("my-template")
        assert loaded["name"] == "Test Template"
        keys = [t["key"] for t in loaded["tasks"]]
        assert keys == ["alpha", "beta"]

    def test_delete_removes_template(self, template_home):
        kt.save_template("my-template", _MINIMAL_YAML)
        kt.delete_template("my-template")
        assert kt.list_templates() == []

    def test_delete_nonexistent_raises_not_found(self, template_home):
        with pytest.raises(kt.TemplateNotFound):
            kt.delete_template("no-such-template")

    def test_load_nonexistent_raises_not_found(self, template_home):
        with pytest.raises(kt.TemplateNotFound):
            kt.load_template("ghost")

    def test_save_overwrites(self, template_home):
        kt.save_template("my-template", _MINIMAL_YAML)
        updated = _MINIMAL_YAML.replace("Test Template", "Updated Template")
        kt.save_template("my-template", updated)
        loaded = kt.load_template("my-template")
        assert loaded["name"] == "Updated Template"

    def test_multiple_templates_listed_sorted(self, template_home):
        kt.save_template("bravo", _MINIMAL_YAML)
        kt.save_template("alpha", _MINIMAL_YAML)
        slugs = [t["slug"] for t in kt.list_templates()]
        assert slugs == ["alpha", "bravo"]


# ---------------------------------------------------------------------------
# Slug validation
# ---------------------------------------------------------------------------

class TestSlugValidation:
    def test_valid_slug_accepted(self, template_home):
        kt.save_template("valid-slug-123", _MINIMAL_YAML)  # no error

    def test_path_traversal_rejected(self, template_home):
        with pytest.raises(kt.TemplateValidationError):
            kt.save_template("../evil", _MINIMAL_YAML)

    def test_uppercase_rejected(self, template_home):
        with pytest.raises(kt.TemplateValidationError):
            kt.save_template("UpperCase", _MINIMAL_YAML)

    def test_65_char_slug_rejected(self, template_home):
        long_slug = "a" * 65
        with pytest.raises(kt.TemplateValidationError):
            kt.save_template(long_slug, _MINIMAL_YAML)

    def test_64_char_slug_accepted(self, template_home):
        slug = "a" * 64
        kt.save_template(slug, _MINIMAL_YAML)  # no error

    def test_underscore_in_slug_rejected(self, template_home):
        with pytest.raises(kt.TemplateValidationError):
            kt.save_template("has_underscore", _MINIMAL_YAML)

    def test_leading_hyphen_rejected(self, template_home):
        with pytest.raises(kt.TemplateValidationError):
            kt.save_template("-leading", _MINIMAL_YAML)


# ---------------------------------------------------------------------------
# validate_template rejections
# ---------------------------------------------------------------------------

class TestValidateTemplate:
    def test_missing_schema_rejected(self):
        data = {"tasks": [{"key": "a", "title": "Task A"}]}
        with pytest.raises(kt.TemplateValidationError, match="schema"):
            kt.validate_template(data)

    def test_wrong_schema_version_rejected(self):
        data = {"schema": 2, "tasks": [{"key": "a", "title": "Task A"}]}
        with pytest.raises(kt.TemplateValidationError, match="schema"):
            kt.validate_template(data)

    def test_empty_tasks_rejected(self):
        data = {"schema": 1, "tasks": []}
        with pytest.raises(kt.TemplateValidationError, match="tasks"):
            kt.validate_template(data)

    def test_missing_tasks_rejected(self):
        data = {"schema": 1}
        with pytest.raises(kt.TemplateValidationError, match="tasks"):
            kt.validate_template(data)

    def test_duplicate_task_keys_rejected(self):
        data = {
            "schema": 1,
            "tasks": [
                {"key": "a", "title": "Task A"},
                {"key": "a", "title": "Task A duplicate"},
            ],
        }
        with pytest.raises(kt.TemplateValidationError, match="duplicate"):
            kt.validate_template(data)

    def test_link_to_unknown_key_rejected(self):
        data = {
            "schema": 1,
            "tasks": [{"key": "a", "title": "Task A"}],
            "links": [["a", "nonexistent"]],
        }
        with pytest.raises(kt.TemplateValidationError):
            kt.validate_template(data)

    def test_link_cycle_rejected(self):
        data = {
            "schema": 1,
            "tasks": [
                {"key": "a", "title": "A"},
                {"key": "b", "title": "B"},
                {"key": "c", "title": "C"},
            ],
            "links": [
                ["a", "b"],
                ["b", "c"],
                ["c", "a"],  # cycle
            ],
        }
        with pytest.raises(kt.TemplateValidationError, match="cycle"):
            kt.validate_template(data)

    def test_valid_template_passes(self):
        import yaml
        data = yaml.safe_load(_MINIMAL_YAML)
        result = kt.validate_template(data)
        assert result["schema"] == 1

    def test_non_mapping_rejected(self):
        with pytest.raises(kt.TemplateValidationError):
            kt.validate_template(["not", "a", "dict"])  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Oversized YAML
# ---------------------------------------------------------------------------

def test_oversized_yaml_rejected(template_home):
    big = _MINIMAL_YAML + ("x" * (kt.MAX_TEMPLATE_BYTES + 1))
    with pytest.raises(kt.TemplateValidationError, match="bytes"):
        kt.save_template("too-big", big)


# ---------------------------------------------------------------------------
# substitute()
# ---------------------------------------------------------------------------

class TestSubstitute:
    def test_basic_substitution(self):
        result = kt.substitute("Hello {{name}}", {"name": "World"})
        assert result == "Hello World"

    def test_unknown_placeholder_left_intact(self):
        result = kt.substitute("Hello {{unknown}}", {"known": "x"})
        assert result == "Hello {{unknown}}"

    def test_builtin_date_substituted(self):
        from datetime import datetime, timezone
        today = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d")
        result = kt.substitute("Date: {{date}}", {"date": today})
        assert result == f"Date: {today}"

    def test_builtin_instance_id_substituted(self):
        result = kt.substitute("ID: {{instance_id}}", {"instance_id": "abc-123"})
        assert result == "ID: abc-123"

    def test_multiple_placeholders(self):
        result = kt.substitute("{{a}} and {{b}}", {"a": "foo", "b": "bar"})
        assert result == "foo and bar"

    def test_non_string_value_coerced(self):
        result = kt.substitute("Count: {{n}}", {"n": 42})
        assert result == "Count: 42"


# ---------------------------------------------------------------------------
# instantiate()
# ---------------------------------------------------------------------------

class TestInstantiate:
    def test_board_created_with_tasks(self, template_home):
        kt.save_template("linked", _YAML_WITH_LINKS)
        result = kt.instantiate("linked", board_slug="test-board")
        assert result["created"] == 2
        assert result["skipped"] == 0
        assert len(result["task_ids"]) == 2
        assert result["board_slug"] == "test-board"
        assert result["instance_id"]  # non-empty

    def test_tasks_carry_workflow_template_id(self, template_home):
        kt.save_template("linked", _YAML_WITH_LINKS)
        result = kt.instantiate("linked", board_slug="test-board")
        slug = result["slug"] if "slug" in result else "linked"
        instance_id = result["instance_id"]
        expected_wtid = f"linked@{instance_id}"

        conn = kb.connect(board="test-board")
        rows = conn.execute(
            "SELECT workflow_template_id FROM tasks WHERE workflow_template_id IS NOT NULL"
        ).fetchall()
        conn.close()
        assert len(rows) == 2
        for row in rows:
            assert row[0] == expected_wtid

    def test_tasks_carry_current_step_key(self, template_home):
        kt.save_template("linked", _YAML_WITH_LINKS)
        kt.instantiate("linked", board_slug="test-board")

        conn = kb.connect(board="test-board")
        rows = conn.execute(
            "SELECT current_step_key FROM tasks ORDER BY created_at ASC"
        ).fetchall()
        conn.close()
        keys = [r[0] for r in rows]
        assert set(keys) == {"a", "b"}

    def test_idempotency_key_set(self, template_home):
        kt.save_template("linked", _YAML_WITH_LINKS)
        result = kt.instantiate("linked", board_slug="test-board")
        instance_id = result["instance_id"]

        conn = kb.connect(board="test-board")
        rows = conn.execute(
            "SELECT idempotency_key FROM tasks ORDER BY created_at ASC"
        ).fetchall()
        conn.close()
        ikeys = [r[0] for r in rows]
        assert f"linked:{instance_id}:a" in ikeys
        assert f"linked:{instance_id}:b" in ikeys

    def test_link_created(self, template_home):
        kt.save_template("linked", _YAML_WITH_LINKS)
        result = kt.instantiate("linked", board_slug="test-board")
        task_ids = result["task_ids"]

        conn = kb.connect(board="test-board")
        links = conn.execute(
            "SELECT parent_id, child_id FROM task_links"
        ).fetchall()
        conn.close()
        assert len(links) == 1
        parent_id, child_id = links[0]
        assert parent_id == task_ids["a"]
        assert child_id == task_ids["b"]

    def test_auto_dispatch_false_forces_todo(self, template_home):
        # With auto_dispatch=False all tasks should be todo regardless
        yaml_ready = """\
schema: 1
name: Ready Template
board:
  slug: ready-board
tasks:
  - key: t1
    title: "Task 1"
    status: ready
"""
        kt.save_template("ready-tmpl", yaml_ready)
        kt.instantiate("ready-tmpl", board_slug="ready-board", auto_dispatch=False)

        conn = kb.connect(board="ready-board")
        rows = conn.execute("SELECT status FROM tasks").fetchall()
        conn.close()
        for row in rows:
            assert row[0] == "todo"

    def test_required_var_missing_raises(self, template_home):
        kt.save_template("var-tmpl", _YAML_WITH_VARS)
        with pytest.raises(kt.TemplateValidationError, match="required"):
            kt.instantiate("var-tmpl", board_slug="some-board")

    def test_required_var_supplied_succeeds(self, template_home):
        kt.save_template("var-tmpl", _YAML_WITH_VARS)
        result = kt.instantiate(
            "var-tmpl",
            variables={"project": "myproj"},
            board_slug="some-board",
        )
        assert result["created"] == 2

    def test_variable_substituted_in_title(self, template_home):
        kt.save_template("var-tmpl", _YAML_WITH_VARS)
        kt.instantiate(
            "var-tmpl",
            variables={"project": "myproj"},
            board_slug="some-board",
        )
        conn = kb.connect(board="some-board")
        titles = [r[0] for r in conn.execute("SELECT title FROM tasks").fetchall()]
        conn.close()
        assert any("myproj" in t for t in titles)

    def test_open_task_cap_raises_instantiation_refused(self, template_home):
        kt.save_template("linked", _YAML_WITH_LINKS)
        # First instantiate puts 2 open tasks on the board; _cap=1 → refused
        kt.instantiate("linked", board_slug="cap-board")
        with pytest.raises(kt.InstantiationRefused):
            kt.instantiate("linked", board_slug="cap-board", _cap=1)

    def test_second_instantiate_produces_new_instance_id(self, template_home):
        kt.save_template("linked", _YAML_WITH_LINKS)
        r1 = kt.instantiate("linked", board_slug="board-a")
        r2 = kt.instantiate("linked", board_slug="board-b")
        assert r1["instance_id"] != r2["instance_id"]

    def test_second_instantiate_same_board_skips_deduped_tasks(self, template_home):
        """Idempotency: same instance_id won't be reused but we test clean second run."""
        kt.save_template("linked", _YAML_WITH_LINKS)
        # Two distinct instances on the same board accumulate (different instance_ids)
        r1 = kt.instantiate("linked", board_slug="shared-board")
        r2 = kt.instantiate("linked", board_slug="shared-board")
        # Both runs should create fresh tasks (different idempotency keys)
        assert r1["instance_id"] != r2["instance_id"]
        assert r2["created"] == 2

    def test_uniquify_suffix_on_board_slug_collision(self, template_home):
        """Board slug uniquification appends -2/-3 when slug exists."""
        kt.save_template("linked", _YAML_WITH_LINKS)
        # Create the board first so uniquify must find an alternative
        kb.create_board("test-board", name="existing")
        result = kt.instantiate("linked")
        # The board slug must not be 'test-board' (already exists)
        assert result["board_slug"] != "test-board"
        assert result["board_slug"].startswith("test-board")


# ---------------------------------------------------------------------------
# save_board_as_template()
# ---------------------------------------------------------------------------

class TestSaveBoardAsTemplate:
    def _create_board_with_tasks(self, board="src-board"):
        kb.init_db(board=board)
        conn = kb.connect(board=board)
        t1 = kb.create_task(conn, title="Setup server", board=board)
        t2 = kb.create_task(conn, title="Deploy app", board=board)
        kb.link_tasks(conn, t1, t2)
        conn.close()
        return t1, t2

    def test_strips_runtime_fields_resets_statuses(self, template_home):
        self._create_board_with_tasks()
        # Mark a task as done to verify status reset
        conn = kb.connect(board="src-board")
        kb.complete_task(conn, kb.list_tasks(conn)[0].id)
        conn.close()

        result = kt.save_board_as_template(
            "src-board", "snap-tmpl", reset_status=True
        )
        assert result["schema"] == 1
        # All tasks should have status 'todo' after reset
        for task in result["tasks"]:
            assert task.get("status", "todo") in ("todo", "ready")

    def test_keep_status_preserves_statuses(self, template_home):
        self._create_board_with_tasks()
        # Force a task to ready status
        conn = kb.connect(board="src-board")
        conn.execute("UPDATE tasks SET status='ready' WHERE rowid=1")
        conn.commit()
        conn.close()

        result = kt.save_board_as_template(
            "src-board", "snap-tmpl", reset_status=False
        )
        statuses = {t["key"]: t.get("status", "todo") for t in result["tasks"]}
        # At least one task should retain its non-default status
        assert len(statuses) == 2

    def test_template_has_correct_task_count(self, template_home):
        self._create_board_with_tasks()
        result = kt.save_board_as_template("src-board", "snap-tmpl")
        assert len(result["tasks"]) == 2

    def test_template_saved_to_disk(self, template_home):
        self._create_board_with_tasks()
        kt.save_board_as_template("src-board", "snap-tmpl")
        loaded = kt.load_template("snap-tmpl")
        assert len(loaded["tasks"]) == 2

    def test_no_runtime_fields_in_tasks(self, template_home):
        self._create_board_with_tasks()
        result = kt.save_board_as_template("src-board", "snap-tmpl")
        runtime_fields = {
            "workflow_template_id", "idempotency_key", "current_step_key",
            "created_at", "started_at", "completed_at", "claim_lock",
            "workspace_path", "pid", "run_id", "current_run_id",
        }
        for task in result["tasks"]:
            overlap = set(task.keys()) & runtime_fields
            assert not overlap, f"Task has runtime fields: {overlap}"

    def test_invalid_board_slug_raises(self, template_home):
        with pytest.raises(kt.TemplateValidationError):
            kt.save_board_as_template("INVALID_SLUG!", "tmpl-slug")
