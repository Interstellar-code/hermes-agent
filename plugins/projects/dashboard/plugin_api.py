"""Dashboard API for Hermes' first-class projects.

Mounted by ``hermes_cli.web_server`` at ``/api/plugins/projects``.  The
dashboard supplies authentication for these routes; this module exposes the
per-profile ``projects.db`` store through the existing REST plugin surface.
"""

from __future__ import annotations

from contextlib import contextmanager

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field, model_validator

from hermes_cli import projects_db
from plugins.projects.dashboard.activity import decode_cursor, project_activity
from plugins.projects.dashboard.enrichment import enrich_projects

@contextmanager
def _projects_profile(profile: str | None):
    requested = (profile or "").strip()
    if not requested or requested.lower() == "current":
        yield
        return
    try:
        from hermes_cli.profiles import get_profile_dir, normalize_profile_name, profile_exists
        from hermes_constants import reset_hermes_home_override, set_hermes_home_override

        name = normalize_profile_name(requested)
        if not profile_exists(name):
            raise HTTPException(status_code=404, detail=f"profile '{name}' not found")
        token = set_hermes_home_override(get_profile_dir(name))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    try:
        yield
    finally:
        reset_hermes_home_override(token)


async def _project_profile_dependency(profile: str | None = None):
    with _projects_profile(profile):
        yield


router = APIRouter(dependencies=[Depends(_project_profile_dependency)])


