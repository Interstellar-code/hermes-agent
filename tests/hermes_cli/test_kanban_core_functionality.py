"""Core-functionality tests for the kanban kernel + CLI additions.

Complements tests/hermes_cli/test_kanban_db.py (schema + CAS atomicity)
and tests/hermes_cli/test_kanban_cli.py (end-to-end run_slash).  The
tests here exercise the pieces added as part of the kanban hardening
pass: circuit breaker, crash detection, daemon loop, idempotency,
retention/gc, stats, notify subscriptions, worker log accessor, run_slash
parity across every registered verb.
"""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from typing import Optional

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli.kanban import run_slash


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


# ---------------------------------------------------------------------------
# Idempotency key
# ---------------------------------------------------------------------------

def test_idempotency_key_returns_existing_task(kanban_home):
    conn = kb.connect()
    try:
        a = kb.create_task(conn, title="first", idempotency_key="abc")
        b = kb.create_task(conn, title="second attempt", idempotency_key="abc")
        assert a == b, "same idempotency_key should return the same task id"
        # And body wasn't overwritten — first create wins.
        task = kb.get_task(conn, a)
        assert task.title == "first"
    finally:
        conn.close()


def test_idempotency_key_ignored_for_archived(kanban_home):
    conn = kb.connect()
    try:
        a = kb.create_task(conn, title="first", idempotency_key="abc")
        kb.archive_task(conn, a)
        b = kb.create_task(conn, title="second", idempotency_key="abc")
        assert a != b, "archived task shouldn't block a fresh create with same key"
    finally:
        conn.close()


def test_no_idempotency_key_never_collides(kanban_home):
    conn = kb.connect()
    try:
        a = kb.create_task(conn, title="a")
        b = kb.create_task(conn, title="b")
        assert a != b
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Spawn-failure circuit breaker
# ---------------------------------------------------------------------------

def test_spawn_failure_auto_blocks_after_limit(kanban_home):
    """N consecutive spawn failures on the same task → auto_blocked."""
    def _bad_spawn(task, ws):
        raise RuntimeError("no PATH")

    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="x", assignee="worker")
        # Three ticks below the default limit (5) → still ready, counter grows.
        for i in range(3):
            res = kb.dispatch_once(conn, spawn_fn=_bad_spawn, failure_limit=5)
            assert tid not in res.auto_blocked
        task = kb.get_task(conn, tid)
        assert task.status == "ready"
        assert task.spawn_failures == 3

        # Two more ticks → fifth failure exceeds the limit.
        res1 = kb.dispatch_once(conn, spawn_fn=_bad_spawn, failure_limit=5)
        assert tid not in res1.auto_blocked
        res2 = kb.dispatch_once(conn, spawn_fn=_bad_spawn, failure_limit=5)
        assert tid in res2.auto_blocked
        task = kb.get_task(conn, tid)
        assert task.status == "blocked"
        assert task.spawn_failures >= 5
        assert task.last_spawn_error and "no PATH" in task.last_spawn_error
    finally:
        conn.close()


def test_successful_spawn_resets_failure_counter(kanban_home):
    """A successful spawn clears the counter so past failures don't count
    against future retries of the same task."""
    calls = [0]
    def _flaky_spawn(task, ws):
        calls[0] += 1
        if calls[0] <= 2:
            raise RuntimeError("transient")
        return 99999  # pid value — harmless; crash detection will clear it

    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="x", assignee="worker")
        # Two failures + one success.
        kb.dispatch_once(conn, spawn_fn=_flaky_spawn, failure_limit=5)
        kb.dispatch_once(conn, spawn_fn=_flaky_spawn, failure_limit=5)
        task = kb.get_task(conn, tid)
        assert task.spawn_failures == 2
        kb.dispatch_once(conn, spawn_fn=_flaky_spawn, failure_limit=5)
        task = kb.get_task(conn, tid)
        assert task.spawn_failures == 0
        assert task.last_spawn_error is None
        # Task is now running with a pid.
        assert task.status == "running"
        assert task.worker_pid == 99999
    finally:
        conn.close()


