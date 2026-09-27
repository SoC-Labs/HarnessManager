"""A fake XVC 1.0 server on 127.0.0.1 (lane XVC-CORE; promoted from the design spike,
``tests/spikes/xvc_fake_server.py``, which stays as the spike left it).

It is modelled on the shell's own server, ``firmware/xvc_server/xvc_server.c``
(platform ``feat/rm-ila-mint``; the Linux harness runs the same file inside
``mps3-harnessd``), so the behaviours Harness Manager has to cope with are the real
ones:

- ``getinfo:`` -> ``xvcServer_v1.0:2048\\n`` (xvc_server.c:456, xvc_server.h:270).
- ``settck:`` <u32 LE> -> the same 4 bytes (advisory only, xvc_server.c:463-471).
- ``shift:`` <u32 bits LE> <tms> <tdi> -> <tdo>; 0 bits or more than the ACCEPT
  ceiling (4 x 2048, xvc_server.h:305-309) drops the client (fail closed).
- Anything that is not an XVC prefix drops the client (xvc_server.c:437-442).
- **One client.** A second connection is accepted and closed at once, and the
  first is not disturbed (xvc_server.c:516-527).
- **Swap gating.** While ``gated`` is set, ``getinfo:``/``settck:`` still answer and a
  complete ``shift:`` STALLS unanswered until the gate clears (xvc_server.c:495-497).
- **The claim lock** (CLAIMED-LOCK, xvc_server.h ``MPS3_XVC_LOCKED_LINE``). With ``claimed``
  set, a peer other than ``trusted_peer`` (the board itself) gets ONE line, ``{"ok":false,
  "err":"xvc locked: board claimed (use ssh)","code":"locked"}``, then the close, checked at
  accept and before the single-client rule (``lock_refusals`` counts them).

Behind the protocol sits a single IEEE 1149.1 TAP (default IDCODE 0x0A003093, what
Vivado reported for ``debug_bridge_0`` on silicon, platform
``docs/evidence/2026-09-w2/xvc_smoke_20260923.txt``), so a real hw_server can scan a
chain. Every connection, refusal and command is logged in ``events`` with a
monotonic timestamp. ``kick()`` drops the client (a harness restart); ``close()``
stops listening (nothing served: a connect is refused).

Run it alone: ``python -m tests.fakes.xvc_server --port 0`` prints the port.
"""

from __future__ import annotations

import argparse
import socket
import struct
import threading
import time
from dataclasses import dataclass, field

MAX_VECTOR_BITS = 2048
ACCEPT_VECTOR_BITS = 4 * MAX_VECTOR_BITS
GETINFO = b"getinfo:"
SETTCK = b"settck:"
SHIFT = b"shift:"

# --- a minimal IEEE 1149.1 TAP -----------------------------------------------------------

TLR, RTI, SEL_DR, CAP_DR, SH_DR, EX1_DR, PA_DR, EX2_DR, UPD_DR, \
    SEL_IR, CAP_IR, SH_IR, EX1_IR, PA_IR, EX2_IR, UPD_IR = range(16)

_NEXT = {  # state: (next if TMS=0, next if TMS=1)
    TLR: (RTI, TLR), RTI: (RTI, SEL_DR), SEL_DR: (CAP_DR, SEL_IR),
    CAP_DR: (SH_DR, EX1_DR), SH_DR: (SH_DR, EX1_DR), EX1_DR: (PA_DR, UPD_DR),
    PA_DR: (PA_DR, EX2_DR), EX2_DR: (SH_DR, UPD_DR), UPD_DR: (RTI, SEL_DR),
    SEL_IR: (CAP_IR, TLR), CAP_IR: (SH_IR, EX1_IR), SH_IR: (SH_IR, EX1_IR),
    EX1_IR: (PA_IR, UPD_IR), PA_IR: (PA_IR, EX2_IR), EX2_IR: (SH_IR, UPD_IR),
    UPD_IR: (RTI, SEL_DR),
}


