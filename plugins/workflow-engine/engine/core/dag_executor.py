"""
DAG Workflow Executor.

Ports dag-executor.ts (core algorithmic subset):
- Kahn's algorithm topological layering
- Layer-by-layer parallel execution via asyncio.gather
- Cycle detection (runtime safety check)
- when: condition evaluation + trigger_rule enforcement
- Subgraph pre-expansion + output aggregation
- Per-node retry with exponential backoff + on_error: skip|fail|continue
- Approval node block-and-resume (sets run status = paused)
- Cancel node (marks run cancelled)
- Bash / script / command / prompt / loop node dispatch (via node_dispatcher)

NOT ported from TS (Phase 2c/3 concerns):
- DB event persistence (caller provides emit_event callback)
- Session threading (prompt nodes are stateless in this phase)
- Platform.sendMessage / SSE streaming (caller's responsibility)
- Hot-reload / SIGHUP watch
"""

from __future__ import annotations

import asyncio
import copy
import json
import logging
import math
import re
import time
import uuid
from dataclasses import dataclass, field
from typing import (
    Any,
    Callable,
    Coroutine,
    Dict,
    List,
    Mapping,
    Optional,
    Set,
    Tuple,
)

from engine.schemas.dag_node import (
    DagNode,
    TriggerRule,
    is_bash_node,
    is_loop_node,
    is_approval_node,
    is_cancel_node,
    is_script_node,
    is_subgraph_node,
)
from engine.schemas.workflow_run import NodeOutput, WorkflowRunStatus, make_node_output
from engine.core.condition_evaluator import evaluate_condition
from engine.core.executor_shared import substitute_node_output_refs
from engine.core.logger import (
    log_node_start,
    log_node_complete,
    log_node_error,
    log_node_skip,
)

logger = logging.getLogger("workflow.dag-executor")

# ── Types & Protocols ─────────────────────────────────────────────────────────

EmitEventFn = Callable[[str, Dict[str, Any]], None]
"""Callback: emit_event(event_type, payload_dict)"""

GetRunStatusFn = Callable[[], Coroutine[Any, Any, Optional[str]]]
"""Async callback: returns current WorkflowRunStatus string or None."""

PauseRunFn = Callable[[Dict[str, Any]], Coroutine[Any, Any, None]]
"""Async callback: pause the run with metadata dict."""

CancelRunFn = Callable[[], Coroutine[Any, Any, None]]
"""Async callback: cancel the run."""

SendMessageFn = Callable[[str], Coroutine[Any, Any, None]]
"""Async callback: send a user-visible message."""

GetSubgraphYamlFn = Callable[[str], Optional[Tuple[str, str]]]
"""Sync callback: returns (yaml_content, kind) for a subgraph ref, or None."""


@dataclass
class DagRunContext:
    """
    Execution context passed through the DAG executor.
    Abstracts all side-effects so the executor is unit-testable.
    """
    run_id: str
    emit_event: EmitEventFn
    get_run_status: GetRunStatusFn
    pause_run: PauseRunFn
    cancel_run: CancelRunFn
    send_message: SendMessageFn
    get_subgraph_yaml: GetSubgraphYamlFn
    llm: Any = None
    log_dir: Optional[str] = None
    # Map of node_id → output for nodes already completed in a prior run (resume)
    prior_completed: Optional[Dict[str, str]] = None


@dataclass
class NodeExecutionResult:
    state: str  # "completed" | "failed" | "skipped"
    output: str = ""
    error: Optional[str] = None


# ── Retry Config ──────────────────────────────────────────────────────────────

DEFAULT_MAX_RETRIES = 2
DEFAULT_RETRY_DELAY_MS = 3000


def _get_retry_config(node: DagNode) -> Tuple[int, int, str]:
    """Returns (max_retries, delay_ms, on_error). on_error: 'transient'|'all'."""
    retry = getattr(node, "retry", None)
    if retry is not None:
        max_r = getattr(retry, "max_attempts", DEFAULT_MAX_RETRIES)
        delay = getattr(retry, "delay_ms", DEFAULT_RETRY_DELAY_MS)
        on_err = getattr(retry, "on_error", "transient")
        return max_r, delay, on_err
    return DEFAULT_MAX_RETRIES, DEFAULT_RETRY_DELAY_MS, "transient"


