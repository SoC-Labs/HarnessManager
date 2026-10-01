"""FIX-PACK-6 item 4: estimated progress while one big frame is in flight.

H1 (board 1, 1 Oct): ``deploy: push 169000/2467736 (6%)`` then ``2467736/2467736 (100%)``,
nothing between, for the push and for the card: pyverify reports per FRAME, and the partial is
one 2.3 MB frame. ``harness_manager_mps3.frame_progress`` estimates inside the frame (marked
``estimated``), snaps to the real bytes when the frame completes, never goes backwards and never
passes the frame's end early. Each check has its negative twin.
"""

from __future__ import annotations

import threading
import time

import pytest
from pyverify.pusher import BitstreamKind, BitstreamPusher

from harness_manager.cli.context import describe_event
from harness_manager.cli.output import StderrProgress
from harness_manager.core.events import Event, EventBus
from harness_manager.core.pack import TAKES_DETAIL, detail_of
from harness_manager.services.deploy import DeployService
from harness_manager_mps3 import deploy as deploymod
from harness_manager_mps3 import frame_progress as fp

CLEARING, PARTIAL = 169_000, 2_298_736
TOTAL = CLEARING + PARTIAL


class Clock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


def estimator(sent: dict, reports: list, clock: Clock, nominal: float = 35_000.0):
    def emit(done: int) -> None:
        reports.append(("est", done))

    return fp.FrameEstimator(emit, lambda kind: sum(v for k, v in sent.items() if k != kind),
                             nominal_bps=nominal, tick_s=0, clock=clock)


def test_the_partial_moves_at_the_clearings_measured_rate_and_snaps_at_its_end():
    clock, reports, sent = Clock(), [], {}
    est = estimator(sent, reports, clock)
    # the clearing: 169 KB in 4 s (42 KB/s), estimated on the nominal rate meanwhile
    est.started(BitstreamKind.CLEARING, CLEARING)
    clock.t += 2.0
    first = est.tick()
    assert first is not None and 0 < first < CLEARING
    clock.t += 2.0
    est.finished(ok=True)
    sent[BitstreamKind.CLEARING] = CLEARING                 # the snap: on_frame's real report
    assert est.rate_bps() == pytest.approx(CLEARING / 4.0)
    # the partial: it moves from 6 % at the measured rate, a step per tick
    est.started(BitstreamKind.PARTIAL, PARTIAL)
    seen = []
    for _ in range(30):
        clock.t += 2.0
        v = est.tick()
        if v is not None:
            seen.append(v)
    assert seen and seen == sorted(seen) and len(set(seen)) == len(seen)   # never backwards
    assert all(CLEARING < v < TOTAL for v in seen)
    expect = CLEARING + int(PARTIAL * fp.eased(10.0 * est.rate_bps() / PARTIAL))
    assert seen[4] == pytest.approx(expect, rel=0.01)                    # rate x elapsed
    est.finished(ok=True)
    assert est.tick() is None                                            # nothing after the snap


def test_twin_an_estimate_never_reaches_the_frames_end_however_long_it_takes():
    clock, reports, sent = Clock(), [], {}
    est = estimator(sent, reports, clock, nominal=10_000_000.0)          # a wildly fast guess
    est.started(BitstreamKind.PARTIAL, PARTIAL)
    clock.t += 3600.0
    v = est.tick()
    assert v is not None and v <= int(PARTIAL * fp.END_FRACTION) < PARTIAL
    clock.t += 3600.0
    assert est.tick() is None or est.estimates[-1] < PARTIAL             # still short of it


def test_twin_a_failed_frame_stops_the_estimates_and_measures_nothing():
    clock, reports, sent = Clock(), [], {}
    est = estimator(sent, reports, clock)
    est.started(BitstreamKind.PARTIAL, PARTIAL)
    clock.t += 5.0
    assert est.tick() is not None
    est.finished(ok=False)
    clock.t += 5.0
    assert est.tick() is None and est.rate_bps() == est.nominal_bps


