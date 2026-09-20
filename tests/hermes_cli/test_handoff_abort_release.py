"""#221 — a CLI abort during the handoff wait must release the row it queued.

``expire_stale_handoffs`` (the TTL backstop) only runs inside a live gateway
watcher loop, so a Ctrl-C at the CLI leaves a ``pending`` row that nothing
clears until some gateway next ticks.  The wait is wrapped so any teardown that
still runs Python CAS-clears the row.

Scoped to ``pending`` on purpose: once the watcher has claimed the row
(``running``) it owns the terminal state, and a waiter-side unconditional fail
is the split-brain bug ``fail_handoff``'s docstring warns about.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from hermes_cli.cli_commands_mixin import CLICommandsMixin


class _Mixin(CLICommandsMixin):
    def __init__(self, db):
        self._session_db = db
        self.session_id = "sess-abort-1"


def test_release_pending_handoff_is_pending_scoped():
    db = MagicMock()
    _Mixin(db)._release_pending_handoff("abandoned")
    db.fail_handoff.assert_called_once_with(
        "sess-abort-1", "abandoned", only_states=("pending",))


def test_release_falls_back_when_db_lacks_only_states():
    """Mixed install: legacy SessionDB without the keyword still gets cleared."""
    db = MagicMock()
    db.fail_handoff.side_effect = [TypeError("unexpected keyword 'only_states'"), True]
    _Mixin(db)._release_pending_handoff("abandoned")
    assert db.fail_handoff.call_count == 2
    assert db.fail_handoff.call_args_list[1].args == ("sess-abort-1", "abandoned")


def test_release_swallows_db_errors():
    """Teardown path: a dead DB must not mask the original KeyboardInterrupt."""
    db = MagicMock()
    db.fail_handoff.side_effect = RuntimeError("db gone")
    _Mixin(db)._release_pending_handoff("abandoned")  # must not raise


def test_ctrl_c_during_wait_releases_then_reraises(monkeypatch):
    """Drives the real _handle_handoff_command call site, not a re-implementation."""
    db = MagicMock()
    db.request_handoff_status.return_value = "queued"
    mixin = _Mixin(db)

    monkeypatch.setattr(_Mixin, "_handoff_validate_target",
                        lambda self, p: SimpleNamespace(name="home-1"))
    monkeypatch.setattr(_Mixin, "_handoff_prepare_session", lambda self: "my-session")
    monkeypatch.setattr(_Mixin, "_handoff_wait",
                        lambda self, *a, **k: (_ for _ in ()).throw(KeyboardInterrupt()))

    with pytest.raises(KeyboardInterrupt):
        mixin._handle_handoff_command("/handoff telegram")

    db.fail_handoff.assert_called_once_with(
        "sess-abort-1", "abandoned before the gateway picked it up",
        only_states=("pending",))


def test_normal_completion_does_not_release(monkeypatch):
    """Guard against over-firing: a clean wait must leave the row alone."""
    db = MagicMock()
    db.request_handoff_status.return_value = "queued"
    mixin = _Mixin(db)

    monkeypatch.setattr(_Mixin, "_handoff_validate_target",
                        lambda self, p: SimpleNamespace(name="home-1"))
    monkeypatch.setattr(_Mixin, "_handoff_prepare_session", lambda self: "my-session")
    monkeypatch.setattr(_Mixin, "_handoff_wait", lambda self, *a, **k: False)

    assert mixin._handle_handoff_command("/handoff telegram") is False
    db.fail_handoff.assert_not_called()
