"""Lane LM2 (DISPLAY-MPS3): the MPS3 display adapter, end to end, board-free.

``harness_manager_mps3.display`` (``docs/design/LCD_MIRROR.md`` §7.1-§7.2) through the REAL
``claim.open_forward`` and ``tunnel.SshTunnel``, driven by the tunnel tests' ``FakeSsh``, with
LM1's ``FakeLcdMirror`` behind the forward and the REAL compositor (``DisplayService``) in
front. The harness is a stub ``shell.live()`` (pyverify's FakeShell refuses the engine name);
the claim is the real ``Mps3Claim`` with a pinned host key, its identify observation stubbed.

Every check has a negative twin: the lease holder only (someone else: refused by name, no
forward; yours: opens), lease loss closes (a lease change that is not an end does not), the
feature gate by NAME (a bit number never counts), each reason (bare metal, no engine, no
claim, no SSH), the forward released 30 s after the last viewer (not at 29 s), three failed
forwards stop (two then a good one does not).
"""

from __future__ import annotations

import dataclasses
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

import pytest

from harness_manager.core import capabilities as C
from harness_manager.core.capabilities import negotiate
from harness_manager.core.display import DisplayUnavailable
from harness_manager.core.errors import ExitCode, HeldError, UnavailableError, UnreachableError
from harness_manager.core.events import Event, EventBus
from harness_manager.core.model import Candidate, Link, LinkKind
from harness_manager.core.pack import BoardPack
from harness_manager.daemon.display_api import refusal
from harness_manager.services.display import DisplayService, DisplayTimings
from harness_manager_mps3 import claim as CL
from harness_manager_mps3 import display as D
from harness_manager_mps3 import tunnel as tunmod
from harness_manager_mps3.capabilities import NEEDS_LCD_MIRROR, SPECS
from harness_manager_mps3.pack import Mps3Pack
from harness_manager_mps3.shell import ShellLive
from tests.fakes import lm1_golden as G
from tests.fakes.l1_fake_ssh import FakeSsh
from tests.fakes.lc_fake_board_ssh import make_key_line
from tests.fakes.lm1_fake_lcd_mirror import FakeLcdMirror, FakePanel, ViewerModel, bind_ephemeral

BID = "mps3-01"
BOARD_HOST = "192.168.10.101"
HUB_HOST = "hub.test"
TARGET = "mps3_01_pl"
LCDM = 6940
BOARD_KEY = make_key_line("the-board")
BOARD_FP = CL.fingerprint(BOARD_KEY.split()[1])
#: The Linux image's bit names (net-protocol "version.features" bits 0-12, 0.15 order).
BITS = ("clcd", "clcd_kvm", "touch", "hwicap_fifo", "windowed", "dut_egress", "jtag_server",
        "xvc_dbgbr", "stats", "log", "reboot", "touch_cal")
ENGINE = (*BITS, "lcd_mirror")                      # the ENGINE name, after every bit name
BLOCK = {"port": LCDM, "mode": "sw", "proto": 1}    # version.lcd_mirror
FAST = DisplayTimings(grace_s=30.0, ping_s=0.1, stale_s=2.0, dead_s=5.0, tick_s=0.02,
                      hello_timeout_s=5.0, backoff_s=(0.2, 0.3, 0.4), refused_retry_s=0.3,
                      no_service_retry_s=0.4, dim_persist_s=0.2, fps_window_s=1.0)


def wait_for(pred: Callable[[], Any], timeout: float = 20.0, what: str = "") -> Any:
    deadline = time.monotonic() + timeout
    while True:
        v = pred()
        if v:
            return v
        if time.monotonic() > deadline:
            raise AssertionError(f"timed out waiting for {what or pred}")
        time.sleep(0.02)


# --- the rig ---------------------------------------------------------------------------------------


