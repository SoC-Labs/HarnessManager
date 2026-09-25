"""The board-agnostic service interfaces (frozen for Wave 1).

The owners implement them in ``harness_manager.services`` and ``harness_manager.engine``:

| Protocol          | Owner | Implementation                      |
|-------------------|-------|-------------------------------------|
| ``Engine``        | T1    | ``harness_manager.engine.Engine``        |
| ``ContentStore``  | T1    | ``harness_manager.services.store``       |
| ``TelemetryService`` | T1 | ``harness_manager.services.telemetry``   |
| ``DeployService`` | T2    | ``harness_manager.services.deploy``      |
| ``ConsoleBroker`` | T4    | ``harness_manager.services.console``     |
| ``DebugService``  | T4    | ``harness_manager.services.debug``       |

Board-controller and storage operations are ADAPTERS on the session
(``session.controller`` / ``session.storage``, MPS3: Team T3). The engine
exposes them through ``Engine.session(board_id)``.

The front-ends (CLI T5, GUI T6) depend on these protocols ONLY. Until the
implementations merge, they test against their own fakes of these protocols.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol, runtime_checkable

from .events import EventBus
from .model import BoardInfo, Candidate, Reading
from .pack import BoardPack, BoardSession, DeployResult, OverlayRef, PreflightItem, ProbeHints
from .session import LockOwner

# --- content store -----------------------------------------------------------------


@runtime_checkable
class ContentStore(Protocol):
    root: Path

    def put_bytes(self, data: bytes, *, kind: str, meta: dict[str, str]) -> str:
        """Store ``data``; return its sha256. Idempotent for identical content."""
        ...

    def put_file(self, path: Path, *, kind: str, meta: dict[str, str]) -> str: ...
    def path(self, sha256: str) -> Path: ...
    def verify(self, sha256: str) -> bool:
        """Re-hash the stored blob. False means corrupted."""
        ...

    def find(self, kind: str, **meta: str) -> list[tuple[str, dict[str, str]]]: ...


# --- deploy --------------------------------------------------------------------------


@runtime_checkable
class DeployService(Protocol):
    def overlays(self, session: BoardSession) -> Sequence[OverlayRef]: ...

    def compatible(self, session: BoardSession) -> tuple[list[OverlayRef], dict[str, str]]:
        """(loadable overlays, {overlay name: why it cannot load})."""
        ...

    def preflight(self, session: BoardSession, overlay: OverlayRef) -> Sequence[PreflightItem]: ...

    def deploy(self, session: BoardSession, overlay: OverlayRef) -> DeployResult:
        """Run the preflight, refuse on any MISMATCH, deploy, re-read identity, confirm.

        Emits ``deploy.started|progress|done|failed`` on the engine bus. The real service
        also takes ``keep_on_card=True`` (keep the design on the board's card; off by
        default) and answers ``card_status(session) -> CardStatus``; callers pass the
        keyword only when asked, so a service without it is unchanged.
        """
        ...

    def restore_baseline(self, session: BoardSession) -> DeployResult: ...


# --- consoles ----------------------------------------------------------------------


@runtime_checkable
class ConsoleStream(Protocol):
    name: str

    def read(self, timeout: float | None = None) -> bytes:
        """Next chunk (b"" on timeout). Never blocks forever when a timeout is given."""
        ...

    def write(self, data: bytes) -> None: ...
    def close(self) -> None: ...


@runtime_checkable
class ConsoleBroker(Protocol):
    def names(self, session: BoardSession) -> list[str]: ...
    def subscribe(self, session: BoardSession, name: str) -> ConsoleStream: ...
    def export_tcp(self, session: BoardSession, name: str, port: int = 0) -> int:
        """Re-export a console on 127.0.0.1:<port> for an external terminal; returns the port."""
        ...

    def close_all(self, board_id: str) -> None: ...


# --- debug ----------------------------------------------------------------------------


@dataclass(frozen=True)
class DebugStatus:
    state: str                    # "down" | "starting" | "up" | "failed"
    gdb_port: int = 0
    telnet_port: int = 0
    tcl_port: int = 0
    config: tuple[str, ...] = ()
    pid: int = 0
    detail: str = ""


@runtime_checkable
class DebugService(Protocol):
    def detect(self, session: BoardSession) -> str:
        """Non-intrusive: the TAP IDCODE ("0x6ba00477"), or ``NothingOnTargetError``."""
        ...

    def up(self, session: BoardSession) -> DebugStatus: ...
    def down(self, session: BoardSession) -> DebugStatus: ...
    def status(self, session: BoardSession) -> DebugStatus: ...


# --- telemetry -------------------------------------------------------------------


@runtime_checkable
class TelemetryService(Protocol):
    def readings(self, session: BoardSession) -> list[Reading]:
        """Every reading from every source, with provenance; stale values are flagged."""
        ...


# --- engine ----------------------------------------------------------------------


@dataclass(frozen=True)
class EngineConfig:
    state_dir: Path | None = None          # default: $HARNESS_MANAGER_STATE_DIR or ~/.config/harness-manager
    pack_overrides: dict[str, dict] = field(default_factory=dict)   # per-pack constructor kwargs


@runtime_checkable
class Engine(Protocol):
    bus: EventBus
    store: ContentStore
    deploy: DeployService
    consoles: ConsoleBroker
    debug: DebugService
    telemetry: TelemetryService

    def packs(self) -> dict[str, BoardPack]: ...
    def probe(self, hints: ProbeHints | None = None) -> list[Candidate]: ...
    def candidate_for(self, target: str, pack: str = "mps3", via: str = "") -> Candidate: ...
    def open(self, candidate: Candidate, *, note: str = "") -> BoardSession:
        """Take the board's SessionLock and open a session (``HeldError`` if held)."""
        ...

    def session(self, board_id: str) -> BoardSession: ...
    def info(self, board_id: str) -> BoardInfo: ...
    def close(self, board_id: str) -> None: ...
    def close_all(self) -> None: ...
    def lock_owner(self, board_id: str) -> LockOwner | None:
        """Who holds the board right now (any process), without opening it."""
        ...

    def open_boards(self) -> list[str]: ...
