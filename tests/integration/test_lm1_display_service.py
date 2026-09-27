"""The live display compositor against FakeLcdMirror (docs/design/LCD_MIRROR.md §7.2-§7.3;
lane LM1). Every behaviour the transport spike checked, as collected tests, on loopback
with shortened clocks: KEY on start, whole keyframes, ACK (H1), snap_last assembly, a seq
gap, refusal of a third client, no service, drop and reclaim, sw-blind, the handover hatch,
RATE clamp and max(viewers), per-viewer ack and dirty sets, grace close, close on demand,
stale/reconnect, PNG.

A viewer here is ``ViewerModel`` (what a browser holds): after every message it applies,
its picture is checked against the board's.
"""

from __future__ import annotations

import collections
import json
import os
import threading
import time
import zlib
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from harness_manager.core import display_wire as w
from harness_manager.core.display import DisplayUnavailable
from harness_manager.core.errors import HarnessError, UnavailableError
from harness_manager.core.events import EventBus
from harness_manager.services.display import DisplayService, DisplayTimings
from tests.fakes import lm1_golden as G
from tests.fakes.lm1_fake_lcd_mirror import (
    CardAnimator,
    FakeLcdMirror,
    FakePanel,
    NoiseAnimator,
    ViewerModel,
    crc_valid,
)

FAST = DisplayTimings(grace_s=0.6, ping_s=0.1, stale_s=2.0, dead_s=5.0, tick_s=0.02,
                      hello_timeout_s=5.0, backoff_s=(0.05, 0.1, 0.2), refused_retry_s=0.3,
                      no_service_retry_s=0.4, dim_persist_s=0.2, fps_window_s=1.0)
BID = "mps3-01"


def wait_for(pred: Callable[[], Any], timeout: float = 20.0, what: str = "") -> Any:
    deadline = time.monotonic() + timeout
    while True:
        v = pred()
        if v:
            return v
        if time.monotonic() > deadline:
            raise AssertionError(f"timed out waiting for {what or pred}")
        time.sleep(0.01)


@pytest.fixture
def bus() -> EventBus:
    return EventBus()


@pytest.fixture
def svc(bus: EventBus) -> Any:
    s = DisplayService(bus, timings=FAST)
    yield s
    s.shutdown()


@pytest.fixture
def events(bus: EventBus) -> list[dict[str, Any]]:
    got: list[dict[str, Any]] = []
    bus.subscribe("display.state", lambda e: got.append(dict(e.data, board=e.board_id)))
    return got


def harness_panel(name: str = "boot") -> FakePanel:
    model, _grid, regs = G.harness_pictures()[name]
    return FakePanel(model, regs=regs)


def converged(vm: ViewerModel, viewer: Any, fake: FakeLcdMirror) -> bool:
    vm.pump(viewer)
    frame, valid = fake.picture()
    return vm.matches(frame, valid)


# --- the basics ------------------------------------------------------------------------------------


def test_first_viewer_opens_keys_and_shows_the_golden_picture(svc: DisplayService,
                                                             events: list[dict[str, Any]]) -> None:
    with FakeLcdMirror(harness_panel("boot"), mode="sw") as fake:
        vm = ViewerModel()
        v = svc.attach(BID, fake.connect)
        wait_for(lambda: converged(vm, v, fake), what="the keyframe")
        _model, grid, _regs = G.harness_pictures()["boot"]
        assert bytes(vm.frame) == grid                       # the renderer's own grid, pixel for pixel
        assert vm.keys == 1 and vm.messages == 1             # ONE message: the whole keyframe
        st = svc.status(BID)
        assert st["state"] == "live" and st["mode"] == "sw" and st["owner"] == "harness"
        assert st["hello"]["static_id"] == "0x44EE76D5" and st["hello"]["rate_max"] == 30
        assert st["hatched"] == 0 and st["badges"] == [] and st["flags"]["text_only"]
        assert st["counters"]["keys_sent"] == 1 and st["counters"]["acks_sent"] >= 1
        wait_for(lambda: svc.status(BID)["rtt_ms"] is not None, what="a PONG")
        assert fake.stats["acks"] >= 1 and fake.stats["max_unacked"] <= w.ACK_WINDOW
        assert fake.stats["rate_asked"] == 20 and svc.status(BID)["rate"] == 20
        states = [e["state"] for e in events]
        assert states[:3] == ["connecting", "syncing", "live"]
        # an incremental repaint: only the changed tiles travel
        model2, grid2, regs2 = G.harness_pictures()["link_down"]
        with fake.edit() as p:
            p.set_frame(model2)
            p.regs[:] = regs2
        wait_for(lambda: converged(vm, v, fake), what="the link_down repaint")
        assert bytes(vm.frame) == grid2
        assert vm.messages == 2 and 0 < vm.tiles - 300 < 300
        v.close()


