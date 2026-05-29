# a2a_fleet

A2A (Agent-to-Agent) protocol plugin for Hermes Agent. Enables Hermes profiles to discover, communicate with, and delegate tasks to each other over standard A2A/JSON-RPC — transforming independent Hermes instances into a coordinated fleet.

_(issue TBD)_

---

## 🔁 v0.1 Architecture Revision (2026-05-28)

**Embedded uvicorn, NOT dashboard router.** Earlier plan revisions assumed routes could mount on the Hermes dashboard gateway under `/api/plugins/a2a_fleet/...`. Verified at implementation time that the Hermes gateway is architecturally **localhost-only / dashboard-only**: session-token middleware (`web_server.py:236`), CORS regex restricted to `localhost|127.0.0.1` (line 104), and Host header validation (line 213) collectively block any cross-machine peer access. No existing plugin (`workflow-engine`, `kanban`, `memory`) exposes cross-machine endpoints — they all rely on the dashboard frontend supplying the ephemeral session token.

**Pivot:** a2a_fleet runs its own uvicorn instance on a dedicated A2A port, fully isolated from the dashboard gateway. The plugin owns its CORS, host validation, and bearer-auth surface. Hermes core gets zero patches. As a bonus, this restores spec-strict `/.well-known/agent-card.json` root-mount because the plugin owns its server's URL surface.

**Net effect on plan:**
- Drop `dashboard/manifest.json`, `dashboard/__init__.py`, `dashboard/dist/index.js`, `dashboard/plugin_api.py` (no longer a dashboard plugin)
- Add `server.py` — FastAPI app + embedded uvicorn lifecycle started by `register(ctx)`, stopped by `disable()`
- `fleet.yaml` gains `server.bind_host` (default `127.0.0.1`) and `server.bind_port` (required) fields
- Agent Card path is `/.well-known/agent-card.json` (root mount — plugin owns the server, RFC 8615 compliance restored)
- JSON-RPC endpoint is `POST /jsonrpc` (root mount — same reason)
- `client.py` peer URLs read `http://<bind_host>:<bind_port>` directly from fleet.yaml — no `/api/plugins/a2a_fleet/` prefix anywhere

The rest of the design (echo handler, securitySchemes, public Agent Card, sync Message reply, fleet.yaml unidirectional schema) is unchanged.

### ⚠️ Everything below this line is the PRE-PIVOT plan, retained for historical context only

The "Architecture", "Implementation Plan", "Step-by-step Implementation Order", "URL surface", "Acceptance Test", and "Files" sections below still reference:

- `/api/plugins/a2a_fleet/...` URLs (pre-pivot — NOT shipped)
- `dashboard/manifest.json`, `dashboard/plugin_api.py`, `dashboard/__init__.py`, `dashboard/dist/index.js` (pre-pivot — NOT shipped)
- `request.base_url` for self URL (pre-pivot — replaced by `bind_host:bind_port` from fleet.yaml)
- `/.well-known/` mount as a deferred-to-v0.2 item (post-pivot — now ACTIVE in v0.1)

**Authoritative v0.1 architecture** is the source tree itself (`server.py`, `__init__.py`, `fleet_config.py`, `client.py`, `fleet_tools.py`, `response_handler.py`) plus the as-built record in `PROGRESS.md`. Treat all paths and file references below as the original planning document, not the shipped contract.

A clean v0.2 README rewrite is planned alongside TaskManager work; v0.1 keeps this audit trail intact rather than retroactively rewriting it.

---

## Why this exists

Hermes profiles run as independent gateways with their own config, tools, sessions, and SOULs. Switch Agent on Mac Mini can SSH into the Construct on Unraid and run CLI commands, but this is one-way command execution — no conversation, no streaming, no task state tracking, no multi-turn negotiation.

A2A solves this properly:

```
Before (SSH):    Switch → ssh: `hermes cron list` → stdout (dead text)
After (A2A/SSE): Switch → "check your crons and fix failures" → Construct thinks, replies, streams progress
```

The A2A protocol (v1.0, Apache-2.0) is purpose-built for opaque agent-to-agent communication over HTTP/JSON-RPC with SSE streaming. It needs no access to internal state, memory, or tools — agents collaborate purely through declared capabilities.

This plugin wraps each Hermes profile as an A2A server, publishes an Agent Card route under the plugin prefix, and exposes JSON-RPC endpoints for task dispatch, streaming, and push notifications. A fleet registry (initially static config, eventually dynamic) lets the orchestrator discover and route to remote agents.

---

## Architecture

> **Diagram note:** The architecture diagrams below show the full target architecture. v0.1 ships Agent Card + JSON-RPC `SendMessage` only. TaskManager and SSE are deferred to v0.2+.

```
┌─────────────────────────────────────────────────────────┐
│                   Your Chat (Telegram / CLI)              │
│              You talk to Switch Agent (T1)               │
└──────────────────────────┬──────────────────────────────┘
                           │
                   ┌───────▼───────┐
                   │  Switch Agent  │    ← You are here (Mac Mini)
                   │  (hermes-switch│
                   │   profile)     │
                   └───┬───┬───┬───┘
                       │   │   │
          ┌────────────┘   │   └────────────┐
          ▼                ▼                ▼
    ┌──────────┐   ┌──────────┐   ┌──────────────┐
    │Construct  │   │ Linux PC │   │  Any Hermes  │
    │ (Docker)  │   │  Hermes  │   │  instance    │
    └──────────┘   └──────────┘   └──────────────┘
         ▲               ▲               ▲
         └───────────────┴───────────────┘
              A2A over JSON-RPC + SSE
        (each agent exposes A2A endpoints)
```

Each Hermes profile runs as an independent A2A server. The orchestrator (Switch Agent, on Mac Mini) maintains a fleet registry of known remote agents and dispatches tasks via the A2A client SDK. Remote agents process tasks in their own Hermes sessions and stream results back.

### Plugin placement in Hermes

The locked v0.1 shape is **dual-surface**, not gateway-only:

- **Gateway/dashboard surface**: `dashboard/manifest.json` + `dashboard/plugin_api.py` expose HTTP routes through `_discover_dashboard_plugins()` and `_mount_plugin_api_routes()` (`hermes_cli/web_server.py:3926-3997`, `4340-4378`).
- **Agent plugin surface**: `plugin.yaml` + `__init__.py:register(ctx)` expose `fleet_send(...)` through the normal Hermes plugin loader (`hermes_cli/plugins.py:19-20`, `1143-1167`).

