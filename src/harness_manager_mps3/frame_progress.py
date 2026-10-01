"""Progress while one big frame is in flight (FIX-PACK-6 item 4).

pyverify's pusher sends a pair as two frames, the clearing (~169 KB) then the partial
(~2.3 MB), and reports nothing inside a frame. The deploy's ``on_frame`` (and the card's
``on_card_frame``, and ``card commit``'s) reported per FRAME, so the bar sat at 6 % (the
clearing) for the whole partial and then jumped to 100 % (H1 r5, board 1, 1 Oct). The real
fix is per-chunk reporting inside pyverify's pusher (the platform repo); until then this
fills the gap on the Harness Manager side:

- while a frame is in flight, an ESTIMATE is reported at most every ``TICK_S`` (0.5 s), with
  ``{"estimated": True}`` in the progress detail (``core.pack.report_progress``), so the event
  and the job's progress carry ``estimated: true`` and the front-ends mark it ("~");
- the estimate is the measured rate of this push's earlier frames (a frame that took at
  least ``MIN_MEASURE_S``), else the nominal rate the caller gives, against the time the
  frame has been in flight; past 90 % of the frame it slows down and it never reaches the
  frame's end (``END_FRACTION``, and one byte short): only the frame's completion does;
- when the frame completes the caller reports the real bytes (the snap); nothing estimated
  is reported after that, and an estimate never goes below the bytes already reported (the
  earlier frames') nor below the last estimate: the bar never goes backwards.

A frame that fails stops the estimates; the caller's error says what happened.
"""

from __future__ import annotations

import math
import threading
import time
from collections.abc import Callable
from typing import Any

#: At most one estimate this often (seconds).
TICK_S = 0.5
#: An estimate stops short of the frame's end: only the frame's completion reaches it.
END_FRACTION = 0.99
#: Past this fraction of the frame the estimate slows down (the rate may be optimistic).
EASE_FROM = 0.9
#: A frame faster than this says nothing about the rate (it went into the socket's buffer).
MIN_MEASURE_S = 0.5
#: The first frame's rate, bytes a second, when nothing was measured yet, by transport:
#: the Linux harness takes a plain TCP push at ~35 KB/s through the hub (nanosoc's ~2.5 MB
#: in ~70 s, HIL D2 on board 2); bare metal's windowed push ~570 KB/s (OTW, 2f8813d).
NOMINAL_PUSH_BPS = {"tcp": 35_000.0, "tcp+windowed": 500_000.0, "tftp": 200_000.0}
NOMINAL_DEFAULT_BPS = 35_000.0

ESTIMATED = {"estimated": True}


def eased(fraction: float) -> float:
    """``fraction`` of the frame expected by now -> what is shown: the same up to
    ``EASE_FROM``, then an approach to ``END_FRACTION`` that never reaches it."""
    if fraction <= EASE_FROM:
        return max(0.0, fraction)
    room = END_FRACTION - EASE_FROM
    return EASE_FROM + room * (1.0 - math.exp(-(fraction - EASE_FROM) / room))


class FrameEstimator:
    """Estimated progress for the frames of one push (module docstring).

    ``emit(done_bytes)`` reports an estimate (the caller adds ``estimated: true``);
    ``base_of(kind)`` is the bytes the caller has already reported for the OTHER frames
    when ``kind`` starts (its per-frame ``sent`` table). ``started``/``finished`` bracket
    each frame's send (``_ReportingPusher._send``)."""

    def __init__(self, emit: Callable[[int], None], base_of: Callable[[Any], int], *,
                 nominal_bps: float = NOMINAL_DEFAULT_BPS, tick_s: float = TICK_S,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self._emit = emit
        self._base_of = base_of
        self.nominal_bps = float(nominal_bps) if nominal_bps and nominal_bps > 0 else \
            NOMINAL_DEFAULT_BPS
        self.tick_s = tick_s
        self._clock = clock
        self._lock = threading.Lock()
        self._flight: tuple[int, int, float] | None = None     # (base, size, started at)
        self._measured_bytes = 0
        self._measured_s = 0.0
        self._last = -1                 # the last value reported (estimated or not)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        #: every estimate reported (tests read it)
        self.estimates: list[int] = []

    # -- the rate -------------------------------------------------------------------------------

    def rate_bps(self) -> float:
        """The measured rate of this push's earlier frames, else the nominal one."""
        if self._measured_s > 0 and self._measured_bytes > 0:
            return self._measured_bytes / self._measured_s
        return self.nominal_bps

    def estimate(self, now: float | None = None) -> int | None:
        """The bytes to show now, or None when no frame is in flight."""
        with self._lock:
            return self._estimate_locked(self._clock() if now is None else now)

    def _estimate_locked(self, now: float) -> int | None:
        if self._flight is None:
            return None
        base, size, t0 = self._flight
        if size <= 1:
            return base
        expected = max(0.0, now - t0) * self.rate_bps() / size
        within = int(size * eased(expected))
        within = min(within, int(size * END_FRACTION), size - 1)
        return base + max(0, within)

    # -- the frame's bracket --------------------------------------------------------------------

    def started(self, kind: Any, size: int) -> None:
        base = int(self._base_of(kind) or 0)
        with self._lock:
            self._flight = (base, int(size), self._clock())
            self._last = max(self._last, base)
        self._stop.clear()
        if self.tick_s > 0:
            self._thread = threading.Thread(target=self._tick, daemon=True,
                                            name="hm-frame-estimate")
            self._thread.start()

    def finished(self, *, ok: bool) -> None:
        """The frame's send returned (``ok``) or raised. No estimate is reported after this
        returns; the caller then reports the real bytes."""
        self._stop.set()
        with self._lock:
            flight, self._flight = self._flight, None
            if flight is not None and ok:
                took = self._clock() - flight[2]
                if took >= MIN_MEASURE_S:
                    self._measured_bytes += flight[1]
                    self._measured_s += took
                self._last = max(self._last, flight[0] + flight[1])
        thread, self._thread = self._thread, None
        if thread is not None and thread is not threading.current_thread():
            thread.join(max(1.0, 4 * self.tick_s))

    def _tick(self) -> None:
        while not self._stop.wait(self.tick_s):
            self.tick()

    def tick(self) -> int | None:
        """Report one estimate now, if it moves the bar forward (the thread's step; tests
        call it with a fake clock)."""
        with self._lock:
            value = self._estimate_locked(self._clock())
            if value is None or value <= self._last:
                return None
            self._last = value
            self.estimates.append(value)
            # Under the lock: once ``finished`` holds it, no estimate follows the snap.
            self._emit(value)
            return value


def nominal_push_bps(transport: str) -> float:
    return NOMINAL_PUSH_BPS.get(transport, NOMINAL_DEFAULT_BPS)


def for_phase(report: Callable[..., None], phase: str, total: int, sent: dict[Any, int], *,
              nominal_bps: float) -> FrameEstimator:
    """The estimator of one phase whose frames the caller tracks in ``sent`` (kind -> bytes
    reported): its estimates go to ``report`` as ``(phase, done, total)`` with
    ``{"estimated": True}`` (``core.pack.report_progress``: only to a progress that takes a
    detail; any other gets the plain triple, so a caller that cannot mark it still moves)."""
    from harness_manager.core.pack import report_progress

    def emit(done: int) -> None:
        report_progress(report, phase, done, total, ESTIMATED)

    def base_of(kind: Any) -> int:
        return sum(v for k, v in sent.items() if k != kind)

    return FrameEstimator(emit, base_of, nominal_bps=nominal_bps, tick_s=TICK_S)
