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
import logging
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Dict, List, Optional

logger = logging.getLogger("workflow.event-bus")

# Sentinel to signal subscriber shutdown
_STOP = object()


@dataclass
class _Subscriber:
    queue: asyncio.Queue
    run_id: Optional[str]  # None = all runs


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
    ) -> None:
        """
        Persist event to DB and fan out to all matching subscribers.
        Non-blocking — drops oldest item if queue full.
        """
        # 1. Persist
        try:
            self._run_store.insert_event(
                workflow_run_id=run_id,
                event_type=event_type,
                node_run_id=node_run_id,
                data=data,
            )
        except Exception as exc:
            logger.warning("EventBus: failed to persist event %s: %s", event_type, exc)

        # 2. Fan out
        payload = {
            "run_id": run_id,
            "event_type": event_type,
            "node_run_id": node_run_id,
            "data": data or {},
        }
        dead: List[_Subscriber] = []
        for sub in list(self._subscribers):
            if sub.run_id is not None and sub.run_id != run_id:
                continue
            try:
                sub.queue.put_nowait(payload)
            except asyncio.QueueFull:
                # Drop oldest, then put new
                try:
                    sub.queue.get_nowait()
                except asyncio.QueueEmpty:
                    pass
                try:
                    sub.queue.put_nowait(payload)
                except Exception:
                    dead.append(sub)
            except Exception:
                dead.append(sub)
        for d in dead:
            self._unsubscribe(d)

    def _unsubscribe(self, sub: _Subscriber) -> None:
        try:
            self._subscribers.remove(sub)
        except ValueError:
            pass
        # Signal the async generator to stop
        try:
            sub.queue.put_nowait(_STOP)
        except Exception:
            pass

    async def subscribe(self, run_id: Optional[str] = None) -> AsyncIterator[Dict[str, Any]]:
        """
        Async generator yielding events.

        Replays last 50 DB events, then yields live events until the caller
        breaks or the generator is garbage-collected.
        """
        queue: asyncio.Queue = asyncio.Queue(maxsize=1000)
        sub = _Subscriber(queue=queue, run_id=run_id)
        self._subscribers.append(sub)

        try:
            # Replay last 50 events from DB
            try:
                replayed = self._run_store.list_recent_events(run_id, limit=50)
                for evt_row in replayed:
                    yield {
                        "run_id": evt_row.get("workflow_run_id", run_id),
                        "event_type": evt_row.get("event_type", ""),
                        "node_run_id": evt_row.get("node_run_id"),
                        "data": evt_row.get("data") or {},
                        "created_at": evt_row.get("created_at"),
                        "_replayed": True,
                    }
            except Exception as exc:
                logger.warning("EventBus: replay failed: %s", exc)

            # Live events
            while True:
                item = await queue.get()
                if item is _STOP:
                    break
                yield item
        finally:
            self._unsubscribe(sub)

    def close_all(self) -> None:
        """Signal all subscribers to stop (called on engine shutdown)."""
        for sub in list(self._subscribers):
            self._unsubscribe(sub)
        self._subscribers.clear()