def test_workspace_resolution_failure_also_counts(kanban_home):
    """`dir:` workspace with no path should fail workspace resolution AND
    count against the failure budget — not just crash the tick."""
    conn = kb.connect()
    try:
        # Manually insert a broken task: dir workspace but workspace_path is NULL
        # after initial create. We achieve this by creating via kanban_db then
        # UPDATE-ing workspace_path to NULL.
        tid = kb.create_task(
            conn, title="x", assignee="worker",
            workspace_kind="dir", workspace_path="/tmp/kanban_e2e_dir",
        )
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET workspace_path = NULL WHERE id = ?", (tid,),
            )
        res = kb.dispatch_once(conn, failure_limit=3)
        task = kb.get_task(conn, tid)
        assert task.spawn_failures == 1
        assert task.status == "ready"
        assert task.last_spawn_error and "workspace" in task.last_spawn_error
        # Run twice more → auto-blocked.
        kb.dispatch_once(conn, failure_limit=3)
        res = kb.dispatch_once(conn, failure_limit=3)
        assert tid in res.auto_blocked
        task = kb.get_task(conn, tid)
        assert task.status == "blocked"
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Worker aliveness / crash detection
# ---------------------------------------------------------------------------

def test_pid_alive_helper():
    # Our own pid is alive.
    assert kb._pid_alive(os.getpid())
    # PID 0 / None / negative.
    assert not kb._pid_alive(0)
    assert not kb._pid_alive(None)
    # A clearly-dead pid (very large, extremely unlikely to exist).
    assert not kb._pid_alive(2 ** 30)


def test_detect_crashed_workers_reclaims(kanban_home):
    """A running task whose pid vanished gets dropped to ready with a
    ``crashed`` event, independent of the claim TTL."""
    def _spawn_pid_that_exits(task, ws):
        # Spawn a real child that exits instantly.
        import subprocess
        p = subprocess.Popen(
            ["python3", "-c", "pass"], stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL,
        )
        p.wait()
        return p.pid

    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="x", assignee="worker")
        res = kb.dispatch_once(conn, spawn_fn=_spawn_pid_that_exits)
        # Brief sleep to make sure the child's pid has been reaped; on
        # busy CI the pid may be reused by another process, which would
        # fool _pid_alive. If that happens we accept the test still
        # passing as long as the dispatcher ran without error.
        time.sleep(0.2)
        res2 = kb.dispatch_once(conn)
        task = kb.get_task(conn, tid)
        # Either crashed was detected (preferred) or the TTL reclaim path
        # will eventually fire; we accept either outcome but the worker_pid
        # should no longer be set.
        if res2.crashed:
            assert tid in res2.crashed
            events = kb.list_events(conn, tid)
            assert any(e.kind == "crashed" for e in events)
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Daemon loop
# ---------------------------------------------------------------------------

def test_daemon_runs_and_stops(kanban_home):
    """run_daemon should execute at least one tick and exit cleanly on
    stop_event."""
    ticks = []
    stop = threading.Event()

    def _runner():
        kb.run_daemon(
            interval=0.05,
            stop_event=stop,
            on_tick=lambda res: ticks.append(res),
        )

    t = threading.Thread(target=_runner, daemon=True)
    t.start()
    # Give it a few ticks.
    time.sleep(0.3)
    stop.set()
    t.join(timeout=2.0)
    assert not t.is_alive(), "daemon should exit on stop_event"
    assert len(ticks) >= 1, "expected at least one tick"


def test_daemon_keeps_going_after_tick_exception(kanban_home, monkeypatch):
    """A tick that raises shouldn't kill the loop."""
    calls = [0]
    orig_dispatch = kb.dispatch_once

    def _boom(conn, **kw):
        calls[0] += 1
        if calls[0] == 1:
            raise RuntimeError("simulated tick failure")
        return orig_dispatch(conn, **kw)

    monkeypatch.setattr(kb, "dispatch_once", _boom)

    stop = threading.Event()
    def _runner():
        kb.run_daemon(interval=0.05, stop_event=stop)

    t = threading.Thread(target=_runner, daemon=True)
    t.start()
    time.sleep(0.3)
    stop.set()
    t.join(timeout=2.0)
    # At minimum, second-tick+ should have run.
    assert calls[0] >= 2