`workflow-engine` is the in-repo precedent for this split: its dashboard router is auto-mounted by `web_server.py`, while its `register(ctx)` separately adds agent tools (`plugins/workflow-engine/__init__.py:1-71`).

```
┌──────────────────────────────────────┐
│          Hermes Gateway              │
│                                      │
│  ┌────────────────────────────┐     │
│  │  a2a_fleet plugin (FastAPI) │     │
│  │                            │     │
│  │  GET  agent-card.json      │     │
│  │  POST /jsonrpc             │     │
│  │  GET  /sse/{task_id}       │     │
│  │  tasks.* methods           │     │
│  └────────────┬───────────────┘     │
│               │                     │
│  ┌────────────▼───────────────┐     │
│  │    TaskManager              │     │
│  │    - spawns agent sessions  │     │
│  │    - tracks task lifecycle  │     │
│  │    - bridges SSE output     │     │
│  └────────────────────────────┘     │
│                                      │
│  ┌────────────────────────────┐     │
│  │  Fleet Registry (config)    │     │
│  │  - known remote agents      │     │
│  │  - cached Agent Cards       │     │
│  │  - auth credentials         │     │
│  └────────────────────────────┘     │
└──────────────────────────────────────┘
```

### Why FastAPI on the gateway, plus an agent tool

The A2A protocol requires:
- A persistent HTTP server with SSE support (streaming task updates)
- Long-lived connections that survive agent restarts
- Agent Card discovery endpoint clients can fetch before sending JSON-RPC

Inbound A2A server behavior does not fit the agent-tool model by itself, so the HTTP server side belongs on the gateway router. But outbound orchestration still benefits from an agent-visible tool. So v0.1 uses:

- gateway routes for `/api/plugins/a2a_fleet/agent-card.json` and `/api/plugins/a2a_fleet/jsonrpc`
- agent tooling for `fleet_send(agent, message)`

That split matches current Hermes internals instead of fighting them.

---

## A2A Protocol Mapping (v1.0)

The A2A v1.0 spec defines three layers. Here's how each maps to Hermes:

### Layer 1: Data Model

| A2A Concept | Hermes Mapping |
|---|---|
| **AgentCard** | One per profile. Generated from profile config + SOUL.md + tool manifests. Served at `/api/plugins/a2a_fleet/agent-card.json` (v0.1 — fleet-only interop; spec-strict `/.well-known/` mount deferred to v0.2+). |
| **Task** | Maps to a Hermes agent session. Task `id` → session ID. Task state tracked in TaskManager. |
| **Message** | A2A Message with `role` ("user" for incoming, "agent" for response). Parts contain the actual content. |
| **Part** | Text parts for prompts, Data parts for structured results, File parts for artifacts. |
| **Artifact** | Hermes tool outputs, file writes, generated content. Returned as task artifacts. |
| **TaskState** | Mirrors A2A lifecycle: `submitted` → `working` → `completed` / `failed` / `canceled` / `input-required` / `auth-required`. |

#### Task Lifecycle

```
                    ┌─────────────┐
                    │  submitted   │  ← Client sends SendMessage
                    └──────┬──────┘
                           │
                    ┌──────▼──────┐
                    │   working    │  ← Agent session spawned, processing
                    └──────┬──────┘
                           │
          ┌────────────────┼────────────────┐
          │                │                │
   ┌──────▼──────┐  ┌──────▼──────┐  ┌──────▼──────┐
   │  completed   │  │   failed    │  │  canceled   │  ← Terminal states
   └─────────────┘  └─────────────┘  └─────────────┘
          │
   (artifacts available)
          │
   ┌──────▼──────┐
   │input-required│  ← Non-terminal. Agent needs clarification.
   └──────┬──────┘   Client sends another message. Task resumes.
          │
   ┌──────▼──────┐
   │auth-required │  ← Non-terminal. Agent needs credentials.
   └─────────────┘   Typically resolved out-of-band.
```

#### Agent Card Schema (per profile)

```json
{
  "name": "Switch Agent",
  "description": "Hermes T1 orchestrator — routes tasks to Neo, Morpheus, or Trinity",
  "url": "https://hermes-macmini.local:9119",
  "provider": {
    "organization": "Interstellar Consulting GmbH",
    "url": "https://interstellarconsulting.com"
  },
  "version": "1.0.0",
  "capabilities": {
    "streaming": true,
    "pushNotifications": false,
    "stateTransitionHistory": true
  },
  "defaultInputModes": ["text", "text/plain"],
  "defaultOutputModes": ["text", "text/plain", "application/json"],
  "securitySchemes": {
    "bearerAuth": {
      "type": "http",
      "scheme": "bearer",
      "description": "Pre-shared bearer token; clients supply the token configured via fleet.yaml token_env."
    }
  },
  "security": [{"bearerAuth": []}],
  "skills": [
    {
      "id": "task-routing",
      "name": "Task Routing & Orchestration",
      "description": "Routes incoming tasks to the right Hermes tier-2 specialist",
      "tags": ["orchestration", "routing", "delegation"],
      "examples": ["Route this bug report to Neo", "Check all agent statuses"]
    },
    {
      "id": "status-reporting",
      "name": "Fleet Status Reporting",
      "description": "Reports health and status of all fleet agents",
      "tags": ["monitoring", "status"],
      "examples": ["Are all agents healthy?", "What's Neo working on?"]
    }
  ]
}
```

### Layer 2: Abstract Operations

| A2A Operation | Hermes Implementation |
|---|---|
| **Send Message** | `POST /api/plugins/a2a_fleet/jsonrpc` with method `SendMessage`. v0.1 runs the echo handler and returns an immediate Message result. |
| **Send Streaming Message** | Same endpoint, but returns SSE stream. Deferred in v0.1. |
| **Get Task** | `tasks.get` — queries TaskManager for task state, artifacts, and history. |
| **List Tasks** | `tasks.list` — queries TaskManager for tasks filtered by contextId, state, or recency. |
| **Cancel Task** | `tasks.cancel` — interrupts the Hermes agent session, transitions task to canceled. |
| **Get Agent Card** | `GET /api/plugins/a2a_fleet/agent-card.json` — returns the profile's Agent Card (cached, regenerated on profile config change). Public endpoint, no auth required. |
| **Subscribe to Task** | `GET /api/plugins/a2a_fleet/sse/{task_id}` — SSE stream for an existing task's updates. Stubbed in v0.1. |

