---
name: deploy-cc-receiver
description: (v0.3 — planned) End-to-end procedure for deploying a Claude Code executor receiver into a target repo so Hermes can orchestrate Claude Code over A2A — confirm the repo path, call deploy_cc_receiver, run the roles/repo/comm/purpose handshake, then relay tasks via fleet_send and monitor replies. Use when asked to deploy Claude Code as an executor, set up a repo-scoped A2A executor, or have Hermes drive Claude Code in a specific repo.
metadata:
  hermes:
    tags: [a2a_fleet, a2a, agent-to-agent, claude-code, executor, orchestrator]
---

# a2a_fleet: deploy-cc-receiver

> **Status: v0.3 — planned / in progress.** The `deploy_cc_receiver` tool and the
> standalone receiver ship in a later v0.3 phase. This skill is the orchestration
> procedure Hermes follows once the tool is available; the steps and contracts
> below are the target design (see `.omc/plans/a2a-fleet-v0.3-plan.md`).

How to make **Claude Code** a repo-scoped executor in the fleet. Hermes is the
**orchestrator**; Claude Code is the **executor** running inside one specific
repo with that repo's FULL harness — skills, MCP, plugins, `.claude/` settings,
`CLAUDE.md`, claude-mem. The whole point of routing through Claude Code (not the
plain `llm` handler) is to leverage that harness: exactly what the user would
have manually, now driven by Hermes over A2A.

## Topology

```
              fleet_send (outbound)
Hermes agent ───────────────────────────▶  cc_receiver :9300  (deployed in <repo>/.hermes/)
(:9219, response_handler: agent)                    │  spawns claude -p  cwd=<repo>
      ▲                                             │  (full repo harness, persistent session)
      └──────── reply POST :9219 ◀──────────────────┘
```

- This node (Hermes, `:9219`, `response_handler: agent`) receives Claude's
  replies as real Hermes-agent turns (Route B — see `deploy-fleet` / README).
- The `cc_receiver` (`:9300`) is a NEW standalone Claude-side peer, NOT a Hermes
  `response_handler`. Hermes deploys, owns, and launches it as a managed daemon.

## Key facts

- **One repo at a time** (single receiver on `:9300`); multi-repo is deferred.
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

## fleet.yaml peer schema (repo-aware — v0.3)

A Claude Code peer gains repo binding; `load_fleet()` surfaces these fields:

```yaml
fleet:
  agents:
    claude-code:
      url: http://127.0.0.1:9300
      repo_path: /Users/you/dev/some-repo   # the bound repo (cwd of claude -p)
      managed: true                          # Hermes owns/launches the daemon
      mode: claude_code                      # distinguishes from plain url/token peers
```

### fleet.yaml peer entry (after deploy)

`deploy_cc_receiver` does **NOT** auto-edit `fleet.yaml` — it preserves your
comments and hand-maintained structure. After a successful deploy, ensure the
`claude-code` peer exists in `fleet.yaml` so `fleet_send` authenticates and
boot-reconcile manages it. The deploy result returns the exact values to wire:

- `repo_path` — the canonical repo path (echoed in the result),
- `receiver_token_env` — the **stable** inbound-token env var NAME (e.g.
  `A2A_CC_TOKEN_<SLUG>_<HASH8>`); the same name every redeploy, so it can be
  referenced persistently. The token VALUE is fresh per deploy and is published
  into the gateway's environment + the child's env — never written to disk.

Add (or confirm) this block, using the `receiver_token_env` deploy returned:

```yaml
fleet:
  agents:
    claude-code:
      url: http://127.0.0.1:9300
      repo_path: /Users/you/dev/some-repo
      managed: true
      mode: claude_code
      token_env: A2A_CC_TOKEN_SOME_REPO_1A2B3C4D   # <- receiver_token_env from deploy
```

With `token_env` set, `fleet_send(agent="claude-code", ...)` resolves the bearer
from the gateway environment and presents it on `POST :9300/jsonrpc`. With
`managed: true` + `mode: claude_code` + `repo_path`, boot-reconcile re-provisions
this receiver on the next gateway start if it is down (the token is re-minted —
receiver conversation context survives via the claude `--resume` session files).

## Procedure

End-to-end: **confirm repo → `deploy_cc_receiver` → ensure `fleet.yaml` peer entry
(with `token_env`) → handshake → (per task) `fleet_send` + monitor + summarize to
the user + await direction.**

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
   - canonicalizes `repo_path` (rejects symlink escapes / non-canonical paths),
   - copies the receiver into `<repo>/.hermes/cc_receiver.py`,
   - writes binding config `<repo>/.hermes/a2a_receiver.json` (cwd pinned,
     atomic temp-file + rename),
   - writes the A2A-role text to `<repo>/.hermes/A2A.md` and appends a single
     idempotent `@import .hermes/A2A.md` line to `<repo>/CLAUDE.md` (between
     `<!-- a2a-fleet:start -->` / `:end -->`; creates `CLAUDE.md` if absent,
     never clobbers existing content) — role text stays out of tracked files,
   - stops any existing receiver for this repo before launching a fresh one,
   - launches the receiver **detached** (survives gateway restart) on `:9300`,
     records `<repo>/.hermes/cc_receiver.pid`, health-checks `:9300/health`.

   Returns `{deployed, pid, port, repo_path, status}`. If `status` is not
   healthy, surface the error to the user — do not start relaying tasks.

3. **Ensure the `fleet.yaml` peer entry** (see schema above). Confirm the
   `claude-code` peer block exists with the `token_env` the deploy returned, and
   set a generous turn timeout — **`agent.timeout_s` must be 300+** (a `claude -p`
   turn that uses tools runs 30s–5min; a short timeout will look like a failure
   when the executor is simply still working). A no-reply-yet is NOT an error: the
   async reply POSTs back to `:9219` minutes later.

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
- **Receiver-side bounds.** The receiver caps concurrency (`max_concurrent_turns`)
  and tears itself down after an idle timeout (no messages for N min). So: a
  "busy" response means wait, not retry; and if the receiver has idled out, the
  next request needs a fresh deploy (or boot-reconcile relaunch) — don't hammer a
  torn-down port.
- **Error handling — surface, don't silently retry.** If Claude's reply is an
  `[error] ...` (e.g. `claude` not found, permission error, broken session) or the
  receiver is unreachable, **surface it to the user clearly** and stop. Do not loop
  re-sending. Re-deploy / `cc_receiver_status` to diagnose if needed.
- **Authorized repo only / scope discipline.** cwd is pinned at deploy; never pass
  a repo/cwd from a message. Claude operates ONLY in the bound repo — frame tasks
  accordingly. Deploy only where the user authorized.

## Success criteria

- `deploy_cc_receiver` returns `{status: healthy}`; `:9300/health` is up; the
  managed `CLAUDE.md` block is present; a PID is tracked.
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
- **"busy, retry" on `:9300`** → a turn for that `context_id` is still running;
  wait for it, don't fire a second concurrent turn on the same context.
- **Receiver down after gateway restart** → boot-reconcile relaunches `managed`
  peers; if it's still down, re-run `deploy_cc_receiver` (or
  `cc_receiver_status` / `cc_receiver_stop`).
- **Deploying to an unauthorized repo** → never. cwd is pinned and runs with
  `bypassPermissions`; confirm the path with the user first (Step 1).
