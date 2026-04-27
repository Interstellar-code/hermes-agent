---
sidebar_position: 12
title: "Kanban (Multi-Agent Board)"
description: "Durable SQLite-backed task board for coordinating multiple Hermes profiles"
---

# Kanban — Multi-Agent Profile Collaboration

Hermes Kanban is a durable task board, shared across all your Hermes profiles, that lets multiple named agents collaborate on work without fragile in-process subagent swarms. Every task is a row in `~/.hermes/kanban.db`; every handoff is a row anyone can read and write; every worker is a full OS process with its own identity.

This is the shape that covers the workloads `delegate_task` can't:

- **Research triage** — parallel researchers + analyst + writer, human-in-the-loop.
- **Scheduled ops** — recurring daily briefs that build a journal over weeks.
- **Digital twins** — persistent named assistants (`inbox-triage`, `ops-review`) that accumulate memory over time.
- **Engineering pipelines** — decompose → implement in parallel worktrees → review → iterate → PR.
- **Fleet work** — one specialist managing N subjects (50 social accounts, 12 monitored services).

For the full design rationale, comparative analysis against Cline Kanban / Paperclip / NanoClaw / Google Gemini Enterprise, and the eight canonical collaboration patterns, see `docs/hermes-kanban-v1-spec.pdf` in the repository.

## Kanban vs. `delegate_task`

They look similar; they are not the same primitive.

| | `delegate_task` | Kanban |
|---|---|---|
| Shape | RPC call (fork → join) | Durable message queue + state machine |
| Parent | Blocks until child returns | Fire-and-forget after `create` |
| Child identity | Anonymous subagent | Named profile with persistent memory |
| Resumability | None — failed = failed | Block → unblock → re-run; crash → reclaim |
| Human in the loop | Not supported | Comment / unblock at any point |
| Agents per task | One call = one subagent | N agents over task's life (retry, review, follow-up) |
| Audit trail | Lost on context compression | Durable rows in SQLite forever |
| Coordination | Hierarchical (caller → callee) | Peer — any profile reads/writes any task |

**One-sentence distinction:** `delegate_task` is a function call; Kanban is a work queue where every handoff is a row any profile (or human) can see and edit.

**Use `delegate_task` when** the parent agent needs a short reasoning answer before continuing, no humans involved, result goes back into the parent's context.

**Use Kanban when** work crosses agent boundaries, needs to survive restarts, might need human input, might be picked up by a different role, or needs to be discoverable after the fact.

They coexist: a kanban worker may call `delegate_task` internally during its run.

## Core concepts

- **Task** — a row with title, optional body, one assignee (a profile name), status (`triage | todo | ready | running | blocked | done | archived`), optional tenant namespace, optional idempotency key (dedup for retried automation).
- **Link** — `task_links` row recording a parent → child dependency. The dispatcher promotes `todo → ready` when all parents are `done`.
- **Comment** — the inter-agent protocol. Agents and humans append comments; when a worker is (re-)spawned it reads the full comment thread as part of its context.
- **Workspace** — the directory a worker operates in. Three kinds:
  - `scratch` (default) — fresh tmp dir under `~/.hermes/kanban/workspaces/<id>/`.
  - `dir:<path>` — an existing shared directory (Obsidian vault, mail ops dir, per-account folder).
  - `worktree` — a git worktree under `.worktrees/<id>/` for coding tasks.
- **Dispatcher** — a long-lived loop that, every N seconds (default 60): reclaims stale claims, reclaims crashed workers (PID gone but TTL not yet expired), promotes ready tasks, atomically claims, spawns assigned profiles. Runs as `hermes kanban daemon` (foreground) or as a systemd user service. After ~5 consecutive spawn failures on the same task the dispatcher auto-blocks it with the last error as the reason — prevents thrashing on tasks whose profile doesn't exist, workspace can't mount, etc.
- **Tenant** — optional string namespace. One specialist fleet can serve multiple businesses (`--tenant business-a`) with data isolation by workspace path and memory key prefix.

## Quick start

