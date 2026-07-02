"""Tests for the leak-proof per-turn persona lifecycle (Phase 1).

A persona must be active ONLY on the turn whose user message carried the
trigger. These tests exercise:

1. A trigger message -> ``harness.handle_trigger`` activates and returns the
   composed persona text (containing the role + lens content).
2. The ``pre_llm_call`` hook (``__init__._inject_persona``) returns the text for
   a trigger message and clears + returns ``None`` for a non-trigger message —
   no leak across turns.
3. The ``post_llm_call`` hook (``__init__._clear_persona``) clears the active
   persona as a backstop.
"""

from __future__ import annotations

import sys
from pathlib import Path

# Put the parent of the plugin dir on the path so ``matrix_coder`` imports as a
# package — its relative imports (``from .core import ...``) then resolve to the
# SAME modules these tests reach through the package, so the shared ``bridge``
# instance is identical (a top-level ``import core`` would create a *second*
# bridge and break the shared-state assertions).
_PLUGIN_DIR = Path(__file__).resolve().parent.parent
if str(_PLUGIN_DIR.parent) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_DIR.parent))

import matrix_coder as plugin  # noqa: E402
from matrix_coder.core import harness  # noqa: E402
from matrix_coder.core.hermes_bridge import bridge  # noqa: E402


def test_handle_trigger_activates_and_returns_composed():
    bridge.clear_active_persona()
    composed = harness.handle_trigger("matrix review security: check auth")

    assert composed is not None
    assert bridge.is_active() is True
    assert bridge.inject_persona_text() == composed
    # Role persona + security lens content are both present.
    assert "Review Specialist" in composed
    assert "Review Lens: Security" in composed


def test_handle_trigger_no_trigger_returns_none():
    bridge.clear_active_persona()
    assert harness.handle_trigger("just a normal message") is None


def test_pre_llm_call_hook_injects_on_trigger():
    bridge.clear_active_persona()
    injected = plugin._inject_persona(user_message="matrix executor add export")

    # Hook now returns {"context": <str>, "target": "developer"} — trusted tier.
    assert injected is not None
    assert isinstance(injected, dict)
    assert "Executor Specialist" in injected["context"]
    assert injected["target"] == "developer"
    assert bridge.is_active() is True


def test_pre_llm_call_hook_implicitly_routes_security_review():
    bridge.clear_active_persona()
    # Strong-signal implicit security review (advisory "is this auth safe?" now
    # quiets under the #140 policy; an explicit-role lead still routes).
    injected = plugin._inject_persona(
        user_message="review the auth login flow for security"
    )

    assert injected is not None
    assert isinstance(injected, dict)
    assert "Review Specialist" in injected["context"]
    assert "Review Lens: Security" in injected["context"]
    assert injected["target"] == "developer"
    assert bridge.is_active() is True


def test_pre_llm_call_hook_explicit_trigger_overrides_inference():
    bridge.clear_active_persona()
    injected = plugin._inject_persona(
        user_message="matrix executor: is this auth safe?"
    )

    assert injected is not None
    assert isinstance(injected, dict)
    assert "Executor Specialist" in injected["context"]
    assert "Review Specialist" not in injected["context"]
    assert injected["target"] == "developer"
    assert bridge.is_active() is True


def test_pre_llm_call_hook_direct_recommendation_does_not_activate_persona():
    bridge.clear_active_persona()
    injected = plugin._inject_persona(user_message="fix README typo")

    assert injected is not None
    assert isinstance(injected, dict)
    assert "<verdict>direct</verdict>" in injected["context"]
    assert injected["target"] == "developer"
    assert bridge.is_active() is False


def test_pre_llm_call_hook_clears_on_non_trigger():
    # Simulate a stale active persona from a prior turn; a non-trigger turn must
    # clear it and return None (no leak forward).
    bridge.set_active_persona("=== STALE PERSONA ===")
    result = plugin._inject_persona(user_message="an ordinary follow-up")

    assert result is None
    assert bridge.is_active() is False


def test_post_llm_call_hook_clears_backstop():
    bridge.set_active_persona("=== ACTIVE ===")
    assert bridge.is_active() is True

    result = plugin._clear_persona()
    assert result is None
    assert bridge.is_active() is False
