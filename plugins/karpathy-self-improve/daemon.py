"""
daemon.py — CLI entry point and async daemon loop for karpathy-self-improve.

Subcommands:
  collect              Run _metrics.collect_profile_metrics once.
  propose --profile P  Run proposer once for profile P.
  status               Print active experiments + baselines.
  daemon --interval N  Run the self-improvement loop continuously.

The daemon loop:
- For each enabled+non-paused profile with no active experiment, attempts propose.
- For each 'live' experiment, increments live_sessions_observed (from metrics window),
  and when observed >= target runs a live eval:
    - Score holds/improves → transition to 'verified' + record baseline.
    - Score drops → _git_ratchet.revert_commit + transition to 'reverted'.

Uses asyncio with signal handlers for graceful shutdown.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import signal
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# When invoked via the `hermes karpathy` CLI entry point, this plugin directory
# is NOT on sys.path (unlike the test harness or the web_server router loader),
# so the bare `from _db import ...` / `from _metrics import ...` imports used by
# the command handlers below would fail with ModuleNotFoundError. Add the plugin
# root to sys.path so sibling modules resolve regardless of invocation context.
_PLUGIN_DIR = Path(__file__).resolve().parent
if str(_PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_DIR))

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Parser (shape preserved from stub)
# ---------------------------------------------------------------------------


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
        help="Show active experiments and baselines per profile.",
    )

    # hermes karpathy propose
    # Profile comes from the global `hermes --profile <name>` (pinned via
    # HERMES_HOME by main._apply_profile_override, which strips --profile from
    # argv before argparse). These per-profile subcommands must NOT declare
    # their own --profile — it could never receive a value. See #180.
    daemon_sub.add_parser(
        "propose",
        help="Run the proposer once (target profile from `hermes --profile`).",
    )

    # hermes karpathy daemon
    p_daemon = daemon_sub.add_parser(
        "daemon",
        help="Run the self-improvement scheduler continuously.",
    )
    p_daemon.add_argument(
        "--interval",
        type=float,
        default=3600.0,
        help="Poll interval in seconds (default: 3600).",
    )

    # hermes karpathy init
    p_init = daemon_sub.add_parser(
        "init",
        help="Initialize (or locate) the metrics DB; optionally persist a custom path to config.yaml.",
    )
    p_init.add_argument(
        "--db-path",
        dest="db_path",
        default=None,
        help="Custom DB path to write into config.yaml under plugins.karpathy_self_improve.db_path.",
    )

    # hermes karpathy bootstrap / pause / resume
    # Target profile comes from the global `hermes --profile <name>` (see the
    # note on `propose` above and #180); no per-subcommand --profile flag.
    daemon_sub.add_parser(
        "bootstrap",
        help=(
            "Write the per-profile target config (target_relpath, profile_root) to "
            "config.yaml and set the profile paused. Run once per profile before "
            "the daemon or `propose` will operate on it. "
            "Usage: hermes --profile <name> karpathy bootstrap"
        ),
    )
    daemon_sub.add_parser(
        "pause",
        help="Pause self-improvement (propose/verify/revert) for a profile.",
    )
    daemon_sub.add_parser(
        "resume",
        help="Resume self-improvement for a paused profile.",
    )


# ---------------------------------------------------------------------------
# Subcommand handlers
# ---------------------------------------------------------------------------


def _cmd_collect() -> None:
    from _metrics import collect_profile_metrics

    snapshots = collect_profile_metrics()
    print(f"Collected {len(snapshots)} snapshot(s).")
    for s in snapshots:
        print(
            f"  profile={s['profile']} sessions={s.get('sessions_count', 0)} "
            f"errors={s.get('error_count', 0)}"
        )


def _cmd_init(db_path_arg: "str | None") -> None:
    """Initialize the DB, optionally persisting a custom path to config.yaml."""
    if db_path_arg is not None:
        # Persist into config.yaml via the canonical save_config path.
        try:
            from hermes_cli.config import load_config, save_config  # type: ignore[import]
            config = load_config()
            plugins_section = config.setdefault("plugins", {})
            ksi_section = plugins_section.setdefault("karpathy_self_improve", {})
            ksi_section["db_path"] = db_path_arg
            save_config(config)
            print(f"Wrote plugins.karpathy_self_improve.db_path = {db_path_arg!r} to config.yaml")
        except Exception as exc:
            print(f"Warning: could not save db_path to config.yaml: {exc}", file=sys.stderr)

    # Now resolve + open (triggers announce-on-create if fresh).
    from _db import resolve_db_path, open_db
    resolved = resolve_db_path()
    open_db(resolved)
    print(f"DB path: {resolved}")


# Candidate identity files bootstrap looks for, in priority order (#176).
_BOOTSTRAP_IDENTITY_CANDIDATES = ("SOUL.md", "USER.md", "MEMORY.md", "system_prompt.md")


def _cmd_bootstrap(profile: str) -> None:
    """Write plugins.karpathy_self_improve.profiles.<profile> to config.yaml.

    Resolves the profile's real directory (same resolver the API uses),
    detects its identity file, and persists {target_relpath, profile_root,
    live_sessions_target, paused: true}. The profile starts paused — an
    operator must explicitly `hermes --profile <profile> karpathy resume`.
    """
    from hermes_cli.profiles import get_profile_dir
    from hermes_cli.config import load_config, save_config
    from _git_ratchet import is_git_repo

    try:
        profile_root = get_profile_dir(profile)
    except Exception as exc:
        print(f"Error: could not resolve profile dir for {profile!r}: {exc}", file=sys.stderr)
        return

    if not profile_root.is_dir():
        print(f"Error: profile directory does not exist: {profile_root}", file=sys.stderr)
        return

    target_relpath = next(
        (c for c in _BOOTSTRAP_IDENTITY_CANDIDATES if (profile_root / c).is_file()),
        None,
    )
    if target_relpath is None:
        print(
            f"Error: no identity file ({', '.join(_BOOTSTRAP_IDENTITY_CANDIDATES)}) "
            f"found under {profile_root}",
            file=sys.stderr,
        )
        return

    config = load_config()
    plugins_section = config.setdefault("plugins", {})
    ksi_section = plugins_section.setdefault("karpathy_self_improve", {})
    profiles_section = ksi_section.setdefault("profiles", {})
    block = {
        "target_relpath": target_relpath,
        "profile_root": str(profile_root),
        "live_sessions_target": 10,
        "paused": True,
    }
    profiles_section[profile] = block
    save_config(config)

    from _db import get_db
    get_db().set_paused(profile, True)

    print(f"Bootstrapped profile {profile!r}:")
    for k, v in block.items():
        print(f"  {k} = {v!r}")
    if not is_git_repo(profile_root):
        print(
            f"tip: {profile_root} is not a git repo — the DB snapshot table is the "
            "revert source of truth (#173), so this is not required."
        )
    print(f"Run `hermes --profile {profile} karpathy resume` when ready.")


def _cmd_pause(profile: str) -> None:
    from _db import get_db
    get_db().set_paused(profile, True)
    print(f"Paused profile {profile!r}.")


def _cmd_resume(profile: str) -> None:
    from _db import get_db
    get_db().set_paused(profile, False)
    print(f"Resumed profile {profile!r}.")


def _cmd_status() -> None:
    from _db import get_db, resolve_db_path

    # Print DB header line.
    resolved = resolve_db_path()
    exists = resolved.exists()
    size = resolved.stat().st_size if exists else 0
    print(f"DB: {resolved} (exists={exists}, size={size})")

    db = get_db()

    # Active experiments (proposed / approved / live).
    active = []
    for state in ("proposed", "approved", "live"):
        active.extend(db.list_experiments(state=state))

    if active:
        print(f"Active experiments ({len(active)}):")
        for exp in active:
            print(
                f"  [{exp['id']}] profile={exp['profile']} state={exp['state']} "
                f"file={exp.get('target_relpath', '?')} "
                f"offline_score={exp.get('offline_score')}"
            )
    else:
        print("No active experiments.")

    # Baselines.
    cur = db._conn.execute(
        "SELECT DISTINCT profile FROM baselines ORDER BY profile"
    )
    profiles = [row[0] for row in cur.fetchall()]
    if profiles:
        print(f"\nBaselines for {len(profiles)} profile(s):")
        for p in profiles:
            rows = db.list_baselines(p)
            if rows:
                latest = rows[0]
                print(
                    f"  {p}: score={latest['score']:.3f} "
                    f"commit={str(latest.get('commit_sha', '?'))[:8]} "
                    f"captured_at={latest.get('created_at', '?')}"
                )
    else:
        print("No baselines recorded.")

    # Paused profiles.
    try:
        cur = db._conn.execute(
            "SELECT profile FROM controls WHERE paused = 1 ORDER BY profile"
        )
        paused = [row[0] for row in cur.fetchall()]
        if paused:
            print(f"\nPaused profiles: {', '.join(paused)}")
    except Exception:  # pylint: disable=broad-except
        pass


def _cmd_propose(profile: str) -> None:
    from _db import get_db
    from _proposer import propose_for_profile
    from _wiring import resolve_propose_kwargs, resolve_target_for_profile

    db = get_db()

    try:
        target_relpath, profile_root = resolve_target_for_profile(profile, db)
    except ValueError as exc:
        print(f"Error: {exc}")
        return

    try:
        propose_kwargs = resolve_propose_kwargs(profile)
    except ValueError as exc:
        print(f"Error: {exc}")
        return

    result = propose_for_profile(
        db=db,
        profile=profile,
        target_relpath=target_relpath,
        profile_root=profile_root,
        **propose_kwargs,
    )

    if result.skipped:
        print(f"Skipped: {result.skip_reason}")
    elif result.ok:
        print(
            f"Proposed experiment {result.experiment_id} "
            f"offline_score={result.offline_score}"
        )
    else:
        print(f"Error: {result.error}")


# ---------------------------------------------------------------------------
# Daemon loop helpers
# ---------------------------------------------------------------------------


def _is_paused(db: Any, profile: str) -> bool:
    """Return True if *profile* is paused in the controls table."""
    try:
        cur = db._conn.execute(
            "SELECT paused FROM controls WHERE profile = ?", (profile,)
        )
        row = cur.fetchone()
        return bool(row and row[0])
    except Exception:  # pylint: disable=broad-except
        return False


def _get_enabled_profiles(db: Any) -> list:
    """Return distinct profiles that have at least one scenario or experiment."""
    try:
        cur = db._conn.execute(
            "SELECT DISTINCT profile FROM scenarios "
            "UNION SELECT DISTINCT profile FROM experiments "
            "ORDER BY 1"
        )
        return [row[0] for row in cur.fetchall() if row[0]]
    except Exception:  # pylint: disable=broad-except
        return []


def _get_latest_sessions_count(db: Any, profile: str) -> int:
    """Return the most recent sessions_count for *profile* from metrics."""
    try:
        rows = db.list_metrics(profile=profile, limit=1)
        if rows:
            return int(rows[0].get("sessions_count", 0))
    except Exception:  # pylint: disable=broad-except
        pass
    return 0



# M-4: per-experiment consecutive failure counter (in-process, reset on daemon restart).
_consecutive_tick_failures: dict[int, int] = {}
_MAX_CONSECUTIVE_TICK_FAILURES = 3


def _tick_live_experiments(db: Any) -> None:
    """Check all 'live' experiments: increment observed count; verify or revert."""
    from _eval_runner import run_eval
    from _git_ratchet import revert_commit
    from _state_machine import transition

    live_exps = db.list_experiments(state="live")
    now = datetime.now(timezone.utc).isoformat()

    for exp in live_exps:
        exp_id = exp["id"]
        profile = exp["profile"]
        target_sessions = exp.get("live_sessions_target") or 10
        observed = int(exp.get("live_sessions_observed") or 0)

        # Increment observed count using latest metrics delta.
        new_observed = _get_latest_sessions_count(db, profile)
        if new_observed > observed:
            observed = new_observed
            db.update_experiment_fields(
                exp_id, live_sessions_observed=observed, updated_at=now
            )

        if observed < target_sessions:
            logger.debug(
                "karpathy-self-improve: experiment %d live: %d/%d sessions observed",
                exp_id,
                observed,
                target_sessions,
            )
            continue

        # Enough sessions — run live eval.
        proposer_model = exp.get("proposer_model")
        judge_model = exp.get("judge_model")

        try:
            live_score = run_eval(
                db=db,
                experiment_id=exp_id,
                profile=profile,
                kind="live",
                proposer_model=proposer_model,
                judge_model=judge_model,
                include_holdout=True,
            )
            # Reset failure counter on success.
            _consecutive_tick_failures.pop(exp_id, None)
        except Exception as exc:  # pylint: disable=broad-except
            failures = _consecutive_tick_failures.get(exp_id, 0) + 1
            _consecutive_tick_failures[exp_id] = failures
            logger.error(
                "karpathy-self-improve: live eval failed for experiment %d "
                "(consecutive failures: %d/%d): %s",
                exp_id,
                failures,
                _MAX_CONSECUTIVE_TICK_FAILURES,
                exc,
            )
            # M-4: after N consecutive failures, auto-revert the experiment so it
            # does not loop forever as a poison pill.
            if failures >= _MAX_CONSECUTIVE_TICK_FAILURES:
                logger.error(
                    "karpathy-self-improve: experiment %d exceeded %d consecutive "
                    "eval failures — auto-reverting",
                    exp_id,
                    _MAX_CONSECUTIVE_TICK_FAILURES,
                )
                _consecutive_tick_failures.pop(exp_id, None)
                try:
                    transition(
                        db,
                        exp_id,
                        "reverted",
                        actor="daemon",
                        reason=f"auto-revert: {_MAX_CONSECUTIVE_TICK_FAILURES} consecutive eval failures",
                    )
                except Exception as tr_exc:
                    logger.error(
                        "karpathy-self-improve: could not auto-revert experiment %d: %s",
                        exp_id,
                        tr_exc,
                    )
            continue

        db.update_experiment_fields(exp_id, live_score=live_score, updated_at=now)

        baseline_score = None
        baselines = db.list_baselines(profile)
        if baselines:
            baseline_score = baselines[0].get("score")

        score_holds = baseline_score is None or live_score >= baseline_score

        if score_holds:
            # Transition to verified and record new baseline.
            try:
                transition(db, exp_id, "verified", actor="daemon")
            except ValueError as exc:
                logger.warning(
                    "karpathy-self-improve: cannot verify experiment %d: %s",
                    exp_id,
                    exc,
                )
                continue

            apply_commit_sha = exp.get("apply_commit_sha") or ""
            db.insert_baseline(
                profile=profile,
                file=exp.get("target_relpath", ""),
                commit_sha=apply_commit_sha,
                score=live_score,
                experiment_id=exp_id,
                created_at=now,
            )
            logger.info(
                "karpathy-self-improve: experiment %d verified live_score=%.3f",
                exp_id,
                live_score,
            )
        else:
            # Score dropped — auto-revert.
            # #173: the DB snapshot is the source of truth — restore the
            # exact prior bytes (works on non-git profile dirs). Only
            # pre-#173 rows (apply_commit_sha set, no snapshot) fall back
            # to the legacy git-based revert.
            apply_commit_sha = exp.get("apply_commit_sha") or ""
            profile_root = exp.get("target_profile_root")

            if not profile_root:
                # #176: never default to "." (daemon CWD) — that would write
                # or revert files in the wrong directory. State transition to
                # 'reverted' still proceeds below; only the byte/commit revert
                # is skipped.
                logger.warning(
                    "karpathy-self-improve: experiment %d has no target_profile_root — "
                    "skipping file/commit revert (state still transitions to 'reverted')",
                    exp_id,
                )
            else:
                snap = db.get_snapshot(exp_id)
                if snap is not None:
                    if snap.get("target_relpath"):
                        target_path = Path(profile_root) / snap["target_relpath"]
                        target_path.write_bytes(snap["prior_bytes"])
                elif apply_commit_sha:
                    revert_result = revert_commit(
                        Path(profile_root),
                        apply_commit_sha,
                        message=f"chore: revert karpathy experiment {exp_id} (live score dropped)",
                    )
                    if not revert_result.ok:
                        logger.warning(
                            "karpathy-self-improve: revert failed for experiment %d: %s",
                            exp_id,
                            revert_result.error,
                        )

            try:
                transition(
                    db,
                    exp_id,
                    "reverted",
                    actor="daemon",
                    reason=(
                        f"live_score={live_score:.3f} < baseline={baseline_score:.3f}"
                    ),
                )
            except ValueError as exc:
                logger.warning(
                    "karpathy-self-improve: cannot revert experiment %d: %s",
                    exp_id,
                    exc,
                )

            logger.info(
                "karpathy-self-improve: experiment %d reverted live_score=%.3f baseline=%.3f",
                exp_id,
                live_score,
                baseline_score or 0.0,
            )


def _tick_proposals(db: Any) -> None:
    """For each enabled+non-paused profile with no active experiment, attempt propose."""
    from _proposer import propose_for_profile
    from _wiring import resolve_propose_kwargs, resolve_target_for_profile

    profiles = _get_enabled_profiles(db)
    for profile in profiles:
        if _is_paused(db, profile):
            continue

        # Check for active experiment.
        has_active = any(
            db.list_experiments(profile=profile, state=s)
            for s in ("proposed", "approved", "live")
        )
        if has_active:
            continue

        try:
            target_relpath, profile_root = resolve_target_for_profile(profile, db)
        except ValueError as exc:
            logger.error(
                "karpathy-self-improve: skipping propose for %r — %s",
                profile, exc,
            )
            continue

        try:
            propose_kwargs = resolve_propose_kwargs(profile)
        except ValueError as exc:
            logger.error(
                "karpathy-self-improve: skipping propose for %r — model config error: %s",
                profile, exc,
            )
            continue

        result = propose_for_profile(
            db=db,
            profile=profile,
            target_relpath=target_relpath,
            profile_root=profile_root,
            **propose_kwargs,
        )

        if result.ok and not result.skipped:
            logger.info(
                "karpathy-self-improve: proposed experiment %d for profile %r",
                result.experiment_id,
                profile,
            )
        elif result.skipped:
            logger.debug(
                "karpathy-self-improve: skipped proposal for %r: %s",
                profile,
                result.skip_reason,
            )
        else:
            logger.warning(
                "karpathy-self-improve: proposal failed for %r: %s",
                profile,
                result.error,
            )


async def _daemon_loop(interval: float) -> None:
    """Main async daemon loop."""
    from _db import get_db

    loop = asyncio.get_running_loop()
    stop_event = asyncio.Event()

    def _handle_signal(signum: int, _frame: Any) -> None:
        logger.info("karpathy-self-improve daemon: received signal %d, stopping.", signum)
        loop.call_soon_threadsafe(stop_event.set)

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, _handle_signal)
        except (OSError, ValueError):
            # Cannot set signal in non-main thread — ignore.
            pass

    logger.info(
        "karpathy-self-improve daemon started (interval=%.0fs).", interval
    )

    while not stop_event.is_set():
        db = get_db()
        try:
            _tick_live_experiments(db)
        except Exception as exc:  # pylint: disable=broad-except
            logger.warning("karpathy-self-improve daemon: tick_live error: %s", exc)

        try:
            _tick_proposals(db)
        except Exception as exc:  # pylint: disable=broad-except
            logger.warning("karpathy-self-improve daemon: tick_propose error: %s", exc)

        try:
            await asyncio.wait_for(
                asyncio.shield(stop_event.wait()),
                timeout=interval,
            )
        except asyncio.TimeoutError:
            pass  # Normal — loop continues.

    logger.info("karpathy-self-improve daemon stopped.")


def _cmd_daemon(interval: float) -> None:
    asyncio.run(_daemon_loop(interval))


# ---------------------------------------------------------------------------
# CLI handler (called by hermes karpathy ...)
# ---------------------------------------------------------------------------


def _resolve_cli_profile(cmd: str) -> str:
    """Resolve the target profile for a per-profile karpathy subcommand.

    The top-level ``hermes`` CLI consumes ``--profile/-p`` before argparse
    (``main._apply_profile_override``) and pins it via ``HERMES_HOME``, so
    these subcommands read the active profile instead of declaring their own
    ``--profile`` flag (which the pre-strip would starve — #180).
    Exits with a clear message when no named profile is selected.
    """
    name = ""
    try:
        from hermes_cli.profiles import get_active_profile_name  # type: ignore[import]
        name = (get_active_profile_name() or "").strip()
    except Exception:
        name = ""
    if not name or name == "default":
        print(
            f"error: `hermes karpathy {cmd}` needs a named profile.\n"
            f"Run: hermes --profile <name> karpathy {cmd}",
            file=sys.stderr,
        )
        raise SystemExit(2)
    return name


def _run(ns: argparse.Namespace) -> None:
    """Handler called by the CLI harness when `hermes karpathy ...` is invoked."""
    cmd = getattr(ns, "karpathy_cmd", None)

    if cmd == "collect":
        _cmd_collect()
    elif cmd == "status":
        _cmd_status()
    elif cmd == "propose":
        _cmd_propose(_resolve_cli_profile("propose"))
    elif cmd == "daemon":
        _cmd_daemon(ns.interval)
    elif cmd == "init":
        _cmd_init(getattr(ns, "db_path", None))
    elif cmd == "bootstrap":
        _cmd_bootstrap(_resolve_cli_profile("bootstrap"))
    elif cmd == "pause":
        _cmd_pause(_resolve_cli_profile("pause"))
    elif cmd == "resume":
        _cmd_resume(_resolve_cli_profile("resume"))
    else:
        print(
            "Usage: hermes karpathy "
            "{collect,status,propose,daemon,init,bootstrap,pause,resume}"
        )
