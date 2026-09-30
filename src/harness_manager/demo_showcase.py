"""The demo's showcase boards: one of each harness, so ``app --demo`` shows every feature.

``DemoEngine(showcase=True)`` (what ``harness-manager app --demo`` / ``ui --demo`` serve)
scripts four boards, each for a different set of features. Their adapters are modelled on
the lanes' test fakes and built on the product's own types and helpers, so a shape cannot
drift from the real one (``XvcStatus``/``vivado_tcl``, ``PanelState``/``rebuilt_frame``,
``SlotStatus``, ``CardStatus``, the lease service's notes):

- ``BOARD_LINUX`` (mps3-lx): the Linux harness (``impl: linux``) on 0x4C1A0003 with
  ``usd``, ``presence``, ``panel``, ``locate`` and ``xvc_lock``. A card in the user microSD
  slot (the overlay store's slots A/B, nanosoc on A) with the OS slots A/B on it; its SSH
  claimed by you; the front panel read from the glass (sessions, touch health, a tap);
  Identify; an XVC session that opens (board-SSH reach, no warning: the lock is on).
  Ethernet and SSH only, so the Debug-USB capabilities say why they are missing. Its Live
  display mirrors the panel from an in-memory lcd_mirror board (``demo_display``, LM4); the
  two bare-metal boards say why they have none (the leased one: 409 HELD naming alice).
  (``tests/fakes``: ``lxslots_board``/``lxslots_mock_card``, ``lc_mock_claim``,
  ``clcd_panel_shell``/``p1_mock_panel``, ``x3_mock_xvc``.)
- ``BOARD_V011`` (mps3-01): today's fielded bare-metal harness, v0.11 on 0x72BB0A36, with
  the Debug USB (MCC console, config SD, clocks). Its front panel is the REBUILT view
  (bare metal has no ``panel`` verb), Identify is unavailable with the reason, and XVC
  opens with the "unauthenticated" warning. The harness catalogue gives it every verdict;
  its DUT build kit is in the cache.
- ``BOARD_LEASED`` (mps3-02): a v0.11 board behind the hub (mapstone-dev), its hub lease
  held by alice (no Harness Manager session answers for her) with bob queued behind your
  own request, which alice has not answered in time: Request, the queue and Force-release
  are all live (``tests/fakes/lrb_fake_hub.py``'s hub, in miniature). Its MCC is reached ON
  the hub (a ``hub-mcc://`` link), never through a share on ``tty_00`` (MCC-FIX).
- ``BOARD_SPARE`` (mps3-03, LEASE-UI): another board behind the same hub whose lease is FREE,
  so the lease badges show all three states: free, yours (Acquire it) and held by alice
  (mps3-02); Release and Close board's "also release the lease?" work on it.

Nothing here opens a socket or starts a process: the XVC ports and the tunnel's ports are
made up (nothing listens on them), the hub is in memory, the claim is scripted.
"""

from __future__ import annotations

import getpass
import threading
import time
import zlib
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from harness_manager.core import capabilities as C
from harness_manager.core.errors import (
    AbsentError,
    AlreadyError,
    HeldError,
    RefusedError,
    UnavailableError,
    UsageError,
)
from harness_manager.core.events import Event
from harness_manager.core.model import (
    BoardIdentity,
    Candidate,
    Check,
    Health,
    Link,
    LinkKind,
    Reading,
)
from harness_manager.core.pack import SlotInfo, SlotStatus
from harness_manager.core.panel import (
    REBUILT_NOTE,
    ROLE_IDENTIFY,
    ROLE_TEXT,
    SOURCE_PANEL,
    SOURCE_REBUILT,
    THEME_TODAY,
    PanelEvent,
    PanelFrame,
    PanelSession,
    PanelState,
    PanelSupport,
    order_sessions,
    touch_health,
)

from . import demo_catalog as cat

CONTROL_PORT = 6900
BOARD_LINUX = "mps3@192.168.10.104:6900"
BOARD_V011 = "mps3@192.168.10.105:6900"
BOARD_LEASED = "mps3@192.168.10.106:6900"
BOARD_SPARE = "mps3@192.168.10.107:6900"

HUB_HOST = "mapstone-dev.ecs.soton.ac.uk"
HUB_TARGET = "mps3_02_pl"
HUB_BOARD = "mps3_02"
#: The MCC's console on the hub (``hub_mcc.mcc_tty_for``'s default for the target).
HUB_MCC_TTY = f"/dev/{HUB_TARGET}/tty_00"
#: LEASE-UI: the spare board's target on the same hub; nobody holds its lease.
SPARE_TARGET = "mps3_03_pl"
SPARE_BOARD = "mps3_03"

#: The Linux harness's front-panel features, and its XVC lock (docs/design/XVC_DEBUG.md §7.1).
PANEL_FEATURES = ("presence", "panel", "locate")
#: ... and ``lcd_mirror`` (LM4): the Live display, the in-memory board of ``demo_display``.
LINUX_DEMO_FEATURES = cat.LINUX_FEATURES + ("identify", "usd") + PANEL_FEATURES + ("xvc_lock",
                                                                                  "lcd_mirror")
V011_DEMO_FEATURES = cat.V011

HOST_KEY = "SHA256:dEm0hOsTkEyOfThELinuxHaRnEsSmPs3lXdEmO0q1"
KEY_FP = "SHA256:y0uRkEyy0uRkEyy0uRkEyy0uRkEyy0uRkEyDeMo42"

SWAP_ROWS_LX = (
    "MPS3-LX             nanoSoC harness     ",
    "-" * 40,
    "DUT : nanosoc              v1.0         ",
    "SWAP: LOADED VERIFIED     #007  last OK ",
    "SID : 0x4C1A0003  USD : nanosoc [A]     ",
    "NET : 192.168.10.104  UP 100/FD         ",
    "UP  : 001:04:12:48                      ",
    "DUT : RST-REL  CLK-ALIVE   MMCM-LOCK    ",
    "ICAP: 1835072 B  rxdrop 0 txerr 0       ",
    "CFG : 1x Cortex-M0  no ETH  1x UART     ",
    "OS  : A* 2.0.0 (boot)  B 2.0.0-rc2      ",
    "hm   {who:<15} +1 watching      ",
    "SYS : linux  ssh claimed SHA256:y0uRkE  ",
    "-" * 40,
    "MAC 02:00:00:4D:50:53             hb \\  ",
)


def _who() -> str:
    from harness_manager.services.presence import default_who

    return default_who()


