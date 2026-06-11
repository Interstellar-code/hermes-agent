# Work Plan: `hermes-switch-ui` Bundled Plugin + SwitchUI Settings Section

**Plan name:** hermes-switch-ui-plugin
**Repo (Part 1):** `/Users/rohits/.hermes/hermes-agent`
**Repo (Part 2):** `Interstellar-code/hermes-switchui` (frontend, spec + GitHub issue only — not implemented here)
**Status:** Ready for execution (sonnet executors). Requirements fully decided. Do NOT re-interview.

---

## Context

SwitchUI (`hermes-switchui`) is the primary browser frontend for Hermes. Today the backend agent has **zero awareness** of it: it cannot tell a user what SwitchUI does, where to find it, whether it is running, or what settings it reports. There is no config-sync channel between the two.

This plan delivers a new **standalone bundled plugin** `hermes-switch-ui` in `hermes-agent` that gives the agent (a) turn-by-turn awareness via a `pre_llm_call` hook, (b) deep knowledge via registered tools backed by a bundled capability doc + live-registered manifest, and (c) a bidirectional config-sync HTTP API. It also specifies the matching **Part 2** SwitchUI settings section and ships a ready-to-file GitHub issue for that repo.

### Verified architecture facts (cite these; do NOT re-explore)

**Plugin anatomy** (modeled on `plugins/workflow-engine` and `plugins/karpathy-self-improve`):
- `plugins/<name>/plugin.yaml`: `name`, `version`, `description`, `author`, `kind: standalone`, `provides_tools`, `pip_dependencies` (`fastapi`), `requires_env`, `optional_env`, `dashboard_manifest`.
- `plugins/<name>/__init__.py` must expose `register(ctx)`. `ctx` API: `register_tool(name, toolset, schema, handler)`, `register_hook("pre_llm_call"|"post_tool_call"|"on_session_end", cb)`, `register_command`.
- `pre_llm_call` cb returns `{"context": "..."}` or `str`; injected into the **USER message** (never system prompt — preserves prompt cache); ephemeral per-turn. Docs: `hermes_cli/plugins.py:1540-1565`. Loader: `hermes_cli/plugins.py:1411` (`_load_plugin`).
- `dashboard/plugin_api.py` with a **module-level `router = APIRouter()`** is auto-mounted at `/api/plugins/<name>/` by `web_server._mount_plugin_api_routes()` (`web_server.py:9166`, `include_router` at `:9233`). Loaded **flat** via `spec_from_file_location` — **NO relative imports**. Use `sys.path` injection of the plugin dir so `_state`, `_knowledge` etc. resolve as top-level modules (verified pattern in karpathy `dashboard/plugin_api.py`).
- **Auth:** per-route `Depends(_require_auth)` calling `web_server._is_authenticated` (session cookie/token), with a try/except fallback that no-ops in test/standalone contexts (verified karpathy pattern).
- **CORS:** localhost/127.0.0.1 on any port already allowed (`web_server.py:155-164`) — SwitchUI on `:3002` is covered.
- `dashboard/manifest.json` optional (only for a dashboard tab — not required here).
- **Enable:** add to `plugins.enabled` list in profile `config.yaml`. The `hermes-switch` profile (`/Users/rohits/.hermes/profiles/hermes-switch/config.yaml`) currently has **NO `plugins` block** — must be added. Root default profile = `/Users/rohits/.hermes/config.yaml`.
- **Precedent:** `workflow-engine` plugin already serves the hermes-switchui frontend and uses `~/.hermes/switchui/` (subdir `workflows/`). We add a sibling `~/.hermes/switchui/state.json` — must NOT collide with `workflows/`.

