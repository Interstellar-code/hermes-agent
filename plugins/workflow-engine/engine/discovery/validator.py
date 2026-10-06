"""
Workflow YAML validation helpers.
Wraps Pydantic validation and formats errors in a consistent way.
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Tuple
import yaml as yaml_lib
from pydantic import ValidationError

from engine.schemas.workflow import WorkflowDefinition, WorkflowLoadError


def validate_workflow_yaml(
    content: str, filename: str
) -> tuple[WorkflowDefinition | None, WorkflowLoadError | None]:
    """
    Parse and validate a YAML string as a WorkflowDefinition.

    Returns (workflow, None) on success or (None, error) on failure.
    """
    # 1. YAML parse (bounded: alias bombs and deep nesting are parse errors)
    try:
        _root, raw = _load_bounded(content)
    except yaml_lib.YAMLError as exc:
        error = f"YAML parse error: {exc}"
    except _Rejected as exc:
        error = str(exc)
    except (RecursionError, MemoryError):
        error = _TOO_DEEP_MSG
    else:
        return validate_workflow_raw(raw, filename)
    return None, WorkflowLoadError(filename=filename, error=error, errorType="parse_error")


def validate_workflow_raw(
    raw: Any, filename: str
) -> tuple[WorkflowDefinition | None, WorkflowLoadError | None]:
    """validate_workflow_yaml after the YAML parse step (raw = safe_load result)."""
    if not isinstance(raw, dict):
        return None, WorkflowLoadError(
            filename=filename,
            error="Workflow YAML must be a mapping at the top level",
            errorType="parse_error",
        )

    # 2. Required field checks (mirrors TS loader early-exit pattern)
    if not isinstance(raw.get("name"), str) or not raw["name"].strip():
        return None, WorkflowLoadError(
            filename=filename,
            error="Missing required field 'name'",
            errorType="validation_error",
        )
    if not isinstance(raw.get("description"), str) or not raw["description"].strip():
        return None, WorkflowLoadError(
            filename=filename,
            error="Missing required field 'description'",
            errorType="validation_error",
        )

    # 3. Reject legacy steps-based workflows
    if isinstance(raw.get("steps"), list) and len(raw["steps"]) > 0:
        return None, WorkflowLoadError(
            filename=filename,
            error=(
                "`steps:` format has been removed. Workflows now use `nodes:` (DAG) "
                "format exclusively."
            ),
            errorType="validation_error",
        )

    # 4. Require nodes:
    if not isinstance(raw.get("nodes"), list) or len(raw["nodes"]) == 0:
        return None, WorkflowLoadError(
            filename=filename,
            error="Workflow must have 'nodes:' configuration",
            errorType="validation_error",
        )

    # 5. Pydantic validation of the whole document
    try:
        workflow = WorkflowDefinition.model_validate(raw)
    except ValidationError as exc:
        return None, WorkflowLoadError(
            filename=filename,
            error=f"Schema validation failed: {exc}",
            errorType="validation_error",
        )

    # 6. Per-node DAG node validation
    _dag_nodes, node_errors = workflow.get_dag_nodes()
    if node_errors:
        return None, WorkflowLoadError(
            filename=filename,
            error=f"DAG node validation failed: {'; '.join(node_errors)}",
            errorType="validation_error",
        )

    return workflow, None


# ---------------------------------------------------------------------------
# Lint — positioned diagnostics for the editor (POST /definitions/validate)
# ---------------------------------------------------------------------------

# `$INPUTS.name` is the only form the engine substitutes (subgraph expansion,
# case-sensitive — dag_executor.substitute_subgraph_inputs). Any other casing
# (`$inputs.name`) is left as literal text at run time.
_INPUT_REF_RE = re.compile(r"\$INPUTS\.([A-Za-z_][A-Za-z0-9_]*)")
_MISCASED_INPUT_REF_RE = re.compile(r"\$(?!INPUTS\.)(?i:inputs)\.([A-Za-z_][A-Za-z0-9_]*)")
_MAX_EXPANDED_NODES = 100_000  # composed nodes after alias expansion
_MAX_DIAGNOSTICS = 200  # per list; overflow becomes one `truncated` warning
# Longest int/float scalar: Python's own int-digit limit. Also bounds PyYAML's
# quadratic base-60 int (`1:1:1:...`) construction.
_MAX_NUMBER_CHARS = 4300
_NUMBER_TAGS = ("tag:yaml.org,2002:int", "tag:yaml.org,2002:float")


def _expanded_size(root: Any) -> int:
    """Node count with aliases expanded, memoized by node identity (so the
    check is linear in the composed graph). Raises ValueError on a recursive alias,
    _Rejected on an over-long number."""
    memo: Dict[int, Optional[int]] = {}

    def size(n: Any) -> int:
        key = id(n)
        if key in memo:
            if memo[key] is None:
                raise ValueError("recursive alias")
            return memo[key]  # type: ignore[return-value]
        memo[key] = None
        if isinstance(n, yaml_lib.SequenceNode):
            total = 1 + sum(size(c) for c in n.value)
        elif isinstance(n, yaml_lib.MappingNode):
            total = 1 + sum(size(k) + size(v) for k, v in n.value)
        else:
            if n.tag in _NUMBER_TAGS and len(n.value) > _MAX_NUMBER_CHARS:
                raise _Rejected(f"YAML parse error: number longer than {_MAX_NUMBER_CHARS} characters")
            total = 1
        memo[key] = total
        return total

    return size(root)


class _Rejected(Exception):
    """Hostile or unconstructible YAML; str(exc) is the user-facing error."""


_TOO_LARGE_MSG = (f"YAML parse error: document expands too large (over {_MAX_EXPANDED_NODES} nodes "
                  "after alias expansion, or a recursive alias)")
_TOO_DEEP_MSG = "YAML parse error: document too deeply nested"


def _load_bounded(content: str) -> Tuple[Any, Any]:
    """(composed root node, data) — yaml.safe_load's own steps (compose, then
    construct) with alias expansion capped at _MAX_EXPANDED_NODES in between.
    Raises yaml.YAMLError, _Rejected, or RecursionError (deep nesting)."""
    loader = yaml_lib.SafeLoader(content)
    try:
        root = loader.get_single_node()
        try:
            too_big = root is not None and _expanded_size(root) > _MAX_EXPANDED_NODES
        except ValueError:
            too_big = True
        if too_big:
            raise _Rejected(_TOO_LARGE_MSG)
        try:
            return root, (loader.construct_document(root) if root is not None else None)
        except yaml_lib.YAMLError:
            raise
        # Composition already passed the bounds; SafeConstructor itself raises
        # non-YAML errors on bad values (base-60 float overflow, `!!int ''`,
        # `!!bool maybe`, bad dates).
        except Exception as exc:
            raise _Rejected(f"YAML parse error: {exc!r}") from exc
    finally:
        loader.dispose()


def _diag(code: str, message: str, pos: Optional[Tuple[int, Optional[int]]] = None,
          node_id: Optional[str] = None) -> Dict[str, Any]:
    d: Dict[str, Any] = {
        "line": pos[0] if pos else None, "col": pos[1] if pos else None,
        "code": code, "message": message,
    }
    if node_id is not None:
        d["node_id"] = node_id
    return d


def _node_at(root: Any, path: List[Any]) -> Any:
    """Deepest composed YAML node reachable along path (None if not even the first step)."""
    node, found = root, None
    for key in path:
        if isinstance(node, yaml_lib.MappingNode):
            node = next((v for k, v in node.value if k.value == str(key)), None)
        elif isinstance(node, yaml_lib.SequenceNode) and isinstance(key, int) \
                and 0 <= key < len(node.value):
            node = node.value[key]
        else:
            node = None
        if node is None:
            break
        found = node
    return found if path else root


def _locate(root: Any, path: List[Any]) -> Optional[Tuple[int, int]]:
    """1-based (line, col) of _node_at(root, path), None when unresolvable."""
    node = _node_at(root, path) if root is not None else None
    return (node.start_mark.line + 1, node.start_mark.column + 1) if node is not None else None


def _scalars(node: Any, seen: Optional[set] = None):
    """Each scalar node once: an alias shares its anchor's node, so revisiting
    it would only repeat the same diagnostics (and the scan cost)."""
    seen = set() if seen is None else seen
    if id(node) in seen:
        return
    seen.add(id(node))
    if isinstance(node, yaml_lib.ScalarNode):
        yield node
    elif isinstance(node, yaml_lib.SequenceNode):
        for v in node.value:
            yield from _scalars(v, seen)
    elif isinstance(node, yaml_lib.MappingNode):
        for _k, v in node.value:
            yield from _scalars(v, seen)


def lint_workflow_yaml(content: str) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """(errors, warnings) for a workflow YAML, each ``{line, col, code, message, node_id?}``.

    Schema validity is decided by validate_workflow_raw (what save accepts);
    graph checks match what the runner's topological layering accepts.
    Read-only: never touches a DB. Hostile input (deep nesting, alias bombs)
    comes back as a ``yaml_parse`` error, never an exception. Each list is
    capped at _MAX_DIAGNOSTICS; the overflow count is one ``truncated`` warning.
    """
    try:
        errors, warnings = _lint(content)
    except (RecursionError, MemoryError):
        return [_diag("yaml_parse", _TOO_DEEP_MSG)], []
    dropped = max(0, len(errors) - _MAX_DIAGNOSTICS) + max(0, len(warnings) - _MAX_DIAGNOSTICS)
    errors, warnings = errors[:_MAX_DIAGNOSTICS], warnings[:_MAX_DIAGNOSTICS]
    if dropped:
        warnings.append(_diag("truncated", f"+{dropped} more diagnostics not shown"))
    return errors, warnings


def _lint(content: str) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    from engine.core.dag_executor import build_topological_layers  # noqa: PLC0415
    from engine.core.executor_shared import reserved_input_name  # noqa: PLC0415
    from engine.schemas.dag_node import BashNode, LoopNode, ScriptNode, validate_dag_node  # noqa: PLC0415

    errors: List[Dict[str, Any]] = []
    warnings: List[Dict[str, Any]] = []

    # One parse, the same one save runs, so `raw` is what save would see;
    # `root` keeps positions.
    try:
        root, raw = _load_bounded(content)
    except _Rejected as exc:
        return [_diag("yaml_parse", str(exc))], warnings
    except yaml_lib.YAMLError as exc:
        mark = getattr(exc, "problem_mark", None) or getattr(exc, "context_mark", None)
        pos = (mark.line + 1, mark.column + 1) if mark else None
        return [_diag("yaml_parse", f"YAML parse error: {exc}", pos)], warnings

    workflow, error = validate_workflow_raw(raw, "<inline>")
    if error is not None or workflow is None:
        msg = error.error if error else "invalid workflow"
        if msg.startswith("DAG node validation failed") and isinstance(raw, dict):
            for i, raw_node in enumerate(raw["nodes"]):
                _n, node_errors = validate_dag_node(raw_node, i)
                nid = raw_node.get("id") if isinstance(raw_node, dict) else None
                for e in node_errors:
                    errors.append(_diag("schema", e, _locate(root, ["nodes", i]),
                                        str(nid) if nid else None))
        elif msg.startswith("Schema validation failed") and isinstance(raw, dict):
            try:
                WorkflowDefinition.model_validate(raw)
            except ValidationError as exc:
                for e in exc.errors():
                    loc = list(e.get("loc", ()))
                    errors.append(_diag("schema", f"{'.'.join(map(str, loc))}: {e['msg']}",
                                        _locate(root, loc)))
        if not errors:
            errors.append(_diag("schema", msg, _locate(root, [])))
        return errors, warnings

    dag_nodes, _ = workflow.get_dag_nodes()  # all valid here, so index == YAML index
    ids = [n.id for n in dag_nodes]
    known = set(ids)

    # Declared inputs — the shapes the runner's _resolve_inputs accepts that
    # also pass the schema (list of {name}, required_inputs/optional_inputs).
    declared = {i.name for i in workflow.inputs or []}
    for key in ("required_inputs", "optional_inputs"):
        names = raw.get(key)
        if isinstance(names, list):
            declared |= {n for n in names if isinstance(n, str)}
    for j, inp in enumerate(workflow.inputs or []):
        if reserved_input_name(inp.name):
            errors.append(_diag("schema", f"input name '{inp.name}' is reserved "
                                "(would override the subprocess env); rename it",
                                _locate(root, ["inputs", j, "name"])))

    seen: set = set()
    scanned: set = set()  # scalar nodes already scanned, shared across nodes (aliases)
    for i, n in enumerate(dag_nodes):
        if n.id in seen:
            errors.append(_diag("duplicate_id", f"duplicate node id '{n.id}'",
                                _locate(root, ["nodes", i, "id"]), n.id))
        seen.add(n.id)
        for j, dep in enumerate(n.depends_on or []):
            if dep not in known:
                errors.append(_diag("unknown_dependency",
                                    f"node '{n.id}' depends on unknown node '{dep}'",
                                    _locate(root, ["nodes", i, "depends_on", j]), n.id))
        for s in _scalars(_node_at(root, ["nodes", i]), scanned):
            for rx in (_INPUT_REF_RE, _MISCASED_INPUT_REF_RE):
                for m in rx.finditer(s.value):
                    line = s.start_mark.line + 1
                    if s.style in ("|", ">"):  # block scalar body starts below the indicator
                        line += 1 + (s.value[:m.start()].count("\n") if s.style == "|" else 0)
                    pos = (line, s.start_mark.column + 1 if line == s.start_mark.line + 1 else None)
                    if m.group(1) not in declared:
                        errors.append(_diag("undeclared_input",
                                            f"node '{n.id}' references undeclared input '{m.group(1)}'",
                                            pos, n.id))
                    if rx is _MISCASED_INPUT_REF_RE:
                        warnings.append(_diag("inputs_ref_syntax",
                                              f"node '{n.id}': '{m.group(0)}' is never substituted; "
                                              f"use '$INPUTS.{m.group(1)}' (subgraphs) or "
                                              f"'${m.group(1)}' (prompt/command nodes)", pos, n.id))
        if isinstance(n, (BashNode, ScriptNode)) or (isinstance(n, LoopNode) and n.loop.until_bash):
            kind = "loop until_bash" if isinstance(n, LoopNode) else type(n).__name__[:-4].lower()
            warnings.append(_diag("risky_shell", f"node '{n.id}' runs {kind} code on this machine",
                                  _locate(root, ["nodes", i, "id"]), n.id))

    # Graph: the runner's Kahn layering counts every dep (unknown ones too),
    # so anything it can't place never runs. Split those into cycle members
    # and the nodes merely stuck behind a cycle / unknown dep.
    deps = {n.id: list(n.depends_on or []) for n in dag_nodes}
    blocked = set(known)
    progress = True
    while progress:
        progress = False
        for nid in list(blocked):
            if not any(d in blocked or d not in known for d in deps[nid]):
                blocked.discard(nid)
                progress = True
    core = set(blocked)
    for strip_no_deps in (True, False):  # drop chain heads, then chain tails
        progress = True
        while progress:
            progress = False
            for nid in list(core):
                linked = (any(d in core for d in deps[nid]) if strip_no_deps
                          else any(nid in deps[o] for o in core))
                if not linked:
                    core.discard(nid)
                    progress = True
    if core:
        members = ", ".join(sorted(core))
        for nid in sorted(core):
            errors.append(_diag("cycle", f"node '{nid}' is in a dependency cycle ({members})",
                                None, nid))
    direct_unknown = {nid for nid, ds in deps.items() if any(d not in known for d in ds)}
    for i, n in enumerate(dag_nodes):
        if n.id in blocked and n.id not in core and n.id not in direct_unknown:
            errors.append(_diag("unreachable_node",
                                f"node '{n.id}' can never run (it depends on a cycle "
                                "or an unknown node)", _locate(root, ["nodes", i, "id"]), n.id))

    if not errors:
        try:
            build_topological_layers(dag_nodes)  # the runner's own check
        except ValueError as exc:
            errors.append(_diag("cycle", str(exc)))
    return errors, warnings
