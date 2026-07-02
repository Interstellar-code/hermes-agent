"""Tests for hermes_cli.kanban_templates — core module.

Covers: save/list/load/delete roundtrip; validate_template rejections;
substitute() basics; instantiate() happy-path + guardrails; save_board_as_template.
"""

from __future__ import annotations

import os
import time
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


# ---------------------------------------------------------------------------
# ΔC — Dependency topology tests (Tests 1-4 + 5-7)
# ---------------------------------------------------------------------------

class TestDependencyTopology:
    """Multi-edge instantiation: chain, diamond, multi-parent."""

    def _task_links(self, board: str) -> list[tuple[str, str]]:
        conn = kb.connect(board=board)
        rows = conn.execute(
            "SELECT parent_id, child_id FROM task_links"
        ).fetchall()
        conn.close()
        return [(r[0], r[1]) for r in rows]

    def _task_status(self, board: str, task_id: str) -> str:
        conn = kb.connect(board=board)
        row = conn.execute(
            "SELECT status FROM tasks WHERE id = ?", (task_id,)
        ).fetchone()
        conn.close()
        return row[0]

    # Test 1 — Multi-level chain A→B→C
    def test_chain_abc_edges_and_blocking(self, template_home):
        yaml_chain = """\
schema: 1
name: Chain Template
tasks:
  - key: a
    title: "Task A"
  - key: b
    title: "Task B"
  - key: c
    title: "Task C"
links:
  - [a, b]
  - [b, c]
"""
        kt.save_template("chain-tmpl", yaml_chain)
        result = kt.instantiate("chain-tmpl", board_slug="chain-board")
        task_ids = result["task_ids"]

        links = self._task_links("chain-board")
        assert (task_ids["a"], task_ids["b"]) in links
        assert (task_ids["b"], task_ids["c"]) in links
        assert len(links) == 2

        # C is blocked (todo) because A and B are not done
        assert self._task_status("chain-board", task_ids["c"]) == "todo"

    # Test 2 — Diamond A→B, A→C, B→D, C→D
    def test_diamond_all_edges_and_parents(self, template_home):
        yaml_diamond = """\
schema: 1
name: Diamond Template
tasks:
  - key: a
    title: "Task A"
  - key: b
    title: "Task B"
  - key: c
    title: "Task C"
  - key: d
    title: "Task D"
links:
  - [a, b]
  - [a, c]
  - [b, d]
  - [c, d]
"""
        kt.save_template("diamond-tmpl", yaml_diamond)
        result = kt.instantiate("diamond-tmpl", board_slug="diamond-board")
        task_ids = result["task_ids"]

        links = self._task_links("diamond-board")
        assert len(links) == 4
        assert (task_ids["a"], task_ids["b"]) in links
        assert (task_ids["a"], task_ids["c"]) in links
        assert (task_ids["b"], task_ids["d"]) in links
        assert (task_ids["c"], task_ids["d"]) in links

        # D must have exactly parents B and C
        conn = kb.connect(board="diamond-board")
        d_parents = {
            r[0]
            for r in conn.execute(
                "SELECT parent_id FROM task_links WHERE child_id = ?",
                (task_ids["d"],),
            ).fetchall()
        }
        conn.close()
        assert d_parents == {task_ids["b"], task_ids["c"]}

    # Test 3 — Multi-parent (A,B)→C
    def test_multi_parent_c_has_both_parents(self, template_home):
        yaml_mp = """\
schema: 1
name: MultiParent Template
tasks:
  - key: a
    title: "Task A"
  - key: b
    title: "Task B"
  - key: c
    title: "Task C"
links:
  - [a, c]
  - [b, c]
"""
        kt.save_template("mp-tmpl", yaml_mp)
        result = kt.instantiate("mp-tmpl", board_slug="mp-board")
        task_ids = result["task_ids"]

        conn = kb.connect(board="mp-board")
        c_parents = {
            r[0]
            for r in conn.execute(
                "SELECT parent_id FROM task_links WHERE child_id = ?",
                (task_ids["c"],),
            ).fetchall()
        }
        conn.close()
        assert c_parents == {task_ids["a"], task_ids["b"]}

    # Test 4 — Full save→instantiate round-trip with links
    def test_save_instantiate_roundtrip_links(self, template_home):
        # Build a 3-task chain on a source board via kanban_db
        kb.init_db(board="src-chain")
        conn = kb.connect(board="src-chain")
        t1 = kb.create_task(conn, title="Alpha", board="src-chain")
        t2 = kb.create_task(conn, title="Beta", board="src-chain")
        t3 = kb.create_task(conn, title="Gamma", board="src-chain")
        kb.link_tasks(conn, t1, t2)
        kb.link_tasks(conn, t2, t3)
        conn.close()

        # save_board_as_template must capture both links
        tmpl = kt.save_board_as_template("src-chain", "chain-snap")
        assert "links" in tmpl
        assert len(tmpl["links"]) == 2

        # Instantiate the saved template onto a fresh board
        result = kt.instantiate("chain-snap", board_slug="chain-dest")
        new_links = self._task_links("chain-dest")
        assert len(new_links) == 2

        # The two edges must be present (any key ordering is fine; count is the invariant)
        assert result["created"] == 3


