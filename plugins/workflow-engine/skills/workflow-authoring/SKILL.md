---
name: workflow-authoring
description: Create, edit, validate, or save workflow definitions and DAG YAML for the workflow-engine plugin. Use when asked to author a new workflow, modify a workflow DAG, validate YAML syntax and node dependencies, or manage workflow definition versions and lifecycles.
metadata:
  hermes:
    tags: [workflow-engine, dag, authoring, yaml]
---

# workflow-engine: workflow-authoring

Procedures, schema rules, and API conventions for authoring, validating, and saving DAG workflows.

## Architecture & API Boundary

- **Tool vs HTTP Boundary**: Agent model tools (`workflow_list`, `workflow_run`, `workflow_status`, `workflow_approve`, `workflow_cancel`) handle execution and monitoring. Workflow authoring, validation, saving, versioning, and deletion are served via HTTP endpoints on the dashboard plugin API:
  `http://127.0.0.1:9119/api/plugins/workflow-engine/...` (port 9119 default, requires dashboard auth token; `dashboard/manifest.json:10`).
- **Storage**: Workflow definitions and run history persist in `$HERMES_HOME/switchui-workflows.db` (profile-scoped; `engine/wiring.py`).

## Workflow Authoring Lifecycle

Follow this sequence strictly: **Draft YAML -> Validate -> Check ID -> Save (Create-Only) -> Verify**.

### 1. Validate Before Save

Always call `POST /definitions/validate` (`dashboard/plugin_api.py:249`) before persisting:
```bash
curl -s -X POST http://localhost:9119/api/plugins/workflow-engine/definitions/validate \
  -H "Content-Type: application/json" \
  -d '{"yaml": "<YAML_STRING>", "id": "<NEW_ID_OPTIONAL>"}'
```
- Send `id` **only** when checking ID availability for a new workflow.
- **Response**: `{ok: bool, errors: [{line, col, code, message, node_id?}], warnings: [...], id_available: bool|null}` (`plugin_api.py:280`).
- **Codes & Actions** (`engine/discovery/validator.py:233-384`):
  - `yaml_parse`: Hostile YAML (alias expansion > 100k, numbers > 4300 chars, nesting too deep) or syntax error. -> Fix syntax; remove alias bombs.
  - `schema`: Missing top-level `name`/`description`/`nodes`, legacy `steps:`, reserved input names (`PATH`, `LD_*`), or node schema violation. -> Fix schema fields.
  - `id_taken`: Requested `id` already exists in definition store. -> Pick a new unique ID.
  - `duplicate_id`: Multiple nodes share the same `id`. -> Give each node a unique ID.
  - `unknown_dependency`: `depends_on` references a non-existent node ID. -> Correct or remove reference.
  - `undeclared_input`: `$INPUTS.var` is used but not declared in top-level `inputs` or `required_inputs`. -> Declare input in `inputs:`.
  - `cycle`: Dependency graph has a cycle. -> Break circular `depends_on` dependencies.
  - `unreachable_node`: Node depends on a cycle or missing node. -> Resolve upstream dependency.
  - `inputs_ref_syntax` (Warning): Mis-cased `$inputs.var`. -> Use `$INPUTS.var` for subgraphs or `$var` for prompts/commands.
  - `risky_shell` (Warning): Node executes `bash`, `script`, or `loop until_bash`. -> Informational audit warning.
  - `truncated` (Warning): Diagnostic count exceeded 200 items. -> Fix first 200 errors and re-validate.

### 2. Save Definitions

Call `POST /definitions` (`dashboard/plugin_api.py:148`):
```bash
curl -s -X POST http://localhost:9119/api/plugins/workflow-engine/definitions \
  -H "Content-Type: application/json" \
  -d '{"id": "my-flow", "name": "My Flow", "yaml": "<YAML_STRING>", "source": "project", "if_absent": true}'
```
- **Create-Only (`if_absent: true`)**: Mandatory for new definitions. If the ID exists, the server returns 409 `{"code": "id_taken"}`. **Never** retry without `if_absent`; **never** overwrite existing workflows. Select a new unique ID.
- **Save Races (409 Conflict)**: When saving updates or non-create paths, concurrent inserts return 409 `ConflictError` ("definition '<id>' was created concurrently; reload and retry"; `plugin_api.py:236`). Reload definition and retry.
- **Updates (`expected_checksum`)**: Pass `expected_checksum` to enforce optimistic locking (`plugin_api.py:190`). Note: `if_absent` and `expected_checksum` are mutually exclusive (400 if both passed).
- **Hostile-YAML Limits**: Max size 1 MB / 1,048,576 bytes (413; `plugin_api.py:170`), no lone surrogates (400; `plugin_api.py:168`), max 100k nodes after alias expansion (422), max 4300 chars for numeric scalars (422).

