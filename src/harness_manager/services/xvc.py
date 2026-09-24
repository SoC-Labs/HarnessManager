"""Fabric debug over XVC: the relay, the hw_server Harness Manager owns, swaps and leases.

Lane XVC-CORE (X1), from docs/design/XVC_DEBUG.md and the design spike
(``tests/spikes/xvc_spike_lib.py``). Board-agnostic: the board pack's ``XvcAdapter``
(``core.pack``) says where the harness's XVC server is reachable and which probes
files match the loaded design.

**Scope (david, 2026-09-24).** The harness runs its own XVC server for the
reconfigurable partition: the Debug Bridge and the debug hub and ILAs of the design
loaded in the partition. Harness Manager treats XVC as exactly that, and never offers
whole-device JTAG over it (``PARTITION_SCOPE`` is the one wording, shown by the CLI,
the API and the UI).

The picture (mode M1, the default; ``byo`` drops HM's hw_server)::

    Vivado HW Manager -> hw_server (HM's, 127.0.0.1:H, -p0, no -d/-I)
                             |  xilinx-xvc:127.0.0.1:R
    your own hw_server ----->|                                   (--byo)
                             v
                      XvcRelay 127.0.0.1:R   one local client; holds the board's slot
                             |
                      the adapter's endpoint (hub tunnel | board SSH | direct)
                             v
                      harness xvc_server :2542 -> debug_bridge_0 -> RM hub (+ MIG hub, Linux)

**The relay holds the board's one XVC slot while a session is open (D-X4).** It keeps
ONE upstream connection and serves local clients in turn. It forwards whole XVC
commands only (it parses ``getinfo:``/``settck:``/``shift:`` and reads each reply in
full), so a local client that leaves mid-command never leaves the upstream mid-command,
and the next client starts clean. That also removes the firmware's reconnect race
(accept before it notices a close: ``xvc_server.c:513-590``) between hw_server's own
connects, and stops anyone else taking the slot between swaps. A second local client
is accepted and closed at once, as the firmware does, and the refusal is recorded.

**Swaps (D-X3: refresh automatically).** ``deploy.started`` (synchronously, before
the swap RPC): hold the relay (kick the client, drop the board's slot) and stop HM's
hw_server. ``deploy.done`` verified: start a FRESH hw_server on the same port, take
the slot again, publish ``ready`` with the new design's probes file. An unverified or
failed swap closes the session with the reason (the words the debug service uses).

**Leases (X6, and every board behind a hub).** ``open`` is for the lease holder only:
the board's ``hub`` adapter is asked through ``LeaseService.view`` and anything but
``mine`` is refused (409 HELD naming the holder). A hub that cannot be asked refuses
too. ``lease.state`` released/expired/lost closes the session. A board with no hub has
no lease; the session lock (one Harness Manager per board) is the gate.

**hw_server (D-X2).** HM runs its own: ``-q -p0 -s TCP:127.0.0.1:H -e "set
auto-open-servers xilinx-xvc:127.0.0.1:R" -e "set jtag-port-filter Xilinx/XVC/127.0.0.1:R"``
(the XVC cable only, never a local USB one: ``hw_server_argv``), never ``-d`` or ``-I`` (a daemonised or
idle-lingering hw_server outlives the swap: ILA-mint finding #19), in its own process
group, restarted around every swap. Only processes this service started are ever
signalled; one a killed owner left behind is stopped only while its command line is
still the recorded one.

Events: ``xvc.state`` ``{state, open, mode, relay_port, hw_server_port, hw_server_pid,
url, attached, board_slot, reach, ltx, warnings, scope, rm_id, rm_name, detail}``.
States: ``down``, ``starting``, ``ready`` (nothing attached), ``attached``, ``held``
(someone else holds the board's slot), ``swapping``, ``failed``.
"""

from __future__ import annotations

import contextlib
import getpass
import json
import logging
import os
import re
import select
import shutil
import signal
import socket
import struct
import subprocess
import sys
import threading
import time
import zlib
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from harness_manager.core.capabilities import DEBUG_FABRIC
from harness_manager.core.errors import (
    ActionFailedError,
    AlreadyError,
    HarnessError,
    HeldError,
    PortBoundError,
    UnavailableError,
    UnreachableError,
    UsageError,
)
from harness_manager.core.events import Event, EventBus
from harness_manager.core.pack import BoardSession

log = logging.getLogger(__name__)

__all__ = ["XvcService", "XvcStatus", "XvcRelay", "XvcPorts", "XvcClient", "probe",
           "hw_server_argv", "xvc_port_filter", "find_hw_server", "vivado_tcl", "PARTITION_SCOPE",
           "TOPIC"]

CAPABILITY = DEBUG_FABRIC
TOPIC = "xvc.state"
STATES = ("down", "starting", "ready", "attached", "held", "swapping", "failed")

#: The one wording of what XVC reaches here (david, 2026-09-24). CLI, API and UI show it.
PARTITION_SCOPE = (
    "XVC here is scoped to the reconfigurable partition's debug chain: the harness's "
    "Debug Bridge and the debug hub and ILAs of the design loaded in the partition. "
    "It is never whole-device JTAG: it cannot configure or read back the FPGA.")
#: X6: the bare-metal harness's 2542 is open on every interface. Packs use these words.
UNAUTHENTICATED_WARNING = (
    "XVC on this harness is unauthenticated: anyone who can reach the board network can "
    "attach. Accepted until the Linux cutover; Harness Manager opens it for the lease "
    "holder only.")
SWAP_NOTE = "a partition swap closes this session; it reopens on the new design"

HW_SERVER_ENV = "HARNESS_MANAGER_HW_SERVER"
PORT_BASE_ENV = "HARNESS_MANAGER_XVC_PORT_BASE"
STATE_DIR_ENV = "HARNESS_MANAGER_STATE_DIR"
#: 23600-23727: clear of 2542, 3121, hw_server's GDB 3000-3005 and the debug block (23300+).
DEFAULT_PORT_BASE = 23600
PORT_SLOTS = 64
PORT_BLOCK = 2           # relay R, hw_server H
#: Ports never used for a relay or an hw_server (the board's XVC, the shared hw_server).
AVOID_PORTS = frozenset({2542, 3121})

# --- the XVC 1.0 wire ----------------------------------------------------------------------

GETINFO = b"getinfo:"
SETTCK = b"settck:"
SHIFT = b"shift:"
GETINFO_REPLY_PREFIX = "xvcServer_v"
#: The firmware accepts a shift of up to 4 x its 2048-bit vector (xvc_server.h:305-309).
MAX_ACCEPT_BITS = 4 * 2048
_MIN_COMMAND = len(GETINFO)          # every command is at least this long
_MAX_INFO_LINE = 128


class XvcClosed(ConnectionError):
    """The far end closed the connection without answering."""


class XvcProtocolError(ValueError):
    """Bytes that are not an XVC 1.0 command (or reply): the connection is dropped."""


class _Reader:
    """Buffered exact reads from a socket."""

    def __init__(self, sock: socket.socket) -> None:
        self.sock = sock
        self.buf = bytearray()

    def fill(self, n: int) -> bool:
        while len(self.buf) < n:
            chunk = self.sock.recv(65536)
            if not chunk:
                return False
            self.buf += chunk
        return True

    def take(self, n: int) -> bytes:
        out = bytes(self.buf[:n])
        del self.buf[:n]
        return out

    def line(self, limit: int = _MAX_INFO_LINE) -> bytes | None:
        while b"\n" not in self.buf:
            if len(self.buf) > limit:
                raise XvcProtocolError("a getinfo reply without a newline")
            chunk = self.sock.recv(256)
            if not chunk:
                return None
            self.buf += chunk
        end = self.buf.index(b"\n") + 1
        return self.take(end)


