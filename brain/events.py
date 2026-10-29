"""A tiny in-process event bus: the brain narrates what it is doing so the
status page (a separate process) can show it live.

Hard rules, because this sits on the brain's hot path:
  * `emit()` never blocks, never raises and never awaits. With nobody
    listening it is a deque append.
  * Subscribers get a bounded queue; a slow or stuck one loses its oldest
    events rather than holding anything up.
  * It is observation only. Nothing in the brain reads events back.

Events carry a `turn` id (set with `begin_turn`) so everything one user
message caused -- the prompt, the classifier verdict, the routing, the model's
reasoning, the reply, the speech -- can be grouped. The id travels in a
contextvar, so emit() calls deep in the router or an LLM client need no
plumbing.
"""

from __future__ import annotations

import asyncio
import contextlib
import contextvars
import itertools
import threading
import time
from collections import deque
from collections.abc import Iterator
from typing import Any

BACKLOG = 2000
SUBSCRIBER_QUEUE = 500

_current_turn: contextvars.ContextVar[str | None] = contextvars.ContextVar("deskmate_turn", default=None)
_purpose: contextvars.ContextVar[str | None] = contextvars.ContextVar("deskmate_llm_purpose", default=None)


class EventBus:
    def __init__(self, backlog: int = BACKLOG) -> None:
        self._buffer: deque[dict[str, Any]] = deque(maxlen=backlog)
        self._seq = itertools.count(1)
        self._lock = threading.Lock()
        self._subscribers: list[tuple[asyncio.AbstractEventLoop, asyncio.Queue]] = []

    def emit(self, type_: str, payload: dict[str, Any] | None = None, *, turn: str | None = None) -> None:
        try:
            event = {
                "seq": next(self._seq),
                "ts": time.time(),
                "type": type_,
                "turn": turn if turn is not None else _current_turn.get(),
                "data": payload or {},
            }
            with self._lock:
                self._buffer.append(event)
                subscribers = list(self._subscribers)
            for loop, queue in subscribers:
                try:
                    loop.call_soon_threadsafe(self._offer, queue, event)
                except RuntimeError:  # that subscriber's loop is closed
                    pass
        except Exception:  # noqa: BLE001, S110 -- observation must never break the brain, and logging here could recurse
            pass

    @staticmethod
    def _offer(queue: asyncio.Queue, event: dict[str, Any]) -> None:
        if queue.full():
            with contextlib.suppress(asyncio.QueueEmpty):
                queue.get_nowait()  # drop the oldest
        queue.put_nowait(event)

    def since(self, seq: int) -> list[dict[str, Any]]:
        with self._lock:
            return [e for e in self._buffer if e["seq"] > seq]

    @contextlib.contextmanager
    def subscribe(self) -> Iterator[asyncio.Queue]:
        """Must be called from the event loop that will consume the queue."""
        entry = (asyncio.get_running_loop(), asyncio.Queue(maxsize=SUBSCRIBER_QUEUE))
        with self._lock:
            self._subscribers.append(entry)
        try:
            yield entry[1]
        finally:
            with self._lock:
                self._subscribers.remove(entry)


bus = EventBus()


def emit(type_: str, payload: dict[str, Any] | None = None, *, turn: str | None = None) -> None:
    bus.emit(type_, payload, turn=turn)


def begin_turn() -> str:
    """Start a new turn in the current context and return its id."""
    turn = f"t{int(time.time() * 1000)}"
    _current_turn.set(turn)
    return turn


def current_turn() -> str | None:
    return _current_turn.get()


@contextlib.contextmanager
def llm_purpose(purpose: str) -> Iterator[None]:
    """Label the LLM calls made inside the block ("classify", "reply", ...)."""
    token = _purpose.set(purpose)
    try:
        yield
    finally:
        _purpose.reset(token)


def current_purpose() -> str | None:
    return _purpose.get()
