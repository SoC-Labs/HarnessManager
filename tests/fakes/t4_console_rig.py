"""Team T4 test rig: a single-client console port, a fake UART, an event log, a bare session.

``SingleClientProxy`` sits in front of one FakeShell console port and makes it
behave like the real firmware, which FakeShell does not model:

- **one client**: a second connection is accepted and closed at once
  (``firmware/uart_over_eth/uart_over_eth.c:188-198``); ``accepted`` and
  ``refused`` count both, so a test can assert "the board saw ONE connection";
- **the swap drop**: ``drop()`` closes the live connection, as a partition swap
  does to UART1/SWO (harness handover);
- **a board that is away**: ``down()`` stops listening (connection refused),
  ``up()`` listens again on the same port.

Loopback only. Stdlib only.
"""

from __future__ import annotations

import socket
import threading
import time
from collections.abc import Callable

from harness_manager.core.events import Event, EventBus
from harness_manager.core.model import BoardIdentity, Candidate, Health, Link, LinkKind
from harness_manager.core.pack import BoardSession


class SingleClientProxy:
    def __init__(self, target_port: int, host: str = "127.0.0.1") -> None:
        self.target = (host, target_port)
        self.accepted = 0
        self.refused = 0
        self._lock = threading.Lock()
        self._pair: tuple[socket.socket, socket.socket] | None = None
        self._stop = threading.Event()
        self._listener: socket.socket | None = None
        self.port = 0
        self.up()

    # -- control ---------------------------------------------------------------------

    def up(self) -> None:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind(("127.0.0.1", self.port))
        s.listen(8)
        s.settimeout(0.1)
        self.port = s.getsockname()[1]
        self._listener = s
        self._stop.clear()
        threading.Thread(target=self._accept_loop, args=(s,), daemon=True).start()

    def down(self) -> None:
        """Stop listening (new connections are refused) and drop the live one."""
        self._stop.set()
        if self._listener is not None:
            self._listener.close()
            self._listener = None
        self.drop()

    def drop(self) -> None:
        """Close the live connection, as a partition swap does to UART1/SWO."""
        with self._lock:
            pair, self._pair = self._pair, None
        if pair is not None:
            for s in pair:
                _close(s)

    @property
    def busy(self) -> bool:
        with self._lock:
            return self._pair is not None

    def close(self) -> None:
        self.down()

    # -- plumbing ---------------------------------------------------------------------

    def _accept_loop(self, listener: socket.socket) -> None:
        while not self._stop.is_set():
            try:
                client, _ = listener.accept()
            except (TimeoutError, OSError):
                if self._stop.is_set():
                    break
                continue
            with self._lock:
                busy = self._pair is not None
            if busy:
                self.refused += 1              # uart_over_eth.c:188-198: accept, then close
                _close(client)
                continue
            try:
                board = socket.create_connection(self.target, timeout=2.0)
            except OSError:
                _close(client)
                continue
            # The timeout was for the connect only: left on, a console idle for 2 s after
            # its banner timed out in _pump and looked like a swap drop (DEBUG-6921).
            board.settimeout(None)
            with self._lock:
                self._pair = (client, board)
                self.accepted += 1
            threading.Thread(target=self._pump, args=(client, board), daemon=True).start()
            threading.Thread(target=self._pump, args=(board, client), daemon=True).start()

    def _pump(self, src: socket.socket, dst: socket.socket) -> None:
        try:
            while True:
                data = src.recv(4096)
                if not data:
                    break
                dst.sendall(data)
        except OSError:
            pass
        with self._lock:
            mine = self._pair is not None and src in self._pair
            if mine:
                self._pair = None
        if mine:
            _close(src)
            _close(dst)


def _close(s: socket.socket) -> None:
    try:
        s.shutdown(socket.SHUT_RDWR)
    except OSError:
        pass
    s.close()


