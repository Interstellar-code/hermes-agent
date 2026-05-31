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
  load). The managed `CLAUDE.md` block is the source of truth for the role.
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

## Procedure

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
   - writes/refreshes the idempotent managed A2A-role block in
     `<repo>/CLAUDE.md` (between `<!-- a2a-fleet:start -->` / `:end -->`;
     creates the file if absent, never clobbers existing content),
   - launches the receiver **detached** (survives gateway restart) on `:9300`,
     records `<repo>/.hermes/cc_receiver.pid`, health-checks `:9300/health`.

   Returns `{deployed, pid, port, repo_path, status}`. If `status` is not
   healthy, surface the error to the user — do not start relaying tasks.

3. **Handshake** — send the executor a structured first message on a reserved
   `context_id` (e.g. `handshake:<repo-hash>`) and read the confirmation:
   - **Hermes declares:** role=orchestrator; bound repo=`<repo_path>`; comm
     contract (same `context_id` = same persistent session; replies POSTed to
     `:9219`); purpose/scope.
   - **Claude confirms:** role=executor; the repo it's operating in (echo its
     `cwd`); harness loaded (skills / MCP inventory, optional); ready.
   Report roles + readiness + the harness inventory back to the user. If the
   harness did not load (e.g. `.mcp.json` absent/malformed), say so — the feature
   runs with a reduced harness, never silently.

4. **Relay tasks** — for each user task, call:
   ```
   fleet_send(agent="claude-code", message="<task>", context_id="<thread-id>")
   ```
   Reuse the SAME `context_id` for a continuing conversation/thread (persistent
   Claude session); use a fresh one to start an independent thread.

5. **Monitor + liaise** — await Claude's reply (it arrives back on `:9219` as a
   real Hermes-agent turn). Relay status/results to the user, ask follow-ups,
   and feed the next instruction back via `fleet_send` with the same
   `context_id`. You are the liaison between the user and the executor.

## Autonomous-operation guardrails

`bypassPermissions` + "plan and execute autonomously" is powerful — bound it:

- **Per-turn timeout.** Each turn spawns a fresh `claude -p` (seconds of
  cold-start even with `--resume`). Honor the configured per-turn timeout; do not
  block indefinitely.
- **Do NOT loop.** One `fleet_send` per task instruction. Do not auto-resend or
  spin awaiting a reply — relay, await once, then bring the result to the user.
- **One in-flight turn per `context_id`.** Two overlapping `--resume <same>`
  turns corrupt the session; the receiver serializes per `context_id` and
  returns "busy, retry" for a second concurrent turn. Respect it — don't retry-
  storm.
- **Authorized repo only.** cwd is pinned at deploy; never pass a repo/cwd from a
  message. Deploy only where the user authorized.

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
