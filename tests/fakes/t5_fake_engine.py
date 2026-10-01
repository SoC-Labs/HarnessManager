"""Team T5: a scripted fake of the frozen ``Engine`` protocol and its services.

It implements ``harness_manager.core.services`` (``Engine``, ``DeployService``,
``ConsoleBroker``, ``DebugService``, ``TelemetryService``) and a board session
with every adapter the CLI touches (``resets``, ``clocks``, ``controller``,
``storage``) plus an MPS3-style ``shell`` handle for the lab verbs. Nothing here
talks to a network.

Scripting:

- ``fake.raises["deploy.deploy"] = SomeHarnessError(...)`` makes that call raise;
  every method checks its own key (``"<area>.<method>"``).
- adapters can be removed (``fake.adapters["controller"] = None``) to prove the
  unavailable path, and ``fake.unavailable`` is what ``info`` reports for them.
- ``fake.calls`` records every call as ``"<area>.<method>"`` so a test can assert
  which protocol methods a verb used (and which it did NOT use).
"""

from __future__ import annotations

import threading
import time
import zipfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from pyverify.client import (
    DisplayResponse,
    DisplaySettleResponse,
    DutRxResponse,
    LinkResponse,
    MacGenResponse,
)

from harness_manager.core.errors import (
    AlreadyError,
    HarnessError,
    NothingOnTargetError,
    RefusedError,
    UsageError,
)
from harness_manager.core.events import Event, EventBus
from harness_manager.core.model import (
    BoardIdentity,
    BoardInfo,
    Candidate,
    Check,
    Health,
    Link,
    LinkKind,
    Reading,
)
from harness_manager.core.pack import (
    CARD_NO_STORE,
    BackupRecord,
    BoardSession,
    CardOutcome,
    CardStatus,
    DeployResult,
    OverlayRef,
    PreflightItem,
    ProbeHints,
    Progress,
)
from harness_manager.core.services import DebugStatus
from harness_manager.core.session import LockOwner

SHELL_ID = "0x3f1a560f"
DANGEROUS = {"FORMAT", "DEL", "EEPROM", "USB_OFF", "SHUTDOWN", "REN", "COPY", "CAP", "FILL"}


def _now() -> float:
    return time.time()


@dataclass
class FakeState:
    """Everything the fake does, in one place a test can edit."""

    calls: list[str] = field(default_factory=list)
    raises: dict[str, HarnessError] = field(default_factory=dict)
    candidates: list[Candidate] = field(default_factory=list)
    identity: BoardIdentity = field(default_factory=lambda: BoardIdentity(
        board_type="mps3", shell_id=SHELL_ID, rm_id="0x00000000", rm_name="greybox",
        harness_version="1.0.0", firmware_sha="cb31b0f2",
        features=("clcd", "clcd_kvm", "touch", "hwicap_fifo", "windowed")))
    health: Health = field(default_factory=lambda: Health(reachable=True, control_channel="idle"))
    capabilities: frozenset[str] = frozenset({"identify", "health", "deploy_partial",
                                              "console_dut", "debug_dut", "reset_dut"})
    unavailable: dict[str, str] = field(default_factory=lambda: {
        "reboot_board": "needs the Debug USB cable",
        "console_controller": "needs the Debug USB cable",
        "clock_board": "needs the Debug USB cable",
        "storage_backup": "needs the Debug USB cable (or a card reader)",
        "storage_install": "needs the Debug USB cable (or a card reader)",
    })
    adapters: dict[str, Any] = field(default_factory=dict)
    # deploy
    overlays: list[OverlayRef] = field(default_factory=lambda: [
        OverlayRef("greybox", "0x00000000", SHELL_ID, size_bytes=1024, ip_class="open"),
        OverlayRef("nanosoc", "0x01000001", SHELL_ID, size_bytes=4096, ip_class="arm-aaa"),
        OverlayRef("led_old", "0x0100001e", "0xdeadbeef", size_bytes=2048, ip_class="open"),
    ])
    preflight: dict[str, list[PreflightItem]] = field(default_factory=dict)
    deploy_verified: bool = True
    # Keep on the card (L1): what card_status answers, what a kept deploy did, and the
    # keep_on_card each deploy was called with (False = the keyword was not passed).
    card: CardStatus = field(default_factory=lambda: CardStatus(store=False,
                                                                reason=CARD_NO_STORE))
    card_outcome: CardOutcome = field(default_factory=lambda: CardOutcome(kept=True, slot="A"))
    deploy_keeps: list[bool] = field(default_factory=list)
    #: FIX-PACK-7: ``force`` per deploy / restore (``program|restore --force``)
    deploy_forces: list[bool] = field(default_factory=list)
    restore_forces: list[bool] = field(default_factory=list)
    #: FIX-PACK-7: a ``deploy.warning`` the next deploy publishes before it starts
    deploy_warning: str = ""
    # consoles
    console_names: list[str] = field(default_factory=lambda: ["uart0", "uart1", "swo"])
    console_chunks: list[bytes] = field(default_factory=lambda: [
        b"Hello world\r\n", b"TEST PASSED\r\n"])
    exported: list[tuple[str, int]] = field(default_factory=list)
    # debug
    debug_state: str = "down"
    idcode: str = "0x6ba00477"
    # telemetry
    readings: list[Reading] = field(default_factory=lambda: [
        Reading("mcc_temp", 35.5, "degC", "mcc-console"),
        Reading.unavailable("board_power", "W", "the MPS3 has no power sensor"),
    ])
    # held lock
    lock_owners: dict[str, LockOwner] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)

    def hit(self, key: str) -> None:
        self.calls.append(key)
        exc = self.raises.get(key)
        if exc is not None:
            raise exc


