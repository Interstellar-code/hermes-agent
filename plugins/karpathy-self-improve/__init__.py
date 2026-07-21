"""
karpathy-self-improve — Hermes plugin.

Registers:
  - CLI command  : hermes karpathy  (setup_fn from daemon.setup_parser)
  - Slash command: /karpathy        (status/help, read-only)

Dashboard routes are auto-mounted by web_server._mount_plugin_api_routes()
from dashboard/plugin_api.py — NOT registered here.

Never raises from register(). All errors are swallowed with debug logging.
"""
from __future__ import annotations

import logging
from typing import Optional

logger = logging.getLogger(__name__)

_VERSION = "0.2.0"


# ---------------------------------------------------------------------------
# Slash-command handler  (/karpathy)
# ---------------------------------------------------------------------------

def _handle_karpathy(raw_args: str) -> Optional[str]:
    """
    Handler for the /karpathy slash command.

    Read-only: returns status / help text.
    raw_args is the string after "/karpathy " (may be empty).
    """
    try:
        args = raw_args.strip().lower() if raw_args else ""
        if not args or args in ("help", "?"):
            return (
                "karpathy-self-improve v{version}\n"
                "Commands:\n"
                "  /karpathy status  — show latest metrics per profile\n"
                "  /karpathy help    — this help text\n"
                "\n"
                "CLI: hermes karpathy {{collect,status,daemon}}\n"
                "Dashboard: /self-improve"
            ).format(version=_VERSION)
        if args == "status":
            try:
                from _db import get_db  # absolute import; sys.path set by loader
                db = get_db()
                rows = db.latest_metrics_per_profile()
                if not rows:
                    return "karpathy-self-improve: no metrics collected yet."
                lines = ["karpathy-self-improve — latest metrics per profile:"]
                for row in rows:
                    lines.append(
                        "  {profile}: sessions={sessions_count} errors={error_count} "
                        "warns={warn_count} captured={captured_at}".format(**row)
                    )
                return "\n".join(lines)
            except Exception as exc:
                logger.debug("karpathy /karpathy status error: %s", exc)
                return f"karpathy-self-improve: error reading metrics — {exc}"
        return f"karpathy-self-improve: unknown sub-command '{raw_args}'. Try /karpathy help"
    except Exception as exc:  # pragma: no cover — defensive
        logger.debug("karpathy-self-improve: _handle_karpathy error: %s", exc)
        return None


# ---------------------------------------------------------------------------
# register(ctx)
# ---------------------------------------------------------------------------

def register(ctx) -> None:  # noqa: ANN001
    """
    Called by the Hermes plugin loader at startup.

    Registers:
      1. CLI command  "karpathy"
      2. Slash command "/karpathy"

    Never raises — all errors are caught and logged at DEBUG level.
    """
    try:
        from . import daemon  # relative import works when loaded as a package
    except ImportError:
        try:
            import daemon  # absolute import fallback (spec_from_file_location path)
        except ImportError as exc:
            logger.debug(
                "karpathy-self-improve: could not import daemon module: %s", exc
            )
            daemon = None  # type: ignore[assignment]

    # 1. CLI command: hermes karpathy ...
    if daemon is not None:
        try:
            ctx.register_cli_command(
                name="karpathy",
                help="Karpathy self-improvement CLI (collect, status, daemon).",
                setup_fn=daemon.setup_parser,
                handler_fn=daemon._run,
                description="Self-improvement engine: metrics, experiments, scenarios.",
            )
            logger.debug("karpathy-self-improve: registered CLI command 'karpathy'")
        except Exception as exc:
            logger.debug(
                "karpathy-self-improve: register_cli_command failed: %s", exc
            )

    # 2. Slash command: /karpathy
    try:
        ctx.register_command(
            "karpathy",
            handler=_handle_karpathy,
            description="Karpathy self-improve status/help (read-only).",
            args_hint="[status|help]",
        )
        logger.debug("karpathy-self-improve: registered slash command '/karpathy'")
    except Exception as exc:
        logger.debug(
            "karpathy-self-improve: register_command failed: %s", exc
        )
