"""The fallback-provider api_mode hint must be validated like the primary path.

Root cause of the 2026-09-21 ``'NoneType' object has no attribute 'build_kwargs'``
crash: the hermes-switch profile's fallback entry carries
``api_mode: openai_compatible``, which is not a registered transport.
``_fallback_api_mode_hint`` passed the raw string through as "explicit", fallback
activation assigned it to ``agent.api_mode``, and ``get_transport`` returned None.
"""

from agent.chat_completion_helpers import _fallback_api_mode_hint

LAN = "http://192.168.0.56:38238/v1"


def _hint(api_mode, provider="custom", base_url=LAN):
    return _fallback_api_mode_hint({"provider": provider, "base_url": base_url, "api_mode": api_mode}, provider, base_url)


def test_unregistered_api_mode_is_rejected_not_passed_through():
    explicit, mode = _hint("openai_compatible")
    assert mode == "chat_completions"
    assert explicit is False  # auto-detection, not a trusted explicit value


def test_legacy_alias_is_canonicalized_and_stays_explicit():
    assert _hint("openai") == (True, "chat_completions")


def test_valid_explicit_api_mode_is_kept():
    assert _hint("anthropic_messages") == (True, "anthropic_messages")


def test_absent_api_mode_auto_detects():
    assert _hint(None) == (False, "chat_completions")
