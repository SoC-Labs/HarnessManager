"""Team T9 test doubles for SYSMON over JTAG.

- ``SysmonRegs``: a SYSMON register file with the DRP JTAG semantics: a read command's
  data comes back on the NEXT DR shift; every command is recorded, and a write is
  recorded separately so tests can prove none is ever sent.
- ``Ku115Tap`` + ``FakeRbbServer``: one KU115-shaped TAP (IR 12, capture ...01, IDCODE
  0x1390D093, SYSMON_DRP 0xDE4, everything else BYPASS) behind OpenOCD's
  ``remote_bitbang`` protocol on 127.0.0.1, for the real-OpenOCD test.
- ``make_fake_xsdb``: an executable that runs our generated xsdb script in a real
  ``tclsh`` with ``connect`` / ``jtag targets`` / ``jtag sequence`` modelled in Tcl
  against the same register semantics (modes: ok, refuse, notarget, readfail).
- ``ScriptedRunner``: a ``subprocess.run`` stand-in for unit tests.
"""

from __future__ import annotations

import socket
import stat
import subprocess
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path

from socharness_board_mps3.sysmon import IR_SYSMON_DRP

IR_IDCODE = 0x249          # BSDL: IDCODE (001001001001)
IR_BYPASS = 0xFFF
IR_CAPTURE = 0x351         # BSDL INSTRUCTION_CAPTURE "XXXXXXXXXX01"
KU115_IDCODE_V1 = 0x1390D093

# Codes for a plausible board: 44.0 degC, 0.951 V, 1.800 V, 0.950 V; internal reference.
def code_for_temp(t: float) -> int:
    return round((t + 273.6777) * 65536 / 501.3743)


def code_for_v(v: float) -> int:
    return round(v * 65536 / 3.0)


FLAG_INTERNAL = 1 << 9
GOOD_REGS: dict[int, int] = {
    0x00: code_for_temp(44.0), 0x01: code_for_v(0.951), 0x02: code_for_v(1.800),
    0x06: code_for_v(0.950),
    0x20: code_for_temp(51.5), 0x21: code_for_v(0.962), 0x22: code_for_v(1.812),
    0x23: code_for_v(0.958),
    0x24: code_for_temp(30.25), 0x25: code_for_v(0.940), 0x26: code_for_v(1.790),
    0x27: code_for_v(0.941),
    0x3F: FLAG_INTERNAL,
}


@dataclass
class SysmonRegs:
    regs: dict[int, int] = field(default_factory=lambda: dict(GOOD_REGS))
    out: int = 0                                  # what the next capture returns
    commands: list[tuple[int, int]] = field(default_factory=list)   # (cmd, addr)
    writes: list[tuple[int, int]] = field(default_factory=list)

    def capture(self) -> int:
        return self.out

    def update(self, word: int) -> None:
        cmd, addr, data = (word >> 26) & 0xF, (word >> 16) & 0x3FF, word & 0xFFFF
        self.commands.append((cmd, addr))
        if cmd == 0b0001:
            self.out = (addr << 16) | (self.regs.get(addr, 0) & 0xFFFF)
        elif cmd == 0b0010:
            self.writes.append((addr, data))


# --- a KU115-shaped TAP behind remote_bitbang ----------------------------------------------

TLR, RTI, SELDR, CAPDR, SHDR, EX1DR, PADR, EX2DR, UPDR = range(9)
SELIR, CAPIR, SHIR, EX1IR, PAIR, EX2IR, UPIR = range(9, 16)
_NEXT = {
    TLR: (RTI, TLR), RTI: (RTI, SELDR),
    SELDR: (CAPDR, SELIR), CAPDR: (SHDR, EX1DR), SHDR: (SHDR, EX1DR),
    EX1DR: (PADR, UPDR), PADR: (PADR, EX2DR), EX2DR: (SHDR, UPDR), UPDR: (RTI, SELDR),
    SELIR: (CAPIR, TLR), CAPIR: (SHIR, EX1IR), SHIR: (SHIR, EX1IR),
    EX1IR: (PAIR, UPIR), PAIR: (PAIR, EX2IR), EX2IR: (SHIR, UPIR), UPIR: (RTI, SELDR),
}


