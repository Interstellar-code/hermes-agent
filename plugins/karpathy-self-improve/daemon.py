"""
daemon.py — CLI entry point for karpathy-self-improve.

P0 stub: subcommands exist but print "not yet implemented".
The daemon will eventually run the metrics-collect loop and
experiment-evaluation scheduler as a background process.
"""
from __future__ import annotations

import argparse
import logging

logger = logging.getLogger(__name__)


def setup_parser(sub: argparse.ArgumentParser) -> None:
    """Add karpathy subcommands to the argparse subparser."""
    daemon_sub = sub.add_subparsers(dest="karpathy_cmd")

    # hermes karpathy collect
    daemon_sub.add_parser(
        "collect",
        help="Collect metrics snapshots for all profiles.",
    )

    # hermes karpathy status
    daemon_sub.add_parser(
        "status",
        help="Show latest metrics per profile.",
    )

    # hermes karpathy daemon
    p_daemon = daemon_sub.add_parser(
        "daemon",
        help="Run the self-improvement scheduler (not yet implemented).",
    )
    p_daemon.add_argument(
        "--interval",
        type=float,
        default=3600.0,
        help="Poll interval in seconds (default: 3600).",
    )


def _run(ns: argparse.Namespace) -> None:
    """Handler called by the CLI harness when `hermes karpathy ...` is invoked."""
    cmd = getattr(ns, "karpathy_cmd", None)
    if cmd == "collect":
        print("not yet implemented")
    elif cmd == "status":
        print("not yet implemented")
    elif cmd == "daemon":
        print("not yet implemented")
    else:
        print("Usage: hermes karpathy {collect,status,daemon}")
