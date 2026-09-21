"""An explicit project binding must outrank cwd inference in the session tree.

`build_tree` is pure, so the caller bulk-fetches bindings and passes them in.
The failure mode this guards is quiet: without the binding map, build_tree still
returns a perfectly valid tree -- sessions just land under whatever project
their cwd happens to sit in, so an explicitly-bound session silently appears in
the wrong place and nothing errors.
"""

from __future__ import annotations

from hermes_cli.projects_db import SessionBinding
from tui_gateway import project_tree


def _resolve(path):
    """Stand-in for git_probe.resolve: returns the repo info dict build_tree expects."""
    path = str(path or "")
    return {"repo_root": path, "branch": "main", "is_main": True} if path else None


PROJECTS = [
    {"id": "p-alpha", "name": "Alpha", "slug": "alpha", "folders": [{"path": "/repo/alpha"}]},
    {"id": "p-beta", "name": "Beta", "slug": "beta", "folders": [{"path": "/repo/beta"}]},
]


def _tree(session_bindings):
    sessions = [{"id": "s-1", "cwd": "/repo/alpha", "message_count": 3}]
    return project_tree.build_tree(
        PROJECTS, sessions, [], _resolve,
        preview_limit=10, hydrate=False,
        is_junk_root=lambda *_a, **_k: False,
        is_junk_cwd=lambda *_a, **_k: False,
        exists=lambda *_a, **_k: True,
        session_bindings=session_bindings,
    )


def _owner_of(tree, session_id):
    """Which project node owns ``session_id``.

    Sessions appear both in previewSessions and nested under
    repos[].groups[].sessions; check both so the assertion does not depend on
    which surface a given tier populates.
    """
    for node in tree.get("projects", []):
        if any(s.get("id") == session_id for s in node.get("previewSessions") or []):
            return node.get("id")
        for repo in node.get("repos") or []:
            for group in repo.get("groups") or []:
                if any(s.get("id") == session_id for s in group.get("sessions") or []):
                    return node.get("id")
    return None


def test_cwd_inference_when_unbound():
    """Baseline: with no bindings the session follows its cwd."""
    assert _owner_of(_tree(None), "s-1") == "p-alpha"


def test_binding_overrides_cwd():
    """The whole point: bound to beta, sitting in alpha's folder -> beta wins."""
    bindings = {"s-1": SessionBinding(project_id="p-beta", session_id="s-1", bound_at=0)}
    assert _owner_of(_tree(bindings), "s-1") == "p-beta"


def test_binding_to_archived_project_falls_back_to_cwd():
    projects = [PROJECTS[0], {**PROJECTS[1], "archived": True}]
    bindings = {"s-1": SessionBinding(project_id="p-beta", session_id="s-1", bound_at=0)}
    tree = project_tree.build_tree(
        projects, [{"id": "s-1", "cwd": "/repo/alpha", "message_count": 3}], [],
        _resolve, preview_limit=10, hydrate=False,
        is_junk_root=lambda *_a, **_k: False, is_junk_cwd=lambda *_a, **_k: False,
        exists=lambda *_a, **_k: True, session_bindings=bindings)
    assert _owner_of(tree, "s-1") == "p-alpha"


def test_binding_to_unknown_project_falls_back_to_cwd():
    bindings = {"s-1": SessionBinding(project_id="p-ghost", session_id="s-1", bound_at=0)}
    assert _owner_of(_tree(bindings), "s-1") == "p-alpha"
