# a2a_fleet — Changelog

## v0.7.0 — in progress (PR #79)

- Added OpenAI Codex CLI as a third managed repo-scoped executor peer
  (`mode: codex`, default port 9311):
  - new standalone template `templates/codex_receiver.py` (STDLIB-only, no
    a2a_fleet import dependency)
  - new deploy/manage module `codex_deploy.py` with
    `deploy_codex_receiver_handler`, `codex_receiver_status_handler`,
    `codex_receiver_stop_handler`; full dict-unwrap for all params
  - `codex exec` / `codex exec resume` command builder; JSONL parser for
    `thread.started` → `thread_id` and last `item.completed agent_message`
    → reply text
  - `_is_session_not_found` checks both reply text and stderr for
    `"no rollout found for thread id"`

### Bug fixes

- **remint clears stale thread_id**: when a stored `thread_id` is dead and
  the remint retry emits no `thread.started`, the stale id is now deleted
  from `a2a-codex-sessions.json` before the fresh `_invoke(None)` call.
  Previously the bad id stayed on disk and every subsequent turn attempted
  `codex exec resume <dead-id>` first.

- **codex_extra_flags sanitized on resume**: forbidden flags (`--color`,
  `-s`/`--sandbox`, `--ephemeral`) are now stripped from `codex_extra_flags`
  before appending to the command. `--ephemeral` is stripped on any command
  (breaks resume); `--color`/`-s`/`--sandbox` are stripped on resume only
  (rejected by `codex exec resume` in codex-cli 0.135.0). Both
  `--flag value` and `--flag=value` forms are handled; a warning is logged
  for each dropped token.

## v0.6.1 — shipped

### Bug fixes

- **H2 (dispatch)**: `deploy_oc_receiver_handler`, `deploy_cc_receiver_handler` now
  unwrap ALL recognised params (`bind_port`, `model`, `no_auth`,
  `hermes_auth_token_env`) when the registry passes the whole args dict as the
  first positional argument. Previously only `repo_path` was extracted (via
  `canonicalize_repo_path`), so `bind_port` and `model` silently defaulted
  regardless of what the caller sent.

- **H1 (remint)**: `_is_session_not_found` in `oc_receiver.py` now accepts
  `(reply, stderr)` and checks both, mirroring `cc_receiver`'s implementation.
  The dead `session_missing` third return value from `parse_opencode_output` has
  been removed. Previously the dead-session remint guard only inspected stderr, so
  a session-not-found signal that appeared only in the parsed reply text was
  silently ignored and the stale session was re-used.

### Cleanup

- **H3**: Removed dead `_managed_cc_peers()` from `cc_deploy.py` (superseded by
  `managed_peers.iter_supported_managed_peers`). Its unit test is retargeted to
  `iter_supported_managed_peers` and now covers both `claude_code` and `opencode`
  managed modes.

### Tests added

- `test_deploy_handler_dict_dispatch_extracts_all_params` (oc) and
  `test_deploy_cc_handler_dict_dispatch_extracts_all_params` (cc): confirm
  registry-style dict dispatch uses the caller-supplied `bind_port`, not the
  default.
- `test_run_opencode_turn_remints_when_dead_session_in_reply`: confirms remint
  fires when the dead-session signal is in parsed reply text (not stderr).
- `test_reconcile_down_opencode_peer_triggers_oc_redeploy`: boot-reconcile routes
  a down `mode: opencode` peer through `oc_deploy`.
- `test_reconcile_legacy_no_mode_peer_is_ignored`: legacy no-`mode` peer is
  ignored gracefully.
- `test_oc_receiver_bearer_auth_*`: per-request bearer auth on the OC receiver
  HTTP layer — wrong/missing token → 401, correct token → accepted.

## v0.6.0 — shipped

- Added OpenCode as a second managed repo-scoped executor peer alongside Claude
  Code:
  - new tools `deploy_oc_receiver`, `oc_receiver_status`, `oc_receiver_stop`
  - new standalone template `templates/oc_receiver.py`
  - new deploy/manage module `oc_deploy.py`
- Managed-peer plumbing is now mode-aware for both `claude_code` and
  `opencode`:
  - `fleet_yaml_io.upsert_managed_peer(...)` + `upsert_oc_peer(...)`
  - `fleet_config.load_fleet()` validates and surfaces managed OpenCode peers
  - boot-reconcile now scans and redeploys both managed modes
- OpenCode receiver runtime is isolated from Claude receiver runtime:
  separate config, inbox, offset, transcript, pid, token, and session-map files
  so both peers can coexist in one repo.
- OpenCode receiver persists a durable `contextId -> sessionID` map and reuses
  it on later turns; `Session not found` remints exactly once under the existing
  per-context lock.

## v0.3.0 — planned / in progress

> **Status: planned.** Direction only — NOT shipped. Tracks the v0.3 milestone:
> Claude Code as a repo-scoped A2A executor peer. Full design lives in
> `.omc/plans/a2a-fleet-v0.3-plan.md`.