def test_twin_a_frame_faster_than_the_measure_floor_is_not_a_rate():
    clock, reports, sent = Clock(), [], {}
    est = estimator(sent, reports, clock)
    est.started(BitstreamKind.CLEARING, CLEARING)
    clock.t += fp.MIN_MEASURE_S / 2                  # into the socket's buffer, not the board
    est.finished(ok=True)
    assert est.rate_bps() == est.nominal_bps


def test_the_ticker_reports_at_most_every_tick_and_none_after_the_frame():
    reports: list[int] = []
    lock = threading.Lock()

    def emit(done: int) -> None:
        with lock:
            reports.append(done)

    est = fp.FrameEstimator(emit, lambda kind: 0, nominal_bps=200_000.0, tick_s=0.05)
    est.started(BitstreamKind.PARTIAL, PARTIAL)
    time.sleep(0.4)
    est.finished(ok=True)
    with lock:
        n = len(reports)
    time.sleep(0.2)
    with lock:
        assert len(reports) == n                     # the thread is gone with the frame
    assert 2 <= n <= 9 and reports == sorted(reports) and reports[-1] < PARTIAL


# --- the pusher: a fake slow frame ---------------------------------------------------------------


def test_a_slow_partial_reports_estimates_between_the_real_frames(monkeypatch):
    """``_ReportingPusher`` over a fake ``_send`` that takes 0.6 s for the partial: the
    deploy's report sees 6 %, then estimates (marked), then the real 100 %."""
    calls: list[tuple[str, int, int, dict]] = []

    def report(phase: str, done: int, total: int, detail: dict | None = None) -> None:
        calls.append((phase, done, total, dict(detail or {})))
    setattr(report, TAKES_DETAIL, True)

    def slow_send(self, frame: bytes, *, kind):
        time.sleep(0.6 if kind is BitstreamKind.PARTIAL else 0.0)
        return None

    monkeypatch.setattr(BitstreamPusher, "_send", slow_send)
    monkeypatch.setattr(fp, "TICK_S", 0.1)
    sent: dict = {}

    def on_frame(kind, n: int) -> None:
        sent[kind] = n
        report("push", sum(sent.values()), TOTAL)

    est = fp.for_phase(report, "push", TOTAL, sent, nominal_bps=1_000_000.0)
    pusher = deploymod._ReportingPusher(on_frame=on_frame, estimator=est, host="127.0.0.1",
                                        transport="tcp", tcp_port=9, timeout_s=5.0)
    pusher._send(b"\0" * (deploymod.HEADER_SIZE + CLEARING), kind=BitstreamKind.CLEARING)
    pusher._send(b"\0" * (deploymod.HEADER_SIZE + PARTIAL), kind=BitstreamKind.PARTIAL)
    done = [c[1] for c in calls]
    marked = [c for c in calls if c[3].get("estimated")]
    assert calls[0][1] == CLEARING and not calls[0][3]                   # the clearing: real
    assert calls[-1][1] == TOTAL and not calls[-1][3]                    # the snap: real
    assert len(marked) >= 2, calls                                       # moved in between
    assert all(CLEARING < c[1] < TOTAL for c in marked)
    assert done == sorted(done)                                          # never backwards


def test_twin_without_an_estimator_the_pusher_reports_per_frame_only(monkeypatch):
    calls: list[int] = []
    monkeypatch.setattr(BitstreamPusher, "_send",
                        lambda self, frame, *, kind: time.sleep(0.3 if kind is
                                                                BitstreamKind.PARTIAL else 0))
    sent: dict = {}

    def on_frame(kind, n: int) -> None:
        sent[kind] = n
        calls.append(sum(sent.values()))

    pusher = deploymod._ReportingPusher(on_frame=on_frame, host="127.0.0.1", transport="tcp",
                                        tcp_port=9, timeout_s=5.0)
    pusher._send(b"\0" * (deploymod.HEADER_SIZE + CLEARING), kind=BitstreamKind.CLEARING)
    pusher._send(b"\0" * (deploymod.HEADER_SIZE + PARTIAL), kind=BitstreamKind.PARTIAL)
    assert calls == [CLEARING, TOTAL]                                    # the H1 stall