```bash
# 1. Create the board
hermes kanban init

# 2. Start the dispatcher (foreground; Ctrl-C to stop)
hermes kanban daemon &

# 3. Create a task
hermes kanban create "research AI funding landscape" --assignee researcher

# 4. Watch activity live
hermes kanban watch

# 5. See the board
hermes kanban list
hermes kanban stats
```

### Running the dispatcher as a service

For production, install the systemd user unit shipped at
`plugins/kanban/systemd/hermes-kanban-dispatcher.service`:

```bash
mkdir -p ~/.config/systemd/user
cp plugins/kanban/systemd/hermes-kanban-dispatcher.service \
   ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now hermes-kanban-dispatcher.service
systemctl --user status hermes-kanban-dispatcher
journalctl --user -u hermes-kanban-dispatcher -f   # follow logs
```

Without a running dispatcher `ready` tasks stay where they are — `hermes kanban init` will remind you of this on first run.

### Idempotent create (for automation / webhooks)

```bash
# First call creates the task. Any subsequent call with the same key
# returns the existing task id instead of duplicating.
hermes kanban create "nightly ops review" \
    --assignee ops \
    --idempotency-key "nightly-ops-$(date -u +%Y-%m-%d)" \
    --json
```

### Bulk CLI verbs

All the lifecycle verbs accept multiple ids so you can clean up a batch
in one command:

```bash
hermes kanban complete t_abc t_def t_hij --result "batch wrap"
hermes kanban archive  t_abc t_def t_hij
hermes kanban unblock  t_abc t_def
hermes kanban block    t_abc "need input" --ids t_def t_hij
```

## The worker skill

Any profile that should be able to work kanban tasks must load the `kanban-worker` skill. It teaches the worker the full lifecycle:

1. On spawn, read `$HERMES_KANBAN_TASK` env var.
2. Run `hermes kanban context $HERMES_KANBAN_TASK` to read title + body + parent results + full comment thread.
3. `cd $HERMES_KANBAN_WORKSPACE` and do the work there.
4. Complete with `hermes kanban complete <id> --result "<summary>"`, or block with `hermes kanban block <id> "<reason>"` if stuck.

Load it with:

```bash
hermes skills install devops/kanban-worker
```

## The orchestrator skill

A **well-behaved orchestrator does not do the work itself.** It decomposes the user's goal into tasks, links them, assigns each to a specialist, and steps back. The `kanban-orchestrator` skill encodes this: anti-temptation rules, a standard specialist roster (`researcher`, `writer`, `analyst`, `backend-eng`, `reviewer`, `ops`), and a decomposition playbook.

Load it into your orchestrator profile:

```bash
hermes skills install devops/kanban-orchestrator
```

For best results, pair it with a profile whose toolsets are restricted to board operations (`kanban`, `gateway`, `memory`) so the orchestrator literally cannot execute implementation tasks even if it tries.

## Dashboard (GUI)

The `/kanban` CLI and slash command are enough to run the board headlessly, but a visual board is often the right interface for humans-in-the-loop: triage, cross-profile supervision, reading comment threads, and dragging cards between columns. Hermes ships this as a **bundled dashboard plugin** at `plugins/kanban/` — not a core feature, not a separate service — following the model laid out in [Extending the Dashboard](./extending-the-dashboard).

Open it with:

```bash
hermes kanban init      # one-time: create kanban.db if not already present
hermes dashboard        # "Kanban" tab appears in the nav, after "Skills"
```

### What the plugin gives you

