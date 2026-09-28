"""QUIET-POLL: a single-client control port (6900) in front of a FakeShell, that counts.

The real harness's control port serves ONE client: while a connection is open every other
client is turned away (bare metal: accept then close; Linux: the kernel may reset it). The
FakeShells serve any number of clients at once, so they cannot show the etiquette. This
front can:

- ``SingleClientFront(shell)`` listens on an ephemeral 127.0.0.1 port and answers each
  request line with ``shell.handle_control`` (the FakeShell's own answers); a second client
  while one is connected is turned away at once, with a RESET (``turn_away="rst"``, what cost
  the soak its control call) or an accept-then-EOF (``"eof"``, bare metal).
- ``hold()`` / ``release()``: another client (the soak) holds the port: every connect is
  turned away until released.
- Every connection is recorded (``Conn``): who (the soak connects from ``SOAK_IP``,
  127.0.0.2; anything else is Harness Manager), when it opened, each request's op and when
  it was answered, and when it closed. ``turned_away`` records each refused connect: when,
  and whose.
- ``strict(reason)``: from now on ANY connection is a violation (the counting fake of the
  brief: "fails if any background connect happens while the lease is someone else's");
  ``lenient()`` ends it. ``violations`` lists them; ``assert_clean()`` fails the test.

SERIAL-6900 additions (both off by default, so QUIET-POLL's tests see what they always saw):

- ``reply_delay_s``: each request takes that long to answer, so a connection stays open long
  enough for a concurrent one to meet it (harnessd's card reads take tens of ms);
- ``close_lag_s``: the lagging close through an SSH forward. The server learns that a client
  closed only that long after it did (ssh forwarded the next channel's open before the old
  channel's EOF), so a connect that follows a close more closely than that is turned away.
"""

from __future__ import annotations

import json
import select
import socket
import struct
import threading
import time
from dataclasses import dataclass, field
from typing import Any

#: The soak's source address (any 127/8 address is loopback on Linux).
SOAK_IP = "127.0.0.2"


@dataclass
class Conn:
    opened: float
    who: str = "hm"
    requests: list[tuple[float, float, str]] = field(default_factory=list)  # (in, out, op)
    closed: float | None = None

    @property
    def held_s(self) -> float:
        return (self.closed if self.closed is not None else time.monotonic()) - self.opened

    @property
    def parked_s(self) -> float:
        """How long the connection stayed open with no request being answered: before the
        first request, between requests, and after the last answer until the close."""
        if not self.requests:
            return self.held_s
        busy = sum(out - t_in for t_in, out, _op in self.requests)
        return max(0.0, self.held_s - busy)


