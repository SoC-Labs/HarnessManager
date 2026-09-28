"""Lane LOCATE: Identify's rate limit, the lease note, ``who``, ``until_ms``, the MPS3 wire.

docs/design/BOARD_LOCATE.md. The board side is the Linux lead's confirmed ``locate`` (images
rc2_v7/v7n): ``{op, s, who}`` -> ``{ok, until_ms}``, the backlight at 2 Hz, the banner only while
the harness owns the panel, a tap stops it, no ``hello``/``panel``. Board-free: the presence
service over a recording panel, and the MPS3 adapter over ``PanelFakeShell``'s
``LINUX_LOCATE`` profile. Every check has its negative twin.
"""

from __future__ import annotations

import time
from types import SimpleNamespace

import pytest

from harness_manager.core import capabilities as C
from harness_manager.core.errors import AlreadyError, UnavailableError, UsageError
from harness_manager.core.events import EventBus
from harness_manager.core.panel import (
    LOCATE_MAX_MS,
    LOCATE_WHO_MAX,
    LOCATE_WHO_WIRE_MAX,
    PanelState,
    PanelSupport,
    locate_who,
)
from harness_manager.daemon.panel_api import identify_lease_note, lease_for_identify
from harness_manager.services import presence as S
from harness_manager.services.presence import LocateLimiter, PresenceService
from harness_manager_mps3.capabilities import NEEDS_LOCATE

WALL = 1_790_000_000.0
ME = "dam1n19@srv03335"


class Clock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


class Panel:
    """A panel adapter that records every locate as the board would get it."""

    def __init__(self, *, locate: str = "") -> None:
        self.why = locate
        self.locates: list[tuple[int, str]] = []

    def support(self) -> PanelSupport:
        return PanelSupport(locate=self.why)

    def locate(self, seconds: int, who: str) -> float:
        self.locates.append((seconds, who))
        return WALL + seconds

    def hello(self, _hello):
        return PanelState()


def rig(panel: Panel | None = None):
    bus = EventBus()
    events: list = []
    bus.subscribe("panel.*", events.append)
    clock = Clock()
    panel = panel or Panel()
    session = SimpleNamespace(candidate=SimpleNamespace(name="mps3-02", board_id="b2"),
                              panel=panel, hub=None)
    svc = PresenceService(bus, clock=clock, wall=lambda: WALL, ride_wait_s=0.0, who=ME,
                          app="hm/0.1.0")
    return svc, panel, clock, events, session


# --- the rate limit ------------------------------------------------------------------------------


def test_one_start_per_board_every_10_s_and_the_refusal_says_when():
    svc, panel, clock, _events, session = rig()
    out = svc.identify("b2", session, 5)
    assert out["seconds"] == 5 and out["next_at"] == WALL + 10
    clock.t += 4
    with pytest.raises(AlreadyError) as exc:
        svc.identify("b2", session, 5)
    assert exc.value.data["retry_after_s"] == 6.0 and "try again in 6 s" in exc.value.hint
    assert exc.value.data["next_at"] == WALL + 6
    assert [s for s, _w in panel.locates] == [5], "the refused one never reached the board"


def test_twin_after_10_s_another_board_or_a_stop_goes():
    svc, panel, clock, _events, session = rig()
    svc.identify("b2", session, 5)
    svc.identify("b3", session, 5)                    # another board: its own slot
    svc.identify("b2", session, 0)                    # a stop is never limited
    clock.t += 10
    svc.identify("b2", session, 5)
    assert [s for s, _w in panel.locates] == [5, 5, 0, 5]


def test_a_start_that_failed_gives_its_slot_back():
    svc, panel, _clock, _events, session = rig(Panel(locate="needs harness feature 'locate' "
                                                     "(Linux harness)"))
    with pytest.raises(UnavailableError):
        svc.identify("b2", session, 5)
    assert svc.limiter.wait_s("b2") == 0.0, "a refused start costs no wait"
    panel.why = ""
    svc.identify("b2", session, 5)                    # at once
    assert len(panel.locates) == 1


