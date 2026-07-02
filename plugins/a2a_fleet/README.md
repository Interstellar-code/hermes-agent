# a2a_fleet

Version: `0.8.14` · v0.3 executor + v0.4 config bootstrap + v0.5 dashboard API + v0.6 OpenCode executor + v0.7 Codex executor + v0.8 Antigravity (`agy`) executor + v0.8.1 mode-aware dashboard + v0.8.5 per-mode port bands + v0.8.6 dashboard dedup-by-(repo,mode) + v0.8.7–0.8.8 executor tool-parity (gh/git PATH, codex stdin, agy add-dir/timeout) + v0.8.9 cross-mode port-claim + v0.8.10/0.8.12 managed-token resolves from `.token` + v0.8.11 agy empty-output/HERMES_HOME + v0.8.13 agy prefix-drift flag + v0.8.14 A2A listener starts on `adapter.connect()` (bind-race fix) + Hermes↔Hermes peering docs. All four managed executor modes (Claude Code + OpenCode + Codex + agy) appear in the conversations/peers tab. See `CHANGELOG.md` for per-version detail.

Agent-to-Agent (A2A) communication for Hermes Agent. The plugin makes a Hermes
profile a **fleet member**: it runs its own embedded uvicorn A2A server, exposes
the profile as a discoverable A2A peer, registers the outbound `fleet_send` tool,
and (for inbound) dispatches messages through one of three response handlers —
including straight into the real Hermes agent via a platform adapter.

> **Authoritative source**: this README matches the shipped code
> (`server.py` embedded-uvicorn architecture, `fleet_config.SUPPORTED_HANDLERS`,
> `adapter.py` Route B bridge). `CHANGELOG.md` tracks notable changes. The v0.3
> Claude Code executor, v0.4 config bootstrap, and v0.6 OpenCode executor are
> shipped.

---

## What this plugin gives a profile

1. **An embedded A2A server** (`server.py`) on a dedicated port — Agent Card
   discovery, `/health`, and a JSON-RPC `SendMessage` endpoint.
2. **The outbound `fleet_send` tool** — the agent calls a named peer and gets
   the reply back, with optional multi-turn `context_id` threading.
3. **A platform adapter** (`adapter.py`) — when `response_handler: agent`,
   inbound A2A messages are dispatched into the real Hermes agent (its
   conversation loop, SOUL, tools, memory) and the agent's reply is returned
   synchronously to the peer.
4. **Four managed executor modes** — `deploy_*_receiver` tools that drop a
   repo-scoped receiver into a target repo's `.hermes/`, spawn a real CLI agent
   (Claude Code / OpenCode / Codex / Antigravity), and POST replies back. See
   **Managed executors** below.

---

## Managed executors (the four deploy modes)

Hermes can deploy a repo-scoped **managed executor** into a target repo: a
standalone receiver in `<repo>/.hermes/` that spawns a real CLI agent with the
repo's harness and POSTs replies back to the Hermes node on `:9219`. Four modes
ship, each with a deploy/status/stop tool trio. `deploy_*_receiver`
**auto-upserts its peer into `fleet.yaml`** (surgical, comment-preserving ruamel
round-trip) — you do NOT hand-edit it.

| Deploy tool (+ `_status` / `_stop`) | Mode | Port band | Default | Params (beyond `repo_path`) | Transcript | Session model |
|-------------------------------------|------|-----------|---------|-----------------------------|------------|---------------|
| `deploy_cc_receiver` | `claude_code` | `9300-9309` | `9300` | `bind_port?`, `model?`, `no_auth?`, `hermes_auth_token_env?` | `a2a-transcript.jsonl` | `uuid5(contextId)` → `claude -p --session-id` / `--resume` |
| `deploy_oc_receiver` | `opencode` | `9310-9319` | `9310` | `bind_port?`, `model?`, `no_auth?`, `hermes_auth_token_env?` | `a2a-oc-transcript.jsonl` | captured `sessionID` → `opencode run --session <id>` |
| `deploy_codex_receiver` | `codex` | `9320-9329` | `9320` | `bind_port?`, `model?`, `sandbox?` (**string**, default `workspace-write`), `no_auth?`, `hermes_auth_token_env?` | `a2a-codex-transcript.jsonl` | `thread.started` id → `codex exec resume <id>` |
| `deploy_agy_receiver` | `agy` | `9330-9339` | `9330` | `bind_port?`, `sandbox?` (**boolean** toggle), `no_auth?`, `hermes_auth_token_env?` — **NO `model`** | `a2a-agy-transcript.jsonl` | `cwd`-keyed uuid from `last_conversations.json` → `agy --conversation <uuid>` |

