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
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

from engine.core.dag_executor import DagRunContext, execute_dag
from engine.core.executor_shared import reserved_input_name
from engine.discovery.loader import parse_workflow
from engine.store.run_store import HEARTBEAT_S, RunStore
from engine.store.definition_store import DefinitionStore, _sha256
from engine.emitter.bus import EventBus

logger = logging.getLogger("workflow.runner")

RETENTION_SWEEP_S = 86_400.0  # daily retention sweep in long-lived engines


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
        runs_dir: Optional[str] = None,
    ) -> None:
        self._run_store = run_store
        self._def_store = def_store
        self._bus = bus
        self._llm = llm
        # Per-run artifacts + run logs live here, never in the user's
        # working_path (L2-01).
        self._runs_dir = Path(runs_dir or Path(tempfile.gettempdir()) / "hermes-workflow-runs")
        self._tasks: Dict[str, asyncio.Task] = {}  # run_id → Task
        self.retention_days = 0  # >0: heartbeat_forever sweeps daily (wiring sets it)

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

        resolved_inputs = _resolve_inputs(yaml_text, inputs)  # raises on reserved names

        # 3. Create run row
        conversation_id = trigger.get("conversation_id", f"trigger-{workflow_id}")
        working_path = trigger.get("working_path", "/tmp")
        user_message = trigger.get("user_message", f"run {workflow_id}")
        # Pin the exact YAML so resume / inspect see what this run started
        # from. Keyed by the hash of the YAML itself (same function as
        # workflow_definitions.checksum) so the key can never name other text.
        checksum = _sha256(yaml_text)
        self._run_store.insert_definition_snapshot(
            workflow_id=workflow_id, checksum=checksum,
            version=defn.get("version"), yaml=yaml_text,
        )
        run = self._run_store.create_workflow_run(
            workflow_id=workflow_id,
            conversation_id=conversation_id,
            working_path=working_path,
            user_message=user_message,
            trigger=trigger,
            priority=priority,
            max_runtime_s=max_runtime_s,
            inputs=inputs,
            parent_run_id=trigger.get("parent_run_id"),
            definition_checksum=checksum,
            definition_version=defn.get("version"),
        )
        run_id = run["id"]
        # Pin every subgraph the run can expand, so resume / retry expand
        # the same YAML even after the subgraph definition is edited.
        pins = self._pin_subgraphs(dag_nodes, {})

        # 4. Mark running and emit workflow_started
        self._run_store.update_workflow_run(
            run_id, status="running",
            metadata={"subgraph_pins": pins} if pins else None,
        )
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
                run_id, workflow_id, dag_nodes,
                resolved_inputs, working_path,
                max_runtime_s=max_runtime_s,
            ),
            name=f"run-{run_id}",
        )
        self._register_task(run_id, task)

        return self._run_store.get_workflow_run(run_id)  # type: ignore[return-value]

    def _pin_subgraphs(self, nodes: List[Any], pins: Dict[str, str]) -> Dict[str, str]:
        """Snapshot each subgraph ref reachable from ``nodes`` (recursively)
        and return ``{ref: checksum}``. Missing refs are left unpinned: their
        expansion fails at run time exactly as before."""
        for node in nodes:
            ref = getattr(getattr(node, "subgraph", None), "ref", None)
            if not ref or ref in pins:
                continue
            defn = self._def_store.get_definition(ref)
            if defn is None:
                continue
            pins[ref] = _sha256(defn["yaml"])
            self._run_store.insert_definition_snapshot(
                workflow_id=ref, checksum=pins[ref],
                version=defn.get("version"), yaml=defn["yaml"],
            )
            child, _ = parse_workflow(defn["yaml"], f"{ref}.yaml")
            if child is not None:
                self._pin_subgraphs(child.get_dag_nodes()[0], pins)
        return pins

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
        Restart DAG execution after a pause (e.g. post-approval). Loads the
        definition the run was pinned to (snapshot) — the current one only
        for pre-009 runs — builds `prior_completed` from node_runs that are
        already terminal, and fires a fresh _execute task. The DAG executor
        skips any node whose id is in prior_completed.
        """
        run = self._run_store.get_workflow_run(run_id)
        if run is None:
            raise ValueError(f"Run not found: {run_id}")

        workflow_id = run["workflow_id"]
        defn = run.get("definition_checksum") and self._run_store.get_definition_snapshot(
            workflow_id, run["definition_checksum"],
        )
        if run.get("definition_checksum") and not defn:
            logger.warning(
                "resume(%s): pinned snapshot %s/%s missing; using the current definition",
                run_id, workflow_id, run["definition_checksum"],
            )
        defn = defn or self._def_store.get_definition(workflow_id)
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
            if nr.get("status") in ("completed", "skipped") and nr.get("loop_iteration") is None:
                prior_completed[nr["dag_node_id"]] = nr.get("summary") or ""

        working_path = run.get("working_path", "/tmp")
        meta = run.get("metadata") or {}
        inputs = _resolve_inputs(defn["yaml"], meta.get("inputs") or {})

        # Interactive loop gate (L2-06): re-run the loop node from the
        # iteration after the one that paused, instead of skipping it (its
        # node_run was claimed 'completed' by approve) or restarting at 0.
        loop_resume: Dict[str, Dict[str, Any]] = {}
        pause = meta.get("pause") or {}
        if pause.get("type") == "interactive_loop" and pause.get("node_id"):
            prior_completed.pop(pause["node_id"], None)
            loop_resume[pause["node_id"]] = pause

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
                loop_resume=loop_resume,
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
        loop_resume: Optional[Dict[str, Dict[str, Any]]] = None,
    ) -> None:
        start_ms = int(time.time() * 1000)
        try:
            run_row = self._run_store.get_workflow_run(run_id) or {}
            cwd = working_path if os.path.isdir(working_path) else None
            base_branch = str(inputs.get("base_branch") or "") or await asyncio.to_thread(
                _detect_base_branch, cwd,
            )
            artifacts_dir = self._runs_dir / run_id / "artifacts"
            artifacts_dir.mkdir(parents=True, exist_ok=True)
            ctx = self._build_ctx(
                run_id, working_path, prior_completed,
                {n.id: i for i, n in enumerate(dag_nodes)},
            )
            ctx.cwd = cwd
            ctx.home = str(self._runs_dir.parent)
            ctx.log_dir = str(self._runs_dir / run_id)
            ctx.loop_resume = loop_resume or {}
            ctx.workflow_vars = {
                "workflow_id": workflow_id,
                "user_message": run_row.get("user_message") or "",
                "artifacts_dir": str(artifacts_dir),
                "base_branch": base_branch,
                "inputs": inputs,
            }
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
        step_index_by_node: Optional[Dict[str, int]] = None,
    ) -> DagRunContext:
        run_store = self._run_store
        bus = self._bus
        step_index_by_node = step_index_by_node or {}
        log_node_runs: Dict[str, str] = {}  # node_id -> node_run_id for node_log
        # node_id -> (max_retries, delay_ms), set by the executor before a
        # node's first attempt; popped at that attempt's node_started.
        attempt_config: Dict[str, Any] = {}

        def emit_event(event_type: str, payload: Dict[str, Any]) -> None:
            node_run_id = payload.pop("node_run_id", None)
            if event_type == "node_log":
                # High-volume live output: resolve the row once, never write it.
                node_id = payload.get("node_id", "")
                node_run_id = log_node_runs.get(node_id)
                if node_run_id is None:
                    nr = run_store.find_node_run(run_id, node_id)
                    if nr:
                        node_run_id = log_node_runs[node_id] = nr["id"]
            # Persist node_run row for node_started events
            elif event_type == "node_started":
                node_id = payload.get("node_id", "")
                node_type = payload.get("node_type", "prompt")
                provided_nr_id = payload.get("node_run_id_hint")
                try:
                    # Re-executed node (loop resumed after its gate, retry):
                    # reuse its row — UNIQUE(.., loop_iteration NULL) does
                    # not dedupe, so a fresh insert would leave a zombie.
                    nr = run_store.find_node_run(run_id, node_id)
                    if nr is not None:
                        run_store.update_node_run(nr["id"], {
                            "status": "running", "completed_at": None,
                            # a retried node must not show the old attempt's
                            # result, error or routed run
                            "error": None, "summary": None,
                            "session_id": None, "gateway_run_id": None,
                        })
                    else:
                        nr = run_store.create_node_run(
                            workflow_run_id=run_id,
                            dag_node_id=node_id,
                            node_type=node_type,
                            node_run_id=provided_nr_id,
                        )
                    node_run_id = nr["id"]
                    cfg = attempt_config.pop(node_id, None)
                    if cfg is not None:
                        # Attempt 1: attempt counters come from the node's
                        # retry config (node_retrying updates them later).
                        # retry_delay_ms = wait before the latest retry; until
                        # one happens, the configured base delay (= the first
                        # retry's wait; backoff doubles per attempt).
                        run_store.update_node_run(nr["id"], {
                            "retries": 0, "max_retries": cfg[0], "retry_delay_ms": cfg[1],
                        })
                except Exception as e:
                    logger.debug("create_node_run skipped: %s", e)
            elif event_type == "node_retrying":
                nr = run_store.find_node_run(run_id, payload.get("node_id", ""))
                if nr:
                    node_run_id = nr["id"]
                    try:
                        run_store.update_node_run(nr["id"], {
                            "retries": payload["attempt"] - 1,
                            "max_retries": payload["max_attempts"] - 1,
                            "retry_delay_ms": payload["delay_ms"],
                        })
                    except Exception as e:
                        logger.debug("update_node_run failed: %s", e)
            elif event_type == "node_session_started":
                nr = run_store.find_node_run(run_id, payload.get("node_id", ""))
                if nr:
                    node_run_id = nr["id"]
                    try:
                        run_store.update_node_run(nr["id"], {
                            "assigned_agent": payload.get("profile"),
                            "session_id": payload.get("session_id"),
                            "gateway_run_id": payload.get("gateway_run_id"),
                        })
                    except Exception as e:
                        logger.debug("update_node_run failed: %s", e)
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
                        patch["summary"] = payload.get("output")
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
                        patch["approval_message"] = payload.get("message") or "Approval required"
                    if patch:
                        try:
                            run_store.update_node_run(nr["id"], patch)
                        except Exception as e:
                            logger.debug("update_node_run failed: %s", e)
                    if payload.get("usage"):
                        try:
                            run_store.add_node_usage(nr["id"], payload["usage"])
                        except Exception as e:
                            logger.debug("add_node_usage failed: %s", e)
            elif event_type in (
                "loop_iteration_started",
                "loop_iteration_completed",
                "loop_iteration_failed",
            ):
                # One node_run per loop iteration, child of the wrapper row.
                node_id = payload.get("node_id", "")
                iteration = payload.get("iteration")
                try:
                    # No iteration → never resolve to the wrapper row.
                    nr = run_store.find_node_run(run_id, node_id, iteration) if iteration is not None else None
                    if iteration is None:
                        pass
                    elif event_type == "loop_iteration_started":
                        if nr is not None:  # loop retried / resumed: reuse
                            run_store.update_node_run(nr["id"], {
                                "status": "running", "started_at": int(time.time() * 1000),
                                "completed_at": None, "error": None,
                            })
                        else:
                            parent = run_store.find_node_run(run_id, node_id)
                            nr = run_store.create_node_run(
                                workflow_run_id=run_id,
                                dag_node_id=node_id,
                                node_type="loop",
                                loop_iteration=iteration,
                                loop_parent_node_run_id=parent["id"] if parent else None,
                            )
                    elif nr is not None:
                        done = event_type == "loop_iteration_completed"
                        run_store.update_node_run(nr["id"], {
                            "status": "completed" if done else "failed",
                            "completed_at": int(time.time() * 1000),
                            ("summary" if done else "error"):
                                payload.get("output" if done else "error"),
                        })
                        if payload.get("usage"):
                            # Wrapper = sum of iterations, added here (not on
                            # the wrapper's node_* events) so it survives a crash.
                            run_store.add_node_usage(nr["id"], payload["usage"])
                            if nr.get("loop_parent_node_run_id"):
                                run_store.add_node_usage(nr["loop_parent_node_run_id"], payload["usage"])
                    if nr is not None:
                        node_run_id = nr["id"]
                except Exception as e:
                    logger.debug("loop iteration node_run skipped: %s", e)

            step_name = payload.get("node_id") or None
            bus.emit(
                run_id=run_id,
                event_type=event_type,
                node_run_id=node_run_id,
                data=payload,
                step_index=step_index_by_node.get(step_name) if step_name else None,
                step_name=step_name,
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
            # The run's pinned snapshot first; live only for unpinned refs.
            run = run_store.get_workflow_run(run_id) or {}
            checksum = ((run.get("metadata") or {}).get("subgraph_pins") or {}).get(ref)
            snap = checksum and run_store.get_definition_snapshot(ref, checksum)
            if snap:
                sub, _ = parse_workflow(snap["yaml"], f"{ref}.yaml")
                return (snap["yaml"], (sub.kind if sub else None) or "workflow")
            if checksum:
                logger.warning(
                    "run %s: pinned subgraph snapshot %s/%s missing; using live",
                    run_id, ref, checksum,
                )
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
            llm=self._llm or getattr(sys.modules.get("engine"), "HOST_LLM", None),
            prior_completed=prior_completed,
            attempt_config=attempt_config,
        )

    async def heartbeat_forever(self, interval_s: float = HEARTBEAT_S) -> None:
        """Keep this process's live runs fresh so other engines' crash
        recovery (stale-heartbeat reaper) leaves them alone (L2-02)."""
        from engine.runtime.resume import mark_crashed_runs
        next_sweep = time.monotonic() + RETENTION_SWEEP_S  # boot already swept
        while True:
            try:
                self._run_store.heartbeat_runs(list(self._tasks))
                mark_crashed_runs(self._run_store)
            except Exception:
                logger.exception("heartbeat tick failed")
            if self.retention_days > 0 and time.monotonic() >= next_sweep:
                next_sweep = time.monotonic() + RETENTION_SWEEP_S
                try:  # bounds node_log growth: events go with their run (CASCADE)
                    self._run_store.delete_terminal_runs_older_than(self.retention_days)
                except Exception:
                    logger.exception("retention sweep failed")
            await asyncio.sleep(interval_s)


def _resolve_inputs(yaml_text: str, inputs: Dict[str, Any]) -> Dict[str, Any]:
    """Declared workflow inputs only, caller value else YAML ``default``.

    Only author-declared names are exported (as env vars / prompt
    substitutions) so a caller can't inject e.g. PATH or PYTHONPATH.
    """
    try:
        doc = yaml.safe_load(yaml_text) or {}
    except yaml.YAMLError:
        doc = {}
    if not isinstance(doc, dict):
        doc = {}
    # Three authoring shapes (same as SwitchUI's parsed route): `inputs:` list
    # of {name, ...}, `inputs:` mapping name -> {default, ...}, and top-level
    # required_inputs / optional_inputs name lists.
    raw = doc.get("inputs") or []
    declared: List[Dict[str, Any]] = (
        [{**(v if isinstance(v, dict) else {}), "name": k} for k, v in raw.items()]
        if isinstance(raw, dict) else [d for d in raw if isinstance(d, dict)]
    )
    for key in ("required_inputs", "optional_inputs"):
        names = doc.get(key)
        if isinstance(names, list):
            declared += [{"name": n} for n in names if isinstance(n, str)]
    out: Dict[str, Any] = {}
    for d in declared:
        name = d.get("name")
        if not isinstance(name, str) or not name:
            continue
        if reserved_input_name(name):
            raise ValueError(
                f"workflow input name '{name}' is reserved (would override the "
                "subprocess env: PATH, PYTHONPATH, HOME, LD_*, DYLD_*, HERMES_*, ...); rename it"
            )
        value = inputs.get(name, d.get("default"))
        if value is not None:
            out[name] = value
    if "base_branch" in inputs:  # engine var, shell-quoted / script-guarded
        out.setdefault("base_branch", inputs["base_branch"])
    return out


def _detect_base_branch(cwd: Optional[str]) -> str:
    """origin/HEAD's branch in cwd, '' when unknown ($BASE_BRANCH then errors)."""
    if not cwd:
        return ""
    try:
        out = subprocess.run(
            ["git", "-C", cwd, "rev-parse", "--abbrev-ref", "origin/HEAD"],
            capture_output=True, text=True, timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    ref = out.stdout.strip() if out.returncode == 0 else ""
    return ref.split("/", 1)[1] if ref.startswith("origin/") else ""
