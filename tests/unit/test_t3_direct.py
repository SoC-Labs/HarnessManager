"""T3: the serial:// opener (pyserial), with no hardware.

pyserial's ``loop://`` URL handler is a real pyserial port object that echoes
what is written, so the registered opener is exercised end to end without a
device. Error mapping is checked on synthetic exceptions shaped like pyserial's
POSIX and Windows messages.
"""

from __future__ import annotations

import errno
import sys

import pytest

from harness_manager.core.errors import (
    AbsentError,
    ExitCode,
    HeldError,
    UnreachableError,
    UsageError,
)
from harness_manager.core.transport import open_serial
from harness_manager.transports import direct


def test_importing_mcc_registers_the_serial_scheme():
    import harness_manager_mps3.mcc  # noqa: F401 - the import is the registration
    from harness_manager.core import transport

    assert transport._OPENERS["serial"] is direct.open_pyserial


def test_serial_url_opens_a_real_pyserial_port():
    port = open_serial("serial://loop://")
    try:
        assert port.baudrate == 115200 and port.bytesize == 8 and port.parity == "N"
        port.write(b"Cmd> ")
        assert port.read(5) == b"Cmd> "
        assert port.in_waiting == 0
    finally:
        port.close()


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="POSIX device path")
def test_missing_device_is_absent():
    with pytest.raises(AbsentError) as info:
        open_serial("serial:///dev/ttyT3-does-not-exist")
    assert info.value.code == ExitCode.ABSENT


def test_unknown_pyserial_handler_is_a_usage_error():
    with pytest.raises(UsageError):
        open_serial("serial://nosuchhandler://x")


def test_empty_address_is_a_usage_error():
    with pytest.raises(UsageError):
        direct.open_pyserial("")


class _SerialException(OSError):
    pass


@pytest.mark.parametrize(("exc", "kind"), [
    (_SerialException(errno.ENOENT, "could not open port /dev/ttyUSB9: No such file"), AbsentError),
    (_SerialException(errno.EBUSY, "could not open port /dev/ttyUSB9: Device or resource busy"),
     HeldError),
    (_SerialException(errno.EACCES, "could not open port /dev/ttyUSB9: Permission denied"),
     UnreachableError),
    (_SerialException("could not open port 'COM7': PermissionError(13, 'Access is denied.', None, 5)"),
     HeldError),
    (_SerialException("could not open port 'COM9': FileNotFoundError(2, 'The system cannot find the "
                      "file specified.', None, 2)"), AbsentError),
    (_SerialException(errno.EIO, "could not open port /dev/ttyUSB9: Input/output error"),
     UnreachableError),
])
def test_open_errors_map_to_exit_codes(exc, kind):
    err = direct.classify_open_error("/dev/ttyUSB9", exc)
    assert type(err) is kind


def test_permission_error_hints_at_the_group():
    err = direct.classify_open_error("/dev/ttyUSB9", _SerialException(errno.EACCES, "Permission denied"))
    assert "group" in err.hint


def test_missing_pyserial_is_a_usage_error(monkeypatch):
    monkeypatch.setitem(sys.modules, "serial", None)      # import serial -> ImportError
    with pytest.raises(UsageError, match="pyserial is not installed") as info:
        direct.open_pyserial("/dev/ttyUSB10")
    assert "harness-manager[serial]" in info.value.hint
    with pytest.raises(UsageError):
        direct.comports()


def test_opener_is_windows_safe_for_com_names(monkeypatch):
    seen = {}

    class FakeSerialModule:
        EIGHTBITS, PARITY_NONE, STOPBITS_ONE = 8, "N", 1

        @staticmethod
        def serial_for_url(url, **kw):
            seen["url"] = url
            seen.update(kw)
            return object()

    monkeypatch.setitem(sys.modules, "serial", FakeSerialModule)
    open_serial("serial://COM7")
    assert seen["url"] == "COM7" and seen["baudrate"] == 115200
    open_serial("serial:///dev/ttyUSB10", 9600)
    assert seen["url"] == "/dev/ttyUSB10" and seen["baudrate"] == 9600
