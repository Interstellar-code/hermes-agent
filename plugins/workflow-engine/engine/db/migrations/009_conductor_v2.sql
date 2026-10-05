-- Conductor v2: definition pinning + run lineage. Additive only; NULL on
-- pre-009 runs (= unpinned, no parent).
ALTER TABLE workflow_runs ADD COLUMN parent_run_id TEXT;
ALTER TABLE workflow_runs ADD COLUMN definition_checksum TEXT;
ALTER TABLE workflow_runs ADD COLUMN definition_version TEXT;
-- YAML a run started from, keyed by content hash: one row per distinct
-- revision, shared by every run of it. No FK so deleting a definition keeps
-- its runs' snapshots.
CREATE TABLE IF NOT EXISTS workflow_definition_snapshots (
  workflow_id TEXT NOT NULL,
  checksum    TEXT NOT NULL,
  version     TEXT,
  yaml        TEXT NOT NULL,
  created_at  INTEGER NOT NULL,
  PRIMARY KEY (workflow_id, checksum)
);
CREATE INDEX IF NOT EXISTS idx_wr_parent ON workflow_runs(parent_run_id);
CREATE INDEX IF NOT EXISTS idx_wr_started_id ON workflow_runs(started_at DESC, id);
CREATE INDEX IF NOT EXISTS idx_we_run_node ON workflow_events(workflow_run_id, node_run_id);
