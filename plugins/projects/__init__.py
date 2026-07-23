"""Projects plugin marker for Hermes discovery.

The projects CLI owns the agent-facing capability and web_server auto-mounts
the dashboard API from dashboard/plugin_api.py. Keeping register() empty makes
the declared plugin safe to enable without duplicating either surface.
"""


def register(ctx) -> None:  # noqa: ANN001
    """Expose no additional agent tools; CLI and dashboard API are independent."""