**SwitchUI facts:**
- Local checkout: `/Volumes/Ext-nvme/Development/hermes-switchui`. Stack: React 19 + TanStack Start/Router, Hono BFF on port `3002`, Zustand, Tailwind v4, Vite, pnpm, optional Electron.
- Talks to gateway `8642` (`HERMES_API_URL` — chat/sessions/memory) and dashboard `9119` (`HERMES_DASHBOARD_URL` via `/api/dashboard-proxy/$` splat with server-side bearer injection).
- Env: `HERMES_API_URL`, `HERMES_API_TOKEN`, `HERMES_DASHBOARD_TOKEN`, `PORT=3002`, `HERMES_PASSWORD`; live overrides in `~/.hermes/workspace-overrides.json` via Settings→Connection.
- Settings screen: `src/screens/settings/settings-screen.tsx`; sections are `src/screens/settings/sections/section-*.tsx` (16 existing). Providers wizard precedent at `/settings/providers`.
- Features: chat, dashboard, files, terminal, memory, skills, jobs, matrix3d, boards, profiles, mcp, tasks, operations, self-improve, workflows, settings, commands, conductor, agora, docs.
- Reference links (ship in doc): repo `https://github.com/Interstellar-code/hermes-switchui`, website `https://hermes-switchui.zi0n.space/`, docs `https://hermes-switchui.zi0n.space/docs/welcome/`.

### Port reference (canonical, used across endpoints)
- Gateway: **8642**
- Dashboard: **9119**
- SwitchUI (Hono BFF): **3002**

---

## Work Objectives

1. Make the backend agent **aware** of SwitchUI once per session (short hook nudge on the first LLM call of each session) and **knowledgeable** on demand (tools + bundled doc).
2. Establish a **bidirectional config-sync HTTP API** under `/api/plugins/hermes-switch-ui/`: backend→frontend connection info, frontend→backend settings report (persisted), heartbeat/health, and version-compat check.
3. Persist live registration + heartbeat + reported settings to `~/.hermes/switchui/state.json` with TTL-based "running" detection (no background threads).
4. Test the plugin with pytest mirroring the karpathy `spec_from_file_location` harness.
5. Enable the plugin in the `hermes-switch` profile and document it.
6. Specify Part 2 (SwitchUI settings section) and ship a ready-to-file GitHub issue.

## Guardrails

**Must Have**
- `register(ctx)` installs exactly ONE `pre_llm_call` hook returning a short one-paragraph nudge ONLY on the first call of each session (tracked via `session_id` kwarg in an in-process set; return `None` thereafter — `invoke_hook` drops `None`). No full doc dump, no per-turn repetition — protects prompt cache & token budget.
- All HTTP routes auth-gated via `Depends(_require_auth)` with the karpathy try/except fallback.
- `dashboard/plugin_api.py` uses module-level `router` + `sys.path` injection; NO relative imports.
- Live-registered manifest overrides static doc values (URL/port/version/features).
- State writes are atomic (temp file + `os.replace`) and payload-validated/size-limited.
- Heartbeat "running" is TTL-derived at read time — no daemon/thread.

**Must NOT Have**
- No background threads/daemons for heartbeat.
- No relative imports in `plugin_api.py`.
- No injection into the system prompt (USER-message context only).
- No collision with `~/.hermes/switchui/workflows/`.
- No unauthenticated mutating endpoints.
- No Part 2 frontend code in this repo (spec + issue only).

---

## Task Flow

```
Phase 1: Scaffold plugin (yaml + __init__ + dirs)
        │
Phase 2: Knowledge doc + tools + pre_llm_call hook
        │
Phase 3: Sync API (plugin_api.py endpoints) + state persistence (_state.py) + version compat (_version_compat.py)
        │
Phase 4: Tests (pytest, spec_from_file_location harness)
        │
Phase 5: Enablement (profile config) + docs (README)
        │
Phase 6: Part 2 spec + GitHub issue (file to Interstellar-code/hermes-switchui)
```

---

## File-by-File Layout (new plugin)

```
plugins/hermes-switch-ui/
├── plugin.yaml
├── __init__.py                      # register(ctx): tools + pre_llm_call hook
├── README.md                        # operator docs
├── _state.py                        # state.json read/write, TTL heartbeat, validation
├── _knowledge.py                    # load bundled doc, merge live manifest, version-compat constants
├── _version_compat.py               # semver range check (or inline constants in _knowledge)
├── capability.md                    # bundled static capability doc (features, routes, URLs, connect guide)
├── dashboard/
│   └── plugin_api.py                # module-level router; 4 sync endpoints + register endpoint
└── tests/
    ├── test_register_contract.py    # register(ctx) installs hook + tools
    ├── test_api_routes.py           # spec_from_file_location load; endpoint behavior
    ├── test_state.py                # state.json roundtrip, TTL heartbeat, validation/limits
    └── test_version_compat.py       # semver range matching + mismatch warning
```

