"""The MPS3 board controller (MCC) over its USB serial console. Team T3.

``make_controller_adapter(session)`` is the hook ``pack.py`` calls. It returns
an ``Mps3Controller`` (a ``ControllerAdapter``) when the session has a
``USB_SERIAL`` link, else ``None``.

Hardware facts this driver is built on, with their sources:

1. **The MCC is FT4232H interface 00.** The MCC boot log says
   ``UART0: MCC, UART1: FPGA0`` (docs/evidence/2026-09-w2/pB_mcc_log_20260923.txt,
   mps3-nanosoc-platform), and fpgahub probed tty_00 answering ``Cmd>``
   (fpgahub mcc.py, 2026-07-14). Interface 01 is FPGA UART lane 0 and is
   silent to commands: every recorded "no-op REBOOT" (2026-07-28 x3, 07-30,
   08-04) was sent there. So before sending anything that matters, the
   driver checks for a ``Cmd>``/``Debug>`` prompt, and refuses to go on
   without one (``NothingOnTargetError``).
2. **The MCC drops burst input.** Characters that arrive less than ~50 ms
   after the previous one are lost, so a one-shot ``write(b"REBOOT\\r")``
   delivers only the ``R`` (fpgahub 30ae4f3; ``constants.MCC_CHAR_PACE_S``).
   Every write here is one character, at least ``MccTiming.pace_s`` (60 ms,
   a 20% margin) after the previous character, measured on the injected clock.
3. **``CFG R`` works only in the DEBUG menu** (TRM 100765 §3.6.3). The driver
   tracks the menu from the prompt (``Cmd>`` main, ``Debug>`` debug), enters
   DEBUG for CFG commands and always leaves with EXIT, so the next REBOOT is
   typed at the main menu (fpgahub mcc.py ``debug()``).
4. **Replies** (live board, 2026-07-14 and 2026-09-23):
   ``CFG R TEMP 0`` -> ``MB Device 0 Temp: 35.5 degC`` (the sensor is probably
   the FPGA die via IOFPGA_TMP; unverified);
   ``CFG R OSC n`` -> ``MB OSC<n> clock read = 25.000 MHz`` (a set-point, not
   a measurement);
   ``CFG R V n`` -> ``ERROR: Unable to perform requested function`` on every
   device, which becomes an unavailable reading, never a crash or a 0.
5. **REBOOT power-cycles the board and reloads the FPGA from the SD** (paced,
   on tty_00: proven 2026-08-04). The fpgahub ``mps3_msd.py`` docstring still
   calls the console REBOOT a no-op; that was the wrong tty. Because a no-op
   REBOOT looks exactly like success from the sending side, ``reboot()``
   proves it with a witness (below) and fails with
   ``ActionFailedError("REBOOT sent but no restart observed")``.
6. **Never type during a boot.** The MCC banner prints ``Press Enter to stop
   auto boot...`` and waits ``AUTORUNDELAY`` s (3 s in our config.txt); any key
   then STOPS the boot and the FPGA is never configured. After REBOOT the
   driver only listens until the banner is complete, and every operation
   first listens briefly and waits out a boot already in progress.

Hard safety rails (code, not docs): ``FORMAT``, ``DEL``, ``EEPROM``,
``USB_OFF``, ``SHUTDOWN``, ``REN``, ``COPY``, ``CAP`` and ``FILL`` are refused
with ``RefusedError`` (exit 15) and never reach the port. Only ``HELP``/``?``,
``DEBUG``, ``EXIT``, ``CFG R …`` and ``REBOOT`` are allowed; ``CFG W OSC`` needs
an explicit ``arm=True``. A line with control characters (a way to smuggle a
second command) is refused.

The reboot witness:

- **went down**: with an Ethernet shell that answered before the REBOOT, ping
  must fail ``MccTiming.down_pings`` (2) times in a row. Without one, the MCC must print its boot banner, drop off USB,
  or go quiet (no prompt after the REBOOT echo);
- **came back**: with an Ethernet shell, ping answers again (its shell_id is
  recorded). Without one, the banner reaches ``Enabling debug USB.`` and
  reports ``FPGA configuration complete.``;
- progress phases ``sent``, ``down``, ``up`` (``Progress(phase, done, 3)``),
  which the engine publishes as ``controller.reboot {phase}``.
"""

from __future__ import annotations

import logging
import re
import time
from collections.abc import Callable, Iterable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from typing import Any

import harness_manager.transports.direct as _direct  # noqa: F401 - registers the serial:// scheme
from harness_manager.core.errors import (
    ActionFailedError,
    HarnessError,
    NothingOnTargetError,
    RefusedError,
    UnreachableError,
    UsageError,
)
from harness_manager.core.model import LinkKind, Reading
from harness_manager.core.pack import Progress
from harness_manager.core.transport import SerialPort, open_serial
from harness_manager.services import reset_guard

from .constants import MCC_CHAR_PACE_S

log = logging.getLogger(__name__)

MCC_BAUD = 115200            # 8N1 (fpgahub tty_share.SerialConfig)
PROMPT_RE = re.compile(r"(Cmd|Debug)>\s*$")
TEMP_CAVEAT = "sensor identity unverified (probably FPGA die via IOFPGA_TMP)"
OSC_CAVEAT = "the MCC reports the programmed set-point, not a measured frequency"
OSC_COUNT = 6                # OSC0..OSC5 answer CFG R OSC (boot log OSCCLK0..5)

# Hard-denied MCC commands and why. Never written to the port, in any case or menu.
DENIED: dict[str, str] = {
    "FORMAT": "it formats the configuration SD",
    "DEL": "it deletes files on the configuration SD",
    "EEPROM": "it rewrites the board EEPROM",
    "USB_OFF": "it turns off the USB link this manager depends on",
    "SHUTDOWN": "it powers the board to standby; only a physical PBON press brings it back",
    "REN": "it renames files on the configuration SD",
    "COPY": "it copies files on the configuration SD (a copied .ebf silently reflashes the MCC)",
    "CAP": "it changes board state and the manager has no use for it",
    "FILL": "it changes board state and the manager has no use for it",
}