# --- session adapters -------------------------------------------------------------------


class FakeResets:
    def __init__(self, st: FakeState, targets: Sequence[str] = ("dut",)) -> None:
        self.st, self.targets, self.done = st, tuple(targets), []

    def reset_targets(self) -> Sequence[str]:
        self.st.hit("resets.reset_targets")
        return self.targets

    def reset(self, target: str) -> None:
        self.st.hit("resets.reset")
        if target not in self.targets:
            raise UsageError(f"reset target {target!r} is not supported")
        self.done.append(target)


class FakeClocks:
    def __init__(self, st: FakeState) -> None:
        self.st, self.mhz = st, 50.0

    def clocks(self) -> Sequence[Reading]:
        self.st.hit("clocks.clocks")
        return [Reading("dut", self.mhz, "MHz", "shell")]

    def set_clock(self, name: str, mhz: float) -> Reading:
        self.st.hit("clocks.set_clock")
        self.mhz = mhz
        return Reading(name, mhz, "MHz", "shell")


class FakeController:
    def __init__(self, st: FakeState) -> None:
        self.st, self.lines = st, []

    def command(self, line: str) -> str:
        self.st.hit("controller.command")
        if line.split()[0].upper() in DANGEROUS:
            raise RefusedError(f"MCC command {line.split()[0]!r} is denied",
                               hint="it can erase or brick the board")
        self.lines.append(line)
        return f"MB Device 0 Temp: 35.5 degC\r\n(reply to {line})"

    def reboot(self, progress: Progress | None = None, wait_s: float = 120.0) -> None:
        self.st.hit("controller.reboot")
        for phase in ("sent", "down", "up"):
            if progress:
                progress(phase, 0, 0)

    def temperatures(self) -> Sequence[Reading]:
        self.st.hit("controller.temperatures")
        return [Reading("mcc_temp", 35.5, "degC", "mcc-console")]

    def oscillators(self) -> Sequence[Reading]:
        self.st.hit("controller.oscillators")
        return [Reading("osc0", 25.0, "MHz", "mcc-console"),
                Reading("osc1", 50.0, "MHz", "mcc-console")]


class FakeStorage:
    def __init__(self, st: FakeState) -> None:
        self.st = st
        self.installed: dict[str, Path] = {}
        self.restored: list[BackupRecord] = []

    def locate(self) -> str:
        self.st.hit("storage.locate")
        return "/media/V2M-MPS3"

    def backup(self, dest_dir: Path, progress: Progress | None = None) -> BackupRecord:
        self.st.hit("storage.backup")
        path = Path(dest_dir) / "sd-backup.zip"
        with zipfile.ZipFile(path, "w") as zf:
            zf.writestr("config.txt", "TITLE: V2M-MPS3 config\n")
            zf.writestr("MB/HBI0309C/board.txt", "APPFILE: x\n")
        if progress:
            progress("copy", 2, 2)
        return BackupRecord(str(path), "ab" * 32, _now(), 2, "V2M-MPS3")

    def install(self, files: Mapping[str, Path], *, backup: BackupRecord,
                progress: Progress | None = None, **kw: Any) -> None:
        self.st.hit("storage.install")
        #: FIX-PACK-7: ``allow_mcc_update`` (only when asked), and the notes it says
        self.install_kw = dict(kw)
        self.installed = dict(files)
        self.install_notes = list(getattr(self, "notes_to_say", []))
        if progress:
            progress("write", len(files), len(files))

    def restore(self, backup: BackupRecord, progress: Progress | None = None) -> None:
        self.st.hit("storage.restore")
        self.restored.append(backup)


