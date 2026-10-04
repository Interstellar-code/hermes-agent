# system-one-router

Advisory routing via the hosted Jev decision API. Jev suggests (kind + lane + confidence); the orchestrator still decides. Dormant unless enabled.

Tools: `system_one_route`, `system_one_decide`, `system_one_status`. A fresh-task route costs two paid calls (kind, then lane); don't call it on every message.

Config (`plugins.system-one-router.*` in the profile config.yaml; env overrides `SYSTEM_ONE_ROUTER_{ENABLED,BASE_URL,MODEL,MAX_MONTHLY_USD}`):
- `enabled` (default false), `base_url`, `model`
- `max_monthly_usd` (default 2.0): hard monthly spend cap, best-effort (concurrent calls can overshoot by one call each). Invalid, negative or non-finite values fail closed (cap always trips).

Data sent to a third party: the message (first 1200 chars), reply target, and up to 4 prior turns, after secret redaction (`agent.redact.redact_for_egress`). Redirects are not followed; host is allowlisted.

Decision log: `<HERMES_HOME>/system-one-router.db` (SQLite, per profile). Stores hashes, never message text; rows older than 90 days are pruned on first use per process. Cap-exceeded calls are not logged.