def test_the_picture_is_the_boards_after_every_message(svc: DisplayService) -> None:
    """A running clcd_demo card: after EVERY message a viewer applies, its picture is one the
    board actually sent (a SNAP), never a mix."""
    anim = CardAnimator(period_s=0.05)
    with FakeLcdMirror(FakePanel(), animate=anim, rate_default=20) as fake:
        vm = ViewerModel()
        seen: set[int] = set()
        v = svc.attach(BID, fake.connect, rate=30)
        deadline = time.monotonic() + 30.0
        while time.monotonic() < deadline and len(seen) < 8:
            m = v.next_message()
            if m is None:
                time.sleep(0.005)
                continue
            vm.apply(m)
            v.ack()
            counter = G.card_counter(bytes(vm.frame))
            assert bytes(vm.frame) == G.card_picture(counter), \
                "the viewer's picture is not any card the board painted"
            assert counter <= anim.counter
            seen.add(counter)
        assert len(seen) >= 8                                 # the picture moved, whole each time
        assert svc.status(BID)["fps"] > 0
        v.close()


def test_rate_is_the_max_of_the_viewers_and_the_clamp_is_echoed(svc: DisplayService) -> None:
    with FakeLcdMirror(harness_panel(), rate_max=30) as fake:
        a = svc.attach(BID, fake.connect, rate=5)
        wait_for(lambda: fake.stats["rate_asked"] == 5, what="RATE 5")
        b = svc.attach(BID, fake.connect, rate=12)
        wait_for(lambda: fake.stats["rate_asked"] == 12, what="RATE max(viewers)")
        b.set_rate(50)
        wait_for(lambda: svc.status(BID)["rate"] == 30, what="the clamp echoed")
        assert fake.stats["rate_asked"] == 50 and svc.status(BID)["rate_asked"] == 50
        b.close()
        wait_for(lambda: fake.stats["rate_asked"] == 5, what="RATE back to the remaining viewer")
        assert fake.stats["connects"] == 1                   # one upstream for every viewer
        a.close()


# --- H1: flow control and whole snapshots --------------------------------------------------------


def test_ack_keeps_at_most_two_updates_in_flight(svc: DisplayService) -> None:
    with FakeLcdMirror(FakePanel(), animate=NoiseAnimator(0.03), max_msg=8192, rate_default=30) as fake:
        v = svc.attach(BID, fake.connect, ack=False, rate=30)
        wait_for(lambda: svc.status(BID)["counters"]["deltas_presented"] >= 5, what="deltas")
        assert fake.stats["snap_parts_max"] >= 3              # SNAPs really were split
        assert fake.stats["max_unacked"] <= w.ACK_WINDOW
        st = svc.status(BID)["counters"]
        assert st["acks_sent"] == st["updates"] > 0           # every UPDATE, on receipt
        v.close()


@pytest.mark.parametrize("snap_last", [True, False])
def test_a_split_snap_is_shown_whole(svc: DisplayService, snap_last: bool) -> None:
    """snap_last on: every picture a viewer ever holds is a SNAP the board took. Negative
    twin, a board without snap_last: shown part by part, the viewer sees torn pictures."""
    snaps: set[int] = set()
    anim = NoiseAnimator(0.04)

    def animate(p: FakePanel, now: float) -> None:
        anim(p, now)
        snaps.add(crc_valid(p.gram, set(range(300))))

    with FakeLcdMirror(FakePanel(), animate=animate, max_msg=16384, rate_default=15,
                       snap_last=snap_last) as fake:
        vm = ViewerModel()
        v = svc.attach(BID, fake.connect, ack=False, rate=15)
        torn = whole = 0
        deadline = time.monotonic() + 30.0
        while time.monotonic() < deadline and whole + torn < 25:
            m = v.next_message()
            if m is None:
                time.sleep(0.002)
                continue
            vm.apply(m)
            if len(vm.valid) == 300:
                if vm.crc() in snaps:
                    whole += 1
                else:
                    torn += 1
        v.close()
        assert whole + torn >= 5
        if snap_last:
            assert torn == 0
        else:
            assert torn > 0


