"""Walking-skeleton tests for the matrix_coder core modules.

These import the ``core`` package directly (the plugin dir is on the path via
conftest-less sys.path manipulation below) to keep the test independent of the
Hermes loader. They cover:

1. ``harness.run_passthrough`` returns a SpecialistResult whose rendered
   markdown contains the goal.
2. The ``pre_llm_call`` persona injection returns the composed persona while a
   dispatch is active and ``None`` once cleared.
3. The hermes_bridge file-claim bookkeeping: claim / query / conflict / release.
"""

from __future__ import annotations

import sys
from pathlib import Path

_PLUGIN_DIR = Path(__file__).resolve().parent.parent
if str(_PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_DIR))

from core import harness, reporting  # noqa: E402
from core.hermes_bridge import bridge  # noqa: E402
from core.models import SpecialistResult  # noqa: E402


def test_run_passthrough_markdown_contains_goal():
    bridge.clear_active_persona()
    goal = "hello"
    result = harness.run_passthrough(goal)

    assert isinstance(result, SpecialistResult)
    assert result.role == "_passthrough"

    md = reporting.render_markdown(result)
    assert goal in md
    # Output contract sections present.
    assert "## Findings" in md
    assert "## Open Questions" in md
    assert "## Positive Observations" in md
    assert "## Recommendation" in md


def test_run_passthrough_does_not_leak_active_persona():
    # A completed dispatch must deactivate so the pre_llm_call hook no-ops on
    # subsequent ordinary turns. Guards against the persona-state leak.
    bridge.clear_active_persona()
    harness.run_passthrough("no leak please")
    assert bridge.is_active() is False
    assert bridge.inject_persona_text() is None


def test_pre_llm_call_injection_active_then_cleared():
    # Injection is exercised at the bridge level: while a persona is active the
    # hook returns it; once cleared it returns None.
    bridge.clear_active_persona()
    assert bridge.inject_persona_text() is None

    bridge.set_active_persona("=== PERSONA ===\nbe excellent")
    injected = bridge.inject_persona_text()
    assert injected is not None
    assert "PERSONA" in injected  # composed persona has the section marker

    bridge.clear_active_persona()
    assert bridge.inject_persona_text() is None


def test_file_claim_bookkeeping():
    bridge.release_files()
    assert bridge.claimed_files() == set()

    bridge.claim_files(["/tmp/a.py", "/tmp/b.py"])
    claimed = bridge.claimed_files()
    assert "/tmp/a.py" in claimed
    assert "/tmp/b.py" in claimed

    assert bridge.would_conflict("/tmp/a.py") is True
    assert bridge.would_conflict("/tmp/c.py") is False

    bridge.release_files()
    assert bridge.claimed_files() == set()
    assert bridge.would_conflict("/tmp/a.py") is False
