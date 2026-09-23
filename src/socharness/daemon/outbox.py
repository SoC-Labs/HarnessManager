"""A bounded, drop-oldest queue from engine threads to one WebSocket sender.

Engine threads (the event bus, a console reader) must never block on a slow
browser tab. So they ``put`` into an ``Outbox``, which never blocks: when it
holds more than ``max_items`` frames or ``max_bytes`` bytes, the OLDEST frames
are dropped and counted. The WebSocket's sender task awaits ``take()``, which
returns everything queued plus the drop counts since the last take, so the
sender can report the loss to the client before the frames that survived.

uvicorn's WebSocket ``send`` awaits the transport's flow control, so a client
that stops reading stalls only its own sender task; this queue then fills and
drops, and nothing upstream notices.
"""

from __future__ import annotations

import asyncio
import threading
from collections import deque
from dataclasses import dataclass

#: Queued after the last frame when the stream behind the socket has ended.
CLOSE = object()


@dataclass(frozen=True)
class Batch:
    items: list
    dropped_items: int = 0
    dropped_bytes: int = 0
    closed: bool = False


class Outbox:
    def __init__(self, loop: asyncio.AbstractEventLoop, *, max_items: int = 2000,
                 max_bytes: int = 4 * 1024 * 1024) -> None:
        self._loop = loop
        self.max_items = max(1, max_items)
        self.max_bytes = max(1, max_bytes)
        self._lock = threading.Lock()
        self._items: deque = deque()
        self._bytes = 0
        self._dropped_items = 0
        self._dropped_bytes = 0
        self.total_dropped_items = 0
        self.total_dropped_bytes = 0
        self._closed = False
        self._scheduled = False
        self._wake = asyncio.Event()

    @property
    def closed(self) -> bool:
        return self._closed

    def put(self, item: str | bytes) -> None:
        """Queue one frame from any thread. Never blocks; may drop the oldest frames."""
        with self._lock:
            if self._closed:
                return
            self._items.append(item)
            self._bytes += len(item)
            while len(self._items) > 1 and (len(self._items) > self.max_items
                                            or self._bytes > self.max_bytes):
                old = self._items.popleft()
                self._bytes -= len(old)
                self._dropped_items += 1
                self._dropped_bytes += len(old)
                self.total_dropped_items += 1
                self.total_dropped_bytes += len(old)
        self._kick()

    def note_dropped(self, items: int = 0, nbytes: int = 0) -> None:
        """Count a loss that happened upstream of this queue (e.g. a subscriber buffer)."""
        if not items and not nbytes:
            return
        with self._lock:
            self._dropped_items += items
            self._dropped_bytes += nbytes
            self.total_dropped_items += items
            self.total_dropped_bytes += nbytes
        self._kick()

    def close(self) -> None:
        """End the stream: ``take`` returns what is queued, then ``closed=True``."""
        with self._lock:
            if self._closed:
                return
            self._items.append(CLOSE)
            self._closed = True
        self._kick(force=True)

    def _kick(self, force: bool = False) -> None:
        with self._lock:
            if self._scheduled and not force:
                return
            self._scheduled = True
        try:
            self._loop.call_soon_threadsafe(self._wake_up)
        except RuntimeError:          # the loop is gone: the socket is gone with it
            pass

    def _wake_up(self) -> None:
        with self._lock:
            self._scheduled = False
        self._wake.set()

    async def take(self) -> Batch:
        """Wait for frames; return all of them (oldest first) and the drops since last time."""
        while True:
            await self._wake.wait()
            self._wake.clear()
            with self._lock:
                items = list(self._items)
                self._items.clear()
                self._bytes = 0
                dropped = (self._dropped_items, self._dropped_bytes)
                self._dropped_items = self._dropped_bytes = 0
            closed = bool(items) and items[-1] is CLOSE
            if closed:
                items.pop()
            if items or closed or dropped != (0, 0):
                return Batch(items, dropped[0], dropped[1], closed)
