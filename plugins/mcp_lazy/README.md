# mcp_lazy

Lazy MCP tool schema loading for Hermes Agent. **Phase 0** of the project: passive cache-hit-rate logger only.

## Phase 0 scope (this commit)

A single observer callback registered with `agent/usage_pricing.py:normalize_usage()`. Every canonical usage record (Anthropic, Codex, Chat Completions) is appended to:

```
~/.hermes/mcp-lazy/cache-baseline.jsonl
```

No tool list mutation. No schema stubs. No conversation impact. The data feeds the Phase 1 design decision (immediate vs deferred promotion).

## Enable

```
hermes plugins enable mcp_lazy
```

Disable Phase 0 logging without disabling the plugin:

```
HERMES_MCP_LAZY_BASELINE=0
```

in `~/.hermes/.env`.

## Inspect

```
python -m plugins.mcp_lazy.scripts.cache_report
```

Prints pooled and per-request hit rates, plus the recommended Phase 1 promotion strategy.

## Roadmap

| Phase | Status | Description |
|---|---|---|
| 0 | **this commit** | Baseline cache hit-rate instrumentation |
| 1 | planned | Two-pass MVP: stub schemas + `mcp_load_tools` meta-tool + auto-promote |
| 2 | planned | BM25 pre-selection on top of stubs |
| 3 | descoped | Anthropic native `tool_search` mode (fork-local experiment) |

Full plan: `.omc/plans/mcp-lazy-loading-v4.md`.

Tracks issue Interstellar-code/hermes-agent#5.
