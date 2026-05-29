---
name: deploy-fleet
description: End-to-end procedure for bringing up an a2a_fleet node and testing peer communication — fleet.yaml layout, bearer tokens, server verification, ping/pong via fleet_send. Use when asked to deploy, configure, set up, or test an A2A fleet / agent-to-agent connection.
metadata:
  hermes:
    tags: [a2a_fleet, a2a, agent-to-agent]
---

# a2a_fleet: deploy-fleet

How to stand up an A2A fleet node and verify it talks to a peer. The
`fleet_send` tool schema is already in context — this fills the procedural gap:
config layout, startup, discovery verification, and the ping/pong smoke test.

## Key facts

- The plugin runs its **own uvicorn server** on a dedicated port (a daemon
  thread), separate from the dashboard gateway. It does NOT mount on `:8642`.
- `bind_port` is **required** in `fleet.yaml` — no default. Loader raises
  `FleetConfigError` if missing.
- `auth_required` **defaults to `true`** — inbound `/jsonrpc` requires a bearer
  token. Newly-created configs are protected by default.
- Only `response_handler: echo` is supported in v0.1. `ping` → `pong`, anything
  else echoes verbatim. `llm` raises `FleetConfigError`.
- Tokens are never in the YAML — `token_env` names an environment variable.
  Convention: `<PEER>_A2A_TOKEN`.
- Agent Card is served PUBLIC (no auth) at `/.well-known/agent-card.json`.

## Config layout (`$HERMES_HOME/fleet.yaml`)

```yaml
fleet:
  enabled: true
  response_handler: echo
  server:
    bind_host: 0.0.0.0          # 127.0.0.1 for loopback-only
    bind_port: 9219             # REQUIRED, pick a free port
    auth_required: true
    token_env: SWITCH_A2A_TOKEN # this node's inbound token
  self:
    name: switch
  agents:
    construct:
      url: http://10.0.0.5:9220 # peer base URL (/jsonrpc appended automatically)
      token_env: CONSTRUCT_A2A_TOKEN
      description: "Construct peer"
```

## Procedure

1. **Set env tokens** before starting the agent process:
   ```bash
   export SWITCH_A2A_TOKEN=<this-node-inbound-secret>
   export CONSTRUCT_A2A_TOKEN=<token-the-peer-expects>
   ```
   If `auth_required: true` and no token resolves, `/jsonrpc` returns HTTP 503.
2. **Enable + dependencies**: `hermes plugins enable a2a_fleet`. The inbound
   server needs `fastapi` + `uvicorn` (`hermes-agent[web]`); without them the
   plugin loads but the server stays idle (outbound `fleet_send` still works).
3. **Start / restart**: `hermes gateway restart`. Confirm in the agent log:
   ```
   a2a_fleet: registered fleet_send tool + spawned A2A server thread
   a2a_fleet: server started on 0.0.0.0:9219
   ```
4. **Verify discovery (public, no auth)**:
   ```bash
   curl http://<bind_host>:<bind_port>/.well-known/agent-card.json
   curl http://<bind_host>:<bind_port>/health   # → {"ok":true,...,"peer_count":N}
   ```
5. **Test the local JSON-RPC endpoint** (auth path):
   ```bash
   curl -s -X POST http://<bind_host>:<bind_port>/jsonrpc \
     -H "Authorization: Bearer $SWITCH_A2A_TOKEN" \
     -H "content-type: application/json" \
     -d '{"jsonrpc":"2.0","id":"1","method":"SendMessage",
          "params":{"message":{"role":"user","parts":[{"text":"ping"}]}}}'
   # result.message.parts[0].text == "pong"
   ```
   Same request without the bearer header → HTTP 401.
6. **Test a peer via the agent tool**: call `fleet_send(agent="construct",
   message="ping")`. Expect `{"reply": "pong"}`. On failure you get
   `{"error": "..."}` (never a raised exception) — read the string:
   - `HTTP 401` → token mismatch between this node's `CONSTRUCT_A2A_TOKEN` and
     the peer's inbound `token_env`.
   - `network error` → peer not reachable / wrong `url` / port.
   - `unknown agent` → peer name not in `fleet.agents`.
7. **CLI smoke test** (optional, outside the agent):
   ```bash
   cd plugins && HERMES_HOME=~/.hermes python -m a2a_fleet.client construct ping
   # → pong
   ```

## Success criteria

- Agent log shows `server started on <host>:<port>`.
- Agent Card + `/health` return 200 with no auth.
- `/jsonrpc` ping returns `pong` with a valid bearer, 401 without.
- `fleet_send` to the peer returns `{"reply": "pong"}`.

## Pitfalls

- **503 on `/jsonrpc`** → `auth_required: true` but token env unset/empty.
- **Server never starts** → `fastapi`/`uvicorn` missing, or `bind_port` already
  in use (check the log for the start error).
- **Cross-machine bearer over plain HTTP** → tokens travel in cleartext;
  terminate TLS in front when binding to a non-loopback address.
- **No CORS** → expected; A2A is server-to-server, browsers are not clients.
