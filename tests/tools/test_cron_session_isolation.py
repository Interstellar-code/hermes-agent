"""Regression: cron-session detection must be task-local, not process-global.

The cron scheduler runs in a THREAD of the gateway process
(gateway/run.py: InProcessCronScheduler). It used to mark cron jobs by writing
``os.environ["HERMES_CRON_SESSION"]="1"`` — a process-global mutation that was
never unset, so after the first cron tick every concurrent interactive session
read as a cron job and had ``execute_code`` denied by the approval guard.

The fix moves the marker to a ContextVar set via
``set_session_vars(is_cron=...)``. These tests pin the isolation guarantee.
Each scenario runs in a fresh ``contextvars.Context`` so it mirrors a distinct
gateway task/cron-thread and is independent of test ordering.
"""

import contextvars

from tools.approval import _is_cron_session
from gateway.session_context import set_session_vars, clear_session_vars


def test_interactive_context_not_cron_despite_stale_env(monkeypatch):
    """The leak fix: an interactive session is NOT cron even if the process
    env is still poisoned (as the old in-thread scheduler left it)."""
    monkeypatch.setenv("HERMES_CRON_SESSION", "1")

    def scenario():
        tokens = set_session_vars(platform="telegram", chat_id="c1")  # is_cron defaults False
        try:
            return _is_cron_session()
        finally:
            clear_session_vars(tokens)

    # ContextVar set to "" by the interactive set_session_vars wins over the
    # stale os.environ value → not classified as cron.
    assert contextvars.Context().run(scenario) is False


def test_cron_context_is_cron(monkeypatch):
    """A cron job (set_session_vars(is_cron=True)) is detected as cron even
    with no process env var set."""
    monkeypatch.delenv("HERMES_CRON_SESSION", raising=False)

    def scenario():
        tokens = set_session_vars(platform="", is_cron=True)
        try:
            return _is_cron_session()
        finally:
            clear_session_vars(tokens)

    assert contextvars.Context().run(scenario) is True


def test_separate_process_env_fallback(monkeypatch):
    """Genuine separate-process cron / CLI / tests: no ContextVar was set in
    this context, so detection falls back to os.environ (compat preserved)."""
    monkeypatch.setenv("HERMES_CRON_SESSION", "1")
    # Fresh context => _CRON_SESSION is unset => get_session_env falls back to env.
    assert contextvars.Context().run(_is_cron_session) is True


def test_no_env_no_contextvar_not_cron(monkeypatch):
    """Baseline: nothing set anywhere → not cron."""
    monkeypatch.delenv("HERMES_CRON_SESSION", raising=False)
    assert contextvars.Context().run(_is_cron_session) is False


if __name__ == "__main__":  # pragma: no cover - manual smoke
    import pytest, sys
    sys.exit(pytest.main([__file__, "-v"]))
