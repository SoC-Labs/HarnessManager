"""Transport seams: how adapters reach a serial port or a volume without caring
whether it is real hardware or a test fake.

Serial endpoints are URLs:

- ``serial:///dev/ttyUSB10``, ``serial://COM7``: a real port. The opener is
  registered by ``socharness.transports.direct`` (Team T3).
- ``fake://<name>``: an in-process fake registered by a test with
  ``register_fake_serial``.

A ``SerialPort`` is the subset of ``serial.Serial`` the drivers use. The
``FakeMcc`` in tests/fakes already satisfies it.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Protocol, runtime_checkable
from urllib.parse import urlparse

from .errors import AbsentError, UsageError


@runtime_checkable
class SerialPort(Protocol):
    def write(self, data: bytes) -> int: ...
    def read(self, size: int = 1) -> bytes: ...
    def read_until(self, expected: bytes = b"\n", size: int | None = None) -> bytes: ...
    @property
    def in_waiting(self) -> int: ...
    def reset_input_buffer(self) -> None: ...
    def close(self) -> None: ...


SerialOpener = Callable[[str, int], SerialPort]   # (address, baud) -> port

_OPENERS: dict[str, SerialOpener] = {}
_FAKES: dict[str, SerialPort] = {}


def register_serial_scheme(scheme: str, opener: SerialOpener) -> None:
    _OPENERS[scheme] = opener


def register_fake_serial(name: str, port: SerialPort) -> str:
    """Register an in-process fake; returns its URL (``fake://<name>``)."""
    _FAKES[name] = port
    return f"fake://{name}"


def unregister_fake_serial(name: str) -> None:
    _FAKES.pop(name, None)


def open_serial(url: str, baud: int = 115200) -> SerialPort:
    parsed = urlparse(url)
    if parsed.scheme == "fake":
        name = parsed.netloc or parsed.path.lstrip("/")
        if name not in _FAKES:
            raise AbsentError(f"no fake serial port named {name!r}")
        return _FAKES[name]
    opener = _OPENERS.get(parsed.scheme)
    if opener is None:
        raise UsageError(
            f"no opener for serial URL scheme {parsed.scheme!r}",
            hint="install socharness[serial] (pyserial) or check the URL",
        )
    address = parsed.netloc + parsed.path if parsed.netloc else parsed.path
    return opener(address, baud)
