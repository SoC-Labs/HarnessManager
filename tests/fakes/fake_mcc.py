"""FakeMcc: the MPS3 board controller's ``Cmd>`` console, modelled for tests.

Modelled behaviours, each from a real observation:

- **Burst drop.** The MCC drops characters that arrive less than ~50 ms after
  the previous one; only the first character of a burst lands (fpgahub
  30ae4f3). A driver that sends "REBOOT\\r" in one write gets "R" through.
- **Menus.** ``DEBUG`` enters the debug menu, and ``CFG R …`` only works there
  (TRM 100765 §3.6.3). ``EXIT`` returns to the main menu.
- **Replies** are copied from the live board where we have them
  (docs/evidence/2026-09-w2/pB_mcc_log_20260923.txt and the fpgahub mps3_mcc
  probe of 2026-07-14):
  ``CFG R TEMP 0`` -> ``MB Device 0 Temp: 35.5 degC``;
  ``CFG R V n`` -> ERROR on every device.
- **REBOOT** calls ``on_reboot`` at once (the board goes down; the virtual
  board counts it), then replays the real power-on banner (``BOOT_BANNER``,
  the first block of pB_mcc_log_20260923.txt with the 92 ``Address:`` progress
  records shortened) over ``down_s + boot_s`` seconds of the injected clock.
  ``on_boot`` fires at ``FPGA configuration complete.`` (the fabric is back).
  The banner ends with a ``Cmd>`` prompt.
- **Auto-boot abort (a trap).** The banner prints ``Press Enter to stop auto
  boot...`` and the MCC waits ``AUTORUNDELAY`` seconds (config.txt, 3 s in
  fpga/mps3_sd/templates/config.txt) for a key. A character written in that
  window STOPS the boot: the MCC drops to ``Cmd>`` and the FPGA is never
  configured (``autoboot_aborted``; ``on_boot`` never fires). Characters
  written at any other time during the boot are ignored
  (``ignored_while_booting``). A driver must never write while the MCC boots.
- **No-op REBOOT (a trap).** ``ignore_reboot=True`` accepts ``REBOOT``, echoes
  it and prints a prompt, and does nothing else: the "ok, REBOOT sent" that
  changed nothing (fpgahub reset.mcc, 2026-07-28 x3, 07-30, 08-04; those were
  sent to tty_01, an FPGA lane, but the symptom is the same).
- **USB drop (optional).** ``drop_port_on_reboot=True`` makes every port call
  raise ``OSError`` from REBOOT until the banner's ``Enabling usb remote...``
  line, and the lines printed meanwhile are lost. The fpgahub driver warns the
  MCC "re-enumerates the USB stack as it reboots" (mcc.py ``reboot``); the
  FT4232H is a separate USB device, so this is unverified, but a driver must
  survive it.
- **Post-SD-write quirk (optional).** ``bare_crlf_crs = N`` answers the next N bare CRs
  with ``\r\n`` only, no prompt: what the MCC does for a few seconds after an SD write
  (silicon, reproduced twice, 2026-09-26).
- **Destructive commands** are recorded in ``dangerous`` so tests can prove the
  driver never sends them. ``writes`` records every byte written.

The time source is injectable (``clock``; also settable after construction),
so pacing and boot timing are deterministic. The public surface mimics the
subset of ``serial.Serial`` the driver uses: ``write``, ``read``,
``read_until``, ``in_waiting``, ``reset_input_buffer`` and ``close``.
"""

from __future__ import annotations

import time
from collections.abc import Callable

PROMPT_MAIN = b"Cmd> "
PROMPT_DEBUG = b"Debug> "
DANGEROUS = {"FORMAT", "DEL", "EEPROM", "USB_OFF", "SHUTDOWN", "REN", "COPY", "CAP", "FILL"}

