"""A serial port over raw TCP: the ``tcp://`` serial scheme (lane L1).

Importing this module registers ``tcp`` with ``harness_manager.core.transport``,
so ``open_serial("tcp://127.0.0.1:12000")`` returns a ``SerialPort`` whose bytes
travel over one TCP connection. What it is for: an fpgahub TTY share. The hub
serves a board's USB serial port (the MPS3 MCC on ``tty_00``, the FPGA UART
lanes on ``tty_01..03``) as a raw TCP byte stream (``fpgahub share start``;
fpgahub ``tty_share.TtyShareBroker``), and srv03335 reaches that stream through
an SSH forward. The MCC adapter and the console broker then drive it exactly as
they drive a pyserial port.

Three rules, each from the share's behaviour:

- **Every write goes out at once, unmerged.** The MCC drops characters that
  arrive less than ~50 ms apart, so its adapter paces one character per write
  (``mcc.MccTiming.pace_s``). This port sets ``TCP_NODELAY`` and sends each
  ``write`` as it comes, so Nagle cannot coalesce two paced characters into one
  segment. It never buffers or reorders writes.
- **The baud is not ours to set.** The share opened the TTY at the rate it was
  started with (``--baud``, 115200 for the MCC). ``baudrate`` reports that rate
  and refuses a different one; ``baud_settable`` is False and ``baud_reason``
  says why, so a baud control can say "set by the hub share (115200)".
- **First writer wins.** The share broadcasts every byte it reads to every
  client, but only the FIRST connected client's writes reach the TTY; later
  clients' writes are dropped silently (``TtyShareBroker._handle_client``).
  A port opened with ``read_only=True`` (the caller knows another client holds
  the write slot) raises ``OSError`` on ``write`` instead of letting a command
  vanish.

Reads follow pyserial's configured-timeout behaviour (``READ_TIMEOUT_S``), and
a connection the far side closed is an ``OSError`` once the buffered bytes are
read, like an unplugged pyserial port. ``in_waiting`` never blocks.
"""

from __future__ import annotations

import select
import socket
import threading
import time
from dataclasses import dataclass
from urllib.parse import urlparse

from harness_manager.core.errors import UnavailableError, UnreachableError, UsageError
from harness_manager.core.transport import SerialPort, register_serial_scheme

TCP_SCHEME = "tcp"
READ_TIMEOUT_S = 0.1          # the same as transports.direct: drivers poll in_waiting
CONNECT_TIMEOUT_S = 5.0
DEFAULT_SHARE_BAUD = 115200   # fpgahub `share start --baud` default; the MCC's rate

#: The capability name a refused baud change is reported under (L2's baud API).
BAUD_CAPABILITY = "console_baud"


def share_baud_reason(baud: int | None) -> str:
    """The one sentence a baud control shows for a port whose rate the hub sets."""
    return (f"set by the hub share ({baud})" if baud else "set by the hub share") + \
        "; restart the share with --baud to change it"


@dataclass(frozen=True)
class BaudInfo:
    """What a baud control needs to know about a console endpoint (lane L2's API)."""

    baud: int | None
    settable: bool
    reason: str
    source: str = "serial"     # docs/API.md: serial | design | harness | unknown


# Endpoints known to be hub shares, so a baud control can tell one from a shell
# Ethernet console (both are tcp://). Keyed by the endpoint URL without a query.
_SHARE_BAUDS: dict[str, int | None] = {}
_SHARE_LOCK = threading.Lock()


def mark_share(url: str, baud: int | None) -> None:
    """Record that ``url`` is a hub share running at ``baud`` (None: not known)."""
    with _SHARE_LOCK:
        _SHARE_BAUDS[url.split("?", 1)[0]] = baud


def baud_info(url: str) -> BaudInfo | None:
    """The baud facts for a serial URL served by a hub share; None for anything else.

    ``hub://`` URLs are always shares. A ``tcp://`` URL is a share only when
    something marked it (``mark_share``): a shell's Ethernet UART console is a
    ``tcp://`` endpoint too, and its rate is the loaded design's, not the hub's.
    """
    bare = url.split("?", 1)[0]
    scheme = urlparse(bare).scheme
    with _SHARE_LOCK:
        known = bare in _SHARE_BAUDS
        baud = _SHARE_BAUDS.get(bare)
    if scheme == "hub" and not known:
        baud = DEFAULT_SHARE_BAUD
        known = True
    if not known:
        return None
    return BaudInfo(baud=baud, settable=False, reason=share_baud_reason(baud))


def parse_host_port(address: str) -> tuple[str, int]:
    """``host:port`` or ``[v6]:port`` -> (host, port). ``UsageError`` when malformed."""
    host, sep, port = address.rpartition(":")
    if not sep or not host or not port.isdigit():
        raise UsageError(f"tcp serial address {address!r} is not host:port",
                         hint="e.g. tcp://127.0.0.1:12000 (an fpgahub share through an SSH forward)")
    value = int(port)
    if not 0 < value < 65536:
        raise UsageError(f"tcp serial address {address!r}: port {value} is out of range")
    return host.strip("[]"), value


