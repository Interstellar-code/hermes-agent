---
name: switchui
plugin: hermes-switch-ui
description: >
  Operator runbook for the hermes-switch-ui plugin — starting, configuring,
  and troubleshooting the SwitchUI browser frontend and its Hermes bridge.
tags: [switchui, frontend, ui, plugin, hermes-switch-ui]
---

# SwitchUI operator runbook

## What is SwitchUI?

SwitchUI is the browser frontend for Hermes agents. It runs on **port 3002**
and connects to the Hermes dashboard gateway on **port 9119**. Source:
[Interstellar-code/hermes-switchui](https://github.com/Interstellar-code/hermes-switchui).

The **hermes-switch-ui plugin** bridges the two sides:

- It gives the agent **awareness** of the frontend (per-turn nudge, two tools).
- It serves a **config-sync REST API** at `/api/plugins/hermes-switch-ui/` so
  SwitchUI can register itself, report settings, and send heartbeats to the backend.

---

## Key facts

- Gateway port: **8642** (Hermes agent API)
- Dashboard / plugin API port: **9119**
- SwitchUI frontend port: **3002**
- State file: `~/.hermes/switchui/state.json` (override: `SWITCHUI_STATE_PATH`)
- Heartbeat TTL: **90 seconds** — `running` flips to `false` if no heartbeat
  arrives within 90 s
- Body cap on POST endpoints: **32 KB** (oversized → HTTP 413)
- Plugin version compat range: `>=1.0.0,<2.0.0`

---

## Agent tools

### `switchui_info`

Returns the static capability document (`capability.md`) merged with any live
manifest fields stored in `state.json`.

```
switchui_info()               # static doc only
switchui_info(refresh=true)   # also fetches SWITCHUI_DOCS_URL (best-effort)
```

Useful when a user asks: "What can SwitchUI do?", "What's the repo URL?",
"What features does the frontend support?"

### `switchui_status`

Returns connection parameters and TTL-derived running status.

```
switchui_status()
```

Example response:

```json
{
  "running": true,
  "last_heartbeat": "2026-06-12T10:01:30Z",
  "ttl_seconds": 90,
  "manifest": { "version": "1.2.0", "url": "http://localhost:3002", "port": 3002 },
  "reported_settings": { "theme": "dark" }
}
```

### Interpreting `running` and TTL

`running: true` means a heartbeat was received within the last 90 seconds.
`running: false` means one of:

1. SwitchUI has not been started or has not connected yet.
2. SwitchUI was running but stopped sending heartbeats (frontend closed, crash,
   or Part 2 of the sync handshake not yet shipped).
3. The plugin was just enabled and no registration has occurred.

There is no background daemon; `running` is computed on each read.

---

## Sync API endpoints

All endpoints require the standard Hermes auth header. Replace `<TOKEN>` with
your dashboard bearer token (same credential used for the gateway UI at
port 9119).

### GET /connection — backend connection parameters for SwitchUI

```bash
curl -s -H "Authorization: Bearer <TOKEN>" \
  http://localhost:9119/api/plugins/hermes-switch-ui/connection | jq .
```

Example response:

```json
{
  "gateway_port": 8642,
  "dashboard_port": 9119,
  "frontend_port": 3002,
  "active_profile": "hermes-switch",
  "enabled_plugins": ["hermes-switch-ui", "a2a_fleet"],
  "auth_mode": null
}
```

`active_profile`, `enabled_plugins`, and `auth_mode` are nullable (`null` if
the config was not readable at call time).

### POST /register — SwitchUI registers itself

```bash
curl -s -X POST \
  -H "Authorization: Bearer <TOKEN>" \
  -H "Content-Type: application/json" \
  -d '{"version":"1.2.0","url":"http://localhost:3002","port":3002,"hermes_api_url":"http://localhost:9119","enabled_features":["chat","settings"],"registered_at":"2026-06-12T10:00:00Z"}' \
  http://localhost:9119/api/plugins/hermes-switch-ui/register | jq .
```

Accepted manifest keys: `version` (str), `url` (str), `port` (int),
`hermes_api_url` (str), `enabled_features` (list of strings),
`registered_at` (str). Unknown scalar keys are silently ignored;
unknown nested objects return 422.

Example response:

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

### POST /settings — SwitchUI reports its settings

```bash
curl -s -X POST \
  -H "Authorization: Bearer <TOKEN>" \
  -H "Content-Type: application/json" \
  -d '{"theme":"dark","locale":"en"}' \
  http://localhost:9119/api/plugins/hermes-switch-ui/settings | jq .
```

Keys containing `token`, `password`, or `secret` (case-insensitive substring)
are **stripped before persistence**. Values that are nested objects or lists
return 422.

Response: `{ "ok": true }`

### GET /status — TTL-derived running status

```bash
curl -s -H "Authorization: Bearer <TOKEN>" \
  http://localhost:9119/api/plugins/hermes-switch-ui/status | jq .
```

`running` is `true` when `(now − last_heartbeat) < 90 s`.

### POST /heartbeat — explicit liveness ping

```bash
curl -s -X POST \
  -H "Authorization: Bearer <TOKEN>" \
  http://localhost:9119/api/plugins/hermes-switch-ui/heartbeat | jq .
```

Response: `{ "ok": true }`

---

## Procedure — confirming the bridge is live

1. Check the plugin is loaded (call the tool inside an agent session):

   ```
   switchui_status()
   ```

2. Confirm SwitchUI is registered and running via curl:

   ```bash
   curl -s -H "Authorization: Bearer <TOKEN>" \
     http://localhost:9119/api/plugins/hermes-switch-ui/status | jq .running
   # true = heartbeat received within the last 90 s
   ```

3. Manually send a test heartbeat to verify the API is reachable:

   ```bash
   curl -s -X POST -H "Authorization: Bearer <TOKEN>" \
     http://localhost:9119/api/plugins/hermes-switch-ui/heartbeat
   # {"ok":true}
   ```

4. Confirm `/connection` returns the expected active profile:

   ```bash
   curl -s -H "Authorization: Bearer <TOKEN>" \
     http://localhost:9119/api/plugins/hermes-switch-ui/connection | jq .active_profile
   ```

---

## Troubleshooting

### Plugin not available / tools missing

**Symptom:** `switchui_info` / `switchui_status` tools are not present;
no per-turn nudge on the first LLM call.

**Cause:** Plugin not listed in profile `plugins.enabled`.

**Fix:** Edit `~/.hermes/profiles/<profile>/config.yaml`:

```yaml
plugins:
  enabled:
    - hermes-switch-ui
```

Restart the agent gateway after saving.

---

### `running: false` — stale or missing heartbeat

**Symptom:** `switchui_status()` returns `running: false`.

**Possible causes:**

1. **SwitchUI frontend is not running.** Start it:

   ```bash
   cd /path/to/hermes-switchui && npm run dev
   ```

   Then open `http://localhost:3002`.

2. **Part 2 of the sync handshake not yet shipped.** The frontend-side
   heartbeat loop lives in the SwitchUI codebase
   (`Interstellar-code/hermes-switchui`). If that code has not been
   released, no heartbeats arrive even when the tab is open. Use the
   manual curl heartbeat above as a workaround:

   ```bash
   curl -s -X POST -H "Authorization: Bearer <TOKEN>" \
     http://localhost:9119/api/plugins/hermes-switch-ui/heartbeat
   ```

3. **TTL elapsed.** SwitchUI was running but sent its last heartbeat more
   than 90 seconds ago (tab backgrounded, network hiccup). Reload the
   SwitchUI tab.

---

### State file not found or unexpected values

**Symptom:** `switchui_status()` shows `last_heartbeat: null` or errors
appear in the agent gateway log.

**Check the file:**

```bash
cat ~/.hermes/switchui/state.json
```

**Override the path:**

```bash
export SWITCHUI_STATE_PATH=/tmp/switchui-state.json
```

Set this in the profile environment or shell before starting the gateway.

---

### API returns 401

Auth header missing or token invalid. Confirm the token matches the one used
to access the dashboard at `http://localhost:9119`.

### API returns 413

Request body exceeds 32 KB. Trim the payload.

### API returns 422

Invalid JSON, wrong type for a whitelisted manifest key, or an unknown nested
object/list in the payload. Check the `detail` field in the response body for
the exact key that triggered validation.

---

## Related

- Plugin source: `plugins/hermes-switch-ui/` in the hermes-agent repo
- Plugin README: `plugins/hermes-switch-ui/README.md`
- SwitchUI repo: https://github.com/Interstellar-code/hermes-switchui
- Capability doc: `plugins/hermes-switch-ui/capability.md`
