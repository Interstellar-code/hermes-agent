# Review Lens: Performance

Primary focus for this review: **performance**. Weight your findings toward the
concerns below, but never ignore an unrelated BLOCKER (including a security one)
you happen to see.

## What to scrutinize

- **Hot paths** — work on the critical path that runs often or per-request;
  expensive operations inside tight loops; synchronous work that blocks.
- **N+1 patterns** — per-row queries/requests inside a loop, missing batch /
  bulk / join, repeated lookups that should be hoisted or memoized.
- **Algorithmic cost** — accidental O(n^2) (nested scans, `in` over a list,
  repeated re-sorting), unbounded growth, work that scales with input where it
  need not.
- **Allocation & I/O waste** — needless copies/allocations in loops, re-opening
  files/connections, reading entire payloads to use a fraction, chatty I/O that
  should be batched, missing streaming for large data.
- **Caching** — missing caching of stable/expensive results, cache that is never
  invalidated (correctness risk) or invalidated too aggressively (no benefit),
  unbounded caches that leak memory.
- **Concurrency & resources** — serialized work that could parallelize safely,
  lock contention, connection-pool exhaustion, leaked resources under load.

## Severity guidance (apply the shared rubric)

- **BLOCKER** — a change that makes a hot path unusable at expected scale
  (timeouts, OOM, pathological complexity on the main path).
- **HIGH** — a clear regression or N+1 / superlinear cost that will be felt
  under normal load.
- **MED** — avoidable waste (extra allocations/I/O, a missing cache) that
  matters under foreseeable load but not yet critical.
- **LOW / NIT** — micro-inefficiencies with negligible real-world impact.

When unsure between two levels, pick the higher and note the uncertainty in the
Finding's evidence. Anchor cost claims to the code path (and, where possible, to
scale assumptions) rather than to intuition. Prefer fewer, well-evidenced
findings over speculation.