# ── Subgraph Helpers ──────────────────────────────────────────────────────────

def _namespaced_child_id(parent_id: str, inner_id: str) -> str:
    return f"{parent_id}.{inner_id}"


def substitute_subgraph_inputs(source: str, inputs: Dict[str, Any]) -> str:
    """Substitute $INPUTS.<name> references (ports substituteSubgraphInputs)."""
    def replacer(m: re.Match) -> str:
        name = m.group(1)
        if name not in inputs:
            logger.warning("dag.subgraph_input_ref_unknown name=%s", name)
            return ""
        value = inputs[name]
        if isinstance(value, str):
            return value
        if isinstance(value, (int, float, bool)):
            return str(value).lower() if isinstance(value, bool) else str(value)
        if value is None:
            return ""
        return json.dumps(value)

    return re.sub(r"\$INPUTS\.([a-zA-Z_][a-zA-Z0-9_]*)", replacer, source)


def _rewrite_inner_output_refs(
    source: str,
    inner_ids: Set[str],
    parent_id: str,
) -> str:
    """Rewrite $<inner-id>.output[.field] to $<parent>.<inner-id>.output[.field]."""
    def replacer(m: re.Match) -> str:
        inner_id = m.group(1)
        suffix = m.group(2) or ""
        if inner_id not in inner_ids:
            return m.group(0)
        return f"${_namespaced_child_id(parent_id, inner_id)}.output{suffix}"

    return re.sub(
        r"\$([a-zA-Z_][a-zA-Z0-9_-]*)\.output((?:\.[a-zA-Z_][a-zA-Z0-9_]*)?)",
        replacer,
        source,
    )


def _rewrite_string(s: str, inner_ids: Set[str], parent_id: str, inputs: Dict[str, Any]) -> str:
    s = substitute_subgraph_inputs(s, inputs)
    s = _rewrite_inner_output_refs(s, inner_ids, parent_id)
    return s


def _clone_and_rewrite_node(
    node: DagNode,
    parent_id: str,
    inner_ids: Set[str],
    inputs: Dict[str, Any],
) -> DagNode:
    """Deep-clone a DagNode and rewrite string fields for subgraph embedding."""
    cloned = copy.deepcopy(node)

    def rw(s: str) -> str:
        return _rewrite_string(s, inner_ids, parent_id, inputs)

    # Rewrite id
    object.__setattr__(cloned, "id", _namespaced_child_id(parent_id, node.id))

    # Rewrite depends_on references
    if cloned.depends_on:
        new_deps = []
        for dep in cloned.depends_on:
            if dep in inner_ids:
                new_deps.append(_namespaced_child_id(parent_id, dep))
            else:
                new_deps.append(dep)
        object.__setattr__(cloned, "depends_on", new_deps)

    # HIGH 5: Rewrite when: field (if present) so $INPUTS.* refs are substituted
    if hasattr(cloned, "when") and isinstance(cloned.when, str):
        object.__setattr__(cloned, "when", rw(cloned.when))

    # Rewrite type-specific string fields
    if is_bash_node(cloned):
        object.__setattr__(cloned, "bash", rw(cloned.bash))
    elif is_cancel_node(cloned):
        object.__setattr__(cloned, "cancel", rw(cloned.cancel))
    elif is_script_node(cloned):
        object.__setattr__(cloned, "script", rw(cloned.script))
    elif is_approval_node(cloned):
        ap = copy.deepcopy(cloned.approval)
        object.__setattr__(ap, "message", rw(ap.message))
        object.__setattr__(cloned, "approval", ap)
    elif is_loop_node(cloned):
        lp = copy.deepcopy(cloned.loop)
        if hasattr(lp, "prompt") and isinstance(lp.prompt, str):
            object.__setattr__(lp, "prompt", rw(lp.prompt))
        object.__setattr__(cloned, "loop", lp)
    else:
        # PromptNode or CommandNode — rewrite prompt/command
        if hasattr(cloned, "prompt") and isinstance(cloned.prompt, str):
            object.__setattr__(cloned, "prompt", rw(cloned.prompt))
        if hasattr(cloned, "command") and isinstance(cloned.command, str):
            object.__setattr__(cloned, "command", rw(cloned.command))

    return cloned


