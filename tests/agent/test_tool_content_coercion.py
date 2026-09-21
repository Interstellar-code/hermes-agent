"""A dict-typed tool result must never reach a chat-completions provider.

Once one is in history the provider 400s on EVERY subsequent turn, so the
conversation cannot drain itself back to a valid state -- the session is dead
until the history is edited. convert_messages coerces on the way out so an
already-poisoned session heals.

The subtle part is the return contract: _sanitize_message returns None for
"nothing to do", and convert_messages returns the ORIGINAL list when every
message returns None. A coercion that does not also mark the message as changed
is silently discarded, which is why test_coercion_survives_convert_messages
exists separately from the unit-level check.
"""

from __future__ import annotations

import pytest

from agent.transports.chat_completions import ChatCompletionsTransport


@pytest.fixture
def transport():
    return ChatCompletionsTransport()


def _tool_msg(content):
    return {"role": "tool", "tool_call_id": "call_1", "content": content}


@pytest.mark.parametrize("content", [
    {"ok": True, "rows": 3},
    {"nested": {"a": [1, 2]}},
    [1, 2, 3],          # list is already legal -- must pass through untouched
    "plain string",     # str is already legal
])
def test_only_illegal_types_are_coerced(transport, content):
    out = transport.convert_messages([_tool_msg(content)])[0]
    if isinstance(content, (str, list)):
        assert out["content"] == content
    else:
        assert isinstance(out["content"], str)
        assert "ok" in out["content"] or "nested" in out["content"]


def test_coercion_survives_convert_messages(transport):
    """The whole point: a lone dict tool result, nothing else to sanitize.

    If the coercion does not mark the message changed, convert_messages returns
    the original list and the dict reaches the provider anyway.
    """
    msgs = [_tool_msg({"rows": 3})]
    out = transport.convert_messages(msgs)
    assert isinstance(out[0]["content"], str), "dict leaked through convert_messages"


def test_caller_message_is_not_mutated(transport):
    original = _tool_msg({"rows": 3})
    transport.convert_messages([original])
    assert original["content"] == {"rows": 3}, "convert_messages mutated the caller's message"


def test_unserializable_content_falls_back_to_str(transport):
    class Weird:
        def __repr__(self): return "<weird>"
    out = transport.convert_messages([_tool_msg({"obj": Weird()})])[0]
    assert isinstance(out["content"], str)


def test_non_tool_roles_are_untouched(transport):
    """Only tool results are constrained; do not rewrite other roles."""
    msg = {"role": "user", "content": "hi"}
    assert transport.convert_messages([msg])[0]["content"] == "hi"


# ── build half: make_tool_result_message must not create the poison ──────────
# The transport fix heals history that already carries a dict; this stops one
# entering history in the first place. Both halves are required.

def test_build_path_coerces_dict_content():
    from agent.tool_dispatch_helpers import make_tool_result_message
    msg = make_tool_result_message("some_tool", {"rows": 3}, tool_call_id="call_1")
    assert isinstance(msg["content"], str), "dict tool result entered history uncoerced"


def test_build_path_leaves_legal_types_alone():
    from agent.tool_dispatch_helpers import make_tool_result_message
    msg = make_tool_result_message("some_tool", "plain", tool_call_id="call_1")
    assert "plain" in msg["content"]


def test_build_and_wire_halves_agree():
    """Both halves must produce the same wire shape for the same dict."""
    from agent.tool_dispatch_helpers import make_tool_result_message
    built = make_tool_result_message("t", {"rows": 3}, tool_call_id="c1")
    wired = ChatCompletionsTransport().convert_messages(
        [{"role": "tool", "tool_call_id": "c1", "content": {"rows": 3}}])[0]
    assert isinstance(built["content"], str) and isinstance(wired["content"], str)
    assert "rows" in built["content"] and "rows" in wired["content"]