class StubShell:
    """What the adapter reads of ``session.shell``: ``live()``, ping + version with the raw
    reply (the ``impl`` key, ``version.lcd_mirror``)."""

    host, port = BOARD_HOST, 6900

    def __init__(self, *, impl: str = "linux", features: tuple[str, ...] = ENGINE,
                 block: dict[str, Any] | None = BLOCK) -> None:
        self.impl, self.features, self.block = impl, tuple(features), block
        self.reads = 0

    def live(self) -> ShellLive:
        self.reads += 1
        raw: dict[str, Any] = {"ok": True, "harness": "0.15.0", "features": list(self.features)}
        if self.block is not None:
            raw["lcd_mirror"] = dict(self.block)
        if self.impl != "bare-metal":
            raw["impl"] = self.impl                 # bare metal sends no impl key
        return ShellLive("0x44EE76D5", "0x00010001",
                         version=SimpleNamespace(ok=True, features=self.features), raw_version=raw)


@dataclass
class Session:
    """What ``Mps3Display`` and ``Mps3Claim`` read of an MPS3 session."""

    candidate: Candidate
    shell: Any
    hub: Any = None
    reach: Any = None
    tftp_port: int | None = None
    claim: Any = None
    display: Any = None


@dataclass
class Leases:
    """``LeaseService`` as the adapter asks it: ``view(hub)`` and ``forget(hub)``."""

    holder: str = "you@here"
    mine: bool = True
    held: bool = True
    fail: str = ""
    views: int = 0
    forgets: int = 0

    def forget(self, hub: Any) -> None:
        self.forgets += 1

    def view(self, hub: Any) -> dict[str, Any]:
        self.views += 1
        if self.fail:
            raise UnreachableError(self.fail)
        if not self.held:
            return {"lease": None}
        return {"lease": {"target": hub.target, "holder": self.holder, "mine": self.mine}}


class Clock:
    """A monotonic clock the test moves (the compositor's grace)."""

    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t

    def advance(self, s: float) -> None:
        self.t += s


@dataclass
class Rig:
    session: Session
    shell: StubShell
    ssh: FakeSsh
    leases: Leases
    bus: EventBus
    svc: DisplayService
    adapter: D.Mps3Display
    observed: dict[str, Any] = field(default_factory=dict)

    def serve(self, board: FakeLcdMirror, port: int = LCDM) -> None:
        """What the hub reaches as the board's 127.0.0.1:<port> is the fake mirror."""
        self.ssh.routes[("127.0.0.1", port)] = ("127.0.0.1", board.port)

    def status(self) -> dict[str, Any]:
        return self.svc.status(BID)

    def forward_up(self) -> bool:
        return self.adapter.tunnel_status() is not None and len(self.ssh.live()) == 1


def _candidate(via: str = "") -> Candidate:
    addr = f"{BOARD_HOST}:6900"
    cand = Candidate(pack="mps3", board_id=f"mps3@{addr}",
                     links=(Link(LinkKind.ETHERNET, addr, "shell control channel"),))
    return tunmod.with_via(cand, via) if via else cand


@pytest.fixture
def rig_factory(monkeypatch: pytest.MonkeyPatch) -> Any:
    made: list[Rig] = []

    def make(*, impl: str = "linux", features: tuple[str, ...] = ENGINE,
             block: dict[str, Any] | None = BLOCK, hub: bool = False, leases: Leases | None = None,
             pinned: bool = True, claim: bool = True, claimed: bool | None = True,
             reported_key: str = BOARD_FP, timings: DisplayTimings = FAST,
             clock: Callable[[], float] | None = None, share_leases: bool = True,
             seed: bool = True) -> Rig:
        fake_ssh = FakeSsh(hub_loopback=False)
        monkeypatch.setattr(tunmod, "DEFAULT_LAUNCHER", fake_ssh)
        monkeypatch.setattr(tunmod, "DEFAULT_SSH_G", fake_ssh.ssh_g)
        shell = StubShell(impl=impl, features=features, block=block)
        cand = _candidate(f"ssh:{HUB_HOST}" if hub else "")
        if seed:
            # What the probe read as the board opened (REVIEW-W5 4: the refusal path knows
            # the gate from this, never from a read of its own).
            cand = dataclasses.replace(cand, identity=SimpleNamespace(
                harness_impl=impl, features=tuple(features)))
        session = Session(cand, shell,
                          hub=SimpleNamespace(host=HUB_HOST, target=TARGET) if hub else None)
        observed: dict[str, Any] = {"claimed": claimed, "host_key": reported_key}
        if claim:
            session.claim = CL.Mps3Claim(session)
            monkeypatch.setattr(session.claim, "observe", lambda **_kw: CL.Observation(
                observed["claimed"], observed["host_key"], "identify (test)", time.time()))
        if pinned:
            CL.write_ssh_settings(session.candidate, {"host_key": BOARD_KEY})
        lease_svc = leases or Leases()
        bus = EventBus()
        kw: dict[str, Any] = {"clock": clock} if clock is not None else {}
        svc = DisplayService(bus, timings=timings, leases=lease_svc, **kw)
        adapter = D.display_adapter(session)
        assert isinstance(adapter, D.Mps3Display)
        if share_leases:                        # else the display service hands them on attach
            adapter.use_leases(lease_svc)
        rig = Rig(session, shell, fake_ssh, lease_svc, bus, svc, adapter, observed)
        made.append(rig)
        return rig

    yield make
    for r in made:
        r.svc.shutdown()
        r.adapter.close()
        r.ssh.close()


