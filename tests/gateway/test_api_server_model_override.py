"""Tests for per-session ``model`` overrides on the API server adapter.

Covers the HTTP switch surface (``POST /api/sessions/{id}/chat`` and
``.../chat/stream`` reading ``body["model"]``), where the resolved override is
stored (GatewayRunner map vs adapter-local map, keyed by session key or session
id), that ``_create_agent`` actually builds the agent on it, its precedence over
``model_routes``, the secret-free write-through to the ``sessions`` row, the
restart rehydration path, and the 400s for unusable ``model`` values.

``hermes_cli.model_switch.switch_model`` is patched in every test that can reach
it — the real one performs live HTTP (models.dev catalog + a ``/v1/models``
probe).
"""

import json
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from gateway.config import PlatformConfig
from gateway.platforms.api_server import APIServerAdapter
from hermes_state import SessionDB

_MOD = "gateway.platforms.api_server"

API_KEY = "sk-test"
AUTH = {"Authorization": f"Bearer {API_KEY}"}
SESSION_ID = "sess-model-override"
SESSION_KEY = "chat-key-42"

SWITCHED_MODEL = "zai/glm-4.6"
SWITCHED_PROVIDER = "openrouter"
SWITCHED_BASE_URL = "https://openrouter.ai/api/v1"
SWITCHED_API_KEY = "sk-upstream-secret-value"
SWITCHED_API_MODE = "chat_completions"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def session_db(tmp_path):
    db = SessionDB(tmp_path / "state.db")
    db.create_session(SESSION_ID, "api_server")
    try:
        yield db
    finally:
        close = getattr(db, "close", None)
        if callable(close):
            close()


def _make_adapter(session_db, **extra) -> APIServerAdapter:
    """Adapter wired to *session_db* (setting _session_db skips lazy init)."""
    adapter = APIServerAdapter(PlatformConfig(enabled=True, extra={"key": API_KEY, **extra}))
    adapter._session_db = session_db
    return adapter


@pytest.fixture
def adapter(session_db):
    return _make_adapter(session_db)


def _create_session_app(adapter: APIServerAdapter) -> web.Application:
    app = web.Application()
    app.router.add_post("/api/sessions/{session_id}/chat", adapter._handle_session_chat)
    app.router.add_post(
        "/api/sessions/{session_id}/chat/stream", adapter._handle_session_chat_stream
    )
    return app


def _switch_result(
    model: str = SWITCHED_MODEL,
    *,
    provider: str = SWITCHED_PROVIDER,
    api_key: str = SWITCHED_API_KEY,
    base_url: str = SWITCHED_BASE_URL,
    api_mode: str = SWITCHED_API_MODE,
) -> SimpleNamespace:
    """A successful ``switch_model`` result shape."""
    return SimpleNamespace(
        success=True,
        new_model=model,
        target_provider=provider,
        api_key=api_key,
        base_url=base_url,
        api_mode=api_mode,
        error_message=None,
    )


def _failed_switch_result(message: str = "Unknown model: nope") -> SimpleNamespace:
    return SimpleNamespace(
        success=False,
        new_model="",
        target_provider="",
        api_key="",
        base_url="",
        api_mode="",
        error_message=message,
    )


@contextmanager
def _patched_switch(result=None):
    """Patch the model resolver at its use site plus the config read.

    ``_resolve_model_switch`` does ``from hermes_cli.model_switch import
    switch_model`` *inside* the method, so the module attribute is the seam.
    """
    mock = MagicMock(return_value=result if result is not None else _switch_result())
    with (
        patch("hermes_cli.model_switch.switch_model", mock),
        patch("gateway.run._load_gateway_config", return_value={}),
    ):
        yield mock


def _stub_run_agent(adapter: APIServerAdapter) -> AsyncMock:
    mock = AsyncMock(
        return_value=({"final_response": "ok", "session_id": SESSION_ID}, {"total_tokens": 3})
    )
    adapter._run_agent = mock
    return mock


