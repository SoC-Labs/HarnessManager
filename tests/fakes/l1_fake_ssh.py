"""A fake ``ssh`` for the tunnel tests (lane L1): real local TCP forwarders, no network.

``FakeSsh`` is a ``tunnel.Launcher``. Given the argv the tunnel builds, it binds
each ``-L 127.0.0.1:L:RHOST:RPORT`` on 127.0.0.1 (real sockets, so the tunnel's
readiness check sees them in /proc/net/tcp) and relays every accepted
connection to ``routes[(RHOST, RPORT)]``: the address a real hub would have
reached. Tests map the lab board's real ports (``192.168.10.101:6900`` ...) to a
``VirtualMps3`` on ephemeral ports, so the pack runs with its real defaults.

Modelled from OpenSSH's behaviour, because the tunnel code depends on each:

- ``ExitOnForwardFailure=yes``: a ``-L`` whose local port is taken ends the
  process with status 255 and ssh's own message ("cannot listen to port").
- A forward whose far end refuses (no route, or the board is off) still
  ACCEPTS locally, then closes the connection, and ssh logs
  "channel N: open failed: connect failed: Connection refused". That is how an
  ``ssh -L`` looks to a client, and why a dead board through a tunnel reads as
  "accepted then closed".
- ``BatchMode=yes`` with no usable key: status 255, "Permission denied (publickey)."
- ``drop()``: the connection to the hub dies (ServerAlive gave up): status 255,
  every forward and relayed connection closed.

``ssh_g(argv)`` stands in for ``ssh -G`` (config evaluation); ``config_output``
is what it prints, so a test can give the host LocalForward lines.
"""

from __future__ import annotations

import itertools
import re
import socket
import threading
from collections.abc import Sequence

_L_SPEC = re.compile(r"^(?P<bind>[^:]+):(?P<lport>\d+):(?P<rhost>\[[^\]]+\]|[^:]+):(?P<rport>\d+)$")
_PIDS = itertools.count(40000)


def _pipe(src: socket.socket, dst: socket.socket, done: threading.Event) -> None:
    try:
        while not done.is_set():
            data = src.recv(65536)
            if not data:
                break
            dst.sendall(data)
    except OSError:
        pass
    finally:
        try:
            dst.shutdown(socket.SHUT_WR)
        except OSError:
            pass