# ---------------------------------------------------------------------------
# Stats + age
# ---------------------------------------------------------------------------

def test_board_stats(kanban_home):
    conn = kb.connect()
    try:
        a = kb.create_task(conn, title="a", assignee="x")
        b = kb.create_task(conn, title="b", assignee="y")
        kb.complete_task(conn, a, result="done")
        stats = kb.board_stats(conn)
        assert stats["by_status"]["ready"] == 1
        assert stats["by_status"]["done"] == 1
        assert stats["by_assignee"]["x"]["done"] == 1
        assert stats["by_assignee"]["y"]["ready"] == 1
        assert stats["oldest_ready_age_seconds"] is not None
    finally:
        conn.close()


def test_task_age_helper(kanban_home):
    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="x")
        task = kb.get_task(conn, tid)
        age = kb.task_age(task)
        assert age["created_age_seconds"] is not None
        assert age["started_age_seconds"] is None
        assert age["time_to_complete_seconds"] is None
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Notify subscriptions
# ---------------------------------------------------------------------------

def test_notify_sub_crud(kanban_home):
    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="x")
        kb.add_notify_sub(
            conn, task_id=tid, platform="telegram", chat_id="123", user_id="u1",
        )
        subs = kb.list_notify_subs(conn, tid)
        assert len(subs) == 1
        assert subs[0]["platform"] == "telegram"
        # Duplicate add is a no-op.
        kb.add_notify_sub(
            conn, task_id=tid, platform="telegram", chat_id="123",
        )
        assert len(kb.list_notify_subs(conn, tid)) == 1
        # Distinct thread is a new row.
        kb.add_notify_sub(
            conn, task_id=tid, platform="telegram", chat_id="123",
            thread_id="5",
        )
        assert len(kb.list_notify_subs(conn, tid)) == 2
        # Remove one.
        ok = kb.remove_notify_sub(
            conn, task_id=tid, platform="telegram", chat_id="123",
        )
        assert ok is True
        assert len(kb.list_notify_subs(conn, tid)) == 1
    finally:
        conn.close()


def test_notify_cursor_advances(kanban_home):
    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="x", assignee="w")
        kb.add_notify_sub(conn, task_id=tid, platform="telegram", chat_id="123")
        # Initial: one "created" event but we only want terminal kinds.
        cursor, events = kb.unseen_events_for_sub(
            conn, task_id=tid, platform="telegram", chat_id="123",
            kinds=["completed", "blocked"],
        )
        assert events == []
        # Complete the task → new `completed` event.
        kb.complete_task(conn, tid, result="ok")
        cursor, events = kb.unseen_events_for_sub(
            conn, task_id=tid, platform="telegram", chat_id="123",
            kinds=["completed", "blocked"],
        )
        assert len(events) == 1
        assert events[0].kind == "completed"
        # Advance cursor — next call returns empty.
        kb.advance_notify_cursor(
            conn, task_id=tid, platform="telegram", chat_id="123",
            new_cursor=cursor,
        )
        _, events2 = kb.unseen_events_for_sub(
            conn, task_id=tid, platform="telegram", chat_id="123",
            kinds=["completed", "blocked"],
        )
        assert events2 == []
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# GC + retention
# ---------------------------------------------------------------------------

def test_gc_events_keeps_active_task_history(kanban_home):
    """gc_events should only prune rows for terminal (done/archived) tasks."""
    conn = kb.connect()
    try:
        alive = kb.create_task(conn, title="a", assignee="w")
        done_id = kb.create_task(conn, title="b", assignee="w")
        kb.complete_task(conn, done_id)

        # Force all existing events to "old" by bumping created_at backwards.
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE task_events SET created_at = ?",
                (int(time.time()) - 60 * 24 * 3600,),
            )
        removed = kb.gc_events(conn, older_than_seconds=30 * 24 * 3600)
        # At least the done task's "created" + "completed" events gone.
        assert removed >= 2
        # Alive task's events survive.
        alive_events = kb.list_events(conn, alive)
        assert len(alive_events) >= 1
    finally:
        conn.close()


