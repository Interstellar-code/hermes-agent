"""Read-only dashboard API for Hermes' first-class projects.

Mounted by ``hermes_cli.web_server`` at ``/api/plugins/projects``.  The
dashboard supplies authentication for these routes; this module only exposes
the per-profile ``projects.db`` store through the existing REST plugin
surface.  Mutations remain in ``hermes project`` and the TUI gateway for v1.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query

from hermes_cli import projects_db
from plugins.projects.dashboard.activity import decode_cursor, project_activity
from plugins.projects.dashboard.enrichment import enrich_projects

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
    enriched, errors = enrich_projects(projects, active_id)
    response = {"projects": enriched, "active_id": active_id}
    if errors:
        response["enrichment_errors"] = errors
    return response


@router.get("/{project_id_or_slug}/folders")
def get_project_folders(project_id_or_slug: str) -> dict:
    """Return the folder list for one project, resolving id or slug."""
    project = _project_or_404(project_id_or_slug)
    return {"project_id": project.id, "folders": [folder.to_dict() for folder in project.folders]}


@router.get("/{project_id_or_slug}/activity")
def get_project_activity(
    project_id_or_slug: str,
    limit: int = Query(10, ge=1, le=50),
    cursor: str | None = Query(None),
) -> dict:
    """Return stable, newest-first task/session activity for one project."""
    project = _project_or_404(project_id_or_slug)
    try:
        decoded = decode_cursor(cursor)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if decoded is not None:
        cursor_project, timestamp, key = decoded
        if cursor_project != project.id:
            raise HTTPException(status_code=400, detail="cursor belongs to another project")
        after = (timestamp, key)
    else:
        after = None
    return project_activity(project, limit=limit, after=after)


@router.get("/{project_id_or_slug}")
def get_project(project_id_or_slug: str) -> dict:
    """Return one project, resolving either its stable id or slug."""
    project = _project_or_404(project_id_or_slug)
    with projects_db.connect_closing() as conn:
        active_id = projects_db.get_active_id(conn)
    enriched, errors = enrich_projects([project], active_id)
    response = {"project": enriched[0]}
    if errors:
        response["enrichment_errors"] = errors
    return response
