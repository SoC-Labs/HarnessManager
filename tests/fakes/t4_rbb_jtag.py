"""A fake of the MPS3 shell's JTAG server (TCP 6921) for Team T4's tests.

OpenOCD's ``remote_bitbang`` adapter speaks a one-character-per-request ASCII
protocol (OpenOCD doc/manual/jtag/drivers/remote_bitbang.txt): ``0``..``7``
set TCK/TMS/TDI, ``R`` samples TDO, ``r``..``u`` drive TRST/SRST, ``B``/``b``
blink, ``Q`` quits. The firmware twin is ``firmware/jtag_server/jtag_server.c``.

Behind the protocol sits one IEEE 1149.1 TAP with the MPS3 DUT's identity:
the CoreSight SoC-400 SWJ-DP in JTAG mode, IR length 4, IDCODE ``0x6ba00477``
(host/openocd/nanosoc_mps3_jtag.cfg; silicon-proven). A minimal ADIv5 JTAG-DP
answers DPACC/APACC with OK and acknowledges the debug power-up request, which
is all OpenOCD's ``init`` (``transport init`` + ``dap init``) needs while the
target is ``-defer-examine``. It does not model a MEM-AP or a Cortex-M0, so a
gdb attach (which examines the core) is out of scope.

Traps modelled, each from the firmware:

- **One client.** ``jtag_server_poll()`` accepts a second connection and closes
  it at once (jtag_server.c:185-192). ``accepted``/``refused`` count both.
- **No TAP.** ``tdo_stuck`` = 0 or 1 models a design with nothing on the RP
  ``jtag_*`` pins (greybox): the scan reads all zeroes or all ones.

Loopback only (127.0.0.1). Stdlib only.
"""

from __future__ import annotations

import socket
import threading

# TAP controller states (IEEE 1149.1 figure 6-1).
TLR, RTI, SELDR, CAPDR, SHDR, EX1DR, PADR, EX2DR, UPDR = range(9)
SELIR, CAPIR, SHIR, EX1IR, PAIR, EX2IR, UPIR = range(9, 16)

_NEXT = {
    TLR: (RTI, TLR), RTI: (RTI, SELDR),
    SELDR: (CAPDR, SELIR), CAPDR: (SHDR, EX1DR), SHDR: (SHDR, EX1DR),
    EX1DR: (PADR, UPDR), PADR: (PADR, EX2DR), EX2DR: (SHDR, UPDR), UPDR: (RTI, SELDR),
    SELIR: (CAPIR, TLR), CAPIR: (SHIR, EX1IR), SHIR: (SHIR, EX1IR),
    EX1IR: (PAIR, UPIR), PAIR: (PAIR, EX2IR), EX2IR: (SHIR, UPIR), UPIR: (RTI, SELDR),
}

# ADIv5 JTAG-DP instructions (IR = 4 bits).
IR_ABORT, IR_DPACC, IR_APACC, IR_IDCODE, IR_BYPASS = 0x8, 0xA, 0xB, 0xE, 0xF
ACK_OK = 0b010          # JTAG-DP: OK/FAULT
DAP_IDCODE = 0x6BA00477  # nanosoc_mps3_jtag.cfg: _DAP_TAPID
DPIDR = 0x6BA02477       # SoC-400 SWJ-DP DPIDR (HAPS-work openocd/README.md, verify-target leg 1)
CSYSPWRUPREQ, CSYSPWRUPACK = 1 << 30, 1 << 31
CDBGPWRUPREQ, CDBGPWRUPACK = 1 << 28, 1 << 29


