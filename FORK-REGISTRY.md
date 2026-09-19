# FORK-REGISTRY — Interstellar hermes-agent fork feature registry

**Purpose.** A maintained map of every custom feature/code surface the Interstellar fork owns,
consulted at **every upstream-merge exercise** so nothing obvious is missed, and updated **in the
same commit** as any divergence-touching change. Git history remains the source of truth; this file
is a **claims ledger with provenance** — every entry carries implementing SHAs and a last-verified
stamp. An entry without a SHA is a rumor, not a claim.

**Verification anchors** — re-stamped **2026-09-19** against **v0.21.3**, the current integration target. Read-only.

| Anchor | Value |
|---|---|
| fork `main` HEAD | `422c26e928` (v0.19.17 line, 2026.8.14 tag + post-tag fork work) |
| **TARGET — upstream v0.21.3** (= `v2026.9.14`) | **`345cd2b057`** ← pin everything here |
| upstream v0.21.1 tag (= `v2026.9.7`) | `2237be3559` (superseded target; clean ancestor of v0.21.3) |
| upstream v0.20.6 tag (= `v2026.8.27`) | `5fc308a707` (historical; the 0.20 doc's target) |
| `upstream/main` (local ref, fetched 2026-09-19) | `228022ef5b` — **moving; never a target.** Was `0dcadf6f41` |
| merge-base fork↔upstream | `3ef6bbd201` (upstream v0.19.0 release) |
| **replay surface at v0.21.3** | **126 files / 173 fork commits** (51 of the 126 are tests) |
| replay surface at v0.21.1 | 126 files / 171 fork commits — **identical file set, zero new collisions** |
| replay surface at v0.20.6 | 125 files / 171 fork commits |
| fork-only paths vs v0.21.3 | **1090** = **334 class B** + **756 class C** |
| fork-only paths vs v0.21.1 | 695 = 333 class B + 362 class C |
| class C needing a decision at v0.21.3 | **11 of 756** — the other **745 are inherit-the-deletion** (0 fork commits) |
| Last-verified stamp for all entries | **2026-09-19** (re-stamped to v0.21.3) |

> **Reading the 0.21.1 → 0.21.3 delta.** The window is large — 2,025 commits, 3,695 files,
> +233,118/−49,072 — but it costs the fork almost nothing. The colliding file set is **byte-identical
> at 126** with **zero new collisions**; the replay grows by **2 commits** (171 → 173). Class C
> doubling (362 → 756) is upstream housekeeping: only **11** of the 756 are paths the fork ever
> committed to, and 9 of those 11 are test files that follow their subject — the two substantive
> ones (`acp_registry/agent.json`, `plugins/observability/nemo_relay/__init__.py`) are already
> decided in §2b. Class B is **unchanged**: the set delta vs v0.21.1 is empty; the count moved
> 333 → 334 only because this registry file was itself added to the fork.
>
> **Therefore: target v0.21.3 directly.** Skipping v0.21.1 adds no conflict surface and avoids a
> second integration. Pin late — if a newer tag lands before execution, re-run §5 rather than
> trusting this table.

## §0 The three divergence classes (read this before §1)

A registry organized only around *collision* misses two-thirds of the divergence. Every fork/upstream
difference falls into exactly one class, and each fails differently at adopt time:

| Class | Size at **v0.21.3** | How it fails | Covered by |
|---|---|---|---|
| **A — SHARED-MODIFIED** (both sides changed the same existing file) | **126** (173 fork commits) | textual/semantic conflict | §2, §3 |
| **B — FORK-ADDED** (exists on fork, never existed upstream) | **334** | **silently dropped** by a tree-level adopt — never appears in a conflict list | §1, §2 |
| **C — UPSTREAM-REMOVED** (existed at merge-base; upstream deleted it since; fork still carries it) | **756**, of which **only 11 need a decision** | **silently deleted** by adopt — usually correct, occasionally not | §2b |

Class B is the one the intersection cannot see: a `read-tree`/checkout from upstream drops fork-added
paths without a conflict marker. The `_matrix-memory-mnemosyne` gitlink hazard (§1) is one instance of
this class, not a special case.

Class C is mostly **inherit the deletion** — upstream reorganized `skills/` (243 paths), `apps/desktop`
(13), `website/` (27), `optional-skills/` (9), `.github/` (8), `infographic/` (9). The fork has **2
commits total** touching `skills/`, i.e. it authored none of it; it is carrying upstream's pre-reorg
tree. Do not mistake these for fork assets. The exceptions — class-C paths the fork *actively
maintains* — are enumerated in §2b and are the only ones requiring a decision.

SHA discipline used here: every fork SHA listed was re-checked `git rev-parse` **and**
`git merge-base --is-ancestor <sha> main` on 2026-09-11; upstream SHAs are ancestors of the cited
tag or marked post-release where relevant. Anything not re-checkable is marked **UNVERIFIED**
in-row rather than omitted.

Evidence sources: `hermes-0.20-rebase-procedure.md` (incl. Review Amendments §A–§C),
`neo-0.20-commit-ledger.md` (36-commit classification: 13 MECHANICS / 11 NOT_UPSTREAMED /
9 PARTIALLY_UPSTREAMED / 2 FORK_ONLY / 1 SUPERSEDED / 0 UPSTREAMED), `hermes-0.21-upstream-analysis.md`
(§4.4 R-03 corrections), `hermes-0.20-rebase-procedure-review.md` + `-hygiene-review.md`.

---

## §1 Fork-only plugins

All six runtime-enabled plugin dirs are **absent at the v0.20.6 tag** (`git cat-file -e
5fc308a707:plugins/<name>` → exit 128, re-verified 2026-09-11) — clean replay for the directories
themselves. The risk lives in the *seams*, noted per row. (Six plugin dirs + the memory/submodule
row = seven rows below.) All are **class B** (§0): the intersection never lists them, so their
survival must be asserted explicitly, not inferred from a clean conflict report.

> **G1 ANSWERED — 2026-09-19: the Sep-2026 plugin-compat layer does NOT disable our plugins. It is
> advisory-only at `345cd2b057`.** An earlier pass concluded "enforcement is live, all 7 plugins
> DISABLED on adopt, 80 gated imports". That is **withdrawn**. Three independent findings, each
> verified separately:
>
> 1. **No load-path gate exists.** `PluginManager.discover_and_load` gates through
>    `_gate_manifest(m, disabled, enabled)` → `gate_manifest(manifest, disabled, enabled)` — it takes
>    no compat input. `_refresh_plugin_compat_report` runs *after* loading and is fail-open (bare
>    `except` → `logger.debug`). Grepping `removal_in_effect|ALLOW_KEY|allow_deprecated_imports` and
>    `scan_plugin|compat_report` over every non-test `.py` at the tag returns **only reporting
>    surfaces**: `cli_info_mixin.py` (banner), `doctor_config.py` (a check), `plugins_cmd.py`
>    (`hermes plugins compat`), `update_cmd_maint.py` (post-update notice), `subcommands/plugins.py`
>    (a help string). The "plugin is **not loaded**" claim lives in a docstring
>    (`plugin_compat.py:14`) and argparse help text — **not in code**.
> 2. **Bundled plugins are exempt regardless.** `compat_report()` filters
>    `source != "bundled"`, and `plugins.py:3` defines bundled as `<repo>/plugins/<name>/` — where
>    all seven fork plugins live.
> 3. **Zero gated names regardless of 1 and 2.** Running upstream's own `plugin_compat.scan_source`
>    against upstream's own `compat_manifest.json` (2091 entries / 335 facades, extracted from the
>    tag) over the seven fork plugin dirs yields **0 hits**. Positive control: the same scanner flags
>    3/3 synthetic `moved-lazy` imports, so 0 is a measurement, not a parse failure.
>
> **Where "80" came from:** it counted import *statements* whose module merely carries a
> `PLUGIN-COMPAT` block (fork-7 = 89 by this metric). That is not the criterion — the criterion is
> name-level. Example: `gateway.config`'s compat block restores exactly one name, `json`;
> `Platform` and `PlatformConfig` are still normally defined at `gateway/config.py:167,360`.
>
> **Do not rely on `plugins.allow_deprecated_imports`** — not because it is risky, but because at
> this tag nothing reads it on the load path. It is a no-op for our case.
>
> **Coverage of that audit:** 157 first-party imported names across the seven plugins resolved
> against the tag's real module tree; 0 tag modules failed to parse; 0 plain `import X` first-party
> statements (so no attribute-style blind spot). Residue: **3** names, none of them compat-layer —
> see the `mcp_lazy`, `hermes-switch-ui` and `personas` rows below.

