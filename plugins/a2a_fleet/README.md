# a2a_fleet

Version: `0.1.0`

Agent-to-Agent (A2A) communication for Hermes Agent. Lets one Hermes profile send plain-text messages to peer agents over JSON-RPC 2.0 and exposes this profile as a discoverable A2A fleet member.

v0.1 ships an **echo handler** (ping → pong) — the wiring is complete end-to-end; the response logic is deliberately trivial. TaskManager, streaming (SSE), and an LLM-backed handler are deferred to v0.2+.

> **Authoritative source**: this README matches the shipped code (`server.py` embedded-uvicorn architecture). `PROGRESS.md` is the as-built ralph-loop record; `CHANGELOG.md` tracks notable changes.

---

## Architecture

The plugin runs its **own** FastAPI/uvicorn server on a dedicated port — it does **not** mount routes on the Hermes dashboard gateway. This isolation sidesteps the gateway's session-token middleware, localhost-only CORS, and Host-header validation, all of which would block cross-machine peer access.

```
┌────────────────────────────────────────────┐
│           Hermes Agent Process              │
│                                             │
│  ┌────────────────────┐                     │
│  │  Dashboard Gateway  │  localhost:8642     │
│  └────────────────────┘                     │
│                                             │
│  ┌────────────────────────────────────┐     │
│  │  a2a_fleet server (server.py)       │     │
│  │  uvicorn on its own event loop,     │     │
│  │  running on a daemon thread,        │     │
│  │  bound to fleet.yaml bind_host:port │     │
│  └────────────────────────────────────┘     │
└────────────────────────────────────────────┘
```

- `register(ctx)` registers the `fleet_send` tool and spawns the server on a **named daemon thread** with its own `asyncio` event loop (`_start_server_in_thread`). This works whether `register()` is called from a synchronous context or inside a running loop — the daemon-thread design fixes the "server never starts in sync context" bug.
- An `atexit` handler (`_atexit_stop`) signals uvicorn to exit on process shutdown. There is **no** `disable()` hook in v0.1; the daemon thread is reaped by the OS on exit.
- Config (`fleet.yaml`) is re-read on **every request**, so handler/peer edits take effect without a server restart.

### Routes

The server (bound to `fleet.server.bind_host:bind_port`) serves:

| Method | Path | Auth | Description |
|--------|------|------|-------------|
| `GET` | `/.well-known/agent-card.json` | **public** | A2A capability discovery (RFC 8615). Always anonymous. |
| `GET` | `/health` | public | `{"ok": true, "version": "0.1.0", "peer_count": N}` |
| `POST` | `/jsonrpc` | bearer (if `auth_required`) | A2A JSON-RPC 2.0 `SendMessage` endpoint |

Interactive docs (`/docs`, `/redoc`, `/openapi.json`) are disabled — this is a peer-facing surface.

**No CORS middleware.** A2A is server-to-server; browsers are not A2A clients. Wildcard CORS would be misleading, so it is intentionally omitted.

---

## Configuration

Config lives in a standalone `fleet.yaml` under the active Hermes home (`$HERMES_HOME/fleet.yaml`). For early-checkout compatibility, the loader falls back to `$HERMES_HOME/profiles/<name>/fleet.yaml` if the primary file is absent.

```yaml
fleet:
  enabled: true                   # set false to keep the plugin idle for this profile
  response_handler: echo          # only "echo" is supported in v0.1 (anything else → FleetConfigError)

  server:
    bind_host: 0.0.0.0            # default 127.0.0.1
    bind_port: 9219              # REQUIRED — no default; pick a free port per profile
    auth_required: true          # DEFAULT true — inbound /jsonrpc requires a bearer token
    token_env: SWITCH_A2A_TOKEN  # env var holding THIS node's inbound bearer token

  self:
    name: switch                 # name advertised in the Agent Card

  agents:                        # peers this node can call via fleet_send
    construct:
      url: http://10.0.0.5:9220              # peer's base URL (/jsonrpc appended by the client)
      agent_card_url: http://10.0.0.5:9220/.well-known/agent-card.json   # optional
      token_env: CONSTRUCT_A2A_TOKEN          # env var holding the token used to call this peer
      description: "Construct build agent"
```

### Key config facts