CFG_READ_KINDS = ("OSC", "TEMP", "V", "SCC")
_TOKEN_RE = re.compile(r"^[A-Z0-9_.+-]{1,32}$")
_DEVICE_RE = re.compile(r"^\d{1,2}$")
_VALUE_RE = re.compile(r"^\d{1,4}(\.\d{1,6})?$")
_MAX_LINE = 64


# --- timing ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MccTiming:
    pace_s: float = MCC_CHAR_PACE_S * 1.2   # >= 50 ms per char, 20% margin (fpgahub 30ae4f3)
    reply_timeout_s: float = 5.0            # prompt must return within this (fpgahub DEFAULT_TIMEOUT_S)
    poll_s: float = 0.02
    listen_s: float = 0.2                   # passive listen before the first write of an operation
    boot_guard_s: float = 120.0             # wait out a boot already in progress
    ping_interval_s: float = 1.0            # reboot witness: shell ping cadence
    ping_timeout_s: float = 1.0
    down_pings: int = 2                     # consecutive failed pings that count as "down"
    reopen_interval_s: float = 0.5          # reboot witness: retry a port that dropped off USB
    quiet_s: float = 2.0                    # reboot witness: no prompt after REBOOT for this long
    banner_tail_s: float = 30.0             # reboot witness: read the banner to its prompt after the shell is up


DEFAULT_TIMING = MccTiming()
#: The MCC reached over an fpgahub TTY share (``tcp://``/``hub://``): 100 ms a character,
#: the rate the remote REBOOT was proven at across the share's jitter (ILA mint findings
#: 2026-09-24 #2). Over the Debug USB the 60 ms default stands (fpgahub 30ae4f3).
SHARE_PACE_S = 0.1


def timing_for(url: str, base: MccTiming | None = None) -> MccTiming:
    """The pacing for an MCC console URL: slower across a hub share. The settings
    ``mps3.mcc.pace_ms`` and ``mps3.mcc.share_pace_ms`` move the two paces when someone set
    them (lane SET-WIRE; read as the controller is made, at a board open)."""
    from .settings import configured_s

    if base is None:
        pace = configured_s("mps3.mcc.pace_ms", DEFAULT_TIMING.pace_s)
        base = DEFAULT_TIMING if pace == DEFAULT_TIMING.pace_s else \
            replace(DEFAULT_TIMING, pace_s=pace)
    if url.startswith(("tcp://", "hub://")):
        share = configured_s("mps3.mcc.share_pace_ms", SHARE_PACE_S)
        return replace(base, pace_s=max(base.pace_s, share))
    return base
# Module-level so tests can swap in a fake clock; read at adapter creation time.
DEFAULT_CLOCK: Callable[[], float] = time.monotonic
DEFAULT_SLEEP: Callable[[float], None] = time.sleep

#: The post-SD-write quirk (silicon, reproduced twice, 2026-09-26): straight after an SD
#: write the MCC answers a bare CR with only ``\r\n`` for a few seconds, so a REBOOT that
#: needs its ``Cmd>`` is refused. The pre-REBOOT check is tried again, this many times this
#: far apart, ONLY while the reply is bare CR/LF (``bare_crlf``); any other answer, and
#: silence, is final. The SD write itself is never retried. Local and hub (``hub_mcc``).
POST_WRITE_TRIES = 3
POST_WRITE_GAP_S = 5.0
_BARE_CRLF = re.compile(r"[\r\n]+")


def bare_crlf(text: str | None) -> bool:
    """True when the MCC's whole answer was CR/LF: the post-SD-write quirk, not a refusal."""
    return bool(text) and _BARE_CRLF.fullmatch(text) is not None


def guard_reset(session: Any, action: str) -> None:
    """SLOT-TIMING's reset guard (``harness_manager.services.reset_guard``): never reset
    while the board's card job writes or reads back (B2 2026-09-26: a reboot mid-job left
    the card with "uSD init error"). ``reset_guard.check(session, reset_guard.<action>)``: it
    raises its ``CardBusyError`` (exit 4) while the card job runs. Inside a caller's
    ``guarded`` block for this board on this thread (the CLI's and the daemon's MCC REBOOT,
    with or without ``--force``) it passes without asking the board again, so the two layers
    never both refuse and ``--force`` reaches through. ``action``: ``ACTION_MCC_REBOOT`` /
    ``ACTION_HARNESS_REBOOT``. A controller with no session (built by hand) has no board
    to ask."""
    if session is None:
        return
    reset_guard.check(session, getattr(reset_guard, action))


# --- command classification (the allowlist) -------------------------------------------


@dataclass(frozen=True)
class MccCommand:
    text: str          # normalised: upper case, single spaces
    head: str
    menu: str          # the menu it must be typed in: "main" | "debug"


def _refuse(head: str) -> RefusedError:
    return RefusedError(f"MCC command {head} is hard-denied: {DENIED[head]}",
                        hint="do it by hand at the board if you really mean it")


