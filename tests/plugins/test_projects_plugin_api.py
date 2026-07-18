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


def _delete_json(client: TestClient, path: str, payload: dict):
    return client.request("DELETE", path, json=payload)


def test_create_update_and_validation_contract(client: TestClient) -> None:
    response = client.post(
        "/api/plugins/projects",
        json={
            "name": "  Demo Project  ",
            "folders": ["/tmp/demo-a", "/tmp/demo-b"],
            "description": "  keep spacing  ",
            "board_slug": " Work-Board ",
        },
    )
    assert response.status_code == 200
    project = response.json()["project"]
    assert project["name"] == "Demo Project"
    assert project["slug"] == "demo-project"
    assert project["description"] == "  keep spacing  "
    assert project["board_slug"] == "work-board"
    assert project["folders"][0]["is_primary"] is True
    assert project["folder_count"] == 2
    assert project["is_active"] is False

    pid = project["id"]
    response = client.patch(
        f"/api/plugins/projects/{pid}",
        json={"description": "", "icon": "", "color": "blue", "board_slug": ""},
    )
    assert response.status_code == 200
    project = response.json()["project"]
    assert project["description"] == ""
    assert project["icon"] is None
    assert project["color"] == "blue"
    assert project["board_slug"] is None

    # Omitted patch fields remain untouched; explicit null is not a clear value.
    response = client.patch(f"/api/plugins/projects/{pid}", json={"description": None})
    assert response.status_code == 422
    assert client.patch(f"/api/plugins/projects/{pid}", json={"name": " "}).status_code == 400
    assert client.patch(f"/api/plugins/projects/{pid}", json={"unknown": 1}).status_code == 422
    assert client.post("/api/plugins/projects", json={"name": " "}).status_code == 400
    assert client.post("/api/plugins/projects", json={"name": "x", "folders": [" "]}).status_code == 400
    assert client.post("/api/plugins/projects", json={"name": "x", "slug": "Bad Slug"}).status_code == 400

    duplicate = client.post("/api/plugins/projects", json={"name": "Demo Project", "slug": "demo-project"})
    assert duplicate.status_code == 200
    assert duplicate.json()["project"]["slug"] == "demo-project-2"


def test_folder_mutations_preserve_primary_and_reject_missing(client: TestClient) -> None:
    pid = _create_project()
    response = client.post(
        f"/api/plugins/projects/{pid}/folders", json={"path": "/tmp/second", "is_primary": True}
    )
    assert response.status_code == 200
    project = response.json()["project"]
    assert project["primary_path"] == "/tmp/second"
    assert [f["is_primary"] for f in project["folders"]].count(True) == 1

    response = client.post(
        f"/api/plugins/projects/{pid}/folders/primary", json={"path": "/tmp/second"}
    )
    assert response.status_code == 200
    response = _delete_json(
        client, f"/api/plugins/projects/{pid}/folders", {"path": "/tmp/second"}
    )
    assert response.status_code == 200
    project = response.json()["project"]
    assert project["primary_path"].endswith("/src/hermes")
    assert _delete_json(
        client, f"/api/plugins/projects/{pid}/folders", {"path": "/does/not/exist"}
    ).status_code == 404
    assert client.post(
        f"/api/plugins/projects/{pid}/folders/primary", json={"path": " "}
    ).status_code == 400


def test_write_routes_resolve_slugs_and_windows_style_paths(client: TestClient) -> None:
    pid = _create_project()
    response = client.patch("/api/plugins/projects/hermes-agent", json={"color": "green"})
    assert response.status_code == 200
    assert response.json()["project"]["id"] == pid

    windows_path = r"C:\work\repo"
    response = client.post(
        f"/api/plugins/projects/{pid}/folders", json={"path": windows_path}
    )
    assert response.status_code == 200
    assert any("C:\\work\\repo" in folder["path"] for folder in response.json()["project"]["folders"])


