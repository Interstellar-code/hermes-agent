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
