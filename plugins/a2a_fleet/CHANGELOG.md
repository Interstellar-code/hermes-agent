# a2a_fleet — Changelog

## v0.8.15 — oc receiver ergonomics: workdir prompt injection, wait-for-reply client helper, duplicate-dispatch guard

Addresses three issues surfaced by a Phase-1 oc-receiver field test (reviewer was
on ~0.8.9; two earlier-reported P0s — opencode `cwd` and `receiver_token`
persistence — were already fixed in 0.8.10–0.8.12 and are not repeated here).

- **Workdir injected into the role prompt (P1-7):** the static `A2A_ROLE_TEXT`
  said "cwd is pinned" but never stated the actual path, so OpenCode guessed its
  working directory and relative-path tool calls could silently hit the wrong
  dir. `oc_deploy.py` now builds the role text via `role_text_for(repo_path)`,
  appending the concrete `repo_path` plus a "use absolute paths" directive at
  both the receiver-config and `.hermes/A2A.md` write sites.

- **`send_message_and_wait()` client helper (P0-4):** async receivers (opencode)
  return a `[queued]` ack and deliver the real reply later, forcing callers to
  sleep-and-tail by hand. The new `client.send_message_and_wait(...)` returns
  immediately for synchronous receivers, and for async ones polls the receiver's
  transcript jsonl (located via the peer's `repo_path` or an explicit
  `transcript_path=`) by `context_id` for the final reply, with a `max_wait`
  deadline. Same-machine + oc-specific; cross-machine peers still use the
  push-back-to-Hermes channel.

- **Duplicate-dispatch guard with distinct error code (P1-5):** a second dispatch
  on a context already running a turn used to be silently accepted as a second
  indistinguishable `[queued]` ack. The oc receiver now tracks in-flight
  contexts (`claim_inflight`/`release_inflight`, released in a `finally` around
  the turn) and rejects a duplicate with JSON-RPC **`-32001`**
  ("duplicate dispatch… retry"). `server.py`'s Route B `A2ABusyError` now uses
  the same `-32001` code (was the generic `-32000`) so clients can branch on it.
  - **Behavior change:** a concurrent same-context dispatch is now rejected
    (client retries) instead of queued-and-serialized. Sequential follow-ups
    after a turn completes are unaffected.

## v0.8.14 — A2A listener starts on adapter.connect() (bind-race + bridge-colocation fix) + Hermes↔Hermes peering docs (#120)