def harness_board(name: str = "boot", **kw: Any) -> tuple[FakeLcdMirror, bytes]:
    model, grid, regs = G.harness_pictures()[name]
    return FakeLcdMirror(FakePanel(model, regs=regs), mode="sw", **kw), grid


def shows(vm: ViewerModel, viewer: Any, grid: bytes) -> bool:
    vm.pump(viewer)
    return bytes(vm.frame) == grid


# --- 1. the full path: FakeSsh + FakeLcdMirror behind claim.open_forward -----------------------------


def test_the_full_path_is_exact_through_the_claims_forward(rig_factory: Any) -> None:
    rig = rig_factory()
    board, grid = harness_board()
    with board:
        rig.serve(board)
        assert rig.adapter.display_reason() == ""
        vm = ViewerModel()
        v = rig.svc.attach(BID, rig.adapter)
        wait_for(lambda: shows(vm, v, grid), what="the golden harness picture")
        st = rig.status()
        assert st["state"] == "live" and st["mode"] == "sw" and st["hello"]["static_id"]
        # the forward is claim.open_forward's: -L to the board's LOOPBACK 6940, root, the pin
        argv = rig.ssh.launches[0]
        local = rig.adapter.tunnel_status()["forwards"][D.FORWARD]["local"]
        assert argv[-1] == BOARD_HOST and argv[argv.index("-l") + 1] == "root"
        assert argv[argv.index("-L") + 1] == f"127.0.0.1:{local}:127.0.0.1:{LCDM}"
        for opt in ("ControlPath=none", "ExitOnForwardFailure=yes", "BatchMode=yes",
                    "StrictHostKeyChecking=yes", f"HostKeyAlias={CL.host_key_alias(rig.session.candidate.board_id)}"):
            assert opt in argv
        assert "-J" not in argv                           # a board on this LAN: no jump
        # a stream that drops reconnects over the SAME forward: one ssh, one local port
        board.drop()
        wait_for(lambda: rig.status()["counters"]["connects"] >= 2
                 and rig.status()["state"] == "live", what="the reconnect")
        assert len(rig.ssh.launches) == 1 and rig.adapter.opens == 1
        assert rig.adapter.tunnel_status()["forwards"][D.FORWARD]["local"] == local
        v.close()


@pytest.mark.parametrize("block, port", [({"port": 6941, "mode": "hw", "proto": 1}, 6941),
                                         (None, LCDM)])
def test_the_port_is_version_lcd_mirror_port_else_6940(rig_factory: Any, block: Any,
                                                       port: int) -> None:
    rig = rig_factory(block=block)
    with FakeLcdMirror() as board:
        rig.serve(board, port)
        sock = rig.adapter.display_connect()
        try:
            assert sock.recv(2) == b"LM"                   # HELLO's framing: the mirror answered
        finally:
            sock.close()
    spec = rig.ssh.launches[0][rig.ssh.launches[0].index("-L") + 1]
    assert spec.endswith(f":127.0.0.1:{port}")
    assert rig.adapter.facts().port == port and rig.adapter.facts().source == "version"


# --- 2. D3: the lease holder only ----------------------------------------------------------------------


