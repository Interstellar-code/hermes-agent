# Workflow Engine YAML Schema & API Reference

Comprehensive reference for the `workflow-engine` plugin YAML schema, DAG execution model, validation rules, and dashboard HTTP APIs.

Every statement in this document is derived directly from the codebase with exact `file:line` source citations.

---

## 1. Top-Level Workflow Schema

Source: `plugins/workflow-engine/engine/schemas/workflow.py:61-110`, `plugins/workflow-engine/engine/discovery/validator.py:37-100`

A valid workflow YAML document must be a mapping at the top level (`validator.py:41`).

| Field | Type | Required | Description & Source Reference |
|---|---|---|---|
| `name` | `string` | **Yes** | Human-readable workflow title (min length 1; `workflow.py:70`, `validator.py:49`). |
| `description` | `string` | **Yes** | Detailed workflow description (min length 1; `workflow.py:71`, `validator.py:55`). |
| `nodes` | `list` | **Yes** | Non-empty list of DAG node mappings (`workflow.py:95`, `validator.py:74`). Validated per-node via `validate_dag_node` (`workflow.py:105`, `dag_node.py:288`). |
| `id` | `string` | No | Identifier matching `^[a-zA-Z0-9][a-zA-Z0-9_-]*$` (`workflow.py:67`). If omitted in YAML, supplied when saving via `POST /definitions` (`plugin_api.py:156`). |
| `kind` | `string` | No | `"workflow"` (default) or `"subgraph"` (`workflow.py:66`). |
| `inputs` | `list` | No | List of input declarations (`workflow.py:68`), each having `name` (`^[a-zA-Z][a-zA-Z0-9_]*$`; `workflow.py:35`), optional `type` (`"string"\|"number"\|"boolean"\|"object"\|"array"`; `workflow.py:36`), `required` (`bool`; `workflow.py:37`), and `description` (`string`; `workflow.py:38`). |
| `outputs` | `list` | No | List of output declarations (`workflow.py:69`), each having `name` (`workflow.py:42`), `from` (`workflow.py:43`), and `description` (`workflow.py:44`). |
| `required_inputs` | `list[string]` | No | Alternative list of required input names; counted as declared inputs by validator (`validator.py:301-304`). |
| `optional_inputs` | `list[string]` | No | Alternative list of optional input names; counted as declared inputs by validator (`validator.py:301-304`). |
| `tags` | `list[string]` | No | Classification tags (`workflow.py:85`, `plugin_api.py:185`). |
| `version` | `string` | No | Version label string (`workflow.py:64`, `plugin_api.py:183`). |
| `worktree` | `mapping` | No | Worktree isolation policy: `worktree.enabled: bool` (`workflow.py:53-55, 83`). |
| `mutates_checkout` | `bool` | No | Declares whether workflow steps modify the git checkout (`workflow.py:84`). |

### Forbidden & Removed Top-Level Keys
- **`steps:`**: Legacy sequential format is explicitly rejected with validation error (`validator.py:63-71`).
- **`provider:` / `model:`**: While parsed by Pydantic for legacy compatibility (`workflow.py:72-73`), workflow YAML definitions in Hermes **must not hardcode provider or model**. Model routing is provided by Hermes host configuration, agent settings, or `hermes_task` hints (`dag_node.py:117`).

---

## 2. DAG Node Kinds & Field Specifications

Source: `plugins/workflow-engine/engine/schemas/dag_node.py:173-245`, `dag_node.py:288-414`

Every node mapping in `nodes:` must specify **exactly one** mode field (`command`, `prompt`, `bash`, `script`, `loop`, `approval`, `cancel`, `subgraph`). Specifying zero or more than one mode field raises a validation error (`dag_node.py:316-340`).

### 2.1 `prompt` Node (AI Completion / Delegation)
Source: `dag_node.py:180-182`, `engine/nodes/prompt.py:1-85`, `engine/nodes/agent_session.py:1-250`
- `prompt` (`string`, required): Non-empty prompt text evaluated by the LLM (`dag_node.py:181, 308, 331`).
- `hermes_task` (`mapping`, optional; `dag_node.py:117-124`):
  - `skills` (`list[string]`, optional): Skills enabled for this task turn (`dag_node.py:118`).
  - `agent_hint` (`string`, optional): Subagent role/persona hint (`dag_node.py:119`).
  - `model_hint` (`string`, optional): Model tier/routing hint (`dag_node.py:120`).
  - `profile` (`string`, optional): Target Hermes profile for routed execution (`^[a-z0-9][a-z0-9_-]{0,63}$`; `dag_node.py:122`). Only allowed on `prompt` nodes; cannot route to `"default"` (`dag_node.py:377-384`).
  - `timeout_s` (`integer`, optional): Timeout in **seconds** for routed profile execution (`gt=0, le=86400`; `dag_node.py:123`, `agent_session.py:157`).