- **Bind-race / bridge-colocation fix (blocker, proven broken at runtime):**
  `register()` called `_start_server_in_thread()` unconditionally, but
  `register(ctx)` runs in EVERY process that loads the plugin (gateway, CLI tool
  startup, dashboard web tier). They raced to bind `fleet.server.bind_port`; the
  **bridge-less dashboard process won**, so a direct `agent` SendMessage to the
  A2A port returned `-32000 "agent bridge not ready"` — Route B (and therefore
  Hermes↔Hermes) was non-functional. (A `hasattr(ctx, "register_platform")` gate
  was attempted first but is a no-op — that method exists on every
  `PluginContext`; caught by a Codex review.) **Fix:** the listener now starts in
  `A2AFleetAdapter.connect()` — the one place that runs ONLY in the gateway/agent
  process, on the gateway loop, exactly where the Route B bridge is wired — so
  listener + bridge are co-located by construction. `register()` no longer starts
  the server; `disconnect()` stops it.
  - **Behavior change:** an A2A node's listener now comes up when the
    `a2a_fleet` platform connects (gateway with `platforms.a2a_fleet` enabled),
    not at plugin import. For `response_handler: agent` that platform was already
    required; `echo`/`llm` nodes must now enable the platform too. (Supersedes the
    issue-#33 register()-starts-server behavior.)
  - Tests: `register()` does NOT start the server; `connect()` starts it + wires
    the bridge; integration — register leaves the port closed, connect yields a
    reachable `/health`. All falsification-verified.
- **Docs:** corrected the stale `references/hermes-gateway-plugin-guide.md` — it
  described a never-implemented `/api/plugins/a2a_fleet/jsonrpc|sse|tasks` mount;
  the real transport is the standalone uvicorn (`server.py`, `/jsonrpc` +
  `/.well-known/agent-card.json` + `/health`) with an in-process Route B bridge.
  Added a "Hermes↔Hermes peering" section to the `deploy-fleet` skill (per-profile
  `bind_port` map, plain-agent-peer shape with BASE url, profile-scoped token
  envs, the gateway-run prereq, handshake convention).
- Groundwork for Hermes↔Hermes profile-to-profile A2A (#120). No new server — it
  reuses the existing `agent` protocol pointed at another profile.
- Full suite 406 passed.

## v0.8.13 — agy prefix-drift made observable (#108) + #109 near-term

Addresses the agy intermittent `[no reply produced by agy]` / stale-context
replies after a mid-session receiver restart (#108), via the #109 near-term plan.

- **`prefix_drifted` flag (the key fix).** When a resume turn's stdout is NOT a
  prefix of the persisted `last_stdout` (baseline lost after a restart),
  `run_agy_turn` now records `prefix_drifted: true` + `drifted_at` in
  `a2a-agy-sessions.json` and logs a warning. A clean turn records
  `prefix_drifted: false`. This converts a previously-silent extractor fallback
  into a machine-observable event the dashboard / Hermes can surface.
  `load_session_map` preserves the flag across reads.
- **Honest reply on drift.** A drifted turn that yields nothing extractable now
  returns `[drift detected — persisted last_stdout does not match agy's
  cumulative output...]` instead of the opaque `[no reply produced by agy]`.
  (In practice the extractor already returns the full cumulative output on a
  prefix mismatch — visible, not empty — so this is the belt-and-suspenders edge.)
- **Atomic persistence** (#109 item 1) was already in place — `_write_session_map`
  uses tmp + `os.replace` under the per-context lock; no change needed.
- Long-term (#71 handshake v2) remains the durable fix and is out of scope here.
- Tests: drift detection, flag persist/clear + read-back, run_agy_turn flags a
  restart-resume drift. Falsification-verified. Full suite 405 passed.

## v0.8.12 — managed-token resolution prefers the authoritative .token (P0-3)

- **Token precedence inverted for managed peers.** `_resolve_managed_token` now
  prefers the persisted `<repo>/.hermes/<token_filename>` over `os.environ`,
  falling back to the env var only when the file is absent/unreadable. The
  `.token` is authoritative — it is exactly what the currently-running receiver
  requires and every deploy writes it last; `os.environ[token_env]` is only an
  in-process cache that goes STALE across an out-of-process redeploy. The old
  env-first order meant a stale env value shadowed a fresh `.token` and sent the
  wrong bearer → HTTP 401 (operator-reported P0-3). This still satisfies #104
  (file present → used; only when the receiver was never deployed on this host
  does it fall back to the env var). Falsification-verified.
- **Docs:** `deploy-fleet` skill documents the `connection refused` mid-session
  cause (`idle_timeout_s` self-teardown, default 1800s) and the `idle_timeout_s: 0`
  knob / idempotent re-deploy mitigation (operator-reported P0-1).
- Full suite 402 passed.

## v0.8.11 — agy empty-output is actionable (#105) + spawners pin HERMES_HOME (#98)

- **#105 — agy `--print` rc=0 + empty stdout is now actionable.** agy v1.0.4 in
  `--print` mode exits `rc=0` with EMPTY stdout/stderr when not signed in — a
  SILENT failure (no hang, no marker string). The receiver's auth heuristic
  required `empty stdout AND an auth-marker`, so this case slipped through to the
  opaque `[no reply produced by agy]`. `run_agy_turn` now treats ANY empty turn
  as the actionable sign-in failure and returns the existing
  `agy not authenticated — run \`agy\` interactively once...` hint. (The root
  cause is host onboarding — agy must be signed in once via Keychain; the code
  just surfaces it clearly instead of an opaque fallback.)
- **#98 — receiver spawners pin `HERMES_HOME` in the child env.** All four
  `deploy_*_receiver` handlers now always build the child env and set
  `HERMES_HOME = str(get_hermes_home())` (previously the env was only built when
  a token was provisioned, and `HERMES_HOME` was never set explicitly). A
  detached receiver therefore resolves the SAME profile the deployer did, never
  the silent `~/.hermes` default-profile fallback that would write state to the
  wrong profile.
- Tests: agy empty-output → auth hint (not opaque fallback); cc deploy child env
  always carries `HERMES_HOME` (incl. the `no_auth` path). Both
  falsification-verified. Full suite 401 passed.

## v0.8.10 — managed-peer token resolves from persisted .token (token-drift fix, #104)

- **Bug fix (#104)**: `fleet_send` could send no bearer → HTTP 401 against a
  freshly (re)deployed managed receiver. `fleet_config.load_fleet` resolved a
  peer's token **only** from `os.environ[token_env]`, so any process that did
  not itself run `deploy_*_receiver` — a fresh/worker process, or the gateway
  before boot-reconcile — resolved `None`. The persisted
  `<repo>/.hermes/<token_file>` (`.token`/`.oc-token`/`.codex-token`/`.agy-token`)
  the receiver was launched with was ignored. Managed peers now fall back to that
  file when the env var is unset (`_resolve_managed_token`); the env var stays an
  in-process cache/override and still wins when set. Plain (non-managed) peers
  are unchanged (env-only). The `.token` file is the source of truth — the same
  one boot-reconcile already republishes — so a redeploy is usable immediately,
  no gateway restart and no manual token sync.
- Adds `managed_peers.token_filename_for(mode)` (single source for the per-mode
  token filename) and `_resolve_managed_token` tests (file fallback, env
  precedence, unknown-mode/missing-repo → None). Falsification-verified. Full
  suite 400 passed.

## v0.8.9 — port allocation claims peers cross-mode (collision fix)

- **Bug fix**: `resolve_managed_bind_port` / `_ports_claimed_by_other_repos`
  only treated **same-mode** peers as claiming a port, so a managed peer of a
  *different* mode sitting inside the target band got handed out again. Observed
  live: a `claude_code` peer bound on `9310` (inside the opencode band) let a
  fresh `opencode` deploy allocate `9310` too → two fleet.yaml peers on the same
  port, and `fleet_send` to either could cross-wire. The claim scan is now
  **cross-mode** — every managed peer's port (any mode) is claimed; only this
  exact `(repo, mode)` slot is excluded (its own reuse is handled by
  `_configured_bind_port` upstream). The live socket probe remains the backstop.
- Tests: cross-mode claim + same-(repo,mode) exclusion in
  `test_port_bands.py`. Falsification-verified. Full suite 397 passed.

## v0.8.8 — agy runner env fix + timeout normalization (Codex review of v0.8.7)

Follow-up to v0.8.7 from a skeptical Codex review of the merge:

- **HIGH — agy `_subprocess_runner` ignored the augmented env.** v0.8.7 added
  `_tool_env()` (PATH + `AGY_CLI_DISABLE_LATEX`) but the real turn runner still
  built `env = dict(os.environ)` directly, so live agy turns launched with the
  raw (launchd-minimal) PATH while only the CLI probe saw the augmentation — the
  exact regression v0.8.7 claimed to fix. The v0.8.7 live proof masked it (the
  ad-hoc test ran with an ambient PATH that already had `gh`). The runner now
  uses `env=_tool_env()` and `stdin=subprocess.DEVNULL`. (Caught by Codex; the
  unit suite missed it because agy lacked the runner-env test codex/opencode had.)
- **MEDIUM — `--print-timeout` could truncate to `0s`.** The flag value
  (`int(float(agy_timeout_s))`) and the receiver backstop (`float + 60`) were
  computed independently, so a fractional/tiny budget broke the invariant
  (`0.5 → --print-timeout 0s` but backstop `60.5s`). Both now derive from one
  `_print_timeout_s()` helper (ceil, min 1s); backstop = that + 60s grace.
- **LOW — added the missing agy runner-env test** (mirrors codex/opencode):
  patches `Popen`, asserts the augmented PATH + `AGY_CLI_DISABLE_LATEX` + closed
  stdin reach the child. Plus a `--print-timeout` floor test. Falsification-verified.

Codex confirmed sound: append-vs-prepend PATH (no hijack), codex `stdin=DEVNULL`
on both first+resume turns, `opencode_agent=None` keeps tool access, and the
forbidden-flag stripping. Full suite 395 passed.

## v0.8.7 — executor tool-parity: opencode/codex/agy do real repo work (#97, #99, #100)

Brings the three non-Claude executors up to the `claude_code` bar — real
tool/file/`gh` access driven non-interactively, not chat-only.

- **Shared PATH augmentation** (all three receiver runners): a receiver launched
  by launchd inherits a minimal PATH, so the agent's bash/tool calls couldn't
  find `gh`/`git`/node. Each runner now appends the common tool dirs
  (`/opt/homebrew/bin`, `/usr/local/bin`, `~/.local/bin`, …) to PATH without
  shadowing an explicit parent PATH.
- **codex (#97)**: prompt is passed as a positional arg AND the subprocess runner
  closes stdin (`stdin=subprocess.DEVNULL`). codex-cli ≥0.136 inspects whether
  stdin is a pipe; a daemon's inherited pipe made it block "Reading additional
  input from stdin..." and exit rc=1 with no parseable output. Unit + falsification
  verified; **live re-verify pending a codex-cli auth refresh.**
- **opencode (#99)**: removed the forced `--agent build` (it is a *subagent* in
  some installs, e.g. 1.15.13, and triggers a fallback-to-default warning). The
  default primary agent already has the full tool set; the real fix was PATH.
  `opencode_agent` defaults to `None` (use opencode's default) and is honored only
  when an operator pins a specific tool-enabled primary agent. **Verified live**:
  ran `gh issue list … | grep -c .` and returned the count.
- **agy (#100)**: `build_agy_command` now adds `--add-dir <repo>` (workspace
  access — without it agy treats the task as out-of-workspace and returns only a
  plan) and `--print-timeout <budget>` (agy's 5m default produced plan-only/no-
  result turns on real tasks). Default `agy_timeout_s` raised 300→900; the
  receiver's subprocess backstop is now `agy_timeout_s + 60s` grace so agy reaches
  its own timeout and self-exits cleanly instead of being killpg'd mid-write.
  **Verified live**: ran `gh issue list` and returned the count.
- New `tests/plugins/a2a_fleet/test_executor_capabilities.py`: command-build +
  stdin/PATH guards for all three modes, the agy backstop invariant, and a
  live-smoke (gated behind `A2A_LIVE_SMOKE=1`). Full suite 393 passed.

## v0.8.6 — dashboard: dedup managed peers by (repo, mode), not repo alone (#95)

- **Bug fix (#95)**: the read-only dashboard API `_managed_repos()` deduped
  managed peers by `repo_path` alone, so a repo running more than one mode
  (e.g. a `claude_code` + `codex` peer in the same repo) had every mode after
  the first silently dropped from the conversations/peers feed — and therefore
  from the Matrix3D A2A page. Dedup is now keyed by `(repo_path, mode)`; each
  mode in a repo surfaces as its own peer. Docstring corrected (it claimed
  "covers all 4 modes" while the key contradicted it). No front-end change
  needed (switchui #183 was already correct); restart the dashboard to pick up
  the fix. Note: `opencode`/`agy` still need their own `fleet.yaml` entries to
  appear — they were genuinely unconfigured, not dedup casualties.

## v0.8.5 — per-mode port bands + auto-allocation (multi-session safe)

- **Port bands**: each managed mode now owns a contiguous 10-port band so
  multiple same-mode receivers (one per repo) can run without colliding with a
  neighbouring mode's port — `claude_code` 9300-9309, `opencode` 9310-9319,
  `codex` 9320-9329, `agy` 9330-9339. The band start is the mode's
  `DEFAULT_BIND_PORT`; **codex default moves 9311→9320 and agy 9313→9330** (cc/oc
  unchanged). Single source of truth: `managed_peers._MODE_PORT_BANDS`, with a
  parity test asserting each deploy module's `DEFAULT_BIND_PORT` equals its band
  start.
- **Auto-allocation**: deploy handlers now take `bind_port` as optional. When
  omitted, `resolve_managed_bind_port()` (a) reuses this repo's existing
  configured port if present (idempotent re-deploy), else (b) picks the first
  free port in the mode's band, skipping ports already claimed by other repos'
  managed peers (read from fleet.yaml, best-effort) and any port currently bound
  (live socket probe). Band exhausted → a clear error instead of a silent
  collision. An explicit `bind_port` is still honored verbatim (may sit outside
  the band for power users).
- Tool schemas + README updated to document the bands and the omit-to-auto-pick
  behaviour. Boot-reconcile is unaffected (it always passes an explicit port).

## v0.8.3 — security: system_prompt_file path-traversal guard + deploy schema regression guard (#84, #72)

- **Security fix (#84)**: `load_fleet()` now validates `llm.system_prompt_file` against
  `get_hermes_home()` before storing the path.  Relative paths are resolved relative to
  the Hermes home; `~` is expanded; symlinks and `..` components are resolved via
  `Path.resolve()`.  If the resolved path is not within `get_hermes_home().resolve()`,
  `FleetConfigError` is raised at config-load time (fast-fail, before any file is read).
  The validated, fully-resolved absolute path is stored back into the config dict so
  `llm_handler` reads exactly the guarded value.
- **Regression guard (#72)**: test asserts that `deploy_cc_receiver`, `deploy_oc_receiver`,
  `deploy_codex_receiver`, and `deploy_agy_receiver` each have `repo_path` in both
  `schema.properties` and `schema.required`.  The underlying bug (missing `repo_path` in
  the old stub) was already fixed; this test prevents it from silently regressing.

## v0.8.1 — dashboard mode-aware (issue #80)

- **Dashboard now covers all four managed executor modes** (claude_code, opencode,
  codex, agy) — previously `_managed_repos()` filtered on `mode == "claude_code"`
  only, so OpenCode, Codex, and agy peers were invisible in the conversations/peers
  tab.
- `managed_peers._MODE_SPECS` gains a `transcript_filename` field per mode
  (e.g. `"a2a-codex-transcript.jsonl"`) so the dashboard reads the correct file
  for each executor type.  New public accessor `transcript_filename_for(mode)`
  with graceful fallback to the claude_code filename for unknown/legacy modes.
- `dashboard/plugin_api.py`:
  - `_managed_repos()` returns `(name, repo, mode)` 3-tuples; uses
    `supports_managed_mode()` to accept every known mode.
  - `_transcript_path(repo, mode)` and `_read_transcript(repo, mode)` are
    now mode-aware; `TRANSCRIPT_RELPATH` kept as a back-compat constant.
  - `mode` field surfaced in all response payloads: `list_conversations`,
    `get_conversation`, `list_peers` — additive, backward-compatible.
- Tests: 8 new cases in `test_dashboard_api.py` covering all-four-modes
  discovery, per-mode transcript path resolution, mixed-fleet end-to-end,
  legacy-no-mode graceful exclusion, and `mode` in API responses.
  Full suite: 348 passed.

## v0.8.0 — in progress (issue #75)

- Added Google Antigravity CLI (`agy`) as a fourth managed repo-scoped executor
  peer (`mode: agy`, default port 9313):
  - new standalone template `templates/agy_receiver.py` (STDLIB-only, no
    a2a_fleet import dependency; injects `AGY_CLI_DISABLE_LATEX=1` into the agy
    subprocess env)
  - new deploy/manage module `agy_deploy.py` with
    `deploy_agy_receiver_handler`, `agy_receiver_status_handler`,
    `agy_receiver_stop_handler`; full dict-unwrap for all params
  - command builder: first turn `agy --print "<prompt>" --dangerously-skip-permissions`,
    resume turn `agy --conversation <uuid> --print ...`. `sandbox` is a BOOLEAN
    toggle (`--sandbox`); there is **NO model selection** (agy has no `--model`).
  - **conversation-id discovery**: agy does not let the caller assign the id on
    turn 1. After a first turn the receiver reads
    `~/.gemini/antigravity-cli/cache/last_conversations.json` (keyed by the
    pinned repo cwd) to capture the uuid agy minted, persisting
    `contextId -> {conversation_id, last_stdout}` in `a2a-agy-sessions.json`.
  - **plain-text transcript-tail extraction**: agy re-echoes the ENTIRE prior
    transcript (newline-separated assistant replies, no role markers) on every
    resume, then appends the new reply. The receiver persists the full prior
    stdout per contextId and strips it as a literal prefix to recover only the
    latest reply; if the prefix drifts it returns the FULL stdout (never just
    the tail line) so multi-line replies are never silently truncated.
  - never uses `--continue` (cwd-global, unsafe for concurrent contexts sharing a
    cwd); always pins an explicit `--conversation <uuid>` under the per-context
    lock.

### Bug fixes / hardening (baked in from start)

- **remint clears stale conversation_id**: when a stored uuid is dead, agy emits
  `Warning: conversation "<id>" not found.` as the first stdout line and then
  runs fresh in the SAME invocation. The receiver clears the stale entry from
  `a2a-agy-sessions.json` before persisting, strips the warning line from the
  reply, and captures the NEW uuid agy minted. If discovery yields no new uuid,
  the dead id is NOT re-persisted. (Regression test asserts this.)
- **auth-failure surfacing**: agy auth is macOS Keychain (no headless login). A
  turn that hangs to timeout or emits an auth signal surfaces a clear
  "agy not authenticated — run `agy` interactively once to sign in" error rather
  than hanging silently.
- **dict-dispatch unwrap**: the deploy/status/stop handlers detect a dict first
  positional (registry dispatch shape) and extract all params (`repo_path`,
  `bind_port`, `sandbox`, `no_auth`, `hermes_auth_token_env` — note: no model).
  (Regression test asserts a non-default `bind_port=9314` is honored.)
- **PR #88 review fixes (D1, D2)**: (1) cross-context conversation bleed — a
  process-global `_FIRST_TURN_LOCK` now serializes the first-turn mint section
  [spawn -> read `last_conversations.json` -> persist `contextId->uuid`] so two
  different contextIds doing their first turn in the shared repo cwd can no
  longer cross-capture each other's minted uuid; resume turns skip the lock for
  full concurrency. (2) `extract_reply` no longer truncates a multi-line reply
  to its last line when the stored prefix drifts — it returns the full stdout.
- **agy_extra_flags sanitized**: forbidden session/print selectors
  (`--continue`/`-c`, `--conversation`, `--print`/`-p`/`--prompt`,
  `--prompt-interactive`/`-i`) are stripped so a stale config cannot break the
  explicit `--conversation`/`--print` the receiver always sets.

### Runtime files (coexist with cc/oc/codex in one `.hermes/`)

- `agy_receiver.json`, `a2a-agy-inbox.jsonl`, `a2a-agy-inbox.offset`,
  `a2a-agy-transcript.jsonl`, `agy_receiver.pid`, `a2a-agy-sessions.json`,
  `.agy-token`.

## v0.7.0 — in progress (PR #79)

- Added OpenAI Codex CLI as a third managed repo-scoped executor peer
  (`mode: codex`, default port 9311):
  - new standalone template `templates/codex_receiver.py` (STDLIB-only, no
    a2a_fleet import dependency)
  - new deploy/manage module `codex_deploy.py` with
    `deploy_codex_receiver_handler`, `codex_receiver_status_handler`,
    `codex_receiver_stop_handler`; full dict-unwrap for all params
  - `codex exec` / `codex exec resume` command builder; JSONL parser for
    `thread.started` → `thread_id` and last `item.completed agent_message`
    → reply text
  - `_is_session_not_found` checks both reply text and stderr for
    `"no rollout found for thread id"`

### Bug fixes

- **remint clears stale thread_id**: when a stored `thread_id` is dead and
  the remint retry emits no `thread.started`, the stale id is now deleted
  from `a2a-codex-sessions.json` before the fresh `_invoke(None)` call.
  Previously the bad id stayed on disk and every subsequent turn attempted
  `codex exec resume <dead-id>` first.

- **codex_extra_flags sanitized on resume**: forbidden flags (`--color`,
  `-s`/`--sandbox`, `--ephemeral`) are now stripped from `codex_extra_flags`
  before appending to the command. `--ephemeral` is stripped on any command
  (breaks resume); `--color`/`-s`/`--sandbox` are stripped on resume only
  (rejected by `codex exec resume` in codex-cli 0.135.0). Both
  `--flag value` and `--flag=value` forms are handled; a warning is logged
  for each dropped token.

## v0.6.1 — shipped

### Bug fixes

- **H2 (dispatch)**: `deploy_oc_receiver_handler`, `deploy_cc_receiver_handler` now
  unwrap ALL recognised params (`bind_port`, `model`, `no_auth`,
  `hermes_auth_token_env`) when the registry passes the whole args dict as the
  first positional argument. Previously only `repo_path` was extracted (via
  `canonicalize_repo_path`), so `bind_port` and `model` silently defaulted
  regardless of what the caller sent.

- **H1 (remint)**: `_is_session_not_found` in `oc_receiver.py` now accepts
  `(reply, stderr)` and checks both, mirroring `cc_receiver`'s implementation.
  The dead `session_missing` third return value from `parse_opencode_output` has
  been removed. Previously the dead-session remint guard only inspected stderr, so
  a session-not-found signal that appeared only in the parsed reply text was
  silently ignored and the stale session was re-used.

### Cleanup

- **H3**: Removed dead `_managed_cc_peers()` from `cc_deploy.py` (superseded by
  `managed_peers.iter_supported_managed_peers`). Its unit test is retargeted to
  `iter_supported_managed_peers` and now covers both `claude_code` and `opencode`
  managed modes.

### Tests added

- `test_deploy_handler_dict_dispatch_extracts_all_params` (oc) and
  `test_deploy_cc_handler_dict_dispatch_extracts_all_params` (cc): confirm
  registry-style dict dispatch uses the caller-supplied `bind_port`, not the
  default.
- `test_run_opencode_turn_remints_when_dead_session_in_reply`: confirms remint
  fires when the dead-session signal is in parsed reply text (not stderr).
- `test_reconcile_down_opencode_peer_triggers_oc_redeploy`: boot-reconcile routes
  a down `mode: opencode` peer through `oc_deploy`.
- `test_reconcile_legacy_no_mode_peer_is_ignored`: legacy no-`mode` peer is
  ignored gracefully.
- `test_oc_receiver_bearer_auth_*`: per-request bearer auth on the OC receiver
  HTTP layer — wrong/missing token → 401, correct token → accepted.

## v0.6.0 — shipped

- Added OpenCode as a second managed repo-scoped executor peer alongside Claude
  Code:
  - new tools `deploy_oc_receiver`, `oc_receiver_status`, `oc_receiver_stop`
  - new standalone template `templates/oc_receiver.py`
  - new deploy/manage module `oc_deploy.py`
- Managed-peer plumbing is now mode-aware for both `claude_code` and
  `opencode`:
  - `fleet_yaml_io.upsert_managed_peer(...)` + `upsert_oc_peer(...)`
  - `fleet_config.load_fleet()` validates and surfaces managed OpenCode peers
  - boot-reconcile now scans and redeploys both managed modes
- OpenCode receiver runtime is isolated from Claude receiver runtime:
  separate config, inbox, offset, transcript, pid, token, and session-map files
  so both peers can coexist in one repo.
- OpenCode receiver persists a durable `contextId -> sessionID` map and reuses
  it on later turns; `Session not found` remints exactly once under the existing
  per-context lock.

## v0.3.0 — planned / in progress

> **Status: planned.** Direction only — NOT shipped. Tracks the v0.3 milestone:
> Claude Code as a repo-scoped A2A executor peer. Full design lives in
> `.omc/plans/a2a-fleet-v0.3-plan.md`.

### Planned
- `deploy_cc_receiver(repo_path)` tool — deploys a standalone Claude Code
  executor receiver into a target repo's `.hermes/`, writes the A2A-role text to
  `<repo>/.hermes/A2A.md` and appends a single idempotent `@import .hermes/A2A.md`
  line to `<repo>/CLAUDE.md` (between `<!-- a2a-fleet:start -->` / `:end -->`
  markers — keeps role text out of tracked files, no git pollution), and launches
  a detached, Hermes-managed daemon on `:9300` (with idle-timeout self-teardown). Each inbound task spawns `claude -p` with
  `cwd=<repo>` and the repo's full harness loaded
  (`--setting-sources user,project,local`, `--mcp-config`, no `--bare`), keyed
  to a persistent session per `context_id` (`--session-id` / `--resume`).
  Vision: Hermes = orchestrator, Claude Code = executor with the repo's real
  skills/tools/MCP/CLAUDE.md/claude-mem.
- Companion tools `cc_receiver_status` / `cc_receiver_stop` (PID + `/health`).
- Repo-aware `fleet.yaml` peer schema — `repo_path`, `managed`, `mode`
  (`claude_code`) surfaced by `load_fleet()` so Hermes knows which repo a link
  drives and whether it owns the daemon (boot-reconcile).
- Handshake protocol exchanging roles (orchestrator / executor), bound repo,
  comm contract (same `context_id` = same persistent session; replies POSTed to
  `:9219`), and purpose.
- New `skills/deploy-cc-receiver/SKILL.md` — the orchestration procedure Hermes
  loads on demand.
- Guardrails: cwd pinned at deploy time (never from a message), per-`context_id`
  serialization, bearer auth on `:9300`, bounded autonomous operation (per-turn
  timeout, restart backoff, idle cap).

### Phase 0 (this entry's scope — shipped)
- Documentation refresh so the plugin self-describes correctly: README, plugin
  manifest description, this changelog, and the deploy skills now reflect the
  three inbound handlers (echo/llm/agent) and the v0.3 direction. Drops the
  stale v0.1 "echo handler only / TaskManager+SSE deferred" framing.

## Route B — 2026-05-31 (#54)

`agent` response handler — inbound A2A dispatched into the **real Hermes
agent** (its conversation loop, SOUL, tools, memory), not a raw model call.

### Added
- `SUPPORTED_HANDLERS` extended to `{"echo", "llm", "agent"}`; `agent` raises
  `FleetConfigError` on any unsupported value, like the others.
- `adapter.py` — `A2AFleetAdapter` (a gateway `BasePlatformAdapter`). Registered
  via `ctx.register_platform("a2a_fleet", ...)` in `register()` and self-registered
  in `platform_registry` so `Platform("a2a_fleet")` resolves without living under
  `plugins/platforms/`.
- `bridge_sync(text, context_id, peer_id, timeout)` — called from the uvicorn
  worker thread; submits a `MessageEvent` to the gateway event loop via
  `asyncio.run_coroutine_threadsafe(self._message_handler(event), gateway_loop)`
  and blocks for the real agent turn. `MessageEvent.internal=True` bypasses the
  gateway user-auth (the A2A bearer is the gate). The A2A `contextId` maps to the
  Hermes session `chat_id`, so the same `context_id` continues the same session.
- `agent_bridge.py` — global bridge registry + `A2ABusyError` /
  `A2ABridgeNotReady`. Per-`context_id` threading lock serializes same-context
  turns; an overlapping second turn gets `A2ABusyError` ("peer busy on this
  context, retry") instead of racing.
- Reasoning-preamble strip: the gateway's leading `💭 **Reasoning:**` block is
  removed before the answer goes over the wire (A2A peers want the final answer).
- Optional `fleet.agent.timeout_s` block (default 120) bounds the synchronous
  wait for the agent reply.

### Requires
- `platforms.a2a_fleet.enabled=true` in the active profile config so the gateway
  calls `adapter.connect()` and wires the bridge. If the bridge is not ready,
  `/jsonrpc` returns a JSON-RPC error telling you to enable it.

## v0.2.0 — 2026-05-31 (#53)

Real conversational inbound replies (Route A) + multi-turn context. Moves the
plugin past echo-only without yet reaching the agent (that's Route B).

### Added
- `llm` response handler (`llm_handler.py`, Route A) — a **stateless** call to
  the active profile's configured provider (`resolve_provider_client("auto")`).
  Delivers real reasoning / Q&A / persona replies. **Bypasses the Hermes agent**:
  no live tools, memory, MCP, or SOUL — treat as a plain-chat fallback.
- `context_store.py` — per-`context_id` multi-turn history + per-context async
  locks; `llm_handler` holds the lock across read→build→call→append for causal
  ordering of overlapping same-context turns.
- `HandlerResult` dataclass (`response_handler.py`) — the internal result type
  returned by every inbound handler (`text`, `context_id`, `kind`); `kind`
  reserved for the future async/task phase.
- `message/send` accepted as an alias of `SendMessage` on `/jsonrpc`.
- Outbound `context_id` threading on `fleet_send` — omit to have the server mint
  one (returned in the reply); pass it back on later turns to continue the
  thread.
- `llm` config block: `system_prompt` / `system_prompt_file` (explicit > file >
  built-in default), `max_tokens` (2048), `temperature` (0.7). Provider/api_key
  are intentionally NOT read here — they come from the active profile.

## v0.1.0 — 2026-05-28

Initial release. Echo-only Agent-to-Agent (A2A v1.0) communication over JSON-RPC between Hermes profiles. Each plugin instance runs its own embedded uvicorn server on a dedicated A2A port.

### Added
- Spec-compliant A2A `/.well-known/agent-card.json` (public, with `securitySchemes.bearerAuth`)
- A2A JSON-RPC `SendMessage` endpoint with sync `Message` reply (`result.kind="message"`)
- Echo `response_handler` — `ping → pong`, otherwise echoes input
- Standalone `fleet.yaml` config (per-profile), env-var token resolution
- Unidirectional peer schema — declare peers you initiate calls to; inbound side just sets `server.token_env`
- `fleet_send(agent, message)` agent tool registered via `ctx.register_tool(..., is_async=True, ...)`
- Minimal `httpx`-based A2A client with `python -m a2a_fleet.client <agent> <message>` CLI
- Plugin lifecycle: `register(ctx)` boots the A2A uvicorn; `disable()` stops it gracefully
- Test suite: 23 tests across 6 files (fleet config, agent card, JSON-RPC, client, server lifecycle, regression guards)

### Architecture
- Embedded uvicorn on dedicated port — not mounted under the Hermes dashboard gateway. Decision rationale and pivot history live at the top of `README.md`.

### Safety & correctness fixes (post-review)
- `start_server()` polls the background task and surfaces uvicorn bind failures (e.g. port-in-use) as `A2AServerStartError` instead of falsely reporting success.
- `register()` captures + logs `start_server()` exceptions instead of swallowing them via `loop.create_task`.
- `fleet.enabled: false` is honored — no tool registration, no server start.
- Misconfiguration (`auth_required: true` with unset `token_env`) returns a JSON-RPC error envelope (`-32603`) instead of a plain HTTP 500.
- Bearer-token comparison uses `hmac.compare_digest` (timing-safe).
- Removed a module-level `asyncio.Lock` that bound to the first event loop and broke across multiple `asyncio.run()` calls.

### Deferred to v0.2+
- `TaskManager` bridging A2A tasks to real Hermes agent sessions
- SSE streaming for `SendStreamingMessage` and task status updates
- LLM-backed `response_handler` (reads existing Hermes LLM config)
- Push notifications, task persistence, OAuth/mTLS
- Optional upstream Hermes patch for third-party A2A client discovery (current fleet is closed-discovery via `agent_card_url` in `fleet.yaml`)

### Security notes
- v0.1 defaults `auth_required: true` — inbound `/jsonrpc` requires a bearer token unless explicitly disabled. Configure `token_env` for the self-server and each peer. Bearer comparison is constant-time (`hmac.compare_digest`).
- No CORS middleware: A2A is server-to-server (browsers are not A2A clients), so wildcard CORS would be misleading and is intentionally omitted.
- The Hermes dashboard gateway and the a2a_fleet uvicorn are two independent surfaces — disabling one does not affect the other.