def test_twin_a_start_that_went_keeps_its_slot():
    limiter = LocateLimiter(10.0, clock=Clock(), wall=lambda: WALL)
    t = limiter.claim("b")
    limiter.release("b", t + 1)                       # not this claim: nothing given back
    assert limiter.wait_s("b") == 10.0
    with pytest.raises(AlreadyError):
        limiter.claim("b")


def test_the_default_is_5_s():
    assert S.IDENTIFY_DEFAULT_S == 5 and S.check_seconds(None) == 5


# --- who, and until_ms -------------------------------------------------------------------------


def test_who_says_harness_manager_and_fits_the_banner():
    assert locate_who("d@lab") == "d@lab via Harness Manager"
    assert locate_who(ME) == f"{ME} via HM", "36 characters would not fit: the short form"
    assert LOCATE_WHO_MAX == 30 and len(locate_who(ME)) <= 30


def test_twin_a_long_or_odd_who_is_clipped_never_overflowing():
    long = "a-very-long-user-name@a-very-long-host-name"
    assert locate_who(long) == long[:30] and "via" not in locate_who(long)
    assert locate_who("dé@lab") == "d?@lab via Harness Manager", "printable ASCII only"


def test_the_board_hears_who_via_harness_manager_and_the_answer_has_until_ms():
    svc, panel, _clock, events, session = rig()
    out = svc.identify("b2", session, 5)
    assert panel.locates == [(5, f"{ME} via HM")]
    assert out["until_ms"] == 5000 and out["until"] == WALL + 5
    loc = [e.data for e in events if e.topic == "panel.locate"]
    assert loc[0]["state"] == "on" and loc[0]["who"] == ME


def test_twin_a_stop_is_until_ms_0():
    svc, panel, _clock, _events, session = rig()
    out = svc.identify("b2", session, 0)
    assert out == {"until": WALL, "until_ms": 0, "seconds": 0, "next_at": WALL}
    assert panel.locates == [(0, f"{ME} via HM")]


# --- the lease: not needed, named when someone else has it -------------------------------------


class Leases:
    def __init__(self, view) -> None:
        self._view, self.calls = view, []

    def view(self, hub, *, cached_only=False, max_age_s=None):
        self.calls.append((cached_only, max_age_s))
        return self._view


def test_a_lease_held_elsewhere_is_named_in_the_answer():
    leases = Leases({"lease": {"holder": "alice@mapstone-dev", "mine": False}})
    holder = lease_for_identify(leases, SimpleNamespace(hub=object()))
    assert holder == "alice@mapstone-dev"
    assert leases.calls == [(True, 60.0)], "the cached view only: Identify never waits on the hub"
    extra = identify_lease_note(holder)
    assert extra["lease_holder"] == "alice@mapstone-dev"
    assert "Identify needs no lease" in extra["note"]


def test_twin_my_lease_a_free_one_no_view_or_no_hub_names_nobody():
    for view in ({"lease": {"holder": ME, "mine": True}}, {"lease": None}, None):
        assert lease_for_identify(Leases(view), SimpleNamespace(hub=object())) == ""
    assert lease_for_identify(Leases(None), SimpleNamespace(hub=None)) == ""
    assert lease_for_identify(None, SimpleNamespace(hub=object())) == ""
    assert identify_lease_note("") == {}


# --- the MPS3 wire: exactly the Linux lead's locate (rc2_v7/v7n) ---------------------------------


@pytest.fixture
def board(tmp_path):
    from harness_manager.core.services import EngineConfig
    from harness_manager.engine import Engine
    from harness_manager_mps3.pack import Mps3Pack
    from tests.fakes.clcd_panel_shell import LINUX_LOCATE, PanelVirtualMps3

    with PanelVirtualMps3(tmp_path / "lx", LINUX_LOCATE) as vb:
        engine = Engine(EngineConfig(state_dir=tmp_path / "state"),
                        packs={"mps3": Mps3Pack(console_ports=vb.console_ports)})
        session = engine.open(vb.candidate(), note="locate")
        yield vb, session
        engine.close_all()


