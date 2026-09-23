"""EventBus -> Qt signal bridge.

The engine publishes on its ``EventBus`` from whatever thread did the work.
``EventBridge`` subscribes once and re-emits each event on the GUI thread
through a queued connection, in the order it was published. The bus never
touches Qt and a widget never sees an event on a worker thread.
"""

from __future__ import annotations

import logging

from PySide6.QtCore import QObject, Qt, Signal, Slot

from socharness.core.events import Event, EventBus

log = logging.getLogger(__name__)


def topic_matches(pattern: str, topic: str) -> bool:
    """The bus's own rule: ``*``, an exact topic, or a ``prefix.*``."""
    if pattern in ("*", topic):
        return True
    return pattern.endswith(".*") and topic.startswith(pattern[:-1])


class EventBridge(QObject):
    """Re-emits every bus event on the GUI thread as ``event(Event)``."""

    event = Signal(object)
    _relay = Signal(object)

    def __init__(self, bus: EventBus, pattern: str = "*", parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._closed = False
        self._relay.connect(self._deliver, Qt.ConnectionType.QueuedConnection)
        self._unsubscribe = bus.subscribe(pattern, self._on_bus)

    def _on_bus(self, ev: Event) -> None:
        # Runs on the publisher's thread. Only a queued emit happens here.
        if self._closed:
            return
        try:
            self._relay.emit(ev)
        except RuntimeError:  # the Qt object is gone; the bus will drop us on close()
            log.debug("event %s arrived after the bridge was deleted", ev.topic)

    @Slot(object)
    def _deliver(self, ev: Event) -> None:
        if not self._closed:
            self.event.emit(ev)

    def close(self) -> None:
        if not self._closed:
            self._closed = True
            self._unsubscribe()
