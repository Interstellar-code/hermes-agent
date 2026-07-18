"""REST contract tests for the bundled projects dashboard plugin."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from hermes_cli import projects_db
from hermes_cli import kanban_db
from hermes_state import SessionDB


@pytest.fixture()
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    home = tmp_path / "hermes_home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    projects_db._INITIALIZED_PATHS.clear()

    from plugins.projects.dashboard.plugin_api import router

    app = FastAPI()
    app.include_router(router, prefix="/api/plugins/projects")
    return TestClient(app)


def _create_project() -> str:
    with projects_db.connect_closing() as conn:
        return projects_db.create_project(
            conn,
            name="Hermes Agent",
            slug="hermes-agent",
            folders=["~/src/hermes"],
            description="The agent repository",
            board_slug="agent-work",
        )


def test_manifest_is_backend_only() -> None:
    manifest = json.loads(
        (Path(__file__).parents[2] / "plugins/projects/dashboard/manifest.json").read_text(
            encoding="utf-8"
        )
    )
    assert manifest["name"] == "projects"
    assert manifest["api"] == "plugin_api.py"
    assert manifest["tab"] == {"hidden": True}


def test_dashboard_scanner_discovers_bundled_plugin() -> None:
    from hermes_cli import web_server

    project_plugins = [
        plugin
        for plugin in web_server._discover_dashboard_plugins()
        if plugin["name"] == "projects"
    ]
    assert len(project_plugins) == 1
    assert project_plugins[0]["source"] == "bundled"
    assert project_plugins[0]["_api_file"] == "plugin_api.py"


def test_list_returns_projects_and_active_id(client: TestClient) -> None:
    assert client.get("/api/plugins/projects/").json() == {"projects": [], "active_id": None}

    project_id = _create_project()
    with projects_db.connect_closing() as conn:
        projects_db.set_active(conn, project_id)

    response = client.get("/api/plugins/projects/")
    assert response.status_code == 200
    body = response.json()
    assert body["active_id"] == project_id
    assert body["projects"][0]["slug"] == "hermes-agent"
    assert body["projects"][0]["folders"][0]["is_primary"] is True
    assert body["projects"][0]["folder_count"] == 1
    assert body["projects"][0]["is_active"] is True
    assert body["projects"][0]["task_count"] == 0
    assert body["projects"][0]["open_task_count"] == 0
    assert body["projects"][0]["session_count"] == 0
    assert not any(body["projects"][0]["task_status_counts"].values())
    assert body["projects"][0]["last_activity_at"] is None
    assert body["projects"][0]["bound_board"] is None


def test_list_surfaces_unavailable_enrichment(client: TestClient, monkeypatch) -> None:
    from plugins.projects.dashboard import plugin_api

    with projects_db.connect_closing() as conn:
        projects_db.create_project(
            conn,
            name="Hermes Agent",
            slug="hermes-agent",
            folders=["/tmp/hermes-agent"],
        )

    def unavailable(projects, active_id):
        rows = []
        for project in projects:
            data = project.to_dict()
            data.update(
                task_count=None,
                open_task_count=None,
                task_status_counts=None,
                session_count=None,
                last_task_activity_at=None,
                last_session_activity_at=None,
                last_activity_at=None,
                is_active=project.id == active_id,
                folder_count=len(project.folders),
                bound_board=None,
            )
            rows.append(data)
        return rows, ["board work: simulated failure"]

    monkeypatch.setattr(plugin_api, "enrich_projects", unavailable)
    response = client.get("/api/plugins/projects")
    assert response.status_code == 200
    body = response.json()
    assert body["projects"][0]["task_count"] is None
    assert body["enrichment_errors"] == ["board work: simulated failure"]


def test_get_and_folders_resolve_slug_or_id(client: TestClient) -> None:
    project_id = _create_project()

    by_slug = client.get("/api/plugins/projects/hermes-agent")
    assert by_slug.status_code == 200
    assert by_slug.json()["project"]["id"] == project_id
    assert by_slug.json()["project"]["folder_count"] == 1

    folders = client.get(f"/api/plugins/projects/{project_id}/folders")
    assert folders.status_code == 200
    assert folders.json()["project_id"] == project_id
    assert folders.json()["folders"][0]["path"].endswith("/src/hermes")


def test_activity_is_cross_board_mixed_and_paginated(client: TestClient, tmp_path: Path, monkeypatch) -> None:
    project_id = _create_project()
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path / "kanban-root"))
    monkeypatch.delenv("HERMES_KANBAN_DB", raising=False)
    kanban_db._INITIALIZED_PATHS.clear()
    kanban_db.create_board("work", name="Work")
    with kanban_db.connect(board="default") as conn:
        first = kanban_db.create_task(conn, title="Default task", project_id=project_id)
        conn.execute("UPDATE task_events SET created_at = 100 WHERE task_id = ?", (first,))
        conn.commit()
    with kanban_db.connect(board="work") as conn:
        second = kanban_db.create_task(conn, title="Work task", project_id=project_id)
        conn.execute("UPDATE task_events SET created_at = 300 WHERE task_id = ?", (second,))
        conn.commit()

    with projects_db.connect_closing() as conn:
        project = projects_db.get_project(conn, project_id)
    db = SessionDB(db_path=tmp_path / "hermes_home" / "state.db")
    try:
        db.ensure_session("s1", source="tui", cwd=project.primary_path)
        db.append_message("s1", "user", "hello", timestamp=200)
    finally:
        db.close()

    first_page = client.get(f"/api/plugins/projects/{project_id}/activity?limit=2")
    assert first_page.status_code == 200
    body = first_page.json()
    assert [(item["kind"], item["occurred_at"]) for item in body["items"]] == [
        ("task", 300),
        ("session", 200),
    ]
    assert body["items"][0]["board_slug"] == "work"
    assert body["next_cursor"]

    second_page = client.get(
        f"/api/plugins/projects/{project_id}/activity",
        params={"limit": 2, "cursor": body["next_cursor"]},
    )
    assert [(item["kind"], item["occurred_at"]) for item in second_page.json()["items"]] == [
        ("task", 100)
    ]
    assert second_page.json()["next_cursor"] is None


def test_activity_rejects_bad_cursor_and_limits(client: TestClient) -> None:
    project_id = _create_project()
    assert client.get(f"/api/plugins/projects/{project_id}/activity?cursor=bad").status_code == 400
    assert client.get(f"/api/plugins/projects/{project_id}/activity?limit=0").status_code == 422
    assert client.get(f"/api/plugins/projects/{project_id}/activity?limit=51").status_code == 422


def test_activity_cursor_is_stable_across_ties_and_newer_inserts(
    client: TestClient, tmp_path: Path, monkeypatch
) -> None:
    project_id = _create_project()
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path / "kanban-root"))
    monkeypatch.delenv("HERMES_KANBAN_DB", raising=False)
    kanban_db._INITIALIZED_PATHS.clear()
    with kanban_db.connect(board="default") as conn:
        ids = [kanban_db.create_task(conn, title=f"task-{n}", project_id=project_id) for n in range(3)]
        conn.execute("UPDATE task_events SET created_at = 100")
        conn.commit()

    seen = []
    cursor = None
    for page_number in range(3):
        response = client.get(
            f"/api/plugins/projects/{project_id}/activity",
            params={"limit": 1, **({"cursor": cursor} if cursor else {})},
        ).json()
        seen.append(response["items"][0]["id"])
        cursor = response["next_cursor"]
        if page_number == 0:
            with kanban_db.connect(board="default") as conn:
                newer = kanban_db.create_task(conn, title="newer", project_id=project_id)
                conn.execute("UPDATE task_events SET created_at = 200 WHERE task_id = ?", (newer,))
                conn.commit()

    assert len(set(seen)) == 3
    assert set(seen) == set(ids)


def test_archived_projects_are_opt_in(client: TestClient) -> None:
    project_id = _create_project()
    with projects_db.connect_closing() as conn:
        projects_db.archive_project(conn, project_id)

    assert client.get("/api/plugins/projects/").json()["projects"] == []
    assert len(client.get("/api/plugins/projects/?include_archived=true").json()["projects"]) == 1
    assert client.get("/api/plugins/projects/hermes-agent").status_code == 200


@pytest.mark.parametrize("path", ["/missing", "/missing/folders", "/missing/activity"])
def test_missing_project_is_404(client: TestClient, path: str) -> None:
    response = client.get(f"/api/plugins/projects{path}")
    assert response.status_code == 404
