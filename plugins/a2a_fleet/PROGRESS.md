# a2a_fleet v0.1 — Ralph Loop Progress

## US-000 — Scaffold ✅
- `plugin.yaml` + `__init__.py` exist
- Obsolete `dashboard/*` tree removed (architecture pivoted to embedded uvicorn)
- `manifest.yaml` deleted

## US-001 — fleet_config.py loader ✅
- `load_fleet()` returns the required shape (self/agents) with `bind_host` + `bind_port`
- Token env resolution works
- `self.url` is NOT cached (per-request)
- `response_handler: llm` raises FleetConfigError immediately
- Missing `bind_port` raises FleetConfigError
- Verified: `HERMES_HOME=/tmp/a2a_fleet_test python -c "from a2a_fleet.fleet_config import load_fleet; print(load_fleet())"` returns valid agent map

## US-002 — server.py / Agent Card ✅
- `build_app()` returns FastAPI app with `GET /.well-known/agent-card.json` + `POST /jsonrpc`
- Agent Card includes `securitySchemes.bearerAuth` (`type: http, scheme: bearer`)
- Agent Card URL = `http://{bind_host}:{bind_port}/jsonrpc`
- Agent Card endpoint is PUBLIC (returns 200 with no auth header AND ignores bad Bearer headers)
- `start_server()` / `stop_server()` lifecycle works via TestClient AND live uvicorn (curl GET /.well-known/agent-card.json returned 200 on bound port)

## US-003 — JSON-RPC SendMessage + bearer + echo ✅
- Raw `await request.json()` — NO Pydantic in route signatures
- `auth_required: false` (v0.1 default): no bearer check
- `auth_required: true`: missing/wrong Bearer returns HTTP 401
- Malformed JSON → HTTP 200 + JSON-RPC code `-32700`
- Unknown methods (tasks.get/list/cancel, SendStreamingMessage) → `-32601`
- SendMessage("ping") returns `result.kind="message"` with `parts[0].text == "pong"`
- Agent Card route remains 200 with no auth header even when auth_required=true

## US-004 — client.py ✅
- `async send_message(agent_name, text) -> str` returns the peer's reply text
- httpx.AsyncClient(timeout=30), no a2a-sdk
- Bearer header attached when peer has token
- `if __name__ == "__main__"` block: `cd plugins && HERMES_HOME=~/.hermes python -m a2a_fleet.client construct ping` prints `pong`
- Verified live against loopback server AND against subprocess construct server

## US-005 — fleet_send tool + register/disable lifecycle ✅
- `fleet_tools.fleet_send_handler` returns `{reply}` or `{error}` (never raises)
- `__init__.py.register(ctx)` uses lazy imports inside the function body
- `ctx.register_tool(name, toolset='a2a', schema, handler, check_fn=None, is_async=True, description, emoji)` — `is_async=True` present
- `register()` schedules `start_server()` on the running loop
- `disable()` schedules `stop_server()`
- Verified stub ctx: register_tool was called with correct kwargs

## US-006 — Test suite (5 files / 19 tests) ✅
```
tests/test_agent_card.py        2 tests PASS
tests/test_client.py            4 tests PASS
tests/test_fleet_config.py      5 tests PASS
tests/test_jsonrpc_echo.py      5 tests PASS
tests/test_server_lifecycle.py  3 tests PASS
========================= 19 passed in 0.91s ============================
```

## US-007 — End-to-end two-server ✅
- Two HERMES_HOME dirs (`switch_home`, `construct_home`) with distinct profiles on ports 9219/9220, `auth_required: true`
- Construct subprocess booted; `GET /.well-known/agent-card.json` → 200 + valid card
- `POST /jsonrpc` with `Bearer tok-construct` + ping → reply `pong`
- Same POST without bearer → HTTP 401
- `python -m a2a_fleet.client construct ping` from switch profile → printed `pong` (exit 0)
- fleet_send agent tool already verified in US-005 against loopback; same code path applies

## Files shipped (v0.1)
- `plugin.yaml` — agent plugin manifest
- `__init__.py` — register(ctx) + disable() lifecycle hooks
- `fleet_config.py` — fleet.yaml loader, env resolution, fail-fast validation
- `server.py` — FastAPI app + uvicorn lifecycle, Agent Card, JSON-RPC handler
- `response_handler.py` — `echo_handler(text, context_id)`
- `client.py` — async A2A client + `__main__` CLI
- `fleet_tools.py` — `fleet_send_handler` wrapping the client
- `tests/conftest.py` + 5 test files (19 tests, all passing)
- `references/a2a-spec-v1-summary.md` + `references/hermes-gateway-plugin-guide.md` (existing)