### `plugin.yaml`
```yaml
name: hermes-switch-ui
version: "0.1.0"
description: "Backend awareness of the SwitchUI frontend + bidirectional config sync. Injects a per-turn nudge, exposes switchui_info/switchui_status tools, and serves a config-sync API at /api/plugins/hermes-switch-ui/."
author: Interstellar-code
kind: standalone

provides_tools:
  - switchui_info
  - switchui_status

pip_dependencies:
  - fastapi

requires_env: []

optional_env:
  - SWITCHUI_STATE_PATH        # override ~/.hermes/switchui/state.json
  - SWITCHUI_DOCS_URL          # override docs fetch URL for refresh

# Informational only; no dashboard tab needed (no dashboard/manifest.json).
# dashboard_manifest: dashboard/manifest.json

# SwitchUI version compatibility (semver range the plugin understands)
# Mirrored in _knowledge.py constants; see version-compat scheme below.
compatible_switchui: ">=1.0.0,<2.0.0"
```

### `__init__.py` — `register(ctx)` sketch
```python
"""hermes-switch-ui plugin. Awareness hook + tools. API lives in dashboard/plugin_api.py."""
from __future__ import annotations
import logging
from pathlib import Path

log = logging.getLogger(__name__)
_PLUGIN_DIR = Path(__file__).resolve().parent

# Import sibling modules as top-level (plugin dir is on sys.path during load).
import sys
if str(_PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_DIR))
import _knowledge   # noqa: E402
import _state       # noqa: E402

_NUDGE = (
    "SwitchUI is the primary browser frontend for this Hermes agent. "
    "If the user asks about the UI, its features, how to connect, or whether it is running, "
    "call the `switchui_info` tool (capabilities/connection) or `switchui_status` "
    "(live running/heartbeat/version state). Do not guess SwitchUI details."
)

# Once-per-session nudge: hook kwargs include session_id (and is_first_turn) — see
# hermes_cli/hooks.py "pre_llm_call" payload contract. Track seen sessions in-process;
# inject only on the first LLM call of each session, return None afterwards
# (invoke_hook drops None results). Process restart clears the set -> re-inject,
# which also covers session resume after restart (injection is never persisted).
_nudged_sessions: set = set()

def _pre_llm_call(session_id: str = "", **kwargs):
    if session_id in _nudged_sessions:
        return None
    _nudged_sessions.add(session_id)
    # Ephemeral, one short paragraph -> USER message, preserves prompt cache.
    return {"context": _NUDGE}

def _tool_switchui_info(args: dict) -> dict:
    # Static capability doc merged with last live-registered manifest (live overrides static).
    return _knowledge.get_info(refresh=bool(args.get("refresh")))

def _tool_switchui_status(args: dict) -> dict:
    # TTL-derived running state + heartbeat + reported settings + version-compat verdict.
    return _state.get_status()

def register(ctx):
    ctx.register_hook("pre_llm_call", _pre_llm_call)
    ctx.register_tool(
        "switchui_info", "hermes-switch-ui",
        {
            "type": "object",
            "properties": {"refresh": {"type": "boolean",
                "description": "If true, attempt to refresh capability info from the docs site."}},
            "additionalProperties": False,
        },
        _tool_switchui_info,
    )
    ctx.register_tool(
        "switchui_status", "hermes-switch-ui",
        {"type": "object", "properties": {}, "additionalProperties": False},
        _tool_switchui_status,
    )
    log.info("hermes-switch-ui registered: 1 hook, 2 tools")
```
> Note: confirm exact `register_tool` signature/arg order against `hermes_cli/plugins.py` and the karpathy/workflow-engine `register()` at execution time; adapt the call shape (positional vs kwargs) to match the live API. The schema/handler contract above is the intent.