class _Request(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CreateProjectRequest(_Request):
    name: str
    slug: str | None = None
    folders: list[str] = Field(default_factory=list)
    primary_path: str | None = None
    description: str | None = None
    icon: str | None = None
    color: str | None = None
    board_slug: str | None = None


class UpdateProjectRequest(_Request):
    name: str | None = None
    description: str | None = None
    icon: str | None = None
    color: str | None = None
    board_slug: str | None = None

    @model_validator(mode="after")
    def reject_explicit_nulls(self):
        nulls = [name for name in self.model_fields_set if getattr(self, name) is None]
        if nulls:
            raise ValueError(f"null is not allowed for: {', '.join(sorted(nulls))}")
        return self


class FolderRequest(_Request):
    path: str
    label: str | None = None
    is_primary: bool = False


class PathRequest(_Request):
    path: str


def _clean(value: str | None) -> str | None:
    return value.strip() if value is not None else None


def _require_path(path: str) -> str:
    path = path.strip()
    if not path:
        raise HTTPException(status_code=400, detail="folder path must not be empty")
    return path


def _bad_value(exc: ValueError) -> HTTPException:
    return HTTPException(status_code=400, detail=str(exc))


def _project_payload(project) -> dict:
    with projects_db.connect_closing() as conn:
        active_id = projects_db.get_active_id(conn)
    enriched, errors = enrich_projects([project], active_id)
    response = {"project": enriched[0]}
    if errors:
        response["enrichment_errors"] = errors
    return response


def _projects_payload() -> dict:
    with projects_db.connect_closing() as conn:
        projects = projects_db.list_projects(conn, include_archived=True)
        active_id = projects_db.get_active_id(conn)
    enriched, errors = enrich_projects(projects, active_id)
    response = {"projects": enriched, "active_id": active_id}
    if errors:
        response["enrichment_errors"] = errors
    return response


def _project_or_404(project_id_or_slug: str):
    with projects_db.connect_closing() as conn:
        project = projects_db.get_project(conn, project_id_or_slug)
    if project is None:
        raise HTTPException(status_code=404, detail="project not found")
    return project


def _mutate_project(project_id_or_slug: str, operation):
    """Resolve and mutate one project while keeping the DB connection short-lived."""
    with projects_db.connect_closing() as conn:
        project = projects_db.get_project(conn, project_id_or_slug)
        if project is None:
            raise HTTPException(status_code=404, detail="project not found")
        try:
            operation(conn, project)
        except ValueError as exc:
            raise _bad_value(exc) from exc
        updated = projects_db.get_project(conn, project.id)
    return updated


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


@router.post("")
def create_project(body: CreateProjectRequest) -> dict:
    """Create a project and return its persisted representation."""
    folders = [_require_path(path) for path in body.folders]
    primary_path = _require_path(body.primary_path) if body.primary_path is not None else None
    try:
        with projects_db.connect_closing() as conn:
            project_id = projects_db.create_project(
                conn,
                name=body.name.strip(),
                slug=_clean(body.slug),
                folders=folders,
                primary_path=primary_path,
                description=body.description,
                icon=_clean(body.icon),
                color=_clean(body.color),
                board_slug=_clean(body.board_slug),
            )
            project = projects_db.get_project(conn, project_id)
    except ValueError as exc:
        raise _bad_value(exc) from exc
    return _project_payload(project)


@router.patch("/{project_id_or_slug}")
def update_project(project_id_or_slug: str, body: UpdateProjectRequest) -> dict:
    """Patch top-level fields; omitted values remain untouched."""
    values = {field: getattr(body, field) for field in body.model_fields_set}
    for field in values.keys() & {"name", "icon", "color", "board_slug"}:
        values[field] = _clean(values[field])
    return _project_payload(
        _mutate_project(
            project_id_or_slug,
            lambda conn, project: projects_db.update_project(conn, project.id, **values),
        )
    )


@router.post("/{project_id_or_slug}/folders")
def add_project_folder(project_id_or_slug: str, body: FolderRequest) -> dict:
    path = _require_path(body.path)
    return _project_payload(
        _mutate_project(
            project_id_or_slug,
            lambda conn, project: projects_db.add_folder(
                conn,
                project.id,
                path,
                label=_clean(body.label),
                is_primary=body.is_primary,
            ),
        )
    )


@router.delete("/{project_id_or_slug}/folders")
def remove_project_folder(project_id_or_slug: str, body: PathRequest) -> dict:
    path = _require_path(body.path)

    def remove(conn, project):
        if not projects_db.remove_folder(conn, project.id, path):
            raise HTTPException(status_code=404, detail="folder not found")

    return _project_payload(_mutate_project(project_id_or_slug, remove))


@router.post("/{project_id_or_slug}/folders/primary")
def set_primary_folder(project_id_or_slug: str, body: PathRequest) -> dict:
    path = _require_path(body.path)

    def set_primary(conn, project):
        if not projects_db.set_primary(conn, project.id, path):
            raise HTTPException(status_code=404, detail="folder not found")

    return _project_payload(_mutate_project(project_id_or_slug, set_primary))


@router.post("/{project_id_or_slug}/archive")
def archive_project(project_id_or_slug: str) -> dict:
    project = _mutate_project(
        project_id_or_slug,
        lambda conn, project: projects_db.archive_project(conn, project.id),
    )
    return {**_projects_payload(), **_project_payload(project)}


@router.post("/{project_id_or_slug}/restore")
def restore_project(project_id_or_slug: str) -> dict:
    project = _mutate_project(
        project_id_or_slug,
        lambda conn, project: projects_db.restore_project(conn, project.id),
    )
    return {**_projects_payload(), **_project_payload(project)}


@router.post("/{project_id_or_slug}/active")
def set_active_project(project_id_or_slug: str) -> dict:
    with projects_db.connect_closing() as conn:
        project = projects_db.get_project(conn, project_id_or_slug)
        if project is None:
            raise HTTPException(status_code=404, detail="project not found")
        projects_db.set_active(conn, project.id)
    return _projects_payload()


@router.delete("/{project_id_or_slug}")
def delete_project(project_id_or_slug: str) -> dict:
    with projects_db.connect_closing() as conn:
        project = projects_db.get_project(conn, project_id_or_slug)
        if project is None:
            raise HTTPException(status_code=404, detail="project not found")
        if not project.archived:
            raise HTTPException(status_code=409, detail="only archived projects can be deleted")
        deleted = projects_db.delete_project(
            conn, project.id, clear_active=True, archived_only=True
        )
        if not deleted:
            current = projects_db.get_project(conn, project.id)
            if current is None:
                raise HTTPException(status_code=404, detail="project not found")
            raise HTTPException(status_code=409, detail="only archived projects can be deleted")
    return _projects_payload()


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
