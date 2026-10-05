"""Native cron schedules ("Repeat"): next fire time in the host's local TZ.

croniter's tz-aware mode mis-handles DST transitions (it can return a past
instant after fall-back, or the wrong offset after spring-forward), so the
expression is evaluated on naive local wall-clock time and each candidate is
mapped to an instant with ``time.mktime`` (libc honours the host zone, DST
included). Wall times that don't exist (spring-forward gap) normalise forward;
candidates not strictly after ``after`` are skipped, so the result always
moves forward. Wall times that repeat (fall-back) map to one instant: there
are no fires in the repeated DST hour.
"""
from __future__ import annotations

import os
import time
from datetime import datetime, timezone

MAX_CRON_LEN = 128


def _croniter():
    from croniter import croniter  # noqa: PLC0415 — optional dep, 501 when missing
    return croniter


def validate_cron(expr: str) -> None:
    """Raise ValueError for anything but a 5-field (or @alias) expression.
    ImportError propagates when croniter is not installed."""
    croniter = _croniter()
    if not isinstance(expr, str) or not expr.strip() or len(expr) > MAX_CRON_LEN:
        raise ValueError(f"schedule.cron must be a cron string of at most {MAX_CRON_LEN} chars")
    expr = expr.strip()
    fields = expr.split()
    ok = len(fields) == 5 or (len(fields) == 1 and expr.startswith("@") and expr != "@reboot")
    if not ok or not croniter.is_valid(expr):
        raise ValueError(f"invalid cron expression: {expr!r}")


def next_fire(expr: str, after: float) -> float:
    """Epoch seconds of the first occurrence strictly after ``after``."""
    it = _croniter()(expr.strip(), datetime.fromtimestamp(after).replace(microsecond=0))
    for _ in range(1000):
        ts = time.mktime(it.get_next(datetime).timetuple())
        if ts > after:
            return ts
    raise ValueError(f"cron expression never fires: {expr!r}")


def to_iso(ts: float) -> str:
    """UTC ISO-8601, the format scheduled_runs.run_at is compared in."""
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()


def local_tz_name() -> str:
    """Informational name of the host zone (stored as trigger_json.tz)."""
    if os.environ.get("TZ"):
        return os.environ["TZ"]
    real = os.path.realpath("/etc/localtime")
    return real.split("zoneinfo/", 1)[1] if "zoneinfo/" in real else time.tzname[0]
