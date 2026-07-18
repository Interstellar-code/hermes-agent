"""Read-only dashboard API for Hermes' first-class projects.

Mounted by ``hermes_cli.web_server`` at ``/api/plugins/projects``.  The
dashboard supplies authentication for these routes; this module only exposes
the per-profile ``projects.db`` store through the existing REST plugin
surface.  Mutations remain in ``hermes project`` and the TUI gateway for v1.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query

from hermes_cli import projects_db

router = APIRouter()


def _project_or_404(project_id_or_slug: str):
    with projects_db.connect_closing() as conn:
        project = projects_db.get_project(conn, project_id_or_slug)
    if project is None:
        raise HTTPException(status_code=404, detail="project not found")
    return project


@router.get("")
def list_projects(
    include_archived: bool = Query(False, description="Include archived projects"),
) -> dict:
    """Return projects and the active-project pointer for the current profile."""
    with projects_db.connect_closing() as conn:
        projects = projects_db.list_projects(conn, include_archived=include_archived)
        active_id = projects_db.get_active_id(conn)
    return {
        "projects": [project.to_dict() for project in projects],
        "active_id": active_id,
    }


@router.get("/{project_id_or_slug}/folders")
def get_project_folders(project_id_or_slug: str) -> dict:
    """Return the folder list for one project, resolving id or slug."""
    project = _project_or_404(project_id_or_slug)
    return {"project_id": project.id, "folders": [folder.to_dict() for folder in project.folders]}


@router.get("/{project_id_or_slug}")
def get_project(project_id_or_slug: str) -> dict:
    """Return one project, resolving either its stable id or slug."""
    return {"project": _project_or_404(project_id_or_slug).to_dict()}
