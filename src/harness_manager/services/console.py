"""The console broker: one connection per board console, fanned out to many readers.

Why a broker at all
-------------------
Every TCP service on the MPS3 shell accepts ONE client and refuses the rest:
``uart_over_eth_poll()`` accepts a second connection and closes it at once
(``firmware/uart_over_eth/uart_over_eth.c:188-198``; the JTAG server does the
same, ``jtag_server.c:185``). So a GUI tab, a CLI ``console`` verb and an
external terminal cannot each dial the board. The broker dials each console
ONCE per (board, name) and hands every subscriber its own buffered stream.

Behaviour
---------
- **One upstream per (board, console).** It opens on the first subscriber (or
  TCP export) and closes when the last one leaves, which frees the board's port
  for other tools.
- **Fan-out without head-of-line blocking.** Each subscriber has its own buffer
  (``buffer_bytes``, oldest bytes dropped and counted in ``dropped`` if a
  reader stalls), so a slow reader never blocks the board connection or the
  other readers.
- **Scrollback.** The last ``history_bytes`` are replayed to a new subscriber
  (``replay=True``, the default), so a console opened a moment after the boot
  banner still shows it.
- **Writes are serialised** onto the one upstream connection, whole chunk by
  whole chunk, so two writers never interleave inside a chunk.
- **Paced writes.** A console the board pack declares slow
  (``ConsoleAdapter.console_write_pace_s() -> {name: seconds}``, optional) gets
  one byte per ``seconds``, sent from a writer thread so ``write`` never blocks
  the caller. The MPS3 DUT UARTs need it: the nanoSoC UART has no receive FIFO,
  and unpaced input is dropped (board window 2026-09-23).
- **Reconnect with backoff.** UART1 (6931) and SWO (6932) drop on every
  partition swap (harness handover; the RP is decoupled), and a connection can
  drop for other reasons. The broker re-dials with exponential backoff
  (``backoff`` = (first, max) seconds).
- **Swaps.** On ``deploy.started`` every console of that board is closed and
  paused (subscribers stay attached). ``deploy.done`` with ``verified: true``
  re-dials at once. ``deploy.done`` unverified, or ``deploy.failed`` after the
  swap started (stage ``deploy``/``confirm``), leaves them down with the reason
  in ``console.state``; ``reconnect(board_id)`` re-dials. A preflight refusal
  (``deploy.failed`` with no ``deploy.started``) touches nothing.
- **Events** on the engine bus (``docs/CONTRACTS.md`` topics):
  ``console.state {name, state, detail, endpoint}`` with state one of
  ``connecting | up | down | paused | closed``; and ``console.line {name, text}`` per
  complete line (UTF-8, ``errors="replace"``, trailing CR stripped). A partial
  line idle for ``line_idle_s`` (a prompt such as ``>>> ``) is published with
  ``partial: True`` and is not repeated when its line completes.
- **A shared lab board** (lane QUIET-POLL). ``lease_holder(board_id)``, when set (the
  daemon sets it from its lease service), names who holds the board's hub lease when it is
  someone else's (else ""; a board without a hub has none). While it names someone, an
  upstream never RE-dials: a live connection is kept until it drops, then the console waits
  in state ``paused`` ("paused: lease held by X") and dials again when the lease becomes ours
  or free (``lease.state`` or ``hub.event`` for the board, or the ``lease_recheck_s`` look).
  An explicit open (``subscribe``, ``export_tcp``, ``pty``) that would need a new board
  connection is refused ``HeldError`` naming the holder; one that joins a live connection
  is not (no new connection). ``explicit=False`` (a PTY reopened after an app update) waits
  paused instead.
- **Re-export** on ``127.0.0.1`` as raw TCP (``export_tcp``), not a PTY, so it
  works on Windows too. Any raw-TCP terminal attaches: ``nc 127.0.0.1 <port>``,
  ``socat -,raw,echo=0 tcp:127.0.0.1:<port>``, PuTTY "Raw". Each connected
  terminal is one more subscriber; the board still sees one connection.
- **PTYs for screen** (``pty``, lane L2): one pseudo-terminal per console with
  a stable path, ``/tmp/harness-manager-$USER/<board-slug>/<console>``, for
  ``screen <path>`` in any terminal; one more subscriber (``services/pty.py``).
- **Baud** (``baud``/``set_baud``, lane L2). A serial console's rate is the
  host port's: ``set_baud`` reopens the port at the new rate (``0`` goes back
  to the URL's ``?baud=``). An Ethernet console's rate is set inside the board:
  the pack reports it (optional ``ConsoleAdapter.console_baud_info()``, cached
  for ``baud_ttl_s`` and dropped on a swap) and changes it only when it can
  (optional ``console_set_baud(name, baud)``, the MPS3 harness verb
  ``uart_baud``); otherwise ``UnavailableError`` with the pack's reason. A
  ``tcp://`` console the pack says nothing about reports ``source: unknown``.
  A change publishes ``console.state`` with ``baud``.

Endpoints come from the board session's ``ConsoleAdapter``, and the broker
opens nothing else: ``tcp://host:port`` (the shell's consoles),
``serial://...`` (FPGA UART lanes ``fpga_uart0..3`` on the Debug USB; Team T3
registers the scheme) and ``fake://name`` (tests). A serial URL may carry
``?baud=N`` (default 115200). The MPS3 MCC console is deliberately never an
endpoint (the controller adapter opens it per operation; a second opener would
collide). ``shell`` is an alias for ``fpga_uart2`` (``ALIASES``), and two names
for one URL share one connection.
"""

from __future__ import annotations

import logging
import queue
import select
import socket
import threading
import time
from collections.abc import Callable
from typing import Any
from urllib.parse import parse_qs, urlparse

from harness_manager.core.errors import (
    AbsentError,
    HarnessError,
    HeldError,
    PortBoundError,
    UnavailableError,
    UnreachableError,
    UsageError,
)
from harness_manager.core.events import Event, EventBus
from harness_manager.core.pack import BoardSession
from harness_manager.core.transport import SerialPort, open_serial
from harness_manager.services import pty as _pty

log = logging.getLogger(__name__)

__all__ = ["ConsoleBroker", "ConsoleSubscription"]

