"""The board-pack interface. This is the seam that keeps the core board-agnostic.

A board pack knows one board family. It supplies:

- ``capability_specs()``: what the board can do and what each capability needs;
- ``probe()``: finds candidate boards on the links it can see (USB, network);
- ``open()``: opens a ``BoardSession`` for one candidate.

A ``BoardSession`` exposes optional *adapters*, one per service area. The
board-agnostic services (``socharness.core.services`` protocols, implemented
in ``socharness.services``) drive boards only through these adapters. An
adapter a board cannot provide is ``None``, and the matching capability is
then unavailable with a reason.

CONTRACT (frozen for Wave 1): changes go through docs/CONTRACTS.md.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, runtime_checkable

from .capabilities import CapabilitySpec
from .model import BoardIdentity, Candidate, Check, Health, Reading


@dataclass(frozen=True)
class ProbeHints:
    """Where to look. Empty means "use the pack's defaults"."""

    hosts: tuple[str, ...] = ()          # explicit shell addresses, "192.168.10.101[:6900]"
    serial_ports: tuple[str, ...] = ()   # explicit serial URLs ("serial:///dev/ttyUSB10", "fake://mcc")
    volumes: tuple[str, ...] = ()        # explicit mounted volumes (config SD)
    scan_usb: bool = True
    scan_network: bool = True
    timeout_s: float = 2.0


# --- shared value types used by adapters --------------------------------------------


@dataclass(frozen=True)
class OverlayRef:
    """One loadable partition design (an overlay), as the deploy adapter knows it."""

    name: str                 # "nanosoc"
    rm_id: str                # "0x01000001"
    static_id: str            # the shell it is keyed to, "0x3f1a560f"
    static_usercode: str = ""  # the implementation run it is keyed to
    source: str = ""          # manifest path or content-store key
    size_bytes: int = 0
    ip_class: str = "unknown"  # "open" | "arm-aaa" | "unknown"


@dataclass(frozen=True)
class PreflightItem:
    name: str                 # "shell_id matches", "crc", "clearing fits", "transport"
    check: Check              # OK / MISMATCH / UNCHECKED (UNCHECKED is not a pass)
    detail: str = ""


@dataclass(frozen=True)
class DeployResult:
    rm_id: str
    verified: bool
    seconds: float
    transport: str = ""       # "tcp+windowed", "tftp"


Progress = Callable[[str, int, int], None]   # (phase, done, total)


@dataclass(frozen=True)
class BackupRecord:
    path: str                 # the backup archive
    sha256: str
    created_at: float
    files: int
    volume_label: str


# --- service adapters (each optional on a session) ------------------------------------


@runtime_checkable
class DeployAdapter(Protocol):
    def overlays(self) -> Sequence[OverlayRef]: ...
    def preflight(self, overlay: OverlayRef) -> Sequence[PreflightItem]: ...
    def deploy(self, overlay: OverlayRef, progress: Progress | None = None) -> DeployResult: ...
    def baseline(self) -> OverlayRef | None:
        """The safe design to restore to (greybox on the MPS3), if known."""
        ...


@runtime_checkable
class ConsoleAdapter(Protocol):
    def console_endpoints(self) -> dict[str, str]:
        """Console name -> endpoint URL ("tcp://host:6930", "serial:///dev/ttyUSB12", "fake://x")."""
        ...


@runtime_checkable
class DebugAdapter(Protocol):
    def openocd_config(self) -> tuple[str, ...]:
        """OpenOCD target-half config files for the loaded design.

        Raises ``NothingOnTargetError`` when the loaded design has no debug port.
        """
        ...

    def openocd_probe_args(self) -> tuple[str, ...]:
        """Probe-half ``-c`` commands (adapter, host, port). They go before any ``-f``."""
        ...

    def openocd_search_paths(self) -> tuple[Path, ...]:
        """Directories OpenOCD should search (``-s``) for the target-half configs."""
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


@runtime_checkable
class ControllerAdapter(Protocol):
    """The board controller (the MCC on the MPS3)."""

    def command(self, line: str) -> str:
        """Run one ALLOWLISTED command and return its reply text; else ``RefusedError``."""
        ...

    def reboot(self, progress: Progress | None = None, wait_s: float = 120.0) -> None:
        """Reboot and prove it: the board went down, then came back."""
        ...

    def temperatures(self) -> Sequence[Reading]: ...
    def oscillators(self) -> Sequence[Reading]: ...


@runtime_checkable
class StorageAdapter(Protocol):
    """The board's configuration storage (the MPS3 config microSD over USB MSD)."""

    def locate(self) -> str: ...
    def backup(self, dest_dir: Path, progress: Progress | None = None) -> BackupRecord: ...
    def install(self, files: Mapping[str, Path], *, backup: BackupRecord,
                progress: Progress | None = None) -> None: ...
    def restore(self, backup: BackupRecord, progress: Progress | None = None) -> None: ...


# --- session and pack ----------------------------------------------------------------


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
    controller: ControllerAdapter | None = None
    storage: StorageAdapter | None = None

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

    def candidate_for_host(self, spec: str) -> Candidate:
        """Build a candidate from an explicit address. Packs that can, override this."""
        from .errors import UsageError

        raise UsageError(f"pack {self.name!r} cannot open a board by address")