@contextmanager
def _observed_agent(runtime_kwargs=None):
    """Patch out everything ``_create_agent`` touches and expose AIAgent."""
    base = {
        "api_key": "config-key",
        "base_url": "https://config.example/v1",
        "provider": "config-provider",
        "api_mode": None,
        "command": None,
        "args": [],
    }
    if runtime_kwargs:
        base.update(runtime_kwargs)
    with (
        patch("gateway.run._resolve_runtime_agent_kwargs", return_value=dict(base)),
        patch("gateway.run._resolve_gateway_model", return_value="config/default-model"),
        patch("gateway.run._load_gateway_config", return_value={}),
        patch("run_agent.AIAgent") as mock_agent_cls,
    ):
        mock_agent_cls.return_value = MagicMock()
        yield mock_agent_cls


def _chat_body(model=None, message="hello"):
    body = {"message": message}
    if model is not None:
        body["model"] = model
    return body


def _raw_model_config(session_db, session_id=SESSION_ID):
    row = session_db.get_session(session_id)
    assert row is not None
    return row.get("model_config")


async def _post_chat(cli, path, body, headers=None):
    hdrs = dict(AUTH)
    if headers:
        hdrs.update(headers)
    return await cli.post(f"/api/sessions/{SESSION_ID}{path}", json=body, headers=hdrs)


# ---------------------------------------------------------------------------
# 1. Where the override is stored
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["/chat", "/chat/stream"])
async def test_override_stored_under_session_key_when_header_present(adapter, path):
    """X-Hermes-Session-Key present -> the runner map is keyed by that key."""
    runner = SimpleNamespace(_session_model_overrides={})
    _stub_run_agent(adapter)
    app = _create_session_app(adapter)

    with _patched_switch() as mock_switch, patch(
        "gateway.run._gateway_runner_ref", lambda: runner
    ):
        async with TestClient(TestServer(app)) as cli:
            resp = await _post_chat(
                cli, path, _chat_body(SWITCHED_MODEL), {"X-Hermes-Session-Key": SESSION_KEY}
            )
            assert resp.status == 200

    assert mock_switch.call_count == 1
    assert set(runner._session_model_overrides) == {SESSION_KEY}
    override = runner._session_model_overrides[SESSION_KEY]
    assert override["model"] == SWITCHED_MODEL
    assert override["provider"] == SWITCHED_PROVIDER
    assert override["api_key"] == SWITCHED_API_KEY
    assert override["base_url"] == SWITCHED_BASE_URL
    assert override["api_mode"] == SWITCHED_API_MODE
    # The adapter-local map stays empty while a runner owns the process.
    assert adapter._local_session_model_overrides == {}


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["/chat", "/chat/stream"])
async def test_override_stored_under_session_id_without_header(adapter, path):
    """No session-key header -> the override is keyed by the session id."""
    runner = SimpleNamespace(_session_model_overrides={})
    _stub_run_agent(adapter)
    app = _create_session_app(adapter)

    with _patched_switch(), patch("gateway.run._gateway_runner_ref", lambda: runner):
        async with TestClient(TestServer(app)) as cli:
            resp = await _post_chat(cli, path, _chat_body(SWITCHED_MODEL))
            assert resp.status == 200

    assert set(runner._session_model_overrides) == {SESSION_ID}
    assert runner._session_model_overrides[SESSION_ID]["model"] == SWITCHED_MODEL


@pytest.mark.asyncio
async def test_override_falls_back_to_adapter_local_map_without_runner(adapter):
    """Standalone api_server (no GatewayRunner) keeps the switch on the adapter."""
    _stub_run_agent(adapter)
    app = _create_session_app(adapter)

    with _patched_switch(), patch("gateway.run._gateway_runner_ref", lambda: None):
        async with TestClient(TestServer(app)) as cli:
            resp = await _post_chat(
                cli, "/chat", _chat_body(SWITCHED_MODEL), {"X-Hermes-Session-Key": SESSION_KEY}
            )
            assert resp.status == 200

    assert set(adapter._local_session_model_overrides) == {SESSION_KEY}
    assert adapter._local_session_model_overrides[SESSION_KEY]["model"] == SWITCHED_MODEL


