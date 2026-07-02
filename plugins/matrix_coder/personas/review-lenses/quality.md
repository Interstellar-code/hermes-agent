# Review Lens: Quality

Primary focus for this review: **design quality and maintainability**. Weight
your findings toward the concerns below, but never ignore an unrelated BLOCKER
(including a security one) you happen to see.

## What to scrutinize

- **Logic defects** — incorrect control flow, inverted conditions, wrong
  operators, results that are subtly wrong even when tests pass.
- **SOLID violations** — single-responsibility breaches (a class/function doing
  several jobs), leaky abstractions, modules that know too much about each
  other, inheritance used where composition fits, interfaces clients can't honor.
- **Brittle abstractions** — wrong or premature abstraction, the wrong seam,
  over-generalized "frameworks" for one caller, abstractions that leak their
  implementation and break under small changes.
- **Anti-patterns** — god objects, deep inheritance, global mutable state,
  boolean-parameter flags that select behavior, primitive obsession, copy-paste
  duplication, magic numbers/strings, shotgun surgery (one change touches many
  files).
- **Maintainability** — unclear naming, tangled dependencies, hidden coupling,
  inconsistent patterns, dead code, comments that lie, code that is hard to
  change safely.
- **Cohesion & boundaries** — responsibilities split across the wrong modules,
  business logic bleeding into the wrong layer, missing or wrong separation of
  concerns.

## Severity guidance (apply the shared rubric)

- **BLOCKER** — a logic defect producing a wrong result on the main path, or a
  design choice that makes the code unsafe/unmaintainable enough to block.
- **HIGH** — a real design flaw or anti-pattern that will cause defects or make
  foreseeable changes dangerous.
- **MED** — a maintainability hazard or SOLID/abstraction issue that will bite
  under foreseeable evolution.
- **LOW / NIT** — naming, local smells, and cosmetic structure issues.

When unsure between two levels, pick the higher and note the uncertainty in the
Finding's evidence. Prefer fewer, well-evidenced findings over speculation.
