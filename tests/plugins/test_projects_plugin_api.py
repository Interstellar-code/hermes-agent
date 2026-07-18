"""REST contract tests for the bundled projects dashboard plugin."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from hermes_cli import projects_db


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


def test_get_and_folders_resolve_slug_or_id(client: TestClient) -> None:
    project_id = _create_project()

    by_slug = client.get("/api/plugins/projects/hermes-agent")
    assert by_slug.status_code == 200
    assert by_slug.json()["project"]["id"] == project_id

    folders = client.get(f"/api/plugins/projects/{project_id}/folders")
    assert folders.status_code == 200
    assert folders.json()["project_id"] == project_id
    assert folders.json()["folders"][0]["path"].endswith("/src/hermes")


def test_archived_projects_are_opt_in(client: TestClient) -> None:
    project_id = _create_project()
    with projects_db.connect_closing() as conn:
        projects_db.archive_project(conn, project_id)

    assert client.get("/api/plugins/projects/").json()["projects"] == []
    assert len(client.get("/api/plugins/projects/?include_archived=true").json()["projects"]) == 1
    assert client.get("/api/plugins/projects/hermes-agent").status_code == 200


@pytest.mark.parametrize("path", ["/missing", "/missing/folders"])
def test_missing_project_is_404(client: TestClient, path: str) -> None:
    response = client.get(f"/api/plugins/projects{path}")
    assert response.status_code == 404
