---
name: deploy-cc-receiver
description: End-to-end procedure for deploying a Claude Code executor receiver into a target repo so Hermes can orchestrate Claude Code over A2A — confirm the repo path, call deploy_cc_receiver, run the roles/repo/comm/purpose handshake, then relay tasks via fleet_send and monitor replies. Use when asked to deploy Claude Code as an executor, set up a repo-scoped A2A executor, or have Hermes drive Claude Code in a specific repo.
metadata:
  hermes:
    tags: [a2a_fleet, a2a, agent-to-agent, claude-code, executor, orchestrator]
---

# a2a_fleet: deploy-cc-receiver

> **Status: shipped.** `deploy_cc_receiver` / `cc_receiver_status` /
> `cc_receiver_stop` are live tools and the standalone receiver ships in
> `templates/cc_receiver.py`. This skill is the Claude-Code-specific deep dive.
> **Claude Code is the ONLY mode with full tool/file/gh access today** — for the
> three EXPERIMENTAL modes (opencode / codex / agy) and the canonical multi-mode
> deploy procedure, see the `deploy-fleet` skill.

How to make **Claude Code** a repo-scoped executor in the fleet. Hermes is the
**orchestrator**; Claude Code is the **executor** running inside one specific
repo with that repo's FULL harness — skills, MCP, plugins, `.claude/` settings,
`CLAUDE.md`, claude-mem. The whole point of routing through Claude Code (not the
plain `llm` handler) is to leverage that harness: exactly what the user would
have manually, now driven by Hermes over A2A.

## Topology

```
              fleet_send (outbound)
Hermes agent ──────────────────────────▶  cc_receiver :930x  (deployed in <repo>/.hermes/)
(:9219, response_handler: agent)                    │  spawns claude -p  cwd=<repo>
      ▲                                             │  (full repo harness, persistent session)
      └──────── reply POST :9219 ◀──────────────────┘
```

- This node (Hermes, `:9219`, `response_handler: agent`) receives Claude's
  replies as real Hermes-agent turns (Route B — see `deploy-fleet` / README).
- The `cc_receiver` (`:930x` — auto-allocated in the `9300-9309` band) is a NEW
  standalone Claude-side peer, NOT a Hermes `response_handler`. Hermes deploys,
  owns, and launches it as a managed daemon.

## Key facts

- **Many repos, one band.** Claude Code receivers own the **`9300-9309`** port
  band (10 ports). `deploy_cc_receiver` with no `bind_port` **reuses this repo's
  existing port** on re-deploy (idempotent) else **auto-picks the first free port
  in the band**, skipping ports claimed by other repos' peers; an explicit
  `bind_port` is honored verbatim. Band start (`9300`) is the default. Multiple
  same-mode receivers across different repos coexist without colliding; the band
  being exhausted yields a clear error.
- **cwd is pinned at deploy time** to `repo_path` — NEVER taken from an inbound
  message. Claude runs with `bypassPermissions` inside that repo, so it can do
  anything there: only deploy to repos the user has explicitly authorized.
- **Persistent session per A2A thread**: the same `context_id` maps to the same
  `claude -p` session (`--session-id` first turn, `--resume` after), so context
  accumulates across turns. Different `context_id` = different session.
- The receiver loads the repo harness via `--setting-sources user,project,local`
  + `--mcp-config <repo>/.mcp.json` and **no `--bare`** (so `CLAUDE.md` + hooks
  load). The role is written to `<repo>/.hermes/A2A.md` and pulled in via a single
  `@import .hermes/A2A.md` line appended to `<repo>/CLAUDE.md` — keeps the role
  text out of the tracked `CLAUDE.md` (no git pollution) while still auto-loading.
- The receiver self-terminates after an idle timeout (no messages for N min);
  Hermes redeploys on the next request.
- Replies are POSTed back to Hermes on `:9219` with the same `context_id`.

## fleet.yaml peer schema (repo-aware) — AUTO-WIRED

A Claude Code peer carries repo binding; `load_fleet()` surfaces these fields:

