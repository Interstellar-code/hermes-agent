# Domain Pack: Infra / CLI

This pack ADDS stack-specific context for CLI tooling, packaging, deployment,
and infrastructure work. It does NOT override the active role's contract,
output format, or severity rubric. Apply it alongside your role persona —
treat it as an extra checklist lens.

## Stack context this pack adds

- **CLI ergonomics** — consistent flag naming (`--verbose` not `-V` for
  verbosity), `--help` always available, `--dry-run` for destructive
  operations, exit codes (0 = success, non-zero = failure), stderr for
  diagnostics / stdout for machine-readable output, no interactive prompts in
  non-TTY contexts without a `--yes`/`--force` flag.
- **Config & env** — configuration layered (defaults < env vars < config file <
  CLI flags), required secrets from env/vault never hard-coded, env var names
  documented, config validation at startup (fail fast, clear error).
- **Packaging** — pinned / locked dependencies in the artifact (not just
  ranges), deterministic builds, no credentials in the image or artifact,
  minimal runtime image (no dev tools in prod), reproducible builds.
- **Processes & signals** — SIGTERM handled gracefully (drain in-flight work,
  flush buffers), child processes reaped, no zombie processes, long-running
  scripts emit heartbeat logs.
- **Deployment** — rollback path documented and tested, blue/green or
  canary strategy for stateful changes, database migrations applied before
  new code starts (not after), health check / readiness probe present.
- **Observability** — structured logs (JSON or key=value), log levels
  respected (DEBUG off in prod by default), metrics emitted for key
  operations, trace/correlation IDs propagated, alerts on error-rate
  and latency baselines.

## Common pitfalls to flag

- Hard-coded credentials, tokens, or IPs in scripts or Dockerfiles.
- `set -e` missing in shell scripts (silent failures continue).
- Destructive operations without a dry-run flag or confirmation.
- `latest` tag for Docker images in production (non-reproducible).
- Missing cleanup on early exit (temp files, child processes, locks).
- Log statements that include secrets, PII, or raw request bodies.
- Missing readiness/liveness probes causing traffic to unhealthy instances.
