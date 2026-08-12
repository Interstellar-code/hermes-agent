"""Tests for session handoff (CLI to gateway platform).

The handoff state machine lives on the ``sessions`` table:

    None  → "pending" → "running" → ("completed" | "failed")

CLI side calls ``request_handoff`` and poll-waits on ``get_handoff_state``.
Gateway side iterates ``list_pending_handoffs``, calls ``claim_handoff`` to
flip pending → running, and finishes with ``complete_handoff`` or
``fail_handoff``.
"""

from __future__ import annotations

import time

import pytest

from hermes_state import SessionDB


class TestHandoffStateDB:
    """Test the handoff schema + helper methods on SessionDB."""

    @pytest.fixture
    def db(self, tmp_path, monkeypatch):
        home = tmp_path / ".hermes"
        home.mkdir()
        monkeypatch.setenv("HERMES_HOME", str(home))
        return SessionDB(db_path=home / "state.db")

    def _make_session(self, db, session_id, source="cli", title=None):
        """Insert a session row directly for testing."""
        def _do(conn):
            conn.execute(
                "INSERT OR IGNORE INTO sessions (id, source, title, started_at) "
                "VALUES (?, ?, ?, ?)",
                (session_id, source, title, time.time()),
            )
        db._execute_write(_do)

    def test_columns_exist(self, db):
        db._conn.execute(
            "SELECT handoff_state, handoff_platform, handoff_error "
            "FROM sessions LIMIT 0"
        )

    def test_request_handoff_marks_pending(self, db):
        sid = "sess-1"
        self._make_session(db, sid)

        assert db.request_handoff(sid, "telegram") is True

        state = db.get_handoff_state(sid)
        assert state == {
            "state": "pending",
            "platform": "telegram",
            "error": None,
        }

    def test_request_handoff_rejects_in_flight(self, db):
        sid = "sess-2"
        self._make_session(db, sid)

        assert db.request_handoff(sid, "telegram") is True
        # Still pending → reject re-request
        assert db.request_handoff(sid, "discord") is False

        # And after gateway claims it (running) → still rejected
        assert db.claim_handoff(sid) is True
        assert db.request_handoff(sid, "discord") is False

    def test_request_handoff_after_terminal_state_resets_error(self, db):
        sid = "sess-3"
        self._make_session(db, sid)
        db.request_handoff(sid, "telegram")
        db.claim_handoff(sid)
        db.fail_handoff(sid, "earlier failure")

        # User retries — should be allowed and clear the prior error.
        assert db.request_handoff(sid, "discord") is True
        state = db.get_handoff_state(sid)
        assert state["state"] == "pending"
        assert state["platform"] == "discord"
        assert state["error"] is None

    def test_list_pending_handoffs_excludes_running_and_terminal(self, db):
        a, b, c, d = "sess-a", "sess-b", "sess-c", "sess-d"
        for sid in (a, b, c, d):
            self._make_session(db, sid)

        db.request_handoff(a, "telegram")
        db.request_handoff(b, "discord")
        db.request_handoff(c, "telegram")
        db.claim_handoff(c)  # c is now running, not pending
        db.request_handoff(d, "slack")
        db.claim_handoff(d)
        db.complete_handoff(d)  # d is terminal

        pending = db.list_pending_handoffs()
        ids = [r["id"] for r in pending]
        assert set(ids) == {a, b}

    def test_claim_handoff_is_atomic(self, db):
        sid = "sess-claim"
        self._make_session(db, sid)
        db.request_handoff(sid, "telegram")

        # First claim wins
        assert db.claim_handoff(sid) is True
        # Second claim is a no-op (state is now "running", not "pending")
        assert db.claim_handoff(sid) is False
        assert db.get_handoff_state(sid)["state"] == "running"

    def test_complete_handoff_clears_error(self, db):
        sid = "sess-complete"
        self._make_session(db, sid)
        db.request_handoff(sid, "telegram")
        db.claim_handoff(sid)
        db.fail_handoff(sid, "transient")
        # User retries; mock the watcher path
        db.request_handoff(sid, "telegram")
        db.claim_handoff(sid)
        db.complete_handoff(sid)

        state = db.get_handoff_state(sid)
        assert state["state"] == "completed"
        assert state["error"] is None

    def test_fail_handoff_records_reason(self, db):
        sid = "sess-fail"
        self._make_session(db, sid)
        db.request_handoff(sid, "telegram")
        db.claim_handoff(sid)
        db.fail_handoff(sid, "no home channel for telegram")

        state = db.get_handoff_state(sid)
        assert state["state"] == "failed"
        assert state["error"] == "no home channel for telegram"

    def test_fail_handoff_truncates_long_reasons(self, db):
        sid = "sess-fail-long"
        self._make_session(db, sid)
        db.request_handoff(sid, "telegram")
        db.claim_handoff(sid)

        # 1000-character error string
        big_err = "x" * 1000
        db.fail_handoff(sid, big_err)

        state = db.get_handoff_state(sid)
        assert len(state["error"]) <= 500

    def test_get_handoff_state_for_unknown_session(self, db):
        assert db.get_handoff_state("does-not-exist") is None

    def test_full_pending_to_completed_flow(self, db):
        """End-to-end sequence the CLI + gateway watcher follow."""
        sid = "sess-flow"
        self._make_session(db, sid, title="my session")
        db.append_message(sid, "user", "Hello")
        db.append_message(sid, "assistant", "Hi there!")

        # CLI: request handoff
        assert db.request_handoff(sid, "telegram") is True
        assert db.get_handoff_state(sid)["state"] == "pending"

        # Gateway watcher: discover + claim
        pending = db.list_pending_handoffs()
        assert len(pending) == 1
        assert pending[0]["id"] == sid
        assert db.claim_handoff(sid) is True
        assert db.get_handoff_state(sid)["state"] == "running"

        # Gateway uses get_messages to load the transcript (real flow uses
        # session_store.switch_session which reads the same table).
        messages = db.get_messages(sid)
        assert [m["role"] for m in messages] == ["user", "assistant"]

        # Gateway: mark completed
        db.complete_handoff(sid)
        assert db.get_handoff_state(sid)["state"] == "completed"
        assert db.list_pending_handoffs() == []


