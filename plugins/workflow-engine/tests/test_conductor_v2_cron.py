"""Conductor v2 B6: native cron schedules ("Repeat")."""
from __future__ import annotations

import asyncio
import time
from datetime import datetime, timedelta, timezone

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("croniter")

from fastapi import FastAPI
from fastapi.testclient import TestClient

import engine.cron.schedule as cron
from engine.wiring import create_engine


def _arun(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _utc(*a):
    return datetime(*a, tzinfo=timezone.utc)


@pytest.fixture()
def new_york(monkeypatch):
    monkeypatch.setenv("TZ", "America/New_York")
    time.tzset()
    yield
    monkeypatch.undo()
    time.tzset()


def _chain(expr, start, n):
    t, out = start.timestamp(), []
    for _ in range(n):
        t = cron.next_fire(expr, t)
        out.append(cron.to_iso(t)[5:16])
    return out


# ── next-fire computation (host-local TZ, DST) ───────────────────────────────


def test_next_fire_keeps_local_wall_time_across_dst(new_york):
    assert cron.local_tz_name() == "America/New_York"
    # 09:00 local: 14:00Z in EST, 13:00Z once EDT starts (2026-03-08)
    assert _chain("0 9 * * *", _utc(2026, 3, 6, 20), 3) == ["03-07T14:00", "03-08T13:00", "03-09T13:00"]
    # 02:30 doesn't exist on spring-forward day: normalised forward, once
    assert _chain("30 2 * * *", _utc(2026, 3, 7, 12), 2) == ["03-08T07:30", "03-09T06:30"]
    # 01:30 happens twice on fall-back day: fires once
    assert _chain("30 1 * * *", _utc(2026, 10, 31, 12), 2) == ["11-01T05:30", "11-02T06:30"]


def test_next_fire_always_moves_forward(new_york):
    # inside the repeated 01:xx hour (second pass, EST): never a past instant
    start = _utc(2026, 11, 1, 6, 10)
    for expr in ("*/5 * * * *", "*/30 * * * *", "30 1 * * *", "0 * * * *", "@hourly"):
        assert cron.next_fire(expr, start.timestamp()) > start.timestamp()
    t = start.timestamp()
    for _ in range(50):
        nt = cron.next_fire("*/5 * * * *", t)
        assert 0 < nt - t <= 3600
        t = nt


@pytest.mark.parametrize("bad", ["nope", "* * * * * *", "61 * * * *", "@reboot", "", "1 " * 70, 5])
def test_validate_rejects(bad):
    with pytest.raises(ValueError):
        cron.validate_cron(bad)


# ── engine / API ─────────────────────────────────────────────────────────────

_YAML = """\
name: tick
description: d
nodes:
  - id: a
    bash: "echo hi"
"""


@pytest.fixture()
def eng():
    e = create_engine(db_path=":memory:", seed_bundled=False, write_manifest=False, crash_recovery=False)
    _arun(e.upsert_definition("tick", _YAML))
    yield e
    _arun(e.shutdown())


@pytest.fixture()
def client(eng):
    import plugins.workflow_engine.dashboard.plugin_api as api_mod
    original = api_mod._engine
    api_mod._engine = lambda: eng
    app = FastAPI()
    app.include_router(api_mod.router)
    with TestClient(app) as c:
        yield c
    api_mod._engine = original


def _create(client, expr="*/5 * * * *", **extra):
    return client.post("/runs", json={
        "workflow_id": "tick", "conversation_id": "c1", "user_message": "go",
        "schedule": {"type": "cron", "cron": expr}, **extra,
    })


def _row(eng, sid):
    return eng._run_store.get_scheduled_run(sid)


def test_create_cron_schedule(client, eng):
    r = _create(client)
    assert r.status_code == 201
    run = r.json()["run"]
    assert run["status"] == "scheduled" and run["cron"] == "*/5 * * * *"
    nxt = datetime.fromisoformat(run["next_run_at"])
    assert nxt > datetime.now(tz=timezone.utc) and nxt.minute % 5 == 0 and nxt.second == 0
    sched = client.get("/schedules?workflow_id=tick").json()["schedules"]
    assert [s["id"] for s in sched] == [run["id"]]
    assert sched[0]["kind"] == "cron" and sched[0]["enabled"] and sched[0]["tz"]
    assert client.get("/schedules?workflow_id=other").json() == {"schedules": []}


@pytest.mark.parametrize("expr", ["nope", "* * * * * *", "x" * 129, 7])
def test_invalid_cron_400(client, expr):
    r = _create(client, expr)
    assert r.status_code == 400


def test_missing_croniter_501(client, monkeypatch):
    def boom():
        raise ImportError("no croniter")
    monkeypatch.setattr(cron, "_croniter", boom)
    assert _create(client).status_code == 501


def test_fire_advances_by_interval_with_cron_trigger(client, eng):
    sid = _create(client).json()["run"]["id"]
    due = _row(eng, sid)["next_run_at"]
    assert _arun(eng.fire_due_scheduled_runs(now_iso=due)) == 1
    row = _row(eng, sid)
    assert row["status"] == "pending" and row["last_error"] is None
    assert datetime.fromisoformat(row["next_run_at"]) - datetime.fromisoformat(due) == timedelta(minutes=5)
    assert _arun(eng.get_run(row["last_run_id"]))["workflow_id"] == "tick"
    # not due again until the next occurrence
    assert _arun(eng.fire_due_scheduled_runs(now_iso=due)) == 0


def test_fire_trigger_shape(client, eng, monkeypatch):
    sid = _create(client).json()["run"]["id"]
    seen = {}

    async def fake_start(wid, inputs, trigger, **kw):
        seen.update(trigger)
        return {"id": "r-1"}

    monkeypatch.setattr(eng, "start_run", fake_start)
    _arun(eng.fire_due_scheduled_runs(now_iso=_row(eng, sid)["next_run_at"]))
    assert seen["kind"] == "cron" and seen["schedule_id"] == sid and seen["cron_expr"] == "*/5 * * * *"
    assert seen["conversation_id"] == "c1" and "claimed_at" not in seen and "last_error" not in seen
    assert _row(eng, sid)["last_run_id"] == "r-1"


def test_failing_start_still_advances_and_records_error(client, eng, monkeypatch):
    sid = _create(client).json()["run"]["id"]

    async def broken(*a, **kw):
        raise RuntimeError("definition exploded")

    monkeypatch.setattr(eng, "start_run", broken)
    due = _row(eng, sid)["next_run_at"]
    for i in range(3):  # never permanently failed
        now = (datetime.fromisoformat(due) + timedelta(minutes=5 * i)).isoformat()
        assert _arun(eng.fire_due_scheduled_runs(now_iso=now)) == 0
        row = _row(eng, sid)
        assert row["status"] == "pending"
        assert "definition exploded" in row["last_error"]
        assert row["next_run_at"] > now
    monkeypatch.undo()
    now = (datetime.fromisoformat(due) + timedelta(minutes=15)).isoformat()
    assert _arun(eng.fire_due_scheduled_runs(now_iso=now)) == 1
    assert _row(eng, sid)["last_error"] is None


def test_no_backfill_after_downtime(client, eng):
    sid = _create(client).json()["run"]["id"]
    due = datetime.fromisoformat(_row(eng, sid)["next_run_at"])
    later = due + timedelta(hours=3, minutes=2)
    assert _arun(eng.fire_due_scheduled_runs(now_iso=later.isoformat())) == 1
    nxt = datetime.fromisoformat(_row(eng, sid)["next_run_at"])
    assert later < nxt <= later + timedelta(minutes=5)


def test_stale_firing_row_recovers(client, eng):
    sid = _create(client).json()["run"]["id"]
    due = datetime.fromisoformat(_row(eng, sid)["next_run_at"])
    assert eng._run_store.claim_scheduled_run(sid, due.isoformat())  # tick died here
    # 5s later (< 2 ticks): still owned, untouched
    soon = (due + timedelta(seconds=5)).isoformat()
    assert _arun(eng.fire_due_scheduled_runs(now_iso=soon, stale_firing_s=20)) == 0
    assert _row(eng, sid)["status"] == "firing"
    # 30s later: reset to pending, then fired and rescheduled
    late = (due + timedelta(seconds=30)).isoformat()
    assert _arun(eng.fire_due_scheduled_runs(now_iso=late, stale_firing_s=20)) == 1
    assert _row(eng, sid)["status"] == "pending"


def test_disable_enable_delete(client, eng):
    sid = _create(client).json()["run"]["id"]
    due = _row(eng, sid)["next_run_at"]
    r = client.patch(f"/schedules/{sid}", json={"enabled": False})
    assert r.status_code == 200 and r.json()["schedule"]["enabled"] is False
    far = (datetime.fromisoformat(due) + timedelta(days=1)).isoformat()
    assert _arun(eng.fire_due_scheduled_runs(now_iso=far)) == 0  # disabled never fires
    assert _row(eng, sid)["status"] == "disabled"

    r = client.patch(f"/schedules/{sid}", json={"enabled": True})
    s = r.json()["schedule"]
    assert s["enabled"] and s["status"] == "pending"
    assert s["next_run_at"] > datetime.now(tz=timezone.utc).isoformat()  # recomputed from now

    assert client.patch(f"/schedules/{sid}", json={"enabled": "yes"}).status_code == 400
    assert client.delete(f"/schedules/{sid}").json()["status"] == "cancelled"
    assert client.get("/schedules").json()["schedules"] == []
    assert _arun(eng.fire_due_scheduled_runs(now_iso=far)) == 0
    assert client.delete(f"/schedules/{sid}").status_code == 404
    assert client.patch(f"/schedules/{sid}", json={"enabled": True}).status_code == 404


def test_at_rows_keep_one_shot_semantics(client, eng, monkeypatch):
    past = (datetime.now(tz=timezone.utc) - timedelta(seconds=5)).isoformat()
    ok = client.post("/runs", json={"workflow_id": "tick", "conversation_id": "c", "user_message": "m",
                                    "schedule": {"type": "at", "at": past}}).json()["run"]["id"]
    assert _arun(eng.fire_due_scheduled_runs()) == 1
    assert eng._run_store.get_scheduled_run(ok)["status"] == "fired"

    bad = client.post("/runs", json={"workflow_id": "tick", "conversation_id": "c", "user_message": "m",
                                     "schedule": {"type": "at", "at": past}}).json()["run"]["id"]

    async def broken(*a, **kw):
        raise RuntimeError("x")

    monkeypatch.setattr(eng, "start_run", broken)
    _arun(eng.fire_due_scheduled_runs())
    assert eng._run_store.get_scheduled_run(bad)["status"] == "failed"


def test_health_lists_b6_features(client):
    assert {"cron_schedule", "schedules_api"} <= set(client.get("/health").json()["features"])