# --- seq gap -------------------------------------------------------------------------------------


def test_seq_gap_sends_key_and_recovers_exactly(svc: DisplayService) -> None:
    anim = CardAnimator(period_s=0.05)
    with FakeLcdMirror(FakePanel(), animate=anim, rate_default=20) as fake:
        vm = ViewerModel()
        v = svc.attach(BID, fake.connect, rate=20)
        wait_for(lambda: svc.status(BID)["counters"]["deltas_presented"] >= 3, what="deltas")
        fake.gap(1)
        wait_for(lambda: svc.status(BID)["counters"]["gaps"] >= 1, what="the gap seen")
        wait_for(lambda: svc.status(BID)["counters"]["keys_presented"] >= 2, what="a second keyframe")
        st = svc.status(BID)
        assert fake.stats["gaps"] == 1 and st["counters"]["keys_sent"] >= 2
        with fake.lock:
            anim.frozen = True
        wait_for(lambda: converged(vm, v, fake), what="exact after the gap")
        assert svc.status(BID)["state"] == "live"
        v.close()


# --- refusal, no service, drop and reclaim ----------------------------------------------------------


def test_a_third_client_is_refused(bus: EventBus) -> None:
    services = [DisplayService(bus, timings=FAST) for _ in range(3)]
    try:
        with FakeLcdMirror(harness_panel()) as fake:
            views = [s.attach(BID, fake.connect) for s in services[:2]]
            for s in services[:2]:
                wait_for(lambda s=s: s.status(BID)["state"] == "live", what="two served")
            third = services[2].attach(BID, fake.connect)
            wait_for(lambda: services[2].status(BID)["state"] == "refused", what="refused")
            st = services[2].status(BID)
            assert "busy (2 clients)" in st["reason"] and fake.stats["refused"] >= 1
            assert third.next_message() is None
            views[0].close()
            services[0].close(BID, "left")
            wait_for(lambda: services[2].status(BID)["state"] == "live", what="a free slot taken")
    finally:
        for s in services:
            s.shutdown()


def test_no_service_is_reconnecting_then_live(svc: DisplayService) -> None:
    fake = FakeLcdMirror(harness_panel())
    fake.start()
    fake.stop_service()                                      # an image without the service
    try:
        v = svc.attach(BID, fake.connect)
        wait_for(lambda: svc.status(BID)["state"] == "reconnecting", what="reconnecting")
        assert "refused" in svc.status(BID)["reason"].lower()
        fake.start_service()
        wait_for(lambda: svc.status(BID)["state"] == "live", what="live once the service is up")
        v.close()
    finally:
        fake.close()


class _AcceptThenClose:
    """What ``ssh -L`` does when the far end refuses: accept locally, then close."""

    def __init__(self) -> None:
        from tests.fakes.lm1_fake_lcd_mirror import bind_ephemeral

        self.srv = bind_ephemeral()
        self.srv.listen(4)
        self.port = self.srv.getsockname()[1]
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self) -> None:
        while True:
            try:
                c, _ = self.srv.accept()
            except OSError:
                return
            c.close()

    def connect(self) -> Any:
        import socket

        return socket.create_connection(("127.0.0.1", self.port), timeout=5)

    def display_reason(self) -> str:
        return ""

    def display_connect(self) -> Any:
        return self.connect()

    def display_diagnose(self, since: float) -> str:
        return "no lcd_mirror service on the board (open failed: connect failed: Connection refused)"

    def close(self) -> None:
        self.srv.close()


def test_no_service_through_a_forward_is_down_with_the_reason(svc: DisplayService) -> None:
    src = _AcceptThenClose()
    try:
        v = svc.attach(BID, src)
        wait_for(lambda: svc.status(BID)["state"] == "down", what="down")
        assert svc.status(BID)["reason"].startswith("no lcd_mirror service")
        assert v.next_message() is None
        v.close()
    finally:
        src.close()


