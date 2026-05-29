---
name: test-workflow
description: End-to-end procedure for running and verifying a workflow-engine DAG — preconditions, trigger, monitor nodes, handle approval gates, cancel. Use when asked to test, trigger, run, or smoke-check a workflow definition through the workflow-engine plugin.
metadata:
  hermes:
    tags: [workflow-engine, dag, testing]
---

# workflow-engine: test-workflow

How to drive a workflow definition end-to-end and verify each step. The tool
schemas (`workflow_list`, `workflow_run`, `workflow_status`, `workflow_approve`,
`workflow_cancel`) are already in context — this fills the procedural gap:
order of operations, preconditions, pitfalls, and what success looks like.

## Key facts (do not get these wrong)

- **API base path is `/api/plugins/workflow-engine/`**, NOT `/api/workflows/`.
  The Hermes dashboard mounts plugin routers under `/api/plugins/<name>/`.
- The dashboard gateway and the **background daemon are separate processes**.
  Cron-triggered and Kanban-dispatched workflows only advance when the daemon
  (`hermes workflow daemon`) is running. Manually-triggered runs via
  `workflow_run` advance through the engine regardless.
- DB lives at `~/.hermes/switchui-workflows.db`; bundled defaults are copied to
  `~/.hermes/switchui/workflows/` on first enable (27 bundled workflows).
- There is **no Workflows tab** in the Hermes dashboard sidebar
  (`tab.hidden: true`). The UI lives in the separate Switch UI app.

## Preconditions

1. Plugin enabled: `hermes plugins enable workflow-engine` and gateway restarted.
2. Confirm the API is live (substitute the dashboard port, default 8642):
   ```bash
   curl -s http://localhost:8642/api/plugins/workflow-engine/health
   # → {"ok": true, "version": "0.1.0"}
   ```
   A 404 here almost always means you used the wrong base path.
3. If the workflow uses `cron:` or `provider: hermes-kanban` / `claude` /
   `codex` nodes, confirm the daemon is running (systemd/launchd or
   `hermes workflow daemon --interval 60`). Pure prompt/bash DAGs do not need it.

## Procedure

1. **List definitions** — `workflow_list`. Confirm the target definition id
   exists. If absent, the defaults may not have been copied (re-enable) or it
   was never created.
2. **Inspect the DAG** (optional) — `GET /definitions/{def_id}/parsed` to see
   nodes, edges, and which providers/approval gates it contains. Know in advance
   whether the run will pause for approval.
3. **Trigger the run** — `workflow_run` with the definition id and any required
   inputs/working path. Capture the returned `run_id`. Note: `run_rate_per_session`
   defaults to 5 — repeated test runs in one session can hit the rate gate.
4. **Monitor** — poll `workflow_status` (or `GET /runs/{run_id}` +
   `GET /runs/{run_id}/nodes`) until terminal. Watch node states transition
   `pending → running → succeeded/failed`. For live progress use the SSE stream
   at `GET /api/plugins/workflow-engine/events`.
5. **Approval gates** — if a node enters a paused/awaiting-approval state, call
   `workflow_approve` (run id + approve/reject). `approve_any` defaults to false,
   so only the owning session can approve unless config says otherwise.
6. **Cancel** — to abort, `workflow_cancel` with the run id. Verify the run
   moves to a cancelled terminal state via `workflow_status`.

## Success criteria

- Health endpoint returned `{"ok": true}` at the correct base path.
- `workflow_run` returned a `run_id`.
- `workflow_status` reached a terminal state (`succeeded` / `failed` /
  `cancelled`) — not stuck in `running` (a stall on a kanban/cron node usually
  means the daemon is not running).
- Each node's final state matches expectation; approval gates resolved as intended.

## Common pitfalls

- **404 on every call** → wrong base path (`/api/workflows/` vs
  `/api/plugins/workflow-engine/`).
- **Run never advances past a kanban/cron node** → daemon process not running.
- **"No Workflows tab in dashboard"** → expected; the tab is hidden by design.
- **Rate-limit rejection** → `run_rate_per_session` (default 5) exceeded;
  start a new session or raise the limit in config.
