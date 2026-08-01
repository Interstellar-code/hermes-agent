"""Project Context block in the cached system prompt (issue #201).

The bridge from an explicit project↔session binding to the model-visible
prompt. What these tests actually defend is the CACHE invariant: the block is
materialized once, when the prompt is first built, and a conversation that has
already built its prompt never sees a binding change. Everything else here is
in service of that.
"""

from __future__ import annotations

import sqlite3
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from agent.conversation_loop import _restore_or_build_system_prompt
from agent.system_prompt import build_system_prompt_parts
from hermes_cli import projects_db as pdb
from hermes_cli.projects_prompt import format_project_context, project_context_block


@pytest.fixture
def projects_home(tmp_path, monkeypatch):
    """Point the default projects.db at a temp dir.

    The helper resolves its own connection (it is called deep inside prompt
    construction and is given only a session id), so the DB has to be
    redirected at the path level rather than injected.
    """
    # Redirect via the ENVIRONMENT, not by patching projects_db_path.
    #
    # The helper resolves its own connection from whatever `hermes_cli.projects_db`
    # is in sys.modules when it runs. Another test in the suite reloads that
    # module, so this file's import-time `pdb` can be a stale object — patching
    # an attribute on it then silently does nothing, the helper opens the real
    # default projects.db, finds no binding, and returns "". That failure is
    # indistinguishable from a correct "unbound" result, which is exactly the
    # kind of green-looking broken test worth spending a fixture on.
    #
    # get_hermes_home() reads HERMES_HOME on every call, so an env redirect
    # binds every module object at once, stale or fresh.
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    db_path = tmp_path / "projects.db"
    assert pdb.projects_db_path() == db_path, "projects.db is not redirected"
    return db_path


def _project(conn, **kw) -> str:
    kw.setdefault("name", "SwitchUI")
    kw.setdefault("slug", "hermes-switchui")
    kw.setdefault("folders", ["/Users/rohits/Development/hermes-switchui"])
    return pdb.create_project(conn, **kw)


# ---------------------------------------------------------------------------
# Resolution
# ---------------------------------------------------------------------------


def test_bound_session_renders_its_project(projects_home):
    conn = pdb.connect(db_path=projects_home)
    try:
        pid = _project(
            conn,
            primary_path="/Users/rohits/Development/hermes-switchui",
            board_slug="default",
        )
        pdb.bind_session(conn, pid, "session-alpha")
    finally:
        conn.close()

    block = project_context_block("session-alpha")

    assert block.startswith("## Project Context")
    assert "Project: SwitchUI (`hermes-switchui`)" in block
    assert f"Project ID: {pid}" in block
    assert "Primary path: /Users/rohits/Development/hermes-switchui" in block
    assert "Bound board: default" in block


def test_unbound_session_gets_nothing(projects_home):
    conn = pdb.connect(db_path=projects_home)
    try:
        _project(conn)  # a project exists, but this session is not bound to it
    finally:
        conn.close()

    assert project_context_block("session-with-no-binding") == ""


def test_only_the_bound_project_is_rendered(projects_home):
    """The explicit binding is the ONLY source — no cwd or active fallback.

    Guessing a project from the working directory would be a claim the model
    carries for the whole conversation, because this text is cached.
    """
    conn = pdb.connect(db_path=projects_home)
    try:
        bound = _project(conn, name="Bound", slug="bound", folders=["/srv/bound"])
        _project(conn, name="Elsewhere", slug="elsewhere", folders=["/srv/elsewhere"])
        pdb.bind_session(conn, bound, "session-alpha")
    finally:
        conn.close()

    block = project_context_block("session-alpha")
    assert "Bound" in block
    assert "Elsewhere" not in block


def test_empty_session_id_is_not_a_lookup(projects_home):
    assert project_context_block("") == ""
    assert project_context_block(None) == ""


# ---------------------------------------------------------------------------
# Fail-closed
# ---------------------------------------------------------------------------


def test_orphaned_binding_degrades_to_no_block(projects_home):
    """A binding whose project row is gone must not break agent startup."""
    conn = pdb.connect(db_path=projects_home)
    try:
        pid = _project(conn)
        pdb.bind_session(conn, pid, "session-alpha")
        # Drop the project row out from under the binding, leaving the
        # project_sessions row behind.
        conn.execute("PRAGMA foreign_keys=OFF")
        conn.execute("DELETE FROM projects WHERE id = ?", (pid,))
        conn.commit()
    finally:
        conn.close()

    assert project_context_block("session-alpha") == ""


def test_unreadable_store_degrades_to_no_block(projects_home, monkeypatch):
    """A locked or corrupt projects.db must not stop an agent from starting."""

    def _boom(*_a, **_k):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(pdb, "connect", _boom)
    assert project_context_block("session-alpha") == ""


# ---------------------------------------------------------------------------
# Bounded, deterministic serialization
# ---------------------------------------------------------------------------


def test_folder_list_is_bounded_and_reports_what_it_hid():
    project = MagicMock()
    project.name = "Big"
    project.slug = "big"
    project.id = "prj_big"
    project.primary_path = None
    project.board_slug = None
    project.folders = [MagicMock(path=f"/srv/p{i:02d}") for i in range(25)]

    block = format_project_context(project)

    assert block.count("- /srv/") == 10
    assert "- /srv/p09" in block
    assert "- /srv/p10" not in block
    assert "(+15 more not shown)" in block


