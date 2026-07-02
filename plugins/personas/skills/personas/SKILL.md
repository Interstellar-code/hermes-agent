# personas

Canonical persona store for Hermes. Owns the 20 curated persona templates as the
single source of truth, exposes them as runtime tools on every platform, and binds
promoted profiles to a persona via `agent.persona_ref` — no copy, no drift.

## What a persona is

A persona is a **specialized lens overlaid on an agent's stable identity** (SOUL.md),
not a replacement for it. "Security Engineer", "Migration Specialist", "Performance
Engineer" are personas applied on top of the same underlying agent.

## Runtime tools

- `persona_list(category?)` — persona metadata (id, name, category, tags, suggested model/mcps/toolsets). Optional category filter.
- `persona_get(persona_id)` — full persona including the `system_prompt` overlay text.
- `persona_apply(persona_id, target="delegate")` — composed overlay + metadata, formatted for injection into a `delegate_task` goal/context block (the ephemeral T3 path). Does **not** mutate config.

## persona_ref binding (promoted profiles)

A promoted profile sets `agent.persona_ref: <persona-id>` in its `config.yaml`. The
plugin's `pre_llm_call` hook resolves it and injects the overlay as a **trusted**
system addition (`target="developer"`), appended to the effective system prompt —
never the user message. With no `persona_ref` set, the hook is a no-op (cache stays warm).

## REST API

`/api/plugins/personas/` (auth required):
- `GET /list?category=` → `{ personas, count }`
- `GET /get?id=` → `{ persona }`
- `POST /promote` → 501 (reserved; deferred follow-up)

## Library

Flat markdown files under `library/*.md`. YAML frontmatter
(`id, category, glyph, name, description, tags, default_model,
default_memory_provider, suggested_mcps, suggested_toolsets`) + markdown body =
`system_prompt`. Malformed files are skipped with a warning; duplicate ids are non-fatal (first wins, later skipped with an error).