### `dashboard/plugin_api.py` — endpoints sketch
```python
"""FastAPI router for hermes-switch-ui.
Mounted at /api/plugins/hermes-switch-ui/ by web_server._mount_plugin_api_routes().
Loaded flat via spec_from_file_location — NO relative imports. sys.path injection below.
"""
from __future__ import annotations
import logging, sys
from pathlib import Path
from typing import Optional

_PLUGIN_DIR = Path(__file__).resolve().parent.parent   # plugins/hermes-switch-ui/
if str(_PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_DIR))

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
import _state, _knowledge, _version_compat   # top-level resolution

log = logging.getLogger(__name__)
_PLUGIN_NAME = "hermes-switch-ui"
_VERSION = "0.1.0"

def _require_auth(request: Request) -> None:
    try:
        from hermes_cli.web_server import _is_authenticated  # type: ignore[import]
        if not _is_authenticated(request):
            raise HTTPException(status_code=401, detail="Unauthorized")
    except (ImportError, AttributeError):
        pass  # test/standalone

router = APIRouter()

# 1) backend -> frontend connection info
@router.get("/connection", dependencies=[Depends(_require_auth)])
async def connection_info():
    return _knowledge.connection_info()   # ports, active profile, enabled plugins, auth mode

# 2) frontend -> backend live registration (manifest). Overrides static.
@router.post("/register", dependencies=[Depends(_require_auth)])
async def register_frontend(request: Request):
    payload = await request.json()
    manifest = _state.validate_manifest(payload)        # validate + size-limit; raises 422 on bad
    _state.save_manifest(manifest)                       # also stamps last_heartbeat = now
    compat = _version_compat.check(manifest.get("version"))
    return JSONResponse({"ok": True, "compat": compat})  # compat.warn on mismatch

# 3) frontend -> backend settings report (persisted)
@router.post("/settings", dependencies=[Depends(_require_auth)])
async def report_settings(request: Request):
    payload = await request.json()
    settings = _state.validate_settings(payload)         # validate + size-limit
    _state.save_settings(settings)                        # stamps last_heartbeat = now
    return JSONResponse({"ok": True})

# 4) health / heartbeat — TTL-derived running state, no thread
@router.get("/status", dependencies=[Depends(_require_auth)])
async def status():
    return _state.get_status()       # {running, last_heartbeat, ttl_s, manifest, settings, compat}

# (optional) explicit heartbeat ping (frontend polls or pings)
@router.post("/heartbeat", dependencies=[Depends(_require_auth)])
async def heartbeat():
    _state.touch_heartbeat()
    return {"ok": True}
```

### Endpoint contract (canonical — also used verbatim in the Part 2 issue)

| Method | Path | Direction | Body | Response |
|---|---|---|---|---|
| GET | `/api/plugins/hermes-switch-ui/connection` | backend→frontend | — | `{ gateway_port: 8642, dashboard_port: 9119, active_profile: str, enabled_plugins: [str], auth_mode: str }` |
| POST | `/api/plugins/hermes-switch-ui/register` | frontend→backend | manifest (below) | `{ ok: true, compat: { compatible: bool, warn: str\|null, plugin_range: str, frontend_version: str } }` |
| POST | `/api/plugins/hermes-switch-ui/settings` | frontend→backend | settings object | `{ ok: true }` |
| GET | `/api/plugins/hermes-switch-ui/status` | frontend polls | — | status object (below) |
| POST | `/api/plugins/hermes-switch-ui/heartbeat` | frontend pings | — | `{ ok: true }` |

**Register manifest body (POST /register):**
```json
{
  "version": "1.0.0",
  "url": "http://localhost:3002",
  "port": 3002,
  "hermes_api_url": "http://localhost:8642",
  "enabled_features": ["chat","dashboard","files","terminal","memory","matrix3d"]
}
```

**Status response (GET /status):**
```json
{
  "running": true,
  "last_heartbeat": "2026-06-11T12:00:00Z",
  "ttl_seconds": 90,
  "manifest": { "version": "1.0.0", "url": "http://localhost:3002", "port": 3002,
                "hermes_api_url": "http://localhost:8642", "enabled_features": ["..."] },
  "reported_settings": { "...": "..." },
  "compat": { "compatible": true, "warn": null, "plugin_range": ">=1.0.0,<2.0.0", "frontend_version": "1.0.0" }
}
```

### State file schema — `~/.hermes/switchui/state.json`
```json
{
  "schema_version": 1,
  "manifest": {
    "version": "1.0.0",
    "url": "http://localhost:3002",
    "port": 3002,
    "hermes_api_url": "http://localhost:8642",
    "enabled_features": ["chat","dashboard","files","terminal","memory","matrix3d"],
    "registered_at": "2026-06-11T11:59:00Z"
  },
  "last_heartbeat": "2026-06-11T12:00:00Z",
  "reported_settings": { "...": "..." }
}
```
- Path resolution: `SWITCHUI_STATE_PATH` env → else `~/.hermes/switchui/state.json`. Create `~/.hermes/switchui/` if absent (must NOT touch `workflows/`).
- Writes are atomic: write to `state.json.tmp` then `os.replace`.
- `register`, `settings`, and `heartbeat` all stamp `last_heartbeat = utcnow()`.

