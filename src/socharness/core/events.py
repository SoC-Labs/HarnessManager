"""A small thread-safe event bus.

The engine publishes and the front-ends subscribe. The GUI marshals events
onto its own thread; the bus never touches Qt. Handlers must be quick. A
handler that raises is logged and dropped from that delivery, never
propagated into the engine.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Event:
    topic: str                     # "board.found", "deploy.progress", "console.line", ...
    board_id: str = ""
    data: dict[str, Any] = field(default_factory=dict)
    at: float = field(default_factory=time.time)


Handler = Callable[[Event], None]


class EventBus:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._subs: dict[str, list[Handler]] = {}

    def subscribe(self, topic: str, handler: Handler) -> Callable[[], None]:
        """Subscribe to ``topic`` ("*" for everything, "deploy.*" for a prefix).

        Returns an unsubscribe function.
        """
        with self._lock:
            self._subs.setdefault(topic, []).append(handler)

        def _unsub() -> None:
            with self._lock:
                handlers = self._subs.get(topic, [])
                if handler in handlers:
                    handlers.remove(handler)

        return _unsub

    def publish(self, event: Event) -> None:
        with self._lock:
            targets = [
                h
                for pattern, handlers in self._subs.items()
                if _matches(pattern, event.topic)
                for h in handlers
            ]
        for handler in targets:
            try:
                handler(event)
            except Exception:  # noqa: BLE001 - isolation is the point
                log.exception("event handler failed for %s", event.topic)


def _matches(pattern: str, topic: str) -> bool:
    if pattern == "*" or pattern == topic:
        return True
    if pattern.endswith(".*"):
        return topic.startswith(pattern[:-1])
    return False