class TcpSerialPort:
    """``core.transport.SerialPort`` over one TCP connection (an fpgahub TTY share)."""

    def __init__(self, host: str, port: int, *, baud: int | None = DEFAULT_SHARE_BAUD,
                 read_only: bool = False, read_only_reason: str = "",
                 connect_timeout: float = CONNECT_TIMEOUT_S,
                 read_timeout: float = READ_TIMEOUT_S, label: str = "") -> None:
        self.host = host
        self.port = port
        self.label = label or f"tcp://{host}:{port}"
        self.timeout = read_timeout
        self.read_only = read_only
        self.read_only_reason = read_only_reason
        self._baud = baud
        self._buf = bytearray()
        self._eof = False
        self._closed = False
        self._lock = threading.Lock()
        try:
            self._sock = socket.create_connection((host, port), timeout=connect_timeout)
        except OSError as exc:
            raise UnreachableError(
                f"cannot connect to the serial stream at {self.label}: {exc}",
                hint="is the hub share running and the SSH tunnel up? "
                     "(`harness-manager share list TARGET`)") from exc
        self._sock.setblocking(False)
        # Each paced MCC character must leave as its own segment (module docstring).
        self._sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)

    # -- baud ---------------------------------------------------------------------------

    @property
    def baudrate(self) -> int | None:
        return self._baud

    @baudrate.setter
    def baudrate(self, value: int) -> None:
        if value == self._baud:
            return
        raise UnavailableError(BAUD_CAPABILITY, share_baud_reason(self._baud))

    baud_settable = False

    @property
    def baud_reason(self) -> str:
        return share_baud_reason(self._baud)

    # -- reads --------------------------------------------------------------------------

    def _pull(self, wait_s: float) -> None:
        """Move whatever the socket has into the buffer, waiting up to ``wait_s`` for some."""
        if self._eof or self._closed:
            return
        try:
            ready, _, _ = select.select([self._sock], [], [], max(0.0, wait_s))
        except (OSError, ValueError):
            self._eof = True
            return
        if not ready:
            return
        while True:
            try:
                chunk = self._sock.recv(65536)
            except BlockingIOError:
                return
            except OSError:
                self._eof = True
                return
            if not chunk:
                self._eof = True
                return
            self._buf += chunk
            if len(chunk) < 65536:
                return

    def _dead(self) -> OSError:
        what = "closed" if self._closed else "was closed by the far side"
        return OSError(f"the serial stream {self.label} {what}")

    @property
    def in_waiting(self) -> int:
        with self._lock:
            if self._closed:
                raise self._dead()
            self._pull(0.0)
            if not self._buf and self._eof:
                raise self._dead()
            return len(self._buf)

    def read(self, size: int = 1) -> bytes:
        deadline = time.monotonic() + (self.timeout or 0.0)
        with self._lock:
            if self._closed:
                raise self._dead()
            while len(self._buf) < size and not self._eof:
                left = deadline - time.monotonic()
                if left <= 0:
                    break
                self._pull(left)
            if not self._buf and self._eof:
                raise self._dead()
            out = bytes(self._buf[:size])
            del self._buf[:size]
            return out

    def read_until(self, expected: bytes = b"\n", size: int | None = None) -> bytes:
        deadline = time.monotonic() + (self.timeout or 0.0)
        with self._lock:
            if self._closed:
                raise self._dead()
            while True:
                idx = self._buf.find(expected)
                if idx >= 0:
                    end = idx + len(expected)
                    break
                if size is not None and len(self._buf) >= size:
                    end = size
                    break
                left = deadline - time.monotonic()
                if left <= 0 or self._eof:
                    end = len(self._buf)
                    break
                self._pull(left)
            if size is not None:
                end = min(end, size)
            if end == 0 and self._eof:
                raise self._dead()
            out = bytes(self._buf[:end])
            del self._buf[:end]
            return out

    def reset_input_buffer(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._pull(0.0)
            self._buf.clear()

    # -- writes -------------------------------------------------------------------------

    def write(self, data: bytes) -> int:
        if self._closed:
            raise self._dead()
        if self.read_only:
            raise OSError(f"{self.label} is read-only: {self.read_only_reason or 'another client holds the write slot'}")
        view = memoryview(bytes(data))
        sent = 0
        # Non-blocking socket: wait for room rather than spin; a stalled peer is a timeout.
        deadline = time.monotonic() + 5.0
        while sent < len(view):
            try:
                sent += self._sock.send(view[sent:])
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise OSError(f"write to {self.label} timed out") from None
                select.select([], [self._sock], [], 0.1)
            except OSError as exc:
                self._eof = True
                raise OSError(f"write to {self.label} failed: {exc}") from exc
        return sent

    def flush(self) -> None:
        """Writes are never held back (module docstring), so there is nothing to flush."""

    # -- lifecycle ----------------------------------------------------------------------

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
        try:
            self._sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        self._sock.close()

    @property
    def is_open(self) -> bool:
        return not self._closed

    def __enter__(self) -> TcpSerialPort:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def __repr__(self) -> str:
        return f"TcpSerialPort({self.label!r}, baud={self._baud})"


def open_tcp_serial(address: str, baud: int = DEFAULT_SHARE_BAUD) -> SerialPort:
    """The ``tcp://`` opener. ``baud`` is recorded, not applied: the share sets the rate.

    A URL marked as a share (``mark_share``) with a known rate refuses a
    different one, so "open the MCC at 9600" cannot silently run at 115200.
    """
    host, port = parse_host_port(address)
    url = f"{TCP_SCHEME}://{address}"
    info = baud_info(url)
    if info is not None and info.baud and baud and baud != info.baud:
        raise UnavailableError(BAUD_CAPABILITY, info.reason)
    return TcpSerialPort(host, port, baud=(info.baud if info is not None else baud) or baud)


register_serial_scheme(TCP_SCHEME, open_tcp_serial)