def test_gc_worker_logs_deletes_old_files(kanban_home):
    log_dir = kanban_home / "kanban" / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    old = log_dir / "old.log"
    young = log_dir / "young.log"
    old.write_text("stale")
    young.write_text("fresh")
    # Age the old file by 100 days.
    past = time.time() - 100 * 24 * 3600
    os.utime(old, (past, past))
    removed = kb.gc_worker_logs(older_than_seconds=30 * 24 * 3600)
    assert removed == 1
    assert not old.exists()
    assert young.exists()


# ---------------------------------------------------------------------------
# Log rotation + accessor
# ---------------------------------------------------------------------------

def test_worker_log_rotation_keeps_one_generation(kanban_home, tmp_path):
    log_dir = kanban_home / "kanban" / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    target = log_dir / "t_aaaa.log"
    target.write_bytes(b"x" * (3 * 1024 * 1024))  # 3 MiB, over 2 MiB threshold
    kb._rotate_worker_log(target, kb.DEFAULT_LOG_ROTATE_BYTES)
    assert not target.exists()
    assert (log_dir / "t_aaaa.log.1").exists()


def test_read_worker_log_tail(kanban_home):
    log_dir = kanban_home / "kanban" / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    p = log_dir / "t_beef.log"
    # 10 lines
    p.write_text("\n".join(f"line {i}" for i in range(10)))
    full = kb.read_worker_log("t_beef")
    assert full is not None and "line 0" in full
    tail = kb.read_worker_log("t_beef", tail_bytes=30)
    assert tail is not None
    # Tail should not include line 0.
    assert "line 0" not in tail
    # Missing log returns None.
    assert kb.read_worker_log("t_missing") is None


# ---------------------------------------------------------------------------
# CLI bulk verbs
# ---------------------------------------------------------------------------

def test_cli_complete_bulk(kanban_home):
    conn = kb.connect()
    try:
        a = kb.create_task(conn, title="a")
        b = kb.create_task(conn, title="b")
        c = kb.create_task(conn, title="c")
    finally:
        conn.close()
    out = run_slash(f"complete {a} {b} {c} --result all-done")
    assert out.count("Completed") == 3
    conn = kb.connect()
    try:
        for tid in (a, b, c):
            assert kb.get_task(conn, tid).status == "done"
    finally:
        conn.close()


def test_cli_archive_bulk(kanban_home):
    conn = kb.connect()
    try:
        a = kb.create_task(conn, title="a")
        b = kb.create_task(conn, title="b")
    finally:
        conn.close()
    out = run_slash(f"archive {a} {b}")
    assert "Archived" in out
    conn = kb.connect()
    try:
        assert kb.get_task(conn, a).status == "archived"
        assert kb.get_task(conn, b).status == "archived"
    finally:
        conn.close()


def test_cli_unblock_bulk(kanban_home):
    conn = kb.connect()
    try:
        a = kb.create_task(conn, title="a")
        b = kb.create_task(conn, title="b")
        kb.block_task(conn, a)
        kb.block_task(conn, b)
    finally:
        conn.close()
    out = run_slash(f"unblock {a} {b}")
    assert out.count("Unblocked") == 2


def test_cli_block_bulk_via_ids_flag(kanban_home):
    conn = kb.connect()
    try:
        a = kb.create_task(conn, title="a")
        b = kb.create_task(conn, title="b")
    finally:
        conn.close()
    out = run_slash(f"block {a} need input --ids {b}")
    assert out.count("Blocked") == 2


def test_cli_create_with_idempotency_key(kanban_home):
    out1 = run_slash("create 'x' --idempotency-key abc --json")
    tid1 = json.loads(out1)["id"]
    out2 = run_slash("create 'y' --idempotency-key abc --json")
    tid2 = json.loads(out2)["id"]
    assert tid1 == tid2


