# Review Lens: Code

Primary focus for this review: **general correctness and maintainability**.
Weight your findings toward the concerns below, but never ignore an unrelated
BLOCKER (including a security one) you happen to see.

## What to scrutinize

- **Logic defects** — off-by-one errors, inverted conditions, wrong operator,
  incorrect comparisons, broken control flow, results that are simply wrong on
  the main path.
- **Edge cases** — empty/`None`/zero/negative inputs, boundary values, empty
  collections, very large inputs, concurrency/ordering assumptions, time-zone
  and encoding edges.
- **Regressions** — behavior the change silently alters, removed handling,
  broken backward compatibility, contract changes that callers don't expect.
- **Error handling** — swallowed exceptions, bare `except`, errors that lose
  context, missing handling for failure modes, resource leaks (unclosed files /
  connections), missing cleanup on the error path.
- **Readability & maintainability** — unclear naming, dead code, duplication
  that should be reused, single-use abstractions that add no value, functions
  doing too much, comments that lie about the code.
- **Correctness of tests** — tests that don't actually assert the behavior,
  tests modified to pass rather than to verify, missing coverage for the change.

## Severity guidance (apply the shared rubric)

- **BLOCKER** — wrong result on the main path, crash, data loss, or a broken
  build/test with no safe workaround.
- **HIGH** — wrong behavior in a common case, a real regression, or a contract
  violation that callers will hit.
- **MED** — edge-case bug, missing handling, or a maintainability hazard that
  will bite under foreseeable conditions.
- **LOW / NIT** — localized smells, naming, and cosmetic issues.

When unsure between two levels, pick the higher and note the uncertainty in the
Finding's evidence. Prefer fewer, well-evidenced findings over speculation.
