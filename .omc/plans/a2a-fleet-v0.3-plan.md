# a2a_fleet v0.3 — Claude Code as a repo-scoped A2A executor peer

Status: DRAFT · Author: Claude · Date: 2026-05-31
Builds on: v0.2 (merged, #53) + Route B (merged, #54). Additive — does not rewrite the existing adapter/agent bridge.

## Vision

Hermes = **orchestrator**. Claude Code = **executor** working inside a specific repo with the user's FULL harness (skills, MCP, plugins, `.claude/` settings, CLAUDE.md, claude-mem). The whole point of routing through Claude Code (not a raw LLM) is to leverage that harness — exactly what the user would have manually, but now driven by Hermes over A2A.

**Flow:**
1. User → Hermes: "help me on repo X where Claude Code is set up + authorized."
2. Hermes: "path is `<X>` — confirm?" → user confirms.
3. Hermes **deploys** the receiver into `<X>/.hermes/`, writes the A2A role into `<X>/CLAUDE.md` (managed block), launches the receiver daemon, **handshakes**.
4. User → Hermes: "tell Claude Code to do <task>, plan + execute autonomously."
5. Hermes relays via A2A, monitors, awaits updates, liaises.

## Topology (explicit — additive to v0.2)

```
              fleet_send (outbound)
Hermes agent ───────────────────────────▶  cc_receiver :9300  (deployed in <repo>/.hermes/)
(:9219, response_handler: agent)                    │  spawns claude -p  cwd=<repo>
      ▲                                             │  (full repo harness, persistent session)
      └──────── reply POST :9219 ◀──────────────────┘
```
- The **Hermes node** (`:9219`, `response_handler: agent`) is unchanged — it receives Claude's replies as real Hermes-agent turns.
- The **cc_receiver** is a NEW standalone Claude-side peer (not a Hermes response_handler). It is the v0.3 deliverable.

## Locked decisions (from user)
- Lifecycle: **Hermes-managed daemon** (gateway launches/tracks per repo).
- Scope: **one repo at a time** (single receiver, `:9300`); multi-repo deferred.
- Repo binding: **fleet.yaml peer entry** `claude-code: {url, repo_path, ...}`.
- Handshake: **roles + repo + comm + purpose**.
- Memory model: **persistent session** (`--session-id/--resume`) + **full harness** (no `--bare`) + **CLAUDE.md** role injection. 12K harness load is desired, not waste.

---

## Components

### 1. Receiver template — `plugins/a2a_fleet/templates/cc_receiver.py`
Standalone A2A server (no Hermes gateway dependency). Introduces the plugin's first `templates/` asset dir (resolved via `Path(__file__).parent / "templates"`, same pattern as `register_skill`'s SKILL.md path). Hardened from the scratch `.omc/research/a2a_receiver.py`:
- Reads `<repo>/.hermes/a2a_receiver.json` on startup: `{repo_path, bind_port, hermes_url, role_file, claude_flags, model}`. **cwd pinned to `repo_path`** — never taken from an inbound message (security).
- A2A surface: `GET /health`, `GET /.well-known/agent-card.json`, `POST /jsonrpc` (SendMessage/message/send). Inbox JSONL + poll loop (mirrors v0.2 receiver).
- Per inbound message → `claude -p` with:
  - `cwd=<repo>` (harness inheritance)
  - `--session-id <uuid5(contextId)>` first turn, `--resume <same>` after → **persistent session per A2A thread**
  - `--setting-sources user,project,local` (**critical** — opt into repo settings/MCP in headless)
  - `--mcp-config <repo>/.mcp.json` if present
  - `--append-system-prompt <A2A role>` (belt-and-suspenders over CLAUDE.md)
  - `--permission-mode bypassPermissions`
  - `--output-format stream-json --verbose`
  - `--model <pinned>` (avoid drift)
  - **NO `--bare`** (would disable CLAUDE.md + hooks — the opposite of the goal)
- Parses `{"type":"result"}` → POSTs reply back to `hermes_url` (:9219) with same contextId. Logs both legs to a transcript.
- Writes a **PID file** `<repo>/.hermes/cc_receiver.pid` for lifecycle.