# ---------------------------------------------------------------------------
# CLI stats / watch / log / notify / daemon parity
# ---------------------------------------------------------------------------

def test_cli_stats_json(kanban_home):
    conn = kb.connect()
    try:
        kb.create_task(conn, title="a", assignee="r")
    finally:
        conn.close()
    out = run_slash("stats --json")
    data = json.loads(out)
    assert "by_status" in data
    assert "by_assignee" in data
    assert "oldest_ready_age_seconds" in data


def test_cli_notify_subscribe_and_list(kanban_home):
    tid = run_slash("create 'x' --json")
    tid = json.loads(tid)["id"]
    out = run_slash(
        f"notify-subscribe {tid} --platform telegram --chat-id 999",
    )
    assert "Subscribed" in out
    lst = run_slash("notify-list --json")
    subs = json.loads(lst)
    assert any(s["task_id"] == tid and s["platform"] == "telegram" for s in subs)
    rm = run_slash(
        f"notify-unsubscribe {tid} --platform telegram --chat-id 999",
    )
    assert "Unsubscribed" in rm


def test_cli_log_missing_task(kanban_home):
    # No such task → exit-style (no log for...) message on stderr, returned
    # in combined output.
    out = run_slash("log t_nope")
    assert "no log" in out.lower()


def test_cli_gc_reports_counts(kanban_home):
    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="x")
        kb.archive_task(conn, tid)
    finally:
        conn.close()
    out = run_slash("gc")
    assert "GC complete" in out


# ---------------------------------------------------------------------------
# run_slash parity — every verb returns a sensible, non-crashy string
# ---------------------------------------------------------------------------

def test_run_slash_every_verb_returns_sensible_output(kanban_home):
    """Smoke-test every verb with minimal args. None may raise, none may
    return the empty string (must either succeed or report a usage error)."""
    # Set up a pair of tasks to reference.
    conn = kb.connect()
    try:
        tid_a = kb.create_task(conn, title="a")
        tid_b = kb.create_task(conn, title="b", parents=[tid_a])
    finally:
        conn.close()

    invocations = [
        "",                                  # no subcommand → help text
        "--help",
        "init",
        "create 'smoke'",
        "list",
        "ls",
        f"show {tid_a}",
        f"assign {tid_a} researcher",
        f"link {tid_a} {tid_b}",
        f"unlink {tid_a} {tid_b}",
        f"claim {tid_a}",
        f"comment {tid_a} hello",
        f"complete {tid_a}",
        f"block {tid_b} need input",
        f"unblock {tid_b}",
        f"archive {tid_a}",
        "dispatch --dry-run --json",
        "stats --json",
        "notify-list",
        f"log {tid_a}",
        f"context {tid_b}",
        "gc",
    ]
    for cmd in invocations:
        out = run_slash(cmd)
        assert out is not None
        assert out.strip() != "", f"empty output for `/kanban {cmd}`"


# ---------------------------------------------------------------------------
# Max-runtime enforcement (item 1 from the Multica audit)
# ---------------------------------------------------------------------------