def classify(line: str, *, arm: bool = False) -> MccCommand:
    """Check ``line`` against the allowlist. Raises ``RefusedError`` (exit 15)."""
    if not isinstance(line, str):
        raise UsageError("an MCC command must be a string")
    upper = line.upper()
    # Name a denied command wherever it appears, even smuggled after a CR or ';'.
    for word in re.split(r"[^A-Z0-9_?]+", upper):
        if word in DENIED:
            raise _refuse(word)
    if any(not (0x20 <= ord(c) < 0x7F) for c in line):
        raise RefusedError(f"MCC command {line!r} contains control or non-ASCII characters",
                           hint="one command per call, printable ASCII only")
    words = upper.split()
    if not words:
        raise UsageError("empty MCC command")
    if len(line.strip()) > _MAX_LINE:
        raise RefusedError(f"MCC command is longer than {_MAX_LINE} characters")
    head = words[0]
    if head in ("HELP", "?") and len(words) == 1:
        return MccCommand(head, head, "main")
    if not all(_TOKEN_RE.match(w) for w in words):
        raise RefusedError(f"MCC command {line!r} has a token that is not plain [A-Z0-9_.+-]")
    if head == "DEBUG" and len(words) == 1:
        return MccCommand("DEBUG", head, "main")
    if head == "EXIT" and len(words) == 1:
        return MccCommand("EXIT", head, "debug")
    if head == "REBOOT" and len(words) == 1:
        return MccCommand("REBOOT", head, "main")
    if head == "CFG" and len(words) >= 4:
        rw, kind, dev = words[1], words[2], words[3]
        if not _DEVICE_RE.match(dev):
            raise RefusedError(f"CFG device {dev!r} is not a device number")
        if rw == "R" and len(words) == 4 and kind in CFG_READ_KINDS:
            return MccCommand(" ".join(words), head, "debug")
        if rw == "W" and kind == "OSC" and len(words) == 5:
            if not _VALUE_RE.match(words[4]):
                raise RefusedError(f"CFG W OSC value {words[4]!r} is not a frequency in MHz")
            if not arm:
                raise RefusedError(
                    f"{' '.join(words)} changes a board oscillator",
                    hint="pass arm=True to confirm; the change lasts until the next REBOOT",
                )
            return MccCommand(" ".join(words), head, "debug")
        if rw == "W":
            raise RefusedError(f"CFG W {kind} is not allowed; only CFG W OSC (armed) is")
    raise RefusedError(f"MCC command {line.strip()!r} is not on the allowlist",
                       hint="allowed: HELP, ?, DEBUG, EXIT, CFG R <OSC|TEMP|V|SCC> <n>, "
                            "REBOOT, CFG W OSC <n> <MHz> (armed)")


# --- reply parsers --------------------------------------------------------------------

_TEMP_RE = re.compile(r"MB Device\s+(\d+)\s+Temp:\s*(-?\d+(?:\.\d+)?)\s*degC", re.I)
_OSC_RE = re.compile(r"MB OSC(\d+) clock read\s*=\s*(\d+(?:\.\d+)?)\s*MHz", re.I)
_ERR_RE = re.compile(r"^\s*(ERROR:.*|Command error.*)$", re.M | re.I)


def parse_temp(text: str) -> float | None:
    m = _TEMP_RE.search(text)
    return float(m.group(2)) if m else None


def parse_osc(text: str) -> tuple[int, float] | None:
    m = _OSC_RE.search(text)
    return (int(m.group(1)), float(m.group(2))) if m else None


def reply_error(text: str) -> str | None:
    """The MCC's error line in ``text``, or None if the reply is not an error."""
    m = _ERR_RE.search(text)
    return m.group(1).strip() if m else None


def strip_echo(raw: str, command: str) -> str:
    """Reply text without the MCC's echo of ``command`` and the trailing prompt."""
    text = raw.replace("\r\n", "\n").replace("\r", "\n")
    lines = text.split("\n")
    while lines and not lines[0].strip():
        lines.pop(0)
    if command and lines and lines[0].strip().upper() == command.strip().upper():
        lines.pop(0)
    while lines and (not lines[-1].strip() or PROMPT_RE.search(lines[-1])):
        lines.pop()
    return "\n".join(line.rstrip() for line in lines).strip()


# --- boot-log parser ------------------------------------------------------------------


@dataclass
class BootRecord:
    """One MCC power-on, as the MCC console printed it."""

    bootloader: str = ""            # "v1.0.0"
    hbi_build: str = ""             # "HBI0309 build 567"
    firmware: str = ""              # MCC firmware, "v1.3.2"
    build_date: str = ""            # "Apr 20 2018"
    usb_serial: str = ""
    board: str = ""                 # "rev C, var A"
    board_file: str = ""            # "\\MB\\HBI0309C\\Nanosoc\\nanosoc.txt"
    fpga_file: str = ""             # "\\MB\\HBI0309C\\Nanosoc\\nanosoc.bit"
    fpga_config_records: int = 0    # "Address:" progress records (128 KiB each)
    fpga_last_address: int | None = None
    fpga_configured: bool = False   # "FPGA configuration complete."
    osc_mhz: dict[int, float] = field(default_factory=dict)
    osc_setup: str = ""             # "PASSED" | "FAILED"
    gtxclk: str = ""
    uart_map: dict[str, str] = field(default_factory=dict)   # {"UART0": "MCC", "UART1": "FPGA0"}
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    preceded_by: list[str] = field(default_factory=list)     # e.g. "External reset request..."
    trailing: list[str] = field(default_factory=list)
    complete: bool = False          # reached "Enabling debug USB." or the prompt
    at_prompt: bool = False         # the MCC printed its prompt after the banner
    lines: list[str] = field(default_factory=list)


_BOOT_START_RE = re.compile(r"^ARM V2M-MPS3 Boot loader\s+(\S+)")
# Lines that prove a boot is in progress even when its first lines were missed.
_BANNER_MARKERS = (
    "HBI0309 build", "ARM V2M-MPS3 Firmware", "Press Enter to stop auto boot",
    "Enabling usb remote", "Powering up system", "Switching on main power",
    "Configuring motherboard", "Reading Board File", "Configuring FPGA from file",
)
_ADDR_RE = re.compile(r"Address:\s*0x([0-9A-Fa-f]+)")
_LINE_SPLIT_RE = re.compile(r"\r\n|\r|\n")


def _parse_uart_map(line: str) -> dict[str, str] | None:
    parts = [p.strip() for p in line.split(",")]
    out: dict[str, str] = {}
    for part in parts:
        m = re.match(r"^(UART\d+):\s*(\S+)$", part)
        if not m:
            return None
        out[m.group(1)] = m.group(2)
    return out or None