# Names a user may type for a console the board lists under another name.
# "shell": on the MPS3 the shell's console is FPGA UART lane 2 on the Debug USB
# (harness_manager_mps3/usb.py:16-17, V2M-MPS3 schematic sheet 9). An alias only
# applies when the board has no console of that name itself (harness firmware
# A12 will serve "shell" over Ethernet, TCP 6939).
ALIASES = {"shell": "fpga_uart2"}

# A connection that the board closes within this many seconds, having sent
# nothing, is reported as probably held by another client (the firmware's
# accept-then-close refusal looks exactly like this on the wire).
_REFUSAL_WINDOW_S = 1.0
_READ_SLICE_S = 0.2          # how often the reader thread checks for stop/idle flush
_WRITE_TIMEOUT_S = 10.0      # a write to the board gives up after this long (whole chunk)
_LINE_FORCE_FLUSH = 4096     # never hold more than this much of an unterminated line
#: QUIET-POLL: a console paused for someone else's lease looks at the lease again this often
#: (besides every lease.state / hub.event for its board).
LEASE_RECHECK_S = 30.0
PAUSED = "paused"

#: A serial URL's rate when it has no ``?baud=`` (the ``serial://`` opener's default).
DEFAULT_SERIAL_BAUD = 115200
#: Rates offered for a serial console (a host port takes any of them; the FT4232H up to 12 Mbaud).
SERIAL_CHOICES = (9600, 19200, 38400, 57600, 115200, 230400, 460800, 921600)
MAX_BAUD = 12_000_000
#: The capability label on a refused rate change (an error label, not a negotiated capability).
BAUD_CAPABILITY = "console_baud"
UNKNOWN_RATE = ("the board pack does not report this console's rate (a TCP console the "
                "board sets up itself)")


def _bus_of(engine: Any) -> EventBus | None:
    if engine is None:
        return None
    if isinstance(engine, EventBus):
        return engine
    return getattr(engine, "bus", None)


def _pace(session: BoardSession, name: str) -> float:
    """Seconds per byte the board pack asks for on console ``name`` (0: unpaced)."""
    fn = getattr(getattr(session, "consoles", None), "console_write_pace_s", None)
    if fn is None:
        return 0.0
    try:
        return max(0.0, float(fn().get(name, 0.0)))
    except (TypeError, ValueError, AttributeError):
        return 0.0


# --- upstream links ----------------------------------------------------------------


class _TcpLink:
    def __init__(self, host: str, port: int, connect_timeout: float) -> None:
        self.sock = socket.create_connection((host, port), timeout=connect_timeout)
        # One socket timeout serves both directions, and sendall's timeout caps the
        # WHOLE chunk (Python >= 3.5). So writes get a generous timeout and reads
        # wait in select() in short slices instead.
        self.sock.settimeout(_WRITE_TIMEOUT_S)

    def recv(self) -> bytes | None:
        """Bytes; ``None`` when nothing arrived this slice; ``b""`` at end of stream."""
        try:
            ready, _, _ = select.select([self.sock], [], [], _READ_SLICE_S)
            if not ready:
                return None
            return self.sock.recv(4096)
        except (OSError, ValueError):
            return b""                     # reset, or closed under us by stop()

    def send(self, data: bytes) -> None:
        self.sock.sendall(data)

    def close(self) -> None:
        try:
            self.sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        self.sock.close()


class _SerialLink:
    def __init__(self, url: str, baud: int) -> None:
        self.port: SerialPort = open_serial(url, baud)

    def recv(self) -> bytes | None:
        # Poll in_waiting: a pyserial read() with no timeout set would block forever.
        try:
            waiting = self.port.in_waiting
            if waiting:
                return self.port.read(waiting) or None
        except (OSError, ValueError):
            return b""                     # unplugged or closed: a drop
        time.sleep(0.02)
        return None

    def send(self, data: bytes) -> None:
        self.port.write(data)

    def close(self) -> None:
        try:
            self.port.close()
        except (OSError, ValueError):
            pass


def url_baud(endpoint: str) -> int:
    """A serial URL's ``?baud=`` (``DEFAULT_SERIAL_BAUD`` when it has none)."""
    query = parse_qs(urlparse(endpoint).query)
    try:
        return int(query.get("baud", [str(DEFAULT_SERIAL_BAUD)])[0])
    except ValueError:
        raise UsageError(f"console endpoint {endpoint!r} has a bad ?baud=") from None


def _link_opener(endpoint: str, connect_timeout: float,
                 baud: int | None = None) -> Callable[[], _TcpLink | _SerialLink]:
    """How to dial ``endpoint``. ``baud`` overrides a serial URL's ``?baud=``."""
    parsed = urlparse(endpoint)
    if parsed.scheme == "tcp":
        if not parsed.hostname or not parsed.port:
            raise UsageError(f"console endpoint {endpoint!r} needs a host and a port")
        host, port = parsed.hostname, parsed.port
        return lambda: _TcpLink(host, port, connect_timeout)
    rate = baud or url_baud(endpoint)
    bare = endpoint.split("?", 1)[0]
    return lambda: _SerialLink(bare, rate)


# --- subscriber streams ------------------------------------------------------------


class ConsoleSubscription:
    """One reader's view of a console (implements ``core.services.ConsoleStream``).

    ``read(timeout)`` returns everything buffered so far, ``b""`` on timeout or
    once closed (check ``closed``). ``write`` goes to the board through the one
    shared connection.
    """

    def __init__(self, upstream: _Upstream, limit: int) -> None:
        self.name = upstream.name
        self.board_id = upstream.board_id
        self.dropped = 0
        self._up = upstream
        self._limit = limit
        self._buf = bytearray()
        self._cond = threading.Condition()
        self._closed = False

    @property
    def closed(self) -> bool:
        return self._closed

    @property
    def state(self) -> str:
        return self._up.state

    def _feed(self, data: bytes) -> None:
        with self._cond:
            if self._closed:
                return
            self._buf += data
            excess = len(self._buf) - self._limit
            if excess > 0:
                del self._buf[:excess]
                self.dropped += excess
            self._cond.notify_all()

    def _end(self) -> None:
        with self._cond:
            self._closed = True
            self._cond.notify_all()

    def read(self, timeout: float | None = None) -> bytes:
        with self._cond:
            self._cond.wait_for(lambda: self._buf or self._closed, timeout)
            out = bytes(self._buf)
            self._buf.clear()
            return out

    def write(self, data: bytes) -> None:
        if self._closed:
            raise UsageError(f"console {self.name!r} subscription is closed")
        self._up.write(data)

    def close(self) -> None:
        """Stop reading. Unread bytes are discarded; ``read`` then returns ``b""``."""
        if self._closed:
            return
        with self._cond:
            self._buf.clear()
        self._end()
        self._up.detach(self)

    def __enter__(self) -> ConsoleSubscription:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


