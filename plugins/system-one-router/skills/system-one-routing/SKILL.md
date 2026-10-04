---
name: system-one-routing
description: Discipline for using system-one-router — advisory-only decisions via the Jev API, confidence gates, fail-closed fallback.
version: "0.1.0"
---

# system-one-routing (discipline layer)

## What this plugin is

Advisory-only v1. The Jev decision API suggests (message kind + specialist lane + confidence);
Switch (the orchestrator) still makes every routing decision. The tool NEVER dispatches, never
delegates, never mutates anything.

## When to call system_one_route

When a message is genuinely ambiguous between keep-vs-delegate (not on every message: a fresh task costs two paid API calls and adds network latency; clear cases need no opinion):

```
system_one_route(message=<verbatim text>, is_followup=<bool>, reply_target=<quoted parent if any>,
                 conversation=[{from, text} x up to 4 prior turns])
```

## How to read the result

- `status != "ok"` (fallback): proceed EXACTLY as you would have without the plugin. Never invent
  a route to avoid the fallback.
- `kind == "continuation"` or `"general"`: the message is not a fresh assignment — it belongs to
  the ongoing exchange. Strong signal to keep handling it yourself.
- `kind == "fresh_task"` + `lane` + `confidence >= 0.7`: a *suggestion* that the named
  lane fits. Weigh it against conversation state as always. A high-confidence neo suggestion for a
  message you know is coordination is WRONG — your judgment outranks the model.
- `confidence < 0.7`: treat as "no opinion." Route as you normally would.

## Hard rules

1. Never auto-dispatch on Jev output. The decision is always yours (v1).
2. Never send credentials, API keys, tokens, or full file contents in `state`. Trim to the
   decision-relevant facts.
3. Fallback ≠ failure. The plugin is a second opinion, not a dependency.
4. If the spend cap trips (cap_exceeded), stop calling until next month; route normally.
5. Decision-log stores hashes only — never paste raw message text anywhere on its behalf.

## Other tools

- `system_one_decide(state, questions)`: generic typed decision (choice/score) for one-off questions. Max 3 questions, bounded payload. Same cost and advisory rules.
- `system_one_status()`: config, key presence, decision-log stats (month spend, p95 latency). Free; no network.