```yaml
fleet:
  agents:
    claude-code:
      url: http://127.0.0.1:9301             # the auto-allocated band port
      repo_path: /Users/you/dev/some-repo    # the bound repo (cwd of claude -p)
      managed: true                          # Hermes owns/launches the daemon
      mode: claude_code                      # distinguishes from plain url/token peers
      token_env: A2A_CC_TOKEN_SOME_REPO_1A2B3C4D   # the receiver's stable token env name
```

### fleet.yaml peer entry (after deploy) — written FOR you

`deploy_cc_receiver` **auto-upserts** this peer into `fleet.yaml` for you — a
surgical ruamel round-trip that **preserves your comments and formatting**. You
do **NOT** hand-edit `fleet.yaml`. The upsert result is returned under
`fleet_peer`; a config-write hiccup is a non-fatal warning (the receiver is
already healthy). With auth it writes a managed peer (`url` + `token_env` +
`managed: true` + `mode: claude_code` + `repo_path`); a `no_auth` deploy writes a
plain `url` peer. The default peer name is `claude-code`; a second repo reusing
that default name gets a distinct `-<repo>` suffix.

The deploy result also echoes the exact values it wired:

- `repo_path` — the canonical repo path,
- `port` — the bound port (auto-allocated in the `9300-9309` band, or your
  explicit `bind_port`),
- `receiver_token_env` — the **stable** inbound-token env var NAME (e.g.
  `A2A_CC_TOKEN_<SLUG>_<HASH12>`); the same name every redeploy, so it can be
  referenced persistently. The token VALUE is fresh per deploy and is published
  into the gateway's environment + the child's env AND persisted (on a successful
  deploy) to `<repo>/.hermes/.token` (chmod 0600) so a gateway restart can
  re-publish the same token and leave a healthy receiver running. The deploy also
  writes `<repo>/.hermes/.gitignore` so `.token` / `*.pid` / `*.log` / inbox +
  transcript runtime files are never committed (the tracked `A2A.md` @import is
  fine to commit).

With `token_env` set, `fleet_send(agent="claude-code", ...)` resolves the bearer
from the gateway environment and presents it on the receiver's `POST /jsonrpc`.
With `managed: true` + `mode: claude_code` + `repo_path`, boot-reconcile on the next
gateway start **leaves a healthy receiver running** (it re-publishes the persisted
`.token` so in-session `fleet_send` keeps working — it does NOT kill an executor
that may be mid-task) and only redeploys when the receiver is down (then the token
is re-minted and `.token` rewritten — receiver conversation context survives via
the claude `--resume` session files). The desired bind port comes from this peer's
`url`, not from on-disk state.

`token_env` for a managed `claude_code` peer **must** equal the `receiver_token_env`
the deploy wrote (the stable per-repo name); `load_fleet` rejects a mismatch so
the gateway and receiver never resolve different vars. Because the deploy
auto-wires this peer, the mismatch only arises if you later hand-edit `fleet.yaml`.

## Procedure

End-to-end: **confirm repo → `deploy_cc_receiver` (auto-wires `fleet.yaml`) →
handshake → (per task) `fleet_send` + monitor + summarize to the user + await
direction.**

1. **Ask the user for the target repo path, then CONFIRM it back before acting.**
   Do not proceed on an assumed path. Example:
   > "I'll deploy a Claude Code executor into `/Users/you/dev/some-repo` — Claude
   > will run there with `bypassPermissions` and that repo's full harness.
   > Confirm this is the repo to authorize?"
   Only continue after the user confirms. The repo must exist, be a directory,
   and ideally be a git repo (`deploy_cc_receiver` refuses otherwise).

