"""
WorkflowEngine facade — single object the API layer (Phase 3) calls.

All methods are async. Phase 3 wires these 1:1 to HTTP endpoints.

Engine loop (L2-07): every async method runs on ONE long-lived event loop
owned by the engine (a daemon thread). Callers — agent tools on throwaway
per-call loops, the dashboard's uvicorn loop, the daemon — just await; the
coroutine is marshalled with run_coroutine_threadsafe. Run tasks therefore
outlive the tool call that started them, and sqlite I/O never runs on the
caller's loop (L2-11). Store access from other threads is serialized by
engine.store.run_store.STORE_LOCK (L2-12).
"""
from __future__ import annotations

import asyncio
import functools
import logging
import sqlite3
import threading
import time
from typing import Any, AsyncIterator, Dict, List, Literal, Optional

from engine.store.run_store import RunStore
from engine.store.definition_store import DefinitionStore
from engine.emitter.bus import EventBus
from engine.runtime.runner import WorkflowRunner
from engine.runtime.manifest import ManifestWriter
from engine.discovery.loader import parse_workflow

logger = logging.getLogger("workflow.engine")


def _on_engine_loop(fn):
    """Run the wrapped coroutine method on the engine's own loop."""
    @functools.wraps(fn)
    async def wrapper(self: "WorkflowEngine", *args: Any, **kwargs: Any) -> Any:
        coro = fn(self, *args, **kwargs)
        if asyncio.get_running_loop() is self._loop:
            return await coro
        return await asyncio.wrap_future(
            asyncio.run_coroutine_threadsafe(coro, self._loop)
        )
    return wrapper


