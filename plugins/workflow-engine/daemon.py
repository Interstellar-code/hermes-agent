"""hermes workflow daemon — runs CronPoller + scheduled-run tick.

Start via:
    hermes workflow daemon --interval 60

Or supervised:
    systemctl --user start hermes-workflow-dispatcher   (Linux)
    launchctl load ~/Library/LaunchAgents/ai.hermes.workflow-dispatcher.plist  (macOS)

IMPORTANT: If no supervisor is installed this process does NOT auto-restart
on crash. Use foreground mode (hermes workflow daemon) for development; use
a supervisor for production.

Signal handling:
    SIGINT / SIGTERM → clean shutdown (tasks cancelled, daemon exits 0)
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import signal
import sys
from pathlib import Path
from typing import Any

log = logging.getLogger("workflow.daemon")


async def _main(args: Any) -> int:
    from ._shared import get_engine  # noqa: PLC0415
    from engine.cron.poller import CronPoller  # noqa: PLC0415
    from engine.runtime.scheduler_tick import heartbeat_path, run_scheduler_tick_loop  # noqa: PLC0415

    engine = get_engine()
    # Heartbeat lives next to the DB the engine opened so /health finds it by construction.
    db_path = engine.db_path
    heartbeat_file = heartbeat_path(Path(db_path).parent) if db_path else None
    poller = CronPoller(engine, poll_interval_s=args.interval)

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()

    def _handle_stop() -> None:
        log.info("workflow daemon: shutdown signal received")
        stop.set()

    # Signal handlers — guarded for Windows where add_signal_handler is absent
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _handle_stop)  # windows-footgun: ok — wrapped in try/except below; the grep-based checker matches the call itself and can't see the surrounding try/except.
        except (NotImplementedError, OSError):
            # Windows / some embedded loops — fall back to signal.signal
            signal.signal(sig, lambda *_: _handle_stop())

    log.info(
        "workflow daemon started (interval=%.1fs, pid=%d)",
        args.interval,
        __import__("os").getpid(),
    )

    tasks = [
        asyncio.create_task(poller.run_forever(), name="wf-cron-poller"),
        asyncio.create_task(
            run_scheduler_tick_loop(engine, heartbeat_file=heartbeat_file),
            name="wf-scheduler-tick",
        ),
    ]

    await stop.wait()

    log.info("workflow daemon: cancelling tasks")
    for t in tasks:
        t.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)

    log.info("workflow daemon: clean exit")
    return 0


def _acquire_single_instance_lock(pidfile: Any) -> Any:
    """flock a profile-scoped file (HERMES_HOME) so two daemons can't double-fire.

    Returns the open file (keep referenced for process life) or None if held.
    """
    import fcntl  # noqa: PLC0415
    from pathlib import Path  # noqa: PLC0415
    from hermes_constants import get_hermes_home  # noqa: PLC0415

    path = Path(pidfile) if pidfile else get_hermes_home() / "workflow-daemon.pid"
    path.parent.mkdir(parents=True, exist_ok=True)
    f = open(path, "a+", encoding="utf-8")  # noqa: SIM115 — held for process life
    try:
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        f.close()
        return None
    f.seek(0)
    f.truncate()
    f.write(str(__import__("os").getpid()))
    f.flush()
    return f


def _setup(sub: argparse.ArgumentParser) -> None:
    """Configure the argparse subparser for `hermes workflow`.

    Registers a `daemon` sub-subcommand so the invocation is:
        hermes workflow daemon --interval 60
    """
    subs = sub.add_subparsers(dest="wf_subcommand", title="subcommands")

    daemon_sub = subs.add_parser(
        "daemon",
        help="Run the workflow scheduler (cron poller + scheduled-run tick).",
    )
    daemon_sub.add_argument(
        "--interval",
        type=float,
        default=60.0,
        metavar="SECONDS",
        help="Poll interval in seconds for cron poller (default: 60).",
    )
    daemon_sub.add_argument(
        "--pidfile",
        default=None,
        metavar="PATH",
        help="Lock/PID file (default: $HERMES_HOME/workflow-daemon.pid).",
    )

    def _run(ns: argparse.Namespace) -> None:
        logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s %(levelname)-8s %(name)s — %(message)s",
        )
        lock = _acquire_single_instance_lock(ns.pidfile)
        if lock is None:
            log.error("workflow daemon: another instance holds the lock; exiting")
            sys.exit(0)  # clean exit: supervisors must not respawn a duplicate
        sys.exit(asyncio.run(_main(ns)))

    daemon_sub.set_defaults(func=_run)

    def _no_subcommand(ns: argparse.Namespace) -> None:
        sub.print_help()
        sys.exit(0)

    sub.set_defaults(func=_no_subcommand)
