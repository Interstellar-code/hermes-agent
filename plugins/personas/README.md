# personas

Backend plugin that makes **personas a first-class Hermes capability**. It owns the
canonical library of curated persona templates, exposes them as runtime tools on every
platform (CLI, Telegram, Discord, gateway), and serves a REST API so the
[SwitchUI](https://github.com/Interstellar-code/hermes-switchui) profile wizard consumes
them as a thin client instead of shipping its own copy.

---

## Purpose

A **persona** is a specialized lens overlaid on an agent's stable identity (SOUL.md) —
"Security Engineer", "Migration Specialist", "Performance Engineer" — not a replacement
for it.

Previously the persona templates lived in the SwitchUI frontend repo. That created three
problems this plugin fixes:

1. **Wrong ownership** — personas are applied at runtime by Hermes agents, but a frontend
   owned them; a CLI/Telegram/Discord user had no access.
2. **Drift** — the wizard *copied* prompt text into each new profile's `config.yaml`; template
   edits never reached existing profiles. Two sources of truth.
3. **Fragile path** — runtime use required knowing an external-drive asset path.

This plugin makes the backend `library/` the single source of truth.

---

## Architecture

```
┌─────────────────────────────────────────────┐
│  Hermes agent (port 8642 gateway)           │
│                                             │
│  __init__.py  register(ctx):                │
│    register_hook(pre_llm_call)   ← resolves agent.persona_ref → trusted overlay
│    register_tool(persona_list)   ← metadata (id/name/category/...)
│    register_tool(persona_get)    ← full system_prompt
│    register_tool(persona_apply)  ← composed overlay for delegate_task
│                                             │
│  _library.py  ← load + validate + cache library/*.md
│  dashboard/plugin_api.py  ← REST at /api/plugins/personas/
│  library/*.md  ← 20 canonical persona templates (flat)
└─────────────────────────────────────────────┘
```

---

## Capabilities

### 1. Runtime tools (toolset `personas`)

- **`persona_list(category?)`** — persona metadata only (no full prompt). Optional category filter.
- **`persona_get(persona_id)`** — full persona including the `system_prompt` overlay text.
- **`persona_apply(persona_id, target="delegate")`** — composed overlay + metadata formatted
  for a `delegate_task` goal/context block (the ephemeral T3 path). Does **not** mutate config.

### 2. `persona_ref` binding hook

A *promoted* profile sets `agent.persona_ref: <persona-id>` in its `config.yaml`. The
`pre_llm_call` hook resolves it and injects the overlay as **trusted, developer-tier**
context (`{"context": ..., "target": "developer"}`) appended to the effective system prompt —
**never the user message** (the #140-safe contract). With no `persona_ref` set the hook returns
`None`, so the cached system prefix stays byte-stable.

### 3. REST API

Auto-mounts at `/api/plugins/personas/` (driven by `dashboard/manifest.json`'s `"api"` key).
All routes require auth.

| Method | Path | Returns |
|---|---|---|
| GET | `/list?category=` | `{ personas, count }` — metadata, optional filter |
| GET | `/get?id=` | `{ persona }` — full incl. `system_prompt`; 404 if unknown |
| POST | `/promote` | 501 — reserved (deferred follow-up) |

---

## Library format

Flat markdown files under `library/*.md`. **20 personas, 8 categories**
(design 2, devops 2, engineering 4, leadership 4, product 2, research 2, testing 2, writing 2).

- **YAML frontmatter**: `id, category, glyph, name, description, tags`,
  `default_model, default_memory_provider, suggested_mcps, suggested_toolsets`
- **Markdown body**: the `system_prompt` overlay text

Malformed files are skipped with a logged warning (never fatal). A duplicate `id` is non-fatal too: the first file (sorted) wins and the later duplicate is skipped with a logged error, so a stray copy can never brick plugin startup.

---

## Enabling

Tools/hook load only when the plugin is listed in the active profile's
`config.yaml` under `plugins.enabled`:

```yaml
plugins:
  enabled:
    - personas
```

The REST API and dashboard discovery mount independently of that list.

---

## Tests

```
venv/bin/python -m pytest plugins/personas/tests/ -q
```

Covers library parity (20 files / 8 categories), register contract (3 tools + 1 hook),
the `pre_llm_call` trusted-target invariant, and the REST routes.

---

## Status & follow-up

Build 1 (this plugin) ships the store, tools, hook, and read API. Tracked by issue #143.

Deferred: refactor the SwitchUI wizard to consume `/list` + `/get`, write `agent.persona_ref`
on promotion (consuming the dormant hook), implement real `POST /promote`, and delete
SwitchUI's `assets/personas/curated/` copy.