def read_command(reader: _Reader) -> tuple[bytes, int, int] | None:
    """One whole XVC command: ``(bytes, reply length (0 = a line), shift bits)``.

    ``None`` when the peer closed (a partial command is discarded, never forwarded).
    ``XvcProtocolError`` for anything the firmware would drop the client for.
    """
    if not reader.fill(_MIN_COMMAND):
        return None
    head = bytes(reader.buf[:_MIN_COMMAND])
    if head == GETINFO:
        return reader.take(len(GETINFO)), 0, 0
    if head.startswith(SETTCK):
        if not reader.fill(len(SETTCK) + 4):
            return None
        return reader.take(len(SETTCK) + 4), 4, 0
    if head.startswith(SHIFT):
        if not reader.fill(len(SHIFT) + 4):
            return None
        bits = struct.unpack("<I", bytes(reader.buf[len(SHIFT):len(SHIFT) + 4]))[0]
        if bits == 0 or bits > MAX_ACCEPT_BITS:
            raise XvcProtocolError(f"a shift of {bits} bits (the harness accepts 1..{MAX_ACCEPT_BITS})")
        nbytes = (bits + 7) // 8
        total = len(SHIFT) + 4 + 2 * nbytes
        if not reader.fill(total):
            return None
        return reader.take(total), nbytes, bits
    raise XvcProtocolError(f"not an XVC command: {head!r}")


def read_reply(reader: _Reader, length: int) -> bytes:
    """The reply to one command (``length`` 0: the getinfo line). ``XvcClosed`` at EOF."""
    if length == 0:
        line = reader.line()
        if line is None:
            raise XvcClosed("closed before the getinfo reply")
        return line
    if not reader.fill(length):
        raise XvcClosed(f"closed after {len(reader.buf)} of {length} reply bytes")
    return reader.take(length)


class XvcClient:
    """A minimal XVC 1.0 client (one TCP connection): enough to probe and to test."""

    def __init__(self, host: str, port: int, *, timeout: float = 5.0) -> None:
        self.sock = socket.create_connection((host, port), timeout=timeout)
        self.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        self.sock.settimeout(timeout)
        self._r = _Reader(self.sock)

    def getinfo(self) -> str:
        self.sock.sendall(GETINFO)
        return read_reply(self._r, 0).decode("ascii", "replace").strip()

    def settck(self, period_ns: int) -> int:
        self.sock.sendall(SETTCK + struct.pack("<I", period_ns))
        return struct.unpack("<I", read_reply(self._r, 4))[0]

    def shift(self, nbits: int, tms: bytes, tdi: bytes) -> bytes:
        nbytes = (nbits + 7) // 8
        if len(tms) != nbytes or len(tdi) != nbytes:
            raise ValueError("tms/tdi must be ceil(nbits/8) bytes")
        self.sock.sendall(SHIFT + struct.pack("<I", nbits) + tms + tdi)
        return read_reply(self._r, nbytes)

    def close(self) -> None:
        with contextlib.suppress(OSError):
            self.sock.close()

    def __enter__(self) -> XvcClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


def probe(host: str, port: int, *, timeout: float = 3.0) -> dict[str, Any]:
    """``{state: free|held|refused|timeout, info, rtt_ms}``. It TAKES the slot for one getinfo,
    so it is only for the lease holder, and never right before an attach."""
    t0 = time.perf_counter()
    try:
        c = XvcClient(host, port, timeout=timeout)
    except ConnectionRefusedError:
        return {"state": "refused", "info": "", "rtt_ms": None}
    except OSError as exc:
        return {"state": "timeout", "info": str(exc), "rtt_ms": None}
    try:
        info = c.getinfo()
        state = "free" if info.startswith(GETINFO_REPLY_PREFIX) else "held"
    except (XvcClosed, ConnectionResetError, BrokenPipeError, XvcProtocolError):
        info, state = "", "held"             # accept-then-close: another client has the slot
    except OSError as exc:
        info, state = str(exc), "timeout"
    finally:
        c.close()
    return {"state": state, "info": info, "rtt_ms": round((time.perf_counter() - t0) * 1e3, 3)}


# --- who is on the other end of a local TCP connection (Linux /proc) -------------------------


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
    try:
        procs = list(Path("/proc").iterdir())
    except OSError:
        return None
    for d in procs:
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


# --- the relay ------------------------------------------------------------------------------------


@dataclass
class Attachment:
    peer: str
    since: float
    pid: int | None = None
    command: str = ""
    bytes_up: int = 0
    bytes_down: int = 0
    shifts: int = 0

    def public(self) -> dict[str, Any]:
        return {"peer": self.peer, "pid": self.pid, "command": self.command, "since": self.since,
                "bytes_up": self.bytes_up, "bytes_down": self.bytes_down, "shifts": self.shifts}


def _close(sock: socket.socket | None) -> None:
    if sock is None:
        return
    with contextlib.suppress(OSError):
        sock.shutdown(socket.SHUT_RDWR)
    with contextlib.suppress(OSError):
        sock.close()