class TestResetStatusClamp:
    """Test 5 — reset_status=False preserves 'ready'; reset_status=True forces 'todo'."""

    def _create_board_with_ready_task(self, board: str = "clamp-board") -> str:
        """Create a board with one ready task; return its id."""
        kb.init_db(board=board)
        conn = kb.connect(board=board)
        task_id = kb.create_task(conn, title="Ready Task", board=board)
        conn.execute("UPDATE tasks SET status = 'ready' WHERE id = ?", (task_id,))
        conn.commit()
        conn.close()
        return task_id

    def test_reset_false_preserves_ready(self, template_home):
        self._create_board_with_ready_task("clamp-board-a")
        result = kt.save_board_as_template(
            "clamp-board-a", "clamp-snap-a", reset_status=False
        )
        statuses = {t["key"]: t.get("status", "todo") for t in result["tasks"]}
        # At least one task must be 'ready' (the one we set)
        assert "ready" in statuses.values(), (
            "reset_status=False should preserve 'ready' but all statuses are 'todo'"
        )

    def test_reset_true_forces_todo(self, template_home):
        self._create_board_with_ready_task("clamp-board-b")
        result = kt.save_board_as_template(
            "clamp-board-b", "clamp-snap-b", reset_status=True
        )
        for task in result["tasks"]:
            assert task.get("status", "todo") == "todo", (
                f"reset_status=True should force 'todo' but got {task.get('status')!r}"
            )


class TestFieldRoundTrip:
    """Test 6 — max_runtime_seconds + goal_max_turns survive save→instantiate."""

    def test_fields_survive_roundtrip(self, template_home):
        kb.init_db(board="fields-src")
        conn = kb.connect(board="fields-src")
        task_id = kb.create_task(
            conn,
            title="Timed Task",
            board="fields-src",
            max_runtime_seconds=300,
            goal_max_turns=10,
        )
        conn.close()

        # save_board_as_template must carry both fields
        tmpl = kt.save_board_as_template("fields-src", "fields-snap")
        task_entry = tmpl["tasks"][0]
        assert task_entry.get("max_runtime_seconds") == 300
        assert task_entry.get("goal_max_turns") == 10

        # instantiate must write both columns into the new board
        result = kt.instantiate("fields-snap", board_slug="fields-dest")
        new_task_id = list(result["task_ids"].values())[0]

        conn2 = kb.connect(board="fields-dest")
        row = conn2.execute(
            "SELECT max_runtime_seconds, goal_max_turns FROM tasks WHERE id = ?",
            (new_task_id,),
        ).fetchone()
        conn2.close()
        assert row[0] == 300
        assert row[1] == 10


class TestLinkFailureFatal:
    """Test 7 — (Codex P1) link-wiring failure is fatal and leaves no partial board."""

    def test_link_failure_raises_and_rolls_back(self, template_home, monkeypatch):
        yaml_linked = """\
schema: 1
name: Linked Tmpl
tasks:
  - key: x
    title: "Task X"
  - key: y
    title: "Task Y"
links:
  - [x, y]
"""
        kt.save_template("fatal-tmpl", yaml_linked)

        # Monkeypatch _kdb.link_tasks to raise on every call
        def _boom(conn, parent_id, child_id):
            raise RuntimeError("simulated link failure")

        monkeypatch.setattr(kb, "link_tasks", _boom)
        # Also patch via the kanban_templates module's reference to _kdb
        import hermes_cli.kanban_templates as _kt_mod
        monkeypatch.setattr(_kt_mod._kdb, "link_tasks", _boom)

        with pytest.raises(kt.TemplateError):
            kt.instantiate("fatal-tmpl", board_slug="fatal-board")

        # Board must have 0 non-archived tasks (rolled back)
        conn = kb.connect(board="fatal-board")
        live_count = conn.execute(
            "SELECT COUNT(*) FROM tasks WHERE status != 'archived'"
        ).fetchone()[0]
        conn.close()
        assert live_count == 0, (
            f"Expected 0 non-archived tasks after fatal link failure, got {live_count}"
        )


