"""Shared fixtures for the mcp_lazy test suite.

The plugin stashes the active agent in a module-level ContextVar
(``plugins.mcp_lazy.pool._current_agent_var``). Tests that exercise the
"agent present" path set it; without an explicit reset the value leaks into
later test files running in the same process, breaking the "no agent in
context" assertions (handler/pre_tool_call should pass through). Reset it
around every test so the suite is order-independent.
"""

import pytest

from plugins.mcp_lazy.pool import _current_agent_var


@pytest.fixture(autouse=True)
def _reset_current_agent_var():
    _current_agent_var.set(None)
    try:
        yield
    finally:
        _current_agent_var.set(None)
