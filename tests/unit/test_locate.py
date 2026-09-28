"""Lane LOCATE: Identify's rate limit, the lease rule, the board's ring, the MPS3 wire.

docs/design/BOARD_LOCATE.md. Board-free: the presence service over a recording panel, and
the MPS3 adapter over the ``PanelFakeShell`` (which follows the design's wire). Every check
has its negative twin.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from harness_manager.core.errors import AlreadyError, UnavailableError
from harness_manager.core.events import EventBus
from harness_manager.core.panel import PanelEvent, PanelState, PanelSupport
from harness_manager.daemon.panel_api import identify_leds, lease_for_identify
from harness_manager.services import presence as S
from harness_manager.services.presence import LocateLimiter, PresenceService

WALL = 1_790_000_000.0
ME = "david@srv03335"


class Clock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


class Panel:
    """A panel adapter that records every locate (and the ``leds`` it was asked for)."""

    def __init__(self, *, locate: str = "", fail: Exception | None = None) -> None:
        self.why, self.fail = locate, fail
        self.locates: list[tuple[int, str, str]] = []
        self.reply = PanelState(page="status", owner="harness", count=1)

    def support(self) -> PanelSupport:
        return PanelSupport(locate=self.why)

    def locate(self, seconds: int, who: str, *, leds: str = "") -> float:
        if self.fail is not None:
            raise self.fail
        self.locates.append((seconds, who, leds))
        return WALL + seconds

    def hello(self, _hello):
        return self.reply


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
    assert [s for s, _w, _l in panel.locates] == [5], "the refused one never reached the board"


def test_twin_after_10_s_another_board_or_a_stop_goes():
    svc, panel, clock, _events, session = rig()
    svc.identify("b2", session, 5)
    svc.identify("b3", session, 5)                    # another board: its own slot
    svc.identify("b2", session, 0)                    # a stop is never limited
    clock.t += 10
    svc.identify("b2", session, 5)
    assert [s for s, _w, _l in panel.locates] == [5, 5, 0, 5]


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


# --- leds: never another person's DUT LEDs -----------------------------------------------------


class Leases:
    def __init__(self, view) -> None:
        self._view, self.calls = view, []

    def view(self, hub, *, cached_only=False, max_age_s=None):
        self.calls.append((cached_only, max_age_s))
        return self._view


def test_a_lease_held_elsewhere_blinks_led0_only_and_names_the_holder():
    leases = Leases({"lease": {"holder": "alice@mapstone-dev", "mine": False}})
    holder, known = lease_for_identify(leases, SimpleNamespace(hub=object()))
    assert (holder, known) == ("alice@mapstone-dev", True)
    assert leases.calls == [(True, 60.0)], "the cached view only: Identify never waits on the hub"
    leds, extra = identify_leds(holder, known)
    assert leds == "hb" and extra["lease_holder"] == "alice@mapstone-dev"
    assert "their DUT's LEDs are left alone" in extra["note"]


def test_twin_my_lease_a_free_one_or_no_hub_blinks_every_led():
    for view in ({"lease": {"holder": "david@srv03335", "mine": True}}, {"lease": None}):
        holder, known = lease_for_identify(Leases(view), SimpleNamespace(hub=object()))
        assert identify_leds(holder, known) == ("", {})
    assert lease_for_identify(Leases(None), SimpleNamespace(hub=None)) == ("", True)
    assert lease_for_identify(None, SimpleNamespace(hub=object())) == ("", True)


def test_a_lease_not_read_yet_is_treated_as_someone_elses():
    holder, known = lease_for_identify(Leases(None), SimpleNamespace(hub=object()))
    assert (holder, known) == ("", False)
    leds, extra = identify_leds(holder, known)
    assert leds == "hb" and "not been read yet" in extra["note"] and "lease_holder" not in extra


def test_leds_go_to_the_adapter_only_when_asked_for():
    svc, panel, clock, _events, session = rig()
    svc.identify("b2", session, 5, leds="hb")
    clock.t += 10
    svc.identify("b2", session, 5)
    assert [lds for _s, _w, lds in panel.locates] == ["hb", ""]

    class OldPanel(Panel):                           # an adapter written before LOCATE
        def locate(self, seconds, who):              # type: ignore[override]
            self.locates.append((seconds, who, "-"))
            return WALL + seconds

    svc2, old, _c, _e, session2 = rig(OldPanel())
    svc2.identify("b2", session2, 5)
    assert old.locates == [(5, ME, "-")]


# --- the board's ring: someone identified the board -------------------------------------------


def _reply(*events: PanelEvent) -> PanelState:
    return PanelState(page="status", owner="harness", count=1, seq=max(e.seq for e in events),
                      events=events)


def test_a_locate_in_the_ring_from_someone_else_is_a_panel_locate_for_the_holder():
    svc, panel, _clock, events, session = rig()
    svc.track("b2", session)
    panel.reply = _reply(PanelEvent(seq=1, kind="tap", on="nav", ms_ago=100, at=WALL))
    svc.beat_due()
    panel.reply = _reply(PanelEvent(seq=1, kind="tap", on="nav", ms_ago=100, at=WALL),
                         PanelEvent(seq=2, kind="locate", ms_ago=50, at=WALL - 0.05,
                                    who="bob@lab-pc-03"))
    svc.beat_due(now=svc._clock() + 31)
    loc = [e.data for e in events if e.topic == "panel.locate"]
    assert loc == [{"state": "on", "source": "board", "who": "bob@lab-pc-03", "mine": False,
                    "at": WALL - 0.05, "seq": 2}]
    assert not [e for e in events if e.topic == "panel.tap" and e.data["seq"] == 2], \
        "a locate is not a tap"


def test_twin_my_own_locate_in_the_ring_is_mine_and_a_tap_stays_a_tap():
    svc, panel, _clock, events, session = rig()
    svc.track("b2", session)
    panel.reply = _reply(PanelEvent(seq=1, kind="tap", on="request", ms_ago=10, at=WALL))
    svc.beat_due()
    panel.reply = _reply(PanelEvent(seq=1, kind="tap", on="request", ms_ago=10, at=WALL),
                         PanelEvent(seq=2, kind="locate", ms_ago=5, at=WALL, who=ME))
    svc.beat_due(now=svc._clock() + 31)
    loc = [e.data for e in events if e.topic == "panel.locate"]
    assert len(loc) == 1 and loc[0]["mine"] is True
    assert [e.data["on"] for e in events if e.topic == "panel.tap"] == ["request"]


# --- the MPS3 wire (the PanelFakeShell follows BOARD_LOCATE.md §2) --------------------------------


@pytest.fixture
def board(tmp_path):
    from harness_manager.core.services import EngineConfig
    from harness_manager.engine import Engine
    from harness_manager_mps3.pack import Mps3Pack
    from tests.fakes.clcd_panel_shell import LINUX_PANEL, PanelVirtualMps3

    with PanelVirtualMps3(tmp_path / "lx", LINUX_PANEL) as vb:
        engine = Engine(EngineConfig(state_dir=tmp_path / "state"),
                        packs={"mps3": Mps3Pack(console_ports=vb.console_ports)})
        session = engine.open(vb.candidate(), note="locate")
        yield vb, session
        engine.close_all()


def test_mps3_sends_leds_hb_only_when_asked_and_reads_who_from_the_ring(board):
    vb, session = board
    session.panel.locate(5, ME, leds="hb")
    assert vb.shell.locates[-1] == {"op": "locate", "s": 5, "who": ME, "leds": "hb"}
    state = session.panel.state()
    loc = [e for e in state.events if e.kind == "locate"]
    assert len(loc) == 1 and loc[0].who == ME and loc[0].on == ""


def test_twin_mps3_sends_no_leds_by_default_and_a_tap_names_nobody(board):
    vb, session = board
    session.panel.locate(5, ME)
    assert vb.shell.locates[-1] == {"op": "locate", "s": 5, "who": ME}
    vb.shell.tap("identify")
    taps = [e for e in session.panel.state().events if e.kind == "tap"]
    assert taps and all(e.who == "" for e in taps)


def test_the_boards_own_limit_is_already_with_the_wait_and_forgets_nothing(board):
    vb, session = board
    session.panel.locate(5, ME)
    before = session.panel._ident
    with pytest.raises(AlreadyError) as exc:
        session.panel.locate(5, "bob@lab-pc-03")      # another client, 0 s later
    assert "rate limited" in exc.value.message and 9 <= exc.value.data["retry_after_s"] <= 10
    assert session.panel._ident is before, "the feature is fine: nothing forgotten"
    assert len(vb.shell.locates) == 1


def test_twin_a_stop_is_never_limited_by_the_board(board):
    vb, session = board
    session.panel.locate(5, ME)
    session.panel.locate(0, ME)
    assert [q["s"] for q in vb.shell.locates] == [5, 0]


# --- the demo's fake blink ------------------------------------------------------------------------


def test_the_demo_linux_board_blinks_with_the_banner_on_its_glass():
    from harness_manager.demo import DemoEngine
    from harness_manager.demo_showcase import BOARD_LINUX, DemoPanel

    engine = DemoEngine(speed=0.0, showcase=True)
    try:
        panel = DemoPanel(engine, BOARD_LINUX)
        panel.locate(5, "bob@lab-pc-03")
        frame = DemoPanel(engine, BOARD_LINUX).frame()      # a later session sees it too
        assert "IDENTIFY" in frame.rows[10] and "asked by bob@lab-pc-03" in frame.rows[11]
        assert frame.roles[400:520] == "i" * 120, "rows 10-12 inverted"
        state = panel.state()
        assert state.banner == "identify"
        assert [(e.kind, e.who) for e in state.events if e.kind == "locate"] == [
            ("locate", "bob@lab-pc-03")]
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