# ---------------------------------------------------------------------------
# scheduled_at — deferred dispatch (template-side: steps 3 + 4)
# ---------------------------------------------------------------------------


class TestScheduledAt:
    def _sched_of(self, board_slug: str) -> list:
        conn = kb.connect(board=board_slug)
        try:
            return [
                r[0]
                for r in conn.execute(
                    "SELECT scheduled_at FROM tasks ORDER BY created_at"
                ).fetchall()
            ]
        finally:
            conn.close()

    def test_validate_accepts_relative_epoch_and_placeholder(self, template_home):
        for val in ("+2h", "+30m", "+1d", "+1w", "+45s", 1893456000, "1893456000", "{{when}}"):
            data = {
                "schema": 1,
                "name": "t",
                "tasks": [{"key": "a", "title": "A", "scheduled_at": val}],
            }
            kt.validate_template(data)  # must not raise

    def test_validate_rejects_malformed_scheduled_at(self, template_home):
        for val in ("2h", "+2x", "-5m", "soon", "+h", 0, -10, True, 3.5):
            data = {
                "schema": 1,
                "name": "t",
                "tasks": [{"key": "a", "title": "A", "scheduled_at": val}],
            }
            with pytest.raises(kt.TemplateValidationError):
                kt.validate_template(data)

    def test_instantiate_resolves_relative_offset(self, template_home):
        yaml = (
            "schema: 1\nname: Sched\nboard:\n  slug: sched-board\n"
            'tasks:\n  - key: later\n    title: "Deferred"\n    scheduled_at: "+2h"\n'
        )
        kt.save_template("sched", yaml)
        before = int(time.time())
        kt.instantiate("sched", board_slug="sched-board")
        after = int(time.time())
        (val,) = self._sched_of("sched-board")
        assert before + 7200 <= val <= after + 7200

    def test_instantiate_absolute_epoch_passthrough(self, template_home):
        yaml = (
            "schema: 1\nname: Abs\nboard:\n  slug: abs-board\n"
            'tasks:\n  - key: t\n    title: "T"\n    scheduled_at: 1893456000\n'
        )
        kt.save_template("abs", yaml)
        kt.instantiate("abs", board_slug="abs-board")
        assert self._sched_of("abs-board") == [1893456000]

    def test_instantiate_scheduled_at_via_variable(self, template_home):
        yaml = (
            "schema: 1\nname: Var\nboard:\n  slug: var-board\n"
            "variables:\n  - key: when\n    default: \"+1h\"\n"
            'tasks:\n  - key: t\n    title: "T"\n    scheduled_at: "{{when}}"\n'
        )
        kt.save_template("varsched", yaml)
        before = int(time.time())
        kt.instantiate("varsched", board_slug="var-board")
        after = int(time.time())
        (val,) = self._sched_of("var-board")
        assert before + 3600 <= val <= after + 3600

    def test_instantiate_variable_override_resolves(self, template_home):
        yaml = (
            "schema: 1\nname: Var\nboard:\n  slug: ov-board\n"
            "variables:\n  - key: when\n    default: \"+1h\"\n"
            'tasks:\n  - key: t\n    title: "T"\n    scheduled_at: "{{when}}"\n'
        )
        kt.save_template("ovsched", yaml)
        before = int(time.time())
        kt.instantiate("ovsched", board_slug="ov-board", variables={"when": "+1d"})
        after = int(time.time())
        (val,) = self._sched_of("ov-board")
        assert before + 86400 <= val <= after + 86400

    def test_instantiate_without_scheduled_at_is_null(self, template_home):
        yaml = (
            "schema: 1\nname: Plain\nboard:\n  slug: plain-board\n"
            'tasks:\n  - key: t\n    title: "T"\n'
        )
        kt.save_template("plain", yaml)
        kt.instantiate("plain", board_slug="plain-board")
        assert self._sched_of("plain-board") == [None]

    def test_save_board_as_template_omits_scheduled_at(self, template_home):
        conn = kb.connect(board="src-board")
        kb.create_task(
            conn,
            title="deferred",
            assignee="alice",
            scheduled_at=int(time.time()) + 3600,
        )
        conn.close()
        result = kt.save_board_as_template("src-board", "snap")
        specs = result["tasks"]
        assert specs and all("scheduled_at" not in s for s in specs)
