"""SPIKE ONLY (lane XVC, 2026-09-24): the host-side pieces an XVC service would need.

Not wired into the engine, the daemon or the CLI, and kept under ``tests/spikes/`` so it
never ships in the wheel (it was ``src/harness_manager/services/xvc_spike.py`` on the
lane branch). ``docs/design/XVC_DEBUG.md`` is the design;
``tests/spikes/xvc_tunnel_spike.py`` drives this module against a fake XVC server
through Harness Manager's real ``SshTunnel`` and a private loopback sshd.

What is here, and why each piece exists:

- ``XvcClient``: the three XVC 1.0 commands, enough to probe and to time round trips.
- ``probe``: is the board's one XVC slot free, held, or not served? It costs one
  ``getinfo:``. The shell accepts a second client and closes it at once
  (``firmware/xvc_server/xvc_server.c:516-527``), and through ``ssh -L`` a refused far
  end ALSO reads as accept-then-EOF, so "held" here means "no answer to getinfo"; the
  tunnel's ``open_failures_since`` tells the two apart.
- ``XvcRelay``: a one-client TCP relay Harness Manager would own between the user's
  hw_server and the tunnel's local XVC port. It gives HM the three things a bare
  forward cannot: WHO is attached (the local peer's pid and command), a ``kick`` that
  frees the board's slot before a partition swap, and ``hold``/``release`` so nobody
  re-attaches mid-swap.
- ``hw_server_argv``: an hw_server that HM would own (a fixed per-board port, no
  daemon mode, no idle linger, no GDB ports) with the XVC target pre-opened.
"""

from __future__ import annotations

import contextlib
import os
import socket
import struct
import threading
import time
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

GETINFO_REPLY_PREFIX = b"xvcServer_v"


class XvcClosed(ConnectionError):
    """The far end closed the connection without answering."""


class XvcClient:
    """A minimal XVC 1.0 client (one TCP connection)."""

    def __init__(self, host: str, port: int, *, timeout: float = 5.0) -> None:
        self.sock = socket.create_connection((host, port), timeout=timeout)
        self.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        self.sock.settimeout(timeout)

    def _recv_exact(self, n: int) -> bytes:
        out = bytearray()
        while len(out) < n:
            chunk = self.sock.recv(n - len(out))
            if not chunk:
                raise XvcClosed(f"closed after {len(out)} of {n} bytes")
            out += chunk
        return bytes(out)

    def getinfo(self) -> str:
        self.sock.sendall(b"getinfo:")
        line = bytearray()
        while not line.endswith(b"\n"):
            chunk = self.sock.recv(64)
            if not chunk:
                raise XvcClosed("closed before the getinfo reply")
            line += chunk
        return line.decode("ascii", "replace").strip()

    def settck(self, period_ns: int) -> int:
        self.sock.sendall(b"settck:" + struct.pack("<I", period_ns))
        return struct.unpack("<I", self._recv_exact(4))[0]

    def shift(self, nbits: int, tms: bytes, tdi: bytes) -> bytes:
        nbytes = (nbits + 7) // 8
        if len(tms) != nbytes or len(tdi) != nbytes:
            raise ValueError("tms/tdi must be ceil(nbits/8) bytes")
        self.sock.sendall(b"shift:" + struct.pack("<I", nbits) + tms + tdi)
        return self._recv_exact(nbytes)

    def close(self) -> None:
        with contextlib.suppress(OSError):
            self.sock.close()

    def __enter__(self) -> XvcClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