class XvcRelay:
    """127.0.0.1:``port`` -> the board's XVC server, holding the board's one slot (D-X4).

    ``endpoint()`` gives the upstream (host, port); it is called on every (re)connect,
    so an adapter may open a forward lazily. ``board_slot``: ``ours`` (we hold it),
    ``held`` (someone else does), ``refused`` (nothing serves it), ``down`` (lost,
    reconnecting), ``released`` (dropped on purpose: a swap or a close).
    """

    def __init__(self, endpoint: Callable[[], tuple[str, int]], *, port: int = 0,
                 on_change: Callable[[], None] | None = None,
                 refused_why: Callable[[float], str] | None = None,
                 identify_peer: bool = True, connect_timeout: float = 5.0,
                 backoff_s: Sequence[float] = (0.5, 1.0, 2.0, 5.0, 10.0),
                 label: str = "") -> None:
        self._endpoint = endpoint
        self._on_change = on_change
        self._refused_why = refused_why
        self.identify_peer = identify_peer
        self.connect_timeout = connect_timeout
        self._backoff = tuple(backoff_s) or (1.0,)
        self.label = label or "xvc relay"
        self._srv = socket.socket()
        self._srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            self._srv.bind(("127.0.0.1", port))
        except OSError as exc:
            self._srv.close()
            raise PortBoundError(f"local port {port} for the XVC relay is already in use",
                                 hint="another program holds it; close it or pick another "
                                      f"base with {PORT_BASE_ENV}") from exc
        self._srv.listen(4)
        self.port: int = self._srv.getsockname()[1]
        self._mu = threading.Lock()             # state below
        self._io = threading.Lock()             # one command in flight upstream
        self._held = ""                         # non-empty: refuse attaches, with why
        self._up: socket.socket | None = None
        self._upr: _Reader | None = None
        self.board_slot = "released"
        self.slot_detail = ""
        self.info = ""                          # the board's getinfo line
        self._client: socket.socket | None = None
        self.attachment: Attachment | None = None
        self.refusals: list[tuple[float, str, str]] = []
        self.kicks: list[tuple[float, str]] = []
        self.reconnects = 0
        self.served = 0                         # local client sessions that have ended
        self._auto = False                      # reconnect a lost slot on our own
        self._attempt = 0
        self._next_try = 0.0
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []

    # -- lifecycle ---------------------------------------------------------------------------

    def start(self) -> XvcRelay:
        for target, name in ((self._accept_loop, "accept"), (self._supervise, "watch")):
            t = threading.Thread(target=target, name=f"xvc-relay-{name}-{self.port}", daemon=True)
            t.start()
            self._threads.append(t)
        return self

    def close(self) -> None:
        self._stop.set()
        with contextlib.suppress(OSError):
            self._srv.shutdown(socket.SHUT_RDWR)   # wakes the blocked accept (Linux)
        with contextlib.suppress(OSError):
            self._srv.close()
        self.kick("the relay was closed")
        self._drop_upstream("released", "closed")
        for t in self._threads:
            if t is not threading.current_thread():
                t.join(timeout=3)

    # -- views -------------------------------------------------------------------------------

    def status(self) -> dict[str, Any]:
        with self._mu:
            a = self.attachment
            return {"port": self.port, "held": self._held, "board_slot": self.board_slot,
                    "slot_detail": self.slot_detail, "info": self.info,
                    "attached": None if a is None else a.public(),
                    "refusals": len(self.refusals), "reconnects": self.reconnects}

    @property
    def held(self) -> str:
        with self._mu:
            return self._held

    def _changed(self) -> None:
        cb = self._on_change
        if cb is not None:
            try:
                cb()
            except Exception:  # noqa: BLE001 - a watcher must never stop the relay
                log.exception("xvc relay change handler failed")

    # -- the board's slot ----------------------------------------------------------------------

    def acquire(self, timeout: float = 8.0) -> None:
        """Take the board's slot: connect upstream and check it answers ``getinfo:``.

        A slot another client holds reads as accept-then-close; it is retried until
        ``timeout`` (a client that just left is still counted for one firmware poll:
        the reconnect race). Raises ``HeldError`` (someone else holds it) or
        ``UnreachableError`` (nothing serves it). Afterwards the relay keeps the slot and
        reconnects on its own if it is lost.
        """
        deadline = time.monotonic() + timeout
        delay = 0.1
        while True:
            state, detail = self._connect_once()
            if state == "ours":
                with self._mu:
                    self._auto = True
                    self._attempt = 0
                self._changed()
                return
            if state != "held" or time.monotonic() + delay > deadline or self._stop.is_set():
                with self._mu:
                    self.board_slot, self.slot_detail = state, detail
                self._changed()
                if state == "held":
                    raise HeldError(f"the board's XVC slot is held by another client ({detail})",
                                    holder="another XVC client on the board",
                                    hint="the harness serves one XVC client; close the other "
                                         "Vivado/hw_server first (a hub user, or an old session)")
                raise UnreachableError(f"the board's XVC server did not answer ({detail})",
                                       hint="is the harness up and serving XVC on 2542? "
                                            "(`harness-manager info TARGET`)")
            time.sleep(delay)
            delay = min(delay * 2, 1.0)

    def _connect_once(self) -> tuple[str, str]:
        """``(ours|held|refused, detail)``; on ``ours`` the upstream is installed."""
        t0 = time.monotonic()
        try:
            host, port = self._endpoint()
        except HarnessError as exc:
            return "refused", exc.message
        try:
            up = socket.create_connection((host, port), timeout=self.connect_timeout)
        except ConnectionRefusedError:
            return "refused", f"nothing listens on {host}:{port}"
        except OSError as exc:
            return "refused", f"{host}:{port}: {exc}"
        up.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        reader = _Reader(up)
        try:
            up.settimeout(self.connect_timeout)
            up.sendall(GETINFO)
            info = read_reply(reader, 0).decode("ascii", "replace").strip()
            if not info.startswith(GETINFO_REPLY_PREFIX):
                raise XvcProtocolError(f"unexpected getinfo reply {info!r}")
            up.settimeout(None)
        except (XvcClosed, ConnectionResetError, BrokenPipeError, XvcProtocolError) as exc:
            _close(up)
            why = self._refused_why(t0) if self._refused_why is not None else ""
            if why:                 # through ssh -L a refused far end also accepts, then closes
                return "refused", why
            return "held", f"accepted, then closed ({type(exc).__name__})"
        except OSError as exc:
            _close(up)
            return "refused", f"no getinfo reply: {exc}"
        with self._mu:
            old, self._up, self._upr = self._up, up, reader
            self.board_slot, self.slot_detail, self.info = "ours", "", info
        _close(old)
        return "ours", info

    def _drop_upstream(self, slot: str, detail: str) -> None:
        with self._mu:
            up, self._up, self._upr = self._up, None, None
            self.board_slot, self.slot_detail = slot, detail
            if slot == "released":
                self._auto = False
        _close(up)

    def _lost(self, why: str) -> None:
        """The upstream died under us (a harness restart, the tunnel): kick, reconnect later."""
        with self._mu:
            if self.board_slot != "ours":
                return
            self._attempt = 0
            self._next_try = time.monotonic() + self._backoff[0]
        log.info("%s: lost the board's XVC slot: %s", self.label, why)
        self._drop_upstream("down", why)
        self.kick(f"the board's XVC connection was lost ({why})")
        self._changed()

    def hold(self, why: str) -> None:
        """Refuse attaches, kick the client and free the board's slot (a swap is next)."""
        with self._mu:
            self._held = why or "held"
        self.kick(why)
        self._drop_upstream("released", why)
        self._changed()

    def release(self) -> None:
        """Allow attaches again (``acquire`` takes the slot back)."""
        with self._mu:
            self._held = ""

    def kick(self, why: str) -> bool:
        """Close the local client. The board's slot stays ours (the relay holds it)."""
        with self._mu:
            client, self._client = self._client, None
            self.attachment = None
        if client is None:
            return False
        self.kicks.append((time.monotonic(), why))
        _close(client)
        return True

    # -- the supervisor ----------------------------------------------------------------------

    def _supervise(self) -> None:
        while not self._stop.wait(0.2):
            with self._mu:
                slot, held, auto = self.board_slot, self._held, self._auto
                up = self._up
            if held:
                continue
            if slot == "ours" and up is not None:
                self._check_idle_upstream(up)
            elif auto and slot in ("down", "held", "refused") and time.monotonic() >= self._next_try:
                state, detail = self._connect_once()
                with self._mu:
                    if state == "ours":
                        self.reconnects += 1
                        self._attempt = 0
                    else:
                        self.board_slot, self.slot_detail = state, detail
                        self._attempt += 1
                        wait = self._backoff[min(self._attempt, len(self._backoff) - 1)]
                        self._next_try = time.monotonic() + wait
                if state == "ours":
                    log.info("%s: took the board's XVC slot back", self.label)
                    self._changed()

    def _check_idle_upstream(self, up: socket.socket) -> None:
        """An idle upstream that became readable has closed (or broke the protocol)."""
        if not self._io.acquire(blocking=False):
            return                                  # a command is in flight: it will notice
        try:
            if self._up is not up:
                return
            try:
                ready, _, _ = select.select([up], [], [], 0)
            except (OSError, ValueError):
                ready = [up]
            if not ready:
                return
            try:
                data = up.recv(1, socket.MSG_PEEK)
            except OSError as exc:
                data, why = b"", f"socket error: {exc}"
            else:
                why = "the board closed it (a harness restart, or the tunnel dropped)"
            if data:
                why = "unexpected bytes from the board while idle"
        finally:
            self._io.release()
        self._lost(why)

    # -- serving local clients ------------------------------------------------------------------

    def _accept_loop(self) -> None:
        while not self._stop.is_set():
            try:
                conn, peer = self._srv.accept()
            except OSError:
                return
            peer_s = f"{peer[0]}:{peer[1]}"
            with self._mu:
                refuse = self._held or ("another client is attached" if self._client else "")
                if not refuse and self.board_slot != "ours":
                    refuse = f"the board's XVC slot is {self.board_slot}" + (
                        f" ({self.slot_detail})" if self.slot_detail else "")
                if not refuse:
                    self._client = conn
            if refuse:
                self.refusals.append((time.monotonic(), peer_s, refuse))
                _close(conn)                        # the firmware's own answer
                continue
            att = Attachment(peer=peer_s, since=time.time())
            if self.identify_peer:
                inode = _inode_of_local_peer(peer[1], self.port)
                who = pid_of_socket(inode) if inode else None
                if who:
                    att.pid, att.command = who
            with self._mu:
                if self._client is conn:
                    self.attachment = att
            self._changed()
            threading.Thread(target=self._serve, args=(conn, att), daemon=True,
                             name=f"xvc-relay-serve-{self.port}").start()

    def _serve(self, conn: socket.socket, att: Attachment) -> None:
        conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        reader = _Reader(conn)
        lost = ""
        try:
            while not self._stop.is_set():
                try:
                    cmd = read_command(reader)
                except XvcProtocolError as exc:
                    log.info("%s: dropped a client that broke the protocol: %s", self.label, exc)
                    break
                except OSError:
                    break
                if cmd is None:
                    break
                data, reply_len, bits = cmd
                with self._io:
                    with self._mu:
                        up, upr = self._up, self._upr
                    if up is None or upr is None:
                        break
                    try:
                        up.sendall(data)
                        reply = read_reply(upr, reply_len)
                    except (OSError, XvcClosed, XvcProtocolError) as exc:
                        lost = f"{type(exc).__name__}: {exc}" if str(exc) else type(exc).__name__
                        if self._up is not up:
                            lost = ""           # dropped on purpose (hold/close): not a loss
                        break
                att.bytes_up += len(data)
                att.bytes_down += len(reply)
                if bits:
                    att.shifts += 1
                try:
                    conn.sendall(reply)
                except OSError:
                    break                            # the client left; upstream stays in step
        finally:
            with self._mu:
                mine = self._client is conn
                if mine:
                    self._client = None
                    self.attachment = None
                self.served += 1
            _close(conn)
            if lost:
                self._lost(lost)
            elif mine:
                self._changed()