- **`bind_port` is required** — `FleetConfigError` if missing. No default.
- **`auth_required` defaults to `true`.** Newly-created profiles opt into bearer protection automatically. If you copy an example with no `auth_required` line, the server enforces bearer tokens.
- **`response_handler` must be `echo`** in v0.1. `llm` or any other value raises `FleetConfigError` at load time.
- Peer `url` must be `http`/`https` with a real host or load fails.
- Tokens are never stored in `fleet.yaml`. Each `token_env` names an environment variable holding the actual pre-shared bearer token. Convention: `<PEER>_A2A_TOKEN`.

### Auth behavior

- `auth_required: true` + no resolved token → server returns **HTTP 503** (misconfig; does not leak the `token_env` name).
- Missing/malformed `Authorization: Bearer ...` → **HTTP 401**.
- Bearer comparison uses `hmac.compare_digest` (constant-time, resists timing attacks).
- Sending plaintext bearer tokens over non-loopback HTTP is inadvisable — terminate TLS in front of the server when binding to a public address.

---

## The `fleet_send` tool (agent-facing)

The plugin registers exactly **one** agent tool in v0.1:

```json
{
  "name": "fleet_send",
  "parameters": {
    "type": "object",
    "properties": {
      "agent":   {"type": "string", "description": "Name of the fleet peer (matches fleet.yaml)."},
      "message": {"type": "string", "description": "Plain-text message to send to the peer agent."}
    },
    "required": ["agent", "message"]
  }
}
```

Returns `{"reply": "..."}` on success or `{"error": "..."}` on any failure (network error, peer 401, JSON-RPC error). It never raises — the calling agent can surface the string verbatim.

> `fleet_status`, `fleet_discover`, and `fleet_get_agent_card` do **not** exist in v0.1.

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

`POST /jsonrpc` accepts raw JSON (no Pydantic in route signatures):

- `SendMessage` → returns `{result: {kind: "message", message: {role: "agent", parts: [{text}], contextId}}}`.
- Malformed JSON → HTTP 200 with JSON-RPC error `-32700`.
- Non-object body → `-32600`; non-object `params` → `-32602`.
- `SendStreamingMessage`, `tasks.get`, `tasks.list`, `tasks.cancel` → `-32601` ("deferred to v0.2+").
- Any other method → `-32601` ("Method not found").

The echo handler: `ping` → `pong`; anything else echoes the input verbatim.

---

## Install / enable

```bash
hermes plugins enable a2a_fleet
hermes gateway restart
```

The inbound server requires `fastapi` + `uvicorn` (install `hermes-agent[web]`). If those are missing, the plugin loads but the server stays idle and logs a warning — `fleet_send` (outbound) still works.

Look for in the agent log:
```
a2a_fleet: registered fleet_send tool + spawned A2A server thread
a2a_fleet: server started on 0.0.0.0:9219
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
| `__init__.py` | `register(ctx)` — registers `fleet_send`, spawns server daemon thread, registers `atexit` stop |
| `server.py` | FastAPI app factory (`build_app`), Agent Card builder, JSON-RPC handler, uvicorn lifecycle (`start_server`/`stop_server`/`stop_server_sync`) |
| `fleet_config.py` | `fleet.yaml` loader, env-var token resolution, fail-fast validation, `SUPPORTED_HANDLERS = {"echo"}` |
| `fleet_tools.py` | `fleet_send_handler` — wraps the client in a `{reply}`/`{error}` dict |
| `client.py` | Async A2A client (`send_message`) over httpx + `__main__` CLI |
| `response_handler.py` | `echo_handler(text, context_id)` |
| `plugin.yaml` | Hermes plugin manifest |
| `references/` | A2A spec summary + Hermes plugin guide |

Tests live in the repo `tests/` tree at `tests/plugins/a2a_fleet/` (9 modules): agent card, client, config, JSON-RPC echo, server lifecycle, concurrent/sync register, blocker fixes, hardening.

```bash
cd ~/.hermes/hermes-agent
venv/bin/python -m pytest tests/plugins/a2a_fleet/ -q
```

---

## Roadmap

| Phase | Status | Description |
|-------|--------|-------------|
| v0.1 | ✅ shipped | Embedded uvicorn server, Agent Card discovery, JSON-RPC `SendMessage`, bearer auth, echo handler, `fleet_send` tool, async client |
| v0.2 | planned | LLM-backed response handler, TaskManager (`tasks.*`), streaming (`SendStreamingMessage` / SSE) |
