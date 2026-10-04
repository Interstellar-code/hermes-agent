-- Cron poller cursor: last cron tick already fired per Hermes cron job.
-- Previously absent from every migration, so the poller crashed on a real DB.
CREATE TABLE IF NOT EXISTS workflow_cron_jobs (
  cron_job_id   TEXT PRIMARY KEY,
  last_fired_at TEXT
);