def test_mps3_sends_op_s_who_only_and_takes_until_ms(board):
    vb, session = board
    until = session.panel.locate(5, locate_who(ME))
    assert vb.shell.locates == [{"op": "locate", "s": 5, "who": f"{ME} via HM"}]
    assert vb.shell.blinking and vb.shell.banner_text == f"IDENTIFY: {ME} via HM"
    assert 4.0 < until - time.time() <= 5.5


def test_twin_mps3_stop_sends_s_0_alone(board):
    vb, session = board
    session.panel.locate(5, ME)
    session.panel.locate(0, ME)
    assert vb.shell.locates[-1] == {"op": "locate", "s": 0} and not vb.shell.blinking


def test_the_rc2_image_has_locate_without_hello_or_panel(board):
    vb, session = board
    sup = session.panel.support()
    assert sup.locate == "" and "presence" in sup.presence and sup.source == "rebuilt"
    session.panel.locate(5, ME)
    assert not {"hello", "panel"} & {q.get("op") for q in vb.shell.requests}


def test_twin_bare_metal_is_refused_with_the_reason_and_sent_nothing(tmp_path):
    from harness_manager.core.services import EngineConfig
    from harness_manager.engine import Engine
    from harness_manager_mps3.pack import Mps3Pack
    from tests.fakes.clcd_panel_shell import V011_BARE_METAL, PanelVirtualMps3

    with PanelVirtualMps3(tmp_path / "bm", V011_BARE_METAL) as vb:
        engine = Engine(EngineConfig(state_dir=tmp_path / "state"),
                        packs={"mps3": Mps3Pack(console_ports=vb.console_ports)})
        session = engine.open(vb.candidate(), note="locate")
        try:
            with pytest.raises(UnavailableError) as exc:
                session.panel.locate(5, ME)
            assert exc.value.capability == C.LOCATE
            assert exc.value.reason == NEEDS_LOCATE
            assert "locate" not in {q.get("op") for q in vb.shell.requests}
        finally:
            engine.close_all()


# --- the fake is the board the Linux lead described ----------------------------------------------


def test_the_fake_a_tap_stops_it_and_harnessd_restart_restores_it(board):
    vb, session = board
    session.panel.locate(5, ME)
    vb.shell.tap("nav")
    assert not vb.shell.blinking and vb.shell.locate_tap_stops == 1
    session.panel.locate(5, ME)
    vb.shell.restart_harnessd()
    assert not vb.shell.blinking


def test_twin_the_fake_the_dut_owning_the_panel_blinks_the_backlight_only(board):
    vb, session = board
    vb.shell.display_owner = vb.shell.display_target = "dut"
    session.panel.locate(5, ME)
    assert vb.shell.blinking and vb.shell.banner_text == ""


# --- the demo's fake blink ------------------------------------------------------------------------


def test_the_demo_linux_board_blinks_with_the_banner_on_its_glass():
    from harness_manager.demo import DemoEngine
    from harness_manager.demo_showcase import BOARD_LINUX, DemoPanel

    engine = DemoEngine(speed=0.0, showcase=True)
    try:
        panel = DemoPanel(engine, BOARD_LINUX)
        panel.locate(5, "bob@lab-pc-03 via HM")
        frame = DemoPanel(engine, BOARD_LINUX).frame()      # a later session sees it too
        assert frame.rows[11].rstrip() == "IDENTIFY: bob@lab-pc-03 via HM"
        # PANEL-V017: the wire's letter for the harness's IDENTIFY banner, banner-busy ("t"),
        # never "i" (that is "ok" on the wire)
        assert frame.roles[400:520] == "t" * 120, "rows 10-12: the banner-busy role"
        assert {frame.role_at(r, 0) for r in (10, 11, 12)} == {"banner-busy"}
        assert panel.state().banner == "identify"
    finally:
        engine.close_all()


def test_twin_the_demo_board_after_a_stop_and_bare_metal_show_no_banner():
    from harness_manager.demo import DemoEngine
    from harness_manager.demo_showcase import BOARD_LINUX, BOARD_V011, DemoPanel

    engine = DemoEngine(speed=0.0, showcase=True)
    try:
        panel = DemoPanel(engine, BOARD_LINUX)
        panel.locate(5, ME)
        panel.locate(0, ME)
        assert "IDENTIFY" not in "".join(panel.frame().rows) and panel.state().banner == ""
        with pytest.raises(UnavailableError):
            DemoPanel(engine, BOARD_V011).locate(5, ME)
    finally:
        engine.close_all()