### Heartbeat / "running" semantics (no threads)
- `running` is computed at **read time**: `running = (utcnow() - last_heartbeat) < TTL`. TTL default **90s** (constant; configurable later).
- Frontend is expected to POST `/register` on startup and POST `/heartbeat` (or `/settings`, or any mutating call) on an interval shorter than TTL (recommend 30s) so `running` stays true.
- No background process polls; the plugin only computes freshness when `/status` or `switchui_status` is called.

### Version compatibility scheme — `_version_compat.py`
- Plugin declares a semver range it understands: `PLUGIN_RANGE = ">=1.0.0,<2.0.0"` (mirror `plugin.yaml: compatible_switchui`).
- On `/register`, parse `manifest.version`; `check()` returns `{compatible, warn, plugin_range, frontend_version}`.
- Implementation: use `packaging.specifiers.SpecifierSet` if `packaging` is importable; else a minimal hand-rolled `major.minor.patch` comparator (avoid adding deps — `packaging` is usually present transitively, but degrade gracefully). On mismatch, `warn` is a human string (e.g. `"SwitchUI 2.1.0 is outside supported range >=1.0.0,<2.0.0; some features may not sync."`); register still returns `ok: true` (non-fatal).

---

## Detailed TODOs

### Phase 1 — Scaffold
- [ ] Create `plugins/hermes-switch-ui/` with `plugin.yaml`, empty `__init__.py` stub exposing a no-op `register(ctx)`, `dashboard/`, `tests/`.
- [ ] Write `plugin.yaml` per sketch above.
- **Acceptance:** `python -c "import importlib.util" + spec_from_file_location` loads `__init__.py` without error; `register` is callable. Plugin appears in plugin loader logs when profile enables it.

### Phase 2 — Knowledge + tools + hook
- [ ] Author `capability.md`: SwitchUI features list, routes, ports (8642/9119/3002), env vars, connection guide, the 3 reference links.
- [ ] Implement `_knowledge.py`: `get_info(refresh)` loads `capability.md`, merges last live manifest from `_state` (live overrides static), and on `refresh=True` optionally fetches `SWITCHUI_DOCS_URL`/docs site (best-effort, swallow network errors). `connection_info()` returns ports + active profile + enabled plugins + auth mode (read from running config; in test context return static defaults).
- [ ] Implement `register(ctx)` with the `pre_llm_call` nudge + `switchui_info` + `switchui_status` tools.
- **Acceptance:** `switchui_info` returns merged doc (manifest fields override when present). `pre_llm_call` returns the one-paragraph nudge dict. Hook + 2 tools registered (assert in `test_register_contract.py`).

### Phase 3 — Sync API + persistence + compat
- [ ] Implement `_state.py`: path resolution, atomic read/write, `validate_manifest`, `validate_settings` (reject unknown huge payloads — cap raw body, e.g. 32KB; whitelist top-level keys; coerce types), `save_manifest`, `save_settings`, `touch_heartbeat`, `get_status` (TTL logic).
- [ ] Implement `_version_compat.py` (`check`, `PLUGIN_RANGE`).
- [ ] Implement `dashboard/plugin_api.py` with the 5 routes + `_require_auth` per sketch.
- **Acceptance:** Each endpoint returns the documented shape. `/register` with a >2.x version yields `compat.warn` non-null and `ok:true`. `/status` flips `running` to false once `last_heartbeat` is older than TTL. Oversized/invalid POST bodies return 422.

### Phase 4 — Tests (pytest)
- [ ] `test_register_contract.py`: load `__init__.py` via `spec_from_file_location`, call `register(fake_ctx)`, assert 1 hook + 2 tools captured; assert nudge text non-empty and single paragraph.
- [ ] `test_api_routes.py`: mirror karpathy harness — load `dashboard/plugin_api.py` flat via `spec_from_file_location`, mount `router` on a `fastapi.testclient.TestClient`, exercise all 5 routes (auth fallback no-ops in test). Use a temp `SWITCHUI_STATE_PATH`.
- [ ] `test_state.py`: roundtrip manifest/settings; atomic write leaves no `.tmp`; TTL boundary (fresh→running, stale→not running); validation rejects oversized/garbage.
- [ ] `test_version_compat.py`: in-range → compatible no warn; above/below range → warn string present, compatible false.
- **Acceptance:** `pytest plugins/hermes-switch-ui/tests/ -q` green. Tests do not require a running web server (auth fallback + temp state path).

