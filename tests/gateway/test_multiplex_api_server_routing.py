"""Multiplex /p/<profile>/ routing for the api_server adapter.

Mirrors ``test_multiplex_http_routing.py`` (webhook): the default listener
owns the port, and secondary profiles are reached via a URL prefix when
``gateway.multiplex_profiles`` is on.
"""
from __future__ import annotations

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from gateway.config import GatewayConfig, PlatformConfig
from gateway.platforms.api_server import (
    APIServerAdapter,
    _PROFILE_REJECTED,
    _api_request_profile,
)


def _make_adapter(multiplex: bool = True) -> APIServerAdapter:
    cfg = PlatformConfig(enabled=True, extra={"host": "127.0.0.1", "port": 8642, "key": "test-key"})
    adapter = APIServerAdapter(cfg)

    class _Runner:
        config = GatewayConfig(multiplex_profiles=multiplex)

    adapter.gateway_runner = _Runner()
    return adapter


class _FakeReq:
    def __init__(self, profile=None):
        self.match_info = {"profile": profile} if profile is not None else {}


class TestApiServerProfileResolution:
    def test_no_prefix_returns_none(self):
        adapter = _make_adapter(multiplex=True)
        assert adapter._resolve_request_profile(_FakeReq(None)) is None

    def test_no_prefix_returns_none_when_multiplex_off(self):
        """The hard requirement: unprefixed requests are untouched in BOTH
        modes. A single-profile deployment sees zero change from the strict
        prefix handling — only a request that explicitly supplies a prefix can
        start failing."""
        adapter = _make_adapter(multiplex=False)
        assert adapter._resolve_request_profile(_FakeReq(None)) is None

    def test_prefix_rejected_when_multiplex_off(self):
        """Fail closed. ``connect()`` registers the /p/<profile>/ mirrors
        unconditionally, so an ignored prefix would run the request through the
        ordinary handler scoped to the gateway's own home — silently writing a
        prefixed session into the wrong profile's state.db."""
        adapter = _make_adapter(multiplex=False)
        assert adapter._resolve_request_profile(_FakeReq("anything")) is _PROFILE_REJECTED
        # …including a profile that would be valid were multiplexing on.
        assert adapter._resolve_request_profile(_FakeReq("coder")) is _PROFILE_REJECTED

    def test_known_profile_accepted(self, monkeypatch):
        adapter = _make_adapter(multiplex=True)
        monkeypatch.setattr(
            "hermes_cli.profiles.profiles_to_serve",
            lambda multiplex: [("default", None), ("coder", None)],
        )
        assert adapter._resolve_request_profile(_FakeReq("coder")) == "coder"

    def test_unknown_profile_rejected(self, monkeypatch):
        adapter = _make_adapter(multiplex=True)
        monkeypatch.setattr(
            "hermes_cli.profiles.profiles_to_serve",
            lambda multiplex: [("default", None), ("coder", None)],
        )
        assert adapter._resolve_request_profile(_FakeReq("ghost")) is _PROFILE_REJECTED


class TestApiServerRouteTable:
    def test_route_table_includes_models_and_chat(self):
        """ /p/{profile}/v1/models must be registered — this is the 404 Fadeway hit. """
        adapter = _make_adapter(multiplex=True)
        paths = {path for _method, path, _handler in adapter._http_route_table()}
        assert "/v1/models" in paths
        assert "/v1/chat/completions" in paths
        # connect() mirrors every native path under /p/{profile}/…
        mirrored = {f"/p/{{profile}}{path}" for path in paths}
        assert "/p/{profile}/v1/models" in mirrored
        assert "/p/{profile}/v1/chat/completions" in mirrored


