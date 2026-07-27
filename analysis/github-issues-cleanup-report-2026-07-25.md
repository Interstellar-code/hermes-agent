# GitHub issues cleanup report — 2026-07-25

Scope: all 29 open issues in `Interstellar-code/hermes-agent`, read-only triage against current `origin/main` (`7a1c493912`). No issue, label, milestone, or repository file was changed while preparing this report.

## Executive recommendation

Start with **10 high-confidence closures**. Six retire the `karpathy-self-improve` issue tree, and four are already answered or implemented on current main. This reduces the open list from 29 to 19 without writing code.

Then do one small implementation pass on **#17**. Keep the security bug **#155** and the matrix-memory correctness/resource bugs **#160/#171/#196** ahead of speculative roadmap work.

## Close now — high confidence

Close in this order so child issues are resolved before umbrellas.

| Order | Issue | GitHub reason | Why it is safe |
|---:|---|---|---|
| 1 | [#175](https://github.com/Interstellar-code/hermes-agent/issues/175) | Completed | The owner comment already resolves the question as Option C: the system prompt is stable for the session and SOUL changes reach new sessions. PR #178 stored `live_takes_effect_at_next_session`. |
| 2 | [#186](https://github.com/Interstellar-code/hermes-agent/issues/186) | Completed | Merged PR #190 replaced model-authored unified diffs with full-file output and local `difflib` generation. The reported patch-hunk failure class no longer exists. |
| 3 | [#182](https://github.com/Interstellar-code/hermes-agent/issues/182) | Not planned | The counter-reset bug is real, but it exists only in the daemon being retired. Persisting it would add schema/runtime work to code scheduled for deletion. |
| 4 | [#183](https://github.com/Interstellar-code/hermes-agent/issues/183) | Not planned | This is conditional coordination for the retiring target resolver, not a current personas bug. No replacement issue is needed. |
| 5 | [#134](https://github.com/Interstellar-code/hermes-agent/issues/134) | Not planned | Pre-Exec2 design review for the retiring plugin. Some findings landed; the remainder no longer has an implementation target. Do not call it completed. |
| 6 | [#133](https://github.com/Interstellar-code/hermes-agent/issues/133) | Not planned | Parent/umbrella scope is explicitly abandoned by the sunset decision. Close last among the plugin issues. |
| 7 | [#109](https://github.com/Interstellar-code/hermes-agent/issues/109) | Completed | Its near-term ask—atomic `last_stdout` persistence plus `prefix_drifted`—landed in merged PR #111 and is present in `plugins/a2a_fleet/templates/agy_receiver.py`. The long-term handshake remains independently tracked by #71. |
| 8 | [#120](https://github.com/Interstellar-code/hermes-agent/issues/120) | Completed | The issue's own comment says implemented in merged PR #121; current history contains the corrective listener-on-`adapter.connect()` fix and documentation PR #122. |
| 9 | [#128](https://github.com/Interstellar-code/hermes-agent/issues/128) | Completed | Current main has the archived column/state behavior, `PATCH /api/sessions/{id}`, default exclusion plus archived filters, JSON boolean output, and archive/unarchive tests. The API uses `archived=exclude|only|include`, a superset of the requested boolean filter. |
| 10 | [#169](https://github.com/Interstellar-code/hermes-agent/issues/169) | Completed | The root cause was profiled and fixed by commit `4bdece1f24`: synchronous SessionDB listing and AIAgent construction moved off the event loop; the issue comment records deployed latency recovery. |

Suggested short comments are in the final section.

## Consolidate, but do not close blindly

| Issues | Recommendation | Reason |
|---|---|---|
| [#160](https://github.com/Interstellar-code/hermes-agent/issues/160), [#171](https://github.com/Interstellar-code/hermes-agent/issues/171) | Choose #171 as the canonical executable bug; close #160 as duplicate only after copying its cross-surface acceptance criterion into #171. | Both describe divergent Mnemosyne DB resolution. #160 proves gateway/dashboard count divergence; #171 narrows it to ingestion versus private recall and supplies newer path evidence. They are the same bug class, but #160's “all surfaces agree” verification must not disappear. |
| [#132](https://github.com/Interstellar-code/hermes-agent/issues/132), [#166](https://github.com/Interstellar-code/hermes-agent/issues/166) | Prefer #166's existing generic broadcast-channel infrastructure; mark #132 superseded only after #166 explicitly carries replay/snapshot requirements needed by Matrix3D. | Both seek a fleet-wide delegation event feed. #132 proposes a new plugin and dedicated `/api/activity`; #166 reuses `/api/pub` + `/api/events`, which is the smaller footprint. #132 also requests a recent ring buffer, which #166 currently lists only as optional sequence/replay. |
| [#76](https://github.com/Interstellar-code/hermes-agent/issues/76), [#129](https://github.com/Interstellar-code/hermes-agent/issues/129), [#130](https://github.com/Interstellar-code/hermes-agent/issues/130), [#142](https://github.com/Interstellar-code/hermes-agent/issues/142) | Keep the three concrete gaps open. Update #76 into an index, or close it only after moving every still-open checkbox to child issues. | The merged MVP did not deliver the programmatic workflows or per-dispatch Kanban cards promised by the epic. #142 also retains separate LOW/NIT debt. Closing the epic today without migration would hide accepted gaps. |
| [#71](https://github.com/Interstellar-code/hermes-agent/issues/71), [#146](https://github.com/Interstellar-code/hermes-agent/issues/146) | Keep both for now; link #71 as protocol design and #146 as nearer-term hardening/test execution. | They overlap conceptually but have different deliverables. #109 and #120 can close without closing either. |

## Easiest implementation first

1. **#17 — running cron status in `/api/jobs`**

   Smallest useful implementation candidate. The API currently returns `_cron_list()` unchanged, while cron runs already use `cron_<job_id>_<timestamp>` SessionDB rows and end them with `cron_complete`. Enriching list/get output from existing session state avoids new persistence and new model-tool surface. Add one targeted API test.

2. **#191 — explicit session/project binding**

   Do not start a second implementation: its comment says work is already in flight on `feature/191-session-binding`. Review/finish that branch after #17 instead.

3. **#195 — multiplexing gap-fill**

   Continue only with the 0.19 rebase it explicitly depends on. The issue says the core is already wired; remaining work is fault isolation and routing/config parity, not a rebuild.

## Keep open and prioritize by risk

### Correctness/security before roadmap work

| Priority | Issue | Disposition |
|---:|---|---|
| P0 | [#155](https://github.com/Interstellar-code/hermes-agent/issues/155) | Keep open. This is interpreter-source injection from untrusted workflow input. It is not redundant with closed #81, which dealt with shell semantics. Fix by passing values through the environment and migrating bundled workflow scripts. |
| P1 | [#196](https://github.com/Interstellar-code/hermes-agent/issues/196) | Keep open and reproduce at the exact connection site. It is a resource-exhaustion bug with live FD evidence, but the issue itself says the blamed code path came from external analysis and is not yet reporter-verified. Do not “fix” it with a higher NOFILE limit. |
| P1 | [#171](https://github.com/Interstellar-code/hermes-agent/issues/171) | Keep as canonical matrix-memory DB-path bug; fold #160 evidence into it. |
| P1 | [#153](https://github.com/Interstellar-code/hermes-agent/issues/153) | Keep open. It blocks the already-closed mcp_lazy sunset #152. |
| P1 | [#168](https://github.com/Interstellar-code/hermes-agent/issues/168) | Keep open. Distinct symptom from #153: the fallback mcp_lazy promotion path loops without promoting. If mcp_lazy will be removed immediately after #153, avoid a broad redesign; fix only what is required for the migration window. |

### Feature/roadmap backlog — retain, schedule later

- **#141** categorized memory sections: real feature, not a duplicate. It changes prompt/context behavior and needs cache/invariant-aware design; not an “easy cleanup” item.
- **#143** personas: Build 1 landed, but the issue body still has eight unchecked acceptance items including SwitchUI consumption. Update the checklist/status; do not close until remaining scope is either delivered or split.
- **#146** A2A hardening roadmap: keep as roadmap after closing implemented children #109/#120.
- **#166** delegation broadcast: preferred successor direction for #132, but not implemented.
- **#191** session binding: implementation in flight; avoid duplicate work.
- **#195** multiplexing: active 0.19 rebase gap list.

## Full disposition of all 29 open issues

| Issue | Disposition |
|---|---|
| #17 | Implement first |
| #71 | Keep — protocol RFC |
| #76 | Convert to index / close only after gap migration |
| #109 | Close completed |
| #120 | Close completed |
| #128 | Close completed |
| #129 | Keep — concrete gap |
| #130 | Keep — concrete gap |
| #132 | Supersede with #166 after replay requirement transfer |
| #133 | Close not planned — sunset |
| #134 | Close not planned — sunset |
| #141 | Keep — feature design |
| #142 | Keep — concrete backlog |
| #143 | Update checklist; keep until residual scope split/done |
| #146 | Keep — roadmap |
| #153 | Keep P1 — blocks lazy-loader migration |
| #155 | Keep P0 — security |
| #160 | Duplicate into #171 after evidence transfer |
| #166 | Keep — preferred generic broadcast design |
| #168 | Keep P1 — migration-window bug |
| #169 | Close completed |
| #171 | Keep P1 — canonical DB-path correctness bug |
| #175 | Close completed |
| #182 | Close not planned — sunset |
| #183 | Close not planned — sunset |
| #186 | Close completed |
| #191 | Keep — implementation in flight |
| #195 | Keep — active rebase gap-fill |
| #196 | Keep P1 — reproduce then fix leak |

## Suggested closure comments

### #175

> Resolved as Option C. The stable system prompt is intentionally cached for the AIAgent/session lifetime, so SOUL.md edits affect new conversations rather than the active one. PR #178 shipped the corresponding `live_takes_effect_at_next_session` metadata. Closing the answered question as completed.

### #186

> Implemented by merged PR #190. The proposer now returns the complete updated file and Hermes computes the unified diff locally with `difflib`, so model-authored hunk headers and line counts can no longer cause this failure. Closing as completed.

### #182

> The defect is valid, but it is confined to the daemon we are sunsetting. Persisting this counter would add schema/runtime work solely to code being removed, so we will not implement it. Closing as not planned.

### #183

> This was conditional coordination rather than a current bug. The karpathy target resolver is being retired with the plugin, so there is no resolver to adapt to #143 and no generic work to migrate. Closing as not planned.

### #134

> This was a pre-Exec2 review for `karpathy-self-improve`. Several findings landed, but the plugin is now being sunset and the remaining findings no longer have an implementation target. Closing as not planned, not as fully completed.

### #133

> We have decided to sunset `karpathy-self-improve`; the umbrella's remaining P0–P4 scope will not be completed. Existing merged work remains in history, while removal will be handled as sunset cleanup. Closing as not planned.

### #109

> The near-term fix landed in merged PR #111: `last_stdout` is persisted atomically and prefix drift is recorded/surfaced via `prefix_drifted`. The long-term handshake remains independently tracked by #71. Closing this two-horizon proposal as completed for its shipped near-term scope.

### #120

> Implemented by merged PR #121, with follow-up documentation in PR #122. The listener now starts from `A2AFleetAdapter.connect()` alongside the agent bridge, eliminating the bind race while enabling Hermes↔Hermes agent peering. Closing as completed.

### #128

> Implemented on current main. Session archive state is persisted server-side; `PATCH /api/sessions/{id}` archives/unarchives; default lists exclude archived sessions; archived-only/include filtering and regression tests are present. Closing as completed.

### #169

> Implemented by `4bdece1f24`. The profiled synchronous SessionDB listing and AIAgent construction paths now run off the aiohttp event loop, and the issue's deployment follow-up records restored health latency. Closing as completed.

## Sunset cleanup that should become one new issue/PR

Remove the plugin as a single bounded cleanup rather than opening fixes for its retiring internals:

- `plugins/karpathy-self-improve/`
- plugin listing/docs and profile enablement
- the SwitchUI self-improve surface in its own repository
- the now-orphaned `hermes_identity_override` / `_eval_identity_override` seam, **only after confirming no other runtime consumer remains**

Keep the generic per-conversation prompt-cache contract unchanged.
