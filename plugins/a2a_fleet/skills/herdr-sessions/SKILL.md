---
name: herdr-sessions
description: Invoke as `a2a_fleet:herdr-sessions` (the bare name does not resolve). Discover and inspect running Herdr-managed agent sessions from Hermes — fleet.herdr host/allowlist config, the read-only tools (herdr_status, herdr_list_sessions, herdr_inspect_session), the confirmation-gated action tools (herdr_preview_action, herdr_request_action) and human takeover, the terminal_id vs agent-label identifier rule, and the cwd vs foreground_cwd trap. Use when asked to look at, list, inspect, or connect to Herdr sessions / panes / terminals, or to configure fleet.herdr hosts.
metadata:
  hermes:
    tags: [a2a_fleet, herdr, sessions]
---

# a2a_fleet: herdr-sessions

Load this skill by its **qualified** name, `a2a_fleet:herdr-sessions`. The bare
`herdr-sessions` does not resolve.

Herdr is a terminal workspace manager for AI coding agents. It keeps live Claude
Code / Codex / OpenCode / Antigravity panes running with their terminal context
and local auth intact. These tools let Hermes **look at** those sessions.

Discovery and inspection are read-only. Writing into a session is possible but
gated at five separate points and off by default — see "Submitting a prompt into
a session".

## The tools

| Tool | Purpose |
|---|---|
| `herdr_status(host_alias)` | Version, protocol, transport, reachability, allowed workspaces |
| `herdr_list_sessions(host_alias, workspace?, agent_kind?)` | Sessions on a host, allowlist-filtered |
| `herdr_inspect_session(host_alias, terminal_id)` | One exact session |
| `herdr_preview_action(host_alias, terminal_id, action, summary?)` | Describe a pending submission, mint a confirmation token. Mutates nothing |
| `herdr_request_action(host_alias, terminal_id, confirmation_token, wait_timeout_ms?)` | Submit that prompt, once |
| `herdr_claim_human_takeover` / `herdr_release_human_takeover` | Pause / resume Fleet automation for one session |

The read-only three return a structured dict and never raise. When Herdr is missing,
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
        allow_actions: true        # default false; required before any submission
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

`agent_status` is `idle | working | blocked | done | unknown`.

`revision` is a **presentation counter, not an output counter and not a session
version.** It rides on the `pane_output_changed` event, which makes it look like
an output counter; it is not. In Herdr 0.7.4 it is incremented in exactly three
places, none of them terminal output:

| Site | Trigger |
|---|---|
| `src/terminal/state.rs:198` | stripped terminal title changed |
| `src/app/actions.rs:1083` | metadata-token expiry |
| `src/app/api/panes.rs:1408` | metadata-token patch (`report-metadata`) |

Consequences, both verified live on 2026-07-29:

- An agent can go `done -> working` with `revision` unchanged (status rides
  `pane_agent_status_changed`, which carries no revision).
- A pane can **produce output and receive typed text with `revision` still 0**.
  A throwaway `bash` pane did exactly that, and a deliberately stale
  confirmation token was accepted as a result.

So the staleness guard in `herdr_request_action` is meaningful for agents that
rewrite their title as they work (Claude Code, OpenCode both do) and blind for
anything that does not. Every preview and send reports `revision_guard` saying
which case you are in. When it says `blind`, the protections that remain are
the single-use token, its 300 s TTL, and the human who confirmed it — do not
treat a passed revision check as evidence the pane was idle.

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
| `invalid_terminal_id` | Padded with whitespace, or contains control characters | Pass the exact `term_...` handle; the `hint` field shows the trimmed form. Ids are never normalized for you |

## Submitting a prompt into a session

Typing into a pane a human may be using is gated, not forbidden. The gates that
had to exist first now do, and every one of them applies to every submission:

1. exact `terminal_id` — never an agent label;
2. `allow_actions: true` on the host (default off);
3. no human takeover held on that session;
4. a single-use confirmation token from `herdr_preview_action`, bound to this
   session and its `revision` at preview time;
5. an audit record written **before** the submission, in the profile `state.db`.

The flow is two calls: `herdr_preview_action` (mutates nothing, returns a token
that expires in 300 s) then `herdr_request_action` with that token.

Submission uses Herdr's own `agent prompt` verb, which writes the text and
schedules its Enter in one call. It is deliberately NOT built from a
literal-text write plus a separate keystroke verb: the submission encoding is
per-runtime, Herdr enforces preconditions we cannot see (it refuses when the
agent is no longer the pane's foreground process), and the delay between text
and Enter is a race Herdr already gets right. It is also the narrower surface —
the call takes `(target, text)` and no key list, so there is no arbitrary-key
route at all. Key names inside your action text travel as literal text.

**There is no request ID and no dedup**, so a submission can never be made
idempotent. Read the status exactly:

| Status | Meaning | What to do |
|---|---|---|
| `submitted` | Herdr accepted the prompt and scheduled its Enter | Nothing. It is an acknowledgement, not observed proof the agent acted |
| `submission_rejected` | Herdr refused before writing anything (`agent_not_ready`, `agent_not_found`, …) | Nothing is in the composer. Fix the cause and preview again |
| `draft_inserted_submission_unknown` | The call did not complete cleanly | **Never retry** — inspect the session first; the text may or may not have gone in |

Do not work around any of this by shelling out to `herdr` directly.

## Implementation notes

These tools shell out to the `herdr` CLI and parse its JSON envelope
(`{"id":..., "result":...}` / `{"id":..., "error":{"code","message"}}`). They do
not speak Herdr's socket protocol and do not parse pane or screen content — Herdr
already owns all of that, and a guard test enforces it.

Remote hosts are one argv prefix: `herdr --remote <ssh_target> ...`. Herdr owns the
SSH bridge and its restricted `0600` socket; nothing is exposed over TCP.