def probe(host: str, port: int, *, timeout: float = 3.0) -> dict[str, Any]:
    """``{state: free|held|refused|timeout, info, rtt_ms}``; takes the slot for one getinfo."""
    t0 = time.perf_counter()
    try:
        c = XvcClient(host, port, timeout=timeout)
    except ConnectionRefusedError:
        return {"state": "refused", "info": "", "rtt_ms": None}
    except OSError as exc:
        return {"state": "timeout", "info": str(exc), "rtt_ms": None}
    try:
        info = c.getinfo()
        state = "free" if info.startswith(GETINFO_REPLY_PREFIX.decode()) else "held"
    except (XvcClosed, ConnectionResetError, BrokenPipeError):
        # accept-then-close: another client holds the slot (a close with our getinfo
        # still unread arrives as a reset, not an EOF)
        info, state = "", "held"
    except OSError as exc:
        info, state = str(exc), "timeout"
    finally:
        c.close()
    return {"state": state, "info": info, "rtt_ms": round((time.perf_counter() - t0) * 1e3, 3)}


# --- who is on the other end of a local TCP connection (Linux /proc) --------------------


def _inode_of_local_peer(peer_port: int, listen_port: int) -> int | None:
    """The socket inode of the CLIENT end 127.0.0.1:peer_port -> 127.0.0.1:listen_port."""
    want_local, want_remote = f":{peer_port:04X}", f":{listen_port:04X}"
    for table in ("/proc/net/tcp", "/proc/net/tcp6"):
        try:
            lines = Path(table).read_text().splitlines()[1:]
        except OSError:
            continue
        for line in lines:
            parts = line.split()
            if len(parts) > 9 and parts[1].endswith(want_local) and parts[2].endswith(want_remote):
                return int(parts[9])
    return None


def pid_of_socket(inode: int) -> tuple[int, str] | None:
    """``(pid, command line)`` of the process holding socket ``inode`` (same user only)."""
    target = f"socket:[{inode}]"
    for d in Path("/proc").iterdir():
        if not d.name.isdigit():
            continue
        try:
            for fd in (d / "fd").iterdir():
                if os.readlink(fd) == target:
                    cmd = (d / "cmdline").read_bytes().replace(b"\0", b" ").decode(errors="replace")
                    return int(d.name), cmd.strip()
        except OSError:
            continue
    return None


# --- the relay ------------------------------------------------------------------------------


@dataclass
class Attachment:
    peer: str
    since: float
    pid: int | None = None
    command: str = ""
    bytes_up: int = 0
    bytes_down: int = 0


