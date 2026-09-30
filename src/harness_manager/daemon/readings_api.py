"""The board's readings, kept by this service (lane UI2-API-BUILD, gap G4), loaded through
``app.EXTENSIONS``. docs/API.md "Readings" is the contract.

| Method and path | Returns |
|---|---|
| ``GET /boards/{bid}/readings/history?name=&since=&limit=`` | ``{board_id, series: [{name, unit, source, points: [[at, value]]}], spacing_s, capacity, oldest}`` |

and the additive keys of ``GET /boards/{bid}`` (``answer_ms``, ``uptime_s``, ``os_uptime_s``,
``readings_at``, ``readings_source``, ``stats``). ``services.history`` does the work; this
module holds the daemon's one ``ReadingsHistory`` (``d.readings``) and the two hooks the core
routes call:

- ``info_extra(daemon, bid, board_info, answer_ms)``: the additive keys for the reply of
  ``GET /boards/{bid}`` (and ``POST /boards``), and the ``answer_ms`` point;
- ``note_telemetry(daemon, bid, readings)``: a ``GET /boards/{bid}/telemetry`` read into the
  history.

Neither touches the board: they take what the core route already read. The history route
reads memory only, so the background gate never holds it back and a job never makes it wait.
"""

from __future__ import annotations

from typing import Any

from harness_manager.core.errors import HarnessError
from harness_manager.services.history import (
    ReadingsHistory,
    facts_of,
    info_fields,
    query_args,
)

from .app import _JSON, RouteContext, ok


def history_of(daemon: Any) -> ReadingsHistory:
    """The daemon's history (made on first use, so a hook called before ``register`` works)."""
    hist = getattr(daemon, "readings", None)
    if not isinstance(hist, ReadingsHistory):
        engine = getattr(daemon, "engine", None)
        seed = getattr(engine, "readings_seed", None)
        hist = ReadingsHistory(seed=seed if callable(seed) else None)
        daemon.readings = hist
    return hist


def info_extra(daemon: Any, bid: str, board_info: Any, answer_ms: float | None) -> dict[str, Any]:
    """The additive keys of a board read (docs/API.md "Readings"); keeps ``answer_ms``.
    Never raises: a board read is never failed by its extras."""
    try:
        hist = history_of(daemon)
        if answer_ms is not None:
            hist.note_answer(bid, answer_ms)
        try:
            session = daemon.engine.session(bid)
        except HarnessError:
            session = None
        return info_fields(answer_ms, facts_of(session))
    except Exception:  # noqa: BLE001 - the extras are a nicety of the read
        return info_fields(answer_ms, facts_of(None))


def note_telemetry(daemon: Any, bid: str, readings: Any) -> None:
    """A telemetry read into the history. Never raises."""
    try:
        history_of(daemon).note_readings(bid, list(readings))
    except Exception:  # noqa: BLE001 - the history is a nicety of the read
        return


def register(ctx: RouteContext) -> None:
    d = ctx.daemon
    hist = history_of(d)

    @ctx.api.get("/boards/{bid:path}/readings/history")
    def readings_history(bid: str, name: str | None = None, since: str | None = None,
                         limit: str | None = None) -> Any:
        names, when, count = query_args(name, since, limit, hist.capacity)
        return _JSON(ok(**hist.history(bid, names=names, since=when, limit=count)))
