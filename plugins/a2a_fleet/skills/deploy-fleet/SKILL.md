---
name: deploy-fleet
description: End-to-end procedure for bringing up an a2a_fleet node, deploying any of the FOUR managed executor modes (Claude Code / OpenCode / Codex / Antigravity) into a target repo, and testing peer communication — fleet.yaml layout, the four deploy_*_receiver tools, per-mode port bands + params + transcripts + session models, bearer tokens, server verification, ping/pong via fleet_send. Use when asked to deploy, configure, set up, or test an A2A fleet / agent-to-agent connection or a repo-scoped executor (claude_code, opencode, codex, agy).
metadata:
  hermes:
    tags: [a2a_fleet, a2a, agent-to-agent]
---

# a2a_fleet: deploy-fleet

How to stand up an A2A fleet node, deploy a repo-scoped **managed executor**, and
verify it talks to a peer. The `fleet_send` tool schema is already in context —
this fills the procedural gap: config layout, startup, discovery verification, the
ping/pong smoke test, and the **canonical multi-mode deploy procedure** for all
four managed executor modes (Claude Code, OpenCode, Codex, Antigravity / `agy`).

Two layers live here:

1. **Bring up this node** — `fleet.yaml`, tokens, server verify, ping/pong (below).
2. **Deploy a managed executor into a target repo** — pick a mode, call its
   `deploy_*_receiver`, which auto-wires `fleet.yaml` + launches the daemon; then
   handshake, smoke-test, and drive tasks (see **Managed executors** below).