# --- hw_server --------------------------------------------------------------------------------------

_VIVADO_VERSION = re.compile(r"(?:^|[/\\])(20\d\d\.\d)(?:[/\\]|$)")
_FORBIDDEN_HW_ARGS = ("-d", "-I")
#: hw_server settings ``extra`` may not touch: they would open cables beyond the XVC one.
_CABLE_SETTINGS = ("auto-open-servers", "jtag-port-filter", "always-open-jtag", "--init")


def xvc_port_filter(xvc: str) -> str:
    """hw_server's ``jtag-port-filter`` that admits the XVC cable ``xvc`` (host:port) only.

    hw_server names a port ``<manufacturer>/<product>/<serial>``; an XVC one is
    ``Xilinx/XVC/<host>:<port>``. The filter is a substring match (probed on 2024.1,
    2025.2 and 2026.1: ``XVC`` admits every XVC cable, ``*`` admits none), so the full
    name admits this one cable and nothing else.
    """
    return f"Xilinx/XVC/{xvc}"


def hw_server_argv(binary: str, listen_port: int, xvc: str, *, log_xvc: bool = False,
                   extra: Sequence[str] = ()) -> list[str]:
    """An hw_server on 127.0.0.1:``listen_port`` with the XVC target ``xvc`` (host:port).

    ``-p0``: no GDB ports (the default opens 3000-3005 on every interface). No ``-d``
    and no ``-I``: HM starts and stops it, so it never lingers the 20 s that the
    auto-launched one does (ILA-mint finding #19). ``extra`` may not add them back.

    **Only the XVC cable (david's scope rule: never whole-device JTAG).** By default
    hw_server opens every local cable type (``auto-open-servers`` is ``*``: digilent-ftdi,
    xilinx-ftdi, xilinx-pcusb, bscan-jtag), so a USB JTAG cable on this host would be
    offered as a whole-device target. ``set auto-open-servers xilinx-xvc:<xvc>`` replaces
    that list with the one XVC server, and ``set jtag-port-filter Xilinx/XVC/<xvc>``
    also hides any cable a client opens on this hw_server later (``jtag servers -open``).
    Evidence: docs/assessment/xvc_ui_2026-09-25/hw_server_cable_filter.txt. Proven on
    this host against a second (fake) XVC cable; UNVERIFIED against a physical USB cable
    until the board window. ``extra`` may not change these settings.
    """
    for arg in extra:
        if arg in _FORBIDDEN_HW_ARGS or arg.startswith("-I"):
            raise UsageError(f"hw_server option {arg} is not allowed here",
                             hint="Harness Manager owns this hw_server's lifetime (no -d, no -I)")
        if any(name in arg for name in _CABLE_SETTINGS):
            raise UsageError(f"hw_server option {arg!r} is not allowed here",
                             hint="this hw_server opens the board's XVC cable only; with "
                                  "--byo you run your own")
    argv = [binary, "-q", "-p0", "-s", f"TCP:127.0.0.1:{listen_port}",
            "-e", f"set auto-open-servers xilinx-xvc:{xvc}",
            "-e", f"set jtag-port-filter {xvc_port_filter(xvc)}"]
    if log_xvc:
        argv += ["-L-", "-lxvc"]
    return [*argv, *extra]


def vivado_version_of(path: str) -> str:
    """``"2024.1"`` from ``/apps/Xilinx/Vivado/2024.1/bin/hw_server``; ``""`` when not in the path."""
    m = _VIVADO_VERSION.search(str(path))
    return m.group(1) if m else ""


def _is_file(path: Path) -> bool:
    try:
        return path.is_file()
    except OSError:
        return False


def find_hw_server() -> str:
    """The hw_server of the user's Vivado: ``$HARNESS_MANAGER_HW_SERVER``, else the one next to
    ``vivado`` on PATH, else ``$XILINX_VIVADO/bin``, else ``hw_server`` on PATH (a Lab Edition).

    Never a remote one (the hub's shared 3121 is another Vivado release).
    """
    env = os.environ.get(HW_SERVER_ENV, "").strip()
    if env:
        if _is_file(Path(env)):
            return str(Path(env))
        found = shutil.which(env)
        if found:
            return found
        raise UnavailableError(CAPABILITY, f"{HW_SERVER_ENV}={env} does not exist")
    vivado = shutil.which("vivado")
    if vivado:
        for d in dict.fromkeys((Path(vivado).parent, Path(vivado).resolve().parent)):
            if _is_file(d / "hw_server"):
                return str(d / "hw_server")
    xil = os.environ.get("XILINX_VIVADO", "").strip()
    if xil and _is_file(Path(xil) / "bin" / "hw_server"):
        return str(Path(xil) / "bin" / "hw_server")
    found = shutil.which("hw_server")
    if found:
        return found
    raise UnavailableError(CAPABILITY, "hw_server not found: install Vivado (the Lab Edition is "
                                       f"enough) or set {HW_SERVER_ENV}; or open with --byo and "
                                       "use your own hw_server")


