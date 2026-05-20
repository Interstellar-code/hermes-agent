"""
WorkflowRunner — owns a single run's lifecycle.

1. Reads workflow definition (YAML) from DB.
2. Parses + validates YAML.
3. Creates workflow_run row (status=pending).
4. Marks status=running, kicks off execute_dag fire-and-forget.
5. Persists events to DB via EventBus.emit().
6. Handles cancellation via asyncio.Task cancellation.

Returns immediately after creating the run row.
The background task resolves the run (completed/failed/cancelled).
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Dict, Optional

from engine.core.dag_executor import DagRunContext, execute_dag
from engine.discovery.loader import parse_workflow
from engine.store.run_store import RunStore
from engine.store.definition_store import DefinitionStore
from engine.emitter.bus import EventBus

logger = logging.getLogger("workflow.runner")


class WorkflowRunner:
    """
    Manages in-flight runs. One asyncio.Task per active run.

    Usage::

        runner = WorkflowRunner(run_store, def_store, bus)
        run = await runner.start("hello-world", {}, {"kind": "manual"})
        # run["id"] is now in status=running (background task executing)
    """

    def __init__(
        self,
        run_store: RunStore,
        def_store: DefinitionStore,
        bus: EventBus,
    ) -> None:
        self._run_store = run_store
        self._def_store = def_store
        self._bus = bus
        self._tasks: Dict[str, asyncio.Task] = {}  # run_id → Task

    # ------------------------------------------------------------------ #
    # Public API                                                          #
    # ------------------------------------------------------------------ #

    async def start(
        self,
        workflow_id: str,
        inputs: Dict[str, Any],
        trigger: Dict[str, Any],
    ) -> Dict[str, Any]:
        """
        Create a run row, then fire the DAG executor as a background task.
        Returns the run dict (status=running).
        """
        # 1. Load definition
        defn = self._def_store.get_definition(workflow_id)
        if defn is None:
            raise ValueError(f"Workflow not found: {workflow_id}")

        yaml_text: str = defn["yaml"]

        # 2. Parse to catch schema errors early
        workflow, parse_err = parse_workflow(yaml_text, f"{workflow_id}.yaml")
        if parse_err or workflow is None:
            raise ValueError(f"Workflow parse error: {parse_err.error if parse_err else 'unknown'}")

        # 3a. Validate nodes into typed DagNode objects early
        dag_nodes, node_errors = workflow.get_dag_nodes()
        if node_errors:
            raise ValueError(f"Workflow node validation errors: {node_errors}")

        # 3. Create run row
        conversation_id = trigger.get("conversation_id", f"trigger-{workflow_id}")
        working_path = trigger.get("working_path", "/tmp")
        user_message = trigger.get("user_message", f"run {workflow_id}")
        run = self._run_store.create_workflow_run(
            workflow_id=workflow_id,
            conversation_id=conversation_id,
            working_path=working_path,
            user_message=user_message,
            trigger=trigger,
        )
        run_id = run["id"]

        # 4. Mark running and emit workflow_started
        self._run_store.update_workflow_run(run_id, status="running")
        self._bus.emit(
            run_id=run_id,
            event_type="workflow_started",
            data={
                "workflow_id": workflow_id,
                "workflow_name": workflow.name,
                "trigger": trigger,
                "inputs": inputs,
            },
        )

        # 5. Fire and forget
        task = asyncio.create_task(
            self._execute(run_id, workflow_id, dag_nodes, inputs, working_path),
            name=f"run-{run_id}",
        )
        self._tasks[run_id] = task
        task.add_done_callback(lambda t: self._tasks.pop(run_id, None))

        return self._run_store.get_workflow_run(run_id)  # type: ignore[return-value]

    async def cancel(self, run_id: str) -> None:
        """Cancel a run by cancelling its asyncio Task and marking DB status."""
        task = self._tasks.get(run_id)
        if task and not task.done():
            task.cancel()
            try:
                await asyncio.wait_for(asyncio.shield(task), timeout=2.0)
            except (asyncio.CancelledError, asyncio.TimeoutError):
                pass
        self._run_store.cancel_workflow_run(run_id)
        self._bus.emit(
            run_id=run_id,
            event_type="workflow_cancelled",
            data={"reason": "user_requested"},
        )

    # ------------------------------------------------------------------ #
    # Internal execution                                                  #
    # ------------------------------------------------------------------ #

    async def _execute(
        self,
        run_id: str,
        workflow_id: str,
        dag_nodes: List[Any],
        inputs: Dict[str, Any],
        working_path: str,
    ) -> None:
        start_ms = int(time.time() * 1000)
        try:
            ctx = self._build_ctx(run_id, working_path)
            await execute_dag(dag_nodes, ctx)

            # Completed
            end_ms = int(time.time() * 1000)
            self._run_store.update_workflow_run(run_id, status="completed")
            self._bus.emit(
                run_id=run_id,
                event_type="workflow_completed",
                data={
                    "workflow_id": workflow_id,
                    "duration_ms": end_ms - start_ms,
                },
            )
        except asyncio.CancelledError:
            self._run_store.cancel_workflow_run(run_id)
            self._bus.emit(
                run_id=run_id,
                event_type="workflow_cancelled",
                data={"reason": "cancelled"},
            )
            raise
        except Exception as exc:
            logger.exception("Run %s failed: %s", run_id, exc)
            self._run_store.update_workflow_run(
                run_id, status="failed", error=str(exc)
            )
            self._bus.emit(
                run_id=run_id,
                event_type="workflow_failed",
                data={"error": str(exc)},
            )

    def _build_ctx(self, run_id: str, working_path: str) -> DagRunContext:
        run_store = self._run_store
        bus = self._bus

        def emit_event(event_type: str, payload: Dict[str, Any]) -> None:
            node_run_id = payload.pop("node_run_id", None)
            # Persist node_run row for node_started events
            if event_type == "node_started":
                node_id = payload.get("node_id", "")
                node_type = payload.get("node_type", "prompt")
                provided_nr_id = payload.get("node_run_id_hint")
                try:
                    nr = run_store.create_node_run(
                        workflow_run_id=run_id,
                        dag_node_id=node_id,
                        node_type=node_type,
                        node_run_id=provided_nr_id,
                    )
                    node_run_id = nr["id"]
                except Exception as e:
                    logger.debug("create_node_run skipped: %s", e)
            elif event_type in ("node_completed", "node_failed", "node_skipped"):
                node_id = payload.get("node_id", "")
                nr = run_store.find_node_run(run_id, node_id)
                if nr:
                    node_run_id = nr["id"]
                    patch: Dict[str, Any] = {}
                    if event_type == "node_completed":
                        patch["status"] = "completed"
                        patch["completed_at"] = int(time.time() * 1000)
                    elif event_type == "node_failed":
                        patch["status"] = "failed"
                        patch["error"] = payload.get("error", "")
                        patch["completed_at"] = int(time.time() * 1000)
                    elif event_type == "node_skipped":
                        patch["status"] = "skipped"
                        patch["skip_reason"] = payload.get("reason", "")
                        patch["completed_at"] = int(time.time() * 1000)
                    try:
                        run_store.update_node_run(nr["id"], patch)
                    except Exception as e:
                        logger.debug("update_node_run failed: %s", e)

            bus.emit(
                run_id=run_id,
                event_type=event_type,
                node_run_id=node_run_id,
                data=payload,
            )

        async def get_run_status() -> Optional[str]:
            run = run_store.get_workflow_run(run_id)
            return run["status"] if run else None

        async def pause_run(meta: Dict[str, Any]) -> None:
            run_store.pause_workflow_run(run_id, meta)
            bus.emit(run_id=run_id, event_type="approval_requested", data=meta)

        async def cancel_run() -> None:
            run_store.cancel_workflow_run(run_id)

        async def send_message(msg: str) -> None:
            bus.emit(
                run_id=run_id,
                event_type="platform_message",
                data={"message": msg},
            )

        def get_subgraph_yaml(ref: str):
            defn = self._def_store.get_definition(ref)
            if defn:
                return (defn["yaml"], defn.get("kind", "subgraph"))
            return None

        return DagRunContext(
            run_id=run_id,
            emit_event=emit_event,
            get_run_status=get_run_status,
            pause_run=pause_run,
            cancel_run=cancel_run,
            send_message=send_message,
            get_subgraph_yaml=get_subgraph_yaml,
            log_dir=working_path,
        )
