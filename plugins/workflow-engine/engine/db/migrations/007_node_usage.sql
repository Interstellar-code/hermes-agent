-- Per-node token usage + cost. Additive: retries and loop iterations add to
-- the same row. NULL = no usage recorded (pre-007 runs, non-LLM nodes).
ALTER TABLE node_runs ADD COLUMN input_tokens INTEGER;
ALTER TABLE node_runs ADD COLUMN output_tokens INTEGER;
ALTER TABLE node_runs ADD COLUMN total_tokens INTEGER;
ALTER TABLE node_runs ADD COLUMN cost_usd REAL;
ALTER TABLE node_runs ADD COLUMN model TEXT;
ALTER TABLE node_runs ADD COLUMN provider TEXT;
