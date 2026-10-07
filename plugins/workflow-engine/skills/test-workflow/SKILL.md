---
name: test-workflow
description: End-to-end procedure for running and verifying a workflow-engine DAG — preconditions, trigger, monitor nodes, handle approval gates, cancel, resume/retry. Use when asked to test, trigger, run, or smoke-check a workflow definition through the workflow-engine plugin.
metadata:
  hermes:
    tags: [workflow-engine, dag, testing]
---

# workflow-engine: test-workflow

How to drive a workflow definition end-to-end and verify each step. The tool
schemas (`workflow_list`, `workflow_run`, `workflow_status`, `workflow_approve`,
`workflow_cancel`) are already in context — this fills the procedural gap:
order of operations, preconditions, pitfalls, and what success looks like.

> **Authoring Workflows**: To create, edit, validate, or save workflow definitions,
> refer to the `workflow-authoring` skill (`workflow-engine:workflow-authoring`
> at `plugins/workflow-engine/skills/workflow-authoring/SKILL.md`).

## Key facts (do not get these wrong)

- **API base path is `/api/plugins/workflow-engine/`**, NOT `/api/workflows/`.
  The Hermes dashboard mounts plugin routers under `/api/plugins/<name>/`.
- The dashboard gateway and the **background daemon are separate processes**.
  Cron-triggered and scheduled runs only fire when the daemon
  (`hermes workflow daemon`) is running. Manually-triggered runs via
  `workflow_run` advance through the engine regardless.
- **Trigger Kind (`agent`)**: Runs started with the `workflow_run` agent tool
  carry `metadata.trigger.kind = "agent"` (`type: "agent"`, `source: "workflow_run_tool"`;
  `tools/run_workflow.py:183`). This distinguishes agent runs from manual dashboard
  runs (`kind: "manual"`) and cron poller runs (`kind: "cron"`).
- DB lives at `$HERMES_HOME/switchui-workflows.db` (profile-scoped); the 27 bundled
  workflows are seeded into it lazily on first engine use.
- There is **no Workflows tab** in the Hermes dashboard sidebar
  (`tab.hidden: true`). The UI lives in the separate Switch UI app.
- **Agent Tools vs HTTP Endpoints**: The agent has 5 model tools (`workflow_list`,
  `workflow_run`, `workflow_status`, `workflow_approve`, `workflow_cancel`). Run retries
  and definition authoring are accessed via the dashboard HTTP API.

## Preconditions

1. Plugin enabled: `hermes plugins enable workflow-engine` and gateway restarted.
2. Confirm the API is live (dashboard port, default 9119; needs the dashboard auth token — 401 without it, and 404 on the gateway's 8642):
   ```bash
   curl -s http://localhost:9119/api/plugins/workflow-engine/health
   # → {"ok": true, "version": "0.4.0", "features": [...], ...}
   ```
   A 404 here almost always means you used the wrong base path.
3. **Validate before running**: If you authored or modified a workflow definition,
   validate it first with `POST /definitions/validate` (`dashboard/plugin_api.py:249`)
   to verify syntax, dependencies, and inputs before execution.
4. If the workflow is triggered by a Hermes cron job (`payload.switchui_workflow_id`),
   confirm the daemon is running (systemd/launchd or `hermes workflow daemon --interval 60`).
   Runs started with `workflow_run` do not need it.

## Procedure

1. **List definitions** — `workflow_list`. Confirm the target definition id
   exists. If absent, the defaults may not have been copied (re-enable) or it
   was never created.
2. **Inspect the DAG** (optional) — `GET /definitions/{def_id}/parsed` to see
   nodes, edges, and which providers/approval gates it contains. Know in advance
   whether the run will pause for approval.
3. **Trigger the run** — `workflow_run` with the definition id and any required
   inputs/working path. Capture the returned `run_id`. Stamped with `trigger.kind = "agent"`.
   Note: `run_rate_per_session` defaults to 5 — repeated test runs in one session can hit the rate gate.
4. **Monitor** — poll `workflow_status` (or `GET /runs/{run_id}` +
   `GET /runs/{run_id}/nodes`) until terminal. Watch node states transition
   `pending → running → completed/failed`. For live progress use the SSE stream
   at `GET /api/plugins/workflow-engine/events`.
5. **Approval gates** — if a node enters a paused/awaiting-approval state, call
   `workflow_approve` (run id + approve/reject). `approve_any` defaults to false,
   so only the session that started the run (the recorded owner) can approve or cancel it unless config says otherwise.
6. **Cancel** — to abort, `workflow_cancel` with the run id. Verify the run
   moves to a cancelled terminal state via `workflow_status`.
7. **Resume / Retry from a node (failed or crashed runs)**:
   If a run failed, was cancelled, or crashed, re-execute it in place on its pinned definition snapshot via:
   ```bash
   curl -s -X POST http://localhost:9119/api/plugins/workflow-engine/runs/{run_id}/retry \
     -H "Content-Type: application/json" \
     -d '{"from_node_id": "<optional_node_id>", "actor": "agent"}'
   ```
   - Added in B4 (`0934c875f1`, `dashboard/plugin_api.py:884`).
   - If `from_node_id` is provided, that node and its downstream descendants reset to `pending` and re-run; completed upstream nodes skip as `prior_success`.
   - If `from_node_id` is omitted, all failed/cancelled nodes re-run.
   - Approval gates in the re-run set will pause and request approval again.
   - Returns 409 if run is currently running/paused, completed, or still owned by a live process.

## Success criteria

- Health endpoint returned `{"ok": true}` at the correct base path.
- `workflow_run` returned a `run_id` with `trigger.kind = "agent"`.
- `workflow_status` reached a terminal state (`completed` / `failed` /
  `cancelled`) — not stuck in `running`.
- Each node's final state matches expectation; approval gates resolved as intended.
- Retries/resumes correctly re-execute target nodes without duplicating upstream successful nodes.

## Common pitfalls

- **404 on every call** → wrong base path (`/api/workflows/` vs
  `/api/plugins/workflow-engine/`).
- **Cron-triggered run never starts** → daemon process not running.
- **"No Workflows tab in dashboard"** → expected; the tab is hidden by design.
- **Rate-limit rejection** → `run_rate_per_session` (default 5) exceeded;
  start a new session or raise the limit in config.
- **Retry returns 409** → Run is still active, completed, or owner process heartbeat is still fresh (< 300s).
