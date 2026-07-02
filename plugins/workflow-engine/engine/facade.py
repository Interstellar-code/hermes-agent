"""
WorkflowEngine facade — single object the API layer (Phase 3) calls.

All methods are async. Phase 3 wires these 1:1 to HTTP endpoints.
"""
from __future__ import annotations

import logging
import sqlite3
from typing import Any, AsyncIterator, Dict, List, Literal, Optional

from engine.store.run_store import RunStore
from engine.store.definition_store import DefinitionStore
from engine.emitter.bus import EventBus
from engine.runtime.runner import WorkflowRunner
from engine.runtime.manifest import ManifestWriter
from engine.discovery.loader import parse_workflow

logger = logging.getLogger("workflow.engine")


class WorkflowEngine:
    """
    WorkflowEngine facade.

    Lifecycle::

        engine = create_engine()          # wiring.py
        run = await engine.start_run(...)
        await engine.cancel_run(run["id"])
        async for evt in engine.subscribe_events(run["id"]):
            ...
        await engine.shutdown()
    """

    def __init__(
        self,
        *,
        conn: sqlite3.Connection,
        run_store: RunStore,
        def_store: DefinitionStore,
        bus: EventBus,
        runner: WorkflowRunner,
        manifest_writer: ManifestWriter,
        boot: Dict[str, Any],
    ) -> None:
        self._conn = conn
        self._run_store = run_store
        self._def_store = def_store
        self._bus = bus
        self._runner = runner
        self._manifest_writer = manifest_writer
        self.boot = boot

    def set_llm(self, llm: Any) -> None:
        """Inject the host-owned PluginLlm facade into the workflow runner."""
        self._runner.set_llm(llm)

    # ------------------------------------------------------------------ #
    # Definitions                                                         #
    # ------------------------------------------------------------------ #

    async def list_definitions(self, *, source: Optional[str] = None) -> List[Dict[str, Any]]:
        return self._def_store.list_definitions(source=source)

    async def get_definition(self, definition_id: str) -> Optional[Dict[str, Any]]:
        return self._def_store.get_definition(definition_id)

    async def upsert_definition(
        self,
        definition_id: str,
        yaml_text: str,
        source: str = "user",
        source_path: Optional[str] = None,
        expected_checksum: Optional[str] = None,
    ) -> Dict[str, Any]:
        row = self._def_store.upsert_definition(
            definition_id=definition_id,
            yaml_text=yaml_text,
            source=source,
            source_path=source_path,
            expected_checksum=expected_checksum,
        )
        # Refresh manifest
        self._manifest_writer.write()
        return row

    async def parse_definition(self, definition_id: str) -> Optional[Dict[str, Any]]:
        defn = self._def_store.get_definition(definition_id)
        if defn is None:
            return None
        workflow, error = parse_workflow(defn["yaml"], f"{definition_id}.yaml")
        if error or workflow is None:
            return {"id": definition_id, "error": error.error if error else "parse failed"}
        dag_nodes, _ = workflow.get_dag_nodes()
        return {
            "id": definition_id,
            "name": workflow.name,
            "description": workflow.description,
            "nodes": [
                {"id": n.id, "type": type(n).__name__.replace("Node", "").lower()}
                for n in dag_nodes
            ],
            "kind": workflow.kind or "workflow",
        }

    # ------------------------------------------------------------------ #
    # Runs                                                                #
    # ------------------------------------------------------------------ #

    async def list_runs(
        self,
        *,
        workflow_id: Optional[str] = None,
        limit: int = 50,
    ) -> List[Dict[str, Any]]:
        return self._run_store.list_workflow_runs(
            workflow_id=workflow_id,
            limit=limit,
        )

    async def get_run(self, run_id: str) -> Optional[Dict[str, Any]]:
        return self._run_store.get_workflow_run(run_id)

    async def start_run(
        self,
        workflow_id: str,
        inputs: Dict[str, Any],
        trigger: Dict[str, Any],
        *,
        priority: int = 0,
        max_runtime_s: Optional[int] = None,
    ) -> Dict[str, Any]:
        return await self._runner.start(
            workflow_id, inputs, trigger,
            priority=priority, max_runtime_s=max_runtime_s,
        )

    async def schedule_run(
        self,
        workflow_id: str,
        inputs: Dict[str, Any],
        trigger: Dict[str, Any],
        *,
        schedule: Optional[Dict[str, Any]] = None,
        priority: int = 0,
        max_runtime_s: Optional[int] = None,
    ) -> Dict[str, Any]:
        """Dispatch a run immediately, defer it, or signal cron-not-supported.

        ``schedule`` shapes::
            None | {"type": "now"}            → start_run immediately
            {"type": "at", "at": "<iso>"}     → insert into scheduled_runs
            {"type": "cron", ...}             → raises NotImplementedError
        """
        sched_type = (schedule or {}).get("type") or "now"
        if sched_type == "now":
            return await self.start_run(
                workflow_id, inputs, trigger,
                priority=priority, max_runtime_s=max_runtime_s,
            )
        if sched_type == "at":
            at_iso = (schedule or {}).get("at")
            if not isinstance(at_iso, str) or not at_iso:
                raise ValueError("schedule.at must be an ISO-8601 string")
            row = self._run_store.insert_scheduled_run(
                workflow_id=workflow_id,
                inputs=inputs,
                trigger=trigger,
                run_at=at_iso,
                priority=priority,
                max_runtime_s=max_runtime_s,
            )
            return {
                "id": row["id"],
                "status": "scheduled",
                "scheduled_for": at_iso,
            }
        if sched_type == "cron":
            raise NotImplementedError("cron schedule not yet supported")
        raise ValueError(f"unknown schedule.type: {sched_type!r}")

    async def list_active_node_runs(self) -> List[Dict[str, Any]]:
        return self._run_store.list_active_node_runs()

    async def fire_due_scheduled_runs(self) -> int:
        """Scheduler-tick helper: claim+fire due rows. Returns count fired."""
        from datetime import datetime, timezone
        now_iso = datetime.now(tz=timezone.utc).isoformat()
        due = self._run_store.list_due_scheduled_runs(now_iso)
        fired = 0
        for row in due:
            if not self._run_store.claim_scheduled_run(row["id"], now_iso):
                continue
            try:
                await self.start_run(
                    row["workflow_id"],
                    row.get("inputs") or {},
                    row.get("trigger") or {},
                    priority=row.get("priority") or 0,
                    max_runtime_s=row.get("max_runtime_s"),
                )
                self._run_store.mark_scheduled_fired(row["id"])
                fired += 1
            except Exception as exc:
                logger.exception(
                    "fire_due_scheduled_runs: start_run failed for %s: %s",
                    row["id"], exc,
                )
                self._run_store.mark_scheduled_failed(row["id"])
        return fired

    async def wait_for_run(
        self,
        run_id: str,
        timeout: Optional[float] = None,
    ) -> Optional[Dict[str, Any]]:
        """Block until the run settles, then return its final row.

        Settles == status in ``{completed, failed, cancelled, paused}``.
        Paused counts as settled because there's nothing for the engine
        to do until an out-of-band ``approve()`` arrives — making the
        caller wait further would deadlock the agent tool that just
        started the run.

        Used by in-process callers (the workflow_run agent tool) whose
        own event loop stops pumping the moment they return — without
        this method their fire-and-forget ``start_run`` would be
        orphaned. Dashboard callers (long-lived uvicorn loop) keep
        using bare ``start_run`` and don't need to block.

        ``timeout`` is in seconds; ``None`` waits indefinitely. On
        timeout the latest run row is returned anyway (status will
        still be ``running``); callers decide what to do with it.
        """
        await self._runner.wait_for(run_id, timeout=timeout)
        return self._run_store.get_workflow_run(run_id)

    async def cancel_run(self, run_id: str) -> None:
        await self._runner.cancel(run_id)

    # ------------------------------------------------------------------ #
    # Approvals                                                           #
    # ------------------------------------------------------------------ #

    async def approve(
        self,
        run_id: str,
        node_id: str,
        decision: Literal["approve", "reject"],
        comment: Optional[str] = None,
    ) -> None:
        """
        Process an approval decision.

        1. Find the paused node_run for (run_id, node_id).
        2. Atomic CAS: update status paused → completed/failed.
        3. If claimed: emit approval_received, resume the workflow run.
        """
        nr = self._run_store.find_node_run(run_id, node_id)
        if nr is None:
            raise ValueError(f"Node run not found: run={run_id} node={node_id}")

        claimed = self._run_store.try_claim_approval(nr["id"], decision, comment)
        if not claimed:
            logger.warning(
                "approve: node_run %s was not in 'paused' state (already processed?)",
                nr["id"],
            )
            return

        self._bus.emit(
            run_id=run_id,
            event_type="approval_received",
            node_run_id=nr["id"],
            data={
                "node_id": node_id,
                "decision": decision,
                "comment": comment,
            },
        )

        if decision == "approve":
            self._run_store.resume_workflow_run(run_id)
            # Emit so subscribers know the run is live again
            self._bus.emit(
                run_id=run_id,
                event_type="workflow_resumed",
                data={"node_id": node_id},
            )
            # Restart DAG execution from the next layer. Without this, the
            # run stays in 'running' status but no nodes actually execute.
            try:
                await self._runner.resume(run_id)
            except Exception as exc:
                logger.exception("approve: runner.resume failed run=%s: %s", run_id, exc)
                self._run_store.update_workflow_run(
                    run_id,
                    status="failed",
                    error=f"Resume failed: {exc}",
                )
                self._bus.emit(
                    run_id=run_id,
                    event_type="workflow_failed",
                    data={"error": f"Resume failed: {exc}"},
                )
        else:
            # Reject → fail the run
            self._run_store.update_workflow_run(
                run_id,
                status="failed",
                error=f"Rejected at node {node_id}: {comment or 'no comment'}",
            )
            self._bus.emit(
                run_id=run_id,
                event_type="workflow_failed",
                data={
                    "error": f"Rejected at node {node_id}",
                    "node_id": node_id,
                },
            )

    # ------------------------------------------------------------------ #
    # Extended definitions                                                #
    # ------------------------------------------------------------------ #

    async def mark_user_edit(
        self,
        definition_id: str,
        yaml_text: str,
        expected_checksum: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Edit a bundled workflow in-place; keeps source='bundled', sets user_modified=1."""
        row = self._def_store.mark_user_edit(
            definition_id, yaml_text, expected_checksum=expected_checksum
        )
        self._manifest_writer.write()
        return row

    async def reset_to_factory(
        self,
        definition_id: str,
        factory_yaml: str,
    ) -> Dict[str, Any]:
        """Reset a bundled workflow to factory yaml; clears user_modified."""
        row = self._def_store.reset_to_factory(definition_id, factory_yaml)
        self._manifest_writer.write()
        return row

    async def delete_definition(self, definition_id: str) -> int:
        """Delete a non-bundled definition. Returns rows deleted."""
        rows = self._def_store.delete_definition(definition_id)
        if rows > 0:
            self._manifest_writer.write()
        return rows

    # ------------------------------------------------------------------ #
    # Extended runs                                                        #
    # ------------------------------------------------------------------ #

    async def find_run_by_conversation_id(self, conversation_id: str) -> Optional[Dict[str, Any]]:
        return self._run_store.find_run_by_conversation_id(conversation_id)

    async def get_active_run_by_path(self, scope_path: str) -> Optional[Dict[str, Any]]:
        return self._run_store.get_active_run_by_path(scope_path)

    async def resume_run(self, run_id: str) -> Optional[Dict[str, Any]]:
        self._run_store.resume_workflow_run(run_id)
        return self._run_store.get_workflow_run(run_id)

    # ------------------------------------------------------------------ #
    # Extended node runs                                                  #
    # ------------------------------------------------------------------ #

    async def list_node_runs(self, run_id: str) -> List[Dict[str, Any]]:
        return self._run_store.list_node_runs(run_id)

    async def find_node_run_by_id(self, node_run_id: str) -> Optional[Dict[str, Any]]:
        return self._run_store.find_node_run_by_id(node_run_id)

    # ------------------------------------------------------------------ #
    # Extended events                                                      #
    # ------------------------------------------------------------------ #

    async def append_workflow_event(self, event: Dict[str, Any]) -> None:
        self._run_store.append_workflow_event(
            workflow_run_id=event["workflow_run_id"],
            event_type=event["event_type"],
            node_run_id=event.get("node_run_id"),
            step_index=event.get("step_index"),
            step_name=event.get("step_name"),
            data=event.get("data"),
            event_id=event.get("id"),
            created_at=event.get("created_at"),
        )

    async def list_recent_workflow_events(self, run_id: str, limit: int = 200) -> List[Dict[str, Any]]:
        return self._run_store.list_recent_events(run_id, limit=limit)

    # ------------------------------------------------------------------ #
    # Extended phase transitions                                           #
    # ------------------------------------------------------------------ #

    async def record_phase_transition(
        self,
        *,
        run_id: str,
        to_phase: str,
        decided_by: str,
        decision_data: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        return self._run_store.record_phase_transition(
            run_id=run_id,
            to_phase=to_phase,
            decided_by=decided_by,
            decision_data=decision_data,
        )

    async def list_phase_transitions(self, run_id: str) -> List[Dict[str, Any]]:
        return self._run_store.list_phase_transitions(run_id)

    # ------------------------------------------------------------------ #
    # Extended approvals                                                   #
    # ------------------------------------------------------------------ #

    async def try_claim_approval_for_resume(
        self,
        node_run_id: str,
        decision: Literal["approved", "rejected"],
        approval_response: str,
    ) -> Dict[str, Any]:
        return self._run_store.try_claim_approval_for_resume(
            node_run_id, decision, approval_response
        )

    # ------------------------------------------------------------------ #
    # Events / SSE                                                        #
    # ------------------------------------------------------------------ #

    def subscribe_events(
        self, run_id: Optional[str] = None
    ) -> AsyncIterator[Dict[str, Any]]:
        """
        Return an async iterator of events.
        Replays last 50 DB events then streams live events.
        """
        return self._bus.subscribe(run_id)

    # ------------------------------------------------------------------ #
    # Lifecycle                                                           #
    # ------------------------------------------------------------------ #

    async def shutdown(self) -> None:
        """Close event bus and DB connection."""
        self._bus.close_all()
        try:
            self._conn.close()
        except Exception:
            pass
        logger.info("WorkflowEngine shut down.")
