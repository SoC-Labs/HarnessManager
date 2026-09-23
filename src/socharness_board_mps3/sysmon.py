"""KU115 SYSMON over JTAG: FPGA die temperature, VCCINT, VCCAUX, VCCBRAM and their
min/max since power-up, with NO instantiation in the design. Team T9.

Facts this module is built on (research agent I, 2026-09-23, and the sources it cites):

- **Instruction.** The KU115 IR is 12 bits (BSDL ``xcku115_flvb1760.bsd:2405``) and
  ``SYSMON_DRP = 110111100100 = 0xDE4`` (BSDL :2453; the upper six bits are SLR0's
  0x37). JTAG reaches only the master SLR's SYSMON (SLR0, where the RP also lives;
  UG580 p.9).
- **Data register.** 32 bits: ``[29:26]`` command (0001 read, 0010 write, 0000 no-op),
  ``[25:16]`` DRP address, ``[15:0]`` data. A read's data comes back on the NEXT DR
  shift (UG580 "DRP JTAG Interface"). This module only ever generates read and no-op
  commands: status registers 00h/02h/03h have write side effects (UG580 Fig. 3-1
  notes), so a write is refused in code, not by convention.
- **Registers.** 00h temperature, 01h VCCINT, 02h VCCAUX, 06h VCCBRAM; 20h-23h max
  and 24h-27h min of the same four; 3Fh flags, whose bit 9 (REF) is 1 when the ADC
  uses its internal reference and 0 for the external one, bit 11 (JTGD) is 1 when
  the bitstream disabled JTAG access (UG580 Table 3-2).
- **Conversions.** Supplies: V = code x 3 / 65536. Temperature, on-chip reference
  (UG580 Eq. 2-7): T = code x 501.3743 / 65536 - 273.6777; external reference
  (Eq. 2-5): T = code x 502.9098 / 65536 - 273.8195.
- **The MPS3 caveat.** VREFP (AB17) is tied to the 1.2 V rail, so SYSMON may be on
  an external 1.2 V reference and read about 4 % high (harness handover D8). When
  flag 3Fh says "external", every reading carries that caveat and the VCCAUX
  check (1.80 V nominal). Board check W13 settles it.
- **Tools.** Backend ``xsdb`` drives the hub's hw_server (``jtag sequence``; syntax
  checked against xsdb 2024.1's ``help jtag sequence``). Backend ``openocd`` uses
  ``irscan``/``drscan`` on any JTAG adapter. Both need the J17 JTAG cable: the MPS3's
  on-board CMSIS-DAP does not reach the FPGA configuration TAP. Never use
  openFPGALoader ``--read-xadc``: it writes the configuration registers.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import tempfile
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from socharness.core.errors import UnavailableError, UsageError
from socharness.core.model import Link, LinkKind, Reading

IR_LENGTH = 12
IR_SYSMON_DRP = 0xDE4
IR_IDCODE = 0x249                  # BSDL: IDCODE (001001001001)
KU115_IDCODE = 0x0390D093          # BSDL IDCODE_REGISTER; version nibble ignored
DR_BITS = 32
CMD_NOP = 0b0000
CMD_READ = 0b0001

REG_TEMP, REG_VCCINT, REG_VCCAUX, REG_VCCBRAM = 0x00, 0x01, 0x02, 0x06
REG_MAX = {REG_TEMP: 0x20, REG_VCCINT: 0x21, REG_VCCAUX: 0x22, REG_VCCBRAM: 0x23}
REG_MIN = {REG_TEMP: 0x24, REG_VCCINT: 0x25, REG_VCCAUX: 0x26, REG_VCCBRAM: 0x27}
REG_FLAG = 0x3F
READ_REGS: tuple[int, ...] = (REG_TEMP, REG_VCCINT, REG_VCCAUX, REG_VCCBRAM,
                              *REG_MAX.values(), *REG_MIN.values(), REG_FLAG)

FLAG_OT = 1 << 3
FLAG_REF = 1 << 9                  # 1: internal reference; 0: external (VREFP)
FLAG_JTGR = 1 << 10                # JTAG access restricted to read-only
FLAG_JTGD = 1 << 11                # JTAG access disabled by the bitstream

# (reading name, DRP register, unit, nominal, plausible (lo, hi))
CHANNELS: tuple[tuple[str, int, str, float | None, tuple[float, float]], ...] = (
    ("fpga_die_temp", REG_TEMP, "degC", None, (-55.0, 125.0)),
    ("vccint", REG_VCCINT, "V", 0.95, (0.855, 1.045)),      # KU115 -1: 0.95 V +-10 %
    ("vccaux", REG_VCCAUX, "V", 1.80, (1.62, 1.98)),
    ("vccbram", REG_VCCBRAM, "V", 0.95, (0.855, 1.045)),
)

DEFAULT_HW_SERVER = "tcp:127.0.0.1:3121"
DEFAULT_DEVICE = "xcku115*"
NEEDS_CABLE = "is the JTAG cable on J17 connected and the board powered?"


# --- the register interface ------------------------------------------------------------


def read_command(addr: int) -> int:
    """The 32-bit DR word that reads DRP register ``addr``. Only reads are ever built."""
    if not isinstance(addr, int) or not 0 <= addr <= 0x3FF:
        raise ValueError(f"SYSMON DRP address {addr!r} is out of range 0..0x3ff")
    return (CMD_READ << 26) | (addr << 16)


def temp_c(code: int, *, external_ref: bool = False) -> float:
    if external_ref:
        return code * 502.9098 / 65536 - 273.8195
    return code * 501.3743 / 65536 - 273.6777


def supply_v(code: int) -> float:
    return code * 3.0 / 65536


@dataclass(frozen=True)
class SysmonSample:
    """Raw 16-bit codes by DRP address, as read at ``observed_at``."""

    codes: Mapping[int, int]
    errors: Mapping[int, str] = field(default_factory=dict)   # address -> why it was not read
    source: str = "sysmon-jtag"
    observed_at: float = field(default_factory=time.time)


def _ref_note(sample: SysmonSample) -> tuple[bool, str]:
    """(external reference in use?, caveat for every value)."""
    flag = sample.codes.get(REG_FLAG)
    if flag is None:
        why = sample.errors.get(REG_FLAG, "not read")
        return False, f"reference unknown: flag register 3Fh {why}; values may read ~4 % high"
    if flag & FLAG_REF:
        return False, ""
    note = ("SYSMON is on its external reference (flag 3Fh REF=0) and the MPS3 ties VREFP to "
            "1.2 V, so values may read ~4 % high (unverified: board check W13)")
    aux = sample.codes.get(REG_VCCAUX)
    if aux not in (None, 0x0000, 0xFFFF):
        v = supply_v(aux)
        note += f"; VCCAUX reads {v:.3f} V against 1.800 V nominal ({(v / 1.8 - 1) * 100:+.1f} %)"
    return True, note


def sysmon_readings(sample: SysmonSample) -> list[Reading]:
    """Readings for the four channels and their max/min, with every caveat attached."""
    flag = sample.codes.get(REG_FLAG)
    if flag is not None and flag & FLAG_JTGD and flag != 0xFFFF:
        why = "the bitstream disabled SYSMON JTAG access (BITSTREAM.GENERAL.JTAG_SYSMON, flag JTGD)"
        return [Reading.unavailable(n, u, why, source=sample.source)
                for n, _r, u, _nom, _p in CHANNELS]
    external, ref_note = _ref_note(sample)
    out: list[Reading] = []
    for name, reg, unit, _nominal, (lo, hi) in CHANNELS:
        for suffix, addr, extra in (("", reg, ""),
                                    ("_max", REG_MAX[reg], "maximum since power-up or SYSMON reset"),
                                    ("_min", REG_MIN[reg], "minimum since power-up or SYSMON reset")):
            rname = name + suffix
            code = sample.codes.get(addr)
            if code is None:
                why = sample.errors.get(addr, "not read")
                out.append(Reading.unavailable(rname, unit, f"SYSMON register {addr:02X}h {why}",
                                               source=sample.source))
                continue
            if code in (0x0000, 0xFFFF):
                out.append(Reading.unavailable(
                    rname, unit, f"SYSMON register {addr:02X}h holds 0x{code:04X}: no conversion "
                                 "recorded, or the DR shift did not reach SYSMON", source=sample.source))
                continue
            value = round(temp_c(code, external_ref=external), 2) if unit == "degC" \
                else round(supply_v(code), 4)
            notes = [n for n in (extra, ref_note) if n]
            if not lo <= value <= hi:
                notes.append(f"outside the plausible {lo:g}..{hi:g} {unit}: check the JTAG chain "
                             "(a one-bit DR misalignment halves or doubles a value)")
            if name == "fpga_die_temp" and not suffix and flag is not None and flag & FLAG_OT:
                notes.append("SYSMON over-temperature alarm (flag OT) is set")
            out.append(Reading(rname, value, unit, sample.source,
                               observed_at=sample.observed_at, reason="; ".join(notes)))
    return out


# --- tool output --------------------------------------------------------------------------

_VALUE_RE = re.compile(r"SOCHARNESS_SYSMON ([0-9A-Fa-f]{2}) ([0-9A-Fa-f]{1,8})(?![0-9A-Za-z\[$])")
_ERROR_RE = re.compile(r"SOCHARNESS_SYSMON_ERR (connect|target|read) (.*)$")


@dataclass(frozen=True)
class ToolOutput:
    codes: dict[int, int]
    errors: dict[int, str]
    fatal: tuple[str, str] | None      # (stage, message) for connect/target failures


def parse_output(text: str) -> ToolOutput:
    """Pick the marker lines out of an xsdb/openocd transcript."""
    codes: dict[int, int] = {}
    errors: dict[int, str] = {}
    fatal: tuple[str, str] | None = None
    for line in text.splitlines():
        m = _VALUE_RE.search(line)
        if m:
            codes[int(m.group(1), 16)] = int(m.group(2), 16) & 0xFFFF
            continue
        e = _ERROR_RE.search(line)
        if not e:
            continue
        stage, msg = e.group(1), e.group(2).strip()
        if stage == "read":
            addr, _, why = msg.partition(" ")
            if re.fullmatch(r"[0-9A-Fa-f]{2}", addr):
                errors[int(addr, 16)] = f"read failed: {why.strip() or 'no detail'}"
        elif fatal is None:
            fatal = (stage, msg or "no detail")
    return ToolOutput(codes, errors, fatal)


def _tail(text: str, n: int = 3) -> str:
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    return " | ".join(lines[-n:]) if lines else "no output"


Runner = Callable[..., subprocess.CompletedProcess]


def _resolve_tool(tool: str, what: str, key: str) -> str:
    found = shutil.which(tool)
    if found:
        return found
    if Path(tool).is_file():
        return tool
    raise UnavailableError("sysmon", f"{what} not found ({tool!r}): install it, or set "
                                     f"sysmon.{key} in boards.toml to its full path")


def _run(runner: Runner, argv: list[str], timeout_s: float, what: str, key: str) -> str:
    try:
        proc = runner(argv, capture_output=True, text=True, timeout=timeout_s)
    except FileNotFoundError as exc:
        raise UnavailableError("sysmon", f"{what} not found ({argv[0]!r}): install it, or set "
                                         f"sysmon.{key} in boards.toml to its full path") from exc
    except subprocess.TimeoutExpired as exc:
        raise UnavailableError("sysmon", f"{what} did not finish within {timeout_s:g} s") from exc
    return f"{proc.stdout or ''}\n{proc.stderr or ''}"


# --- backend (a): xsdb + hw_server -------------------------------------------------------

_URL_RE = re.compile(r"^tcp:[A-Za-z0-9.\-]+:\d{1,5}$|^tcp:\[[0-9A-Fa-f:]+\]:\d{1,5}$")
_FILTER_RE = re.compile(r"^[A-Za-z0-9_.*?\-]{1,40}$")


class XsdbSysmon:
    """SYSMON through xsdb and a hw_server (the hub already runs one on the J17 cable)."""

    backend = "xsdb"

    def __init__(self, *, xsdb: str = "xsdb", hw_server: str = DEFAULT_HW_SERVER,
                 device: str = DEFAULT_DEVICE, timeout_s: float = 60.0,
                 runner: Runner = subprocess.run, clock: Callable[[], float] = time.time) -> None:
        # Both values are pasted into Tcl: validate them so a config file cannot inject code.
        if not _URL_RE.match(hw_server):
            raise UsageError(f"sysmon.hw_server {hw_server!r} is not tcp:<host>:<port>")
        if not _FILTER_RE.match(device):
            raise UsageError(f"sysmon.device {device!r} is not a plain JTAG target name pattern")
        self.xsdb = xsdb
        self.hw_server = hw_server
        self.device = device
        self.timeout_s = timeout_s
        self._runner = runner
        self._clock = clock

    @property
    def source(self) -> str:
        return f"sysmon-jtag (xsdb {self.hw_server})"

    def script(self, regs: Sequence[int] = READ_REGS) -> str:
        pairs = " ".join(f"{a} {read_command(a)}" for a in regs)
        return f"""\