def parse_boot_log(text: str) -> list[BootRecord]:
    """Split an MCC console capture into power-on records.

    Accepts the console's mixed CR / LF / CRLF line ends (the ``Address:``
    progress records are CR-separated on the real console). Lines before the
    first banner, and ``#`` comment lines, are ignored.
    """
    records: list[BootRecord] = []
    cur: BootRecord | None = None
    pending: list[str] = []
    for raw in _LINE_SPLIT_RE.split(text):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        m = _BOOT_START_RE.match(line)
        missed_header = (cur is None or cur.complete) and any(line.startswith(k) for k in _BANNER_MARKERS)
        if m or missed_header:
            cur = BootRecord(bootloader=m.group(1) if m else "", preceded_by=pending)
            pending = []
            records.append(cur)
            if m:
                cur.lines.append(line)
                continue
        if cur is None:
            continue
        if cur.complete:
            if PROMPT_RE.search(line):
                cur.at_prompt = True
                continue
            if not pending and line.startswith("USB Serial Number"):
                cur.lines.append(line)          # the last line of every banner
                cur.usb_serial = cur.usb_serial or line.partition("=")[2].strip()
                continue
            pending.append(line)
            continue
        cur.lines.append(line)
        if PROMPT_RE.search(line):
            cur.complete = cur.at_prompt = True
            continue
        if (mm := re.match(r"^(HBI\d+\w*) build (\d+)", line)):
            cur.hbi_build = f"{mm.group(1)} build {mm.group(2)}"
        elif (mm := re.match(r"^ARM V2M-MPS3 Firmware\s+(\S+)", line)):
            cur.firmware = mm.group(1)
        elif (mm := re.match(r"^Build Date:\s*(.+)$", line)):
            cur.build_date = mm.group(1).strip()
        elif (mm := re.match(r"^USB Serial Number\s*=\s*(\S+)", line)):
            cur.usb_serial = mm.group(1)
        elif (mm := re.match(r"^Configuring motherboard \((.+)\)", line)):
            cur.board = mm.group(1)
        elif (mm := re.match(r"^Reading Board File\s+(.+)$", line)):
            cur.board_file = mm.group(1).strip()
        elif (mm := re.match(r"^Configuring FPGA from file\s+(.+)$", line)):
            cur.fpga_file = mm.group(1).strip()
        elif line.startswith("Address:"):
            addrs = _ADDR_RE.findall(line)
            cur.fpga_config_records += len(addrs)
            if addrs:
                cur.fpga_last_address = int(addrs[-1], 16)
        elif line.startswith("FPGA configuration complete"):
            cur.fpga_configured = True
        elif (mm := re.match(r"^OSCCLK(\d+)\s*:\s*(\d+(?:\.\d+)?)\s*MHz", line)):
            cur.osc_mhz[int(mm.group(1))] = float(mm.group(2))
        elif (mm := re.match(r"^OSCCLK setup:\s*(\w+)", line)):
            cur.osc_setup = mm.group(1)
        elif (mm := re.match(r"^GTXCLK\s*\((\d+)\):\s*(\w+)", line)):
            cur.gtxclk = mm.group(2)
        elif line.startswith("UART") and (umap := _parse_uart_map(line)):
            cur.uart_map = umap
        elif (mm := re.match(r"^ERROR:\s*(.+)$", line, re.I)):
            cur.errors.append(mm.group(1).strip())
        elif (mm := re.match(r"^WARNING:\s*(.+)$", line, re.I)):
            cur.warnings.append(mm.group(1).strip())
        elif re.search(r"\bfailed\b", line, re.I):
            cur.errors.append(line)
        elif line.startswith("Enabling debug USB"):
            cur.complete = True
    if pending and records:
        records[-1].trailing.extend(pending)
    return records


class BootWatch:
    """Incremental view of console bytes as boot records (for the witness and the guard)."""

    def __init__(self) -> None:
        self._text = ""
        self._record: BootRecord | None = None
        self._stale = False

    def feed(self, data: bytes) -> None:
        if data:
            self._text += data.decode("ascii", "replace")
            self._stale = True

    @property
    def text(self) -> str:
        return self._text

    @property
    def record(self) -> BootRecord | None:
        if self._stale:
            recs = parse_boot_log(self._text)
            self._record = recs[-1] if recs else None
            self._stale = False
        return self._record

    @property
    def started(self) -> bool:
        return self.record is not None

    @property
    def in_progress(self) -> bool:
        rec = self.record
        return rec is not None and not rec.complete

    @property
    def settling(self) -> bool:
        """The banner is complete but the MCC has not printed its prompt yet."""
        rec = self.record
        return rec is not None and rec.complete and not rec.at_prompt


# --- the console session --------------------------------------------------------------

Opener = Callable[[str, int], SerialPort]