### 2.2 `command` Node (CLI Command Dispatch)
Source: `dag_node.py:176-178`, `dag_node.py:343-347`, `engine/nodes/command.py:1-80`
- `command` (`string`, required): Command name matching `^[a-zA-Z0-9][a-zA-Z0-9_\-/]*$` without `..` (`dag_node.py:280-285, 343-347`).
- `timeout` (`number`, optional): Execution timeout in **milliseconds** (`dag_node.py:350-355`). Converted to seconds by the runner: `timeout = (timeout_raw / 1000.0) if timeout_raw else COMMAND_DEFAULT_TIMEOUT` (`command.py:50-52`).

### 2.3 `bash` Node (Local Shell Execution)
Source: `dag_node.py:184-187`, `dag_node.py:350-355`, `engine/nodes/bash.py:1-95`
- `bash` (`string`, required): Non-empty inline bash script (`dag_node.py:185, 309, 330`). Emits `risky_shell` lint warning (`validator.py:339-342`).
- `timeout` (`number`, optional): Timeout in **milliseconds** (`dag_node.py:354`). Converted to seconds by the runner: `timeout = (timeout_raw / 1000.0) if timeout_raw else BASH_DEFAULT_TIMEOUT` (`bash.py:62-64`).

### 2.4 `script` Node (Polyglot Script Execution)
Source: `dag_node.py:189-194`, `dag_node.py:358-360`, `engine/nodes/script.py:1-200`
- `script` (`string`, required): Non-empty inline script content (`dag_node.py:190, 313, 333`). Emits `risky_shell` lint warning (`validator.py:339-342`).
- `runtime` (`string`, required): Execution runtime; must be `"bun"` or `"uv"` (`dag_node.py:191, 358-360`).
- `deps` (`list[string]`, optional): Package dependencies installed for the script (`dag_node.py:192`).
- `timeout` (`number`, optional): Timeout in **milliseconds** (`dag_node.py:354`). Converted to seconds by the runner: `timeout = (timeout_raw / 1000.0) if timeout_raw else SCRIPT_DEFAULT_TIMEOUT` (`script.py:90-92`).

### 2.5 `approval` Node (Human-in-the-Loop Gate)
Source: `dag_node.py:200-216`, `dag_node.py:399-403`, `engine/nodes/approval.py:1-60`
- `approval` (`mapping` or `string`, required): String value is automatically coerced to `{"message": "<string>"}` (`dag_node.py:399-403`).
- `message` (`string`, required): Prompt message displayed to user awaiting approval (`dag_node.py:201`).
- `capture_response` (`bool`, optional): Whether user reply text is captured in node output (`dag_node.py:202`).
- `on_reject` (`mapping`, optional; `dag_node.py:203`):
  - `prompt` (`string`, required): Instruction executed when rejected (`dag_node.py:207`).
  - `max_attempts` (`integer`, optional): Allowed rejection attempts (1–10; `dag_node.py:208`).

### 2.6 `loop` Node (Iterative Execution)
Source: `dag_node.py:196-198`, `engine/schemas/loop.py:11-57`, `engine/nodes/loop.py:1-150`
- `loop` (`mapping`, required): Loop configuration (`LoopNodeConfig`; `dag_node.py:197`).
  - `prompt` (`string`, required): Prompt executed on each iteration (`loop.py:18-22`).
  - `over` (`list`, optional): Static list of items to iterate over; `$LOOP_ITEM` is substituted each iteration (`loop.py:14-17`).
  - `until` (`string`, optional): Signal string in AI completion output that terminates the loop (e.g., `'COMPLETE'`; `loop.py:23-27`).
  - `until_bash` (`string`, optional): Bash script run after each iteration; exit code 0 completes the loop (`loop.py:37-40`). Emits `risky_shell` lint warning (`validator.py:339-342`).
  - `max_iterations` (`integer`, optional): Iteration ceiling; default 1, must be > 0 (`loop.py:28-32`). Exceeding fails the node.
  - `fresh_context` (`bool`, optional): Whether to spawn a fresh session per iteration; default `false` (`loop.py:33-36`).
  - `interactive` (`bool`, optional): If true, pauses between iterations for user input (`loop.py:41-44`).
  - `gate_message` (`string`, optional): Required if `interactive: true` (`loop.py:45-55`).
