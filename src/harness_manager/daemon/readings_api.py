"""The board's readings, kept by this service (lane UI2-API-BUILD, gap G4), loaded through
``app.EXTENSIONS``. docs/API.md "Readings" is the contract.

| Method and path | Returns |
|---|---|
| ``GET /boards/{bid}/readings/history?name=&since=&limit=`` | ``{board_id, series: [{name, unit, source, points: [[at, value]]}], spacing_s, capacity, oldest}`` |
| ``GET /boards/{bid}/sensors`` | ``{board_id, sensors}``: the harness's ``sensors`` body (mint 4, feature ``sysmon``): ``ref, k, cal, temp_c, temp_raw_c, temp_max_c, temp_min_c, vccint_v, vccaux_v, vccbram_v, touch_temp_c, alm, thr`` plus ``alarms`` (the latched bits in words) and ``calibrated``. 422 UNAVAILABLE with the reason on a board without the sensors |
| ``POST /boards/{bid}/sensors`` ``{act: "set", temp_hi_c?, temp_ot_c?, vccint_lo_v?, vccint_hi_v?, k?}`` or ``{act: "clear_alm"}`` | ``{board_id, act, sensors}`` (``sensors.persisted`` on ``set``). CLAIM-LOCKED: goes through the claim forward; 409 REFUSED with the lock's words when the claim is not this Harness Manager's. 400 USAGE (nothing sent) for a value out of range |

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

from harness_manager.core.errors import HarnessError, UnavailableError, UsageError
from harness_manager.services.history import (
    ReadingsHistory,
    facts_of,
    info_fields,
    query_args,
)

from .app import _JSON, JsonBody, RouteContext, _obj, ok


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

    def sensors_of(bid: str) -> Any:
        """The pack's telemetry adapter when it serves ``sensors`` (an optional extra)."""
        adapter = getattr(ctx.board(bid), "telemetry", None)
        if adapter is None or not callable(getattr(adapter, "sensors_get", None)):
            raise UnavailableError("sensors", "this board's pack has no temperature and voltage "
                                              "sensors")
        return adapter

    @ctx.api.get("/boards/{bid:path}/sensors")
    def sensors_get(bid: str) -> Any:
        adapter = sensors_of(bid)
        with d.gates.op(bid):
            body = adapter.sensors_get()
        return _JSON(ok(board_id=bid, sensors=body))

    @ctx.api.post("/boards/{bid:path}/sensors")
    def sensors_act(bid: str, body: JsonBody = None) -> Any:
        b = _obj(body)
        act = b.get("act")
        if act not in ("set", "clear_alm"):
            raise UsageError("act must be set or clear_alm")
        fields = {k: v for k, v in b.items() if k != "act"}
        adapter = sensors_of(bid)
        with d.gates.op(bid):
            out = adapter.sensors_act(act, fields)
        return _JSON(ok(board_id=bid, act=act, sensors=out))
