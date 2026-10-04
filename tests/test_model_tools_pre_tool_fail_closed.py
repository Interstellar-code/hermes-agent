"""pre_tool_call dispatch exceptions must block (fail closed) on secondary paths."""
import json
import model_tools
from agent.agent_runtime_helpers import _pre_tool_block_message


def _boom(*a, **k):
    raise RuntimeError("boom")


def test_pre_dispatch_guards_fail_closed(monkeypatch):
    monkeypatch.setattr("hermes_cli.plugins._dispatch_pre_tool_call_hooks", _boom)
    ids = model_tools._CallIds("t", "s", "c", None, None)
    _, blocked = model_tools._pre_dispatch_guards("terminal", {}, False, ids, [])
    assert blocked is not None
    assert blocked[1] == "plugin_block" and "RuntimeError" in blocked[2]


def test_handle_function_call_does_not_run_handler(monkeypatch):
    monkeypatch.setattr("hermes_cli.plugins._dispatch_pre_tool_call_hooks", _boom)
    called = []
    monkeypatch.setattr(model_tools, "_execute_tool", lambda *a, **k: called.append(1) or "{}")
    out = model_tools.handle_function_call("terminal", {"command": "true"})
    assert not called and "pre_tool_call dispatch raised" in out


def test_runtime_helper_fail_closed(monkeypatch):
    monkeypatch.setattr("hermes_cli.plugins._dispatch_pre_tool_call_hooks", _boom)
    msg, args = _pre_tool_block_message(object(), "terminal", {"a": 1}, "t", "c", [])
    assert msg and "RuntimeError" in msg and args == {"a": 1}