# socharness T9: KU115 SYSMON over the JTAG DRP. READ-ONLY: every DR command is a read
# (CMD 0001) or a no-op (CMD 0000); a read's data returns on the next DR shift (UG580).
proc socharness_fail {{stage msg}} {{
    puts "SOCHARNESS_SYSMON_ERR $stage [string map {{"\\n" " " "\\r" " "}} $msg]"
    exit 2
}}
if {{[catch {{connect -url {{{self.hw_server}}}}} err]}} {{ socharness_fail connect $err }}
if {{[catch {{jtag targets -set -filter {{name =~ "{self.device}"}}}} err]}} {{ socharness_fail target $err }}
proc socharness_rd {{cmd}} {{
    set s [jtag sequence]
    $s irshift -state IDLE -integer {IR_LENGTH} {IR_SYSMON_DRP}
    $s drshift -state IDLE -integer {DR_BITS} $cmd
    $s delay 1000
    $s drshift -state IDLE -capture -integer {DR_BITS} {CMD_NOP}
    set r [$s run -integer]
    $s delete
    return [lindex $r 0]
}}
foreach {{addr cmd}} {{{pairs}}} {{
    if {{[catch {{socharness_rd $cmd}} v]}} {{
        puts "SOCHARNESS_SYSMON_ERR read [format %02x $addr] [string map {{"\\n" " "}} $v]"
    }} else {{
        puts "SOCHARNESS_SYSMON [format %02x $addr] [format %08x $v]"
    }}
}}
catch {{disconnect}}
exit 0
"""

    def read(self, regs: Sequence[int] = READ_REGS) -> SysmonSample:
        exe = _resolve_tool(self.xsdb, "xsdb", "xsdb")
        with tempfile.TemporaryDirectory(prefix="socharness-sysmon-") as tmp:
            path = Path(tmp) / "sysmon.tcl"
            path.write_text(self.script(regs), encoding="utf-8")
            text = _run(self._runner, [exe, str(path)], self.timeout_s, "xsdb", "xsdb")
        out = parse_output(text)
        if out.fatal is not None:
            stage, msg = out.fatal
            if stage == "connect":
                raise UnavailableError("sysmon", f"cannot reach hw_server at {self.hw_server}: {msg} "
                                                 "(is hw_server running?)")
            raise UnavailableError("sysmon", f"no {self.device} on the JTAG chain behind "
                                             f"{self.hw_server}: {msg} ({NEEDS_CABLE})")
        if not out.codes and not out.errors:
            raise UnavailableError("sysmon", f"xsdb printed no SYSMON values: {_tail(text)}")
        return SysmonSample(out.codes, out.errors, self.source, self._clock())


# --- backend (b): OpenOCD irscan/drscan ------------------------------------------------------

_OPENOCD_FATAL = (
    (re.compile(r"UNEXPECTED: (0x[0-9a-fA-F]+)"),
     "the first JTAG device is not a KU115 (IDCODE {0})"),
    (re.compile(r"scan chain interrogation failed: all (ones|zeroes)", re.I),
     "no JTAG device answered (scan chain all {0}): " + NEEDS_CABLE),
    (re.compile(r"unable to open ftdi device", re.I),
     "no FTDI JTAG adapter found: check its USB cable and permissions"),
)


class OpenOcdSysmon:
    """SYSMON through OpenOCD on a JTAG adapter (e.g. a Digilent JTAG-HS3 on J17).

    ``adapter`` is the probe half as OpenOCD ``-c`` commands, e.g.
    ``["source [find interface/ftdi/digilent-hs3.cfg]", "adapter serial 210299ABCDEF"]``.
    """

    backend = "openocd"

    def __init__(self, *, openocd: str = "openocd", adapter: Sequence[str] = (),
                 speed_khz: int | None = 1000, search: Sequence[str] = (), timeout_s: float = 30.0,
                 runner: Runner = subprocess.run, clock: Callable[[], float] = time.time) -> None:
        if not adapter:
            raise UsageError("sysmon.adapter is required for the openocd backend",
                             hint='e.g. adapter = ["source [find interface/ftdi/digilent-hs3.cfg]"]')
        self.openocd = openocd
        self.adapter = tuple(adapter)
        self.speed_khz = speed_khz
        self.search = tuple(search)
        self.timeout_s = timeout_s
        self._runner = runner
        self._clock = clock

    @property
    def source(self) -> str:
        return "sysmon-jtag (openocd)"

    def commands(self, regs: Sequence[int] = READ_REGS) -> list[str]:
        reads = "; ".join(
            f"if {{[catch {{socharness_rd {read_command(a)}}} v]}} "
            f"{{echo \"SOCHARNESS_SYSMON_ERR read {a:02x} $v\"}} "
            f"else {{echo \"SOCHARNESS_SYSMON {a:02x} [format %08x 0x$v]\"}}"
            for a in regs)
        cmds = list(self.adapter) + ["transport select jtag"]
        if self.speed_khz:
            cmds.append(f"adapter speed {int(self.speed_khz)}")
        cmds += [
            f"jtag newtap ku115 tap -irlen {IR_LENGTH} -expected-id 0x{KU115_IDCODE:08x} -ignore-version",
            "init",
            # READ-ONLY: a read command, then a no-op shift that returns the read's data.
            "proc socharness_rd {cmd} { "
            f"irscan ku115.tap 0x{IR_SYSMON_DRP:03x}; drscan ku115.tap {DR_BITS} $cmd; sleep 1; "
            f"return [drscan ku115.tap {DR_BITS} {CMD_NOP}] }}",
            # Never send SYSMON_DRP to a device that is not a KU115: check the IDCODE first.
            "proc socharness_main {} { "
            f"irscan ku115.tap 0x{IR_IDCODE:03x}; set id [drscan ku115.tap 32 0]; set idv [expr 0x$id]; "
            f"if {{($idv & 0x0fffffff) != 0x{KU115_IDCODE:08x}}} "
            "{ echo \"SOCHARNESS_SYSMON_ERR target IDCODE 0x$id is not a KU115\"; return }; "
            f"{reads} }}",
            "socharness_main",
            "shutdown",
        ]
        return cmds

    def argv(self, exe: str, regs: Sequence[int] = READ_REGS) -> list[str]:
        argv = [exe]
        for s in self.search:
            argv += ["-s", s]
        for c in self.commands(regs):
            argv += ["-c", c]
        return argv

    def read(self, regs: Sequence[int] = READ_REGS) -> SysmonSample:
        exe = _resolve_tool(self.openocd, "openocd", "openocd")
        text = _run(self._runner, self.argv(exe, regs), self.timeout_s, "openocd", "openocd")
        for pattern, template in _OPENOCD_FATAL:
            m = pattern.search(text)
            if m:
                raise UnavailableError("sysmon", template.format(*m.groups()))
        out = parse_output(text)
        if out.fatal is not None:
            raise UnavailableError("sysmon", f"openocd: {out.fatal[1]}")
        if not out.codes and not out.errors:
            err = next((ln.strip() for ln in text.splitlines() if ln.strip().startswith("Error")), "")
            raise UnavailableError("sysmon", f"openocd read no SYSMON values: {err or _tail(text)}")
        return SysmonSample(out.codes, out.errors, self.source, self._clock())


# --- configuration (boards.toml [boards.<id>.sysmon]) -------------------------------------------


def make_sysmon_reader(table: Mapping[str, Any], **kwargs: Any) -> XsdbSysmon | OpenOcdSysmon:
    """Build a reader from a ``sysmon`` table. ``UsageError`` with the exact problem."""
    if not isinstance(table, Mapping):
        raise UsageError("sysmon must be a table")
    backend = table.get("backend", "xsdb")
    common = {"timeout_s": float(table["timeout_s"])} if "timeout_s" in table else {}
    if backend == "xsdb":
        unknown = set(table) - {"backend", "xsdb", "hw_server", "device", "timeout_s", "min_interval_s"}
        if unknown:
            raise UsageError(f"sysmon has unknown keys for backend xsdb: {', '.join(sorted(unknown))}")
        return XsdbSysmon(xsdb=str(table.get("xsdb", "xsdb")),
                          hw_server=str(table.get("hw_server", DEFAULT_HW_SERVER)),
                          device=str(table.get("device", DEFAULT_DEVICE)), **common, **kwargs)
    if backend == "openocd":
        unknown = set(table) - {"backend", "openocd", "adapter", "speed_khz", "search",
                                "timeout_s", "min_interval_s"}
        if unknown:
            raise UsageError(f"sysmon has unknown keys for backend openocd: {', '.join(sorted(unknown))}")
        adapter = table.get("adapter", [])
        if isinstance(adapter, str):
            adapter = [adapter]
        if not isinstance(adapter, list) or not all(isinstance(a, str) for a in adapter):
            raise UsageError("sysmon.adapter must be a list of OpenOCD commands")
        return OpenOcdSysmon(openocd=str(table.get("openocd", "openocd")), adapter=adapter,
                             speed_khz=table.get("speed_khz", 1000),
                             search=tuple(table.get("search", ())), **common, **kwargs)
    raise UsageError(f"sysmon.backend must be 'xsdb' or 'openocd' (got {backend!r})")


def sysmon_link(reader: XsdbSysmon | OpenOcdSysmon) -> Link:
    """The ``JTAG`` link a configured SYSMON reader gives a candidate."""
    if isinstance(reader, XsdbSysmon):
        return Link(LinkKind.JTAG, f"xsdb {reader.hw_server}", "SYSMON over hw_server (boards.toml)")
    return Link(LinkKind.JTAG, "openocd", "SYSMON over an OpenOCD JTAG adapter (boards.toml)")
