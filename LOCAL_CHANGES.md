# Local Hermes Changes

## Interactive clarify and durable backend interactions

Status: local core patch  
Reason: Switch UI needs selectable clarify options, response lifecycle events, and persisted receipts that remain visible after completion/session switch.

Touched files:
- `gateway/platforms/api_server.py`
- `toolsets.py`
- `hermes_cli/config.py`
- `tests/gateway/test_session_api.py`
- `tests/gateway/test_api_server_toolset.py`

Search marker:
- `LOCAL_DELTA(interactive-clarify)`

Behavior contract:
- `hermes-api-server` includes `clarify` in static toolset/capability reporting.
- Agent exposure of `clarify` remains gated by both:
  - request path passes `interactive_clarify=True`
  - config has `api_server.interactive_clarify: true`
- Enhanced session chat stream emits both `clarify.request` and generic `interaction.request`.
- Clarify can be resumed through:
  - `POST /api/sessions/{session_id}/chat/clarify`
  - `POST /api/sessions/{session_id}/chat/interactions/{interaction_id}/respond`
- Successful responses emit `clarify.responded` and `interaction.responded` payloads with session/run/message ids, question, choices, answer, and resolved status.
- Successful clarify responses persist a transcript-visible `clarify` tool receipt into SessionDB.
- `/v1/runs` approval responses emit enriched `approval.responded` events and persist transcript-visible `approval` receipts into the run session.
- Stateless OpenAI-compatible `/v1/chat/completions` and `/v1/responses` paths remain non-interactive and do not receive `clarify` unless the session stream gates are active.

Verification:
- `scripts/run_tests.sh tests/gateway/test_api_server_toolset.py -q`
- `scripts/run_tests.sh tests/gateway/test_session_api.py -q`

Release/rebase checklist:
- `grep -R "LOCAL_DELTA(interactive-clarify)" -n gateway toolsets.py hermes_cli/config.py`
- Confirm `/v1/capabilities` advertises `session_chat_interaction_respond` and reports `interactive_clarify` from config.
- Confirm selected clarify answer survives `GET /api/sessions/{session_id}/messages`.
