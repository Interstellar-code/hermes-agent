# mcp_lazy

Version: `0.2.0`

Lazy MCP tool schema loading for Hermes Agent. Cuts ~80% of per-turn token overhead from large MCP tool catalogs by sending stub schemas in the API request and promoting full schemas on demand.

Two discovery modes:

- **tool mode** (default) — one stub per MCP tool; promote individual tools with `load_mcp_tools`.
- **server mode** — one stub per MCP *server*; promote a server's whole tool set with `load_mcp_server`, then optionally drill into individual tools. Cuts the stub list further (one stub per server instead of per tool).

Tracks issue [Interstellar-code/hermes-agent#5](https://github.com/Interstellar-code/hermes-agent/issues/5).

---

## Why this exists

Hermes injects every registered MCP tool's full JSON schema into every API call. A typical deployment with 10 MCP servers (~300 tools) burns ~68K tokens per turn on tool schemas alone — about 35% of a 200K context window — before the user types anything. Smaller-context providers (Cerebras 64K TPM, Groq 12-30K TPM) can't fit a single request.

This plugin replaces MCP tool schemas with lightweight stubs (~80 tokens each, name + truncated description + sentinel marker) and provides a meta-tool the model calls to promote individual tools to full schemas when it needs them. Promoted tools stay full for the rest of the session.

---

## How it works (end to end)

```
┌─────────────┐                                ┌─────────────┐
│   User      │                                │  Model      │
│             │ ── "find files with X" ───────►│             │
└─────────────┘                                └─────────────┘
                                                       │
                                          sees tool list:
                                          - 38 builtin tools (full)
                                          - 304 MCP tools (STUBS)
                                                       │
                                                       ▼
                                          model picks tool from
                                          stub descriptions, but
                                          stub has no real params
                                                       │
                                                       ▼
                                          calls load_mcp_tools(
                                            tool_names=["mcp_trek_search_files"]
                                          )
                                                       │
        ┌──────────────────────────────────────────────┘
        │
        ▼
┌─────────────────────────────────────┐
│ load_mcp_tools handler:             │
│ 1. resolves agent via ContextVar    │
│ 2. validates against valid_tool_    │
│    names                            │
│ 3. promote_tools() adds name to     │
│    DeferredToolPool[session_id]     │
│ 4. returns JSON {ok, promoted,      │
│    rejected, note}                  │
└─────────────────────────────────────┘
                                                       │
                                          NEXT TURN:
                                                       ▼
                                          tool list rebuilt by hook
                                          - 38 builtin tools (full)
                                          - 1 promoted MCP tool (FULL)
                                          - 303 MCP tools (STUBS)
                                                       │
                                                       ▼
                                          model calls
                                          mcp_trek_search_files(
                                            pattern="**/*.py", ...
                                          ) with proper params
                                                       │
                                                       ▼
                                          real MCP tool executes,
                                          returns real data
```

---

## Architecture

### Two interception points

The plugin hooks the tool-list pipeline at two stages:

1. **`transform_tools`** — pre-request stubbing. Rewrites `agent.tools` before it enters the API call (chokepoint below).
2. **`pre_tool_call`** — mid-dispatch auto-promote. Fires when the model calls an MCP tool. If the tool is still a stub, it promotes the single tool and **blocks** dispatch with a "schema promoted; retry next turn" message instead of letting the stub call hit the MCP server and fail schema validation.

A third hook, **`on_session_reset`**, drops per-session pool state on `/new` / `/reset`.

### Single hook chokepoint

`agent/chat_completion_helpers.py:build_api_kwargs()` is the only place `agent.tools` flows into an API request (covers Anthropic / Bedrock / Codex / chat_completions transports). The plugin registers a `transform_tools` hook that fires here on every request:

```python
# in agent/chat_completion_helpers.py, ~line 235
tools_for_api = agent.tools
hook_results = invoke_hook("transform_tools",
    tools=tools_for_api, agent=agent, api_messages=api_messages)
for result in hook_results or []:
    if isinstance(result, list) and result:
        tools_for_api = result  # first non-empty list wins
        break
```

First non-empty list returned by any plugin wins. Failures are exception-isolated — a plugin bug cannot break the API call.

### Per-session pool

`plugins/mcp_lazy/pool.py:DeferredToolPool` holds per-session promotion state, keyed on `agent.session_id`. Each pool owns:
- `_promoted: set[str]` — names of promoted MCP tools
- `_lock: threading.RLock` — thread-safe mutation

Pools live in a module-level `weakref.WeakValueDictionary`, anchored by the agent's `_mcp_lazy_pool` attribute. When a session ends (`/new` or `/reset`), the agent reassigns and the old pool drops naturally.

Cross-session isolation is enforced: session A promoting `mcp_trek_search` does NOT cause session B to see the full schema.

### Schema-level stub detection

Hermes canonicalizes empty tool args to `"{}"` at `agent/conversation_loop.py:3103-3105`. That makes "empty args = stub call" indistinguishable from real zero-arg MCP tools like `mcp_zai_web_search_search`. To avoid that collision, the sentinel lives in the **schema** we registered, not the model's call:

```python
LAZY_SENTINEL = "__lazy_stub__"

# Stub parameters carry the sentinel; real schemas never do.
stub_params = {
    "type": "object",
    "properties": {
        LAZY_SENTINEL: {"type": "boolean", "const": True, "description": "Internal marker..."},
    },
    "additionalProperties": False,
}
```

### Promotion does NOT rebuild `agent.tools`

`agent.tools` stays the canonical full list at all times. Promotion only adds a name to the pool's `_promoted` set. The next `transform_tools` call reads the snapshot and substitutes stubs for everything *except* promoted names. This avoids cache-key / `valid_tool_names` / threading edge cases that a rebuild-based design would introduce.

### Meta-tool agent resolution

`registry.dispatch` does not natively forward the agent reference to tool handlers. The plugin works around this with a `ContextVar` (`pool._current_agent_var`) set in `transform_tools` and read by `load_mcp_tools.handler`. ContextVars propagate through `asyncio` tasks and `contextvars.copy_context()`, so the value is visible when the tool fires later in the same turn.

### Server-level stubs (server / both modes)

When `mcp.discovery_mode` is `"server"` or `"both"`, `server_stubs.py` collapses each MCP server into a single `mcp_server_<name>` stub instead of one stub per tool. `load_mcp_server` then promotes the server, which surfaces its per-tool stubs (or full schemas, with `eager=true`) on the next turn.

Stub discrimination uses a sentinel key (`__is_server_stub__`), **not** the name prefix. A real MCP server literally named `server` produces concrete tools like `mcp_server_foo` that also start with `mcp_server_` (issue #27); the sentinel is the authoritative discriminator and `valid_tool_names` membership disambiguates real tools from synthetic stubs.

---

## Configuration

### Master toggle (`config.yaml`)

```yaml
mcp:
  lazy_loading: true              # false | true | auto (default false)
  lazy_stub_max_desc: 200         # max chars of description per TOOL stub
  discovery_mode: tool            # tool | server | both (default tool)
  server_stub_max_desc: 150       # max chars of description per SERVER stub
  server_eager_token_threshold: 1500  # eager load_mcp_server above this cost degrades to tool stubs
  lazy_auto_threshold_tokens: 4000    # auto mode: pass through below this MCP schema cost
  lazy_evict_idle_turns: 10           # demote promoted tools idle this many requests (0 = never)
  lazy_evict_cost_threshold_tokens: 3000  # eviction only runs once promoted schemas cost this much
```

| Key | Default | Purpose |
|---|---|---|
| `mcp.lazy_loading` | `false` | Master toggle: `false`/`"off"` = disabled; `true`/`"on"` = always stub; `"auto"` = stub only when the eligible MCP schemas cost ≥ `lazy_auto_threshold_tokens` (pure pass-through below — plugin is a per-turn no-op). |
| `mcp.lazy_stub_max_desc` | `200` | Max description chars per per-tool stub. |
| `mcp.discovery_mode` | `"tool"` | `tool` = per-tool stubs; `server` = per-server stubs; `both` = both meta-tools registered. Invalid values fall back to `tool`. |
| `mcp.server_stub_max_desc` | `150` | Max description chars per server stub (server / both modes). |
| `mcp.server_eager_token_threshold` | `1500` | When `load_mcp_server(eager=true)` would cost more than this many tokens of full schemas, it silently degrades to tool stubs. |
| `mcp.lazy_auto_threshold_tokens` | `4000` | Auto mode only: minimum estimated token cost (chars/4) of stub-eligible MCP schemas before stubbing kicks in. |
| `mcp.lazy_evict_idle_turns` | `10` | Idle eviction: a promoted tool unused for this many LLM requests is demoted back to a stub. `0` disables eviction (pre-eviction behavior). Re-promotion costs one `load_mcp_tools` call or one auto-promote retry. |
| `mcp.lazy_evict_cost_threshold_tokens` | `3000` | Eviction gate: idle tools are only swept once the promoted set's full-schema cost exceeds this. Batches evictions so the tool list (and provider prompt-cache prefix) changes once per sweep, not once per tool. |

### Per-server override

```yaml
mcp_servers:
  trek:                           # uses master setting (lazy if master is on)
    command: npx
    args: [-y, '@trek/mcp-server']
  gmail:                          # forces eager (full schemas always)
    lazy: false
    command: npx
    args: [-y, '@gmail/mcp-server']
  dart:                           # custom server-stub description (server/both modes)
    description: "Task & doc management"
    command: npx
    args: [-y, '@dart/mcp-server']
```

`lazy: false` on a per-server entry opts that server's tools OUT of stubbing — they always get full schemas. A `description:` set on a `lazy: false` server is ignored (logged at INFO).

`mcp_servers.<name>.description` supplies the text shown in that server's `mcp_server_<name>` stub when `discovery_mode` is `server` or `both`.

### Phase 0 baseline logger toggle

```bash
# in ~/.hermes/.env
HERMES_MCP_LAZY_BASELINE=0       # disable the baseline cache hit-rate logger
```

The baseline logger runs independently of `mcp.lazy_loading` — it's passive telemetry that appends one JSONL row per API call to `~/.hermes/mcp-lazy/cache-baseline.jsonl`.

---

## The `load_mcp_tools` meta-tool

Registered when the plugin is enabled. Schema:

```json
{
  "name": "load_mcp_tools",
  "description": "Load full parameter schemas for MCP tools by name. Use when you need to call an MCP tool whose visible schema is a [LAZY] stub. After this call returns, the next turn will see the full parameter spec for each promoted tool; invoke the tool normally on that turn.",
  "parameters": {
    "type": "object",
    "properties": {
      "tool_names": {
        "type": "array",
        "items": {"type": "string"},
        "description": "List of MCP tool names to load full schemas for"
      }
    },
    "required": ["tool_names"]
  }
}
```

Returns JSON:
```json
{
  "ok": true,
  "promoted": ["mcp_trek_search_files", "mcp_dart_get_task"],
  "rejected": ["mcp_typo_name"],
  "note": "Full schemas will be visible on the next turn..."
}
```

Names not present in `agent.valid_tool_names` are silently dropped (returned in `rejected`).

> **Naming note**: meta-tool is `load_mcp_tools`, NOT `mcp_load_tools`. The `mcp_` prefix triggers our own stub filter — naming the tool `mcp_load_tools` would cause it to stub itself.

---

## The `load_mcp_server` meta-tool

Registered **only** when `mcp.discovery_mode` is `"server"` or `"both"`. Promotes one or more MCP servers by name; the next turn shows that server's per-tool stubs (or full schemas, with `eager=true`). Schema:

```json
{
  "name": "load_mcp_server",
  "description": "Load tool stubs for one or more MCP servers by name. Use when you see a server stub (mcp_server_<name>) and need to access its tools. After this call the next turn will show tool stubs for each promoted server. Pass eager=true to load full schemas immediately (use only when you need many tools from one server).",
  "parameters": {
    "type": "object",
    "properties": {
      "server_names": {
        "type": "array",
        "items": {"type": "string"},
        "description": "List of MCP server names to expand (e.g. ['trek', 'gmail'])."
      },
      "eager": {
        "type": "boolean",
        "description": "When true, promote to full schemas instead of tool stubs. Default false."
      }
    },
    "required": ["server_names"]
  }
}
```

Returns JSON:
```json
{
  "ok": true,
  "promoted": ["trek", "gmail"],
  "rejected": ["typo_server"],
  "available_next_turn": true,
  "note": "Tool stubs for the promoted servers will be visible on the next turn..."
}
```

`eager=true` is gated by `mcp.server_eager_token_threshold` (default 1500). If the server's full-schema cost exceeds the threshold, eager silently degrades to tool stubs — the server still promotes, just not to full schemas.

---

## Token economics

Counts are approximate; real numbers depend on tokenizer and per-tool description length.

| | Tokens per tool | × 304 tools |
|---|---|---|
| Full schema (eager mode) | ~225 | ~68,000 |
| Stub schema (lazy mode) | ~80 | ~24,000 |
| **Reduction** | **~64%** | **~44,000 tokens / turn** |

Per-conversation impact:

| Scenario | Tokens / turn | Vs eager |
|---|---|---|
| Pure chat, no MCP needed | ~24K (all stubs) | **-64%** |
| 1 MCP tool promoted | ~24K + 225 = ~24K | **-64%** |
| 5 MCP tools promoted | ~24K + 1.1K = ~25K | **-63%** |
| All MCP tools eventually used | ~68K | 0% (degenerate) |

Real conversations promote 2-5 tools per session → ~60% reduction holds steady.

### Server mode (`discovery_mode: server`)

Server mode collapses the baseline further: one stub per *server* (~80–150 tokens) instead of one per tool. For a 10-server / ~300-tool deployment that's roughly **~800–1,500 tokens total** for the resting stub list, versus ~24,000 in tool mode and ~68,000 eager. The model expands a server with `load_mcp_server` only when it needs that server's tools.

---

## Files

| Path | Purpose |
|---|---|
| `__init__.py` | Plugin entry point; `register(ctx)` registers meta-tool + hooks; Phase 0 baseline logger installs at import |
| `pool.py` | `DeferredToolPool`, `get_pool(session_id)`, `evict(session_id)`, `_current_agent_var` ContextVar |
| `stubs.py` | `LAZY_SENTINEL`, `make_stub_schema`, `is_stub_schema`, `is_mcp_tool`, `mix_full_and_stubs` |
| `server_stubs.py` | Server-level stub construction, `__is_server_stub__` sentinel detection, tool-name derivation |
| `promote.py` | `promote_tools(agent, names)` + `promote_server_tools(agent, servers, eager=)` — pool promotion with `valid_tool_names` filter |
| `meta_tool.py` | `SCHEMA` + `handler` for `load_mcp_tools` |
| `meta_tool_server.py` | `SCHEMA` + `handler` for `load_mcp_server` (registered in server/both modes) |
| `hook_impl.py` | `transform_tools` + `pre_tool_call` + `on_session_reset` callbacks; `_eligible_servers` / `_server_descriptions` config readers |
| `baseline_patch.py` | Phase 0 cache hit-rate logger — appends to `~/.hermes/mcp-lazy/cache-baseline.jsonl` |
| `scripts/cache_report.py` | Summariser CLI for the baseline log |
| `plugin.yaml` | Hermes plugin manifest |

---

## Core changes (outside plugin)

Two small additions to Hermes core enable the plugin without monkey-patching:

| File | Change | Purpose |
|---|---|---|
| `hermes_cli/plugins.py` | Added `transform_tools` to `VALID_HOOKS` | Generic hook surface — other plugins can also rewrite the tool list (cost-cap injection, telemetry tagging, etc) |
| `agent/chat_completion_helpers.py:235` | Fire `transform_tools` hook before tools enter API kwargs | Single chokepoint; exception-isolated |
| `agent/usage_pricing.py` | `register_usage_observer` slot | Phase 0 baseline plugin attaches without inverting dependency direction |

Both changes are generic primitives — no MCP-specific logic in core.

---

## Lifecycle

### Plugin load
1. Hermes loader imports `hermes_plugins.mcp_lazy` (Python package)
2. Module init calls `baseline_patch.install()` — Phase 0 observer attaches to `agent.usage_pricing`
3. Loader calls `register(ctx)` — meta-tool + hooks registered with `PluginContext`

### Per request
1. `build_api_kwargs(agent, api_messages)` reads `agent.tools`
2. Fires `invoke_hook("transform_tools", tools=..., agent=..., api_messages=...)`
3. `hook_impl.transform_tools` checks `mcp.lazy_loading`:
   - If off → returns `None` → original tools used
   - If on → gets `pool = get_pool(agent.session_id)`, attaches to `agent._mcp_lazy_pool`, sets ContextVar
   - Calls `mix_full_and_stubs(tools, promoted_names=pool.snapshot(), lazy_servers=_eligible_servers())`
   - Returns the mixed list
4. Tools flow into the API call

### Promotion
1. Model calls `load_mcp_tools(tool_names=[...])`
2. `meta_tool.handler` resolves agent (kwargs first, then ContextVar)
3. `promote_tools(agent, names)` filters against `valid_tool_names`, calls `pool.promote(accepted)`
4. Returns JSON status
5. Next request's `transform_tools` sees the larger `pool.snapshot()` → those tools come back FULL

### Session reset (`/new` or `/reset`)
1. `cli.py:5980` fires `on_session_reset` hook
2. Plugin's `hook_impl.on_session_reset` runs (no-op by default; WeakValueDictionary GC handles cleanup)
3. Old pool dropped when no strong refs remain

---

## Operations

### Enable

```
hermes plugins enable mcp_lazy
```
Add to `config.yaml`:
```yaml
mcp:
  lazy_loading: true
  # discovery_mode: server   # optional — per-server stubs + load_mcp_server
```
Restart:
```
hermes gateway restart
```

Look for in `~/.hermes/profiles/<profile>/logs/agent.log`:
```
INFO hermes_plugins.mcp_lazy: mcp_lazy: discovery_mode='tool', registered: ['load_mcp_tools']
```
In server / both mode the registered list also includes `load_mcp_server`.

### Verify in a live session

Watch agent.log during a conversation:
```
grep -E "load_mcp_tools|load_mcp_server|mcp_lazy|tool .*(completed|started)" \
     ~/.hermes/profiles/<profile>/logs/agent.log | tail -20
```

You should see:
- `tool load_mcp_tools completed (Xms, Y chars)` — model promoted tools
- `tool mcp_<server>_<tool> completed (...)` next turn — promoted tool actually invoked

### Check cache hit rate

```
cd ~/.hermes/hermes-agent
venv/bin/python -m plugins.mcp_lazy.scripts.cache_report
```

Reports pooled hit rate, per-request mean/median, and the recommended Phase 1 promotion strategy from the original plan (since Phase 1 ships defaulting to in-place promotion, this is now diagnostic only).

### Disable

```yaml
# config.yaml
mcp:
  lazy_loading: false
```
Restart. Plugin stays loaded (Phase 0 logger keeps running) but `transform_tools` returns `None` and tools flow through unchanged.

To fully disable the plugin (including the Phase 0 logger):
```
hermes plugins disable mcp_lazy
```

---

## Edge cases & known behaviour

### Model calls a stub directly without promoting first
Handled by the `pre_tool_call` hook (CRITICAL #1). When the model calls a stubbed MCP tool, the hook auto-promotes that **single** tool (not the whole server) and blocks dispatch with `[mcp_lazy] Tool ... was a stub — full schema promoted. Reissue the call on the next turn`. The stub call never reaches the MCP server, so there is no schema-validation error to recover from.

### `mcp_server_<name>` prefix collision (#27)
A real MCP server named `server` produces concrete tools (`mcp_server_foo`) whose names start with `mcp_server_`, colliding with synthetic server-stub names. The plugin checks `valid_tool_names` membership — names present there are real tools and fall through to normal per-tool handling; names absent are treated as discovery stubs.

### Calling a server stub after the server is already promoted
If the model calls `mcp_server_<name>` after that server was promoted, `pre_tool_call` blocks with a message listing the concrete `mcp_<server>_<tool>` names to use instead (stale-stub guard, #31).

### Mid-session `discovery_mode` flip
If `discovery_mode` changes while a session is live, `transform_tools` logs a WARNING and preserves promoted-server state. The previous mode is tracked on the pool (`_prev_mode`) so it clears on session evict — no module-level leak (#29).

### Model promotes too aggressively
Each promoted tool stays full for the rest of the session. A model that calls `load_mcp_tools` with every visible name effectively disables the savings. Mitigated by the description ending in: "After this call returns, the next turn will see the full parameter spec" — encourages targeted promotion.

### Cache invalidation churn
Promotion changes the tools array shape. Anthropic prompt cache is content-hashed; a tools-array change invalidates the cached prefix for that segment. With deferred promotion (default), the change happens at turn boundary so the new prefix gets cached on the next turn. Phase 0 baseline logger captures the actual cache impact in production.

### Subagent inheritance
Currently subagents inherit the parent's `session_id`, which means they share the parent's `DeferredToolPool`. Parent's promoted tools are visible to the child. If isolated behaviour is desired in future, derive a per-subagent session_id (e.g. `f"{parent.session_id}.sub.{uuid}"`).

### Multi-tool promotion in one call
`load_mcp_tools(tool_names=[a, b, c])` is fully supported. All three promote in the same pool call; all three appear full on the next turn.

---

## Tests

The suite lives at `tests/plugins/mcp_lazy/` (repo `tests/` tree, not in the plugin directory) — 19 test modules covering pool isolation, concurrent promotion, stub mixing, server stubs, mode transitions, eager threshold, config validation, and regression tests for issues #18 / #27 / #29 / #30 / #31.

```
cd ~/.hermes/hermes-agent
venv/bin/python -m pytest tests/plugins/mcp_lazy/ -q
```

## Plan history

Plan went through 4 iterations + 5 Codex adversarial reviews before any code was written. Each iteration surfaced real architectural surprises in Hermes internals:

- v1: assumed `agent.messages` existed (it doesn't)
- v2: assumed `extract_cache_stats` was production code (it's dead)
- v3: assumed cache invalidation triggered `agent.tools` rebuild (it doesn't)
- v4: line-verified every assumption against current source

Final plan at `.omc/plans/mcp-lazy-loading-v4.md` (in repo root, not committed by default).

## Roadmap

| Phase | Status | Description |
|---|---|---|
| 0 | ✅ shipped (#6) | Baseline cache hit-rate instrumentation |
| 1 | ✅ shipped (#7) | Two-pass MVP: stub schemas + `load_mcp_tools` meta-tool |
| 2 | ✅ shipped | Server-level discovery: `discovery_mode` (tool/server/both), `mcp_server_<name>` stubs, `load_mcp_server` meta-tool, `pre_tool_call` auto-promote |
| 3 | planned | BM25 pre-selection on top of stubs — skips the round-trip for most turns |
| 4 | descoped | Anthropic native `tool_search_tool_bm25_20251119` — fork-local experiment only |

Upstream plan: one standalone PR adding the generic `transform_tools` hook surface to NousResearch/hermes-agent. Plugin itself stays fork-local indefinitely if upstream doesn't merge.