- **Loop Restriction**: Node-level `retry:` is **strictly forbidden** on `loop` nodes; the loop manages its own iterations (`dag_node.py:362-368`).

### 2.7 `cancel` Node (Workflow Cancellation)
Source: `dag_node.py:218-220`, `engine/nodes/cancel.py:1-40`
- `cancel` (`string`, required): Reason string explaining why the workflow run was aborted (`dag_node.py:219`).

### 2.8 `subgraph` Node (Modular Subgraph Invocation)
Source: `dag_node.py:222-232`, `engine/core/dag_executor.py:149-235`, `engine/nodes/subgraph.py:1-27`
- `subgraph` (`mapping`, required): Reference configuration (`dag_node.py:231`).
  - `ref` (`string`, required): ID of child workflow definition (`^[a-z0-9][a-z0-9_-]*$`; `dag_node.py:223`).
  - `inputs` (`mapping`, optional): Input arguments passed to child subgraph (`dag_node.py:224`).
  - `when` (`string`, optional): Conditional execution gate for child subgraph (`dag_node.py:225`).
  - `timeout` (`integer`, optional): Timeout in seconds (`gt=0`; `dag_node.py:226`).
  - `max_retries` (`integer`, optional): Retry attempts (`ge=0`; `dag_node.py:227`).

---

## 3. Common Node Fields

Source: `plugins/workflow-engine/engine/schemas/dag_node.py:133-170`

These fields can be attached to any DAG node:

| Field | Type | Description & Source Reference |
|---|---|---|
| `id` | `string` | **Required**. Unique node identifier within the workflow (`dag_node.py:138, 303-305, 314-317`). |
| `phase` | `string` | Optional grouping or staging phase label (`dag_node.py:139`). |
| `depends_on` | `list[string]` | List of upstream node IDs that must finish before this node runs (`dag_node.py:140, 318-322`). |
| `when` | `string` | Condition expression evaluated before scheduling node (`dag_node.py:141`, `condition_evaluator.py:1-35`). |
| `trigger_rule` | `string` | Dependency gating rule (`dag_node.py:142`): `"all_success"` (default), `"one_success"`, `"none_failed_min_one_success"`, `"all_done"` (`dag_node.py:24-36`, `dag_executor.py:415-430`). |
| `retry` | `mapping` | Step retry policy (`StepRetryConfig`; `dag_node.py:150`, `retry.py:11-30`). Forbidden on loop nodes (`dag_node.py:363`). |
| `idle_timeout` | `number` | Inactivity timeout in **milliseconds** (`dag_node.py:149, 371-375`). |
| `context` | `string` | Session context mode: `"fresh"` or `"shared"` (`dag_node.py:145`). |
| `output_format` | `mapping` | Structured JSON output schema definition (`dag_node.py:146`). |
| `allowed_tools` | `list[string]` | Whitelist of tools accessible to this node (`dag_node.py:147`). |
| `denied_tools` | `list[string]` | Blacklist of tools inaccessible to this node (`dag_node.py:148`). |
| `effort` | `string` | LLM reasoning effort: `"low"\|"medium"\|"high"\|"max"` (`dag_node.py:42, 155`). |
| `thinking` | `string\|mapping` | Reasoning config: `"adaptive"`, `{"type": "enabled", "budgetTokens": int}`, `"disabled"` (`dag_node.py:47-72, 156`). |
| `sandbox` | `mapping` | Execution sandbox network and filesystem settings (`dag_node.py:75-105, 161`). |

### 3.1 `when:` Expression Grammar
Source: `plugins/workflow-engine/engine/core/condition_evaluator.py:1-100`

Evaluated at runtime against completed node outputs. Returns `True` to run the node, `False` to skip (`condition_evaluator.py:14`). Unparseable expressions fail-closed to `False` (`condition_evaluator.py:15`).

