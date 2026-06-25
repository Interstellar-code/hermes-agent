# hermes-switch-ui

Backend plugin that gives the Hermes agent awareness of the
[SwitchUI](https://github.com/Interstellar-code/hermes-switchui) browser
frontend and keeps the two sides in sync.

---

## Purpose

SwitchUI is the primary browser frontend for a Hermes agent. Without this
plugin the agent has no knowledge that a UI exists, cannot tell the user
whether it is running, and cannot serve the config-sync API the frontend
uses to stay calibrated.

This plugin provides three capabilities:

1. **Per-turn nudge** — injects a one-paragraph system note on the first
   LLM call of each session so the agent knows to call `switchui_info` or
   `switchui_status` when users ask about the UI.
2. **Two agent tools** — `switchui_info` (capability + feature docs) and
   `switchui_status` (live running status + connection parameters).
3. **Bidirectional config-sync API** — five REST endpoints mounted at
   `/api/plugins/hermes-switch-ui/` that allow SwitchUI to register itself,
   report its settings, and keep the backend informed via heartbeats.

---

## Architecture

```
┌───────────────────────────────────────┐
│  Hermes agent (port 8642 gateway)     │
│                                       │
│  __init__.py                          │
│    register(ctx):                     │
│      ctx.register_hook(pre_llm_call)  │  ← injects nudge once per session
│      ctx.register_tool(switchui_info) │  ← capability doc + live manifest
│      ctx.register_tool(switchui_status│  ← connection info + TTL status
│                                       │
│  dashboard/plugin_api.py              │
│    FastAPI router, 5 endpoints        │  ← mounted at /api/plugins/hermes-switch-ui/
│    loaded via dashboard/manifest.json │     by web_server._mount_plugin_api_routes()
│                                       │
│  _state.py                            │  ← atomic state.json r/w, TTL, validation
│  _knowledge.py                        │  ← capability.md + live manifest merge
└───────────────────────────────────────┘
            ↕  HTTP (port 9119)
┌───────────────────────────────────────┐
│  SwitchUI frontend (port 3002)        │
└───────────────────────────────────────┘
```

The plugin uses **no background threads or daemons**. The `running` field in
every status response is computed on read by comparing `last_heartbeat` to
`now` — there is nothing to keep running.

---

## Pre-LLM-call nudge

On the **first** LLM call of a session, `_pre_llm_call` injects a short
system context paragraph describing what SwitchUI is and which tools to use.
Subsequent calls in the same session receive nothing (the session ID is
tracked in an in-process set; a process restart re-injects, which also covers
session-resume after restart).

---

## Agent tools

### `switchui_info`

```
description: Return SwitchUI capability information (repo, ports, env vars, features).
schema:      { "refresh": boolean }   (optional)
```

Returns the static `capability.md` document merged with any live manifest
fields stored in `state.json`. When `refresh=true` the plugin attempts a
best-effort GET to `SWITCHUI_DOCS_URL` (all errors swallowed).

### `switchui_status`

```
description: Return SwitchUI connection info and live status (ports, active profile,
             enabled plugins).
schema:      {}   (no parameters)
```

Returns connection parameters (`gateway_port`, `dashboard_port`,
`frontend_port`, `active_profile`, `enabled_plugins`, `auth_mode`) and the
TTL-derived `running` flag. Runtime fields are **best-effort / nullable** —
`None` on any config-read failure.

---

## Sync API — endpoints

The router is mounted at `/api/plugins/hermes-switch-ui/`. All endpoints
require authentication (session cookie or bearer token via
`hermes_cli.web_server._is_authenticated`).

| Method | Path | Direction | Purpose |
|--------|------|-----------|---------|
| `GET`  | `/connection` | backend → frontend | Connection parameters for SwitchUI to bootstrap |
| `POST` | `/register`   | frontend → backend | SwitchUI registers itself; persists manifest |
| `POST` | `/settings`   | frontend → backend | SwitchUI reports its settings |
| `GET`  | `/status`     | frontend polls     | TTL-derived running status |
| `POST` | `/heartbeat`  | frontend → backend | Explicit liveness ping |

### `GET /connection`

Response shape:

```json
{
  "gateway_port": 8642,
  "dashboard_port": 9119,
  "frontend_port": 3002,
  "active_profile": "hermes-switch",
  "enabled_plugins": ["hermes-switch-ui", "..."],
  "auth_mode": null
}
```

`active_profile`, `enabled_plugins`, and `auth_mode` are nullable; `null`
means the config was not readable at the time of the call.

### `POST /register`

Request body (JSON, max 32 KB):

```json
{
  "version": "1.2.0",
  "url": "http://localhost:3002",
  "port": 3002,
  "hermes_api_url": "http://localhost:9119",
  "enabled_features": ["chat", "settings"],
  "registered_at": "2026-06-12T10:00:00Z"
}
```

The manifest whitelist accepts exactly these keys:
`version` (str), `url` (str), `port` (int), `hermes_api_url` (str),
`enabled_features` (list of strings), `registered_at` (str).
Unknown scalar keys are silently ignored; unknown nested objects raise 422.

Response on success:

```json
{
  "ok": true,
  "compat": {
    "compatible": true,
    "warn": false,
    "plugin_range": ">=1.0.0,<2.0.0",
    "frontend_version": "1.2.0"
  }
}
```

### `POST /settings`

Request body (JSON, max 32 KB). Any flat key/value map. Keys matching
`token`, `password`, or `secret` (case-insensitive substring) are **stripped
before persistence**. Unknown nested objects (dict or list values) raise 422.

Response: `{ "ok": true }`

### `GET /status`

Response:

```json
{
  "running": true,
  "last_heartbeat": "2026-06-12T10:01:30Z",
  "ttl_seconds": 90,
  "manifest": { "version": "1.2.0", "url": "http://localhost:3002" },
  "reported_settings": { "theme": "dark" }
}
```

`running` is `true` when `(now − last_heartbeat) < 90 s`. When no heartbeat
has ever been recorded, `running` is `false` and `last_heartbeat` is `null`.

### `POST /heartbeat`

Body: empty or `{}`. Stamps `last_heartbeat = now`.

Response: `{ "ok": true }`

### Error codes

| Code | Meaning |
|------|---------|
| `401` | Missing or invalid authentication |
| `413` | Request body exceeds 32 KB |
| `422` | Invalid JSON, wrong type for a whitelisted key, or unknown nested blob |

---

## State file

State is persisted to `~/.hermes/switchui/state.json`. The directory is
separate from `workflows/` and other Hermes state to avoid collision.

**Override:** set `SWITCHUI_STATE_PATH` to an absolute path to use a
different location.

Writes are atomic: data is written to a sibling temp file and then renamed
with `os.replace`, so the file is never partially written.

Schema:

```json
{
  "last_heartbeat": "2026-06-12T10:01:30Z",
  "manifest": { "version": "1.2.0", "url": "http://localhost:3002", "port": 3002 },
  "settings": { "theme": "dark" }
}
```

---

## Heartbeat and TTL semantics

- `HEARTBEAT_TTL = 90` seconds.
- `running` is computed at read time: `(now − last_heartbeat) < 90 s`.
- There is no background thread. If SwitchUI stops sending heartbeats,
  `running` flips to `false` on the next read after 90 s.
- SwitchUI must POST `/heartbeat` (or `/register`) at least once every
  90 seconds to remain `running = true`.

---

## Enabling the plugin

Add `hermes-switch-ui` to `plugins.enabled` in the profile's `config.yaml`:

```yaml
plugins:
  enabled:
    - hermes-switch-ui
```

The agent gateway must be restarted after editing `config.yaml`.

The plugin registers itself via `register(ctx)` which the Hermes plugin
loader calls once at startup. It installs the `pre_llm_call` hook, the two
tools, and the `switchui` skill. The sync API is mounted separately when the
dashboard gateway starts and discovers `dashboard/manifest.json`.

---

## Environment variables

| Variable | Default | Purpose |
|----------|---------|---------|
| `SWITCHUI_STATE_PATH` | `~/.hermes/switchui/state.json` | Override state file location |
| `SWITCHUI_DOCS_URL` | *(unset)* | URL for best-effort remote capability refresh |

---

## Version compatibility

The plugin declares `compatible_switchui: ">=1.0.0,<2.0.0"`. The `compat`
field in the `/register` response will carry `compatible: false` and
`warn: true` when the frontend reports a version outside this range.

---

## Related

- [SwitchUI repo](https://github.com/Interstellar-code/hermes-switchui) — the
  frontend this plugin bridges.
- `plugins/hermes-switch-ui/skills/switchui/SKILL.md` — operator runbook for
  using this plugin in a running agent session.
- `plugins/hermes-switch-ui/capability.md` — bundled static capability doc
  returned by `switchui_info`.
