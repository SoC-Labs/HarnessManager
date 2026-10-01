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
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from .capabilities import CapabilitySpec
from .model import BoardIdentity, Candidate, Check, Health, Reading
from .panel import PanelAdapter

if TYPE_CHECKING:
    from harness_manager.settings.schema import Setting

    from .display import DisplayAdapter


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
class CardOutcome:
    """What a deploy asked to keep on the board's user microSD did (L1 decision a).

    ``kept``: the pair is on the card, and the board loads it at its next power-on.
    ``slot``: the card slot it went to ("A" or "B"). ``why``: why it was not kept.
    """

    kept: bool
    slot: str = ""
    why: str = ""

    def text(self) -> str:
        """The deploy report's line: "Kept on the card (slot B)" / "Not kept: no card ..."."""
        if self.kept:
            return f"Kept on the card (slot {self.slot})" if self.slot else "Kept on the card"
        return f"Not kept: {self.why or 'no reason given'}"


@dataclass(frozen=True)
class CardStatus:
    """The board's user microSD, as "Keep on the card" needs it: one read, nothing written.

    ``store``: the harness keeps designs on a card (the MPS3 harness reports ``usd``).
    ``present``: a card is in the slot. ``state``/``text``: the store's own words
    ("empty", "valid", "foreign"...; the front panel's card row). ``reason``: why a deploy
    cannot keep its design on the card now; "" when it can.
    """

    store: bool
    present: bool = False
    state: str = ""
    text: str = ""
    reason: str = ""
    # --- additive (LINUX-SLOTS ``card status``, CCR LS-1): the rest of the same read, and
    # what the pack's card adapter adds to it. Defaults keep L1-CARD's shape unchanged.
    card_mb: int | None = None               # the card's size, once it is ready
    default: dict[str, str] | None = None    # {rm_id, static_id, slot[, rm_name]}: loaded at power-on
    boot: str = ""                           # the power-on decision: loaded|skipped|none|failed:<why>
    committable: bool = False                # a card commit could land now
    os_slots: SlotStatus | None = None       # Linux: the A/B OS slots on the same card
    notes: tuple[str, ...] = ()


#: "Keep on the card": the capability its refusal names, and the two plain reasons.
KEEP_ON_CARD = "keep_on_card"
CARD_NO_STORE = "this harness has no microSD store"
CARD_NO_CARD = "no card in the USER microSD slot"


def card_status_of(deploy: Any, *args: Any) -> CardStatus:
    """``deploy.card_status(*args)`` (a deploy service with the session, or an adapter);
    a deploy without card support answers "no store". Nothing is written."""
    read = getattr(deploy, "card_status", None)
    if not callable(read):
        return CardStatus(store=False, reason=CARD_NO_STORE)
    return read(*args)


def keep_refusal(card: CardStatus):
    """The error that refuses "Keep on the card" (``UnavailableError``, exit 12), or None
    when the card can take the design. The CLI, the API and the deploy service share it."""
    from .errors import UnavailableError

    return UnavailableError(KEEP_ON_CARD, card.reason) if card.reason else None


@dataclass(frozen=True)
class DeployResult:
    rm_id: str
    verified: bool
    seconds: float
    transport: str = ""       # "tcp+windowed", "tftp"
    #: Set only when the deploy was asked to keep the design on the card (``keep_on_card``).
    card: CardOutcome | None = None


Progress = Callable[[str, int, int], None]   # (phase, done, total)

#: CCR QUIET-1 (lane QUIET-POLL): ``observer(channel, exc)`` after each call a session makes on
#: one of the board's single-client channels. ``exc`` is None when the board answered, else the
#: error the call met. Channels: ``CONTACT_CONTROL`` (the harness's control port) and
#: ``CONTACT_MCC`` (the board controller's console); a pack may add its own.
ContactObserver = Callable[[str, "BaseException | None"], None]
CONTACT_CONTROL = "control"
CONTACT_MCC = "mcc"