# The first power-on block of docs/evidence/2026-09-w2/pB_mcc_log_20260923.txt,
# verbatim except the Address: progress records (92 of them, CR-separated on the
# real console) are cut to three.
BOOT_BANNER: tuple[str, ...] = (
    "ARM V2M-MPS3 Boot loader v1.0.0",
    "HBI0309 build 567",
    "ARM V2M-MPS3 Firmware v1.3.2",
    "Build Date: Apr 20 2018",
    "Press Enter to stop auto boot...",
    "Enabling usb remote...",
    "USB Serial Number = 0000000000000",
    "Powering up system...",
    "ERROR: File not found \\MB\\HBI0309C\\mbb_v141.ebf",
    "Switching on main power...",
    "Configuring motherboard (rev C, var A)...",
    "Reading Board File \\MB\\HBI0309C\\Nanosoc\\nanosoc.txt",
    "Configuring FPGA from file \\MB\\HBI0309C\\Nanosoc\\nanosoc.bit",
    "Address: 0x00000000\rAddress: 0x00020000\rAddress: 0x00B60000",
    "FPGA configuration complete.",
    "OSCCLK0 : 25.000000MHz",
    "OSCCLK1 : 50.000000MHz",
    "OSCCLK2 : 50.000000MHz",
    "OSCCLK3 : 50.000000MHz",
    "OSCCLK4 : 24.576000MHz",
    "OSCCLK5 : 23.750000MHz",
    "OSCCLK setup: PASSED",
    "GTXCLK (100): PASSED",
    "FPGA does not support MCC<>SCC interface",
    "WARNING: Image file not found",
    "Reading images file \\MB\\HBI0309C\\Nanosoc\\images.txt",
    "ERROR: File not found \\MB\\HBI0309C\\Nanosoc\\images.txt",
    "ERROR: The SMSC9220 bus is in 16-bit mode. 32-bit mode was expected.",
    "Reading the Ethernet ID register failed.",
    "Check that the SMSC9220 device is present on the system.",
    "SMSC9220 initialisation failed.",
    "UART0: MCC, UART1: FPGA0",
    "Releasing CB_nRST",
    "Enabling debug USB.",
    "USB Serial Number = 0000000000000",
)

AUTOBOOT_LINE = "Press Enter to stop auto boot..."
USB_BACK_LINE = "Enabling usb remote..."
FPGA_DONE_LINE = "FPGA configuration complete."


