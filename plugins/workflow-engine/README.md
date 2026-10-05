# workflow-engine plugin

Version: `0.3.0`

A DAG workflow engine for [hermes-agent](https://github.com/Interstellar-code/hermes-agent), ported from the Switch UI TypeScript implementation. It runs YAML-defined multi-node workflows with conditional branching, parallel execution, bash nodes, approval gates, and cron-triggered runs.

## What it is

The workflow-engine plugin exposes a REST API plus Hermes agent tools for defining, triggering, monitoring, approving, and cancelling DAG-based workflows. Each workflow is a YAML definition describing nodes (steps), dependencies, and conditional edges. The engine stores state in SQLite and emits SSE events for live progress.

## Install

The plugin ships **bundled** with this repository's hermes-agent build.

If you are using this repo, do **not** install a separate package or plugin repo — enable the bundled plugin instead.

## Enable

```bash
hermes plugins enable workflow-engine
hermes dashboard restart
```

Or set in your Hermes config:

```yaml
plugins:
  workflow-engine:
    enabled: true
```

## Config

### Environment variables currently read by the engine

- `WORKFLOW_DB_PATH`
  - Default: `$HERMES_HOME/switchui-workflows.db` (profile-scoped; `~/.hermes` only for the default profile)
  - Purpose: SQLite database path.
- `TOOL_CATALOG_ROOT`
  - Default: unset
  - Purpose: root path for the bundled `tool-catalog-write` workflow.

### Important note on unsupported env vars

The following env vars are **mentioned historically but are not currently read by the engine**:

- `WORKFLOW_DEFAULTS_DIR`
- `WORKFLOW_YAML_DIR`
- `WORKFLOW_POLL_INTERVAL`

Do **not** rely on them. Today the daemon CLI supports only:

```bash
hermes workflow daemon --interval 60 [--pidfile PATH]
```

There are currently **no** `--defaults-dir` or `--yaml-dir` daemon flags.

### Paths

- DB location: `$HERMES_HOME/switchui-workflows.db` (SQLite, auto-migrated on first engine use)
- Bundled defaults source: `plugins/workflow-engine/defaults/`
- Workflow definitions live in the DB (seeded from the bundled defaults); no on-disk user workflow directory is read.

## API endpoints

All plugin API routes are mounted by the Hermes dashboard server under:

```text
/api/plugins/workflow-engine
```

The routes live on the **dashboard** (default port 9119, not the gateway's 8642, which returns 404). The dashboard requires its session auth token (an unauthenticated request gets 401):

```text
http://localhost:9119/api/plugins/workflow-engine
```

### Route summary

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/health` | Health check. Returns `{"ok": true, "version": "0.3.0", "features": [...], ...}` |
| `GET` | `/definitions` | List workflow definitions |
| `POST` | `/definitions` | Create or upsert a workflow definition |
| `GET` | `/definitions/{def_id}` | Get one definition |
| `GET` | `/definitions/{def_id}/parsed` | Get parsed/validated DAG |
| `DELETE` | `/definitions/{def_id}` | Delete a mutable definition |
| `GET` | `/runs` | List workflow runs |
| `GET` | `/runs/active` | Get active run for a scope/path |
| `GET` | `/runs/by-conversation/{conv_id}` | Find run by conversation ID |
| `POST` | `/runs` | Trigger a run |
| `GET` | `/runs/{run_id}` | Get run details |
| `POST` | `/runs/{run_id}/approve` | Approve a paused approval node |
| `POST` | `/runs/{run_id}/cancel` | Cancel a run |
| `POST` | `/runs/{run_id}/resume` | Resume a paused run (409 if not resumable: not paused, or waiting on an approval/loop gate) |
| `GET` | `/runs/{run_id}/nodes` | List node-runs for a run |
| `POST` | `/runs/{run_id}/events` | Append run event (internal) |
| `GET` | `/runs/{run_id}/events` | List stored run events |
| `POST` | `/runs/{run_id}/phase-transitions` | Record phase transition (internal) |
| `GET` | `/runs/{run_id}/phase-transitions` | List phase transitions |
| `POST` | `/runs/{run_id}/approval-claim` | Deprecated (410) — use the `workflow_approve` tool |
| `GET` | `/node-runs/active` | List active node-runs |
| `GET` | `/node-runs/{node_run_id}` | Get one node-run |
| `GET` | `/events` | SSE stream for live workflow events |

## Switch UI integration

This plugin is used together with **two separate applications**:

1. **Hermes dashboard** — hosts the plugin API routes at `/api/plugins/workflow-engine/...`
2. **hermes-switchui** — separate frontend application with the workflows UI

That distinction matters:

- Enabling the plugin in Hermes gives you the backend API.
- The **Workflows → Backend** toggle lives in **Switch UI**, not in the Hermes dashboard.
- If you only enable the plugin and open the Hermes dashboard, you should **not** expect a new Workflows tab to appear there.

In [hermes-switchui](https://github.com/Interstellar-code/hermes-switchui), the `/workflows` settings panel exposes a backend toggle:

- **native** — uses the TypeScript workflow engine built into Switch UI
- **plugin** — proxies workflow API calls to this plugin through the Hermes dashboard/gateway

Toggle location in Switch UI:

```text
Settings → Workflows → Backend
```

The choice is persisted in `localStorage` and sent as `?backend=plugin` on workflow API calls.

## Architecture

The plugin uses two distinct Hermes extension surfaces that must not be confused.

### 1. Dashboard router (HTTP)

`dashboard/plugin_api.py` exports a FastAPI `APIRouter`.
Hermes mounts it automatically under:

```text
/api/plugins/workflow-engine
```

The router uses `_shared.get_engine()` so the HTTP layer and agent tools share the same engine singleton.

### 2. Agent tools (5 tools)

`__init__.py:register(ctx)` registers 5 tools via `ctx.register_tool`:

| Tool | Description |
|------|-------------|
| `workflow_list` | List workflow definitions |
| `workflow_run` | Start a run |
| `workflow_status` | Get run status and recent events |
| `workflow_approve` | Approve/reject a paused approval node |
| `workflow_cancel` | Cancel an active run |

Relevant config gates:

```yaml
workflow:
  allowed_roots: ["~", "${HERMES_HOME}"]
  run_rate_per_session: 5
  approve_any: false
```

### 3. Background daemon

The background daemon is a **separate process**, not part of the gateway request loop.

Start it with:

```bash
hermes workflow daemon --interval 60
```

Optional PID file support:

```bash
hermes workflow daemon --interval 60 [--pidfile PATH]
```

The daemon runs two long-lived tasks:

- `CronPoller`
- `run_scheduler_tick_loop`

Lifecycle notes:

- the daemon owns its own `asyncio.run()` loop
- `SIGINT` / `SIGTERM` trigger clean shutdown
- the lock file is held for the process lifetime and released on exit
- the daemon does **not** auto-restart itself; use systemd / launchd / another supervisor in production

## Cron integration

`engine/cron/poller.py` polls Hermes cron jobs (in-process `cron.jobs.list_jobs`, HTTP fallback). A Hermes cron job whose `payload.switchui_workflow_id` names a workflow starts a run (trigger `{"kind": "cron", "cron_job_id": ...}`) each time the job's `last_run_at` advances. Create the cron job through Hermes cron, not via a `cron:` field in the workflow YAML (that field is not read). The cursor is stored in `workflow_cron_jobs`; a job seen for the first time only seeds the cursor and does not fire.

## Runtime conventions

- Declared workflow inputs are exported as env vars (`"$name"` in bash, `os.environ["name"]` in scripts). Node outputs reach scripts via `NODE_<ID>_OUTPUT`; inline `$node.output` in script bodies is refused.
- Run logs/artifacts: `<db dir>/workflow-runs/<run_id>/`.
- Crash recovery uses a per-run heartbeat; runs stale for more than 5 minutes are failed.
- Config `workflow.retention_days` (default 30): terminal runs older than this are deleted at boot.

## Ownership

`workflow_run` records the calling session as the run's owner. `workflow_approve` / `workflow_cancel` require the caller's session to match the owner unless `workflow.approve_any=true`; a caller with no session is denied.

## Logs

Daemon logs go to `$HERMES_HOME/logs/workflow-daemon.log` and `workflow-daemon-error.log` (see the launchd/systemd units).

## Bundled default workflows

This plugin currently bundles **27** default workflow YAMLs.

See [`defaults/README.md`](defaults/README.md) for the current list. Do not rely on older counts in issue comments or reviews.

## Key invariant: `_shared.py` is the only sys.path mutator

```text
_shared.py          ← sys.path injection (once, idempotent, thread-safe)
  └─ get_engine()   ← singleton WorkflowEngine, shared across dashboard + tools

dashboard/plugin_api.py   ← imports from ._shared behavior, no extra path mutation
__init__.py               ← imports from ._shared, no extra path mutation
daemon.py                 ← imports from ._shared, no extra path mutation
tools/*.py                ← import engine access through shared bootstrap
```
