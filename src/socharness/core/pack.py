"""The board-pack interface. This is the seam that keeps the core board-agnostic.

A board pack knows one board family. It supplies:

- ``capability_specs()``: what the board can do and what each capability needs;
- ``probe()``: finds candidate boards on the links it can see (USB, network);
- ``open()``: opens a ``BoardSession`` for one candidate.

A ``BoardSession`` exposes optional *adapters*, one per service area. The
board-agnostic services in ``socharness.services`` drive boards only through
these adapters. An adapter a board cannot provide is ``None``, and the
matching capability is then unavailable with a reason.

CONTRACT: changes to anything in this file go through docs/CONTRACTS.md.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from .capabilities import CapabilitySpec
from .model import BoardIdentity, Candidate, Health, Reading


@dataclass(frozen=True)
class ProbeHints:
    """Where to look. Empty means "use the pack's defaults"."""

    hosts: tuple[str, ...] = ()          # explicit shell addresses, "192.168.10.101[:6900]"
    serial_ports: tuple[str, ...] = ()   # explicit serial devices
    volumes: tuple[str, ...] = ()        # explicit mounted volumes
    scan_usb: bool = True
    scan_network: bool = True
    timeout_s: float = 2.0


# --- service adapters (each optional on a session) --------------------------------


@runtime_checkable
class DeployAdapter(Protocol):
    def list_compatible(self) -> Sequence[str]: ...
    def deploy(self, rm_name: str) -> BoardIdentity: ...


@runtime_checkable
class ConsoleAdapter(Protocol):
    def console_endpoints(self) -> dict[str, str]:
        """Console name -> endpoint URL ("tcp://host:6930", "serial:///dev/ttyUSB12")."""
        ...


@runtime_checkable
class DebugAdapter(Protocol):
    def openocd_config(self) -> tuple[str, ...]:
        """OpenOCD target-half config files for the currently loaded design.

        Raises ``NothingOnTargetError`` when the loaded design has no debug port.
        """
        ...

    def openocd_probe_args(self) -> tuple[str, ...]:
        """Probe-half ``-c`` commands (adapter, host, port), set before any ``-f``."""
        ...


@runtime_checkable
class ResetAdapter(Protocol):
    def reset_targets(self) -> Sequence[str]: ...
    def reset(self, target: str) -> None: ...


@runtime_checkable
class ClockAdapter(Protocol):
    def clocks(self) -> Sequence[Reading]: ...
    def set_clock(self, name: str, mhz: float) -> Reading: ...


@runtime_checkable
class TelemetryAdapter(Protocol):
    def readings(self) -> Sequence[Reading]: ...


# --- session and pack ----------------------------------------------------------


class BoardSession(ABC):
    """An open connection to one board. Always use it as a context manager."""

    candidate: Candidate

    @abstractmethod
    def identity(self) -> BoardIdentity: ...

    @abstractmethod
    def health(self) -> Health: ...

    # Optional adapters. A None adapter means the capability is unavailable.
    deploy: DeployAdapter | None = None
    consoles: ConsoleAdapter | None = None
    debug: DebugAdapter | None = None
    resets: ResetAdapter | None = None
    clocks: ClockAdapter | None = None
    telemetry: TelemetryAdapter | None = None

    def close(self) -> None:  # noqa: B027 - optional hook
        """Release anything the session holds. Idempotent."""

    def __enter__(self) -> BoardSession:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


class BoardPack(ABC):
    name: str = ""          # entry-point name, "mps3"
    title: str = ""         # "Arm MPS3 (V2M-MPS3, HBI0309C)"

    @abstractmethod
    def capability_specs(self) -> Iterable[CapabilitySpec]: ...

    @abstractmethod
    def probe(self, hints: ProbeHints) -> list[Candidate]: ...

    @abstractmethod
    def open(self, candidate: Candidate) -> BoardSession: ...
