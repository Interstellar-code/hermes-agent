# Herdr mode — operations note

Observed values, not aspirations. Everything below was measured against the
real local Herdr instance on 2026-07-29 with the soak described at the end.

## Environment as measured

| Property | Observed |
|---|---|
| Herdr version | 0.7.4 |
| API protocol | 16 (`schema_version: 1`) |
| Transport | `local_socket` |
| Socket | `~/.config/herdr/herdr.sock`, mode `0600` |
| Discovery latency (`herdr agent list`) | median 26 ms, max 33 ms over 7 runs |
| Exact inspection (`herdr agent get`) | median 22 ms |
| Capability probe (`herdr status` + `api schema`) | median 21 ms |
| Preview (inspect + token mint) | median 24 ms |

Latency is CLI-process dominated: every call spawns `herdr`. That is the price
of the CLI-wrapper transport decision and it is worth it — Herdr owns protocol
negotiation, timeouts, and the SSH bridge. Budget ~25 ms per call, not ~1 ms.

## Two protocol findings that changed the design

### `revision` is a pane-output counter, not a session version

In protocol 16, `revision` appears on exactly one event, `pane_output_changed`,
and the subscription filter that consumes it is `min_revision` on that same
event. Status transitions ride `pane_agent_status_changed`, which carries no
revision.

Consequence: an agent can go `done -> working` with `revision` unchanged. This
was observed live and initially looked like a stale read. It is not.

- Correct use: "has this pane produced output since I looked" — which is
  exactly the staleness guard `herdr_request_action` needs.
- Incorrect use: a general optimistic-concurrency guard. It silently misses
  every status-only change.

### Hermes cannot hold Herdr authority

`src/detect/mod.rs:283` hardcodes which `--source` values may report agent
lifecycle:

```rust
full_lifecycle_hook_authority: ("herdr:pi","pi") | ("herdr:omp","omp")
  | ("herdr:mastracode","mastracode") | ("herdr:opencode","opencode")
  | ("herdr:kilo","kilo") | ("herdr:kimi","kimi")
session_identity_only_integration: ("herdr:hermes","hermes")
```

`set_hook_authority_at` returns early for the session-identity-only case, so a
`pane report-agent` from Hermes is **dropped**. `--source` is not
caller-chosen, and claiming authority under some other source would mean lying
to Herdr about which agent owns the pane.

Consequence: the original plan's "bind Fleet state to Herdr's authority verbs"
is not implementable. Fleet **reads** authority-adjacent state and never writes
it. `herdr_claim_human_takeover` is a Fleet-side pause held in SQLite; the
Herdr session is untouched and keeps running.

Note also that `PaneInfo` exposes no authority-holder field, so Fleet cannot
see *who* holds authority — only `agent_status` and `revision`.

## Operating the mode

Mutations are off by default and require **two** switches:

```yaml
fleet:
  herdr:
    require_confirmation_for_mutations: true   # default; false = fleet_send may send directly
    hosts:
      mac-mini:
        transport: local_socket
        allow_actions: true                    # default false; per host, never global
        allowed_workspaces: [/Users/rohits/hermes]
```

- `allow_actions: false` (default) → every action tool returns
  `actions_disabled` and names the switch.
- `require_confirmation_for_mutations: true` (default) → `fleet_send` to a
  herdr peer returns a confirmation token instead of typing into the pane.

Confirmation tokens are single-use, expire in 300 s, are bound to one session,
and are refused once the session's `revision` moves.

State lives in SQLite in the profile `state.db` (`herdr_tokens`,
`herdr_bindings`, `herdr_audit`) — deliberately not `ContextStore`, which is an
in-memory LRU wiped on gateway restart. Herdr sessions outlive the gateway, so
a takeover pause held in memory would silently lift on a bounce.

## Failure modes and what they look like

| Situation | Response | Recovery |
|---|---|---|
| Unknown host alias | `unknown_host_alias` + `known_hosts` | fix `host_alias` |
| Session gone | `not_found` | re-discover with `herdr_list_sessions` |
| Agent label used as selector | `ambiguous_identifier` | use the exact `term_...` |
| Padded / control-char id | `invalid_terminal_id` + `hint` | pass the exact handle |
| Session outside allowlist | `workspace_denied` | widen `allowed_workspaces` deliberately |
| Two sessions match a peer | refusal naming both | target one by `terminal_id` |
| Bound context's pane vanished | refusal, no re-homing | rebind deliberately |
| Token replayed | `token_already_used` | preview again |
| Pane produced output first | `revision_moved`, token spent | inspect, then preview again |
| Send connection dropped | `outcome_unknown`, `retry: never` | inspect the pane by eye |
| No `done` within timeout | `completion_state: pending` | keep waiting; never treated as done |

**Never retry a send.** `herdr agent send` carries no request ID and Herdr has
no dedup, so a delivered action and a lost one are indistinguishable
afterwards. The audit records intent *before* the send precisely so an unknown
outcome is visible rather than invisible.

**Completion is only Herdr's `done` signal** (`wait agent-status --status
done`). Silence, an idle prompt, and a quiet pane are not evidence.

## Soak coverage — 2026-07-29, 12/12 checks

Ran against real Herdr with a throwaway `HERMES_HOME` (the real `fleet.yaml`
and `state.db` were untouched): capability probe, discovery and inspection
latency, `agent_kind` population, vanished session, agent-label rejection,
malformed identifiers, allowlist enforcement, live token mint, takeover
block + release with the session surviving, audit completeness.

### Not covered, and why

- **The write path.** `herdr_request_action` was never invoked against a live
  session: the only sessions available are the operator's own working panes,
  and typing into them to prove a test passes is not an acceptable trade. It
  needs a throwaway pane and an explicit go-ahead.
- **Remote / SSH bridge.** No second Herdr host exists. Descoped from Phase 0
  onward; `transport: ssh_bridge` is implemented (an argv prefix) but unproven.
- **SSH disconnect/reconnect and remote Herdr restart.** Blocked on the same
  missing host.