@dataclass
class ExpandedSubgraph:
    placeholder_run_id: str
    parent_node_id: str
    child_nodes: List[DagNode]
    child_ids: List[str]
    terminal_child_ids: List[str]
    inner_id_by_child_id: Dict[str, str]
    outputs_spec: List[Dict[str, str]]  # [{name, from}, ...]
    skipped: bool = False
    skip_reason: Optional[str] = None


def _expand_subgraph(
    node: Any,  # SubgraphNode
    parent_run_id: str,
    ctx: DagRunContext,
) -> ExpandedSubgraph:
    """
    Expand a SubgraphNode into child DagNodes.
    Ports expandSubgraph() from dag-executor.ts.
    """
    from engine.schemas.workflow import WorkflowDefinition
    from engine.discovery import parse_workflow_yaml

    ref = node.subgraph.ref
    row = ctx.get_subgraph_yaml(ref)
    if row is None:
        raise ValueError(f"Subgraph '{ref}' not found in store")
    yaml_content, kind = row
    if kind != "subgraph":
        raise ValueError(f"Workflow definition '{ref}' is not a subgraph (kind={kind})")

    result = parse_workflow_yaml(yaml_content, f"{ref}.yaml")
    if result.error or not result.workflow:
        msg = result.error.error if result.error else "unknown parse error"
        raise ValueError(f"Failed to parse subgraph '{ref}': {msg}")

    definition = result.workflow
    inner_nodes, node_errors = definition.get_dag_nodes()
    if node_errors:
        raise ValueError(f"Subgraph '{ref}' has node validation errors: {node_errors}")
    if not inner_nodes:
        raise ValueError(f"Subgraph '{ref}' has no nodes")

    inner_ids: Set[str] = {n.id for n in inner_nodes}
    inputs: Dict[str, Any] = dict(node.subgraph.inputs or {})

    child_nodes = [
        _clone_and_rewrite_node(n, node.id, inner_ids, inputs)
        for n in inner_nodes
    ]

    inner_id_by_child_id: Dict[str, str] = {
        _namespaced_child_id(node.id, n.id): n.id
        for n in inner_nodes
    }

    child_ids = [c.id for c in child_nodes]

    # Terminal child ids: inner nodes with no inner dependents
    has_dependent = {dep for n in inner_nodes for dep in (n.depends_on or [])}
    terminal_inner = [n.id for n in inner_nodes if n.id not in has_dependent]
    terminal_child_ids = [_namespaced_child_id(node.id, i) for i in terminal_inner]

    # Extract outputs spec from definition
    raw_def = definition
    outputs_spec: List[Dict[str, str]] = []
    if hasattr(raw_def, "outputs") and raw_def.outputs:
        for o in raw_def.outputs:
            if hasattr(o, "name") and hasattr(o, "from_"):
                outputs_spec.append({"name": o.name, "from": o.from_})
            elif isinstance(o, dict):
                outputs_spec.append({"name": o.get("name", ""), "from": o.get("from", "")})

    return ExpandedSubgraph(
        placeholder_run_id=str(uuid.uuid4()),
        parent_node_id=node.id,
        child_nodes=child_nodes,
        child_ids=child_ids,
        terminal_child_ids=terminal_child_ids,
        inner_id_by_child_id=inner_id_by_child_id,
        outputs_spec=outputs_spec,
    )