class Ku115Tap:
    IR_LEN = 12

    def __init__(self, sysmon: SysmonRegs, idcode: int = KU115_IDCODE_V1) -> None:
        self.sysmon = sysmon
        self.idcode = idcode
        self.state = TLR
        self.ir = IR_IDCODE
        self.ir_shift = 0
        self.dr_shift = 0
        self.dr_len = 32
        self._tck = 0

    def tdo(self) -> int:
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
            if self.ir == IR_IDCODE:
                self.dr_len, self.dr_shift = 32, self.idcode
            elif self.ir == IR_SYSMON_DRP:
                self.dr_len, self.dr_shift = 32, self.sysmon.capture()
            else:
                self.dr_len, self.dr_shift = 1, 0
        elif st == SHDR:
            self.dr_shift = (self.dr_shift >> 1) | (tdi << (self.dr_len - 1))
        elif st == CAPIR:
            self.ir_shift = IR_CAPTURE
        elif st == SHIR:
            self.ir_shift = (self.ir_shift >> 1) | (tdi << (self.IR_LEN - 1))
        nxt = _NEXT[st][1 if tms else 0]
        self.state = nxt
        if nxt == TLR:
            self.ir = IR_IDCODE
        elif nxt == UPIR:
            self.ir = self.ir_shift & 0xFFF
        elif nxt == UPDR and self.ir == IR_SYSMON_DRP:
            self.sysmon.update(self.dr_shift & 0xFFFFFFFF)


class FakeRbbServer:
    """OpenOCD ``remote_bitbang`` on 127.0.0.1 with one ``Ku115Tap`` behind it."""

    def __init__(self, sysmon: SysmonRegs | None = None, *, idcode: int = KU115_IDCODE_V1) -> None:
        self.sysmon = sysmon or SysmonRegs()
        self.idcode = idcode
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind(("127.0.0.1", 0))
        self._sock.listen(1)
        self._sock.settimeout(0.2)
        self.port = self._sock.getsockname()[1]
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._serve, daemon=True)

    def __enter__(self) -> FakeRbbServer:
        self._thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self._stop.set()
        self._thread.join(timeout=2)
        self._sock.close()

    def _serve(self) -> None:
        while not self._stop.is_set():
            try:
                conn, _ = self._sock.accept()
            except (TimeoutError, OSError):
                continue
            self._client(conn)

    def _client(self, conn: socket.socket) -> None:
        tap = Ku115Tap(self.sysmon, self.idcode)
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
                for ch in data:
                    if 0x30 <= ch <= 0x37:
                        v = ch - 0x30
                        tap.write((v >> 2) & 1, (v >> 1) & 1, v & 1)
                    elif ch == 0x52:                          # 'R'
                        out.append(0x31 if tap.tdo() else 0x30)
                    elif ch == 0x51:                          # 'Q'
                        break
                if out:
                    conn.sendall(bytes(out))
        finally:
            conn.close()


# --- a Tcl model of xsdb, run by a real tclsh --------------------------------------------------

