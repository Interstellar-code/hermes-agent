# Hermes A2A Plugin MVP Implementation Plan

> **For Hermes:** Use subagent-driven-development skill to implement this plan task-by-task.

**Goal:** Add an experimental A2A (Agent-to-Agent) plugin to Hermes Agent so one Hermes profile can talk to another over a standardized HTTP protocol with Agent Card discovery and a minimal task lifecycle.

**Architecture:** Implement A2A as a standalone Hermes plugin mounted on the existing gateway/plugin API surface. The MVP will expose an Agent Card at `/.well-known/agent.json`, accept JSON-RPC 2.0 task submissions over HTTP, bridge each incoming A2A task into a real Hermes session running under the target profile, and return a minimal task state machine (`submitted → working → completed/failed`). Multi-turn `input-required`, SSE streaming, and external-agent interoperability validation are deferred until after the first internal Hermes-to-Hermes round-trip works.

**Tech Stack:** Hermes Agent plugin system, FastAPI/Starlette router, existing gateway session/runtime plumbing, JSON-RPC 2.0, optional `a2a-sdk` later (not required for MVP), pytest.

---

## Why this exists

Hermes profiles are currently isolated in the places that matter most for real multi-agent operations:

- per-profile `MEMORY.md` / `USER.md`
- per-profile session transcripts
- no native cross-profile session search
- no native inter-agent request/response protocol

Today the only shared bridge is Hindsight, which is useful but not sufficient. It is recall-oriented, not conversation-oriented. If Switch needs Neo's actual reasoning about a past task, Switch cannot ask Neo directly. It can only hope the right fact was retained and is retrievable.

A2A is the correct missing layer because it gives Hermes:

- standardized agent discovery
- live agent-to-agent requests
- durable task identities
- eventual multi-turn negotiation via `input-required`
- a path to external agent interoperability without inventing a Hermes-only protocol first

This plan intentionally starts with the smallest useful slice: **internal Hermes profile ↔ Hermes profile communication**.

---

## Research summary

### Prior internal findings

From prior Hermes and Construct research:

- A2A is the right protocol boundary for **agent ↔ agent** communication.
- MCP remains the right boundary for **agent ↔ tool** calls.
- Hermes `delegate_task` / ACP handles **internal one-shot subprocess delegation**, not persistent peer-to-peer conversation.
- Hermes Kanban `task_events` can still act as a workflow/event substrate, but they are not a substitute for direct live inter-agent messaging.

### External protocol findings

From the A2A spec and ecosystem research:

- transport: JSON-RPC 2.0 over HTTP(S)
- discovery: Agent Card at `/.well-known/agent.json`
- task lifecycle: `submitted → working → input-required → completed/failed/canceled`
- supports: sync, async, SSE streaming, push notifications
- language SDKs exist, but MVP can be done with direct JSON parsing and plain FastAPI

### Product stance for Hermes MVP

The MVP should **not** try to implement the entire spec.

That would be ceremony without proof.

The MVP should prove four things only:

1. one Hermes profile can discover another via Agent Card
2. one Hermes profile can submit a task to another over HTTP
3. the target profile can execute the request in its own real session context
4. the caller can receive a structured result with task status and final output

If those four work, the rest is iteration.

---

## Scope

## In scope for MVP

- standalone Hermes plugin: `plugins/a2a/`
- plugin HTTP API mounted on gateway
- Agent Card endpoint
- minimal JSON-RPC request handler
- minimal task storage/in-memory registry for active tasks
- bridging incoming task into Hermes runtime under the receiving profile
- synchronous request path first
- minimal status values: `submitted`, `working`, `completed`, `failed`
- explicit config block for enable/disable and profile exposure
- tests for card serving, request validation, task execution, failure handling
- docs + example curl commands

## Out of scope for MVP

- full A2A spec compliance
- `input-required`
- SSE streaming
- push notifications
- external auth schemes beyond local trusted deployment
- agent registry/discovery service
- Kanban integration
- Telegram bot relay
- cross-profile session search
- persistent task DB storage
- external non-Hermes clients beyond basic curl/manual validation

---

## Proposed user-visible behavior

### Receiving side