def test_source_says_unavailable_then_retries(svc: DisplayService) -> None:
    with FakeLcdMirror(harness_panel()) as fake:
        fake.block(DisplayUnavailable("needs a claimed board", retry_s=0.3))
        v = svc.attach(BID, fake.connect)
        wait_for(lambda: svc.status(BID)["reason"] == "needs a claimed board", what="down, reason")
        assert svc.status(BID)["state"] == "down"
        fake.unblock()
        wait_for(lambda: svc.status(BID)["state"] == "live", what="retried after retry_s")
        v.close()


def test_drop_and_reclaim_recovers_exactly(svc: DisplayService) -> None:
    """The tunnel drops (the board rebooted), the key is refused for a while (claim lost),
    then works again: reconnecting over the last picture, KEY, exact."""
    with FakeLcdMirror(harness_panel("boot")) as fake:
        vm = ViewerModel()
        v = svc.attach(BID, fake.connect)
        wait_for(lambda: converged(vm, v, fake), what="first picture")
        connects = fake.stats["connects"]
        fake.block(ConnectionRefusedError("Permission denied (publickey)"))
        wait_for(lambda: svc.status(BID)["state"] in ("reconnecting", "connecting"), what="reconnecting")
        assert svc.picture(BID).rgb565 == bytes(vm.frame)    # the last picture stays up
        model, _grid, regs = G.harness_pictures()["banner"]
        with fake.edit() as p:                                # the board repainted meanwhile
            p.set_frame(model)
            p.regs[:] = regs
        fake.unblock()
        wait_for(lambda: svc.status(BID)["state"] == "live", what="live again")
        wait_for(lambda: converged(vm, v, fake), what="exact after the reclaim")
        assert fake.stats["connects"] == connects + 1
        assert svc.status(BID)["counters"]["keys_sent"] >= 2
        v.close()


def test_a_fatal_source_error_stops_the_upstream(svc: DisplayService) -> None:
    with FakeLcdMirror(harness_panel()) as fake:
        v = svc.attach(BID, fake.connect)
        wait_for(lambda: svc.status(BID)["state"] == "live", what="live")
        fake.block(HarnessError("THE BOARD'S SSH HOST KEY IS NOT THE PINNED ONE"))
        wait_for(lambda: svc.status(BID)["state"] == "down", what="down")
        assert "HOST KEY" in svc.status(BID)["reason"] and svc.boards() == []
        fake.unblock()
        v2 = svc.attach(BID, fake.connect)                    # a viewer asking again retries
        wait_for(lambda: svc.status(BID)["state"] == "live", what="live after a new attach")
        v.close()
        v2.close()


# --- owners: sw-blind and the handover hatch --------------------------------------------------------


def test_sw_mode_is_blind_while_the_dut_owns_the_panel(svc: DisplayService) -> None:
    panel = harness_panel()
    panel.owner = w.OWNER_DUT
    panel.valid = set()
    with FakeLcdMirror(panel, mode="sw") as fake:
        vm = ViewerModel()
        v = svc.attach(BID, fake.connect)
        wait_for(lambda: vm.pump(v) or vm.messages, what="a keyframe")
        st = svc.status(BID)
        assert st["owner"] == "dut" and st["hatched"] == 300
        assert st["flags"]["blind"] and st["flags"]["text_only"] and not st["flags"]["exact"]
        assert [b["key"] for b in st["badges"]] == ["blind"]
        assert vm.valid == set() and vm.owner == w.OWNER_DUT
        v.close()


def test_handover_hatches_then_clears_on_the_first_repaint(svc: DisplayService) -> None:
    with FakeLcdMirror(harness_panel()) as fake:
        vm = ViewerModel()
        v = svc.attach(BID, fake.connect)
        wait_for(lambda: converged(vm, v, fake), what="the harness picture")
        fake.handover(w.OWNER_DUT)
        wait_for(lambda: svc.status(BID)["hatched"] == 300, what="all hatched")
        vm.pump(v)
        assert vm.valid == set() and svc.status(BID)["resets"] == 1
        assert [b["key"] for b in svc.status(BID)["badges"]] == ["held"]
        rects_model, rects, _regs = G.nanosoc_after_handover()
        for half in (range(0, 150), range(150, 300)):        # the DUT paints, progressively
            with fake.edit() as p:
                for t in half:
                    p.put_tile(t, w.tile_of_frame(rects_model, t))
                    p.valid.add(t)
            wait_for(lambda half=half: svc.status(BID)["hatched"] == 300 - half.stop, what="progress")
        wait_for(lambda: converged(vm, v, fake), what="exact")
        assert bytes(vm.frame) == rects
        v.close()