class FakeShellClient:
    """The subset of pyverify ``ShellClient`` the lab verbs use, scripted."""

    def __init__(self, st: FakeState, shell: FakeShellHandle) -> None:
        self.st, self.sh = st, shell

    def link(self, event: str) -> LinkResponse:
        self.st.hit("shell.link")
        return LinkResponse(ok=event in ("up", "down", "pulse") and not self.sh.refuse)

    def display_owner(self) -> DisplayResponse:
        self.st.hit("shell.display_owner")
        if not self.sh.kvm:
            return DisplayResponse(ok=False, err="clcd_kvm not present")
        return DisplayResponse(ok=True, owner=self.sh.owner)

    def display_settled(self, owner: str, *, timeout: float = 2.0) -> DisplaySettleResponse:
        self.st.hit("shell.display_settled")
        if not self.sh.kvm:
            return DisplaySettleResponse(ok=False, requested=owner, err="clcd_kvm not present")
        target = owner if owner != "toggle" else ("dut" if self.sh.owner == "harness"
                                                   else "harness")
        if self.sh.lands:
            self.sh.owner = target
        return DisplaySettleResponse(ok=True, requested=target, owner=self.sh.owner,
                                     landed=self.sh.owner == target, polls=1, waited_s=0.01)

    def macgen(self, *, gen: bool = True, chk: bool = True, inject: str = "none",
               injects: Any = None) -> MacGenResponse:
        self.st.hit("shell.macgen")
        if self.sh.refuse:
            return MacGenResponse(ok=False)
        return MacGenResponse(ok=True, tx=8 if gen else 0, rx=8 if chk else 0,
                              err=1 if inject != "none" else 0)

    def read_dut_frame(self) -> tuple[bytes | None, DutRxResponse]:
        self.st.hit("shell.read_dut_frame")
        if not self.sh.egress:
            return None, DutRxResponse(ok=False, err="dut_egress not present")
        if not self.sh.frames:
            return None, DutRxResponse(ok=True, rx=self.sh.rx)
        frame = self.sh.frames.pop(0)
        return frame, DutRxResponse(ok=True, frame_len=len(frame), n=len(frame), last=True,
                                    frames=len(self.sh.frames), rx=self.sh.rx, data=frame)


class FakeShellHandle:
    """Stands in for ``Mps3Shell``: ``call(fn)`` runs ``fn`` against a fake client."""

    def __init__(self, st: FakeState) -> None:
        self.st = st
        self.kvm = True
        self.egress = True
        self.lands = True
        self.refuse = False
        self.owner = "harness"
        self.frames: list[bytes] = []
        self.rx = 0

    def call(self, fn: Any) -> Any:
        self.st.hit("shell.call")
        return fn(FakeShellClient(self.st, self))


class FakeSession(BoardSession):
    def __init__(self, candidate: Candidate, st: FakeState) -> None:
        self.candidate = candidate
        self.st = st
        for name in ("resets", "clocks", "controller", "storage", "shell"):
            setattr(self, name, st.adapters.get(name))

    def identity(self) -> BoardIdentity:
        self.st.hit("session.identity")
        return self.st.identity

    def health(self) -> Health:
        self.st.hit("session.health")
        return self.st.health


# --- services ---------------------------------------------------------------------------


