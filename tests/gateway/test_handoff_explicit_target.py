"""FORK-ONLY (switchui): handoff into an EXISTING chat/topic via ``sessions.handoff_target``."""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from hermes_state import SessionDB
from gateway.config import HomeChannel, Platform
from gateway.session import build_session_key
from tests.gateway.test_telegram_topic_mode import _make_runner, _make_source

CHAT = "208214988"


def _runner(tmp_path, session_db=None):
    runner = _make_runner(session_db=session_db)
    runner.config.platforms[Platform.TELEGRAM].home_channel = HomeChannel(
        platform=Platform.TELEGRAM, chat_id=CHAT, name="Tester DM")
    adapter = runner.adapters[Platform.TELEGRAM]
    adapter.create_handoff_thread = AsyncMock(return_value="99999")
    adapter.send.return_value = SimpleNamespace(success=True)
    captured = {}

    async def fake_handle_message(event):
        captured["event"] = event
        return "handoff ok"

    runner._handle_message = AsyncMock(side_effect=fake_handle_message)
    return runner, adapter, captured


def _row(target, source="api_server"):
    return {"id": "web-session", "title": "Web work", "handoff_platform": "telegram",
            "handoff_target": target, "source": source}


@pytest.mark.asyncio
async def test_existing_dm_topic_reuses_thread_and_inbound_key(tmp_path):
    runner, adapter, captured = _runner(tmp_path)
    await runner._process_handoff(_row(f"telegram:{CHAT}:27865"))
    adapter.create_handoff_thread.assert_not_called()
    expected_key = build_session_key(_make_source(thread_id="27865"))
    assert expected_key == f"agent:main:telegram:dm:{CHAT}:27865"
    runner.session_store.switch_session.assert_called_once_with(expected_key, "web-session")
    src = captured["event"].source
    assert (src.chat_id, src.thread_id, src.chat_type, src.user_id) == (CHAT, "27865", "dm", CHAT)
    assert "from the web UI" in captured["event"].text
    call = adapter.send.await_args
    assert CHAT in call.args and {"thread_id": "27865"} in (*call.args, *call.kwargs.values())


@pytest.mark.asyncio
async def test_unknown_chat_is_refused(tmp_path):
    runner, adapter, _ = _runner(tmp_path)
    with patch("gateway.channel_directory.DIRECTORY_PATH", tmp_path / "none.json"):
        with pytest.raises(RuntimeError, match="not a known telegram chat"):
            await runner._process_handoff(_row("telegram:555:1"))
    runner.session_store.switch_session.assert_not_called()


@pytest.mark.asyncio
async def test_platform_mismatch_is_refused(tmp_path):
    runner, _, _ = _runner(tmp_path)
    with pytest.raises(RuntimeError, match="invalid handoff target"):
        await runner._process_handoff(_row("discord:1:2"))


@pytest.mark.asyncio
async def test_directory_chat_allowed_and_chat_only_target_creates_thread(tmp_path):
    runner, adapter, captured = _runner(tmp_path)
    directory = tmp_path / "channel_directory.json"
    directory.write_text(json.dumps({"platforms": {"telegram": [{"id": "777:12", "type": "dm"}]}}))
    with patch("gateway.channel_directory.DIRECTORY_PATH", directory):
        await runner._process_handoff(_row("telegram:777"))
    adapter.create_handoff_thread.assert_awaited_once_with("777", "Hermes — Web work")
    src = captured["event"].source
    assert (src.chat_id, src.thread_id, src.user_id) == ("777", "99999", "777")


@pytest.mark.asyncio
async def test_topic_mode_binding_is_repointed_to_handed_off_session(tmp_path):
    db = SessionDB(db_path=tmp_path / "state.db")
    db.enable_telegram_topic_mode(chat_id=CHAT, user_id=CHAT)
    old_key = build_session_key(_make_source(thread_id="27865"))
    db.create_session("old-session", "telegram")
    db.create_session("web-session", "api_server")
    db.bind_telegram_topic(chat_id=CHAT, thread_id="27865", user_id=CHAT, session_key=old_key,
                           session_id="old-session")
    runner, _, _ = _runner(tmp_path, session_db=db)
    await runner._process_handoff(_row(f"telegram:{CHAT}:27865", source="cli"))
    binding = db.get_telegram_topic_binding(chat_id=CHAT, thread_id="27865")
    assert binding["session_id"] == "web-session"


def test_state_target_column_set_and_cleared(tmp_path):
    db = SessionDB(db_path=tmp_path / "state.db")
    db.create_session("s1", "api_server")
    assert db.request_handoff_status("s1", "telegram", f"telegram:{CHAT}:1") == "queued"
    assert db.list_pending_handoffs()[0]["handoff_target"] == f"telegram:{CHAT}:1"
    db.complete_handoff("s1")
    assert db.get_session("s1")["handoff_target"] is None
    db.request_handoff_status("s1", "telegram", f"telegram:{CHAT}:2")
    db.fail_handoff("s1", "boom")
    assert db.get_session("s1")["handoff_target"] is None
    db.request_handoff_status("s1", "telegram")
    assert db.get_session("s1")["handoff_target"] is None


@pytest.mark.parametrize("target,ok", [(f"telegram:{CHAT}:27865", True), ("discord:1:2", False)])
def test_rpc_handoff_request_passes_validated_target(monkeypatch, target, ok):
    import contextlib

    from gateway.config import GatewayConfig, PlatformConfig
    from tui_gateway import methods_session, server

    methods_session.register(server)
    seen = {}

    def load_config():
        config = GatewayConfig()
        config.platforms[Platform.TELEGRAM] = PlatformConfig(enabled=True, home_channel=HomeChannel(
            platform=Platform.TELEGRAM, chat_id=CHAT, name="DM"))
        return config

    class DB:
        def get_session(self, key):
            return {"id": key}

        def expire_stale_handoffs(self):
            return []

        def request_handoff_status(self, key, platform, target=None):
            seen["target"] = target
            return "queued"

    @contextlib.contextmanager
    def db(_session):
        yield DB()

    monkeypatch.setattr("gateway.config.load_gateway_config", load_config)
    monkeypatch.setattr(server, "_ensure_session_db_row", lambda _session: None)
    monkeypatch.setattr(server, "_session_db", db)
    server._sessions["handoff-target"] = {"running": False, "session_key": "web-session"}
    try:
        resp = server.handle_request({"id": "1", "method": "handoff.request", "params": {
            "session_id": "handoff-target", "platform": "telegram", "target": target}})
    finally:
        server._sessions.pop("handoff-target", None)
    if ok:
        assert resp["result"]["target"] == target and seen["target"] == target
    else:
        assert resp["error"]["code"] == 4038 and "target" not in seen
