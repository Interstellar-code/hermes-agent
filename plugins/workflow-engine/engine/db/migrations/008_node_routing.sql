-- Routed prompt nodes (hermes_task.profile): the gateway session + run they
-- ran as. assigned_agent holds the profile. NULL = ran locally.
ALTER TABLE node_runs ADD COLUMN session_id TEXT;
ALTER TABLE node_runs ADD COLUMN gateway_run_id TEXT;
