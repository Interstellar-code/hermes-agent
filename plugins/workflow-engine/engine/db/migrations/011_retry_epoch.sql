-- Conductor v2: retry ownership epoch. reopen_run bumps it; an owner that
-- started at an older epoch (woke up after its run was retried elsewhere)
-- stops at its next status check and never finalises the new attempt.
ALTER TABLE workflow_runs ADD COLUMN retry_epoch INTEGER NOT NULL DEFAULT 0;
