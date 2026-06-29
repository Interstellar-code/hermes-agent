# Matrix Memory

Matrix Memory is a local-first Hermes memory provider plugin.

## Scope

- Tier 1 episodic facts via `MEMORY.md` / `USER.md`
- Tier 2 markdown wiki under `$HERMES_HOME/matrix-memory/wiki/`
- Tier 3 SQLite FTS5 recall under `$HERMES_HOME/matrix-memory/memory.db`
- 5 baseline tools in all sessions
- 9 additional read/audit tools in chat-mode sessions
- Bundled skill docs under `skills/matrix-memory/`

## Notes

- The provider is configured through `hermes memory setup matrix-memory`.
- In chat mode, write tools default to `dry_run=true`; destructive applies require a `confirm_token`.
- The bundled skill is registered as a plugin skill when the plugin context supports `register_skill`.
