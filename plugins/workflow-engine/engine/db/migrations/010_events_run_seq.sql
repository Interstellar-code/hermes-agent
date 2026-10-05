-- Conductor v2: run-scoped event paging / SSE DB tail
-- (WHERE workflow_run_id = ? AND rowid > ? ORDER BY rowid). The index's
-- implicit rowid suffix serves both the range and the order: no temp b-tree.
CREATE INDEX IF NOT EXISTS idx_we_run_seq ON workflow_events(workflow_run_id);