#: SLOT-TIMING (additive): a ``Progress`` that also takes ``detail=`` says so with this
#: attribute set True. The detail of a long card job: ``{rate_bps, eta_s, text}``
#: ("writing slot B: 12.3 MB / 29 MB, ~6 min left"). Every other progress is called as before.
TAKES_DETAIL = "takes_detail"
#: The keys a progress ``detail`` carries (an event relays these, and only these).
DETAIL_KEYS = ("slot", "rate_bps", "eta_s", "text")


def detail_of(data: Mapping[str, Any]) -> dict[str, Any]:
    """The progress detail in an event's data (``DETAIL_KEYS``); {} when it has none."""
    return {k: data[k] for k in DETAIL_KEYS if k in data}


def report_progress(progress: Progress | None, phase: str, done: int, total: int,
                    detail: Mapping[str, Any] | None = None) -> None:
    """Call ``progress``; with ``detail`` too when it takes it (``TAKES_DETAIL``)."""
    if progress is None:
        return
    if detail and getattr(progress, TAKES_DETAIL, False):
        progress(phase, done, total, detail=dict(detail))  # type: ignore[call-arg]
    else:
        progress(phase, done, total)


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
    """A board's partition deploy.

    Optional, for a board that can keep a design on a card (the MPS3's user microSD):
    ``card_status() -> CardStatus``, and ``deploy(overlay, progress, keep_on_card=True)``
    filling ``DeployResult.card``. The deploy service calls ``deploy`` with the keyword
    only when it is asked to keep, so an adapter without the card support is unchanged.
    """

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
                progress: Progress | None = None) -> None:
        """Write ``files`` (SD path -> local file). FIX-PACK-7 (optional, additive): the MPS3
        pack's adapters also take ``allow_mcc_update=True`` (callers pass it only when asked)
        and leave what the write said beside its files in ``install_notes`` ("MBBIOS kept:
        …"), which the CLI prints and the update's outcome carries."""
        ...

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


@runtime_checkable
class XvcAdapter(Protocol):
    """Xilinx Virtual Cable to the harness's OWN XVC server (CCR X-3, docs/design/XVC_DEBUG.md).

    Scope: the reconfigurable partition's debug chain only (the harness's Debug Bridge
    and the debug hub and ILAs of the design loaded in the partition). It is never
    whole-device JTAG, and an adapter must never return an endpoint that is.

    Optional members the service uses when present: ``xvc_release()`` (close anything
    ``xvc_endpoint`` opened, e.g. a board-SSH forward), ``xvc_open_failures_since(t0)``
    (ssh's "open failed" lines: a far end that refused, not a held slot) and
    ``use_store(store)`` (the engine's content store, for probes files).
    """

    def xvc_endpoint(self) -> tuple[str, int]:
        """Where the harness's XVC server is reachable from this host, already tunnelled.

        May open a forward the first time (a board-SSH tunnel); raises
        ``UnreachableError`` when it cannot.
        """
        ...

    def xvc_reason(self) -> str:
        """``""`` when XVC can be used on this board now; else why not, in words."""
        ...

    def xvc_probes(self, rm_id: str) -> dict[str, Any]:
        """``{rm, static, full: {path, name, crc_ok, vivado, source} or None, vivado, rm_name}``."""
        ...

    def xvc_facts(self) -> dict[str, Any]:
        """``{scope, reach, impl, authenticated, warnings: [..], target, notes: [..]}`` for the UI."""
        ...


# --- OS boot slots and the user card (CCR T7-2, HARNESS-DIST H12; lane LINUX-SLOTS) ------------
#
# A Linux harness boots from one of two OS slots (A/B) on the board's user microSD, and
# the same card holds the D13 overlay store (the default overlay loaded at power-on).
# The board writes its own card: a host PUSHES an image into the slot that is neither
# running nor the default, the board reads it back, and ``commit`` makes it the default
# stage0 boots next. Every act answers the same status (the state AFTER the act).
# Wire: net-protocol.md v0.14 "Slot images" + v0.13 "User microSD"; the host side is
# SLOT_VERB_DRAFT.md with HM's answers (HARNESS_DISTRIBUTION.md §9).

