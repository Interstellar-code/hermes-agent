"""Tests for the observer slot in agent/usage_pricing.py.

The slot lets plugins (e.g. mcp_lazy baseline logger) attach to the
canonical usage pipeline without core importing any plugin. We verify:
  - registration order is preserved
  - observers see the canonical usage record
  - observer exceptions don't break callers
  - unregister actually removes the callback
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from agent import usage_pricing
from agent.usage_pricing import (
    CanonicalUsage,
    normalize_usage,
    register_usage_observer,
    unregister_usage_observer,
)


@pytest.fixture(autouse=True)
def _clean_slot():
    """Reset the observer list around each test so they're isolated."""
    original = list(usage_pricing._usage_observers)
    usage_pricing._usage_observers.clear()
    yield
    usage_pricing._usage_observers.clear()
    usage_pricing._usage_observers.extend(original)


def _fake_anthropic_usage(read=100, write=50, input_=200):
    return SimpleNamespace(
        input_tokens=input_,
        output_tokens=10,
        cache_read_input_tokens=read,
        cache_creation_input_tokens=write,
    )


def test_register_appends_observer():
    cb = lambda u: None  # noqa: E731
    register_usage_observer(cb)
    assert usage_pricing._usage_observers == [cb]


def test_observer_receives_canonical_usage():
    captured: list[CanonicalUsage] = []
    register_usage_observer(captured.append)

    normalize_usage(_fake_anthropic_usage(), provider="anthropic")

    assert len(captured) == 1
    u = captured[0]
    assert u.cache_read_tokens == 100
    assert u.cache_write_tokens == 50
    assert u.input_tokens == 200


def test_observer_exception_does_not_break_normalize():
    def explode(_):
        raise RuntimeError("observer is buggy")

    register_usage_observer(explode)
    # Must still return a usable CanonicalUsage despite the observer raising.
    result = normalize_usage(_fake_anthropic_usage(), provider="anthropic")
    assert result.cache_read_tokens == 100


def test_multiple_observers_all_called_in_order():
    order: list[str] = []
    register_usage_observer(lambda u: order.append("a"))
    register_usage_observer(lambda u: order.append("b"))
    register_usage_observer(lambda u: order.append("c"))

    normalize_usage(_fake_anthropic_usage(), provider="anthropic")

    assert order == ["a", "b", "c"]


def test_unregister_removes_callback():
    captured: list = []
    cb = captured.append
    register_usage_observer(cb)
    unregister_usage_observer(cb)

    normalize_usage(_fake_anthropic_usage(), provider="anthropic")

    assert captured == []


def test_unregister_missing_callback_is_noop():
    # Should not raise.
    unregister_usage_observer(lambda u: None)


def test_no_observers_no_overhead():
    # With no observers registered the function still works normally.
    result = normalize_usage(_fake_anthropic_usage(), provider="anthropic")
    assert result.cache_read_tokens == 100
    assert result.input_tokens == 200


def test_observer_skipped_for_empty_usage():
    # The early-return on falsy usage must not invoke observers.
    captured: list = []
    register_usage_observer(captured.append)
    normalize_usage(None, provider="anthropic")
    assert captured == []