def _iso(t: float) -> str:
    return datetime.fromtimestamp(t, timezone.utc).isoformat(timespec="seconds")


def _eth(host: str, detail: str = "shell control channel") -> Link:
    return Link(LinkKind.ETHERNET, f"{host}:{CONTROL_PORT}", detail)


# --- the four boards -----------------------------------------------------------------------------


def script() -> dict[str, Any]:
    """The showcase's boards (``demo._Board``s), keyed by board id."""
    from .demo import USB_MSD_LINK, USB_SERIAL_LINK, DemoCandidate, _Board, _counters

    linux = _Board(
        candidate=Candidate(
            "mps3", BOARD_LINUX,
            (_eth("192.168.10.104"),
             Link(LinkKind.SSH, "root@192.168.10.104", "dropbear, key-only (claimed)")),
            label="MPS3 Linux harness · nanosoc on 0x4c1a0003",
            evidence="answered identify (UDP 6899) and ping", name="mps3-lx",
            name_source="harness"),
        identity=BoardIdentity(board_type="mps3", shell_id=cat.S_LNX.lower(),
                               rm_id="0x01000001", rm_name="nanosoc", harness_version="1.0.0",
                               firmware_sha=cat.FW_LNX, features=LINUX_DEMO_FEATURES,
                               build_check=Check.OK, harness_impl="linux", proto="0.14",
                               usercode=cat.U_LNX.lower(), name="mps3-lx", ver32="0x01000000"),
        health=Health(reachable=True, control_channel="idle", counters=_counters(7)),
        consoles=("uart0", "uart1", "swo", "shell"),
        readings=[
            Reading("dut_clk", 50.0, "MHz", source="shell-6900", reason="preset"),
            Reading.unavailable("mcc_temp", "degC", "needs the Debug USB cable",
                                source="mcc-console"),
            Reading.unavailable("board_power", "W", "the MPS3 has no power sensor; add a "
                                "metered plug or an INA260"),
        ],
        port_base=3363, card="valid", card_slot="A", overlay_shell=cat.S_LNX.lower(),
        kind="linux")
    v011 = _Board(
        candidate=Candidate(
            "mps3", BOARD_V011,
            (_eth("192.168.10.105"), USB_SERIAL_LINK, USB_MSD_LINK),
            label="MPS3 nanosoc on shell 0x72bb0a36",
            evidence="answered ping; FT4232H 0403:6011", name="mps3-01",
            name_source="config"),
        identity=BoardIdentity(board_type="mps3", shell_id=cat.S_ILA.lower(),
                               rm_id="0x01000001", rm_name="nanosoc", harness_version="1.0.0",
                               firmware_sha=cat.FW_ILA, features=V011_DEMO_FEATURES,
                               build_check=Check.OK, harness_impl="bare-metal", proto="0.11",
                               usercode=cat.U_ILA.lower(), ver32="0x01000000"),
        health=Health(reachable=True, control_channel="idle", counters=_counters(24)),
        consoles=("uart0", "uart1", "swo", "mcc", "shell"),
        readings=[
            Reading("dut_clk", 50.0, "MHz", source="shell-6900", reason="preset"),
            Reading("mcc_temp", 41.0, "degC", source="mcc-console"),
            Reading("osc0", 50.0, "MHz", source="mcc-console", reason="CFG R OSC"),
            Reading.unavailable("fpga_die_temp", "degC",
                                "needs a JTAG cable on J17 (SYSMON over xsdb)",
                                source="sysmon-jtag"),
            Reading.unavailable("board_power", "W", "the MPS3 has no power sensor; add a "
                                "metered plug or an INA260"),
        ],
        port_base=3373, overlay_shell=cat.S_ILA.lower(), kind="bare-metal")
    leased = _Board(
        candidate=Candidate(
            "mps3", BOARD_LEASED,
            (_eth("192.168.10.106", "shell control channel, through the hub's ssh tunnel"),
             # MCC-FIX: the MCC is reached ON the hub (a HUB link, as hub.mcc_link makes it),
             # never through an fpgahub share on tty_00
             Link(LinkKind.HUB, f"hub-mcc://{HUB_HOST}/{HUB_TARGET}{HUB_MCC_TTY}",
                  f"the MCC console {HUB_MCC_TTY}, reached ON the hub {HUB_HOST} (pyverify's "
                  "tools run there; never an fpgahub share)", via="hub")),
            label="MPS3 nanosoc_upy on shell 0x72bb0a36 (via mapstone-dev)",
            evidence="listed by the hub (fpgahub 0.3.0)", name="mps3-02", name_source="hub"),
        identity=BoardIdentity(board_type="mps3", shell_id=cat.S_ILA.lower(),
                               rm_id="0x01000005", rm_name="nanosoc_upy",
                               harness_version="1.0.0", firmware_sha=cat.FW_ILA,
                               features=V011_DEMO_FEATURES, build_check=Check.OK,
                               harness_impl="bare-metal", proto="0.11",
                               usercode=cat.U_ILA.lower(), ver32="0x01000000"),
        health=Health(reachable=True, control_channel="idle", counters=_counters(52)),
        # no "mcc" console: nothing streams tty_00 from the hub (one reader, MCC-FIX); the
        # MCC's reads and REBOOT run on the hub (`mcc` verbs, Power > Board reboot)
        consoles=("uart0", "uart1", "swo", "shell"),
        readings=[
            Reading("dut_clk", 25.0, "MHz", source="shell-6900", reason="preset"),
            Reading("mcc_temp", 39.5, "degC", source="mcc-console (hub)"),
            Reading.unavailable("board_power", "W", "the MPS3 has no power sensor; add a "
                                "metered plug or an INA260"),
        ],
        port_base=3383, overlay_shell=cat.S_ILA.lower(), kind="leased")
    spare_mcc = f"/dev/{SPARE_TARGET}/tty_00"
    spare = _Board(
        candidate=Candidate(
            "mps3", BOARD_SPARE,
            (_eth("192.168.10.107", "shell control channel, through the hub's ssh tunnel"),
             Link(LinkKind.HUB, f"hub-mcc://{HUB_HOST}/{SPARE_TARGET}{spare_mcc}",
                  f"the MCC console {spare_mcc}, reached ON the hub {HUB_HOST} (pyverify's "
                  "tools run there; never an fpgahub share)", via="hub")),
            label="MPS3 nanosoc on shell 0x72bb0a36 (via mapstone-dev)",
            evidence="listed by the hub (fpgahub 0.3.0)", name="mps3-03", name_source="hub"),
        identity=BoardIdentity(board_type="mps3", shell_id=cat.S_ILA.lower(),
                               rm_id="0x01000001", rm_name="nanosoc",
                               harness_version="1.0.0", firmware_sha=cat.FW_ILA,
                               features=V011_DEMO_FEATURES, build_check=Check.OK,
                               harness_impl="bare-metal", proto="0.11",
                               usercode=cat.U_ILA.lower(), ver32="0x01000000"),
        health=Health(reachable=True, control_channel="idle", counters=_counters(9)),
        consoles=("uart0", "uart1", "swo", "shell"),
        readings=[
            Reading("dut_clk", 50.0, "MHz", source="shell-6900", reason="preset"),
            Reading("mcc_temp", 38.0, "degC", source="mcc-console (hub)"),
            Reading.unavailable("board_power", "W", "the MPS3 has no power sensor; add a "
                                "metered plug or an INA260"),
        ],
        port_base=3393, overlay_shell=cat.S_ILA.lower(), kind="spare")
    boards = (linux, v011, leased, spare)
    for b in boards:
        b.candidate = DemoCandidate(**{f: getattr(b.candidate, f) for f in (
            "pack", "board_id", "links", "label", "evidence", "name", "name_source")},
            identity=b.identity)
    return {b.candidate.board_id: b for b in boards}


