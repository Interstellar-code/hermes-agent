"""Phase 1: HTTP-inbound /p/<profile>/ routing for the webhook adapter."""
import pytest

from gateway.config import GatewayConfig, Platform
from gateway.session import SessionSource, build_session_key


class TestSessionSourceProfileField:
    def test_profile_roundtrips(self):
        s = SessionSource(
            platform=Platform.WEBHOOK if hasattr(Platform, "WEBHOOK") else Platform.TELEGRAM,
            chat_id="c1",
            chat_type="webhook",
            profile="coder",
        )
        restored = SessionSource.from_dict(s.to_dict())
        assert restored.profile == "coder"


class TestWebhookProfileResolution:
    """_resolve_request_profile validates the /p/<profile>/ prefix."""

    def _adapter(self, multiplex: bool, served=("default", "coder")):
        from gateway.platforms.webhook import WebhookAdapter, _PROFILE_REJECTED

        class _FakeReq:
            def __init__(self, profile):
                self.match_info = {"profile": profile} if profile is not None else {}

        cfg = GatewayConfig(multiplex_profiles=multiplex)

        class _Runner:
            config = cfg

        # Construct minimally; we only call _resolve_request_profile.
        adapter = WebhookAdapter.__new__(WebhookAdapter)
        adapter.gateway_runner = _Runner()
        return adapter, _FakeReq, _PROFILE_REJECTED, served

    def test_no_prefix_returns_none(self):
        adapter, Req, _REJ, _ = self._adapter(multiplex=True)
        assert adapter._resolve_request_profile(Req(None)) is None

    def test_no_prefix_returns_none_when_multiplex_off(self):
        """Unprefixed webhooks are untouched in both modes."""
        adapter, Req, _REJ, _ = self._adapter(multiplex=False)
        assert adapter._resolve_request_profile(Req(None)) is None

    def test_foreign_prefix_rejected_when_multiplex_off(self, monkeypatch):
        """Fail closed: the /p/<profile>/ route is registered unconditionally, so silently
        ignoring the prefix would run the event through the default profile's home and persist
        the session there — serving one profile's routes under another profile's URL."""
        adapter, Req, rejected, _ = self._adapter(multiplex=False)
        monkeypatch.setattr("hermes_cli.profiles.profile_matches_home", lambda name: False)
        assert adapter._resolve_request_profile(Req("anything")) is rejected
        assert adapter._resolve_request_profile(Req("coder")) is rejected

    def test_self_referential_prefix_allowed_when_multiplex_off(self, monkeypatch):
        """A prefix naming this gateway's OWN profile falls through to the bare route."""
        adapter, Req, _REJ, _ = self._adapter(multiplex=False)
        monkeypatch.setattr(
            "hermes_cli.profiles.profile_matches_home", lambda name: name == "mine")
        assert adapter._resolve_request_profile(Req("mine")) is None

    def test_unserved_prefix_is_rejected(self, monkeypatch):
        adapter, Req, rejected, served = self._adapter(
            multiplex=True, served=("default", "worker"),
        )
        monkeypatch.setattr(
            "hermes_cli.profiles.profiles_to_serve",
            lambda multiplex: [(name, f"/profiles/{name}") for name in served],
        )

        assert adapter._resolve_request_profile(Req("worker")) == "worker"
        assert adapter._resolve_request_profile(Req("restricted")) is rejected