def test_dim_badge_after_it_persists() -> None:
    svc = DisplayService(timings=DisplayTimings(**{**FAST.__dict__, "dim_persist_s": 2.0}))
    with FakeLcdMirror(harness_panel()) as fake:
        v = svc.attach(BID, fake.connect)
        wait_for(lambda: svc.status(BID)["state"] == "live", what="live")
        with fake.edit() as p:
            p.csr &= ~w.ST_BL
            p.fill_tile(0, 0x1234)                           # something changes, an UPDATE flows
        wait_for(lambda: not svc.status(BID)["flags"]["bl"], what="the status")
        assert "backlight_off" not in [b["key"] for b in svc.status(BID)["badges"]]
        wait_for(lambda: "backlight_off" in [b["key"] for b in svc.status(BID)["badges"]],
                 what="the badge after dim_persist_s")
        v.close()
    svc.shutdown()


# --- viewers: ack, dirty sets, drop-to-latest ----------------------------------------------------


def test_a_viewer_that_does_not_ack_gets_one_message_then_the_latest(svc: DisplayService) -> None:
    anim = CardAnimator(period_s=0.03)
    with FakeLcdMirror(FakePanel(), animate=anim, rate_default=30) as fake:
        slow, fast = ViewerModel(), ViewerModel()
        vs = svc.attach(BID, fake.connect, rate=30)
        vf = svc.attach(BID, fake.connect, rate=30)
        wait_for(lambda: slow.pump(vs, ack=False) or slow.messages, what="the slow viewer's keyframe")
        t_end = time.monotonic() + 30.0
        while time.monotonic() < t_end and fast.messages <= 5:
            fast.pump(vf)
            assert vs.next_message() is None                 # one in flight, not acked: nothing
            time.sleep(0.01)
        assert slow.messages == 1 and fast.messages > 5
        # negative twin: the withheld ack leaves the slow viewer detectably stale
        frame, valid = fake.picture()
        assert not slow.matches(frame, valid)
        with fake.lock:
            anim.frozen = True
        vs.ack()
        wait_for(lambda: converged(slow, vs, fake), what="the slow viewer catches up")
        assert slow.messages <= 3                             # the latest tiles, not the backlog
        assert converged(fast, vf, fake)
        vs.close()
        vf.close()


def test_negative_twin_drop_oldest_outbox_leaves_a_tile_stale(svc: DisplayService) -> None:
    """Why the display must not reuse the events Outbox (§7.3): a queue that drops the
    OLDEST message loses a tile for ever; the per-viewer dirty set never does."""
    with FakeLcdMirror(harness_panel()) as fake:
        dirty, outboxed = ViewerModel(), ViewerModel()
        vd = svc.attach(BID, fake.connect)                    # acks: dirty set + one in flight
        vo = svc.attach(BID, fake.connect, ack=False)         # every message, into a lossy queue
        outbox: collections.deque[bytes] = collections.deque(maxlen=2)
        wait_for(lambda: converged(dirty, vd, fake), what="first picture")
        outboxed.pump(vo, ack=False)
        steps = [(7, 0xF800)] + [(8, c) for c in (0x07E0, 0x001F, 0xFFE0, 0x0000)]
        for t, colour in steps:
            with fake.edit() as p:
                p.fill_tile(t, colour)
            wait_for(lambda t=t, colour=colour: svc.picture(BID).pixel(*w.tile_origin(t)) == colour,
                     what="the step presented")
            m = vo.next_message()
            while m is not None:
                outbox.append(m)                            # full: the OLDEST is dropped
                m = vo.next_message()
        for m in outbox:
            outboxed.apply(m)
        frame, valid = fake.picture()
        assert not outboxed.matches(frame, valid)            # tile 7 is stale for ever
        assert w.tile_of_frame(outboxed.frame, 7) != w.tile_of_frame(frame, 7)
        assert converged(dirty, vd, fake)                    # the dirty set is exact
        vd.close()
        vo.close()


