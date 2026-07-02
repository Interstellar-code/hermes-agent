# Review Lens: API

Primary focus for this review: **API contracts**. Weight your findings toward
the concerns below, but never ignore an unrelated BLOCKER (including a security
one) you happen to see.

## What to scrutinize

- **Backward compatibility** — removed/renamed endpoints, fields, parameters, or
  enum values; changed defaults; narrowed accepted inputs; behavior callers
  already depend on that silently changes.
- **Schema drift** — request/response shapes that diverge from the documented or
  generated schema, nullable/required changes, type changes, field reordering
  where order is significant.
- **Error semantics** — changed status codes, error shapes, or error codes;
  failures that now surface differently; success masking a partial failure;
  inconsistent error contracts across endpoints.
- **Versioning** — breaking changes shipped without a version bump or
  deprecation path; missing/incorrect version negotiation; mixing v1 and v2
  semantics on one route.
- **Breaking changes** — anything that forces existing clients to change to keep
  working, including stricter validation, tightened rate limits, or new required
  fields/headers.
- **Contract hygiene** — undocumented behavior, mismatch between code and
  spec/docs, idempotency and pagination contracts, content-type and encoding
  assumptions.

## Severity guidance (apply the shared rubric)

- **BLOCKER** — an undeclared breaking change on a public/consumed contract that
  will break live clients with no migration path.
- **HIGH** — a real compatibility break or schema drift that callers will hit,
  even if a workaround exists.
- **MED** — error-semantics or versioning inconsistency that bites under
  foreseeable conditions, or a contract documented incorrectly.
- **LOW / NIT** — naming, doc mismatches, and cosmetic contract inconsistencies
  with limited blast radius.

When unsure between two levels, pick the higher and note the uncertainty in the
Finding's evidence. Prefer fewer, well-evidenced findings over speculation.
