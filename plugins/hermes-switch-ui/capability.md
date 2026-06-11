# SwitchUI — Hermes Browser Frontend

SwitchUI (`hermes-switchui`) is the primary browser frontend for the Hermes AI agent backend.

## Repository

<https://github.com/Interstellar-code/hermes-switchui>

Local checkout (development): `/Volumes/Ext-nvme/Development/hermes-switchui`

## Technology Stack

- **Framework**: React 19 + TanStack Start/Router
- **Backend-for-Frontend**: Hono BFF
- **State management**: Zustand
- **Styling**: Tailwind v4
- **Build**: Vite, pnpm
- **Optional**: Electron wrapper for desktop deployment

## Ports

| Service | Default Port | Config |
|---------|-------------|--------|
| SwitchUI BFF (Hono) | `3002` | `PORT` env var |
| Hermes API gateway | `8642` | `HERMES_API_URL` |
| Hermes dashboard | `9119` | `HERMES_DASHBOARD_URL` → `/api/dashboard-proxy/$` |

## Environment Variables

| Variable | Purpose |
|----------|---------|
| `HERMES_API_URL` | Full URL to Hermes API gateway (e.g. `http://localhost:8642`) |
| `HERMES_API_TOKEN` | Bearer token for gateway authentication |
| `HERMES_DASHBOARD_TOKEN` | Bearer token for dashboard API access |
| `PORT` | SwitchUI BFF listen port (default `3002`) |
| `HERMES_PASSWORD` | Password for UI login (if auth enabled) |

Workspace overrides can be set in `~/.hermes/workspace-overrides.json`.

## Features

- **Chat**: Full conversation interface with the Hermes agent
- **Dashboard**: Live agent metrics, plugin status, and configuration panels
- **Files**: File browser and editor for workspace files
- **Terminal**: Integrated terminal access
- **Memory**: View and manage agent memory / context
- **Matrix3D**: 3D visualisation of A2A fleet conversations and agent relationships

## Dashboard Proxy

SwitchUI reaches the Hermes dashboard server at port `9119` via a proxy path:
`/api/dashboard-proxy/$` — all dashboard API calls are routed through the BFF.

## State File

The plugin uses `~/.hermes/switchui/state.json` to persist frontend-reported metadata.
This path is separate from `~/.hermes/switchui/workflows/` used by the workflow-engine plugin.

## Heartbeat / TTL

SwitchUI sends `POST /api/plugins/hermes-switch-ui/heartbeat` on a ~30 s interval.
The backend treats SwitchUI as "running" while `last_heartbeat` is within the TTL window (90 s).
No daemon thread or background process is used; freshness is computed on-demand at read time.