_PRELUDE = r"""
# Fake xsdb for socharness T9 tests. Models only what the generated script uses.
set ::mode {%(mode)s}
set ::regs [dict create %(regs)s]
set ::log [open {%(log)s} w]
set ::connected 0
set ::selected 0
set ::last 0
set ::seqn 0
proc connect {args} {
    if {$::mode eq "refuse"} { error "Connection refused" }
    set ::connected 1
    return tcfchan#0
}
proc disconnect {args} { set ::connected 0 }
proc jtag {sub args} {
    switch -- $sub {
        targets {
            if {!$::connected} { error {Invalid target. Use "connect" command to connect to hw_server/TCF agent} }
            if {$::mode eq "notarget"} { error {no targets found with "name =~ "xcku115*"". available targets: none} }
            set ::selected 1
            return {}
        }
        sequence {
            set name ::socharness_seq[incr ::seqn]
            set ::ops($name) {}
            proc $name {cmd args} "return \[seqcmd $name \$cmd \$args\]"
            return $name
        }
        default { error "unknown jtag subcommand $sub" }
    }
}
proc seqcmd {name cmd a} {
    switch -- $cmd {
        irshift - drshift - delay - state { lappend ::ops($name) [linsert $a 0 $cmd]; return {} }
        run { return [runseq $name] }
        delete { rename $name {}; unset ::ops($name); return {} }
        default { error "bad sequence command $cmd" }
    }
}
proc runseq {name} {
    if {!$::selected} { error {Invalid target. Use "connect" command to connect to hw_server/TCF agent} }
    set out {}
    set ir -1
    foreach op $::ops($name) {
        switch -- [lindex $op 0] {
            irshift { set ir [lindex $op end] }
            drshift {
                if {$ir != 3556} { error "DR shift under IR $ir" }
                if {[lsearch -exact $op -capture] >= 0} { lappend out $::last }
                set v [lindex $op end]
                set c [expr {($v >> 26) & 0xF}]
                set a [expr {($v >> 16) & 0x3FF}]
                puts $::log "$c $a"
                flush $::log
                if {$c == 1} {
                    if {$::mode eq "readfail" && $a == 63} { error "JTAG chain broken" }
                    set d 0
                    if {[dict exists $::regs $a]} { set d [dict get $::regs $a] }
                    set ::last [expr {($a << 16) | $d}]
                }
            }
        }
    }
    return $out
}
source [lindex $argv 0]
"""


def make_fake_xsdb(directory: Path, *, regs: Mapping[int, int] | None = None,
                   mode: str = "ok", tclsh: str = "tclsh") -> tuple[Path, Path]:
    """Write an executable fake ``xsdb``; returns (its path, the DR command log path)."""
    directory.mkdir(parents=True, exist_ok=True)
    log = directory / "dr_commands.log"
    regs = GOOD_REGS if regs is None else regs
    prelude = directory / "fake_xsdb_prelude.tcl"
    prelude.write_text(_PRELUDE % {
        "mode": mode, "log": str(log),
        "regs": " ".join(f"{a} {v}" for a, v in regs.items())}, encoding="utf-8")
    exe = directory / "xsdb"
    exe.write_text(f'#!/bin/sh\nexec {tclsh} "{prelude}" "$@"\n', encoding="utf-8")
    exe.chmod(exe.stat().st_mode | stat.S_IXUSR)
    return exe, log


def logged_commands(log: Path) -> list[tuple[int, int]]:
    return [tuple(int(x) for x in line.split()) for line in log.read_text().splitlines() if line.strip()]


# --- a subprocess.run stand-in ---------------------------------------------------------------


class ScriptedRunner:
    """Answers ``runner(argv, ...)`` with canned output; records argv and any script file."""

    def __init__(self, stdout: str = "", stderr: str = "", returncode: int = 0, *,
                 raises: BaseException | None = None,
                 respond: Callable[[list[str]], str] | None = None) -> None:
        self.stdout, self.stderr, self.returncode = stdout, stderr, returncode
        self.raises = raises
        self.respond = respond
        self.calls: list[list[str]] = []
        self.scripts: list[str] = []
        self.kwargs: list[dict] = []

    def __call__(self, argv: list[str], **kwargs) -> subprocess.CompletedProcess:
        self.calls.append(list(argv))
        self.kwargs.append(kwargs)
        if len(argv) > 1 and argv[1].endswith(".tcl") and Path(argv[1]).is_file():
            self.scripts.append(Path(argv[1]).read_text())
        if self.raises is not None:
            raise self.raises
        out = self.respond(argv) if self.respond else self.stdout
        return subprocess.CompletedProcess(argv, self.returncode, out, self.stderr)


def marker_output(regs: Mapping[int, int], *, extra: str = "") -> str:
    """What a successful xsdb/openocd run prints, for the given codes."""
    lines = [f"SOCHARNESS_SYSMON {a:02x} {((a << 16) | v):08x}" for a, v in regs.items()]
    return "\n".join(lines) + ("\n" + extra if extra else "") + "\n"