### Phase 5 — Enablement + docs
- [ ] Add a `plugins` block to `/Users/rohits/.hermes/profiles/hermes-switch/config.yaml` (currently has none): `plugins:\n  enabled:\n    - hermes-switch-ui`. (Confirm exact key shape against another profile that enables a plugin, e.g. root `/Users/rohits/.hermes/config.yaml`.)
- [ ] Write `plugins/hermes-switch-ui/README.md`: purpose, endpoints table, state file, heartbeat/TTL, how to enable, tool descriptions.
- **Acceptance:** Starting the agent under `hermes-switch` profile loads the plugin (loader log) and mounts routes at `/api/plugins/hermes-switch-ui/`; `curl -s localhost:9119/api/plugins/hermes-switch-ui/status` (authed) returns the status object.

### Phase 6 — Part 2 spec + issue
- [ ] File the GitHub issue (markdown below) to `Interstellar-code/hermes-switchui` (remember: pass `--repo Interstellar-code/hermes-switchui`, do not let `gh` default to upstream).
- **Acceptance:** Issue created with the full spec body; contains exact endpoint paths/payloads from Phase 3 and the degraded-mode behavior.

---

## Security Notes
- All routes auth-gated via `Depends(_require_auth)` reusing `web_server._is_authenticated`; test/standalone fallback no-ops only when `hermes_cli.web_server` is not importable.
- `/register` and `/settings` are mutating and persisted: validate strictly — cap raw request body (~32KB), whitelist/coerce top-level keys, reject unexpected nested blobs. Never `eval`/exec reported content.
- State file is local user-scoped (`~/.hermes/switchui/state.json`); contains only frontend self-reported metadata + connection hints (no secrets — do NOT persist `HERMES_API_TOKEN`/passwords even if reported; strip them in `validate_settings`).
- `connection_info()` returns ports/profile/plugin names/auth-mode — non-secret operational metadata; do not include tokens.
- CORS already restricts to localhost/127.0.0.1 any port — no widening needed.

---

## Part 2 Spec — SwitchUI Settings Section (frontend, NOT implemented here)

Add `src/screens/settings/sections/section-hermes-plugin.tsx` to the existing `/settings` screen (`settings-screen.tsx`), modeled on the 16 existing `section-*.tsx` and the providers wizard precedent.

**The section displays:**
1. **Plugin status** — active / inactive, derived by calling `GET /api/plugins/hermes-switch-ui/status` (via the dashboard proxy at `9119`). `running` + `last_heartbeat` shown.
2. **Connection info** — from `GET /api/plugins/hermes-switch-ui/connection`: gateway port (8642), dashboard port (9119), active profile, enabled plugins, auth mode.
3. **SwitchUI's own reported settings** — echo back what the frontend last reported via `POST /settings` (round-trips through `status.reported_settings`).
4. **Version compatibility** — surface `status.compat.warn` as a banner when non-null.

**Frontend lifecycle wiring:**
- On app startup, `POST /register` with the manifest (version, url, port, hermes_api_url, enabled_features).
- On an interval (~30s, < 90s TTL), `POST /heartbeat` (or reuse any poll) so backend sees it as running.
- On settings change, `POST /settings`.

**Degraded mode (plugin inactive):** If `/status` or `/connection` 404s or fails (plugin not enabled on the backend profile), the section renders an informational "Hermes plugin not detected — enable `hermes-switch-ui` in your active profile" state, hides live fields, and does not error the settings screen. Poll backs off.

---

## Ready-to-file GitHub Issue (Part 2)

> File to `Interstellar-code/hermes-switchui` with `gh issue create --repo Interstellar-code/hermes-switchui ...`

