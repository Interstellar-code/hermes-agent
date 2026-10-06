-- Definition version history: snapshots are now also written on every
-- definition save, not only at run start. Additive; both columns nullable.
--   saved_at — when this content last became the definition through a save
--              path (run snapshots: when first captured). Pre-012 rows get
--              created_at.
--   source   — what wrote the row: save | import | reset | seed | run.
--              Pre-012 rows were all written by run start, so 'run'.
-- The backfill UPDATEs only touch NULLs, so re-running them is a no-op.
ALTER TABLE workflow_definition_snapshots ADD COLUMN saved_at INTEGER;
ALTER TABLE workflow_definition_snapshots ADD COLUMN source TEXT;
UPDATE workflow_definition_snapshots SET saved_at = created_at WHERE saved_at IS NULL;
UPDATE workflow_definition_snapshots SET source = 'run' WHERE source IS NULL;