def port_in_use(port: int) -> bool:
    """Is anything bound to 127.0.0.1:``port``? A bind test: it never connects."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            s.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        else:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind(("127.0.0.1", port))
        return False
    except OSError:
        return True
    finally:
        s.close()


def _pid_alive(pid: int) -> bool:
    from harness_manager.core.session import pid_alive

    return pid_alive(pid)


def _cmdline(pid: int) -> list[str] | None:
    try:
        raw = Path(f"/proc/{pid}/cmdline").read_bytes()
    except OSError:
        return None
    return [a.decode(errors="replace") for a in raw.split(b"\0") if a]


def _log_tail(path: Path, n: int = 3) -> str:
    try:
        lines = [ln.strip() for ln in path.read_text(errors="replace").splitlines() if ln.strip()]
    except OSError:
        return ""
    return " | ".join(lines[-n:])


class HwServer:
    """One hw_server process this service started (its own process group on POSIX)."""

    def __init__(self, binary: str, port: int, xvc: str, log_path: Path, *,
                 start_timeout: float = 60.0, stop_timeout: float = 5.0) -> None:
        self.binary = binary
        self.port = port
        self.xvc = xvc
        self.log_path = log_path
        self.start_timeout = start_timeout
        self.stop_timeout = stop_timeout
        self.argv = hw_server_argv(binary, port, xvc)
        self.version = vivado_version_of(binary)
        self.proc: subprocess.Popen | None = None

    @property
    def pid(self) -> int:
        return self.proc.pid if self.proc is not None else 0

    def start(self) -> HwServer:
        """Start it and wait until it listens on 127.0.0.1:port. Raises on failure."""
        if port_in_use(self.port):
            raise PortBoundError(f"local port {self.port} for hw_server is already in use",
                                 hint="another program holds it; close it or pick another base "
                                      f"with {PORT_BASE_ENV}")
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        flags: dict[str, Any] = {}
        if os.name == "posix":
            flags["start_new_session"] = True
        elif sys.platform == "win32":
            flags["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(
                subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        with open(self.log_path, "wb") as logf:
            try:
                self.proc = subprocess.Popen(self.argv, stdin=subprocess.DEVNULL, stdout=logf,
                                             stderr=subprocess.STDOUT,
                                             cwd=str(self.log_path.parent), **flags)
            except OSError as exc:
                raise UnavailableError(CAPABILITY, f"cannot run {self.binary}: {exc}") from exc
        deadline = time.monotonic() + self.start_timeout
        while True:
            rc = self.proc.poll()
            if rc is not None:
                tail = _log_tail(self.log_path)
                self.proc = None
                raise ActionFailedError(f"hw_server exited with code {rc} before it listened"
                                        f"{f': {tail}' if tail else ''}",
                                        hint=f"log {self.log_path}")
            if port_in_use(self.port):
                return self
            if time.monotonic() > deadline:
                self.stop()
                raise ActionFailedError(
                    f"hw_server did not listen on 127.0.0.1:{self.port} within "
                    f"{self.start_timeout:.0f}s", hint=f"log {self.log_path}")
            time.sleep(0.1)

    def alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def stop(self) -> int | None:
        """SIGTERM its process group, then SIGKILL. Only ever our own child."""
        proc, self.proc = self.proc, None
        if proc is None:
            return None
        if proc.poll() is None:
            self._signal(proc, signal.SIGTERM)
            try:
                proc.wait(self.stop_timeout)
            except subprocess.TimeoutExpired:
                self._signal(proc, getattr(signal, "SIGKILL", signal.SIGTERM))
                with contextlib.suppress(subprocess.TimeoutExpired):
                    proc.wait(2.0)
        return proc.returncode

    @staticmethod
    def _signal(proc: subprocess.Popen, sig: int) -> None:
        if os.name == "posix":
            with contextlib.suppress(OSError):
                os.killpg(proc.pid, sig)            # start_new_session: the group is ours
                return
        with contextlib.suppress(OSError):
            if sig == signal.SIGTERM:
                proc.terminate()
            else:
                proc.kill()


def _reap_recorded_hw_server(rec: dict[str, Any]) -> bool:
    """Stop the hw_server a dead owner left, only while it still runs the recorded argv."""
    hw = rec.get("hw_server") or {}
    pid = int(hw.get("pid") or 0)
    argv = [str(a) for a in hw.get("argv") or []]
    if pid <= 0 or not argv or not _pid_alive(pid):
        return False
    now = _cmdline(pid)
    if not now or now != argv:
        return False                                # pid reuse: never signal a stranger
    with contextlib.suppress(OSError):
        if os.name == "posix":
            os.killpg(pid, signal.SIGTERM)
        else:
            os.kill(pid, signal.SIGTERM)
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline and _pid_alive(pid):
        time.sleep(0.05)
    if _pid_alive(pid) and os.name == "posix":
        with contextlib.suppress(OSError):
            os.killpg(pid, signal.SIGKILL)
    return True


# --- ports ------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class XvcPorts:
    relay: int
    hw_server: int

    @classmethod
    def block(cls, base: int) -> XvcPorts:
        return cls(relay=base, hw_server=base + 1)

    def all(self) -> tuple[int, int]:
        return (self.relay, self.hw_server)


# --- the Vivado Tcl -------------------------------------------------------------------------------


def vivado_tcl(*, hw_server_url: str = "", xvc_url: str = "", ltx: str | None = None,
               byo: bool = False) -> str:
    """The snippet a user pastes into the Vivado Tcl console.

    M1 connects to HM's hw_server (``hw_server_url``, which already has the XVC target
    open); ``byo`` connects to the user's own hw_server on 3121 and opens the relay
    (``xvc_url``) itself. After a swap only the probes and refresh lines are re-run.
    """
    lines = [f"# {PARTITION_SCOPE}", "open_hw_manager"]
    if byo:
        lines += ["connect_hw_server -url localhost:3121", f"open_hw_target -xvc_url {xvc_url}"]
    else:
        lines += [f"connect_hw_server -url {hw_server_url}", "open_hw_target"]
    lines.append("current_hw_device [lindex [get_hw_devices] 0]")
    if ltx:
        lines += [f"set_property PROBES.FILE {{{ltx}}} [current_hw_device]",
                  f"set_property FULL_PROBES.FILE {{{ltx}}} [current_hw_device]"]
    lines.append("refresh_hw_device [current_hw_device]")
    return "\n".join(lines) + "\n"


# --- status -----------------------------------------------------------------------------------------


@dataclass(frozen=True)
class XvcStatus:
    state: str = "down"            # STATES
    open: bool = False             # a session exists (it may be reconnecting or swapping)
    mode: str = ""                 # "m1" (HM's hw_server) | "byo" (your own)
    relay_port: int = 0
    hw_server_port: int = 0
    hw_server_pid: int = 0
    hw_server: str = ""            # the binary, with its Vivado version when the path says
    url: str = ""                  # what Vivado connects to (M1: localhost:H; byo: 127.0.0.1:R)
    attached: dict | None = None   # {peer, pid, command, since, bytes_up, bytes_down, shifts}
    board_slot: str = "unknown"    # ours | held | refused | down | released | unknown
    reach: str = ""                # hub-tunnel | board-ssh | direct
    ltx: dict = field(default_factory=dict)       # {rm, static, full, preferred}
    warnings: tuple[str, ...] = ()
    scope: str = PARTITION_SCOPE
    rm_id: str = ""
    rm_name: str = ""
    reason: str = ""               # why XVC cannot be used here ("" when it can)
    detail: str = ""

    def to_json(self) -> dict[str, Any]:
        from dataclasses import asdict

        out = asdict(self)
        out["warnings"] = list(self.warnings)
        return out


# --- the service ------------------------------------------------------------------------------------


@dataclass
class _Live:
    board_id: str
    session: BoardSession
    adapter: Any
    relay: XvcRelay
    ports: XvcPorts
    mode: str
    hw: HwServer | None = None
    binary: str = ""
    rm_id: str = ""
    rm_name: str = ""
    probes: dict[str, Any] = field(default_factory=dict)
    facts: dict[str, Any] = field(default_factory=dict)
    swapping: str = ""             # non-empty while a swap holds the session
    failed: str = ""
    detail: str = ""


def _safe_name(board_id: str) -> str:
    return "".join(c if c.isalnum() or c in "-_." else "_" for c in board_id)


def _bus_of(engine: Any) -> EventBus | None:
    if engine is None:
        return None
    if isinstance(engine, EventBus):
        return engine
    return getattr(engine, "bus", None)


def _version_key(text: str) -> tuple[int, ...]:
    try:
        return tuple(int(p) for p in str(text).split("."))
    except ValueError:
        return ()


class XvcService:
    """Fabric debug sessions over the harness's XVC server, one per board.

    ``engine`` may be the Engine (``bus``, ``state_dir``, ``store``, ``session()``), an
    ``EventBus`` or ``None``. ``leases``: an object with ``view(hub) -> {lease: {mine,
    holder}}`` (the daemon passes its ``LeaseService``); by default one is made on the
    state dir when a board has a hub.
    """

    def __init__(self, engine: Any = None, *, port_base: int | None = None,
                 hw_server: str | None = None, leases: Any = None,
                 start_timeout: float = 60.0, stop_timeout: float = 5.0,
                 acquire_timeout: float = 8.0) -> None:
        self.engine = None if isinstance(engine, EventBus) else engine
        self.bus = _bus_of(engine)
        self.port_base = port_base
        self.hw_server_binary = hw_server
        self.leases = leases
        self.start_timeout = start_timeout
        self.stop_timeout = stop_timeout
        self.acquire_timeout = acquire_timeout
        self._live: dict[str, _Live] = {}
        self._locks: dict[str, threading.RLock] = {}
        self._guard = threading.Lock()
        self._reserved: set[int] = set()
        self._last: dict[str, XvcStatus] = {}          # the last state of a closed session
        self._known: set[str] = set()                  # boards whose identity was read here
        self._idents: dict[str, Any] = {}              # ...and what it said, last time
        self.threads: list[threading.Thread] = []      # swap/lease workers (tests join them)
        self._unsubs: list[Callable[[], None]] = []
        if self.bus is not None:
            self._unsubs += [
                self.bus.subscribe("deploy.started", self._on_deploy_started),
                self.bus.subscribe("deploy.done", self._on_deploy_done),
                self.bus.subscribe("deploy.failed", self._on_deploy_failed),
                self.bus.subscribe("lease.state", self._on_lease_state),
            ]

    # -- where things live -------------------------------------------------------------------

    @property
    def state_dir(self) -> Path:
        sd = getattr(self.engine, "state_dir", None)
        if sd is not None:
            return Path(sd)
        env = os.environ.get(STATE_DIR_ENV)
        return Path(env) if env else Path.home() / ".config" / "harness-manager"

    @property
    def registry_dir(self) -> Path:
        return self.state_dir / "xvc"

    def _record_path(self, board_id: str) -> Path:
        return self.registry_dir / f"{_safe_name(board_id)}.json"

    def log_path(self, board_id: str) -> Path:
        return self.registry_dir / f"{_safe_name(board_id)}.hw_server.log"

    def _read_record(self, board_id: str) -> dict[str, Any] | None:
        try:
            return json.loads(self._record_path(board_id).read_text())
        except (OSError, ValueError):
            return None

    def _write_record(self, live: _Live) -> None:
        self.registry_dir.mkdir(parents=True, exist_ok=True)
        rec = {"board_id": live.board_id, "mode": live.mode, "relay_port": live.ports.relay,
               "hw_server_port": live.ports.hw_server,
               "hw_server": ({"pid": live.hw.pid, "argv": live.hw.argv}
                             if live.hw is not None and live.hw.alive() else None),
               "owner": {"pid": os.getpid(), "host": socket.gethostname(),
                         "user": getpass.getuser()},
               "started_at": time.time()}
        path = self._record_path(live.board_id)
        tmp = path.with_suffix(f".tmp{os.getpid()}")
        tmp.write_text(json.dumps(rec, indent=1))
        os.replace(tmp, path)

    def _drop_record(self, board_id: str) -> None:
        self._record_path(board_id).unlink(missing_ok=True)

    def _board_lock(self, board_id: str) -> threading.RLock:
        with self._guard:
            return self._locks.setdefault(board_id, threading.RLock())

    def _get(self, board_id: str) -> _Live | None:
        with self._guard:
            return self._live.get(board_id)

    # -- events -----------------------------------------------------------------------------

    def _publish(self, board_id: str, status: XvcStatus) -> None:
        if self.bus is not None:
            self.bus.publish(Event(TOPIC, board_id, status.to_json()))

    def _publish_live(self, board_id: str) -> None:
        live = self._get(board_id)
        if live is not None:
            self._publish(board_id, self._status_of(live))

    # -- the adapter ---------------------------------------------------------------------------

    def _adapter(self, session: BoardSession) -> Any:
        adapter = getattr(session, "xvc", None)
        if adapter is None:
            raise UnavailableError(CAPABILITY, "this board session has no XVC route (it needs "
                                               "the Ethernet link to the harness)")
        store = getattr(self.engine, "store", None)
        use_store = getattr(adapter, "use_store", None)
        if store is not None and callable(use_store):
            with contextlib.suppress(Exception):
                use_store(store)
        return adapter

    @staticmethod
    def _facts(adapter: Any) -> dict[str, Any]:
        try:
            facts = dict(adapter.xvc_facts() or {})
        except HarnessError as exc:
            facts = {"notes": [f"facts unavailable: {exc.message}"]}
        facts.setdefault("scope", PARTITION_SCOPE)
        return facts

    # -- the lease --------------------------------------------------------------------------

    def _lease_service(self) -> Any:
        if self.leases is None:
            from harness_manager.services.lease import LeaseService

            self.leases = LeaseService(self.state_dir, self.bus)
        return self.leases

    def check_lease(self, session: BoardSession) -> str:
        """``""`` for a board with no hub; the holder when the lease is ours; else HeldError."""
        hub = getattr(session, "hub", None)
        if hub is None:
            return ""
        target = getattr(hub, "target", "") or "the board"
        leases = self._lease_service()
        forget = getattr(leases, "forget", None)
        if callable(forget):
            forget(hub)            # a fresh answer: a lease taken a moment ago counts
        try:
            view = leases.view(hub)
        except HarnessError as exc:
            raise HeldError(f"cannot confirm you hold the lease on {target}: {exc.message}",
                            holder="unknown (the hub did not answer)",
                            hint="XVC opens for the lease holder only; retry when the hub "
                                 "answers (`harness-manager lease show TARGET`)") from exc
        lease = (view or {}).get("lease")
        if not lease:
            raise HeldError(f"XVC is for the lease holder only, and nobody holds {target}",
                            holder="nobody",
                            hint="take the lease first: `harness-manager lease acquire TARGET`")
        if not lease.get("mine"):
            who = lease.get("holder") or "someone else"
            raise HeldError(f"XVC is for the lease holder only: {who} holds {target}",
                            holder=who,
                            hint="ask for the board: `harness-manager lease request TARGET`")
        return str(lease.get("holder") or "")

    # -- ports -------------------------------------------------------------------------------

    def _pinned_base(self) -> int | None:
        if self.port_base is not None:
            return self.port_base
        env = os.environ.get(PORT_BASE_ENV)
        return int(env) if env else None

    def _candidates(self, board_id: str) -> Iterator[XvcPorts]:
        pinned = self._pinned_base()
        if pinned is not None:
            yield XvcPorts.block(pinned)
            return
        first = zlib.crc32(board_id.encode()) % PORT_SLOTS
        for i in range(PORT_SLOTS):
            yield XvcPorts.block(DEFAULT_PORT_BASE + PORT_BLOCK * ((first + i) % PORT_SLOTS))

    def _allocate(self, board_id: str, prefer: XvcPorts | None = None) -> XvcPorts:
        pinned = self._pinned_base() is not None
        blocks = ([prefer] if prefer is not None else []) + list(self._candidates(board_id))
        for ports in blocks:
            with self._guard:
                taken = [p for p in ports.all() if p in self._reserved or p in AVOID_PORTS
                         or port_in_use(p)]
                if not taken:
                    self._reserved.update(ports.all())
                    return ports
            if pinned:
                raise PortBoundError(
                    f"local port {taken[0]} (XVC block {ports.relay}-{ports.hw_server}) is "
                    "already in use",
                    hint=f"the base is pinned ({PORT_BASE_ENV} / port_base), so it is not moved; "
                         "free it or pin another base")
        raise PortBoundError(
            f"no free local port pair in {DEFAULT_PORT_BASE}-"
            f"{DEFAULT_PORT_BASE + PORT_BLOCK * PORT_SLOTS - 1}",
            hint=f"pin a free base with {PORT_BASE_ENV}")

    def _release_ports(self, ports: XvcPorts) -> None:
        with self._guard:
            self._reserved.difference_update(ports.all())

    # -- identity and probes files ------------------------------------------------------------

    def _identity(self, session: BoardSession) -> Any:
        try:
            ident = session.identity()
        except HarnessError as exc:
            log.info("xvc: identity read failed (%s); using the probe's", exc)
            ident = getattr(session.candidate, "identity", None)
        note = getattr(session.xvc, "xvc_note_identity", None) if session.xvc else None
        if ident is not None and callable(note):
            note(ident)
        if ident is not None:
            self._known.add(session.candidate.board_id)
            self._idents[session.candidate.board_id] = ident
        return ident

    def _probes(self, adapter: Any, rm_id: str, hw_version: str = "") -> dict[str, Any]:
        try:
            probes = dict(adapter.xvc_probes(rm_id) or {})
        except HarnessError as exc:
            probes = {"note": exc.message}
        preferred = None
        for key in ("full", "rm"):                      # X5: the full-design file first
            item = probes.get(key)
            if item and item.get("path"):
                preferred = key
                break
        probes["preferred"] = preferred
        vivado = str(probes.get("vivado") or "")
        if vivado and hw_version and _version_key(vivado) > _version_key(hw_version):
            probes["version_warning"] = (
                f"the probes file was written by Vivado {vivado}; this hw_server is "
                f"{hw_version}: use Vivado {vivado} or newer")
        return probes

    @staticmethod
    def _ltx_public(probes: dict[str, Any]) -> dict[str, Any]:
        def one(item: Any) -> Any:
            if not item:
                return None
            return {k: (str(v) if isinstance(v, Path) else v) for k, v in item.items()}

        return {"rm": one(probes.get("rm")), "static": one(probes.get("static")),
                "full": one(probes.get("full")), "preferred": probes.get("preferred"),
                "note": probes.get("note", ""), "static_note": probes.get("static_note", "")}

    # -- status ------------------------------------------------------------------------------

    def _state_of(self, live: _Live) -> str:
        if live.failed:
            return "failed"
        if live.swapping:
            return "swapping"
        st = live.relay.status()
        if st["held"]:
            return "swapping"
        slot = st["board_slot"]
        if slot == "ours":
            return "attached" if st["attached"] else "ready"
        if slot == "held":
            return "held"
        return "down"

    def _status_of(self, live: _Live) -> XvcStatus:
        st = live.relay.status()
        warnings = list(live.facts.get("warnings") or ())
        if live.probes.get("version_warning"):
            warnings.append(live.probes["version_warning"])
        if live.mode == "byo":
            warnings.append("your own hw_server lingers 20 s after its last client: after a "
                            "swap, close and reopen the target (or wait it out)")
        warnings.append(SWAP_NOTE)
        hw = live.hw
        url = (f"localhost:{live.ports.hw_server}" if live.mode == "m1"
               else f"127.0.0.1:{live.ports.relay}")
        slot_problem = (f"the board's slot is {st['board_slot']}: {st['slot_detail']}"
                        if st["board_slot"] not in ("ours", "released") and st["slot_detail"]
                        else "")
        detail = slot_problem or live.failed or live.detail
        return XvcStatus(
            state=self._state_of(live), open=True, mode=live.mode,
            relay_port=live.ports.relay,
            hw_server_port=live.ports.hw_server if live.mode == "m1" else 0,
            hw_server_pid=hw.pid if hw is not None and hw.alive() else 0,
            hw_server=(f"{live.binary} ({hw.version})" if hw is not None and hw.version
                       else live.binary),
            url=url, attached=st["attached"], board_slot=st["board_slot"],
            reach=str(live.facts.get("reach") or ""), ltx=self._ltx_public(live.probes),
            warnings=tuple(warnings), scope=str(live.facts.get("scope") or PARTITION_SCOPE),
            rm_id=live.rm_id, rm_name=live.rm_name, detail=detail)

    def identity_known(self, session: BoardSession) -> bool:
        """Has this service read the board's identity (or did the probe carry one)?"""
        return (session.candidate.board_id in self._known
                or getattr(session.candidate, "identity", None) is not None)

    def status(self, session: BoardSession, *, refresh: bool = False) -> XvcStatus:
        """The session's state. Never touches the board (safe while a job runs), unless
        ``refresh`` and no session is open: then it reads the identity once, so the reason
        XVC cannot be used is the board's, not a guess."""
        board_id = session.candidate.board_id
        live = self._get(board_id)
        if live is not None:
            return self._status_of(live)
        if refresh and getattr(session, "xvc", None) is not None:
            self._identity(session)
        last = self._last.get(board_id)
        adapter = getattr(session, "xvc", None)
        if adapter is None:
            return XvcStatus(state="down", reason="this board session has no XVC route",
                             detail=last.detail if last else "")
        facts = self._facts(adapter)
        try:
            reason = str(adapter.xvc_reason() or "")
        except HarnessError as exc:
            reason = exc.message
        return XvcStatus(state=last.state if last and last.state == "failed" else "down",
                         reach=str(facts.get("reach") or ""),
                         warnings=tuple(facts.get("warnings") or ()),
                         scope=str(facts.get("scope") or PARTITION_SCOPE), reason=reason,
                         detail=last.detail if last else "")

    # -- open / close ---------------------------------------------------------------------------

    def check_open(self, session: BoardSession, *, byo: bool = False) -> None:
        """Everything ``open`` refuses for, without taking anything: already open, another
        process's session, not the lease holder, no XVC on this image, no hw_server. It
        reads the board's identity (the daemon calls it under the board gate, before a job)."""
        with self._board_lock(session.candidate.board_id):
            self._prechecks(session, byo=byo)

    def _prechecks(self, session: BoardSession, *, byo: bool) -> tuple[Any, Any, str]:
        board_id = session.candidate.board_id
        if self._get(board_id) is not None:
            st = self._status_of(self._live[board_id])
            raise AlreadyError(f"the XVC session for {board_id} is already open",
                               hint=f"Vivado: {st.url}; `xvc close` first to restart it")
        adapter = self._adapter(session)
        self._check_foreign_owner(board_id)
        self.check_lease(session)                         # X6 / lease-holder-only
        ident = self._identity(session)
        reason = str(adapter.xvc_reason() or "")
        if reason:
            raise UnavailableError(CAPABILITY, reason)
        binary = "" if byo else (self.hw_server_binary or find_hw_server())
        return adapter, ident, binary

    def open(self, session: BoardSession, *, byo: bool = False) -> XvcStatus:
        """Take the board's XVC slot behind a relay, and (unless ``byo``) start HM's hw_server.

        Exit codes: 12 no XVC here / no hw_server, 4 not the lease holder or the board's
        slot is held, 8 already open, 5 a local port is taken, 7 the board did not answer.
        """
        board_id = session.candidate.board_id
        with self._board_lock(board_id):
            adapter, ident, binary = self._prechecks(session, byo=byo)
            rm_id = str(getattr(ident, "rm_id", "") or "")
            rm_name = str(getattr(ident, "rm_name", "") or "")
            facts = self._facts(adapter)
            ports = self._allocate(board_id)
            relay: XvcRelay | None = None
            live: _Live | None = None
            try:
                self._publish(board_id, XvcStatus(state="starting", open=True,
                                                  mode="byo" if byo else "m1",
                                                  relay_port=ports.relay,
                                                  reach=str(facts.get("reach") or ""),
                                                  warnings=tuple(facts.get("warnings") or ()),
                                                  detail="taking the board's XVC slot"))
                relay = XvcRelay(adapter.xvc_endpoint, port=ports.relay,
                                 on_change=lambda: self._publish_live(board_id),
                                 refused_why=self._refused_why(adapter),
                                 label=f"xvc relay {board_id}")
                live = _Live(board_id, session, adapter, relay, ports, "byo" if byo else "m1",
                             binary=binary, rm_id=rm_id, rm_name=rm_name, facts=facts)
                relay.start()
                relay.acquire(self.acquire_timeout)
                if not byo:
                    live.hw = HwServer(binary, ports.hw_server, f"127.0.0.1:{ports.relay}",
                                       self.log_path(board_id), start_timeout=self.start_timeout,
                                       stop_timeout=self.stop_timeout).start()
                live.probes = self._probes(adapter, rm_id, live.hw.version if live.hw else "")
                with self._guard:
                    self._live[board_id] = live
                self._last.pop(board_id, None)
                self._write_record(live)
            except BaseException as exc:
                if live is not None and live.hw is not None:
                    live.hw.stop()
                if relay is not None:
                    relay.close()
                self._adapter_release(adapter)
                self._release_ports(ports)
                if isinstance(exc, HarnessError):
                    failed = XvcStatus(state="failed", reach=str(facts.get("reach") or ""),
                                       warnings=tuple(facts.get("warnings") or ()),
                                       detail=str(exc))
                    self._last[board_id] = failed
                    self._publish(board_id, failed)
                raise
            status = self._status_of(live)
            self._publish(board_id, status)
            return status

    @staticmethod
    def _refused_why(adapter: Any) -> Callable[[float], str] | None:
        since = getattr(adapter, "xvc_open_failures_since", None)
        if not callable(since):
            return None

        def why(t0: float) -> str:
            try:
                lines = since(t0)
            except Exception:  # noqa: BLE001 - only an explanation
                return ""
            return f"the far end refused it ({lines[-1]})" if lines else ""

        return why

    @staticmethod
    def _adapter_release(adapter: Any) -> None:
        release = getattr(adapter, "xvc_release", None)
        if callable(release):
            try:
                release()
            except Exception:  # noqa: BLE001 - closing must always finish
                log.exception("releasing the XVC route failed")

    def _check_foreign_owner(self, board_id: str) -> None:
        """Another live process has this board's XVC session: HELD. A dead one: reap it."""
        rec = self._read_record(board_id)
        if rec is None:
            return
        owner = rec.get("owner") or {}
        pid = int(owner.get("pid") or 0)
        if owner.get("host") and owner.get("host") != socket.gethostname():
            self._drop_record(board_id)
            return
        if pid and pid != os.getpid() and _pid_alive(pid):
            who = f"{owner.get('user', '?')} (pid {pid})"
            raise HeldError(f"the XVC session for {board_id} belongs to another process",
                            holder=who, hint=f"held by {who}; close it there first")
        if pid != os.getpid() and _reap_recorded_hw_server(rec):
            log.warning("stopped an orphaned hw_server for %s (its owner pid %d is gone)",
                        board_id, pid)
        self._drop_record(board_id)

    def close(self, session: BoardSession, *, reason: str = "") -> XvcStatus:
        """Kick the client, stop HM's hw_server, free the board's slot. Safe when not open."""
        return self._close(session.candidate.board_id, reason or "closed")

    def _close(self, board_id: str, reason: str, *, failed: bool = False) -> XvcStatus:
        with self._board_lock(board_id):
            with self._guard:
                live = self._live.pop(board_id, None)
                if live is not None:        # the reason is visible the moment it is closed
                    st = XvcStatus(state="failed" if failed else "down",
                                   reach=str(live.facts.get("reach") or ""),
                                   warnings=tuple(live.facts.get("warnings") or ()),
                                   rm_id=live.rm_id, rm_name=live.rm_name, detail=reason)
                    self._last[board_id] = st
            if live is None:
                return XvcStatus(state="down", detail="no XVC session was open")
            live.relay.close()
            if live.hw is not None:
                live.hw.stop()
            self._adapter_release(live.adapter)
            self._release_ports(live.ports)
            self._drop_record(board_id)
            self._publish(board_id, st)
            return st

    # -- probes files and the Tcl ------------------------------------------------------------------

    def probes(self, session: BoardSession, *, refresh: bool = False) -> dict[str, Any]:
        """The probes files for the loaded design (``refresh`` reads the board's identity)."""
        live = self._get(session.candidate.board_id)
        if live is not None and not refresh:
            return self._ltx_public(live.probes)
        adapter = self._adapter(session)
        board_id = session.candidate.board_id
        ident = self._identity(session) if refresh else (
            self._idents.get(board_id) or getattr(session.candidate, "identity", None))
        rm_id = str(getattr(ident, "rm_id", "") or "")
        return self._ltx_public(self._probes(adapter, rm_id))

    def ltx(self, session: BoardSession, which: str = "auto", *,
            refresh: bool = False) -> dict[str, Any]:
        """``{which, path, name, crc_ok, source, ...}`` of one probes file; AbsentError if none."""
        from harness_manager.core.errors import AbsentError

        if which not in ("auto", "rm", "static", "full"):
            raise UsageError(f"which must be auto, rm, static or full, not {which!r}")
        probes = self.probes(session, refresh=refresh)
        key = probes.get("preferred") if which == "auto" else which
        item = probes.get(key) if key else None
        if not item or not item.get("path"):
            note = (probes.get("static_note") if which == "static" else "") or \
                probes.get("note") or ""
            kind = {"auto": "", "rm": "RM ", "static": "static ", "full": "full-design "}[which]
            raise AbsentError(f"no {kind}probes file (.ltx) for the loaded design"
                              f"{f' ({note})' if note else ''}",
                              hint="the RM's .ltx comes with its overlay; the static and "
                                   "full-design ones with the mint (import them)")
        return {"which": key, **item}

    def tcl(self, session: BoardSession, *, byo: bool | None = None,
            refresh: bool = False) -> dict[str, Any]:
        """``{tcl, url, ltx, which, mode, scope}``: the snippet for what is open (or would be).
        ``refresh`` (no session open): read the board's identity for the loaded design."""
        live = self._get(session.candidate.board_id)
        mode = live.mode if live is not None else ("byo" if byo else "m1")
        if byo is not None and live is not None and (mode == "byo") != byo:
            mode = "byo" if byo else "m1"
        probes = (self._ltx_public(live.probes) if live is not None
                  else self.probes(session, refresh=refresh))
        key = probes.get("preferred")
        ltx = (probes.get(key) or {}).get("path") if key else None
        if live is not None:
            hw_url, xvc_url = (f"localhost:{live.ports.hw_server}",
                               f"127.0.0.1:{live.ports.relay}")
        else:
            hw_url, xvc_url = "localhost:<H: run `xvc open` first>", "127.0.0.1:<R>"
        text = vivado_tcl(hw_server_url=hw_url, xvc_url=xvc_url, ltx=ltx, byo=mode == "byo")
        return {"tcl": text, "url": xvc_url if mode == "byo" else hw_url, "ltx": ltx,
                "which": key, "mode": mode, "scope": PARTITION_SCOPE, "open": live is not None}

    # -- swaps -------------------------------------------------------------------------------------

    def _on_deploy_started(self, event: Event) -> None:
        board_id = event.board_id
        with self._board_lock(board_id):
            live = self._get(board_id)
            if live is None:
                return
            what = event.data.get("overlay") or event.data.get("rm_id") or "a new design"
            live.swapping = f"partition swap to {what}"
            live.detail = f"closed for a partition swap to {what}; it reopens on the new design"
            live.relay.hold("partition swap")             # kick + free the board's slot
            if live.hw is not None:
                live.hw.stop()                              # a FRESH one after the swap
            self._write_record(live)
        self._publish_live(board_id)

    def _on_deploy_done(self, event: Event) -> None:
        board_id = event.board_id
        live = self._get(board_id)
        if live is None or not live.swapping:
            return
        if not event.data.get("verified"):
            self._close(board_id, "XVC not reopened: the swap was not verified by the board; "
                                  "check what is loaded")
            return
        rm_id = str(event.data.get("rm_id") or "")
        worker = threading.Thread(target=self._reopen, args=(board_id, rm_id), daemon=True,
                                  name=f"xvc-reopen-{board_id}")
        self.threads.append(worker)
        worker.start()

    def _reopen(self, board_id: str, rm_id: str) -> None:
        with self._board_lock(board_id):
            live = self._get(board_id)
            if live is None:
                return
            try:
                live.relay.release()
                live.relay.acquire(self.acquire_timeout)
                if live.mode == "m1":
                    live.hw = HwServer(live.binary, live.ports.hw_server,
                                       f"127.0.0.1:{live.ports.relay}", self.log_path(board_id),
                                       start_timeout=self.start_timeout,
                                       stop_timeout=self.stop_timeout).start()
            except HarnessError as exc:
                live.swapping = ""
                self._close(board_id, f"XVC not reopened after the swap: {exc.message}",
                            failed=True)
                return
            if rm_id:
                live.rm_id = rm_id
            ident = self._identity(live.session) if not rm_id else None
            if ident is not None:
                live.rm_id = str(getattr(ident, "rm_id", "") or live.rm_id)
            live.rm_name = self._rm_name(live) or live.rm_name
            live.probes = self._probes(live.adapter, live.rm_id,
                                       live.hw.version if live.hw else "")
            live.swapping = ""
            name = live.rm_name or live.rm_id or "the new design"
            live.detail = (f"reopened on {name}: re-run the probes lines of `xvc tcl` "
                           "(or refresh_hw_device)")
            self._write_record(live)
        self._publish_live(board_id)

    @staticmethod
    def _rm_name(live: _Live) -> str:
        name = getattr(live.adapter, "xvc_rm_name", None)
        if callable(name) and live.rm_id:
            with contextlib.suppress(Exception):
                return str(name(live.rm_id) or "")
        return ""

    def _on_deploy_failed(self, event: Event) -> None:
        live = self._get(event.board_id)
        if live is None or not live.swapping:
            return                       # a preflight refusal: nothing was held
        stage = event.data.get("stage") or "an unknown stage"
        reason = event.data.get("reason", "")
        self._close(event.board_id, f"XVC not reopened: the swap failed at {stage}"
                                    f"{f' ({reason})' if reason else ''}; check what is loaded")

    # -- leases ------------------------------------------------------------------------------------

    def _on_lease_state(self, event: Event) -> None:
        state = str(event.data.get("state") or "")
        if state not in ("released", "expired", "lost"):
            return
        live = self._get(event.board_id)
        if live is None:
            return
        live.relay.hold(f"lease {state}")                # kick at once, free the slot
        worker = threading.Thread(target=self._close, args=(event.board_id,
                                                            f"closed: the lease was {state}"),
                                  daemon=True, name=f"xvc-lease-{event.board_id}")
        self.threads.append(worker)
        worker.start()

    # -- shutdown ----------------------------------------------------------------------------------

    def open_boards(self) -> list[str]:
        with self._guard:
            return list(self._live)

    def close_all(self) -> None:
        for board_id in self.open_boards():
            try:
                self._close(board_id, "engine closed")
            except Exception:  # noqa: BLE001 - every session must still be closed
                log.exception("closing the XVC session for %s", board_id)

    def shutdown(self) -> None:
        """Close every session and stop listening for events."""
        self.close_all()
        for unsub in self._unsubs:
            unsub()
        self._unsubs.clear()