### Layer 3: Protocol Bindings

The plugin implements **JSON-RPC 2.0 over HTTP** as the primary binding:

- **Endpoint:** `POST /api/plugins/a2a_fleet/jsonrpc`
- **Content-Type:** `application/json`
- **SSE streaming:** `GET /api/plugins/a2a_fleet/sse/{task_id}` (or returned via `SendStreamingMessage`)
- **Agent Card:** `GET /api/plugins/a2a_fleet/agent-card.json` (public, no auth)

REST binding (`GET /tasks`, `POST /tasks`, etc.) is a follow-up — JSON-RPC is simpler and the A2A Python SDK speaks it natively.

---

## Fleet Model

### Static Config (Phase 1)

Fleet members are defined in a standalone `fleet.yaml`. The schema is **unidirectional**: each agent lists only the peers it **initiates calls to** in the `agents:` block. The inbound side only needs a `server.token_env` declaration — it does NOT need to list its callers. Tokens are still validated on receive; the peer list purely drives outbound routing.

```yaml
# ~/.hermes/profiles/hermes-switch/fleet.yaml  (standalone file)
fleet:
  enabled: true
  self:
    name: "switch"
    # url is auto-detected from the running gateway's bind host + port — do not set.
  server:
    auth_required: false    # v0.1 default: permissive for dev. Set true in prod.
    token_env: "SWITCH_A2A_TOKEN"
  response_handler: echo    # v0.1 only supports "echo". Any other value fails at startup.
  agents:
    construct:
      url: "http://192.168.0.200:8642"
      agent_card_url: "http://192.168.0.200:8642/api/plugins/a2a_fleet/agent-card.json"
      token_env: "CONSTRUCT_A2A_TOKEN"
      description: "Docker-based Hermes on Unraid — runs Dinesh/Brain, Switch UI, Hindsight"

    linux-pc:
      url: "http://192.168.0.50:9119"
      agent_card_url: "http://192.168.0.50:9119/api/plugins/a2a_fleet/agent-card.json"
      token_env: "LINUXPC_A2A_TOKEN"
      description: "Hermes on Linux desktop — general purpose"
```

**Unidirectional schema example:** `switch` lists `construct` in its `agents:` block because switch initiates calls to construct. Construct's `fleet.yaml` does NOT need to list switch — it only needs a `server.token_env` block so it can validate inbound bearer tokens:

```yaml
# ~/.hermes/profiles/construct/fleet.yaml  (inbound-only — no agents: block needed)
fleet:
  enabled: true
  self:
    name: "construct"
  server:
    auth_required: false    # v0.1 default
    token_env: "CONSTRUCT_A2A_TOKEN"
  response_handler: echo
  # agents: block omitted — construct does not initiate calls to anyone
```

**Token symmetry:** `switch`'s outbound `token_env: CONSTRUCT_A2A_TOKEN` must contain the same value as `construct`'s inbound `server.token_env: CONSTRUCT_A2A_TOKEN`. Both sides must export `CONSTRUCT_A2A_TOKEN=<shared-secret>`.

Each entry caches the remote agent's Agent Card on first contact. v0.1 clients fetch from the configured `agent_card_url`, **not** from a well-known root path — fleet members read the URL directly from `fleet.yaml`. The orchestrator uses these to route tasks.

### Dynamic Discovery (Phase 2)

- Support `agent_card_url` scanning on configured domains (spec-strict `/.well-known/agent-card.json` discovery deferred to v0.2+ alongside upstream root-mount patch)
- Optional registry service (file-based or lightweight HTTP)
- Auto-populate fleet from discovered agents

### Fleet Tool (agent-facing)

Once the plugin is active, the orchestrator agent gets a `fleet` toolset:

```
fleet_send(agent="construct", message="search memory for Project Uno invoices")
fleet_status(agent="construct")             → task list, health
fleet_status(all=true)                      → all agents
fleet_get_agent_card(agent="construct")     → cached Agent Card
fleet_discover(url="http://192.168.0.50:9119") → fetch + register new agent
```

These appear as regular Hermes tools in the agent's tool list — no different from terminal or web_search. The agent dispatches via A2A and gets structured results back.

---

## TaskManager — Bridging A2A Tasks to Hermes Sessions

This is the core of the plugin. For each incoming A2A task:

1. **Receive** `SendMessage` request → extract `messageId`, `contextId`, parts
2. **Resolve** which Hermes profile handles this (from the endpoint or routing config)
3. **Create** a task record in the TaskManager with state `submitted`
4. **Spawn** a Hermes agent session:
   - Build a user message from the A2A Message parts
   - Run `run_conversation()` in a background thread/task
   - The agent has its full tool set (terminal, web, files, etc.)
5. **Stream** agent output as A2A events:
   - Tool calls → TaskStatusUpdate messages
   - Final response → Task with state `completed` and artifacts
   - Errors → Task with state `failed` and error details
6. **Handle** `input-required` — when the agent calls `clarify()`, map to A2A `input-required` task state. Client sends a follow-up message, task resumes.
7. **Handle** cancellation — interrupt the Hermes session, mark task `canceled`.

### State persistence

Tasks are ephemeral by default (in-memory). For production:

- **Lightweight:** SQLite (same as Hermes session DB)
- **Production:** The TaskManager can persist to the Kanban DB or its own SQLite file
- **Cleanup:** Completed/failed tasks older than N hours are pruned

### Concurrency

The Hermes gateway already handles concurrent sessions. Multiple A2A tasks run in parallel — each gets its own agent session. TaskManager enforces per-task isolation.

---

## Authentication

**Public Agent Card endpoint:** `GET /api/plugins/a2a_fleet/agent-card.json` is **public** — no bearer token required. Only the JSON-RPC endpoint (`POST /api/plugins/a2a_fleet/jsonrpc`) enforces bearer auth. Capability discovery must remain anonymous so clients can read the card before they have credentials.

### Between agents

| Auth Type | Use Case | Implementation |
|---|---|---|
| **None** | Local agents (same machine, localhost) | Skip auth entirely |
| **Bearer Token** | LAN agents (Unraid, Linux PC) | Pre-shared token in Hermes `.env`, passed as `Authorization: Bearer <token>` header |
| **mTLS** | Cross-network / enterprise | Future — Hermes gateway already has TLS support |
| **OAuth2** | Third-party agents | Future — per A2A spec |