# --- the front panel -------------------------------------------------------------------------------


class DemoPanel:
    """``session.panel``: an image with ``panel`` (read from the glass), or one without it
    (bare metal, or a Linux image before R1-R3): the rebuilt view, with Identify and presence
    unavailable (``harness_manager_mps3.panel``)."""

    def __init__(self, engine: Any, board_id: str) -> None:
        self._e, self._bid = engine, board_id
        self._mu = threading.Lock()
        self._seq = 3
        now = time.time()
        self._ring = [(3, "identify", now - 40.0)]
        self._sid = ""                 # the daemon's hello sid (it picks one per open)
        self._hello_at = 0.0
        # LOCATE: the blink is the board's, not this session's: a board identified while it
        # was not open (the daemon opens it for that one request) still shows the banner.
        glass = engine.__dict__.setdefault("_demo_locate", {})
        self._glass = glass.setdefault(board_id, {"until": 0.0, "who": ""})

    def _features(self) -> frozenset[str]:
        return frozenset(self._e._board(self._bid).identity.features)

    def support(self) -> PanelSupport:
        from harness_manager_mps3.capabilities import NEEDS_LOCATE, NEEDS_PANEL, NEEDS_PRESENCE

        f = self._features()
        panel = "panel" in f
        return PanelSupport(front_panel="" if panel or "clcd_kvm" in f else NEEDS_PANEL,
                            presence="" if "presence" in f else NEEDS_PRESENCE,
                            locate="" if "locate" in f else NEEDS_LOCATE,
                            source=SOURCE_PANEL if panel else SOURCE_REBUILT,
                            impl=self._e._board(self._bid).identity.harness_impl or "")

    def _sessions(self) -> tuple[PanelSession, ...]:
        mine_age = max(0.0, time.time() - self._hello_at) if self._hello_at else 3.0
        return order_sessions((
            PanelSession(self._sid or "hm00demo", _who(), "owner", round(mine_age, 1),
                         mine=True),
            PanelSession("b0b0c0de", "bob@lab-pc-03", "watch", 12.0)))

    def state(self) -> PanelState:
        now = time.time()
        if self.support().source == SOURCE_REBUILT:
            return PanelState(owner="harness",
                              touch=touch_health({"touch_ok": True, "touch_bus_lost": 0,
                                                  "touch_recoveries": 0}),
                              source=SOURCE_REBUILT, observed_at=now, note=REBUILT_NOTE)
        board = self._e._board(self._bid)          # the glass's card row, as the store says
        card = (f"{board.identity.rm_name} [{board.card_slot}]" if board.card == "valid"
                else board.card or "")
        with self._mu:
            events = tuple(PanelEvent(seq=s, on=on, ms_ago=int((now - at) * 1000), at=at)
                           for s, on, at in self._ring)
            seq = self._seq
        sessions = self._sessions()
        banner = "identify" if self._blinking() else ""
        return PanelState(page="status", owner="harness", card=card, banner=banner,
                          touch=touch_health({"touch_ok": True, "touch_bus_lost": 0,
                                              "touch_recoveries": 1},
                                             {"present": True, "cal": True}),
                          sessions=sessions, count=len(sessions), seq=seq, events=events,
                          source=SOURCE_PANEL, observed_at=now)

    def frame(self) -> PanelFrame:
        board = self._e._board(self._bid)
        if self.support().source == SOURCE_REBUILT:
            from harness_manager_mps3.panel import rebuilt_frame

            host = self._bid.split("@", 1)[-1].rsplit(":", 1)[0]
            return rebuilt_frame(name=board.candidate.name, identity=board.identity, host=host,
                                 owner="harness", wall=time.time())
        rows = [r.format(who=_who()[:15]).ljust(40)[:40] for r in SWAP_ROWS_LX]
        roles = [ROLE_TEXT * 40 for _ in rows]
        if self._blinking():
            # BOARD_LOCATE.md §2: the banner rows 10-12 inverted, "IDENTIFY: <who>" on row 11,
            # in the role the Linux harness gives them (banner-busy, PANEL-V017).
            with self._mu:
                who = self._glass["who"]
            rows[10] = " " * 40
            rows[11] = f"IDENTIFY: {who}".ljust(40)[:40]
            rows[12] = " " * 40
            for r in (10, 11, 12):
                roles[r] = ROLE_IDENTIFY * 40
        return PanelFrame(rows=tuple(rows), roles="".join(roles), source=SOURCE_PANEL,
                          observed_at=time.time(), theme=THEME_TODAY)

    def hello(self, hello: Any) -> PanelState:
        why = self.support().presence
        if why:
            raise UnavailableError(C.PRESENCE, why)
        with self._mu:
            self._sid = getattr(hello, "sid", "") or self._sid
            self._hello_at = time.time()
        st = self.state()
        return replace(st, sessions=())          # a hello reply carries the count only

    def locate(self, seconds: int, who: str) -> float:
        """The fake blink (LOCATE, the Linux lead's R3): the backlight blinks, and while the
        harness owns the panel (it always does here) the glass shows "IDENTIFY: <who>" in the
        banner rows 10-12 (docs/design/BOARD_LOCATE.md §2); the state's banner says
        ``identify``."""
        why = self.support().locate
        if why:
            raise UnavailableError(C.LOCATE, why)
        until = time.time() + seconds if seconds else 0.0
        with self._mu:
            self._glass.update(until=until, who=who)
        return until or time.time()

    def _blinking(self) -> bool:
        with self._mu:
            return self._glass["until"] > time.time()