# --- lifecycle: grace, close, stale ------------------------------------------------------------------


def test_grace_close_and_reuse(bus: EventBus, events: list[dict[str, Any]]) -> None:
    svc = DisplayService(bus, timings=DisplayTimings(**{**FAST.__dict__, "grace_s": 2.0}))
    with FakeLcdMirror(harness_panel()) as fake:
        v = svc.attach(BID, fake.connect)
        wait_for(lambda: svc.status(BID)["state"] == "live", what="live")
        v.close()
        time.sleep(0.3)
        v2 = svc.attach(BID, fake.connect)                    # back within the grace: reused
        assert v2.next_message() is not None                 # a keyframe at once, from the compositor
        assert fake.stats["connects"] == 1
        v2.close()
        wait_for(lambda: fake.clients == 0, what="closed after the grace")
        st = svc.status(BID)
        assert st["state"] == "down" and "no viewer" in st["reason"] and svc.boards() == []
        assert events[-1]["state"] == "down"
    svc.shutdown()


def test_close_at_once_for_the_lease_hooks(svc: DisplayService) -> None:
    with FakeLcdMirror(harness_panel()) as fake:
        v = svc.attach(BID, fake.connect)
        wait_for(lambda: svc.status(BID)["state"] == "live", what="live")
        st = svc.close(BID, "the lease was released")
        assert st["state"] == "down" and st["reason"] == "the lease was released"
        wait_for(lambda: fake.clients == 0, timeout=5, what="the board connection closed")
        assert v.status()["state"] == "down"
        assert svc.boards() == []


def test_a_silent_board_goes_stale_then_reconnects() -> None:
    t = DisplayTimings(grace_s=5, ping_s=0.1, stale_s=0.5, dead_s=1.5, tick_s=0.02,
                       backoff_s=(0.05,), hello_timeout_s=5, key_retry_s=0.5)
    svc = DisplayService(timings=t)
    try:
        with FakeLcdMirror(harness_panel()) as fake:
            v = svc.attach(BID, fake.connect)
            wait_for(lambda: svc.status(BID)["state"] == "live", what="live")
            real_control = fake._control
            fake._control = lambda c, rbuf: rbuf.clear()      # the board stops answering PINGs
            wait_for(lambda: svc.status(BID)["state"] == "stale", what="stale")
            wait_for(lambda: svc.status(BID)["counters"]["connects"] >= 2, what="a reconnect")
            fake._control = real_control                      # (the new connection's KEY was lost)
            wait_for(lambda: svc.status(BID)["state"] == "live", what="live again: KEY re-sent")
            v.close()
    finally:
        svc.shutdown()


def test_picture_opens_waits_and_the_png_is_the_board(svc: DisplayService) -> None:
    card = G.card_picture(0x0F0F)
    with FakeLcdMirror(FakePanel(card)) as fake:
        with pytest.raises(UnavailableError):
            svc.picture(BID)                                 # nothing open, no source
        pic = svc.picture(BID, fake.connect, wait_s=10)
        assert pic.rgb565 == card and pic.hatched == frozenset()
        assert pic.png().startswith(b"\x89PNG")
        assert svc.status(BID)["viewers"] == 0               # the grace keeps it for the next one
        wait_for(lambda: fake.clients == 0, what="closed after the grace")


def test_an_incompatible_board_is_down_with_why(svc: DisplayService) -> None:
    fake = FakeLcdMirror(harness_panel())
    fake.hello = lambda: w.DisplayInfo(proto=2)              # type: ignore[method-assign]
    with fake:
        v = svc.attach(BID, fake.connect)
        wait_for(lambda: svc.status(BID)["state"] == "down", what="down")
        assert "proto 2" in svc.status(BID)["reason"]
        v.close()


def test_a_viewer_wake_callback_fires(svc: DisplayService) -> None:
    woke = threading.Event()
    with FakeLcdMirror(harness_panel()) as fake:
        v = svc.attach(BID, fake.connect, wake=woke.set)
        assert woke.wait(15)
        wait_for(lambda: v.pending() or v.next_message() is not None, what="something to send")
        st = v.next_status()
        assert st is not None and v.next_status() is None     # only on a change
        v.close()