def _parsed_payload(yaml_text: str, definition_id: str) -> Dict[str, Any]:
    """Node list + metadata of a YAML, or ``{id, error}`` when it won't parse."""
    workflow, error = parse_workflow(yaml_text, f"{definition_id}.yaml")
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
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(
            target=self._loop.run_forever, name="workflow-engine-loop", daemon=True,
        )
        self._thread.start()
        asyncio.run_coroutine_threadsafe(self._runner.heartbeat_forever(), self._loop)

    @property
    def db_path(self) -> Optional[str]:
        """File path of the opened DB (None for :memory:)."""
        return self._conn.execute("PRAGMA database_list").fetchone()[2] or None

    def set_owner_session(self, run_id: str, session_id: str) -> None:
        """Record the session that started ``run_id`` (approve/cancel ownership)."""
        self._run_store.set_owner_session(run_id, session_id)

    def set_llm(self, llm: Any) -> None:
        """Inject the host-owned PluginLlm facade into the workflow runner."""
        self._runner.set_llm(llm)

    # ------------------------------------------------------------------ #
    # Definitions                                                         #
    # ------------------------------------------------------------------ #

    @_on_engine_loop
    async def list_definitions(self, *, source: Optional[str] = None) -> List[Dict[str, Any]]:
        return self._def_store.list_definitions(source=source)

    @_on_engine_loop
    async def get_definition(self, definition_id: str) -> Optional[Dict[str, Any]]:
        return self._def_store.get_definition(definition_id)

    @_on_engine_loop
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

    @_on_engine_loop
    async def parse_definition(self, definition_id: str) -> Optional[Dict[str, Any]]:
        defn = self._def_store.get_definition(definition_id)
        if defn is None:
            return None
        return _parsed_payload(defn["yaml"], definition_id)

    # ------------------------------------------------------------------ #
    # Runs                                                                #
    # ------------------------------------------------------------------ #

    @_on_engine_loop
    async def list_runs(
        self,
        *,
        workflow_id: Optional[str] = None,
        statuses: Optional[List[str]] = None,
        limit: int = 50,
        parent_run_id: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        return self._run_store.list_workflow_runs(
            workflow_id=workflow_id,
            statuses=statuses,
            limit=limit,
            parent_run_id=parent_run_id,
        )

    @_on_engine_loop
    async def get_run(self, run_id: str) -> Optional[Dict[str, Any]]:
        return self._run_store.get_workflow_run(run_id)

    @_on_engine_loop
    async def get_run_definition(self, run_id: str) -> Optional[Dict[str, Any]]:
        """The definition a run executes: its pinned snapshot, else (pre-009
        run) the current one. None when the run is missing; ``definition``
        is None when neither exists any more."""
        run = self._run_store.get_workflow_run(run_id)
        if run is None:
            return None
        wf_id = run["workflow_id"]
        current = self._def_store.get_definition(wf_id)
        snap = run.get("definition_checksum") and self._run_store.get_definition_snapshot(
            wf_id, run["definition_checksum"],
        )
        src = snap or current
        if not src:
            return {"definition": None, "parsed": None}
        return {
            "definition": {
                "workflow_id": wf_id,
                "checksum": src["checksum"],
                "version": src.get("version"),
                "yaml": src["yaml"],
                "source": "snapshot" if snap else "current",
                "pinned": bool(snap),
                "current_checksum": current["checksum"] if current else None,
                "current_updated_at": current["updated_at"] if current else None,
            },
            "parsed": _parsed_payload(src["yaml"], wf_id),
        }

    @_on_engine_loop
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

    @_on_engine_loop
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
            {"type": "cron", "cron": "<expr>"} → recurring scheduled_runs row
                (host-local TZ; ValueError when invalid, NotImplementedError
                when croniter is missing)
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
            from engine.cron import schedule as cron  # noqa: PLC0415
            expr = (schedule or {}).get("cron") or (schedule or {}).get("cron_expr")
            try:
                cron.validate_cron(expr)
            except ImportError as exc:
                raise NotImplementedError("cron schedules need croniter") from exc
            expr = expr.strip()
            next_iso = cron.to_iso(cron.next_fire(expr, time.time()))
            row = self._run_store.insert_scheduled_run(
                workflow_id=workflow_id,
                inputs=inputs,
                trigger={**trigger, "tz": cron.local_tz_name()},
                run_at=next_iso,
                priority=priority,
                max_runtime_s=max_runtime_s,
                cron_expr=expr,
            )
            return {
                "id": row["id"],
                "status": "scheduled",
                "cron": expr,
                "next_run_at": next_iso,
                "scheduled_for": next_iso,
            }
        raise ValueError(f"unknown schedule.type: {sched_type!r}")

    @_on_engine_loop
    async def list_active_node_runs(self) -> List[Dict[str, Any]]:
        return self._run_store.list_active_node_runs()

    @_on_engine_loop
    async def fire_due_scheduled_runs(
        self, now_iso: Optional[str] = None, *, stale_firing_s: float = 20.0,
    ) -> int:
        """Scheduler-tick helper: claim+fire due rows. Returns count fired.

        First resets rows left 'firing' for longer than ``stale_firing_s``
        (2x the tick interval; a tick that died mid-fire). Cron rows are
        always rescheduled to their next occurrence after now — on success and
        on failure (``trigger_json.last_error``); they never end 'failed' and
        missed occurrences are not backfilled.
        """
        from datetime import datetime, timedelta, timezone
        from engine.cron import schedule as cron  # noqa: PLC0415
        now_dt = datetime.fromisoformat(now_iso) if now_iso else datetime.now(tz=timezone.utc)
        now_iso = now_dt.isoformat()
        reset = self._run_store.reset_stale_firing(
            (now_dt - timedelta(seconds=stale_firing_s)).isoformat()
        )
        if reset:
            logger.warning("fire_due_scheduled_runs: reset %d stale 'firing' rows", reset)
        due = self._run_store.list_due_scheduled_runs(now_iso)
        fired = 0
        for row in due:
            if not self._run_store.claim_scheduled_run(row["id"], now_iso):
                continue
            expr = row.get("cron_expr")
            trigger = row.get("trigger") or {}
            if expr:
                trigger = {
                    k: v for k, v in trigger.items()
                    if k not in ("last_error", "last_run_id", "claimed_at")
                }
                trigger.update(kind="cron", schedule_id=row["id"], cron_expr=expr)
            run_id: Optional[str] = None
            error: Optional[str] = None
            try:
                run = await self.start_run(
                    row["workflow_id"],
                    row.get("inputs") or {},
                    trigger,
                    priority=row.get("priority") or 0,
                    max_runtime_s=row.get("max_runtime_s"),
                )
                run_id = run.get("id") if isinstance(run, dict) else None
                fired += 1
            except Exception as exc:
                logger.exception(
                    "fire_due_scheduled_runs: start_run failed for %s: %s",
                    row["id"], exc,
                )
                error = f"{type(exc).__name__}: {exc}"[:500]
            if not expr:
                if error is None:
                    self._run_store.mark_scheduled_fired(row["id"])
                else:
                    self._run_store.mark_scheduled_failed(row["id"])
                continue
            try:
                next_iso = cron.to_iso(cron.next_fire(expr, now_dt.timestamp()))
            except Exception as exc:  # stored expr went bad: retry in an hour
                error = error or f"next fire failed: {exc}"[:500]
                next_iso = (now_dt + timedelta(hours=1)).isoformat()
            self._run_store.reschedule_cron_row(
                row["id"], next_iso, last_error=error, last_run_id=run_id,
            )
        return fired

    # ------------------------------------------------------------------ #
    # Schedules (native cron + deferred "at")                             #
    # ------------------------------------------------------------------ #

    @_on_engine_loop
    async def list_schedules(self, workflow_id: Optional[str] = None) -> List[Dict[str, Any]]:
        return self._run_store.list_schedules(workflow_id)

    @_on_engine_loop
    async def set_schedule_enabled(self, schedule_id: str, enabled: bool) -> Optional[Dict[str, Any]]:
        """Disable, or re-enable (cron: run_at recomputed from now). None if
        the schedule is missing or no longer live (fired / cancelled / failed)."""
        row = self._run_store.get_scheduled_run(schedule_id)
        if row is None or row["status"] not in ("pending", "firing", "disabled"):
            return None
        run_at = None
        if enabled and row["cron"]:
            from engine.cron import schedule as cron  # noqa: PLC0415
            run_at = cron.to_iso(cron.next_fire(row["cron"], time.time()))
        self._run_store.set_schedule_status(
            schedule_id, "pending" if enabled else "disabled", run_at=run_at,
        )
        return self._run_store.get_scheduled_run(schedule_id)

    @_on_engine_loop
    async def cancel_schedule(self, schedule_id: str) -> bool:
        return self._run_store.set_schedule_status(schedule_id, "cancelled")

    @_on_engine_loop
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

    @_on_engine_loop
    async def cancel_run(self, run_id: str) -> None:
        await self._runner.cancel(run_id)

    # ------------------------------------------------------------------ #
    # Approvals                                                           #
    # ------------------------------------------------------------------ #

    @_on_engine_loop
    async def approve(
        self,
        run_id: str,
        node_id: str,
        decision: Literal["approve", "reject"],
        comment: Optional[str] = None,
        actor: Optional[str] = None,
    ) -> None:
        """
        Process an approval decision.

        1. Find the paused node_run for (run_id, node_id).
        2. Atomic CAS: update status paused → completed/failed, recording
           ``actor`` (self-reported approver) + decided_at in its metadata.
        3. If claimed: emit approval_received, resume the workflow run.
        """
        nr = self._run_store.find_node_run(run_id, node_id)
        if nr is None:
            raise ValueError(f"Node run not found: run={run_id} node={node_id}")

        claimed = self._run_store.try_claim_approval(nr["id"], decision, comment, actor)
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
                "approved_by": actor,
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
                self._fail_run(run_id, f"Resume failed: {exc}", ("running", "paused"))
        else:
            # Reject → fail the run (CAS: never clobber a run that already
            # finished or was cancelled meanwhile).
            if not self._fail_run(
                run_id,
                f"Rejected at node {node_id}: {comment or 'no comment'}",
                ("paused",),
                emit=False,
            ):
                return
            self._bus.emit(
                run_id=run_id,
                event_type="workflow_failed",
                data={
                    "error": f"Rejected at node {node_id}",
                    "node_id": node_id,
                },
            )

    def _fail_run(self, run_id: str, error: str, from_statuses: tuple, emit: bool = True) -> bool:
        won = self._run_store.finish_workflow_run_if_running(
            run_id, status="failed", error=error, from_statuses=from_statuses,
        )
        if won:
            self._run_store.record_phase_transition(
                run_id=run_id, to_phase="failed", decided_by="user",
                decision_data={"error": error},
            )
            if emit:
                self._bus.emit(run_id=run_id, event_type="workflow_failed", data={"error": error})
        return won

    # ------------------------------------------------------------------ #
    # Extended definitions                                                #
    # ------------------------------------------------------------------ #

    @_on_engine_loop
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

    @_on_engine_loop
    async def reset_to_factory(
        self,
        definition_id: str,
        factory_yaml: str,
    ) -> Dict[str, Any]:
        """Reset a bundled workflow to factory yaml; clears user_modified."""
        row = self._def_store.reset_to_factory(definition_id, factory_yaml)
        self._manifest_writer.write()
        return row

    @_on_engine_loop
    async def delete_definition(self, definition_id: str) -> int:
        """Delete a non-bundled definition. Returns rows deleted."""
        rows = self._def_store.delete_definition(definition_id)
        if rows > 0:
            self._manifest_writer.write()
        return rows

    # ------------------------------------------------------------------ #
    # Extended runs                                                        #
    # ------------------------------------------------------------------ #

    @_on_engine_loop
    async def find_run_by_conversation_id(self, conversation_id: str) -> Optional[Dict[str, Any]]:
        return self._run_store.find_run_by_conversation_id(conversation_id)

    @_on_engine_loop
    async def get_active_run_by_path(self, scope_path: str) -> Optional[Dict[str, Any]]:
        return self._run_store.get_active_run_by_path(scope_path)

    @_on_engine_loop
    async def resume_run(self, run_id: str) -> Optional[Dict[str, Any]]:
        """Resume a paused run for real (L2-09): CAS paused→running, then
        restart the DAG via the runner.

        Raises ValueError when the run is not paused, or when it is paused at
        an approval / interactive-loop gate — those need an explicit
        ``approve`` decision (workflow_approve), never a silent bypass.
        """
        gate = next(
            (nr for nr in self._run_store.list_node_runs(run_id) if nr.get("status") == "paused"),
            None,
        )
        if gate is not None:
            raise ValueError(
                f"run {run_id} is waiting on approval at node '{gate['dag_node_id']}'; "
                "use workflow_approve"
            )
        if not self._run_store.resume_workflow_run(run_id):
            raise ValueError(f"run {run_id} is not paused")
        await self._runner.resume(run_id)
        return self._run_store.get_workflow_run(run_id)

    # ------------------------------------------------------------------ #
    # Extended node runs                                                  #
    # ------------------------------------------------------------------ #

    @_on_engine_loop
    async def list_node_runs(self, run_id: str) -> List[Dict[str, Any]]:
        return self._run_store.list_node_runs(run_id)

    @_on_engine_loop
    async def find_node_run_by_id(self, node_run_id: str) -> Optional[Dict[str, Any]]:
        return self._run_store.find_node_run_by_id(node_run_id)

    # ------------------------------------------------------------------ #
    # Extended events                                                      #
    # ------------------------------------------------------------------ #

    @_on_engine_loop
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

    @_on_engine_loop
    async def list_recent_workflow_events(self, run_id: str, limit: int = 200) -> List[Dict[str, Any]]:
        return self._run_store.list_recent_events(run_id, limit=limit)

    @_on_engine_loop
    async def query_workflow_events(
        self,
        run_id: str,
        *,
        limit: int,
        after: Optional[int] = None,
        node_run_id: Optional[str] = None,
        types: Optional[List[str]] = None,
    ) -> List[Dict[str, Any]]:
        """``after`` given: forward page by seq; else the newest ``limit``.
        node_log is only returned when listed in ``types``."""
        if after is not None:
            return self._run_store.list_events_after(
                run_id, after, limit, node_run_id=node_run_id, types=types,
                exclude_types=("node_log",),
            )
        return self._run_store.list_recent_events(
            run_id, limit=limit, node_run_id=node_run_id, types=types,
        )

    # ------------------------------------------------------------------ #
    # Extended phase transitions                                           #
    # ------------------------------------------------------------------ #

    @_on_engine_loop
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

    @_on_engine_loop
    async def list_phase_transitions(self, run_id: str) -> List[Dict[str, Any]]:
        return self._run_store.list_phase_transitions(run_id)

    # ------------------------------------------------------------------ #
    # Events / SSE                                                        #
    # ------------------------------------------------------------------ #

    def subscribe_events(
        self, run_id: Optional[str] = None, *, tail_interval_s: Optional[float] = None,
    ) -> AsyncIterator[Dict[str, Any]]:
        """
        Return an async iterator of events.
        Replays last 50 DB events then streams live events; with
        ``tail_interval_s`` (run-scoped) also tails the DB for events
        persisted by other processes.
        """
        return self._bus.subscribe(run_id, tail_interval_s=tail_interval_s)

    # ------------------------------------------------------------------ #
    # Lifecycle                                                           #
    # ------------------------------------------------------------------ #

    async def shutdown(self) -> None:
        """Cancel engine-loop tasks, stop the loop thread, close the DB."""
        self._bus.close_all()

        async def _drain() -> None:
            tasks = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

        if self._loop.is_running() and threading.current_thread() is not self._thread:
            await asyncio.wrap_future(asyncio.run_coroutine_threadsafe(_drain(), self._loop))
            self._loop.call_soon_threadsafe(self._loop.stop)
            await asyncio.to_thread(self._thread.join, 5)
            self._loop.close()
        try:
            self._conn.close()
        except Exception:
            pass
        logger.info("WorkflowEngine shut down.")