# ---------------------------------------------------------------------------
# 2. The next turn's agent actually runs on it
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_next_turn_agent_is_built_on_the_switched_model(adapter):
    """After an HTTP switch, _create_agent hands the new model to AIAgent."""
    runner = SimpleNamespace(_session_model_overrides={})
    _stub_run_agent(adapter)
    app = _create_session_app(adapter)

    with _patched_switch(), patch("gateway.run._gateway_runner_ref", lambda: runner):
        async with TestClient(TestServer(app)) as cli:
            resp = await _post_chat(
                cli, "/chat", _chat_body(SWITCHED_MODEL), {"X-Hermes-Session-Key": SESSION_KEY}
            )
            assert resp.status == 200
            assert (await resp.json())["model"] == SWITCHED_MODEL

        # Next turn: no ``model`` field at all — the switch is sticky.
        with _observed_agent() as mock_agent_cls:
            adapter._create_agent(session_id=SESSION_ID, gateway_session_key=SESSION_KEY)

    kwargs = mock_agent_cls.call_args.kwargs
    assert kwargs["model"] == SWITCHED_MODEL
    assert kwargs["provider"] == SWITCHED_PROVIDER
    assert kwargs["api_key"] == SWITCHED_API_KEY
    assert kwargs["base_url"] == SWITCHED_BASE_URL
    assert kwargs["api_mode"] == SWITCHED_API_MODE


def test_create_agent_honours_a_preseeded_runner_override(adapter):
    """Regression: an override installed by another transport must still apply.

    Simulates an adapter-issued ``/model`` on a shared session key — the entry
    lands in ``GatewayRunner._session_model_overrides`` with no HTTP ``model``
    field involved.  Before the fix the override was consulted only as a veto
    on ``model_routes``, so the agent was silently rebuilt on the config model.
    """
    runner = SimpleNamespace(
        _session_model_overrides={
            SESSION_KEY: {
                "model": SWITCHED_MODEL,
                "provider": SWITCHED_PROVIDER,
                "api_key": SWITCHED_API_KEY,
                "base_url": SWITCHED_BASE_URL,
                "api_mode": SWITCHED_API_MODE,
            }
        }
    )

    with patch("gateway.run._gateway_runner_ref", lambda: runner):
        with _observed_agent() as mock_agent_cls:
            adapter._create_agent(session_id=SESSION_ID, gateway_session_key=SESSION_KEY)

    kwargs = mock_agent_cls.call_args.kwargs
    assert kwargs["model"] == SWITCHED_MODEL
    assert kwargs["provider"] == SWITCHED_PROVIDER
    assert kwargs["api_key"] == SWITCHED_API_KEY
    assert kwargs["base_url"] == SWITCHED_BASE_URL


def test_create_agent_skips_none_valued_override_fields(adapter):
    """A credential-less override must not clobber the resolved runtime auth."""
    runner = SimpleNamespace(
        _session_model_overrides={
            SESSION_ID: {
                "model": SWITCHED_MODEL,
                "provider": None,
                "api_key": None,
                "base_url": None,
            }
        }
    )

    with patch("gateway.run._gateway_runner_ref", lambda: runner):
        with _observed_agent() as mock_agent_cls:
            adapter._create_agent(session_id=SESSION_ID)

    kwargs = mock_agent_cls.call_args.kwargs
    assert kwargs["model"] == SWITCHED_MODEL
    assert kwargs["api_key"] == "config-key"
    assert kwargs["provider"] == "config-provider"


# ---------------------------------------------------------------------------
# 3. Precedence over model_routes
# ---------------------------------------------------------------------------


ROUTE_ALIAS = "fast-alias"
ROUTE_CONFIG = {
    ROUTE_ALIAS: {
        "model": "route/route-model",
        "provider": "route-provider",
        "api_key": "sk-route-key",
        "base_url": "https://route.example/v1",
    }
}


def test_model_route_applies_without_a_session_override(session_db):
    """Baseline: with no session override the route wins over global config."""
    adapter = _make_adapter(session_db, model_routes=ROUTE_CONFIG)
    route = adapter._resolve_route(ROUTE_ALIAS)
    assert route is not None

    with (
        patch("gateway.run._gateway_runner_ref", lambda: None),
        patch(
            "gateway.run._resolve_runtime_agent_kwargs_for_provider",
            return_value={"provider": "route-provider", "api_key": "sk-route-key"},
        ),
    ):
        with _observed_agent() as mock_agent_cls:
            adapter._create_agent(session_id=SESSION_ID, route=route)

    assert mock_agent_cls.call_args.kwargs["model"] == "route/route-model"