class TestPrefixedWriteScoping:
    """End-to-end through the real profile-prefix middleware.

    The handler stands in for any write endpoint: it persists to whatever
    ``HERMES_HOME`` the request ends up scoped to and records it. That makes
    "the write landed in the wrong profile" directly observable instead of
    inferred — and proves the 404 path never reaches a handler at all.
    """

    @staticmethod
    def _app(adapter, sink):
        async def _write(request):
            from hermes_constants import get_hermes_home

            home = get_hermes_home()
            home.mkdir(parents=True, exist_ok=True)
            (home / "state.db").write_text("written", encoding="utf-8")
            sink.append(home)
            return web.json_response({"home": str(home)})

        app = web.Application(middlewares=[adapter._make_profile_prefix_middleware()])
        app.router.add_route("POST", "/api/sessions", _write)
        app.router.add_route("POST", "/p/{profile}/api/sessions", _write)
        return app

    @staticmethod
    def _homes(tmp_path, monkeypatch):
        """Gateway process home + two profile homes, none of them seeded."""
        gateway_home = tmp_path / "gateway-home"
        gateway_home.mkdir()
        profiles = tmp_path / "profiles"
        for name in ("coder", "ops"):
            (profiles / name).mkdir(parents=True)
        monkeypatch.setenv("HERMES_HOME", str(gateway_home))
        monkeypatch.setattr(
            "hermes_cli.profiles.get_profile_dir", lambda name: profiles / name
        )
        monkeypatch.setattr(
            "hermes_cli.profiles.profiles_to_serve",
            lambda multiplex: [("default", None), ("coder", None), ("ops", None)],
        )
        return gateway_home, profiles

    @staticmethod
    def _state_dbs(tmp_path):
        return sorted(p for p in tmp_path.rglob("state.db"))

    async def _post(self, adapter, sink, path):
        async with TestClient(TestServer(self._app(adapter, sink))) as client:
            resp = await client.post(path)
            return resp.status

    @pytest.mark.asyncio
    async def test_prefixed_write_404s_and_persists_nothing_when_multiplex_off(
        self, tmp_path, monkeypatch
    ):
        gateway_home, _profiles = self._homes(tmp_path, monkeypatch)
        adapter = _make_adapter(multiplex=False)
        sink = []

        assert await self._post(adapter, sink, "/p/coder/api/sessions") == 404
        # The handler never ran, so nothing was persisted anywhere — not into
        # the named profile, and (the actual bug) not into the gateway's own
        # home either.
        assert sink == []
        assert self._state_dbs(tmp_path) == []
        assert not (gateway_home / "state.db").exists()

    @pytest.mark.asyncio
    async def test_prefixed_write_scopes_to_that_profile_when_multiplex_on(
        self, tmp_path, monkeypatch
    ):
        gateway_home, profiles = self._homes(tmp_path, monkeypatch)
        adapter = _make_adapter(multiplex=True)
        sink = []

        assert await self._post(adapter, sink, "/p/coder/api/sessions") == 200
        assert sink == [profiles / "coder"]
        assert (profiles / "coder" / "state.db").exists()
        assert not (gateway_home / "state.db").exists()
        assert not (profiles / "ops" / "state.db").exists()

    @pytest.mark.asyncio
    async def test_unknown_prefix_404s_when_multiplex_on(self, tmp_path, monkeypatch):
        self._homes(tmp_path, monkeypatch)
        adapter = _make_adapter(multiplex=True)
        sink = []

        assert await self._post(adapter, sink, "/p/ghost/api/sessions") == 404
        assert sink == []
        assert self._state_dbs(tmp_path) == []

    @pytest.mark.asyncio
    async def test_unprefixed_write_unchanged_multiplex_off(self, tmp_path, monkeypatch):
        gateway_home, _profiles = self._homes(tmp_path, monkeypatch)
        adapter = _make_adapter(multiplex=False)
        sink = []

        assert await self._post(adapter, sink, "/api/sessions") == 200
        assert sink == [gateway_home]
        assert (gateway_home / "state.db").exists()

    @pytest.mark.asyncio
    async def test_unprefixed_write_unchanged_multiplex_on(self, tmp_path, monkeypatch):
        """No prefix under multiplexing still scopes to the gateway's own
        process home — the default profile owns the port."""
        gateway_home, profiles = self._homes(tmp_path, monkeypatch)
        adapter = _make_adapter(multiplex=True)
        sink = []

        assert await self._post(adapter, sink, "/api/sessions") == 200
        assert sink == [gateway_home]
        assert (gateway_home / "state.db").exists()
        assert not (profiles / "coder" / "state.db").exists()


class TestInteractionStateIsProfileKeyed:
    """``_clarify_streams`` / ``_session_interactions`` are process-global.

    Under multiplexing two profiles can legitimately hold the same session id,
    so raw-session-id keys alias: one profile's live clarify stream would be
    overwritten (or cleaned up) by the other's.
    """

    @staticmethod
    def _interaction(session_id):
        return {
            "interaction_id": "clarify_1",
            "clarify_id": "clarify_1",
            "kind": "text",
            "tool_name": "clarify",
            "session_id": session_id,
        }

    def test_same_session_id_in_two_profiles_does_not_alias(self):
        adapter = _make_adapter(multiplex=True)
        session_id = "sess_shared"
        seen = {"coder": [], "ops": []}

        # Both profiles open a live clarify stream on the SAME session id.
        for profile in ("coder", "ops"):
            adapter._clarify_streams[(profile, session_id)] = (
                lambda name, payload, _p=profile: seen[_p].append(name)
            )
            adapter._session_interactions[(profile, "clarify_1")] = self._interaction(
                session_id
            )

        assert len(adapter._clarify_streams) == 2
        assert len(adapter._session_interactions) == 2

        # Resolving under 'coder' must touch only coder's state.
        token = _api_request_profile.set("coder")
        try:
            scope = _api_request_profile.get()
            adapter._session_interactions.pop((scope, "clarify_1"), None)
            adapter._clarify_streams[(scope, session_id)]("clarify.responded", {})
        finally:
            _api_request_profile.reset(token)

        assert seen == {"coder": ["clarify.responded"], "ops": []}
        assert (("ops", "clarify_1")) in adapter._session_interactions
        assert (("coder", "clarify_1")) not in adapter._session_interactions
        assert (("ops", session_id)) in adapter._clarify_streams

    def test_stream_teardown_only_clears_its_own_profile(self):
        """The stream handler's ``finally`` sweeps interactions by session id;
        profile-keying stops it reaping the other profile's pending clarify."""
        adapter = _make_adapter(multiplex=True)
        session_id = "sess_shared"
        for profile in ("coder", "ops"):
            adapter._clarify_streams[(profile, session_id)] = lambda *_a, **_k: None
            adapter._session_interactions[(profile, "clarify_1")] = self._interaction(
                session_id
            )

        stream_profile = "coder"  # what the handler captured at request time
        adapter._clarify_streams.pop((stream_profile, session_id), None)
        for key, meta in list(adapter._session_interactions.items()):
            if key[0] == stream_profile and meta.get("session_id") == session_id:
                adapter._session_interactions.pop(key, None)

        assert list(adapter._clarify_streams) == [("ops", session_id)]
        assert list(adapter._session_interactions) == [("ops", "clarify_1")]


class TestApiServerModelsUnderProfile:
    def test_resolve_model_name_follows_active_profile(self, monkeypatch):
        """When the request is scoped to a named profile, advertise that name."""
        adapter = _make_adapter(multiplex=True)
        adapter._model_name = "hermes-agent"
        monkeypatch.setattr(
            "hermes_cli.profiles.get_active_profile_name",
            lambda: "coder",
        )
        token_prof = _api_request_profile.set("coder")
        try:
            assert adapter._resolve_model_name("") == "coder"
        finally:
            _api_request_profile.reset(token_prof)