class XvcRelay:
    """One-client relay: 127.0.0.1:<port> -> ``upstream`` (the tunnel's local XVC port)."""

    def __init__(self, upstream: tuple[str, int], *, port: int = 0,
                 identify_peer: bool = True) -> None:
        self.upstream = upstream
        self.identify_peer = identify_peer
        self._srv = socket.socket()
        self._srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._srv.bind(("127.0.0.1", port))
        self._srv.listen(4)
        self.port = self._srv.getsockname()[1]
        self._mu = threading.Lock()
        self._held = ""                       # non-empty: refuse new attaches, with why
        self._pair: tuple[socket.socket, socket.socket] | None = None
        self.attachment: Attachment | None = None
        self.refusals: list[tuple[float, str, str]] = []
        self.kicks: list[tuple[float, str]] = []
        self._stop = threading.Event()
        self._t = threading.Thread(target=self._accept_loop, name="xvc-relay", daemon=True)

    def start(self) -> XvcRelay:
        self._t.start()
        return self

    def status(self) -> dict[str, Any]:
        with self._mu:
            a = self.attachment
            return {"port": self.port, "upstream": f"{self.upstream[0]}:{self.upstream[1]}",
                    "held": self._held,
                    "attached": None if a is None else {
                        "peer": a.peer, "pid": a.pid, "command": a.command,
                        "since": a.since, "bytes_up": a.bytes_up, "bytes_down": a.bytes_down}}

    def hold(self, why: str) -> None:
        """Refuse new attaches (a swap is running), and drop the current one."""
        with self._mu:
            self._held = why or "held"
        self.kick(why)

    def release(self) -> None:
        with self._mu:
            self._held = ""

    def kick(self, why: str) -> bool:
        """Close the current session both ways; the board's slot frees at once."""
        with self._mu:
            pair, self._pair = self._pair, None
        if pair is None:
            return False
        self.kicks.append((time.monotonic(), why))
        for s in pair:
            with contextlib.suppress(OSError):
                s.shutdown(socket.SHUT_RDWR)
            with contextlib.suppress(OSError):
                s.close()
        return True

    def close(self) -> None:
        self._stop.set()
        with contextlib.suppress(OSError):
            self._srv.close()
        self.kick("relay closed")
        self._t.join(timeout=3)

    def _accept_loop(self) -> None:
        while not self._stop.is_set():
            try:
                conn, peer = self._srv.accept()
            except OSError:
                return
            peer_s = f"{peer[0]}:{peer[1]}"
            with self._mu:
                refuse = self._held or ("another client is attached" if self._pair else "")
            if refuse:
                self.refusals.append((time.monotonic(), peer_s, refuse))
                conn.close()                   # the firmware's own answer: accept, then close
                continue
            try:
                up = socket.create_connection(self.upstream, timeout=10)
            except OSError as exc:
                self.refusals.append((time.monotonic(), peer_s, f"upstream: {exc}"))
                conn.close()
                continue
            for s in (conn, up):
                s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                s.settimeout(None)
            att = Attachment(peer=peer_s, since=time.time())
            if self.identify_peer:
                inode = _inode_of_local_peer(peer[1], self.port)
                who = pid_of_socket(inode) if inode else None
                if who:
                    att.pid, att.command = who
            with self._mu:
                self._pair = (conn, up)
                self.attachment = att
            threading.Thread(target=self._pump, args=(conn, up, att, "up"), daemon=True).start()
            threading.Thread(target=self._pump, args=(up, conn, att, "down"), daemon=True).start()

    def _pump(self, src: socket.socket, dst: socket.socket, att: Attachment, way: str) -> None:
        try:
            while True:
                data = src.recv(65536)
                if not data:
                    break
                dst.sendall(data)
                if way == "up":
                    att.bytes_up += len(data)
                else:
                    att.bytes_down += len(data)
        except OSError:
            pass
        finally:
            with self._mu:
                pair = self._pair
                if pair is not None and src in pair:
                    self._pair = None
                    self.attachment = None
            for s in (src, dst):
                with contextlib.suppress(OSError):
                    s.shutdown(socket.SHUT_RDWR)
                with contextlib.suppress(OSError):
                    s.close()


# --- an hw_server Harness Manager owns ------------------------------------------------------


def hw_server_argv(binary: str, listen_port: int, xvc: str, *,
                   log_xvc: bool = False, extra: Sequence[str] = ()) -> list[str]:
    """An hw_server on 127.0.0.1:``listen_port`` with the XVC target ``xvc`` (host:port) open.

    ``-p0``: no GDB ports (the default opens 3000-3005 on every interface). No ``-d``
    and no ``-I``: HM starts and stops it, so it never lingers the 20 s that the
    auto-launched one does (ILA-mint finding #19).
    """
    argv = [binary, "-q", "-p0", "-s", f"TCP:127.0.0.1:{listen_port}",
            "-e", f"set auto-open-servers xilinx-xvc:{xvc}"]
    if log_xvc:
        argv += ["-L-", "-lxvc"]
    return [*argv, *extra]


def vivado_tcl(hw_server_url: str, ltx: str | None) -> str:
    """The snippet a user pastes into the Vivado Tcl console for an HM-owned hw_server."""
    lines = ["open_hw_manager", f"connect_hw_server -url {hw_server_url}",
             "open_hw_target", "current_hw_device [lindex [get_hw_devices] 0]"]
    if ltx:
        lines += [f"set_property PROBES.FILE {{{ltx}}} [current_hw_device]",
                  f"set_property FULL_PROBES.FILE {{{ltx}}} [current_hw_device]"]
    lines.append("refresh_hw_device [current_hw_device]")
    return "\n".join(lines)