def test_noise_at_12_fps_is_exact_after_settling(svc: DisplayService) -> None:
    anim = NoiseAnimator(1 / 12)
    with FakeLcdMirror(FakePanel(os.urandom(w.FRAME_BYTES)), animate=anim, rate_default=20) as fake:
        vm = ViewerModel()
        v = svc.attach(BID, fake.connect, rate=20)
        t0 = time.monotonic()
        while time.monotonic() - t0 < 1.5:
            vm.pump(v)
            time.sleep(0.01)
        with fake.lock:
            anim.frozen = True
        wait_for(lambda: converged(vm, v, fake), what="exact")
        assert svc.status(BID)["counters"]["protocol_errors"] == 0
        v.close()


# --- the board's §6.1 corrections (platform feat/lcd-mirror d86ce81) ------------------------------


def test_seq_starts_at_1_and_the_first_ack_is_1(svc: DisplayService) -> None:
    with FakeLcdMirror(harness_panel()) as fake:
        v = svc.attach(BID, fake.connect)
        wait_for(lambda: svc.status(BID)["state"] == "live", what="live")
        assert svc.status(BID)["seq"] == 1                   # a one-part keyframe: seq 1
        wait_for(lambda: "first_ack" in fake.stats, what="an ACK")
        assert fake.stats["first_ack"] == 1                  # never ACK 0: it acknowledges nothing
        v.close()


def test_a_long_connection_crosses_the_seq_wrap_without_a_gap(svc: DisplayService) -> None:
    anim = CardAnimator(period_s=0.05)
    with FakeLcdMirror(FakePanel(), animate=anim, rate_default=20, seq_base=0xFFFFFFFD) as fake:
        vm = ViewerModel()
        v = svc.attach(BID, fake.connect, rate=20)
        wait_for(lambda: svc.status(BID)["counters"]["deltas_presented"] >= 6, what="past the wrap")
        st = svc.status(BID)
        assert st["counters"]["gaps"] == 0 and st["counters"]["keys_sent"] == 1
        assert st["seq"] < 100                               # it wrapped
        with fake.lock:
            anim.frozen = True
        wait_for(lambda: converged(vm, v, fake), what="exact")
        v.close()


def test_a_still_screen_stays_live_on_pongs_alone() -> None:
    """No heartbeat UPDATEs: a still screen is silent but for PONGs, and must stay live
    (the board's correction 4). fps counts whole SNAPs that carried tiles: 0 here."""
    t = DisplayTimings(grace_s=5, ping_s=0.1, stale_s=0.6, dead_s=3.0, tick_s=0.02,
                       backoff_s=(0.05,), fps_window_s=0.5)
    svc = DisplayService(timings=t)
    try:
        with FakeLcdMirror(harness_panel()) as fake:
            v = svc.attach(BID, fake.connect)
            wait_for(lambda: svc.status(BID)["state"] == "live", what="live")
            updates = fake.stats["updates"]
            t_end = time.monotonic() + 3 * t.stale_s
            while time.monotonic() < t_end:
                assert svc.status(BID)["state"] == "live"
                time.sleep(0.05)
            assert fake.stats["updates"] == updates          # the board sent nothing new
            assert svc.status(BID)["counters"]["pongs"] >= 5
            assert svc.status(BID)["fps"] == 0
            with fake.edit() as p:                           # a header-only change: not a frame
                p.csr ^= w.ST_STANDBY
            wait_for(lambda: svc.status(BID)["flags"]["standby"], what="the header change")
            assert svc.status(BID)["fps"] == 0
            with fake.edit() as p:
                p.fill_tile(3, 0x1234)
            wait_for(lambda: svc.status(BID)["fps"] > 0, what="a frame with tiles")
            v.close()
    finally:
        svc.shutdown()


