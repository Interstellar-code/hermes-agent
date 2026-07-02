"""Tests for the Phase 2 Kanban audit-mirror (``core/kanban_audit.py``).

These use a FAKE kanban backend injected via ``kanban_audit._kb = Fake()`` — no
real DB, no venv. They assert the audit-mirror invariants:

* ``open_card`` creates a ``running`` card with ``created_by="matrix_coder"`` and
  NO assignee (so the dispatcher never claims and re-runs it);
* ``close_card`` routes to ``complete_task`` (``done``) or ``block_task``
  (``blocked``) with ``expected_run_id`` left at its default (``None``);
* the disabled path (``_kb=None``) and any backend that raises are clean no-ops;
* the full lifecycle through ``harness.handle_trigger`` + the pre-LLM hook opens
  and closes a card via the shared ``bridge``.
"""

from __future__ import annotations

import sys
from pathlib import Path

# Same package-path shim as the other tests so the SHARED ``bridge`` instance is
# identical across the plugin package and these tests.
_PLUGIN_DIR = Path(__file__).resolve().parent.parent
if str(_PLUGIN_DIR.parent) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_DIR.parent))

import matrix_coder as plugin  # noqa: E402
from matrix_coder.core import harness, kanban_audit  # noqa: E402
from matrix_coder.core.hermes_bridge import bridge  # noqa: E402


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------

class _FakeConn:
    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


class FakeKb:
    """Records create/complete/block calls and hands back a fixed fake id."""

    def __init__(self, card_id: str = "card-123") -> None:
        self.card_id = card_id
        self.create_calls: list = []
        self.complete_calls: list = []
        self.block_calls: list = []
        self.conns: list = []

    def connect(self, *args, **kwargs):
        c = _FakeConn()
        self.conns.append(c)
        return c

    def create_task(self, conn, **kwargs):
        self.create_calls.append(kwargs)
        return self.card_id

    def complete_task(self, conn, task_id, **kwargs):
        self.complete_calls.append((task_id, kwargs))
        return True

    def block_task(self, conn, task_id, **kwargs):
        self.block_calls.append((task_id, kwargs))
        return True


class RaisingKb:
    """A backend whose every method raises — exercises the defensive guards."""

    def connect(self, *args, **kwargs):
        raise RuntimeError("kanban down")

    def create_task(self, *args, **kwargs):
        raise RuntimeError("kanban down")

    def complete_task(self, *args, **kwargs):
        raise RuntimeError("kanban down")

    def block_task(self, *args, **kwargs):
        raise RuntimeError("kanban down")


# ---------------------------------------------------------------------------
# Helpers: swap the module-level backend, restore after.
# ---------------------------------------------------------------------------

def _install(kb):
    prev = kanban_audit._kb
    kanban_audit._kb = kb
    return prev


# ---------------------------------------------------------------------------
# open_card
# ---------------------------------------------------------------------------

def test_open_card_creates_running_card_no_assignee():
    fake = FakeKb()
    prev = _install(fake)
    try:
        cid = kanban_audit.open_card("review", "security", "check auth", "s1")
        assert cid == "card-123"
        assert len(fake.create_calls) == 1
        kw = fake.create_calls[0]
        assert kw["initial_status"] == "running"
        assert kw["created_by"] == "matrix_coder"
        assert kw["tenant"] == "matrix_coder"
        assert kw["session_id"] == "s1"
        # NO assignee passed -> dispatcher (claims status='ready') never picks it.
        assert "assignee" not in kw
        assert kw["title"].startswith("matrix review/security:")
        assert kw["body"] == "check auth"
        assert kw.get("idempotency_key")  # uuid4 hex present
        assert fake.conns[0].closed is True
    finally:
        kanban_audit._kb = prev


def test_open_card_no_lens_title():
    fake = FakeKb()
    prev = _install(fake)
    try:
        kanban_audit.open_card("executor", None, "add export", None)
        assert fake.create_calls[0]["title"].startswith("matrix executor:")
    finally:
        kanban_audit._kb = prev


# ---------------------------------------------------------------------------
# close_card
# ---------------------------------------------------------------------------

def test_close_card_done_calls_complete_with_default_run_id():
    fake = FakeKb()
    prev = _install(fake)
    try:
        kanban_audit.close_card("card-123", "all good", status="done")
        assert len(fake.complete_calls) == 1
        task_id, kw = fake.complete_calls[0]
        assert task_id == "card-123"
        assert kw["summary"] == "all good"
        # expected_run_id is left at its default (None) -> never passed.
        assert "expected_run_id" not in kw
        assert not fake.block_calls
    finally:
        kanban_audit._kb = prev


def test_close_card_blocked_calls_block_task():
    fake = FakeKb()
    prev = _install(fake)
    try:
        kanban_audit.close_card("card-123", "stuck", status="blocked")
        assert len(fake.block_calls) == 1
        task_id, kw = fake.block_calls[0]
        assert task_id == "card-123"
        assert kw["reason"] == "stuck"
        assert "expected_run_id" not in kw
        assert not fake.complete_calls
    finally:
        kanban_audit._kb = prev


