# Domain Pack: Data / Database

This pack ADDS stack-specific context for schema, query, and data-layer work.
It does NOT override the active role's contract, output format, or severity
rubric. Apply it alongside your role persona — treat it as an extra checklist
lens.

## Stack context this pack adds

- **Schema & migrations** — migrations are reversible (down path present),
  column types match usage (e.g. TEXT for unbounded strings, not VARCHAR(255)
  when the limit is arbitrary), NOT NULL constraints where the domain demands
  them, sensible defaults, no orphaned columns from rolled-back features.
- **Queries** — parameterized queries only (no string-concatenated SQL), select
  only the columns needed, avoid `SELECT *` in production paths, use `LIMIT`
  on unbounded scans.
- **Indexing** — foreign keys have indexes, columns used in WHERE/ORDER/GROUP
  have covering indexes where the table is large, no redundant indexes.
- **Transactions** — multi-step mutations wrapped in a transaction, explicit
  isolation level when ordering matters, avoid long-held locks, handle
  deadlock retry at the application layer.
- **Data integrity** — enforce invariants in the DB (FK constraints, CHECK
  constraints, unique indexes) rather than only in application code; cascade
  rules are intentional (CASCADE DELETE vs RESTRICT).
- **N+1 queries** — loaders eagerly fetch related records in one query or use
  a batch loader; avoid per-row sub-queries inside loops.

## Common pitfalls to flag

- String-interpolated SQL (injection risk).
- Migration with no rollback path applied to a large table without a lock
  strategy (e.g. adding a NOT NULL column without a default).
- Missing index on a frequently filtered or joined column.
- Loading an entire table into memory to filter in Python/JS.
- Storing JSON blobs for data that warrants its own relational columns.
- Sensitive PII stored unencrypted or logged in query traces.
- Using the ORM's lazy-load feature in a serializer hot path (N+1).
