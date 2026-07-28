---
name: herdr-sessions
description: Discover and inspect running Herdr-managed agent sessions from Hermes — fleet.herdr host/allowlist config, the three read-only tools (herdr_status, herdr_list_sessions, herdr_inspect_session), the terminal_id vs agent-label identifier rule, the cwd vs foreground_cwd trap, and why no pane-mutation tool exists yet. Use when asked to look at, list, inspect, or connect to Herdr sessions / panes / terminals, or to configure fleet.herdr hosts.
metadata:
  hermes:
    tags: [a2a_fleet, herdr, sessions, read-only]
---

# a2a_fleet: herdr-sessions

Herdr is a terminal workspace manager for AI coding agents. It keeps live Claude
Code / Codex / OpenCode / Antigravity panes running with their terminal context
and local auth intact. These tools let Hermes **look at** those sessions.

Phase 1 is **read-only by construction**. There is no tool here that types into a
pane, and that is deliberate — not an oversight. See "Why there is no send tool".

## The three tools

| Tool | Purpose |
|---|---|
| `herdr_status(host_alias)` | Version, protocol, transport, reachability, allowed workspaces |
| `herdr_list_sessions(host_alias, workspace?, agent_kind?)` | Sessions on a host, allowlist-filtered |
| `herdr_inspect_session(host_alias, terminal_id)` | One exact session |

All three return a structured dict and never raise. When Herdr is missing,
unreachable, or on a different protocol, you get a `status` field explaining
which — not an exception and not a silent empty list.

## Configure first

Nothing works until `fleet.herdr` exists in the profile's `fleet.yaml`. An absent
block means the feature is off (not an error), and every tool will report
`unknown_host_alias` with an empty `known_hosts`.

```yaml
fleet:
  herdr:
    read_only_default: true
    require_exact_session: true
    require_confirmation_for_mutations: true
    deny_raw_input: true
    deny_wildcard_operations: true
    hosts:
      mac-mini:
        transport: local_socket
        allowed_workspaces:
          - /Users/rohits/hermes
          - /Users/rohits/.hermes/hermes-agent
      build-host:
        transport: ssh_bridge
        ssh_target: build-host          # required for ssh_bridge, forbidden otherwise
        allowed_workspaces:
          - /srv/workspaces/project-a
```

`allowed_workspaces` is a **security boundary**, not a convenience filter. Sessions
whose `cwd` falls outside it are dropped from `herdr_list_sessions` (and counted in
`filtered_out_by_allowlist`) and rejected by `herdr_inspect_session` with
`status: workspace_denied`. Containment is checked on resolved paths, so
`/srv/workspaces/project-a-evil` does not match `/srv/workspaces/project-a`.

Host aliases are **not** network addresses. They index this table. An unknown alias
is an error with the known aliases listed — never a fallback to some default host.

## Two identifier rules that will bite you

**1. `terminal_id` is the identifier. The `agent` field is not.**

`agent` is a kind label — `"claude"`, `"opencode"`, `"agy"` — and it is *not
unique*. Two Claude panes both report `"claude"`. Addressing by it would pick an
arbitrary one, which is exactly the ambiguous-selection failure this design
forbids. Always carry the `terminal_id` (`term_656fd55fd56381a`) from
`herdr_list_sessions` into `herdr_inspect_session`. `pane_id`
(`w6523b0e28fea48:pH`) rides alongside for future pane-addressed verbs.

**2. Workspace identity is `cwd`, never `foreground_cwd`.**

These diverge in practice. A live Claude pane reports:

```
cwd:            /Users/rohits/.hermes/hermes-agent      <- the session's workspace
foreground_cwd: /Users/rohits/.claude/plugins/cache/... <- a transient child process
```

`foreground_cwd` tracks whatever subprocess is in the foreground right now. Match
an allowlist against it and you will both deny valid sessions and risk matching an
unintended one. The normalizer drops it entirely; do not reintroduce it.

## Typical flow

```
herdr_status(host_alias="mac-mini")
  -> {"status":"ok","version":"0.7.4","protocol":16,
      "socket":"/Users/rohits/.config/herdr/herdr.sock",
      "transport":"local_socket","allowed_workspaces":[...]}

herdr_list_sessions(host_alias="mac-mini")
  -> {"status":"ok","count":2,"filtered_out_by_allowlist":1,
      "sessions":[{"terminal_id":"term_656fd55fd56381a","pane_id":"w652...:pH",
                   "agent_kind":"claude","agent_status":"working",
                   "cwd":"/Users/rohits/.hermes/hermes-agent","revision":35}, ...]}

herdr_inspect_session(host_alias="mac-mini", terminal_id="term_656fd55fd56381a")
  -> {"status":"ok","session":{...}}
```

`agent_status` is `idle | working | blocked | unknown`. `revision` is a monotonic
counter that advances as the session changes — Phase 2 will use it as an
optimistic-concurrency guard, so preserve it when you pass records around.

## Reading the status field

| `status` | Meaning | Fix |
|---|---|---|
| `ok` | Working | — |
| `unknown_host_alias` | Alias not in `fleet.herdr.hosts` | Add it, or use a listed alias |
| `herdr_missing` | Binary not on PATH | Install herdr |
| `herdr_unreachable` | Server not running / socket unreachable | Start herdr; check the reported socket path |
| `herdr_protocol_mismatch` | Herdr upgraded past the pinned protocol | Re-pin after re-capturing `herdr api schema --json` |
| `herdr_verbs_missing` | Required CLI verbs absent | Herdr version too old |
| `workspace_denied` | Session outside `allowed_workspaces` | Widen the allowlist *deliberately*, or pick another session |

## Why there is no send tool

Wrapping `herdr agent send` would let Hermes type into a pane a human may be
using. Three things must exist first, and none do yet:

- a confirmation token bound to a preview of the exact action;
- a `revision` guard so a stale action cannot land on a changed pane;
- an audit record that survives a gateway restart.

`herdr agent send` also has **no request ID**, so it can never be made idempotent
— a timeout leaves a genuinely unknown outcome. The rule is therefore: an unknown
outcome stays unknown and is surfaced, never retried.

Do not work around this by shelling out to `herdr` directly to type into a pane.
If a task truly needs it, say so and it gets designed properly.

## Implementation notes

These tools shell out to the `herdr` CLI and parse its JSON envelope
(`{"id":..., "result":...}` / `{"id":..., "error":{"code","message"}}`). They do
not speak Herdr's socket protocol and do not parse pane or screen content — Herdr
already owns all of that, and a guard test enforces it.

Remote hosts are one argv prefix: `herdr --remote <ssh_target> ...`. Herdr owns the
SSH bridge and its restricted `0600` socket; nothing is exposed over TCP.
