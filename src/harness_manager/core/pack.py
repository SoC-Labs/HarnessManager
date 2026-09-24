"""The board-pack interface. This is the seam that keeps the core board-agnostic.

A board pack knows one board family. It supplies:

- ``capability_specs()``: what the board can do and what each capability needs;
- ``probe()``: finds candidate boards on the links it can see (USB, network);
- ``open()``: opens a ``BoardSession`` for one candidate.

A ``BoardSession`` exposes optional *adapters*, one per service area. The
board-agnostic services (``harness_manager.core.services`` protocols, implemented
in ``harness_manager.services``) drive boards only through these adapters. An
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
    via: str = ""                        # "ssh:HOST": reach the hosts through an SSH tunnel (L1)


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
    # Optional files that travel with the pair (additive; "" = the overlay has none):
    ltx_sha256: str = ""      # the ILA probes file (<rm>.ltx), by its sha256
    receipt_sha256: str = ""  # the build receipt (<rm>_build.json), by its sha256


@dataclass(frozen=True)
class PreflightItem:
    name: str                 # "shell_id matches", "crc", "clearing fits", "transport"
    check: Check              # OK / MISMATCH / UNCHECKED (UNCHECKED is not a pass)
    detail: str = ""
    identity: bool = False    # a MISMATCH here means "built for a different board"


def preflight_refusal(items: Sequence[PreflightItem], overlay_name: str):
    """The error a front-end or service raises for a failed preflight, or None.

    Any MISMATCH refuses. An identity MISMATCH is ``IncompatibleError`` (14);
    any other is ``RefusedError`` (15). UNCHECKED never blocks. The rule lives
    here so the CLI, GUI and deploy service always agree.
    """
    from .errors import IncompatibleError, RefusedError

    bad = [i for i in items if i.check == Check.MISMATCH]
    if not bad:
        return None
    text = "; ".join(f"{i.name}: {i.detail}" for i in bad)
    if any(i.identity for i in bad):
        return IncompatibleError(
            f"{overlay_name} does not match this board ({text})",
            hint="use an overlay built for the running shell, or restore the shell it was built for")
    return RefusedError(f"refusing to deploy {overlay_name} ({text})",
                        hint="nothing was pushed; rebuild or re-import the overlay")


@dataclass(frozen=True)
class BuildProfile:
    """What building a DUT for one static needs: the KIT-GUIDE pack hook (CCR KG-1).

    A pack's ``KitAdapter.build_profile(static_id, kit)`` returns one. The facts come
    from the static's build kit when there is one (``source == "kit"``), else from what
    the pack itself knows (``"pack"``: its pin model), with ``vivado`` then unknown.
    """

    pack: str                 # "mps3"
    static_id: str            # "0x72BB0A36"
    part: str                 # "xcku115-flvb1760-1-c"
    rp_inst: str              # "u_rp_dut"
    rp_pblock: str            # "pblock_rp_dut"
    boundary_ports: int       # 47
    boundary_bits: int        # 148
    clr_max: int              # the harness's clearing arena, bytes
    vivado: str = ""          # the release the static was written by ("2024.1"); "" = unknown
    vivado_build: int = 0
    static_usercode: str = ""
    harness_impl: str = ""    # "bare-metal" | "linux" | ""
    kit_id: str = ""          # "mps3/0x72BB0A36/vivado-2024.1" when a kit backs the profile
    user_design_ids: tuple[int, int] = (0x8000, 0xFFFF)   # rm_id[15:0] range for user RMs (K8)
    source: str = "pack"      # "kit" | "pack"


@dataclass(frozen=True)
class KitCheck:
    """One check of a build kit, a receipt or a partial pair (four states, unlike
    ``PreflightItem``: a Vivado release mismatch is a warning, never a refusal, david K4)."""

    name: str
    state: str                # "ok" | "mismatch" | "warning" | "unchecked"
    detail: str = ""
    identity: bool = False    # a mismatch here means "built for a different static"


def kit_refusal(items: Sequence[KitCheck], what: str):
    """The error for failed kit checks, or None. Any ``mismatch`` refuses: an identity one
    is ``IncompatibleError`` (14), any other ``RefusedError`` (15). ``warning`` and
    ``unchecked`` never block (the ``preflight_refusal`` rule, with one more state)."""
    from .errors import IncompatibleError, RefusedError

    bad = [i for i in items if i.state == "mismatch"]
    if not bad:
        return None
    text = "; ".join(f"{i.name}: {i.detail}" for i in bad[:4]) + (" ..." if len(bad) > 4 else "")
    if any(i.identity for i in bad):
        return IncompatibleError(f"{what} does not match ({text})",
                                 hint="use the kit, build or board of the same static")
    return RefusedError(f"{what} failed {len(bad)} check{'s' if len(bad) != 1 else ''} ({text})",
                        hint="every check is listed with --json")


@runtime_checkable
class KitAdapter(Protocol):
    """A board pack's DUT-build support (KIT-CORE; docs/design/DUT_BUILD_*.md).

    Found by module convention, like the pin model: ``<pack package>.kit`` with
    ``make_kit_adapter() -> KitAdapter``. A pack without that module has no build kit
    (capability ``build_kit`` unavailable, with the reason). ``kit`` arguments are
    ``harness_manager.services.kit.KitManifest`` objects.
    """

    def build_profile(self, static_id: str, kit: object | None = None) -> BuildProfile | None:
        """The facts for building against ``static_id``; None if the pack knows nothing of it."""
        ...

    def check_kit(self, kit: object, identity: BoardIdentity | None) -> Sequence[KitCheck]:
        """The kit against the board's live static: shell_id (identity), usercode (identity;
        unchecked when the board does not report it), part. ``identity`` None = no board."""
        ...

    def kit_from_dir(self, directory: Path) -> tuple[dict, dict[str, Path]] | None:
        """A kit.json document and its files ``{kit path: source file}`` for a directory of
        loose mint files (a ``fielded/<sid>/`` or a mint ``prod/`` dir); None if it is not one."""
        ...


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

    def command(self, line: str, *, arm: bool = False) -> str:
        """Run one ALLOWLISTED command and return its reply text; else ``RefusedError``.

        ``arm=True`` is required for allowlisted commands that change state
        (e.g. ``CFG W OSC``).
        """
        ...

    def reboot(self, progress: Progress | None = None, wait_s: float | None = None) -> dict | None:
        """Reboot and prove it: the board went down, then came back. Returns the evidence."""
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
    def load_backup(self, path: Path) -> BackupRecord:
        """Rebuild a ``BackupRecord`` from a backup archive on disk (and verify it)."""
        ...

    def pending(self) -> dict | None:
        """An interrupted install's journal, if one exists (show "restore" first); else None."""
        ...


@runtime_checkable
class PowerAdapter(Protocol):
    """A metered outlet or meter on the board's supply (T9). Board-agnostic."""

    label: str

    def read(self) -> list[Reading]:
        """``board_power`` (W), ``supply_voltage`` (V), ``supply_current`` (A). Never raises."""
        ...

    @property
    def cycle_reason(self) -> str:
        """Why ``power_cycle`` cannot work ("" when it can; a meter-only INA260 cannot)."""
        ...

    def power_cycle(self, off_s: float = 5.0, *, wait: bool = True,
                    progress: Progress | None = None) -> dict:
        """A COLD power cycle through the outlet, timed by the device. Returns the evidence."""
        ...


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
    power: PowerAdapter | None = None

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

    def candidate_for_host(self, spec: str, via: str = "") -> Candidate:
        """Build a candidate from an explicit address. Packs that can, override this.

        ``via`` ("ssh:HOST") asks for the board to be reached through an SSH tunnel
        on HOST; a pack that cannot do that raises ``UsageError`` for a non-empty one.
        """
        from .errors import UsageError

        raise UsageError(f"pack {self.name!r} cannot open a board by address")
