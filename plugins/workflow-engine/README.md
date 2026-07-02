# workflow-engine plugin

Version: `0.1.0`

A DAG workflow engine for [hermes-agent](https://github.com/Interstellar-code/hermes-agent), ported from the Switch UI TypeScript implementation. It runs YAML-defined multi-node workflows with conditional branching, parallel execution, bash nodes, approval gates, cron polling, and Kanban task dispatch.

## What it is

The workflow-engine plugin exposes a REST API plus Hermes agent tools for defining, triggering, monitoring, approving, and cancelling DAG-based workflows. Each workflow is a YAML definition describing nodes (steps), dependencies, providers (`claude`, `codex`, `hermes-kanban`), and conditional edges. The engine stores state in SQLite, emits SSE events for live progress, and integrates with the Hermes Kanban dispatcher for agent task routing.

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
  - Default: `~/.hermes/switchui-workflows.db`
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
hermes workflow daemon --interval 60 --pidfile /tmp/hermes-workflow.pid
```

There are currently **no** `--defaults-dir` or `--yaml-dir` daemon flags.

### Paths

- DB location: `~/.hermes/switchui-workflows.db` (SQLite, auto-migrated on startup)
- Bundled defaults source: `plugins/workflow-engine/defaults/`
- User workflow store: `~/.hermes/switchui/workflows/`

## API endpoints

All plugin API routes are mounted by the Hermes dashboard server under:

```text
/api/plugins/workflow-engine
```

If the dashboard is running on the default port, the base URL is typically:

```text
http://localhost:8642/api/plugins/workflow-engine
```

### Route summary

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/health` | Health check. Returns `{"ok": true, "version": "0.1.0"}` |
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
| `POST` | `/runs/{run_id}/resume` | Resume a paused run |
| `GET` | `/runs/{run_id}/nodes` | List node-runs for a run |
| `POST` | `/runs/{run_id}/events` | Append run event (internal) |
| `GET` | `/runs/{run_id}/events` | List stored run events |
| `POST` | `/runs/{run_id}/phase-transitions` | Record phase transition (internal) |
| `GET` | `/runs/{run_id}/phase-transitions` | List phase transitions |
| `POST` | `/runs/{run_id}/approval-claim` | Claim approval gate |
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
hermes workflow daemon --interval 60 --pidfile /tmp/hermes-workflow.pid
```

The daemon runs three long-lived tasks:

- `CronPoller`
- `KanbanDispatcher`
- `run_scheduler_tick_loop`

Lifecycle notes:

- the daemon owns its own `asyncio.run()` loop
- `SIGINT` / `SIGTERM` trigger clean shutdown
- `--pidfile` writes a PID file on start and removes it on clean exit
- the daemon does **not** auto-restart itself; use systemd / launchd / another supervisor in production

## Cron integration

The plugin ships a built-in cron poller (`engine/cron/poller.py`). Workflows with a `cron:` field are triggered automatically by the daemon process.

Example:

```yaml
name: my-workflow
cron: "0 * * * *"
nodes: ...
```

## Kanban integration

Nodes with `provider: hermes-kanban` (and the related routed providers `claude` / `codex` used by the workflow engine) are dispatched through the daemon's `KanbanDispatcher`.

High-level lifecycle:

1. a workflow node resolves to a Kanban-dispatched provider
2. the workflow engine writes a Kanban task to the Hermes Kanban DB
3. the daemon's `KanbanDispatcher` promotes / dispatches it
4. a Kanban worker picks up the task and executes it
5. completion/failure state is reported back into the workflow run state and events stream

This means Kanban dispatch is not a fire-and-forget side path — it is part of the workflow run lifecycle tracked by the engine.

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