class FakeDeploy:
    def __init__(self, st: FakeState, bus: EventBus) -> None:
        self.st, self.bus = st, bus

    def overlays(self, session: BoardSession) -> Sequence[OverlayRef]:
        self.st.hit("deploy.overlays")
        return list(self.st.overlays)

    def compatible(self, session: BoardSession) -> tuple[list[OverlayRef], dict[str, str]]:
        self.st.hit("deploy.compatible")
        ok = [o for o in self.st.overlays if o.static_id == self.st.identity.shell_id]
        bad = {o.name: f"built for shell {o.static_id}, this board runs "
                       f"{self.st.identity.shell_id}"
               for o in self.st.overlays if o.static_id != self.st.identity.shell_id}
        return ok, bad

    def preflight(self, session: BoardSession, overlay: OverlayRef) -> Sequence[PreflightItem]:
        self.st.hit("deploy.preflight")
        if overlay.name in self.st.preflight:
            return self.st.preflight[overlay.name]
        match = Check.OK if overlay.static_id == self.st.identity.shell_id else Check.MISMATCH
        return [PreflightItem("session held", Check.OK),
                PreflightItem("shell_id matches", match, f"{overlay.static_id}"),
                PreflightItem("crc", Check.OK)]

    def card_status(self, session: BoardSession) -> CardStatus:
        self.st.hit("deploy.card_status")
        return self.st.card

    def deploy(self, session: BoardSession, overlay: OverlayRef, **kw: Any) -> DeployResult:
        self.st.hit("deploy.deploy")
        keep = bool(kw.pop("keep_on_card", False))
        self.st.deploy_forces.append(bool(kw.pop("force", False)))
        assert not kw, kw
        self.st.deploy_keeps.append(keep)
        bid = session.candidate.board_id
        if self.st.deploy_warning:
            self.bus.publish(Event("deploy.warning", bid, {"overlay": overlay.name,
                                                           "message": self.st.deploy_warning}))
        self.bus.publish(Event("deploy.started", bid, {"rm": overlay.name}))
        for done in (0, overlay.size_bytes // 2, overlay.size_bytes):
            self.bus.publish(Event("deploy.progress", bid, {
                "phase": "push", "bytes": done, "total": overlay.size_bytes}))
        self.bus.publish(Event("deploy.done", bid, {"rm_id": overlay.rm_id,
                                                    "verified": self.st.deploy_verified}))
        self.st.identity = replace(self.st.identity, rm_id=overlay.rm_id, rm_name=overlay.name)
        return DeployResult(overlay.rm_id, self.st.deploy_verified, 3.25, "tcp+windowed",
                            card=self.st.card_outcome if keep else None)

    def restore_baseline(self, session: BoardSession, **kw: Any) -> DeployResult:
        self.st.hit("deploy.restore_baseline")
        self.st.restore_forces.append(bool(kw.pop("force", False)))
        assert not kw, kw
        self.st.identity = replace(self.st.identity, rm_id="0x00000000", rm_name="greybox")
        return DeployResult("0x00000000", True, 2.5, "tcp+windowed")


class FakeStream:
    def __init__(self, name: str, chunks: list[bytes]) -> None:
        self.name = name
        self._chunks = list(chunks)
        self._idle = threading.Event()
        self.closed = False

    def read(self, timeout: float | None = None) -> bytes:
        if self._chunks:
            return self._chunks.pop(0)
        self._idle.wait(timeout if timeout is not None else 0.05)   # honour the timeout
        return b""

    def write(self, data: bytes) -> None:
        pass

    def close(self) -> None:
        self.closed = True


class FakeConsoles:
    def __init__(self, st: FakeState) -> None:
        self.st = st
        self.streams: list[FakeStream] = []

    def names(self, session: BoardSession) -> list[str]:
        self.st.hit("consoles.names")
        return list(self.st.console_names)

    def subscribe(self, session: BoardSession, name: str) -> FakeStream:
        self.st.hit("consoles.subscribe")
        stream = FakeStream(name, self.st.console_chunks)
        self.streams.append(stream)
        return stream

    def export_tcp(self, session: BoardSession, name: str, port: int = 0) -> int:
        self.st.hit("consoles.export_tcp")
        port = port or 41234
        self.st.exported.append((name, port))
        return port

    def close_all(self, board_id: str) -> None:
        self.st.hit("consoles.close_all")


class FakeDebug:
    def __init__(self, st: FakeState) -> None:
        self.st = st

    def _status(self) -> DebugStatus:
        if self.st.debug_state == "up":
            return DebugStatus("up", 29555, 31155, 31955, ("nanosoc_mps3_jtag.cfg",), 4242)
        return DebugStatus(self.st.debug_state)

    def detect(self, session: BoardSession) -> str:
        self.st.hit("debug.detect")
        if not self.st.idcode:
            raise NothingOnTargetError("the loaded design (greybox) has no debug port",
                                       hint="load nanosoc first")
        return self.st.idcode

    def up(self, session: BoardSession) -> DebugStatus:
        self.st.hit("debug.up")
        if self.st.debug_state == "up":
            raise AlreadyError("the debug server is already up")
        if self.st.debug_state != "failed":
            self.st.debug_state = "up"
        return self._status()

    def down(self, session: BoardSession) -> DebugStatus:
        self.st.hit("debug.down")
        self.st.debug_state = "down"
        return self._status()

    def status(self, session: BoardSession) -> DebugStatus:
        self.st.hit("debug.status")
        return self._status()


class FakeTelemetry:
    def __init__(self, st: FakeState) -> None:
        self.st = st

    def readings(self, session: BoardSession) -> list[Reading]:
        self.st.hit("telemetry.readings")
        return list(self.st.readings)


class FakeStore:
    root = Path(".")


# --- engine -------------------------------------------------------------------------------


class FakeEngine:
    """Implements ``harness_manager.core.services.Engine`` with scripted behaviour."""

    def __init__(self, state: FakeState | None = None) -> None:
        self.st = state or FakeState()
        if not self.st.adapters:
            self.st.adapters = {
                "resets": FakeResets(self.st), "clocks": FakeClocks(self.st),
                "controller": FakeController(self.st), "storage": FakeStorage(self.st),
                "shell": FakeShellHandle(self.st),
            }
        self.bus = EventBus()
        self.store = FakeStore()
        self.deploy = FakeDeploy(self.st, self.bus)
        self.consoles = FakeConsoles(self.st)
        self.debug = FakeDebug(self.st)
        self.telemetry = FakeTelemetry(self.st)
        self.sessions: dict[str, FakeSession] = {}
        self.closed_all = False

    # convenience for tests
    @property
    def calls(self) -> list[str]:
        return self.st.calls

    def packs(self) -> dict[str, Any]:
        self.st.hit("engine.packs")
        return {"mps3": SimpleNamespace(title="fake MPS3", capability_specs=lambda: ())}

    def probe(self, hints: ProbeHints | None = None) -> list[Candidate]:
        self.st.hit("engine.probe")
        return list(self.st.candidates)

    def candidate_for(self, target: str, pack: str = "mps3") -> Candidate:
        self.st.hit("engine.candidate_for")
        if ":" in target and not target.rsplit(":", 1)[1].isdigit():
            raise ValueError(f"invalid literal for int(): {target.rsplit(':', 1)[1]!r}")
        return Candidate(pack=pack, board_id=f"{pack}@{target}",
                         links=(Link(LinkKind.ETHERNET, target, "shell control channel"),),
                         label=f"fake {pack} at {target}", evidence="given explicitly")

    def open(self, candidate: Candidate, *, note: str = "") -> BoardSession:
        self.st.hit("engine.open")
        self.st.notes.append(note)
        session = FakeSession(candidate, self.st)
        self.sessions[candidate.board_id] = session
        return session

    def session(self, board_id: str) -> BoardSession:
        self.st.hit("engine.session")
        return self.sessions[board_id]

    def info(self, board_id: str) -> BoardInfo:
        self.st.hit("engine.info")
        s = self.sessions[board_id]
        return BoardInfo(s.candidate, s.identity(), s.health(), self.st.capabilities,
                         dict(self.st.unavailable))

    def close(self, board_id: str) -> None:
        self.st.hit("engine.close")
        self.sessions.pop(board_id, None)

    def close_all(self) -> None:
        self.st.hit("engine.close_all")
        self.closed_all = True
        self.sessions.clear()

    def lock_owner(self, board_id: str) -> LockOwner | None:
        self.st.hit("engine.lock_owner")
        return self.st.lock_owners.get(board_id)

    def open_boards(self) -> list[str]:
        return list(self.sessions)


def make_engine(_args: Any = None) -> FakeEngine:
    """A factory for ``$HARNESS_MANAGER_CLI_ENGINE=tests.fakes.t5_fake_engine:make_engine``."""
    return FakeEngine()