class _Console:
    """One paced, prompt-synchronised conversation on the MCC port."""

    def __init__(self, ctl: Mps3Controller, port: SerialPort) -> None:
        self._ctl = ctl
        self.port: SerialPort | None = port
        self.menu: str | None = None
        self.transcript = bytearray()
        self.heard = ""                    # what the last failed sync heard, raw

    # -- raw I/O --

    def write_paced(self, text: str) -> None:
        if self.port is None:
            raise UnreachableError(f"the MCC port {self._ctl.url} is not open")
        ctl = self._ctl
        for ch in text.encode("ascii"):
            last = ctl._last_tx
            if last is not None:
                wait = ctl.timing.pace_s - (ctl._clock() - last)
                if wait > 0:
                    ctl._sleep(wait)
            try:
                self.port.write(bytes([ch]))
            except OSError as exc:
                raise UnreachableError(f"write to the MCC port {ctl.url} failed: {exc}") from exc
            ctl._last_tx = ctl._clock()

    def read_available(self) -> bytes:
        """Whatever has arrived, without blocking. Raises ``OSError`` if the port died."""
        if self.port is None:
            return b""
        waiting = self.port.in_waiting
        if not waiting:
            return b""
        data = bytes(self.port.read(waiting))
        self.transcript += data
        return data

    def listen(self, seconds: float) -> bytes:
        ctl = self._ctl
        buf = bytearray()
        end = ctl._clock() + seconds
        while True:
            try:
                buf += self.read_available()
            except OSError as exc:
                raise UnreachableError(f"the MCC port {ctl.url} failed: {exc}") from exc
            if ctl._clock() >= end:
                return bytes(buf)
            ctl._sleep(ctl.timing.poll_s)

    def read_until_prompt(self, timeout: float) -> str:
        ctl = self._ctl
        buf = bytearray()
        deadline = ctl._clock() + timeout
        while True:
            try:
                chunk = self.read_available()
            except OSError as exc:
                raise UnreachableError(f"the MCC port {ctl.url} failed: {exc}") from exc
            if chunk:
                buf += chunk
                m = PROMPT_RE.search(buf.decode("ascii", "replace"))
                if m:
                    self.menu = "debug" if m.group(1) == "Debug" else "main"
                    return buf.decode("ascii", "replace")
                continue
            if ctl._clock() >= deadline:
                raise TimeoutError(buf.decode("ascii", "replace"))
            ctl._sleep(ctl.timing.poll_s)

    # -- conversation --

    def sync(self) -> None:
        """Bare CR -> prompt. Proves this port is the MCC and learns the menu."""
        self.write_paced("\r")
        self.heard = ""
        try:
            self.read_until_prompt(self._ctl.timing.reply_timeout_s)
        except TimeoutError as exc:
            self.heard = str(exc)                # raw: the post-write quirk is bare CR/LF
            heard = str(exc).strip()
            raise NothingOnTargetError(
                f"no MCC prompt (Cmd>/Debug>) on {self._ctl.url} within "
                f"{self._ctl.timing.reply_timeout_s:.0f}s"
                + (f"; heard {heard[:60]!r}" if heard else ", the port is silent"),
                hint="the MCC is FT4232H interface 00; interfaces 01-03 are FPGA UART lanes "
                     "(a REBOOT typed there is a silent no-op)",
            ) from None

    def run(self, text: str) -> str:
        """Type one command (paced) and return its reply without echo and prompt."""
        self.write_paced(text + "\r")
        try:
            raw = self.read_until_prompt(self._ctl.timing.reply_timeout_s)
        except TimeoutError as exc:
            raise UnreachableError(
                f"the MCC did not return to its prompt after {text!r} within "
                f"{self._ctl.timing.reply_timeout_s:.0f}s (heard {str(exc)[:80]!r})",
                hint="characters may have been dropped; the MCC needs >=50 ms between characters",
            ) from None
        return strip_echo(raw, text)

    def goto(self, menu: str) -> None:
        if self.menu == menu:
            return
        self.run("DEBUG" if menu == "debug" else "EXIT")
        if self.menu != menu:
            raise ActionFailedError(f"the MCC did not enter its {menu} menu (prompt says {self.menu})")


# --- the witness record ---------------------------------------------------------------


@dataclass(frozen=True)
class RebootWitness:
    sent_at: float
    down_after_s: float
    up_after_s: float
    down_evidence: tuple[str, ...]
    up_evidence: str
    shell_id_before: str | None = None
    shell_id_after: str | None = None
    boot: BootRecord | None = None

    def summary(self) -> str:
        return (f"REBOOT witnessed: down after {self.down_after_s:.1f}s "
                f"({'; '.join(self.down_evidence)}), up after {self.up_after_s:.1f}s "
                f"({self.up_evidence})")

    def as_dict(self) -> dict:
        """The evidence as plain JSON (the ``ControllerAdapter.reboot`` return, T7-6)."""
        return {
            "summary": self.summary(),
            "down_after_s": round(self.down_after_s, 3),
            "up_after_s": round(self.up_after_s, 3),
            "down_evidence": list(self.down_evidence),
            "up_evidence": self.up_evidence,
            "shell_id_before": self.shell_id_before,
            "shell_id_after": self.shell_id_after,
            "fpga_configured": self.boot.fpga_configured if self.boot is not None else None,
            # What the MCC said it loaded (MCC-FIX): "" when the banner had no such line.
            **boot_fields(self.boot),
        }


def sd_path(mcc_path: str) -> str:
    """An MCC console path as an SD-relative POSIX path:
    ``\\MB\\HBI0309C\\Nanosoc\\nanosoc.bit`` -> ``MB/HBI0309C/Nanosoc/nanosoc.bit``."""
    return mcc_path.strip().replace("\\", "/").strip("/")


def boot_fields(boot: BootRecord | None) -> dict[str, str]:
    """The banner's parsed facts a reboot result carries: which ``.bit`` and board file the
    MCC loaded (SD-relative, so ``updates.sd_ab``'s ``F0FILE`` flip is provable from HM),
    and the MCC's own firmware. Every value is ``""`` when the banner did not print it."""
    b = boot or BootRecord()
    return {"fpga_file": sd_path(b.fpga_file), "board_file": sd_path(b.board_file),
            "mcc_firmware": b.firmware, "mcc_build_date": b.build_date,
            "hbi_build": b.hbi_build, "bootloader": b.bootloader}


# A shell probe returns the shell_id when ping is answered, ``SHELL_BUSY`` when the
# control port is alive but did not answer (held by another client, reset, protocol
# error), and None when the shell is unreachable.
ShellProbe = Callable[[], "str | None"]
SHELL_BUSY = "(busy)"


# --- the adapter ----------------------------------------------------------------------