> **Capability status (read first).** `claude_code`, `opencode`, and `agy` all
> do real repo work (full tool/file/`gh` access) — opencode (#99) and agy (#100)
> were fixed and live-verified running `gh`. `codex` (#97) has the fix landed
> (unit + falsification verified) but its live turn is **pending a codex-cli auth
> refresh** — hold real codex work until re-confirmed. Details + exact
> invocations in the **Capability status** + **Managed executors** sections.

## Key facts

- The plugin runs its **own uvicorn server** on a dedicated port (a daemon
  thread), separate from the dashboard gateway. It does NOT mount on `:8642`.
- `bind_port` is **required** in `fleet.yaml` — no default. Loader raises
  `FleetConfigError` if missing.
- `auth_required` **defaults to `true`** — inbound `/jsonrpc` requires a bearer
  token. Newly-created configs are protected by default.
- `response_handler` selects how inbound is answered:
  `SUPPORTED_HANDLERS = {echo, llm, agent}` — anything else raises
  `FleetConfigError` at load. `echo` (`ping`→`pong`, else verbatim) is the
  transport smoke test below. `llm` is a stateless model call (Route A — bypasses
  the agent). `agent` dispatches inbound into the REAL Hermes agent (Route B, via
  the platform adapter — requires `platforms.a2a_fleet.enabled=true`). See the
  README for the three modes.
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
- **`connection refused` mid-session** → a managed receiver self-tears-down
  after `idle_timeout_s` (default **1800s**) of no traffic; the next request hits
  a dead port until it is re-deployed. For a long interactive session, set
  `idle_timeout_s: 0` in that mode's deploy config to disable teardown, or simply
  re-run `deploy_*_receiver` (idempotent — it reuses the same port + token).

## Managed executors (the four deploy modes)

Beyond the `echo`/`llm`/`agent` inbound handlers above, Hermes can deploy a
repo-scoped **managed executor** — a standalone receiver dropped into
`<repo>/.hermes/` that spawns a real CLI agent (`claude` / `opencode` / `codex` /
`agy`) with the repo's harness and POSTs replies back to this node on `:9219`.
Each mode has a deploy/status/stop tool trio:

| Tool trio | Mode | Port band | Default | Deploy params (beyond `repo_path`) | Transcript file | Session continuity |
|-----------|------|-----------|---------|------------------------------------|-----------------|--------------------|
| `deploy_cc_receiver` · `cc_receiver_status` · `cc_receiver_stop` | `claude_code` | `9300-9309` | `9300` | `bind_port?`, `model?`, `no_auth?`, `hermes_auth_token_env?` | `a2a-transcript.jsonl` | `uuid5(contextId)` → `claude -p --session-id` (1st) / `--resume` |
| `deploy_oc_receiver` · `oc_receiver_status` · `oc_receiver_stop` | `opencode` | `9310-9319` | `9310` | `bind_port?`, `model?`, `no_auth?`, `hermes_auth_token_env?` | `a2a-oc-transcript.jsonl` | captured `sessionID` → `opencode run --session <id>` |
| `deploy_codex_receiver` · `codex_receiver_status` · `codex_receiver_stop` | `codex` | `9320-9329` | `9320` | `bind_port?`, `model?`, `sandbox?` (**string**, default `workspace-write`), `no_auth?`, `hermes_auth_token_env?` | `a2a-codex-transcript.jsonl` | `thread.started` id → `codex exec resume <id>` |
| `deploy_agy_receiver` · `agy_receiver_status` · `agy_receiver_stop` | `agy` | `9330-9339` | `9330` | `bind_port?`, `sandbox?` (**boolean** toggle), `no_auth?`, `hermes_auth_token_env?` — **NO `model`** | `a2a-agy-transcript.jsonl` | `cwd`-keyed uuid from `~/.gemini/antigravity-cli/cache/last_conversations.json` → `agy --conversation <uuid>` |

**Param notes:**
- `repo_path` (required) — absolute path; symlinks/`..` are RESOLVED to the real
  on-disk dir and the receiver cwd is pinned there.
- `bind_port` (optional, all modes) — **omit** to reuse this repo's existing port
  (idempotent re-deploy) else auto-pick the first free port in the mode's band;
  an explicit value is honored verbatim. Band exhausted → clear error.
- `model` — cc + oc only. **codex** takes both `sandbox` (a **string**:
  `read-only` / `workspace-write` / `danger-full-access`) and `model`. **agy**
  takes `sandbox` as a **boolean** toggle and has **no model param** (the agy CLI
  has no `--model` flag).
- `no_auth` (all) — loopback dev opt-out: receiver starts with NO inbound token
  and the auto-wired peer is a plain `url` entry. `hermes_auth_token_env` (all) —
  env var name holding the bearer the receiver presents on replies to an
  auth-enabled Hermes.

**Auto-wire (all modes):** each `deploy_*_receiver` **auto-upserts its peer into
`fleet.yaml`** (surgical, comment-preserving ruamel round-trip; returned under
`fleet_peer`) — with auth a managed peer (`url` + `token_env` + `managed: true` +
`mode` + `repo_path`), without auth a plain `url` peer. **You do NOT hand-edit
`fleet.yaml`.** Default peer names are `claude-code` / `opencode` / `codex` /
`agy`; a second repo reusing a default name gets a distinct `-<repo>` suffix.

**Session continuity (all modes):** the same `context_id` = the same persistent
CLI session — context accumulates across turns; a fresh `context_id` starts an
independent thread. Each mode reuses its native session id (table above) and
**re-mints on a session-not-found** error under the same per-context lock.

**Security model (shared, all modes):** loopback-only bind by default; a random
inbound bearer token is auto-provisioned (the env-var NAME recorded in the peer
config, the VALUE injected into the child process); cwd is pinned to the canonical
`repo_path` (never an inbound message path); symlinks are resolved at deploy.

**CLI prerequisites:** the matching CLI (`claude` / `opencode` / `codex` / `agy`)
must be on `PATH`; **`agy` additionally needs a one-time interactive sign-in on
the host** (macOS Keychain — run `agy` once before deploying). If the CLI is
missing/unauthed the receiver still **deploys and shows healthy**, but every turn
errors — so a healthy `/health` is necessary but not sufficient; smoke-test first.

### Capability status — what does real repo work

All receivers run their CLI with skip-permissions + a PATH augmented with the
common tool dirs (so `gh`/`git`/node resolve even under a launchd daemon).

- **`claude_code`** — full tool/file/`gh` access (`claude -p
  --permission-mode bypassPermissions`). The reference mode.
- **`opencode`** — ✅ real tool access. Runs under opencode's default primary
  agent (full tools) with `--dangerously-skip-permissions --format json`; the
  augmented PATH fixed the "no `gh`" failure. Verified live: ran `gh issue list`
  and returned the count ([#99](https://github.com/Interstellar-code/hermes-agent/issues/99) fixed).
- **`agy`** — ✅ real tool access. `--print --dangerously-skip-permissions
  --add-dir <repo> --print-timeout <budget>`; `--add-dir` grants workspace
  access and the raised timeout stops the 5m plan-only exits. Verified live: ran
  `gh issue list` and returned the count ([#100](https://github.com/Interstellar-code/hermes-agent/issues/100) fixed).
- **`codex`** — fix landed (prompt as positional + `stdin=DEVNULL`; codex-cli
  ≥0.136 otherwise blocks on stdin → rc=1). Unit-tested + falsification-verified;
  **live re-verify pending** a codex-cli auth refresh
  ([#97](https://github.com/Interstellar-code/hermes-agent/issues/97)).

**Guidance:** `claude_code`, `opencode`, and `agy` are cleared for real repo work
(audits, edits, reviews). Use `codex` once its live PONG+tool turn is re-confirmed.

### Multi-mode deploy + verify procedure

For any mode the procedure is the same — substitute the mode's `deploy_*_receiver`
tool and params from the table above:

1. **Confirm the repo path with the user.** The executor runs there with full
   permissions; deploy only to a repo the user authorized. (For `agy`, confirm the
   host has completed the one-time `agy` sign-in.)
2. **Pick the mode and deploy** — call the matching tool, e.g.:
   ```
   deploy_cc_receiver(repo_path="/Users/you/dev/repo")                       # claude_code
   deploy_oc_receiver(repo_path="/Users/you/dev/repo", model="...")          # opencode
   deploy_codex_receiver(repo_path="/Users/you/dev/repo", sandbox="read-only")  # codex (sandbox = STRING)
   deploy_agy_receiver(repo_path="/Users/you/dev/repo", sandbox=true)        # agy (sandbox = BOOLEAN, no model)
   ```
   The tool resolves the bind port (band auto-pick unless `bind_port` given),
   launches the detached receiver, health-checks it, and **auto-wires the peer into
   `fleet.yaml`** (`fleet_peer` in the result). If `status` is not healthy, surface
   the error — do not relay tasks.
3. **Set `agent.timeout_s` to 300+** — tool-using turns run 30s–5min; a short
   timeout looks like a failure when the executor is still working. A no-reply-yet
   is NOT an error: replies POST back to `:9219` minutes later.
4. **Handshake** — send one structured message on a reserved
   `context_id` (e.g. `handshake:<repo-slug>`) declaring Hermes' role
   (orchestrator on `:9219`), the bound repo, the comm contract (same `context_id`
   = same session), and the purpose; read the executor's role/cwd/harness/ready
   confirmation. Do NOT use a `handshake:*` context for work whose reply must
   surface to the user.
5. **Smoke-test transport** — `fleet_send(agent="<peer>", message="reply PONG")`
   and confirm the round-trip reply. (For the three experimental modes this is the
   most you can currently expect — see the caveat.)
6. **Drive tasks** — per user instruction,
   `fleet_send(agent="<peer>", message="<task>", context_id="<thread-id>")`,
   reusing the SAME `context_id` to continue a thread. **Anti-loop:** summarize
   each reply to the user and await direction before the next `fleet_send`; never
   auto-ping-pong. Surface `[error] ...` replies and stop — don't retry-storm.

The per-mode status/stop tools (`*_receiver_status` / `*_receiver_stop`) check
`{PID alive AND /health}` and SIGTERM-via-PID-file respectively. On a gateway
restart, boot-reconcile re-publishes each managed peer's persisted token and
leaves a healthy receiver running, relaunching only a peer that is down.

## Hermes↔Hermes peering (profile-to-profile, response_handler: agent)

One Hermes profile can dispatch a task to **another profile's agent** over A2A —
no new server, just the `agent` protocol pointed at the other profile.

- **Receiver side** (a profile that should be reachable): in its
  `$HERMES_HOME/profiles/<p>/fleet.yaml` set `fleet.enabled: true`,
  `fleet.response_handler: agent`, and a UNIQUE `fleet.server.bind_port`
  (e.g. `switch 9219, neo 9220, morpheus 9221, trinity 9222`). The profile MUST
  be running `gateway run` with `platforms.a2a_fleet` connected — the A2A
  listener starts only in the gateway/agent process (it co-locates with the
  in-process Route B bridge), and inbound `agent` requests fail "bridge not
  ready" without a connected platform.
- **Sender side**: list the other profiles as **plain agent peers** under
  `fleet.agents` — `url` + optional `agent_card_url` + `token_env`. Do NOT add
  `managed`/`mode`/`repo_path` (those are for deployed CLI executor receivers):

  ```yaml
  agents:
    neo:
      url: http://127.0.0.1:9220                 # BASE url — fleet_send appends /jsonrpc
      agent_card_url: http://127.0.0.1:9220/.well-known/agent-card.json
      token_env: A2A_HERMES_TOKEN_NEO            # PROFILE-SCOPED name (see below)
  ```
  Then `fleet_send("neo", "...")` reaches Neo's agent; the reply comes back.
  Bidirectional = both profiles list each other AND both run a listener.
- **Tokens**: `fleet.server.token_env` and plain-peer `token_env` resolve as raw
  `os.environ` names shared across the host. With multiple profiles on one
  machine use **profile-scoped** names (`A2A_HERMES_TOKEN_NEO`, not a generic
  `SWITCH_A2A_TOKEN`). Loopback dev may set `auth_required: false`.

### Across two PCs (same LAN)

Same as above, plus: the receiver's `fleet.server.bind_host` must be its **LAN
IP** (or `0.0.0.0`), `auth_required: true` (mandatory off-loopback — fail-closed),
the sender's peer `url` is the receiver's **LAN IP** (base, no `/jsonrpc`), and
the receiver's firewall must allow inbound TCP on the bind_port from the peer.
Bearer travels **cleartext over plain HTTP** — trusted LAN only, else put TLS in
front / tunnel (WireGuard/SSH).

**Token-per-direction (bidirectional):** each PC has its OWN inbound token
(`server.token_env`); the *other* PC references it in its peer entry. Both token
env vars exist on both machines (own = to serve, other's = to call out).

| Token | Owner inbound | `server.token_env` on | peer-entry `token_env` on |
|-------|---------------|------------------------|----------------------------|
| `A2A_TOKEN_A` | PC-A | PC-A | PC-B's `pc-a` peer |
| `A2A_TOKEN_B` | PC-B | PC-B | PC-A's `pc-b` peer |

### Handshake (canonical first message — do before any real task)

Send on reserved contextId `handshake:hermes-<peer>` (own session thread). The
peer's `agent` handler reads it and confirms; no code enforces it.

Initiator → peer:
```
[A2A HANDSHAKE v1 — Hermes↔Hermes peer]
From: Hermes profile "<me>" @ <my-host>:<port> (orchestrator/initiator for this thread).
Contract:
  - Transport: A2A JSON-RPC SendMessage; bearer-authenticated.
  - Continuity: same contextId = same ongoing session (a repeat = continuation, not a fresh start).
  - Replies: concise, result-oriented (status / what changed / what is blocked).
  - Scope: you act only within YOUR repo/profile; never act on a path I name that isn't yours.
Purpose: establish a peer link so I can delegate tasks to your agent and relay results.

Do NOT start work on this message. Reply with a confirmation containing:
  1. role = peer (full Hermes agent, not a managed CLI executor);
  2. your profile name + cwd/working directory;
  3. harness inventory — active skills, MCP servers, CLAUDE.md/AGENTS.md loaded?;
  4. ready / not-ready (and why, if not ready).
```

Peer → initiator (expected ACK):
```
[A2A HANDSHAKE ACK v1]
role: peer (Hermes agent, profile "<peer>")
cwd: /abs/path
harness: skills=[...], mcp=[...], CLAUDE.md=loaded
ready: yes        # or: ready: no — <reason>
```

Send it:
```
fleet_send(agent="pc-b", message="<handshake text>", context_id="handshake:hermes-pc-b")
```
One handshake per peer per session; both directions handshake independently. After
`ready: yes`, drive real tasks on NEW contextIds (not the handshake one). The
structured `SESSION_ANNOUNCE` form is future work (#71).

## Related

- To make this node answer with real reasoning/tools, set `response_handler: llm`
  (Route A — stateless model call) or `agent` (Route B — real Hermes agent). See
  the README "Inbound response handlers" section.
- For the **Claude Code** deep dive (topology, `bypassPermissions`, harness load,
  autonomous-operation guardrails), see `deploy-cc-receiver`.