def _aggregate_subgraph_outputs(
    expansion: ExpandedSubgraph,
    node_outputs: Dict[str, NodeOutput],
) -> str:
    """Aggregate child outputs per subgraph outputs: spec. Returns JSON string."""
    result: Dict[str, Any] = {}
    for spec in expansion.outputs_spec:
        name = spec["name"]
        from_ref = spec["from"]
        match = re.match(
            r"^([a-zA-Z_][a-zA-Z0-9_-]*)\.output(?:\.([a-zA-Z_][a-zA-Z0-9_]*))?$",
            from_ref,
        )
        if not match:
            logger.warning(
                "dag.subgraph_output_spec_unparseable name=%s from=%s parent=%s",
                name, from_ref, expansion.parent_node_id,
            )
            result[name] = None
            continue
        inner_id, field = match.group(1), match.group(2)
        child_id = _namespaced_child_id(expansion.parent_node_id, inner_id)
        child_out = node_outputs.get(child_id)
        if not child_out or child_out.state != "completed":
            result[name] = None
            continue
        if not field:
            result[name] = child_out.output
            continue
        try:
            parsed = json.loads(child_out.output)
            result[name] = parsed.get(field) if isinstance(parsed, dict) else None
        except (json.JSONDecodeError, TypeError):
            result[name] = None
    return json.dumps(result)


# ── Topological Layers (Kahn's Algorithm) ─────────────────────────────────────

def build_topological_layers(nodes: List[DagNode]) -> List[List[DagNode]]:
    """
    Build topological layers using Kahn's algorithm.
    Layer 0: nodes with no dependencies.
    Raises ValueError if a cycle is detected at runtime.
    Ports buildTopologicalLayers() from dag-executor.ts.
    """
    in_degree: Dict[str, int] = {}
    dependents: Dict[str, List[str]] = {}
    node_map: Dict[str, DagNode] = {}

    for node in nodes:
        node_map[node.id] = node
        in_degree[node.id] = len(node.depends_on or [])
        for dep in (node.depends_on or []):
            dependents.setdefault(dep, []).append(node.id)

    layers: List[List[DagNode]] = []
    ready = [n for n in nodes if in_degree.get(n.id, 0) == 0]

    while ready:
        layers.append(ready)
        next_ids: List[str] = []
        for node in ready:
            for dep_id in dependents.get(node.id, []):
                new_degree = in_degree.get(dep_id, 0) - 1
                in_degree[dep_id] = new_degree
                if new_degree == 0:
                    next_ids.append(dep_id)
        ready = [node_map[nid] for nid in next_ids if nid in node_map]

    total_placed = sum(len(layer) for layer in layers)
    if total_placed < len(nodes):
        raise ValueError(
            "[DagExecutor] Cycle detected at runtime — was cycle detection skipped at load?"
        )

    return layers


# ── Trigger Rule ──────────────────────────────────────────────────────────────

def check_trigger_rule(
    node: DagNode,
    node_outputs: Dict[str, NodeOutput],
) -> str:
    """Returns 'run' or 'skip'. Ports checkTriggerRule() from dag-executor.ts."""
    deps = node.depends_on or []
    if not deps:
        return "run"

    upstreams = [
        node_outputs.get(dep)
        or make_node_output("failed", "", f"upstream '{dep}' missing from outputs")
        for dep in deps
    ]

    rule: str = getattr(node, "trigger_rule", None) or "all_success"

    if rule == "all_success":
        return "run" if all(u.state == "completed" for u in upstreams) else "skip"
    elif rule == "one_success":
        return "run" if any(u.state == "completed" for u in upstreams) else "skip"
    elif rule == "none_failed_min_one_success":
        any_failed = any(u.state == "failed" for u in upstreams)
        any_succeeded = any(u.state == "completed" for u in upstreams)
        return "run" if (not any_failed and any_succeeded) else "skip"
    elif rule == "all_done":
        all_done = all(u.state in ("completed", "failed", "skipped") for u in upstreams)
        return "run" if all_done else "skip"
    elif rule == "always":
        return "run"

    return "skip"


# ── Main DAG Executor ─────────────────────────────────────────────────────────