# --- V7-ALIGN: locate as SHIPPED (net-protocol v0.16, platform 18622e5, locate_linux.c) ---------


def test_v7_who_up_to_the_boards_32_reaches_it_unclipped(board):
    vb, session = board
    assert LOCATE_WHO_WIRE_MAX == 32 and LOCATE_WHO_MAX == 30, "32 on the wire, 30 on the glass"
    session.panel.locate(5, "w" * 32)
    assert vb.shell.locates[-1]["who"] == "w" * 32 and vb.shell.blinking


def test_v7_twin_a_longer_who_is_clipped_to_32_never_refused(board):
    vb, session = board
    session.panel.locate(5, "w" * 40 + "\t")
    assert vb.shell.locates[-1]["who"] == "w" * 32 and vb.shell.blinking
    # the board itself refuses 33, and anything not printable ASCII
    assert vb.shell.handle_control({"op": "locate", "s": 5, "who": "w" * 33}) == {
        "ok": False, "err": "invalid who: a string of <= 32 characters", "code": "invalid"}
    assert vb.shell.handle_control({"op": "locate", "s": 5, "who": "a\tb"})["code"] == "invalid"


def test_v7_until_ms_is_relative_ms_from_the_answer(board, monkeypatch):
    vb, session = board
    monkeypatch.setattr(vb.shell, "_op_locate",
                        lambda req: {"ok": True, "op": "locate", "until_ms": 3000})
    until = session.panel.locate(5, ME)
    assert 2.5 < until - time.time() <= 3.5, "the board said 3 s from now"


def test_v7_twin_an_absolute_until_ms_is_not_believed(board, monkeypatch):
    """A draft read until_ms as an absolute epoch. The shipped board never sends more than s
    seconds, so such a value is not a countdown of years: the asked s."""
    vb, session = board
    epoch_ms = int(time.time() * 1000) + 5000
    monkeypatch.setattr(vb.shell, "_op_locate",
                        lambda req: {"ok": True, "op": "locate", "until_ms": epoch_ms})
    until = session.panel.locate(5, ME)
    assert 4.5 < until - time.time() <= 5.5


def test_v7_locate_ms_the_rules():
    from harness_manager_mps3.panel import locate_ms

    assert LOCATE_MAX_MS == 30_000
    assert locate_ms({"until_ms": 3000}, 5) == 3000.0
    assert locate_ms({"until_ms": 0}, 0) == 0.0
    # twins: not believed -> s seconds
    for bad in (None, "3000", True, -1, 5001, 1_790_000_000_000):
        assert locate_ms({"until_ms": bad}, 5) == 5000.0, bad
    assert locate_ms({}, 5) == 5000.0 and locate_ms({"until_ms": 40_000}, 30) == 30_000.0


def test_v7_invalid_is_a_usage_error_and_the_features_are_kept(board, monkeypatch):
    vb, session = board
    monkeypatch.setattr(vb.shell, "_op_locate", lambda req: {
        "ok": False, "err": "invalid who: printable ASCII only", "code": "invalid"})
    with pytest.raises(UsageError, match="invalid who"):
        session.panel.locate(5, ME)
    assert session.panel._forgot is False, "HM sent something wrong: the image still has locate"


def test_v7_twin_not_supported_is_unavailable_and_the_features_are_read_again(board):
    vb, session = board
    vb.shell.locate_decline = "not_supported"          # a build without the panel
    with pytest.raises(UnavailableError, match="locate not supported") as exc:
        session.panel.locate(5, ME)
    assert exc.value.capability == C.LOCATE and session.panel._forgot is True


def test_v7_the_reply_carries_op_and_one_without_it_is_taken_too(board):
    vb, session = board
    assert vb.shell.handle_control({"op": "locate", "s": 2}) == {
        "ok": True, "op": "locate", "until_ms": 2000}
    vb.shell.locate_reply_op = False                   # a draft's reply
    until = session.panel.locate(5, ME)
    assert 4.0 < until - time.time() <= 5.5
