-- Migration 005: Definition provenance tracking.
--
-- Adds three new columns to workflow_definitions:
--
--   user_modified     — 1 once a user edits a bundled row; 0 = factory-clean.
--   bundled_checksum  — sha256 of the factory YAML this row was seeded/reset from.
--                       Distinct from `checksum` (sha256 of the *live DB yaml*).
--                       NULL for pure user/project rows; set on INSERT/UPGRADE by seed.
--   bundled_version   — optional human-readable version string from the factory YAML.
--
-- NOTE: bundled_checksum is intentionally left NULL here for existing bundled rows.
-- The per-row reconciliation (compare stored yaml sha vs factory file sha) happens
-- in seed_bundled() on the first post-migration boot (CR-2 reconciliation path).
-- SQL cannot sha256 file contents, so we do NOT blanket-backfill bundled_checksum here.

ALTER TABLE workflow_definitions ADD COLUMN user_modified INTEGER NOT NULL DEFAULT 0;
ALTER TABLE workflow_definitions ADD COLUMN bundled_checksum TEXT;
ALTER TABLE workflow_definitions ADD COLUMN bundled_version TEXT;

-- schema_version is bumped to 5 programmatically by migrate.py after this
-- script runs (same as 002-004); no inline schema_meta write needed here.
