# Lazy MCP Tool Schema Loading — Implementation Plan v4

**Issue**: Interstellar-code/hermes-agent#5
**Status**: v4 — all line numbers DIRECTLY verified by reading source (no subagent intermediary)
**Replaces**: v3 (kept for history). v3's investigator-supplied facts were wrong on 4 points.
**Target**: Build in Interstellar fork, soak, then submit ONE generic-hook PR upstream.

---

## v4 changes vs v3 (Codex-verified)

| Codex v3 finding | v3 said | v4 does |
|---|---|---|
| B1 (Phase 0 patch is no-op) | Patch `anthropic.py:150 extract_cache_stats` | **Patch `agent/usage_pricing.py:698`** (the real production canonicaliser that ALL providers flow through, called by usage tracking + cost calc). Plus zero-div guard on denominator. |
| A1 (`/reset` exists) | "Only `/new` exists" | **`/reset` is alias of `/new`** at `hermes_cli/commands.py:66` (`aliases=("reset",)`). Both routes invoke `new_session()` at `cli.py:5887`. Cleanup at `cli.py:5900` covers both naturally. |
| A2 (grep test misses real path) | Grep for `api_kwargs["tools"] =` | Grep for **`\.tools\s*=\s*get_tool_definitions`** + assert callers in 4-element allowlist (`agent_init.py:802`, `cli.py:9604`, `acp_adapter/server.py:708`, plugin's own promotion path). Catches all real injection sites. |
| A3 (`agent.messages` fictional) | "Mention-mine from `agent.messages`" | **No `agent.messages` attribute exists.** Mention-mining sources `api_messages` parameter (full history in API shape — content is preserved, only the envelope is formatted). For Anthropic shape, walk `[{role, content}]`; for OpenAI shape, same. Single helper handles both. |
| A4 (cache invalidation doesn't trigger rebuild) | "Bump cache key → next call rebuilds" | **Explicit rebuild copying `cli.py:9604-9612` precedent**: `agent.tools = get_tool_definitions(..., lazy_promoted=pool.promoted)` AND `agent.valid_tool_names = {t["function"]["name"] for t in agent.tools}`. NOT a cache-key trick. |
| C2 (`agent.messages` at call site) | "Helper accesses agent.messages" | Helper accesses `api_messages` parameter (already in scope, content preserved). |
| C3 (`extract_cache_stats` zero prod callers) | "Patch extract_cache_stats" | `extract_cache_stats` confirmed unused in prod (3 transport defs, zero callers). Patch moved to `usage_pricing.py:698` where canonical usage is built. |

---

## Verified architecture (read directly from source)

### Cache token flow (real, not the dead path)
```
Anthropic response → usage_pricing.py:698-727 (canonicalisation, build CanonicalUsage)
                  → plugin_llm.py:497-516 (alternate path for plugin-routed LLMs)
                  → plugins/observability/langfuse/__init__.py:503-866 (exports)
```
**Phase 0 patch point: `agent/usage_pricing.py:698`** — runs for every Anthropic call in production.

`agent/transports/anthropic.py:150 extract_cache_stats()` — **dead code path**. 3 transport-level defs, zero production callers. Don't patch it.

### Tool list mutation precedent
**`cli.py:9604-9612`** — exact pattern used by MCP server refresh today:
```python
self.agent.tools = get_tool_definitions(
    enabled_toolsets=self.agent.enabled_toolsets if hasattr(self.agent, "enabled_toolsets") else None,
    quiet_mode=True,
)
self.agent.valid_tool_names = {
    tool["function"]["name"] for tool in self.agent.tools
} if self.agent.tools else set()
# Appends user-visible "tools changed" message to conversation_history
```
Promotion in our plugin copies this verbatim, passing additional `lazy_promoted=frozenset(pool.promoted)` kwarg into `get_tool_definitions`.

### Single hook point
**`run_agent.py:3451 _build_api_kwargs(self, api_messages: list) -> dict`** — agent method called from EXACTLY 3 production sites:
- `agent/conversation_loop.py:926` (main turn dispatch)
- `agent/chat_completion_helpers.py:968` (codex_kwargs)
- `agent/chat_completion_helpers.py:1050` (codex_kwargs summary/retry)

Patching the method body covers all 3 call sites automatically — single chokepoint confirmed.

### Session id sources
- `hermes_cli/cli.py:2807-2809` (initial), `:5906-5907` (new_session), `:6305-6306` (branch)
- Format: `f"{timestamp_str}_{short_uuid}"`, guaranteed unique
- Pool key: `agent.session_id` directly

### Session boundary cleanup
- `/new` handler: `hermes_cli/cli.py:7860` → `new_session(...)` at `:7869`
- `/reset` alias declared at `hermes_cli/commands.py:66` → routes to same `new_session()`
- `new_session()` body at `cli.py:5887-5980`; OLD session ends at `:5900` (`_session_db.end_session()`); NEW id at `:5906-5907`
- Pool eviction insert: **`cli.py:5900`** via `pre_session_reset` hook (new) OR direct attribute access pattern (no hook needed if we keep cleanup inside our plugin's own state tracking)

### Background review session derivation
`agent/background_review.py:427-428` — current line `review_agent.session_id = agent.session_id` → mutate to `f"{agent.session_id}.review.{uuid4().hex[:8]}"` for fresh per-review pool.

### Args canonicalisation gotcha
`agent/conversation_loop.py:3103-3105` — empty/whitespace tool args become `"{}"`. Confirms args-based stub detection is impossible. Use schema-level sentinel only.

### Cache key tuple
`model_tools.py:297-302` — `(frozenset(enabled), frozenset(disabled), registry._generation, cfg_fp)`. All hashable. v4 does NOT extend this — promotion rebuilds tools explicitly instead.

---

## Phase 0 — Diagnostic baseline (v4)

**Patch point**: `agent/usage_pricing.py:698`

```python
# Inside the usage canonicalisation function (line ~698)
cache_read_tokens = _to_int(getattr(response_usage, "cache_read_input_tokens", 0))
cache_write_tokens = _to_int(getattr(response_usage, "cache_creation_input_tokens", 0))
input_tokens = _to_int(getattr(response_usage, "input_tokens", 0))

# NEW: baseline log line
try:
    _baseline_log({
        "ts": time.time(),
        "cache_read": cache_read_tokens,
        "cache_creation": cache_write_tokens,
        "input_tokens": input_tokens,
        "session_id": _current_session_id_or_unknown(),
    })
except Exception:
    pass
```

**Hit rate denominator (locked, zero-div guard)**:
```python
total = cache_read + cache_creation + input_tokens
hit_rate = (cache_read / total) if total > 0 else 0.0
```

**Phase 0 files**:
- `plugins/mcp_lazy/baseline_patch.py` — `_baseline_log()` writer (~30 LOC)
- `agent/usage_pricing.py:698` — 3-line import + call
- `plugins/mcp_lazy/scripts/cache_report.py` — summarizer CLI (~80 LOC)

**Phase 0 LOC**: ~115

**Exit criteria**: 72h log. Decision in `.omc/plans/mcp-lazy-baseline-decision.md`. Strategy:
- `hit_rate > 0.60` → use **deferred-promotion** (promote in pool only; next user turn naturally rebuilds via existing turn flow)
- `hit_rate < 0.30` → use **immediate-promotion** (rebuild + retry-instruct in same turn)
- Between → deferred (conservative default)

---

## Phase 1 v4 — Two-pass MVP

### Single hook point: patch `run_agent.py:3451 _build_api_kwargs`

```python
def _build_api_kwargs(self, api_messages: list) -> dict:
    """Build API kwargs; consult lazy-loading hook before tools are sent."""
    if getattr(self, "mcp_lazy_loading", False):
        # plugin hook returns new tools list; falsy = use original
        from hermes_cli.plugins import invoke_hook
        result = invoke_hook(
            "transform_api_kwargs",
            tools=self.tools,
            api_messages=api_messages,
            agent=self,
        )
        if result and isinstance(result, dict) and result.get("tools") is not None:
            # Temporarily swap; restore in finally below isn't needed because
            # self.tools is only read here; we apply in-call only by passing
            # the swapped list to the existing builder via a local override.
            return self._build_api_kwargs_with_tools(api_messages, result["tools"])
    return self._build_api_kwargs_with_tools(api_messages, self.tools)
```

Backward compat: when `mcp_lazy_loading` flag is False (default), behaves identically to current code.

### Stub detection (schema-level, not args)

`plugins/mcp_lazy/stubs.py`:
```python
LAZY_SENTINEL = "__lazy_stub__"

def make_stub_schema(full: dict, max_desc: int = 200) -> dict:
    func = full.get("function", {})
    desc = func.get("description", "")[:max_desc]
    return {
        "type": "function",
        "function": {
            "name": func.get("name", ""),
            "description": f"[LAZY] {desc}",
            "parameters": {
                "type": "object",
                "properties": {LAZY_SENTINEL: {"const": True}},
                "additionalProperties": False,
            },
        },
    }

def is_stub_schema(schema: dict) -> bool:
    props = schema.get("function", {}).get("parameters", {}).get("properties", {})
    return LAZY_SENTINEL in props
```

Zero-arg real MCP tools (e.g. `mcp_zai_web_search_search`) lack the sentinel → not detected as stub → survive.

### Promotion (copies cli.py:9604 precedent)

`plugins/mcp_lazy/promote.py`:
```python
def promote(agent, tool_names: list[str]) -> None:
    """Rebuild agent.tools with promoted tools fully loaded.

    Mirrors the precedent at hermes_cli/cli.py:9604-9612 (MCP server
    refresh) — explicit reassignment of agent.tools + agent.valid_tool_names.
    """
    pool = get_pool(agent.session_id)
    pool.promoted.update(tool_names)
    # NB: get_tool_definitions accepts a lazy_promoted kwarg added by us
    # at model_tools.py via additive shim.
    agent.tools = get_tool_definitions(
        enabled_toolsets=getattr(agent, "enabled_toolsets", None),
        quiet_mode=True,
        lazy_promoted=frozenset(pool.promoted),  # NEW kwarg
    )
    agent.valid_tool_names = {
        t["function"]["name"] for t in agent.tools
    } if agent.tools else set()
```

### Per-session pool (Codex Q11)

`plugins/mcp_lazy/pool.py`:
- Module-level `_pools: weakref.WeakValueDictionary[str, DeferredToolPool]`
- `get_pool(session_id) -> DeferredToolPool` — creates on first access
- Each pool owns `_promoted: set[str]`, `_lock: threading.RLock`
- Per-request snapshot: `frozenset(pool.promoted)` captured before injection
- Explicit eviction: `evict(session_id)` called from `cli.py:5900` via `pre_session_reset` hook

### Background review session id (Codex Q11 fix)

`agent/background_review.py:427-428`:
```python
# Before: review_agent.session_id = agent.session_id
# After:
import uuid
review_agent.session_id = f"{agent.session_id}.review.{uuid.uuid4().hex[:8]}"
```

### AIAgent init kwargs (Codex Q4)

`run_agent.py:349-415` AIAgent.__init__ signature — add after line 414 (before forwarding loop at `:418-484`):
```python
mcp_lazy_loading: bool = False,
lazy_promoted: frozenset[str] = frozenset(),
```

### Subagent inheritance

- `tools/delegate_tool.py:1101-1115` — pass `mcp_lazy_loading=parent.mcp_lazy_loading, lazy_promoted=frozenset()` (child starts fresh)
- `agent/background_review.py:381-393` — same pattern

### Files (Phase 1 v4)

| Path | Action | LOC |
|---|---|---|
| `plugins/mcp_lazy/__init__.py` | Plugin manifest | 30 |
| `plugins/mcp_lazy/pool.py` | Per-session pool + `evict` + `get_pool` | 90 |
| `plugins/mcp_lazy/stubs.py` | `make_stub_schema`, `is_stub_schema`, `mix_full_and_stubs` | 80 |
| `plugins/mcp_lazy/promote.py` | `promote(agent, tool_names)` — `cli.py:9604` pattern | 50 |
| `plugins/mcp_lazy/meta_tool.py` | `mcp_load_tools` meta-tool handler | 70 |
| `plugins/mcp_lazy/hook_impl.py` | `transform_api_kwargs` + `pre_session_reset` callbacks | 90 |
| `plugins/mcp_lazy/baseline_patch.py` | Phase 0 logger (carries into Phase 1) | 30 |
| `agent/usage_pricing.py:698` | 3-line import + log call | 4 mod |
| `run_agent.py:3451` | Hook invocation inside `_build_api_kwargs` | 15 mod |
| `run_agent.py:414` | Two new `__init__` kwargs | 4 mod |
| `agent/conversation_loop.py` | Stub-call detection via `is_stub_schema(registered_schema)` BEFORE arg canon at `:3103-3105` → call `promote()` | 35 mod |
| `tools/delegate_tool.py:1101-1115` | Pass `mcp_lazy_loading` to child | 5 mod |
| `agent/background_review.py:381-393` | Same for review fork | 5 mod |
| `agent/background_review.py:427-428` | Derived session_id | 3 mod |
| `model_tools.py:get_tool_definitions` | Accept `lazy_promoted: frozenset[str] = frozenset()` kwarg, pass through to internal builders | 12 mod |
| `hermes_cli/plugins.py:128-168` | Add `transform_api_kwargs` + `pre_session_reset` to `VALID_HOOKS` | 4 mod |
| `hermes_cli/cli.py:5900` | Fire `pre_session_reset` hook before `end_session()` | 5 mod |
| Config | `mcp.lazy_loading`, `mcp.lazy_stub_max_desc`, per-server `lazy` | additive |

**Phase 1 v4 totals**: ~360 LOC plugin + ~92 LOC core mods across 9 files.

### Tests (Phase 1 v4)

| Test | Asserts |
|---|---|
| `tests/plugins/mcp_lazy/test_stubs.py` | `make_stub_schema` round-trip; zero-arg real tool not flagged |
| `tests/plugins/mcp_lazy/test_pool.py` | Per-session isolation; eviction clears |
| `tests/plugins/mcp_lazy/test_promote.py` | `agent.tools` AND `agent.valid_tool_names` updated; promoted tool now full |
| `tests/agent/test_background_review_session.py` | review_agent.session_id has `.review.<hex>` suffix |
| `tests/plugins/mcp_lazy/test_no_orphan_tool_assignment.py` | grep `\.tools\s*=\s*get_tool_definitions` → only 4 sites: agent_init.py:802, cli.py:9604, acp_adapter/server.py:708, plugins/mcp_lazy/promote.py |
| Integration: stub-call → promote → retry-instruct response | |
| Integration: meta-tool flow | |
| Integration: 2 concurrent sessions → independent state | |
| Integration: subagent fresh promoted set | |
| Benchmark: cache hit rate delta ≤ baseline + 5% | |

---

## Phase 2 v4 — BM25 pre-selection

### Files unchanged from v3 except mention-mining

`plugins/mcp_lazy/query.py`:
```python
def build_query(api_messages: list) -> str:
    """Construct BM25 query from api_messages (last user message + recent
    tool mentions). api_messages contains full conversation in API shape;
    we walk it to extract user content and tool_use names from recent turns.
    """
    parts = []
    # Last user message
    for msg in reversed(api_messages):
        if msg.get("role") == "user":
            content = msg.get("content", "")
            if isinstance(content, str):
                parts.append(content)
            elif isinstance(content, list):  # Anthropic block shape
                parts.extend(b.get("text", "") for b in content if b.get("type") == "text")
            break
    # Tool mentions from last 6 turns
    recent = api_messages[-6:]
    mentions = []
    for msg in recent:
        c = msg.get("content", [])
        if isinstance(c, list):
            for block in c:
                if block.get("type") == "tool_use":
                    mentions.append(block.get("name", ""))
    if mentions:
        parts.append(f"Recently used: {' '.join(mentions)}")
    return " ".join(parts)
```

Other Phase 2 files unchanged: bm25.py, tokenizer.py, selector.py, safety.py.

**Phase 2 v4 totals**: ~340 LOC plugin-only, zero core touches.

---

## Phase 3 v4 — DESCOPED

Fork-local opt-in only. Not in upstream PR.

---

## Cross-cutting

### Config (final v4)
```yaml
mcp:
  lazy_loading: false
  lazy_stub_max_desc: 200
  baseline_log: true
  promote_strategy: deferred              # immediate|deferred (set by Phase 0)
  lazy_selection:
    mode: eager                           # eager|keyword
    top_k: 8
    always_include: [terminal, read_file, write_file]
    never_defer: [terminal, read_file]
    disabled_tools: []
    min_tokens_saved: 2000
    min_tools_deferred: 50
    aliases: {}
mcp_servers:
  trek:
    command: npx
    args: [-y, '@trek/mcp-server']
    lazy: true
```

### Rollout (11-day)

| Day | Phase | Activity | Exit gate |
|---|---|---|---|
| 1 | 0 | Patch `usage_pricing.py:698` + ship logger | Logging live |
| 1-3 | 0 | 72h soak | Logs captured |
| 4 | 0 | Strategy decision recorded | Strategy chosen |
| 5-6 | 1 | Build + unit tests | Unit tests green |
| 7 | 1 | Integration + 2-session leak test + cache delta | Loss ≤ baseline + 5% |
| 8 | 1 | Dev profile soak | 24h no regressions |
| 9-10 | 2 | BM25 build + eval | Recall@8 ≥ 80% |
| 11 | 2 | Soak | No false-negative misses |
| 12+ | — | Upstream PR prep | Standalone diff |

### Upstream PR (v4)

**ONE standalone PR**, ~60 LOC core:
- Adds `transform_api_kwargs` + `pre_session_reset` to `VALID_HOOKS` (`hermes_cli/plugins.py:128-168`)
- Adds `lazy_promoted` kwarg to `model_tools.get_tool_definitions`
- Inserts hook call inside `run_agent.py:3451 _build_api_kwargs`
- Inserts cleanup hook fire inside `cli.py:5900`

PR body shows 3 use cases for `transform_api_kwargs`:
1. MCP lazy-loading (our plugin)
2. Cost-cap injection (drop tools when budget exceeded)
3. Per-platform telemetry tagging

Plugin code stays fork-local indefinitely.

### What v4 explicitly avoids
- ❌ Patching `extract_cache_stats` (zero prod callers)
- ❌ Wrong assumption that `agent.messages` exists
- ❌ Wrong assumption that cache invalidation triggers tool rebuild
- ❌ Wrong assumption that `/reset` is missing
- ❌ Grep CI test targeting wrong injection pattern
- ❌ MCP-only upstream scope
- ❌ Stacked dependent PRs
- ❌ Args-based stub detection
- ❌ Module-level shared pool
- ❌ Same-day self-close
- ❌ Anthropic native mode in upstream PR

---

## Decision gate

**Phase 0 MUST complete before Phase 1 starts.** Phase 1 promotion strategy depends on Phase 0's hit-rate measurement.

---

## Addendum: v4 design refinements (Codex round 4)

Three real design issues surfaced during Codex sign-off. Folded inline rather than spinning a v5 doc.

### D2 — `lazy_promoted` must enter the cache key

`model_tools.py:286-291` (the cache key tuple) does NOT include `lazy_promoted`. Without inclusion, two requests with identical `enabled_toolsets` but different `lazy_promoted` sets would hit the same cache entry and return stale tools. Fix:

```python
# Extend the cache key tuple at model_tools.py:297-302
key = (
    frozenset(enabled_toolsets) if enabled_toolsets else None,
    frozenset(disabled_toolsets) if disabled_toolsets else None,
    registry._generation,
    cfg_fp,
    lazy_promoted,  # NEW (already a frozenset, hashable)
)
```

Apply during Phase 1 Step 1 (when `lazy_promoted` kwarg is added).

### D5 — Plugin-to-core dependency direction

`agent/usage_pricing.py:698` (core) must NOT import from `plugins/mcp_lazy/baseline_patch.py` (plugin) — wrong dependency direction. Fix via callback-slot indirection:

```python
# agent/usage_pricing.py — top of module
_usage_observers: list = []

def register_usage_observer(callback) -> None:
    """Plugins call this at load to receive every canonicalised usage record."""
    _usage_observers.append(callback)

# Inside the canonicalisation function at line ~698
for observer in _usage_observers:
    try:
        observer(usage_record)
    except Exception:
        logger.debug("usage observer failed", exc_info=True)
```

```python
# plugins/mcp_lazy/baseline_patch.py — load hook
from agent.usage_pricing import register_usage_observer
register_usage_observer(_baseline_log)
```

Plugin imports from core (correct direction). Core declares a slot; plugin fills it. Zero-coupling between core and any specific plugin.

### A2 — Grep pattern + allowlist correction

Investigator-corrected: there are **4 production sites** of agent tool assignment but they use different syntactic shapes. Single regex won't catch all. Use 2-pattern grep + allowlist:

```python
# tests/plugins/mcp_lazy/test_no_orphan_tool_assignment.py
ALLOWED_SITES = {
    "agent/agent_init.py:802",          # _ra().get_tool_definitions(...)
    "hermes_cli/cli.py:9604",            # MCP refresh
    "acp_adapter/server.py:708",         # ACP server
    "plugins/mcp_lazy/promote.py:*",     # our promotion (Phase 1)
    "mini_swe_runner.py:237",            # mini SWE runner [TERMINAL_TOOL_DEFINITION]
}

PATTERNS = [
    r"agent\.tools\s*=",
    r"self\.tools\s*=",
]
```

Test asserts every match maps to an allowlisted site.

### Phase 0 callback-slot patch revised

```python
# agent/usage_pricing.py:680 — add module-level slot list
_usage_observers: list = []

def register_usage_observer(callback) -> None:
    _usage_observers.append(callback)

# Inside _canonicalize_usage_for_provider() at ~line 698
# (after cache_read_tokens / cache_write_tokens / input_tokens calculated):
if _usage_observers:
    payload = {
        "cache_read": cache_read_tokens,
        "cache_creation": cache_write_tokens,
        "input_tokens": input_tokens,
    }
    for observer in _usage_observers:
        try:
            observer(payload)
        except Exception:
            logger.debug("usage observer error", exc_info=True)
```

```python
# plugins/mcp_lazy/baseline_patch.py
import json, time
from pathlib import Path
from agent.usage_pricing import register_usage_observer

_LOG = Path.home() / ".hermes" / "mcp-lazy" / "cache-baseline.jsonl"

def _baseline_log(payload: dict) -> None:
    _LOG.parent.mkdir(parents=True, exist_ok=True)
    payload = {"ts": time.time(), **payload}
    with _LOG.open("a") as f:
        f.write(json.dumps(payload) + "\n")

register_usage_observer(_baseline_log)
```

Hit-rate denominator (locked, zero-div safe) computed by reader script:
```python
total = cache_read + cache_creation + input_tokens
hit_rate = (cache_read / total) if total > 0 else 0.0
```

Plugin load triggers slot registration once. Core has zero knowledge of any plugin.