def test_serialization_is_deterministic(projects_home):
    conn = pdb.connect(db_path=projects_home)
    try:
        pid = _project(conn, folders=["/srv/b", "/srv/a", "/srv/c"])
        pdb.bind_session(conn, pid, "session-alpha")
    finally:
        conn.close()

    assert project_context_block("session-alpha") == project_context_block("session-alpha")


# ---------------------------------------------------------------------------
# Cache stability — the point of the whole exercise
# ---------------------------------------------------------------------------


def _agent(session_id: str, session_db=None):
    agent = MagicMock()
    agent._cached_system_prompt = None
    agent.session_id = session_id
    agent.model = "test-model"
    agent.provider = "openrouter"
    agent.platform = "cli"
    agent._session_db = session_db
    return agent


def test_rebinding_after_the_first_turn_does_not_touch_the_cached_prompt(projects_home):
    """Turn 2 restores turn 1's prompt verbatim, even after a rebind.

    This is the cost-control invariant: a rebuilt prefix is a cache miss for
    every remaining turn of the conversation, and a system prompt that changes
    under the model mid-conversation is its own kind of bug.
    """
    conn = pdb.connect(db_path=projects_home)
    try:
        first = _project(conn, name="First", slug="first", folders=["/srv/first"])
        second = _project(conn, name="Second", slug="second", folders=["/srv/second"])
        pdb.bind_session(conn, first, "session-alpha")
    finally:
        conn.close()

    # Turn 1: no history → builds, and the built prompt carries the binding.
    db = MagicMock()
    db.get_session.return_value = None
    agent = _agent("session-alpha", session_db=db)
    agent._build_system_prompt = MagicMock(
        side_effect=lambda _msg: "PROMPT\n\n" + project_context_block(agent.session_id)
    )
    _restore_or_build_system_prompt(agent, None, None)
    turn_one_prompt = agent._cached_system_prompt
    assert "First" in turn_one_prompt

    # The user rebinds the live session to another project.
    conn = pdb.connect(db_path=projects_home)
    try:
        pdb.bind_session(conn, second, "session-alpha")
    finally:
        conn.close()

    # Turn 2 on a fresh agent (the gateway builds one per turn) restores the
    # stored prompt instead of rebuilding it.
    db2 = MagicMock()
    db2.get_session.return_value = {"system_prompt": turn_one_prompt}
    agent2 = _agent("session-alpha", session_db=db2)
    agent2._build_system_prompt = MagicMock(return_value="REBUILT — must not happen")
    _restore_or_build_system_prompt(agent2, None, [{"role": "user", "content": "hi"}])

    assert agent2._cached_system_prompt == turn_one_prompt
    assert "Second" not in agent2._cached_system_prompt
    agent2._build_system_prompt.assert_not_called()


def _prompt_parts(session_id: str):
    """Run the real prompt assembly with everything else stubbed out."""
    agent = SimpleNamespace(
        load_soul_identity=False,
        skip_context_files=False,
        valid_tool_names=[],
        _task_completion_guidance=False,
        _tool_use_enforcement=False,
        _environment_probe=False,
        _kanban_worker_guidance="",
        _memory_store=None,
        _memory_manager=None,
        model="",
        provider="",
        platform="",
        pass_session_id=False,
        session_id=session_id,
    )
    with (
        patch("run_agent.load_soul_md", return_value=""),
        patch("run_agent.build_nous_subscription_prompt", return_value=""),
        patch("run_agent.build_environment_hints", return_value=""),
        patch("run_agent.build_context_files_prompt", return_value=""),
    ):
        return build_system_prompt_parts(agent)


def test_block_lands_in_the_context_tier_not_the_volatile_one(projects_home):
    """Tier placement IS the cache contract: volatile content is not cached."""
    conn = pdb.connect(db_path=projects_home)
    try:
        pid = _project(conn, primary_path="/Users/rohits/Development/hermes-switchui")
        pdb.bind_session(conn, pid, "session-alpha")
    finally:
        conn.close()

    parts = _prompt_parts("session-alpha")

    assert "## Project Context" in parts["context"]
    assert "SwitchUI" in parts["context"]
    assert "## Project Context" not in parts["volatile"]
    assert "## Project Context" not in parts["stable"]


def test_unbound_session_prompt_is_unchanged(projects_home):
    conn = pdb.connect(db_path=projects_home)
    try:
        _project(conn)
    finally:
        conn.close()

    assert "## Project Context" not in _prompt_parts("session-with-no-binding")["context"]


def test_a_new_session_picks_up_its_own_binding(projects_home):
    """Rebinding is not forbidden — it just belongs to the NEXT session."""
    conn = pdb.connect(db_path=projects_home)
    try:
        first = _project(conn, name="First", slug="first", folders=["/srv/first"])
        second = _project(conn, name="Second", slug="second", folders=["/srv/second"])
        pdb.bind_session(conn, first, "session-alpha")
        pdb.bind_session(conn, second, "session-beta")
    finally:
        conn.close()

    assert "First" in project_context_block("session-alpha")
    assert "Second" in project_context_block("session-beta")
    assert "Second" not in project_context_block("session-alpha")