def test_negative_twin_updates_without_pongs_go_stale() -> None:
    """The twin of the above: a board that keeps sending UPDATEs but never answers a PING is
    stale; UPDATEs are not the liveness probe."""
    t = DisplayTimings(grace_s=5, ping_s=0.1, stale_s=0.6, dead_s=30.0, tick_s=0.02, backoff_s=(0.05,))
    svc = DisplayService(timings=t)
    try:
        with FakeLcdMirror(FakePanel(), animate=CardAnimator(period_s=0.05), rate_default=20) as fake:
            real = fake._control

            def no_pongs(c: Any, rbuf: bytearray) -> None:
                keep = bytearray()
                while len(rbuf) >= 8:
                    _m, typ, _r, ln = w.HEADER.unpack_from(rbuf)
                    if len(rbuf) < 8 + ln:
                        break
                    if typ != w.T_PING:
                        keep += rbuf[:8 + ln]
                    del rbuf[:8 + ln]
                keep += rbuf
                rbuf[:] = keep
                real(c, rbuf)

            fake._control = no_pongs
            v = svc.attach(BID, fake.connect, rate=20)
            wait_for(lambda: svc.status(BID)["counters"]["deltas_presented"] >= 1, what="deltas")
            wait_for(lambda: svc.status(BID)["state"] == "stale", what="stale")
            n = svc.status(BID)["counters"]["deltas_presented"]
            wait_for(lambda: svc.status(BID)["counters"]["deltas_presented"] > n, what="more deltas")
            assert svc.status(BID)["state"] == "stale"           # UPDATEs did not un-stale it
            fake._control = real
            wait_for(lambda: svc.status(BID)["state"] == "live", what="a PONG ends stale")
            v.close()
    finally:
        svc.shutdown()


class _VectorBoard:
    """The board's own bytes (tests/fixtures/lcdmirror_wire): HELLO, then on KEY the split
    keyframe; PONGs answered; ACKs recorded."""

    DIR = Path(__file__).resolve().parents[1] / "fixtures" / "lcdmirror_wire"

    def __init__(self) -> None:
        from tests.fakes.lm1_fake_lcd_mirror import bind_ephemeral

        self.manifest = json.loads((self.DIR / "manifest.json").read_text())
        self.key = next(e for e in self.manifest["vectors"] if e["kind"] == "keyframe")
        self.srv = bind_ephemeral()
        self.srv.listen(2)
        self.port = self.srv.getsockname()[1]
        self.acks: list[int] = []
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self) -> None:
        try:
            conn, _ = self.srv.accept()
        except OSError:
            return
        with conn:
            conn.sendall((self.DIR / "hello.bin").read_bytes())
            buf = bytearray()
            while True:
                try:
                    data = conn.recv(4096)
                except OSError:
                    return
                if not data:
                    return
                buf += data
                while len(buf) >= 8:
                    _m, typ, _r, ln = w.HEADER.unpack_from(buf)
                    if len(buf) < 8 + ln:
                        break
                    body = bytes(buf[8:8 + ln])
                    del buf[:8 + ln]
                    if typ == w.T_KEY:
                        for part in self.key["parts"]:
                            conn.sendall((self.DIR / part).read_bytes())
                    elif typ == w.T_PING:
                        conn.sendall(w.pong_msg(w.U32.unpack(body)[0]))
                    elif typ == w.T_RATE:
                        conn.sendall(w.message(w.T_RATE, bytes([min(30, body[0])])))
                    elif typ == w.T_ACK:
                        self.acks.append(w.U32.unpack(body)[0])

    def connect(self) -> Any:
        import socket

        return socket.create_connection(("127.0.0.1", self.port), timeout=5)

    def close(self) -> None:
        self.srv.close()


def test_the_boards_own_bytes_through_the_compositor(svc: DisplayService) -> None:
    board = _VectorBoard()
    try:
        vm = ViewerModel()
        v = svc.attach(BID, board.connect)
        wait_for(lambda: svc.status(BID)["state"] == "live", what="the board's keyframe presented")
        st = svc.status(BID)
        assert st["hello"]["static_id"] == "0x5a5a0001" and st["mode"] == "sw" and st["hatched"] == 0
        assert st["seq"] == board.key["seq_first"] + 2 and st["rate"] == 20
        pic = svc.picture(BID)
        assert zlib.crc32(pic.rgb565) == board.key["frame_crc"]
        wait_for(lambda: board.acks[-3:] == [100, 101, 102], what="each part ACKed")
        vm.pump(v)
        assert vm.keys == 1 and zlib.crc32(bytes(vm.frame)) == board.key["frame_crc"]
        v.close()
    finally:
        board.close()
