"""What this service has read from each board, kept for the Overview (lane UI2-API-BUILD, gap G4).

docs/planning/UI_V2_PLAN.md §2 G4 and docs/API.md "Readings". Two things, both kept in
memory only (a service restart forgets them) and both fed by reads the service makes
anyway, so nothing here ever talks to a board:

- **the facts beside a board read** (``BoardFacts``): how long ``engine.info`` took
  (``answer_ms``), how long the harness and its OS have been up, and the harness's
  ``stats`` reply, whitelisted (``stats_fields``). ``facts_of(session)`` asks the session's
  optional seam ``readings_facts()`` (the demo's), else its telemetry adapter's (the MPS3
  pack's keeps the last ``stats`` reply its telemetry read made);
- **the history** (``ReadingsHistory``): a ring per board and series of ``(at, value)``,
  at most one point per ``spacing_s`` (30 s) per series and ``capacity`` (720) points, so
  six hours. A series is a numeric reading ``GET /telemetry`` returned (its name, unit and
  source) or ``answer_ms``. An engine may seed a board's past (``engine.readings_seed``:
  the demo's 30 minutes of temperature), the first time the board is seen.

Board-agnostic: the pack decides what its ``stats`` reply holds; this module keeps only the
keys ``STATS_KEYS`` names (and every ``svc_*``), so a harness adding a key never widens the
API by accident.
"""

from __future__ import annotations

import math
import threading
import time
from collections import deque
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

from harness_manager.core.errors import UsageError

#: At most one point per series this often (the page polls telemetry every 30 s).
SPACING_S = 30.0
#: Points kept per series: 720 x 30 s = 6 h.
CAPACITY = 720
#: The series name of the service's own answer time.
ANSWER_SERIES = "answer_ms"
ANSWER_SOURCE = "harness-manager (engine.info)"

#: The ``stats`` keys served whole (net-protocol v0.11 "Stats"), and every ``svc_*``.
STATS_KEYS = ("up_ms", "swap_n", "swap", "swap_ok", "icap", "rxdrop", "txerr", "link", "spd",
              "fdx", "clk_alive", "lock", "rm_ok")
STATS_PREFIXES = ("svc_",)


def stats_fields(raw: Mapping[str, Any] | None) -> dict[str, Any] | None:
    """The whitelisted part of a ``stats`` reply (only what the harness SENT: pyverify fills
    an absent key with 0, and a 0 there is not a measurement). None for no reply."""
    if not isinstance(raw, Mapping):
        return None
    out: dict[str, Any] = {}
    for key, value in raw.items():
        if key in STATS_KEYS or key.startswith(STATS_PREFIXES):
            if isinstance(value, (bool, int, float, str)) or value is None:
                out[key] = value
    return out