- **String equality / inequality**: `"$nodeId.output == 'VALUE'"` or `"$nodeId.output != 'VALUE'"` (`condition_evaluator.py:6, 30-33`).
- **JSON dot notation**: `"$nodeId.output.field == 'VALUE'"` (parses output as JSON and extracts field; `condition_evaluator.py:7, 51-66`).
- **Numeric comparisons**: `"$nodeId.output > '80'"`, `">="`, `"<"`, `"<="` (both sides must parse as finite floats; `condition_evaluator.py:8-9`).
- **Compound AND/OR**: `"$a.output == 'X' && $b.output != 'Y'"` or `"$a.output == 'X' || $b.output == 'Y'"` (`condition_evaluator.py:10-12`). `&&` takes precedence over `||`. Parentheses are not supported (`condition_evaluator.py:12`).

### 3.2 `retry:` Configuration
Source: `plugins/workflow-engine/engine/schemas/retry.py:11-30`

- `max_attempts` (`integer`, required): Maximum retry attempts (excluding initial try); must be between 1 and 5 (`retry.py:14-19`).
- `delay_ms` (`number`, optional): Initial backoff delay in **milliseconds** (1000–60000 ms; `retry.py:20-25`). Doubled on each subsequent attempt.
- `on_error` (`string`, optional): `"transient"` (default) or `"all"` (`retry.py:26-29`).

---

## 4. Variable & Input Substitution Rules

Source: `plugins/workflow-engine/engine/core/executor_shared.py:25-75, 138-153, 485-533`, `engine/core/dag_executor.py:149-164`, `engine/discovery/validator.py:107-111`

### 4.1 Input References
- **In Subgraphs**: `$INPUTS.<name>` (case-sensitive) is the only syntax substituted during subgraph expansion (`dag_executor.py:149-164`, `validator.py:107-110`).
- **In LLM Prompts & Commands**: `$<name>` is replaced with the caller input value (`executor_shared.py:138-153`).
- **In Bash & Script Nodes**: Inputs are **never source-interpolated** (to avoid code injection; `script.py:101`). Instead, declared inputs are exported into the subprocess environment:
  - Read via `"$repo_path"` in bash (`executor_shared.py:59-60`).
  - Read via `os.environ["repo_path"]` or `process.env.repo_path` in scripts (`executor_shared.py:60`).
- **Reserved Input Names**: Input names that override critical environment variables are rejected during validation (`validator.py:306-309`). Reserved names include `_RESERVED_ENV` (`PATH`, `PYTHONPATH`, `HOME`, `SHELL`, `IFS`, `BASH_ENV`, `ENV`) and prefixes `_RESERVED_PREFIXES` (`LD_`, `DYLD_`, `HERMES_`, `PYTHON`, `NODE_`) (`executor_shared.py:46-54`).

### 4.2 Node Output References
- In prompts: `$node_id.output` (entire text) or `$node_id.output.field` (JSON field extracted) (`executor_shared.py:492-532`).
- In script environments: outputs of completed upstream nodes are injected as environment variables formatted as `NODE_<NODE_ID>_OUTPUT` (`executor_shared.py:41-44, 71-73`).

---

## 5. Validation Diagnostics Reference

Source: `plugins/workflow-engine/engine/discovery/validator.py:233-384`

`lint_workflow_yaml(content)` returns `(errors, warnings)` where each diagnostic object contains `{line, col, code, message, node_id?}` (`validator.py:182-190, 233-234`).

### 5.1 Error Codes (`errors[]`)

| Code | Cause | Remediation | Source |
|---|---|---|---|
| `yaml_parse` | Hostile YAML payload, unclosed token, invalid syntax, recursive alias, or alias expansion exceeding 100,000 nodes (`_MAX_EXPANDED_NODES`). | Fix syntax; remove alias bombs; verify indentation. | `validator.py:151-177, 265, 270` |
| `schema` | Missing required top-level fields (`name`, `description`, `nodes`), reserved input name, or invalid node fields (bad command name, missing runtime on script, missing required fields). | Check schema; supply required fields; remove reserved input names. | `validator.py:275-292, 306-309` |
| `id_taken` | In `POST /definitions/validate`, the optional `id` parameter already exists in the definition store. | Choose a different unique definition ID. | `plugin_api.py:278-279` |
| `duplicate_id` | Multiple nodes declare the same `id`. | Ensure every node in `nodes:` has a distinct ID. | `validator.py:314-317` |
| `unknown_dependency` | A node's `depends_on` lists an ID not present in `nodes:`. | Correct the node ID in `depends_on` or add the missing node. | `validator.py:318-322` |
| `undeclared_input` | A node scalar references `$INPUTS.<name>`, but `<name>` is not listed in `inputs:` or `required_inputs:`. | Declare the input under top-level `inputs:`. | `validator.py:330-333` |
| `cycle` | Circular dependency exists among nodes (e.g., A -> B -> A). | Remove circular references from `depends_on`. | `validator.py:367-371, 383` |
| `unreachable_node` | A node cannot execute because it is trapped behind a cycle or unknown dependency. | Fix the upstream cycle or unknown dependency. | `validator.py:374-377` |

