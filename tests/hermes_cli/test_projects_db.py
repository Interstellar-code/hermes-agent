"""Tests for the per-profile Projects store (hermes_cli/projects_db)."""

from __future__ import annotations

import os

import pytest

from hermes_cli import projects_db as pdb


@pytest.fixture
def conn(tmp_path):
    c = pdb.connect(db_path=tmp_path / "projects.db")
    try:
        yield c
    finally:
        c.close()


def test_record_and_list_discovered_repos(conn):
    n = pdb.record_discovered_repos(conn, [("/www/alpha", "alpha"), ("/www/beta", None)])
    assert n == 2

    rows = {r["root"]: r["label"] for r in pdb.list_discovered_repos(conn)}
    assert rows["/www/alpha"] == "alpha"
    # Label defaults to the basename when not given.
    assert rows["/www/beta"] == "beta"


def test_record_discovered_repos_upserts(conn):
    pdb.record_discovered_repos(conn, [("/www/alpha", "old")])
    pdb.record_discovered_repos(conn, [("/www/alpha", "new")])

    rows = pdb.list_discovered_repos(conn)
    assert len(rows) == 1
    assert rows[0]["label"] == "new"


def test_record_discovered_repos_replace_drops_stale_rows(conn):
    pdb.record_discovered_repos(conn, [("/www/alpha", "alpha"), ("/www/beta", "beta")])
    pdb.record_discovered_repos(conn, [("/www/alpha", "fresh")], replace=True)

    rows = {r["root"]: r["label"] for r in pdb.list_discovered_repos(conn)}
    assert rows == {"/www/alpha": "fresh"}


def test_create_get_list(conn):
    pid = pdb.create_project(conn, name="Hermes Agent", folders=["/tmp/hermes"])
    proj = pdb.get_project(conn, pid)

    assert proj is not None
    assert proj.slug == "hermes-agent"
    assert proj.name == "Hermes Agent"
    # First folder becomes primary.
    assert proj.primary_path == "/tmp/hermes"
    assert [f.path for f in proj.folders] == ["/tmp/hermes"]
    assert proj.folders[0].is_primary is True

    # Lookup by slug too.
    assert pdb.get_project(conn, "hermes-agent").id == pid
    assert len(pdb.list_projects(conn)) == 1


def test_slug_collision_disambiguates(conn):
    pdb.create_project(conn, name="Hermes Agent")
    pdb.create_project(conn, name="Hermes Agent")
    slugs = sorted(p.slug for p in pdb.list_projects(conn))

    assert slugs == ["hermes-agent", "hermes-agent-2"]


def test_empty_name_rejected(conn):
    with pytest.raises(ValueError):
        pdb.create_project(conn, name="   ")


def test_add_remove_folder_and_primary_repoint(conn):
    pid = pdb.create_project(conn, name="P", folders=["/a"])
    pdb.add_folder(conn, pid, "/b")
    pdb.add_folder(conn, pid, "/c", is_primary=True)

    proj = pdb.get_project(conn, pid)
    assert proj.primary_path == "/c"
    assert {f.path for f in proj.folders} == {"/a", "/b", "/c"}

    # Removing the primary repoints to the oldest remaining folder.
    pdb.remove_folder(conn, pid, "/c")
    proj = pdb.get_project(conn, pid)
    assert proj.primary_path == "/a"

    # Removing the last folder clears the primary.
    pdb.remove_folder(conn, pid, "/a")
    pdb.remove_folder(conn, pid, "/b")
    proj = pdb.get_project(conn, pid)
    assert proj.primary_path is None
    assert proj.folders == []


def test_set_primary_requires_existing_folder(conn):
    pid = pdb.create_project(conn, name="P", folders=["/a"])
    assert pdb.set_primary(conn, pid, "/nope") is False
    assert pdb.set_primary(conn, pid, "/a") is True


def test_paths_normalized(conn):
    pid = pdb.create_project(conn, name="P", folders=["/a/b/../c/"])
    proj = pdb.get_project(conn, pid)
    # Trailing slash stripped, .. collapsed.
    assert proj.primary_path == "/a/c"


def test_project_for_path_longest_prefix(conn):
    outer = pdb.create_project(conn, name="Outer", folders=["/www"])
    inner = pdb.create_project(conn, name="Inner", folders=["/www/app"])

    assert pdb.project_for_path(conn, "/www/app/src/x.py").id == inner
    assert pdb.project_for_path(conn, "/www/other").id == outer
    assert pdb.project_for_path(conn, "/elsewhere") is None
    # Segment-wise prefix only: /www/app must not match /www/application.
    assert pdb.project_for_path(conn, "/www/application").id == outer


