"""Regression: cron-session detection must be task-local, not process-global.

At the merge-base, ``cron/scheduler.py`` marked cron jobs by writing
``os.environ["HERMES_CRON_SESSION"]="1"`` — a process-global mutation that was never
unset, so after the first cron tick every concurrent interactive session in the gateway
process read as a cron job and had ``execute_code`` denied by the approval guard.

**Both the fork and upstream fixed this independently** (convergent evolution, confirmed
during the v0.21.3 integration):

* the fork moved the marker to a ContextVar defaulting to ``""`` on every
  ``set_session_vars`` call;
* upstream moved it to a ContextVar the *scheduler* sets and token-resets around each job
  (``cron/scheduler.py`` ``enter()``/``exit()``), and deliberately leaves the var
  ``_UNSET`` for callers that never mention cron — its comment: "pinning '' would suppress
  the legacy os.environ fallback used by standalone entrypoints/tests".

Upstream's is the one that ships, so these tests pin UPSTREAM's contract. The difference
matters and is deliberate: with no ContextVar in scope, detection still falls back to
``os.environ``. That is the documented separate-process path, not a leak — no production
code on either side writes that variable any more.
"""

import contextvars

from tools.approval_context import _is_cron_approval_context
from gateway.session_context import set_session_vars, clear_session_vars


def test_cron_context_is_cron(monkeypatch):
    """A job scope marked cron reads as cron with no process env var set."""
    monkeypatch.delenv("HERMES_CRON_SESSION", raising=False)

    def scenario():
        tokens = set_session_vars(platform="", cron_session="1")
        try:
            return _is_cron_approval_context()
        finally:
            clear_session_vars(tokens)

    assert contextvars.Context().run(scenario) is True


def test_interactive_scope_is_not_cron(monkeypatch):
    """An interactive session that declares cron_session="" is NOT cron, even with the
    process env poisoned. This is the isolation guarantee the original bug violated."""
    monkeypatch.setenv("HERMES_CRON_SESSION", "1")

    def scenario():
        tokens = set_session_vars(platform="telegram", chat_id="c1", cron_session="")
        try:
            return _is_cron_approval_context()
        finally:
            clear_session_vars(tokens)

    assert contextvars.Context().run(scenario) is False


def test_cleared_scope_is_not_cron(monkeypatch):
    """clear_session_vars pins "" rather than _UNSET, so a torn-down gateway turn cannot
    inherit a stale env value either."""
    monkeypatch.setenv("HERMES_CRON_SESSION", "1")

    def scenario():
        tokens = set_session_vars(platform="telegram", cron_session="1")
        clear_session_vars(tokens)
        return _is_cron_approval_context()

    assert contextvars.Context().run(scenario) is False


def test_separate_process_env_fallback(monkeypatch):
    """Genuine separate-process cron / CLI / tests: no ContextVar was ever set in this
    context, so detection falls back to os.environ. Upstream preserves this DELIBERATELY —
    see the module docstring. Do not "fix" it by defaulting the var to ""."""
    monkeypatch.setenv("HERMES_CRON_SESSION", "1")
    assert contextvars.Context().run(_is_cron_approval_context) is True


def test_no_env_no_contextvar_not_cron(monkeypatch):
    """Baseline: nothing set anywhere → not cron."""
    monkeypatch.delenv("HERMES_CRON_SESSION", raising=False)
    assert contextvars.Context().run(_is_cron_approval_context) is False


def test_no_production_code_writes_the_env_var():
    """The root cause is gone and must stay gone: nothing outside tests may set
    ``os.environ["HERMES_CRON_SESSION"]``. A process-global write re-creates the original
    cross-session leak no matter how correct the ContextVar plumbing is."""
    import pathlib
    import re

    root = pathlib.Path(__file__).resolve().parents[2]
    pattern = re.compile(r"""environ\[["']HERMES_CRON_SESSION["']\]\s*=""")
    offenders = []
    for path in root.rglob("*.py"):
        parts = path.parts
        if "tests" in parts or "node_modules" in parts or ".git" in parts:
            continue
        try:
            if pattern.search(path.read_text(encoding="utf-8", errors="ignore")):
                offenders.append(str(path.relative_to(root)))
        except OSError:
            continue
    assert offenders == [], f"process-global cron marker reintroduced in: {offenders}"


if __name__ == "__main__":  # pragma: no cover - manual smoke
    import pytest, sys
    sys.exit(pytest.main([__file__, "-v"]))