class Mps3Controller:
    """``ControllerAdapter`` for the MPS3 MCC. Opens the port per operation, never holds it."""

    def __init__(
        self,
        url: str,
        *,
        timing: MccTiming | None = None,
        clock: Callable[[], float] | None = None,
        sleep: Callable[[float], None] | None = None,
        shell_probe: ShellProbe | None = None,
        opener: Opener | None = None,
    ) -> None:
        self.url = url
        self.timing = timing or DEFAULT_TIMING
        self._clock = clock or time.monotonic
        self._sleep = sleep or time.sleep
        self._shell_probe = shell_probe
        self._opener: Opener = opener or open_serial
        self._last_tx: float | None = None
        self.last_reboot: RebootWitness | None = None
        self.last_transcript = b""
        self.session: Any = None             # for the reset guard (make_controller_adapter)

    # -- plumbing --

    @contextmanager
    def _session(self) -> Iterator[_Console]:
        port = self._opener(self.url, MCC_BAUD)
        con = _Console(self, port)
        try:
            self._guard_boot(con)
            yield con
        finally:
            self.last_transcript = bytes(con.transcript)
            if con.port is not None:
                try:
                    con.port.close()
                except OSError:
                    pass

    def _guard_boot(self, con: _Console) -> None:
        """Listen first; if the MCC is mid-boot, wait for the banner to finish.

        A keypress during "Press Enter to stop auto boot..." stops the boot and
        leaves the FPGA unconfigured, so nothing is typed while a banner runs.
        """
        watch = BootWatch()
        watch.feed(con.listen(self.timing.listen_s))
        if not watch.in_progress:
            return
        deadline = self._clock() + self.timing.boot_guard_s
        while watch.in_progress:
            if self._clock() >= deadline:
                raise ActionFailedError(
                    f"the MCC on {self.url} is still booting after {self.timing.boot_guard_s:.0f}s; "
                    "not typing (a keypress now would stop auto-boot)",
                    hint="wait for the boot to finish, then retry",
                )
            watch.feed(con.listen(self.timing.poll_s * 10))
        self._settle(con, watch)

    def _drain_to_prompt(self, con: _Console, watch: BootWatch, after: bytearray) -> None:
        """Read (never type) until the console ends in a prompt.

        Stops at ``banner_tail_s``, or after ``2 * quiet_s`` of silence (a prompt missed
        while the port was off USB); the next command's CR then finds the prompt itself.
        """
        if con.port is None:
            return
        t = self.timing
        end = self._clock() + t.banner_tail_s
        last_rx = self._clock()
        while not PROMPT_RE.search(after.decode("ascii", "replace")):
            now = self._clock()
            if now >= end or now - last_rx >= 2 * t.quiet_s:
                return
            try:
                data = con.listen(t.poll_s * 5)
            except UnreachableError:
                return
            if data:
                after += data
                watch.feed(data)
                last_rx = self._clock()

    def _settle(self, con: _Console, watch: BootWatch) -> None:
        """After a banner completes, give the MCC a moment to print its prompt before typing."""
        end = self._clock() + self.timing.reply_timeout_s
        while watch.settling and self._clock() < end:
            watch.feed(con.listen(self.timing.poll_s * 5))

    # -- ControllerAdapter --

    def command(self, line: str, *, arm: bool = False) -> str:
        """Run one allowlisted command; return the reply text. ``RefusedError`` otherwise.

        ``REBOOT`` runs the witnessed ``reboot()`` and returns its summary.
        ``CFG`` commands enter DEBUG and leave with EXIT automatically.
        """
        cmd = classify(line, arm=arm)
        if cmd.head == "REBOOT":
            self.reboot()
            assert self.last_reboot is not None
            return self.last_reboot.summary()
        with self._session() as con:
            con.sync()
            if cmd.head == "DEBUG":
                con.goto("debug")
                return ""
            if cmd.head == "EXIT":
                con.goto("main")
                return ""
            entered = cmd.menu == "debug" and con.menu != "debug"
            con.goto(cmd.menu)
            try:
                reply = con.run(cmd.text)
            finally:
                if entered and con.menu == "debug":
                    con.goto("main")
        err = reply_error(reply)
        if err:
            raise ActionFailedError(f"the MCC refused {cmd.text!r}: {err}")
        return reply

    def cfg_read(self, items: Iterable[tuple[str, int]]) -> list[str | HarnessError]:
        """Several ``CFG R`` reads in one DEBUG visit. Each result is the reply or its error."""
        cmds = [classify(f"CFG R {kind} {dev}") for kind, dev in items]
        results: list[str | HarnessError] = []
        with self._session() as con:
            con.sync()
            con.goto("debug")
            try:
                for cmd in cmds:
                    reply = con.run(cmd.text)
                    err = reply_error(reply)
                    results.append(ActionFailedError(f"MCC: {err}") if err else reply)
            finally:
                if con.menu == "debug":
                    try:
                        con.goto("main")
                    except HarnessError:
                        log.warning("MCC on %s: EXIT from DEBUG failed", self.url)
        return results

    def temperatures(self) -> Sequence[Reading]:
        name, unit, source = "mcc_temp", "degC", "mcc-console"
        try:
            (result,) = self.cfg_read([("TEMP", 0)])
        except HarnessError as exc:
            return [Reading.unavailable(name, unit, str(exc), source=source)]
        if isinstance(result, HarnessError):
            return [Reading.unavailable(name, unit, str(result), source=source)]
        value = parse_temp(result)
        if value is None:
            return [Reading.unavailable(name, unit, f"unrecognised MCC reply {result!r}", source=source)]
        return [Reading(name=name, value=value, unit=unit, source=source, reason=TEMP_CAVEAT)]

    def oscillators(self) -> Sequence[Reading]:
        source = "mcc-console setpoint"
        try:
            results = self.cfg_read([("OSC", n) for n in range(OSC_COUNT)])
        except HarnessError as exc:
            return [Reading.unavailable(f"osc{n}", "MHz", str(exc), source=source)
                    for n in range(OSC_COUNT)]
        out: list[Reading] = []
        for n, result in enumerate(results):
            parsed = None if isinstance(result, HarnessError) else parse_osc(result)
            if parsed is None or parsed[0] != n:
                why = str(result) if isinstance(result, HarnessError) else f"unrecognised MCC reply {result!r}"
                out.append(Reading.unavailable(f"osc{n}", "MHz", why, source=source))
            else:
                out.append(Reading(name=f"osc{n}", value=parsed[1], unit="MHz", source=source,
                                   reason=OSC_CAVEAT))
        return out

    def voltages(self, devices: Iterable[int] = range(4)) -> Sequence[Reading]:
        """``CFG R V n``. MCC firmware v1.3.2 answers ERROR for every device, so these
        come back unavailable, with the MCC's own words as the reason."""
        devs = list(devices)
        source = "mcc-console"
        try:
            results = self.cfg_read([("V", n) for n in devs])
        except HarnessError as exc:
            return [Reading.unavailable(f"mcc_v{n}", "V", str(exc), source=source) for n in devs]
        out: list[Reading] = []
        for n, result in zip(devs, results, strict=True):
            if isinstance(result, HarnessError):
                out.append(Reading.unavailable(f"mcc_v{n}", "V", str(result), source=source))
                continue
            m = re.search(r"(-?\d+(?:\.\d+)?)\s*(m?V)\b", result)
            if not m:
                out.append(Reading.unavailable(f"mcc_v{n}", "V", f"unrecognised MCC reply {result!r}",
                                               source=source))
                continue
            volts = float(m.group(1)) / (1000.0 if m.group(2) == "mV" else 1.0)
            out.append(Reading(name=f"mcc_v{n}", value=volts, unit="V", source=source))
        return out

    # -- reboot with a witness --

    def reboot(self, progress: Progress | None = None, wait_s: float | None = None) -> dict:
        """Send a paced REBOOT and prove the board went down and came back.

        Returns the witness as a dict (the full record is kept on ``last_reboot``)."""
        if wait_s is None:
            # 120 s bare-metal, 180 s Linux (stage0 + µSD + kernel), per constants (T12-6).
            from .constants import reboot_wait_s
            ident_fn = getattr(self, "identity_fn", None)
            try:
                wait_s = reboot_wait_s(ident_fn()) if ident_fn else reboot_wait_s(None)
            except Exception:  # noqa: BLE001 - an unreadable identity must not block a reboot
                wait_s = reboot_wait_s(None)
        guard_reset(self.session, "ACTION_MCC_REBOOT")     # SLOT-TIMING: not mid card job
        emit: Progress = progress or (lambda phase, done, total: None)
        shell_before = self._probe_shell()
        with self._session() as con:
            self._sync_for_reboot(con)      # refuses a port that is not the MCC
            con.goto("main")                # REBOOT is a main-menu command
            con.write_paced("REBOOT\r")
            sent_at = self._clock()
            emit("sent", 1, 3)
            self.last_reboot = self._witness(con, sent_at, wait_s, shell_before, emit)
        return self.last_reboot.as_dict()

    def _sync_for_reboot(self, con: _Console) -> int:
        """``sync``, tried again while the MCC answers a bare CR with only CR/LF (the
        post-SD-write quirk, ``POST_WRITE_TRIES``). Returns the attempt that found the prompt."""
        for attempt in range(1, POST_WRITE_TRIES + 1):
            try:
                con.sync()
                return attempt
            except NothingOnTargetError:
                if attempt >= POST_WRITE_TRIES or not bare_crlf(con.heard):
                    raise
                log.info("MCC on %s answered a bare CR with CR/LF only (after an SD write); "
                         "trying again in %.0f s (%d/%d)", self.url, POST_WRITE_GAP_S,
                         attempt, POST_WRITE_TRIES)
                self._sleep(POST_WRITE_GAP_S)
        raise AssertionError("unreachable")     # pragma: no cover

    def _probe_shell(self) -> str | None:
        if self._shell_probe is None:
            return None
        try:
            return self._shell_probe()
        except Exception:  # noqa: BLE001 - a probe failure means "not answering"
            return None

    def _reopen(self, con: _Console) -> None:
        try:
            con.port = self._opener(self.url, MCC_BAUD)
        except HarnessError:
            con.port = None

    def _witness(self, con: _Console, sent_at: float, wait_s: float,
                 shell_before: str | None, emit: Progress) -> RebootWitness:
        t = self.timing
        eth = self._shell_probe is not None
        eth_baseline = shell_before is not None
        watch = BootWatch()
        after = bytearray()                 # console bytes since the REBOOT
        last_rx = sent_at
        port_lost = False
        next_reopen = sent_at
        next_ping = sent_at
        ping_failed = False
        failed_in_row = 0
        down_at: float | None = None
        down_evidence: list[str] = []
        deadline = sent_at + wait_s

        while True:
            now = self._clock()
            # 1. the console, passively: never type during a boot.
            if con.port is None:
                if now >= next_reopen:
                    next_reopen = now + t.reopen_interval_s
                    self._reopen(con)
            else:
                try:
                    data = con.read_available()
                except OSError:
                    data = b""
                    port_lost = True
                    try:
                        con.port.close()
                    except OSError:
                        pass
                    con.port = None
                    next_reopen = now + t.reopen_interval_s
                if data:
                    after += data
                    watch.feed(data)
                    last_rx = now
            text_after = after.decode("ascii", "replace")
            prompt_back = (not watch.started) and "REBOOT" in text_after and bool(
                PROMPT_RE.search(text_after.split("REBOOT", 1)[1]))
            quiet = (not watch.started and not prompt_back
                     and now - last_rx >= t.quiet_s and "REBOOT" in text_after)

            # 2. the shell, if there is one.
            shell_now: str | None = None
            answered = False
            if eth and now >= next_ping:
                next_ping = now + t.ping_interval_s
                shell_now = self._probe_shell()
                failed_in_row = failed_in_row + 1 if shell_now is None else 0
                answered = shell_now not in (None, SHELL_BUSY)
                if failed_in_row >= t.down_pings:   # one lost ping is a glitch, not a reboot
                    ping_failed = True

            # 3. went down?
            if down_at is None:
                console_ev = []
                if watch.started:
                    console_ev.append("the MCC printed its boot banner")
                if port_lost:
                    console_ev.append("the MCC serial port dropped off USB")
                if quiet:
                    console_ev.append("the console went quiet after REBOOT (no prompt came back)")
                if eth and eth_baseline:
                    if ping_failed:
                        down_evidence = ["the shell stopped answering ping", *console_ev]
                elif console_ev:
                    down_evidence = console_ev
                if down_evidence:
                    down_at = now
                    emit("down", 2, 3)

            # 4. came back?
            if down_at is not None:
                rec = watch.record
                if eth:
                    # Up needs a real ping reply: a busy/reset port may be a shell mid-restart.
                    if answered and (ping_failed or not eth_baseline):
                        # The shell can answer before the MCC has finished its banner: let
                        # it reach its prompt, or the next MCC command finds none (T7-6).
                        self._drain_to_prompt(con, watch, after)
                        emit("up", 3, 3)
                        return RebootWitness(
                            sent_at=sent_at, down_after_s=down_at - sent_at, up_after_s=now - sent_at,
                            down_evidence=tuple(down_evidence),
                            up_evidence=f"the shell answers ping again (shell_id {shell_now})",
                            shell_id_before=shell_before, shell_id_after=shell_now,
                            boot=watch.record or rec)
                elif rec is not None and rec.complete:
                    if con.port is not None:
                        try:
                            self._settle(con, watch)
                        except UnreachableError:
                            pass
                        rec = watch.record or rec
                    if not rec.fpga_configured:
                        raise ActionFailedError(
                            "the MCC rebooted but did not configure the FPGA"
                            + (f": {'; '.join(rec.errors)}" if rec.errors else
                               " (the boot stopped before 'FPGA configuration complete.')"),
                            hint="check the SD's board.txt / nanosoc.txt and the .bit it names",
                        )
                    emit("up", 3, 3)
                    return RebootWitness(
                        sent_at=sent_at, down_after_s=down_at - sent_at, up_after_s=now - sent_at,
                        down_evidence=tuple(down_evidence),
                        up_evidence="the MCC boot banner completed with 'FPGA configuration complete.'",
                        boot=rec)

            if now >= deadline:
                break
            self._sleep(t.poll_s)

        raise self._no_restart(wait_s, down_at, down_evidence, watch, eth, eth_baseline,
                               prompt_back=prompt_back)

    @staticmethod
    def _no_restart(wait_s: float, down_at: float | None, down_evidence: list[str],
                    watch: BootWatch, eth: bool, eth_baseline: bool,
                    *, prompt_back: bool) -> ActionFailedError:
        rec = watch.record
        seen = []
        if rec is not None:
            seen.append("boot banner seen" + (f" (errors: {'; '.join(rec.errors)})" if rec.errors else ""))
        if down_at is None:
            if eth and eth_baseline and rec is not None:
                seen.append("but the shell never stopped answering ping")
            elif prompt_back:
                seen.append("the MCC echoed REBOOT and returned to its prompt")
            detail = ", ".join(seen) or "no banner, no dropped port, the shell never went down"
            return ActionFailedError(
                f"REBOOT sent but no restart observed within {wait_s:.0f}s: {detail}",
                hint="the command did not take (the old no-op trap); check the port is FT4232H "
                     "interface 00 and that nothing else holds the MCC console",
            )
        what = "the shell never answered ping again" if eth else "the MCC boot banner never completed"
        return ActionFailedError(
            f"the board went down after REBOOT ({'; '.join(down_evidence)}) but did not come back "
            f"within {wait_s:.0f}s: {what}" + (f"; {', '.join(seen)}" if seen else ""),
            hint="check the SD contents (board.txt, the .bit it names) and the MCC console",
        )


