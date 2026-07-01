"""Tests for the delegate_task child bypass guard in _inject_persona.

Guard added before the implicit-routing block:
  conversation_history contains a system message with the delegate_task
  child marker ("focused subagent working on a specific delegated task")
  → skip IMPLICIT persona routing (return None).

This prevents the IntentGate from re-classifying a child's explicit task
and routing execution work to read-only specialists like 'verify' (#151).
EXPLICIT ``matrix ...`` triggers are NOT affected — workflow personas
that explicitly invoke a role still work inside subagents.

Each guard is validated both for suppression AND for non-regression
(same message on a top-level session still routes implicitly).
"""
from __future__ import annotations

import sys
from pathlib import Path

_PLUGIN_DIR = Path(__file__).resolve().parent.parent
if str(_PLUGIN_DIR.parent) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_DIR.parent))

import matrix_coder as plugin  # noqa: E402
from matrix_coder.core.hermes_bridge import bridge  # noqa: E402

# A coding message that would trigger implicit routing on a top-level session.
_CODING_MSG = "refactor the auth module to use async"

# The system prompt prefix that _build_child_system_prompt (delegate_tool.py:674)
# always sets on delegate_task children. Top-level agents never carry this.
_CHILD_SYSTEM = (
    "You are a focused subagent working on a specific delegated task.\n\n"
    "YOUR TASK:\nProcess YouTube URL through MP3 cycle.\n"
    "Use terminal tool to run yt-dlp, ffmpeg, rm, ls."
)

_CHILD_HISTORY = [{"role": "system", "content": _CHILD_SYSTEM}]
_TOP_LEVEL_HISTORY = [
    {"role": "system", "content": "You are Hermes Agent, a self-improving AI assistant."},
]


def setup_function():
    bridge.clear_active_persona()


# ---------------------------------------------------------------------------
# 1. delegate_task child suppresses IMPLICIT injection
# ---------------------------------------------------------------------------


def test_subagent_child_suppresses_implicit_routing():
    """A delegate_task child's system-prompt marker must suppress implicit routing."""
    result = plugin._inject_persona(
        user_message=_CODING_MSG,
        session_id="child-session-123",
        conversation_history=_CHILD_HISTORY,
    )
    assert result is None
    assert bridge.is_active("child-session-123") is False


def test_subagent_marker_detected_regardless_of_position():
    """The guard scans system messages even if the child prompt is long."""
    long_child_history = [
        {"role": "system", "content": "You are a focused subagent working on a specific delegated task." + " padding " * 200},
        {"role": "user", "content": "do stuff"},
    ]
    result = plugin._inject_persona(
        user_message=_CODING_MSG,
        session_id="child-session-long",
        conversation_history=long_child_history,
    )
    assert result is None


# ---------------------------------------------------------------------------
# 2. Same message on top-level session DOES route implicitly (regression guard)
# ---------------------------------------------------------------------------


def test_top_level_session_routes_implicitly():
    """A top-level agent without the child marker must still hit implicit routing."""
    # _CODING_MSG ("refactor the auth module") triggers implicit routing.
    # The return may be a persona dict or a direct-verdict recommendation,
    # but it must NOT be None (which would mean routing was skipped).
    result = plugin._inject_persona(
        user_message=_CODING_MSG,
        session_id="top-level-session",
        conversation_history=_TOP_LEVEL_HISTORY,
    )
    # Implicit routing produces either a MATRIX verdict (persona dict) or a
    # DIRECT verdict (recommendation dict). Both are non-None.
    assert result is not None, "top-level implicit routing was wrongly skipped"


# ---------------------------------------------------------------------------
# 3. EXPLICIT "matrix ..." trigger still works inside subagents
# ---------------------------------------------------------------------------


def test_explicit_matrix_trigger_works_in_subagent():
    """An explicit 'matrix executor: ...' trigger must fire even inside a child.

    Workflow personas (ralph, autopilot, ultraqa) spawn children with explicit
    matrix triggers. The subagent guard must not block the explicit path.
    """
    result = plugin._inject_persona(
        user_message="matrix executor: implement the CSV export feature",
        session_id="child-explicit",
        conversation_history=_CHILD_HISTORY,
    )
    assert result is not None
    assert isinstance(result, dict)
    assert "context" in result


# ---------------------------------------------------------------------------
# 4. Edge cases: empty / missing / malformed history
# ---------------------------------------------------------------------------


def test_empty_history_does_not_crash():
    """No conversation_history → treated as top-level (guard does not fire)."""
    result = plugin._inject_persona(
        user_message=_CODING_MSG,
        session_id="no-history-session",
        conversation_history=[],
    )
    # Must not raise; falls through to normal implicit routing.
    assert result is None or isinstance(result, dict)


def test_none_history_does_not_crash():
    result = plugin._inject_persona(
        user_message=_CODING_MSG,
        session_id="none-history-session",
        conversation_history=None,
    )
    assert result is None or isinstance(result, dict)


def test_history_without_system_message_does_not_crash():
    """If there's no system message, the guard cannot fire — safe fallback."""
    result = plugin._inject_persona(
        user_message=_CODING_MSG,
        session_id="no-system-msg",
        conversation_history=[{"role": "user", "content": "hello"}],
    )
    assert result is None or isinstance(result, dict)