def test_someone_elses_lease_is_refused_by_name_and_no_forward_opens(rig_factory: Any) -> None:
    rig = rig_factory(hub=True, leases=Leases(holder="alice@lab", mine=False))
    with FakeLcdMirror() as board:
        rig.serve(board)
        reason = rig.adapter.display_reason()
        assert "lease holder only" in reason and f"alice@lab holds {TARGET}" in reason
        with pytest.raises(HeldError) as exc:
            rig.adapter.display_connect()
        assert exc.value.holder == "alice@lab" and rig.leases.forgets >= 1   # asked fresh
        v = rig.svc.attach(BID, rig.adapter)
        wait_for(lambda: rig.status()["state"] == "down" and "alice@lab" in rig.status()["reason"],
                 what="down, naming the holder")
        v.close()
        assert rig.ssh.launches == [] and board.stats["connects"] == 0     # never opened


def test_negative_twin_your_lease_opens_through_the_hub(rig_factory: Any) -> None:
    # the display service hands its lease service to the adapter on attach (the hub API's)
    rig = rig_factory(hub=True, leases=Leases(holder="you@here", mine=True), share_leases=False)
    board, grid = harness_board()
    with board:
        rig.serve(board)
        vm = ViewerModel()
        v = rig.svc.attach(BID, rig.adapter)
        wait_for(lambda: shows(vm, v, grid), what="the picture for the holder")
        assert rig.adapter.display_reason() == ""
        argv = rig.ssh.launches[0]
        assert argv[argv.index("-J") + 1] == HUB_HOST
        assert rig.leases.forgets >= 1 and rig.leases.views >= 2       # asked fresh, then cached
        v.close()


def test_nobodys_lease_and_an_unanswering_hub_open_nothing(rig_factory: Any) -> None:
    rig = rig_factory(hub=True, leases=Leases(held=False))
    assert f"nobody holds {TARGET}" in rig.adapter.display_reason()
    with pytest.raises(HeldError):
        rig.adapter.display_connect()
    rig.leases.held, rig.leases.fail = True, "the hub did not answer"
    assert rig.adapter.display_reason().startswith(f"cannot confirm you hold the lease on {TARGET}")
    with pytest.raises(DisplayUnavailable) as exc:
        rig.adapter.display_connect()
    assert exc.value.retry_s == D.LEASE_RETRY_S                   # a transient: asked again later
    assert rig.ssh.launches == []


def test_negative_twin_a_board_with_no_hub_has_no_lease_to_hold(rig_factory: Any) -> None:
    rig = rig_factory(hub=False, leases=Leases(holder="alice@lab", mine=False))
    with FakeLcdMirror() as board:
        rig.serve(board)
        assert rig.adapter.display_reason() == ""
        rig.adapter.display_connect().close()
    assert rig.leases.views == 0 and len(rig.ssh.launches) == 1


# --- 3. lease loss (and the board closing) closes the display --------------------------------------------


@pytest.mark.parametrize("state", ["released", "expired", "lost"])
def test_lease_loss_closes_the_display_and_its_forward(rig_factory: Any, state: str) -> None:
    rig = rig_factory(hub=True)
    board, grid = harness_board()
    with board:
        rig.serve(board)
        vm = ViewerModel()
        v = rig.svc.attach(BID, rig.adapter)
        wait_for(lambda: shows(vm, v, grid) and rig.forward_up(), what="live")
        rig.leases.mine, rig.leases.holder = False, "alice@lab"            # the hub moved on
        rig.bus.publish(Event("lease.state", BID, {"target": TARGET, "state": state,
                                                   "holder": "you@here", "expires_at": ""}))
        wait_for(lambda: rig.status()["state"] == "down", what="down")
        assert rig.status()["reason"] == f"closed: the lease was {state}"
        wait_for(lambda: rig.adapter.tunnel_status() is None and rig.ssh.live() == []
                 and board.clients == 0, what="the forward closed")
        # a viewer that comes back is asked again, and refused: no second forward
        v2 = rig.svc.attach(BID, rig.adapter)
        wait_for(lambda: "alice@lab" in rig.status()["reason"], what="refused by name")
        assert len(rig.ssh.launches) == 1
        v.close()
        v2.close()