def test_project_for_path_skips_archived(conn):
    pid = pdb.create_project(conn, name="P", folders=["/www/app"])
    pdb.archive_project(conn, pid)

    assert pdb.project_for_path(conn, "/www/app/src") is None
    # Archived hidden from the default list but visible with include_archived.
    assert pdb.list_projects(conn) == []
    assert len(pdb.list_projects(conn, include_archived=True)) == 1

    pdb.restore_project(conn, pid)
    assert pdb.project_for_path(conn, "/www/app/src").id == pid


def test_active_pointer(conn):
    pid = pdb.create_project(conn, name="P")
    assert pdb.get_active_id(conn) is None
    pdb.set_active(conn, pid)
    assert pdb.get_active_id(conn) == pid
    pdb.set_active(conn, None)
    assert pdb.get_active_id(conn) is None


def test_archived_only_delete_is_atomic_and_preserves_active_on_rejection(conn):
    pid = pdb.create_project(conn, name="P")
    pdb.set_active(conn, pid)
    assert pdb.delete_project(conn, pid, clear_active=True, archived_only=True) is False
    assert pdb.get_project(conn, pid) is not None
    assert pdb.get_active_id(conn) == pid

    pdb.archive_project(conn, pid)
    assert pdb.delete_project(conn, pid, clear_active=True, archived_only=True) is True
    assert pdb.get_project(conn, pid) is None
    assert pdb.get_active_id(conn) is None


def test_branch_name_for_is_deterministic():
    proj = pdb.Project(id="p_1", slug="web-app", name="Web App", created_at=0)

    assert pdb.branch_name_for(proj, "t_abc") == "web-app/t_abc"
    assert pdb.branch_name_for(proj, "t_abc", title="Add login!") == "web-app/t_abc-add-login"
    # Stable across calls.
    assert pdb.branch_name_for(proj, "t_abc") == pdb.branch_name_for(proj, "t_abc")


def test_per_profile_isolation(tmp_path):
    # Two distinct DB paths stand in for two profiles' HERMES_HOME.
    a = pdb.connect(db_path=tmp_path / "a" / "projects.db")
    b = pdb.connect(db_path=tmp_path / "b" / "projects.db")
    try:
        pdb.create_project(a, name="Only In A", folders=["/a"])

        assert [p.slug for p in pdb.list_projects(a)] == ["only-in-a"]
        assert pdb.list_projects(b) == []
    finally:
        a.close()
        b.close()


def test_db_path_under_hermes_home():
    # Resolves under HERMES_HOME (set by the autouse isolation fixture).
    assert pdb.projects_db_path().name == "projects.db"
    assert os.path.basename(str(pdb.projects_db_path().parent))  # non-empty parent


# ---------------------------------------------------------------------------
# Per-session project binding (issue #191)
# ---------------------------------------------------------------------------


def test_project_sessions_table_idempotent(tmp_path):
    """Opening the same DB twice must not duplicate the table or the index.

    Uses a fresh DB path (not the shared ``conn`` fixture) so the second
    ``pdb.connect`` call actually runs the schema script — proving the
    ``CREATE TABLE IF NOT EXISTS`` / ``CREATE INDEX IF NOT EXISTS`` clauses
    keep the second open idempotent.
    """
    db_path = tmp_path / "projects.db"
    a = pdb.connect(db_path=db_path)
    try:
        # Touch the new helpers so the second-open path isn't a complete no-op.
        pdb.create_project(a, name="P", folders=["/x"])
    finally:
        a.close()

    b = pdb.connect(db_path=db_path)
    try:
        rows = b.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='project_sessions'"
        ).fetchall()
        assert [r["name"] for r in rows] == ["project_sessions"]
        idx = b.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type='index' AND name='idx_project_sessions_session'"
        ).fetchall()
        assert [r["name"] for r in idx] == ["idx_project_sessions_session"]
    finally:
        b.close()


def test_bind_unbind_happy_path(conn):
    pid = pdb.create_project(conn, name="P", folders=["/x"])
    binding = pdb.bind_session(conn, pid, "s1", bound_by="alice")

    assert binding.project_id == pid
    assert binding.session_id == "s1"
    assert binding.bound_by == "alice"
    assert binding.bound_at > 0
    # Round-trip.
    got = pdb.get_session_project(conn, "s1")
    assert got.project_id == pid
    assert got.session_id == "s1"
    # Per-project list shows it.
    listed = pdb.list_session_bindings(conn, pid)
    assert [b.session_id for b in listed] == ["s1"]