# ---------------------------------------------------------------------------
# disabled path
# ---------------------------------------------------------------------------

def test_disabled_backend_open_returns_none_close_noop():
    prev = _install(None)
    try:
        assert kanban_audit.is_enabled() is False
        assert kanban_audit.open_card("review", None, "x", "s1") is None
        # close must be a clean no-op (no exception) when disabled.
        kanban_audit.close_card("card-123", "x", status="done")
        kanban_audit.close_card(None, None)
    finally:
        kanban_audit._kb = prev


# ---------------------------------------------------------------------------
# defensive: backend raises
# ---------------------------------------------------------------------------

def test_raising_backend_is_swallowed():
    prev = _install(RaisingKb())
    try:
        assert kanban_audit.open_card("review", None, "x", "s1") is None
        # close swallows the exception entirely.
        kanban_audit.close_card("card-123", "x", status="done")
        kanban_audit.close_card("card-123", "x", status="blocked")
    finally:
        kanban_audit._kb = prev


# ---------------------------------------------------------------------------
# lifecycle integration through harness + hooks
# ---------------------------------------------------------------------------

def test_lifecycle_trigger_opens_card_then_noncontrigger_closes():
    fake = FakeKb(card_id="life-1")
    prev = _install(fake)
    bridge.clear_active_persona()
    bridge.clear_active_card()
    try:
        composed = harness.handle_trigger(
            "matrix review security: x", session_id="s1"
        )
        assert composed is not None
        assert bridge.active_card_id("s1") == "life-1"
        assert len(fake.create_calls) == 1

        # A non-trigger pre_llm_call closes the orphan card + clears bookkeeping.
        # KB-1: an abandoned dispatch (no completion signal) closes as BLOCKED,
        # not done. Same session_id ("s1") so the orphan is found in its slot.
        result = plugin._inject_persona(
            user_message="ordinary follow-up", session_id="s1"
        )
        assert result is None
        assert bridge.active_card_id("s1") is None
        assert len(fake.block_calls) == 1
        assert fake.block_calls[0][0] == "life-1"
        assert not fake.complete_calls
    finally:
        kanban_audit._kb = prev
        bridge.clear_active_persona("s1")
        bridge.clear_active_card("s1")


def test_lifecycle_post_llm_call_closes_with_response():
    fake = FakeKb(card_id="life-2")
    prev = _install(fake)
    bridge.clear_active_persona()
    bridge.clear_active_card()
    try:
        harness.handle_trigger("matrix executor: add export", session_id="s2")
        assert bridge.active_card_id("s2") == "life-2"

        plugin._clear_persona(assistant_response="done implementing", session_id="s2")
        assert bridge.active_card_id("s2") is None
        assert len(fake.complete_calls) == 1
        task_id, kw = fake.complete_calls[0]
        assert task_id == "life-2"
        assert kw["summary"] == "done implementing"
    finally:
        kanban_audit._kb = prev
        bridge.clear_active_persona("s2")
        bridge.clear_active_card("s2")


def test_lifecycle_stale_card_superseded_on_new_trigger():
    fake = FakeKb(card_id="new-card")
    prev = _install(fake)
    bridge.clear_active_persona()
    bridge.clear_active_card()
    try:
        # Simulate a stale card left from an interrupted prior turn, in the same
        # session ("s3") the new trigger arrives on.
        bridge.set_active_card("stale-card", "s3")
        harness.handle_trigger("matrix review: y", session_id="s3")
        # Stale card was completed (superseded) and a fresh one opened.
        assert any(c[0] == "stale-card" for c in fake.complete_calls)
        assert bridge.active_card_id("s3") == "new-card"
    finally:
        kanban_audit._kb = prev
        bridge.clear_active_persona("s3")
        bridge.clear_active_card("s3")


# ---------------------------------------------------------------------------
# signature-drift guard: the kwargs kanban_audit uses must bind to the REAL
# hermes_cli.kanban_db functions. Cheap (no DB) — catches a future rename or a
# keyword->positional change in kanban_db that the kwarg-tolerant FakeKb hides.
# ---------------------------------------------------------------------------

def test_audit_kwargs_bind_to_real_kanban_db_signatures():
    import inspect

    try:
        from hermes_cli import kanban_db as realkb  # type: ignore
    except Exception:
        return  # real module unavailable in this env -> skip (no failure)

    _conn = object()  # placeholder for the positional `conn`

    # open_card -> create_task(conn, *, title, body, created_by, tenant,
    #              session_id, initial_status, idempotency_key)
    inspect.signature(realkb.create_task).bind(
        _conn,
        title="t",
        body="b",
        created_by="matrix_coder",
        tenant="matrix_coder",
        session_id="s",
        initial_status="running",
        idempotency_key="k",
    )

    # close_card (done) -> complete_task(conn, task_id, *, summary, metadata)
    inspect.signature(realkb.complete_task).bind(
        _conn, "card-id", summary="s", metadata={"source": "matrix_coder"}
    )

    # close_card (blocked) -> block_task(conn, task_id, *, reason)
    inspect.signature(realkb.block_task).bind(_conn, "card-id", reason="r")

    # connect() called with no args
    inspect.signature(realkb.connect).bind()