class Tap:
    """One TAP: IDCODE after reset, BYPASS for every other instruction."""

    def __init__(self, idcode: int = 0x0A003093, irlen: int = 6, idcode_ir: int = 0x09) -> None:
        self.idcode = idcode & 0xFFFFFFFF
        self.irlen = irlen
        self.idcode_ir = idcode_ir
        self.state = TLR
        self.ir = idcode_ir
        self.ir_sr = 0
        self.dr_sr = 0
        self.dr_len = 32

    def clock(self, tms: int, tdi: int) -> int:
        """One TCK: return TDO for this bit, then take the rising edge."""
        tdo = 0
        if self.state == SH_DR:
            tdo = self.dr_sr & 1
        elif self.state == SH_IR:
            tdo = self.ir_sr & 1
        if self.state == CAP_DR:
            if self.ir == self.idcode_ir:
                self.dr_sr, self.dr_len = self.idcode, 32
            else:
                self.dr_sr, self.dr_len = 0, 1            # BYPASS captures 0
        elif self.state == SH_DR:
            self.dr_sr = (self.dr_sr >> 1) | (tdi << (self.dr_len - 1))
        elif self.state == CAP_IR:
            self.ir_sr = 0b01                             # IEEE 1149.1: xx..01
        elif self.state == SH_IR:
            self.ir_sr = (self.ir_sr >> 1) | (tdi << (self.irlen - 1))
        elif self.state == UPD_IR:
            self.ir = self.ir_sr & ((1 << self.irlen) - 1)
        self.state = _NEXT[self.state][tms & 1]
        if self.state == TLR:
            self.ir = self.idcode_ir
        return tdo

    def shift(self, nbits: int, tms: bytes, tdi: bytes) -> bytes:
        out = bytearray((nbits + 7) // 8)
        for i in range(nbits):
            if self.clock((tms[i >> 3] >> (i & 7)) & 1, (tdi[i >> 3] >> (i & 7)) & 1):
                out[i >> 3] |= 1 << (i & 7)
        return bytes(out)


# --- the server -------------------------------------------------------------------------


@dataclass
class Stats:
    connections: int = 0
    refused_second: int = 0
    getinfo: int = 0
    settck: int = 0
    shifts: int = 0
    shift_bits: int = 0
    stalled_shifts: int = 0
    dropped: int = 0
    events: list[tuple[float, str, str]] = field(default_factory=list)


class FakeXvcServer:
    def __init__(self, host: str = "127.0.0.1", port: int = 0, *, tap: Tap | None = None,
                 echo: bool = False, max_bits: int = MAX_VECTOR_BITS,
                 log_limit: int = 5000) -> None:
        #: echo=True answers a shift with its TDI (no TAP): latency runs time the transport,
        #: not this file's per-bit Python loop.
        self.echo = echo
        self.tap = tap or Tap()
        self.max_bits = max_bits
        self.accept_bits = 4 * max_bits
        self.gated = threading.Event()
        self.stats = Stats()
        #: the claim lock (tests.fakes.claimed_lock): claimed, and who "the board itself" is
        self.claimed = False
        self.trusted_peer = "127.0.0.3"
        self.lock_refusals = 0
        self._log_limit = log_limit
        self._mu = threading.Lock()
        self._client: socket.socket | None = None
        self._client_peer = ""
        self._stop = threading.Event()
        self._srv = socket.socket()
        self._srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._srv.bind((host, port))
        self._srv.listen(8)
        self.host, self.port = self._srv.getsockname()[:2]
        self._t = threading.Thread(target=self._accept_loop, name="fake-xvc-accept", daemon=True)

    # -- views ------------------------------------------------------------------------------

    def event(self, kind: str, detail: str = "") -> None:
        with self._mu:
            if len(self.stats.events) < self._log_limit:
                self.stats.events.append((time.monotonic(), kind, detail))

    def events(self, kind: str | None = None) -> list[tuple[float, str, str]]:
        with self._mu:
            return [e for e in self.stats.events if kind is None or e[1] == kind]

    @property
    def attached(self) -> str:
        """The current client's peer address, or ``""``."""
        with self._mu:
            return self._client_peer if self._client is not None else ""

    # -- lifecycle -------------------------------------------------------------------------

    def start(self) -> FakeXvcServer:
        self._t.start()
        return self

    def close(self) -> None:
        self._stop.set()
        try:
            self._srv.shutdown(socket.SHUT_RDWR)      # wakes the blocked accept (Linux)
        except OSError:
            pass
        try:
            self._srv.close()
        except OSError:
            pass
        with self._mu:
            c = self._client
        if c is not None:
            try:
                c.shutdown(socket.SHUT_RDWR)          # a FIN, and the serving thread wakes
            except OSError:
                pass
            try:
                c.close()
            except OSError:
                pass
        self._t.join(timeout=3)

    def __enter__(self) -> FakeXvcServer:
        return self.start()

    def __exit__(self, *exc: object) -> None:
        self.close()

    def kick(self) -> None:
        """Drop the current client (what a harness restart does to it)."""
        with self._mu:
            c = self._client
        if c is not None:
            try:
                c.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

    # -- the wire --------------------------------------------------------------------------

    def _accept_loop(self) -> None:
        while not self._stop.is_set():
            try:
                conn, peer = self._srv.accept()
            except OSError:
                return
            peer_s = f"{peer[0]}:{peer[1]}"
            if self.claimed and peer[0] != self.trusted_peer:
                from tests.fakes.claimed_lock import XVC_LOCKED_LINE, refuse_with_line

                self.lock_refusals += 1
                self.event("locked", peer_s)
                refuse_with_line(conn, XVC_LOCKED_LINE)
                continue
            with self._mu:
                busy = self._client is not None
                if not busy:
                    self._client, self._client_peer = conn, peer_s
                    self.stats.connections += 1
                else:
                    self.stats.refused_second += 1
            if busy:
                self.event("refused", peer_s)   # accept-then-close, xvc_server.c:526
                conn.close()
                continue
            self.event("connect", peer_s)
            threading.Thread(target=self._serve, args=(conn, peer_s), daemon=True,
                             name=f"fake-xvc-{peer_s}").start()

    def _serve(self, conn: socket.socket, peer: str) -> None:
        conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        buf = bytearray()
        why = "client closed"
        try:
            while not self._stop.is_set():
                rc = self._dispatch(conn, buf)
                if rc < 0:
                    why = "protocol violation: dropped"
                    with self._mu:
                        self.stats.dropped += 1
                    break
                if rc > 0:
                    continue
                if self.gated.is_set() and buf.startswith(SHIFT):
                    time.sleep(0.005)             # a complete shift is waiting on the gate
                    continue
                data = conn.recv(65536)
                if not data:
                    break
                buf += data
        except OSError as exc:
            why = f"socket error: {exc}"
        finally:
            with self._mu:
                if self._client is conn:
                    self._client, self._client_peer = None, ""
            try:
                conn.close()
            except OSError:
                pass
            self.event("disconnect", f"{peer} ({why})")

    def _dispatch(self, conn: socket.socket, buf: bytearray) -> int:
        """1 = one command served; 0 = need more bytes (or a stalled shift); -1 = drop."""
        if not buf:
            return 0
        n = len(buf)
        if not any(bytes(buf[:len(p)]) == p[:min(n, len(p))] for p in (GETINFO, SETTCK, SHIFT)):
            return -1
        if buf.startswith(GETINFO):
            del buf[:len(GETINFO)]
            conn.sendall(f"xvcServer_v1.0:{self.max_bits}\n".encode())
            with self._mu:
                self.stats.getinfo += 1
            self.event("getinfo")
            return 1
        if buf.startswith(SETTCK):
            if n < 11:
                return 0
            echo = bytes(buf[7:11])
            del buf[:11]
            conn.sendall(echo)
            with self._mu:
                self.stats.settck += 1
            self.event("settck", str(struct.unpack("<I", echo)[0]))
            return 1
        if buf.startswith(SHIFT):
            if n < 10:
                return 0
            bits = struct.unpack("<I", bytes(buf[6:10]))[0]
            if bits == 0 or bits > self.accept_bits:
                return -1
            nbytes = (bits + 7) // 8
            total = 10 + 2 * nbytes
            if n < total:
                return 0
            if self.gated.is_set():
                with self._mu:
                    self.stats.stalled_shifts += 1
                return 0
            tms = bytes(buf[10:10 + nbytes])
            tdi = bytes(buf[10 + nbytes:total])
            del buf[:total]
            tdo = tdi if self.echo else self.tap.shift(bits, tms, tdi)
            conn.sendall(tdo)
            with self._mu:
                self.stats.shifts += 1
                self.stats.shift_bits += bits
            return 1
        return 0


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--port", type=int, default=0)
    ap.add_argument("--idcode", type=lambda s: int(s, 0), default=0x0A003093)
    ap.add_argument("--irlen", type=int, default=6)
    ns = ap.parse_args()
    srv = FakeXvcServer(port=ns.port, tap=Tap(ns.idcode, ns.irlen)).start()
    print(f"fake XVC on {srv.host}:{srv.port}", flush=True)
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        srv.close()


if __name__ == "__main__":
    main()