def test_bind_session_idempotent_updates_bound_at_and_bound_by(conn):
    pid = pdb.create_project(conn, name="P", folders=["/x"])
    first = pdb.bind_session(conn, pid, "s1", bound_by="alice")
    second = pdb.bind_session(conn, pid, "s1", bound_by="bob")
    # Same row, refreshed metadata.
    assert second.project_id == first.project_id
    assert second.session_id == first.session_id
    assert second.bound_by == "bob"
    assert second.bound_at >= first.bound_at
    # Still exactly one row for the pair.
    assert len(pdb.list_session_bindings(conn, pid)) == 1


def test_unbind_returns_false_when_no_such_binding(conn):
    pid = pdb.create_project(conn, name="P", folders=["/x"])
    assert pdb.unbind_session(conn, pid, "s1") is False
    # Unbind of an existing row returns True.
    pdb.bind_session(conn, pid, "s1")
    assert pdb.unbind_session(conn, pid, "s1") is True
    assert pdb.unbind_session(conn, pid, "s1") is False  # gone now


def test_unbind_does_not_touch_other_sessions(conn):
    pid = pdb.create_project(conn, name="P", folders=["/x"])
    pdb.bind_session(conn, pid, "s1")
    pdb.bind_session(conn, pid, "s2")
    pdb.unbind_session(conn, pid, "s1")
    assert {b.session_id for b in pdb.list_session_bindings(conn, pid)} == {"s2"}


def test_bind_rejects_empty_session_id_and_missing_project(conn):
    pid = pdb.create_project(conn, name="P", folders=["/x"])
    with pytest.raises(ValueError):
        pdb.bind_session(conn, pid, "")
    with pytest.raises(ValueError):
        pdb.bind_session(conn, pid, "   ")
    with pytest.raises(ValueError):
        pdb.bind_session(conn, "p_does_not_exist", "s1")
    with pytest.raises(ValueError):
        pdb.unbind_session(conn, "", "s1")


def test_cascade_delete_removes_bindings_when_project_is_deleted(conn):
    pid_keep = pdb.create_project(conn, name="Keep", folders=["/k"])
    pid_drop = pdb.create_project(conn, name="Drop", folders=["/d"])
    pdb.bind_session(conn, pid_keep, "shared-session", bound_by="ops")
    pdb.bind_session(conn, pid_drop, "shared-session", bound_by="ops")
    pdb.bind_session(conn, pid_drop, "another")

    # ``delete_project`` with ``archived_only=True`` is the canonical
    # destructive path; archive first, then drop.
    pdb.archive_project(conn, pid_drop)
    assert pdb.delete_project(conn, pid_drop, clear_active=True, archived_only=True) is True

    remaining = pdb.get_session_project(conn, "shared-session")
    assert remaining is not None
    assert remaining.project_id == pid_keep

    # ON DELETE CASCADE wipes bindings owned by the dropped project; the
    # bindings still owned by the surviving project are untouched.
    assert pdb.list_session_bindings(conn, pid_drop) == []
    assert {b.session_id for b in pdb.list_session_bindings(conn, pid_keep)} == {
        "shared-session"
    }
    # And the other session bound only to Drop is gone (its row cascaded out).
    assert pdb.get_session_project(conn, "another") is None


def test_get_session_project_prefers_most_recently_bound(conn):
    """When a session is bound to multiple projects, the most recent binding
    wins — the resolution downstream treats any binding as authoritative, so
    callers see a stable, deterministic winner."""
    pid_a = pdb.create_project(conn, name="A", folders=["/a"])
    pid_b = pdb.create_project(conn, name="B", folders=["/b"])
    pdb.bind_session(conn, pid_a, "s1")
    # Force a later timestamp on the second bind by sleeping just over a second
    # (bound_at is unix epoch seconds, the natural unit chosen to match the
    # rest of the projects_db schema).
    import time as _time

    _time.sleep(1.1)
    pdb.bind_session(conn, pid_b, "s1")
    winner = pdb.get_session_project(conn, "s1")
    assert winner is not None
    assert winner.project_id == pid_b


def test_list_session_bindings_orders_most_recent_first(conn):
    pid = pdb.create_project(conn, name="P", folders=["/x"])
    pdb.bind_session(conn, pid, "s1")
    import time as _time

    _time.sleep(1.1)
    pdb.bind_session(conn, pid, "s2")
    _time.sleep(1.1)
    pdb.bind_session(conn, pid, "s3")
    bindings = pdb.list_session_bindings(conn, pid)
    assert [b.session_id for b in bindings] == ["s3", "s2", "s1"]


def test_get_session_project_returns_none_when_unbound(conn):
    pid = pdb.create_project(conn, name="P", folders=["/x"])
    pdb.bind_session(conn, pid, "s1")
    # Unbound session returns None.
    assert pdb.get_session_project(conn, "s_unknown") is None
