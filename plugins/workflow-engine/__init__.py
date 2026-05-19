"""
workflow-engine plugin — Phase 5: cron poller + kanban dispatcher added.

Exposes `register(host)` for the Hermes plugin loader.
"""

from __future__ import annotations

import asyncio
import logging

logger = logging.getLogger("workflow.plugin")

# Background tasks kept alive for the lifetime of the plugin.
_background_tasks: list[asyncio.Task] = []  # type: ignore[type-arg]


def register(host) -> None:  # noqa: ANN001
    """Register the workflow-engine plugin with the Hermes dashboard host."""
    from plugins.workflow_engine.dashboard.plugin_api import router, _engine  # noqa: PLC0415
    from engine.cron.poller import CronPoller  # noqa: PLC0415
    from engine.dispatcher.kanban import KanbanDispatcher  # noqa: PLC0415

    host.include_router(router, prefix="/api/plugins/workflow-engine")

    # Start background tasks (asyncio loop is running at this point inside
    # the Hermes dashboard ASGI server).
    poller = CronPoller(_engine)
    dispatcher = KanbanDispatcher(_engine)

    _background_tasks.append(asyncio.create_task(poller.run_forever(), name="wf-cron-poller"))
    _background_tasks.append(asyncio.create_task(dispatcher.run_forever(), name="wf-kanban-dispatcher"))

    logger.info("workflow-engine plugin registered (cron poller + kanban dispatcher started)")


def disable() -> None:
    """Cancel background tasks when the plugin is disabled."""
    for task in _background_tasks:
        task.cancel()
    _background_tasks.clear()
    logger.info("workflow-engine plugin disabled (background tasks cancelled)")
