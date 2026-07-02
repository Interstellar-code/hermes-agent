# Domain Pack: Plugin / Skill Authoring (Hermes)

This pack ADDS stack-specific context for authoring Hermes plugins and OMC
skills. It does NOT override the active role's contract, output format, or
severity rubric. Apply it alongside your role persona — treat it as an extra
checklist lens.

## Stack context this pack adds

- **`register(ctx)` entrypoint** — the sole plugin entrypoint; must be
  synchronous, must not raise, must not perform I/O or side effects beyond
  registering hooks and commands. All registration calls use `ctx.*` methods
  (``register_hook``, ``register_command``, ``register_tool``).
- **`plugin.yaml` manifest** — `name`, `version` (semver string), `description`,
  `author`, `kind` (`standalone` | `integration`), `hooks`, `provides_tools`,
  `requires_env`. Manifest is doc-only; `register()` is authoritative for
  what actually runs.
- **Hooks (sync + defensive)** — hooks receive `**kwargs`, must never raise,
  must complete quickly (no blocking I/O on the hot path), return `None` to
  no-op or a string to inject. Defensive pattern: wrap body in
  `try/except Exception` and log at DEBUG level.
- **`SKILL.md` conventions** — skill files live under `.omc/skills/<name>.md`;
  front-matter: `name`, `description`, `triggers` (keyword list), `version`;
  body: role, constraints, step-by-step procedure, examples. Skills are
  invoked via `/oh-my-claudecode:<name>`.
- **Lifecycle** — plugins load once at startup; hooks fire per-turn; state
  shared across turns lives in the bridge / shared memory, NOT in module
  globals unless they are constants.
- **Testing** — unit-test hooks in isolation by calling them directly with
  kwargs dicts; integration-test via `PluginManager` + `register(ctx)` +
  `pm.invoke_hook(...)` (see `test_integration_loader.py` pattern).

## Common pitfalls to flag

- Raising in a hook (crashes the Hermes turn).
- Blocking I/O (network, disk) inside `pre_llm_call` (blocks every turn).
- Storing mutable state in module globals (shared across all sessions).
- `register()` doing work beyond registration (side effects, I/O).
- `plugin.yaml` version string not quoted (YAML interprets `0.4.0` as float
  in some parsers — always use `"0.4.0"`).
- Hooks that return a non-None value when no injection is intended (can
  accidentally override the assistant response).
- Skills without `triggers` front-matter (won't auto-detect invocations).
- Missing defensive `try/except` around hook body (one exception silences
  all subsequent hooks for that event).