# --- the SSH claim (Linux) ------------------------------------------------------------------------


class DemoClaim:
    """``session.claim``: the Linux harness's SSH, claimed by your key (lc_mock_claim's shape)."""

    def __init__(self, engine: Any, board_id: str, state: str = "mine") -> None:
        self._e, self._bid = engine, board_id
        self.state = state
        self.at = time.time() - 26 * 3600

    def claimable(self) -> str:
        return ""

    def claim_status(self, identity: Any = None, *, refresh: bool = False,
                     hub_ok: bool = False) -> dict[str, Any]:
        claimed = None
        if self.state == "mine":
            claimed = {"by": _who(), "key_fp": KEY_FP, "at": _iso(self.at), "mine": True}
        elif self.state == "other":
            claimed = {"by": "another key", "key_fp": None, "at": None, "mine": False}
        pinned = HOST_KEY if self.state == "mine" else None
        return {"state": self.state, "claimed": claimed,
                "host_key": {"reported": HOST_KEY, "pinned": pinned,
                             "match": True if pinned else None, "seen_before": None},
                "route": "lan", "user": "root", "source": "identify (demo)",
                "checked_at": _iso(time.time()), "live": True, "notes": []}

    def claim(self, *, key: str | None = None, adopt: bool = False,
              replace_host_key: bool = False, progress: Any = None) -> dict[str, Any]:
        if self.state != "unclaimed" and not adopt:
            raise AlreadyError(f"{self._bid} is already claimed"
                               + (" by your key" if self.state == "mine" else ""),
                               hint="nothing to do" if self.state == "mine" else
                               "the board refuses every other key's claim")
        if progress is not None:
            progress("putting your key (demo: nothing is sent)")
        self.state, self.at = "mine", time.time()
        return {**self.claim_status(), "action": "adopted" if adopt else "claimed"}

    def ssh_argv(self, command: Any = (), *, tty: bool = False) -> list[str]:
        host = self._bid.split("@", 1)[-1].rsplit(":", 1)[0]
        return ["ssh", *(["-t"] if tty else []), "-o", "StrictHostKeyChecking=yes",
                "-o", f"HostKeyAlias=harness-manager-{host}", "-l", "root", host, *command]


# --- the user microSD and the OS slots (Linux) -------------------------------------------------------


class DemoOsSlots:
    """``session.os_slots``: OS slots A (running, the default) and B (a verified older image).
    The reply carries the Linux lead's additive fields (LINUX-ANSWERS, S2/S5): ``confirmed``
    (harnessd confirmed this boot, so A is "booted, confirmed healthy", not "booted (not yet
    confirmed)") and ``claimed`` (the board's SSH, claimed by you: ``DemoClaim``)."""

    def __init__(self, engine: Any, board_id: str) -> None:
        self._e, self._bid = engine, board_id

    def slots_reason(self) -> str:
        return "" if self._e._board(self._bid).card else "no card in the USER microSD slot"

    def status(self) -> SlotStatus:
        sid = cat.S_LNX.lower()
        return SlotStatus(
            running="A", default="A", target="B", fabric_sid=sid, seq=7,
            slots={"A": SlotInfo("A", state="valid", hdr_crc="0x3e5e9c2c", length=24354312,
                                 sid=sid, verified="boot", version="2.0.0"),
                   "B": SlotInfo("B", state="valid", hdr_crc="0x1d0c55a1", length=24100864,
                                 sid=sid, verified="readback", version="2.0.0-rc2")},
            raw={"confirmed": True, "claimed": True})

    def _refuse(self, *_: Any, **__: Any) -> Any:
        raise RefusedError("the demo does not write the card: the OS slots are scripted",
                           hint="`harness-manager slot` does this on a real Linux harness")

    push = commit = rollback = verify = reboot = _refuse


class DemoCard:
    """``session.card``: the overlay store's default (A/B), plus the OS slots on the same card."""

    def __init__(self, engine: Any, board_id: str, slots: DemoOsSlots) -> None:
        self._e, self._bid, self._slots = engine, board_id, slots

    def card_reason(self) -> str:
        return "" if "usd" in self._e._board(self._bid).identity.features else \
            "this harness has no microSD store"

    def status(self) -> Any:
        return self.annotate(self._e.deploy.card_status(self._e.session(self._bid)))

    def annotate(self, status: Any) -> Any:
        board = self._e._board(self._bid)
        if not status.present:
            return replace(status, boot="none",
                           notes=("no card: the board boots exactly as it always has",))
        ident = board.identity
        return replace(status, card_mb=15193, boot="loaded", committable=True,
                       default={"rm_id": ident.rm_id, "static_id": ident.shell_id,
                                "slot": board.card_slot, "rm_name": ident.rm_name},
                       os_slots=self._slots.status(),
                       notes=(f"power-on loads {ident.rm_name} from store slot "
                              f"{board.card_slot}",
                              "OS slot A boots; B holds 2.0.0-rc2, verified by read-back"))

    def commit(self, progress: Any = None) -> dict[str, Any]:
        raise RefusedError("the demo does not write the card",
                           hint="Program with \"Keep on the card\" shows the flow")

    def clear(self) -> Any:
        raise RefusedError("the demo does not write the card")


# --- the hub (alice holds the lease, bob queues behind you) -------------------------------------------


class _Queued:
    def __init__(self, position: int, holder: str, user: str) -> None:
        self.position, self.holder, self.user = position, holder, user


class _Status:
    def __init__(self, held: bool, holder: str = "", user: str = "", expires_at: str = "",
                 queue: tuple[_Queued, ...] = ()) -> None:
        self.held, self.holder, self.user, self.expires_at, self.queue = (
            held, holder, user, expires_at, queue)