class TestHandoffRowAndExpiry:
    """Regressions for #221: dead row fallback, merged failure causes, orphans."""

    @pytest.fixture
    def db(self, tmp_path, monkeypatch):
        home = tmp_path / ".hermes"
        home.mkdir()
        monkeypatch.setenv("HERMES_HOME", str(home))
        return SessionDB(db_path=home / "state.db")

    def _backdate_request(self, db, session_id, age_s):
        def _do(conn):
            conn.execute(
                "UPDATE sessions SET handoff_requested_at = ? WHERE id = ?",
                (time.time() - age_s, session_id),
            )
        db._execute_write(_do)

    def test_set_session_title_still_does_not_create_rows(self, db):
        """Pins the premise: the old fallback could never have worked."""
        assert db.set_session_title("ghost", "whatever") is False
        assert db.get_session("ghost") is None

    def test_ensure_session_row_creates_the_row(self, db):
        assert db.ensure_session_row("fresh", source="cli", title="handoff-fresh") is True
        row = db.get_session("fresh")
        assert row is not None
        assert row["source"] == "cli"
        assert row["title"] == "handoff-fresh"

    def test_ensure_session_row_is_idempotent_and_keeps_title(self, db):
        db.ensure_session_row("keep", source="cli")
        db.set_session_title("keep", "user-chosen")
        assert db.ensure_session_row("keep", source="tui", title="handoff-keep") is False
        row = db.get_session("keep")
        assert row["title"] == "user-chosen"
        assert row["source"] == "cli"

    def test_ensure_session_row_survives_title_collision(self, db):
        db.ensure_session_row("first", title="taken")
        assert db.ensure_session_row("second", title="taken") is True
        assert db.get_session("second") is not None

    def test_ensure_session_row_then_request_succeeds(self, db):
        """The full CLI fallback path: no row → row → queued (not 'in flight')."""
        sid = "no-history-yet"
        assert db.request_handoff_status(sid, "telegram") == "missing"
        db.ensure_session_row(sid, source="cli", title=f"handoff-{sid[:8]}")
        assert db.request_handoff_status(sid, "telegram") == "queued"

    def test_request_handoff_status_distinguishes_causes(self, db):
        sid = "distinct"
        # No row at all.
        assert db.request_handoff_status(sid, "telegram") == "missing"
        # Row exists, first request wins.
        db.ensure_session_row(sid)
        assert db.request_handoff_status(sid, "telegram") == "queued"
        # Second request while pending is a genuine in-flight rejection.
        assert db.request_handoff_status(sid, "discord") == "in_flight"
        db.claim_handoff(sid)
        assert db.request_handoff_status(sid, "discord") == "in_flight"
        # Terminal state re-arms.
        db.complete_handoff(sid)
        assert db.request_handoff_status(sid, "telegram") == "queued"

    def test_request_handoff_bool_wrapper_unchanged(self, db):
        db.ensure_session_row("compat")
        assert db.request_handoff("compat", "telegram") is True
        assert db.request_handoff("compat", "telegram") is False
        assert db.request_handoff("nonexistent", "telegram") is False

    def test_request_handoff_stamps_requested_at(self, db):
        db.ensure_session_row("stamped")
        before = time.time()
        db.request_handoff("stamped", "telegram")
        row = db.get_session("stamped")
        assert row["handoff_requested_at"] >= before

    def test_expire_stale_handoffs_releases_abandoned_requests(self, db):
        sid = "abandoned"
        db.ensure_session_row(sid)
        db.request_handoff(sid, "telegram")
        self._backdate_request(db, sid, 3600)

        assert db.expire_stale_handoffs(max_age_s=600) == [sid]
        state = db.get_handoff_state(sid)
        assert state["state"] == "failed"
        assert "expired" in state["error"]
        # And the user can retry afterwards.
        assert db.request_handoff_status(sid, "telegram") == "queued"

    def test_expire_stale_handoffs_leaves_fresh_and_running_rows(self, db):
        fresh, running = "fresh-req", "running-req"
        for sid in (fresh, running):
            db.ensure_session_row(sid)
            db.request_handoff(sid, "telegram")
        db.claim_handoff(running)
        self._backdate_request(db, running, 3600)

        assert db.expire_stale_handoffs(max_age_s=600) == []
        assert db.get_handoff_state(fresh)["state"] == "pending"
        assert db.get_handoff_state(running)["state"] == "running"

    def test_stale_pending_row_is_not_handed_to_the_watcher(self, db):
        """The watcher must not resurrect a request the user walked away from."""
        sid = "orphan"
        db.ensure_session_row(sid)
        db.request_handoff(sid, "telegram")
        self._backdate_request(db, sid, 86400)

        assert db.list_pending_handoffs() == []
        db.expire_stale_handoffs()
        assert db.get_handoff_state(sid)["state"] == "failed"

    def test_legacy_pending_row_without_timestamp_expires(self, db):
        """Rows written before handoff_requested_at existed fall back to started_at."""
        sid = "legacy"

        def _do(conn):
            conn.execute(
                "INSERT INTO sessions (id, source, started_at, handoff_state, "
                "handoff_platform) VALUES (?, ?, ?, 'pending', 'telegram')",
                (sid, "cli", time.time() - 86400),
            )
        db._execute_write(_do)

        assert db.list_pending_handoffs() == []
        assert db.expire_stale_handoffs() == [sid]
        assert db.get_handoff_state(sid)["state"] == "failed"


