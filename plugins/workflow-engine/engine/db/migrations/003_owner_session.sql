-- Phase 3a: Add owner_session to workflow_runs.
--
-- Tracks which Hermes session (session_key) started a run. Used by agent
-- tools (workflow_approve, workflow_cancel) to enforce ownership: only the
-- session that started a run may approve/cancel it unless workflow.approve_any
-- is true.
--
-- NULL default is safe: runs started before this migration (or by the
-- dashboard directly) will have owner_session = NULL. Tools treat NULL as
-- "unauthenticated owner" and refuse approve/cancel unless approve_any=true.

ALTER TABLE workflow_runs ADD COLUMN owner_session TEXT;
CREATE INDEX idx_wr_owner ON workflow_runs(owner_session);
