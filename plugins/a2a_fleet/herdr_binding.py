"""Durable Herdr binding, confirmation-token, and audit store.

Everything here lives in SQLite in the profile ``state.db``. It deliberately
does NOT live in :mod:`context_store`, which is an in-memory LRU (max 500
contexts) wiped on every gateway restart. Herdr sessions *survive* gateway
restarts — that is the whole value of the mode — so a binding or audit record
that evaporates on a bounce produces exactly the failure this store exists to
prevent: a live pane carrying automation state that no longer exists anywhere,
with no way to tell whether the last action landed.

Three tables, all prefixed ``herdr_`` because ``state.db`` is shared:

``herdr_tokens``    confirmation tokens issued by preview, consumed by request.
``herdr_bindings``  one row per (host_alias, terminal_id) session binding.
``herdr_audit``     append-only trail; the only durable record of what was sent.

Functions here are synchronous. Callers on the gateway event loop must wrap
them in ``asyncio.to_thread`` — a sync SQLite call against this profile's
1.8 GB ``state.db`` on the loop is how #169's event-loop stall happened.
"""
from __future__ import annotations

import logging
import secrets
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

log = logging.getLogger("a2a_fleet.herdr_binding")

# Tokens are meant to be confirmed by a human in the same breath as the
# preview. Minutes, not hours: a stale token is a stale intention, and the
# revision guard only covers the pane changing, not the operator changing.
TOKEN_TTL_SECONDS = 300

