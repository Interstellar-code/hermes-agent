# Domain Pack: Backend API

This pack ADDS stack-specific context for backend / HTTP API work. It does NOT
override the active role's contract, output format, or severity rubric. Apply
it alongside your role persona — treat it as an extra checklist lens.

## Stack context this pack adds

- **HTTP / API design** — correct status codes (201 vs 200, 422 vs 400 vs 500),
  idempotency of PUT/DELETE, versioning strategy (URI vs header vs content-type),
  consistent field naming (camelCase vs snake_case), stable resource paths.
- **Request / response contracts** — schema validation on input (not just
  type coercion), explicit nullable vs optional fields, consistent error
  response envelope, no internal model types leaking into the public shape.
- **Authentication & authorization** — auth on every non-public endpoint,
  authz checks at the handler level (not just middleware), object-level
  ownership checks (IDOR), token expiry and rotation.
- **Validation & error semantics** — validate before persisting; return
  actionable errors (which field, why); distinguish 4xx (caller's fault) from
  5xx (server's fault); never swallow exceptions silently.
- **Persistence boundaries** — no raw SQL from untrusted input; ORM queries
  scoped to the authenticated principal; transactions wrapping multi-step
  mutations; rollback on partial failure.
- **Performance basics** — pagination on list endpoints, select only needed
  columns, avoid N+1 in serializers/nested loaders, connection pool sizing.

## Common pitfalls to flag

- Missing input validation (trusting caller-supplied types/ranges).
- Authorization logic only in middleware, missing at the resource level.
- Returning 200 on a failed mutation.
- Exposing stack traces or internal error messages to callers.
- Unbounded list endpoints (no pagination, no limit).
- Mutations that are not idempotent but should be.
- Secrets or credentials hard-coded in configuration files.
- Missing rate limiting on unauthenticated or expensive endpoints.