# --- one board console -------------------------------------------------------------


class _Upstream:
    """The single connection to one board console, and its reader thread."""

    def __init__(self, broker: ConsoleBroker, board_id: str, name: str, endpoint: str,
                 pace_s: float = 0.0) -> None:
        self.broker = broker
        self.pace_s = pace_s          # > 0: one byte per pace_s, from the writer thread
        self.write_dropped = 0        # paced bytes discarded because the link went down
        self._wq: queue.Queue[bytes] = queue.Queue()
        self._writer: threading.Thread | None = None
        self.board_id = board_id
        self.name = name
        self.endpoint = endpoint
        self.state = "connecting"
        self.connects = 0             # successful upstream connections (tests, diagnostics)
        self.history = bytearray()
        self._opener = _link_opener(endpoint, broker.connect_timeout,
                                    broker._rates.get((board_id, name)))
        self._reopen_detail = ""
        self._subs: set[ConsoleSubscription] = set()
        self._exports: set[_Export] = set()
        self._lock = threading.RLock()
        self._wlock = threading.Lock()
        self._link: _TcpLink | _SerialLink | None = None
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._paused = threading.Event()
        self._pause_detail = ""
        self.lease_paused = ""        # QUIET-POLL: the holder while re-dials wait for the lease
        self._backoff = broker.backoff[0]
        self._line = bytearray()
        self._line_at = 0.0
        self._thread = threading.Thread(target=self._run, daemon=True,
                                        name=f"console-{board_id}-{name}")

    # -- holders ------------------------------------------------------------------

    def start(self) -> None:
        self._thread.start()

    def attach(self, replay: bool, limit: int | None = None) -> ConsoleSubscription:
        """A new subscriber; ``replay`` feeds it the scrollback (its last ``limit`` bytes)."""
        sub = ConsoleSubscription(self, self.broker.buffer_bytes)
        with self._lock:
            if replay and self.history:
                sub._feed(bytes(self.history if limit is None else self.history[-limit:]))
            self._subs.add(sub)
        return sub

    def detach(self, sub: ConsoleSubscription) -> None:
        with self._lock:
            self._subs.discard(sub)
        self._maybe_release()

    def add_export(self, exp: _Export) -> None:
        with self._lock:
            self._exports.add(exp)

    def remove_export(self, exp: _Export) -> None:
        with self._lock:
            self._exports.discard(exp)
        self._maybe_release()

    def _maybe_release(self) -> None:
        self.broker._release(self)

    # -- control ------------------------------------------------------------------

    def kick(self) -> None:
        """Retry now, from the first backoff step."""
        self._backoff = self.broker.backoff[0]
        self._wake.set()

    def pause(self, detail: str) -> None:
        """Close the board connection and do not re-dial until ``resume()``."""
        self._pause_detail = detail
        self._paused.set()
        with self._lock:
            link = self._link
        if link is not None:
            link.close()                        # the reader reports "down" with the detail
        else:
            self._set_state("down", detail)
        self._wake.set()

    def hold(self, detail: str) -> None:
        """Stay paused, and say why (a failed swap leaves the console down)."""
        if self._paused.is_set():
            self._pause_detail = detail
            self._set_state("down", detail)

    def forget(self) -> None:
        """Drop the scrollback: it is the previous design's output (a verified swap)."""
        with self._lock:
            self.history.clear()
            self._line.clear()

    def resume(self) -> None:
        self._paused.clear()
        self.kick()

    def reopen(self, baud: int | None, detail: str) -> None:
        """Dial again now with a new serial rate (``None``: the URL's own)."""
        self._opener = _link_opener(self.endpoint, self.broker.connect_timeout, baud)
        with self._lock:
            link = self._link
        if link is not None:
            self._reopen_detail = detail
            link.close()                        # the reader sees end of stream and re-dials
        self.kick()                             # not connected: the next dial uses the new rate

    @property
    def paused(self) -> bool:
        return self._paused.is_set()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        with self._lock:
            link = self._link
            subs = list(self._subs)
            exports = list(self._exports)
            self._subs.clear()
            self._exports.clear()
        if link is not None:
            link.close()
        for exp in exports:
            exp.close(release=False)
        for sub in subs:
            sub._end()
        if self._thread.is_alive() and threading.current_thread() is not self._thread:
            self._thread.join(timeout=2.0)

    def write(self, data: bytes) -> None:
        if self.pace_s > 0:
            self._write_paced(data)
            return
        with self._wlock:
            link = self._link
            if link is None or self.state != "up":
                raise self._not_up()
            try:
                link.send(data)
            except OSError as exc:
                link.close()                # the reader sees end of stream and re-dials
                raise UnreachableError(f"console {self.name!r} write failed: {exc}") from exc

    def _not_up(self) -> UnreachableError:
        return UnreachableError(
            f"console {self.name!r} on {self.board_id} is not connected (state {self.state})",
            hint="it reconnects by itself; retry when console.state is 'up'",
        )

    def _write_paced(self, data: bytes) -> None:
        if self._link is None or self.state != "up":
            raise self._not_up()
        self._wq.put(bytes(data))
        with self._lock:
            if self._writer is None or not self._writer.is_alive():
                self._writer = threading.Thread(target=self._write_loop, daemon=True,
                                                name=f"console-{self.board_id}-{self.name}-tx")
                self._writer.start()

    def _write_loop(self) -> None:
        """One byte per ``pace_s``. A link that drops discards the rest of the queue."""
        while not self._stop.is_set():
            try:
                chunk = self._wq.get(timeout=0.2)
            except queue.Empty:
                continue
            for i in range(len(chunk)):
                with self._wlock:
                    link = self._link
                    ok = link is not None and self.state == "up"
                    if ok:
                        try:
                            link.send(chunk[i:i + 1])
                        except OSError:
                            link.close()        # the reader sees end of stream and re-dials
                            ok = False
                if not ok:
                    self._drop_queued(len(chunk) - i)
                    break
                time.sleep(self.pace_s)

    def _drop_queued(self, first: int) -> None:
        dropped = first
        while True:
            try:
                dropped += len(self._wq.get_nowait())
            except queue.Empty:
                break
        self.write_dropped += dropped
        if self._stop.is_set():
            # The last reader left (or the board closed) while paced input was queued: an
            # ordinary close, not a fault. "link down" here sent field reports astray (Q2).
            log.info("console %s on %s: closed with %d unsent byte(s)",
                     self.name, self.board_id, dropped)
            return
        log.warning("console %s on %s: link down, %d unsent byte(s) dropped",
                    self.name, self.board_id, dropped)

    # -- reader thread ---------------------------------------------------------------

    def _set_state(self, state: str, detail: str = "") -> None:
        self.state = state
        self.broker._publish("console.state", self.board_id, {
            "name": self.name, "state": state, "detail": detail, "endpoint": self.endpoint})

    def _lease_wait(self) -> bool:
        """QUIET-POLL: True (after waiting) while the board's lease is someone else's: no dial.
        The wait ends on a lease change for the board (``kick``), ``stop``, or the recheck."""
        holder = self.broker._lease_elsewhere(self.board_id)
        if not holder:
            if self.lease_paused:
                self.lease_paused = ""
                self._backoff = self.broker.backoff[0]
            return False
        detail = f"paused: lease held by {holder}"
        if self.lease_paused != holder or self.state != PAUSED:
            self.lease_paused = holder
            self._set_state(PAUSED, detail)
        self._wake.wait(self.broker.lease_recheck_s)
        self._wake.clear()
        return True

    def _run(self) -> None:
        first, cap = self.broker.backoff
        while not self._stop.is_set():
            if self._paused.is_set():
                self._wake.wait()              # until resume() or stop()
                self._wake.clear()
                continue
            if self._lease_wait():
                continue
            self._set_state("connecting")
            try:
                link = self._opener()
            except (OSError, HarnessError) as exc:
                self._set_state("down", f"cannot open {self.endpoint}: {exc}")
                self._sleep_backoff(cap)
                continue
            with self._lock:
                if self._stop.is_set() or self._paused.is_set():
                    link.close()
                    continue
                self._link = link
            self.connects += 1
            self._set_state("up")
            connected_at = time.monotonic()
            received = 0
            while not self._stop.is_set() and not self._paused.is_set():
                data = link.recv()
                if data is None:
                    self._idle_flush()
                    continue
                if not data:
                    break
                received += len(data)
                self._deliver(data)
            with self._lock:
                self._link = None
            link.close()
            self._flush_partial()
            if self._stop.is_set():
                break
            if self._paused.is_set():
                self._set_state("down", self._pause_detail)
                continue
            if self._reopen_detail:                # set_baud: re-dial at once, no backoff
                self._reopen_detail = ""
                self._backoff = first
                continue
            lived = time.monotonic() - connected_at
            if received == 0 and lived < _REFUSAL_WINDOW_S:
                detail = ("the board closed the connection at once: another client probably "
                          "holds this port (the board serves one client per port)")
            else:
                detail = "connection dropped (a partition swap drops UART1 and SWO)"
                if lived > cap:
                    self._backoff = first      # it had been healthy: retry promptly
            self._set_state("down", detail)
            self._sleep_backoff(cap)
        self._set_state("closed")

    def _sleep_backoff(self, cap: float) -> None:
        delay = self._backoff
        self._backoff = min(self._backoff * 2, cap)
        self._wake.wait(delay)
        self._wake.clear()

    def _deliver(self, data: bytes) -> None:
        with self._lock:
            self.history += data
            excess = len(self.history) - self.broker.history_bytes
            if excess > 0:
                del self.history[:excess]
            subs = list(self._subs)
        for sub in subs:
            sub._feed(data)
        self._lines(data)

    # -- console.line ------------------------------------------------------------------

    def _lines(self, data: bytes) -> None:
        self._line += data
        self._line_at = time.monotonic()
        while True:
            nl = self._line.find(b"\n")
            if nl < 0:
                break
            raw = bytes(self._line[:nl])
            del self._line[:nl + 1]
            self._emit_line(raw, partial=False)
        if len(self._line) >= _LINE_FORCE_FLUSH:
            self._flush_partial()

    def _idle_flush(self) -> None:
        if self._line and time.monotonic() - self._line_at >= self.broker.line_idle_s:
            self._flush_partial()

    def _flush_partial(self) -> None:
        if self._line:
            raw = bytes(self._line)
            self._line.clear()
            self._emit_line(raw, partial=True)

    def _emit_line(self, raw: bytes, *, partial: bool) -> None:
        text = raw.decode("utf-8", errors="replace").rstrip("\r")
        data: dict[str, Any] = {"name": self.name, "text": text}
        if partial:
            data["partial"] = True
        self.broker._publish("console.line", self.board_id, data)


