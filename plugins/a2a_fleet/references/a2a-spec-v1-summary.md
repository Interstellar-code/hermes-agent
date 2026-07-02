# A2A Protocol v1.0 — Condensed Reference

Based on: [A2A Protocol Specification v1.0](https://a2a-protocol.org/latest/specification/) and [a2a.proto](https://github.com/a2aproject/A2A/blob/main/spec/a2a.proto).

## Core Concepts

| Concept | Definition | Hermes Mapping |
|---|---|---|
| **A2A Client** | App/agent that initiates requests to an A2A Server | Switch Agent calling `fleet_send()` |
| **A2A Server (Remote Agent)** | Agent/agentic system exposing A2A endpoints | Any Hermes profile with fleet.server enabled |
| **Agent Card** | JSON metadata (identity, capabilities, skills, endpoint, auth) | Generated from profile config + SOUL.md + tool manifests |
| **Message** | Communication turn with `role` ("user"/"agent") and `Parts` | Incoming A2A message → Hermes user message |
| **Task** | Stateful unit of work with unique ID and lifecycle | Maps to a Hermes agent session |
| **Part** | Content container: text, file (inline/URL), or structured data | Maps to prompt content / tool outputs |
| **Artifact** | Concrete output generated during task processing | Hermes tool outputs, file writes |
| **Streaming** | Real-time incremental updates via SSE | SSE events for each tool call / response step |
| **Push Notifications** | Server-initiated HTTP POST to client webhook | Phase 3 — for disconnected/long-running scenarios |
| **Context** | Optional `contextId` to group related tasks/messages | Session lineage tracking |

## Task States

```
TASK_STATE_UNSPECIFIED     = 0   // Unknown/indeterminate
TASK_STATE_SUBMITTED       = 1   // Received and acknowledged
TASK_STATE_WORKING         = 2   // Actively being processed
TASK_STATE_INPUT_REQUIRED  = 3   // Agent needs user/client input (non-terminal)
TASK_STATE_AUTH_REQUIRED   = 5   // Agent needs credentials (non-terminal)
TASK_STATE_COMPLETED       = 4   // Finished successfully (terminal)
TASK_STATE_FAILED          = 6   // Task failed (terminal)
TASK_STATE_CANCELED        = 7   // Client canceled (terminal)
TASK_STATE_REJECTED        = 8   // Agent rejected the task (terminal)
```

Non-terminal states: `input-required`, `auth-required`.  These can transition back to `working` once resolved.
Terminal states: `completed`, `failed`, `canceled`, `rejected`.  Tasks in terminal states are **immutable** — a new task must be created for follow-ups.

## JSON-RPC 2.0 Binding

### Endpoint
`POST /a2a/jsonrpc` with `Content-Type: application/json`

### Methods

**Core operations** (binding-independent abstract operations):

| Operation | JSON-RPC Method | Description |
|---|---|---|
| Send Message | `SendMessage` | Send a message to the agent, receive Task or Message |
| Send Streaming Message | `SendStreamingMessage` | Same, but response is SSE stream |
| Get Task | `tasks.get` | Get full task state + artifacts |
| List Tasks | `tasks.list` | List tasks (filterable by contextId, state) |
| Cancel Task | `tasks.cancel` | Request cancellation of an active task |
| Get Agent Card | `agent.getCard` | Return the agent's card (same as well-known URI) |
| Subscribe to Task | `tasks.subscribe` | SSE stream for an existing task |

**Request format:**
```json
{
  "jsonrpc": "2.0",
  "id": "req-uuid-123",
  "method": "SendMessage",
  "params": {
    "message": {
      "role": "user",
      "parts": [
        {"text": "Check the Construct's cron jobs and fix any failures"}
      ],
      "messageId": "msg-001",
      "contextId": "ctx-abc"
    }
  }
}
```

**Response (Task created):**
```json
{
  "jsonrpc": "2.0",
  "id": "req-uuid-123",
  "result": {
    "kind": "task",
    "id": "task-456",
    "contextId": "ctx-abc",
    "status": {
      "state": "TASK_STATE_SUBMITTED",
      "message": {
        "role": "agent",
        "parts": [{"text": "Task received. Processing..."}]
      }
    }
  }
}
```

**Response (immediate Message, no task):**
```json
{
  "jsonrpc": "2.0",
  "id": "req-uuid-123",
  "result": {
    "kind": "message",
    "message": {
      "role": "agent",
      "parts": [{"text": "I don't support that operation."}]
    }
  }
}
```

## Agent Card

Published at `GET /.well-known/agent-card.json`. Schema:

```json
{
  "name": "string",
  "description": "string",
  "url": "string (service endpoint URL)",
  "provider": {
    "organization": "string",
    "url": "string (optional)"
  },
  "version": "string",
  "documentationUrl": "string (optional)",
  "iconUrl": "string (optional)",
  "capabilities": {
    "streaming": "boolean",
    "pushNotifications": "boolean",
    "stateTransitionHistory": "boolean"
  },
  "defaultInputModes": ["string (MIME types)"],
  "defaultOutputModes": ["string (MIME types)"],
  "skills": [
    {
      "id": "string",
      "name": "string",
      "description": "string (optional)",
      "tags": ["string"],
      "examples": ["string"],
      "inputModes": ["string (optional)"],
      "outputModes": ["string (optional)"]
    }
  ],
  "security": {
    "schemes": ["string (e.g., 'bearer', 'oauth2')"],
    "credentials": "string (optional, out-of-band)"
  }
}
```

## SSE Streaming Protocol

When `SendStreamingMessage` is called or a client subscribes via `tasks.subscribe`, the server returns an SSE stream:

```
Content-Type: text/event-stream

event: task_status
data: {"taskId": "task-456", "state": "TASK_STATE_WORKING", "timestamp": "..."}

event: status_update
data: {"taskId": "task-456", "status": {"message": {"role": "agent", "parts": [{"text": "Checking cron jobs..."}]}}, "final": false}

event: artifact_update
data: {"taskId": "task-456", "artifact": {"artifactId": "art-1", "name": "Cron Report", "parts": [{"text": "2 jobs failed, 5 healthy"}]}}

event: task_status
data: {"taskId": "task-456", "state": "TASK_STATE_COMPLETED", "timestamp": "..."}
```

## Push Notifications

For long-running tasks where the SSE connection may drop, the client can provide a `PushNotificationConfig` with a webhook URL. The server POSTs task updates to that URL:

```json
POST <webhook_url>
{
  "taskId": "task-456",
  "state": "TASK_STATE_COMPLETED",
  "timestamp": "..."
}
```

## Key Design Principles

1. **Opaque execution** — agents do not share internal state, memory, or tool implementations
2. **Async-first** — designed for long-running tasks with human-in-the-loop
3. **Modality agnostic** — Part containers handle text, files, structured data uniformly
4. **Task immutability** — terminal tasks cannot restart; follow-ups create new tasks
5. **Context grouping** — `contextId` ties related tasks together without coupling state

## Python SDK (`a2a-sdk`)

Install: `pip install a2a-sdk>=1.0.0`

Key classes:
- `RequestContext` — wraps incoming request metadata
- `TaskManager` (for servers) — in-memory task store (pluggable: SQL, Redis)
- `Client` (for clients) — `send_message()`, `get_task()`, `subscribe_task()`
- `AgentCardBuilder` — builder pattern for Agent Cards
- `Part` / `Message` / `Task` / `Artifact` — data model classes

Minimal server (~80 lines):
```python
from a2a.server.apps import A2AStarletteApplication
from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.tasks import InMemoryTaskManager

# Build Agent Card
card = AgentCardBuilder(name="My Agent", url="http://host:port").build()

# Create handler that maps SendMessage to your logic
class MyHandler(DefaultRequestHandler):
    async def on_send_message(self, context, params):
        task = await self.task_manager.create_task(context_id=...)
        # Spawn background processing
        asyncio.create_task(self._process(task))
        return task

# Mount as Starlette app on Hermes gateway
app = A2AStarletteApplication(build=card, task_manager=InMemoryTaskManager())
starlette_app.mount("/a2a", app.build())
```
