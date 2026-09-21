"""Three fork fixes whose call sites moved during upstream's decomposition.

Each lived in a file the fork edited (conversation_loop.py, anthropic_adapter.py)
that upstream since split apart. The fix did not travel with the code, so each
was silently absent at the new location while the file it came from looked
"already ported".
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest


# --- #150: memory-context must not leak through a non-streamed final response ---

def test_final_response_is_scrubbed_of_memory_context():
    """The streaming path has StreamingContextScrubber; a non-streamed response
    never passes through it, so a model echoing its injected context would leak
    the whole block verbatim."""
    import inspect
    from agent import turn_final_response
    src = inspect.getsource(turn_final_response)
    assert "sanitize_context(assistant_message.content" in src, \
        "final_response is assigned without scrubbing injected context (#150)"


def test_sanitize_context_actually_removes_the_span():
    from agent.memory_manager import sanitize_context
    leaked = "Sure. <memory-context>PRIVATE RECALL</memory-context> Done."
    assert "PRIVATE RECALL" not in sanitize_context(leaked)


# --- deferred tools called by bare name must recover, not dead-end ---

def test_invalid_name_path_consults_deferred_recovery():
    """Under Tool Search a deferred tool is absent from the visible tools array,
    so a legitimate call by bare name looks identical to a hallucination. The
    validator must offer the schema before falling back to the generic error."""
    import inspect
    from agent import turn_tool_validation
    src = inspect.getsource(turn_tool_validation)
    assert "deferred_tool_recovery_message(agent, tc.function.name)" in src
    # …and the generic error must remain the fallback, not be replaced.
    assert "_invalid_tool_name_error_content(tc.function.name, valid_names)" in src


def test_deferred_recovery_helper_is_importable_with_expected_signature():
    """py_compile cannot catch a wrong import path or arity."""
    import inspect
    from agent.tool_executor import deferred_tool_recovery_message
    params = list(inspect.signature(deferred_tool_recovery_message).parameters)
    assert params == ["agent", "name"]


# --- keychain: NOT ported, upstream solves it in the harness ---

def test_keychain_is_neutralized_by_the_test_harness():
    """The fork guarded this in PRODUCTION by detecting a patched Path.home().

    Upstream instead neutralizes it in tests/conftest.py via the autouse
    _neutralize_macos_keychain_creds fixture, which stubs the reader outright
    unless a test opts in with @pytest.mark.allow_macos_keychain. That is
    strictly better: unconditional rather than a heuristic that infers "a test is
    running" from Path.home() disagreeing with expanduser("~"), and impossible to
    fool. Same shape as the pytest log-isolation guard, also dropped.

    Pinned here so the fork's version is not "re-discovered" and re-added to
    production code by a later pass.
    """
    import inspect
    from agent import anthropic_credentials as ac
    src = inspect.getsource(ac._read_claude_code_credentials_from_keychain)
    assert "unittest.mock" not in src, \
        "test-detection heuristic leaked back into production credential code"
    # The harness stub is what actually protects the developer's keychain.
    assert ac._read_claude_code_credentials_from_keychain() is None
