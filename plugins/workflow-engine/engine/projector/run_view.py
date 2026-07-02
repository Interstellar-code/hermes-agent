"""
Projector — reduces a sequence of RunEvents into a RunView dict.

project_run(events) is a pure function.
Output shape mirrors the TS projector output byte-for-byte (modulo ordering).

TS reference: src/server/workflow-engine/projector/node-runs-projector.ts
The Python projector operates on the same event sequence but produces a
summary RunView rather than writing to DB rows (that is the store's job).
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Literal, Optional


NodeRunStatus = Literal[
    "pending", "ready", "running", "paused",
    "completed", "failed", "cancelled", "skipped",
]

RunStatus = Literal[
    "pending", "running", "paused", "completed", "failed", "cancelled",
]


@dataclass
class NodeRunView:
    id: str
    dag_node_id: str
    node_type: str
    status: str = "pending"
    started_at: Optional[int] = None
    completed_at: Optional[int] = None
    duration_ms: Optional[int] = None
    error: Optional[str] = None
    skip_reason: Optional[str] = None
    loop_iteration: Optional[int] = None
    approval_message: Optional[str] = None
    summary: Optional[str] = None
    cost_usd: Optional[float] = None
    num_turns: Optional[int] = None
    stop_reason: Optional[str] = None
    parent_subgraph_node_run_id: Optional[str] = None


@dataclass
class RunView:
    run_id: str
    workflow_id: str = ""
    status: str = "pending"
    started_at: Optional[int] = None
    completed_at: Optional[int] = None
    duration_ms: Optional[int] = None
    error: Optional[str] = None
    node_runs: List[NodeRunView] = field(default_factory=list)
    event_count: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "run_id": self.run_id,
            "workflow_id": self.workflow_id,
            "status": self.status,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "duration_ms": self.duration_ms,
            "error": self.error,
            "event_count": self.event_count,
            "node_runs": [
                {
                    "id": nr.id,
                    "dag_node_id": nr.dag_node_id,
                    "node_type": nr.node_type,
                    "status": nr.status,
                    "started_at": nr.started_at,
                    "completed_at": nr.completed_at,
                    "duration_ms": nr.duration_ms,
                    "error": nr.error,
                    "skip_reason": nr.skip_reason,
                    "loop_iteration": nr.loop_iteration,
                    "approval_message": nr.approval_message,
                    "summary": nr.summary,
                    "cost_usd": nr.cost_usd,
                    "num_turns": nr.num_turns,
                    "stop_reason": nr.stop_reason,
                    "parent_subgraph_node_run_id": nr.parent_subgraph_node_run_id,
                }
                for nr in self.node_runs
            ],
        }


def _now_ms() -> int:
    return int(time.time() * 1000)


def project_run(events: List[Dict[str, Any]]) -> RunView:
    """
    Pure function: fold a sequence of RunEvent dicts into a RunView.

    Each event dict must have at minimum:
        run_id: str
        event_type: str
        data: dict (optional payload)
        node_run_id: str | None
    """
    if not events:
        return RunView(run_id="")

    # Derive run_id from first event
    run_id = events[0].get("run_id", "")
    view = RunView(run_id=run_id)
    view.event_count = len(events)

    # node_run_id → NodeRunView (keyed by node_run_id when available, else dag_node_id+iteration)
    nr_by_id: Dict[str, NodeRunView] = {}
    # dag_node_id+iter → node_run_id (to handle events that only carry dag_node_id)
    dag_to_nr: Dict[str, str] = {}

    def _nr_key(dag_node_id: str, loop_iteration: Optional[int]) -> str:
        return f"{dag_node_id}:{loop_iteration}"

    def _get_or_create_nr(
        node_run_id: Optional[str],
        dag_node_id: str,
        node_type: str = "prompt",
        loop_iteration: Optional[int] = None,
    ) -> NodeRunView:
        key = _nr_key(dag_node_id, loop_iteration)
        if node_run_id and node_run_id in nr_by_id:
            return nr_by_id[node_run_id]
        if key in dag_to_nr:
            return nr_by_id[dag_to_nr[key]]
        nr_id = node_run_id or f"{dag_node_id}_{loop_iteration or 0}"
        nr = NodeRunView(id=nr_id, dag_node_id=dag_node_id, node_type=node_type, loop_iteration=loop_iteration)
        nr_by_id[nr_id] = nr
        dag_to_nr[key] = nr_id
        view.node_runs.append(nr)
        return nr

    def _find_nr(dag_node_id: str, loop_iteration: Optional[int] = None) -> Optional[NodeRunView]:
        key = _nr_key(dag_node_id, loop_iteration)
        nr_id = dag_to_nr.get(key)
        if nr_id:
            return nr_by_id.get(nr_id)
        return None

    for evt in events:
        etype = evt.get("event_type", "")
        data: Dict[str, Any] = evt.get("data") or {}
        node_run_id: Optional[str] = evt.get("node_run_id")
        created_ms: Optional[int] = None
        ca = evt.get("created_at")
        if ca is not None:
            if hasattr(ca, "timestamp"):
                created_ms = int(ca.timestamp() * 1000)
            else:
                created_ms = int(ca)

        if etype == "workflow_started":
            view.status = "running"
            if created_ms:
                view.started_at = created_ms
            view.workflow_id = data.get("workflow_id", view.workflow_id)

        elif etype == "workflow_completed":
            view.status = "completed"
            if created_ms:
                view.completed_at = created_ms
            if view.started_at and view.completed_at:
                view.duration_ms = view.completed_at - view.started_at

        elif etype == "workflow_failed":
            view.status = "failed"
            view.error = data.get("error", "")
            if created_ms:
                view.completed_at = created_ms

        elif etype == "workflow_cancelled":
            view.status = "cancelled"
            if created_ms:
                view.completed_at = created_ms

        elif etype == "node_started":
            dag_node_id = data.get("node_id", "")
            node_type = data.get("node_type", "prompt")
            nr = _get_or_create_nr(node_run_id, dag_node_id, node_type)
            nr.status = "running"
            nr.started_at = created_ms
            if data.get("parent_subgraph_node_run_id"):
                nr.parent_subgraph_node_run_id = data["parent_subgraph_node_run_id"]

        elif etype == "node_completed":
            dag_node_id = data.get("node_id", "")
            nr = _find_nr(dag_node_id)
            if nr:
                nr.status = "completed"
                nr.completed_at = created_ms
                if nr.started_at and nr.completed_at:
                    nr.duration_ms = nr.completed_at - nr.started_at
                if "cost_usd" in data:
                    nr.cost_usd = data["cost_usd"]
                if "num_turns" in data:
                    nr.num_turns = data["num_turns"]
                if "stop_reason" in data:
                    nr.stop_reason = data["stop_reason"]

        elif etype == "node_failed":
            dag_node_id = data.get("node_id", "")
            nr = _find_nr(dag_node_id)
            if nr:
                nr.status = "failed"
                nr.error = data.get("error", "")
                nr.completed_at = created_ms

        elif etype == "node_skipped":
            dag_node_id = data.get("node_id", "")
            nr = _get_or_create_nr(node_run_id, dag_node_id, "prompt")
            nr.status = "skipped"
            nr.skip_reason = data.get("reason")
            nr.completed_at = created_ms

        elif etype == "node_skipped_prior_success":
            dag_node_id = data.get("node_id", "")
            nr = _get_or_create_nr(node_run_id, dag_node_id, "prompt")
            nr.status = "skipped"
            nr.skip_reason = "prior_success"
            nr.completed_at = created_ms

        elif etype == "approval_requested":
            dag_node_id = data.get("node_id", "")
            nr = _find_nr(dag_node_id)
            if nr:
                nr.status = "paused"
                nr.approval_message = data.get("message")

        elif etype == "loop_iteration_started":
            dag_node_id = data.get("node_id", "")
            if dag_node_id:
                iteration = data.get("iteration", 0)
                _get_or_create_nr(node_run_id, dag_node_id, "prompt", iteration)

        elif etype == "loop_iteration_completed":
            dag_node_id = data.get("node_id", "")
            iteration = data.get("iteration")
            if dag_node_id:
                nr = _find_nr(dag_node_id, iteration)
                if nr:
                    nr.status = "completed"
                    nr.completed_at = created_ms

        elif etype == "loop_iteration_failed":
            dag_node_id = data.get("node_id", "")
            iteration = data.get("iteration")
            if dag_node_id:
                nr = _find_nr(dag_node_id, iteration)
                if nr:
                    nr.status = "failed"
                    nr.error = data.get("error", "")
                    nr.completed_at = created_ms

    return view
