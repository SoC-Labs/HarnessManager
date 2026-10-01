"""A scripted, in-process engine for demos and UI tests (``harness-manager ui --demo``).

``DemoEngine`` implements the frozen ``harness_manager.core.services.Engine``
protocol with no hardware and no sockets except one optional 127.0.0.1
listener per console export. It stands in for Team T1's ``Engine`` until that
merged, and keeps working as the web UI's demo mode (moved out of the retired
Qt GUI package at the T14 merge).

Three scripted boards, each modelled on a real lab situation:

- ``BOARD_FIELDED``: an Ethernet-only MPS3 on the fielded shell 0x3F1A560F
  (harness 1.0.0, greybox loaded, build check "unchecked" because USR_ACCESS
  is unreadable on that mint). Everything that needs the Debug USB is
  unavailable, with the reason the MPS3 pack gives.
- ``BOARD_USB``: the same shell with the Debug USB attached (MCC console,
  config SD), nanosoc loaded, build check OK.
- ``BOARD_HELD``: a board somebody else's session holds.

It follows the real engine's (T1, ``harness_manager.engine``) semantics where the
GUI depends on them: ``info()`` answers only for a board open in this engine,
``open()`` of an open board is ``AlreadyError``, ``lock_owner()`` names the
holder without opening. Its candidates carry the identity their probe read
(``DemoCandidate``): that previews contract change request T6-1.

It has no GUI code and no Qt import, so it can be used from any test.

Test and demo knobs (not part of the Engine protocol):

- ``delays["debug.detect"] = 2.0``: that call sleeps first (a slow engine);
- ``failures["deploy.push"] = ActionFailedError(...)``: raise at that point;
- ``calls``: every protocol call made, in order, as ``(name, args)``;
- ``set_links``/``set_features``/``set_build_check``/``set_owner``: change a
  board and publish ``board.identity``, the way a real engine reports a
  changed capability view;
- ``inject_console(board_id, name, text)``: bytes arrive on a console;
- ``set_sd_journal(board_id, journal)``: an interrupted SD install (T3's journal);
- ``failures["controller.reboot.confirm"]``: REBOOT sent, no restart observed;
- ``set_card(board_id, state)``: a card in the board's user microSD slot, its store in
  ``state`` ("empty", "valid", "foreign"...), or None to take it out. "Keep on the card"
  also needs the harness to report ``usd`` (``set_features``). No classic board has either
  until a test sets them.
- ``set_debug_down(board_id, why, lock=False)`` (FIX-PACK-7, DEBUG-DOWN-FIRST): the board's
  ``mps3-debug down`` fails with ``why`` ("" answers down), so a deploy is refused (15) unless
  forced, or ``lock`` (the launcher lists harnessd's lock) lets it go on with a warning. A
  Linux board (the showcase's) is asked before every deploy; another board only once a test
  set this. The words are the real service's (``services.debug_onboard.DownFirst``).

**The showcase** (``DemoEngine(showcase=True, state_dir=...)``, what ``harness-manager app
--demo`` and ``ui --demo`` serve): four other boards, one of each harness and a spare, so
every part of the app has something to show (``harness_manager.demo_showcase``): a Linux harness
(``BOARD_LINUX``: the card, OS slots, the SSH claim, the front panel read from the glass,
Identify, XVC), today's bare-metal v0.11 board (``BOARD_V011``: the rebuilt panel, Identify
unavailable, XVC with its warning, every harness-catalogue verdict, the Debug USB) and a board
behind a hub whose lease alice holds (``BOARD_LEASED``: the queue, a request, force-release),
plus a free board on the same hub (``BOARD_SPARE``, LEASE-UI: free, then yours).
The showcase engine also has what the classic one leaves to the real engine: ``xvc``,
``board_claim``, ``update`` (a signed catalogue in the demo's state dir,
``harness_manager.demo_catalog``) and ``kit_channel`` (the DUT build kits from that
catalogue), all offline. ``HARNESS_MANAGER_DEMO_UPDATE=staged`` (or ``app_update="staged"``)
stages a pretend app update so the banner shows; it is off by default. The classic three
boards stay the default of ``DemoEngine()`` (the tests' fixture).
"""

from __future__ import annotations

import getpass
import os
import queue
import socket
import threading
import time
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from harness_manager.core.capabilities import CapabilitySpec, negotiate
from harness_manager.core.errors import (
    AbsentError,
    AlreadyError,
    HarnessError,
    HeldError,
    IncompatibleError,
    NothingOnTargetError,
    RefusedError,
    UnreachableError,
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
    CARD_NO_CARD,
    CARD_NO_STORE,
    BackupRecord,
    BoardPack,
    BoardSession,
    CardOutcome,
    CardStatus,
    DeployResult,
    OverlayRef,
    PreflightItem,
    ProbeHints,
    Progress,
    card_status_of,
    keep_refusal,
)
from harness_manager.core.services import DebugStatus
from harness_manager.core.session import LockOwner

SHELL_FIELDED = "0x3f1a560f"
SHELL_OLD = "0xcd74b6ae"
CONTROL_PORT = 6900

BOARD_FIELDED = "mps3@192.168.10.101:6900"
BOARD_USB = "mps3@192.168.10.102:6900"
BOARD_HELD = "mps3@192.168.10.103:6900"

FIELDED_FEATURES = ("clcd", "clcd_kvm", "touch", "hwicap_fifo", "windowed")

# rm_id design numbers (rm_id & 0xFFFF), from harness_manager_mps3.constants.
DESIGNS = {0x0000: "greybox", 0x0001: "nanosoc", 0x0003: "nanosoc_multicore",
           0x0005: "nanosoc_upy", 0x0008: "nanosoc_iice", 0x001E: "led"}
DAP_DESIGNS = {0x0001: ("nanosoc_mps3_jtag.cfg", "nanosoc_ops.tcl"),
               0x0005: ("nanosoc_mps3_jtag.cfg", "nanosoc_ops.tcl"),
               0x0008: ("nanosoc_iice_chain.cfg", "nanosoc_ops.tcl")}
DAP_IDCODE = "0x6ba00477"
#: DEBUG-ONBOARD: the Linux harness runs OpenOCD itself (``mps3-debug``), with a config for
#: the two-core design too (one gdb port per core); the demo's Linux board shows that path.
ONBOARD_DAP_DESIGNS = {**DAP_DESIGNS,
                       0x0003: ("interface/mps3_jtagbb.cfg", "target/nanosoc_multicore.cfg")}
ONBOARD_CORES = {0x0003: ("cpu0", "cpu1")}


def _design(rm_id: str) -> int:
    return int(rm_id, 16) & 0xFFFF


def _specs() -> tuple[CapabilitySpec, ...]:
    # The real MPS3 capability table, so the demo's reasons are the product's reasons.
    from harness_manager_mps3.capabilities import SPECS

    return SPECS


