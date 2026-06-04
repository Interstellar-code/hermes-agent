# Matrix Coder

A specialist-coder layer for Hermes. Matrix Coder turns a generic Hermes
subagent into a focused **specialist** for a single coding task.

> **Phase 0 — scaffold only.** This directory currently ships the skeleton:
> the plugin entrypoint/manifest, the shared `_base/` specialist contracts,
> one `_passthrough` smoke persona, the `core/` package, and a walking-skeleton
> `/matrix` command. The 8 role personas, per-role skills, scripts, and tools
> arrive in later phases. Tracks epic issue **#76**.

## The persona model

There is **no subagent persona API**. A specialist is a **persona** — plain
text — that is composed into a child subagent's `context` and re-asserted on
every turn via the `pre_llm_call` hook.

A composed persona = shared `_base/` contracts + the role persona
(+ optional review lens / domain pack):

- **`_base/specialist-contract.md`** — common identity, scope discipline,
  evidence-first rule, and the shared output contract.
- **`_base/severity-rubric.md`** — BLOCKER / HIGH / MED / LOW / NIT.
- **`_base/evidence-protocol.md`** — cite `file:line`, verify over assert,
  mark low-confidence items as Open Questions.
- **`_base/boundary-table.md`** — what specialists may/may not do, advisory
  per-role read vs write, the single-writer-per-file rule, escalate-don't-guess.

### The 8 roles (Phase 1 / 1.5 — not yet populated)

`explore`, `plan`, `executor`, `review` (lenses: security / code / api /
performance / quality / deps), `debug`, `test`, `verify`, `simplify`.

## Shared output contract

Every specialist returns exactly four sections, in order:

1. **Findings**
2. **Open Questions**
3. **Positive Observations**
4. **Recommendation**

See `core/reporting.py` for the renderer.

## Guardrail: single-writer-per-file

No file is edited by two agents at once. This is enforced at **orchestration
time** via disjoint file assignment / worktree isolation — *not* by a per-role
hook block. The per-role read/write nature in the boundary table is **advisory**
persona guidance. `core/hermes_bridge.py` holds the file-claim bookkeeping
(`claim_files` / `release_files` / `claimed_files` / `would_conflict`) that
future enforcement builds on.

## Invocation (conversational)

Matrix Coder is invoked conversationally. Examples (later phases):

```
matrix review this for security
matrix explore how auth flows through the gateway
matrix plan the refactor of the dispatch layer
```

The `/matrix <goal>` slash command is the explicit entrypoint. In Phase 0 it
runs the passthrough harness, which echoes the goal back in the output contract.

## Layout

```
matrix_coder/
  __init__.py          # plugin entrypoint: register(ctx), hooks, /matrix command
  plugin.yaml          # manifest
  pyproject.toml       # optional packaging metadata
  core/                # pure-Python building blocks (no Hermes runtime imports)
  personas/
    _base/             # shared specialist contracts
    _passthrough.md    # Phase 0 smoke persona
  skills/ scripts/ tools/ templates/ examples/   # later phases
  tests/               # walking-skeleton tests
```
