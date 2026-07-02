-- Phase 1: scheduled runs + per-run priority / max_runtime / scheduled_for.
--
-- Adds three new columns on workflow_runs that are persisted on every run
-- (defaulting to safe no-op values for existing rows) plus a new
-- scheduled_runs table that holds deferred ("at" today, "cron" in phase 2)
-- launches until the scheduler tick fires them.
--
-- NULL default on priority is intentional via DEFAULT 0 so the row shape
-- stays compatible with existing run creation paths that don't pass the
-- new fields.

ALTER TABLE workflow_runs ADD COLUMN priority INTEGER NOT NULL DEFAULT 0;
ALTER TABLE workflow_runs ADD COLUMN max_runtime_s INTEGER;
ALTER TABLE workflow_runs ADD COLUMN scheduled_for TEXT;

CREATE TABLE scheduled_runs (
  id             TEXT PRIMARY KEY,
  workflow_id    TEXT NOT NULL,
  inputs_json    TEXT NOT NULL,
  trigger_json   TEXT NOT NULL,
  run_at         TEXT NOT NULL,
  priority       INTEGER NOT NULL DEFAULT 0,
  max_runtime_s  INTEGER,
  cron_expr      TEXT,
  status         TEXT NOT NULL DEFAULT 'pending',
  created_at     TEXT NOT NULL
);

CREATE INDEX idx_sr_due ON scheduled_runs(status, run_at);