# --- the marks on the way out ----------------------------------------------------------------------


def test_the_deploy_service_marks_an_estimate_and_only_an_estimate():
    bus, seen = EventBus(), []
    bus.subscribe("deploy.progress", seen.append)

    class Adapter:
        def preflight(self, overlay):
            return []

        def deploy(self, overlay, progress):
            progress("push", CLEARING, TOTAL)
            progress("push", CLEARING + 1000, TOTAL, detail={"estimated": True})
            progress("push", TOTAL, TOTAL)
            from harness_manager.core.pack import DeployResult
            return DeployResult(rm_id="0x01000001", verified=True, seconds=1.0, transport="tcp")

    from types import SimpleNamespace

    from harness_manager.core.model import BoardIdentity
    from harness_manager.core.pack import OverlayRef

    session = SimpleNamespace(deploy=Adapter(),
                              candidate=SimpleNamespace(board_id="mps3@x:6900"),
                              identity=lambda: BoardIdentity(board_type="mps3",
                                                             rm_id="0x01000001"))
    DeployService(SimpleNamespace(bus=bus)).deploy(session, OverlayRef("nanosoc", "0x01000001",
                                                                       "0x44ee76d5"))
    marks = [e.data.get("estimated") for e in seen]
    assert marks == [None, True, None]
    assert detail_of({"estimated": True, "other": 1}) == {"estimated": True}


def test_the_cli_marks_estimates_with_a_tilde_and_real_bytes_without():
    est = Event("deploy.progress", "b", {"phase": "push", "bytes": 1_234_000, "total": TOTAL,
                                         "estimated": True})
    real = Event("deploy.progress", "b", {"phase": "push", "bytes": CLEARING, "total": TOTAL})
    assert describe_event(est) == f"deploy: push ~1234000/{TOTAL} (~50%)"
    assert describe_event(real) == f"deploy: push {CLEARING}/{TOTAL} (6%)"
    import io

    out = io.StringIO()
    p = StderrProgress("commit", out)
    p("commit", CLEARING, TOTAL)
    p("commit", 1_234_000, TOTAL, detail={"estimated": True})
    p("commit", TOTAL, TOTAL)
    assert out.getvalue().splitlines() == [
        f"commit: commit {CLEARING}/{TOTAL} (6%)",
        f"commit: commit ~1234000/{TOTAL} (~50%)",
        f"commit: commit {TOTAL}/{TOTAL} (100%)"]


def test_the_cli_shows_one_estimate_per_ten_percent(monkeypatch):
    """``Ctx.bus_progress``: the estimates (every 0.5 s) print once per 10 % step; every real
    report prints as before."""
    import argparse
    import io

    from harness_manager.cli.context import Ctx

    bus = EventBus()
    err = io.StringIO()
    ctx = Ctx(argparse.Namespace(), type("E", (), {"bus": bus})(), "human", err=err)
    with ctx.bus_progress("b", "deploy"):
        bus.publish(Event("deploy.progress", "b", {"phase": "push", "bytes": CLEARING,
                                                   "total": TOTAL}))
        for done in range(CLEARING + 10_000, TOTAL, 20_000):        # ~115 estimates
            bus.publish(Event("deploy.progress", "b", {"phase": "push", "bytes": done,
                                                       "total": TOTAL, "estimated": True}))
        bus.publish(Event("deploy.progress", "b", {"phase": "push", "bytes": TOTAL,
                                                   "total": TOTAL}))
    lines = err.getvalue().splitlines()
    assert lines[0].endswith("(6%)") and lines[-1].endswith("(100%)")
    est = [ln for ln in lines if "~" in ln]
    assert 8 <= len(est) <= 11, lines                                    # one per 10 % step
