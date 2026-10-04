"""pre_llm_call target routing (restores fork 9d75ee0504, dropped in the 0.19 migration).

Bare strings / target-less dicts -> current user message. ``target`` in
("system", "developer") -> appended to the effective system prompt at API time only,
after the cached prompt (cache prefix untouched) and never into the user turn.
"""
from types import SimpleNamespace
from unittest.mock import patch

from agent.turn_context import _collect_pre_llm_call_context, build_api_messages


class _SendAgent(SimpleNamespace):
    api_mode = "chat_completions"
    ephemeral_system_prompt = None
    _compression_warning = None
    _current_turn_timestamp = 10_000.0
    _persist_disabled = False
    session_id = "s1"
    model = "test/model"
    platform = "cli"

    @staticmethod
    def _copy_reasoning_content_for_api(_source, _target):
        return None

    @staticmethod
    def _should_sanitize_tool_calls():
        return False


def test_trusted_target_lands_in_system_not_user_message():
    agent = _SendAgent()
    messages = [{"role": "user", "content": "hello"}]
    results = [
        {"context": "PERSONA OVERLAY", "target": "developer"},
        {"context": "SYS RULE", "target": "system"},
        {"context": "recall A"},
        "plain B",
        {"context": "explicit C", "target": "user_message"},
    ]
    with patch("hermes_cli.lifecycle.invoke_hook", return_value=results):
        user_ctx = _collect_pre_llm_call_context(
            agent, effective_task_id="t", turn_id="u", original_user_message="hello",
            messages=messages, conversation_history=[],
        )
    for s in ("recall A", "plain B", "explicit C"):
        assert s in user_ctx
    assert "PERSONA OVERLAY" not in user_ctx and "SYS RULE" not in user_ctx

    api_messages, effective_system = build_api_messages(
        agent, messages, current_turn_user_idx=0, ext_prefetch_cache="",
        plugin_user_context=user_ctx, moa_config=None, active_system_prompt="CACHED PROMPT",
    )
    assert api_messages[0]["role"] == "system"
    assert effective_system.startswith("CACHED PROMPT")  # cached prefix stays first
    assert "PERSONA OVERLAY" in effective_system and "SYS RULE" in effective_system
    user_wire = str(api_messages[-1]["content"])
    assert "PERSONA OVERLAY" not in user_wire and "recall A" in user_wire

    # Next turn with no trusted result: overlay must not linger.
    with patch("hermes_cli.lifecycle.invoke_hook", return_value=["plain only"]):
        _collect_pre_llm_call_context(
            agent, effective_task_id="t", turn_id="u2", original_user_message="hi",
            messages=messages, conversation_history=[{}],
        )
    assert agent._plugin_trusted_context == ""
