"""
KanbanDispatcher — Phase 5

Subscribes to the engine event bus for ``node_completed`` events.
When a node's output contains a ``kanban_task_request`` field, POSTs a new
task to the Kanban plugin and records the returned task id back onto the
node_run row.

POST endpoint: http://127.0.0.1:8642/api/plugins/kanban/tasks
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, Optional

import httpx  # type: ignore[import]

logger = logging.getLogger("workflow.kanban-dispatcher")

KANBAN_TASKS_URL: str = "http://127.0.0.1:8642/api/plugins/kanban/tasks"


class KanbanDispatcher:
    """
    Subscribes to the engine event bus and dispatches kanban tasks for
    node_completed events that carry a ``kanban_task_request`` output field.

    Lifecycle::

        dispatcher = KanbanDispatcher(engine)
        task = asyncio.create_task(dispatcher.run_forever())
        # on shutdown:
        task.cancel()
    """

    def __init__(self, engine: Any, kanban_url: str = KANBAN_TASKS_URL) -> None:
        self._engine = engine
        self._kanban_url = kanban_url

    # ------------------------------------------------------------------
    # Public
    # ------------------------------------------------------------------

    async def run_forever(self) -> None:
        logger.info("kanban dispatcher started")
        try:
            async for event in self._engine._bus.subscribe():
                if event.get("event_type") != "node_completed":
                    continue
                data = event.get("data") or {}
                output = data.get("output") or {}
                req = output.get("kanban_task_request")
                if not req:
                    continue

                node_run_id = event.get("node_run_id")
                run_id = event.get("run_id")

                try:
                    await self._handle_request(req, node_run_id=node_run_id, run_id=run_id)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    logger.exception(
                        "kanban dispatcher: error handling request for run=%s node_run=%s: %s",
                        run_id,
                        node_run_id,
                        exc,
                    )
        except asyncio.CancelledError:
            logger.info("kanban dispatcher stopped")

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    async def _handle_request(
        self,
        req: Dict[str, Any],
        *,
        node_run_id: Optional[str],
        run_id: Optional[str],
    ) -> None:
        """POST to kanban and patch the node_run output with kanban_task_id."""
        # Build kanban task payload from the request dict
        payload: Dict[str, Any] = {
            "title": req.get("title", "workflow task"),
        }
        for field in (
            "body", "assignee", "tenant", "priority",
            "workspace_kind", "workspace_path", "parents",
            "triage", "idempotency_key", "max_runtime_seconds", "skills",
        ):
            if field in req:
                payload[field] = req[field]

        # Append workflow context to body for traceability
        if run_id:
            body_suffix = f"\n\n_workflow_run_id: {run_id}_"
            payload["body"] = (payload.get("body") or "") + body_suffix

        # No Authorization header is added here.  The kanban endpoint is on
        # localhost (127.0.0.1:8642) and is only reachable by processes on the
        # same host, so network-level isolation is the security boundary.  If
        # the gateway ever requires session-token auth for inter-plugin calls,
        # pass `headers={"Authorization": f"Bearer {token}"}` and source the
        # token from the HERMES_SESSION_TOKEN env var or a shared secret store.
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(self._kanban_url, json=payload)
            resp.raise_for_status()
            result = resp.json()

        task_id: Optional[str] = (result.get("task") or {}).get("id") or result.get("id")
        if not task_id:
            logger.warning(
                "kanban dispatcher: POST succeeded but no task id in response: %s", result
            )
            return

        logger.info(
            "kanban dispatcher: created task=%s for run=%s node_run=%s",
            task_id,
            run_id,
            node_run_id,
        )

        # Patch the node_run row to record the kanban task id
        if node_run_id:
            try:
                self._engine._run_store.update_node_run(
                    node_run_id,
                    {"kanban_task_id": task_id},
                )
            except Exception as exc:
                logger.warning(
                    "kanban dispatcher: could not patch node_run %s with kanban_task_id: %s",
                    node_run_id,
                    exc,
                )