@pytest.mark.parametrize("state", ["held", "queued"])
def test_negative_twin_a_lease_change_that_is_not_an_end_keeps_it_open(rig_factory: Any,
                                                                      state: str) -> None:
    rig = rig_factory(hub=True)
    board, grid = harness_board()
    with board:
        rig.serve(board)
        vm = ViewerModel()
        v = rig.svc.attach(BID, rig.adapter)
        wait_for(lambda: shows(vm, v, grid) and rig.forward_up(), what="live")
        rig.bus.publish(Event("lease.state", BID, {"target": TARGET, "state": state,
                                                   "holder": "you@here", "expires_at": ""}))
        time.sleep(0.5)
        for w in rig.svc.threads:
            w.join(timeout=5)
        assert rig.svc.threads == [] and rig.status()["state"] == "live" and rig.forward_up()
        v.close()


def test_the_board_closing_closes_the_display_and_refuses_a_reconnect(rig_factory: Any) -> None:
    rig = rig_factory()
    board, grid = harness_board()
    with board:
        rig.serve(board)
        vm = ViewerModel()
        v = rig.svc.attach(BID, rig.adapter)
        wait_for(lambda: shows(vm, v, grid) and rig.forward_up(), what="live")
        rig.adapter.close()                                   # Mps3Session.close() calls this
        rig.bus.publish(Event("session.closed", BID, {"pack": "mps3"}))
        wait_for(lambda: rig.status()["state"] == "down" and rig.ssh.live() == [],
                 what="down, the forward gone")
        with pytest.raises(DisplayUnavailable) as exc:
            rig.adapter.display_connect()
        assert exc.value.reason == D.CLOSED and exc.value.retry_s is None
        assert rig.adapter.display_reason() == D.CLOSED
        v.close()


def test_the_pack_session_holds_one_adapter_and_closing_it_drops_the_forward() -> None:
    pack = Mps3Pack()
    s = bind_ephemeral()                                      # nothing is asked at open
    cand = pack.candidate_for_host(f"127.0.0.1:{s.getsockname()[1]}")
    session = pack.open(cand)
    try:
        assert isinstance(session.display, D.Mps3Display)
        assert pack.display_adapter(session) is session.display
        assert D.display_adapter(session) is session.display           # made once per session
        assert BoardPack.display_adapter(pack, session) is session.display
    finally:
        session.close()
        s.close()
    assert session.display.display_reason() == D.CLOSED
    # twin: a session with no Ethernet shell has no display adapter
    bare = Session(_candidate(), shell=None)
    assert D.display_adapter(bare) is None and D.make_display_adapter(bare) is None
    assert BoardPack.display_adapter(pack, bare) is None


# --- 4. the feature gate, by name ---------------------------------------------------------------------


def test_the_capability_is_registered_and_gated_by_the_engine_name() -> None:
    spec = next(s for s in SPECS if s.name == C.DISPLAY_MIRROR)
    assert spec.title == "Live display" and spec.needs_hint == NEEDS_LCD_MIRROR
    for links in ([LinkKind.ETHERNET], [LinkKind.HUB], [LinkKind.SSH]):
        available, _ = negotiate(SPECS, links, ENGINE)
        assert C.DISPLAY_MIRROR in available


@pytest.mark.parametrize("features", [(*BITS, "13"), tuple(str(i) for i in range(14)),
                                      (*BITS, "bit13"), BITS])
def test_negative_twin_a_bit_number_is_never_the_engine(rig_factory: Any,
                                                        features: tuple[str, ...]) -> None:
    _, why = negotiate(SPECS, [LinkKind.ETHERNET], features)
    assert why[C.DISPLAY_MIRROR] == NEEDS_LCD_MIRROR
    rig = rig_factory(features=features, block=BLOCK)          # even with version.lcd_mirror
    assert rig.adapter.display_reason() == D.NO_ENGINE
    with pytest.raises(DisplayUnavailable) as exc:
        rig.adapter.display_connect()
    assert exc.value.reason == D.NO_ENGINE and exc.value.retry_s is None
    assert rig.ssh.launches == []


# --- 5. the reasons: bare metal, no engine, no claim, no SSH -----------------------------------------------


