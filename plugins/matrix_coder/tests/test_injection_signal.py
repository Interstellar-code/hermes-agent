"""Tests for the observable injection signal (feat/mc-injection-signal).

Verifies:
1. Composed persona contains the ``[matrix-coder active: ...]`` marker line.
2. Composed persona contains the "Begin your reply..." instruction.
3. INFO log emitted on explicit injection.
4. INFO log emitted on implicit injection.
5. DEBUG log emitted when IntentGate declines (no coding intent).
6. DEBUG log emitted when DIRECT verdict fires (no persona activated).
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

_PLUGIN_DIR = Path(__file__).resolve().parent.parent
if str(_PLUGIN_DIR.parent) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_DIR.parent))

import matrix_coder as plugin  # noqa: E402
from matrix_coder.core import harness  # noqa: E402
from matrix_coder.core.hermes_bridge import bridge  # noqa: E402


# ---------------------------------------------------------------------------
# 1. Marker present in composed persona
# ---------------------------------------------------------------------------


def test_composed_persona_contains_marker_explicit():
    bridge.clear_active_persona()
    composed = harness.handle_trigger("matrix review security: check auth")

    assert composed is not None
    assert "[matrix-coder active: role=review, lens=security]" in composed


def test_composed_persona_contains_marker_no_lens():
    bridge.clear_active_persona()
    composed = harness.handle_trigger("matrix executor: add export")

    assert composed is not None
    assert "[matrix-coder active: role=executor, lens=none]" in composed


def test_composed_persona_contains_begin_reply_instruction():
    bridge.clear_active_persona()
    composed = harness.handle_trigger("matrix explore: map the repo")

    assert composed is not None
    assert "Begin your reply with the line above exactly as written." in composed


# ---------------------------------------------------------------------------
# 2. INFO log on explicit injection
# ---------------------------------------------------------------------------


def test_info_log_emitted_on_explicit_injection(caplog):
    bridge.clear_active_persona()
    with caplog.at_level(logging.INFO, logger="matrix_coder"):
        plugin._inject_persona(user_message="matrix executor add export")

    info_records = [r for r in caplog.records if r.levelno == logging.INFO]
    assert any(
        "persona injected" in r.message and "role=executor" in r.message and "mode=explicit" in r.message
        for r in info_records
    ), f"Expected INFO injection log, got: {[r.message for r in caplog.records]}"


def test_info_log_includes_lens_on_explicit_lensed_injection(caplog):
    bridge.clear_active_persona()
    with caplog.at_level(logging.INFO, logger="matrix_coder"):
        plugin._inject_persona(user_message="matrix review security: check auth")

    info_records = [r for r in caplog.records if r.levelno == logging.INFO]
    assert any(
        "lens=security" in r.message and "mode=explicit" in r.message
        for r in info_records
    )


# ---------------------------------------------------------------------------
# 3. INFO log on implicit injection
# ---------------------------------------------------------------------------


def test_info_log_emitted_on_implicit_injection(caplog):
    bridge.clear_active_persona()
    with caplog.at_level(logging.INFO, logger="matrix_coder"):
        plugin._inject_persona(user_message="is this auth safe?")

    info_records = [r for r in caplog.records if r.levelno == logging.INFO]
    assert any(
        "persona injected" in r.message and "mode=implicit" in r.message
        for r in info_records
    ), f"Expected implicit INFO log, got: {[r.message for r in caplog.records]}"


# ---------------------------------------------------------------------------
# 4. DEBUG log on decline — no coding intent
# ---------------------------------------------------------------------------


def test_debug_log_on_no_coding_intent(caplog):
    bridge.clear_active_persona()
    with caplog.at_level(logging.DEBUG, logger="matrix_coder"):
        result = plugin._inject_persona(user_message="an ordinary follow-up message")

    assert result is None
    debug_messages = [r.message for r in caplog.records if r.levelno == logging.DEBUG]
    assert any("no injection" in m for m in debug_messages), (
        f"Expected DEBUG no-injection log, got: {debug_messages}"
    )


# ---------------------------------------------------------------------------
# 5. DEBUG log on DIRECT verdict (persona not activated)
# ---------------------------------------------------------------------------


def test_debug_log_on_direct_verdict(caplog):
    bridge.clear_active_persona()
    with caplog.at_level(logging.DEBUG, logger="matrix_coder"):
        result = plugin._inject_persona(user_message="fix README typo")

    # DIRECT verdict: result is not None (recommendation text) but bridge inactive.
    assert result is not None
    assert bridge.is_active() is False
    debug_messages = [r.message for r in caplog.records if r.levelno == logging.DEBUG]
    assert any("no injection" in m for m in debug_messages), (
        f"Expected DEBUG direct-verdict log, got: {debug_messages}"
    )