def test_lifecycle_active_and_archived_only_delete(client: TestClient) -> None:
    pid = _create_project()
    assert client.post(f"/api/plugins/projects/{pid}/active").json()["active_id"] == pid

    # Active projects may be archived, and the pointer remains until deletion.
    archived = client.post(f"/api/plugins/projects/{pid}/archive")
    assert archived.status_code == 200
    assert archived.json()["project"]["archived"] is True
    assert archived.json()["projects"][0]["archived"] is True
    assert client.get("/api/plugins/projects").json()["active_id"] == pid
    assert client.delete(f"/api/plugins/projects/{pid}").status_code == 200
    assert client.get("/api/plugins/projects?include_archived=true").json() == {
        "projects": [],
        "active_id": None,
    }

    pid = _create_project()
    assert client.delete(f"/api/plugins/projects/{pid}").status_code == 409
    assert client.post(f"/api/plugins/projects/{pid}/archive").status_code == 200
    assert client.post(f"/api/plugins/projects/{pid}/archive").json()["project"]["archived"] is True
    restored = client.post(f"/api/plugins/projects/{pid}/restore")
    assert restored.status_code == 200
    assert restored.json()["project"]["archived"] is False
    assert client.post(f"/api/plugins/projects/{pid}/restore").json()["project"]["archived"] is False
    assert client.post("/api/plugins/projects/missing/active").status_code == 404


def test_write_route_table_contract() -> None:
    from plugins.projects.dashboard.plugin_api import router

    routes = {(method, route.path) for route in router.routes for method in route.methods}
    assert {
        ("POST", ""),
        ("PATCH", "/{project_id_or_slug}"),
        ("POST", "/{project_id_or_slug}/folders"),
        ("DELETE", "/{project_id_or_slug}/folders"),
        ("POST", "/{project_id_or_slug}/folders/primary"),
        ("POST", "/{project_id_or_slug}/archive"),
        ("POST", "/{project_id_or_slug}/restore"),
        ("POST", "/{project_id_or_slug}/active"),
        ("DELETE", "/{project_id_or_slug}"),
    } <= routes


def test_mounted_projects_mutations_use_dashboard_auth(monkeypatch, tmp_path: Path) -> None:
    home = tmp_path / "mounted-home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    projects_db._INITIALIZED_PATHS.clear()

    from hermes_cli import web_server

    mounted = TestClient(web_server.app)
    assert mounted.post("/api/plugins/projects", json={"name": "Nope"}).status_code == 401
    mounted.headers[web_server._SESSION_HEADER_NAME] = web_server._SESSION_TOKEN
    response = mounted.post("/api/plugins/projects", json={"name": "Mounted"})
    assert response.status_code == 200


def test_rest_projects_follow_active_profile(client: TestClient, tmp_path: Path, monkeypatch) -> None:
    assert client.post("/api/plugins/projects", json={"name": "Profile A"}).status_code == 200
    profile_b = tmp_path / "profile-b"
    profile_b.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(profile_b))
    projects_db._INITIALIZED_PATHS.clear()
    assert client.get("/api/plugins/projects?include_archived=true").json() == {
        "projects": [],
        "active_id": None,
    }


def test_profile_query_scopes_all_project_routes(
    client: TestClient, tmp_path: Path, monkeypatch
) -> None:
    root = tmp_path / "hermes_home"
    profile = root / "profiles" / "switch"
    profile.mkdir(parents=True)
    response = client.post(
        "/api/plugins/projects?profile=switch", json={"name": "Scoped"}
    )
    assert response.status_code == 200
    project_id = response.json()["project"]["id"]
    assert client.get("/api/plugins/projects").json()["projects"] == []
    scoped = client.get(f"/api/plugins/projects/{project_id}?profile=switch")
    assert scoped.status_code == 200
    assert scoped.json()["project"]["name"] == "Scoped"
    assert client.get("/api/plugins/projects?profile=missing").status_code == 404
