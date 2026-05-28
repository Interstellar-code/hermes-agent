# Hermes Dashboard Plugin — FastAPI Router Registration Guide

How dashboard plugins expose HTTP endpoints on the Hermes gateway.

## Discovery

`_discover_dashboard_plugins()` in `hermes_cli/web_server.py` scans plugin directories for `manifest.yaml` or `manifest.json` files that contain a top-level `api` key.

## Manifest Format

Create `manifest.yaml` (or `manifest.json`) in the plugin directory:

```yaml
name: my_plugin
version: "1.0.0"
description: "What this plugin does"
author: Your Name
api: dashboard/plugin_api.py    # Python file relative to plugin root
# Optional:
dashboard: dashboard/            # Frontend assets directory (if any)
icon: assets/icon.svg
```

The `api` key must point to a Python file that:
1. Exposes a module-level `router` attribute (a `fastapi.APIRouter`)
2. Can be loaded via `importlib.util.spec_from_file_location` (no package imports required)
3. Is fully self-contained — no relative imports (use `sys.path` hack if needed)

## Mount Point

Routes are mounted under `/api/plugins/<plugin_name>/`:

```python
# In _mount_plugin_api_routes():
app.include_router(router, prefix=f"/api/plugins/{plugin['name']}")
```

So a plugin named `a2a_fleet` with route `@router.get("/health")` becomes:
`GET /api/plugins/a2a_fleet/health`

## Loading Mechanism

```python
# web_server.py line ~4355
spec = importlib.util.spec_from_file_location(module_name, api_path)
mod = importlib.util.module_from_spec(spec)
sys.modules[module_name] = mod          # Register BEFORE exec for pydantic forward refs
spec.loader.exec_module(mod)
router = getattr(mod, "router", None)   # Must expose module-level `router`
app.include_router(router, prefix=f"/api/plugins/{plugin['name']}")
```

## What Works

- FastAPI dependency injection
- Pydantic models for request/response validation
- SSE (StreamingResponse with `text/event-stream`)
- WebSocket routes
- Background tasks (asyncio)
- Middleware on the router

## What Doesn't Work

- Access to the running agent instance (the gateway is a separate process)
- Agent hooks (transform_tools, pre_tool_call, etc.) — those are agent-plugin concepts
- Direct imports from `run_agent.py` or `cli.py` (different process)

## Pattern: Engine Bootstrap

The workflow-engine plugin shows the pattern for heavy initialization:

```python
# plugins/workflow-engine/dashboard/plugin_api.py
import sys as _sys
from pathlib import Path as _Path
_PLUGIN_DIR = _Path(__file__).resolve().parent.parent  # plugins/workflow-engine/
if str(_PLUGIN_DIR) not in _sys.path:
    _sys.path.insert(0, str(_PLUGIN_DIR))

from engine import WorkflowEngine
from _shared import get_engine

_engine: WorkflowEngine = get_engine()
router = APIRouter()

@router.get("/health")
async def health():
    return {"ok": True}
```

## Key Source Files

| File | Line | Purpose |
|---|---|---|
| `hermes_cli/web_server.py` | ~4008 | `_get_dashboard_plugins()` — discovery |
| `hermes_cli/web_server.py` | ~4340 | `_mount_plugin_api_routes()` — router mounting |
| `hermes_cli/web_server.py` | ~4384 | Mount call (before SPA catch-all) |

## A2A Fleet Plugin Mount Points

For the A2A fleet plugin, routes will be:

```
GET  /api/plugins/a2a_fleet/health                    → Health check
GET  /api/plugins/a2a_fleet/.well-known/agent-card     → Agent Card (or redirect to /.well-known/)
POST /api/plugins/a2a_fleet/jsonrpc                    → JSON-RPC 2.0 endpoint
GET  /api/plugins/a2a_fleet/sse/{task_id}              → SSE streaming for task updates
GET  /api/plugins/a2a_fleet/agents                     → List fleet agents (from config)
GET  /api/plugins/a2a_fleet/agents/{name}/card         → Cached Agent Card for named agent
GET  /api/plugins/a2a_fleet/tasks                      → List tasks
GET  /api/plugins/a2a_fleet/tasks/{task_id}            → Get task state
POST /api/plugins/a2a_fleet/tasks/{task_id}/cancel     → Cancel task
```

Note: The well-known URI (`/.well-known/agent-card.json`) would need a top-level route rewrite or redirect. For MVP, the Agent Card lives under the plugin prefix.