### 5.2 Warning Codes (`warnings[]`)

| Code | Cause | Remediation | Source |
|---|---|---|---|
| `inputs_ref_syntax` | Mis-cased input reference like `$inputs.var` instead of `$INPUTS.var`. | Change syntax to `$INPUTS.var` (for subgraphs) or `$var` (for prompts/commands). | `validator.py:334-338` |
| `risky_shell` | Node runs `bash`, `script`, or `loop until_bash` code on the local system. | Informational audit warning; verify script safety. | `validator.py:339-342` |
| `truncated` | Emitted when total diagnostics exceed 200 (`_MAX_DIAGNOSTICS`). Excess diagnostics are dropped. | Fix reported errors and re-run validation. | `validator.py:113, 246-249` |

---

## 6. HTTP API Endpoints Reference

Base URL prefix: `http://127.0.0.1:9119/api/plugins/workflow-engine` (`dashboard/manifest.json:10`). All dashboard routes require dashboard auth token header.

### 6.1 `POST /definitions/validate`
Source: `plugins/workflow-engine/dashboard/plugin_api.py:249-281`
- **Request Body**: `{"yaml": string, "id": string?}`
- **Behavior**: Runs `lint_workflow_yaml` off the event loop (`plugin_api.py:268`). Checks size against `_MAX_YAML_BYTES` (1 MB; 413 on overflow; `plugin_api.py:265-266`). If `id` is provided, tests ID availability in the definition database (`plugin_api.py:276-279`).
- **Response** (200 OK):
  ```json
  {
    "ok": true,
    "errors": [],
    "warnings": [],
    "id_available": true
  }
  ```

### 6.2 `POST /definitions` (Save / Create)
Source: `plugins/workflow-engine/dashboard/plugin_api.py:148-241`
- **Request Body**:
  - `id` (`string`, required): 1–128 chars matching `^[A-Za-z0-9_:.-]{1,128}$` (`plugin_api.py:71, 156-157`).
  - `name` (`string`, required): 1–256 chars (`plugin_api.py:159-161`).
  - `yaml` (`string`, required): Valid UTF-8 string <= 1,048,576 bytes (`plugin_api.py:72, 163-170`).
  - `source` (`string`, optional): `"project"` (default), `"user"`, `"bundled"` (`plugin_api.py:172-174`). Note: Explicit creation with `source='bundled'` is forbidden (403; `plugin_api.py:221-222`).
  - `if_absent` (`bool`, optional): When `true`, enables atomic insert. If the ID is taken, raises `DefinitionExistsError` returning 409 `{"error": "definition '<id>' already exists", "code": "id_taken"}` (`plugin_api.py:197-200, 234-235`). Mutually exclusive with `expected_checksum` (400 if both provided; `plugin_api.py:200-201`).
  - `expected_checksum` (`string`, optional): Enforces optimistic locking on updates (`plugin_api.py:190`). Returns 409 `ConflictError` if the database row does not match `expected_checksum` (`plugin_api.py:214-215, 236-237`).
  - `save_source` (`string`, optional): `"save"` (default) or `"import"` (`plugin_api.py:192-195`). Recorded as version snapshot provenance.
  - `scope_path` (`string`, optional): Absolute path without `..` segments (`plugin_api.py:176-179`).
- **Bundled Definition Edits**: Editing an existing bundled definition invokes `mark_user_edit` (`plugin_api.py:204-218`). It preserves `source='bundled'`, sets `user_modified=1`, and takes a snapshot.
- **Save Races**: Upsert checks for the ID, then inserts. If a concurrent process inserts between check and insert, a 409 `ConflictError` ("definition '<id>' was created concurrently; reload and retry") is returned (`plugin_api.py:236-237`).
- **Response**: 201 Created (or 200 OK for bundled edits) returning `{"definition": {...}}`.