# --- scripted boards -------------------------------------------------------------------


@dataclass(frozen=True)
class DemoCandidate(Candidate):
    """A candidate that carries the identity its probe read (contract change request T6-1)."""

    identity: BoardIdentity | None = None


@dataclass
class _Board:
    candidate: Candidate
    identity: BoardIdentity
    health: Health
    owner: LockOwner | None = None
    reachable: bool = True
    consoles: tuple[str, ...] = ("uart0", "uart1", "swo")
    readings: list[Reading] = field(default_factory=list)
    debug: DebugStatus = field(default_factory=lambda: DebugStatus(state="down"))
    port_base: int = 3333
    sd_journal: dict | None = None
    card: str | None = None          # the user microSD's store state; None = no card
    card_slot: str = "B"             # the slot the last kept design went to
    overlay_shell: str = SHELL_FIELDED   # the static the demo's overlays are keyed to
    kind: str = ""       # showcase: "linux" | "bare-metal" | "leased" | "spare" ("": classic)
    #: FIX-PACK-7: the board's ``mps3-debug down`` fails with this ("": it answers down);
    #: None: the board has no on-board route (a classic board until a test sets it)
    debug_down: str | None = None
    debug_lock: bool = False         # the launcher lists harnessd's lock (a failed down warns)


def _eth(host: str) -> Link:
    return Link(LinkKind.ETHERNET, f"{host}:{CONTROL_PORT}", "shell control channel")


USB_SERIAL_LINK = Link(LinkKind.USB_SERIAL, "/dev/ttyUSB10", "FT4232H if00 (MCC)")
USB_MSD_LINK = Link(LinkKind.USB_MSD, "/media/V2M-MPS3", "V2M-MPS3 volume")


def _counters(swaps: int) -> dict[str, int]:
    return {"rx_frames": 18342, "tx_frames": 18011, "swaps": swaps, "crc_errors": 0,
            "svc_skipped": 0, "push_windows": 412}


def _script() -> dict[str, _Board]:
    fielded = _Board(
        candidate=Candidate("mps3", BOARD_FIELDED, (_eth("192.168.10.101"),),
                            label="MPS3 greybox on shell 0x3f1a560f", evidence="answered ping",
                            name="mps3-01", name_source="hub"),     # the lab board (N1)
        identity=BoardIdentity(board_type="mps3", shell_id=SHELL_FIELDED, rm_id="0x00000000",
                               rm_name="greybox", harness_version="1.0.0",
                               firmware_sha="cb31b0f2", features=FIELDED_FEATURES,
                               build_check=Check.UNCHECKED),
        health=Health(reachable=True, control_channel="idle", counters=_counters(3)),
        readings=[
            Reading("dut_clk", 50.0, "MHz", source="shell-6900", reason="preset"),
            Reading.unavailable("mcc_temp", "degC", "needs the Debug USB cable",
                                source="mcc-console"),
            Reading.unavailable("fpga_die_temp", "degC",
                                "needs a JTAG cable on J17 (SYSMON over xsdb)",
                                source="sysmon-jtag"),
            Reading.unavailable("board_power", "W", "the MPS3 has no power sensor; add a "
                                "metered plug or an INA260"),
        ],
        port_base=3333,
    )
    usb = _Board(
        candidate=Candidate("mps3", BOARD_USB,
                            (_eth("192.168.10.102"), USB_SERIAL_LINK, USB_MSD_LINK),
                            label="MPS3 nanosoc on shell 0x3f1a560f",
                            evidence="answered ping; FT4232H 0403:6011"),
        identity=BoardIdentity(board_type="mps3", shell_id=SHELL_FIELDED, rm_id="0x01000001",
                               rm_name="nanosoc", harness_version="1.0.0",
                               firmware_sha="cb31b0f2", features=FIELDED_FEATURES,
                               build_check=Check.OK),
        health=Health(reachable=True, control_channel="idle", counters=_counters(11)),
        consoles=("uart0", "uart1", "swo", "mcc", "shell"),
        readings=[
            Reading("dut_clk", 50.0, "MHz", source="shell-6900", reason="preset"),
            Reading("mcc_temp", 38.5, "degC", source="mcc-console"),
            Reading("osc0", 50.0, "MHz", source="mcc-console", reason="CFG R OSC"),
            Reading.unavailable("fpga_die_temp", "degC",
                                "needs a JTAG cable on J17 (SYSMON over xsdb)",
                                source="sysmon-jtag"),
            Reading.unavailable("board_power", "W", "the MPS3 has no power sensor; add a "
                                "metered plug or an INA260"),
        ],
        port_base=3343,
    )
    held = _Board(
        candidate=Candidate("mps3", BOARD_HELD, (_eth("192.168.10.103"),),
                            label="MPS3 nanosoc_upy on shell 0x3f1a560f",
                            evidence="answered ping"),
        identity=BoardIdentity(board_type="mps3", shell_id=SHELL_FIELDED, rm_id="0x01000005",
                               rm_name="nanosoc_upy", harness_version="1.0.0",
                               firmware_sha="cb31b0f2", features=FIELDED_FEATURES,
                               build_check=Check.MISMATCH),
        health=Health(reachable=True, control_channel="busy", counters=_counters(40),
                      notes=("another session is using the control channel",)),
        owner=LockOwner(user="alice", host="lab-pc-07", pid=31337,
                        since=time.time() - 5400, note="overnight soak"),
        port_base=3353,
    )
    boards = (fielded, usb, held)
    for b in boards:
        b.candidate = DemoCandidate(**{f: getattr(b.candidate, f) for f in (
            "pack", "board_id", "links", "label", "evidence", "name", "name_source")},
            identity=b.identity)
    return {b.candidate.board_id: b for b in boards}


def _overlays(static: str = SHELL_FIELDED) -> list[OverlayRef]:
    """The overlay store: every design keyed to ``static`` but one, keyed to an old static."""
    old = SHELL_OLD if static == SHELL_FIELDED else SHELL_FIELDED

    def ov(name: str, design: int, shell: str = static, size: int = 412_160) -> OverlayRef:
        rm = "0x00000000" if design == 0 else f"0x0100{design:04x}"
        return OverlayRef(name=name, rm_id=rm, static_id=shell, static_usercode="0x5f3a9c11",
                          source=f"fielded/{name}/manifest.json", size_bytes=size,
                          ip_class="open" if name in ("greybox", "led") else "arm-aaa")

    return [ov("greybox", 0x0000, size=86_016), ov("nanosoc", 0x0001), ov("nanosoc_upy", 0x0005),
            ov("nanosoc_iice", 0x0008, size=498_304), ov("led", 0x001E, size=102_400),
            ov("nanosoc_multicore", 0x0003, shell=old, size=640_512)]


# --- the pack ---------------------------------------------------------------------------


