"""A fake fpgahub for lane L1: the hub CLI's text replies, and TTY shares served over real TCP.

``FakeHub`` is a hub runner (``(argv, timeout=None) -> RunResult``), the seam
pyverify's ``LeaseClient`` and ``harness_manager_mps3.hub.HubClient`` call. It
answers ``fpgahub lease …`` and ``fpgahub share …`` with the exact lines the
fpgahub 0.3.0 CLI prints (fpgahub ``cli.py`` ``lease_*``/``share_*``):

- ``lease show``: ``held by H (user U, expires E)`` or ``not leased``;
- ``lease acquire``: ``granted token=T expires=E tier=interactive`` or
  ``queued position=N queue=interactive`` (``queue_first`` acquires queue first);
- ``lease heartbeat``: ``extended expires=E``; a wrong holder/token fails with
  ``holder or token does not match current lease``, a lapsed lease with
  ``no current lease for board`` (fpgahub ``lease.py`` ``heartbeat``);
- ``lease release``: ``released`` / ``no lease to release``; ``lease cancel``;
- ``share list``: the rich table (TTY path, TCP, Writer, Readers, Running);
- ``share start``: ``share <tty> → 0.0.0.0:<port>``; an existing share is
  returned as it is (``TtyShareManager.start_share``);
- ``share stop``: recorded in ``share_stops`` (tests assert it stays empty).

``FakeShareServer`` is ``tty_share.TtyShareBroker`` in miniature: one serial
fake (e.g. ``FakeMcc``) served on a TCP port; every byte read is broadcast to
every client; only the FIRST connected client's writes reach the TTY.
"""

from __future__ import annotations

import socket
import threading
import time
from collections.abc import Sequence
from typing import Any

from pyverify.lease import RunResult

EXPIRES = "2026-09-25T12:00:00+00:00"


class FakeLane:
    """An FPGA UART lane (e.g. the shell console on tty_02) as a serial fake: it prints
    ``banner`` once, then echoes what it is sent."""

    def __init__(self, banner: bytes = b"mps3-harness login: ") -> None:
        self._out = bytearray(banner)
        self.received = bytearray()

    @property
    def in_waiting(self) -> int:
        return len(self._out)

    def read(self, size: int = 1) -> bytes:
        data = bytes(self._out[:size])
        del self._out[:size]
        return data

    def write(self, data: bytes) -> int:
        self.received += data
        self._out += data
        return len(data)

    def read_until(self, expected: bytes = b"\n", size: int | None = None) -> bytes:
        return self.read(len(self._out))

    def reset_input_buffer(self) -> None:
        self._out.clear()

    def close(self) -> None:
        pass


class FakeShareServer:
    def __init__(self, port_like: Any, tty: str) -> None:
        self.tty = tty
        self._port = port_like
        self._lock = threading.Lock()          # the serial fake is not thread-safe
        self._clients: list[socket.socket] = []
        self._cmu = threading.Lock()
        self._stop = threading.Event()
        self._srv = socket.socket()
        self._srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._srv.bind(("127.0.0.1", 0))
        self._srv.listen(8)
        self.port = self._srv.getsockname()[1]
        self.dropped_writes = 0
        self.written = bytearray()
        threading.Thread(target=self._accept, daemon=True, name=f"share-{tty}").start()
        threading.Thread(target=self._pump, daemon=True, name=f"share-pump-{tty}").start()

    @property
    def readers(self) -> int:
        with self._cmu:
            return len(self._clients)

    @property
    def writer(self) -> str:
        with self._cmu:
            if not self._clients:
                return ""
            try:
                host, port = self._clients[0].getpeername()[:2]
            except OSError:
                return "?"
            return f"{host}:{port}"

    def _accept(self) -> None:
        while not self._stop.is_set():
            try:
                c, _ = self._srv.accept()
            except OSError:
                return
            with self._cmu:
                self._clients.append(c)
            threading.Thread(target=self._client, args=(c,), daemon=True).start()

    def _client(self, c: socket.socket) -> None:
        try:
            while not self._stop.is_set():
                data = c.recv(4096)
                if not data:
                    break
                with self._cmu:
                    is_writer = bool(self._clients) and self._clients[0] is c
                if not is_writer:
                    self.dropped_writes += len(data)
                    continue
                with self._lock:
                    self.written += data
                    self._port.write(data)
        except OSError:
            pass
        finally:
            with self._cmu:
                if c in self._clients:
                    self._clients.remove(c)
            c.close()

    def _pump(self) -> None:
        while not self._stop.is_set():
            with self._lock:
                try:
                    n = self._port.in_waiting
                    data = self._port.read(n) if n else b""
                except OSError:
                    data = b""
            if data:
                with self._cmu:
                    clients = list(self._clients)
                for c in clients:
                    try:
                        c.sendall(data)
                    except OSError:
                        pass
            else:
                time.sleep(0.005)

    def close(self) -> None:
        self._stop.set()
        for step in (lambda: self._srv.shutdown(socket.SHUT_RDWR), self._srv.close):
            try:
                step()
            except OSError:
                pass
        with self._cmu:
            clients, self._clients = self._clients, []
        for c in clients:
            try:
                c.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            c.close()


