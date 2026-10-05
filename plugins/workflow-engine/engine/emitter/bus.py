"""
EventBus — asyncio.Queue-backed pub/sub for workflow run events.

Design:
- One EventBus per process (singleton via module-level _bus).
- Each subscriber gets a dedicated asyncio.Queue(maxsize=1000).
- On overflow, oldest item is dropped (non-blocking put).
- subscribe(run_id=X) filters to that run; subscribe() receives all events.
- Replay: on subscribe, the last 50 events from DB are sent before live events.
- Every emitted event is also persisted to workflow_events via RunStore.
"""
from __future__ import annotations

import asyncio
import heapq
import logging
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Dict, List, Optional

from engine.store.run_store import STORE_LOCK

logger = logging.getLogger("workflow.event-bus")

# Sentinel to signal subscriber shutdown
_STOP = object()

_SEEN_MAX = 2000  # seqs remembered per subscriber for bus/tail dedupe
_TAIL_BATCH = 500  # rows per DB-tail poll
# node_log is high-volume live output: its insert waits at most this long for
# the store lock / another process's sqlite write lock, else it is dropped
# from the DB (still fanned out here) so the engine loop never stalls on it.
_BEST_EFFORT_TYPES = ("node_log",)
_BEST_EFFORT_WAIT_S = 0.05
# The run-scoped DB tail stops _TERMINAL_GRACE_S after a terminal event (rows
# another process commits just after it still arrive) and restarts on a retry.
_TERMINAL_TYPES = ("workflow_completed", "workflow_failed", "workflow_cancelled")
_TERMINAL_GRACE_S = 5.0


def _row_payload(row: Dict[str, Any], run_id: Optional[str]) -> Dict[str, Any]:
    """A workflow_events row in the live-payload shape."""
    return {
        "id": row.get("id"),
        "seq": row.get("seq"),
        "run_id": row.get("workflow_run_id", run_id),
        "event_type": row.get("event_type", ""),
        "node_run_id": row.get("node_run_id"),
        "data": row.get("data") or {},
        "created_at": row.get("created_at"),
    }


@dataclass
class _Subscriber:
    queue: asyncio.Queue
    run_id: Optional[str]  # None = all runs
    # Consumer's loop. emit() runs on the engine-loop thread; asyncio.Queue
    # is not thread-safe, so cross-loop puts go through call_soon_threadsafe.
    loop: Optional[asyncio.AbstractEventLoop] = None