REASONS = [
    ("bare-metal", {"impl": "bare-metal", "features": BITS, "block": None},
     "needs the Linux harness with lcd_mirror (this board runs the bare-metal harness)"),
    ("bare-metal-named", {"impl": "bare-metal", "features": ENGINE},
     "needs the Linux harness with lcd_mirror"),
    ("no-engine", {"features": BITS, "block": None}, "this harness image has no lcd_mirror"),
    ("unclaimed", {"pinned": False}, D.CLAIM_HINT),
    ("unclaimed-now", {"claimed": False}, "the board is unclaimed now; " + D.CLAIM_HINT),
    ("no-ssh", {"claim": False}, D.NO_SSH),
    ("host-key", {"reported_key": "SHA256:" + "A" * 43}, "THE BOARD'S SSH HOST KEY CHANGED"),
]


@pytest.mark.parametrize("name, kw, reason", REASONS, ids=[r[0] for r in REASONS])
def test_each_reason_refuses_before_any_forward(rig_factory: Any, name: str, kw: dict[str, Any],
                                                reason: str) -> None:
    rig = rig_factory(**kw)
    got = rig.adapter.display_reason()
    assert got.startswith(reason), got
    with pytest.raises(DisplayUnavailable) as exc:
        rig.adapter.display_connect()
    assert exc.value.reason == got and exc.value.retry_s is None
    v = rig.svc.attach(BID, rig.adapter)
    wait_for(lambda: rig.status()["state"] == "down" and rig.status()["reason"] == got,
             what="down with the reason")
    v.close()
    assert rig.ssh.launches == []
    if name.startswith("unclaimed") or name == "no-ssh":
        assert "harness-manager board claim TARGET" in got               # the claim hint


def test_a_host_key_changed_back_to_one_pinned_before_is_said_plainly(rig_factory: Any) -> None:
    """SMALL-4: the Linux harness's /persist (the user microSD, or tmpfs) flips its host key."""
    card_fp = CL.fingerprint(make_key_line("the-card-persist").split()[1])
    rig = rig_factory(reported_key=card_fp)
    CL.ClaimRecords().update(rig.session.candidate.board_id, host_key_fp=BOARD_FP,
                             at="2026-09-25T08:00:00Z", host_keys_seen=[
                                 {"fp": card_fp, "first": "2026-09-20T08:00:00Z",
                                  "last": "2026-09-24T09:30:00Z"},
                                 {"fp": BOARD_FP, "first": "2026-09-25T08:00:00Z",
                                  "last": "2026-09-25T08:00:00Z"}])
    got = rig.adapter.display_reason()
    assert got.startswith("host key changed back to one seen on 2026-09-24"), got
    assert "/persist (the user microSD) mounting or not" in got
    assert "`harness-manager board claim TARGET --adopt` if you trust it" in got
    with pytest.raises(DisplayUnavailable) as exc:             # never opened: no auto-accept
        rig.adapter.display_connect()
    assert exc.value.reason == got and rig.ssh.launches == []
    # the twin: a key never pinned here keeps the loud reason
    rig.observed["host_key"] = "SHA256:" + "A" * 43
    assert rig.adapter.display_reason().startswith("THE BOARD'S SSH HOST KEY CHANGED")


def test_negative_twin_a_claimed_linux_board_with_the_engine_has_no_reason(rig_factory: Any) -> None:
    rig = rig_factory()
    assert rig.adapter.display_reason() == ""
    assert rig.adapter.facts().engine and rig.adapter.display_facts()["reach"] == "board-ssh"


# --- 5b. the gate is answered before the lease (``display_api.refusal``, SMALL-4) -------------------


GATES = [
    ("bare-metal", {"impl": "bare-metal", "features": BITS, "block": None},
     D.NEEDS_LINUX.format(impl="bare-metal")),
    ("no-engine", {"features": BITS, "block": None}, D.NO_ENGINE),
]


@pytest.mark.parametrize("name, kw, gate", GATES, ids=[g[0] for g in GATES])
def test_a_board_that_can_never_show_it_is_unavailable_whoever_holds_the_lease(
        rig_factory: Any, name: str, kw: dict[str, Any], gate: str) -> None:
    rig = rig_factory(hub=True, leases=Leases(holder="alice@lab", mine=False), **kw)
    assert rig.adapter.display_gate() == gate
    assert rig.adapter.display_reason() == gate                  # still its first reason
    views = rig.leases.views
    err = refusal(rig.adapter, rig.session, rig.leases)
    assert isinstance(err, UnavailableError), err                # 422, not 409 naming alice
    assert err.code == ExitCode.UNAVAILABLE and err.reason == gate and "alice" not in err.message
    assert rig.leases.views == views                             # the lease was not asked
    assert rig.ssh.launches == []


