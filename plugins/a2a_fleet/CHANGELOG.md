# a2a_fleet — Changelog

## v0.1.0 — 2026-05-28

Initial release. Echo-only Agent-to-Agent (A2A v1.0) communication over JSON-RPC between Hermes profiles. Each plugin instance runs its own embedded uvicorn server on a dedicated A2A port.

### Added
- Spec-compliant A2A `/.well-known/agent-card.json` (public, with `securitySchemes.bearerAuth`)
- A2A JSON-RPC `SendMessage` endpoint with sync `Message` reply (`result.kind="message"`)
- Echo `response_handler` — `ping → pong`, otherwise echoes input
- Standalone `fleet.yaml` config (per-profile), env-var token resolution
- Unidirectional peer schema — declare peers you initiate calls to; inbound side just sets `server.token_env`
- `fleet_send(agent, message)` agent tool registered via `ctx.register_tool(..., is_async=True, ...)`
- Minimal `httpx`-based A2A client with `python -m a2a_fleet.client <agent> <message>` CLI
- Plugin lifecycle: `register(ctx)` boots the A2A uvicorn; `disable()` stops it gracefully
- Test suite: 23 tests across 6 files (fleet config, agent card, JSON-RPC, client, server lifecycle, regression guards)

### Architecture
- Embedded uvicorn on dedicated port — not mounted under the Hermes dashboard gateway. Decision rationale and pivot history live at the top of `README.md`.

### Safety & correctness fixes (post-review)
- `start_server()` polls the background task and surfaces uvicorn bind failures (e.g. port-in-use) as `A2AServerStartError` instead of falsely reporting success.
- `register()` captures + logs `start_server()` exceptions instead of swallowing them via `loop.create_task`.
- `fleet.enabled: false` is honored — no tool registration, no server start.
- Misconfiguration (`auth_required: true` with unset `token_env`) returns a JSON-RPC error envelope (`-32603`) instead of a plain HTTP 500.
- Bearer-token comparison uses `hmac.compare_digest` (timing-safe).
- Removed a module-level `asyncio.Lock` that bound to the first event loop and broke across multiple `asyncio.run()` calls.

### Deferred to v0.2+
- `TaskManager` bridging A2A tasks to real Hermes agent sessions
- SSE streaming for `SendStreamingMessage` and task status updates
- LLM-backed `response_handler` (reads existing Hermes LLM config)
- Push notifications, task persistence, OAuth/mTLS
- Optional upstream Hermes patch for third-party A2A client discovery (current fleet is closed-discovery via `agent_card_url` in `fleet.yaml`)

### Security notes
- v0.1 defaults `auth_required: true` — inbound `/jsonrpc` requires a bearer token unless explicitly disabled. Configure `token_env` for the self-server and each peer. Bearer comparison is constant-time (`hmac.compare_digest`).
- No CORS middleware: A2A is server-to-server (browsers are not A2A clients), so wildcard CORS would be misleading and is intentionally omitted.
- The Hermes dashboard gateway and the a2a_fleet uvicorn are two independent surfaces — disabling one does not affect the other.