def _ms_to_s(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
        return None
    return round(float(value) / 1000.0, 3)


@dataclass(frozen=True)
class BoardFacts:
    """What a board said beside its identity and health, as last read (``to_json``)."""

    uptime_s: float | None = None      # the harness (bare metal: the shell; Linux: harnessd)
    os_uptime_s: float | None = None   # the Linux OS; None on bare metal
    stats: dict[str, Any] | None = None
    at: float | None = None            # when these were read (epoch seconds)
    source: str = ""                   # "stats (6900)", "identify (UDP 6899)", "demo"

    @classmethod
    def from_raw(cls, raw: Mapping[str, Any] | None) -> BoardFacts:
        """From a seam's answer: ``{stats?, up_ms?, os_up_ms?, at?, source?}``. The harness's
        ``up_ms`` comes from ``stats.up_ms`` when the seam does not name it."""
        if not isinstance(raw, Mapping):
            return cls()
        stats = stats_fields(raw.get("stats"))
        up = raw.get("up_ms")
        if up is None and stats is not None:
            up = stats.get("up_ms")
        at = raw.get("at")
        return cls(uptime_s=_ms_to_s(up), os_uptime_s=_ms_to_s(raw.get("os_up_ms")),
                   stats=stats, at=float(at) if isinstance(at, (int, float))
                   and not isinstance(at, bool) else None,
                   source=str(raw.get("source") or ""))

    def to_json(self) -> dict[str, Any]:
        return {"uptime_s": self.uptime_s, "os_uptime_s": self.os_uptime_s,
                "readings_at": self.at, "readings_source": self.source or None,
                "stats": self.stats}


def facts_of(session: Any) -> BoardFacts:
    """The session's facts from its optional seams; empty (every field None) when it has none.
    A seam that fails is no facts, never a failed read."""
    for owner in (session, getattr(session, "telemetry", None)):
        seam = getattr(owner, "readings_facts", None)
        if callable(seam):
            try:
                return BoardFacts.from_raw(seam())
            except Exception:  # noqa: BLE001 - an optional seam never fails the board read
                return BoardFacts()
    return BoardFacts()


def info_fields(answer_ms: float | None, facts: BoardFacts) -> dict[str, Any]:
    """The additive keys of ``GET /boards/{bid}`` (docs/API.md "Readings")."""
    return {"answer_ms": None if answer_ms is None else round(float(answer_ms), 1),
            **facts.to_json()}


@dataclass
class _Series:
    name: str
    unit: str
    source: str
    points: deque = field(default_factory=lambda: deque(maxlen=CAPACITY))

    def to_json(self, since: float | None, limit: int | None) -> dict[str, Any]:
        pts = [p for p in self.points if since is None or p[0] >= since]
        if limit is not None:
            pts = pts[-limit:]
        return {"name": self.name, "unit": self.unit, "source": self.source,
                "points": [[round(at, 3), v] for at, v in pts]}


Seed = Callable[[str], Iterable[Mapping[str, Any]]]


class ReadingsHistory:
    """The rings, one per (board, series). Thread-safe; memory only."""

    def __init__(self, *, spacing_s: float = SPACING_S, capacity: int = CAPACITY,
                 clock: Callable[[], float] = time.time, seed: Seed | None = None) -> None:
        self.spacing_s = spacing_s
        self.capacity = capacity
        self._clock = clock
        self._seed = seed
        self._mu = threading.Lock()
        self._boards: dict[str, dict[str, _Series]] = {}

    # -- writing -------------------------------------------------------------------------------

    def _board(self, board_id: str) -> dict[str, _Series]:
        """The board's series (the caller holds the lock); seeded the first time it is seen."""
        series = self._boards.get(board_id)
        if series is None:
            series = self._boards[board_id] = {}
            if self._seed is not None:
                try:
                    seeded = list(self._seed(board_id) or ())
                except Exception:  # noqa: BLE001 - a seed is a demo nicety, never a failure
                    seeded = []
                for s in seeded:
                    one = self._series(series, str(s.get("name", "")), str(s.get("unit", "")),
                                       str(s.get("source", "")))
                    for at, value in s.get("points") or ():
                        one.points.append((float(at), float(value)))
        return series

    def _series(self, series: dict[str, _Series], name: str, unit: str,
                source: str) -> _Series:
        one = series.get(name)
        if one is None:
            one = series[name] = _Series(name, unit, source,
                                         deque(maxlen=self.capacity))
        elif source:
            one.source = source
        return one

    def add(self, board_id: str, name: str, value: Any, *, unit: str = "", source: str = "",
            at: float | None = None) -> bool:
        """One point; False when it was not kept (not a finite number, or too soon after the
        series' last point: ``spacing_s``)."""
        if not name or isinstance(value, bool) or not isinstance(value, (int, float)) \
                or not math.isfinite(value):
            return False
        at = self._clock() if at is None else float(at)
        with self._mu:
            one = self._series(self._board(board_id), name, unit, source)
            if one.points and at - one.points[-1][0] < self.spacing_s:
                return False
            one.points.append((at, float(value)))
            return True

    def note_readings(self, board_id: str, readings: Iterable[Any]) -> int:
        """Every available numeric ``Reading`` of a telemetry read; how many were kept."""
        kept = 0
        for r in readings:
            value = getattr(r, "value", None)
            if value is None:
                continue
            kept += self.add(board_id, str(getattr(r, "name", "")), value,
                             unit=str(getattr(r, "unit", "")),
                             source=str(getattr(r, "source", "")),
                             at=getattr(r, "observed_at", None))
        return kept

    def note_answer(self, board_id: str, answer_ms: float) -> bool:
        """The service's answer time, one decimal as ``GET /boards/{bid}`` says it."""
        if isinstance(answer_ms, bool) or not isinstance(answer_ms, (int, float)):
            return False
        return self.add(board_id, ANSWER_SERIES, round(float(answer_ms), 1), unit="ms",
                        source=ANSWER_SOURCE)

    def forget(self, board_id: str) -> None:
        with self._mu:
            self._boards.pop(board_id, None)

    # -- reading -------------------------------------------------------------------------------

    def history(self, board_id: str, *, names: Iterable[str] = (), since: float | None = None,
                limit: int | None = None) -> dict[str, Any]:
        """``GET /boards/{bid}/readings/history`` (docs/API.md "Readings")."""
        wanted = {n for n in names if n}
        with self._mu:
            series = self._board(board_id) if board_id in self._boards or self._seed \
                else {}
            out = [s.to_json(since, limit) for n, s in sorted(series.items())
                   if not wanted or n in wanted]
        oldest = min((p[0][0] for p in (s["points"] for s in out) if p), default=None)
        return {"board_id": board_id, "series": out, "spacing_s": self.spacing_s,
                "capacity": self.capacity, "oldest": oldest}


def query_args(name: str | None, since: str | None, limit: str | None,
               capacity: int = CAPACITY) -> tuple[list[str], float | None, int | None]:
    """The route's query: ``name`` comma-separated, ``since`` epoch seconds, ``limit`` 1-N.
    ``UsageError`` (400) for a value that is not one."""
    names = [n.strip() for n in (name or "").split(",") if n.strip()]
    when: float | None = None
    if since not in (None, ""):
        try:
            when = float(since)          # type: ignore[arg-type]
        except ValueError:
            raise UsageError(f"since must be epoch seconds, not {since!r}") from None
        if not math.isfinite(when):
            raise UsageError(f"since must be epoch seconds, not {since!r}")
    count: int | None = None
    if limit not in (None, ""):
        try:
            count = int(limit)           # type: ignore[arg-type]
        except ValueError:
            raise UsageError(f"limit must be an integer 1-{capacity}, not {limit!r}") from None
        if not 1 <= count <= capacity:
            raise UsageError(f"limit must be an integer 1-{capacity}, not {limit!r}")
    return names, when, count