- A **Kanban** tab showing one column per status: `triage`, `todo`, `ready`, `running`, `blocked`, `done` (plus `archived` when the toggle is on).
  - `triage` is the parking column for rough ideas a specifier is expected to flesh out. Tasks created with `hermes kanban create --triage` (or via the Triage column's inline create) land here and the dispatcher leaves them alone until a human or specifier promotes them to `todo` / `ready`.
- Cards show the task id, title, priority badge, tenant tag, assigned profile, comment/link counts, a **progress pill** (`N/M` children done when the task has dependents), and "created N ago". A per-card checkbox enables multi-select.
- **Per-profile lanes inside Running** — toolbar checkbox toggles sub-grouping of the Running column by assignee.
- **Live updates via WebSocket** — the plugin tails the append-only `task_events` table on a short poll interval; the board reflects changes the instant any profile (CLI, gateway, or another dashboard tab) acts. Reloads are debounced so a burst of events triggers a single refetch.
- **Drag-drop** cards between columns to change status. The drop sends `PATCH /api/plugins/kanban/tasks/:id` which routes through the same `kanban_db` code the CLI uses — the three surfaces can never drift. Moves into destructive statuses (`done`, `archived`, `blocked`) prompt for confirmation. Touch devices use a pointer-based fallback so the board is usable from a tablet.
- **Inline create** — click `+` on any column header to type a title, assignee, priority, and (optionally) a parent task from a dropdown over every existing task. Creating from the Triage column automatically parks the new task in triage.
- **Multi-select with bulk actions** — shift/ctrl-click a card or tick its checkbox to add it to the selection. A bulk action bar appears at the top with batch status transitions, archive, and reassign (by profile dropdown, or "(unassign)"). Destructive batches confirm first. Per-id partial failures are reported without aborting the rest.
- **Click a card** (without shift/ctrl) to open a side drawer (Escape or click-outside closes) with:
  - **Editable title** — click the heading to rename.
  - **Editable assignee / priority** — click the meta row to rewrite.
  - **Editable description** — markdown-rendered by default (headings, bold, italic, inline code, fenced code, `http(s)` / `mailto:` links, bullet lists), with an "edit" button that swaps in a textarea. Markdown rendering is a tiny, XSS-safe renderer — every substitution runs on HTML-escaped input, only `http(s)` / `mailto:` links pass through, and `target="_blank"` + `rel="noopener noreferrer"` are always set.
  - **Dependency editor** — chip list of parents and children, each with an `×` to unlink, plus dropdowns over every other task to add a new parent or child. Cycle attempts are rejected server-side with a clear message.
  - **Status action row** (→ triage / → ready / → running / block / unblock / complete / archive) with confirm prompts for destructive transitions.
  - Result section (also markdown-rendered), comment thread with Enter-to-submit, the last 20 events.
- **Toolbar filters** — free-text search, tenant dropdown (defaults to `dashboard.kanban.default_tenant` from `config.yaml`), assignee dropdown, "show archived" toggle, "lanes by profile" toggle, and a **Nudge dispatcher** button so you don't have to wait for the next 60 s tick.

Visually the target is the familiar Linear / Fusion layout: dark theme, column headers with counts, coloured status dots, pill chips for priority and tenant. The plugin reads only theme CSS vars (`--color-*`, `--radius`, `--font-mono`, ...), so it reskins automatically with whichever dashboard theme is active.

### Architecture

The GUI is strictly a **read-through-the-DB + write-through-kanban_db** layer with no domain logic of its own:

```
┌────────────────────────┐      WebSocket (tails task_events)
│   React SPA (plugin)   │ ◀──────────────────────────────────┐
│   HTML5 drag-and-drop  │                                    │
└──────────┬─────────────┘                                    │
           │ REST over fetchJSON                              │
           ▼                                                  │
┌────────────────────────┐     writes call kanban_db.*        │
│  FastAPI router        │     directly — same code path      │
│  plugins/kanban/       │     the CLI /kanban verbs use      │
│  dashboard/plugin_api.py                                    │
└──────────┬─────────────┘                                    │
           │                                                  │
           ▼                                                  │
┌────────────────────────┐                                    │
│  ~/.hermes/kanban.db   │ ───── append task_events ──────────┘
│  (WAL, shared)         │
└────────────────────────┘
```

### REST surface

All routes are mounted under `/api/plugins/kanban/` and protected by the dashboard's ephemeral session token:

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/board?tenant=<name>&include_archived=…` | Full board grouped by status column, plus tenants + assignees for filter dropdowns |
| `GET` | `/tasks/:id` | Task + comments + events + links |
| `POST` | `/tasks` | Create (wraps `kanban_db.create_task`, accepts `triage: bool` and `parents: [id, …]`) |
| `PATCH` | `/tasks/:id` | Status / assignee / priority / title / body / result |
| `POST` | `/tasks/bulk` | Apply the same patch (status / archive / assignee / priority) to every id in `ids`. Per-id failures reported without aborting siblings |
| `POST` | `/tasks/:id/comments` | Append a comment |
| `POST` | `/links` | Add a dependency (`parent_id` → `child_id`) |
| `DELETE` | `/links?parent_id=…&child_id=…` | Remove a dependency |
| `POST` | `/dispatch?max=…&dry_run=…` | Nudge the dispatcher — skip the 60 s wait |
| `GET` | `/config` | Read `dashboard.kanban` preferences from `config.yaml` — `default_tenant`, `lane_by_profile`, `include_archived_by_default`, `render_markdown` |
| `WS` | `/events?since=<event_id>` | Live stream of `task_events` rows |

Every handler is a thin wrapper — the plugin is ~700 lines of Python (router + WebSocket tail + bulk batcher + config reader) and adds no new business logic. A tiny `_conn()` helper auto-initializes `kanban.db` on every read and write, so a fresh install works whether the user opened the dashboard first, hit the REST API directly, or ran `hermes kanban init`.

### Dashboard config

Any of these keys under `dashboard.kanban` in `~/.hermes/config.yaml` changes the tab's defaults — the plugin reads them at load time via `GET /config`:

```yaml
dashboard:
  kanban:
    default_tenant: acme              # preselects the tenant filter
    lane_by_profile: true             # default for the "lanes by profile" toggle
    include_archived_by_default: false
    render_markdown: true             # set false for plain <pre> rendering
```

Each key is optional and falls back to the shown default.

### Security model

The dashboard's HTTP auth middleware [explicitly skips `/api/plugins/`](./extending-the-dashboard#backend-api-routes) — plugin routes are unauthenticated by design because the dashboard binds to localhost by default. That means the kanban REST surface is reachable from any process on the host.

The WebSocket takes one additional step: it requires the dashboard's ephemeral session token as a `?token=…` query parameter (browsers can't set `Authorization` on an upgrade request), matching the pattern used by the in-browser PTY bridge.

If you run `hermes dashboard --host 0.0.0.0`, every plugin route — kanban included — becomes reachable from the network. **Don't do that on a shared host.** The board contains task bodies, comments, and workspace paths; an attacker reaching these routes gets read access to your entire collaboration surface and can also create / reassign / archive tasks.

Tasks in `~/.hermes/kanban.db` are profile-agnostic on purpose (that's the coordination primitive). If you open the dashboard with `hermes -p <profile> dashboard`, the board still shows tasks created by any other profile on the host. Same user owns all profiles, but this is worth knowing if multiple personas coexist.

### Live updates

`task_events` is an append-only SQLite table with a monotonic `id`. The WebSocket endpoint holds each client's last-seen event id and pushes new rows as they land. When a burst of events arrives, the frontend reloads the (very cheap) board endpoint — simpler and more correct than trying to patch local state from every event kind. WAL mode means the read loop never blocks the dispatcher's `BEGIN IMMEDIATE` claim transactions.

### Extending it

The plugin uses the standard Hermes dashboard plugin contract — see [Extending the Dashboard](./extending-the-dashboard) for the full manifest reference, shell slots, page-scoped slots, and the Plugin SDK. Extra columns, custom card chrome, tenant-filtered layouts, or full `tab.override` replacements are all expressible without forking this plugin.

To disable without removing: add `dashboard.plugins.kanban.enabled: false` to `config.yaml` (or delete `plugins/kanban/dashboard/manifest.json`).

### Scope boundary

The GUI is deliberately thin. Everything the plugin does is reachable from the CLI; the plugin just makes it comfortable for humans. Auto-assignment, budgets, governance gates, and org-chart views remain user-space — a router profile, another plugin, or a reuse of `tools/approval.py` — exactly as listed in the out-of-scope section of the design spec.

## CLI command reference

```
hermes kanban init                                     # create kanban.db + print daemon hint
hermes kanban create "<title>" [--body ...] [--assignee <profile>]
                                [--parent <id>]... [--tenant <name>]
                                [--workspace scratch|worktree|dir:<path>]
                                [--priority N] [--triage] [--idempotency-key KEY]
                                [--max-runtime 30m|2h|1d|<seconds>]
                                [--json]
hermes kanban list [--mine] [--assignee P] [--status S] [--tenant T] [--archived] [--json]
hermes kanban show <id> [--json]
hermes kanban assign <id> <profile>                    # or 'none' to unassign
hermes kanban link <parent_id> <child_id>
hermes kanban unlink <parent_id> <child_id>
hermes kanban claim <id> [--ttl SECONDS]
hermes kanban comment <id> "<text>" [--author NAME]

# Bulk verbs — accept multiple ids:
hermes kanban complete <id>... [--result "..."]
hermes kanban block <id> "<reason>" [--ids <id>...]
hermes kanban unblock <id>...
hermes kanban archive <id>...

hermes kanban tail <id>                                # follow a single task's event stream
hermes kanban watch [--assignee P] [--tenant T]        # live stream ALL events to the terminal
        [--kinds completed,blocked,…] [--interval SECS]
hermes kanban heartbeat <id> [--note "..."]            # worker liveness signal for long ops
hermes kanban assignees [--json]                       # profiles on disk + per-assignee task counts
hermes kanban dispatch [--dry-run] [--max N]           # one-shot pass
        [--failure-limit N] [--json]
hermes kanban daemon [--interval SECS] [--max N]       # long-lived loop
        [--failure-limit N] [--pidfile PATH] [-v]
hermes kanban stats [--json]                           # per-status + per-assignee counts
hermes kanban log <id> [--tail BYTES]                  # worker log from ~/.hermes/kanban/logs/
hermes kanban notify-subscribe <id>                    # gateway bridge hook (used by /kanban in the gateway)
        --platform <name> --chat-id <id> [--thread-id <id>] [--user-id <id>]
hermes kanban notify-list [<id>] [--json]
hermes kanban notify-unsubscribe <id>
        --platform <name> --chat-id <id> [--thread-id <id>]
hermes kanban context <id>                             # what a worker sees
hermes kanban gc [--event-retention-days N]            # workspaces + old events + old logs
        [--log-retention-days N]
```

All commands are also available as a slash command in the gateway (`/kanban list`, `/kanban comment t_abc "need docs"`, etc.). The slash command bypasses the running-agent guard, so you can `/kanban unblock` a stuck worker while the main agent is still chatting.

## Collaboration patterns

The board supports these eight patterns without any new primitives:

| Pattern | Shape | Example |
|---|---|---|
| **P1 Fan-out** | N siblings, same role | "research 5 angles in parallel" |
| **P2 Pipeline** | role chain: scout → editor → writer | daily brief assembly |
| **P3 Voting / quorum** | N siblings + 1 aggregator | 3 researchers → 1 reviewer picks |
| **P4 Long-running journal** | same profile + shared dir + cron | Obsidian vault |
| **P5 Human-in-the-loop** | worker blocks → user comments → unblock | ambiguous decisions |
| **P6 `@mention`** | inline routing from prose | `@reviewer look at this` |
| **P7 Thread-scoped workspace** | `/kanban here` in a thread | per-project gateway threads |
| **P8 Fleet farming** | one profile, N subjects | 50 social accounts |
| **P9 Triage specifier** | rough idea → `triage` → specifier expands body → `todo` | "turn this one-liner into a spec' task" |

For worked examples of each, see `docs/hermes-kanban-v1-spec.pdf`.

## Multi-tenant usage

When one specialist fleet serves multiple businesses, tag each task with a tenant:

```bash
hermes kanban create "monthly report" \
    --assignee researcher \
    --tenant business-a \
    --workspace dir:~/tenants/business-a/data/
```

Workers receive `$HERMES_TENANT` and namespace their memory writes by prefix. The board, the dispatcher, and the profile definitions are all shared; only the data is scoped.

## Gateway notifications

When you run `/kanban create …` from the gateway (Telegram, Discord, Slack, etc.), the originating chat is automatically subscribed to the new task. The gateway's background notifier polls `task_events` every few seconds and delivers one message per terminal event (`completed`, `blocked`, `gave_up`, `crashed`, `timed_out`) to that chat. Completed tasks also send the first line of the worker's `--result` so you see the outcome without having to `/kanban show`.

You can manage subscriptions explicitly from the CLI — useful when a script / cron job wants to notify a chat it didn't originate from:

```bash
hermes kanban notify-subscribe t_abcd \
    --platform telegram --chat-id 12345678 --thread-id 7
hermes kanban notify-list
hermes kanban notify-unsubscribe t_abcd \
    --platform telegram --chat-id 12345678 --thread-id 7
```

A subscription removes itself automatically once the task reaches `done` or `archived`; no cleanup needed.

## Event reference

Every transition appends a row to `task_events`. The kinds group into three clusters so filtering is easy (`hermes kanban watch --kinds completed,gave_up,timed_out`):

**Lifecycle** (what changed about the task as a logical unit):

| Kind | When |
|---|---|
| `created` | Task inserted. |
| `promoted` | `todo → ready` because all parents hit `done`. |
| `claimed` | Dispatcher atomically claimed a `ready` task for spawn. |
| `completed` | Worker wrote `--result` and task hit `done`. |
| `blocked` | Worker or human flipped the task to `blocked`. |
| `unblocked` | `blocked → ready`, either manually or via `/unblock`. |
| `archived` | Hidden from the default board. |

**Edits** (human-driven changes that aren't transitions):

| Kind | When |
|---|---|
| `assigned` | Assignee changed (including unassignment). |
| `edited` | Title or body updated. |
| `reprioritized` | Priority changed. |
| `status` | Dashboard drag-drop wrote a status directly (e.g. `todo → ready`). |

**Worker telemetry** (about the execution process, not the logical task):

| Kind | Payload | When |
|---|---|---|
| `spawned` | `{pid}` | Dispatcher successfully started a worker process. |
| `heartbeat` | `{note?}` | Worker called `hermes kanban heartbeat $TASK` to signal liveness during long operations. |
| `reclaimed` | `{stale_lock}` | Claim TTL expired without a completion; task goes back to `ready`. |
| `crashed` | `{pid, claimer}` | Worker PID no longer alive but TTL hadn't expired yet. |
| `timed_out` | `{pid, elapsed_seconds, limit_seconds, sigkill}` | `max_runtime_seconds` exceeded; dispatcher SIGTERM'd (then SIGKILL'd after 5 s grace) and re-queued. |
| `spawn_failed` | `{error, failures}` | One spawn attempt failed (missing PATH, workspace unmountable, …). Counter increments; task returns to `ready` for retry. |
| `gave_up` | `{failures, error}` | Circuit breaker fired after N consecutive `spawn_failed`. Task auto-blocks with the last error. Default N = 5; override via `--failure-limit`. |

`hermes kanban tail <id>` shows these for a single task. `hermes kanban watch` streams them board-wide.

## Out of scope

Kanban is deliberately single-host. `~/.hermes/kanban.db` is a local SQLite file and the dispatcher spawns workers on the same machine. Running a shared board across two hosts is not supported — there's no coordination primitive for "worker X on host A, worker Y on host B," and the crash-detection path assumes PIDs are host-local. If you need multi-host, run an independent board per host and use `delegate_task` / a message queue to bridge them.

## Design spec

The complete design — architecture, concurrency correctness, comparison with other systems, implementation plan, risks, open questions — lives in `docs/hermes-kanban-v1-spec.pdf`. Read that before filing any behavior-change PR.