class EventBus:
    """
    Async pub/sub event bus for workflow run events.

    Usage::

        bus = EventBus(run_store=store)

        # Publisher side (sync, called from runner):
        bus.emit(run_id="abc", event_type="node_started", data={"node_id": "n1"})

        # Subscriber side (async):
        async for event in bus.subscribe(run_id="abc"):
            print(event)
    """

    def __init__(self, run_store: Any) -> None:  # run_store: RunStore
        self._run_store = run_store
        self._subscribers: List[_Subscriber] = []

    def emit(
        self,
        *,
        run_id: str,
        event_type: str,
        node_run_id: Optional[str] = None,
        data: Optional[Dict[str, Any]] = None,
        step_index: Optional[int] = None,
        step_name: Optional[str] = None,
    ) -> None:
        """
        Persist event to DB and fan out to all matching subscribers.
        Non-blocking — drops oldest item if queue full.
        """
        # 1. Persist
        eid: Optional[str] = None
        seq: Optional[int] = None
        row = dict(
            workflow_run_id=run_id,
            event_type=event_type,
            node_run_id=node_run_id,
            data=data,
            step_index=step_index,
            step_name=step_name,
        )
        best_effort = event_type in _BEST_EFFORT_TYPES
        try:
            if not best_effort:
                eid, seq = self._run_store.insert_event(**row)
            elif STORE_LOCK.acquire(timeout=_BEST_EFFORT_WAIT_S):
                try:
                    eid, seq = self._run_store.insert_event(
                        **row, busy_timeout_ms=int(_BEST_EFFORT_WAIT_S * 1000),
                    )
                finally:
                    STORE_LOCK.release()
            else:
                logger.debug("EventBus: store busy, %s not persisted", event_type)
        except Exception as exc:
            (logger.debug if best_effort else logger.warning)(
                "EventBus: failed to persist event %s: %s", event_type, exc,
            )

        # 2. Fan out. id/seq let subscribers dedupe against DB-tailed rows.
        payload = {
            "id": eid,
            "seq": seq,
            "run_id": run_id,
            "event_type": event_type,
            "node_run_id": node_run_id,
            "data": data or {},
        }
        try:
            here = asyncio.get_running_loop()
        except RuntimeError:
            here = None
        for sub in list(self._subscribers):
            if sub.run_id is not None and sub.run_id != run_id:
                continue
            if sub.loop is None or sub.loop is here:
                self._deliver(sub, payload)
            else:
                try:
                    sub.loop.call_soon_threadsafe(self._deliver, sub, payload)
                except RuntimeError:  # consumer loop closed
                    self._unsubscribe(sub)

    def _deliver(self, sub: _Subscriber, payload: Dict[str, Any]) -> None:
        """put_nowait on the consumer's loop; drop oldest when full."""
        try:
            sub.queue.put_nowait(payload)
        except asyncio.QueueFull:
            try:
                sub.queue.get_nowait()
            except asyncio.QueueEmpty:
                pass
            try:
                sub.queue.put_nowait(payload)
            except Exception:
                self._unsubscribe(sub)
        except Exception:
            self._unsubscribe(sub)

    def _unsubscribe(self, sub: _Subscriber) -> None:
        try:
            self._subscribers.remove(sub)
        except ValueError:
            pass
        # Signal the async generator to stop
        try:
            if sub.loop is not None and not sub.loop.is_closed():
                sub.loop.call_soon_threadsafe(sub.queue.put_nowait, _STOP)
            else:
                sub.queue.put_nowait(_STOP)
        except Exception:
            pass

    async def subscribe(
        self,
        run_id: Optional[str] = None,
        *,
        tail_interval_s: Optional[float] = None,
    ) -> AsyncIterator[Dict[str, Any]]:
        """
        Async generator yielding events.

        Replays last 50 DB events (node_log excluded), then yields live events
        until the caller breaks or the generator is garbage-collected.

        ``tail_interval_s`` (run-scoped only): also poll workflow_events by
        rowid so events persisted by *other processes* (gateway, daemon) reach
        this subscriber. Bus-delivered and tailed rows are deduped on ``seq``.
        """
        queue: asyncio.Queue = asyncio.Queue(maxsize=1000)
        loop = asyncio.get_running_loop()
        sub = _Subscriber(queue=queue, run_id=run_id, loop=loop)
        self._subscribers.append(sub)
        tail_wanted = bool(tail_interval_s) and run_id is not None
        tail = tail_wanted
        tail_until: Optional[float] = None  # set by a terminal event
        # Bounded seen-seq set. Evict the *smallest* seq: the tail cursor only
        # moves forward, so low seqs can't come back from the tail, while a
        # bus burst far ahead of the tail must stay remembered.
        seen: set = set()
        seen_heap: List[int] = []

        def unseen(evt: Dict[str, Any]) -> bool:
            # Settled run: stop tailing after a grace; a retry reopens it.
            nonlocal tail, tail_until
            if evt.get("event_type") in _TERMINAL_TYPES:
                if tail and tail_until is None:
                    tail_until = loop.time() + _TERMINAL_GRACE_S
            elif evt.get("event_type") == "workflow_retried":
                tail, tail_until = tail_wanted, None
            seq = evt.get("seq")
            if seq is None:
                return True
            if seq in seen:
                return False
            seen.add(seq)
            heapq.heappush(seen_heap, seq)
            if len(seen_heap) > _SEEN_MAX:
                seen.discard(heapq.heappop(seen_heap))
            return True

        try:
            # Snapshot the tail cursor before the replay query so no row
            # committed in between is missed (duplicates are deduped).
            cursor = 0
            if tail_wanted:
                try:
                    cursor = await asyncio.to_thread(self._run_store.max_event_rowid, run_id)
                except Exception as exc:
                    logger.warning("EventBus: tail cursor failed, tail off: %s", exc)
                    tail = tail_wanted = False
            # Replay last 50 events from DB
            try:
                replayed = self._run_store.list_recent_events(run_id, limit=50)
                for evt_row in replayed:
                    evt = _row_payload(evt_row, run_id)
                    evt["_replayed"] = True
                    if unseen(evt):
                        yield evt
            except Exception as exc:
                logger.warning("EventBus: replay failed: %s", exc)

            # Live events (+ DB tail every tail_interval_s)
            next_poll = loop.time() + (tail_interval_s or 0)
            while True:
                if tail:
                    try:
                        item = await asyncio.wait_for(
                            queue.get(), timeout=max(0.0, next_poll - loop.time()),
                        )
                    except asyncio.TimeoutError:
                        item = None
                else:
                    item = await queue.get()
                if item is _STOP:
                    break
                if item is not None and unseen(item):
                    yield item
                if tail and loop.time() >= next_poll:
                    next_poll = loop.time() + tail_interval_s
                    try:
                        rows = await asyncio.to_thread(
                            self._run_store.list_events_after, run_id, cursor, _TAIL_BATCH,
                        )
                    except Exception as exc:
                        logger.warning("EventBus: tail poll failed: %s", exc)
                        rows = []
                    for row in rows:
                        cursor = max(cursor, row["seq"])
                        evt = _row_payload(row, run_id)
                        if unseen(evt):
                            yield evt
                    if tail_until is not None and loop.time() >= tail_until:
                        tail = False
        finally:
            self._unsubscribe(sub)

    def close_all(self) -> None:
        """Signal all subscribers to stop (called on engine shutdown)."""
        for sub in list(self._subscribers):
            self._unsubscribe(sub)
        self._subscribers.clear()