#: A slot's ``state`` as the board reports it.
SLOT_ABSENT, SLOT_EMPTY, SLOT_BAD, SLOT_VALID, SLOT_IO = "absent", "empty", "bad", "valid", "io"
#: A slot's ``verified``: ``boot`` = stage0 booted this OS from it; ``readback`` = its region
#: CRCs were read back off the card this boot and bound to the fabric static_id.
VERIFIED_NO, VERIFIED_BOOT, VERIFIED_READBACK = "no", "boot", "readback"
#: The card job states during which nothing may reset the board (``services.reset_guard``).
JOB_BUSY = ("writing", "verifying")


@dataclass(frozen=True)
class SlotInfo:
    """One OS slot. The board's words, plus what THIS host pushed there (when it did)."""

    name: str                       # "A" | "B"
    state: str = SLOT_EMPTY         # absent | empty | bad | valid | io
    hdr_crc: str = ""               # "0x........": the S0LB table CRC, the image's identity on the card
    length: int = 0
    sid: str = ""                   # the static_id a slot record binds ("" = no record)
    verified: str = VERIFIED_NO     # no | boot | readback
    err: str = ""
    image_sha256: str = ""          # host-side: the image HM pushed under this hdr_crc ("" = unknown)
    version: str = ""               # host-side: the release HM pushed there ("" = unknown)

    @property
    def valid(self) -> bool:
        return self.state == SLOT_VALID


@dataclass(frozen=True)
class SlotJob:
    """The board's one card job (a push's or a verify's read-back)."""

    act: str = "none"               # none | push | verify
    slot: str = ""
    state: str = "idle"             # idle | writing | verifying | ok | failed
    got: int = 0
    length: int = 0
    err: str = ""
    # SLOT-TIMING (additive): the pack's host-side estimates while the job runs (never the
    # board's words, so never compared): bytes a second, and seconds to the job's end.
    rate_bps: float = field(default=0.0, compare=False)
    eta_s: float | None = field(default=None, compare=False)

    @property
    def busy(self) -> bool:
        """Writing or reading back: a reset now can wedge the card (silicon B2, 2026-09-26)."""
        return self.state in JOB_BUSY


@dataclass(frozen=True)
class SlotStatus:
    """``slot status``: which slot runs, which is the default, where a push would go."""

    running: str                                  # A | B | rescue | none | unknown
    slots: dict[str, SlotInfo] = field(default_factory=dict)
    card: bool = True
    default: str = ""
    target: str = ""                              # "" = no free slot (rule 1: roll back first)
    staged: str = ""                              # pushed + read back this boot: what commit flips to
    fabric_sid: str = ""
    seq: int = 0
    job: SlotJob = field(default_factory=SlotJob)
    raw: dict[str, Any] = field(default_factory=dict, compare=False)

    @property
    def active(self) -> str:
        """The slot the board booted (T7's name for ``running``)."""
        return self.running

    @property
    def active_info(self) -> SlotInfo:
        return self.slots.get(self.running, SlotInfo(self.running, state=SLOT_ABSENT))

    @property
    def inactive(self) -> str:
        """Where a push goes: the board's ``target``; raises when there is none."""
        if self.target:
            return self.target
        raise ValueError("the board reports no free slot: roll back the pending commit first")

    @property
    def pending_commit(self) -> str:
        """The slot committed as the default but not booted yet ("" = none). While there is
        one the board has no push target (rule 1): it must be rolled back first."""
        if self.running in ("A", "B") and self.default and self.default != self.running:
            return self.default
        return ""