### Client → Server flow

```
1. Client reads fleet config → knows agent URL, agent_card_url, and auth type
2. Client fetches Agent Card: GET {agent_card_url} (PUBLIC — no auth required)
3. Client includes auth header in all subsequent JSON-RPC calls
4. Server Gateway validates token → routes to TaskManager → spawns agent session
```

### Token management

Tokens are stored in `~/.hermes/.env` and referenced by env var name in fleet config:

```bash
# ~/.hermes/.env
CONSTRUCT_A2A_TOKEN=hs256_abc123def456
LINUXPC_A2A_TOKEN=hs256_ghi789jkl012
```

Each remote agent generates its own token via `hermes fleet token-generate` (or manual entry). Tokens are opaque bearer strings — no JWT required for MVP.

---

## Implementation Plan

### v0.1 Goal (one paragraph)

Ship the smallest plugin that lets **two installed A2A-fleet plugins exchange a message and get a reply**. A profile starts the gateway, exposes a public Agent Card at `/api/plugins/a2a_fleet/agent-card.json` (under the standard plugin prefix — no Hermes core patch required for v0.1), accepts a JSON-RPC `SendMessage` at `/api/plugins/a2a_fleet/jsonrpc`, runs it through a **pluggable response handler** (v0.1 ships **echo only**: `ping → pong`, anything else is echoed back), and returns the reply synchronously as an immediate A2A `Message` result. The orchestrator side gets a single agent-facing tool, `fleet_send(agent, message)`, registered via `ctx.register_tool` in the plugin's `register(ctx)` entry point. It reads the fleet from a standalone `fleet.yaml` (including each peer's configured `agent_card_url`), attaches a bearer token, and POSTs to the remote `/api/plugins/a2a_fleet/jsonrpc`. No Hermes session spawning, no LLM, no SSE, no TaskManager, no persistence. `response_handler.py` ships a single plain async function (`echo_handler`) — no Protocol class abstraction in v0.1. v0.2 adds a second handler and introduces the interface then.

### v0.1 Scope vs. Deferred

| Concern | v0.1 | Deferred |
|---|---|---|
| Agent Card discovery (`/api/plugins/a2a_fleet/agent-card.json`, public) | ✅ ship | spec-strict `/.well-known/` root mount → v0.2+ |
| JSON-RPC `SendMessage` (synchronous reply) | ✅ ship | — |
| Pluggable response handler (echo only) | ✅ ship | swap for TaskManager in v0.2 |
| LLM-backed response handler | ❌ defer | v0.2+ (reads existing Hermes LLM config) |
| Fleet yaml config loader (standalone `fleet.yaml`) | ✅ ship | dynamic discovery in v0.2 |
| `self.url` auto-detect from gateway bind host+port | ✅ ship | — |
| `fleet_send(agent, message)` tool | ✅ ship | `fleet_status`, `fleet_discover` in v0.2 |
| Bearer-token auth | ✅ ship | mTLS / OAuth2 later |
| `tasks.get` / `tasks.list` / `tasks.cancel` | ⚠️ stub (return "not implemented") | full impl in v0.2 |
| SSE `/sse/{task_id}` | ⚠️ stub (returns 501) | v0.3 |
| TaskManager spawning Hermes sessions | ❌ defer | v0.2 |
| `input-required` loops | ❌ defer | v0.2 |
| Task persistence / SQLite | ❌ defer | v0.4 |
| Push notifications | ❌ defer | v0.3 |

### v0.1 File List

**Ships (new or rewritten):**