COMPLETION_STATES = frozenset(
    {"pending", "completed", "failed", "cancelled", "human_takeover"}
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS herdr_tokens (
    token       TEXT PRIMARY KEY,
    action_id   TEXT NOT NULL,
    host_alias  TEXT NOT NULL,
    terminal_id TEXT NOT NULL,
    pane_id     TEXT,
    revision    INTEGER,
    action      TEXT NOT NULL,
    summary     TEXT,
    created_at  REAL NOT NULL,
    expires_at  REAL NOT NULL,
    consumed_at REAL
);
CREATE TABLE IF NOT EXISTS herdr_bindings (
    binding_id               TEXT PRIMARY KEY,
    fleet_context_id         TEXT,
    host_alias               TEXT NOT NULL,
    transport_kind           TEXT,
    terminal_id              TEXT NOT NULL,
    pane_id                  TEXT,
    revision_at_preview      INTEGER,
    agent_kind               TEXT,
    workspace_canonical_path TEXT,
    action_id                TEXT,
    started_at               TEXT,
    last_observed_at         TEXT,
    completion_state         TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS herdr_audit (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          TEXT NOT NULL,
    host_alias  TEXT,
    terminal_id TEXT,
    action_id   TEXT,
    event       TEXT NOT NULL,
    detail      TEXT
);
CREATE INDEX IF NOT EXISTS herdr_audit_session_idx
    ON herdr_audit (host_alias, terminal_id);
"""


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def db_path() -> Path:
    from hermes_constants import get_hermes_home  # noqa: PLC0415 — lazy, mirrors fleet_config.

    return get_hermes_home() / "state.db"


def connect(path: Optional[Path] = None) -> sqlite3.Connection:
    """Open the store and ensure its schema exists."""
    conn = sqlite3.connect(str(path or db_path()))
    conn.row_factory = sqlite3.Row
    conn.executescript(_SCHEMA)
    return conn


def binding_id(host_alias: str, terminal_id: str) -> str:
    return f"{host_alias}:{terminal_id}"


# ---------------------------------------------------------------------------
# Confirmation tokens
# ---------------------------------------------------------------------------


def issue_token(
    conn: sqlite3.Connection,
    *,
    host_alias: str,
    terminal_id: str,
    pane_id: Optional[str],
    revision: Optional[int],
    action: str,
    summary: str,
    ttl_seconds: Optional[int] = None,
) -> Dict[str, Any]:
    """Mint a single-use confirmation token bound to one session and revision.

    ``ttl_seconds`` resolves at call time rather than as a default argument, so
    ``TOKEN_TTL_SECONDS`` stays a real knob instead of a value frozen at import.
    """
    ttl_seconds = TOKEN_TTL_SECONDS if ttl_seconds is None else ttl_seconds
    now = time.time()
    token = secrets.token_urlsafe(24)
    action_id = f"act_{secrets.token_hex(8)}"
    with conn:
        conn.execute(
            "INSERT INTO herdr_tokens (token, action_id, host_alias, terminal_id, "
            "pane_id, revision, action, summary, created_at, expires_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                token,
                action_id,
                host_alias,
                terminal_id,
                pane_id,
                revision,
                action,
                summary,
                now,
                now + ttl_seconds,
            ),
        )
    return {
        "token": token,
        "action_id": action_id,
        "expires_at": now + ttl_seconds,
        "revision": revision,
    }


def consume_token(
    conn: sqlite3.Connection,
    *,
    token: str,
    host_alias: str,
    terminal_id: str,
) -> Dict[str, Any]:
    """Atomically claim ``token`` for exactly this session.

    Returns ``{"ok": True, "row": {...}}`` or ``{"ok": False, "reason": ...}``.

    The claim is a single conditional UPDATE, not read-then-write: two
    concurrent requests racing the same token must not both pass, and
    ``agent send`` has no request ID, so a double-claim would be a genuine
    double-send with no way to detect it afterwards.
    """
    now = time.time()
    with conn:
        cur = conn.execute(
            "UPDATE herdr_tokens SET consumed_at = ? "
            "WHERE token = ? AND consumed_at IS NULL AND expires_at > ? "
            "AND host_alias = ? AND terminal_id = ?",
            (now, token, now, host_alias, terminal_id),
        )
        claimed = cur.rowcount == 1

    row = conn.execute(
        "SELECT * FROM herdr_tokens WHERE token = ?", (token,)
    ).fetchone()

    if claimed and row is not None:
        return {"ok": True, "row": dict(row)}

    # Report *why* it failed, since "invalid token" hides an already-sent action.
    if row is None:
        return {"ok": False, "reason": "unknown_token"}
    if row["consumed_at"] is not None and row["consumed_at"] != now:
        return {"ok": False, "reason": "token_already_used"}
    if row["expires_at"] <= now:
        return {"ok": False, "reason": "token_expired"}
    return {"ok": False, "reason": "token_session_mismatch"}


# ---------------------------------------------------------------------------
# Bindings
# ---------------------------------------------------------------------------


def upsert_binding(conn: sqlite3.Connection, **fields: Any) -> str:
    """Insert or update the binding for one session; returns its ``binding_id``.

    Only the fields passed are written — an update never blanks a column the
    caller did not mention (a takeover must not erase the workspace or
    action_id the operator needs in order to inspect what was running).
    """
    host_alias = fields["host_alias"]
    terminal_id = fields["terminal_id"]
    bid = binding_id(host_alias, terminal_id)
    state = fields.get("completion_state", "pending")
    if state not in COMPLETION_STATES:
        raise ValueError(f"unknown completion_state {state!r}")

    columns = {
        "fleet_context_id",
        "transport_kind",
        "pane_id",
        "revision_at_preview",
        "agent_kind",
        "workspace_canonical_path",
        "action_id",
    }
    provided = {k: v for k, v in fields.items() if k in columns}

    with conn:
        existing = conn.execute(
            "SELECT binding_id FROM herdr_bindings WHERE binding_id = ?", (bid,)
        ).fetchone()
        if existing is None:
            cols = ["binding_id", "host_alias", "terminal_id", "started_at",
                    "last_observed_at", "completion_state", *provided]
            values = [bid, host_alias, terminal_id, _now_iso(), _now_iso(), state,
                      *provided.values()]
            conn.execute(
                f"INSERT INTO herdr_bindings ({', '.join(cols)}) "
                f"VALUES ({', '.join('?' * len(cols))})",
                values,
            )
        else:
            sets = ["last_observed_at = ?", "completion_state = ?"]
            values = [_now_iso(), state]
            for key, value in provided.items():
                sets.append(f"{key} = ?")
                values.append(value)
            values.append(bid)
            conn.execute(
                f"UPDATE herdr_bindings SET {', '.join(sets)} WHERE binding_id = ?",
                values,
            )
    return bid


def get_binding(
    conn: sqlite3.Connection, host_alias: str, terminal_id: str
) -> Optional[Dict[str, Any]]:
    row = conn.execute(
        "SELECT * FROM herdr_bindings WHERE binding_id = ?",
        (binding_id(host_alias, terminal_id),),
    ).fetchone()
    return dict(row) if row is not None else None


def get_binding_by_context(
    conn: sqlite3.Connection, fleet_context_id: str
) -> Optional[Dict[str, Any]]:
    """Find the session a Fleet context is already bound to.

    This is what makes ``context_id`` continuity real across gateway restarts:
    the same A2A context must reach the same pane, not merely a pane that
    currently looks similar. Most recent wins if a context was ever rebound.
    """
    if not fleet_context_id:
        return None
    row = conn.execute(
        "SELECT * FROM herdr_bindings WHERE fleet_context_id = ? "
        "ORDER BY last_observed_at DESC LIMIT 1",
        (fleet_context_id,),
    ).fetchone()
    return dict(row) if row is not None else None


def automation_blocked(
    conn: sqlite3.Connection, host_alias: str, terminal_id: str
) -> bool:
    """True while a human holds this session.

    Fleet-side only, and deliberately so. Herdr's own authority model
    (``pane report-agent`` / ``release-agent``) cannot carry this: its
    ``--source`` is a hardcoded allowlist in ``src/detect/mod.rs``, and
    ``("herdr:hermes", "hermes")`` is classified session-identity-only —
    ``set_hook_authority_at`` drops those reports outright. Claiming authority
    under any other source would mean lying to Herdr about which agent owns the
    pane. So Fleet keeps its own pause flag and never writes authority.
    """
    binding = get_binding(conn, host_alias, terminal_id)
    return bool(binding and binding["completion_state"] == "human_takeover")


# ---------------------------------------------------------------------------
# Audit
# ---------------------------------------------------------------------------


def record_audit(
    conn: sqlite3.Connection,
    *,
    event: str,
    host_alias: str = "",
    terminal_id: str = "",
    action_id: str = "",
    detail: str = "",
) -> None:
    """Append one audit row. Never raises — losing the trail must not fail work."""
    try:
        with conn:
            conn.execute(
                "INSERT INTO herdr_audit (ts, host_alias, terminal_id, action_id, "
                "event, detail) VALUES (?, ?, ?, ?, ?, ?)",
                (_now_iso(), host_alias, terminal_id, action_id, event, detail),
            )
    except sqlite3.Error:  # pragma: no cover - defensive
        log.warning("herdr audit write failed for event %r", event, exc_info=True)


def recent_audit(
    conn: sqlite3.Connection, host_alias: str, terminal_id: str, limit: int = 20
) -> list:
    rows = conn.execute(
        "SELECT ts, event, action_id, detail FROM herdr_audit "
        "WHERE host_alias = ? AND terminal_id = ? ORDER BY id DESC LIMIT ?",
        (host_alias, terminal_id, limit),
    ).fetchall()
    return [dict(r) for r in rows]