class DemoHubState:
    """The hub's state for one target, shared by every session (in memory). By default the
    leased board's (alice holds it, bob queues behind your request); ``free=True`` is a target
    nobody holds or queues for (the spare board, LEASE-UI)."""

    def __init__(self, me: str, *, target: str = HUB_TARGET, board: str = HUB_BOARD,
                 free: bool = False) -> None:
        from harness_manager.services.lease import RequestNote

        now = time.time()
        self.mu = threading.Lock()
        self.me = me
        self.target, self.board = target, board
        self._tokens = 0
        if free:
            self.current: dict[str, Any] | None = None
            self.queue: list[tuple[str, str]] = []
            self.notes: dict[str, Any] = {}
            self.answers: dict[str, Any] = {}
            self.history: list[dict[str, Any]] = []
            return
        self.current = {
            "holder": "alice@lab-pc-07", "user": "alice", "token": "tok-alice",
            "expires_at": _iso(now + 47 * 60)}
        self.queue = [(me, me.split("@")[0]), ("bob@lab-pc-03", "bob")]
        # Your request, 3 minutes old, unanswered: its 2-minute deadline has passed, so
        # force-release is offered (docs/LEASE_REQUESTS.md).
        self.notes = {
            "r-demo-0001": RequestNote(id="r-demo-0001", by=me, user=me.split("@")[0],
                                       host=me.split("@", 1)[-1],
                                       message="demo: I need mps3-02 for the DUT bring-up",
                                       created_at=_iso(now - 180),
                                       deadline_at=_iso(now - 60))}
        self.answers = {}
        self.history = [
            {"ts": _iso(now - 5400), "event": "lease.granted", "board": HUB_TARGET,
             "holder": "alice@lab-pc-07"},
            {"ts": _iso(now - 1200), "event": "lease.queued", "board": HUB_TARGET,
             "holder": me, "position": 1},
            {"ts": _iso(now - 600), "event": "lease.queued", "board": HUB_TARGET,
             "holder": "bob@lab-pc-03", "position": 2}]

    def token(self) -> str:
        self._tokens += 1
        return f"tok-demo-{self._tokens:04d}"

    def log(self, event: str, **kw: Any) -> None:
        self.history.append({"ts": _iso(time.time()), "event": event, "board": self.target, **kw})

    def promote(self) -> None:
        if self.current is None and self.queue:
            principal, user = self.queue.pop(0)
            self.current = {"holder": principal, "user": user, "token": self.token(),
                            "expires_at": _iso(time.time() + 3600)}
            self.log("lease.promoted", holder=principal)

    def status(self) -> _Status:
        q = tuple(_Queued(i + 1, p, u) for i, (p, u) in enumerate(self.queue))
        c = self.current
        if c is None:
            return _Status(False, queue=q)
        return _Status(True, c["holder"], c["user"], c["expires_at"], q)


class DemoHubClient:
    """The frozen ``HubClient`` (L1's lease verbs + LR-B's request verbs), in memory."""

    transport = "demo"

    def __init__(self, hub: DemoHubState) -> None:
        self.hub = hub
        self.host, self.target = HUB_HOST, hub.target

    def principal(self) -> str:
        return self.hub.me

    def lease_status(self) -> _Status:
        with self.hub.mu:
            return self.hub.status()

    lease_show = lease_status

    def board_id(self) -> str:
        return self.hub.board

    def can_revoke(self) -> tuple[bool, str]:
        return True, ""

    def lease_acquire(self, holder: str, *, ttl: int, poll_s: float = 20.0,
                      timeout_s: float = 3600.0, sleep: Any = None, log_fn: Any = None,
                      **_: Any) -> tuple[Any, str]:
        from pyverify.lease import Lease

        say = log_fn or (lambda _m: None)
        waited = 0.0
        while True:
            h, me = self.hub, self.hub.me
            with h.mu:
                c = h.current
                if c is None or c["holder"] == me:
                    if c is None:
                        h.queue = [(p, u) for p, u in h.queue if p != me]
                        h.current = {"holder": me, "user": me.split("@")[0], "token": h.token(),
                                     "expires_at": _iso(time.time() + ttl)}
                        h.log("lease.granted", holder=me)
                    cur = h.current
                    assert cur is not None
                    say(f"lease granted: {holder} holds {self.target}")
                    return (Lease(token=cur["token"], holder=holder, target=self.target),
                            cur["expires_at"])
                if me not in [p for p, _ in h.queue]:
                    h.queue.append((me, me.split("@")[0]))
                    h.log("lease.queued", holder=me, position=len(h.queue))
                pos = [p for p, _ in h.queue].index(me) + 1
            say(f"queued at position {pos} for {self.target} as {holder}; re-acquiring in "
                f"{poll_s}s")
            if waited >= timeout_s:
                self.lease_cancel(holder)
                raise AbsentError(f"gave up waiting for {self.target} after {timeout_s}s")
            (sleep or time.sleep)(poll_s)
            waited += poll_s

    def lease_heartbeat(self, token: str, holder: str) -> str:
        from harness_manager_mps3.hub import LeaseLostError

        with self.hub.mu:
            c = self.hub.current
            if c is None:
                raise LeaseLostError("lease heartbeat: no current lease for board",
                                     state="expired")
            if c["holder"] != self.hub.me or c["token"] != token:
                raise LeaseLostError("lease heartbeat: holder or token does not match "
                                     "current lease", state="lost")
            c["expires_at"] = _iso(time.time() + 3600)
            return c["expires_at"]

    def lease_release(self, token: str, holder: str) -> None:
        with self.hub.mu:
            c = self.hub.current
            if c is None or c["holder"] != self.hub.me or c["token"] != token:
                raise RefusedError("no lease to release")
            self.hub.current = None
            self.hub.log("lease.released", holder=self.hub.me)
            self.hub.promote()

    def lease_cancel(self, holder: str) -> bool:
        with self.hub.mu:
            before = len(self.hub.queue)
            self.hub.queue = [(p, u) for p, u in self.hub.queue if p != self.hub.me]
            return len(self.hub.queue) != before

    def lease_revoke(self, reason: str) -> dict[str, Any]:
        by = f"unix:{self.hub.me.split('@')[0]}"
        with self.hub.mu:
            prior = self.hub.current
            if prior is None:
                return {"revoked": [], "by": by}
            self.hub.current = None
            self.hub.log("lease.released", holder=prior["holder"])
            self.hub.log("lease.admin_revoked", by=by, reason=f"{reason} (by {by})",
                         prior_holder=prior["holder"])
            self.hub.promote()
        return {"revoked": [self.target], "by": by}

    def lease_history(self, limit: int = 50) -> list[dict[str, Any]]:
        with self.hub.mu:
            return [dict(e) for e in self.hub.history[-limit:]]

    def put_request(self, note: Any) -> None:
        with self.hub.mu:
            self.hub.notes[note.id] = note

    def list_requests(self) -> list[Any]:
        with self.hub.mu:
            return list(self.hub.notes.values())

    def delete_request(self, request_id: str) -> None:
        with self.hub.mu:
            self.hub.notes.pop(request_id, None)

    def put_answer(self, note: Any) -> None:
        with self.hub.mu:
            self.hub.answers[note.id] = note

    def get_answer(self, request_id: str) -> Any:
        with self.hub.mu:
            return self.hub.answers.get(request_id)


