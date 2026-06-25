# Matrix Coder

A specialist-coder layer for Hermes. Matrix Coder turns the active Hermes agent
into a focused **specialist** for a coding task by injecting a composed persona
as **trusted (developer-tier) ephemeral turn context** — delivered alongside the
host system prompt, never appended to the user message, so it cannot be mistaken
for a user instruction or override the host agent's identity (hardened in #140).

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

- **Explicit:** start a message with `matrix`; explicit role/lens/domain parsing
  always wins and always activates the specialist.
- **Implicit (inferred):** a cheap deterministic IntentGate inspects plain
  coding requests. It **silently activates** a specialist ONLY on a **strong
  signal** — an explicit role word (`review ...`, `debug ...`) or a clear
  imperative on code (`refactor the auth module`, `add tests for the parser`).
  Weaker or ambiguous coding requests get a **visible recommendation** asking
  whether to invoke Matrix Coder or let Hermes handle it. Advisory or meta
  questions addressed to Hermes — `what should we refactor?`, `should I ...?`,
  `is X up to date?`, `where is X?` — receive **no injection**; Hermes answers
  them directly as the orchestrator. (Hardened in #140: the old gate silently
  hijacked any coding-adjacent question that ended in `?`.)

Examples:

```
refactor the auth module                       # strong signal -> silent simplify
review API endpoint performance                # strong signal -> review:performance@backend-api
fix README typo                                # trivial -> visible "let Hermes handle?" recommendation
what parts of the frontend need refactoring?   # advisory to Hermes -> no injection
matrix review this for security                # explicit always activates
matrix executor @backend-api: add CSV export   # explicit role/lens/domain
matrix is this safe?                           # explicit, defaults to review
```

Persona text is delivered as **trusted developer-tier context** (alongside the
host system prompt), not appended to your message, and carries no user-visible
activation marker.

**Kill-switch:** set `MATRIX_CODER_IMPLICIT_ROUTING=0` to require the explicit
`matrix` trigger for every dispatch; the explicit path is unaffected.

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