def _table(target: str, shares: list[FakeShareServer]) -> str:
    rows = [(s.tty, f"0.0.0.0:{s.port}", s.writer or "-", str(s.readers), "yes") for s in shares]
    head = ("TTY path", "TCP", "Writer", "Readers", "Running")
    widths = [max(len(head[i]), *(len(r[i]) for r in rows)) for i in range(5)]

    def line(cells: Sequence[str], sep: str) -> str:
        return sep + sep.join(f" {c.ljust(w)} " for c, w in zip(cells, widths, strict=True)) + sep

    top = "┏" + "┳".join("━" * (w + 2) for w in widths) + "┓"
    mid = "┡" + "╇".join("━" * (w + 2) for w in widths) + "┩"
    bot = "└" + "┴".join("─" * (w + 2) for w in widths) + "┘"
    body = [line(r, "│") for r in rows]
    return "\n".join([f"TTY shares ({target})".center(len(top)), top, line(head, "┃"), mid,
                      *body, bot]) + "\n"


class FakeHub:
    def __init__(self, target: str = "mps3_01_pl", *, user: str = "dam1n19") -> None:
        self.target = target
        self.user = user
        self.calls: list[list[str]] = []
        self.current: dict[str, Any] | None = None
        self.queue: list[str] = []
        self.queue_first = 0                 # acquires answered "queued" before a grant
        self.ttys: dict[str, Any] = {}       # tty path -> serial fake a share start can open
        self.shares: dict[str, FakeShareServer] = {}
        self.share_stops: list[list[str]] = []
        self.heartbeats = 0
        self.fail_with = ""                  # the next call fails with this stderr text
        self._tokens = 0

    # -- helpers for tests -----------------------------------------------------------------

    def add_tty(self, tty: str, port_like: Any, *, share: bool = False) -> None:
        self.ttys[tty] = port_like
        if share:
            self.shares[tty] = FakeShareServer(port_like, tty)

    def routes(self) -> dict[tuple[str, int], tuple[str, int]]:
        """What the hub reaches for each share (for ``FakeSsh.routes``)."""
        return {("127.0.0.1", s.port): ("127.0.0.1", s.port) for s in self.shares.values()}

    def expire(self) -> None:
        self.current = None

    def steal(self, holder: str = "someone-else") -> None:
        self.current = {"holder": holder, "user": "other", "token": "tok-other-0001",
                        "expires_at": EXPIRES}

    def close(self) -> None:
        for s in self.shares.values():
            s.close()

    # -- the runner ------------------------------------------------------------------------

    def __call__(self, argv: Sequence[str], timeout: float | None = None) -> RunResult:
        argv = list(argv)
        self.calls.append(argv)
        if self.fail_with:
            text, self.fail_with = self.fail_with, ""
            return RunResult(1, "", text)
        if argv[:1] != ["fpgahub"] or len(argv) < 3:
            return RunResult(2, "", f"Usage: fpgahub [OPTIONS] COMMAND (got {argv})")
        group, verb, rest = argv[1], argv[2], argv[3:]
        name = rest[0] if rest else ""
        if name != self.target:
            return RunResult(1, "", f"HTTP 404: no such board: {name!r}; configured: {self.target}")
        opts = self._opts(rest[1:])
        if group == "lease":
            return getattr(self, f"_lease_{verb}")(opts)
        if group == "share":
            return getattr(self, f"_share_{verb}")(argv, rest[1:], opts)
        return RunResult(2, "", f"no such command {group}")

    @staticmethod
    def _opts(words: list[str]) -> dict[str, str]:
        out: dict[str, str] = {}
        it = iter(range(len(words)))
        for i in it:
            w = words[i]
            if w.startswith("--") and i + 1 < len(words):
                out[w[2:]] = words[i + 1]
                next(it, None)
        return out

    def _lease_show(self, _o: dict[str, str]) -> RunResult:
        if self.current:
            c = self.current
            return RunResult(0, f"held by {c['holder']} (user {c['user']}, expires {c['expires_at']})\n", "")
        return RunResult(0, "not leased\n", "")

    def _lease_acquire(self, o: dict[str, str]) -> RunResult:
        holder = o.get("holder", "")
        if self.current and self.current["holder"] != holder or self.queue_first > 0:
            if self.queue_first > 0:
                self.queue_first -= 1
            if holder not in self.queue:
                self.queue.append(holder)
            return RunResult(0, f"queued position={self.queue.index(holder) + 1} queue=interactive\n", "")
        if holder in self.queue:
            self.queue.remove(holder)
        self._tokens += 1
        token = self.current["token"] if self.current else f"tok-{self._tokens:04d}-abcdef"
        self.current = {"holder": holder, "user": self.user, "token": token, "expires_at": EXPIRES,
                        "ttl": int(o.get("ttl", "3600"))}
        return RunResult(0, f"granted token={token} expires={EXPIRES} tier=interactive\n", "")

    def _lease_heartbeat(self, o: dict[str, str]) -> RunResult:
        if self.current is None:
            return RunResult(1, "", "HTTP 409: no current lease for board")
        if self.current["holder"] != o.get("holder") or self.current["token"] != o.get("token"):
            return RunResult(1, "", "HTTP 409: holder or token does not match current lease")
        self.heartbeats += 1
        return RunResult(0, f"extended expires={EXPIRES}\n", "")

    def _lease_release(self, o: dict[str, str]) -> RunResult:
        c = self.current
        if c and c["holder"] == o.get("holder") and c["token"] == o.get("token"):
            self.current = None
            return RunResult(0, "released\n", "")
        return RunResult(0, "no lease to release\n", "")

    def _lease_cancel(self, o: dict[str, str]) -> RunResult:
        holder = o.get("holder", "")
        if holder in self.queue:
            self.queue.remove(holder)
            return RunResult(0, f"cancelled board={self.target}\n", "")
        return RunResult(0, f"no matching wait to cancel board={self.target}\n", "")

    def _share_list(self, _argv: list[str], _rest: list[str], _o: dict[str, str]) -> RunResult:
        if not self.shares:
            return RunResult(0, f"no active shares for {self.target}\n", "")
        return RunResult(0, _table(self.target, list(self.shares.values())), "")

    def _share_start(self, _argv: list[str], rest: list[str], _o: dict[str, str]) -> RunResult:
        ttys = [w for w in rest if w.startswith("/dev/")]
        lines = []
        for tty in ttys:
            if tty not in self.shares:
                if tty not in self.ttys:
                    return RunResult(1, "", f"HTTP 500: could not open port {tty}: no such device")
                self.shares[tty] = FakeShareServer(self.ttys[tty], tty)
            lines.append(f"share {tty} → 0.0.0.0:{self.shares[tty].port}")
        return RunResult(0, "\n".join(lines) + "\n", "")

    def _share_stop(self, argv: list[str], _rest: list[str], _o: dict[str, str]) -> RunResult:
        self.share_stops.append(argv)
        return RunResult(0, f"all shares stopped for {self.target}\n", "")