| Name | Purpose | Paths | Implementing SHAs | Upstream status | Last verified | Replay guidance |
|---|---|---|---|---|---|---|
| **a2a_fleet** | A2A executor-fleet plugin: deploy/status/stop + `fleet_send` peer messaging, Herdr read-only session tools (v0.9.0) | `plugins/a2a_fleet/` (19 commits MB..main) | `0bcb7c6c21`, `8777522817`, `95a0b3f482`, … `dcef2056c3` (Herdr tools v0.9.0), `7da19f2278`, `7f707cf881` | FORK-ONLY; dir absent at tag, zero upstream churn | 2026-09-11 | **HIGH seam risk**: `_json_tool_result` wrapper + `ctx.register_platform("a2a_fleet", …)`; `adapter.py` imports `gateway.config` / `platform_registry` / `platforms.base` — upstream `3340bbbdad` + `272f4e4abe` config-gate A2A client tools and generalize `register_platform_handler` (post-release evidence). Replay dir clean; re-derive adapter imports + registration against tag's plugin API. |
| **hermes-switch-ui** | SwitchUI integration: `switchui_info`/`switchui_status` tools, `pre_llm_call` first-LLM-call nudge, `switchui` skill | `plugins/hermes-switch-ui/` (6 commits MB..main) | `9662b85c5f`, `f0b4861876`, `79868bf874`; serialization wrapper `7ad272fea2` | FORK-ONLY; dir absent at tag | 2026-09-11 | **MEDIUM (BFF/web_server seam)**: tools query live gateway/dashboard/BFF state (ports 3002/8642/9119); upstream churned 227 `hermes_cli` paths incl. `web_server.py` (`c0ff25a1f8`, post-release). Replay clean; re-verify what the tools observe post-adopt. **Dead defensive code (S-1) — LOW, corrected 2026-09-19:** `dashboard/plugin_api.py:29` `_require_auth()` imports `hermes_cli.web_server._is_authenticated`, a name with **no definition anywhere** (fork tree, merge-base `3ef6bbd201`, or tag `345cd2b057`). `except (ImportError, AttributeError): pass` swallows the guaranteed ImportError, so the guard returns None. Demonstrated: recompiled against a blocked `web_server`, it returns `None` for both plugins. **NOT an auth bypass** — an earlier revision of this row claimed it was; that claim is **withdrawn**. Plugin routers mount at `/api/plugins/{name}` (`web_server.py:19455`) and `auth_middleware:719` gates every `/api/` path absent from `PUBLIC_API_PATHS` (8 paths, no plugin path among them); gated/OAuth mode defers to `gated_auth_middleware` on the session cookie. The routes are gated in **both** modes. So `Depends(_require_auth)` is a **redundant second lock that silently does nothing** — hygiene, not an incident. Note the docstring's "Mirrors standard plugin auth pattern" is false: no other plugin (`kanban`, `a2a_fleet`, `workflow-engine`, `hermes-achievements`) puts any auth dependency on its dashboard routes. Cleanup option: drop the dead guard, or repoint to `_require_token()` (fork `web_server.py:485`, tag `:417`). See §4. |
| **mcp_lazy** | Lazy MCP server hydration: defer tool loading until first use | `plugins/mcp_lazy/` (5 commits MB..main) | `160e27db1f` (replay squash), `3f8db0c93e`, `b3ad70610d` | FORK-ONLY; dir absent at tag | 2026-09-11 | **HIGH seam risk**: `hook_impl.py` uses **`transform_tools` — a fork-only hook** (VALID_HOOKS: fork 24 vs tag 37; 14 tag-only hooks unaccounted — F-04). Upstream `091cc0e8be` (in-tag) scopes hook timeouts and **fails closed on `pre_tool_call`** → slow mcp_lazy hook now blocks the tool call. `transform_tools` survival is an explicit Phase B/F checkpoint. **SECOND fork-core dependency, found 2026-09-19:** `baseline_patch.py:76` does `from agent.usage_pricing import register_usage_observer` — that function is **fork-added core** (`agent/usage_pricing.py:22`), absent at merge-base `3ef6bbd201` **and** at tag `345cd2b057` (0 matches tree-wide). Taking `agent/usage_pricing.py` from upstream deletes it and mcp_lazy fails at import. See the §2 `usage_pricing` row. |
| **personas** | Persona overlays for delegation prompts (`persona_apply`, `persona_get`, `persona_list`) | `plugins/personas/` (2 commits MB..main) | `04e83be847`, `7ad272fea2` (JSON-serializer handler wrap) | FORK-ONLY; "no material conflict found" (`deleg_f91d3094`); task-1 change-count 0 | 2026-09-19 | **Dead defensive code (S-1) — LOW.** Same defect as the `hermes-switch-ui` row (see it for why this is **not** an auth bypass — middleware gates `/api/plugins/*` in both modes): `dashboard/plugin_api.py:33` `_require_auth()` imports the nonexistent `hermes_cli.web_server._is_authenticated` and returns on ImportError. The in-code comment asserts the split guard prevents "an open-auth bypass" — it defends the wrong branch: the ImportError path is the bypass, and it fires every call. **Demonstrated, not inferred:** recompiling the real `_require_auth` against a blocked `hermes_cli.web_server` returns `None` (allow) for both plugins; repo-wide `def _is_authenticated` count = 0. Otherwise replay clean; run registration smoke test at Phase F (absence of evidence ≠ registration works). |
| **projects** | Fork projects/delegation store + skills surface | `plugins/projects/` (6 commits MB..main) | `8a42d17343`, `c74dfa8697`, `7a2dd588be` | FORK-ONLY at file level (change-count 0), **semantic collision** upstream | 2026-09-11 | **MEDIUM (semantic, not file)**: upstream ships its own projects paradigm (`hermes_cli/projects_db*`, PR #49037 chain). Two implementations can coexist or collide at DB/permission seam; interacts with profile-scoped session DB (`92f6633ae2`, §3). Verify projects store + delegation durability post-adopt; reconcile rather than duplicate. |
| **workflow-engine** | DAG workflow orchestration: dashboard router auto-mount, background `hermes workflow daemon`, approvals | `plugins/workflow-engine/` (7 commits MB..main) | `c759806f41`, `9f79a855e9`, `44c703ac6b`; fixes `5cd2d5291c` (profile/home propagation), `7a4bcd19f0` (Windows footguns, FORK_ONLY-classified) | FORK-ONLY; dir absent at tag | 2026-09-11 | **MEDIUM**: `dashboard/plugin_api.py` auto-mounted by `web_server.py` via `_mount_plugin_api_routes` — a **private web_server seam upstream just refactored** (`c0ff25a1f8`, post-release). Daemon ownership + restart interaction also at stake. Replay clean; re-derive the mount seam. |
| **memory + `_matrix-memory-mnemosyne` submodule** | matrix-memory provider under the memory hub; Mnemosyne-backed store | `plugins/memory/` (gitlink **`3c33c50`** — corrected 2026-09-19, was `4d3377f37be`; sibling tree `plugins/memory/matrix-memory/`) | `b7aad360ff` (matrix-memory replay under v0.18 hub), `1a4675b9d4` (fail-loud on unloadable provider) | FORK-ONLY submodule: **gitlink absent at the v0.20.6 tag** (`git ls-tree 5fc308a707 plugins/memory/` shows no entry) | 2026-09-11 | **Adopt hazard**: a read-tree/checkout from upstream **drops the submodule** — re-add at **`3c33c50` or later** after adopt, **never `4d3377f`** (it predates the §2 fd-leak fix and silently reverts it). `plugins/memory/__init__.py` is the single colliding file (see §2). Upstream reorganized memory into a provider hub (byterover, hindsight, holographic, honcho, mem0, openviking, retaindb, supermemory — matrix-memory not among them). |

## §2 Core patches by file (top seam files)

| Name | Purpose | Paths | Implementing SHAs | Upstream status | Last verified | Replay guidance |
|---|---|---|---|---|---|---|
| **Dashboard version/updater safety** | Distinguish running vs on-disk version; detect stale code; strict non-destructive updater API for Switch UI; finish strict update end-to-end | `hermes_cli/web_server.py`; `hermes_cli/strict_update.py` (new); `hermes_cli/main.py`; `hermes_cli/subcommands/update.py` | `b310fa50e0` (#199), `19934ccc09` (#199), `77b6582fd5` (#200), `7b235a3518` (#200) | `web_server.py` ∈ 125-file ∩; upstream refactor hotspot (`c0ff25a1f8`, post-release) | 2026-09-11 | Replay all four; `web_server.py` is an off-loop refactor hotspot — expect textual conflict; verify updater never destructive (Phase I soak item). |
| **`register_usage_observer` (usage-pricing observer hook)** | Fork-added registration point letting a plugin subscribe to canonical usage events; `mcp_lazy`'s baseline logger is its only consumer | `agent/usage_pricing.py:22` (`def register_usage_observer(callback: Callable[["CanonicalUsage"], None]) -> None`); consumed at `plugins/mcp_lazy/baseline_patch.py:76` | (fork-added; SHA not yet attributed — predates this audit) | **class B / FORK-ONLY** — `agent/usage_pricing.py` exists upstream, but `register_usage_observer` does **not**: 0 matches at merge-base `3ef6bbd201` and 0 at tag `345cd2b057`, tree-wide | 2026-09-19 | **Silent-loss risk.** The file survives an adopt, so no conflict marker fires — but taking upstream's copy deletes the function and `mcp_lazy` dies at import with `ImportError: cannot import name 'register_usage_observer'`. This is a **file that merges cleanly while losing a fork symbol**, exactly the §0 class-B failure mode. On adopt: re-apply the function, then assert `python -c "from agent.usage_pricing import register_usage_observer"` exits 0. |
| **Profile / multiplex fixes** | Fail closed on ignored `/p/<profile>/` prefix; reconcile drifted profile schemas on read-only open; resolve profile cutover blockers | `gateway/platforms/api_server.py`, `gateway/platforms/webhook.py`; `hermes_state.py`; `cron/scheduler.py`, `gateway/config.py`, `gateway/run.py` | `eb8a6033eb` (#202), `994980a710` (#202), `86480a4c67` (#205) | All files ∈ 125-file ∩ (`api_server.py` = 22 fork commits touch) | 2026-09-11 | Re-derive against tag's profile machinery (heavy: 216 `profile` mentions in tag `tui_gateway/server.py`); assert cross-profile isolation (Phase I soak cluster 1). |
| **Plugin tool-result serialization** | Fix plugin tool schema envelope (core registry); wrap personas + switch-ui tool handlers with JSON serializer | `tools/registry.py`; `plugins/hermes-switch-ui/__init__.py`, `plugins/personas/__init__.py` (+ their `tests/test_register_contract.py`) | `f7afd535c6` (core registry envelope), `7ad272fea2` (handler wrappers) | `tools/registry.py` ∈ upstream-churned surface | 2026-09-11 | Replay; verify plugin tool results still serialize through the registry after adopt (registration smoke, Phase F). |
| **Memory hub collision** | Fork's matrix-memory shim lives in the ONE file both sides modify | `plugins/memory/__init__.py` | `b7aad360ff`, `1a4675b9d4` (fork side); upstream +364/−47 at the tag window | **Single verified colliding file** of the fork's 4 `plugins/memory` files vs upstream's 22 changed | 2026-09-11 | Phase E must pre-classify **`re-derive`, not `drop`** — merge semantics, never overwrite; the fork's matrix-memory provider work lives here. |
| **Kanban dashboard API** | Restore 7 template REST routes dropped in the 0.18 rebase; accept project links (`project_id`) in task API | `plugins/kanban/dashboard/plugin_api.py` | `ef2674c767` (+#161, current), `f3177fdd86`/`c2afd90f97` (project links; replay-tier re-land) — history includes `71ea654756` + revert `4ff928969a` | Upstream-owned plugin dir with fork-side edits (hygiene-review Part 2); upstream ships `hermes_cli/projects_db.py` but no kanban↔projects wiring | 2026-09-11 | Replay route restorations; re-verify against upstream's kanban churn (`kanban_db.py` +1,054 lines historically); confirm kanban↔projects link semantics against upstream's projects paradigm. |
| **opencode-zen test patch** | Fork-added provider tests (+60 lines) inside an upstream-owned model-provider dir | `plugins/model-providers/opencode-zen/tests/test_opencode_zen.py` | `7e44e6a2b3` | Upstream owns the provider dir; fork owns the test file (class B) | 2026-09-11 | Replay the test file; check upstream provider churn hasn't changed the surface under test. |
| **matrix-memory shim (distinct from the submodule)** | The provider that actually **loads** — the loader skips `_`-prefixed dirs, so the shim, not `_matrix-memory-mnemosyne`, is the live code path | `plugins/memory/matrix-memory/{__init__.py,plugin.yaml}` | `026be5a774` (replay under v0.18 hub), `f734b10b02` (#157/#158, submodule made real) | **class B** — both files absent at v0.20.6 and v0.21.1 | 2026-09-13 | Replay with the §1 memory row, but assert **separately**: the submodule gitlink and the shim are two independent drop risks, and restoring only the gitlink leaves the provider unloadable. `.gitmodules` (fork-added, `f734b10b02`) is a third. |
| **Fork-added `hermes_cli` modules** | Kanban template storage; projects prompt surface | `hermes_cli/kanban_templates.py`, `hermes_cli/projects_prompt.py` | `143199cfaa` (kanban_templates), `ff79a6c8cd` (#201, projects_prompt) | **class B** — absent at both tags; `strict_update.py` (own row above) is the third fork-added module here | 2026-09-13 | Replay all three as file adds. Known latent hazard: `kanban_templates` imports from `cron.jobs` — a prior squash-replay dropped its REST/cron halves and left an `ImportError`; assert the import resolves post-adopt, not just that the file exists. |
| **Operational scripts** | Session-key hygiene script | `scripts/prune_invalid_session_keys.py` | `6f72ffd449` (2 fork commits) | **class B** — absent at both tags | 2026-09-13 | Replay; low risk, but it is invisible to the intersection. |
| **mnemosyne fd-leak fix (inside the submodule)** | `init_triples()` opened a SQLite connection and never closed it — 2 fds (`.db` + `-wal`) leaked per memory operation; exhausted the gateway's fd budget at 7-day uptime (156 of 255 descriptors were this one db) | `plugins/memory/_matrix-memory-mnemosyne` → `mnemosyne/core/triples.py` | **`3c33c50`** in `Interstellar-code/mnemosyne` (submodule repo, branch `main`); gitlink bumped `4d3377f` → `3c33c50` | Fork-owned fix; **not upstreamed** — the mnemosyne repo has issues disabled, so the defect is filed as hermes-agent **#240** | 2026-09-19 | **Highest-risk row in this table.** The fix lives in a *submodule commit*, so it is protected by the gitlink and nothing else — a `read-tree`-style adopt drops the gitlink (§1) and silently reverts this fix along with it. On any adopt: re-add the gitlink at **`3c33c50` or later**, never at `4d3377f`. Verify with the #240 repro: 60 consecutive `init_triples()` calls must move the process fd count by **0**. |
| **mnemosyne connection displacement (#240 secondary — OPEN, unfixed)** | `_get_connection()` caches one connection per thread in a single slot and overwrites it **without closing** on a db_path change or failed liveness probe; the displaced handle stays pinned as `self.conn`, so it is reachable and `gc.collect()` provably cannot reclaim it | `mnemosyne/core/{memory.py:63-81,beam.py:441-475}`; pinned at `beam.py:2781`, `memory.py:159`; re-pinned by `EpisodicGraph`/`VeracityConsolidator`/annotation+canonical stores | none yet — **unfixed** | Fork-owned defect, documented in #240 under "Secondary defect" | 2026-09-19 | **CONFIRMED by isolated repro, not inference** (#240 comment, 2026-09-19). Displaced + unpinned → `gc.collect()` reclaims it; displaced + **pinned as `self.conn`** → reclaims **0**, handle stays open and usable. Therefore `conn_sweep` — which already runs a real `gc.collect()` every `_SWEEP_EVERY = 50` opens from `memory.py:68`/`beam.py:458` — is **structurally incapable** of reclaiming this defect; it is correct only for the stranded-thread case (#196). No sweep interval helps: reachability, not frequency, is the binding constraint. **Fix validated:** path-keyed thread-local turns 20 alternating ops from 20 connections / 42 fds into **2 connections / 4 fds** (2.10 → 0.20 fds per op) with all 133 `self.conn` sites untouched and every pinned handle still usable. Must land in **two** places — `memory.py:63-81` **and** `beam.py:441-475`. Residual after that fix: the liveness-probe trigger (`beam.py:447-452`) still displaces within a path key, and path-keying **bounds** at (paths × threads) rather than closing — `BeamMemory`/`Mnemosyne` still define no `close()`/`__del__`/`__exit__`. ⚠ **Do not size this from per-day fd rates.** Of 254 fds in the live gateway, 138 are `venv/` module files and only 32 are mnemosyne; over 30 s the count was flat. Growth is episodic, driven by path alternation (~2.1 fds/switch), so runway is a workload property, not a clock. The earlier "≈30/day → ~265 days" figure conflated total fds with leaked fds and is withdrawn. |
| **nemo_relay observability plugin** | Delegation `agent_id` propagation; the fork's +2-line edit rides on top | `plugins/observability/nemo_relay/{__init__.py,plugin.yaml,README.md}` (all three) | `1cf54a2c5a` (#194) | ⚠ **CORRECTED 2026-09-13 — class C, not "upstream-owned".** Present at merge-base `3ef6bbd201`, **ABSENT at both v0.20.6 and v0.21.1** → **upstream deleted the plugin.** Tag retains only `scripts/smoke_nemo_relay_shared_metrics.py`, which references it. | 2026-09-19 | **DECIDED 2026-09-19 — DROP** (inherit upstream's deletion, option b). The prior guidance ("re-locate the hunk" against upstream's 964→1238-line growth) is void — there is no upstream `nemo_relay` at the tag to relocate into. At adopt: let all three files go, and drop `1cf54a2c5a`'s `agent_id` propagation with them. **Before the adopt lands,** check whether the tag's surviving `scripts/smoke_nemo_relay_shared_metrics.py` still references the removed module — if it does, that script goes too or it breaks. |

## §2b Class C — upstream deleted it, fork still carries it

362 paths existed at merge-base `3ef6bbd201` and are **gone at v0.21.1**. An adopt deletes all of
them. For most that is the right outcome and needs no row. Only paths the fork has **actively
committed to** since the fork point require a decision — those are here. Everything else is
**inherit the deletion**.

| Path | Fork commits since MB | Decision required | Verified |
|---|---|---|---|
| `acp_registry/{agent.json,icon.svg}` | **12** | **DECIDED 2026-09-19 — RETIRE** (inherit upstream's deletion). Investigated: `agent.json` is a *publishing manifest* for the ACP editor registry, not the ACP feature — the implementation is `acp_adapter/` (10+ modules, alive at v0.21.1, still wired via `hermes-acp = "acp_adapter.entry:main"`), and is **unaffected**. All 12 fork commits are release-script version bumps; the net diff vs merge-base is **whitespace only** (two arrays reflowed). The manifest is already stale — it advertises `hermes-agent[acp]==0.19.0` while the fork is at 0.19.17 — and its entire payload is a uvx/PyPI install instruction for a channel the fork itself abandoned in `9b8c38b378` (#217, "stop shipping via PyPI"). Upstream deleted it in `d84e11af4d` for the same reason. **Reopen only if** the fork intends to publish to an ACP editor registry under its own identity — which needs a rewrite regardless, since the file currently advertises `NousResearch/hermes-agent`. **Retire cost:** delete 2 files, drop `tests/acp/test_registry_manifest.py` + `tests/scripts/test_release_acp_registry.py`, strip manifest maintenance from `scripts/release.py`. | 2026-09-19 |
| `plugins/observability/nemo_relay/*` | 1 (`1cf54a2c5a`) | **DECIDED — DROP.** Inherit the deletion; see the §2 row for the check to run before the adopt lands. | 2026-09-19 |
| `hermes_cli/subcommands/{postinstall.py,version.py}` | 0 | No — inherit deletion (fork never touched them). | 2026-09-13 |
| `tools/mcp_stdio_watchdog.py` | 0 | No — inherit deletion. | 2026-09-13 |
| `packaging/homebrew/*`, `MANIFEST.in`, `optional-mcps/blender/manifest.yaml` | 0 | No — inherit deletion. | 2026-09-13 |
| `skills/` (243), `apps/desktop/*` (13), `website/` (27), `optional-skills/` (9), `.github/pr-screenshots/` (8), `infographic/` (9), `.plans/`, `analysis/`, `docs/` | 2 total, all in `skills/` and incidental | No — **inherit deletion.** Upstream reorganized these trees; the fork authored none of the content. Explicitly listed so a future maintainer does not mistake 243 vanishing `skills/` paths for data loss. | 2026-09-13 |

> Method: class C = `comm -12` (fork tree ∩ merge-base tree) minus target-tag tree. "Fork commits"
> = `git rev-list --count <MB>..main -- <path>`. Zero means the fork is carrying a stale upstream
> file it never edited.

## §3 Contract surface (SwitchUI-facing features of the 36-commit wave, `86480a4c6758f..main`)

Status values: **OURS** (no upstream equivalent — replay) · **UPSTREAM-EQUIVALENT@version** ·
**SEMANTIC-RECONCILIATION-NEEDED** (upstream now ships its own machinery — reconcile, don't replay).
R-03 evidence (`hermes-0.21-upstream-analysis.md` §4.4, corrected 2026-09-11) is cited on the two
upstream-equivalent rows.

| Name | Purpose | Paths | Implementing SHAs | Upstream status | Last verified | Replay guidance |
|---|---|---|---|---|---|---|
| **Per-session model override** | HTTP-API session-scoped model override | `gateway/platforms/api_server.py`, tests | `088f76bc99` (#216) | **SEMANTIC-RECONCILIATION-NEEDED** — upstream ships its own machinery: **6 matches** in v0.21.1 tag `api_server.py` — `_session_model_override_for()` (L1911), `_rehydrate_session_model_override` (L1922), `runner._session_model_overrides` (L1928), call sites L1940/L2035, `"session_model_override"` (L2153); already 7 refs at v0.20.6 (ledger: PARTIALLY_UPSTREAMED). [R-03 correction] | 2026-09-19 | **SIZED 2026-09-19 — MERGE, not reconcile. Tier M.** Diffed both. The fork did **not** build a parallel design: `088f76bc99` *modifies upstream's existing* `_session_model_override_for` (the diff rewrites its docstring in place), so upstream's 6 refs are the **shared ancestor** the fork already builds on. Fork is a strict **superset**: keeps the runner-first read path verbatim, then adds (a) `_local_session_model_overrides` fallback so an HTTP switch works in standalone `api_server` with no `GatewayRunner`, (b) persistence into the sessions row `model_config` blob under `_MODEL_OVERRIDE_CONFIG_KEY = "_model_override"`, (c) `_PERSISTABLE_MODEL_OVERRIDE_KEYS = ("model","provider","base_url")` so `api_key`/`api_mode` are re-resolved, never written to disk. Surface: fork **39** refs vs tag **6**. **No semantic conflict to resolve** — this is a textual merge of ~500 api_server lines + a 672-line test file, relocated onto the decomposed layout. Risk is placement, not meaning. |
| **Approval bypass toggle** | HTTP API toggles its own approval bypass + reads its prompt; approvals emitted on the sessions chat stream | `gateway/platforms/api_server.py`, `tui_gateway/server.py`, + tests | `5953712ee9` (#228), `29c74428ef` (stream emission) | **OURS** — `approval_bypass` → 0 tag matches (fork 211 `approval` refs vs tag 5; upstream refs are argv/env/probe fixes) | 2026-09-11 | Replay bypass-toggle surface + stream emission; reconcile with tag's run-approval routes (`POST /v1/runs/{run_id}/approval`, `_approval_event_choices` L121). Phase D cluster 5 gate commit. |
| **`/goal` evaluate-and-continue** | Evaluate and continue a goal after a turn | `gateway/platforms/api_server.py`, `tests/gateway/test_session_api.py` | `68d49d19bc` (#231) | **OURS** — `/goal` and `goal` → 0 in tag `api_server.py` (2 `goal` refs live in the `api_server_runs.py` sibling) | 2026-09-11 | Replay in full (Phase D cluster 5 gate commit; §6.6 fork-owned surface). |
| **Run steering + per-request `reasoning_effort`** | Steer a running run; per-request reasoning effort; resume-aware history | `gateway/platforms/api_server.py`, `tests/gateway/test_api_server_runs.py`, `test_session_api.py` | `479b22fe5b` (#233) | **SEMANTIC-RECONCILIATION-NEEDED** — upstream ships run steering at v0.21.1: **8 `steer` matches** — `POST /v1/runs/{run_id}/steer` (L85), capability flag `run_steer` (L68), `pending_steer` (L3145–3157), `_handle_steer_run` (L3767). `reasoning_effort`: tag 5 / fork 30. [R-03 correction — original "0" matched only the word `steering`] | 2026-09-19 | **SIZED 2026-09-19 — split verdict.** **(a) Steering: genuine convergent evolution — recommend DROP the fork's and adopt upstream's. Tier S.** Both sides independently implement the *same public contract*: `POST /v1/runs/{run_id}/steer` **and** the `run_steer` capability flag exist on both (fork 2 refs / tag 2 refs — identical). Internals differ: upstream carries a `pending_steer` concept (a steer accepted after the final reply lands in `result["pending_steer"]` and is surfaced on the completed payload, `tag` 5 refs) which the **fork has zero of**; the fork's own implementation is larger (27 `steer` refs vs tag 8). Since the contract SwitchUI consumes is byte-identical on both sides, the fork's internals are most likely redundant. **Verify before dropping:** that upstream's `_handle_steer_run` (a `_run_route_delegate` into `api_server_runs.py`) accepts the same body shape, and that `pending_steer` appearing on completed payloads does not break a SwitchUI consumer that has never seen the field. **(b) `reasoning_effort`: additive, fork superset. Tier S.** Fork 30 refs vs tag 5; fork adds per-request validation (`_requested_reasoning_effort`, error code `invalid_reasoning_effort`) on top of upstream's `reasoning_config`. Straight merge. **(c) Resume-aware history: unexamined — replay in full.** |
| **Toolsets render fix** | Render toolsets as a list, not one character per line | `hermes_cli/dump.py`, `tests/hermes_cli/test_dump_toolsets.py` | `c00aed81ed` (#234) | **OURS** (rendering fix) with an upstream-adjacent block: tag `dump.py` L267–269 already diffs `toolsets` config vs default | 2026-09-11 | Replay rendering fix; verify against tag's toolsets-diff block (ledger: PARTIALLY_UPSTREAMED). |
| **`/subgoal` dispatch** | tui_gateway dispatches `/subgoal` + advertises dispatchable bundles | `tui_gateway/server.py`, `tests/test_tui_gateway_server.py` | `cdaa6500dd` (#237) | **OURS at the dispatch surface** — `subgoal` matches 15 tag files but **none under `tui_gateway/`**; upstream has slash-level `subgoal` in `cli.py`, `gateway/run.py`, `gateway/slash_commands.py`, `hermes_cli/*` | 2026-09-11 | Replay + reconcile with upstream's slash-level subgoal; assert dispatchable-bundle advertisement survives. |
| **Session-DB profile scoping (the 5008 fix)** | Scope session DB access to the session's profile; fixes the `5008 FOREIGN KEY` cause (session.branch not propagating profile) + per-call handle leak | `tui_gateway/server.py`, `tests/test_tui_gateway_server.py` | `92f6633ae2` (#232; body cites the 5008 FK cause and SwitchUI team's `--isolated` workaround) | **OURS** — no equivalent; tag carries heavy adjacent profile machinery (216 `profile` mentions in `tui_gateway/server.py`) | 2026-09-11 | Replay; re-derive against tag's profile handling; assert cross-profile DB isolation (Phase I soak). |

## §4 Diverged / reverted decisions

| Name | Purpose | Paths | Implementing SHAs | Upstream status | Last verified | Replay guidance |
|---|---|---|---|---|---|---|
| **No-PyPI shipping** | Stop shipping via PyPI | `.github/workflows/upload_to_pypi.yml`, `scripts/release.py`, `acp_registry/agent.json`, + tests | `9b8c38b378` (#217) | **SUPERSEDED** — upstream **also** removed the workflow; `upload_to_pypi.yml` ABSENT at tag (`git cat-file -e` → fail) | 2026-09-11 | No replay of the workflow deletion (already gone); replay only fork's `release.py`/`agent.json` deltas; verify no PyPI path resurrects. |
| **`nemo-relay==0.5.0` pin** | Pin instead of inheriting upstream's floating range | `pyproject.toml`-adjacent deps + `uv.lock` | `8712d5b7bf` (pin), `30acf2cb6a` (uv.lock regen, MECHANICS) | Upstream keeps a floating range | 2026-09-11 | Keep the pin; **do not transplant the `uv.lock` blob** — re-derive at the release-mechanics cluster (lockfile regen against target-tag deps). |
| **Six production ports env-override** | Soak hardening: all six hardcoded ports env-driven; unset == byte-identical | `HERMES_GATEWAY_PORT` (default 8642: kanban dispatcher, cron poller, karpathy wiring, switch-ui knowledge ×2), `HERMES_A2A_PORT` (default 9219: a2a codex_deploy) — call sites across gateway/plugins | `44c703ac6b` | No upstream equivalent | 2026-09-11 | Replay; note `HERMES_A2A_PORT` is **not** a bind-port setter (a2a listen port comes only from `fleet.yaml` → `server.bind_port`); `HERMES_GATEWAY_PORT` is inert when `gateway.port` is set in config (prod config sets 8642). |
| **OpenViking identification telemetry — declined** | Do-not-enable decision on upstream's outbound request identification | upstream `3db5267008` (upstream-owned; fork decision) | n/a (an inherit decision, not a fork commit) | Upstream adds request identification; anonymity companion `fab534b503` is safe | 2026-09-11 | **Do NOT enable pending explicit user decision** — the repo's contribution rubric rejects outbound telemetry/usage attribution without a generic opt-in. Inherit `fab534b503` automatically. |
| **Plugin dashboard auth guard (S-1) — LOW, cleanup when convenient** | Two fork plugins carry a dead redundant auth guard; the real gate is middleware and is intact | `plugins/hermes-switch-ui/dashboard/plugin_api.py:29`, `plugins/personas/dashboard/plugin_api.py:33` | n/a — **deliberately not fixed in this PR**; the fix is a behavior change, not an import repoint | Not an upstream defect: `_is_authenticated` has **no definition** at merge-base `3ef6bbd201` or tag `345cd2b057` either — the name was never real | 2026-09-19 | **Severity corrected 2026-09-19 — this row previously read "live auth bypass, AWAITING USER DECISION". That was wrong and is withdrawn.** What is demonstrated: recompiling each real `_require_auth` with `hermes_cli.web_server` blocked returns `None` for both, and repo-wide `def _is_authenticated` = 0 — the guard is a no-op. What was **inferred without checking, and is false**: that this left the endpoints exposed. Plugin routers mount under `/api/plugins/{name}` (`web_server.py:19455`), and `auth_middleware:719` 401s any `/api/` path not in `PUBLIC_API_PATHS` — which contains 8 paths, none of them a plugin path. Gated/OAuth mode defers to `gated_auth_middleware` on the session cookie. **Both modes gate these routes.** Residual risk is only the loss of defense-in-depth plus a misleading docstring. **Options:** (a) delete the dead guard and the `Depends(...)` — matches every other plugin, smallest diff, zero lockout risk; (b) repoint to `_require_token()` to restore a real second layer — still safe, since it correctly defers to the gate in OAuth mode. **Recommend (a)**, or (b) if defense-in-depth on these routes is wanted. Neither is urgent. **Method lesson:** "the guard is a no-op" and "the endpoint is exposed" are two claims; the second needs the mount path and middleware checked, not inferred. |
| **Image-routing reversal — verify before inherit** | Upstream behavior change reversing #29135 (aux-vision-backend route) | upstream `93de1d3430` (upstream-owned; fork gate) | n/a (an inherit decision) | Deliberate upstream behavior change, not fork work | 2026-09-11 | Confirm the new aux-vision-backend route matches the fork's expectations before accepting (Phase H turn-on/verify/inherit, user's call). |

## §5 Registry maintenance protocol

**Update-in-same-commit rule.** Any commit that adds, moves, or removes fork-owned surface — a new
plugin, a core-file patch, a contract route, a reverted decision — updates the relevant §1–§4 row
**in the same commit**. A divergence-touching change without a registry delta is incomplete.

**Per-merge verification procedure** (run at every upstream-merge exercise, against the target tag
*after* refs are refreshed):

1. **Recompute the live intersection (class A).** ⚠ *Corrected 2026-09-13: the command originally
   printed here — `git diff --name-only main...<target-tag>` — returns **7,968** files, not 125. In
   `git diff`, the three-dot form means "changes on the right side since the merge-base", i.e. the
   upstream-changed set alone. The intersection is not expressible as a single `git diff`.*

   ```bash
   MB=$(git merge-base main <target-tag>)
   git diff --name-only "$MB"..main          | sort -u > /tmp/fork.txt  # 463 fork-changed
   git diff --name-only "$MB"..<target-tag>  | sort -u > /tmp/up.txt    # 7,968 @ v0.20.6
   comm -12 /tmp/fork.txt /tmp/up.txt        > /tmp/isect.txt           # 125 @ v0.20.6 · 126 @ v0.21.1
   wc -l < /tmp/isect.txt
   ```
   **Sanity gate:** a result in the thousands means the broken form was re-run. The intersection is
   ~125; step 2 is meaningless against 7,968 rows.
2. **Registry paths vs intersection:** every §1/§2 path that appears in the intersection must have
   a row with current upstream status. **Any intersection file with no registry row = a registry
   gap — close it before replay planning.**
2b. **Fork-added survival check (class B) — the intersection cannot see this.** Fork-added paths
   never appear in a conflict list; a tree-level adopt drops them silently.

   ```bash
   git ls-tree -r --name-only main          | sort > /tmp/f.txt
   git ls-tree -r --name-only <target-tag>  | sort > /tmp/t.txt
   git ls-tree -r --name-only "$MB"         | sort > /tmp/m.txt
   comm -23 /tmp/f.txt /tmp/t.txt > /tmp/forkonly.txt   # 695 @ v0.21.1
   comm -23 /tmp/forkonly.txt /tmp/m.txt               # class B: 333 — MUST survive the adopt
   comm -12 /tmp/forkonly.txt /tmp/m.txt               # class C: 362 — see §2b
   ```
   Assert every class-B path exists post-adopt. Pay special attention to the three independent
   memory drop-risks (submodule gitlink, `matrix-memory` shim, `.gitmodules`) — restoring one does
   not restore the others.
2c. **Class-C triage.** For each class-C path, `git rev-list --count "$MB"..main -- <path>`. Zero
   fork commits → inherit the deletion. Non-zero → it needs a §2b row and an explicit decision.
3. **Registry paths vs upstream-unchanged:** any registry path *unchanged* upstream since the last
   verified stamp → **drop its warning flag** (risk expired) and re-stamp; do not silently keep
   stale HIGH/MEDIUM caveats.
4. Re-verify every SHA in the touched rows (`git rev-parse` + `merge-base --is-ancestor <sha> main`);
   mark anything uncheckable **UNVERIFIED** in-row — never delete a row for failing verification.
5. Re-stamp the anchor table and the entries verified in this pass; the stamp is the provenance
   claim, so it moves only when the check actually ran.

Git history remains the source of truth; where this file and `git log` disagree, git wins — then
fix the file.

---
*Created 2026-09-13 from the 0.20/0.21 rebase-analysis evidence base; all fork SHAs verified
ancestor-of-`main` at write time. Repo treated read-only during registry creation.*
