"""Tests for the cron-session bypass guard in _inject_persona.

Guards added at the top of the try block:
  1. session_id startswith "cron_"  → return None (no injection)
  2. user_message startswith "[IMPORTANT:"  → return None (system preamble)

Each guard is validated both for suppression AND for non-regression
(same message on an interactive session still injects).
"""
from __future__ import annotations

import sys
from pathlib import Path

_PLUGIN_DIR = Path(__file__).resolve().parent.parent
if str(_PLUGIN_DIR.parent) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_DIR.parent))

import matrix_coder as plugin  # noqa: E402
from matrix_coder.core.hermes_bridge import bridge  # noqa: E402

# A coding message that reliably triggers implicit routing on a normal session.
_CODING_MSG = "matrix executor: add a new migration"


def setup_function():
    bridge.clear_active_persona()


# ---------------------------------------------------------------------------
# 1. cron_ session_id suppresses injection
# ---------------------------------------------------------------------------


def test_cron_session_id_suppresses_injection():
    """A cron_ session_id must suppress injection regardless of message."""
    result = plugin._inject_persona(
        user_message=_CODING_MSG,
        session_id="cron_disk_space_check_1749123456",
    )
    assert result is None
    # Bridge must NOT be left active (no side-effect leak).
    assert bridge.is_active() is False


def test_cron_session_id_varied_job_names():
    """Guard works for arbitrary job names embedded in the cron_ prefix."""
    for sid in (
        "cron_email_check_1000000000",
        "cron_daily_brief_9999999999",
        "cron_0_1749000000",
    ):
        bridge.clear_active_persona()
        assert plugin._inject_persona(user_message=_CODING_MSG, session_id=sid) is None


# ---------------------------------------------------------------------------
# 2. Same message on interactive session DOES inject (regression guard)
# ---------------------------------------------------------------------------


def test_interactive_session_still_injects():
    """An explicit trigger on a normal uuid session must not be suppressed."""
    sid = "550e8400-e29b-41d4-a716-446655440000"
    bridge.clear_active_persona(sid)
    result = plugin._inject_persona(
        user_message="matrix executor: add a new migration",
        session_id=sid,
    )
    assert result is not None
    assert bridge.is_active(sid) is True


# ---------------------------------------------------------------------------
# 3. [IMPORTANT: ...] system preamble suppresses injection
# ---------------------------------------------------------------------------


def test_system_preamble_suppresses_injection():
    preamble = (
        "[IMPORTANT: You are running as a scheduled cron job. "
        "Execute the daily-brief task now.]"
    )
    result = plugin._inject_persona(user_message=preamble, session_id="some-uuid")
    assert result is None


def test_system_preamble_with_leading_whitespace_suppresses():
    """Strip() is applied so leading whitespace doesn't defeat the guard."""
    preamble = "  [IMPORTANT: system scheduled task]"
    result = plugin._inject_persona(user_message=preamble, session_id="some-uuid")
    assert result is None


# ---------------------------------------------------------------------------
# 4. None session_id → treated as interactive, no crash
# ---------------------------------------------------------------------------


def test_none_session_id_does_not_crash():
    """None session_id is coerced to '' and behaves as an interactive session."""
    bridge.clear_active_persona()
    # A direct-verdict message (no persona activated) just returns a verdict string.
    result = plugin._inject_persona(
        user_message="fix README typo",
        session_id=None,
    )
    # Must not raise; return value is either None or a trusted-tier dict
    # {"context": str, "target": "developer"} (shape changed in issue #140).
    assert result is None or (isinstance(result, dict) and result.get("context"))