@runtime_checkable
class OsSlotAdapter(Protocol):
    """``BoardSession.os_slots``: the Linux harness's A/B OS slots (CCR T7-2).

    None on a session whose link cannot carry them; an adapter whose ``slots_reason()``
    is not "" cannot be used now (a bare-metal harness, no card, stage0 rescue). Every
    mutation raises ``HarnessError``; nothing here ever writes the running or the default
    slot (the board refuses it too).
    """

    def slots_reason(self) -> str:
        """``""`` when the slot verbs can be used on this board now; else why not."""
        ...

    def status(self) -> SlotStatus:
        ...

    def push(self, image: Path, *, static_id: str, sha256: str = "", version: str = "",
             progress: Progress | None = None) -> SlotStatus:
        """Push ``image`` (an S0LB boot image provisioned for ``static_id``) into the board's
        target slot and wait for the card read-back. Returns the status (``staged``)."""
        ...

    def commit(self, slot: str | None = None) -> SlotStatus:
        """Make the staged slot the default (``slot`` guards which one)."""
        ...

    def rollback(self, slot: str | None = None) -> SlotStatus:
        """Make the other slot the default again (it must be verified)."""
        ...

    def verify(self, slot: str | None = None, progress: Progress | None = None) -> SlotStatus:
        """Read ``slot`` back off the card (default: the one that is not the default)."""
        ...

    def reboot(self, progress: Progress | None = None, wait_s: float = 180.0) -> dict | None:
        """Reboot the board and witness it go down and come back."""
        ...

    # Optional (SLOT-TIMING, ``services.reset_guard``): ``busy_job() -> SlotStatus | None``,
    # the status when the card job is writing or verifying (with the pack's ``rate_bps`` and
    # ``eta_s``), None when there is none to protect (no OS slots, no card, nothing answers);
    # ``HeldError`` when the job cannot be read. Without it the guard uses ``slots_reason()``
    # then ``status()``.


@runtime_checkable
class CardAdapter(Protocol):
    """``BoardSession.card``: the board's user microSD (D13). No card is not an error:
    ``status`` says so, and every mutation refuses cleanly without touching anything.
    ``status`` is L1-CARD's read (``DeployAdapter.card_status``) with the additive fields
    filled; ``commit`` re-pushes with the deploy adapter's commit pusher rules."""

    def card_reason(self) -> str:
        """``""`` when this harness has a user-microSD store; else why not."""
        ...

    def status(self) -> CardStatus:
        ...

    def commit(self, progress: Progress | None = None) -> dict[str, Any]:
        """Persist the RUNNING overlay pair as the power-on default (the v0.13 re-push)."""
        ...

    def clear(self) -> CardStatus:
        """Invalidate the default: the board boots the greybox at the next power-on."""
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
    xvc: XvcAdapter | None = None          # CCR X-3: fabric debug over the harness's XVC
    panel: PanelAdapter | None = None      # CCR PANEL-5: the front panel (core.panel)
    os_slots: OsSlotAdapter | None = None  # CCR T7-2: the Linux harness's A/B OS slots
    card: CardAdapter | None = None        # CCR LS-1: the user microSD (D13 overlay store)
    display: DisplayAdapter | None = None  # LM2: the live display mirror (core.display)

    def set_observer(self, observer: ContactObserver | None) -> None:  # noqa: B027 - optional
        """CCR QUIET-1 (lane QUIET-POLL): report each call on the board's single-client
        channels to ``observer(channel, exc)`` (``ContactObserver``); None stops it. The
        service uses it to back its background reads off while another client holds the
        board. It must never change a call's result or error. Default: nothing is reported."""

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

    def display_adapter(self, session: BoardSession) -> DisplayAdapter | None:
        """The pack hook ``display_adapter`` (docs/design/LCD_MIRROR.md §7.1, lane LM2): the
        board's live display mirror (``core.display.DisplayAdapter``), or None when the board
        has none. One adapter per session (it holds the board's forward), so every caller
        gets the same object. Default: the session's ``display`` adapter."""
        return getattr(session, "display", None)

    def settings(self) -> Iterable[Setting]:
        """The pack's own settings rows (CCR SET-PACK-1, docs/design/SETTINGS.md §3.2).

        Optional: a pack declares none by default. Each row says ``pack=<name>`` and is
        keyed under ``<name>.`` or in a board table (``boards.*.<table>.<field>``); the
        schema refuses any other key and one declared twice. A row's default is this
        instance's value, so the defaults are the resolver's ``pack`` layer.
        """
        return ()

    def candidate_for_host(self, spec: str, via: str = "") -> Candidate:
        """Build a candidate from an explicit address. Packs that can, override this.

        ``via`` ("ssh:HOST") asks for the board to be reached through an SSH tunnel
        on HOST; a pack that cannot do that raises ``UsageError`` for a non-empty one.
        """
        from .errors import UsageError

        raise UsageError(f"pack {self.name!r} cannot open a board by address")