# --- TCP re-export -----------------------------------------------------------------


class _Export:
    """A 127.0.0.1 listener; every terminal that connects becomes a subscriber."""

    def __init__(self, upstream: _Upstream, port: int) -> None:
        self.upstream = upstream
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):            # Windows: no port stealing
            self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        else:                                                 # POSIX: rebind past TIME_WAIT
            self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            self.sock.bind(("127.0.0.1", port))
        except OSError as exc:
            self.sock.close()
            raise PortBoundError(
                f"cannot export console {upstream.name!r} on 127.0.0.1:{port}: {exc.strerror or exc}",
                hint="pick another port, or pass 0 for any free port",
            ) from exc
        self.sock.listen(8)
        self.sock.settimeout(_READ_SLICE_S)
        self.port = self.sock.getsockname()[1]
        self._stop = threading.Event()
        self._clients: list[tuple[socket.socket, ConsoleSubscription]] = []
        self._lock = threading.Lock()
        self._thread = threading.Thread(target=self._accept_loop, daemon=True,
                                        name=f"console-export-{self.port}")
        self._thread.start()

    @property
    def clients(self) -> int:
        with self._lock:
            return len(self._clients)

    def _accept_loop(self) -> None:
        while not self._stop.is_set():
            try:
                conn, _ = self.sock.accept()
            except TimeoutError:
                continue
            except OSError:
                break
            sub = self.upstream.attach(replay=True)
            with self._lock:
                self._clients.append((conn, sub))
            threading.Thread(target=self._to_terminal, args=(conn, sub), daemon=True).start()
            threading.Thread(target=self._from_terminal, args=(conn, sub), daemon=True).start()

    def _to_terminal(self, conn: socket.socket, sub: ConsoleSubscription) -> None:
        try:
            while not self._stop.is_set():
                data = sub.read(_READ_SLICE_S)
                if data:
                    conn.sendall(data)
                elif sub.closed:
                    break
        except OSError:
            pass
        finally:
            self._drop(conn, sub)

    def _from_terminal(self, conn: socket.socket, sub: ConsoleSubscription) -> None:
        try:
            while not self._stop.is_set():
                data = conn.recv(4096)
                if not data:
                    break
                try:
                    sub.write(data)
                except HarnessError as exc:
                    log.info("console export %d: dropped %d bytes: %s", self.port, len(data), exc)
        except OSError:
            pass
        finally:
            self._drop(conn, sub)

    def _drop(self, conn: socket.socket, sub: ConsoleSubscription) -> None:
        with self._lock:
            if (conn, sub) not in self._clients:
                return
            self._clients.remove((conn, sub))
        try:
            conn.close()
        finally:
            sub.close()

    def close(self, *, release: bool = True) -> None:
        self._stop.set()
        self.sock.close()
        with self._lock:
            clients = list(self._clients)
        for conn, sub in clients:
            self._drop(conn, sub)
        if threading.current_thread() is not self._thread:
            self._thread.join(timeout=2.0)
        if release:
            self.upstream.remove_export(self)