async def execute_dag(
    nodes: List[DagNode],
    ctx: DagRunContext,
) -> Dict[str, NodeOutput]:
    """
    Execute a DAG workflow.

    1. Pre-expand subgraph nodes.
    2. Rewrite depends_on for parents of expanded subgraphs.
    3. Build topological layers (Kahn's).
    4. Execute each layer concurrently with asyncio.gather.
    5. For each node: check prior_completed → trigger_rule → when: condition → dispatch.
    6. After all layers: aggregate subgraph outputs.

    Returns a map of node_id → NodeOutput for all executed/skipped nodes.
    """
    from engine.core.node_dispatcher import dispatch_node

    dag_start = time.monotonic()
    node_outputs: Dict[str, NodeOutput] = {}

    # Pre-populate from prior run (resume)
    if ctx.prior_completed:
        for node_id, output_text in ctx.prior_completed.items():
            node_outputs[node_id] = make_node_output("completed", output_text)
        logger.info(
            "dag.workflow_resume_prepopulated run_id=%s count=%d",
            ctx.run_id, len(ctx.prior_completed),
        )

    # ── Subgraph pre-expansion ──────────────────────────────────────────────
    subgraph_expansions: Dict[str, ExpandedSubgraph] = {}
    executable_nodes: List[DagNode] = []

    for node in nodes:
        if not is_subgraph_node(node):
            executable_nodes.append(node)
            continue

        # Evaluate when: at expansion time (resume case with prior outputs)
        # Bug fix: must evaluate node.subgraph.when, not node.when (HIGH 4)
        subgraph_when = getattr(node.subgraph, "when", None)
        if subgraph_when is not None:
            cond_result, cond_parsed = evaluate_condition(subgraph_when, node_outputs)
            if not cond_parsed or not cond_result:
                expansion = ExpandedSubgraph(
                    placeholder_run_id=str(uuid.uuid4()),
                    parent_node_id=node.id,
                    child_nodes=[],
                    child_ids=[],
                    terminal_child_ids=[],
                    inner_id_by_child_id={},
                    outputs_spec=[],
                    skipped=True,
                    skip_reason="when_condition",
                )
                subgraph_expansions[node.id] = expansion
                node_outputs[node.id] = make_node_output("skipped")
                continue

        try:
            expansion = _expand_subgraph(node, ctx.run_id, ctx)
        except Exception as exc:
            logger.error("dag.subgraph_expansion_failed node=%s error=%s", node.id, exc)
            # Re-raise so the DAG run aborts — silent failure leaves downstream
            # nodes in an indeterminate state (HIGH 11, TS parity: TS raises here).
            raise

        # BLOCKER 1: Thread placeholder's depends_on onto root children so they
        # cannot run before the placeholder's upstream dependencies finish.
        placeholder_deps = list(node.depends_on or [])
        if placeholder_deps:
            # Root children = child nodes with no inner depends_on (no namespaced inner dep)
            child_inner_ids = {c.id for c in expansion.child_nodes}
            for i, child in enumerate(expansion.child_nodes):
                child_deps = list(child.depends_on or [])
                # A root child has no deps OR only deps outside the subgraph namespace
                child_inner_deps = [d for d in child_deps if d in child_inner_ids]
                if not child_inner_deps:
                    # Prepend placeholder deps (deduplicated)
                    new_deps = placeholder_deps + [d for d in child_deps if d not in placeholder_deps]
                    expansion.child_nodes[i] = _node_with_deps(child, new_deps)

        subgraph_expansions[node.id] = expansion
        executable_nodes.extend(expansion.child_nodes)

    # ── Rewrite depends_on for parents of subgraph placeholders ────────────
    for i, node in enumerate(executable_nodes):
        if not (node.depends_on):
            continue
        rewritten: List[str] = []
        changed = False
        for dep in node.depends_on:
            exp = subgraph_expansions.get(dep)
            if exp:
                changed = True
                if exp.skipped:
                    rewritten.append(dep)
                else:
                    rewritten.extend(exp.terminal_child_ids)
            else:
                rewritten.append(dep)
        if changed:
            executable_nodes[i] = _node_with_deps(node, rewritten)

    # ── Build layers ────────────────────────────────────────────────────────
    layers = build_topological_layers(executable_nodes)

    logger.info(
        "dag_workflow_starting run_id=%s node_count=%d layer_count=%d",
        ctx.run_id, len(nodes), len(layers),
    )

    # ── Emit subgraph_started events before any layer runs ──────────────────
    for expansion in subgraph_expansions.values():
        if expansion.skipped:
            ctx.emit_event("node_skipped", {
                "run_id": ctx.run_id,
                "node_id": expansion.parent_node_id,
                "reason": expansion.skip_reason or "when_condition",
            })
        else:
            ctx.emit_event("subgraph_started", {
                "run_id": ctx.run_id,
                "node_id": expansion.parent_node_id,
                "node_run_id": expansion.placeholder_run_id,
                "child_count": len(expansion.child_ids),
            })

    # ── Layer loop ──────────────────────────────────────────────────────────
    for layer_idx, layer in enumerate(layers):
        is_parallel = len(layer) > 1

        async def execute_one(node: DagNode) -> Tuple[str, NodeExecutionResult]:
            try:
                return node.id, await _execute_node_with_retry(node, layer_idx, is_parallel, node_outputs, ctx, dispatch_node)
            except Exception as exc:
                logger.error("dag_node_pre_execution_failed node=%s error=%s", node.id, exc)
                ctx.emit_event("node_failed", {
                    "run_id": ctx.run_id,
                    "node_id": node.id,
                    "error": str(exc),
                })
                try:
                    await ctx.send_message(f"Node '{node.id}' failed before execution: {exc}")
                except Exception:
                    pass
                return node.id, NodeExecutionResult(state="failed", error=str(exc))

        results = await asyncio.gather(*[execute_one(node) for node in layer])

        layer_had_failure = False
        for node_id, result in results:
            node_outputs[node_id] = make_node_output(result.state, result.output, result.error)
            if result.state == "failed":
                layer_had_failure = True

        if layer_had_failure:
            logger.warning("dag_layer_had_failures layer=%d count=%d", layer_idx, len(layer))

        # Check for non-running status between layers
        try:
            dag_status = await ctx.get_run_status()
            if dag_status is None or dag_status != "running":
                effective = dag_status or "deleted"
                logger.info(
                    "dag.stop_detected_between_layers run_id=%s layer=%d status=%s",
                    ctx.run_id, layer_idx, effective,
                )
                if effective != "paused":
                    try:
                        await ctx.send_message(
                            f"⚠️ **Workflow stopped** ({effective}): DAG execution stopped "
                            f"after layer {layer_idx + 1}/{len(layers)}"
                        )
                    except Exception:
                        pass
                break
        except Exception as status_err:
            logger.warning(
                "dag.status_check_failed run_id=%s error=%s", ctx.run_id, status_err
            )

    # ── Subgraph output aggregation ─────────────────────────────────────────
    for expansion in subgraph_expansions.values():
        if expansion.skipped:
            continue
        child_outputs = [node_outputs.get(cid) for cid in expansion.child_ids]
        failed_child = next(
            (cid for cid in expansion.child_ids
             if node_outputs.get(cid) and node_outputs[cid].state == "failed"),
            None,
        )
        if failed_child:
            node_outputs[expansion.parent_node_id] = make_node_output("failed", "", f"child node '{failed_child}' failed")
            ctx.emit_event("node_failed", {
                "run_id": ctx.run_id,
                "node_id": expansion.parent_node_id,
                "error": f"child node '{failed_child}' failed",
            })
        else:
            agg_output = _aggregate_subgraph_outputs(expansion, node_outputs)
            node_outputs[expansion.parent_node_id] = make_node_output("completed", agg_output)
            ctx.emit_event("node_completed", {
                "run_id": ctx.run_id,
                "node_id": expansion.parent_node_id,
                "output": agg_output,
            })

    dag_duration_ms = int((time.monotonic() - dag_start) * 1000)
    logger.info(
        "dag_workflow_finished run_id=%s duration_ms=%d",
        ctx.run_id, dag_duration_ms,
    )
    return node_outputs