class SingleClientFront:
    def __init__(self, shell: Any, *, turn_away: str = "rst", reply_delay_s: float = 0.0,
                 close_lag_s: float = 0.0) -> None:
        self.shell = shell
        self.turn_away = turn_away
        self.reply_delay_s = reply_delay_s
        self.close_lag_s = close_lag_s
        self._srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._srv.bind(("127.0.0.1", 0))
        self._srv.listen(16)
        self._srv.settimeout(0.1)
        self.port = self._srv.getsockname()[1]
        self._mu = threading.Lock()
        self._active: Conn | None = None
        self._active_sock: socket.socket | None = None
        self._held = False
        self._strict = ""
        self.conns: list[Conn] = []
        self.turned_away: list[tuple[float, str]] = []      # (when, "hm" | "soak")
        self.violations: list[str] = []
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._accept, daemon=True, name="qp-front")
        self._thread.start()

    @property
    def endpoint(self) -> str:
        return f"127.0.0.1:{self.port}"

    # -- the soak, and the counting fake --------------------------------------------------

    def hold(self) -> None:
        with self._mu:
            self._held = True

    def release(self) -> None:
        with self._mu:
            self._held = False

    def strict(self, reason: str) -> None:
        with self._mu:
            self._strict = reason

    def lenient(self) -> None:
        with self._mu:
            self._strict = ""

    def assert_clean(self) -> None:
        assert not self.violations, "\n".join(self.violations)

    def hm_conns(self, since: float = 0.0) -> list[Conn]:
        with self._mu:
            return [c for c in self.conns if c.who == "hm" and c.opened >= since]

    def attempts(self, since: float = 0.0) -> int:
        """Connects Harness Manager made since ``since``: served or turned away."""
        with self._mu:
            return (sum(1 for c in self.conns if c.who == "hm" and c.opened >= since)
                    + sum(1 for t, who in self.turned_away if who == "hm" and t >= since))

    def refused(self, who: str, since: float = 0.0) -> list[float]:
        with self._mu:
            return [t for t, w in self.turned_away if w == who and t >= since]

    # -- the server ---------------------------------------------------------------------

    def _accept(self) -> None:
        while not self._stop.is_set():
            try:
                conn, addr = self._srv.accept()
            except TimeoutError:
                continue
            except OSError:
                return
            now = time.monotonic()
            who = "soak" if addr[0] == SOAK_IP else "hm"
            with self._mu:
                if self._strict and who == "hm":
                    self.violations.append(f"a connect at {now:.3f} while {self._strict}")
                if self._active is not None and not self.close_lag_s \
                        and _peer_gone(self._active_sock):
                    # The client closed before this connect arrived (its FIN came first); a
                    # real server has seen it by now, whatever this fake's threads did.
                    self._active.closed = self._active.closed or now
                    self._active, self._active_sock = None, None
                busy = self._held or self._active is not None
                if busy:
                    self.turned_away.append((now, who))
                else:
                    self._active = Conn(opened=now, who=who)
                    self._active_sock = conn
                    self.conns.append(self._active)
                    active = self._active
            if busy:
                self._refuse(conn)
                continue
            threading.Thread(target=self._serve, args=(conn, active), daemon=True).start()

    def _refuse(self, conn: socket.socket) -> None:
        if self.turn_away == "rst":
            conn.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
        conn.close()

    def _serve(self, conn: socket.socket, rec: Conn) -> None:
        buf = b""
        conn.settimeout(0.05)
        try:
            while not self._stop.is_set():
                try:
                    chunk = conn.recv(4096)
                except TimeoutError:
                    continue
                except OSError:
                    break
                if not chunk:
                    break
                buf += chunk
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    if not line.strip():
                        continue
                    t_in = time.monotonic()
                    try:
                        req = json.loads(line.decode("utf-8"))
                    except ValueError:
                        req = {}
                    op = str(req.get("op", "")) if isinstance(req, dict) else ""
                    if self.reply_delay_s:
                        time.sleep(self.reply_delay_s)
                    reply = self.shell.handle_control(
                        {k: v for k, v in req.items() if k != "soak"}, peer="127.0.0.1") \
                        if isinstance(req, dict) else {"ok": False, "err": "malformed"}
                    conn.sendall(json.dumps(reply, separators=(",", ":")).encode() + b"\n")
                    rec.requests.append((t_in, time.monotonic(), op))
        finally:
            with self._mu:
                rec.closed = rec.closed or time.monotonic()
                lag = self.close_lag_s
                if self._active is rec and not lag:
                    self._active, self._active_sock = None, None
            conn.close()
            if lag:                       # the server hears of the close only now (ssh -L)
                time.sleep(lag)
                with self._mu:
                    if self._active is rec:
                        self._active, self._active_sock = None, None

    def close(self) -> None:
        self._stop.set()
        self._srv.close()
        self._thread.join(timeout=2.0)


def _peer_gone(sock: socket.socket | None) -> bool:
    """The client end has closed (EOF or reset waiting to be read), without consuming data."""
    if sock is None:
        return True
    try:
        ready, _, _ = select.select([sock], [], [], 0)
        return bool(ready) and sock.recv(1, socket.MSG_PEEK) == b""
    except OSError:
        return True


def soak_call(port: int, timeout: float = 1.0) -> bool:
    """The soak's own control call: connect, one ping, close. False when turned away."""
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=timeout,
                                      source_address=(SOAK_IP, 0)) as s:
            s.sendall(b'{"op":"ping","soak":true}\n')
            s.settimeout(timeout)
            data = b""
            while b"\n" not in data:
                chunk = s.recv(4096)
                if not chunk:
                    return False
                data += chunk
            return json.loads(data.split(b"\n", 1)[0]).get("ok", False) is not False
    except OSError:
        return False
