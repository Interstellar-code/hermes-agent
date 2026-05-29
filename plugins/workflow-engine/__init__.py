"""workflow-engine plugin.

Dashboard router: dashboard/plugin_api.py (auto-mounted by web_server.py via
_mount_plugin_api_routes — no include_router call needed here).

Agent-side tools/hooks/CLI: registered in register() below.
Background scheduler: hermes workflow daemon (see systemd/ and launchd/).
"""
from __future__ import annotations

import logging

logger = logging.getLogger("workflow.plugin")


def register(ctx) -> None:  # noqa: ANN001
    """Register agent-side surface with Hermes PluginContext.

    Registers 5 workflow tools and 1 CLI command (hermes workflow daemon).
    Does NOT call ctx.include_router — the dashboard loader auto-mounts
    dashboard/plugin_api.py independently via _mount_plugin_api_routes.
    Does NOT call asyncio.create_task — background scheduling is owned
    by the standalone daemon process (hermes workflow daemon).
    """
    from ._shared import get_engine  # noqa: PLC0415 — lazy import keeps load cheap
    from .tools.list_workflows import handler as list_handler, SCHEMA as list_schema, check as list_check  # noqa: PLC0415,E501
    from .tools.run_workflow import handler as run_handler, SCHEMA as run_schema, check as run_check  # noqa: PLC0415,E501
    from .tools.workflow_status import handler as status_handler, SCHEMA as status_schema, check as status_check  # noqa: PLC0415,E501
    from .tools.approve_workflow import handler as approve_handler, SCHEMA as approve_schema, check as approve_check  # noqa: PLC0415,E501
    from .tools.cancel_workflow import handler as cancel_handler, SCHEMA as cancel_schema, check as cancel_check  # noqa: PLC0415,E501
    from .daemon import _setup as daemon_setup  # noqa: PLC0415

    # Wire the host-owned PluginLlm facade so prompt/command nodes can execute.
    # Engine is initialized lazily on first tool call via get_engine(); we only
    # call get_engine() here when there is an LLM to wire in, avoiding SQLite
    # migrations and manifest I/O at import time.
    llm = getattr(ctx, "llm", None)
    if llm is not None:
        try:
            get_engine().set_llm(llm)
        except Exception:
            logger.exception("workflow-engine: failed to wire ctx.llm into engine")

    for name, schema, handler, is_async, check_fn in (
        ("workflow_list",    list_schema,    list_handler,    True, list_check),
        ("workflow_run",     run_schema,     run_handler,     True, run_check),
        ("workflow_status",  status_schema,  status_handler,  True, status_check),
        ("workflow_approve", approve_schema, approve_handler, True, approve_check),
        ("workflow_cancel",  cancel_schema,  cancel_handler,  True, cancel_check),
    ):
        ctx.register_tool(
            name=name,
            toolset="workflow",
            schema=schema,
            handler=handler,
            check_fn=check_fn,
            is_async=is_async,
            description=schema.get("description", ""),
            emoji="🔁",
        )
        logger.debug("workflow-engine: registered tool %s", name)

    ctx.register_cli_command(
        name="workflow",
        help="Workflow engine subcommands (run daemon, etc.)",
        setup_fn=daemon_setup,
        description="Run the workflow scheduler (cron poller + kanban dispatcher).",
    )

    # Plugin-scoped skill: end-to-end procedure for running/verifying a workflow.
    # Resolvable as 'workflow-engine:test-workflow' via explicit load only.
    if hasattr(ctx, "register_skill"):
        from pathlib import Path  # noqa: PLC0415
        try:
            ctx.register_skill(
                name="test-workflow",
                path=Path(__file__).parent / "skills" / "test-workflow" / "SKILL.md",
                description="Run and verify a workflow DAG end-to-end (preconditions, trigger, monitor, approve, cancel).",
            )
        except Exception:
            logger.debug("workflow-engine: register_skill failed", exc_info=True)

    logger.info(
        "workflow-engine plugin loaded — 5 tools + 1 CLI command registered; "
        "dashboard router auto-mounted by web_server; "
        "background scheduler: hermes workflow daemon"
    )


def disable() -> None:
    """Called by the plugin loader on hot-reload or shutdown.

    No-op for now — the engine singleton (_shared._engine) is stateless between
    runs.  If in-process run tracking is added in future, call engine.shutdown()
    here to drain active runs before the loader unloads this module.
    """
    logger.info("workflow-engine plugin disabled")