class DemoHubRef:
    """``session.hub``: ``host``, ``target`` and ``client``."""

    def __init__(self, client: DemoHubClient) -> None:
        self.host, self.target, self.client = HUB_HOST, client.target, client

    def board_id(self) -> str:
        return self.client.hub.board


class DemoReach:
    """``session.reach``: the hub's ssh tunnel, as ``GET /boards/{bid}/tunnel`` shows it.
    The ports are made up: nothing listens on them."""

    def __init__(self) -> None:
        self.tunnel = None
        self.since = time.time() - 300

    def status(self) -> dict[str, Any]:
        return {"via": f"ssh:{HUB_HOST}", "host": HUB_HOST, "state": "up",
                "ports": {"6900": 46900, "6921": 46921, "6930": 46930},
                "forwards": 3, "restarts": 0, "pid": None,
                "detail": "demo: an in-memory hub; nothing is forwarded"}


# --- fabric debug over XVC -----------------------------------------------------------------------


class DemoXvc:
    """``engine.xvc`` for the demo: the XVC routes' service (``services.xvc.XvcService``'s
    methods), with ``XvcStatus`` and ``vivado_tcl`` from the product and no relay, no
    hw_server and no socket (x3_mock_xvc's model). The ports are made up."""

    def __init__(self, engine: Any, root: Path) -> None:
        self._e = engine
        self.root = Path(root)
        self.leases: Any = None              # xvc_api shares the daemon's LeaseService here
        self._mu = threading.Lock()
        self._live: dict[str, dict[str, Any]] = {}
        bus = engine.bus
        self._unsubs = [bus.subscribe("deploy.started", self._swap_started),
                        bus.subscribe("deploy.done", self._swap_done),
                        bus.subscribe("deploy.failed", self._swap_failed)]

    # -- facts --

    def _ident(self, session: Any) -> BoardIdentity:
        return session.identity()

    def reason(self, ident: BoardIdentity) -> str:
        from harness_manager_mps3.xvc import (
            DBGBR_FEATURE,
            JTAGBB_FEATURE,
            JTAGBB_REASON,
            NO_DBGBR_REASON,
        )

        feats = set(ident.features or ())
        if JTAGBB_FEATURE in feats and DBGBR_FEATURE not in feats:
            return JTAGBB_REASON
        return "" if DBGBR_FEATURE in feats else NO_DBGBR_REASON

    def _facts(self, session: Any) -> tuple[str, list[str]]:
        from harness_manager.services.xvc import UNAUTHENTICATED_WARNING
        from harness_manager_mps3.xvc import (
            LOCK_FEATURE,
            NO_LOCK_NOTE,
            REACH_BOARD_SSH,
            REACH_DIRECT,
            REACH_HUB,
        )

        ident = self._ident(session)
        linux = ident.harness_impl == "linux"
        locked = LOCK_FEATURE in (ident.features or ())
        if linux:
            return REACH_BOARD_SSH, [] if locked else [NO_LOCK_NOTE]
        reach = REACH_HUB if getattr(session, "hub", None) is not None else REACH_DIRECT
        return reach, [UNAUTHENTICATED_WARNING]

    def _ltx_file(self, name: str) -> dict[str, Any]:
        self.root.mkdir(parents=True, exist_ok=True)
        path = self.root / name
        if not path.is_file():
            path.write_text('{"demo": true, "note": "a demo probes file, not a Vivado .ltx", '
                            f'"probes": [{{"name": "ila_0", "type": "ila", "of": "{name}"}}]}}\n',
                            encoding="utf-8")
        return {"path": str(path), "name": name, "crc_ok": True, "source": "demo",
                "vivado": "2024.1"}

    def probes(self, session: Any, *, refresh: bool = False) -> dict[str, Any]:
        from harness_manager_mps3.xvc import MIG_NOTE, NO_MIG_NOTE

        ident = self._ident(session)
        linux = ident.harness_impl == "linux"
        static = self._ltx_file(f"static_{ident.shell_id}.ltx") if linux else None
        static_note = MIG_NOTE if linux else NO_MIG_NOTE
        if not ident.rm_name or ident.rm_id in ("", "0x00000000"):
            return {"rm": None, "static": static, "full": None, "preferred": None,
                    "note": "the greybox has no ILAs", "static_note": static_note}
        rm = self._ltx_file(f"{ident.rm_name}.ltx")
        return {"rm": rm, "static": static, "full": None, "preferred": "rm", "note": "",
                "static_note": static_note}

    def identity_known(self, session: Any) -> bool:
        return True

    def status(self, session: Any, *, refresh: bool = False) -> Any:
        from harness_manager.services.xvc import PARTITION_SCOPE, SWAP_NOTE, XvcStatus

        bid = session.candidate.board_id
        ident = self._ident(session)
        reach, warnings = self._facts(session)
        with self._mu:
            live = dict(self._live.get(bid) or {})
        if not live:
            return XvcStatus(state="down", reach=reach, warnings=tuple(warnings),
                             reason=self.reason(ident), rm_id=ident.rm_id,
                             rm_name=ident.rm_name, ltx=self.probes(session))
        warnings = [*warnings, SWAP_NOTE]
        if live["mode"] == "byo":
            warnings.append("your own hw_server lingers 20 s after its last client: after a "
                            "swap, close and reopen the target (or wait it out)")
        m1 = live["mode"] == "m1"
        return XvcStatus(
            state=live["state"], open=True, mode=live["mode"], relay_port=live["relay"],
            hw_server_port=live["relay"] + 1 if m1 else 0, hw_server_pid=live["pid"],
            hw_server="/tools/Xilinx/Vivado/2024.1/bin/hw_server (2024.1, demo: not started)"
            if m1 else "",
            url=f"localhost:{live['relay'] + 1}" if m1 else f"127.0.0.1:{live['relay']}",
            attached=None, board_slot=live["slot"], reach=reach, ltx=self.probes(session),
            warnings=tuple(warnings), scope=PARTITION_SCOPE, rm_id=ident.rm_id,
            rm_name=ident.rm_name, detail=live.get("detail", ""))

    def _publish(self, session_or_bid: Any, st: Any = None) -> None:
        from harness_manager.services.xvc import TOPIC

        if isinstance(session_or_bid, str):
            bid = session_or_bid
            session = self._e.session(bid)
        else:
            session, bid = session_or_bid, session_or_bid.candidate.board_id
        st = st or self.status(session)
        self._e.bus.publish(Event(TOPIC, bid, st.to_json()))

    # -- actions --

    def check_open(self, session: Any, *, byo: bool = False) -> None:
        bid = session.candidate.board_id
        with self._mu:
            if bid in self._live:
                raise AlreadyError(f"the XVC session for {bid} is already open",
                                   hint="`xvc close` first to restart it")
        hub = getattr(session, "hub", None)
        if hub is not None:
            leases = self.leases
            view = leases.view(hub) if leases is not None else {}
            lease = (view or {}).get("lease")
            if not lease:
                raise HeldError(f"XVC is for the lease holder only, and nobody holds "
                                f"{hub.target}", holder="nobody",
                                hint="take the lease first: `harness-manager lease acquire "
                                     "TARGET`")
            if not lease.get("mine"):
                who = lease.get("holder") or "someone else"
                raise HeldError(f"XVC is for the lease holder only: {who} holds {hub.target}",
                                holder=who, hint="ask for the board: `harness-manager lease "
                                                 "request TARGET`")
        why = self.reason(self._ident(session))
        if why:
            raise UnavailableError(C.DEBUG_FABRIC, why)

    def open(self, session: Any, *, byo: bool = False) -> Any:
        self.check_open(session, byo=byo)
        bid = session.candidate.board_id
        relay = 41000 + 2 * (zlib.crc32(bid.encode()) % 400)
        with self._mu:
            self._live[bid] = {"state": "ready", "mode": "byo" if byo else "m1",
                               "relay": relay, "pid": 0 if byo else 40000 + relay % 1000,
                               "slot": "ours",
                               "detail": "demo: nothing listens on these ports"}
        st = self.status(session)
        self._publish(session, st)
        return st

    def close(self, session: Any, *, reason: str = "") -> Any:
        from harness_manager.services.xvc import XvcStatus

        bid = session.candidate.board_id
        with self._mu:
            self._live.pop(bid, None)
        reach, warnings = self._facts(session)
        st = XvcStatus(state="down", reach=reach, warnings=tuple(warnings),
                       detail=reason or "closed", reason=self.reason(self._ident(session)))
        self._publish(session, st)
        return st

    def ltx(self, session: Any, which: str = "auto", *, refresh: bool = False) -> dict[str, Any]:
        if which not in ("auto", "rm", "static", "full"):
            raise UsageError(f"which must be auto, rm, static or full, not {which!r}")
        probes = self.probes(session)
        key = probes.get("preferred") if which == "auto" else which
        item = probes.get(key) if key else None
        if not item:
            note = (probes.get("static_note") if which == "static" else "") or \
                probes.get("note") or ""
            raise AbsentError("no probes file (.ltx) for the loaded design"
                              f"{f' ({note})' if note else ''}",
                              hint="the RM's .ltx comes with its overlay")
        return {"which": key, **item}

    def tcl(self, session: Any, *, byo: bool | None = None, refresh: bool = False
            ) -> dict[str, Any]:
        from harness_manager.services.xvc import PARTITION_SCOPE, vivado_tcl

        bid = session.candidate.board_id
        with self._mu:
            live = dict(self._live.get(bid) or {})
        mode = live.get("mode") or ("byo" if byo else "m1")
        if byo is not None:
            mode = "byo" if byo else "m1"
        probes = self.probes(session)
        key = probes.get("preferred")
        ltx = (probes.get(key) or {}).get("path") if key else None
        if live:
            hw, xvc = f"localhost:{live['relay'] + 1}", f"127.0.0.1:{live['relay']}"
        else:
            hw, xvc = "localhost:<H: run `xvc open` first>", "127.0.0.1:<R>"
        return {"tcl": vivado_tcl(hw_server_url=hw, xvc_url=xvc, ltx=ltx, byo=mode == "byo"),
                "url": xvc if mode == "byo" else hw, "ltx": ltx, "which": key, "mode": mode,
                "scope": PARTITION_SCOPE, "open": bool(live)}

    # -- swaps: the session closes for a swap and reopens on the new design --

    def _set(self, bid: str, **changes: Any) -> bool:
        with self._mu:
            live = self._live.get(bid)
            if live is None:
                return False
            live.update(changes)
            return True

    def _swap_started(self, ev: Event) -> None:
        if self._set(ev.board_id, state="swapping", slot="released",
                     detail="closed for a partition swap; it reopens on the new design"):
            self._publish(ev.board_id)

    def _swap_done(self, ev: Event) -> None:
        if not ev.data.get("verified"):
            return
        if self._set(ev.board_id, state="ready", slot="ours",
                     detail="reopened on the new design: re-run the probes lines of `xvc tcl`"):
            self._publish(ev.board_id)

    def _swap_failed(self, ev: Event) -> None:
        with self._mu:
            live = self._live.get(ev.board_id)
        if live is not None and live.get("state") == "swapping":
            with self._mu:
                self._live.pop(ev.board_id, None)
            self._publish(ev.board_id)

    def close_all(self) -> None:
        with self._mu:
            self._live.clear()
        for unsub in self._unsubs:
            unsub()