class JtagTap:
    """One TAP + a minimal JTAG-DP. Drive it with ``write``/``tdo``/``reset``."""

    def __init__(self, idcode: int = DAP_IDCODE, *, tdo_stuck: int | None = None) -> None:
        self.idcode = idcode
        self.tdo_stuck = tdo_stuck
        self.state = TLR
        self.ir = IR_IDCODE
        self.ir_shift = 0
        self.dr_shift = 0
        self.dr_len = 32
        self._tck = 0
        # JTAG-DP state
        self.ctrl_stat = 0
        self.select = 0
        self.read_result = 0      # returned in the NEXT DPACC/APACC capture (posted reads)
        self.dp_ops = 0

    def reset(self, trst: int) -> None:
        if trst:
            self.state = TLR
            self.ir = IR_IDCODE

    def tdo(self) -> int:
        if self.tdo_stuck is not None:
            return self.tdo_stuck
        if self.state == SHDR:
            return self.dr_shift & 1
        if self.state == SHIR:
            return self.ir_shift & 1
        return 0

    def write(self, tck: int, tms: int, tdi: int) -> None:
        rising = tck and not self._tck
        self._tck = tck
        if not rising:
            return
        st = self.state
        if st == CAPDR:
            self._capture_dr()
        elif st == SHDR:
            self.dr_shift = (self.dr_shift >> 1) | (tdi << (self.dr_len - 1))
        elif st == CAPIR:
            self.ir_shift = 0b0001            # IEEE: capture ...01
        elif st == SHIR:
            self.ir_shift = (self.ir_shift >> 1) | (tdi << 3)
        nxt = _NEXT[st][1 if tms else 0]
        self.state = nxt
        if nxt == TLR:
            self.ir = IR_IDCODE
        elif nxt == UPIR:
            self.ir = self.ir_shift & 0xF
        elif nxt == UPDR:
            self._update_dr()

    # -- DR ---------------------------------------------------------------------

    def _capture_dr(self) -> None:
        if self.ir == IR_IDCODE:
            self.dr_len, self.dr_shift = 32, self.idcode
        elif self.ir in (IR_DPACC, IR_APACC, IR_ABORT):
            self.dr_len = 35
            self.dr_shift = ((self.read_result & 0xFFFFFFFF) << 3) | ACK_OK
        else:
            self.dr_len, self.dr_shift = 1, 0

    def _update_dr(self) -> None:
        if self.ir not in (IR_DPACC, IR_APACC):
            return
        value = self.dr_shift
        rnw = value & 1
        addr = ((value >> 1) & 0x3) << 2
        data = (value >> 3) & 0xFFFFFFFF
        self.dp_ops += 1
        if self.ir == IR_DPACC:
            self._dp(addr, rnw, data)
        else:
            self.read_result = 0 if rnw else self.read_result

    def _dp(self, addr: int, rnw: int, data: int) -> None:
        if addr == 0x0:
            if rnw:
                self.read_result = DPIDR
        elif addr == 0x4:
            if rnw:
                acks = 0
                if self.ctrl_stat & CSYSPWRUPREQ:
                    acks |= CSYSPWRUPACK
                if self.ctrl_stat & CDBGPWRUPREQ:
                    acks |= CDBGPWRUPACK
                self.read_result = (self.ctrl_stat & 0x5FFFFFFF) | acks
            else:
                self.ctrl_stat = data
        elif addr == 0x8:
            if rnw:
                self.read_result = self.select
            else:
                self.select = data
        elif addr == 0xC and rnw:
            pass                              # RDBUFF: keep the previous result


class FakeJtagServer:
    """Loopback remote_bitbang server with the firmware's single-client rule.

    ``mode``: ``"tap"`` (a live DAP), ``"none"`` (TDO stuck low: no TAP), or
    ``"slam"`` (accept then close at once, as the firmware does to a second
    client while another debugger holds the port).
    """

    def __init__(self, *, mode: str = "tap", idcode: int = DAP_IDCODE) -> None:
        self.mode = mode
        self.idcode = idcode
        self.accepted = 0
        self.refused = 0
        self.taps: list[JtagTap] = []
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.bind(("127.0.0.1", 0))
        self._sock.listen(4)
        self._sock.settimeout(0.1)
        self.port = self._sock.getsockname()[1]
        self._active: socket.socket | None = None
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._thread = threading.Thread(target=self._serve, daemon=True)

    def __enter__(self) -> FakeJtagServer:
        self._thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def close(self) -> None:
        self._stop.set()
        self._thread.join(timeout=2)
        self._sock.close()
        with self._lock:
            if self._active is not None:
                self._active.close()

    def _serve(self) -> None:
        while not self._stop.is_set():
            try:
                conn, _ = self._sock.accept()
            except (TimeoutError, OSError):
                continue
            with self._lock:
                busy = self._active is not None
                if not busy and self.mode != "slam":
                    self._active = conn
            if busy or self.mode == "slam":
                # jtag_server.c:185-192: extras are accepted and closed at once.
                self.refused += 1
                conn.close()
                continue
            self.accepted += 1
            threading.Thread(target=self._client, args=(conn,), daemon=True).start()

    def _client(self, conn: socket.socket) -> None:
        tap = JtagTap(self.idcode, tdo_stuck=0 if self.mode == "none" else None)
        self.taps.append(tap)
        conn.settimeout(0.2)
        try:
            while not self._stop.is_set():
                try:
                    data = conn.recv(65536)
                except TimeoutError:
                    continue
                except OSError:
                    break
                if not data:
                    break
                out = bytearray()
                quit_ = False
                for ch in data:
                    if 0x30 <= ch <= 0x37:          # '0'..'7'
                        v = ch - 0x30
                        tap.write((v >> 2) & 1, (v >> 1) & 1, v & 1)
                    elif ch == 0x52:                # 'R'
                        out.append(0x31 if tap.tdo() else 0x30)
                    elif 0x72 <= ch <= 0x75:        # 'r'..'u'
                        tap.reset(((ch - 0x72) >> 1) & 1)
                    elif ch == 0x51:                # 'Q'
                        quit_ = True
                        break
                    # 'B'/'b' blink and anything else: ignored.
                if out:
                    conn.sendall(bytes(out))
                if quit_:
                    break
        finally:
            conn.close()
            with self._lock:
                if self._active is conn:
                    self._active = None