If profile `neo` runs Hermes gateway with the A2A plugin enabled, it exposes (all paths per-profile per Clarification #3):

- `GET /api/plugins/a2a/neo/agent.json` — per-profile Agent Card
- `POST /api/plugins/a2a/neo/rpc` — per-profile JSON-RPC endpoint
- `GET /api/plugins/a2a/neo/tasks/{task_id}` — debugging/task inspection

Canonical `/.well-known/agent.json` is NOT served in MVP (single-profile-default is ambiguous and discoverability hurts when more than one profile is enabled). P1 follow-up may add it as a redirect to the default profile.

### Sending side

A caller can send a JSON-RPC request with a task payload such as:

```json
{
  "jsonrpc": "2.0",
  "id": "req-123",
  "method": "message/send",
  "params": {
    "message": {
      "role": "user",
      "parts": [
        { "type": "text", "text": "Summarize what you know about issue X and recommend a fix." }
      ]
    },
    "metadata": {
      "from_agent": "switch",
      "conversation_id": "telegram-7341688567-2026-05-13"
    }
  }
}
```

The receiver translates that into a Hermes session prompt, runs the local profile's agent, and returns:

```json
{
  "jsonrpc": "2.0",
  "id": "req-123",
  "result": {
    "task": {
      "id": "a2a_task_...",
      "status": "completed"
    },
    "artifacts": [
      {
        "type": "text",
        "text": "...final response from neo..."
      }
    ]
  }
}
```

---

## Proposed plugin shape

### Directory

```text
plugins/a2a/
├── plugin.yaml
├── __init__.py
├── server.py
├── models.py
├── task_manager.py
├── agent_card.py
└── README.md
```

### `plugin.yaml`

Use `kind: standalone`.

Expected fields:

```yaml
name: a2a
version: "0.1.0"
description: Experimental Agent-to-Agent protocol plugin for Hermes profiles
author: Hermes
kind: standalone
provides_tools: []
hooks: []
requires_env: []
```

### `__init__.py`

Responsibilities:

- register plugin API router with the dashboard/gateway plugin surface
- load config
- construct task manager singleton
- expose router handlers

### `models.py`

Define minimal typed structures:

- `A2AAgentCard`
- `A2ATask`
- `A2ATaskStatus`
- `JsonRpcRequest`
- `JsonRpcSuccess`
- `JsonRpcError`

Keep this local and small. Do not pull in a heavy SDK until Hermes proves the shape.

### `agent_card.py`

Build a profile-specific Agent Card using:

- current profile name
- optional display name from config
- endpoint URL from gateway host/port if available, otherwise relative path
- skills/capabilities from config, not auto-derived in MVP

### `task_manager.py`

Responsibilities:

- create task ids
- validate supported methods
- hold active task states in memory
- call the Hermes runtime to execute the incoming request
- capture final response / error
- expose lookup by task id

### `server.py`

FastAPI router endpoints:

- `GET /.well-known/agent.json`
- `POST /api/plugins/a2a/rpc`
- `GET /api/plugins/a2a/tasks/{task_id}`

---

## Runtime bridging design

This is the critical part.

The A2A plugin is not its own agent. It is a protocol shim in front of a real Hermes profile.

### Rule

Every inbound A2A task must execute through the receiving profile's normal Hermes runtime so it has access to:

- that profile's `SOUL.md`
- that profile's `MEMORY.md` / `USER.md`
- that profile's enabled tools
- that profile's config, model, provider routing
- that profile's session storage

### MVP execution path

1. Receive JSON-RPC request
2. Validate supported method (`message/send` only in MVP)
3. Extract incoming text from `message.parts`
4. Build a normalized internal prompt wrapper such as:

```text
You are responding to an A2A request from another agent.

Caller: switch
Conversation metadata: ...

User request:
<message text>

Respond directly with the result. Do not explain the protocol.
```

5. Start a real Hermes agent run inside the current profile context
6. Wait for completion synchronously
7. Write task status transitions in memory registry
8. Return JSON-RPC result

### Implementation note — runtime bridge contract (LOCKED)

The plugin **MUST** invoke the Hermes runtime via **direct in-process Python call** to the existing session entry point (e.g. `gateway/chat_handler.py` or `run_agent.py` equivalent). Not via HTTP self-loop to the local gateway. Not via CLI subprocess.

Rationale:
- Plugin runs inside the same gateway process → IPC overhead is wasted
- Shared memory state (profile cache, model client pool) is reusable
- Subprocess is fragile and slow
- HTTP self-loop creates a recursive listener-on-listener pattern

If the existing session entry point is not callable from plugin scope (private symbol, missing async wrapper, etc.), add a **minimal public helper** in `gateway/` named e.g. `run_oneshot_chat(profile, prompt, *, caller_meta) -> ChatResult`. The plugin imports and awaits this helper. Helper must NOT take any A2A-specific arguments — it stays generic for future callers (CLI, MCP, tests).

The helper signature must satisfy:
- input: profile name (str), prompt text (str), caller metadata dict (opt)
- output: dataclass with `text: str`, `success: bool`, `error: str | None`, `session_key: str`, `model: str`
- side effect: writes a real session entry into the profile's normal session store, indistinguishable from any other chat session except for caller metadata

### Concurrency policy (LOCKED)

MVP: **serialize per profile**. One A2A task in flight per profile at a time; additional submissions queue and block on the per-profile asyncio lock. Cross-profile concurrency is unrestricted (different profiles run in parallel).

Rationale: Hermes session state per profile is not designed for concurrent writes; queuing is the safe MVP default. Document max queue depth (default 10) and overflow behavior (reject with `RESOURCE_EXHAUSTED` JSON-RPC error). Parallel-same-profile is a P1 follow-up that requires session-level reentrancy work.

### Error matrix (LOCKED)

Every error path returns a structured JSON-RPC error envelope. Codes follow A2A spec conventions where defined, custom -32xxx range otherwise.

| Failure mode | JSON-RPC code | A2A status | HTTP code |
|---|---|---|---|
| Profile not found | -32004 | rejected | 404 |
| Profile exists but A2A not enabled | -32005 | rejected | 403 |
| Plugin globally disabled | (no route mounted — 404 at HTTP layer) | n/a | 404 |
| Hermes runtime fails to start session | -32603 | failed | 500 |
| Session starts, agent errors mid-execution | -32603 | failed | 200 (JSON-RPC envelope) |
| JSON-RPC parse error | -32700 | n/a | 400 |
| Method not in MVP allowlist (only `message/send`) | -32601 | n/a | 200 |
| Request body exceeds size limit (default 256 KB) | -32600 | n/a | 413 |
| Per-profile concurrency queue overflow | -32004 (resource exhausted) | rejected | 429 |
| Duplicate `messageId` within dedup window | -32001 (idempotent replay) | (return original task) | 409 |
| Task timeout exceeded (default 300s) | -32603 | failed | 200 |

Every error envelope MUST include `data.task_id` (when assigned), `data.profile` (when relevant), and a human-readable `message`. No silent 500s.

---

## Config design

Add a new config section:

```yaml
a2a:
  enabled: true
  public_base_url: "http://127.0.0.1:8642"
  agent_name: "Neo"
  agent_description: "Hermes specialist profile for coding and technical diagnosis"
  skills:
    - ask_questions
    - summarize_context
    - review_code
  allow_methods:
    - message/send
  require_localhost: true
```

### Config rules

- default: disabled
- if enabled and `require_localhost=true`, reject non-local requests unless explicitly relaxed
- do not auto-publish tools as capabilities yet
- do not attempt auth negotiation in MVP

---

## File-by-file plan

### Task 1: Inspect plugin API mounting and choose the cleanest integration point

**Objective:** Confirm exactly how a new plugin exposes HTTP routes in Hermes and identify the smallest valid A2A plugin skeleton.

**Files:**
- Read: `hermes_cli/plugins.py`
- Read: `plugins/kanban/dashboard/plugin_api.py`
- Read: one additional simple plugin with API surface if present
- Create notes in plan only; no code yet

**Step 1: Locate route registration pattern**

Read the plugin manager and the kanban plugin API mounting path.

**Step 2: Identify the exact entry point expected by plugin API modules**

Document whether Hermes expects `dashboard/plugin_api.py`, direct router registration, or another pattern for plugin HTTP routes.

**Step 3: Verify path namespace rules**

Confirm whether the plugin can expose `/.well-known/agent.json` directly or whether Hermes only mounts under `/api/plugins/<name>/...`.

**Step 4: Record decision in code comments or README draft**

If direct root mount is impossible, use a small gateway/core patch to allow a well-known route registration hook.

**Step 5: Commit**

```bash
git add [only if code/comments were created]
git commit -m "docs: note a2a plugin route integration constraints"
```

### Task 2: Create the standalone A2A plugin skeleton

**Objective:** Add the plugin directory, manifest, registration entry point, and a minimal API router that loads without errors.

**Files:**
- Create: `plugins/a2a/plugin.yaml`
- Create: `plugins/a2a/__init__.py`
- Create: `plugins/a2a/server.py`
- Create: `plugins/a2a/README.md`
- Test: plugin-loading test file if appropriate under `tests/`

**Step 1: Write a failing plugin-load test**

Test should assert Hermes can discover the `a2a` plugin and import it successfully when enabled.

**Step 2: Add `plugin.yaml`**

Use minimal standalone manifest.

**Step 3: Add registration stub — config-gated**

`register(ctx)` reads `a2a.enabled` from config FIRST. If `enabled=false`, log "A2A plugin disabled, skipping route registration" and return without mounting any routes. If `enabled=true`, install the router. This guarantees disabled=route-not-mounted (closes the route-mounted-but-disabled hole flagged in security review).

**Step 4: Add a dummy health endpoint**

Return JSON like `{ "ok": true, "plugin": "a2a" }`.

**Step 5: Run targeted tests**

Use pytest only on the new plugin test.

**Step 6: Commit**

```bash
git add plugins/a2a tests/
git commit -m "feat: add a2a plugin skeleton"
```

### Task 3: Add Agent Card model and endpoint

**Objective:** Expose a valid minimal Agent Card for the current Hermes profile.

**Files:**
- Create: `plugins/a2a/models.py`
- Create: `plugins/a2a/agent_card.py`
- Modify: `plugins/a2a/server.py`
- Test: `tests/plugins/test_a2a_agent_card.py`

**Step 1: Write failing test for `GET /api/plugins/a2a/<profile>/agent.json`**

Assert response shape contains name, description, capabilities/skills, and endpoint. Test with two profiles (`neo`, `trinity`) and confirm each card advertises the correct profile-scoped `url` field.

**Step 2: Implement Agent Card builder**

Read profile/config values and produce deterministic JSON. Builder takes profile name as input; one card per profile.

**Step 3: Serve the per-profile card endpoint**

Mount at `/api/plugins/a2a/<profile>/agent.json`. The plugin router resolves `<profile>` against the enabled-profiles allowlist; unknown profiles return 404 per error matrix. Canonical `/.well-known/agent.json` is explicitly NOT mounted in MVP.

**Step 4: Validate response shape**

Keep the payload minimal, human-readable, and stable.

**Step 5: Commit**

```bash
git add plugins/a2a tests/plugins/test_a2a_agent_card.py
git commit -m "feat: expose minimal a2a agent card"
```

### Task 4: Add JSON-RPC request parsing and method validation

**Objective:** Accept well-formed RPC requests and reject malformed/unsupported ones cleanly.

**Files:**
- Modify: `plugins/a2a/models.py`
- Modify: `plugins/a2a/server.py`
- Create: `tests/plugins/test_a2a_rpc_validation.py`

**Step 1: Write failing tests**

Cases:
- invalid JSON-RPC version
- missing id
- unknown method
- missing message text payload
- valid `message/send`

**Step 2: Implement local request/response models**

Return proper JSON-RPC error objects with code/message.

**Step 3: Add `POST /api/plugins/a2a/rpc`**

Only support `message/send` in MVP.

**Step 4: Re-run tests**

Make sure invalid payloads fail predictably.

**Step 5: Commit**

```bash
git add plugins/a2a tests/plugins/test_a2a_rpc_validation.py
git commit -m "feat: add a2a json-rpc validation"
```

### Task 5: Build in-memory task manager with lifecycle transitions

**Objective:** Track submitted work with a real internal task object and observable status transitions.

**Files:**
- Create: `plugins/a2a/task_manager.py`
- Modify: `plugins/a2a/models.py`
- Modify: `plugins/a2a/server.py`
- Create: `tests/plugins/test_a2a_task_manager.py`

**Step 1: Write failing tests for task creation and state changes**

Assert:
- task created with `submitted`
- task moves to `working`
- task ends as `completed` or `failed`
- lookup endpoint returns final task state

**Step 2: Implement task registry**

Use in-memory dict keyed by `task_id`.

**Step 3: Add task inspection endpoint**

`GET /api/plugins/a2a/tasks/{task_id}`

**Step 4: Confirm concurrent requests do not overwrite each other**

A simple lock is enough for MVP.

**Step 5: Commit**

```bash
git add plugins/a2a tests/plugins/test_a2a_task_manager.py
git commit -m "feat: add a2a task lifecycle manager"
```

### Task 6: Bridge `message/send` into a real Hermes profile run

**Objective:** Execute inbound A2A tasks through the receiving profile's real Hermes runtime and return the final response.

**Files:**
- Modify: `plugins/a2a/task_manager.py`
- Modify: `plugins/a2a/server.py`
- Possibly modify: shared runtime helper in `gateway/` or `run_agent.py` only if needed
- Create: `tests/plugins/test_a2a_execution.py`

**Step 1: Write failing execution test**

Mock the runtime first if needed, but also add one higher-level integration test that proves the bridge function is called.

**Step 2: Implement prompt wrapper**

Wrap inbound A2A request into a normalized Hermes prompt preserving caller metadata.

**Step 3: Call the normal Hermes execution path**

Do not fork a weird new path if existing session execution helpers exist.

**Step 4: Map success/failure to A2A result**

- success → `completed`
- exception/runtime failure → `failed`

**Step 5: Return text artifact**

Use a single text artifact in MVP.

**Step 6: Commit**

```bash
git add plugins/a2a tests/plugins/test_a2a_execution.py [any shared runtime helper]
git commit -m "feat: execute a2a tasks through hermes runtime"
```

### Task 7: Add security guardrails and config wiring

**Objective:** Prevent accidental exposure + abuse while the feature is experimental.

**Files:**
- Modify: config handling path(s) for plugin config
- Modify: `plugins/a2a/server.py`, `plugins/a2a/__init__.py`
- Create: `tests/plugins/test_a2a_security.py`
- Update: docs if config docs live in `website/`

**Step 1: Write failing tests** — cover all guardrails:
- Remote (non-localhost) requests rejected when `require_localhost=true`
- Plugin disabled → routes NOT mounted (404 at HTTP layer, not 403)
- Request body > 256 KB → 413 with JSON-RPC -32600
- Per-profile concurrent submission limit (default 10 queued) → 429 with -32004
- Per-task timeout (default 300s) → 200 with task status `failed`, code -32603
- Inbound prompt logging redacts message text (only logs length + sender metadata)

**Step 2: Disabled = route not mounted**

When `a2a.enabled=false` in config, plugin `register()` must early-return without calling `router.include_router(...)`. No 403/404 fallback — the route simply does not exist. This closes the route-mounted-but-disabled hole.

**Step 3: Local-only bind enforcement**

Plugin reads gateway bind address. If gateway is bound to `0.0.0.0` and `a2a.require_localhost=true` (default), plugin refuses to register routes and logs a clear error: "A2A requires localhost-bound gateway; refuse to mount routes."

Plus: even when bound correctly, every inbound request checks `request.client.host` and rejects non-loopback with -32003 if `require_localhost=true`.

**Step 4: Rate limit + size cap + timeout**

- Rate limit: default 30 requests/min per source IP (configurable). Exceeds → 429.
- Body size cap: default 256 KB (configurable). Exceeds → 413 with -32600.
- Task timeout: default 300 seconds wall-clock per task. Exceeds → status `failed`, code -32603, cancel underlying Hermes session via existing dispatcher SIGTERM path (per Clarification #11).

**Step 5: PII-safe logging**

Inbound prompt text MUST NOT be logged at INFO/WARN/ERROR levels. Allowed log fields: task_id, profile, caller_meta (from_agent, conversation_id), prompt_byte_length, status transitions. Full prompt only at DEBUG with explicit `a2a.log_prompts=true` opt-in.

**Step 6: Re-run tests**

All security tests must pass. Plus run gateway with `host=0.0.0.0` and verify plugin refuses to mount.

**Step 7: Commit**

```bash
git add plugins/a2a tests/plugins/test_a2a_security.py
git commit -m "feat: add security guardrails for a2a plugin"
```

### Task 8: Add automated E2E test + manual validation docs

**Objective:** Prove two Hermes profiles can talk via A2A through an automated test (not just manual curl), plus document the manual path for humans.

**Files:**
- Create: `tests/plugins/test_a2a_e2e_two_profiles.py` (automated)
- Update: `plugins/a2a/README.md` (manual)
- Optionally update: website docs

**Step 1: Write automated 2-profile E2E test**

Test setup:
- Spawn two test gateway instances (or one process with two profile contexts) on different ports
- Profile A (`neo`) and profile B (`trinity`)
- Both have `a2a.enabled=true` and `a2a.require_localhost=true`

Test flow:
1. Discover B's Agent Card from A's perspective: `GET http://127.0.0.1:<port_b>/api/plugins/a2a/trinity/agent.json`
2. Assert card has `name="trinity"`, `capabilities.streaming=false`
3. Submit a `message/send` from A to B with a deterministic prompt ("Say PONG and nothing else.")
4. Assert response: valid JSON-RPC envelope, `result.task.status=completed`, `result.artifacts[0].text` contains "PONG"
5. Verify trinity's session store now has a new entry with the caller metadata `from_agent=neo`
6. Submit duplicate `messageId` — assert 409 with original `task.id` referenced

Use pytest fixtures + a lightweight in-process gateway harness (avoid spawning real subprocesses if the gateway provides a test client). If subprocess is unavoidable, mark the test with `@pytest.mark.slow` and ensure CI runs it.

**Step 2: Document two-profile manual setup in README**

Example:
- `switch` profile gateway on one port
- `neo` profile gateway on another port

**Step 3: Document discovery test**

```bash
curl http://127.0.0.1:<neo-port>/api/plugins/a2a/neo/agent.json
```

(NOT `/.well-known/agent.json` — that path is reserved for P1 follow-up per Clarification #3.)

**Step 4: Document task send test**

Include a full JSON-RPC curl example using `message/send` against `/api/plugins/a2a/<profile>/rpc`.

**Step 5: Document error-path smoke tests**

curl examples for: profile-not-found (404), method-not-supported (-32601), body-too-large (413), duplicate-messageId (409).

**Step 6: Commit**

```bash
git add plugins/a2a tests/plugins/test_a2a_e2e_two_profiles.py
git commit -m "feat: a2a e2e test + manual validation docs"
```

**Step 4: Document expected output and failure modes**

Include notes for plugin not enabled, localhost rejection, runtime exception.

**Step 5: Commit**

```bash
git add plugins/a2a/README.md
git commit -m "docs: add a2a plugin mvp validation guide"
```

---

## Suggested implementation notes

### Prefer direct implementation over SDK for the first cut

The A2A SDK is useful later, but for MVP it may hide too much and slow us down.

For the first pass:

- hand-roll the minimal request/response models
- keep the surface tiny
- adopt the SDK later only if it meaningfully reduces drift from the spec

### Keep task storage in memory first

Do not introduce a DB for MVP.

If the process restarts, tasks disappear. Fine. The point is proving agent-to-agent execution, not persistence.

### Keep artifacts to plain text first

Do not implement file/blob handling in MVP.

Once text round-trips work, file artifacts are easy.

### Do not auto-discover capabilities from tools yet

That sounds elegant and is exactly how you waste a day.

Use explicit config for advertised skills in the Agent Card.

### Reuse existing Hermes session machinery

If the plugin has to instantiate `AIAgent` manually, that is acceptable for the experiment.

If a cleaner internal helper exists or can be added, use it. The plugin should not duplicate session bootstrap logic in three places.

---

## Open questions to resolve during implementation

1. **Can a plugin expose `/.well-known/agent.json` directly?**
   If not, do we accept a temporary non-canonical path or add a small gateway hook for root-level well-known routes?

2. **What is the cleanest entry point for executing a single inbound request inside the current profile?**
   Need to inspect existing gateway/API request handlers.

3. **Should the sender be a generic external HTTP client first, or another Hermes profile immediately?**
   Recommendation: validate with curl first, then Hermes↔Hermes.

4. **Do we want a companion sender tool in Hermes later?**
   Probably yes, but not part of this plugin MVP. For now, curl/manual POST is enough.

5. **Should task ids be spec-shaped or Hermes-local?**
   Hermes-local opaque ids are fine for MVP.

---

## Design Clarifications (post-review)

### 1. Local-only enforced at bind, not just config

The plugin listener MUST bind to `127.0.0.1` explicitly — not `0.0.0.0`. Config flag `require_localhost` is a secondary guard; the bind address is the primary enforcement. If a user passes `--host 0.0.0.0`, the plugin MUST refuse to start unless auth is implemented (not in MVP). Document this constraint explicitly in the security task (Task 7) and in `README.md`.

### 2. No SSE is a documented limitation, not silent

The Agent Card capabilities response MUST advertise `streaming: false`. Spec-compliant clients (LangChain, Vertex, MCP-A2A adapters) use this field to decide whether to fall back to request/response polling. MVP is intentionally degraded vs. fully spec-compliant servers — acceptable for "does the protocol work" demo, but a blocker for third-party interop. SSE is a P1 follow-up (added to roadmap below).

### 3. Profile routing — pick (a): per-profile endpoint path

Decision: use **per-profile endpoint path** (`/a2a/<profile>/`) rather than a single endpoint advertising the active default profile. Multi-profile interop is half the value proposition. Forward-compat rationale: a single-profile endpoint would require a breaking API change to go multi-profile later. Document the active profile in the Agent Card `url` field. MVP may implement only the default profile path; the URL structure must already be per-profile.

### 4. Persistent task DB — P1 follow-up, documented

MVP in-memory task table is intentionally ephemeral (lost on restart). This is explicitly a local-demo limitation and a real interop blocker for production use. P1 follow-up: SQLite-backed task table reusing existing `kanban_db.py` patterns. Add to roadmap.

### 5. A2A spec version pinning

Pin to `a2a-sdk` v0.3.24 (March 2026) if/when SDK is adopted. Document that v1.0 GA is expected mid-2026. Add early version negotiation: reject Agent Cards from clients claiming incompatible major versions with a clear error. For MVP hand-rolled implementation, document the targeted spec revision in `README.md`.

### 6. Task ID ↔ Hermes session ID mapping contract

**Decision:** 1 A2A task = 1 new Hermes session (default). A task does NOT continue an existing session unless `contextId` is provided. MVP does not implement `contextId` continuation — document as out of scope.

Mapping table shape (in-memory for MVP, SQLite in P1):

```python
{
  "a2a_task_<uuid>": {
    "hermes_session_id": "<session-id>",
    "status": "submitted|working|completed|failed|canceled",
    "created_at": "<iso8601>",
    "message_id": "<client-provided-idempotency-key>",  # if provided
  }
}
```

### 7. Idempotency

A2A clients retry on transport errors. The task manager MUST support idempotency keys:

- Client provides `messageId` field in the request params
- If `messageId` matches a task created within the last 5 minutes, return `409` with a JSON-RPC error referencing the existing task id
- After 5 minutes the idempotency window expires and the same `messageId` may create a new task
- Store `messageId` in the task registry (in-memory for MVP)

Add to task manager spec (Task 5).

---

## Upstream Awareness

### 8. Plugin-first as deliberate adoption strategy

Upstream issue #514 (teknium1, OPEN) classifies A2A as a TOOL (`tools/a2a_tool.py`), mirroring `tools/mcp_tool.py`. Our MVP is a **plugin** by design — this is the strategic entry path, not a divergence by accident.

**Why plugin-first, then tool eventually:**

1. **Distribution as an add-on, not core invasion.** Plugin can be installed alongside a stock Hermes Agent without modifying core source. Users opt in by enabling the plugin in config; no risk to their existing agent if it breaks or churns with the pre-1.0 SDK.

2. **Plugin system already exposes the surfaces we need.** Hermes plugins can register custom tools, skills, routes, and config sections (see `plugins/kanban/`, `plugins/image_gen/`). The A2A plugin can offer tools to the running profile (e.g., `a2a_discover`, `a2a_call`) and accept inbound A2A tasks through the same plugin host — same execution path as future tool integration, just packaged differently.

3. **Isolation = safer iteration.** Plugin failures are contained; if A2A spec changes or the SDK breaks, only A2A users notice. A core tool change ripples through every agent's startup.

4. **Path to upstream is clear, not blocked.** Once the plugin proves the protocol shim works end-to-end and gathers real-world use, the same code can be refactored into `tools/a2a_tool.py` per teknium's stated direction in #514. The plugin acts as a **staging environment** for the eventual tool form. Most of the implementation — task manager, runtime bridge, agent card generation, security — stays identical; only the registration point changes (plugin host → core tool registry).

5. **Avoids the upstream PR queue trap.** Multiple stalled A2A PRs upstream show the area is contested. Our plugin keeps us shipping without waiting on consensus.

**Reference architecture for the eventual upgrade:** keep the plugin's `TaskStore`, `AgentExecutor`, `AgentCardBuilder` interfaces narrow enough that the tool refactor is a registration-layer swap, not a rewrite. (See item 16 — narrow seams.)

### 9. RPC method names — use current spec names

Use `message/send`, `message/stream`, `tasks/get`, `tasks/cancel`.

Do NOT use the legacy names `tasks/send`, `tasks/sendSubscribe` from older spec drafts. These appear in some older samples and tutorials but are not current spec. MVP only implements `message/send` (synchronous path).

### 10. Agent Card discovery path

Use `/.well-known/agent.json` (current spec). Do NOT use `/.well-known/agent-card.json` — this is the older path found in some samples and legacy issue #4454. Note: some spec page samples still use the old path; verify against the spec source before shipping.

### 11. `tasks/cancel` wired to Hermes session interrupt

Do not defer `tasks/cancel` with a streaming-only assumption. Wire `tasks/cancel` to interrupt the underlying Hermes session via the existing kanban dispatcher SIGTERM path. This satisfies the "mid-task intervention" use case without requiring SSE. MVP can implement a best-effort cancel (signal sent; status transitions to `canceled`).

### 12. `AUTH_REQUIRED` state stub — reserve in state machine

Reserve the `auth-required` lifecycle state in the task state machine even though MVP never emits it. This allows later auth-challenge features to land additively without breaking client state parsers that already handle the enum.

State machine for MVP:

```
submitted → working → completed
                   → failed
                   → canceled     (via tasks/cancel)
[auth-required]                   # reserved, never emitted in MVP
```

### 13. JWS Agent Card signing — no-op stub

The A2A spec marks JWS signing optional. Ship a no-op signature stub now: Agent Card response includes an empty `signature` field (or null). Later signature support becomes additive, not breaking, for clients that start enforcing JWS.

### 14. Explicit capability flags in Agent Card response

MVP Agent Card MUST advertise the following capability flags explicitly:

```json
{
  "capabilities": {
    "streaming": false,
    "pushNotifications": false,
    "stateTransitionHistory": false
  },
  "authentication": {
    "schemes": []
  }
}
```

This tells third-party clients our limits up front and prevents silent fallback assumptions.

### 15. What A2A does NOT solve — explicit non-goals

- **Shared mutable state across agents** — A2A is message-passing, not a blackboard. Out of scope.
- **Token-budget attribution when delegating** — the downstream agent owns its own token budget. Out of scope.
- **Atomic state coordination (propose → validate → commit)** — distributed transaction semantics. Out of scope.

Add a "Non-goals" subsection to `README.md`.

### 16. Narrow seams for transport swap

Structure the plugin's `TaskStore` and `AgentExecutor` as interfaces/abstract classes so a future transport binding (NATS, gRPC) can plug in without forking the module. MVP only ships HTTP+JSON-RPC. Do not hard-code transport assumptions across the whole module — isolate them at the boundary.

```python
# Suggested interface boundary
class TaskStore(Protocol):
    def create(self, ...) -> A2ATask: ...
    def get(self, task_id: str) -> A2ATask | None: ...
    def update_status(self, task_id: str, status: A2ATaskStatus) -> None: ...

class AgentExecutor(Protocol):
    async def execute(self, task: A2ATask, prompt: str) -> str: ...
```

---

## Manual acceptance criteria

The MVP is good enough if all of the following are true (each criterion has a concrete check):

1. **Agent Card served** — `GET /api/plugins/a2a/<profile>/agent.json` returns 200 with a JSON body containing `name`, `description`, `url`, `capabilities`. Verify with `curl`.
2. **RPC envelope correctness** — `POST /api/plugins/a2a/<profile>/rpc` with `message/send` returns valid JSON-RPC 2.0 envelope (matching `jsonrpc`, `id`, exactly one of `result` or `error`). Verify with `curl + jq`.
3. **Real Hermes execution** — after a successful `message/send`, the target profile's session store (e.g. `~/.hermes/profiles/<profile>/sessions/`) contains a NEW session entry timestamped within the request window, and that session entry references the A2A caller metadata (`from_agent`, `conversation_id`). Evidence: `ls -lt` + `head` of the newest session file.
4. **Stable task ID + final status** — response includes `result.task.id` matching `^a2a_task_[0-9a-f-]+$`, and `result.task.status` is one of `completed` or `failed`. Verify by sending one request, capturing the id, and re-fetching via `GET /api/plugins/a2a/<profile>/tasks/<id>` — must return the same task with the same final status.
5. **Two-profile peer test (automated, not manual)** — promoted to automated E2E test in `tests/plugins/test_a2a_e2e_two_profiles.py`: spawn two test gateways with profiles `neo` and `trinity`, each on a separate localhost port; from neo's plugin, submit a task to trinity's RPC endpoint; assert the response artifact contains an output produced by trinity's real Hermes session (verifiable by checking that trinity's session log shows the inbound request).
6. **Disabled-by-default verified** — with `a2a.enabled=false` (the default), `curl /api/plugins/a2a/<any>/agent.json` returns 404 (NOT 403). Confirms route not mounted, not just blocked.
7. **Local-only verified** — with gateway bound to `0.0.0.0` and `a2a.require_localhost=true`, plugin refuses to mount and logs an explicit error. Additionally, with gateway bound to `127.0.0.1`, remote IP simulation (X-Forwarded-For or non-loopback request.client.host) returns -32003.
8. **Capability flags honest** — Agent Card JSON has `capabilities.streaming=false`, `capabilities.pushNotifications=false`, `capabilities.stateTransitionHistory=false`, `authenticationSchemes=[]`. Verify with `curl | jq .capabilities`.
9. **Error matrix smoke** — at least 4 error paths verified: profile-not-found (404), method-not-supported (-32601), body-too-large (413), duplicate-messageId (409).

If we hit all nine, the experiment is successful.

---

## Immediate follow-up after MVP succeeds

In priority order:

1. **Hermes sender tool** — add an `a2a_send` tool or equivalent helper so one Hermes agent can call another without curl.
2. **`input-required`** — enable real multi-turn back-and-forth between agents.
3. **SSE streaming** (`message/stream`) — for long-running tasks; required for full spec compliance and third-party interop.
4. **Persistent task DB** — SQLite-backed task table reusing `kanban_db.py` patterns; required for production interop (tasks survive restart).
5. **JWS Agent Card signing** — promote the no-op stub to real signing; additive, no client breakage.
6. **`AUTH_REQUIRED` handling** — promote the reserved state to real auth-challenge flow.
7. **NATS transport seam** — bind the `TaskStore`/`AgentExecutor` interfaces to a NATS or gRPC transport backend without forking the plugin.
8. **Agent Card canonical route support** — if plugin mounting blocked `/.well-known/agent.json` in MVP.
9. **Kanban bridge** — allow A2A task completion to materialize/advance Kanban tasks.
10. **Telegram/group routing integration** — optional human-visible operations layer.
11. **External interoperability check** — validate against a non-Hermes A2A client/server (LangChain, Vertex, MCP-A2A adapter).

---

## Candidate files likely to inspect while implementing

- `hermes_cli/plugins.py`
- `plugins/kanban/dashboard/plugin_api.py`
- `gateway/run.py`
- `gateway/session.py`
- `run_agent.py`
- API server request handling files under `gateway/` or adjacent runtime modules
- plugin examples with HTTP routes

---

## Suggested branch and commit protocol

Use a clean feature branch in the Hermes Agent repo.

Example:

```bash
cd ~/.hermes/hermes-agent
git checkout -b feat/a2a-plugin-mvp
```

Commit after each task. Keep the plugin experimental and self-contained until the first manual proof works.

---

## Final note

This is the right experiment.

Hermes already has profiles, gateway transport, plugin architecture, and the operational need. What it lacks is the protocol layer that lets one profile ask another profile a question without going through the human every time.

That is exactly what A2A is for.