class DemoXvcAdapter:
    """``session.xvc`` (``core.pack.XvcAdapter``): the board side ``DemoXvc`` reads. It never
    returns an endpoint: nothing is reached."""

    def __init__(self, engine: Any, board_id: str) -> None:
        self._e, self._bid = engine, board_id

    def _session(self) -> Any:
        return self._e.session(self._bid)

    def xvc_endpoint(self) -> tuple[str, int]:
        from harness_manager.core.errors import UnreachableError

        raise UnreachableError("the demo reaches no XVC server (its sessions are scripted)")

    def xvc_reason(self) -> str:
        return self._e.xvc.reason(self._session().identity())

    def xvc_probes(self, rm_id: str) -> dict[str, Any]:
        return self._e.xvc.probes(self._session())

    def xvc_facts(self) -> dict[str, Any]:
        from harness_manager.services.xvc import PARTITION_SCOPE
        from harness_manager_mps3.xvc import TARGET

        reach, warnings = self._e.xvc._facts(self._session())
        impl = self._session().identity().harness_impl
        return {"scope": PARTITION_SCOPE, "reach": reach, "impl": impl,
                "authenticated": not warnings, "warnings": warnings, "target": TARGET,
                "notes": []}


# --- a session's adapters ------------------------------------------------------------------------