# --- the pack hook --------------------------------------------------------------------


def serial_url(address: str) -> str:
    """A link address as a serial URL: '/dev/ttyUSB0' -> 'serial:///dev/ttyUSB0', 'COM7' -> 'serial://COM7'."""
    return address if "://" in address else f"serial://{address}"


def _shell_probe_for(shell: Any, timeout: float) -> ShellProbe:
    from .shell import Mps3Shell

    pinger = Mps3Shell(shell.host, shell.port, timeout=timeout)

    def probe() -> str | None:
        try:
            ping = pinger.call(lambda c: c.ping())
        except UnreachableError:
            return None
        except HarnessError:
            # Held by another client, reset ("or the board is restarting", shell.py), or a
            # protocol error: something is there, but it is not a confirmed answer.
            return SHELL_BUSY
        return getattr(ping, "shell_id", "") or SHELL_BUSY

    return probe


def make_controller_adapter(session: Any) -> Any:
    """The ``pack.py`` hook: the MCC over the local Debug USB, else ON the hub, else ``None``.

    The MCC link is the first ``USB_SERIAL`` link that is not an FT4232H FPGA
    lane (``usb.probe_usb`` lists the MCC first). A candidate with only lane
    links gets no controller: typing REBOOT on a lane is the old silent no-op.

    A ``hub://`` link is never the MCC's (MCC-FIX: Harness Manager never holds an fpgahub
    share on tty_00; ``hub.share_links`` no longer makes one). A board behind a hub gets
    ``hub_mcc.HubMccController``: every MCC operation runs on the hub through pyverify.
    """
    from .usb import is_lane_link

    link = next((lk for lk in session.candidate.links
                 if lk.kind == LinkKind.USB_SERIAL and not is_lane_link(lk)
                 and not lk.address.startswith("hub://")), None)
    if link is None or not link.address:
        from .hub_mcc import make_hub_controller

        return make_hub_controller(session)
    timing = timing_for(serial_url(link.address))
    shell = getattr(session, "shell", None)
    probe = _shell_probe_for(shell, timing.ping_timeout_s) if shell is not None else None
    ctl = Mps3Controller(serial_url(link.address), timing=timing, clock=DEFAULT_CLOCK,
                         sleep=DEFAULT_SLEEP, shell_probe=probe)
    ctl.session = session
    if shell is not None:
        ctl.identity_fn = session.identity   # reboot's default wait follows the harness impl
    return ctl
