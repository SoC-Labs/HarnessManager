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
- **REBOOT** calls ``on_reboot``; the virtual board reloads the shell from its
  SD image.
- **Destructive commands** are recorded in ``dangerous`` so tests can prove the
  driver never sends them.

The time source is injectable (``clock``), so pacing tests are deterministic.
The public surface mimics the subset of ``serial.Serial`` the driver uses:
``write``, ``read``, ``read_until``, ``in_waiting``, ``reset_input_buffer``
and ``close``.
"""

from __future__ import annotations

import time
from collections.abc import Callable

PROMPT_MAIN = b"Cmd> "
PROMPT_DEBUG = b"Debug> "
DANGEROUS = {"FORMAT", "DEL", "EEPROM", "USB_OFF", "SHUTDOWN", "REN", "COPY", "CAP", "FILL"}


class FakeMcc:
    def __init__(
        self,
        *,
        clock: Callable[[], float] = time.monotonic,
        pace_s: float = 0.05,
        temp_c: float = 35.5,
        osc_mhz: dict[int, float] | None = None,
        on_reboot: Callable[[], None] | None = None,
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
        self.accepted_lines: list[str] = []
        self.dropped_chars = 0
        self.dangerous: list[str] = []
        self.reboots = 0
        self.closed = False

    # -- serial.Serial subset -----------------------------------------------------

    def write(self, data: bytes) -> int:
        for byte in data:
            now = self._clock()
            if self._last_char_at is not None and now - self._last_char_at < self._pace:
                self.dropped_chars += 1
                continue
            self._last_char_at = now
            self._feed(bytes([byte]))
        return len(data)

    @property
    def in_waiting(self) -> int:
        return len(self._out)

    def read(self, size: int = 1) -> bytes:
        chunk = bytes(self._out[:size])
        del self._out[:size]
        return chunk

    def read_until(self, expected: bytes = b"\n", size: int | None = None) -> bytes:
        idx = self._out.find(expected)
        end = len(self._out) if idx < 0 else idx + len(expected)
        if size is not None:
            end = min(end, size)
        return self.read(end)

    def reset_input_buffer(self) -> None:
        self._out.clear()

    def close(self) -> None:
        self.closed = True

    # -- command handling ---------------------------------------------------------

    def _feed(self, ch: bytes) -> None:
        self._out += ch  # the MCC echoes
        if ch in (b"\r", b"\n"):
            line = self._line.decode("ascii", "replace").strip()
            self._line.clear()
            self._out += b"\r\n"
            if line:
                self._execute(line)
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
            self.reboots += 1
            self._reply("Rebooting...")
            self.menu = "main"
            if self.on_reboot:
                self.on_reboot()
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
        self._reply(f"Command error {line}")