class DemoPack(BoardPack):
    """Only ``capability_specs`` is real; the demo engine does the probing itself."""

    name = "mps3"
    title = "Arm MPS3 (V2M-MPS3, HBI0309C) - demo"

    def capability_specs(self) -> Iterable[CapabilitySpec]:
        return _specs()

    def probe(self, hints: ProbeHints) -> list[Candidate]:
        return []

    def open(self, candidate: Candidate) -> BoardSession:
        raise UsageError("the demo pack opens boards through DemoEngine.open")

    def settings(self) -> Iterable[Any]:
        """The MPS3 pack's settings rows with its own defaults, so the demo's Settings dialog
        shows what an MPS3 install shows (lane SET-UI-MERGE): the consoles' pacing, the
        OS-slot card timing, each board's hub, SSH and XVC tables."""
        from harness_manager_mps3.settings import mps3_rows

        return mps3_rows()


# --- sessions and adapters ---------------------------------------------------------------


class _Resets:
    def __init__(self, engine: DemoEngine, board_id: str) -> None:
        self._e, self._bid = engine, board_id

    def reset_targets(self) -> Sequence[str]:
        feats = self._e._board(self._bid).identity.features
        return ("dut", "shell") if "reboot" in feats else ("dut",)

    def reset(self, target: str) -> None:
        self._e._enter("resets.reset", self._bid, target)
        if target not in self.reset_targets():
            raise UsageError(f"reset target {target!r} is not supported",
                             hint="targets: " + ", ".join(self.reset_targets()))
        self._e._sleep(0.2)


class _Controller:
    """The MCC, as far as the demo needs it."""

    DENIED = ("FORMAT", "DEL", "EEPROM", "USB_OFF", "SHUTDOWN", "REN", "COPY", "CAP", "FILL")

    def __init__(self, engine: DemoEngine, board_id: str) -> None:
        self._e, self._bid = engine, board_id

    def command(self, line: str, *, arm: bool = False) -> str:
        self._e._enter("controller.command", self._bid, line)
        if line.split()[:1] and line.split()[0].upper() in self.DENIED:
            raise RefusedError(f"MCC command {line.split()[0]!r} is hard-denied")
        return "Cmd>"

    def reboot(self, progress: Progress | None = None, wait_s: float | None = None) -> dict:
        e = self._e
        e._enter("controller.reboot", self._bid)
        for i, phase in enumerate(("sent", "down", "up"), start=1):
            e._sleep(0.3)
            if progress is not None:
                progress(phase, i, 3)
            e.bus.publish(Event("controller.reboot", self._bid, {"phase": phase}))
            if phase == "sent" and e.failures.get("controller.reboot.confirm") is not None:
                # T3's no-op witness: REBOOT sent, the board never went down.
                raise e.failures["controller.reboot.confirm"]
        # A reboot reloads the SD image: the greybox comes back.
        e._set_identity(self._bid, rm_id="0x00000000", rm_name="greybox")
        # The same evidence shape as the MPS3 ControllerAdapter (CONTRACTS.md).
        return {"summary": "REBOOT witnessed (demo): down after 0.3s, up after 0.9s",
                "down_after_s": 0.3, "up_after_s": 0.9,
                "down_evidence": ["the MCC printed its boot banner"],
                "up_evidence": "the MCC boot banner completed with 'FPGA configuration complete.'",
                "shell_id_before": None, "shell_id_after": None, "fpga_configured": True,
                # MCC-FIX: what the MCC said it loaded (mcc.boot_fields), SD-relative
                "fpga_file": "MB/HBI0309C/Nanosoc/nanosoc.bit",
                "board_file": "MB/HBI0309C/Nanosoc/nanosoc.txt", "mcc_firmware": "v1.3.2",
                "mcc_build_date": "Apr 20 2018", "hbi_build": "567", "bootloader": "v1.0.0"}

    def temperatures(self) -> Sequence[Reading]:
        return [r for r in self._e._board(self._bid).readings if r.unit == "degC"]

    def oscillators(self) -> Sequence[Reading]:
        return [r for r in self._e._board(self._bid).readings if r.name.startswith("osc")]


