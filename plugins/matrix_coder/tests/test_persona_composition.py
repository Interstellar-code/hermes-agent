"""Tests for persona composition (``core.prompts.compose_persona``) + loaders.

Verifies that the real Phase 1 personas and lenses load and compose so that the
injected text carries both the role persona and (for a lensed review) the lens
text, alongside the shared ``_base`` contracts.
"""

from __future__ import annotations

import sys
from pathlib import Path

_PLUGIN_DIR = Path(__file__).resolve().parent.parent
if str(_PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_DIR))

from core import registry  # noqa: E402
from core.prompts import compose_persona  # noqa: E402


def test_compose_review_security_includes_persona_and_lens():
    base = registry.load_base_contracts()
    persona = registry.load_persona("review")
    lens = registry.load_lens("security")

    composed = compose_persona(base, persona, lens=lens)

    # Base contract present.
    assert "Specialist Contract" in composed
    # Review persona present.
    assert "Review Specialist" in composed
    # Security lens TEXT (not just a marker) present.
    assert "Review Lens: Security" in composed
    assert "authorization" in composed.lower()
    # Lens section marker emitted.
    assert "# LENS" in composed


def test_compose_review_code_lens_text_present():
    base = registry.load_base_contracts()
    persona = registry.load_persona("review")
    lens = registry.load_lens("code")

    composed = compose_persona(base, persona, lens=lens)
    assert "Review Lens: Code" in composed
    assert "maintainability" in composed.lower()


def test_compose_executor_persona_present_no_lens():
    base = registry.load_base_contracts()
    persona = registry.load_persona("executor")

    composed = compose_persona(base, persona)
    assert "Executor Specialist" in composed
    # No lens supplied -> no LENS section.
    assert "# LENS" not in composed


def test_loaders_return_nonempty_for_phase1_assets():
    assert registry.load_persona("review").strip()
    assert registry.load_persona("executor").strip()
    assert registry.load_lens("security").strip()
    assert registry.load_lens("code").strip()


# -- Phase 1.5: the six new role personas -----------------------------------

# role name -> a distinctive title string from its persona.
_NEW_PERSONAS = {
    "explore": "Explore Specialist",
    "plan": "Plan Specialist",
    "debug": "Debug Specialist",
    "test": "Test Specialist",
    "verify": "Verify Specialist",
    "simplify": "Simplify Specialist",
}


def test_new_personas_load_nonempty_and_compose():
    base = registry.load_base_contracts()
    for name, title in _NEW_PERSONAS.items():
        persona = registry.load_persona(name)
        assert persona.strip(), name
        composed = compose_persona(base, persona)
        assert "Specialist Contract" in composed, name
        assert title in composed, name
        # No lens supplied -> no LENS section.
        assert "# LENS" not in composed, name


# -- Phase 1.5: the four new review lenses ----------------------------------

# lens name -> a distinctive title string from its lens text.
_NEW_LENSES = {
    "api": "Review Lens: API",
    "performance": "Review Lens: Performance",
    "quality": "Review Lens: Quality",
    "deps": "Review Lens: Dependencies",
}


def test_review_with_each_new_lens_includes_lens_text():
    base = registry.load_base_contracts()
    persona = registry.load_persona("review")
    for name, title in _NEW_LENSES.items():
        lens = registry.load_lens(name)
        assert lens.strip(), name
        composed = compose_persona(base, persona, lens=lens)
        assert "Review Specialist" in composed, name
        assert title in composed, name
        assert "# LENS" in composed, name