def test_session_override_beats_a_matching_model_route(session_db):
    """A user-issued /model outranks static per-client model_routes config."""
    adapter = _make_adapter(session_db, model_routes=ROUTE_CONFIG)
    route = adapter._resolve_route(ROUTE_ALIAS)
    runner = SimpleNamespace(
        _session_model_overrides={
            SESSION_KEY: {
                "model": SWITCHED_MODEL,
                "provider": SWITCHED_PROVIDER,
                "api_key": SWITCHED_API_KEY,
                "base_url": SWITCHED_BASE_URL,
                "api_mode": SWITCHED_API_MODE,
            }
        }
    )

    with patch("gateway.run._gateway_runner_ref", lambda: runner):
        with _observed_agent() as mock_agent_cls:
            adapter._create_agent(
                session_id=SESSION_ID, gateway_session_key=SESSION_KEY, route=route
            )

    kwargs = mock_agent_cls.call_args.kwargs
    assert kwargs["model"] == SWITCHED_MODEL
    # None of the route's provider credentials leaked in.
    assert kwargs["provider"] == SWITCHED_PROVIDER
    assert kwargs["api_key"] == SWITCHED_API_KEY
    assert kwargs["base_url"] == SWITCHED_BASE_URL


# ---------------------------------------------------------------------------
# 4. Survives a restart (write-through + rehydration)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_override_survives_restart_via_rehydration(session_db):
    """A fresh adapter over the same SessionDB rebuilds the switched model."""
    adapter = _make_adapter(session_db)
    _stub_run_agent(adapter)
    app = _create_session_app(adapter)

    with _patched_switch(), patch("gateway.run._gateway_runner_ref", lambda: None):
        async with TestClient(TestServer(app)) as cli:
            resp = await _post_chat(cli, "/chat", _chat_body(SWITCHED_MODEL))
            assert resp.status == 200

    # Restart: brand-new adapter, empty in-memory maps, no runner.
    fresh = _make_adapter(session_db)
    assert fresh._local_session_model_overrides == {}

    with (
        patch("gateway.run._gateway_runner_ref", lambda: None),
        patch(
            "gateway.run._resolve_runtime_agent_kwargs_for_provider",
            return_value={
                "api_key": "re-resolved-key",
                "api_mode": SWITCHED_API_MODE,
                "credential_pool": None,
                "base_url": SWITCHED_BASE_URL,
            },
        ) as mock_resolve,
    ):
        with _observed_agent() as mock_agent_cls:
            fresh._create_agent(session_id=SESSION_ID)

    kwargs = mock_agent_cls.call_args.kwargs
    assert kwargs["model"] == SWITCHED_MODEL
    assert kwargs["provider"] == SWITCHED_PROVIDER
    assert kwargs["base_url"] == SWITCHED_BASE_URL
    # Credentials are re-resolved through the provider chain, never read off disk.
    # (Resolved twice: once by _rehydrated_model_override, once by
    # _credential_pool_for_provider, which delegates to the same helper.)
    mock_resolve.assert_any_call(SWITCHED_PROVIDER)
    assert {c.args for c in mock_resolve.call_args_list} == {(SWITCHED_PROVIDER,)}
    assert kwargs["api_key"] == "re-resolved-key"
    # Rehydration is paid once: the override is reinstalled in memory.
    assert fresh._local_session_model_overrides[SESSION_ID]["model"] == SWITCHED_MODEL


