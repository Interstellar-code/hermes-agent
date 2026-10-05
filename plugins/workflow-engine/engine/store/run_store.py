"""
RunStore — CRUD for workflow_runs, node_runs, workflow_events tables.

All methods synchronous. Caller owns transaction commit where noted.
"""
from __future__ import annotations

import functools
import json
import sqlite3
import threading
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Literal, Optional, Sequence, Tuple

# One sqlite connection is shared by the engine-loop thread, dashboard
# threads, tool threads and the cron poller. Python's sqlite3 has one implicit
# transaction per connection, so an unguarded commit on one thread can commit
# (or a rollback discard) another thread's half-done multi-statement write.
# ponytail: one process-wide RLock around every store method; per-connection
# locks if several engines in one process ever need real parallelism.
STORE_LOCK = threading.RLock()


def locked(cls):
    """Class decorator: run every public method under STORE_LOCK."""
    for name, fn in list(vars(cls).items()):
        if isinstance(fn, type(locked)) and not name.startswith("_"):
            def wrap(f):
                @functools.wraps(f)
                def inner(*a, **kw):
                    with STORE_LOCK:
                        return f(*a, **kw)
                return inner
            setattr(cls, name, wrap(fn))
    return cls

# A run's owning process refreshes last_heartbeat every HEARTBEAT_S; a
# pending/running run whose heartbeat is older than STALE_MS has lost its
# owner (process died) and is reaped by any engine's crash recovery.
HEARTBEAT_S = 30.0
STALE_MS = 5 * 60 * 1000


def _now_ms() -> int:
    return int(time.time() * 1000)


def _ms_to_dt(ms: Optional[int]) -> Optional[str]:
    """Convert epoch-ms to ISO-8601 string for JSON serialisation."""
    if ms is None:
        return None
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).isoformat()


# Run-level token usage: sum over top-level node rows only (loop iteration
# rows are already summed into their wrapper row).
_RUN_USAGE_SQL = """
    (SELECT CASE WHEN SUM(n.total_tokens) IS NULL THEN NULL ELSE json_object(
        'input_tokens', SUM(n.input_tokens), 'output_tokens', SUM(n.output_tokens),
        'total_tokens', SUM(n.total_tokens),
        'cost_usd', CASE WHEN COUNT(n.total_tokens) > COUNT(n.cost_usd)
                         THEN NULL ELSE SUM(n.cost_usd) END) END
       FROM node_runs n
      WHERE n.workflow_run_id = workflow_runs.id AND n.loop_iteration IS NULL) AS usage
"""


def _row_to_run(row: sqlite3.Row) -> Dict[str, Any]:
    d = dict(row)
    if "usage" in d:
        d["usage"] = json.loads(d["usage"]) if d["usage"] else None
    d["started_at"] = _ms_to_dt(d.get("started_at"))
    d["completed_at"] = _ms_to_dt(d.get("completed_at"))
    d["last_heartbeat"] = _ms_to_dt(d.get("last_heartbeat"))
    if d.get("metadata"):
        try:
            d["metadata"] = json.loads(d["metadata"])
        except Exception:
            d["metadata"] = {}
    return d


def _row_to_node_run(row: sqlite3.Row) -> Dict[str, Any]:
    d = dict(row)
    d["started_at"] = _ms_to_dt(d.get("started_at"))
    d["completed_at"] = _ms_to_dt(d.get("completed_at"))
    for col in ("depends_on", "skills", "allowed_tools", "denied_tools", "artifact_refs"):
        if d.get(col):
            try:
                d[col] = json.loads(d[col])
            except Exception:
                d[col] = None
    if d.get("metadata"):
        try:
            d["metadata"] = json.loads(d["metadata"])
        except Exception:
            d["metadata"] = {}
    return d


def _row_to_event(row: sqlite3.Row) -> Dict[str, Any]:
    d = dict(row)
    if d.get("data"):
        try:
            d["data"] = json.loads(d["data"])
        except Exception:
            d["data"] = {}
    d["created_at"] = _ms_to_dt(d.get("created_at"))
    return d


def _row_to_schedule(row: sqlite3.Row) -> Dict[str, Any]:
    """API shape of a scheduled_runs row."""
    d = dict(row)
    try:
        trigger = json.loads(d.pop("trigger_json") or "{}")
    except Exception:
        trigger = {}
    try:
        inputs = json.loads(d.pop("inputs_json") or "{}")
    except Exception:
        inputs = {}
    return {
        "id": d["id"],
        "workflow_id": d["workflow_id"],
        "kind": "cron" if d.get("cron_expr") else "at",
        "cron": d.get("cron_expr"),
        "tz": trigger.get("tz"),
        "status": d["status"],
        "enabled": d["status"] != "disabled",
        "next_run_at": d["run_at"],
        "last_error": trigger.get("last_error"),
        "last_run_id": trigger.get("last_run_id"),
        "inputs": inputs,
        "priority": d.get("priority"),
        "max_runtime_s": d.get("max_runtime_s"),
        "created_at": d.get("created_at"),
    }