### Planned
- `deploy_cc_receiver(repo_path)` tool — deploys a standalone Claude Code
  executor receiver into a target repo's `.hermes/`, writes the A2A-role text to
  `<repo>/.hermes/A2A.md` and appends a single idempotent `@import .hermes/A2A.md`
  line to `<repo>/CLAUDE.md` (between `<!-- a2a-fleet:start -->` / `:end -->`
  markers — keeps role text out of tracked files, no git pollution), and launches
  a detached, Hermes-managed daemon on `:9300` (with idle-timeout self-teardown). Each inbound task spawns `claude -p` with
  `cwd=<repo>` and the repo's full harness loaded
  (`--setting-sources user,project,local`, `--mcp-config`, no `--bare`), keyed
  to a persistent session per `context_id` (`--session-id` / `--resume`).
  Vision: Hermes = orchestrator, Claude Code = executor with the repo's real
  skills/tools/MCP/CLAUDE.md/claude-mem.
- Companion tools `cc_receiver_status` / `cc_receiver_stop` (PID + `/health`).
- Repo-aware `fleet.yaml` peer schema — `repo_path`, `managed`, `mode`
  (`claude_code`) surfaced by `load_fleet()` so Hermes knows which repo a link
  drives and whether it owns the daemon (boot-reconcile).
- Handshake protocol exchanging roles (orchestrator / executor), bound repo,
  comm contract (same `context_id` = same persistent session; replies POSTed to
  `:9219`), and purpose.
- New `skills/deploy-cc-receiver/SKILL.md` — the orchestration procedure Hermes
  loads on demand.
- Guardrails: cwd pinned at deploy time (never from a message), per-`context_id`
  serialization, bearer auth on `:9300`, bounded autonomous operation (per-turn
  timeout, restart backoff, idle cap).

### Phase 0 (this entry's scope — shipped)
- Documentation refresh so the plugin self-describes correctly: README, plugin
  manifest description, this changelog, and the deploy skills now reflect the
  three inbound handlers (echo/llm/agent) and the v0.3 direction. Drops the
  stale v0.1 "echo handler only / TaskManager+SSE deferred" framing.

## Route B — 2026-05-31 (#54)

`agent` response handler — inbound A2A dispatched into the **real Hermes
agent** (its conversation loop, SOUL, tools, memory), not a raw model call.

### Added
- `SUPPORTED_HANDLERS` extended to `{"echo", "llm", "agent"}`; `agent` raises
  `FleetConfigError` on any unsupported value, like the others.
- `adapter.py` — `A2AFleetAdapter` (a gateway `BasePlatformAdapter`). Registered
  via `ctx.register_platform("a2a_fleet", ...)` in `register()` and self-registered
  in `platform_registry` so `Platform("a2a_fleet")` resolves without living under
  `plugins/platforms/`.
- `bridge_sync(text, context_id, peer_id, timeout)` — called from the uvicorn
  worker thread; submits a `MessageEvent` to the gateway event loop via
  `asyncio.run_coroutine_threadsafe(self._message_handler(event), gateway_loop)`
  and blocks for the real agent turn. `MessageEvent.internal=True` bypasses the
  gateway user-auth (the A2A bearer is the gate). The A2A `contextId` maps to the
  Hermes session `chat_id`, so the same `context_id` continues the same session.
- `agent_bridge.py` — global bridge registry + `A2ABusyError` /
  `A2ABridgeNotReady`. Per-`context_id` threading lock serializes same-context
  turns; an overlapping second turn gets `A2ABusyError` ("peer busy on this
  context, retry") instead of racing.
- Reasoning-preamble strip: the gateway's leading `💭 **Reasoning:**` block is
  removed before the answer goes over the wire (A2A peers want the final answer).
- Optional `fleet.agent.timeout_s` block (default 120) bounds the synchronous
  wait for the agent reply.

### Requires
- `platforms.a2a_fleet.enabled=true` in the active profile config so the gateway
  calls `adapter.connect()` and wires the bridge. If the bridge is not ready,
  `/jsonrpc` returns a JSON-RPC error telling you to enable it.

## v0.2.0 — 2026-05-31 (#53)

Real conversational inbound replies (Route A) + multi-turn context. Moves the
plugin past echo-only without yet reaching the agent (that's Route B).

### Added
- `llm` response handler (`llm_handler.py`, Route A) — a **stateless** call to
  the active profile's configured provider (`resolve_provider_client("auto")`).
  Delivers real reasoning / Q&A / persona replies. **Bypasses the Hermes agent**:
  no live tools, memory, MCP, or SOUL — treat as a plain-chat fallback.
- `context_store.py` — per-`context_id` multi-turn history + per-context async
  locks; `llm_handler` holds the lock across read→build→call→append for causal
  ordering of overlapping same-context turns.
- `HandlerResult` dataclass (`response_handler.py`) — the internal result type
  returned by every inbound handler (`text`, `context_id`, `kind`); `kind`
  reserved for the future async/task phase.
- `message/send` accepted as an alias of `SendMessage` on `/jsonrpc`.
- Outbound `context_id` threading on `fleet_send` — omit to have the server mint
  one (returned in the reply); pass it back on later turns to continue the
  thread.
- `llm` config block: `system_prompt` / `system_prompt_file` (explicit > file >
  built-in default), `max_tokens` (2048), `temperature` (0.7). Provider/api_key
  are intentionally NOT read here — they come from the active profile.

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
