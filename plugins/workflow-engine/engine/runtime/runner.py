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
from typing import Any, Dict, List, Optional

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
        llm: Any = None,
    ) -> None:
        self._run_store = run_store
        self._def_store = def_store
        self._bus = bus
        self._llm = llm
        self._tasks: Dict[str, asyncio.Task] = {}  # run_id → Task

    def set_llm(self, llm: Any) -> None:
        """Inject the host-owned PluginLlm facade used by prompt/command nodes."""
        self._llm = llm

    # ------------------------------------------------------------------ #
    # Public API                                                          #
    # ------------------------------------------------------------------ #

    async def start(
        self,
        workflow_id: str,
        inputs: Dict[str, Any],
        trigger: Dict[str, Any],
        *,
        priority: int = 0,
        max_runtime_s: Optional[int] = None,
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
            priority=priority,
            max_runtime_s=max_runtime_s,
        )
        run_id = run["id"]

        # 4. Mark running and emit workflow_started
        self._run_store.update_workflow_run(run_id, status="running")
        self._run_store.record_phase_transition(
            run_id=run_id,
            to_phase="running",
            decided_by="system",
            decision_data={"trigger": trigger},
        )
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
            self._execute(
                run_id, workflow_id, dag_nodes, inputs, working_path,
                max_runtime_s=max_runtime_s,
            ),
            name=f"run-{run_id}",
        )
        self._register_task(run_id, task)

        return self._run_store.get_workflow_run(run_id)  # type: ignore[return-value]

    def _register_task(self, run_id: str, task: asyncio.Task) -> None:
        """Track ``task`` for ``run_id`` and arrange for self-cleanup.

        The done-callback only pops the slot when it still points at this
        task — without that identity check, a resume that overwrites
        ``self._tasks[run_id]`` would later see its new entry deleted when
        the *prior* task finishes, leaving cancel/shutdown blind to the
        live task.
        """
        self._tasks[run_id] = task

        def _cleanup(done: asyncio.Task, _run_id: str = run_id) -> None:
            if self._tasks.get(_run_id) is done:
                self._tasks.pop(_run_id, None)

        task.add_done_callback(_cleanup)

    async def resume(self, run_id: str) -> None:
        """
        Restart DAG execution after a pause (e.g. post-approval). Reloads the
        workflow definition, builds `prior_completed` from node_runs that are
        already terminal, and fires a fresh _execute task. The DAG executor
        skips any node whose id is in prior_completed.
        """
        run = self._run_store.get_workflow_run(run_id)
        if run is None:
            raise ValueError(f"Run not found: {run_id}")

        workflow_id = run["workflow_id"]
        defn = self._def_store.get_definition(workflow_id)
        if defn is None:
            raise ValueError(f"Workflow not found: {workflow_id}")

        workflow, parse_err = parse_workflow(defn["yaml"], f"{workflow_id}.yaml")
        if parse_err or workflow is None:
            raise ValueError(
                f"Workflow parse error: "
                f"{parse_err.error if parse_err else 'unknown'}"
            )

        dag_nodes, node_errors = workflow.get_dag_nodes()
        if node_errors:
            raise ValueError(f"Workflow node validation errors: {node_errors}")

        # Drain any prior in-flight task before spinning up a fresh one.
        # The pause path is supposed to leave _tasks empty (the original
        # _execute returns when it sees status=paused), but if a slow
        # finaliser is still in-flight we must let it observe the paused
        # status and exit cleanly — otherwise the CAS in
        # finish_workflow_run_if_running could race the resume's status
        # flip back to 'running' and double-finalise the run.
        prior = self._tasks.get(run_id)
        if prior is not None and not prior.done():
            try:
                # shield so an outer cancel doesn't kill the inner task;
                # we want it to settle on its own paused-status return.
                await asyncio.wait_for(asyncio.shield(prior), timeout=5.0)
            except asyncio.TimeoutError:
                # Prior task hung. Cancel it explicitly so it doesn't
                # leak past this resume — without this the shielded task
                # would keep running detached for the rest of the
                # process lifetime.
                logger.warning(
                    "resume(%s): prior task hung past 5s wait; cancelling",
                    run_id,
                )
                prior.cancel()
                try:
                    # Bare await (no shield) so we actually observe the
                    # cancellation taking effect. A shielded wait here
                    # would let us proceed even when the prior task
                    # ignores .cancel(), leaving two _execute tasks
                    # overlapping on the same run_id.
                    await asyncio.wait_for(prior, timeout=2.0)
                except (asyncio.TimeoutError, asyncio.CancelledError):
                    pass
                except Exception:
                    logger.exception(
                        "resume(%s): prior task raised during cancellation",
                        run_id,
                    )
            except asyncio.CancelledError:
                # We're being cancelled mid-resume — propagate.
                raise
            except Exception as exc:
                # The prior task raised. It already logged the failure
                # via its own except-handler in _execute and finalised
                # the run via CAS, so this is informational only — but
                # we surface it at debug level so it's not totally
                # invisible.
                logger.debug(
                    "resume(%s): prior task raised on settle: %s",
                    run_id, exc,
                )

        prior_completed: Dict[str, str] = {}
        for nr in self._run_store.list_node_runs(run_id):
            # 'completed' / 'skipped' are persisted terminal states the
            # original run actually finished.
            # 'paused' is the approval-gate's own row — it's about to be
            # re-executed on resume, which will overwrite the status to
            # 'completed' once the gate accepts the decision.
            if nr.get("status") in ("completed", "skipped"):
                prior_completed[nr["dag_node_id"]] = nr.get("output") or ""

        working_path = run.get("working_path", "/tmp")
        inputs: Dict[str, Any] = {}

        # Reset to running in case the caller didn't already (idempotent).
        self._run_store.resume_workflow_run(run_id)
        self._bus.emit(
            run_id=run_id,
            event_type="workflow_resumed_execute",
            data={"prior_completed_count": len(prior_completed)},
        )

        task = asyncio.create_task(
            self._execute(
                run_id,
                workflow_id,
                dag_nodes,
                inputs,
                working_path,
                prior_completed=prior_completed,
            ),
            name=f"resume-{run_id}",
        )
        self._register_task(run_id, task)

    async def wait_for(
        self, run_id: str, timeout: Optional[float] = None,
    ) -> None:
        """Await the in-flight ``_execute`` task for ``run_id``, if any.

        Used by in-process callers (the workflow_run agent tool) whose
        own event loop only lives as long as their await chain — by
        awaiting the runner's background task on the *agent's* loop,
        the bash subprocess and event emission get CPU time to finish.
        Without this, ``start()`` 's fire-and-forget task is orphaned
        when the agent tool returns and the loop stops pumping.

        Returns immediately when the slot is empty (the run already
        terminated, was never tracked here, or its done-callback
        already cleaned up). ``timeout=None`` waits forever. On
        timeout, returns silently — the caller inspects the run row to
        decide what to do.

        The wait is shielded so a cancellation of the *caller* does not
        propagate into ``_execute`` and kill an in-flight run; killing
        the run is the explicit job of ``cancel()``.
        """
        task = self._tasks.get(run_id)
        if task is None or task.done():
            return
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=timeout)
        except asyncio.TimeoutError:
            return
        except asyncio.CancelledError:
            raise
        except Exception:
            # _execute has its own CAS-protected failure handler that
            # already logged + finalised the run; we don't want to
            # re-raise into the caller (e.g. an agent tool handler
            # that would surface the traceback as a tool error).
            return

    async def cancel(self, run_id: str) -> None:
        """Cancel a run by cancelling its asyncio Task and marking DB status.

        Emission of ``workflow_cancelled`` is gated on whether *this*
        call actually flipped the row to cancelled. The CancelledError
        branch inside ``_execute`` already calls
        ``cancel_workflow_run`` via the same CAS, so the row may
        already be terminal by the time we get here — in that case the
        CancelledError branch will have emitted the event and we must
        not double-fire.
        """
        task = self._tasks.get(run_id)
        if task and not task.done():
            task.cancel()
            try:
                await asyncio.wait_for(asyncio.shield(task), timeout=2.0)
            except (asyncio.CancelledError, asyncio.TimeoutError):
                pass
        won = self._run_store.cancel_workflow_run(run_id)
        if won:
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
        prior_completed: Optional[Dict[str, str]] = None,
        max_runtime_s: Optional[int] = None,
    ) -> None:
        start_ms = int(time.time() * 1000)
        try:
            ctx = self._build_ctx(run_id, working_path, prior_completed)
            if max_runtime_s is not None and max_runtime_s > 0:
                node_outputs = await asyncio.wait_for(
                    execute_dag(dag_nodes, ctx), timeout=float(max_runtime_s),
                )
            else:
                node_outputs = await execute_dag(dag_nodes, ctx)

            failed_nodes = [
                (node_id, output)
                for node_id, output in node_outputs.items()
                if getattr(output, "state", None) == "failed"
            ]

            # Completed — atomic CAS so we don't clobber a status that
            # changed under us. Two known interleavings this guards:
            #   (a) the approval node flipped status to 'paused' via
            #       ctx.pause_run() before execute_dag returned, and the
            #       DAG's between-layer pause check (dag_executor.py:608)
            #       was bypassed because the approval node sat in the
            #       last layer; finish_..._if_running's WHERE
            #       status='running' clause leaves the paused row alone.
            #   (b) resume() has already flipped the row back to
            #       'running' for a fresh _execute task; we must NOT
            #       finalise on this old task's behalf — let the new
            #       task's CAS win.
            end_ms = int(time.time() * 1000)
            if failed_nodes:
                first_failed_id, first_failed_output = failed_nodes[0]
                error = getattr(first_failed_output, "error", "") or (
                    f"workflow failed because node '{first_failed_id}' failed"
                )
                won = self._run_store.finish_workflow_run_if_running(
                    run_id, status="failed", error=error,
                )
                if not won:
                    logger.info(
                        "run %s: failure finalization skipped — status no longer 'running'",
                        run_id,
                    )
                    return
                self._run_store.record_phase_transition(
                    run_id=run_id,
                    to_phase="failed",
                    decided_by="system",
                    decision_data={
                        "duration_ms": end_ms - start_ms,
                        "failed_node_id": first_failed_id,
                        "failed_node_count": len(failed_nodes),
                    },
                )
                self._bus.emit(
                    run_id=run_id,
                    event_type="workflow_failed",
                    data={
                        "workflow_id": workflow_id,
                        "duration_ms": end_ms - start_ms,
                        "error": error,
                        "failed_node_id": first_failed_id,
                        "failed_node_count": len(failed_nodes),
                    },
                )
                return

            won = self._run_store.finish_workflow_run_if_running(
                run_id, status="completed",
            )
            if not won:
                logger.info(
                    "run %s: completion skipped — status no longer 'running' "
                    "(paused approval-gate or superseded by resume)",
                    run_id,
                )
                return
            self._run_store.record_phase_transition(
                run_id=run_id,
                to_phase="completed",
                decided_by="system",
                decision_data={"duration_ms": end_ms - start_ms},
            )
            self._bus.emit(
                run_id=run_id,
                event_type="workflow_completed",
                data={
                    "workflow_id": workflow_id,
                    "duration_ms": end_ms - start_ms,
                },
            )
        except asyncio.TimeoutError:
            # max_runtime_s exceeded — route through the existing failure
            # CAS path with a distinguished error string.
            logger.warning("Run %s exceeded max_runtime_s=%s", run_id, max_runtime_s)
            won = self._run_store.finish_workflow_run_if_running(
                run_id, status="failed", error="max_runtime_exceeded",
            )
            if not won:
                return
            self._run_store.record_phase_transition(
                run_id=run_id,
                to_phase="failed",
                decided_by="system",
                decision_data={"reason": "max_runtime_exceeded"},
            )
            self._bus.emit(
                run_id=run_id,
                event_type="workflow_failed",
                data={
                    "error": "max_runtime_exceeded",
                    "reason": "max_runtime_exceeded",
                    "max_runtime_s": max_runtime_s,
                },
            )
            return
        except asyncio.CancelledError:
            # CAS so a concurrent resume / completion can't be
            # double-finalised by this branch.
            won = self._run_store.cancel_workflow_run(run_id)
            if won:
                self._run_store.record_phase_transition(
                    run_id=run_id,
                    to_phase="cancelled",
                    decided_by="system",
                    decision_data={"reason": "cancelled"},
                )
                self._bus.emit(
                    run_id=run_id,
                    event_type="workflow_cancelled",
                    data={"reason": "cancelled"},
                )
            raise
        except Exception as exc:
            logger.exception("Run %s failed: %s", run_id, exc)
            won = self._run_store.finish_workflow_run_if_running(
                run_id, status="failed", error=str(exc),
            )
            if not won:
                # Run already moved out of 'running' (paused, cancelled,
                # or a resume task superseded us). Don't double-finalise.
                return
            self._run_store.record_phase_transition(
                run_id=run_id,
                to_phase="failed",
                decided_by="system",
                decision_data={"error": str(exc)},
            )
            self._bus.emit(
                run_id=run_id,
                event_type="workflow_failed",
                data={"error": str(exc)},
            )

    def _build_ctx(
        self,
        run_id: str,
        working_path: str,
        prior_completed: Optional[Dict[str, str]] = None,
    ) -> DagRunContext:
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
            elif event_type in (
                "node_completed",
                "node_failed",
                "node_skipped",
                "node_paused",
            ):
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
                        # prior_success is emitted on resume for nodes that
                        # already completed in the original run — do not
                        # overwrite their persisted status with 'skipped'.
                        if payload.get("reason") == "prior_success":
                            patch = {}
                        else:
                            patch["status"] = "skipped"
                            patch["skip_reason"] = payload.get("reason", "")
                            patch["completed_at"] = int(time.time() * 1000)
                    elif event_type == "node_paused":
                        # No completed_at — the node hasn't actually completed,
                        # it's waiting on a human decision. Will be patched to
                        # completed/failed once the run is resumed.
                        patch["status"] = "paused"
                    if patch:
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
            # CAS-safe — returns False when the row was already
            # terminal (completed/failed/cancelled by a concurrent
            # path). No event emission here; that's the caller's job.
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
            llm=self._llm,
            log_dir=working_path,
            prior_completed=prior_completed,
        )