class FakeUart:
    """A ``core.transport.SerialPort`` for ``fake://`` console endpoints."""

    def __init__(self) -> None:
        self._rx = bytearray()
        self.written = bytearray()
        self.closed = False
        self.broken = False
        self._lock = threading.Lock()

    def feed(self, data: bytes) -> None:
        with self._lock:
            self._rx += data

    def unplug(self) -> None:
        self.broken = True

    # SerialPort
    def write(self, data: bytes) -> int:
        with self._lock:
            self.written += data
        return len(data)

    def read(self, size: int = 1) -> bytes:
        with self._lock:
            out = bytes(self._rx[:size])
            del self._rx[:size]
            return out

    def read_until(self, expected: bytes = b"\n", size: int | None = None) -> bytes:
        return self.read(size or len(self._rx))

    @property
    def in_waiting(self) -> int:
        if self.broken:
            raise OSError("device disconnected")
        with self._lock:
            return len(self._rx)

    def reset_input_buffer(self) -> None:
        with self._lock:
            self._rx.clear()

    def close(self) -> None:
        self.closed = True


class EventLog:
    """Collects bus events; ``wait_for`` blocks on a condition, never on a fixed sleep."""

    def __init__(self, bus: EventBus, topic: str = "*") -> None:
        self.events: list[Event] = []
        self._cond = threading.Condition()
        bus.subscribe(topic, self._on)

    def _on(self, event: Event) -> None:
        with self._cond:
            self.events.append(event)
            self._cond.notify_all()

    def wait_for(self, pred: Callable[[Event], bool], timeout: float = 10.0,
                 after: int = 0) -> Event:
        deadline = time.monotonic() + timeout
        with self._cond:
            while True:
                for ev in self.events[after:]:
                    if pred(ev):
                        return ev
                left = deadline - time.monotonic()
                if left <= 0:
                    seen = [(e.topic, e.data) for e in self.events[after:]]
                    raise AssertionError(f"no matching event within {timeout}s; saw {seen}")
                self._cond.wait(left)

    def mark(self) -> int:
        with self._cond:
            return len(self.events)

    def topics(self, prefix: str = "") -> list[tuple[str, dict]]:
        with self._cond:
            return [(e.topic, dict(e.data)) for e in self.events if e.topic.startswith(prefix)]


class _Consoles:
    def __init__(self, endpoints: dict[str, str]) -> None:
        self._eps = endpoints

    def console_endpoints(self) -> dict[str, str]:
        return dict(self._eps)


class BareSession(BoardSession):
    """A board session with only the adapters a unit test gives it."""

    def __init__(self, board_id: str = "test@board", *, consoles: dict[str, str] | None = None,
                 debug: object | None = None) -> None:
        self.candidate = Candidate(pack="test", board_id=board_id,
                                   links=(Link(LinkKind.ETHERNET, "127.0.0.1"),))
        self.consoles = _Consoles(consoles) if consoles is not None else None
        self.debug = debug

    def identity(self) -> BoardIdentity:
        return BoardIdentity(board_type="test")

    def health(self) -> Health:
        return Health(reachable=True)


def read_until(stream, pattern: bytes, timeout: float = 5.0) -> bytes:
    """Read a ConsoleStream until ``pattern`` appears (event-driven: read blocks)."""
    buf = b""
    deadline = time.monotonic() + timeout
    while pattern not in buf:
        left = deadline - time.monotonic()
        if left <= 0:
            raise AssertionError(f"{pattern!r} not seen within {timeout}s; got {buf!r}")
        buf += stream.read(left)
    return buf


def recv_until(sock: socket.socket, pattern: bytes, timeout: float = 5.0) -> bytes:
    buf = b""
    sock.settimeout(timeout)
    deadline = time.monotonic() + timeout
    while pattern not in buf:
        if time.monotonic() > deadline:
            raise AssertionError(f"{pattern!r} not seen within {timeout}s; got {buf!r}")
        chunk = sock.recv(4096)
        if not chunk:
            raise AssertionError(f"closed before {pattern!r}; got {buf!r}")
        buf += chunk
    return buf


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port