```markdown
## Add Hermes Plugin settings section (`section-hermes-plugin.tsx`) + backend sync wiring

### Summary
The Hermes backend now ships a bundled plugin `hermes-switch-ui` that exposes a config-sync API and makes the agent aware of this frontend. SwitchUI should add a dedicated settings section that surfaces plugin status, connection info, our own reported settings, and version-compatibility — degrading gracefully when the plugin is not enabled on the active backend profile.

### Backend API contract (already implemented in hermes-agent)
Base path (via dashboard proxy on `:9119`): `/api/plugins/hermes-switch-ui/`

| Method | Path | Body | Response |
|---|---|---|---|
| GET | `/connection` | — | `{ gateway_port: 8642, dashboard_port: 9119, active_profile, enabled_plugins[], auth_mode }` |
| POST | `/register` | `{ version, url, port, hermes_api_url, enabled_features[] }` | `{ ok, compat: { compatible, warn, plugin_range, frontend_version } }` |
| POST | `/settings` | settings object (no secrets — tokens stripped backend-side) | `{ ok }` |
| GET | `/status` | — | `{ running, last_heartbeat, ttl_seconds, manifest, reported_settings, compat }` |
| POST | `/heartbeat` | — | `{ ok }` |

All routes are auth-gated (session cookie/token) — reuse the existing dashboard-proxy bearer injection.

### Tasks
- [ ] New `src/screens/settings/sections/section-hermes-plugin.tsx`, registered in `settings-screen.tsx`.
- [ ] On startup: `POST /register` with `{ version, url: <self>, port: 3002, hermes_api_url: <HERMES_API_URL>, enabled_features }`.
- [ ] Heartbeat: `POST /heartbeat` every ~30s (TTL is 90s backend-side) so the backend reports us as running.
- [ ] On settings change: `POST /settings` (omit `HERMES_API_TOKEN`/`HERMES_PASSWORD`).
- [ ] Render: plugin status (active/inactive from `/status.running` + `last_heartbeat`), connection info (`/connection`), reported settings (`/status.reported_settings`), compat banner (`/status.compat.warn`).

### Degraded mode
If `/status` or `/connection` returns 404/network error (plugin not enabled), show: "Hermes plugin not detected — enable `hermes-switch-ui` in your active backend profile." Hide live fields, do not error the settings screen, back off polling.

### Acceptance criteria
- [ ] Section visible in `/settings`; shows active + heartbeat when backend plugin enabled.
- [ ] Connection info reflects gateway 8642 / dashboard 9119 / active profile / enabled plugins.
- [ ] Version mismatch surfaces the `compat.warn` banner.
- [ ] With plugin disabled backend-side, section renders degraded state without breaking settings.
- [ ] No secrets sent in `POST /settings`.
```

---

## Success Criteria
- Plugin loads under the `hermes-switch` profile; routes mounted at `/api/plugins/hermes-switch-ui/`.
- `pre_llm_call` injects the one-paragraph nudge exactly once per session (first LLM call; subsequent calls return `None`); `switchui_info` / `switchui_status` return correct merged/live data.
- All 4 sync capabilities work: connection info, persisted settings report, TTL heartbeat/status, version-compat warning.
- `state.json` persists at `~/.hermes/switchui/state.json` without disturbing `workflows/`.
- `pytest plugins/hermes-switch-ui/tests/ -q` is green.
- README + profile enablement committed.
- Part 2 GitHub issue filed to `Interstellar-code/hermes-switchui`.

## Risks / Open Questions
- **`register_tool` exact signature** — confirm positional vs kwargs and whether handler receives the raw args dict + injected `task_id` (per project memory `registry.dispatch convention`: handler gets the whole args dict as 1st positional). Adapt sketches to live `hermes_cli/plugins.py` API.
- **`connection_info()` data source** — how the plugin reads active profile / enabled plugins / auth mode at runtime (config object vs web_server globals). Resolve during Phase 2; fall back to static in test context.
- **`packaging` availability** — prefer `SpecifierSet`; ship a minimal comparator fallback to avoid a hard new dependency.
- **Profile `plugins` key shape** — verify against a profile that already enables a plugin before editing `hermes-switch/config.yaml` (the profile currently has none).
- **`refresh` docs fetch** — networked refresh in `switchui_info` is best-effort/optional; default off to keep the tool deterministic and offline-safe.
- **Heartbeat ownership** — relies on the frontend (Part 2) polling; until Part 2 ships, `running` will read false (acceptable; `switchui_info` still works from static doc).
```

Open questions for this plan are also tracked at `.omc/plans/open-questions.md`.