### 6.3 `GET /definitions/{id}/versions` & `GET /definitions/{id}/versions/{checksum}`
Source: `plugins/workflow-engine/dashboard/plugin_api.py:306-328`
- `GET /definitions/{def_id}/versions`: Returns list of versions newest first: `[{"checksum": str, "version": str, "saved_at": int, "source": str, "node_count": int, "size_bytes": int, "in_use_by_runs": int}]` (`plugin_api.py:289-303, 306-313`).
- `GET /definitions/{def_id}/versions/{checksum}`: Returns full version record including `yaml` and `parsed` structure (`plugin_api.py:315-328`).
- **Version Snapshot Recording**:
  - Automatically recorded inside save transaction on every `upsert_definition` or `mark_user_edit` (`definition_store.py:122-143`).
  - Automatically recorded on every run start by `RunStore._pin_definition` with `source='run'` (`run_store.py:230-247`).
- **Version Snapshot Retention** (`definition_store.py:31-36, 144-170`):
  - Retains the newest 50 (`SNAPSHOT_KEEP = 50`) unpinned snapshots (`definition_store.py:35`).
  - Snapshots pinned by runs or subgraphs (`in_use_by_runs > 0`) are **never deleted** (`definition_store.py:150-158`).
  - Snapshots created within the last 10 minutes (`SNAPSHOT_GRACE_MS = 600000`) are protected from pruning (`definition_store.py:36, 161`).

### 6.4 `DELETE /definitions/{id}` & `POST /definitions/{id}/reset-factory`
Source: `plugins/workflow-engine/dashboard/plugin_api.py:777-790, 840-858`, `definition_store.py:432-480`
- `DELETE /definitions/{def_id}`:
  - If `source == 'bundled'`, returns 403 `{"error": "bundled definitions are read-only"}` (`plugin_api.py:783`).
  - Checks if definition has active runs in status `('pending', 'running', 'paused')`. If so, raises `ConflictError("definition '<id>' has active runs; cancel them first")` returning 409 (`definition_store.py:446-453`, `plugin_api.py:786-787`).
  - On delete, removes the definition row, all associated `workflow_runs`, `scheduled_runs`, and non-pinned snapshots (`definition_store.py:454-470`).
  - **AGENT SAFETY REQUIREMENT**: Always require explicit user confirmation before executing deletion!
- `POST /definitions/{def_id}/reset-factory`:
  - Resets a modified bundled definition back to original factory YAML from `defaults/*.yaml` (`plugin_api.py:840-858`).
  - 403 if `source != 'bundled'` (`plugin_api.py:846`).
  - User-gated for agents.

### 6.5 `GET /health` (Feature Detection)
Source: `plugins/workflow-engine/dashboard/plugin_api.py:53-62, 97-120`
- **Response**:
  ```json
  {
    "ok": true,
    "version": "0.4.0",
    "features": [
      "definition_pin", "parent_run", "node_attempts", "approver",
      "node_retrying_event", "node_log", "events_query", "sse_db_tail",
      "cross_process_sse", "cron_schedule", "schedules_api", "retry_run",
      "validate", "definition_versions", "create_only"
    ]
  }
  ```
- **Feature Fallback Matrix**:
  - `validate`: If absent, skip HTTP pre-validation; validate client-side before calling save.
  - `create_only`: If absent, `if_absent: true` is not supported; perform `GET /definitions/{id}` existence check prior to save.
  - `definition_versions`: If absent, version history endpoints return 404; do not query `/versions`.
  - `retry_run`: If absent, resume from node via `/runs/{id}/retry` is unsupported; initiate a new run with `workflow_run`.

### 6.6 `POST /runs/{run_id}/retry` (Resume Failed / Crashed Runs)
Source: `plugins/workflow-engine/dashboard/plugin_api.py:884-919`, `engine/facade.py:640-700`, `engine/runtime/runner.py:240-340`
- Re-executes a failed, cancelled, or crashed run in place on its pinned definition snapshot (added in B4 `0934c875f1`).
- **Body**: `{"from_node_id": string?, "actor": string?}` (`plugin_api.py:886, 901-910`).
- **Behavior**:
  - If `from_node_id` is supplied: the target node and all downstream dependent nodes are reset to `pending` and re-executed; upstream completed nodes are skipped as `prior_success`.
  - If `from_node_id` is omitted: all failed and cancelled nodes are re-executed.
  - Approval gates in the re-run set pause and request approval again.
  - 409 if run is in progress, paused, or completed (`plugin_api.py:887-890, 915-916`).
  - 400 if `from_node_id` does not exist in the pinned definition (`plugin_api.py:917-918`).