- **`bind_port`** (all modes, optional): omit to **reuse this repo's existing
  port** (idempotent re-deploy) else **auto-pick the first free port in the band**
  (skipping ports claimed by other repos' peers); an explicit value is honored
  verbatim. Band exhausted → clear error. Band start = the default port (v0.8.5).
- **`model`**: cc + oc only. **codex** takes `sandbox` as a **string**
  (`read-only` / `workspace-write` / `danger-full-access`) plus `model`. **agy**
  takes `sandbox` as a **boolean** toggle and has **no `model`** (the agy CLI has
  no `--model` flag).
- **Session continuity** (all): same `context_id` = same persistent CLI session
  (context accumulates); a fresh `context_id` = a new thread; each mode re-mints
  its session id on a session-not-found error.
- **Security** (all): loopback-only bind by default; a random inbound bearer token
  is auto-provisioned (env-var NAME in the peer config, VALUE injected into the
  child); cwd pinned to the canonical `repo_path` (never an inbound message path);
  symlinks resolved at deploy.
- **CLI prereqs**: `claude` / `opencode` / `codex` / `agy` must be on `PATH`;
  **`agy` additionally needs a one-time interactive host sign-in** (macOS
  Keychain). If the CLI is missing/unauthed the receiver still deploys + shows
  healthy, but every turn errors — smoke-test before relying on a green `/health`.

### Capability status — what does real repo work

Every receiver runs its CLI with skip-permissions + a PATH augmented with the
common tool dirs, so `gh`/`git`/node resolve even under a launchd daemon.

- **`claude_code`** — full tool/file/`gh` access (`claude -p
  --permission-mode bypassPermissions`). Reference mode.
- **`opencode`** — ✅ real tool access. opencode's default primary agent (full
  tools) + `--dangerously-skip-permissions --format json`; the augmented PATH
  fixed the "no `gh`" failure. Verified live (`gh issue list` → count returned).
  [#99](https://github.com/Interstellar-code/hermes-agent/issues/99) fixed.
- **`agy`** — ✅ real tool access. `--print --dangerously-skip-permissions
  --add-dir <repo> --print-timeout <budget>`; `--add-dir` grants workspace access,
  the raised timeout ends the 5m plan-only exits. Verified live.
  [#100](https://github.com/Interstellar-code/hermes-agent/issues/100) fixed.
- **`codex`** — fix landed (prompt positional + `stdin=DEVNULL`; codex-cli ≥0.136
  otherwise blocks on stdin → rc=1). Unit + falsification verified; live re-verify
  pending a codex-cli auth refresh
  ([#97](https://github.com/Interstellar-code/hermes-agent/issues/97)).

`claude_code`, `opencode`, and `agy` are cleared for real repo work; use `codex`
once its live turn is re-confirmed. See `skills/deploy-fleet/SKILL.md` for the
canonical multi-mode deploy procedure and `skills/deploy-cc-receiver/SKILL.md`
for the Claude Code deep dive.

---

## Architecture

The plugin runs its **own** FastAPI/uvicorn server on a dedicated port — it does
**not** mount routes on the Hermes dashboard gateway. This isolation sidesteps
the gateway's session-token middleware, localhost-only CORS, and Host-header
validation, all of which would block cross-machine peer access.

```
┌──────────────────────────────────────────────────────────┐
│                  Hermes Agent Process                     │
│                                                           │
│  ┌────────────────────┐                                   │
│  │  Gateway agent loop │  (SOUL, tools, memory)           │
│  │  + a2a_fleet        │◀───┐                             │
│  │  platform adapter   │    │ run_coroutine_threadsafe     │
│  └────────────────────┘     │ (adapter.bridge_sync,        │
│                             │ in-process, Route B)         │
│  ┌──────────────────────────┴──────────────┐              │
│  │  a2a_fleet server (server.py)            │              │
│  │  uvicorn on its own event loop / daemon  │              │
│  │  thread, bound to fleet.yaml host:port   │              │
│  │  STARTED by adapter.connect() (gateway)  │              │
│  │  inbound → echo | llm | agent handler    │              │
│  └──────────────────────────────────────────┘              │
└──────────────────────────────────────────────────────────┘
```

- `register(ctx)` registers the `fleet_send` tool, the `a2a_fleet` platform
  adapter (via `ctx.register_platform`), and the `deploy-fleet` skill. It does
  **NOT** start the server — `register()` runs in every process that loads the
  plugin (gateway, CLI tool startup, dashboard web tier), so starting the
  listener there raced them all to bind the port and a bridge-less winner broke
  Route B (#120).
- The uvicorn listener is started by **`A2AFleetAdapter.connect()`** — the one
  path that runs only in the gateway/agent process, on the gateway loop, right
  where the Route B bridge is wired (`set_agent_bridge`). So the listener and the
  bridge are co-located by construction; `disconnect()` stops it. The server runs
  on a named daemon thread with its own event loop (`_start_server_in_thread`),
  and an `atexit` handler (`_atexit_stop`) signals uvicorn to exit on shutdown.
  **Consequence:** an A2A node's listener comes up when the `a2a_fleet` platform
  connects (gateway with `platforms.a2a_fleet` enabled), not at plugin import.
- Config (`fleet.yaml`) is re-read on **every request**, so handler/peer edits
  take effect without a server restart.

### Routes

The server (bound to `fleet.server.bind_host:bind_port`) serves:

| Method | Path | Auth | Description |
|--------|------|------|-------------|
| `GET` | `/.well-known/agent-card.json` | **public** | A2A capability discovery (RFC 8615). Always anonymous. |
| `GET` | `/health` | public | `{"ok": true, "version": ..., "peer_count": N}` |
| `POST` | `/jsonrpc` | bearer (if `auth_required`) | A2A JSON-RPC 2.0 `SendMessage` / `message/send` endpoint |

Interactive docs (`/docs`, `/redoc`, `/openapi.json`) are disabled — this is a
peer-facing surface. **No CORS middleware**: A2A is server-to-server; browsers
are not A2A clients.

---

## Inbound response handlers (the three modes)

`fleet.response_handler` selects how an inbound `SendMessage` is answered.
`fleet_config.SUPPORTED_HANDLERS = {"echo", "llm", "agent"}` — any other value
raises `FleetConfigError` at load time.

### `echo` — ping/pong diagnostic
`ping` → `pong`; anything else echoes the input verbatim. No model, no agent.
Use it to smoke-test transport, auth, and discovery end-to-end.

### `llm` — stateless model call (Route A)
Calls the **active profile's configured provider** directly
(`resolve_provider_client("auto")`) with a small per-context history kept in
`context_store`. It delivers real conversational back-and-forth (reasoning, Q&A,
persona replies) with multi-turn context on the same `context_id`.

> **This BYPASSES the Hermes agent.** It has NO access to the profile's live
> tools, memory, MCP, or SOUL — it is a raw model call. Treat `llm` as a
> fallback for plain chat; for tool-grounded or memory-aware answers use `agent`.

System prompt resolves as: `llm.system_prompt` string > `llm.system_prompt_file`
> built-in default. `llm.max_tokens` (default 2048) and `llm.temperature`
(default 0.7) are honored.

### `agent` — dispatch into the real Hermes agent (Route B)
The inbound message is routed into the **real Hermes agent** through the
`a2a_fleet` platform adapter (`adapter.py`). The flow:

1. The uvicorn handler (on the server's daemon-thread loop) calls
   `bridge.bridge_sync(text, context_id, peer_id, timeout)` in a worker thread.
2. `bridge_sync` submits the message to the gateway's event loop via
   `asyncio.run_coroutine_threadsafe(self._message_handler(event), gateway_loop)`
   and blocks for the reply.
3. The gateway runs a **real agent turn** — SOUL, tools, memory — and returns
   the answer; the adapter strips a leading `💭 Reasoning:` preamble (when
   `show_reasoning` is on) and returns the final answer over the wire.

Concurrency: per-`context_id` threading locks serialize same-context turns; a
second overlapping turn on the same context gets an `A2ABusyError` (JSON-RPC
"peer busy on this context, retry") rather than racing the first. `agent.timeout_s`
(default 120) bounds the wait. The A2A `contextId` maps to the Hermes session
`chat_id`, so the same `context_id` continues the same agent session.

Route B requires the gateway to have the adapter connected — i.e.
`platforms.a2a_fleet.enabled=true` in the active profile config so the gateway
calls `adapter.connect()`, which both registers the bridge **and starts the A2A
listener** (v0.8.14 — see Architecture). If the platform is not connected the
listener never comes up; if it is up but the bridge isn't ready, `/jsonrpc`
returns a JSON-RPC error telling you to enable it.

### Hermes↔Hermes peering (profile-to-profile / cross-LAN)

`response_handler: agent` is also how one Hermes profile reaches **another
profile's agent** — same-host or across two PCs on a LAN. Each receiving profile
runs its own A2A listener (unique `bind_port`; LAN bind + `auth_required: true`
off-loopback) and each sender lists the others as **plain agent peers** (`url`
base + `token_env`, no `managed`/`mode`). Full setup — per-profile/port map,
same-host + cross-PC + bidirectional `fleet.yaml` snippets, profile-scoped
tokens, and the handshake convention — is in the **`deploy-fleet` skill**
("Hermes↔Hermes peering" section).

---

## Configuration

Config lives in a standalone `fleet.yaml` under the active Hermes home
(`$HERMES_HOME/fleet.yaml`). For early-checkout compatibility the loader falls
back to `$HERMES_HOME/profiles/<name>/fleet.yaml` if the primary is absent.

```yaml
fleet:
  enabled: true                   # set false to keep the plugin idle for this profile
  response_handler: agent         # echo | llm | agent  (anything else → FleetConfigError)

  server:
    bind_host: 0.0.0.0            # default 127.0.0.1
    bind_port: 9219              # REQUIRED — no default; pick a free port per profile
    auth_required: true          # DEFAULT true — inbound /jsonrpc requires a bearer token
    token_env: SWITCH_A2A_TOKEN  # env var holding THIS node's inbound bearer token

  self:
    name: switch                 # name advertised in the Agent Card

  # Optional — only read when response_handler: llm
  llm:
    system_prompt: "You are ..."   # or system_prompt_file: /path/to/prompt.txt
    max_tokens: 2048
    temperature: 0.7

  # Optional — only read when response_handler: agent (Route B)
  agent:
    timeout_s: 120               # max seconds to wait for the agent reply

  agents:                        # peers this node can call via fleet_send
    construct:
      url: http://10.0.0.5:9220              # peer's base URL (/jsonrpc appended by the client)
      agent_card_url: http://10.0.0.5:9220/.well-known/agent-card.json   # optional
      token_env: CONSTRUCT_A2A_TOKEN          # env var holding the token used to call this peer
      description: "Construct build agent"
```

### Key config facts

- **`bind_port` is required** — `FleetConfigError` if missing. No default.
- **`auth_required` defaults to `true`.** Newly-created profiles opt into bearer
  protection automatically.
- **`response_handler` must be `echo`, `llm`, or `agent`.** Any other value
  raises `FleetConfigError` at load time.
- Peer `url` must be `http`/`https` with a real host or load fails.
- Tokens are never stored in `fleet.yaml`. Each `token_env` names an environment
  variable holding the actual pre-shared bearer token. Convention:
  `<PEER>_A2A_TOKEN`.

### Config bootstrap (v0.4 — shipped)

You no longer hand-write `fleet.yaml` from scratch, and you no longer hand-wire a
deployed receiver's peer entry:

- **First-enable scaffold.** When a profile enables the plugin and no `fleet.yaml`
  exists, `register()` writes a commented example (`enabled: true`,
  `response_handler: agent`, a `server` block, and an empty `agents: {}`) to
  `$HERMES_HOME/fleet.yaml`. The node comes up immediately instead of going
  silently idle. The write is idempotent and never clobbers an existing file.
- **Auto-wired peer.** Every `deploy_*_receiver` (`deploy_cc_receiver`,
  `deploy_oc_receiver`, `deploy_codex_receiver`, `deploy_agy_receiver`) upserts its
  receiver peer into `fleet.yaml` **surgically** (ruamel round-trip — your
  comments and formatting are preserved). With auth they write managed peers
  (`url` + `token_env` + `managed: true` + `mode` + `repo_path`), so
  `fleet_send` resolves the bearer automatically (no more 401) and
  boot-reconcile re-provisions the same token across a gateway restart. A
  `no_auth` deploy gets a plain `url` peer. Default peer names are
  `claude-code` / `opencode` / `codex` / `agy`; a second repo reusing the same
  default name gets a distinct `-<repo>` suffix. The upsert result is returned
  under `fleet_peer`; a config-write hiccup is a non-fatal warning (the receiver
  is already healthy).

### Auth behavior

- `auth_required: true` + no resolved token → server returns **HTTP 503**
  (misconfig; does not leak the `token_env` name).
- Missing/malformed `Authorization: Bearer ...` → **HTTP 401**.
- Bearer comparison uses `hmac.compare_digest` (constant-time).
- Sending plaintext bearer tokens over non-loopback HTTP is inadvisable —
  terminate TLS in front of the server when binding to a public address.

---

## The `fleet_send` tool (agent-facing)

The plugin registers the `fleet_send` outbound tool:

```json
{
  "name": "fleet_send",
  "parameters": {
    "type": "object",
    "properties": {
      "agent":      {"type": "string", "description": "Name of the fleet peer (matches fleet.yaml)."},
      "message":    {"type": "string", "description": "Plain-text message to send to the peer agent."},
      "context_id": {"type": "string", "description": "Optional conversation context id for multi-turn exchanges. When omitted the server generates one and returns it; pass it back on later turns to continue the thread."}
    },
    "required": ["agent", "message"]
  }
}
```

Returns `{"reply": "...", ...}` on success or `{"error": "..."}` on any failure
(network error, peer 401, JSON-RPC error). It never raises — the calling agent
can surface the string verbatim. Pass the returned `context_id` on subsequent
turns to continue a multi-turn thread with the peer.

---

## CLI client

`client.py` ships a `__main__` entry point for manual peer testing:

```bash
cd plugins
HERMES_HOME=~/.hermes python -m a2a_fleet.client construct ping
# → pong
```

`HERMES_HOME` must point at a profile dir whose `fleet.yaml` lists the named peer.

---

## JSON-RPC contract

`POST /jsonrpc` accepts raw JSON:

- `SendMessage` / `message/send` → returns
  `{result: {kind, message: {role: "agent", parts: [{text}], contextId}}}`.
- Malformed JSON → HTTP 200 with JSON-RPC error `-32700`.
- Non-object body → `-32600`; non-object `params` → `-32602`.
- `SendStreamingMessage`, `message/stream`, `tasks.get`, `tasks.list`,
  `tasks.cancel` → `-32601` (not implemented).
- Any other method → `-32601` ("Method not found").

The answering behavior depends on `response_handler` (echo / llm / agent above).

---

## v0.3 — Claude Code as a repo-scoped A2A executor (shipped)

> **Status: shipped.** The pieces below describe current behavior:
> `deploy_cc_receiver` / `cc_receiver_status` / `cc_receiver_stop` are live tools,
> the receiver template ships in `templates/cc_receiver.py`, and v0.4 adds config
> bootstrap (auto-scaffold + auto-wired managed peer). Live-verified end-to-end:
> Hermes → receiver → `claude -p` → reply POSTed back to `:9219` (HTTP 200).

**Vision.** Hermes = **orchestrator**. Claude Code = **executor** running inside
a specific repo with that repo's FULL harness — skills, MCP, plugins, `.claude/`
settings, `CLAUDE.md`, claude-mem. The point of routing through Claude Code (not
a raw LLM) is to leverage that harness: exactly what the user would have manually,
but now driven by Hermes over A2A.

**Deploy flow.**
1. User → Hermes: "work on repo X where Claude Code is set up."
2. Hermes confirms the repo path back to the user.
3. Hermes calls `deploy_cc_receiver(repo_path)` (a live tool). It:
   - copies a standalone receiver into `<repo>/.hermes/cc_receiver.py`,
   - writes binding config `<repo>/.hermes/a2a_receiver.json` (cwd **pinned** to
     `repo_path` — never taken from an inbound message),
   - writes/refreshes an idempotent **managed A2A-role block** into
     `<repo>/CLAUDE.md` (between `<!-- a2a-fleet:start -->` / `:end -->`
     markers),
   - launches the receiver as a **detached, Hermes-managed daemon** on `:9300`,
     records a PID file, health-checks it.
4. **Handshake**: Hermes and Claude Code exchange roles (orchestrator /
   executor), the bound repo, the comm contract (same `context_id` = same
   persistent session; replies POSTed to `:9219`), and purpose.
5. Ongoing: Hermes relays tasks via
   `fleet_send(agent="claude-code", message, context_id)`, monitors, and awaits
   the reply on `:9219`, liaising with the user.

Each inbound task spawns `claude -p` with `cwd=<repo>`, a persistent session
(`--session-id` / `--resume` keyed off the `context_id`), and the repo's harness
loaded (`--setting-sources user,project,local`, `--mcp-config`), so the executor
answers with the repo's real skills/tools/MCP/CLAUDE.md.

**fleet.yaml peer schema.** A managed Claude Code peer carries repo binding
(written automatically by `deploy_cc_receiver` — see "Config bootstrap"):

```yaml
agents:
  claude-code:
    url: http://127.0.0.1:9300
    repo_path: /Users/you/dev/some-repo   # NEW — the bound repo (cwd of claude -p)
    managed: true                          # NEW — Hermes owns/launches the daemon
    mode: claude_code                      # NEW — distinguishes from plain peers
```

`load_fleet()` surfaces `repo_path` / `managed` / `mode` so Hermes knows which
repo a link drives and whether it owns the daemon (boot-reconcile).

**Guardrails.** The receiver runs with `bypassPermissions` in a
real repo, so: cwd is pinned at deploy time (never from a message), per-`context_id`
serialization prevents two `claude -p --resume` overlapping the same session,
bearer auth gates `:9300`, and autonomous operation is bounded (per-turn timeout,
restart backoff, idle cap). Deploy only to repos the user has authorized.

See `skills/deploy-cc-receiver/SKILL.md` for the orchestration procedure and
`.omc/plans/a2a-fleet-v0.3-plan.md` for the full design.

## v0.6 — OpenCode as a second repo-scoped A2A executor (shipped)

- Tools: `deploy_oc_receiver`, `oc_receiver_status`, `oc_receiver_stop`
- Template: `templates/oc_receiver.py`
- Deploy module: `oc_deploy.py`
- Default peer name / mode / port: `opencode` / `opencode` / `9310`

OpenCode mirrors the Claude receiver's security model (loopback-by-default,
bearer auth, cwd pinned to the configured repo, bounded concurrency, idle
teardown, reply retry parity), but its session model differs: the first turn
must mint an OpenCode-generated `sessionID` from the NDJSON event stream and
persist a durable `contextId -> sessionID` map in
`<repo>/.hermes/a2a-oc-sessions.json`; later turns continue with
`opencode run --session <stored_id> ...`. If OpenCode returns
`Error: Session not found`, the receiver remints once under the same
per-context lock and updates the map.

## v0.8 — Google Antigravity CLI (`agy`) as a managed A2A executor (issue #75)

- Tools: `deploy_agy_receiver`, `agy_receiver_status`, `agy_receiver_stop`
- Template: `templates/agy_receiver.py`
- Deploy module: `agy_deploy.py`
- Default peer name / mode / port: `agy` / `agy` / `9330` (band 9330-9339)

Example managed peer block in `fleet.yaml` (auto-wired by `deploy_agy_receiver`,
you do NOT hand-edit it):

```yaml
agents:
  agy:
    url: http://127.0.0.1:9330
    agent_card_url: http://127.0.0.1:9330/.well-known/agent-card.json
    token_env: A2A_AGY_TOKEN_<REPO>
    managed: true
    mode: agy
    repo_path: /abs/path/to/repo
    description: "Google Antigravity CLI executor receiver (repo: ...)"
```

Deploy params: `repo_path` (required), `bind_port` (omit to auto-pick a free
port in the agy band 9330-9339, or reuse this repo's existing port on
re-deploy), and `sandbox` (a **boolean** toggle that passes agy's `--sandbox`).
There is **NO
model selection** — agy has no `--model` flag, so the deploy tool exposes no
model param.

**Requires an interactive `agy` sign-in once on the host** — agy authenticates
via the macOS Keychain (no file, no headless login, no `agy auth` subcommand).
Run `agy` interactively once to sign in before deploying. A turn that fails for
lack of auth surfaces a clear "agy not authenticated — run `agy` interactively
once to sign in" error rather than hanging silently.

agy mirrors the security model of the other receivers (loopback-by-default,
bearer auth, cwd pinned to the configured repo, bounded concurrency, idle
teardown, reply-retry parity), but its session + output model differs:

- **Session id is not caller-assignable.** The first turn runs WITHOUT a
  conversation id; agy mints a uuid and records `cwd -> uuid` in
  `~/.gemini/antigravity-cli/cache/last_conversations.json`. The receiver reads
  that file (keyed by the pinned repo cwd) to capture the uuid and persists
  `contextId -> {conversation_id, last_stdout}` in
  `<repo>/.hermes/a2a-agy-sessions.json`. Later turns pass
  `agy --conversation <uuid> --print ...` (never `--continue`, which is
  cwd-global and unsafe for concurrent contexts).
- **Plain-text transcript-tail extraction.** On every resume agy re-echoes the
  ENTIRE prior transcript (newline-separated assistant replies, no role markers)
  and then appends the new reply, with no delimiter. The receiver persists the
  full prior stdout per contextId and strips it as a literal prefix to recover
  only the latest reply (falling back to the last non-empty line if the prefix
  drifts after a restart).
- **Remint.** If a stored uuid is dead, agy prints
  `Warning: conversation "<id>" not found.` as the first stdout line and then
  runs fresh in the same invocation. The receiver clears the stale entry, strips
  the warning from the reply, and captures the new uuid.

The agy subprocess always runs with `AGY_CLI_DISABLE_LATEX=1` in its environment.

### How it works (end-to-end flow)

Hermes is the **orchestrator** (its own LLM); Claude Code is the **executor**
(the full harness of the target repo). They talk over A2A, keyed by `context_id`.

```
┌──────────────┐   chat    ┌─────────────────────────────────────────────┐
│   USER        │◄─────────►│        HERMES AGENT  (orchestrator)         │
│ (Telegram/…)  │           │  its own LLM · inbound A2A node :9219       │
└──────────────┘           │  tools: deploy_{cc,oc,codex,agy}_receiver · fleet_send │
                           │         {cc,oc,codex,agy}_receiver_status / _stop      │
                           └──────┬───────────────────────────▲──────────┘
                       deploy +   │ fleet_send(msg, context_id) │ reply POST
                       fleet_send │ + Bearer token              │ (same context_id)
                                  ▼                             │
                       ┌────────────────────────────────────────────────┐
                       │  cc_receiver.py  (deployed in <repo>/.hermes/)  │
                       │  A2A server :93xx · bearer auth · inbox/queue   │
                       │  per-contextId lock · idle-timeout teardown     │
                       └──────────────┬─────────────────────────────────┘
                            spawn     │ cwd=<repo>
                            claude -p │ --session-id=uuid5(context_id) / --resume
                                      ▼
                       ┌────────────────────────────────────────────────┐
                       │  CLAUDE CODE  (executor) — a `claude -p` turn   │
                       │  FULL repo harness: skills, MCP, CLAUDE.md      │
                       │  role injected via .hermes/A2A.md (@import)     │
                       │  memory: ~/.claude session files per context_id │
                       └────────────────────────────────────────────────┘
```

**① Setup** — `deploy_cc_receiver("<repo>")` copies `cc_receiver.py` into
`<repo>/.hermes/`, writes the executor role to `<repo>/.hermes/A2A.md` and adds
`@import .hermes/A2A.md` to `<repo>/CLAUDE.md`, provisions a bearer token
(`.token`, 0600, gitignored), launches the daemon, and health-checks it.

**② Handshake (the initial message)** — Hermes sends one `fleet_send` on a
reserved `context_id` (`handshake:<repo>`) declaring its role, the bound repo,
the comm contract (same `context_id` = same session), and the purpose. The
receiver spawns `claude -p` (cwd=repo); `CLAUDE.md` + `A2A.md` load, so Claude
**knows it is the executor** and replies with role / cwd / harness inventory /
ready. Both sides now share a contract before any real work.

**③ Work** — per task, Hermes calls `fleet_send(message, context_id)`; the
receiver runs `claude -p --resume uuid5(context_id)` in the repo (tools live);
the reply is POSTed back to Hermes `:9219` with the same `context_id`. Reusing a
`context_id` continues that Claude session — context accumulates; a new one
starts a fresh thread. Hermes does **not** auto-loop: it summarizes each reply to
the user and awaits direction (anti-loop guardrail).

### Reply delivery & the round-trip (orchestrator responsibility)

The reply is **asynchronous**. The receiver answers the inbound `fleet_send`
immediately with a `[queued]` ack, runs `claude -p` (seconds to minutes), then
makes a **separate outbound** A2A `SendMessage` POST back to `hermes_url`
(`:9219`) carrying the result on the **same `context_id`**. Proven end-to-end
(receiver log: `posted reply to hermes ctx=handshake:... status=200`):

```
hermes → claude   task            (fleet_send, context_id=C)
claude → hermes   "[queued]" ack  (immediate JSON-RPC response)
… claude -p turn runs …
claude → hermes   real reply      (separate POST to :9219, context_id=C, HTTP 200)
```

**The plugin's job ends at HTTP 200.** Transport, auth, and dispatch are the
plugin's responsibility and are verified. **Surfacing that reply to the human is
the orchestrator's job** — and it hinges on `context_id`:

- The reply arrives on `:9219` as an inbound A2A message on `context_id=C`. The
  `agent` handler ingests it into the Hermes agent **on context `C`**.
- If `C` is **not mapped to a live user conversation**, the reply is received but
  **never relayed to the user**. In particular, a reply on the reserved
  **`handshake:<repo>`** context is intentionally *not* user-facing — it closes
  the handshake, nothing more.

**Rule for an orchestrator (Hermes) to make executor replies reach the user:**

1. When dispatching real work, **`fleet_send` with a `context_id` you can map
   back to the originating user conversation** (e.g. derive it from the chat/
   thread id, or keep a `context_id → conversation` table). Do **not** use the
   `handshake:*` context for work whose reply must surface.
2. On inbound A2A delivery (the `agent` handler firing for a peer reply), **look
   up that `context_id`** and **relay the reply text into the mapped
   conversation** (Telegram/Discord/etc.), rather than treating it as a fresh
   agent turn.
3. Keep the anti-loop guardrail: relay-and-summarize; do not auto-reply back to
   the executor without user direction.

This mapping is **orchestrator logic, not plugin logic** — the plugin neither
owns user conversations nor knows the chat platform. See the deploy skill
(`skills/deploy-cc-receiver/SKILL.md`) for the orchestration procedure.

---

## Context & memory model

This plugin stores **no conversation context of its own** — no SQLite DB, no
JSON history file it maintains. Conversation memory is delegated to whichever
agent actually runs the turn. `contextId` is the join key across all paths.

| Path | Where the conversation lives | Durable? |
|------|------------------------------|----------|
| **v0.3 `claude_code` executor** | Claude Code's **native session store** (`~/.claude/` session files), keyed by `--session-id <uuid5(contextId)>` / `--resume` | ✅ Claude Code owns it (incl. its own compaction) |
| **`agent` (Route B)** | The **Hermes agent session** keyed `agent:main:a2a_fleet:dm:{contextId}` (see below) | ✅ Hermes owns it |
| **`llm` (Route A, fallback)** | `context_store.py` — an **in-memory** LRU dict (max ~20 turns / 500 contexts) | ❌ ephemeral, lost on restart |
| **`echo`** | none (stateless ping/pong) | — |

For the v0.3 executor, the receiver only maps `contextId → session-id` and lets
Claude Code remember. Same `contextId` → same Claude session → context
accumulates across turns. The `<repo>/.hermes/*.jsonl` files (`a2a-inbox`,
`a2a-transcript`, `a2a-inbox.offset`) are **operational logs/queues — not
conversation context.**

**Boundary (by design):**
- *Durability = the running agent's retention, not this plugin's.* If Claude
  Code (or Hermes) prunes very old sessions, a long-dormant `contextId` can lose
  its history. Fine for active work; relevant only for resuming weeks-old threads.
- *Session files are host-local* — they live where the agent runs (the repo's
  machine/user home for `claude -p`; the profile dir for Hermes). A `contextId`
  is therefore bound to that host. This matches the single-host repo model; it is
  not portable across machines (which this design does not need).

### How the Hermes agent itself remembers (for reference)
The peer on the Hermes side has two independent layers, separate from this plugin:
- **Session (short-term):** the gateway `SessionStore` writes a per-session
  transcript `<profile>/sessions/{session_id}.jsonl` plus a `sessions.json`
  index. This is the active conversation history (Hermes applies its own
  compaction as it grows).
- **Long-term (cross-session):** a pluggable **memory provider** via
  `agent/memory_manager.py` — e.g. **Hindsight** (local-embedded, SQLite +
  vector index, daemon on `:9177`). It runs RAG-style: *recall* relevant
  memories before a turn (injected into the system prompt) and *store*
  observations after (`prefetch_all` / `sync_all`). The built-in `memory` tool
  (`MEMORY.md` / `USER.md` hot-cache) is the simplest such provider.

So: **two agents, two independent memories.** Hermes remembers via its session
JSONL + its memory provider; Claude Code remembers via its own session files;
`a2a_fleet` just threads `contextId` between them and persists none of it.

---

## Dashboard API — A2A conversation feed (v0.5)

A read-only HTTP surface for a front-end to render the Hermes ⇄ Claude Code
executor conversations (e.g. a "A2A Fleet" tab). Lives in
`dashboard/{manifest.json, plugin_api.py}`; `web_server._mount_plugin_api_routes`
auto-mounts the FastAPI `router` under `/api/plugins/a2a_fleet/`, **behind the
dashboard session auth** (same origin/auth a dashboard front-end already uses).
Bundled-plugin backend imports are allowed; project plugins are not
(GHSA-5qr3-c538-wm9j).

**Source of truth:** each managed `claude_code` peer's per-repo transcript
`<repo>/.hermes/a2a-transcript.jsonl` — both directions, including the `[queued]`
ack and the executor reply. Degrades to an empty list on missing/invalid config or
absent transcripts (never 500). Per-context reads are capped at 2000 messages.

**Profile-agnostic peer discovery (v0.5.2):** the dashboard is a global control
plane — it usually runs under the default Hermes home (`~/.hermes`) while the
managed receivers live in a specific profile (`~/.hermes/profiles/<name>/fleet.yaml`).
So peers are read from **every** profile: the home's own `fleet.yaml` *and* each
`profiles/*/fleet.yaml` beneath it, deduped by `repo_path`. Parsing is lenient (raw
YAML, no schema/`token_env` validation — that is the live server's job), so one bad
file never blanks the feed. This is why the feed populates even though the
dashboard's own profile has no `fleet.yaml`.

| Method · path | Returns |
|---|---|
| `GET /api/plugins/a2a_fleet/conversations` | `{count, conversations: [{contextId, peer, repo_path, message_count, last_ts, last_dir, last_text}]}` — newest activity first |
| `GET /api/plugins/a2a_fleet/conversations/{contextId}` | `{contextId, peer, repo_path, messages: [{ts, dir, from, to, text}]}` — chronological. `contextId` contains a colon (`handshake:hermes-switch`) → URL-encode it. A contextId can be shared across repos → narrow with `?peer=` / `?repo_path=`; an ambiguous bare lookup returns **409** with the candidate peers |
| `GET /api/plugins/a2a_fleet/peers` | `{count, peers: [{name, repo_path, transcript_exists, message_count}]}` |

`dir` values drive rendering: `hermes->claude` (orchestrator→executor),
`claude->hermes (ack)` (the `[queued]` ack), `claude->hermes` (the real reply).
`text` is markdown. Poll `/conversations` (and the open thread) every ~2s for a
live feel. The route appears only after the gateway is (re)started with the
plugin present — discovery + mount happen at `web_server` startup; a 404 means a
restart is needed.

### Which server hosts this, and how to reach it

This endpoint lives on **exactly one** of the several Hermes ports — getting this
wrong is the #1 source of confusion. It is mounted on the **Hermes Dashboard web
server** (`hermes dashboard`, default **`:9119`**), the same app that serves the
SPA. It is **NOT** on any of these:

| Port | Server | Auth | Hosts `/api/plugins/a2a_fleet/*`? |
|------|--------|------|----------------------------------|
| **`:9119`** | **Dashboard web server** (`hermes dashboard`) | **browser session cookie** (dashboard login / OAuth gate) | ✅ **yes** |
| `:8642` | Gateway **API server** (`API_SERVER_PORT`) | `API_SERVER_KEY` **bearer** | ❌ no |
| `:9219` | a2a_fleet **peer server** (`server.py`) | A2A bearer (`fleet.server.token_env`) | ❌ no |
| `:9300+` | deployed **CC receiver** | A2A bearer | ❌ no |

**Auth:** the Dashboard gates `/api/*` with a **browser session cookie**, not the
`API_SERVER_KEY` bearer (that key is for the `:8642` API server and does nothing
here). So a plain `curl` from a terminal gets a 401 / login redirect **by design**
— this is the gate working, not a misconfiguration. A browser front-end on the
dashboard origin already carries the cookie, so it reaches the endpoint with no
extra auth.

**Verifying without a browser (terminal / an agent):** do **not** curl `:9119`
(no session cookie). Read the **source of truth directly** instead — the same
JSONL the endpoint serves:

```bash
cat <repo>/.hermes/a2a-transcript.jsonl      # both directions, one JSON line per message
```

Confirm the route is *mounted* (not auth) by listing discovered dashboard plugins
in-process, e.g. via `web_server._get_dashboard_plugins()`, or just check the
`web_server` startup log for `Mounted plugin API routes: /api/plugins/a2a_fleet/`.

---

## Install / enable

```bash
hermes plugins enable a2a_fleet
hermes gateway restart
```

The inbound server requires `fastapi` + `uvicorn` (install `hermes-agent[web]`).
If those are missing, the plugin loads but the server stays idle and logs a
warning — `fleet_send` (outbound) still works. For Route B (`agent`), also set
`platforms.a2a_fleet.enabled=true` in the active profile config.

Look for in the agent log:
```
a2a_fleet: registered fleet_send tool + spawned A2A server thread
a2a_fleet: server started on 0.0.0.0:9219
a2a_fleet: registered platform adapter with gateway     # when register_platform available
a2a_fleet: adapter connected; bridge ready               # Route B, once the gateway connects it
```

Verify discovery:
```bash
curl http://<bind_host>:<bind_port>/.well-known/agent-card.json
curl http://<bind_host>:<bind_port>/health
```

---

## Files

| Path | Purpose |
|------|---------|
| `__init__.py` | `register(ctx)` — registers `fleet_send`, deploy tools, the `deploy-fleet` skill, and the `a2a_fleet` platform adapter. Does NOT start the server (that's `adapter.connect()`, gateway-only, v0.8.14). `_start_server_in_thread()` spawns the uvicorn daemon thread + registers the `atexit` stop |
| `server.py` | FastAPI app factory (`build_app`), Agent Card builder, JSON-RPC handler (echo/llm/agent dispatch), uvicorn lifecycle |
| `fleet_config.py` | `fleet.yaml` loader, env-var token resolution, validation, `SUPPORTED_HANDLERS = {"echo", "llm", "agent"}`, `llm`/`agent` blocks |
| `fleet_tools.py` | `fleet_send_handler` — wraps the client in a `{reply}`/`{error}` dict, threads `context_id` |
| `client.py` | Async A2A client (`send_message`) over httpx + `__main__` CLI |
| `response_handler.py` | `HandlerResult` dataclass + `echo_handler` |
| `llm_handler.py` | `llm_handler` (Route A) — stateless call to the active profile's provider |
| `adapter.py` | `A2AFleetAdapter` (Route B) — bridges inbound A2A into the real Hermes agent via the gateway loop |
| `agent_bridge.py` | Global bridge registry + `A2ABusyError` / `A2ABridgeNotReady` errors |
| `context_store.py` | Per-`context_id` multi-turn history + locks (used by `llm`) |
| `skills/deploy-fleet/SKILL.md` | Procedure: bring up a node, verify, ping/pong |
| `skills/deploy-cc-receiver/SKILL.md` | Procedure: deploy a Claude Code executor receiver into a repo |
| `fleet_yaml_io.py` | First-enable `fleet.yaml` scaffold + comment-preserving managed-peer upsert (auto-wiring for all four `deploy_*_receiver` tools) |
| `dashboard/manifest.json` · `dashboard/plugin_api.py` | Read-only dashboard API (`/api/plugins/a2a_fleet/conversations` · `/peers`) feeding a front-end A2A conversation tab |
| `templates/cc_receiver.py` | Standalone receiver dropped into `<repo>/.hermes/` by `deploy_cc_receiver` |
| `templates/oc_receiver.py` | Standalone receiver dropped into `<repo>/.hermes/` by `deploy_oc_receiver` |
| `templates/codex_receiver.py` | Standalone receiver dropped into `<repo>/.hermes/` by `deploy_codex_receiver` |
| `templates/agy_receiver.py` | Standalone receiver dropped into `<repo>/.hermes/` by `deploy_agy_receiver` |
| `cc_deploy.py` | `deploy_cc_receiver` / `cc_receiver_status` / `cc_receiver_stop` handlers, token provisioning, `resolve_managed_bind_port`, boot-reconcile across all four managed modes |
| `oc_deploy.py` | `deploy_oc_receiver` / `oc_receiver_status` / `oc_receiver_stop` handlers, token provisioning |
| `codex_deploy.py` | `deploy_codex_receiver` / `codex_receiver_status` / `codex_receiver_stop` handlers, token provisioning |
| `agy_deploy.py` | `deploy_agy_receiver` / `agy_receiver_status` / `agy_receiver_stop` handlers, token provisioning |
| `managed_peers.py` | Shared managed-mode contracts: per-mode port bands (`9300-9309` / `9310-9319` / `9320-9329` / `9330-9339`), band-port allocation, default peer names, transcript filenames, stable token-env naming |
| `plugin.yaml` | Hermes plugin manifest |
| `references/` | A2A spec summary + Hermes plugin guide |

```bash
cd ~/.hermes/hermes-agent
venv/bin/python -m pytest tests/plugins/a2a_fleet/ -q
```

---

## Roadmap

| Phase | Status | Description |
|-------|--------|-------------|
| v0.1 | ✅ shipped | Embedded uvicorn server, Agent Card discovery, JSON-RPC `SendMessage`, bearer auth, echo handler, `fleet_send` tool, async client |
| v0.2 | ✅ shipped | `llm` response handler (Route A — stateless model call + multi-turn `context_store`), `message/send` alias, `HandlerResult`, outbound `context_id` threading |
| Route B | ✅ shipped | `agent` response handler — inbound dispatched into the real Hermes agent via the `a2a_fleet` platform adapter + `run_coroutine_threadsafe` bridge to the gateway loop |
| v0.3 | ✅ shipped | `deploy_cc_receiver` — Claude Code executor receiver deployed into a target repo's `.hermes/`, repo-aware `fleet.yaml` (`repo_path`/`managed`/`mode`), handshake, managed daemon lifecycle |
| v0.6 | ✅ shipped | `deploy_oc_receiver` — OpenCode executor receiver deployed into a target repo's `.hermes/`, durable OpenCode session map, mode-aware peer upsert/load/reconcile, coexistence with Claude receiver in one repo |
| v0.7 | ✅ shipped | `deploy_codex_receiver` — Codex CLI executor receiver (`mode: codex`, codex band 9320-9329, default 9320), durable thread map, JSONL parsing, thread remint, coexistence with cc/oc in one repo |
| v0.8 | ✅ shipped | `deploy_agy_receiver` — Google Antigravity CLI executor receiver (`mode: agy`, agy band 9330-9339, default 9330), conversation-id discovery from `last_conversations.json`, plain-text prefix-strip transcript-tail extraction, remint on `not found`, boolean sandbox toggle, NO model selection, coexistence with cc/oc/codex in one repo |
| v0.8.5 | ✅ shipped | Per-mode 10-port bands (cc 9300-9309, oc 9310-9319, codex 9320-9329, agy 9330-9339) + `bind_port` auto-allocation: omit it to reuse this repo's existing port or auto-pick a free port in the band, skipping ports claimed by other repos' peers; explicit `bind_port` still honored. Multiple same-mode receivers across repos no longer collide |