def adapters(engine: Any, board: Any) -> dict[str, Any]:
    """The extra adapters a showcase board's session carries (``BoardSession`` attributes)."""
    bid = board.candidate.board_id
    from harness_manager.demo_display import DemoDisplay  # LM4: the Live display

    out: dict[str, Any] = {"panel": DemoPanel(engine, bid), "xvc": DemoXvcAdapter(engine, bid),
                           "display": DemoDisplay(engine, bid)}
    if board.kind == "linux":
        slots = DemoOsSlots(engine, bid)
        out.update(claim=DemoClaim(engine, bid), os_slots=slots,
                   card=DemoCard(engine, bid, slots))
    if board.kind == "leased":
        out.update(hub=DemoHubRef(DemoHubClient(engine._hub_state)), reach=DemoReach())
    if board.kind == "spare":                # LEASE-UI: the same hub, a free target
        out.update(hub=DemoHubRef(DemoHubClient(engine._hub_spare)), reach=DemoReach())
    return out


def me() -> str:
    """This client's principal on the demo hub (``user@host``), as fpgahub would name it."""
    import socket

    try:
        user = getpass.getuser()
    except Exception:  # noqa: BLE001 - no passwd entry in a container
        user = "user"
    return f"{user}@{socket.gethostname().split('.')[0]}"


__all__ = ["BOARD_LEASED", "BOARD_LINUX", "BOARD_SPARE", "BOARD_V011", "DemoXvc", "adapters", "me", "script"]


# --- ui2 api-build ---
# UI2-API-BUILD G6 (docs/planning/UI_V2_PLAN.md §2; docs/API.md "OS slots and the card: roll
# back, commit, clear"): the Linux showcase board's OS slots and card TAKE the changes the
# Board > Versions page makes, in memory (no card exists to write): a rollback makes the other
# slot the default (a verify first, a reboot into it unless asked not to), a card commit makes
# the running overlay the power-on default, a clear removes it. A push stays refused: nothing in
# the app writes an OS slot image (the harness install does, and the demo's is scripted).
# The state lives on the board (``ui2_os``, ``ui2_card``), so every session sees the same.


def _ui2_os(adapter: Any) -> dict[str, Any]:
    board = adapter._e._board(adapter._bid)
    st = getattr(board, "ui2_os", None)
    if st is None:
        st = {"running": "A", "default": "A", "verified": {"A": "boot", "B": "readback"}}
        board.ui2_os = st
    return st


_ui2_os_status_scripted = DemoOsSlots.status


def _ui2_os_status(self: DemoOsSlots) -> SlotStatus:
    base = _ui2_os_status_scripted(self)
    st = _ui2_os(self)
    slots = {n: replace(i, verified=st["verified"].get(n, i.verified))
             for n, i in base.slots.items()}
    other = "B" if st["running"] == "A" else "A"
    free = other if st["default"] == st["running"] else ""
    return replace(base, running=st["running"], default=st["default"], target=free,
                   slots=slots)


def _ui2_os_verify(self: DemoOsSlots, slot: str | None = None, progress: Any = None) -> SlotStatus:
    st = _ui2_os(self)
    name = slot or ("B" if st["running"] == "A" else "A")
    if progress is not None:
        progress("verify", 0, 24_100_864)
        progress("verify", 24_100_864, 24_100_864)
    st["verified"][name] = "readback"
    return _ui2_os_status(self)


def _ui2_os_rollback(self: DemoOsSlots, slot: str | None = None) -> SlotStatus:
    st = _ui2_os(self)
    st["default"] = slot or st["running"]
    return _ui2_os_status(self)


def _ui2_os_reboot(self: DemoOsSlots, progress: Any = None, wait_s: float | None = None
                   ) -> dict[str, Any]:
    st = _ui2_os(self)
    if progress is not None:
        progress("reboot", 0, 0)
    before = st["running"]
    st["running"] = st["default"]
    st["verified"][st["running"]] = "boot"
    if progress is not None:
        progress("up", 0, 0)
    return {"rebooted": True, "from": before, "to": st["running"],
            "up_evidence": "demo: harnessd answered after the reboot (scripted)"}


DemoOsSlots.status = _ui2_os_status                       # type: ignore[method-assign]
DemoOsSlots.verify = _ui2_os_verify                       # type: ignore[method-assign]
DemoOsSlots.rollback = _ui2_os_rollback                   # type: ignore[method-assign]
DemoOsSlots.reboot = _ui2_os_reboot                       # type: ignore[method-assign]

_ui2_card_annotate_scripted = DemoCard.annotate


def _ui2_card_annotate(self: DemoCard, status: Any) -> Any:
    out = _ui2_card_annotate_scripted(self, status)
    board = self._e._board(self._bid)
    cleared = getattr(board, "ui2_card", {}).get("cleared", False)
    if out.present and cleared:
        return replace(out, default=None, boot="greybox",
                       notes=("no power-on default: the greybox loads at power-on",
                              *(n for n in out.notes if not n.startswith("power-on loads"))))
    return out


def _ui2_card_commit(self: DemoCard, progress: Any = None) -> dict[str, Any]:
    board = self._e._board(self._bid)
    if not board.card:
        raise UnavailableError("user microSD", "no card in the USER microSD slot")
    ident = board.identity
    slot = "A" if board.card_slot == "B" else "B"
    if progress is not None:
        progress("card", 0, 412_160)
        progress("card", 412_160, 412_160)
    board.card_slot = slot
    board.ui2_card = {"cleared": False}
    return {"rm_id": ident.rm_id, "rm_name": ident.rm_name, "static_id": ident.shell_id,
            "slot": slot, "bytes": 412_160}


def _ui2_card_clear(self: DemoCard) -> Any:
    board = self._e._board(self._bid)
    if not board.card:
        raise UnavailableError("user microSD", "no card in the USER microSD slot")
    board.ui2_card = {"cleared": True}
    return self.status()


DemoCard.annotate = _ui2_card_annotate                    # type: ignore[method-assign]
DemoCard.commit = _ui2_card_commit                        # type: ignore[method-assign]
DemoCard.clear = _ui2_card_clear                          # type: ignore[method-assign]
# --- end ui2 api-build ---