def _node_with_deps(node: DagNode, new_deps: List[str]) -> DagNode:
    """Return a shallow copy of node with replaced depends_on."""
    cloned = copy.copy(node)
    object.__setattr__(cloned, "depends_on", new_deps)
    return cloned


async def _execute_node_with_retry(
    node: DagNode,
    layer_idx: int,
    is_parallel: bool,
    node_outputs: Dict[str, NodeOutput],
    ctx: DagRunContext,
    dispatch_fn: Any,
) -> NodeExecutionResult:
    """
    Wraps node execution with:
    - prior_completed skip
    - trigger_rule skip
    - when: condition skip
    - Retry loop with exponential backoff
    """
    # 0. Skip if completed in prior run
    if ctx.prior_completed and node.id in ctx.prior_completed:
        logger.info("dag.node_skipped_prior_success node=%s", node.id)
        log_node_skip(ctx.log_dir, ctx.run_id, node.id, "prior_success")
        ctx.emit_event("node_skipped", {
            "run_id": ctx.run_id,
            "node_id": node.id,
            "reason": "prior_success",
        })
        existing = node_outputs.get(node.id)
        if existing:
            return NodeExecutionResult(state=existing.state, output=existing.output)
        return NodeExecutionResult(state="skipped")

    # 1. Trigger rule
    trigger = check_trigger_rule(node, node_outputs)
    if trigger == "skip":
        logger.info("dag_node_skipped node=%s reason=trigger_rule", node.id)
        log_node_skip(ctx.log_dir, ctx.run_id, node.id, "trigger_rule")
        ctx.emit_event("node_skipped", {
            "run_id": ctx.run_id,
            "node_id": node.id,
            "reason": "trigger_rule",
        })
        node_outputs[node.id] = make_node_output("skipped")
        return NodeExecutionResult(state="skipped")

    # 2. When condition
    if node.when is not None:
        cond_result, cond_parsed = evaluate_condition(node.when, node_outputs)
        if not cond_parsed:
            msg = (
                f"⚠️ Node '{node.id}': unparseable `when:` expression \"{node.when}\" "
                f"— node skipped (fail-closed)."
            )
            try:
                await ctx.send_message(msg)
            except Exception:
                pass
            logger.error("dag_node_when_parse_failed node=%s when=%r", node.id, node.when)
            ctx.emit_event("node_skipped", {
                "run_id": ctx.run_id,
                "node_id": node.id,
                "reason": "when_unparseable",
            })
            node_outputs[node.id] = make_node_output("skipped")
            return NodeExecutionResult(state="skipped")
        if not cond_result:
            logger.info("dag_node_skipped node=%s reason=when_condition", node.id)
            log_node_skip(ctx.log_dir, ctx.run_id, node.id, "when_condition")
            ctx.emit_event("node_skipped", {
                "run_id": ctx.run_id,
                "node_id": node.id,
                "reason": "when_condition",
            })
            node_outputs[node.id] = make_node_output("skipped")
            return NodeExecutionResult(state="skipped")

    # 3. Retry loop
    max_retries, delay_ms, on_error = _get_retry_config(node)
    result = NodeExecutionResult(state="failed", error="Node did not execute")

    for attempt in range(max_retries + 1):
        result = await dispatch_fn(node, node_outputs, ctx)
        if result.state != "failed":
            break

        # Check if retryable
        error_msg = result.error or ""
        is_fatal = error_msg and classify_error_from_import(error_msg) == "FATAL"
        is_transient = error_msg and classify_error_from_import(error_msg) == "TRANSIENT"

        should_retry = (
            not is_fatal
            and attempt < max_retries
            and (on_error == "all" or is_transient)
        )

        if not should_retry:
            break

        backoff_ms = delay_ms * (2 ** attempt)
        logger.warning(
            "dag_node_transient_retry node=%s attempt=%d max=%d delay_ms=%d error=%s",
            node.id, attempt + 1, max_retries, backoff_ms, error_msg,
        )
        try:
            await ctx.send_message(
                f"⚠️ Node `{node.id}` failed (attempt {attempt + 1}/{max_retries + 1}). "
                f"Retrying in {backoff_ms // 1000}s..."
            )
        except Exception:
            pass
        await asyncio.sleep(backoff_ms / 1000.0)

    return result


def classify_error_from_import(message: str) -> str:
    """Thin wrapper to avoid circular import."""
    from engine.core.executor_shared import classify_error
    return classify_error(message)