class FakeMcc:
    def __init__(
        self,
        *,
        clock: Callable[[], float] = time.monotonic,
        pace_s: float = 0.05,
        temp_c: float = 35.5,
        osc_mhz: dict[int, float] | None = None,
        on_reboot: Callable[[], None] | None = None,
        on_boot: Callable[[], None] | None = None,
        ignore_reboot: bool = False,
        boot_banner: tuple[str, ...] = BOOT_BANNER,
        down_s: float = 0.05,
        boot_s: float = 0.3,
        autoboot_window_s: float = 0.05,
        drop_port_on_reboot: bool = False,
    ) -> None:
        self._clock = clock
        self._pace = pace_s
        self._last_char_at: float | None = None
        self._line = bytearray()
        self._out = bytearray(PROMPT_MAIN)
        self.menu = "main"
        self.temp_c = temp_c
        self.osc_mhz = osc_mhz or {0: 25.0, 1: 50.0, 2: 50.0, 3: 50.0, 4: 24.576, 5: 23.75}
        self.on_reboot = on_reboot
        self.on_boot = on_boot
        self.ignore_reboot = ignore_reboot
        self.boot_banner = boot_banner
        self.down_s = down_s
        self.boot_s = boot_s
        self.autoboot_window_s = autoboot_window_s
        self.drop_port_on_reboot = drop_port_on_reboot
        self.accepted_lines: list[str] = []
        self.dropped_chars = 0
        self.dangerous: list[str] = []
        self.writes = bytearray()
        self.reboots = 0
        self.ignored_reboots = 0
        self.boots_completed = 0
        self.booting = False
        self.autoboot_aborted = False
        self.ignored_while_booting = 0
        self.closed = False
        #: MCC-FIX: the next N bare CRs are answered with only CR/LF (the post-SD-write quirk)
        self.bare_crlf_crs = 0
        self._schedule: list[tuple[float, bytes, Callable[[], None] | None]] = []
        self._autoboot_window: tuple[float, float] | None = None
        self._port_dead_until_usb = False

    @property
    def clock(self) -> Callable[[], float]:
        return self._clock

    @clock.setter
    def clock(self, value: Callable[[], float]) -> None:
        self._clock = value

    # -- serial.Serial subset -----------------------------------------------------

    def write(self, data: bytes) -> int:
        self._advance()
        self._check_port()
        self.writes += data
        for byte in data:
            now = self._clock()
            if self._last_char_at is not None and now - self._last_char_at < self._pace:
                self.dropped_chars += 1
                continue
            self._last_char_at = now
            if self.booting:
                self._key_while_booting(now)
                continue
            self._feed(bytes([byte]))
        return len(data)

    @property
    def in_waiting(self) -> int:
        self._advance()
        self._check_port()
        return len(self._out)

    def read(self, size: int = 1) -> bytes:
        self._advance()
        self._check_port()
        chunk = bytes(self._out[:size])
        del self._out[:size]
        return chunk

    def read_until(self, expected: bytes = b"\n", size: int | None = None) -> bytes:
        self._advance()
        self._check_port()
        idx = self._out.find(expected)
        end = len(self._out) if idx < 0 else idx + len(expected)
        if size is not None:
            end = min(end, size)
        return self.read(end)

    def reset_input_buffer(self) -> None:
        self._advance()
        self._out.clear()

    def close(self) -> None:
        self.closed = True

    # -- boot model -----------------------------------------------------------------

    def _check_port(self) -> None:
        if self._port_dead_until_usb:
            raise OSError(5, "device reports readiness to read but returned no data "
                             "(device disconnected?)")

    def _advance(self) -> None:
        now = self._clock()
        while self._schedule and self._schedule[0][0] <= now:
            _, data, action = self._schedule.pop(0)
            if not self._port_dead_until_usb:
                self._out += data
            if action is not None:
                action()

    def _start_boot(self) -> None:
        now = self._clock()
        self.booting = True
        if self.drop_port_on_reboot:
            self._port_dead_until_usb = True
        lines = list(self.boot_banner)
        step = self.boot_s / max(len(lines), 1)
        t = now + self.down_s
        for line in lines:
            action: Callable[[], None] | None = None
            if line == AUTOBOOT_LINE:
                start = t
                action = self._arm_autoboot_window(start)
            elif line == USB_BACK_LINE:
                action = self._usb_back
            elif line == FPGA_DONE_LINE:
                action = self._fpga_done
            self._schedule.append((t, line.encode() + b"\r\n", action))
            t += step
            if line == AUTOBOOT_LINE:
                t += self.autoboot_window_s   # the MCC waits here for a key
        self._schedule.append((t, PROMPT_MAIN, self._boot_done))

    def _arm_autoboot_window(self, start: float) -> Callable[[], None]:
        def arm() -> None:
            self._autoboot_window = (start, start + self.autoboot_window_s)
        return arm

    def _usb_back(self) -> None:
        self._port_dead_until_usb = False

    def _fpga_done(self) -> None:
        if self.on_boot:
            self.on_boot()

    def _boot_done(self) -> None:
        self.booting = False
        self._autoboot_window = None
        self.boots_completed += 1

    def _key_while_booting(self, now: float) -> None:
        window = self._autoboot_window
        if window is not None and window[0] <= now <= window[1]:
            # "Press Enter to stop auto boot...": a key here stops the boot.
            self.autoboot_aborted = True
            self.booting = False
            self._autoboot_window = None
            self._schedule.clear()
            self._port_dead_until_usb = False
            self._out += b"\r\n" + PROMPT_MAIN
            return
        self.ignored_while_booting += 1

    # -- command handling ---------------------------------------------------------

    def _feed(self, ch: bytes) -> None:
        if ch in (b"\r", b"\n") and not self._line.strip() and self.bare_crlf_crs > 0:
            # The post-SD-write quirk (silicon, 2026-09-26): a bare CR gets only CR/LF.
            self.bare_crlf_crs -= 1
            self._out += b"\r\n"
            return
        self._out += ch  # the MCC echoes
        if ch in (b"\r", b"\n"):
            line = self._line.decode("ascii", "replace").strip()
            self._line.clear()
            self._out += b"\r\n"
            if line:
                self._execute(line)
            if self.booting:
                return                  # the banner ends with its own prompt
            self._out += PROMPT_DEBUG if self.menu == "debug" else PROMPT_MAIN
        else:
            self._line += ch

    def _reply(self, text: str) -> None:
        self._out += text.encode() + b"\r\n"

    def _execute(self, line: str) -> None:
        self.accepted_lines.append(line)
        words = line.upper().split()
        head = words[0]
        if head in DANGEROUS:
            self.dangerous.append(line)
            self._reply(f"{head}: done")
            return
        if head == "REBOOT":
            if self.ignore_reboot:
                self.ignored_reboots += 1
                return
            self.reboots += 1
            self._reply("Rebooting...")
            self.menu = "main"
            if self.on_reboot:
                self.on_reboot()
            self._start_boot()
            return
        if self.menu == "main":
            if head == "DEBUG":
                self.menu = "debug"
            elif head in ("HELP", "?"):
                self._reply("CAP COPY DEBUG DEL DIR EEPROM FILL FORMAT HELP REBOOT REN RESET SHUTDOWN TYPE USB_ON USB_OFF")
            else:
                self._reply(f"Command error {line}")
            return
        # debug menu
        if head == "EXIT":
            self.menu = "main"
            return
        if len(words) >= 4 and words[0] == "CFG" and words[1] == "R":
            what, dev = words[2], words[3]
            if what == "TEMP" and dev == "0":
                self._reply(f"MB Device 0 Temp: {self.temp_c:.1f} degC")
            elif what == "OSC" and dev.isdigit() and int(dev) in self.osc_mhz:
                self._reply(f"MB OSC{dev} clock read = {self.osc_mhz[int(dev)]:.3f} MHz")
            else:
                self._reply("ERROR: Unable to perform requested function")
            return
        if len(words) == 5 and words[0] == "CFG" and words[1] == "W" and words[2] == "OSC":
            dev, value = words[3], words[4]
            if dev.isdigit() and int(dev) in self.osc_mhz:
                try:
                    self.osc_mhz[int(dev)] = float(value)
                except ValueError:
                    self._reply("ERROR: Unable to perform requested function")
                    return
                self._reply(f"MB OSC{dev} clock write = {float(value):.3f} MHz")
            else:
                self._reply("ERROR: Unable to perform requested function")
            return
        self._reply(f"Command error {line}")


class SilentPort:
    """An FT4232H FPGA UART lane with nothing driving it: accepts writes, never replies.

    This is what the historical no-op REBOOT was sent to (tty_01 = FPGA UART
    lane 0; docs/evidence/2026-09-w2/pB_mcc_log_20260923.txt "UART0: MCC,
    UART1: FPGA0").
    """

    def __init__(self) -> None:
        self.writes = bytearray()
        self.closed = False

    def write(self, data: bytes) -> int:
        self.writes += data
        return len(data)

    @property
    def in_waiting(self) -> int:
        return 0

    def read(self, size: int = 1) -> bytes:
        return b""

    def read_until(self, expected: bytes = b"\n", size: int | None = None) -> bytes:
        return b""

    def reset_input_buffer(self) -> None:
        pass

    def close(self) -> None:
        self.closed = True