class TestHandoffCommandMessages:
    """User-visible behaviour of ``/handoff`` (#221)."""

    @pytest.fixture
    def cli(self, tmp_path, monkeypatch):
        import cli as cli_mod
        import gateway.config as gwconfig
        from hermes_cli.cli_commands_mixin import CLICommandsMixin

        home = tmp_path / ".hermes"
        home.mkdir()
        monkeypatch.setenv("HERMES_HOME", str(home))

        class FakeHome:
            name = "Family"
            chat_id = "123"

        class FakePlatformCfg:
            enabled = True

        class FakeConfig:
            platforms = {"telegram": FakePlatformCfg()}

            def get_home_channel(self, _platform):
                return FakeHome()

        monkeypatch.setattr(gwconfig, "Platform", lambda name: name)
        monkeypatch.setattr(gwconfig, "load_gateway_config", lambda: FakeConfig())

        printed: list[str] = []
        monkeypatch.setattr(cli_mod, "_cprint", lambda text: printed.append(str(text)))

        class _Stub(CLICommandsMixin):
            def __init__(self, db):
                self.session_id = "cli-handoff-session"
                self._session_db = db
                self._agent_running = False
                self._should_exit = False

        stub = _Stub(SessionDB(db_path=home / "state.db"))
        stub.printed = printed
        return stub

    def test_missing_row_is_created_not_misreported(self, cli, monkeypatch):
        """The fallback must really create the row, so the request queues."""
        assert cli._session_db.get_session(cli.session_id) is None
        # Don't sit in the 60s poll — assert on the queue step only.
        monkeypatch.setattr(cli._session_db, "get_handoff_state", lambda _sid: {"state": "failed", "error": "stop"})

        cli._handle_handoff_command("/handoff telegram")

        assert cli._session_db.get_session(cli.session_id) is not None
        text = "\n".join(cli.printed)
        assert "already" not in text
        assert "Queued handoff" in text

    def test_in_flight_says_in_flight(self, cli):
        cli._session_db.ensure_session_row(cli.session_id)
        cli._session_db.request_handoff(cli.session_id, "telegram")

        assert cli._handle_handoff_command("/handoff telegram") is True

        text = "\n".join(cli.printed)
        assert "already pending for handoff" in text

    def test_no_session_row_reports_missing_history(self, cli, monkeypatch):
        """When the row genuinely can't be created, say so — don't claim in-flight."""
        monkeypatch.setattr(cli._session_db, "ensure_session_row", lambda *a, **k: False)

        assert cli._handle_handoff_command("/handoff telegram") is True

        text = "\n".join(cli.printed)
        assert "no record in state.db yet" in text
        assert "already" not in text

    def test_interrupted_poll_releases_the_pending_row(self, cli, monkeypatch):
        """Ctrl-C mid-wait must not leave a row armed for the gateway watcher."""
        def _interrupt(_seconds):
            raise KeyboardInterrupt

        monkeypatch.setattr(time, "sleep", _interrupt)

        with pytest.raises(KeyboardInterrupt):
            cli._handle_handoff_command("/handoff telegram")

        assert cli._session_db.get_handoff_state(cli.session_id)["state"] == "failed"
        assert cli._session_db.list_pending_handoffs() == []


class TestHandoffCommandRegistration:
    """Slash-command surface checks."""

    def test_command_registered(self):
        from hermes_cli.commands import resolve_command
        cmd = resolve_command("handoff")
        assert cmd is not None
        assert cmd.name == "handoff"
        assert cmd.category == "Session"

    def test_command_is_cli_only(self):
        """`/handoff` is initiated from the CLI; gateway shouldn't expose it."""
        from hermes_cli.commands import resolve_command, GATEWAY_KNOWN_COMMANDS
        cmd = resolve_command("handoff")
        assert cmd is not None
        assert cmd.cli_only is True
        assert "handoff" not in GATEWAY_KNOWN_COMMANDS