# ---------------------------------------------------------------------------
# 5. No secrets on disk
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_persisted_override_contains_no_api_key(adapter, session_db):
    """Only model/provider/base_url reach the sessions row."""
    _stub_run_agent(adapter)
    app = _create_session_app(adapter)

    with _patched_switch(), patch("gateway.run._gateway_runner_ref", lambda: None):
        async with TestClient(TestServer(app)) as cli:
            resp = await _post_chat(cli, "/chat", _chat_body(SWITCHED_MODEL))
            assert resp.status == 200

    raw = _raw_model_config(session_db)
    assert raw, "model override was not written through to the sessions row"
    assert "api_key" not in raw
    assert SWITCHED_API_KEY not in raw

    persisted = json.loads(raw)["_model_override"]
    assert persisted == {
        "model": SWITCHED_MODEL,
        "provider": SWITCHED_PROVIDER,
        "base_url": SWITCHED_BASE_URL,
    }
    assert set(persisted) <= {"model", "provider", "base_url"}
    # The denormalized ``model`` column follows the switch too.
    assert session_db.get_session(SESSION_ID)["model"] == SWITCHED_MODEL


# ---------------------------------------------------------------------------
# 6. Unknown model -> 400, nothing installed, nothing persisted
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["/chat", "/chat/stream"])
async def test_unknown_model_is_a_400_with_no_silent_fallback(adapter, session_db, path):
    runner = SimpleNamespace(_session_model_overrides={})
    run_agent = _stub_run_agent(adapter)
    app = _create_session_app(adapter)

    with (
        _patched_switch(_failed_switch_result("Unknown model: not-a-real-model")),
        patch("gateway.run._gateway_runner_ref", lambda: runner),
    ):
        async with TestClient(TestServer(app)) as cli:
            resp = await _post_chat(cli, path, _chat_body("not-a-real-model"))
            assert resp.status == 400
            payload = await resp.json()

    assert payload["error"]["code"] == "model_not_available"
    assert payload["error"]["param"] == "model"
    assert "not-a-real-model" in payload["error"]["message"]
    # No silent fallback to the default model: the turn never ran.
    run_agent.assert_not_awaited()
    assert runner._session_model_overrides == {}
    assert adapter._local_session_model_overrides == {}
    raw = _raw_model_config(session_db)
    assert not raw or "_model_override" not in json.loads(raw)


@pytest.mark.asyncio
async def test_resolver_exception_is_a_400_not_a_500(adapter):
    """switch_model raising (network blip) must not fall back or 500."""
    runner = SimpleNamespace(_session_model_overrides={})
    _stub_run_agent(adapter)
    app = _create_session_app(adapter)

    mock = MagicMock(side_effect=RuntimeError("models.dev unreachable"))
    with (
        patch("hermes_cli.model_switch.switch_model", mock),
        patch("gateway.run._load_gateway_config", return_value={}),
        patch("gateway.run._gateway_runner_ref", lambda: runner),
    ):
        async with TestClient(TestServer(app)) as cli:
            resp = await _post_chat(cli, "/chat", _chat_body("some-model"))
            assert resp.status == 400

    assert runner._session_model_overrides == {}


# ---------------------------------------------------------------------------
# 7. Re-sending the active model is free
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_resending_the_active_model_does_not_reinvoke_switch_model(adapter):
    """switch_model can block on network I/O; a no-op switch must skip it."""
    runner = SimpleNamespace(_session_model_overrides={})
    _stub_run_agent(adapter)
    app = _create_session_app(adapter)

    with _patched_switch() as mock_switch, patch(
        "gateway.run._gateway_runner_ref", lambda: runner
    ):
        async with TestClient(TestServer(app)) as cli:
            first = await _post_chat(
                cli, "/chat", _chat_body(SWITCHED_MODEL), {"X-Hermes-Session-Key": SESSION_KEY}
            )
            assert first.status == 200
            assert mock_switch.call_count == 1

            mock_switch.reset_mock()
            second = await _post_chat(
                cli, "/chat", _chat_body(SWITCHED_MODEL), {"X-Hermes-Session-Key": SESSION_KEY}
            )
            assert second.status == 200
            assert mock_switch.call_count == 0
            assert (await second.json())["model"] == SWITCHED_MODEL

        # Still installed, and still the effective model.
        assert runner._session_model_overrides[SESSION_KEY]["model"] == SWITCHED_MODEL
        assert adapter._effective_model_name(SESSION_KEY) == SWITCHED_MODEL


