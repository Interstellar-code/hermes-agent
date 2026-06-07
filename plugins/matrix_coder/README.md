# Matrix Coder

A specialist-coder layer for Hermes. Matrix Coder turns the active Hermes agent
into a focused **specialist** for a coding task by injecting a composed persona
as ephemeral turn context.

Phases 0–5 are implemented: explicit and implicit invocation, eight roles,
review lenses, Kanban audit mirroring, workflow personas, and domain packs.
Tracks epic issue **#76**.

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

### The 8 roles

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

Matrix Coder supports two conversational paths:

- **Implicit (default):** a cheap deterministic IntentGate recognizes plain
  coding requests and infers a role, optional review lens, and optional domain
  pack. Matrix-worthy work silently receives the specialist persona.
- **Explicit override:** start with `matrix`; explicit role/lens/domain parsing
  always wins over inference.

Examples:

```
is this auth safe?                              # review + security lens
why does the API endpoint crash?               # debug + backend-api domain
matrix review this for security
matrix explore how auth flows through the gateway
matrix executor @backend-api: add CSV export   # explicit always wins
```

The implicit intake gate is conservative. Clear, mechanical, low-risk work
(for example, `fix README typo`) injects a visible recommendation asking
whether Hermes should handle it directly; sensitive or nontrivial coding work
silently routes through Matrix Coder. Unrelated chat receives no injection.

The `/matrix` slash command displays status/help; conversational messages are
the dispatch path.

## Layout

```
matrix_coder/
  __init__.py          # plugin entrypoint: register(ctx), hooks, /matrix command
  plugin.yaml          # manifest
  pyproject.toml       # optional packaging metadata
  core/                # pure-Python building blocks (no Hermes runtime imports)
  personas/
    _base/             # shared specialist contracts
    _passthrough.md    # walking-skeleton smoke persona
  skills/ scripts/ tools/ templates/ examples/
  tests/
```