# --- the broker ----------------------------------------------------------------------


class ConsoleBroker:
    """Implements ``core.services.ConsoleBroker``.

    ``engine`` may be the Engine (its ``bus`` is used), an ``EventBus``, or
    ``None`` (no events; unit tests).
    """

    def __init__(self, engine: Any = None, *,
                 backoff: tuple[float, float] = (0.25, 5.0),
                 connect_timeout: float = 3.0,
                 history_bytes: int = 64 * 1024,
                 buffer_bytes: int = 4 * 1024 * 1024,
                 line_idle_s: float = 0.3,
                 baud_ttl_s: float = 3.0,
                 pty_replay_bytes: int = 4096,
                 pty_options: dict[str, Any] | None = None,
                 lease_recheck_s: float = LEASE_RECHECK_S) -> None:
        self.bus = _bus_of(engine)
        self.lease_recheck_s = lease_recheck_s
        self.aliases = dict(ALIASES)
        self.backoff = backoff
        self.connect_timeout = connect_timeout
        self.history_bytes = history_bytes
        self.buffer_bytes = buffer_bytes
        self.line_idle_s = line_idle_s
        self.baud_ttl_s = baud_ttl_s             # how long the pack's rate report is reused
        self.pty_replay_bytes = pty_replay_bytes  # scrollback a new PTY starts with
        self._pty_options = dict(pty_options or {})   # PtyManager keywords (tests: fast polls)
        self._lock = threading.RLock()
        self._ups: dict[tuple[str, str], _Upstream] = {}
        self._rates: dict[tuple[str, str], int] = {}          # serial rate overrides (set_baud)
        self._baud_cache: dict[str, tuple[float, dict[str, dict[str, Any]]]] = {}
        self._ptys: _pty.PtyManager | None = None
        self._unsubs: list[Callable[[], None]] = []
        if self.bus is not None:
            for topic, handler in (("deploy.started", self._on_swap_start),
                                   ("deploy.done", self._on_swap_done),
                                   ("deploy.failed", self._on_swap_failed),
                                   ("board.identity", self._on_identity),
                                   ("lease.state", self._on_lease_change),
                                   ("hub.event", self._on_lease_change)):
                self._unsubs.append(self.bus.subscribe(topic, handler))

    # -- protocol -------------------------------------------------------------------

    def names(self, session: BoardSession) -> list[str]:
        """Every console name, plus each alias whose target this board has."""
        endpoints = self._endpoints(session, required=False)
        extra = [a for a, t in self.aliases.items() if a not in endpoints and t in endpoints]
        return sorted([*endpoints, *extra])

    def endpoint(self, session: BoardSession, name: str) -> str:
        return self.resolve(session, name)[1]

    def resolve(self, session: BoardSession, name: str) -> tuple[str, str]:
        """(the name the broker keys it by, its endpoint URL).

        An alias maps to its target (``shell`` -> ``fpga_uart2``), and two names
        for one URL collapse to one (the first in sorted order): a port has ONE
        opener, whatever it is called.
        """
        endpoints = self._endpoints(session, required=True)
        if name not in endpoints and self.aliases.get(name) in endpoints:
            name = self.aliases[name]
        if name not in endpoints:
            known = ", ".join(self.names(session)) or "none"
            raise AbsentError(f"no console named {name!r}", hint=f"consoles: {known}")
        url = endpoints[name]
        return sorted(n for n, u in endpoints.items() if u == url)[0], url

    def _endpoints(self, session: BoardSession, *, required: bool) -> dict[str, str]:
        consoles = getattr(session, "consoles", None)
        if consoles is None:
            if required:
                raise UnavailableError("console_dut",
                                       "this board session has no console endpoints")
            return {}
        return dict(consoles.console_endpoints())

    def subscribe(self, session: BoardSession, name: str, *,
                  replay: bool = True, replay_bytes: int | None = None,
                  explicit: bool = True) -> ConsoleSubscription:
        """A new reader of console ``name``. ``replay`` first delivers the scrollback
        (only its last ``replay_bytes`` when given). ``explicit`` (a user opened it): refused
        ``HeldError`` while the board's lease is someone else's and no live connection exists
        (QUIET-POLL); ``explicit=False`` waits paused instead."""
        key, endpoint = self.resolve(session, name)
        if explicit:
            self._refuse_if_leased(session.candidate.board_id, key, name)
        with self._lock:
            return self._upstream(session.candidate.board_id, key, endpoint,
                                  _pace(session, key)).attach(replay, replay_bytes)

    def export_tcp(self, session: BoardSession, name: str, port: int = 0) -> int:
        """Serve console ``name`` on 127.0.0.1:``port`` (0 = any free port); returns the port.

        ``PortBoundError`` (exit 5) if the port is taken: an explicit port is
        never silently moved, because a terminal profile points at it.
        """
        key, endpoint = self.resolve(session, name)
        self._refuse_if_leased(session.candidate.board_id, key, name)
        with self._lock:
            up = self._upstream(session.candidate.board_id, key, endpoint, _pace(session, key))
            try:
                exp = _Export(up, port)
            except PortBoundError:
                self._release(up)
                raise
            up.add_export(exp)
        return exp.port

    def unexport(self, board_id: str, port: int) -> None:
        """Close one TCP export (its terminals are disconnected)."""
        with self._lock:
            ups = [u for (b, _), u in self._ups.items() if b == board_id]
        for up in ups:
            with up._lock:
                exps = [e for e in up._exports if e.port == port]
            for exp in exps:
                exp.close()

    def state(self, board_id: str, name: str) -> str:
        """``connecting | up | down``, or ``closed`` when nothing holds it open."""
        with self._lock:
            up = self._ups.get((board_id, name)) or self._ups.get(
                (board_id, self.aliases.get(name, "")))
        return up.state if up is not None else "closed"

    def close_all(self, board_id: str) -> None:
        """Close every console, PTY, subscriber and export of one board. Safe when none is open.

        The board's serial rate overrides go too: the next session starts at each
        URL's own rate."""
        ptys = self._ptys
        if ptys is not None:
            ptys.close_board(board_id)
        with self._lock:
            keys = [k for k in self._ups if k[0] == board_id]
            ups = [self._ups.pop(k) for k in keys]
            for k in [k for k in self._rates if k[0] == board_id]:
                del self._rates[k]
            self._baud_cache.pop(board_id, None)
        for up in ups:
            up.stop()

    def shutdown(self) -> None:
        """Close everything and stop listening for events (engine shutdown)."""
        with self._lock:
            boards = {b for b, _ in self._ups}
            ptys = self._ptys
        if ptys is not None:
            boards |= {p.board_id for p in ptys.ptys()}
        for board_id in boards:
            self.close_all(board_id)
        if ptys is not None:
            ptys.shutdown()
        for unsub in self._unsubs:
            unsub()
        self._unsubs.clear()

    # -- PTYs for screen (lane L2) ------------------------------------------------------

    def _pty_manager(self) -> _pty.PtyManager:
        with self._lock:
            if self._ptys is None:
                self._ptys = _pty.PtyManager(self._publish, self._on_pty_speed,
                                             **self._pty_options)
            return self._ptys

    def pty(self, session: BoardSession, name: str, *, explicit: bool = True) -> dict[str, Any]:
        """Console ``name``'s PTY, created if needed: ``{name, path, device, command, clients}``.

        ``path`` is ``/tmp/harness-manager-$USER/<board-slug>/<console>``, a symlink to
        ``device``; ``command`` is the ``screen`` line to attach with. Idempotent.
        ``UnavailableError`` on a system without PTYs (Windows), hinting at the TCP export.
        """
        if not _pty.supported():
            raise _pty.unavailable()
        key, endpoint = self.resolve(session, name)
        board_id = session.candidate.board_id
        if explicit and self.pty_info(board_id, key) is None:
            self._refuse_if_leased(board_id, key, name)     # QUIET-POLL (a new PTY only)
        row = self._rate_row(session, key, endpoint, live=False)
        serial = _broker_owned(row)
        port = self._pty_manager().open(
            session, key,
            lambda: self.subscribe(session, key, replay_bytes=self.pty_replay_bytes,
                                   explicit=False),
            kind="serial" if serial else "ethernet", baud=row["baud"] if serial else None)
        return self._pty_view(port)

    def pty_info(self, board_id: str, name: str) -> dict[str, Any] | None:
        """The PTY of console ``name`` (an alias resolves to its target), or None."""
        ptys = self._ptys
        if ptys is None:
            return None
        port = ptys.get(board_id, name) or ptys.get(board_id, self.aliases.get(name, ""))
        return self._pty_view(port) if port is not None else None

    def close_pty(self, board_id: str, name: str) -> bool:
        """Close one PTY (its terminal sees the line go away). False when there was none."""
        ptys = self._ptys
        if ptys is None:
            return False
        return ptys.close(board_id, name) or ptys.close(board_id, self.aliases.get(name, ""))

    def open_ptys(self) -> list[dict[str, Any]]:
        """Every open PTY: ``{board_id, name, path, device, clients, command}`` (lane OTA-D:
        a restarting daemon records them, and its successor opens the same paths)."""
        ptys = self._ptys
        if ptys is None:
            return []
        return [{"board_id": p.board_id, **self._pty_view(p)} for p in ptys.ptys()]

    def announce_ptys(self, text_for: Callable[[dict[str, Any]], str]) -> list[str]:
        """Write ``text_for(view)`` into every open PTY now (a restart notice). The paths
        written to."""
        ptys = self._ptys
        if ptys is None:
            return []
        done = []
        for port in ptys.ptys():
            if port.announce(text_for({"board_id": port.board_id, **self._pty_view(port)})):
                done.append(str(port.link))
        return done

    def _pty_view(self, port: _pty.ConsolePty) -> dict[str, Any]:
        rate = None
        if port.kind == "serial":
            try:
                key, endpoint = self.resolve(port.session, port.name)
                rate = self._serial_row(port.board_id, key, endpoint)["baud"]
            except HarnessError:
                rate = None
        return {**port.info(), "command": _pty.screen_command(port.link, rate)}

    def _on_pty_speed(self, port: _pty.ConsolePty, baud: int) -> None:
        """A client set a standard speed on the PTY (``screen <path> 57600``)."""
        try:
            key, endpoint = self.resolve(port.session, port.name)
        except HarnessError:
            return
        row = self._rate_row(port.session, key, endpoint, live=False)
        if not _broker_owned(row):
            log.debug("console %s of %s: the PTY is now at %d baud; ignored (%s)", key,
                      port.board_id, baud, row.get("reason") or "not a host serial port")
            return
        if not _pty.is_standard(row["baud"]):
            log.debug("console %s of %s: the PTY is now at %d baud; ignored (the console runs at "
                      "%s baud, set from the GUI)", key, port.board_id, baud, row["baud"])
            return
        if baud == row["baud"]:
            return
        log.info("console %s of %s: the PTY client set %d baud; reopening the port at it",
                 key, port.board_id, baud)
        self.set_baud(port.session, key, baud)

    # -- baud (lane L2) ---------------------------------------------------------------------

    def baud(self, session: BoardSession, name: str, *, live: bool = True,
             offline_reason: str = "") -> dict[str, Any]:
        """``{name, kind, baud, settable, reason, choices, source, ...}`` for console ``name``.

        ``source``: ``serial`` (the host port's rate), ``design`` (the loaded design
        fixes it), ``harness`` (the harness reports it) or ``unknown``. ``live=False``
        never asks the board: the pack's last report is used, or ``offline_reason``.
        """
        key, endpoint = self.resolve(session, name)
        return {"name": key, **self._rate_row(session, key, endpoint, live=live,
                                              offline_reason=offline_reason)}

    def consoles(self, session: BoardSession, *, live: bool = True,
                 offline_reason: str = "") -> list[dict[str, Any]]:
        """One row per name in ``names``: ``{name, kind, baud, settable, source, reason,
        pty, state}`` (``alias_of`` on an alias)."""
        board_id = session.candidate.board_id
        rows = []
        for name in self.names(session):
            key, endpoint = self.resolve(session, name)
            rate = self._rate_row(session, key, endpoint, live=live, offline_reason=offline_reason)
            port = self.pty_info(board_id, key)
            row: dict[str, Any] = {
                "name": name, "kind": rate["kind"], "baud": rate["baud"],
                "settable": rate["settable"], "source": rate["source"], "reason": rate["reason"],
                "pty": port["path"] if port else None, "state": self.state(board_id, key)}
            if key != name:
                row["alias_of"] = key
            rows.append(row)
        return rows

    def set_baud(self, session: BoardSession, name: str, baud: int) -> dict[str, Any]:
        """Change console ``name``'s rate; ``0`` goes back to its default. ``{name, baud, source}``.

        A serial console reopens its host port at the new rate. An Ethernet console
        asks the pack (``console_set_baud``); a console whose rate cannot be changed
        raises ``UnavailableError`` with the reason.
        """
        if isinstance(baud, bool) or not isinstance(baud, int) or not 0 <= baud <= MAX_BAUD:
            raise UsageError(f"baud must be a whole number from 0 to {MAX_BAUD}, not {baud!r}",
                             hint="0 goes back to the console's default rate")
        key, endpoint = self.resolve(session, name)
        board_id = session.candidate.board_id
        row = self._rate_row(session, key, endpoint, live=True)
        if _broker_owned(row):
            return self._set_serial(board_id, key, endpoint, baud)
        if not row["settable"]:
            raise UnavailableError(BAUD_CAPABILITY, row["reason"] or UNKNOWN_RATE)
        setter = getattr(getattr(session, "consoles", None), "console_set_baud", None)
        if not callable(setter):
            raise UnavailableError(BAUD_CAPABILITY,
                                   "the board pack cannot change this console's rate")
        new = _normal(setter(key, baud))
        with self._lock:
            self._baud_cache.pop(board_id, None)
        what = f"set to {new['baud']} baud" if baud else f"back to {new['baud']} baud"
        self._announce(board_id, key, endpoint, new["baud"], f"{key} {what}")
        return {"name": key, "baud": new["baud"], "source": new["source"],
                "mode": new.get("mode", "")}

    def _set_serial(self, board_id: str, key: str, endpoint: str, baud: int) -> dict[str, Any]:
        with self._lock:
            if baud:
                self._rates[(board_id, key)] = baud
            else:
                self._rates.pop((board_id, key), None)
            up = self._ups.get((board_id, key))
        rate = baud or url_baud(endpoint)
        detail = f"reopened at {rate} baud"
        if up is not None:
            up.reopen(baud or None, detail)
        ptys = self._ptys
        port = ptys.get(board_id, key) if ptys is not None else None
        if port is not None:
            port.set_preset(rate)
        self._announce(board_id, key, endpoint, rate, detail)
        return {"name": key, "baud": rate, "source": "serial"}

    def _announce(self, board_id: str, key: str, endpoint: str, baud: int | None,
                  detail: str) -> None:
        self._publish("console.state", board_id, {
            "name": key, "state": self.state(board_id, key), "detail": detail,
            "endpoint": endpoint, "baud": baud})

    def _serial_row(self, board_id: str, key: str, endpoint: str) -> dict[str, Any]:
        with self._lock:
            rate = self._rates.get((board_id, key)) or url_baud(endpoint)
        return {"kind": "serial", "baud": rate, "source": "serial", "settable": True,
                "reason": "", "choices": sorted({*SERIAL_CHOICES, rate}), "share": False}

    def _rate_row(self, session: BoardSession, key: str, endpoint: str, *, live: bool,
                  offline_reason: str = "") -> dict[str, Any]:
        info, why = self._pack_info(session, live=live, offline_reason=offline_reason)
        entry = info.get(key) if info else None
        if entry is not None and not (entry.get("kind") == "serial" and not entry.get("share")):
            return _normal(entry)
        if urlparse(endpoint).scheme == "tcp":
            return {"kind": "ethernet", "baud": None, "source": "unknown", "settable": False,
                    "reason": why or UNKNOWN_RATE, "choices": []}
        return self._serial_row(session.candidate.board_id, key, endpoint)

    def _pack_info(self, session: BoardSession, *, live: bool,
                   offline_reason: str = "") -> tuple[dict[str, dict[str, Any]] | None, str]:
        """(the pack's ``console_baud_info()``, why it is missing). Cached per board."""
        fn = getattr(getattr(session, "consoles", None), "console_baud_info", None)
        if not callable(fn):
            return {}, ""
        board_id = session.candidate.board_id
        with self._lock:
            cached = self._baud_cache.get(board_id)
        now = time.monotonic()
        if cached is not None and (not live or now - cached[0] < self.baud_ttl_s):
            return cached[1], ""
        if not live:
            return None, offline_reason or "the rate is read from the board when it is free"
        try:
            info = {str(k): dict(v) for k, v in (fn() or {}).items()}
        except HarnessError as exc:
            return {}, f"cannot read the rate: {exc}"
        with self._lock:
            self._baud_cache[board_id] = (now, info)
        return info, ""

    def _on_identity(self, event: Event) -> None:
        with self._lock:
            self._baud_cache.pop(event.board_id, None)

    # -- internals ------------------------------------------------------------------------

    def _upstream(self, board_id: str, name: str, endpoint: str,
                  pace_s: float = 0.0) -> _Upstream:
        """Get or start the one upstream. Call with ``self._lock`` held."""
        up = self._ups.get((board_id, name))
        if up is not None and up.endpoint == endpoint and not up._stop.is_set():
            up.pace_s = pace_s
            return up
        if up is not None:              # the board's endpoint moved: start over
            del self._ups[(board_id, name)]
            up.stop()
        new = _Upstream(self, board_id, name, endpoint, pace_s)
        self._ups[(board_id, name)] = new
        new.start()
        return new

    def _release(self, up: _Upstream) -> None:
        """Stop ``up`` if nothing holds it any more. Re-checked under the broker lock,
        so a subscriber attaching at the same moment keeps it alive."""
        with self._lock:
            with up._lock:
                idle = not up._subs and not up._exports
            if not idle or self._ups.get((up.board_id, up.name)) is not up:
                return
            del self._ups[(up.board_id, up.name)]
        up.stop()

    def _board_ups(self, board_id: str) -> list[_Upstream]:
        with self._lock:
            return [u for (b, _), u in self._ups.items() if b == board_id]

    def _on_swap_start(self, event: Event) -> None:
        # The swap decouples the partition: close before it starts (lead rule, and
        # 6931/6932 would drop anyway). Subscribers stay attached and wait.
        self._on_identity(event)                # the design, so its fixed rate, changes
        overlay = event.data.get("overlay", "")
        for up in self._board_ups(event.board_id):
            up.pause(f"closed for a partition swap{f' to {overlay}' if overlay else ''}")

    def _on_swap_done(self, event: Event) -> None:
        self._on_identity(event)
        for up in self._board_ups(event.board_id):
            if event.data.get("verified"):
                # A new subscriber must not be replayed the old design's output
                # (ILA mint findings 2026-09-24 #4).
                up.forget()
                up.resume()
            else:
                up.hold("not reconnected: the board did not verify the swap; "
                        "check what is loaded, then reconnect")

    def _on_swap_failed(self, event: Event) -> None:
        # A preflight refusal publishes only deploy.failed (stage "preflight"): the
        # consoles were never closed, so there is nothing to report. Otherwise stay down.
        stage = event.data.get("stage", "")
        reason = event.data.get("reason", "")
        if stage not in ("", "preflight"):
            self._on_identity(event)
        for up in self._board_ups(event.board_id):
            up.hold(f"not reconnected: the swap failed at {stage or 'an unknown stage'}"
                    f"{f' ({reason})' if reason else ''}; check what is loaded, then reconnect")

    # -- QUIET-POLL: someone else's lease ---------------------------------------------------

    #: ``board_id -> holder`` when the board's hub lease is someone else's, else "" (the
    #: daemon sets it from its lease service; None: no lease rule, as in the CLI in-process).
    lease_holder: Callable[[str], str] | None = None

    def _lease_elsewhere(self, board_id: str) -> str:
        fn = self.lease_holder
        if fn is None:
            return ""
        try:
            return str(fn(board_id) or "")
        except Exception:  # noqa: BLE001 - a lease that cannot be read never blocks a console
            log.debug("lease of %s for the consoles could not be read", board_id, exc_info=True)
            return ""

    def _refuse_if_leased(self, board_id: str, key: str, name: str) -> None:
        """An explicit open that needs a new board connection, while the lease is someone
        else's: refused, naming the holder. Joining a live connection is not refused."""
        with self._lock:
            up = self._ups.get((board_id, key))
        if up is not None and up.state == "up":
            return
        holder = self._lease_elsewhere(board_id)
        if holder:
            raise HeldError(f"console {name} of {board_id} is not opened: the hub lease is held "
                            f"by {holder}, and the board serves one client per console port",
                            holder=holder,
                            hint="ask for the board: `harness-manager lease request TARGET`")

    def _on_lease_change(self, event: Event) -> None:
        """The board's lease may have changed hands: consoles waiting for it look again now
        (on their own threads: nothing here reads the lease)."""
        for up in self._board_ups(event.board_id):
            if up.lease_paused:
                up.kick()

    def reconnect(self, board_id: str) -> None:
        """Re-dial every console of a board now (also ends a pause left by a failed swap)."""
        for up in self._board_ups(board_id):
            up.resume()

    def _publish(self, topic: str, board_id: str, data: dict[str, Any]) -> None:
        if self.bus is not None:
            self.bus.publish(Event(topic, board_id, data))


def _broker_owned(row: dict[str, Any]) -> bool:
    """A host serial port: the broker sets its rate by reopening it."""
    return row.get("kind") == "serial" and row.get("source") == "serial" and not row.get("share")


def _normal(entry: dict[str, Any]) -> dict[str, Any]:
    """A pack's rate row with every key the API promises."""
    baud = entry.get("baud")
    baud = baud if isinstance(baud, int) and not isinstance(baud, bool) and baud > 0 else None
    settable = entry.get("settable") is True
    choices = [c for c in entry.get("choices") or () if isinstance(c, int) and c > 0]
    out = {**entry, "kind": str(entry.get("kind") or "ethernet"), "baud": baud,
           "source": str(entry.get("source") or "unknown"), "settable": settable,
           "reason": "" if settable else str(entry.get("reason") or UNKNOWN_RATE),
           "choices": choices or ([baud] if baud else [])}
    return out


# How to attach a terminal to an export; one home for the phrase (CLI/GUI help).
EXPORT_HINT = "raw TCP: nc 127.0.0.1 {port}  |  socat -,raw,echo=0 tcp:127.0.0.1:{port}"
