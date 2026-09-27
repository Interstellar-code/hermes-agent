"""A session with a stored model must keep its NAMED custom provider's credentials.

Live incident 2026-09-27: a SwitchUI chat whose session row stored ``model="auto"`` failed
every turn with OpenRouter ``401 Missing Authentication header`` while new chats worked.
The profile's primary provider is a named custom provider (``providers.manifest``, LAN
base_url, own key). Its resolved runtime carries ``provider="custom"`` +
``requested_provider="manifest"``. The session-persisted-model branch re-resolved by the
canonical ``"custom"``, which has no base_url/key of its own and fell through to the generic
``OPENAI_API_KEY`` + the OpenRouter default URL -- sending an unrelated key to OpenRouter.
"""
from unittest.mock import patch

from gateway.config import PlatformConfig
from gateway.platforms import api_server as mod
from gateway.platforms.api_server import APIServerAdapter

NAMED = {"provider": "custom", "requested_provider": "manifest",
         "base_url": "http://lan-proxy:38238/v1", "api_key": "mnfst-key", "api_mode": "chat_completions"}
BARE_CUSTOM = {"provider": "custom", "base_url": "https://openrouter.ai/api/v1",
               "api_key": "generic-openai-env-key", "api_mode": "chat_completions"}


def _fake_resolve(provider, target_model=None):
    return dict(NAMED) if provider == "manifest" else dict(BARE_CUSTOM)


def _select(runtime):
    adapter = APIServerAdapter(PlatformConfig(enabled=True, extra={}))
    with patch.object(mod, "_resolve_request_runtime_agent_kwargs", side_effect=_fake_resolve), \
         patch.object(APIServerAdapter, "_session_model_override_for", return_value=None), \
         patch.object(APIServerAdapter, "_rehydrated_model_override", return_value=None):
        return adapter._select_agent_runtime(
            runtime, "auto", requested_model=None, requested_provider=None, route=None,
            session_model="auto", confirmed_runtime_lock=False,
            gateway_session_key=None, session_id="sess-1")


def test_session_persisted_model_keeps_named_custom_provider_credentials():
    runtime = dict(NAMED)
    _select(runtime)
    assert runtime["base_url"] == "http://lan-proxy:38238/v1"
    assert runtime["api_key"] == "mnfst-key"
    assert "openrouter.ai" not in runtime["base_url"]


def test_builtin_provider_without_requested_name_is_unchanged():
    # No requested_provider (a plain built-in provider): re-resolution still uses `provider`.
    seen = []

    def _rec(provider, target_model=None):
        seen.append(provider)
        return {"provider": provider, "base_url": "https://api.example/v1", "api_key": "k"}

    runtime = {"provider": "openrouter", "base_url": "https://openrouter.ai/api/v1", "api_key": "k"}
    adapter = APIServerAdapter(PlatformConfig(enabled=True, extra={}))
    with patch.object(mod, "_resolve_request_runtime_agent_kwargs", side_effect=_rec), \
         patch.object(APIServerAdapter, "_session_model_override_for", return_value=None), \
         patch.object(APIServerAdapter, "_rehydrated_model_override", return_value=None):
        adapter._select_agent_runtime(
            runtime, "m", requested_model=None, requested_provider=None, route=None,
            session_model="m", confirmed_runtime_lock=False, gateway_session_key=None, session_id="s")
    assert seen == ["openrouter"]