2. **Deploy the receiver** — call the tool:
   ```
   deploy_cc_receiver(repo_path="/Users/you/dev/some-repo")
   ```
   It (deterministic, side-effecting):
   - canonicalizes `repo_path` (RESOLVES symlinks / `..` to the real on-disk
     target and pins the cwd there — symlinked inputs are accepted, not rejected;
     security is preserved because the receiver only ever runs in the real dir),
   - copies the receiver into `<repo>/.hermes/cc_receiver.py`,
   - writes binding config `<repo>/.hermes/a2a_receiver.json` (cwd pinned,
     atomic temp-file + rename),
   - writes the A2A-role text to `<repo>/.hermes/A2A.md` and appends a single
     idempotent `@import .hermes/A2A.md` line to `<repo>/CLAUDE.md` (between
     `<!-- a2a-fleet:start -->` / `:end -->`; creates `CLAUDE.md` if absent,
     never clobbers existing content) — role text stays out of tracked files,
   - resolves the bind port: an explicit `bind_port` is honored verbatim; omit it
     to reuse this repo's already-configured port (idempotent re-deploy) or
     auto-pick the first free port in the `claude_code` band `9300-9309`,
   - stops any existing receiver for this repo before launching a fresh one,
   - launches the receiver **detached** (survives gateway restart) on the resolved
     `:930x`, records `<repo>/.hermes/cc_receiver.pid`, health-checks `/health`,
   - on a SUCCESSFUL deploy (after health passes) persists the provisioned token
     to `<repo>/.hermes/.token` (chmod 0600) and writes/updates
     `<repo>/.hermes/.gitignore` (`.token`, `*.pid`, `*.log`, `a2a-inbox*`,
     `a2a-transcript*`, `a2a-inbox.offset`). On a failed/unhealthy deploy NO token
     is published to the gateway env and NO `.token` is written (no secret leak),
   - **auto-upserts the `claude-code` peer into `fleet.yaml`** (comment-preserving
     ruamel round-trip; result under `fleet_peer`) — you do NOT hand-edit it.

   Optional params: `bind_port` (see above), `model` (pin a claude model, e.g.
   `"sonnet"` / `"opus"`), `no_auth` (loopback dev opt-out — receiver starts with
   NO inbound token; writes a plain `url` peer), `hermes_auth_token_env` (env var
   name holding the bearer the receiver presents on replies to an auth-enabled
   Hermes).

   Returns `{deployed, pid, port, repo_path, status, receiver_token_env, fleet_peer}`.
   If `status` is not healthy, surface the error to the user — do not start relaying
   tasks.

3. **Set the turn timeout.** The peer block is already wired by the deploy, so the
   only thing to confirm is a generous turn timeout — **`agent.timeout_s` must be
   300+** (a `claude -p` turn that uses tools runs 30s–5min; a short timeout will
   look like a failure when the executor is simply still working). `load_fleet`
   logs a warning (not an error) when a managed `claude_code` peer is configured
   with `agent.timeout_s` below 300. A no-reply-yet is NOT an error: the async
   reply POSTs back to `:9219` minutes later.

4. **Handshake** — one-shot, before any real task. Send the executor a structured
   first message on a reserved `context_id` (e.g. `handshake:<repo-slug>`) and read
   the confirmation. The deployed role text (`<repo>/.hermes/A2A.md`) already tells
   a fresh `claude -p` to recognize a handshake and answer with the confirmation
   below.

   **Hermes → Claude** (copy-usable; fill the `<...>` fields):
   ```
   fleet_send(
     agent="claude-code",
     context_id="handshake:<repo-slug>",
     message="""[A2A HANDSHAKE]
   Hermes role: orchestrator (node http://127.0.0.1:9219).
   Bound repo: <repo_path>  (your cwd is pinned here).
   Comm contract: same context_id = the same persistent Claude session (context
     accumulates); your replies POST back to Hermes on :9219.
   Purpose/scope: you are the executor for THIS repo; I relay user tasks, you plan
     and execute them here using your full harness, and reply concisely with
     status/results. I summarize each reply to the user and await direction before
     the next instruction — no autonomous loop.
   Please confirm: (1) role = executor, (2) the repo/cwd you are operating in,
   (3) a brief harness inventory (skills / MCP / CLAUDE.md active), (4) ready or
   not-ready.""",
   )
   ```

   **Claude → Hermes** (the receiver's `claude -p` reply): role=executor; the repo
   it's operating in (echoed `cwd`); harness loaded (skills / MCP / CLAUDE.md
   inventory); ready / not-ready.

   Report roles + readiness + the harness inventory back to the user. If the
   harness did not load (e.g. `.mcp.json` absent/malformed), say so — the feature
   runs with a reduced harness, never silently.