### 2. Deploy tool — `ctx.register_tool("deploy_cc_receiver", ...)`
Mechanical, deterministic, side-effecting (belongs in a tool, not a skill). Schema: `{repo_path: str (required), bind_port?: int=9300, model?: str}`. Steps:
1. Validate `repo_path` exists + is a dir (+ ideally a git repo). Refuse otherwise.
2. `mkdir <repo>/.hermes/`; copy `templates/cc_receiver.py` → `<repo>/.hermes/cc_receiver.py`.
3. Write `<repo>/.hermes/a2a_receiver.json` (binding config; cwd pinned).
4. Write/refresh the **managed CLAUDE.md block** (see #3).
5. Launch the receiver **detached** (reuse gateway's `setsid` / `start_new_session=True` Popen pattern, `run.py:3769`), cwd=<repo>; record PID.
6. Health-check `:9300/health`; return `{deployed, pid, port, repo_path, status}`.

Companion tools: `cc_receiver_status` (reads PID file + /health), `cc_receiver_stop` (SIGTERM via PID file).

### 3. CLAUDE.md role injection (idempotent managed block)
Write between markers in `<repo>/CLAUDE.md` (create if absent; never clobber existing content):
```
<!-- a2a-fleet:start -->
## A2A Executor Role (managed by Hermes a2a_fleet)
You are a Claude Code **executor** peer in an A2A fleet. Orchestrator: Hermes (node http://127.0.0.1:9219).
You receive tasks over A2A and execute them in THIS repo using your full tools/skills/MCP.
Reply concisely with results/status. Same contextId = same ongoing session/thread.
<!-- a2a-fleet:end -->
```
Optionally factor the long form into `<repo>/.hermes/A2A_ROLE.md` and `@import` it. claude-mem is NOT used for role (2nd-session gate + fuzzy) — only as bonus continuity.

### 4. fleet.yaml schema (repo-aware)
Peer entry gains repo binding:
```yaml
agents:
  claude-code:
    url: http://127.0.0.1:9300
    repo_path: /Users/rohits/dev/some-repo     # NEW — the bound repo
    managed: true                               # NEW — Hermes owns the daemon
    mode: claude_code                           # NEW — distinguishes from plain peers
```
`load_fleet()` surfaces `repo_path`/`managed`/`mode`. Hermes reads `repo_path` to know which repo this link drives.

### 5. Skill — `skills/deploy-cc-receiver/SKILL.md`
Procedure for the Hermes agent (mirror `deploy-fleet/SKILL.md` structure):
- Ask the user for the repo path; **confirm it back** before acting.
- Call `deploy_cc_receiver(repo_path)`.
- Run the **handshake** (below); report roles + readiness.
- Ongoing orchestration pattern: relay a task via `fleet_send(agent="claude-code", message, context_id)`, monitor, await the reply on `:9219`, liaise with the user.

### 6. Handshake protocol (roles + repo + comm + purpose)
After deploy, Hermes sends a structured first message; Claude replies confirming. Content:
- Hermes declares: role=orchestrator, bound repo, comm contract (same `context_id` = same persistent session; replies POSTed to :9219), purpose/scope.
- Claude confirms: role=executor, repo it's operating in (echo cwd), tools/MCP available (optional), ready.
Use a reserved `context_id` like `handshake:<repo-hash>`.

### 7. Lifecycle + boot-reconcile
- Launch detached (`setsid` Popen) → survives gateway restart; PID file in `<repo>/.hermes/`.
- **Boot-reconcile:** on gateway start, for each fleet.yaml peer with `managed: true` + `repo_path`, check PID/health; relaunch if down. (Hook into plugin `register()` or a startup pass.)
- v0.3+ (optional): ship launchd (macOS) / systemd unit templates as plugin assets (mirror workflow-engine daemon) for OS-supervised restart + boot persistence.
- Do NOT use `_start_server_in_thread` for the claude driver — a daemon thread dies with the gateway; the receiver must outlive it.

### 8. Plugin documentation refresh (NEW — so Hermes invokes this correctly)
The plugin's own docs are stale (v0.1 "echo handler" framing) — if Hermes reads them it'll mis-invoke or re-do homework and go the wrong direction. v0.3 must refresh the docs the Hermes agent actually reads:
- **`plugins/a2a_fleet/plugin.yaml`** — description/instructions: reflect v0.2 (llm), Route B (agent), and v0.3 (claude_code executor deploy). This is loaded when the plugin registers.
- **`plugins/a2a_fleet/README.md`** — drop the "v0.1 ships an echo handler" framing; document the three response_handlers (echo/llm/agent), the Route B platform adapter, and the v0.3 deploy-cc-receiver flow.
- **`skills/deploy-fleet/SKILL.md`** + new **`skills/deploy-cc-receiver/SKILL.md`** — the procedural instructions Hermes loads on demand. Must state clearly: ask for + confirm repo path → call `deploy_cc_receiver` → handshake → relay/monitor.
- **`CHANGELOG.md`** — v0.2/Route B/v0.3 entries.
Goal: when Hermes loads the plugin, the README/YAML/skill tell it exactly how to orchestrate Claude Code — no guessing, no homework.

---

## Codex 2nd-pass refinements (folded in)
1. **Receiver↔Hermes reply contract — make explicit.** The receiver POSTs replies to `:9219`; nail the exact message shape (method, contextId echo, role) and error frames — don't leave it implied. Document it in the template + skill.
2. **Atomic writes + marker repair.** Write `CLAUDE.md` block and `a2a_receiver.json` via temp-file + atomic rename; on partial/legacy markers, repair rather than duplicate. A torn write must never leave a repo half-managed.
3. **PID file is weak alone.** Boot-reconcile and `status` must validate **PID *and* `/health`** (PID reuse + stale pidfiles cause false "running"). Add restart backoff.
4. **Path safety.** `deploy_cc_receiver` must canonicalize `repo_path` and reject symlink escapes / non-canonical paths before writing into `<repo>/.hermes/`.
5. **`.mcp.json` degradation.** Define behavior when it's absent / malformed / references tools unavailable in the daemon env — log + continue with reduced harness, never silent-degrade the feature's whole purpose. Surface "harness loaded: skills=…, mcp=…" in the handshake so it's visible.
6. **CLAUDE.md is the source of truth**, `--append-system-prompt` is secondary — keep them in sync to avoid role drift.
7. **Concurrent same-contextId turns RACE the same claude session.** Two `claude -p --resume <same>` overlap = corrupted session state. The receiver MUST serialize per `contextId` (per-context lock + "busy, retry" — mirror Route B's `agent_bridge` guard).
8. **Receiver inbound auth.** `:9300` needs the same bearer discipline as the existing A2A server (`auth_required`/token), not just "Hermes manages it" — it's a port that can execute code in a repo with `bypassPermissions`.
9. **Autonomous stop conditions.** `bypassPermissions` + "plan and execute autonomously" needs guardrails: per-turn timeout, max concurrent turns, restart backoff, and ideally a max-turns/idle cap per session — "user authorized the repo once" is not a sufficient bound.
10. **Deterministic reply parsing.** `stream-json --verbose` emits many frames; the template must select the final `type=result` deterministically and handle error frames, not grab the last line.

---

## Hermes-agent review — adaptations (folded in)
The Hermes agent reviewed the v0.3 brief and raised real gaps:
1. **NOT SSE — terminology fix.** `claude -p` is a **blocking subprocess**; multi-turn continuity comes from the Claude **session file** (`--session-id/--resume`), NOT a streaming/SSE connection. Docs + plan must never call this "SSE". (We had loosely said "SSE/SSC server" — wrong.)
2. **Role injection → `<repo>/.hermes/A2A.md` + `@import`** (DECIDED). Role text lives in `<repo>/.hermes/A2A.md`; the deploy tool appends a single idempotent `@import .hermes/A2A.md` line to `<repo>/CLAUDE.md` (between markers). Role text stays out of the tracked `CLAUDE.md` (no git pollution) while still auto-loading every session. (Chosen over CLAUDE.md-block and CLAUDE.local.md.)
3. **Daemon teardown.** On redeploy to the same repo, **stop the old receiver first** (PID file). Add **idle-timeout cleanup** (no messages for N min → receiver can exit) and explicit `stop_cc_receiver`.
4. **Bigger timeout + anti-loop/liaise policy.** Bump `fleet.yaml agent.timeout_s` to 300+. The skill MUST state: Hermes does NOT auto-reply to every inbound A2A turn (loop risk) — it summarizes Claude's reply to the user and waits for direction before the next `fleet_send`.
5. **Error propagation.** `deploy_cc_receiver` and the receiver must surface failures (deploy error, receiver crash, claude error, :9219 down) back to Hermes clearly — never silent-fail.
6. **Multi-repo (deferred, but design the seam):** Hermes's dynamic-registration idea — `deploy_cc_receiver` registers a per-repo peer in fleet.yaml (`claude-code-<repo-slug>` on its own port) so `fleet_send(agent=<slug>)` routes correctly. v0.3 stays single-repo/single-port; keep contextId→repo mapping in mind so multi-repo is additive later.
7. **Bootstrap is eager-on-request** (user says "help me on repo X" → deploy), not lazy. Documented as the model.

---

## Phases (each its own PR, confined to plugins/a2a_fleet + tests)
0. **Docs refresh + `--setting-sources`/auth validation (do FIRST).** Update plugin.yaml/README/CHANGELOG (Component 8) so the plugin self-describes correctly. In parallel, validate on the installed Claude Code: does `claude -p cwd=<repo>` load the repo harness with `--setting-sources user,project,local` + `--mcp-config`, and does a gateway-spawned `claude -p` reach the keychain? These two validations gate the whole feature.
1. **Receiver template** — parameterized standalone `cc_receiver.py` (persistent session + **per-contextId serialization**, repo cwd, harness flags, config file, PID, **bearer auth**, deterministic result parsing, explicit reply contract). Unit-test the command-builder + config parsing + lock; manual live test against a sample repo.
2. **Deploy tool + companions** — `deploy_cc_receiver` / `status` / `stop`, **atomic** CLAUDE.md managed-block writer (idempotent + marker repair), `a2a_receiver.json` writer, **path canonicalization/symlink rejection**, detached launch. Tests for block idempotency + config write + path safety.
3. **fleet.yaml schema** — `repo_path`/`managed`/`mode` in `load_fleet()` (**additive — preserve url/token peers + Route B**); boot-reconcile validating **PID + /health** with backoff; reconcile startup ownership vs the existing unconditional server-thread autostart. Tests.
4. **Skill + handshake** — `deploy-cc-receiver/SKILL.md` (+ refresh `deploy-fleet/SKILL.md`), handshake exchanging roles+repo+comm+purpose + **harness-loaded inventory**, autonomous-operation guardrails (timeout/concurrency/backoff).
5. **Lifecycle hardening** (optional) — launchd/systemd templates.

## Acceptance (v0.3 = phases 1–4)
- `deploy_cc_receiver("<repo>")` → receiver live on :9300, cwd pinned to repo, CLAUDE.md managed block present, PID tracked.
- Hermes `fleet_send` to claude-code → `claude -p` runs **in the repo with its harness** (skills/MCP/CLAUDE.md loaded — verify via a repo-specific skill/file question), replies to :9219.
- Multi-turn: same `context_id` → same persistent claude session (context accumulates).
- Handshake exchanges roles+repo+comm+purpose.
- Gateway restart → boot-reconcile relaunches the managed receiver.
- echo/llm/agent modes + existing tests unaffected.

## Risks / validate-during-build
1. **`--setting-sources` default** (THE risk) — if the installed Claude Code excludes project settings/MCP in headless, the repo harness silently won't load. Always pass `--setting-sources user,project,local` + explicit `--mcp-config`; validate on the target version in Phase 1.
2. **Auth** — gateway-spawned `claude -p` runs as the user → should have keychain access (unlike the Claude-Code tool sandbox). Verify in Phase 1 on the real daemon; if it fails, fall back to user-launched daemon for that repo.
3. **Cold-start latency** — each turn spawns `claude -p` (seconds). `--resume` keeps context but each turn is still a fresh process. Acceptable for orchestration cadence; note it.
4. **Security** — cwd pinned at deploy time, never from an inbound message; deploy validates repo_path; `bypassPermissions` means Claude can do anything in that repo → only deploy to repos the user authorizes.
5. **claude-mem first-session gate** — resolved by CLAUDE.md + `--append-system-prompt`.

## Open questions (lower priority — can decide during build)
1. Should `deploy_cc_receiver` auto-commit the `.hermes/` + CLAUDE.md changes, or leave them unstaged for the user?
2. Handshake: include a capability inventory (tools/MCP available) or keep minimal?
3. Reply formatting: strip Claude's reasoning/preamble for Hermes (as v0.2 did for the agent), or pass full?