@pytest.mark.asyncio
async def test_resending_an_unresolved_alias_does_not_reinvoke_switch_model(adapter):
    """The guard must survive alias resolution.

    ``switch_model`` resolves aliases and aggregator slugs, so the stored
    override holds the resolved id while a UI keeps sending the alias it was
    given. Comparing only against the resolved id would miss on every message
    and re-run the blocking resolver for the entire life of the session.
    """
    alias, resolved = "glm-4.6", "zai/glm-4.6"
    runner = SimpleNamespace(_session_model_overrides={})
    _stub_run_agent(adapter)
    app = _create_session_app(adapter)

    with _patched_switch(_switch_result(resolved)) as mock_switch, patch(
        "gateway.run._gateway_runner_ref", lambda: runner
    ):
        async with TestClient(TestServer(app)) as cli:
            first = await _post_chat(
                cli, "/chat", _chat_body(alias), {"X-Hermes-Session-Key": SESSION_KEY}
            )
            assert first.status == 200
            assert mock_switch.call_count == 1

            mock_switch.reset_mock()
            for _ in range(3):
                again = await _post_chat(
                    cli, "/chat", _chat_body(alias), {"X-Hermes-Session-Key": SESSION_KEY}
                )
                assert again.status == 200
            assert mock_switch.call_count == 0
            # The resolved id short-circuits too, not just the alias.
            assert (
                await _post_chat(
                    cli, "/chat", _chat_body(resolved), {"X-Hermes-Session-Key": SESSION_KEY}
                )
            ).status == 200
            assert mock_switch.call_count == 0

    assert runner._session_model_overrides[SESSION_KEY]["model"] == resolved


@pytest.mark.asyncio
async def test_switching_to_a_different_model_does_reinvoke_switch_model(adapter):
    runner = SimpleNamespace(_session_model_overrides={})
    _stub_run_agent(adapter)
    app = _create_session_app(adapter)

    with _patched_switch() as mock_switch, patch(
        "gateway.run._gateway_runner_ref", lambda: runner
    ):
        async with TestClient(TestServer(app)) as cli:
            assert (await _post_chat(cli, "/chat", _chat_body(SWITCHED_MODEL))).status == 200
            mock_switch.reset_mock()
            mock_switch.return_value = _switch_result("anthropic/claude-x")
            assert (await _post_chat(cli, "/chat", _chat_body("claude-x"))).status == 200

    assert mock_switch.call_count == 1
    assert runner._session_model_overrides[SESSION_ID]["model"] == "anthropic/claude-x"


# ---------------------------------------------------------------------------
# 8. Malformed ``model`` values
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("bad_model", [123, 4.5, True, [], {}, "", "   "])
async def test_non_string_or_empty_model_is_a_400(adapter, session_db, bad_model):
    runner = SimpleNamespace(_session_model_overrides={})
    run_agent = _stub_run_agent(adapter)
    app = _create_session_app(adapter)

    with _patched_switch() as mock_switch, patch(
        "gateway.run._gateway_runner_ref", lambda: runner
    ):
        async with TestClient(TestServer(app)) as cli:
            resp = await _post_chat(cli, "/chat", _chat_body(bad_model))
            assert resp.status == 400
            payload = await resp.json()

    assert payload["error"]["code"] == "invalid_model"
    assert payload["error"]["param"] == "model"
    # Rejected before any (expensive) resolution and before the turn runs.
    assert mock_switch.call_count == 0
    run_agent.assert_not_awaited()
    assert runner._session_model_overrides == {}
    raw = _raw_model_config(session_db)
    assert not raw or "_model_override" not in json.loads(raw)


@pytest.mark.asyncio
async def test_absent_model_field_leaves_the_session_untouched(adapter):
    """No ``model`` key at all is not an error and installs nothing."""
    runner = SimpleNamespace(_session_model_overrides={})
    _stub_run_agent(adapter)
    app = _create_session_app(adapter)

    with _patched_switch() as mock_switch, patch(
        "gateway.run._gateway_runner_ref", lambda: runner
    ):
        async with TestClient(TestServer(app)) as cli:
            resp = await _post_chat(cli, "/chat", _chat_body())
            assert resp.status == 200

    assert mock_switch.call_count == 0
    assert runner._session_model_overrides == {}
