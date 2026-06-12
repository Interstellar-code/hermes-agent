"""Cron tests for kanban template recurrence (upsert_kanban_template_job + _run_kanban_template_job).

Covers:
  - save_template w/ recurrence enabled → job upserted with deterministic id
  - recurrence disabled → job removed
  - delete_template → recurrence job removed
  - invalid cron + enabled → TemplateValidationError at save time
  - _run_kanban_template_job happy path
  - _run_kanban_template_job bad slug → failure recorded without raising
"""

from __future__ import annotations

import importlib
import json
from pathlib import Path
from unittest.mock import patch

import pytest

import hermes_cli.kanban_db as kb
from hermes_cli import kanban_templates as kt


# ---------------------------------------------------------------------------
# Fixture — mirrors pattern from tests/cron/test_jobs.py
# ---------------------------------------------------------------------------

@pytest.fixture()
def cron_home(tmp_path, monkeypatch):
    """Isolated HERMES_HOME with cron dirs + kanban initialized."""
    home = tmp_path / "hermes_home"
    home.mkdir()
    cron_dir = home / "cron"
    cron_dir.mkdir()

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

    # Pin cron module-level path constants to the temp dir so load_jobs /
    # save_jobs never touch the real HERMES_HOME.
    monkeypatch.setattr("cron.jobs.CRON_DIR", cron_dir)
    monkeypatch.setattr("cron.jobs.JOBS_FILE", cron_dir / "jobs.json")
    monkeypatch.setattr("cron.jobs.OUTPUT_DIR", cron_dir / "output")

    try:
        import hermes_constants
        hermes_constants._cached_default_hermes_root = None  # type: ignore[attr-defined]
    except Exception:
        pass
    kb._INITIALIZED_PATHS.clear()
    kb.init_db()
    return home


# ---------------------------------------------------------------------------
# Minimal YAML fixtures
# ---------------------------------------------------------------------------

_MINIMAL_YAML = """\
schema: 1
name: Test Template
tasks:
  - key: a
    title: "Task A"
  - key: b
    title: "Task B"
"""

_RECURRENCE_YAML = """\
schema: 1
name: Recurring Template
board:
  slug: recur-board
tasks:
  - key: a
    title: "Recurring Task"
recurrence:
  enabled: true
  cron: "0 9 * * 1"
"""

_RECURRENCE_DISABLED_YAML = """\
schema: 1
name: Disabled Recurrence
board:
  slug: dis-board
tasks:
  - key: a
    title: "Task A"
recurrence:
  enabled: false
  cron: "0 9 * * 1"
"""

_BAD_CRON_YAML = """\
schema: 1
name: Bad Cron Template
board:
  slug: bad-cron-board
tasks:
  - key: a
    title: "Task A"
recurrence:
  enabled: true
  cron: "not-a-valid-cron"
"""


# ---------------------------------------------------------------------------
# upsert_kanban_template_job via save_template (recurrence.enabled=true)
# ---------------------------------------------------------------------------

class TestRecurrenceJobUpsert:
    def test_save_with_enabled_recurrence_upserts_job(self, cron_home):
        from cron.jobs import load_jobs
        kt.save_template("recur-tmpl", _RECURRENCE_YAML)
        jobs = load_jobs()
        job_ids = [j["id"] for j in jobs]
        assert "kanban-template-recur-tmpl" in job_ids

    def test_job_has_correct_template_slug(self, cron_home):
        from cron.jobs import load_jobs
        kt.save_template("recur-tmpl", _RECURRENCE_YAML)
        jobs = load_jobs()
        job = next(j for j in jobs if j["id"] == "kanban-template-recur-tmpl")
        assert job["payload"]["template_slug"] == "recur-tmpl"

    def test_job_has_correct_type(self, cron_home):
        from cron.jobs import load_jobs
        kt.save_template("recur-tmpl", _RECURRENCE_YAML)
        jobs = load_jobs()
        job = next(j for j in jobs if j["id"] == "kanban-template-recur-tmpl")
        assert job["type"] == "kanban_board_from_template"

    def test_job_is_deterministic_no_duplicates(self, cron_home):
        from cron.jobs import load_jobs
        # Save twice — should upsert, not duplicate
        kt.save_template("recur-tmpl", _RECURRENCE_YAML)
        kt.save_template("recur-tmpl", _RECURRENCE_YAML)
        jobs = load_jobs()
        matching = [j for j in jobs if j["id"] == "kanban-template-recur-tmpl"]
        assert len(matching) == 1

    def test_save_with_disabled_recurrence_removes_job(self, cron_home):
        from cron.jobs import load_jobs
        # First save enabled
        kt.save_template("recur-tmpl", _RECURRENCE_YAML)
        # Then disable
        kt.save_template("recur-tmpl", _RECURRENCE_DISABLED_YAML)
        jobs = load_jobs()
        job_ids = [j["id"] for j in jobs]
        assert "kanban-template-recur-tmpl" not in job_ids

    def test_save_without_recurrence_no_job(self, cron_home):
        from cron.jobs import load_jobs
        kt.save_template("plain-tmpl", _MINIMAL_YAML)
        jobs = load_jobs()
        job_ids = [j["id"] for j in jobs]
        assert "kanban-template-plain-tmpl" not in job_ids


# ---------------------------------------------------------------------------
# delete_template → job removed
# ---------------------------------------------------------------------------

