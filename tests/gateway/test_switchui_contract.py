"""Contract test: pretend to be the hermes-switchui client.

Every assertion here mirrors a read the SwitchUI client actually performs, so a
gateway change that stops sending something the client depends on fails HERE
instead of silently in production. Motivating incident: an upstream upgrade
silently dropped ``goal.status``/``goal.continuation`` emission, dropped the
``clarify`` tool, and ignored ``reasoning_effort``/``model`` request fields —
with zero test failure.

Client sources of truth (read-only, never edited by this test):
  - hermes-switchui/src/routes/api/send-stream.ts   (SSE event consumer)
  - hermes-switchui/src/server/gateway-capabilities.ts (capabilities probe)
  - hermes-switchui/src/stores/goal-progress-store.ts  (goal event consumer)

EVENT_CONTRACT below is the table: event name -> required payload fields,
each entry cites the client file:line that reads it. A field missing from a
payload the server actually emits is exactly the failure mode this guards.

Excluded on purpose (client defines a handler/case for these, but grep of
every ``events.enqueue(...)``/``queue.put(_event_payload(...))`` call in
``_handle_session_chat_stream`` turns up no server-side emitter for them —
they are dead client-side branches, not a gateway feature to test):
  - tool.pending      (client has a case for it; server only emits
                        tool.started/tool.progress/tool.completed/tool.failed)
  - artifact.created   (no artifact subsystem wired into the session stream)
  - memory.updated     (no memory-write event on this stream)
  - skill.loaded       (no skill-loading event on this stream)

Harness reused verbatim (fixtures/app-factory/stub pattern) from
tests/gateway/test_session_api.py and tests/gateway/test_api_server_model_override.py;
per this repo's convention (every test file here defines its own local copies
rather than cross-importing), the small helpers are duplicated locally.
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

SESSION_ID = "switchui-contract-session"
AUTH = {}

# event -> (required fields, citation)
EVENT_CONTRACT = {
    # send-stream.ts reads payload.message.id to seed the assistant bubble id.
    "message.started": ({"message"}, "send-stream.ts: onMessageStarted reads message.id"),
    # send-stream.ts appends payload.delta keyed by payload.message_id.
    "assistant.delta": ({"message_id", "delta"}, "send-stream.ts: onAssistantDelta"),
    # send-stream.ts closes the bubble; needs to know which run/session finished.
    "assistant.completed": ({"session_id", "completed"}, "send-stream.ts: onAssistantCompleted"),
    # send-stream.ts reads usage to update token/context UI.
    "run.completed": ({"usage"}, "send-stream.ts: onRunCompleted reads usage"),
    # goal-progress-store.ts renders turns_used/max_turns/status/message per turn.
    "goal.status": (
        {"message_id", "turns_used", "max_turns", "status", "message"},
        "goal-progress-store.ts: applyGoalStatus",
    ),
    # goal-progress-store.ts starts a new continuation turn from these fields.
    "goal.continuation": (
        {"message_id", "turn", "turns_used", "max_turns", "status", "continuation_prompt"},
        "goal-progress-store.ts: applyGoalContinuation",
    ),
    # send-stream.ts renders live tool-call chips.
    "tool.started": ({"message_id", "tool_name", "preview", "args"}, "send-stream.ts: onToolStarted"),
    "tool.completed": ({"message_id", "tool_name", "preview", "args"}, "send-stream.ts: onToolCompleted"),
    # reasoning.available is server-side; on the wire it is tool.progress with
    # tool_name="_thinking" and a `delta` (not `preview`) field.
    "tool.progress": ({"message_id", "tool_name", "delta"}, "send-stream.ts: onToolProgress / reasoning panel"),
    # send-stream.ts surfaces context_percent in the context-usage meter.
    "usage.update": (
        {"context_percent", "compacted", "messages_before", "messages_after"},
        "send-stream.ts: onUsageUpdate",
    ),
    # send-stream.ts shows a toast with error.message.
    "error": ({"message"}, "send-stream.ts: onError"),
}


# ---------------------------------------------------------------------------
# Harness (mirrors test_session_api.py / test_api_server_model_override.py)
# ---------------------------------------------------------------------------


@pytest.fixture
def session_db(tmp_path):
    db = SessionDB(tmp_path / "state.db")
    try:
        yield db
    finally:
        close = getattr(db, "close", None)
        if callable(close):
            close()


@pytest.fixture
def adapter(session_db):
    ad = APIServerAdapter(PlatformConfig(enabled=True))
    ad._session_db = session_db
    return ad


def _create_session_app(adapter: APIServerAdapter) -> web.Application:
    app = web.Application()
    app.router.add_get("/v1/capabilities", adapter._handle_capabilities)
    app.router.add_post("/api/sessions/{session_id}/chat", adapter._handle_session_chat)
    app.router.add_post("/api/sessions/{session_id}/chat/stream", adapter._handle_session_chat_stream)
    return app


def _sse_events(body: str):
    events = []
    for block in body.split("\n\n"):
        name, payload = None, None
        for line in block.splitlines():
            if line.startswith("event: "):
                name = line[len("event: "):]
            elif line.startswith("data: "):
                payload = json.loads(line[len("data: "):])
        if name is not None:
            events.append((name, payload or {}))
    return events


def _goal_manager_stub(decisions, *, turns_used=1, max_turns=20):
    state = SimpleNamespace(turns_used=turns_used, max_turns=max_turns)
    pending = list(decisions)

    class _Stub:
        def __init__(self, session_id, **kwargs):
            self.session_id = session_id
            self.state = state

        def is_active(self):
            return bool(pending)

        def evaluate_after_turn(self, last_response, **kwargs):
            return pending.pop(0) if len(pending) > 1 else pending[0]

    return _Stub


def _failed_switch_result(message):
    return SimpleNamespace(
        success=False, new_model="", target_provider="", api_key="", base_url="",
        api_mode="", error_message=message,
    )


def _ok_switch_result(model="switched/model"):
    return SimpleNamespace(
        success=True, new_model=model, target_provider="switched-provider",
        api_key="key", base_url="https://switched.example/v1", api_mode=None,
        error_message=None,
    )


@contextmanager
def _patched_switch(result):
    mock = MagicMock(return_value=result)
    with (
        patch("hermes_cli.model_switch.switch_model", mock),
        patch("gateway.run._load_gateway_config", return_value={}),
    ):
        yield mock


async def _post(cli, session_id, path, body):
    return await cli.post(f"/api/sessions/{session_id}{path}", json=body, headers=AUTH)


# ---------------------------------------------------------------------------
# 1-2-7: lifecycle + usage.update + tool/reasoning events, table-driven
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stream_emits_full_event_contract(adapter, session_db):
    session_id = session_db.create_session("contract-full", "api_server")

    class _FakeCompressor:
        last_prompt_tokens = 4000
        context_length = 8000

    fake_agent = SimpleNamespace(context_compressor=_FakeCompressor())

    async def fake_run(**kwargs):
        kwargs["stream_delta_callback"]("hi")
        kwargs["tool_progress_callback"]("tool.started", tool_name="grep", preview="searching", args={"q": "x"})
        kwargs["tool_progress_callback"]("reasoning.available", preview="thinking...")
        kwargs["tool_progress_callback"]("tool.completed", tool_name="grep", preview="found", args={"q": "x"})
        kwargs["usage_callback"](fake_agent, False, 1, 2)
        return {"final_response": "hi", "session_id": session_id}, {"total_tokens": 3}

    app = _create_session_app(adapter)
    with patch.object(adapter, "_run_agent", side_effect=fake_run):
        async with TestClient(TestServer(app)) as cli:
            resp = await _post(cli, session_id, "/chat/stream", {"message": "hello"})
            assert resp.status == 200
            body = await resp.text()

    events = _sse_events(body)
    names = [n for n, _ in events]
    for required in ("run.started", "message.started", "assistant.delta", "assistant.completed",
                     "run.completed", "done", "usage.update", "tool.started", "tool.completed",
                     "tool.progress"):
        assert required in names, f"missing {required}"

    by_name = {}
    for name, payload in events:
        by_name.setdefault(name, []).append(payload)

    for event_name, (fields, citation) in EVENT_CONTRACT.items():
        if event_name not in by_name:
            continue
        for payload in by_name[event_name]:
            missing = fields - payload.keys()
            assert not missing, f"{event_name} missing {missing} required by {citation}"

    # reasoning.available must translate to tool.progress/_thinking with `delta`.
    thinking = [p for p in by_name["tool.progress"] if p["tool_name"] == "_thinking"]
    assert thinking and thinking[0]["delta"] == "thinking..."


# ---------------------------------------------------------------------------
# 3: /goal — goal.status + goal.continuation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_goal_continuation_emits_status_and_continuation_events(adapter, session_db):
    session_id = session_db.create_session("contract-goal", "api_server")
    stub = _goal_manager_stub([
        {"status": "active", "should_continue": True, "continuation_prompt": "keep going",
         "verdict": "continue", "reason": "not done", "message": "still working"},
        {"status": "done", "should_continue": False, "continuation_prompt": None,
         "verdict": "done", "reason": "met", "message": "goal achieved"},
    ])
    runs = []

    async def fake_run(**kwargs):
        runs.append(kwargs["user_message"])
        return {"final_response": f"answer-{len(runs)}", "session_id": session_id}, {"total_tokens": 1}

    app = _create_session_app(adapter)
    with patch.object(adapter, "_run_agent", side_effect=fake_run), \
            patch("hermes_cli.goals.GoalManager", stub):
        async with TestClient(TestServer(app)) as cli:
            resp = await _post(cli, session_id, "/chat/stream", {"message": "start"})
            assert resp.status == 200
            body = await resp.text()

    events = _sse_events(body)
    statuses = [p for n, p in events if n == "goal.status"]
    conts = [p for n, p in events if n == "goal.continuation"]
    assert len(statuses) == 2
    assert len(conts) == 1
    for p in statuses:
        assert EVENT_CONTRACT["goal.status"][0] <= p.keys()
    for p in conts:
        assert EVENT_CONTRACT["goal.continuation"][0] <= p.keys()
    assert conts[0]["continuation_prompt"] == "keep going"


# ---------------------------------------------------------------------------
# 4: reasoning_effort validation + model override / unknown model
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_bad_reasoning_effort_is_400_before_any_sse(adapter, session_db):
    session_id = session_db.create_session("contract-effort", "api_server")
    app = _create_session_app(adapter)
    async with TestClient(TestServer(app)) as cli:
        resp = await _post(cli, session_id, "/chat/stream", {"message": "hi", "reasoning_effort": "nonsense"})
        assert resp.status == 400
        payload = await resp.json()
    assert payload["error"]["code"] == "invalid_reasoning_effort"


@pytest.mark.asyncio
async def test_unknown_model_is_400_model_not_available(adapter, session_db):
    session_id = session_db.create_session("contract-model", "api_server")
    runner = SimpleNamespace(_session_model_overrides={})
    run_agent = AsyncMock()
    adapter._run_agent = run_agent
    app = _create_session_app(adapter)
    with _patched_switch(_failed_switch_result("Unknown model: nope")), \
            patch("gateway.run._gateway_runner_ref", lambda: runner):
        async with TestClient(TestServer(app)) as cli:
            resp = await _post(cli, session_id, "/chat/stream", {"message": "hi", "model": "nope"})
            assert resp.status == 400
            payload = await resp.json()
    assert payload["error"]["code"] == "model_not_available"
    assert payload["error"]["param"] == "model"
    run_agent.assert_not_awaited()


@pytest.mark.asyncio
async def test_known_model_installs_session_override(adapter, session_db):
    session_id = session_db.create_session("contract-model-ok", "api_server")
    runner = SimpleNamespace(_session_model_overrides={})

    async def fake_run(**kwargs):
        return {"final_response": "ok", "session_id": session_id}, {"total_tokens": 1}

    app = _create_session_app(adapter)
    with patch.object(adapter, "_run_agent", side_effect=fake_run), \
            _patched_switch(_ok_switch_result("switched/model")), \
            patch("gateway.run._gateway_runner_ref", lambda: runner):
        async with TestClient(TestServer(app)) as cli:
            resp = await _post(cli, session_id, "/chat/stream", {"message": "hi", "model": "switched/model"})
            assert resp.status == 200
    assert runner._session_model_overrides


# ---------------------------------------------------------------------------
# 5: interactive_clarify gate — agent gets `clarify`, capabilities reports it
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_interactive_clarify_config_reaches_agent_and_capabilities():
    ad = APIServerAdapter(PlatformConfig())
    ad._api_key = "test-key"
    with (
        patch("gateway.run._resolve_runtime_agent_kwargs", return_value={
            "api_key": "k", "base_url": "https://x/v1", "provider": "p",
            "api_mode": None, "command": None, "args": [],
        }),
        patch("gateway.run._resolve_gateway_model", return_value="config/default-model"),
        patch("gateway.run._load_gateway_config",
              return_value={"api_server": {"interactive_clarify": True}}),
        patch("run_agent.AIAgent") as mock_agent_cls,
    ):
        mock_agent_cls.return_value = MagicMock()
        ad._create_agent(interactive_clarify=True)
        assert "clarify" in mock_agent_cls.call_args.kwargs["enabled_toolsets"]

    # A fresh, keyless adapter for the capabilities probe: _api_key on `ad` above
    # gates auth on every route (needed only to exercise _create_agent), and the
    # capabilities contract itself is independent of that.
    ad_caps = APIServerAdapter(PlatformConfig())
    app = web.Application()
    app.router.add_get("/v1/capabilities", ad_caps._handle_capabilities)
    with patch("gateway.run._load_gateway_config",
               return_value={"api_server": {"interactive_clarify": True}}):
        async with TestClient(TestServer(app)) as cli:
            resp = await cli.get("/v1/capabilities")
            data = await resp.json()
    assert data["features"]["interactive_clarify"] is True


# ---------------------------------------------------------------------------
# 6: /v1/capabilities keys gateway-capabilities.ts reads directly
#    (probeEnhancedChatStream: body.features.session_chat_streaming,
#     'session_chat_stream' in body.endpoints)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_capabilities_exposes_keys_switchui_reads_directly(adapter):
    app = web.Application()
    app.router.add_get("/v1/capabilities", adapter._handle_capabilities)
    async with TestClient(TestServer(app)) as cli:
        resp = await cli.get("/v1/capabilities")
        assert resp.status == 200
        data = await resp.json()
    assert isinstance(data["features"]["session_chat_streaming"], bool)
    assert data["features"]["session_chat_streaming"] is True
    assert "session_chat_stream" in data["endpoints"]


# ---------------------------------------------------------------------------
# 7: error event
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_error_emits_error_event_with_message(adapter, session_db):
    session_id = session_db.create_session("contract-error", "api_server")

    async def fake_run(**kwargs):
        raise RuntimeError("boom")

    app = _create_session_app(adapter)
    with patch.object(adapter, "_run_agent", side_effect=fake_run):
        async with TestClient(TestServer(app)) as cli:
            resp = await _post(cli, session_id, "/chat/stream", {"message": "hi"})
            assert resp.status == 200
            body = await resp.text()

    events = _sse_events(body)
    errors = [p for n, p in events if n == "error"]
    assert errors and "message" in errors[0]
    assert "done" in [n for n, _ in events]