5. **Relay tasks** — for each user task, call:
   ```
   fleet_send(agent="claude-code", message="<task>", context_id="<thread-id>")
   ```
   Reuse the SAME `context_id` for a continuing conversation/thread (persistent
   Claude session); use a fresh one to start an independent thread. Frame the task
   so it stays within the bound repo (the executor's cwd is pinned there).

6. **Monitor → summarize → await direction (anti-loop — critical).** Claude's
   reply arrives back on `:9219` as a real Hermes-agent turn, possibly minutes
   later. When it arrives:
   - **SUMMARIZE it to the user** (status, what changed, what's blocked), then
     **WAIT for the user's direction** before the next `fleet_send`. Do NOT
     auto-reply to every inbound turn — no autonomous ping-pong between Hermes and
     the executor. One `fleet_send` per user instruction.
   - **A no-reply-yet is not a failure.** The turn may still be running (tool use
     can take minutes). Do not retry or re-send while a turn is in flight; just
     keep waiting, or tell the user it's still working.
   You are the liaison between the user and the executor — feed the next
   instruction back via `fleet_send` with the same `context_id` only after the
   user directs it.

## Autonomous-operation guardrails

`bypassPermissions` + "plan and execute autonomously" is powerful — bound it:

- **Anti-loop (critical).** Hermes does NOT auto-reply to every inbound A2A turn.
  When Claude's reply lands on `:9219`, summarize it to the user and **wait for the
  user's direction** before the next `fleet_send`. No autonomous ping-pong; one
  `fleet_send` per user instruction.
- **Timeout / async replies.** Set `fleet.yaml agent.timeout_s` to **300+**
  (tool-using turns run 30s–5min). A no-reply-yet is **not** a failure — the async
  reply can arrive minutes later. Don't re-send or retry while a turn is in flight.
- **One in-flight turn per `context_id`.** Two overlapping `--resume <same>` turns
  corrupt the session; the receiver serializes per `context_id` and returns
  "busy, retry" for a second concurrent turn. Respect it — don't retry-storm.
- **Receiver-side bounds.** Anti-loop is enforced ORCHESTRATOR-side (here): the
  receiver runs NO handshake state machine. It enforces only its own bounds —
  per-`context_id` serialization (one in-flight turn per context), concurrency cap
  (`max_concurrent_turns`), and an idle timeout (tears itself down after no
  messages for N min). So: a "busy" response means wait, not retry; and if the
  receiver has idled out, the next request needs a fresh deploy (or boot-reconcile
  relaunch) — don't hammer a torn-down port.
- **Error handling — surface, don't silently retry.** If Claude's reply is an
  `[error] ...` (e.g. `claude` not found, permission error, broken session) or the
  receiver is unreachable, **surface it to the user clearly** and stop. Do not loop
  re-sending. Re-deploy / `cc_receiver_status` to diagnose if needed.
- **Authorized repo only / scope discipline.** cwd is pinned at deploy; never pass
  a repo/cwd from a message. Claude operates ONLY in the bound repo — frame tasks
  accordingly. Deploy only where the user authorized.

## Success criteria

- `deploy_cc_receiver` returns `{status: healthy}`; the receiver's `/health` is
  up; the `claude-code` peer is wired in `fleet.yaml` (`fleet_peer` in the
  result); the managed `CLAUDE.md` block is present; a PID is tracked.
- Handshake confirms roles (orchestrator / executor), the bound repo, and the
  comm contract; harness inventory reported.
- `fleet_send(agent="claude-code", ...)` runs `claude -p` IN the repo with its
  harness (verify via a repo-specific skill/file question) and replies on
  `:9219`.
- Multi-turn: the same `context_id` continues the same persistent Claude session.

## Pitfalls

- **Harness silently not loaded** → check `--setting-sources` / `--mcp-config`;
  the handshake harness inventory should list the repo's skills/MCP. A reduced
  inventory means the repo settings didn't load.
- **"busy, retry" on the receiver** → a turn for that `context_id` is still running;
  wait for it, don't fire a second concurrent turn on the same context.
- **Gateway restart** → boot-reconcile re-publishes each managed peer's persisted
  `.token` and **leaves a healthy receiver running** (it never kills an executor
  that may be mid-task); it relaunches ONLY a `managed` peer that is down. If a
  peer is still down after that, re-run `deploy_cc_receiver` (or
  `cc_receiver_status` / `cc_receiver_stop`).
- **Deploying to an unauthorized repo** → never. cwd is pinned and runs with
  `bypassPermissions`; confirm the path with the user first (Step 1).