| Path | Purpose | Approx LoC |
|---|---|---|
| `dashboard/manifest.json` | Hermes dashboard plugin manifest (the file `_discover_dashboard_plugins()` actually reads). Fields: `name`, `label`, `description`, `version`, `api: "plugin_api.py"`, `tab: {"hidden": true}`, and an explicit `entry` pointing at a tiny dashboard stub bundle. No `root_routes` — Agent Card lives under the standard plugin prefix. | 20 |
| `dashboard/__init__.py` | Empty file — required for Python package resolution (all shipped plugins include it). | 0 |
| `dashboard/dist/index.js` | Minimal no-op dashboard bundle. Recommended to suppress a `LOAD_FAILED` console warning when the web UI fetches the plugin entry script (`web/src/plugins/usePlugins.ts:43-95`). Not strictly required for backend routes — `workflow-engine` ships without it — but include for hygiene. | 10 |
| `plugin.yaml` | Root plugin manifest required by the Hermes plugin loader for `register(ctx)` discovery (`hermes_cli/plugins.py:19-20`). **Rename from `manifest.yaml`.** Strip the `api:` field — it is meaningless after the rename; dashboard router mounts via `dashboard/manifest.json`'s `api:` field. Keep only: `name`, `version`, `description`, `author`. | 10 |
| `__init__.py` | Plugin entry point — `register(ctx)` uses **lazy imports** (heavy deps inside function body, not module-top) and calls `ctx.register_tool(...)` to expose `fleet_send`. Includes a no-op `disable()` hook called by plugin loader on hot-reload/shutdown. | 40 |
| `dashboard/plugin_api.py` | FastAPI router — public `GET agent-card.json` (card dict built inline as a helper function — **no separate `agent_card.py` module**), `POST /jsonrpc` (`SendMessage`) under `/api/plugins/a2a_fleet/`, stub task endpoints, bearer auth dependency applied **only** to JSON-RPC route. Route handlers use `body = await request.json()` + manual extraction with explicit JSON-RPC error responses (no Pydantic request models — see workflow-engine precedent). Resolves `self.url` per-request via `request.base_url`. | ~300 (absorbs agent_card logic) |
| `fleet_config.py` | Loads standalone `~/.hermes/profiles/<profile>/fleet.yaml`, resolves `token_env` vars, returns peer agents. `self.url` not cached here — resolved per-request. Validates `response_handler`: any value other than `"echo"` raises `ValueError: response_handler 'X' not supported in v0.1, only 'echo' is implemented.` (fail fast at startup). Pydantic fine for internal data classes. | 100 |
| `response_handler.py` | `async def echo_handler(text: str, context_id: str) -> str` — returns `"pong"` for input `"ping"`, otherwise echoes input verbatim. Plain function; no `ResponseHandler` Protocol class in v0.1 (one handler, no abstraction needed yet — add in v0.2 when a second handler exists). | 20 |
| `client.py` | Minimal A2A client: `send_message(agent_name, text) → reply_text`. Uses `httpx`, not `a2a-sdk`. Includes `if __name__ == "__main__"` block for CLI invocation. | 80 |
| `fleet_tools.py` | `fleet_send(agent, message)` handler wiring, called from `__init__.py:register(ctx)`. | 50 |
| `tests/test_agent_card.py` | Agent Card schema validation + public access (no auth header). `TestClient` — no live HTTP. | ~15 |
| `tests/test_jsonrpc_echo.py` | JSON-RPC `SendMessage` echo + 401 on wrong/missing token. `TestClient`. | ~20 |
| `tests/test_client.py` | Client envelope parsing — mock httpx response, verify reply extraction. | ~15 |
| `tests/test_fleet_config.py` | fleet.yaml parsing with env-var token resolution and `agent_card_url` field. | ~15 |
| `tests/test_dashboard_stub.py` | Dashboard plugin listing smoke test — confirms `a2a_fleet` present with `tab.hidden: true`. | ~15 |
| `references/a2a-spec-v1-summary.md` | Dev quick-reference cheatsheet. See [A2A v1.0 spec](https://a2a-protocol.org/latest/specification/) for the authoritative spec — local file is convenience only. | — |

**Total v0.1 LoC budget:** ~560–610 (reduced from prior estimate; `ResponseHandler` Protocol class and separate `agent_card.py` module removed, saving ~50–80 LoC).

**Stubbed (present but return 501 / "not implemented"):**

- `tasks.get`, `tasks.list`, `tasks.cancel` JSON-RPC methods
- `GET /sse/{task_id}` endpoint
- `SendStreamingMessage` JSON-RPC method

**Deferred (do not create the file in v0.1):**

- `task_manager.py` — v0.2
- `discovery.py` — v0.2
- Any persistence layer — v0.4

### URL surface for the plugin

| Route | Final URL | Auth |
|---|---|---|
| Agent Card | `/api/plugins/a2a_fleet/agent-card.json` | **public — no bearer required** |
| JSON-RPC | `/api/plugins/a2a_fleet/jsonrpc` | bearer required (configurable) |
| SSE stub | `/api/plugins/a2a_fleet/sse/{task_id}` | bearer required |

All routes mount under the standard Hermes plugin prefix — v0.1 ships **no Hermes core patch**. Spec-strict `/.well-known/agent-card.json` root mount is deferred to v0.2+ (see Non-Goals).

### Step-by-step Implementation Order

Each step ends with a **verifiable success criterion**. Do them in order; do not start the next step until the previous one passes.

**Step 0 — Dashboard manifest + agent-plugin manifest split** (~30 LoC, new/renamed files)
- Create `plugins/a2a_fleet/dashboard/manifest.json` with: `name`, `label`, `description`, `version`, `api: "plugin_api.py"`, `tab: {"hidden": true}`, and an explicit `entry` pointing at a tiny dashboard stub bundle. **No `root_routes` field** — all routes mount under the standard plugin prefix.
- Create `plugins/a2a_fleet/dashboard/__init__.py` as an empty file (required for Python package resolution).
- Create `plugins/a2a_fleet/dashboard/dist/index.js` as a minimal no-op stub bundle. Recommended to suppress a `LOAD_FAILED` console warning — not strictly required for backend routes, but include for hygiene (`workflow-engine` omits it and gets a non-fatal `LOAD_FAILED` log in the browser console).
- **Rename `plugins/a2a_fleet/manifest.yaml` to `plugins/a2a_fleet/plugin.yaml`**. Strip the `api:` field from the renamed file — it is meaningless after the rename; the dashboard router mounts via `dashboard/manifest.json`'s `api:` field. The final `plugin.yaml` should contain only `name`, `version`, `description`, `author`. Example:
  ```yaml
  name: a2a_fleet
  version: 0.1.0
  description: "A2A fleet plugin — agent-to-agent communication over JSON-RPC"
  author: Interstellar Consulting GmbH
  ```
- ✅ Success: `_discover_dashboard_plugins()` lists `a2a_fleet`, `_mount_plugin_api_routes()` mounts `/api/plugins/a2a_fleet/agent-card.json` and `/api/plugins/a2a_fleet/jsonrpc`, and Hermes agent-plugin discovery loads `a2a_fleet.__init__.py` without a missing-manifest failure. `fleet_config.py` reads `response_handler` and raises `ValueError` at startup if the value is not `"echo"`.

**Step 1 — Fleet config loader** (~100 LoC, `fleet_config.py`)
- Load standalone `fleet.yaml` from `~/.hermes/profiles/<profile>/fleet.yaml`. (Not embedded in `config.yaml`; the plugin reads `fleet.yaml` directly.)
- **Do not** cache `self.url` at import time. The plugin api module is imported by `_mount_plugin_api_routes()` (line 4384) *before* `mount_spa(app)` finishes binding the gateway, so any host/port read at import is unreliable. Resolve `self.url` per-request from `request.base_url` inside the Agent Card route instead.
- Resolve `token_env` → real bearer token from environment.
- Expose `load_fleet() → {self: {name, token}, agents: {name → {url, agent_card_url, token, description}}}`.
- Validate `response_handler` field on load: if value is anything other than `"echo"`, raise `ValueError: response_handler 'X' not supported in v0.1, only 'echo' is implemented.` — fail fast at startup, not at first request.
- ✅ Success: `python -c "from a2a_fleet.fleet_config import load_fleet; print(load_fleet())"` (run from `plugins/` with `HERMES_HOME=~/.hermes`) prints the agent map with tokens resolved, includes each peer's `agent_card_url`, and omits any cached `self.url` field (URL is per-request). Setting `response_handler: llm` raises `ValueError` immediately.

**Step 2 — Agent Card (public endpoint)** (~80 LoC, edit `dashboard/plugin_api.py`)
- Implement `_build_agent_card(request)` **inline in `plugin_api.py`** (no separate `agent_card.py` module — ~30 lines inline). Pull `name`, `description` from fleet config and derive `url` from `request.base_url`. v0.1 uses raw `request.base_url`; reverse-proxy handling (honoring `X-Forwarded-Proto`/`X-Forwarded-Host`) is deferred to v0.2 (requires uvicorn `--forwarded-allow-ips` or FastAPI `ProxyHeadersMiddleware`).
- Emit A2A v1.0 `securitySchemes` per OpenAPI alignment:
  ```json
  "securitySchemes": {
    "bearerAuth": {
      "type": "http",
      "scheme": "bearer",
      "description": "Pre-shared bearer token; clients supply the token configured via fleet.yaml token_env."
    }
  },
  "security": [{"bearerAuth": []}]
  ```
- Declare `GET agent-card.json` in the router so it resolves under `/api/plugins/a2a_fleet/agent-card.json` via the standard plugin prefix.
- **This route is PUBLIC** — the handler does not check the `Authorization` header. Capability discovery must work anonymously so clients can read the card before they have credentials. Bearer auth lives on the JSON-RPC route (Step 3), not here.
- ✅ Success: `curl http://localhost:9119/api/plugins/a2a_fleet/agent-card.json` (no auth header) returns a valid card with the `securitySchemes.bearerAuth` block and the request-derived URL.

**Step 3 — JSON-RPC `SendMessage` + bearer middleware + response handler** (~110 LoC, `response_handler.py` + edit `dashboard/plugin_api.py`)
- In `response_handler.py`, implement a plain async function: `async def echo_handler(text: str, context_id: str) -> str` — returns `"pong"` for input `"ping"`, otherwise echoes input verbatim. No `ResponseHandler` Protocol class in v0.1 (add in v0.2 when a second handler exists).
- In `plugin_api.py`, route handlers must use `body = await request.json()` + manual field extraction with explicit JSON-RPC error responses on bad input. **Do not use Pydantic request models in route signatures** — see workflow-engine precedent (`web_server.py:4361-4367` forward-ref complexity). Pydantic is fine for internal data classes (`FleetConfig`, etc.) that do not bind to FastAPI route signatures.
- Add `verify_bearer` dependency on the JSON-RPC route only (`POST /jsonrpc`); reject non-matching `Authorization: Bearer <token>` with HTTP 401 (pre-RPC, before envelope is parsed). Skip auth when `auth_required: false`. **Do not** apply this dependency to the Agent Card route (Step 2) — it stays public.
- Replace `_handle_send_message` TODO with: extract text from `body["params"]["message"]["parts"][0]["text"]`, call `await echo_handler(text, context_id)`, return an A2A **immediate Message result**: `{"jsonrpc":"2.0","id":...,"result":{"kind":"message","message":{"role":"agent","parts":[{"text":"..."}]}}}` (see [A2A v1.0 spec](https://a2a-protocol.org/latest/specification/) — local cheatsheet: `references/a2a-spec-v1-summary.md`). v0.1 picks `Message` because no real session is spawned; v0.2 switches to async `Task` once TaskManager lands.
- Stub error table for this step:

  | Condition | HTTP status | JSON-RPC error code |
  |---|---|---|
  | Missing/wrong bearer token | 401 | — (pre-RPC rejection) |
  | Malformed JSON body | 200 | `-32700` (Parse error) |
  | Unknown method (`tasks.get`, `tasks.list`, `tasks.cancel`, `SendStreamingMessage`) | 200 | `-32601` (Method not found) |
  | SSE `GET /sse/{task_id}` | 501 | — (plain HTTP, not JSON-RPC) |

- ✅ Success: with a valid bearer header, curl roundtrip ("Hello fleet" test) returns `pong` from `/api/plugins/a2a_fleet/jsonrpc`. Wrong/missing token returns HTTP 401. Malformed JSON returns JSON-RPC `-32700`. Unknown method returns JSON-RPC `-32601`. Agent Card route still returns 200 with no auth header.

**Step 4 — Minimal A2A client** (~80 LoC, `client.py`)
- `async def send_message(agent_name: str, text: str) → str`: look up agent in fleet config, read the peer's `url` / `agent_card_url`, POST JSON-RPC `SendMessage` with bearer header, extract `result.message.parts[0].text` from response.
- Use `httpx.AsyncClient(timeout=30)`. No `a2a-sdk` in v0.1.
- Include `if __name__ == "__main__":` block so the module is directly runnable for manual testing.
- ✅ Success: with two profiles running locally on different ports, run from `plugins/` directory:
  ```bash
  cd /path/to/hermes-agent/plugins && HERMES_HOME=~/.hermes python -m a2a_fleet.client construct ping
  ```
  Prints `pong`. (Direct `python -m a2a_fleet.client` from the repo root fails — `plugins/` is not on `sys.path`. Always run from `plugins/` or use the curl Test 2 command instead.)

**Step 5 — `fleet_send` agent tool via `ctx.register_tool`** (~50 LoC, `fleet_tools.py` + wire from `__init__.py`)
- In `__init__.py:register(ctx)`, use **lazy imports** — import `fleet_tools`, `fleet_config`, and `httpx` inside the `register(ctx)` function body, not at module top. This keeps module load cheap (matches workflow-engine reference pattern).
- Call `ctx.register_tool` with full kwargs — missing `is_async=True` causes a silent event-loop hang:
  ```python
  ctx.register_tool(
      name="fleet_send",
      toolset="a2a",
      schema={...},
      handler=fleet_send_handler,
      check_fn=None,
      is_async=True,
      description="Send a message to a fleet peer agent via A2A.",
      emoji="🤝",
  )
  ```
- Also add a no-op `disable()` function at module level in `__init__.py` (plugin loader calls it on hot-reload/shutdown).
- Tool signature: `fleet_send(agent: str, message: str) -> dict` wrapping `client.send_message`, returns `{"reply": "..."}` (or `{"error": "..."}`). Surface clear error strings on auth failure / unreachable host — do not raise.
- ✅ Success: in a Hermes chat on profile A, `fleet_send(agent="construct", message="ping")` returns `{"reply": "pong"}`.

### "Hello fleet" Acceptance Test

**Setup:** two profiles on the same machine, different ports.

```yaml
# ~/.hermes/profiles/switch/fleet.yaml  (standalone file, NOT embedded in config.yaml)
fleet:
  enabled: true
  self:
    name: "switch"
    # url is auto-detected from the running gateway's bind host + port — do not set.
  server:
    auth_required: false    # v0.1 default. Set true for production.
    token_env: "SWITCH_A2A_TOKEN"
  response_handler: echo   # v0.1 only supports "echo"
  agents:
    construct:
      url: "http://localhost:9120"
      agent_card_url: "http://localhost:9120/api/plugins/a2a_fleet/agent-card.json"
      token_env: "CONSTRUCT_A2A_TOKEN"
      description: "Test peer"
```

```bash
export SWITCH_A2A_TOKEN=dev-switch-token
export CONSTRUCT_A2A_TOKEN=dev-construct-token
# Start both gateways as separate Hermes profiles on separate ports.
# Hermes supports `--profile` before command dispatch (`hermes_cli/main.py:120-133`)
# and exposes `hermes dashboard` / `hermes dashboard --port <port>` in the CLI
# help surface (`hermes_cli/_parser.py:73-75`, `hermes_cli/main.py:6250-6252`).
hermes --profile switch dashboard --port 9119
hermes --profile construct dashboard --port 9120
```

#### Preflight — Create profiles and export tokens

Before running any test, set up two Hermes profiles and export the shared bearer tokens:

```bash
# 1. Create profiles (if not already created)
hermes profile create switch
hermes profile create construct

# 2. Enable the plugin for each profile
hermes --profile switch plugins enable a2a_fleet
hermes --profile construct plugins enable a2a_fleet

# 3. Drop fleet.yaml for each profile
# switch initiates calls to construct — lists it in agents:
cat > ~/.hermes/profiles/switch/fleet.yaml << 'EOF'
fleet:
  enabled: true
  self:
    name: "switch"
  server:
    auth_required: false
    token_env: "SWITCH_A2A_TOKEN"
  response_handler: echo
  agents:
    construct:
      url: "http://localhost:9120"
      agent_card_url: "http://localhost:9120/api/plugins/a2a_fleet/agent-card.json"
      token_env: "CONSTRUCT_A2A_TOKEN"
      description: "Test peer"
EOF

# construct only needs a server block — no agents: list needed
cat > ~/.hermes/profiles/construct/fleet.yaml << 'EOF'
fleet:
  enabled: true
  self:
    name: "construct"
  server:
    auth_required: false
    token_env: "CONSTRUCT_A2A_TOKEN"
  response_handler: echo
EOF

# 4. Export tokens — both sides must export CONSTRUCT_A2A_TOKEN with the same value
export SWITCH_A2A_TOKEN=dev-switch-token
export CONSTRUCT_A2A_TOKEN=dev-construct-token

# 5. Start both gateways on separate ports (run in separate terminals)
hermes --profile switch dashboard --port 9119
hermes --profile construct dashboard --port 9120
```

**Token symmetry:** switch's outbound `token_env: CONSTRUCT_A2A_TOKEN` and construct's inbound `server.token_env: CONSTRUCT_A2A_TOKEN` must resolve to the same value in the environment.

**Test 1 — Agent Card discovery (PUBLIC — no auth header):**
```bash
curl -s http://localhost:9120/api/plugins/a2a_fleet/agent-card.json | jq .name
# Expected: "construct"
# NOTE: v0.1 mounts the Agent Card under the standard plugin prefix and serves it
# PUBLICLY (no Authorization header required). Spec-strict /.well-known/agent-card.json
# root mount is deferred to v0.2+ — see Non-Goals.
```

**Test 2 — JSON-RPC SendMessage (the canonical ping→pong, under the plugin prefix):**
```bash
curl -s -X POST http://localhost:9120/api/plugins/a2a_fleet/jsonrpc \
  -H "Authorization: Bearer dev-construct-token" \
  -H "Content-Type: application/json" \
  -d '{
    "jsonrpc": "2.0",
    "id": "1",
    "method": "SendMessage",
    "params": {
      "contextId": "test-ctx-1",
      "message": {
        "role": "user",
        "parts": [{"text": "ping"}]
      }
    }
  }'
```
**Expected response:**
```json
{
  "jsonrpc": "2.0",
  "id": "1",
  "result": {
    "kind": "message",
    "message": {
      "role": "agent",
      "parts": [{"text": "pong"}]
    }
  }
}
```

**Test 3 — Via the agent tool (end-to-end):** In a Hermes chat on the `switch` profile, prompt `use fleet_send to ping construct`. Expected tool result contains `{"reply":"pong"}`.

**Test 4 — Hidden dashboard plugin does not break the web UI:** load `/api/dashboard/plugins`, confirm `a2a_fleet` is present with `tab.hidden: true`, then open the dashboard and verify there is no plugin-script 404 for the declared `entry` bundle. This catches the silent `entry omitted` regression path created by `web_server.py` defaulting `entry` to `dist/index.js` (`hermes_cli/web_server.py:3991`) while the browser eagerly fetches every plugin entry (`web/src/plugins/usePlugins.ts:43-95`).

If all four pass, v0.1 is done.

---

## Risks / Open

- **v0.1 is not discoverable by third-party A2A clients** (`a2a-inspector`, Google reference impl, generic agent listings) because the Agent Card lives at `/api/plugins/a2a_fleet/agent-card.json` instead of the spec-strict `/.well-known/agent-card.json` root path. Fleet members read each peer's `agent_card_url` directly from `fleet.yaml`. Acceptable for a closed Hermes fleet; blocking for public agent listings. v0.2+ addresses this via an upstream Hermes `root_routes` patch.
- **Per-request `self.url` resolution** relies on `request.base_url` being accurate behind reverse proxies. v0.1 uses raw `request.base_url`; if Hermes is deployed behind nginx/Caddy with TLS termination, the Agent Card URL will be wrong. Reverse-proxy handling is deferred to v0.2 (requires uvicorn `--forwarded-allow-ips` or FastAPI `ProxyHeadersMiddleware`). Treat misconfigured proxies as a known v0.1 footgun.
- **No SSE / streaming in v0.1** means long-running handlers (when they land in v0.2) will look like hung HTTP requests until the synchronous reply path is replaced. Acceptable for echo, not for real LLM responses.
- **Public Agent Card endpoint leaks capability metadata.** Anyone who can reach the gateway can read the card. Acceptable because the card contains no secrets, only declared skills and the bearer-auth requirement for the JSON-RPC route.

---

## Future Phases (v0.2+) — demoted from v0.1

The architecture mapping above (TaskManager, SSE streaming, full task lifecycle, input-required, push notifications, persistence) remains the target end-state. v0.1 deliberately ships none of it. The order below is the rough roadmap:

### v0.2 — Real Hermes session bridging + LLM handler

- Introduce `task_manager.py`. Swap `EchoHandler` for a `HermesSessionHandler` that spawns `AIAgent.run_conversation()` per task.
- Implement `tasks.get`, `tasks.list`, `tasks.cancel` against the in-memory task map.
- `SendMessage` switches to returning a **`Task`** with `state: working` (instead of v0.1's synchronous `Message`); the client polls `tasks.get` until terminal.
- Optional `LLMHandler` lands here. It **reads from Hermes's existing LLM config** (same mechanism the Hermes agent itself uses to pick provider + model + key) — **do not introduce a separate `ANTHROPIC_API_KEY` env var or per-plugin LLM config**.

### v0.3 — SSE streaming + push notifications

- Implement `GET /api/plugins/a2a_fleet/sse/{task_id}` bridging real agent stream → A2A `TaskStatusUpdate` / `TaskArtifactUpdate` events.
- Implement `SendStreamingMessage` JSON-RPC method.
- Add push notification webhook for disconnected long-running tasks.

### v0.4 — Production hardening

- Persist tasks to SQLite. Survive gateway restarts.
- Task cleanup / pruning policy.
- Auth: token rotation, mTLS option, optional OAuth2 per A2A spec.
- Multi-profile isolation, OpenTelemetry traces, Agent Card HTTP cache headers.

### v0.5 — Dynamic fleet

- `discovery.py`: scan well-known URIs on configured domains.
- Optional registry service (file-based or lightweight HTTP).
- Additional fleet tools: `fleet_status`, `fleet_get_agent_card`, `fleet_discover`.

---

## Dependencies

### Python packages

```text
httpx>=0.28,<1          # outbound JSON-RPC client for v0.1
fastapi                 # already in Hermes gateway
starlette               # already in Hermes gateway
```

`a2a-sdk` is intentionally **not** a v0.1 dependency in the locked plan. `sse-starlette` is also out of v0.1 scope because streaming is deferred.

### Hermes internal

- Gateway plugin router mount (`_mount_plugin_api_routes` in `hermes_cli/web_server.py`)
- Profile config access (`get_hermes_home()`, profile config loading)
- Tool registration (`PluginContext.register_tool`)

Agent session spawning is deliberately out of v0.1 scope.

### External (optional, Phase 2+)

- `a2a-inspector` — Google's A2A validation tool for testing compliance

---

## Configuration

### Master toggle

Fleet config lives in a **standalone file** — `~/.hermes/profiles/<profile>/fleet.yaml` — and is **not** embedded in `config.yaml`. The plugin reads `fleet.yaml` directly at startup.

```yaml
# ~/.hermes/profiles/<profile>/fleet.yaml
fleet:
  enabled: true
  # self.url is auto-detected from the gateway's bind host + port.
  server:
    auth_required: false    # v0.1 default: permissive for dev. Set true in prod.
    token_env: "HERMES_A2A_SERVER_TOKEN"
  response_handler: echo      # v0.1 only supports "echo". Any other value fails at startup.
  agents:
    # ... fleet members as shown above
```

### Per-profile A2A server mode

Each profile can independently enable or disable A2A server mode by shipping (or omitting) its `fleet.yaml`:

```yaml
# ~/.hermes/profiles/neo/fleet.yaml
fleet:
  enabled: true
  server:
    auth_required: false    # v0.1 default. Set true for production.
    token_env: "NEO_A2A_TOKEN"
  response_handler: echo
```

If `fleet.enabled: false` (or absent), the profile does NOT expose A2A endpoints and is invisible to the fleet.

---

## Edge Cases & Known Behaviour

### Remote agent is unreachable
`fleet_send` returns an error with the failure reason (timeout, connection refused, auth failure). The orchestrator agent can retry or report to the user. Stale Agent Cards are refreshed on next contact.

### Task input-required loop
Out of scope in v0.1. There is no TaskManager and no multi-turn continuation loop yet.

### Long-running tasks
Out of scope in v0.1. Streaming and polling paths stay stubbed or `501 Not Implemented`.

### Concurrent tasks to the same agent
The locked v0.1 plan does not promise concurrency semantics beyond simple request/response echo. Do not document Hermes-session fanout here until TaskManager exists.

### Agent crash during task
For v0.1 echo-only sync replies, a gateway crash is just a failed HTTP request. Persistent task recovery is future work.

### Profile-specific routing
The A2A endpoints are mounted on the Hermes gateway for the **active profile only**. If you want multiple profiles to serve A2A on the same machine, each profile needs its own gateway instance on a different port (already how Hermes multi-profile works with Telegram bots).

---

## Non-Goals (explicitly out of scope)

- **Spec-strict `/.well-known/agent-card.json` root mount (v0.1)** — deferred to v0.2+ via an upstream Hermes `root_routes` patch. v0.1 trades ecosystem interop (third-party A2A clients, public agent listings) for ship speed; fleet members read the agent-card URL directly from `fleet.yaml`'s per-peer `agent_card_url` field.
- **Cross-vendor agent communication** — this plugin targets Hermes-to-Hermes fleet communication. It implements the A2A spec, so non-Hermes A2A agents could theoretically connect, but that's not the design goal.
- **Replacing MCP** — A2A is for agent-to-agent task delegation. MCP is for tool/resource exposure. They're complementary. This plugin does not bridge MCP tools between agents (use the native MCP client for that).
- **Agent-to-agent memory sharing** — A2A preserves opacity. Agents collaborate through declared capabilities and explicit task exchange, not shared memory. Hindsight remains the cross-agent memory layer.
- **Orchestrator auto-election** — the fleet has a designated orchestrator (the Switch Agent profile). There is no leader election or failover in Phase 1.
- **gRPC binding** — JSON-RPC over HTTP only. gRPC adds complexity without benefit for a LAN fleet.

---

## Related

- [A2A Protocol Specification v1.0](https://a2a-protocol.org/latest/specification/)
- [A2A Python SDK](https://github.com/a2aproject/a2a-python) (`pip install a2a-sdk`)
- [A2A Samples](https://github.com/a2aproject/a2a-samples)
- [MCP Lazy Loading Plugin](../mcp_lazy/README.md) — reference for Hermes plugin structure
- [Hermes Gateway Plugin Architecture](../../website/docs/user-guide/features/api-server.md)