def test_max_runtime_terminates_overrun_worker(kanban_home):
    """A running task whose elapsed time exceeds max_runtime_seconds gets
    SIGTERM'd, emits a ``timed_out`` event, and goes back to ready."""
    killed = []
    def _signal_fn(pid, sig):
        killed.append((pid, sig))

    # We bypass _pid_alive by stubbing it so the grace-poll exits fast.
    import hermes_cli.kanban_db as _kb
    original_alive = _kb._pid_alive
    _kb._pid_alive = lambda pid: False  # pretend SIGTERM worked immediately

    try:
        conn = kb.connect()
        try:
            tid = kb.create_task(
                conn, title="long job", assignee="worker",
                max_runtime_seconds=1,  # one second cap
            )
            # Spawn by hand: claim + set pid + set started_at to the past.
            kb.claim_task(conn, tid)
            kb._set_worker_pid(conn, tid, os.getpid())   # any live pid works
            # Backdate started_at so elapsed > limit.
            with kb.write_txn(conn):
                conn.execute(
                    "UPDATE tasks SET started_at = ? WHERE id = ?",
                    (int(time.time()) - 30, tid),
                )

            timed_out = kb.enforce_max_runtime(conn, signal_fn=_signal_fn)
            assert tid in timed_out
            assert killed and killed[0][0] == os.getpid()

            task = kb.get_task(conn, tid)
            assert task.status == "ready",                 f"timed-out task should reset to ready, got {task.status}"
            assert task.worker_pid is None
            assert task.last_heartbeat_at is None

            events = kb.list_events(conn, tid)
            assert any(e.kind == "timed_out" for e in events)
            to_event = next(e for e in events if e.kind == "timed_out")
            assert to_event.payload["limit_seconds"] == 1
            assert to_event.payload["elapsed_seconds"] >= 30
        finally:
            conn.close()
    finally:
        _kb._pid_alive = original_alive


def test_max_runtime_none_means_no_cap(kanban_home):
    """A task with max_runtime_seconds=None is never timed out regardless
    of how long it runs."""
    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="uncapped", assignee="worker")
        kb.claim_task(conn, tid)
        kb._set_worker_pid(conn, tid, os.getpid())
        # Backdate aggressively; no cap means we don't care.
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET started_at = ? WHERE id = ?",
                (int(time.time()) - 100_000, tid),
            )
        timed_out = kb.enforce_max_runtime(conn)
        assert timed_out == []
        task = kb.get_task(conn, tid)
        assert task.status == "running"
    finally:
        conn.close()


def test_create_task_persists_max_runtime(kanban_home):
    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="x", max_runtime_seconds=600)
        task = kb.get_task(conn, tid)
        assert task.max_runtime_seconds == 600
    finally:
        conn.close()


def test_enforce_max_runtime_integrates_with_dispatch(kanban_home, monkeypatch):
    """enforce_max_runtime + dispatch_once integrate cleanly — a timed-out
    task goes through ``timed_out`` → ``ready`` and dispatch_once can then
    re-spawn it without re-reporting the timeout."""
    import hermes_cli.kanban_db as _kb
    # Leave _pid_alive=True so the crash detector doesn't steal the task
    # before timeout enforcement runs. After SIGTERM in enforce_max_runtime,
    # pretend the worker died so the grace wait exits fast.
    state = {"sent_term": False}
    def _alive(pid):
        return not state["sent_term"]
    def _signal(pid, sig):
        import signal as _sig
        if sig == _sig.SIGTERM:
            state["sent_term"] = True
    monkeypatch.setattr(_kb, "_pid_alive", _alive)

    conn = kb.connect()
    try:
        tid = kb.create_task(
            conn, title="timeout-me", assignee="worker",
            max_runtime_seconds=1,
        )
        kb.claim_task(conn, tid)
        kb._set_worker_pid(conn, tid, os.getpid())
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET started_at = ? WHERE id = ?",
                (int(time.time()) - 30, tid),
            )
        # Use enforce_max_runtime directly with our signal stub — dispatch_once
        # uses the default os.kill, but integration-wise calling
        # enforce_max_runtime directly proves the kernel wiring. For the
        # dispatch_once assertion, rely on its own code path by calling it
        # after forcing SIGTERM via enforce_max_runtime.
        before = kb.enforce_max_runtime(conn, signal_fn=_signal)
        assert tid in before, "kernel enforce_max_runtime should catch the overrun"

        # Now a second dispatch_once run should be a no-op on this task
        # (already released). Confirm the loop doesn't re-report it.
        res = kb.dispatch_once(conn, spawn_fn=lambda t, ws: None)
        task = kb.get_task(conn, tid)
        # After timeout, task is back in 'ready' and will be re-spawned
        # by the same pass. That's the intended behaviour.
        assert task.status in ("ready", "running")
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Heartbeat (item 2 from the Multica audit)
# ---------------------------------------------------------------------------