### 3. Versions History

- `GET /definitions/{id}/versions` (`plugin_api.py:306`) lists snapshots newest first with run usage count.
- `GET /definitions/{id}/versions/{checksum}` (`plugin_api.py:315`) retrieves exact historical YAML.
- **Snapshot Triggers**: Recorded on every save (`source='save'|'import'`) and on run start (`source='run'`; `definition_store.py:122`, `run_store.py:234`).
- **Retention**: Newest 50 (`SNAPSHOT_KEEP`) unpinned snapshots kept. Snapshots pinned by runs or subgraphs are never pruned. 10-minute grace period (`SNAPSHOT_GRACE_MS = 600000`) protects newly created snapshots (`definition_store.py:35-36`).

### 4. Delete & Factory Reset

- `DELETE /definitions/{id}` (`plugin_api.py:777`): Deletes definition, runs, scheduled runs, and unpinned snapshots (`definition_store.py:454`).
  - Active runs (`pending`, `running`, `paused`) block deletion with 409 `ConflictError` ("has active runs; cancel them first").
  - Bundled rows (`source='bundled'`) cannot be deleted (returns 403).
  - **AGENT SAFETY RULE**: Always obtain explicit user confirmation before calling DELETE.
- `POST /definitions/{id}/reset-factory` (`plugin_api.py:840`): Resets modified bundled definition back to default YAML from `defaults/*.yaml`. 403 if `source != 'bundled'`. User-gated.

### 5. Feature Detection

Inspect `GET /health` (`plugin_api.py:97`) `features` list (`FEATURES: ["validate", "definition_versions", "create_only", "retry_run", ...]`).
- On older engines lacking `"validate"`: skip server validation or use client lint.
- On engines lacking `"create_only"`: check with `GET /definitions/{id}` first (aware of race conditions).
- On engines lacking `"definition_versions"`: do not call `/versions` endpoints (returns 404).

## YAML Schema Core Rules

- **Required Top-Level**: `name` (str), `description` (str), `nodes` (non-empty list) (`engine/schemas/workflow.py:70-95`).
- **Node Kinds (Mutually Exclusive)**: `prompt`, `command`, `bash`, `script`, `approval`, `loop`, `cancel`, `subgraph` (`engine/schemas/dag_node.py:316-326`).
- **Timeouts**: `timeout` in `bash`, `script`, and `command` nodes is in **milliseconds** (ms) (`dag_node.py:354`, `bash.py:62`). For routed prompts, `hermes_task.timeout_s` is in **seconds** (`dag_node.py:123`).
- **Retry**: `retry.max_attempts` (1-5), `retry.delay_ms` (1000-60000 ms), `retry.on_error` ("transient"|"all") (`engine/schemas/retry.py:11`). Loop nodes reject `retry:` (`dag_node.py:363`).
- **No Provider/Model**: Do NOT hardcode `provider:` or `model:` in workflow YAML; Hermes config / agents supply model routing.
- See `references/schema.md` for complete field specifications and `file:line` source citations.

## Verified Examples

### Example 1: Minimal Valid Workflow
```yaml
name: Minimal Workflow
description: A minimal valid workflow definition
inputs:
  - name: topic
    type: string
nodes:
  - id: generate
    prompt: Write a summary about $INPUTS.topic
  - id: format
    prompt: Format as bullet points
    depends_on:
      - generate
```

### Example 2: Approval Gate + Loop Workflow
```yaml
name: Release Review Workflow
description: Review changes with human approval gate and refinement loop
inputs:
  - name: pr_id
    type: string
    description: Pull request identifier
nodes:
  - id: analyze_pr
    prompt: Analyze pull request $INPUTS.pr_id and summarize changes
    retry:
      max_attempts: 2
      delay_ms: 1000
      on_error: transient
  - id: human_gate
    approval:
      message: Review PR analysis. Proceed with refinement loop?
      capture_response: true
      on_reject:
        prompt: Explain why changes were rejected
        max_attempts: 2
    depends_on:
      - analyze_pr
  - id: refine_code
    loop:
      prompt: Refine pull request $INPUTS.pr_id based on feedback
      max_iterations: 3
      until: COMPLETE
    depends_on:
      - human_gate
    when: "$human_gate.output == 'approved'"
```