class FakeSshProcess:
    def __init__(self, owner: FakeSsh, argv: Sequence[str]) -> None:
        self.owner = owner
        self.argv = list(argv)
        self.pid = next(_PIDS)
        self.returncode: int | None = None
        self.stderr_tail = ""
        self.host = self.argv[-1]
        self.forwards: list[tuple[int, str, int]] = []
        self._listeners: list[socket.socket] = []
        self._conns: list[socket.socket] = []
        self._mu = threading.Lock()
        self._stopped = threading.Event()
        self.channel_failures = 0
        self.accepted = 0
        args = self.argv
        for i, a in enumerate(args):
            if a == "-L" and i + 1 < len(args):
                m = _L_SPEC.match(args[i + 1])
                if not m:
                    self._exit(255, f"Bad local forwarding specification '{args[i + 1]}'")
                    return
                self.forwards.append((int(m.group("lport")), m.group("rhost").strip("[]"),
                                      int(m.group("rport"))))
        if owner.fail == "auth":
            self._exit(255, f"{owner.user}@{self.host}: Permission denied (publickey).")
            return
        if owner.fail == "dns":
            self._exit(255, f"ssh: Could not resolve hostname {self.host}: Name or service not known")
            return
        for lport, rhost, rport in self.forwards:
            s = socket.socket()
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                s.bind(("127.0.0.1", lport))
            except OSError:
                s.close()
                self._close_all()
                self._exit(255, f"bind [127.0.0.1]:{lport}: Address already in use\n"
                                f"channel_setup_fwd_listener_tcpip: cannot listen to port: {lport}\n"
                                "Could not request local forwarding.")
                return
            s.listen(16)
            self._listeners.append(s)
            threading.Thread(target=self._serve, args=(s, rhost, rport), daemon=True,
                             name=f"fake-ssh-{lport}").start()

    # -- the process protocol --------------------------------------------------------------

    def poll(self) -> int | None:
        return self.returncode

    def terminate(self) -> None:
        if self.returncode is None:
            self._close_all()
            self._exit(-15, "")

    def kill(self) -> None:
        self.terminate()

    def wait(self, timeout: float | None = None) -> int:
        self._stopped.wait(timeout)
        return self.returncode if self.returncode is not None else 0

    # -- behaviour --------------------------------------------------------------------------

    def drop(self) -> None:
        """The hub connection dies: status 255, like ServerAlive giving up."""
        if self.returncode is None:
            self._close_all()
            self._exit(255, f"Timeout, server {self.host} not responding.")

    def _exit(self, code: int, message: str) -> None:
        if message:
            self.stderr_tail += message + "\n"
        self.returncode = code
        self._stopped.set()

    def _close_all(self) -> None:
        with self._mu:
            socks = self._listeners + self._conns
            self._listeners, self._conns = [], []
        for s in socks:
            try:
                s.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            s.close()

    def _serve(self, listener: socket.socket, rhost: str, rport: int) -> None:
        n = 0
        while not self._stopped.is_set():
            try:
                client, _ = listener.accept()
            except OSError:
                return
            self.accepted += 1
            n += 1
            target = self.owner.route(rhost, rport)
            try:
                if target is None:
                    raise ConnectionRefusedError("no route in the fake")
                upstream = socket.create_connection(target, timeout=2.0)
                upstream.settimeout(None)
            except OSError:
                # What ssh -L does when the far end refuses: accept, log, close.
                self.channel_failures += 1
                self.stderr_tail += f"channel {n + 1}: open failed: connect failed: Connection refused\n"
                client.close()
                continue
            with self._mu:
                if self._stopped.is_set():
                    client.close()
                    upstream.close()
                    return
                self._conns += [client, upstream]
            done = threading.Event()
            threading.Thread(target=_pipe, args=(client, upstream, done), daemon=True).start()
            threading.Thread(target=_pipe, args=(upstream, client, done), daemon=True).start()


class FakeSsh:
    """The launcher (``__call__``) and ``ssh -G`` stand-in (``ssh_g``) for tunnel tests."""

    def __init__(self, routes: dict[tuple[str, int], tuple[str, int]] | None = None, *,
                 config_output: str = "", fail: str = "", user: str = "dam1n19",
                 hub_loopback: bool = True) -> None:
        self.routes: dict[tuple[str, int], tuple[str, int]] = dict(routes or {})
        #: The hub's own 127.0.0.1 is this machine's in the fake world (fpgahub shares).
        self.hub_loopback = hub_loopback
        self.config_output = config_output
        self.fail = fail
        self.user = user
        self.launches: list[list[str]] = []
        self.procs: list[FakeSshProcess] = []
        self.g_calls: list[list[str]] = []

    def __call__(self, argv: Sequence[str]) -> FakeSshProcess:
        self.launches.append(list(argv))
        proc = FakeSshProcess(self, argv)
        self.procs.append(proc)
        return proc

    def ssh_g(self, argv: Sequence[str]) -> str:
        self.g_calls.append(list(argv))
        # A config copied with -F has had its forwards removed by the tunnel: model that.
        if "-F" in argv:
            return "\n".join(ln for ln in self.config_output.splitlines()
                             if not ln.lower().startswith(("localforward", "remoteforward",
                                                           "dynamicforward")))
        return self.config_output

    def route(self, rhost: str, rport: int) -> tuple[str, int] | None:
        target = self.routes.get((rhost, rport))
        if target is None and self.hub_loopback and rhost in ("127.0.0.1", "localhost"):
            target = ("127.0.0.1", rport)
        return target

    @property
    def current(self) -> FakeSshProcess:
        return self.procs[-1]

    def live(self) -> list[FakeSshProcess]:
        return [p for p in self.procs if p.returncode is None]

    def route_board(self, board_host: str, ports: dict[int, int], local: str = "127.0.0.1") -> None:
        """Map the real board's ports (as the hub sees them) to a virtual board's ports."""
        for remote, real in ports.items():
            if real:
                self.routes[(board_host, remote)] = (local, real)

    def close(self) -> None:
        for p in self.procs:
            p.terminate()
