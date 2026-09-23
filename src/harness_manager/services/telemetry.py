"""Telemetry aggregation with provenance and a maximum age.

Sources on a session, asked in this order when the adapter exists:

1. ``session.telemetry.readings()``           (T9: SYSMON, STMPE811, smart plugs, ...)
2. ``session.controller.temperatures()``      (T3: the MCC's ``CFG R TEMP``)
3. ``session.controller.oscillators()``       (T3: the MCC's ``CFG R OSC``)

Honest-data rules (docs/TEAM_PLAN.md §2.7):

- Every reading keeps its ``source``. A reading an adapter left unlabelled is
  labelled with the adapter it came from.
- A reading older than ``max_age_s`` keeps its value and gains the note
  ``stale (N s old)`` in ``reason``.
- A source that raises becomes one ``Reading.unavailable`` carrying the error
  text. It is never an exception out of the service, and never a 0.
- No sources at all gives one explicit unavailable ``temperature`` reading.
  An empty list is never returned.
"""

from __future__ import annotations

import dataclasses
import logging
import time
from collections.abc import Callable, Sequence

from harness_manager.core.model import Reading
from harness_manager.core.pack import BoardSession

log = logging.getLogger(__name__)

DEFAULT_MAX_AGE_S = 60.0
NO_SOURCE_REASON = "no telemetry source on this board/link set"
EMPTY_SOURCES_REASON = "the telemetry sources returned no readings"


@dataclasses.dataclass(frozen=True)
class _Source:
    label: str                 # provenance when an adapter leaves ``source`` empty
    fallback_name: str         # the reading name used when the source raises
    fallback_unit: str
    fetch: Callable[[], Sequence[Reading]]


class TelemetryService:
    def __init__(self, max_age_s: float = DEFAULT_MAX_AGE_S, *,
                 clock: Callable[[], float] = time.time) -> None:
        self.max_age_s = max_age_s
        self._clock = clock

    def readings(self, session: BoardSession) -> list[Reading]:
        """Every reading from every source, with provenance; stale values are flagged."""
        now = self._clock()
        sources = _sources(session)
        if not sources:
            return [self._missing("temperature", "degC", NO_SOURCE_REASON, "", now)]
        out: list[Reading] = []
        for src in sources:
            out.extend(self._gather(src, now))
        if not out:
            labels = dict.fromkeys(s.label for s in sources)       # ordered, no repeats
            return [self._missing("temperature", "degC", EMPTY_SOURCES_REASON,
                                  ", ".join(labels), now)]
        return [self._flag_stale(r, now) for r in out]

    # -- internals -----------------------------------------------------------------

    @staticmethod
    def _missing(name: str, unit: str, reason: str, source: str, now: float) -> Reading:
        """An unavailable reading observed now (by the service's clock)."""
        return dataclasses.replace(Reading.unavailable(name, unit, reason, source=source),
                                   observed_at=now)

    def _gather(self, src: _Source, now: float) -> list[Reading]:
        try:
            got = list(src.fetch())
        except Exception as err:  # noqa: BLE001 - a failing sensor is a reading, not a crash
            log.warning("telemetry source %s failed: %s", src.label, err)
            reason = str(err) or type(err).__name__
            return [self._missing(src.fallback_name, src.fallback_unit, reason, src.label, now)]
        out: list[Reading] = []
        for r in got:
            if not isinstance(r, Reading):
                out.append(self._missing(
                    src.fallback_name, src.fallback_unit,
                    f"source returned a {type(r).__name__}, not a Reading", src.label, now))
                continue
            out.append(r if r.source else dataclasses.replace(r, source=src.label))
        return out

    def _flag_stale(self, r: Reading, now: float) -> Reading:
        age = now - r.observed_at
        if age <= self.max_age_s:
            return r
        note = f"stale ({int(age)} s old)"
        return dataclasses.replace(r, reason=f"{r.reason}; {note}" if r.reason else note)


def _sources(session: BoardSession) -> list[_Source]:
    sources: list[_Source] = []
    tel = getattr(session, "telemetry", None)
    if tel is not None and callable(getattr(tel, "readings", None)):
        sources.append(_Source("telemetry", "telemetry", "", tel.readings))
    ctl = getattr(session, "controller", None)
    if ctl is not None:
        if callable(getattr(ctl, "temperatures", None)):
            sources.append(_Source("controller", "temperature", "degC", ctl.temperatures))
        if callable(getattr(ctl, "oscillators", None)):
            sources.append(_Source("controller", "oscillators", "MHz", ctl.oscillators))
    return sources