# ---------------------------------------------------------------------------
# open_child_card
# ---------------------------------------------------------------------------

def test_open_child_card_creates_running_card_with_parent():
    fake = FakeKb()
    prev = _install(fake)
    try:
        child_id = kanban_audit.open_child_card(
            "parent-1", "review", "security", "check auth", "s1"
        )
        assert child_id == "card-123"
        assert len(fake.create_calls) == 1
        kw = fake.create_calls[0]
        assert kw["initial_status"] == "running"
        assert kw["created_by"] == "matrix_coder"
        assert kw["tenant"] == "matrix_coder"
        assert kw["session_id"] == "s1"
        assert kw["parents"] == ["parent-1"]
        assert "review/security" in kw["title"]
        assert kw["body"] == "check auth"
        assert kw.get("idempotency_key")
    finally:
        kanban_audit._kb = prev


def test_open_child_card_no_lens():
    fake = FakeKb()
    prev = _install(fake)
    try:
        kanban_audit.open_child_card("parent-1", "executor", None, "add export", None)
        assert fake.create_calls[0]["title"].startswith("  ↳ executor:")
        assert "parents" in fake.create_calls[0]
        assert fake.create_calls[0]["parents"] == ["parent-1"]
    finally:
        kanban_audit._kb = prev


def test_open_child_card_disabled_returns_none():
    prev = _install(None)
    try:
        assert kanban_audit.open_child_card("p", "review", None, "x", "s") is None
    finally:
        kanban_audit._kb = prev


def test_open_child_card_empty_parent_returns_none():
    fake = FakeKb()
    prev = _install(fake)
    try:
        # empty-string parent_id is falsy -> should return None without calling create
        result = kanban_audit.open_child_card("", "review", None, "x", "s")
        assert result is None
        assert fake.create_calls == []
    finally:
        kanban_audit._kb = prev


def test_open_child_card_raising_backend_swallowed():
    prev = _install(RaisingKb())
    try:
        assert kanban_audit.open_child_card("parent-1", "review", None, "x", "s") is None
    finally:
        kanban_audit._kb = prev


# ---------------------------------------------------------------------------
# HermesBridge child-card bookkeeping
# ---------------------------------------------------------------------------

def test_bridge_register_and_pop_child_card_ids():
    bridge.clear_active_card()
    assert bridge.pop_child_card_ids() == []

    bridge.register_child_card("child-1")
    bridge.register_child_card("child-2")
    ids = bridge.pop_child_card_ids()
    assert ids == ["child-1", "child-2"]
    # pop clears the list
    assert bridge.pop_child_card_ids() == []


def test_bridge_clear_active_card_also_clears_children():
    bridge.register_child_card("child-a")
    bridge.set_active_card("parent-x")
    bridge.clear_active_card()
    assert bridge.active_card_id() is None
    assert bridge.pop_child_card_ids() == []


# ---------------------------------------------------------------------------
# single-dispatch collapse: handle_trigger produces exactly one card, no child
# ---------------------------------------------------------------------------

def test_single_dispatch_no_child_card():
    fake = FakeKb(card_id="only-card")
    prev = _install(fake)
    bridge.clear_active_persona()
    bridge.clear_active_card()
    try:
        composed = harness.handle_trigger(
            "matrix review security: check auth", session_id="s1"
        )
        assert composed is not None
        # Only one card created — the parent invocation card
        assert len(fake.create_calls) == 1
        assert bridge.active_card_id("s1") == "only-card"
        # LOW-1 fix: pass the same session_id used by the dispatch ("s1"),
        # not no-arg (which uses the sentinel "") — the old bare call was
        # vacuously passing because it looked in the wrong session slot.
        assert bridge.pop_child_card_ids("s1") == []
    finally:
        kanban_audit._kb = prev
        bridge.clear_active_persona("s1")
        bridge.clear_active_card("s1")


# ---------------------------------------------------------------------------
# signature-drift guard: parents kwarg must bind to the REAL kanban_db.create_task
# ---------------------------------------------------------------------------

def test_create_task_accepts_parents_kwarg():
    import inspect

    try:
        from hermes_cli import kanban_db as realkb  # type: ignore
    except Exception:
        return  # real module unavailable in this env -> skip (no failure)

    _conn = object()
    inspect.signature(realkb.create_task).bind(
        _conn,
        title="t",
        body="b",
        created_by="matrix_coder",
        tenant="matrix_coder",
        session_id="s",
        initial_status="running",
        idempotency_key="k",
        parents=["parent-id"],
    )


if __name__ == "__main__":  # pragma: no cover - stdlib smoke runner
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"PASS {name}")
    print("ALL TESTS PASSED")