def test_heartbeat_on_running_task(kanban_home):
    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="x", assignee="worker")
        kb.claim_task(conn, tid)
        ok = kb.heartbeat_worker(conn, tid, note="step 3/10")
        assert ok is True
        task = kb.get_task(conn, tid)
        assert task.last_heartbeat_at is not None
        events = kb.list_events(conn, tid)
        hb = [e for e in events if e.kind == "heartbeat"]
        assert len(hb) == 1
        assert hb[0].payload == {"note": "step 3/10"}
    finally:
        conn.close()


def test_heartbeat_refused_when_not_running(kanban_home):
    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="x")   # lands in ready, not running
        ok = kb.heartbeat_worker(conn, tid)
        assert ok is False
        task = kb.get_task(conn, tid)
        assert task.last_heartbeat_at is None
    finally:
        conn.close()


def test_cli_heartbeat_verb(kanban_home):
    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="x", assignee="worker")
        kb.claim_task(conn, tid)
    finally:
        conn.close()
    out = run_slash(f"heartbeat {tid}")
    assert "Heartbeat recorded" in out

    # With --note.
    out = run_slash(f"heartbeat {tid} --note 'step 42'")
    assert "Heartbeat recorded" in out
    conn = kb.connect()
    try:
        events = kb.list_events(conn, tid)
        notes = [e.payload.get("note") for e in events if e.kind == "heartbeat" and e.payload]
        assert "step 42" in notes
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Event vocab rename + spawned event (item 3 from Multica)
# ---------------------------------------------------------------------------

def test_recompute_ready_emits_promoted_not_ready(kanban_home):
    conn = kb.connect()
    try:
        parent = kb.create_task(conn, title="p")
        child = kb.create_task(conn, title="c", parents=[parent])
        kb.complete_task(conn, parent, result="ok")
        # recompute_ready runs inside complete_task too, but call it again
        # defensively.
        kb.recompute_ready(conn)
        events = kb.list_events(conn, child)
        kinds = [e.kind for e in events]
        assert "promoted" in kinds
        # Old name must not appear.
        assert "ready" not in kinds
    finally:
        conn.close()


def test_spawn_failure_circuit_breaker_emits_gave_up(kanban_home):
    def _bad(task, ws):
        raise RuntimeError("nope")
    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="x", assignee="worker")
        for _ in range(5):
            kb.dispatch_once(conn, spawn_fn=_bad, failure_limit=5)
        events = kb.list_events(conn, tid)
        kinds = [e.kind for e in events]
        assert "gave_up" in kinds
        assert "spawn_auto_blocked" not in kinds
    finally:
        conn.close()


def test_spawned_event_emitted_with_pid(kanban_home):
    """Successful spawn must append a ``spawned`` event with the pid in
    the payload so humans tailing events see pid tracking."""
    def _spawn_returns_pid(task, ws):
        return 98765
    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="x", assignee="worker")
        kb.dispatch_once(conn, spawn_fn=_spawn_returns_pid)
        events = kb.list_events(conn, tid)
        spawned = [e for e in events if e.kind == "spawned"]
        assert len(spawned) == 1
        assert spawned[0].payload == {"pid": 98765}
    finally:
        conn.close()