def test_negative_twin_a_board_that_could_show_it_is_held_then_the_claim(rig_factory: Any) -> None:
    rig = rig_factory(hub=True, leases=Leases(holder="alice@lab", mine=False))
    assert rig.adapter.display_gate() == ""                      # the gate is not the lease
    err = refusal(rig.adapter, rig.session, rig.leases)
    assert isinstance(err, HeldError) and err.holder == "alice@lab"
    assert err.code == ExitCode.HELD
    # the lease is ours, no SSH to the board: the third step, UNAVAILABLE with the claim hint
    rig2 = rig_factory(hub=True, leases=Leases(holder="you@here", mine=True), claim=False)
    err = refusal(rig2.adapter, rig2.session, rig2.leases)
    assert isinstance(err, UnavailableError) and err.reason == D.NO_SSH
    # a harness whose features are not known is not "never": the lease decides (HELD), and
    # nothing reads the board to find out (REVIEW-W5 4)
    rig3 = rig_factory(hub=True, leases=Leases(holder="alice@lab", mine=False))
    rig3.session.shell = None
    assert rig3.adapter.display_gate() == ""
    assert "alice@lab holds" in rig3.adapter.display_reason()
    err = refusal(rig3.adapter, rig3.session, rig3.leases)
    assert isinstance(err, HeldError) and err.holder == "alice@lab"
    assert rig.ssh.launches == [] and rig2.ssh.launches == [] and rig3.ssh.launches == []


# --- 5c. the refusal never reads the board (REVIEW-W5 4) ---------------------------------------------


def _seeded_rig(rig_factory: Any, clock: Clock, **kw: Any) -> Rig:
    """A Linux board with lcd_mirror whose identity seeds the facts, on a clock we move."""
    rig = rig_factory(hub=True, clock=clock, **kw)
    rig.adapter._clock = clock
    return rig


def test_a_refusal_with_stale_facts_never_reads_the_board(rig_factory: Any) -> None:
    """Someone else holds the lease and the cached facts are older than FACTS_TTL_S. Before
    the fix ``facts()`` read ``version`` on 6900 (the single-client port) on the way to the
    refusal; now the refusal uses what is known (not known is not never)."""
    clock = Clock()
    rig = _seeded_rig(rig_factory, clock, leases=Leases(holder="alice@lab", mine=False))
    assert rig.adapter.display_gate() == ""                   # seeds the cache, no read
    clock.advance(D.FACTS_TTL_S + 60)                         # the cache is stale now
    err = refusal(rig.adapter, rig.session, rig.leases)
    assert isinstance(err, HeldError) and err.holder == "alice@lab"
    assert rig.adapter.display_reason() and rig.adapter.display_facts()["engine"] is True
    assert rig.shell.reads == 0, "the refusal read the board"
    rig.session.candidate = dataclasses.replace(rig.session.candidate, identity=None)
    rig.adapter._facts = None                                 # nothing known at all
    assert refusal(rig.adapter, rig.session, rig.leases).holder == "alice@lab"
    assert rig.adapter.display_gate() == "" and rig.shell.reads == 0
    with pytest.raises(HeldError):
        rig.adapter.display_connect()                         # the lease first: still no read
    assert rig.shell.reads == 0 and rig.ssh.launches == []


def test_twin_the_holders_open_reads_version_after_the_lease_check(rig_factory: Any) -> None:
    """The live ``version`` read belongs to ``_open_forward``, after the lease: the holder's
    open still reads it (the port, the mode), fresh, once."""
    clock = Clock()
    rig = _seeded_rig(rig_factory, clock)
    clock.advance(D.FACTS_TTL_S + 60)
    assert refusal(rig.adapter, rig.session, rig.leases) is None and rig.shell.reads == 0
    with FakeLcdMirror() as board:
        rig.serve(board)
        rig.adapter.display_connect().close()
    assert rig.shell.reads == 1 and rig.adapter.facts().source == "version"
    assert rig.leases.forgets >= 1                            # the lease asked fresh first


