"""Direct transports: serial ports through pyserial (Team T3).

Importing this module registers the ``serial://`` scheme with
``socharness.core.transport``, so ``open_serial("serial:///dev/ttyUSB10")``
and ``open_serial("serial://COM7")`` reach a real port. pyserial is the only
third-party runtime dependency this needs, and it is imported lazily: without
it the module still imports, and opening a ``serial://`` URL raises a
``UsageError`` that says how to install it (``socharness[serial]``).

Address forms (the part after ``serial://``):

- a device path: ``/dev/ttyUSB10``, ``/dev/mps3_01_pl/tty_00``, ``COM7``;
- any pyserial URL handler, because the opener uses ``serial_for_url``:
  ``serial://loop://`` is a pyserial loopback port (tests use it, no hardware).

Port settings are the MPS3 MCC's: 115200 8N1 (fpgahub ``tty_share.SerialConfig``;
fpgahub ``mcc.py`` probes of 2026-07-14). Reads use a short timeout because
the drivers poll ``in_waiting`` and do their own waiting (fpgahub ``mcc.py``
``DEFAULT_SERIAL_READ_TIMEOUT_S = 0.1`` for the same reason).

Open failures are mapped onto the exit-code taxonomy:

- the device does not exist -> ``AbsentError`` (3);
- another program holds it (EBUSY on Linux; "Access is denied" on Windows,
  where COM ports are exclusive) -> ``HeldError`` (4);
- no permission on Linux (not in the tty's group; fpgahub 0.3.0 group-protects
  the hub's ttys) -> ``UnreachableError`` (7) with a hint;
- anything else -> ``UnreachableError`` (7).
"""

from __future__ import annotations

import errno
from typing import Any

from socharness.core.errors import (
    AbsentError,
    HarnessError,
    HeldError,
    UnreachableError,
    UsageError,
)
from socharness.core.transport import SerialPort, register_serial_scheme

SERIAL_SCHEME = "serial"
READ_TIMEOUT_S = 0.1
WRITE_TIMEOUT_S = 2.0

_INSTALL_HINT = "install pyserial: pip install 'socharness[serial]'"


def _import_serial() -> Any:
    """Import pyserial lazily. Raises ``UsageError`` when it is missing."""
    try:
        import serial  # optional dependency, imported on use
    except ImportError as exc:
        raise UsageError("pyserial is not installed, so serial:// ports cannot be opened",
                         hint=_INSTALL_HINT) from exc
    return serial


def classify_open_error(address: str, exc: BaseException) -> HarnessError:
    """Map a pyserial/OS open failure onto a ``HarnessError`` with an exit code."""
    code = getattr(exc, "errno", None)
    text = str(exc)
    low = text.lower()
    if code in (errno.ENOENT, errno.ENODEV, errno.ENXIO) or "filenotfounderror" in low \
            or "cannot find the file" in low or "no such file" in low:
        return AbsentError(f"serial port {address} does not exist ({text})",
                           hint="check the Debug USB cable, then run `socharness probe`")
    if code == errno.EBUSY or "access is denied" in low or "resource busy" in low:
        return HeldError(f"serial port {address} is held by another program ({text})",
                         hint="close the other terminal or fpgahub share using it")
    if code == errno.EACCES or "permission denied" in low:
        return UnreachableError(
            f"no permission to open serial port {address} ({text})",
            hint="add your user to the port's group (dialout, or 'fpga' on the hub) and log in again",
        )
    return UnreachableError(f"cannot open serial port {address}: {text}")


def open_pyserial(address: str, baud: int = 115200) -> SerialPort:
    """Open ``address`` with pyserial at ``baud`` 8N1. The ``serial://`` opener."""
    if not address:
        raise UsageError("empty serial port address", hint="use serial:///dev/ttyUSB0 or serial://COM7")
    serial = _import_serial()
    try:
        port = serial.serial_for_url(
            address,
            baudrate=baud,
            bytesize=serial.EIGHTBITS,
            parity=serial.PARITY_NONE,
            stopbits=serial.STOPBITS_ONE,
            timeout=READ_TIMEOUT_S,
            write_timeout=WRITE_TIMEOUT_S,
        )
    except ValueError as exc:  # a bad setting or an unknown pyserial URL handler
        raise UsageError(f"cannot open serial port {address}: {exc}") from exc
    except OSError as exc:  # serial.SerialException is an OSError (IOError)
        raise classify_open_error(address, exc) from exc
    return port


def comports() -> list[Any]:
    """List the host's serial ports (pyserial ``list_ports.comports``).

    Raises ``UsageError`` when pyserial is missing. Listing never opens a port.
    """
    _import_serial()
    from serial.tools import list_ports

    return list(list_ports.comports())


register_serial_scheme(SERIAL_SCHEME, open_pyserial)