class TestDeleteTemplateRemovesJob:
    def test_delete_removes_recurrence_job(self, cron_home):
        from cron.jobs import load_jobs
        kt.save_template("recur-tmpl", _RECURRENCE_YAML)
        # Verify job exists
        jobs = load_jobs()
        assert any(j["id"] == "kanban-template-recur-tmpl" for j in jobs)
        # Delete template
        kt.delete_template("recur-tmpl")
        jobs = load_jobs()
        assert not any(j["id"] == "kanban-template-recur-tmpl" for j in jobs)

    def test_delete_nonrecurrent_template_no_error(self, cron_home):
        # Should not raise even if no job to remove
        kt.save_template("plain-tmpl", _MINIMAL_YAML)
        kt.delete_template("plain-tmpl")  # must not raise


# ---------------------------------------------------------------------------
# invalid cron + enabled → TemplateValidationError at save time
# ---------------------------------------------------------------------------

class TestInvalidCronRejectsAtSave:
    def test_invalid_cron_raises_validation_error(self, cron_home):
        with pytest.raises(kt.TemplateValidationError):
            kt.save_template("bad-cron", _BAD_CRON_YAML)

    def test_invalid_cron_no_job_written(self, cron_home):
        from cron.jobs import load_jobs
        try:
            kt.save_template("bad-cron", _BAD_CRON_YAML)
        except kt.TemplateValidationError:
            pass
        jobs = load_jobs()
        assert not any(j["id"] == "kanban-template-bad-cron" for j in jobs)


# ---------------------------------------------------------------------------
# _run_kanban_template_job — happy path
# ---------------------------------------------------------------------------

class TestRunKanbanTemplateJob:
    def test_happy_path_returns_success(self, cron_home):
        from cron.scheduler import _run_kanban_template_job
        kt.save_template("recur-tmpl", _RECURRENCE_YAML)
        job = {
            "id": "kanban-template-recur-tmpl",
            "name": "Kanban template: recur-tmpl",
            "type": "kanban_board_from_template",
            "payload": {
                "template_slug": "recur-tmpl",
                "variables": {},
                "auto_dispatch": False,
            },
        }
        success, doc, response, err = _run_kanban_template_job(job)
        assert success is True
        assert err is None
        assert "recur-tmpl" in doc or "recur-board" in doc

    def test_happy_path_board_created(self, cron_home):
        from cron.scheduler import _run_kanban_template_job
        kt.save_template("recur-tmpl", _RECURRENCE_YAML)
        job = {
            "id": "kanban-template-recur-tmpl",
            "name": "Kanban template: recur-tmpl",
            "type": "kanban_board_from_template",
            "payload": {
                "template_slug": "recur-tmpl",
                "variables": {},
                "auto_dispatch": False,
            },
        }
        _run_kanban_template_job(job)
        # Board from template board.slug should exist (possibly uniquified)
        boards = list(kb.list_boards())
        assert any("recur" in b.get("slug", "") for b in boards)

    def test_bad_slug_returns_failure_without_raising(self, cron_home):
        from cron.scheduler import _run_kanban_template_job
        job = {
            "id": "kanban-template-ghost",
            "name": "Kanban template: ghost",
            "type": "kanban_board_from_template",
            "payload": {
                "template_slug": "ghost",
                "variables": {},
                "auto_dispatch": False,
            },
        }
        # Must not raise — failure should be recorded in return tuple
        success, doc, response, err = _run_kanban_template_job(job)
        assert success is False
        assert err is not None
        assert "ghost" in err or "not found" in err.lower() or "ghost" in doc

    def test_missing_template_slug_returns_failure(self, cron_home):
        from cron.scheduler import _run_kanban_template_job
        job = {
            "id": "kanban-template-empty",
            "name": "Empty payload job",
            "type": "kanban_board_from_template",
            "payload": {},
        }
        success, doc, response, err = _run_kanban_template_job(job)
        assert success is False
        assert err is not None


# ---------------------------------------------------------------------------
# upsert_kanban_template_job directly
# ---------------------------------------------------------------------------

class TestUpsertKanbanTemplateJob:
    def test_upsert_creates_job(self, cron_home):
        from cron.jobs import upsert_kanban_template_job, load_jobs
        job = upsert_kanban_template_job(
            job_id="kanban-template-direct",
            schedule_expr="0 6 * * *",
            template_slug="some-template",
        )
        assert job["id"] == "kanban-template-direct"
        jobs = load_jobs()
        assert any(j["id"] == "kanban-template-direct" for j in jobs)

    def test_upsert_is_idempotent(self, cron_home):
        from cron.jobs import upsert_kanban_template_job, load_jobs
        upsert_kanban_template_job(
            job_id="kanban-template-idem",
            schedule_expr="0 6 * * *",
            template_slug="some-template",
        )
        upsert_kanban_template_job(
            job_id="kanban-template-idem",
            schedule_expr="0 6 * * *",
            template_slug="some-template",
        )
        jobs = load_jobs()
        matching = [j for j in jobs if j["id"] == "kanban-template-idem"]
        assert len(matching) == 1

    def test_upsert_stores_variables(self, cron_home):
        from cron.jobs import upsert_kanban_template_job, load_jobs
        upsert_kanban_template_job(
            job_id="kanban-template-vars",
            schedule_expr="0 9 * * 1",
            template_slug="tmpl",
            variables={"env": "prod"},
        )
        jobs = load_jobs()
        job = next(j for j in jobs if j["id"] == "kanban-template-vars")
        assert job["payload"]["variables"] == {"env": "prod"}

    def test_upsert_stores_auto_dispatch(self, cron_home):
        from cron.jobs import upsert_kanban_template_job, load_jobs
        upsert_kanban_template_job(
            job_id="kanban-template-dispatch",
            schedule_expr="0 9 * * 1",
            template_slug="tmpl",
            auto_dispatch=True,
        )
        jobs = load_jobs()
        job = next(j for j in jobs if j["id"] == "kanban-template-dispatch")
        assert job["payload"]["auto_dispatch"] is True
