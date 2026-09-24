"""Lane P1: presence on the front panel (``core.panel`` + ``services.presence``), board-free.

docs/design/CLCD_ALIGNMENT.md §2 and david's decisions of 2026-09-24 (P1 a hello every 30 s,
90 s TTL, <= 4 sessions; P2 a lease-banner tap notifies and never releases). The service
runs on a fake clock against a fake panel adapter; nothing touches a socket. Every check has
a negative twin.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from harness_manager.core import capabilities as C
from harness_manager.core import panel as P
from harness_manager.core.errors import HeldError, UnavailableError
from harness_manager.core.events import EventBus
from harness_manager.core.panel import (
    Hello,
    HelloJob,
    HelloLease,
    PanelEvent,
    PanelSession,
    PanelState,
    PanelSupport,
    encode_hello,
    hello_message,
    order_sessions,
    touch_health,
)
from harness_manager.services import presence as S
from harness_manager.services.presence import PresenceService, hello_lease

REPO = Path(__file__).resolve().parents[2]
WALL = 1_790_000_000.0


def iso(t: float) -> str:
    return datetime.fromtimestamp(t, timezone.utc).isoformat()


def spike():
    """The CLCD-HM spike's model (tools/clcd_mock.py), loaded by path."""
    spec = importlib.util.spec_from_file_location("clcd_mock_p1", REPO / "tools" / "clcd_mock.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["clcd_mock_p1"] = mod
    spec.loader.exec_module(mod)
    return mod


# --- the hello: 256 B, capped fields, relative seconds ---------------------------------------

WORST = Hello(sid="f" * 32, who="x" * 80, app="harness-manager/10.20.30", name="n" * 64,
              role="holder",
              lease=HelloLease(by="y" * 80, left=10**9, q=12345, req="z" * 80, rl=10**6),
              job=HelloJob(kind="program-partition", percent=1000), ttl=100000)


def test_the_worst_case_hello_fits_the_harness_line_buffer():
    line = encode_hello(WORST)
    assert len(line) <= P.LINE_MAX == 256, len(line)
    assert len(line) == 251                     # the spike's figure, byte for byte
    msg = json.loads(line)
    assert len(msg["who"]) == P.WHO_MAX and len(msg["lease"]["req"]) == P.USER_MAX
    assert msg["lease"]["q"] == 99 and msg["job"]["p"] == 100 and msg["ttl"] == 300
    assert msg["lease"]["left"] == 86_400 and msg["lease"]["rl"] == 600


def test_without_the_caps_the_worst_case_would_not_fit(monkeypatch):
    """The twin: the caps are what keep it inside 256 B; widen them and it is refused."""
    for name in ("WHO_MAX", "USER_MAX", "NAME_MAX", "APP_MAX"):
        monkeypatch.setattr(P, name, 80)
    with pytest.raises(ValueError, match="at most 256"):
        encode_hello(WORST)


def test_the_hello_matches_the_spike_byte_for_byte():
    """One wire: the product's hello is the design spike's (docs/design/CLCD_ALIGNMENT.md §2.2)."""
    M = spike()
    want = M.hello_line(sid="f" * 32, who="x" * 80, app="harness-manager/10.20.30",
                        name="n" * 64, role="holder",
                        lease={"by": "y" * 80, "left": 10**9, "q": 12345, "req": "z" * 80,
                               "rl": 10**6},
                        job={"k": "program-partition", "p": 1000}, ttl=100000)
    assert encode_hello(WORST) == want
    short = Hello(sid="a1", who="bob@srv03340", app="hm")
    assert encode_hello(short) == M.hello_line(sid="a1", who="bob@srv03340", app="hm")
    assert encode_hello(short) != M.hello_line(sid="a1", who="bob@srv03340", app="hm",
                                               role="holder")


def test_an_oversized_field_is_capped_and_a_short_one_is_not():
    msg = hello_message(Hello(sid="a1b2c3d4", who="w" * 50, app="hm/0.1.0", name="n" * 40))
    assert msg["who"] == "w" * P.WHO_MAX and msg["name"] == "n" * P.NAME_MAX
    twin = hello_message(Hello(sid="a1b2c3d4", who="bob@srv03340", app="hm/0.1.0",
                               name="mps3-01"))
    assert twin["who"] == "bob@srv03340" and twin["name"] == "mps3-01"


def test_a_hello_is_printable_ascii_and_sends_only_the_user_part_of_a_principal():
    msg = hello_message(Hello(sid="a1", who="dévid@h\x1b[2J", app="hm",
                              lease=HelloLease(by="david@mapstone-dev", left=1)))
    assert msg["who"] == "d?vid@h?[2J", "non-ASCII and control characters never reach the panel"
    assert msg["lease"]["by"] == "david"
    assert hello_message(Hello(sid="a1", who="ok@h", app="hm"))["who"] == "ok@h"


def view(*, mine: bool, expires_in: float | None = 4332.0, queue: int = 1,
         request_in: float | None = 103.0, answered: bool = False) -> dict:
    lease = None if expires_in is None else {
        "target": "mps3_01_pl", "holder": "david@mapstone-dev", "user": "unix:david",
        "expires_at": iso(WALL + expires_in), "mine": mine}
    incoming = [] if request_in is None else [{
        "id": "r1", "by": "bob@srv03340", "deadline_at": iso(WALL + request_in),
        "answer": {"answer": "keep"} if answered else None}]
    return {"hub": "mapstone-dev", "lease": lease, "incoming": incoming, "request": None,
            "queue": [{"position": i + 1, "holder": f"q{i}@h"} for i in range(queue)]}


def test_lease_times_go_out_as_relative_seconds():
    role, lease, fast = hello_lease(view(mine=True), WALL)
    assert role == "holder" and fast
    msg = hello_message(Hello(sid="a1", who="david@srv03335", app="hm", role=role, lease=lease))
    assert msg["lease"] == {"by": "david", "left": 4332, "q": 1, "req": "bob", "rl": 103}
    line = encode_hello(Hello(sid="a1", who="d@h", app="hm", role=role, lease=lease)).decode()
    assert "2026" not in line and "T" not in line.replace("ttl", ""), "no absolute timestamps"


def test_an_expired_lease_is_zero_seconds_never_negative_and_standalone_sends_no_lease():
    _role, lease, _ = hello_lease(view(mine=False, expires_in=-50, request_in=-5), WALL)
    msg = hello_message(Hello(sid="a1", who="d@h", app="hm", lease=lease))
    assert msg["lease"]["left"] == 0 and msg["lease"]["rl"] == 0
    role, none, fast = hello_lease({"hub": None, "lease": None}, WALL)
    assert (role, none, fast) == ("owner", None, False)
    assert "lease" not in hello_message(Hello(sid="a1", who="d@h", app="hm", lease=none))


def test_behind_a_hub_with_nobody_holding_the_lease_says_so_and_a_watcher_is_a_watcher():
    role, lease, fast = hello_lease(view(mine=False, expires_in=None, queue=0, request_in=None),
                                    WALL)
    assert role == "watch" and not fast
    assert hello_message(Hello(sid="a", who="d@h", app="hm", lease=lease))["lease"] == {"q": 0}
    role, _lease, _ = hello_lease(view(mine=False), WALL)
    assert role == "watch", "the lease is someone else's"


def test_an_answered_request_is_not_relayed_as_open():
    _r, lease, fast = hello_lease(view(mine=True, answered=True), WALL)
    assert not fast and lease is not None and lease.req == ""


# --- the model -----------------------------------------------------------------------------


def test_sessions_are_ordered_holder_then_owner_then_watchers_most_recent_first():
    got = order_sessions([PanelSession("w1", "bob@a", "watch", 3.0),
                          PanelSession("o1", "carol@b", "owner", 40.0),
                          PanelSession("h1", "david@c", "holder", 20.0),
                          PanelSession("w2", "eve@d", "watch", 1.0)])
    assert [s.sid for s in got] == ["h1", "o1", "w2", "w1"]


def test_the_ordering_is_by_role_before_recency_and_unknown_roles_go_last():
    got = order_sessions([PanelSession("x", "m@a", "martian", 0.0),
                          PanelSession("w", "bob@a", "watch", 0.0),
                          PanelSession("h", "david@c", "holder", 80.0)])
    assert [s.sid for s in got] == ["h", "w", "x"], "a stale holder still outranks a fresh watcher"


def test_touch_health_is_read_when_present_and_unknown_when_absent():
    bad = touch_health({"ok": True, "touch_ok": False, "touch_bus_lost": 3,
                        "touch_recoveries": 2}, {"present": True, "cal": True})
    assert bad.ok is False and bad.reason == "touch unavailable (bus lost 3x, 2 recoveries)"
    assert bad.present is True
    unknown = touch_health({"ok": True}, None)
    assert unknown.ok is None and unknown.reason == "", "absent means unknown, never ok"
    assert touch_health({"touch_ok": True}).reason == ""


# --- the service ------------------------------------------------------------------------------


class Clock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


class FakePanel:
    """A panel adapter that records what the service asks of it."""

    def __init__(self, *, presence: str = "", locate: str = "") -> None:
        self.presence, self.locate_why = presence, locate
        self.reply = PanelState(page="status", owner="harness", count=1, seq=0)
        self.sent: list[Hello] = []
        self.offered: list[Hello] = []
        self.armed = None
        self.locates: list[tuple[int, str]] = []

    def support(self) -> PanelSupport:
        return PanelSupport(presence=self.presence, locate=self.locate_why)

    def hello(self, hello: Hello) -> PanelState:
        self.sent.append(hello)
        return self.reply

    def offer(self, hello: Hello, on_reply) -> None:
        self.offered.append(hello)
        self.armed = (hello, on_reply)

    def withdraw(self) -> bool:
        armed, self.armed = self.armed, None
        return armed is not None

    def ride(self) -> None:
        """Another connection carries the armed hello."""
        hello, on_reply = self.armed
        self.armed = None
        on_reply(self.reply)

    def state(self) -> PanelState:
        return self.reply

    def locate(self, seconds: int, who: str) -> float:
        self.locates.append((seconds, who))
        return WALL + seconds


class SpyLeases:
    """A lease service where anything that could hand the board over fails the test."""

    def __init__(self, view: dict | None = None, hook: bool = False) -> None:
        self._view = view
        self.notified: list[tuple] = []
        if hook:
            self.notify_holder = self._notify

    def view(self, hub):
        return self._view

    def _notify(self, board_id, hub, *, seq, at):
        self.notified.append((board_id, seq))
        return {"notified": True, "request": {"id": "r1", "by": "bob@srv03340"}}

    def release(self, *a, **k):
        raise AssertionError("a panel tap must never release the lease")

    respond = force = leave = release


def rig(*, panel: FakePanel | None = None, leases: SpyLeases | None = None, hub: bool = False,
        gate=None, ride_wait_s: float = 0.0):
    bus = EventBus()
    events: list = []
    bus.subscribe("panel.*", events.append)
    clock = Clock()
    panel = panel or FakePanel()
    session = SimpleNamespace(candidate=SimpleNamespace(name="mps3-01", board_id="b1"),
                              panel=panel, hub=object() if hub else None)
    svc = PresenceService(bus, lease_view=(lambda s: leases.view(s.hub)) if leases else None,
                          leases=leases, gate=gate, clock=clock, wall=lambda: WALL,
                          ride_wait_s=ride_wait_s, who="david@srv03335", app="hm/0.1.0")
    svc.track("b1", session)
    return svc, panel, clock, events, session


def topics(events, topic):
    return [e for e in events if e.topic == topic]


def test_a_linux_reply_updates_the_model_and_says_so_once():
    svc, panel, clock, events, _ = rig()
    svc.beat_due()
    assert len(panel.sent) == 1 and panel.sent[0].name == "mps3-01"
    assert panel.sent[0].role == "owner", "standalone: this session owns the board"
    assert svc.presence("b1")["active"] and svc.presence("b1")["sent"] == 1
    [ev] = topics(events, "panel.state")
    assert ev.data["page"] == "status" and ev.data["owner"] == "harness" and ev.data["count"] == 1
    clock.t += 30
    svc.beat_due()                      # the same reply again: no second panel.state
    assert len(panel.sent) == 2 and len(topics(events, "panel.state")) == 1
    panel.reply = PanelState(page="apps", owner="dut", count=2)
    clock.t += 30
    svc.beat_due()
    assert [e.data["owner"] for e in topics(events, "panel.state")] == ["harness", "dut"]


def test_hellos_go_every_30_s_not_more_often():
    svc, panel, clock, _events, _ = rig()
    svc.beat_due()
    clock.t += 29.9
    svc.beat_due()
    assert len(panel.sent) == 1, "29.9 s after the last one is too soon"
    clock.t += 0.2
    svc.beat_due()
    assert len(panel.sent) == 2


def test_the_beat_is_fast_while_a_lease_request_is_open():
    leases = SpyLeases(view(mine=True))
    svc, panel, clock, _events, _ = rig(leases=leases, hub=True)
    svc.beat_due()
    assert panel.sent[0].role == "holder" and panel.sent[0].lease.req == "bob@srv03340"
    clock.t += P.FAST_BEAT_S
    svc.beat_due()
    assert len(panel.sent) == 2
    leases._view = view(mine=True, request_in=None)          # the twin: nothing open
    clock.t += P.FAST_BEAT_S
    svc.beat_due()
    clock.t += P.FAST_BEAT_S
    svc.beat_due()
    assert len(panel.sent) == 3, "back to 30 s once the request is gone"


def test_no_hellos_once_the_board_is_closed():
    svc, panel, clock, _events, _ = rig()
    svc.beat_due()
    assert len(panel.sent) == 1
    svc.untrack("b1")
    for _ in range(5):
        clock.t += 30
        svc.beat_due()
    assert len(panel.sent) == 1 and svc.tracked() == []
    assert svc.presence("b1")["active"] is False


def test_a_board_without_presence_gets_no_hello_and_says_why():
    svc, panel, clock, _events, _ = rig(panel=FakePanel(presence="needs harness feature "
                                                                  "'presence' (Linux harness)"))
    for _ in range(3):
        svc.beat_due()
        clock.t += 30
    assert panel.sent == [] and panel.offered == []
    state = svc.presence("b1")
    assert not state["active"] and "Linux harness" in state["reason"]


def test_a_held_board_skips_the_beat_and_a_free_one_does_not():
    held = {"on": True}

    @contextmanager
    def gate(board_id):
        if held["on"]:
            raise HeldError("b1 is busy: deploy job j1 is running")
        yield

    svc, panel, clock, _events, _ = rig(gate=gate)
    svc.beat_due()
    assert panel.sent == [] and svc.presence("b1")["skipped"] == 1
    assert "skipped" in svc.presence("b1")["last_error"]
    held["on"] = False
    clock.t += S.RETRY_S
    svc.beat_due()
    assert len(panel.sent) == 1 and svc.presence("b1")["last_error"] == ""


def test_a_board_that_does_not_answer_is_tried_again_at_the_next_beat_not_sooner():
    from harness_manager.core.errors import UnreachableError

    class Deaf(FakePanel):
        def hello(self, hello):
            self.sent.append(hello)
            raise UnreachableError("shell at 10.0.0.1:6900 did not answer within 3.0s")

    svc, panel, clock, _events, _ = rig(panel=Deaf())
    svc.beat_due()
    clock.t += S.RETRY_S
    svc.beat_due()
    assert len(panel.sent) == 1, "a dead board is not hammered every 10 s"
    assert "did not answer" in svc.presence("b1")["last_error"]
    clock.t += P.BEAT_S
    svc.beat_due()
    assert len(panel.sent) == 2


def test_a_due_hello_rides_another_connection_first():
    svc, panel, clock, events, _ = rig(ride_wait_s=5.0)
    svc.beat_due()
    assert panel.sent == [] and len(panel.offered) == 1, "offered, not sent"
    panel.ride()                                   # e.g. the UI polled the board
    clock.t += 5.0
    svc.beat_due()
    assert panel.sent == [] and svc.presence("b1")["ridden"] == 1
    assert topics(events, "panel.state")


def test_with_no_other_connection_the_service_sends_the_hello_itself():
    svc, panel, clock, _events, _ = rig(ride_wait_s=5.0)
    svc.beat_due()
    clock.t += 4.9
    svc.beat_due()
    assert panel.sent == []
    clock.t += 0.2
    svc.beat_due()
    assert len(panel.sent) == 1 and panel.armed is None and svc.presence("b1")["sent"] == 1


# --- taps ------------------------------------------------------------------------------------


def with_taps(*taps: tuple[int, str, int]) -> PanelState:
    evs = tuple(PanelEvent(seq=s, on=on, ms_ago=ms, at=WALL - ms / 1000) for s, on, ms in taps)
    return PanelState(page="status", owner="harness", count=1,
                      seq=max([0, *(s for s, _, _ in taps)]), events=evs)


def test_each_tap_is_reported_once_by_seq():
    svc, panel, clock, events, _ = rig()
    panel.reply = with_taps((1, "nav", 1000))
    svc.beat_due()
    panel.reply = with_taps((1, "nav", 31000), (2, "identify", 500))
    clock.t += 30
    svc.beat_due()
    clock.t += 30
    svc.beat_due()                         # the same ring again: nothing new
    assert [e.data["seq"] for e in topics(events, "panel.tap")] == [1, 2]


def test_old_taps_are_not_news_on_first_contact_but_recent_ones_are():
    svc, panel, _clock, events, _ = rig()
    panel.reply = with_taps((5, "nav", 600_000), (6, "identify", 2000))
    svc.beat_due()
    assert [e.data["seq"] for e in topics(events, "panel.tap")] == [6]


def test_a_restarted_ring_does_not_silence_the_next_tap():
    svc, panel, clock, events, _ = rig()
    panel.reply = with_taps((40, "nav", 100))
    svc.beat_due()
    panel.reply = with_taps((1, "identify", 100))          # harnessd restarted: seq from 1
    clock.t += 30
    svc.beat_due()
    assert [e.data["seq"] for e in topics(events, "panel.tap")] == [40, 1]


def test_a_lease_banner_tap_notifies_the_holder_and_never_releases():
    leases = SpyLeases(view(mine=True))
    svc, panel, _clock, events, _ = rig(leases=leases, hub=True)
    panel.reply = with_taps((3, "request", 1200))
    svc.beat_due()                                  # SpyLeases fails the test on any release
    [tap] = topics(events, "panel.tap")
    assert tap.data["on"] == "request" and tap.data["notify"] == "holder"
    assert tap.data["request"] == {"id": "r1", "by": "bob@srv03340"}


def test_a_watchers_hm_shows_the_tap_but_does_not_claim_to_notify():
    leases = SpyLeases(view(mine=False))
    svc, panel, _clock, events, _ = rig(leases=leases, hub=True)
    panel.reply = with_taps((3, "request", 1200), (4, "nav", 1000))
    svc.beat_due()
    taps = topics(events, "panel.tap")
    assert [t.data["notify"] for t in taps] == ["", ""]


def test_the_lease_services_notify_hook_is_used_when_it_exists():
    leases = SpyLeases(view(mine=False), hook=True)      # CCR PANEL-1
    svc, panel, _clock, events, _ = rig(leases=leases, hub=True)
    panel.reply = with_taps((7, "request", 100), (8, "nav", 100))
    svc.beat_due()
    assert leases.notified == [("b1", 7)], "only the request tap goes to the lease service"
    assert [t.data["notify"] for t in topics(events, "panel.tap")] == ["holder", ""]


# --- the API's reads --------------------------------------------------------------------------


def test_read_says_identify_is_available_on_a_linux_board_and_why_not_on_bare_metal():
    svc, _panel, _clock, _events, session = rig()
    body = svc.read("b1", session)
    assert body["panel"].page == "status" and body["identify"] == {"available": True,
                                                                     "reason": "", "until": None}
    bare = FakePanel(presence="needs harness feature 'presence' (Linux harness)",
                     locate="needs harness feature 'locate' (Linux harness)")
    svc2, _p, _c, _e, session2 = rig(panel=bare)
    body = svc2.read("b1", session2)
    assert body["identify"]["available"] is False and "locate" in body["identify"]["reason"]


def test_identify_blinks_and_publishes_and_a_board_without_locate_refuses_with_the_reason():
    svc, panel, _clock, events, session = rig()
    out = svc.identify("b1", session, 10)
    assert out == {"until": WALL + 10, "seconds": 10} and panel.locates == [(10, "david@srv03335")]
    assert topics(events, "panel.locate")[0].data["state"] == "on"
    bare = FakePanel(locate="needs harness feature 'locate' (Linux harness)")
    svc2, panel2, _c, _e, session2 = rig(panel=bare)
    with pytest.raises(UnavailableError) as exc:
        svc2.identify("b1", session2, 10)
    assert exc.value.capability == C.LOCATE and "Linux harness" in exc.value.reason
    assert panel2.locates == []


def test_a_board_with_no_panel_adapter_answers_null_with_the_reason():
    session = SimpleNamespace(candidate=SimpleNamespace(name="", board_id="b"), panel=None)
    body = S.read_panel(session, reason_for=lambda cap: f"no Ethernet link ({cap})")
    assert body["panel"] is None and body["reason"] == "no Ethernet link (front_panel)"
    assert body["identify"]["reason"] == "no Ethernet link (locate)"


def test_identify_seconds_are_checked_before_the_board():
    assert S.check_seconds(None) == 10 and S.check_seconds(0) == 0 and S.check_seconds(30) == 30
    for bad in (31, -1, 2.5, True, "10"):
        with pytest.raises(Exception, match="seconds must be"):
            S.check_seconds(bad)