# --- 6. the forward is released 30 s after the last viewer (LM1's grace, an injected clock) -----------------


def test_the_forward_is_released_30_s_after_the_last_viewer(rig_factory: Any) -> None:
    clock = Clock()
    t = DisplayTimings(ping_s=0.5, stale_s=1e6, dead_s=1e6, tick_s=0.02, hello_timeout_s=1e6,
                       backoff_s=(0.2,))
    assert t.grace_s == 30.0                                  # the product's grace, unchanged
    rig = rig_factory(timings=t, clock=clock)
    board, grid = harness_board()
    with board:
        rig.serve(board)
        vm = ViewerModel()
        v = rig.svc.attach(BID, rig.adapter)
        wait_for(lambda: shows(vm, v, grid) and rig.forward_up(), what="live")
        v.close()
        clock.advance(29.0)                                   # twin: inside the grace, kept
        time.sleep(0.5)
        assert rig.forward_up() and board.clients == 1 and rig.status()["state"] == "live"
        v2 = rig.svc.attach(BID, rig.adapter)                 # ... and a returning viewer reuses it
        vm2 = ViewerModel()
        wait_for(lambda: shows(vm2, v2, grid), what="the reused upstream")
        v2.close()
        clock.advance(29.0)
        time.sleep(0.3)
        assert rig.forward_up()
        clock.advance(1.5)                                    # 30.5 s after the last viewer left
        wait_for(lambda: rig.adapter.tunnel_status() is None and rig.ssh.live() == [],
                 what="the forward released")
        assert rig.status()["state"] == "down" and rig.status()["reason"] == "closed: no viewer for 30 s"
        assert len(rig.ssh.launches) == 1
        v3 = rig.svc.attach(BID, rig.adapter)                 # the next viewer opens a new one
        vm3 = ViewerModel()
        wait_for(lambda: shows(vm3, v3, grid), what="a new forward")
        assert len(rig.ssh.launches) == 2 and rig.adapter.opens == 2
        v3.close()


# --- 7. failures: no service, and three forwards that do not come up ------------------------------------


def test_an_image_without_the_service_is_down_with_ssh_s_reason(rig_factory: Any) -> None:
    rig = rig_factory()
    dead = bind_ephemeral()                                   # nothing listens behind it
    rig.ssh.routes[("127.0.0.1", LCDM)] = ("127.0.0.1", dead.getsockname()[1])
    dead.close()
    v = rig.svc.attach(BID, rig.adapter)
    wait_for(lambda: rig.status()["state"] == "down" and rig.status()["reason"], what="down")
    reason = rig.status()["reason"]
    assert reason.startswith("no lcd_mirror service on the board") and "open failed" in reason
    v.close()


def test_three_forwards_that_do_not_come_up_stop_with_ssh_s_reason(rig_factory: Any) -> None:
    rig = rig_factory()
    rig.ssh.fail = "auth"                                     # the board refuses the key
    v = rig.svc.attach(BID, rig.adapter)
    wait_for(lambda: rig.status()["state"] == "down"
             and "did not come up" in rig.status()["reason"], what="stopped")
    reason = rig.status()["reason"]
    assert f"({D.OPEN_FAILURES_MAX} tries)" in reason and "Permission denied" in reason
    assert len(rig.ssh.launches) == D.OPEN_FAILURES_MAX
    v.close()


def test_negative_twin_two_failures_then_a_good_forward_goes_live(rig_factory: Any) -> None:
    rig = rig_factory()
    board, grid = harness_board()
    with board:
        rig.serve(board)
        rig.ssh.fail = "auth"
        vm = ViewerModel()
        v = rig.svc.attach(BID, rig.adapter)
        wait_for(lambda: len(rig.ssh.launches) >= 2, what="two refused forwards")
        rig.ssh.fail = ""                                     # the key works again
        wait_for(lambda: shows(vm, v, grid), what="live after two failures")
        assert rig.adapter._failures == 0 and len(rig.ssh.launches) == 3
        v.close()