class _Storage:
    """The config SD, as far as the GUI needs it (T3's StorageAdapter, in memory)."""

    def __init__(self, engine: DemoEngine, board_id: str) -> None:
        self._e, self._bid = engine, board_id

    def locate(self) -> str:
        return USB_MSD_LINK.address

    def pending(self) -> dict | None:
        self._e._enter("storage.pending", self._bid)
        return self._e._board(self._bid).sd_journal

    def load_backup(self, path: Path) -> BackupRecord:
        self._e._enter("storage.load_backup", self._bid, str(path))
        return BackupRecord(path=str(path), sha256="0" * 64, created_at=time.time() - 3600,
                            files=12, volume_label="V2M-MPS3")

    def backup(self, dest_dir: Path, progress: Progress | None = None) -> BackupRecord:
        self._e._enter("storage.backup", self._bid, str(dest_dir))
        return self.load_backup(Path(dest_dir) / "demo-backup.zip")

    def install(self, files, *, backup: BackupRecord, progress: Progress | None = None,
                **_kw: Any) -> None:
        self._e._enter("storage.install", self._bid)
        raise RefusedError("the demo engine does not write SD cards")

    def restore(self, backup: BackupRecord, progress: Progress | None = None) -> None:
        e = self._e
        e._enter("storage.restore", self._bid, backup.path)
        total = 12 * 65536
        for step in range(1, 5):
            e._sleep(0.2)
            if progress is not None:
                progress("restore", total * step // 4, total)
        e._board(self._bid).sd_journal = None
        e.bus.publish(Event("storage.progress", self._bid, {"op": "restore", "bytes": total,
                                                            "total": total}))


class DemoSession(BoardSession):
    def __init__(self, engine: DemoEngine, candidate: Candidate) -> None:
        self._e = engine
        self.candidate = candidate
        self.resets = _Resets(engine, candidate.board_id)
        self.closed = False
        board = engine._board(candidate.board_id)
        if board.kind:          # the showcase: panel, claim, OS slots, card, hub (demo_showcase)
            from harness_manager.demo_showcase import adapters

            for name, adapter in adapters(engine, board).items():
                setattr(self, name, adapter)

    @property  # type: ignore[override]
    def controller(self):  # noqa: D401 - follows the board's live links
        board = self._e._board(self.candidate.board_id)
        kinds = {lk.kind for lk in board.candidate.links}
        return _Controller(self._e, board.candidate.board_id) \
            if kinds & {LinkKind.USB_SERIAL, LinkKind.HUB} else None

    @property  # type: ignore[override]
    def storage(self):  # noqa: D401 - follows the board's live links
        board = self._e._board(self.candidate.board_id)
        kinds = {lk.kind for lk in board.candidate.links}
        return _Storage(self._e, board.candidate.board_id) \
            if kinds & {LinkKind.USB_MSD, LinkKind.HUB} else None

    def identity(self) -> BoardIdentity:
        return self._e._board(self.candidate.board_id).identity

    def health(self) -> Health:
        return self._e._board(self.candidate.board_id).health

    def close(self) -> None:
        self.closed = True


# --- services ------------------------------------------------------------------------


class DemoStore:
    """An in-memory ContentStore: enough for the protocol, nothing on disk."""

    def __init__(self) -> None:
        self.root = Path("demo-store")
        self._blobs: dict[str, tuple[bytes, str, dict[str, str]]] = {}

    def put_bytes(self, data: bytes, *, kind: str, meta: dict[str, str]) -> str:
        import hashlib

        digest = hashlib.sha256(data).hexdigest()
        self._blobs[digest] = (data, kind, dict(meta))
        return digest

    def put_file(self, path: Path, *, kind: str, meta: dict[str, str]) -> str:
        return self.put_bytes(Path(path).read_bytes(), kind=kind, meta=meta)

    def path(self, sha256: str) -> Path:
        raise AbsentError("the demo store keeps blobs in memory", hint="use the real engine")

    def verify(self, sha256: str) -> bool:
        import hashlib

        blob = self._blobs.get(sha256)
        return blob is not None and hashlib.sha256(blob[0]).hexdigest() == sha256

    def find(self, kind: str, **meta: str) -> list[tuple[str, dict[str, str]]]:
        return [(d, m) for d, (_, k, m) in self._blobs.items()
                if k == kind and all(m.get(a) == b for a, b in meta.items())]


class DemoDeploy:
    """Mirrors T2's DeployService: item names, event shapes, refusal codes."""

    def __init__(self, engine: DemoEngine) -> None:
        self._e = engine

    def _store(self, session: BoardSession) -> list[OverlayRef]:
        return _overlays(self._e._board(session.candidate.board_id).overlay_shell)

    def overlays(self, session: BoardSession) -> Sequence[OverlayRef]:
        self._e._enter("deploy.overlays", session.candidate.board_id)
        return self._store(session)

    def compatible(self, session: BoardSession) -> tuple[list[OverlayRef], dict[str, str]]:
        self._e._enter("deploy.compatible", session.candidate.board_id)
        ok: list[OverlayRef] = []
        why: dict[str, str] = {}
        for ov in self._store(session):
            bad = [i for i in self._items(session, ov) if i.check is Check.MISMATCH]
            if bad:
                why[ov.name] = "; ".join(f"{i.name}: {i.detail}" for i in bad)
            else:
                ok.append(ov)
        return ok, why

    def preflight(self, session: BoardSession, overlay: OverlayRef) -> Sequence[PreflightItem]:
        self._e._enter("deploy.preflight", session.candidate.board_id, overlay.name)
        return self._items(session, overlay)

    @staticmethod
    def _items(session: BoardSession, overlay: OverlayRef) -> list[PreflightItem]:
        shell = session.identity().shell_id
        same = overlay.static_id == shell
        return [
            PreflightItem("control channel free", Check.UNCHECKED,
                          "the fielded firmware cannot report its clients (needs 'stats')"),
            PreflightItem("shell_id matches", Check.OK if same else Check.MISMATCH,
                          f"overlay keyed to {overlay.static_id}; the board runs {shell}"),
            PreflightItem("crc and length", Check.OK,
                          f"crc32 over {overlay.size_bytes // 1024} KiB"),
            PreflightItem("clearing pairs partial", Check.OK, "same rm_id and pblock"),
            PreflightItem("clearing fits", Check.OK, "84 KiB of the 256 KiB arena"),
            PreflightItem("transport", Check.OK, "tcp+windowed (the board reports 'windowed')"),
        ]

    def card_status(self, session: BoardSession) -> CardStatus:
        """The board's user microSD (``set_card``), as the MPS3 pack reads it."""
        bid = session.candidate.board_id
        self._e._enter("deploy.card_status", bid)
        board = self._e._board(bid)
        if "usd" not in board.identity.features:
            return CardStatus(store=False, reason=CARD_NO_STORE)
        if board.card is None:
            return CardStatus(store=True, present=False, state="none", text="none",
                              reason=CARD_NO_CARD)
        if board.card in ("empty", "valid", "stale", "bad"):
            text = f"{board.identity.rm_name} [{board.card_slot}]" if board.card == "valid" \
                else board.card
            return CardStatus(store=True, present=True, state=board.card, text=text)
        return CardStatus(store=True, present=True, state=board.card, text=board.card,
                          reason=f"the card in the USER microSD slot cannot take a design "
                                 f"(state {board.card!r})")

    def _down_first(self, bid: str, force: bool) -> Any:
        """DEBUG-DOWN-FIRST as the real service settles it (``debug_onboard.settle``): the
        same refusal and warning words. None: this board has no on-board route."""
        from harness_manager.services import debug_onboard as ob

        board = self._e._board(bid)
        why = board.debug_down if board.debug_down is not None else \
            ("" if board.kind == "linux" else None)
        if why is None:
            return None
        self._e._enter("deploy.debug_down", bid, force)
        got = ob.DownFirst(asked=True, ok=not why, launcher="mps3-debug", why=why)
        return ob.settle(got, force=force, lock=lambda: board.debug_lock)

    def deploy(self, session: BoardSession, overlay: OverlayRef, *,
               keep_on_card: bool = False, force: bool = False) -> DeployResult:
        e = self._e
        bid = session.candidate.board_id
        e._enter("deploy.deploy", bid, overlay.name, keep_on_card)   # keep last: tests read it
        t0 = time.monotonic()
        items = self._items(session, overlay)
        bad = [i for i in items if i.check is Check.MISMATCH]
        err: HarnessError | None = None
        cleared = None
        if bad:
            text = "; ".join(f"{i.name}: {i.detail}" for i in bad)
            err = IncompatibleError(f"{overlay.name} does not match this board ({text})",
                                    hint="use an overlay built for the running shell")
        elif keep_on_card:
            err = keep_refusal(card_status_of(self, session))
        if err is None:
            try:
                cleared = self._down_first(bid, force)
            except HarnessError as exc:
                err = exc
        if err is not None:
            e.bus.publish(Event("deploy.failed", bid, {"reason": str(err),
                                                       "overlay": overlay.name,
                                                       "stage": "preflight"}))
            raise err
        if cleared is not None and cleared.warning:
            e.bus.publish(Event("deploy.warning", bid, {"overlay": overlay.name,
                                                        "message": cleared.warning}))
        e.bus.publish(Event("deploy.started", bid, {
            "overlay": overlay.name, "rm_id": overlay.rm_id,
            "preflight": [{"name": i.name, "check": i.check.value, "detail": i.detail}
                          for i in items], "keep_on_card": keep_on_card}))
        e.debug._auto_close(bid)
        phases = [("guard", 1, 1), ("swap", 1, 1), ("push", overlay.size_bytes, 6),
                  ("verify", 1, 1)]
        if keep_on_card:
            phases.append(("card", overlay.size_bytes, 3))
        for phase, total, steps in phases:
            for step in range(1, steps + 1):
                e._sleep(0.12)
                failure = e.failures.get(f"deploy.{phase}")
                if failure is not None:
                    e.bus.publish(Event("deploy.failed", bid, {
                        "reason": str(failure), "overlay": overlay.name, "stage": "deploy"}))
                    raise failure
                e.bus.publish(Event("deploy.progress", bid, {
                    "phase": phase, "bytes": total * step // steps, "total": total}))
        e._set_identity(bid, rm_id=overlay.rm_id, rm_name=overlay.name)
        card = None
        if keep_on_card:
            board = e._board(bid)
            if board.card is None:          # pulled mid-deploy: the swap stands
                card = CardOutcome(kept=False, why="no card in the user microSD slot")
            else:
                board.card_slot = "A" if board.card_slot == "B" else "B"
                board.card = "valid"
                card = CardOutcome(kept=True, slot=board.card_slot)
        seconds = time.monotonic() - t0
        e.bus.publish(Event("deploy.done", bid, {
            "rm_id": overlay.rm_id, "verified": True, "overlay": overlay.name,
            "seconds": seconds, "transport": "tcp+windowed",
            "card": None if card is None else {"kept": card.kept, "slot": card.slot,
                                                "why": card.why}}))
        return DeployResult(overlay.rm_id, True, seconds, "tcp+windowed", card=card)

    def restore_baseline(self, session: BoardSession, *, force: bool = False) -> DeployResult:
        self._e._enter("deploy.restore_baseline", session.candidate.board_id)
        greybox = next(o for o in self._store(session) if o.name == "greybox")
        return self.deploy(session, greybox, force=force)


class DemoStream:
    def __init__(self, broker: DemoConsoles, board_id: str, name: str) -> None:
        self.name = name
        self._broker, self._bid = broker, board_id
        self._q: queue.Queue[bytes] = queue.Queue()
        self.closed = False

    def feed(self, data: bytes) -> None:
        self._q.put(data)

    def read(self, timeout: float | None = None) -> bytes:
        if self.closed:
            raise AbsentError(f"console {self.name} is closed")
        try:
            return self._q.get(timeout=timeout)
        except queue.Empty:
            return b""

    def write(self, data: bytes) -> None:
        if self.closed:
            raise AbsentError(f"console {self.name} is closed")
        self._broker._write(self._bid, self.name, data)

    def close(self) -> None:
        self.closed = True
        self._broker._drop(self)


class DemoConsoles:
    def __init__(self, engine: DemoEngine) -> None:
        self._e = engine
        self._lock = threading.Lock()
        self._streams: list[tuple[str, DemoStream]] = []
        self._listeners: list[socket.socket] = []
        self.writes: list[tuple[str, str, bytes]] = []

    def names(self, session: BoardSession) -> list[str]:
        self._e._enter("consoles.names", session.candidate.board_id)
        return list(self._e._board(session.candidate.board_id).consoles)

    def subscribe(self, session: BoardSession, name: str) -> DemoStream:
        bid = session.candidate.board_id
        self._e._enter("consoles.subscribe", bid, name)
        if name not in self._e._board(bid).consoles:
            raise AbsentError(f"{bid} has no console named {name!r}")
        stream = DemoStream(self, bid, name)
        with self._lock:
            self._streams.append((bid, stream))
        self._e.bus.publish(Event("console.state", bid, {"name": name, "state": "up"}))
        return stream

    def inject(self, board_id: str, name: str, data: bytes) -> int:
        """Deliver ``data`` to every subscriber of one console; returns how many got it."""
        with self._lock:
            targets = [s for b, s in self._streams if b == board_id and s.name == name]
        for s in targets:
            s.feed(data)
        return len(targets)

    def export_tcp(self, session: BoardSession, name: str, port: int = 0) -> int:
        bid = session.candidate.board_id
        self._e._enter("consoles.export_tcp", bid, name, port)
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.bind(("127.0.0.1", port))
        srv.listen(1)
        with self._lock:
            self._listeners.append(srv)
        stream = self.subscribe(session, name)
        threading.Thread(target=self._pump, args=(srv, stream), daemon=True,
                         name=f"demo-export-{name}").start()
        return srv.getsockname()[1]

    def _pump(self, srv: socket.socket, stream: DemoStream) -> None:
        try:
            conn, _ = srv.accept()
        except OSError:
            stream.close()
            return
        with conn:
            conn.sendall(f"harness-manager demo: console {stream.name} (simulated)\r\n".encode())
            while not stream.closed:
                try:
                    chunk = stream.read(timeout=0.25)
                    if chunk:
                        conn.sendall(chunk)
                except (OSError, HarnessError):
                    break
        stream.close()

    def close_all(self, board_id: str) -> None:
        with self._lock:
            mine = [s for b, s in self._streams if b == board_id]
        for s in mine:
            s.close()

    def _shutdown(self) -> None:
        with self._lock:
            listeners, self._listeners = self._listeners, []
            streams = [s for _, s in self._streams]
        for srv in listeners:
            srv.close()
        for s in streams:
            s.close()

    def _write(self, board_id: str, name: str, data: bytes) -> None:
        self._e._enter("stream.write", board_id, name, data)
        with self._lock:
            self.writes.append((board_id, name, data))
        # The demo DUT echoes what it is sent, the way a UART REPL does.
        self.inject(board_id, name, data.replace(b"\r\n", b"\n").replace(b"\r", b"\n"))

    def _drop(self, stream: DemoStream) -> None:
        with self._lock:
            self._streams = [(b, s) for b, s in self._streams if s is not stream]


class DemoDebug:
    def __init__(self, engine: DemoEngine) -> None:
        self._e = engine

    def detect(self, session: BoardSession) -> str:
        bid = session.candidate.board_id
        self._e._enter("debug.detect", bid)
        ident = session.identity()
        if _design(ident.rm_id) not in DAP_DESIGNS:
            raise NothingOnTargetError(
                f"the loaded design ({ident.rm_name or ident.rm_id}) has no debug port",
                hint="load nanosoc, nanosoc_upy or nanosoc_iice first")
        return DAP_IDCODE

    def up(self, session: BoardSession) -> DebugStatus:
        e = self._e
        bid = session.candidate.board_id
        e._enter("debug.up", bid)
        board = e._board(bid)
        if board.debug.state == "up":
            raise AlreadyError("the OpenOCD session is already up", hint="close it first")
        on_board = board.kind == "linux"             # DEBUG-ONBOARD: the board runs OpenOCD
        design = _design(board.identity.rm_id)
        cfg = (ONBOARD_DAP_DESIGNS if on_board else DAP_DESIGNS).get(design)
        if cfg is None:
            raise NothingOnTargetError(
                f"the loaded design ({board.identity.rm_name or board.identity.rm_id}) "
                "has no debug port", hint="load nanosoc, nanosoc_upy or nanosoc_iice first")
        where = "board" if on_board else "host"
        self._set(bid, DebugStatus(state="starting", config=cfg, where=where))
        e._sleep(0.3)
        base = board.port_base
        if on_board:
            # the claim forward's local ends of the board's 3333/3334 (made up: nothing listens)
            cores = ONBOARD_CORES.get(design, ("cpu0",))
            status = DebugStatus(state="up", config=cfg, pid=812, gdb_ports=tuple(
                base + 7 * i for i in range(len(cores))), cores=cores, where="board",
                detail="OpenOCD runs on the board; gdb reaches it through the board's SSH "
                       "(pid 812)")
        else:
            status = DebugStatus(state="up", gdb_port=base, telnet_port=base + 1111,
                                 tcl_port=base + 3333, config=cfg, pid=40000 + base,
                                 detail="remote_bitbang to 6921")
        self._set(bid, status)
        return status

    def down(self, session: BoardSession) -> DebugStatus:
        bid = session.candidate.board_id
        self._e._enter("debug.down", bid)
        board = self._e._board(bid)
        was = board.debug.state
        self._e._sleep(0.1)
        status = DebugStatus(state="down", detail="closed" if was != "down" else "was not running",
                             where="board" if board.kind == "linux" else "host")
        self._set(bid, status)
        return status

    def status(self, session: BoardSession) -> DebugStatus:
        self._e._enter("debug.status", session.candidate.board_id)
        return self._e._board(session.candidate.board_id).debug

    def _set(self, board_id: str, status: DebugStatus) -> None:
        self._e._board(board_id).debug = status
        ports = ({k: v for k, v in (("gdb", status.gdb_port), ("telnet", status.telnet_port),
                                    ("tcl", status.tcl_port)) if v}
                 if status.state == "up" else {})
        self._e.bus.publish(Event("debug.state", board_id, {
            "state": status.state, "ports": ports, "where": status.where,
            "gdb_ports": list(status.gdb_ports), "cores": list(status.cores)}))

    def _auto_close(self, board_id: str) -> None:
        # T4's rule: the OpenOCD session closes before a swap.
        board = self._e._board(board_id)
        if board.debug.state != "down":
            on_board = board.kind == "linux"         # DEBUG-ONBOARD: the same words as the service
            self._set(board_id, DebugStatus(
                state="down", where="board" if on_board else "host",
                detail="closed for the swap (OpenOCD on the board stops before a partition swap)"
                if on_board else "closed for a partition swap"))


class DemoTelemetry:
    def __init__(self, engine: DemoEngine) -> None:
        self._e = engine

    def readings(self, session: BoardSession) -> list[Reading]:
        bid = session.candidate.board_id
        self._e._enter("telemetry.readings", bid)
        now = time.time()
        return [replace(r, observed_at=now) for r in self._e._board(bid).readings]


# --- the engine ----------------------------------------------------------------------


class DemoEngine:
    """Implements ``harness_manager.core.services.Engine`` over three scripted boards.

    ``showcase=True``: the showcase's four boards and its offline services (the module
    docstring); ``state_dir`` is where its catalogue, kits, pins and history live (a
    temporary directory, removed by ``close_all``, when not given). ``app_update``:
    ``"staged"`` stages a pretend app update (the banner); None reads
    ``HARNESS_MANAGER_DEMO_UPDATE``; anything else is off.
    """

    #: QUIET-POLL: scripted boards only, so background reads need no etiquette: the
    #: service's background gate (services/quiet.py) says yes to everything in the demo.
    fake_boards = True

    def __init__(self, *, speed: float = 1.0, console_chatter: bool = False,
                 showcase: bool = False, state_dir: Path | str | None = None,
                 app_update: str | None = None) -> None:
        self.bus = EventBus()
        self.store = DemoStore()
        self.deploy = DemoDeploy(self)
        self.consoles = DemoConsoles(self)
        self.debug = DemoDebug(self)
        self.telemetry = DemoTelemetry(self)
        self.speed = speed
        self.delays: dict[str, float] = {}
        self.failures: dict[str, HarnessError] = {}
        self.calls: list[tuple[str, tuple]] = []
        self._lock = threading.RLock()
        self.showcase = showcase
        if state_dir is not None:
            # Only when there is one: code that reads ``getattr(engine, "state_dir", DEFAULT)``
            # keeps its default for a classic engine without one.
            self.state_dir = Path(state_dir)
        self._temp_state: Path | None = None
        self._boards = _script() if not showcase else self._showcase(app_update)
        self._sessions: dict[str, DemoSession] = {}
        self._mine: dict[str, LockOwner] = {}
        self._pack = DemoPack()
        self._chatter_stop = threading.Event()
        if console_chatter:
            threading.Thread(target=self._chatter, daemon=True, name="demo-chatter").start()

    def _showcase(self, app_update: str | None) -> dict[str, _Board]:
        """The showcase's boards, and its services over the demo's own state dir."""
        import tempfile

        from harness_manager import demo_catalog as cat
        from harness_manager import demo_showcase as show
        from harness_manager.services.claim import ClaimService

        if getattr(self, "state_dir", None) is None:
            self.state_dir = self._temp_state = Path(tempfile.mkdtemp(prefix="hm-demo-"))
        self.state_dir.mkdir(parents=True, exist_ok=True)
        fixtures = self.state_dir / "demo-fixtures"
        staged = cat.staged_from_env() if app_update is None else app_update == cat.UPDATE_STAGED
        self.catalog = cat.DemoCatalog(fixtures)
        self.update = cat.update_service(self, self.state_dir, self.catalog, staged=staged)
        self.kit_channel = cat.kit_channel(self.update, self.catalog)
        cat.seed_kit(self.state_dir, self.catalog, self.kit_channel)
        cat.seed_history(self.state_dir, show.BOARD_V011, show.BOARD_LINUX)
        self.xvc = show.DemoXvc(self, fixtures / "xvc")
        self.board_claim = ClaimService(self)
        self._hub_state = show.DemoHubState(show.me())
        self._hub_spare = show.DemoHubState(show.me(), target=show.SPARE_TARGET,
                                            board=show.SPARE_BOARD, free=True)   # LEASE-UI
        return show.script()

    # -- the Engine protocol ------------------------------------------------------------

    def packs(self) -> dict[str, BoardPack]:
        return {"mps3": self._pack}

    def probe(self, hints: ProbeHints | None = None) -> list[Candidate]:
        self._enter("probe", hints)
        self._sleep(0.2)
        with self._lock:
            found = [b.candidate for b in self._boards.values() if b.reachable]
        for c in found:
            self.bus.publish(Event("board.found", c.board_id, {
                "pack": c.pack, "label": c.label, "evidence": c.evidence,
                "links": [{"kind": lk.kind.value, "address": lk.address, "detail": lk.detail}
                          for lk in c.links]}))
        return found

    def candidate_for(self, target: str, pack: str = "mps3") -> Candidate:
        self._enter("candidate_for", target, pack)
        spec = target.strip()
        if pack != "mps3":
            raise AbsentError(f"no board pack named {pack!r}", hint="installed packs: mps3")
        if not spec or any(ch.isspace() for ch in spec) or spec.count(":") > 1:
            raise UsageError(f"{target!r} is not an address", hint="use host or host:port")
        host, _, port = spec.partition(":")
        if port and not port.isdigit():
            raise UsageError(f"{target!r} has a bad port", hint="use host or host:port")
        board_id = f"mps3@{host}:{port or CONTROL_PORT}"
        with self._lock:
            if board_id in self._boards:
                return self._boards[board_id].candidate
            cand = Candidate("mps3", board_id, (_eth(host) if not port else
                                                Link(LinkKind.ETHERNET, f"{host}:{port}",
                                                     "shell control channel"),),
                             label=f"MPS3 at {host}:{port or CONTROL_PORT}",
                             evidence="given explicitly")
            # An address nobody scripted is a board that does not answer.
            self._boards[board_id] = _Board(
                candidate=cand, identity=BoardIdentity(board_type="mps3"),
                health=Health(reachable=False, control_channel="offline"), reachable=False)
        return cand

    def open(self, candidate: Candidate, *, note: str = "") -> BoardSession:
        bid = candidate.board_id
        self._enter("open", bid, note)
        self._sleep(0.1)
        board = self._board(bid)
        with self._lock:
            if bid in self._sessions:
                raise AlreadyError(f"{bid} is already open in this engine",
                                   hint="use engine.session(board_id), or close it first")
        if board.owner is not None:
            who = board.owner.describe()
            raise HeldError(f"{bid} is in use", holder=who, hint=f"held by {who}")
        if not board.reachable:
            raise UnreachableError(f"shell at {candidate.links[0].address} did not answer",
                                   hint="check the Ethernet link and the board's IP")
        with self._lock:
            sess = self._sessions[bid] = DemoSession(self, board.candidate)
            self._mine[bid] = LockOwner(user=getpass.getuser(), host=socket.gethostname(),
                                        pid=os.getpid(), since=time.time(), note=note)
        self.bus.publish(Event("session.opened", bid, {"pack": "mps3", "note": note}))
        return sess

    def session(self, board_id: str) -> BoardSession:
        with self._lock:
            sess = self._sessions.get(board_id)
        if sess is None:
            raise AbsentError(f"{board_id} is not open in this engine",
                              hint="open it first (engine.open(candidate))")
        return sess

    def info(self, board_id: str) -> BoardInfo:
        """Like the real engine: only for a board open in this engine."""
        self._enter("info", board_id)
        self.session(board_id)
        board = self._board(board_id)
        kinds = [lk.kind for lk in board.candidate.links]
        available, unavailable = negotiate(_specs(), kinds, board.identity.features)
        claim = getattr(self.session(board_id), "claim", None)      # LINUX-CLAIM (showcase)
        return BoardInfo(board.candidate, board.identity, board.health, available, unavailable,
                         claim=claim.claim_status(board.identity) if claim is not None else None)

    def close(self, board_id: str) -> None:
        self._enter("close", board_id)
        with self._lock:
            sess = self._sessions.pop(board_id, None)
            self._mine.pop(board_id, None)
        if sess is None:
            return
        self.consoles.close_all(board_id)
        if self._board(board_id).debug.state != "down":
            self.debug._set(board_id, DebugStatus(state="down", detail="session closed"))
        sess.close()
        self.bus.publish(Event("session.closed", board_id, {}))

    def close_all(self) -> None:
        with self._lock:
            ids = list(self._sessions)
        for bid in ids:
            self.close(bid)
        self.consoles._shutdown()
        self._chatter_stop.set()
        xvc = getattr(self, "xvc", None)
        if xvc is not None:
            xvc.close_all()
        if self._temp_state is not None:
            from harness_manager.demo_catalog import cleanup

            cleanup(self._temp_state)
            self._temp_state = None

    def lock_owner(self, board_id: str) -> LockOwner | None:
        """Who holds the board right now (any process), without opening it."""
        with self._lock:
            mine = self._mine.get(board_id)
        return mine if mine is not None else self._board(board_id).owner

    def open_boards(self) -> list[str]:
        with self._lock:
            return list(self._sessions)

    # -- demo / test controls (not part of the protocol) --------------------------------

    def set_links(self, board_id: str, links: Iterable[Link]) -> None:
        board = self._board(board_id)
        board.candidate = replace(board.candidate, links=tuple(links))
        self.bus.publish(Event("board.identity", board_id, {"links": [
            lk.kind.value for lk in board.candidate.links]}))

    def add_usb(self, board_id: str) -> None:
        """Plug the Debug USB cable into a board."""
        links = [lk for lk in self._board(board_id).candidate.links
                 if lk.kind not in (LinkKind.USB_SERIAL, LinkKind.USB_MSD)]
        self.set_links(board_id, [*links, USB_SERIAL_LINK, USB_MSD_LINK])

    def set_features(self, board_id: str, features: Iterable[str]) -> None:
        self._set_identity(board_id, features=tuple(features))

    def set_build_check(self, board_id: str, check: Check) -> None:
        self._set_identity(board_id, build_check=check)

    def set_owner(self, board_id: str, owner: LockOwner | None) -> None:
        """Somebody else takes (or releases) the board's lock."""
        self._board(board_id).owner = owner

    def set_card(self, board_id: str, state: str | None) -> None:
        """Put a card in the user microSD slot (its store in ``state``), or take it out."""
        self._board(board_id).card = state

    def set_debug_down(self, board_id: str, why: str | None, *, lock: bool = False) -> None:
        """FIX-PACK-7: the board's ``mps3-debug down`` fails with ``why`` ("": answers down;
        None: no on-board route); ``lock``: its launcher lists harnessd's lock."""
        board = self._board(board_id)
        board.debug_down, board.debug_lock = why, lock

    def set_sd_journal(self, board_id: str, journal: dict | None) -> None:
        """Leave (or clear) an interrupted SD install on a board's config SD."""
        self._board(board_id).sd_journal = journal

    def inject_console(self, board_id: str, name: str, text: str | bytes) -> int:
        data = text.encode() if isinstance(text, str) else text
        return self.consoles.inject(board_id, name, data)

    # -- internals --------------------------------------------------------------------

    def _board(self, board_id: str) -> _Board:
        with self._lock:
            board = self._boards.get(board_id)
        if board is None:
            raise AbsentError(f"no board {board_id!r}", hint="rescan, or add it by address")
        return board

    def _set_identity(self, board_id: str, **changes: object) -> None:
        board = self._board(board_id)
        board.identity = replace(board.identity, **changes)  # type: ignore[arg-type]
        if isinstance(board.candidate, DemoCandidate):
            board.candidate = replace(board.candidate, identity=board.identity)
        self.bus.publish(Event("board.identity", board_id, {
            "rm_id": board.identity.rm_id, "rm_name": board.identity.rm_name}))

    def _enter(self, name: str, *args: object) -> None:
        with self._lock:
            self.calls.append((name, args))
        delay = self.delays.get(name, 0.0)
        if delay:
            time.sleep(delay)
        failure = self.failures.get(name)
        if failure is not None:
            raise failure

    def _sleep(self, seconds: float) -> None:
        if self.speed > 0:
            time.sleep(seconds * self.speed)

    def called(self, name: str) -> list[tuple]:
        with self._lock:
            return [args for n, args in self.calls if n == name]

    def _chatter(self) -> None:
        n = 0
        while not self._chatter_stop.wait(2.0):
            n += 1
            for bid in list(self._sessions):
                self.consoles.inject(bid, "uart0", f"[{n:04d}] heartbeat: DUT alive\r\n".encode())


__all__ = ["BOARD_FIELDED", "BOARD_HELD", "BOARD_USB", "DemoEngine"]


# --- ui2 api-build ---
# UI2-API-BUILD G4 (docs/planning/UI_V2_PLAN.md §2; docs/API.md "Readings kept by this service"):
# the demo's side of the Overview's readings. ``DemoSession.readings_facts`` is the session seam
# ``services.history.facts_of`` asks (the harness's uptime, the Linux OS's, and a ``stats`` reply
# in the wire's key shape); ``DemoEngine.readings_seed`` gives the service's history ring the
# last 30 minutes of each temperature a board reads (one point a minute, drifting to today's
# value), so the trend has something to draw the moment a board opens. Scripted and
# deterministic per board; nothing here is a measurement.

_UI2_T0 = time.time()
#: Seconds each scripted board's harness has been up when the demo starts (the prototype's
#: OV_SEED figures); a board not named here uses its board id's hash.
_UI2_UP_S = {BOARD_FIELDED: 9360.0, BOARD_USB: 18120.0, BOARD_HELD: 101520.0,
             "mps3@192.168.10.104:6900": 101520.0, "mps3@192.168.10.105:6900": 267060.0,
             "mps3@192.168.10.106:6900": 9360.0, "mps3@192.168.10.107:6900": 18120.0}
_UI2_ICAP_PER_SWAP = 412_160


def _ui2_seed(board_id: str) -> int:
    import zlib

    return zlib.crc32(board_id.encode("utf-8"))


def _ui2_readings_facts(self: DemoSession) -> dict[str, Any]:
    """The demo board's uptime and ``stats`` (the seam ``services.history.facts_of`` reads)."""
    board = self._e._board(self.candidate.board_id)
    now = time.time()
    up_s = _UI2_UP_S.get(board.candidate.board_id,
                         float(1800 + _ui2_seed(board.candidate.board_id) % 86400))
    up_s += now - _UI2_T0
    swaps = int(board.health.counters.get("swaps", 0))
    linux = board.identity.harness_impl == "linux" or board.kind == "linux"
    stats: dict[str, Any] = {"up_ms": int(up_s * 1000), "swap_n": swaps, "swap": "idle",
                             "swap_ok": True, "icap": swaps * _UI2_ICAP_PER_SWAP,
                             "rxdrop": 0, "txerr": 0, "link": True, "spd": 100, "fdx": True,
                             "clk_alive": True, "lock": True, "rm_ok": True}
    if not linux:        # the bare-metal superloop's service telemetry (diag's svc_*)
        stats.update(svc_max_us=182_400, svc_max_ix=3, svc_overruns=0, svc_skips=0)
    out: dict[str, Any] = {"stats": stats, "at": now, "source": "demo"}
    if linux:
        out["os_up_ms"] = int((up_s + 42.0) * 1000)   # the OS booted just before harnessd
    return out


def _ui2_readings_seed(self: DemoEngine, board_id: str) -> list[dict[str, Any]]:
    """30 minutes of each temperature the board reads, one point a minute, ending at today's
    value (the history ring's seed; ``services.history.ReadingsHistory``)."""
    try:
        board = self._board(board_id)
    except HarnessError:
        return []
    now = time.time()
    rnd = _ui2_seed(board_id)
    out = []
    for r in board.readings:
        if r.value is None or r.unit != "degC":
            continue
        drift = ((rnd % 13) - 4) / 10.0                  # -0.4 .. +0.8 degC over the half hour
        points, wob = [], 0.0
        for i in range(30):
            rnd = (rnd * 1664525 + 1013904223) & 0xFFFFFFFF
            wob = wob * 0.6 + ((rnd / 2**32) - 0.5) * 0.4
            value = r.value - drift * (1 - i / 29) + (wob if i < 29 else 0.0)
            points.append([now - (30 - i) * 60.0, round(value, 1)])
        out.append({"name": r.name, "unit": r.unit, "source": r.source, "points": points})
    return out


DemoSession.readings_facts = _ui2_readings_facts          # type: ignore[attr-defined]
DemoEngine.readings_seed = _ui2_readings_seed             # type: ignore[attr-defined]
# --- end ui2 api-build ---
# --- ui2 api-hub -------------------------------------------------------------------------------------
# UI v2 (lane UI2-API-HUB): the demo engine's seams for G3 and G10 (demo_showcase has the data).
# Appended as the lane rules ask: the engine above is only extended here.


def _ui2_hub_for(self: DemoEngine, board_id: str) -> Any:
    """``hub_for`` (the daemon's ``hub_boards``): a showcase board's hub WITHOUT opening it (its
    lease badge, "Request without opening"), None for every other board."""
    if not self.showcase:
        return None
    from harness_manager import demo_showcase as show

    return show.hub_ref(self, board_id)


_ui2_showcase = DemoEngine._showcase


def _ui2_showcase_seeded(self: DemoEngine, app_update: str | None) -> dict[str, _Board]:
    boards = _ui2_showcase(self, app_update)
    from harness_manager import demo_showcase as show

    show.seed_identities(self.state_dir)          # G10: the identity clash to fix
    return boards


DemoEngine.hub_for = _ui2_hub_for                  # type: ignore[attr-defined]
DemoEngine._showcase = _ui2_showcase_seeded        # type: ignore[method-assign]
# --- end ui2 api-hub ---------------------------------------------------------------------------------