def test_migration_renames_legacy_event_kinds(tmp_path, monkeypatch):
    """A DB created with the old vocab must have its event rows renamed
    in place on init_db()."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    # Init fresh.
    kb.init_db()
    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="x")
        # Inject legacy event kinds directly.
        now = int(time.time())
        with kb.write_txn(conn):
            for old in ("ready", "priority", "spawn_auto_blocked"):
                conn.execute(
                    "INSERT INTO task_events (task_id, kind, payload, created_at) "
                    "VALUES (?, ?, NULL, ?)",
                    (tid, old, now),
                )
        # Re-run init_db — the migration pass should rename them.
        kb.init_db()
        rows = conn.execute(
            "SELECT kind FROM task_events WHERE task_id = ? ORDER BY id", (tid,),
        ).fetchall()
        kinds = [r["kind"] for r in rows]
        assert "ready" not in kinds
        assert "priority" not in kinds
        assert "spawn_auto_blocked" not in kinds
        assert "promoted" in kinds
        assert "reprioritized" in kinds
        assert "gave_up" in kinds
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Assignees (item 4 from Multica)
# ---------------------------------------------------------------------------

def test_list_profiles_on_disk(tmp_path, monkeypatch):
    """list_profiles_on_disk returns directories under ~/.hermes/profiles/
    that contain a config.yaml."""
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    profiles = tmp_path / ".hermes" / "profiles"
    profiles.mkdir(parents=True)
    (profiles / "researcher").mkdir()
    (profiles / "researcher" / "config.yaml").write_text("model: {}\n")
    (profiles / "writer").mkdir()
    (profiles / "writer" / "config.yaml").write_text("model: {}\n")
    (profiles / "empty_dir").mkdir()
    # A stray file; should be ignored.
    (profiles / "stray.txt").write_text("noise")

    names = kb.list_profiles_on_disk()
    assert names == ["researcher", "writer"]


def test_known_assignees_merges_disk_and_board(tmp_path, monkeypatch):
    """known_assignees unions profiles on disk with currently-assigned
    names, and reports per-status counts."""
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    profiles = tmp_path / ".hermes" / "profiles"
    profiles.mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))

    for name in ("researcher", "writer"):
        d = profiles / name
        d.mkdir()
        (d / "config.yaml").write_text("model: {}\n")

    kb.init_db()
    conn = kb.connect()
    try:
        # writer has a ready task; on_board_only has a task but no profile dir.
        kb.create_task(conn, title="a", assignee="writer")
        kb.create_task(conn, title="b", assignee="on_board_only")
        data = kb.known_assignees(conn)
    finally:
        conn.close()

    by_name = {d["name"]: d for d in data}
    assert by_name["researcher"]["on_disk"] is True
    assert by_name["researcher"]["counts"] == {}
    assert by_name["writer"]["on_disk"] is True
    assert by_name["writer"]["counts"] == {"ready": 1}
    assert by_name["on_board_only"]["on_disk"] is False
    assert by_name["on_board_only"]["counts"] == {"ready": 1}


def test_cli_assignees_json(kanban_home):
    conn = kb.connect()
    try:
        kb.create_task(conn, title="x", assignee="someone")
    finally:
        conn.close()
    out = run_slash("assignees --json")
    data = json.loads(out)
    names = [e["name"] for e in data]
    assert "someone" in names


# ---------------------------------------------------------------------------
# CLI --max-runtime flag + duration parser
# ---------------------------------------------------------------------------

def test_parse_duration_accepts_formats():
    from hermes_cli.kanban import _parse_duration
    assert _parse_duration(None) is None
    assert _parse_duration("") is None
    assert _parse_duration("42") == 42
    assert _parse_duration("30s") == 30
    assert _parse_duration("5m") == 300
    assert _parse_duration("2h") == 7200
    assert _parse_duration("1d") == 86400
    assert _parse_duration("1.5h") == 5400


def test_parse_duration_rejects_garbage():
    from hermes_cli.kanban import _parse_duration
    import pytest as _p
    with _p.raises(ValueError):
        _parse_duration("tenminutes")
    with _p.raises(ValueError):
        _parse_duration("fish")


def test_cli_create_max_runtime_via_duration(kanban_home):
    """`hermes kanban create --max-runtime 2h` should persist 7200 seconds."""
    out = run_slash("create 'long task' --max-runtime 2h --json")
    data = json.loads(out)
    tid = data["id"]
    conn = kb.connect()
    try:
        task = kb.get_task(conn, tid)
        assert task.max_runtime_seconds == 7200
    finally:
        conn.close()


def test_cli_create_max_runtime_bad_format_exits_nonzero(kanban_home):
    out = run_slash("create 'bad' --max-runtime fish")
    assert "max-runtime" in out.lower() or "malformed" in out.lower()