@locked
class RunStore:
    """CRUD for runs, node_runs and events."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    # ------------------------------------------------------------------ #
    # Workflow Runs                                                        #
    # ------------------------------------------------------------------ #

    def create_workflow_run(
        self,
        *,
        workflow_id: str,
        conversation_id: str,
        working_path: str,
        user_message: str,
        trigger: Optional[Dict[str, Any]] = None,
        priority: int = 0,
        max_runtime_s: Optional[int] = None,
        scheduled_for: Optional[str] = None,
        inputs: Optional[Dict[str, Any]] = None,
        parent_run_id: Optional[str] = None,
        definition_checksum: Optional[str] = None,
        definition_version: Optional[str] = None,
    ) -> Dict[str, Any]:
        run_id = str(uuid.uuid4())
        meta: Dict[str, Any] = {}
        if trigger:
            meta["trigger"] = trigger
        if inputs:
            meta["inputs"] = inputs  # read back by runner.resume
        now = _now_ms()
        self._conn.execute(
            """
            INSERT INTO workflow_runs
              (id, workflow_id, conversation_id, working_path, user_message,
               status, current_phase, metadata, started_at, last_heartbeat,
               priority, max_runtime_s, scheduled_for,
               parent_run_id, definition_checksum, definition_version)
            VALUES (?, ?, ?, ?, ?, 'pending', 'plan', ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                run_id,
                workflow_id,
                conversation_id,
                working_path,
                user_message,
                json.dumps(meta),
                now,
                now,
                priority,
                max_runtime_s,
                scheduled_for,
                parent_run_id,
                definition_checksum,
                definition_version,
            ),
        )
        self._conn.commit()
        return self.get_workflow_run(run_id)  # type: ignore[return-value]

    def insert_definition_snapshot(
        self, *, workflow_id: str, checksum: str, version: Optional[str], yaml: str,
    ) -> None:
        """Pin the YAML a run started from. Idempotent per (workflow_id, checksum)."""
        self._conn.execute(
            "INSERT OR IGNORE INTO workflow_definition_snapshots "
            "(workflow_id, checksum, version, yaml, created_at) VALUES (?, ?, ?, ?, ?)",
            (workflow_id, checksum, version, yaml, _now_ms()),
        )
        self._conn.commit()

    def get_definition_snapshot(self, workflow_id: str, checksum: str) -> Optional[Dict[str, Any]]:
        row = self._conn.execute(
            "SELECT * FROM workflow_definition_snapshots WHERE workflow_id = ? AND checksum = ?",
            (workflow_id, checksum),
        ).fetchone()
        return dict(row) if row else None

    # ------------------------------------------------------------------ #
    # Scheduled Runs                                                       #
    # ------------------------------------------------------------------ #

    def insert_scheduled_run(
        self,
        *,
        workflow_id: str,
        inputs: Dict[str, Any],
        trigger: Dict[str, Any],
        run_at: str,
        priority: int = 0,
        max_runtime_s: Optional[int] = None,
        cron_expr: Optional[str] = None,
    ) -> Dict[str, Any]:
        sid = str(uuid.uuid4())
        created_at = datetime.now(tz=timezone.utc).isoformat()
        self._conn.execute(
            """
            INSERT INTO scheduled_runs
              (id, workflow_id, inputs_json, trigger_json, run_at,
               priority, max_runtime_s, cron_expr, status, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?)
            """,
            (
                sid,
                workflow_id,
                json.dumps(inputs or {}),
                json.dumps(trigger or {}),
                run_at,
                priority,
                max_runtime_s,
                cron_expr,
                created_at,
            ),
        )
        self._conn.commit()
        return {
            "id": sid,
            "workflow_id": workflow_id,
            "run_at": run_at,
            "priority": priority,
            "max_runtime_s": max_runtime_s,
            "cron_expr": cron_expr,
            "status": "pending",
            "created_at": created_at,
        }

    def list_due_scheduled_runs(self, now_iso: str) -> List[Dict[str, Any]]:
        """List pending scheduled rows whose run_at is at or before now_iso."""
        rows = self._conn.execute(
            """
            SELECT * FROM scheduled_runs
             WHERE status = 'pending' AND run_at <= ?
             ORDER BY priority DESC, run_at ASC
            """,
            (now_iso,),
        ).fetchall()
        out: List[Dict[str, Any]] = []
        for r in rows:
            d = dict(r)
            try:
                d["inputs"] = json.loads(d.get("inputs_json") or "{}")
            except Exception:
                d["inputs"] = {}
            try:
                d["trigger"] = json.loads(d.get("trigger_json") or "{}")
            except Exception:
                d["trigger"] = {}
            out.append(d)
        return out

    def claim_scheduled_run(self, scheduled_id: str, now_iso: str) -> bool:
        """Atomic CAS: claim a pending row if still due. True iff this caller won.
        Stamps ``trigger_json.claimed_at`` so a crashed claim can be reset."""
        cur = self._conn.execute(
            """
            UPDATE scheduled_runs
               SET status = 'firing',
                   trigger_json = json_set(trigger_json, '$.claimed_at', ?)
             WHERE id = ? AND status = 'pending' AND run_at <= ?
            """,
            (now_iso, scheduled_id, now_iso),
        )
        self._conn.commit()
        return cur.rowcount == 1

    def mark_scheduled_fired(self, scheduled_id: str) -> None:
        self._conn.execute(
            "UPDATE scheduled_runs SET status = 'fired' WHERE id = ?",
            (scheduled_id,),
        )
        self._conn.commit()

    def mark_scheduled_failed(self, scheduled_id: str) -> None:
        self._conn.execute(
            "UPDATE scheduled_runs SET status = 'failed' WHERE id = ?",
            (scheduled_id,),
        )
        self._conn.commit()

    def reschedule_cron_row(
        self,
        scheduled_id: str,
        run_at: str,
        *,
        last_error: Optional[str] = None,
        last_run_id: Optional[str] = None,
    ) -> bool:
        """firing -> pending at the next occurrence; records the outcome in
        trigger_json. CAS on 'firing' so a disable/delete mid-fire wins."""
        cur = self._conn.execute(
            """
            UPDATE scheduled_runs
               SET status = 'pending', run_at = ?,
                   trigger_json = json_remove(json_set(trigger_json,
                       '$.last_error', ?,
                       '$.last_run_id', COALESCE(?, json_extract(trigger_json, '$.last_run_id'))),
                       '$.claimed_at')
             WHERE id = ? AND status = 'firing'
            """,
            (run_at, last_error, last_run_id, scheduled_id),
        )
        self._conn.commit()
        return cur.rowcount == 1

    def reset_stale_firing(self, cutoff_iso: str) -> int:
        """Rows claimed before ``cutoff_iso`` whose firing never finished (the
        ticking process died mid-fire) go back to 'pending'."""
        cur = self._conn.execute(
            """
            UPDATE scheduled_runs
               SET status = 'pending', trigger_json = json_remove(trigger_json, '$.claimed_at')
             WHERE status = 'firing'
               AND COALESCE(json_extract(trigger_json, '$.claimed_at'), run_at) < ?
            """,
            (cutoff_iso,),
        )
        self._conn.commit()
        return cur.rowcount

    def get_scheduled_run(self, scheduled_id: str) -> Optional[Dict[str, Any]]:
        row = self._conn.execute(
            "SELECT * FROM scheduled_runs WHERE id = ?", (scheduled_id,)
        ).fetchone()
        return _row_to_schedule(row) if row else None

    def list_schedules(self, workflow_id: Optional[str] = None) -> List[Dict[str, Any]]:
        """Live schedules (pending / firing / disabled), soonest first."""
        sql = "SELECT * FROM scheduled_runs WHERE status IN ('pending', 'firing', 'disabled')"
        args: tuple = ()
        if workflow_id:
            sql += " AND workflow_id = ?"
            args = (workflow_id,)
        rows = self._conn.execute(sql + " ORDER BY run_at", args).fetchall()
        return [_row_to_schedule(r) for r in rows]

    def set_schedule_status(
        self, scheduled_id: str, status: str, *, run_at: Optional[str] = None,
    ) -> bool:
        """Move a live schedule to ``status`` (optionally a new run_at)."""
        cur = self._conn.execute(
            "UPDATE scheduled_runs SET status = ?, run_at = COALESCE(?, run_at) "
            "WHERE id = ? AND status IN ('pending', 'firing', 'disabled')",
            (status, run_at, scheduled_id),
        )
        self._conn.commit()
        return cur.rowcount == 1

    def list_active_node_runs(self) -> List[Dict[str, Any]]:
        """Return active node_runs across all workflow_runs.

        Active == status in ('running', 'waiting'). Joins workflow_runs to
        surface the workflow_id alongside each node_run.
        """
        rows = self._conn.execute(
            """
            SELECT nr.id           AS node_run_id,
                   nr.workflow_run_id AS run_id,
                   nr.dag_node_id  AS dag_node_id,
                   wr.workflow_id  AS workflow_id,
                   nr.status       AS status,
                   nr.started_at   AS started_at,
                   nr.assigned_agent AS worker_id,
                   nr.session_id   AS session_id,
                   nr.gateway_run_id AS gateway_run_id
              FROM node_runs nr
              JOIN workflow_runs wr ON wr.id = nr.workflow_run_id
             WHERE nr.status IN ('running', 'waiting')
               AND nr.loop_iteration IS NULL
             ORDER BY nr.started_at ASC
            """,
        ).fetchall()
        out: List[Dict[str, Any]] = []
        for r in rows:
            d = dict(r)
            d["started_at"] = _ms_to_dt(d.get("started_at"))
            out.append(d)
        return out

    def get_workflow_run(self, run_id: str) -> Optional[Dict[str, Any]]:
        row = self._conn.execute(
            f"SELECT *, {_RUN_USAGE_SQL} FROM workflow_runs WHERE id = ?", (run_id,)
        ).fetchone()
        return _row_to_run(row) if row else None

    def list_workflow_runs(
        self,
        *,
        workflow_id: Optional[str] = None,
        statuses: Optional[List[str]] = None,
        limit: int = 50,
        parent_run_id: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        clauses: List[str] = []
        params: List[Any] = []
        if workflow_id:
            clauses.append("workflow_id = ?")
            params.append(workflow_id)
        if parent_run_id:
            clauses.append("parent_run_id = ?")
            params.append(parent_run_id)
        if statuses:
            clauses.append(f"status IN ({','.join('?' * len(statuses))})")
            params.extend(statuses)
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        params.append(limit)
        rows = self._conn.execute(
            f"SELECT *, {_RUN_USAGE_SQL} FROM workflow_runs {where} ORDER BY started_at DESC LIMIT ?",
            params,
        ).fetchall()
        return [_row_to_run(r) for r in rows]

    def update_workflow_run(
        self,
        run_id: str,
        *,
        status: Optional[str] = None,
        error: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> None:
        cols: List[str] = []
        vals: List[Any] = []
        now = _now_ms()
        cols.append("last_heartbeat = ?")
        vals.append(now)
        if status is not None:
            cols.append("status = ?")
            vals.append(status)
            if status in ("completed", "failed", "cancelled"):
                cols.append("completed_at = ?")
                vals.append(now)
        if error is not None:
            cols.append("error = ?")
            vals.append(error)
        if metadata is not None:
            # Merge, never replace: trigger/inputs/pause live side by side.
            # RFC 7396 json_patch: a null value DELETES that key.
            cols.append("metadata = json_patch(COALESCE(metadata, '{}'), json(?))")
            vals.append(json.dumps(metadata))
        vals.append(run_id)
        self._conn.execute(
            f"UPDATE workflow_runs SET {', '.join(cols)} WHERE id = ?",
            vals,
        )
        self._conn.commit()

    def heartbeat_runs(self, run_ids: List[str]) -> None:
        """Refresh last_heartbeat for the live runs this process owns."""
        if not run_ids:
            return
        marks = ",".join("?" * len(run_ids))
        self._conn.execute(
            f"UPDATE workflow_runs SET last_heartbeat = ? "
            f"WHERE id IN ({marks}) AND status IN ('pending', 'running')",
            (_now_ms(), *run_ids),
        )
        self._conn.commit()

    def mark_crashed_runs(self, *, stale_ms: int = STALE_MS) -> int:
        """Fail pending/running runs whose owner stopped heartbeating.

        Every engine heartbeats the runs it is executing (``heartbeat_runs``),
        so a stale heartbeat means the owning process is gone. Live runs owned
        by *other* processes keep fresh heartbeats and are left alone (L2-02).
        No auto-resume.
        """
        now = _now_ms()
        result = self._conn.execute(
            """
            UPDATE workflow_runs
               SET status = 'failed', error = 'crashed: owner process stopped', completed_at = ?
             WHERE status IN ('pending', 'running') AND last_heartbeat < ?
            """,
            (now, now - stale_ms),
        )
        self._conn.commit()
        return result.rowcount

    def delete_terminal_runs_older_than(self, days: int) -> int:
        """Retention sweep: drop terminal runs (and, via CASCADE, their
        node_runs / events / transitions) completed more than ``days`` ago."""
        cutoff = _now_ms() - days * 86_400_000
        cur = self._conn.execute(
            "DELETE FROM workflow_runs WHERE status IN ('completed', 'failed', 'cancelled') "
            "AND completed_at IS NOT NULL AND completed_at < ?",
            (cutoff,),
        )
        self._conn.commit()
        return cur.rowcount

    def set_owner_session(self, run_id: str, session_id: str) -> None:
        self._conn.execute(
            "UPDATE workflow_runs SET owner_session = ? WHERE id = ?",
            (session_id, run_id),
        )
        self._conn.commit()

    def cancel_workflow_run(self, run_id: str) -> bool:
        """Mark a run cancelled if it isn't already terminal.

        Returns True when the workflow_run row was actually flipped to
        cancelled by *this* call (rowcount == 1). Callers gate
        ``workflow_cancelled`` event emission and phase-transition
        records on the return value so a stray late cancel does not
        double-fire after the run already settled (completed/failed
        won the race) or after a prior cancel already recorded the
        transition.
        """
        now = _now_ms()
        cur = self._conn.execute(
            """
            UPDATE workflow_runs
               SET status = 'cancelled', completed_at = ?
             WHERE id = ? AND status NOT IN ('completed', 'failed', 'cancelled')
            """,
            (now, run_id),
        )
        # Cancel any running/pending node_runs too — always safe, the
        # WHERE clause excludes already-terminal rows.
        self._conn.execute(
            """
            UPDATE node_runs
               SET status = 'cancelled', completed_at = ?
             WHERE workflow_run_id = ? AND status NOT IN ('completed', 'failed', 'cancelled', 'skipped')
            """,
            (now, run_id),
        )
        self._conn.commit()
        return cur.rowcount == 1

    def finish_workflow_run_if_running(
        self,
        run_id: str,
        *,
        status: str,
        error: Optional[str] = None,
        from_statuses: tuple = ("running",),
    ) -> bool:
        """Atomic compare-and-set finaliser.

        Flips the run to ``status`` only when its current status is still
        ``running``. Returns True on a real transition, False when somebody
        else (e.g. a resume path or a pause callback) already moved the row
        out of ``running``. Callers use the return value to decide whether
        to emit terminal events — emitting them on the False branch would
        double-fire workflow_completed/workflow_failed after a paused
        approval gate was re-resumed.

        ``status`` must be a terminal phase the schema accepts (completed,
        failed, cancelled).
        """
        if status not in ("completed", "failed", "cancelled"):
            raise ValueError(f"non-terminal status not allowed here: {status}")
        now = _now_ms()
        marks = ",".join("?" * len(from_statuses))
        cur = self._conn.execute(
            f"""
            UPDATE workflow_runs
               SET status = ?, completed_at = ?, last_heartbeat = ?,
                   error = COALESCE(?, error)
             WHERE id = ? AND status IN ({marks})
            """,
            (status, now, now, error, run_id, *from_statuses),
        )
        self._conn.commit()
        return cur.rowcount == 1

    def pause_workflow_run(
        self, run_id: str, metadata: Optional[Dict[str, Any]] = None,
    ) -> bool:
        """Flip the run to 'paused' only when it's still 'running'.

        Returns True when the CAS took. Without the ``status='running'``
        guard, a pause coming from an approval node could clobber a
        ``cancelled`` status set by a concurrent ``cancel_workflow_run``
        between the approval's get-status check and its pause write.
        """
        now = _now_ms()
        cur = self._conn.execute(
            "UPDATE workflow_runs SET status = 'paused', last_heartbeat = ?, "
            "metadata = json_set(COALESCE(metadata, '{}'), '$.pause', json(?)) "
            "WHERE id = ? AND status = 'running'",
            (now, json.dumps(metadata or {}), run_id),
        )
        self._conn.commit()
        return cur.rowcount == 1

    def resume_workflow_run(self, run_id: str) -> bool:
        """CAS paused → running. Returns True when this call flipped it."""
        now = _now_ms()
        cur = self._conn.execute(
            "UPDATE workflow_runs SET status = 'running', last_heartbeat = ? WHERE id = ? AND status = 'paused'",
            (now, run_id),
        )
        self._conn.commit()
        return cur.rowcount == 1

    # ------------------------------------------------------------------ #
    # Node Runs                                                           #
    # ------------------------------------------------------------------ #

    def create_node_run(
        self,
        *,
        workflow_run_id: str,
        dag_node_id: str,
        node_type: str,
        node_run_id: Optional[str] = None,
        agent_profile_hint: Optional[str] = None,
        skills: Optional[List[str]] = None,
        model_hint: Optional[str] = None,
        parent_subgraph_node_run_id: Optional[str] = None,
        loop_iteration: Optional[int] = None,
        loop_parent_node_run_id: Optional[str] = None,
        approval_message: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        nr_id = node_run_id or str(uuid.uuid4())
        now = _now_ms()
        self._conn.execute(
            """
            INSERT OR IGNORE INTO node_runs
              (id, workflow_run_id, dag_node_id, node_type, status,
               agent_profile_hint, skills, model_hint,
               parent_subgraph_node_run_id, loop_iteration, loop_parent_node_run_id,
               approval_message, metadata, started_at)
            VALUES (?, ?, ?, ?, 'running', ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                nr_id,
                workflow_run_id,
                dag_node_id,
                node_type,
                agent_profile_hint,
                json.dumps(skills) if skills else None,
                model_hint,
                parent_subgraph_node_run_id,
                loop_iteration,
                loop_parent_node_run_id,
                approval_message,
                json.dumps(metadata) if metadata else None,
                now,
            ),
        )
        self._conn.commit()
        return self.get_node_run(nr_id)  # type: ignore[return-value]

    def get_node_run(self, node_run_id: str) -> Optional[Dict[str, Any]]:
        row = self._conn.execute(
            "SELECT * FROM node_runs WHERE id = ?", (node_run_id,)
        ).fetchone()
        return _row_to_node_run(row) if row else None

    def find_node_run(
        self,
        workflow_run_id: str,
        dag_node_id: str,
        loop_iteration: Optional[int] = None,
    ) -> Optional[Dict[str, Any]]:
        if loop_iteration is not None:
            row = self._conn.execute(
                "SELECT * FROM node_runs WHERE workflow_run_id = ? AND dag_node_id = ? AND loop_iteration = ?",
                (workflow_run_id, dag_node_id, loop_iteration),
            ).fetchone()
        else:
            row = self._conn.execute(
                "SELECT * FROM node_runs WHERE workflow_run_id = ? AND dag_node_id = ? AND loop_iteration IS NULL",
                (workflow_run_id, dag_node_id),
            ).fetchone()
        return _row_to_node_run(row) if row else None

    def list_node_runs(self, workflow_run_id: str) -> List[Dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT * FROM node_runs WHERE workflow_run_id = ? ORDER BY started_at",
            (workflow_run_id,),
        ).fetchall()
        return [_row_to_node_run(r) for r in rows]

    def update_node_run(self, node_run_id: str, patch: Dict[str, Any]) -> None:
        _ALLOWED = {
            "status", "error", "summary", "completed_at", "started_at",
            "kanban_task_id", "assigned_agent", "approval_response",
            "artifact_refs", "metadata", "skip_reason",
            "retries", "max_retries", "retry_delay_ms",
            "approval_message", "session_id", "gateway_run_id",
        }
        cols: List[str] = []
        vals: List[Any] = []
        for k, v in patch.items():
            if k not in _ALLOWED:
                raise ValueError(f"update_node_run: unknown column '{k}'")
            if k in ("artifact_refs", "metadata") and v is not None:
                v = json.dumps(v)
            cols.append(f"{k} = ?")
            vals.append(v)
        if not cols:
            return
        vals.append(node_run_id)
        self._conn.execute(
            f"UPDATE node_runs SET {', '.join(cols)} WHERE id = ?",
            vals,
        )
        self._conn.commit()

    def add_node_usage(self, node_run_id: str, usage: Dict[str, Any]) -> None:
        """Add a usage payload to a node_run (additive: retries and loop
        iterations accumulate). Unknown cost leaves cost_usd untouched."""
        cost = usage.get("cost_usd")
        self._conn.execute(
            """
            UPDATE node_runs
               SET input_tokens  = COALESCE(input_tokens, 0) + ?,
                   output_tokens = COALESCE(output_tokens, 0) + ?,
                   total_tokens  = COALESCE(total_tokens, 0) + ?,
                   cost_usd      = CASE WHEN ? IS NULL THEN cost_usd
                                        ELSE COALESCE(cost_usd, 0) + ? END,
                   model         = COALESCE(?, model),
                   provider      = COALESCE(?, provider)
             WHERE id = ?
            """,
            (
                int(usage.get("input_tokens") or 0),
                int(usage.get("output_tokens") or 0),
                int(usage.get("total_tokens") or 0),
                cost, cost,
                usage.get("model"), usage.get("provider"),
                node_run_id,
            ),
        )
        self._conn.commit()

    def try_claim_approval(
        self,
        node_run_id: str,
        decision: Literal["approve", "reject"],
        comment: Optional[str],
        actor: Optional[str] = None,
    ) -> bool:
        """Atomic CAS: update node_run status from paused → completed/failed. Returns True if claimed.

        Merges ``{decided_at, approved_by}`` into metadata (approved_by only
        when an actor is given; it is self-reported by the caller)."""
        terminal = "completed" if decision == "approve" else "failed"
        now = _now_ms()
        decided: Dict[str, Any] = {"decided_at": _ms_to_dt(now)}
        if actor:
            decided["approved_by"] = actor
        result = self._conn.execute(
            """
            UPDATE node_runs
               SET status = ?, approval_response = ?, completed_at = ?,
                   metadata = json_patch(COALESCE(metadata, '{}'), json(?))
             WHERE id = ? AND status = 'paused'
            """,
            (terminal, comment or decision, now, json.dumps(decided), node_run_id),
        )
        self._conn.commit()
        return result.rowcount > 0

    # ------------------------------------------------------------------ #
    # Workflow Events                                                      #
    # ------------------------------------------------------------------ #

    def insert_event(
        self,
        *,
        workflow_run_id: str,
        event_type: str,
        node_run_id: Optional[str] = None,
        data: Optional[Dict[str, Any]] = None,
        event_id: Optional[str] = None,
        step_index: Optional[int] = None,
        step_name: Optional[str] = None,
    ) -> Tuple[str, int]:
        """Persist one event; returns ``(id, seq)`` — seq is the row's rowid,
        the cursor for ``list_events_after`` and the SSE dedupe key."""
        eid = event_id or str(uuid.uuid4())
        now = _now_ms()
        cur = self._conn.execute(
            """
            INSERT OR IGNORE INTO workflow_events
              (id, workflow_run_id, node_run_id, event_type, step_index, step_name, data, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                eid,
                workflow_run_id,
                node_run_id,
                event_type,
                step_index,
                step_name,
                json.dumps(data) if data else None,
                now,
            ),
        )
        self._conn.commit()
        if cur.rowcount:
            return eid, cur.lastrowid
        # duplicate id (INSERT OR IGNORE): report the existing row's seq
        row = self._conn.execute("SELECT rowid FROM workflow_events WHERE id = ?", (eid,)).fetchone()
        return eid, row[0]

    def list_events(
        self,
        workflow_run_id: str,
        *,
        limit: int = 50,
        offset: int = 0,
    ) -> List[Dict[str, Any]]:
        rows = self._conn.execute(
            """
            SELECT * FROM workflow_events
             WHERE workflow_run_id = ?
             ORDER BY created_at ASC
             LIMIT ? OFFSET ?
            """,
            (workflow_run_id, limit, offset),
        ).fetchall()
        return [_row_to_event(r) for r in rows]

    @staticmethod
    def _event_filter(
        run_id: Optional[str],
        node_run_id: Optional[str],
        types: Optional[Sequence[str]],
        exclude_types: Sequence[str],
    ) -> Tuple[str, List[Any]]:
        """WHERE clause: ``types`` (when given) wins over ``exclude_types``."""
        clauses: List[str] = []
        args: List[Any] = []
        if run_id:
            clauses.append("workflow_run_id = ?")
            args.append(run_id)
        if node_run_id:
            clauses.append("node_run_id = ?")
            args.append(node_run_id)
        if types:
            clauses.append(f"event_type IN ({','.join('?' * len(types))})")
            args.extend(types)
        elif exclude_types:
            clauses.append(f"event_type NOT IN ({','.join('?' * len(exclude_types))})")
            args.extend(exclude_types)
        return " AND ".join(clauses) or "1", args

    def list_recent_events(
        self,
        run_id: Optional[str],
        *,
        limit: int = 50,
        node_run_id: Optional[str] = None,
        types: Optional[Sequence[str]] = None,
        exclude_types: Sequence[str] = ("node_log",),
    ) -> List[Dict[str, Any]]:
        """Last N events for a run (or all runs if run_id is None), ascending.

        node_log is excluded by default: it is high-volume live output and
        would crowd lifecycle events out of the replay / run-detail windows.
        """
        where, args = self._event_filter(run_id, node_run_id, types, exclude_types)
        rows = self._conn.execute(
            f"""
            SELECT * FROM (
                SELECT rowid AS seq, * FROM workflow_events WHERE {where}
                ORDER BY created_at DESC, rowid DESC LIMIT ?
            ) ORDER BY created_at ASC, seq ASC
            """,
            (*args, limit),
        ).fetchall()
        return [_row_to_event(r) for r in rows]

    def list_events_after(
        self,
        run_id: str,
        after: int,
        limit: int = 500,
        *,
        node_run_id: Optional[str] = None,
        types: Optional[Sequence[str]] = None,
        exclude_types: Sequence[str] = (),
    ) -> List[Dict[str, Any]]:
        """Events with seq (rowid) > ``after``, ascending by seq — cursor
        paging and the cross-process SSE tail. Includes node_log by default."""
        where, args = self._event_filter(run_id, node_run_id, types, exclude_types)
        rows = self._conn.execute(
            f"SELECT rowid AS seq, * FROM workflow_events WHERE {where} AND rowid > ? "
            "ORDER BY rowid LIMIT ?",
            (*args, after, limit),
        ).fetchall()
        return [_row_to_event(r) for r in rows]

    def max_event_rowid(self, run_id: str) -> int:
        row = self._conn.execute(
            "SELECT MAX(rowid) FROM workflow_events WHERE workflow_run_id = ?", (run_id,)
        ).fetchone()
        return row[0] or 0

    # ------------------------------------------------------------------ #
    # Phase Transitions                                                    #
    # ------------------------------------------------------------------ #

    def record_phase_transition(
        self,
        *,
        run_id: str,
        to_phase: str,
        decided_by: str,
        decision_data: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Record a phase transition for a run. Returns {from, to}."""
        row = self._conn.execute(
            "SELECT current_phase FROM workflow_runs WHERE id = ?", (run_id,)
        ).fetchone()
        if row is None:
            raise ValueError(f"WorkflowRun not found: {run_id}")
        from_phase = row["current_phase"]
        if from_phase == to_phase:
            return {"from": from_phase, "to": to_phase}
        tid = str(uuid.uuid4())
        now = _now_ms()
        self._conn.execute(
            "UPDATE workflow_runs SET current_phase = ? WHERE id = ?",
            (to_phase, run_id),
        )
        self._conn.execute(
            """
            INSERT INTO phase_transitions
              (id, workflow_run_id, from_phase, to_phase, decided_by, decision_data, at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                tid,
                run_id,
                from_phase,
                to_phase,
                decided_by,
                json.dumps(decision_data) if decision_data else None,
                now,
            ),
        )
        self._conn.commit()
        return {"from": from_phase, "to": to_phase}

    def list_phase_transitions(self, run_id: str) -> List[Dict[str, Any]]:
        rows = self._conn.execute(
            """
            SELECT id, from_phase, to_phase, decided_by, decision_data, at
              FROM phase_transitions
             WHERE workflow_run_id = ?
             ORDER BY at ASC
            """,
            (run_id,),
        ).fetchall()
        result = []
        for r in rows:
            d = dict(r)
            if d.get("decision_data"):
                try:
                    d["decision_data"] = json.loads(d["decision_data"])
                except Exception:
                    d["decision_data"] = None
            result.append(d)
        return result

    # ------------------------------------------------------------------ #
    # Extended lookups                                                     #
    # ------------------------------------------------------------------ #

    def find_run_by_conversation_id(self, conversation_id: str) -> Optional[Dict[str, Any]]:
        row = self._conn.execute(
            "SELECT * FROM workflow_runs WHERE conversation_id = ? ORDER BY started_at DESC LIMIT 1",
            (conversation_id,),
        ).fetchone()
        return _row_to_run(row) if row else None

    def get_active_run_by_path(self, scope_path: str) -> Optional[Dict[str, Any]]:
        """Return the most recent active run for scope_path (pending/running/paused)."""
        STALE_MS = 5 * 60 * 1000
        stale_threshold = _now_ms() - STALE_MS
        row = self._conn.execute(
            """
            SELECT * FROM workflow_runs
             WHERE working_path = ?
               AND status IN ('pending', 'running', 'paused')
               AND (status != 'pending' OR last_heartbeat >= ?)
             ORDER BY started_at ASC, id ASC
             LIMIT 1
            """,
            (scope_path, stale_threshold),
        ).fetchone()
        return _row_to_run(row) if row else None

    def find_node_run_by_id(self, node_run_id: str) -> Optional[Dict[str, Any]]:
        row = self._conn.execute(
            "SELECT * FROM node_runs WHERE id = ? LIMIT 1", (node_run_id,)
        ).fetchone()
        return _row_to_node_run(row) if row else None

    def append_workflow_event(
        self,
        *,
        workflow_run_id: str,
        event_type: str,
        node_run_id: Optional[str] = None,
        step_index: Optional[int] = None,
        step_name: Optional[str] = None,
        data: Optional[Dict[str, Any]] = None,
        event_id: Optional[str] = None,
        created_at: Optional[int] = None,
    ) -> None:
        """Alias for insert_event with extended fields matching TS appendWorkflowEvent."""
        eid = event_id or str(uuid.uuid4())
        now = created_at or _now_ms()
        self._conn.execute(
            """
            INSERT OR IGNORE INTO workflow_events
              (id, workflow_run_id, node_run_id, event_type, step_index, step_name, data, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                eid,
                workflow_run_id,
                node_run_id,
                event_type,
                step_index,
                step_name,
                json.dumps(data) if data else None,
                now,
            ),
        )
        self._conn.commit()
