"""Cross-store enrichment contract for the Projects dashboard plugin."""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import projects_db as pdb
from hermes_state import SessionDB
from plugins.projects.dashboard.enrichment import enrich_projects


@pytest.fixture()
def stores(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    home = tmp_path / "hermes-home"
    kanban_home = tmp_path / "kanban-root"
    repo = tmp_path / "repo"
    home.mkdir()
    repo.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(kanban_home))
    monkeypatch.delenv("HERMES_KANBAN_DB", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_BOARD", raising=False)
    pdb._INITIALIZED_PATHS.clear()
    kb._INITIALIZED_PATHS.clear()
    return home, repo


def _project(repo: Path):
    with pdb.connect_closing() as conn:
        pid = pdb.create_project(
            conn,
            name="Demo",
            slug="demo",
            folders=[str(repo), str(repo / "nested")],
            board_slug="work",
        )
        return pdb.get_project(conn, pid)


def test_enrichment_aggregates_shared_boards_and_uses_stable_project_id(stores):
    _home, repo = stores
    project = _project(repo)
    kb.create_board("work", name="Work Board", icon="W", color="#abc")

    with kb.connect(board="default") as conn:
        kb.create_task(conn, title="todo", project_id=project.slug)
        done = kb.create_task(conn, title="done", project_id=project.id)
        kb.complete_task(conn, done)
    with kb.connect(board="work") as conn:
        archived = kb.create_task(conn, title="archived", project_id=project.id)
        kb.archive_task(conn, archived)

    enriched, errors = enrich_projects([project], project.id)
    item = enriched[0]
    assert not errors
    assert item["task_count"] == 2
    assert item["open_task_count"] == 1
    assert item["task_status_counts"]["ready"] == 1
    assert item["task_status_counts"]["done"] == 1
    assert item["session_count"] == 0
    assert item["is_active"] is True
    assert item["folder_count"] == 2
    assert item["bound_board"] == {
        "slug": "work",
        "name": "Work Board",
        "description": "",
        "icon": "W",
        "color": "#abc",
        "archived": False,
    }
    assert isinstance(item["last_activity_at"], int)


def test_enrichment_counts_every_canonical_status(stores):
    _home, repo = stores
    project = _project(repo)
    statuses = sorted(kb.VALID_STATUSES)

    with kb.connect(board="default") as conn:
        task_ids = [
            kb.create_task(conn, title=status, project_id=project.id)
            for status in statuses
        ]
        for task_id, status in zip(task_ids, statuses):
            conn.execute("UPDATE tasks SET status = ? WHERE id = ?", (status, task_id))
        conn.commit()

    enriched, errors = enrich_projects([project], None)
    assert not errors
    # Archived tasks are excluded from the total; done and archived tasks are
    # both excluded from the open count.
    assert enriched[0]["task_count"] == len(statuses) - 1
    assert enriched[0]["open_task_count"] == len(statuses) - 2
    counts = enriched[0]["task_status_counts"]
    assert set(counts) == kb.VALID_STATUSES - {"archived"}
    assert all(value == 1 for value in counts.values())
    assert sum(counts.values()) == enriched[0]["task_count"]
    assert sum(value for status, value in counts.items() if status != "done") == enriched[0]["open_task_count"]


def test_enrichment_uses_secondary_folder_session_activity(stores):
    home, repo = stores
    project = _project(repo)
    secondary = repo / "nested"
    secondary.mkdir()
    db = SessionDB(db_path=home / "state.db")
    try:
        db.ensure_session("s1", source="tui", cwd=str(secondary))
        db.append_message("s1", "user", "hello", timestamp=200)
    finally:
        db.close()

    enriched, errors = enrich_projects([project], None)
    assert not errors
    assert enriched[0]["session_count"] == 1
    assert enriched[0]["last_session_activity_at"] == 200
    assert enriched[0]["last_task_activity_at"] is None
    assert enriched[0]["last_activity_at"] == 200


def test_enrichment_unions_event_and_session_activity(stores):
    home, repo = stores
    project = _project(repo)
    event_at = int(time.time()) + 100
    with kb.connect(board="default") as conn:
        task_id = kb.create_task(conn, title="event", project_id=project.id)
        conn.execute(
            "INSERT INTO task_events (task_id, kind, payload, created_at) VALUES (?, ?, ?, ?)",
            (task_id, "updated", None, event_at),
        )
        conn.commit()

    db = SessionDB(db_path=home / "state.db")
    try:
        db.ensure_session("s1", source="tui", cwd=str(repo))
        db.append_message("s1", "user", "hello", timestamp=200)
    finally:
        db.close()

    enriched, errors = enrich_projects([project], None)
    assert not errors
    assert enriched[0]["last_task_activity_at"] == event_at
    assert enriched[0]["last_session_activity_at"] == 200
    assert enriched[0]["last_activity_at"] == event_at


def test_enrichment_returns_null_activity_and_missing_board(stores):
    _home, repo = stores
    project = _project(repo)
    with pdb.connect_closing() as conn:
        pdb.update_project(conn, project.id, board_slug="missing-board")
        project = pdb.get_project(conn, project.id)

    enriched, errors = enrich_projects([project], None)
    assert not errors
    assert enriched[0]["task_count"] == 0
    assert enriched[0]["open_task_count"] == 0
    assert enriched[0]["session_count"] == 0
    assert not any(enriched[0]["task_status_counts"].values())
    assert enriched[0]["last_activity_at"] is None
    assert enriched[0]["bound_board"] is None


def test_enrichment_ignores_dangling_task_links_and_keeps_archived_board(stores):
    _home, repo = stores
    project = _project(repo)
    kb.write_board_metadata("work", archived=True)
    with kb.connect(board="work") as conn:
        task_id = kb.create_task(conn, title="linked", project_id=project.id)
        conn.execute("UPDATE tasks SET project_id = ? WHERE id = ?", ("p_deleted", task_id))
        conn.commit()

    enriched, errors = enrich_projects([project], None)
    assert not errors
    assert enriched[0]["task_count"] == 0
    assert enriched[0]["bound_board"]["archived"] is True


def test_enrichment_isolates_bad_board(stores, monkeypatch):
    _home, repo = stores
    project = _project(repo)
    kb.create_board("work", name="Work Board")
    original = kb.connect

    def fail_named(*args, **kwargs):
        if kwargs.get("db_path") and str(kwargs["db_path"]).endswith("/work/kanban.db"):
            raise RuntimeError("simulated board failure")
        return original(*args, **kwargs)

    monkeypatch.setattr(kb, "connect", fail_named)
    enriched, errors = enrich_projects([project], None)
    assert enriched[0]["task_count"] is None
    assert enriched[0]["open_task_count"] is None
    assert enriched[0]["last_activity_at"] is None
    assert any("board work" in error for error in errors)


def test_enrichment_deduplicates_pinned_kanban_db(stores, monkeypatch):
    _home, repo = stores
    project = _project(repo)
    pinned = Path(kb.kanban_home()) / "pinned.db"
    with kb.connect(db_path=pinned) as conn:
        kb.create_task(conn, title="pinned", project_id=project.id)
    kb.create_board("work", name="Work Board")
    monkeypatch.setenv("HERMES_KANBAN_DB", str(pinned))

    enriched, errors = enrich_projects([project], None)
    assert not errors
    assert enriched[0]["task_count"] == 1
    assert enriched[0]["open_task_count"] == 1
