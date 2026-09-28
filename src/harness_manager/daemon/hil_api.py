"""The HIL checks routes (lane HIL-GUI): start, follow and stop the unattended runbooks.

docs/API.md "HIL checks: the unattended runbooks from the app" (``services/hil_runs.py`` does
the work):

| Method and path | Body | Returns |
|---|---|---|
| ``GET /boards/{bid}/checks`` | none | ``{board_id, run, last, runs, plans, defaults}`` |
| ``POST /boards/{bid}/checks`` | ``{plan?, writes?, until?, interval_s?, repeat?, start_at?, take_lease?, evidence?, expect_static?, margin_min?, gap_s?, max_unreachable?, stop_on_first_fail?, announce_only?}`` | ``{run}``; with ``announce_only``: ``{plan, auto, announce, lease, refusal}`` and nothing started |
| ``DELETE /boards/{bid}/checks`` | none | ``{run}``: stopping (the runner's SIGINT) or cancelled |
| ``GET /boards/{bid}/checks/{run}/report?iteration=&name=`` | none | ``{run, iteration, path, name, text}``: ``REPORT.md`` or ``ANNOUNCE.txt`` |

None of these routes touches the board except ``POST`` with ``plan: auto`` (the identity and the
card are read to pick the plan: an explicit action) and the run itself. None takes the board's
job gate: a run's own commands are the CLI's, through this service, and take it as they go.
Refusals: 404 ABSENT (the board is not open here; no run to stop), 409 ALREADY (one run per
board), 409 HELD (the lease is someone else's), 409 REFUSED (the lease is free and
``take_lease`` is not set; the evidence folder holds evidence), 422 UNAVAILABLE (no hub),
400 USAGE (a bad field).

Loaded by ``create_app`` through ``EXTENSIONS``, before the core ``/boards/{bid:path}`` routes.
"""

from __future__ import annotations

from typing import Any

from harness_manager.core.errors import UsageError
from harness_manager.services.hil_runs import HilRuns

from .app import _JSON, JsonBody, RouteContext, _obj, ok


def _iteration(value: str | None) -> int | None:
    if value in (None, ""):
        return None
    try:
        n = int(str(value))
    except ValueError:
        raise UsageError(f"iteration must be a whole number, not {value!r}") from None
    if n < 1:
        raise UsageError("iteration counts from 1")
    return n


def register(ctx: RouteContext) -> None:
    d = ctx.daemon
    runs = HilRuns(d.engine, d.state_dir, bus=d.bus, leases=lambda: getattr(d, "leases", None),
                   gates=d.gates)
    d.checks = runs                          # tests (and other lanes) reach it here
    d.close_guards.append(runs.guard_close)
    d.shutdown_guards.append(runs.guard_shutdown)
    original_close = d.close

    def close() -> None:
        # The service is stopping: each run finishes its check and restores greybox first.
        runs.close()
        original_close()

    d.close = close

    @ctx.api.get("/boards/{bid:path}/checks/{run_id}/report")
    def checks_report(bid: str, run_id: str, iteration: str | None = None,
                      name: str | None = None) -> _JSON:
        return _JSON(ok(**runs.report(bid, run_id, iteration=_iteration(iteration),
                                      name=name or "REPORT.md")))

    @ctx.api.get("/boards/{bid:path}/checks")
    def checks_status(bid: str) -> _JSON:
        out: dict[str, Any] = runs.status(bid)
        out["open"] = bid in d.engine.open_boards()
        return _JSON(ok(**out))

    @ctx.api.post("/boards/{bid:path}/checks")
    def checks_start(bid: str, body: JsonBody = None) -> _JSON:
        b = _obj(body)
        announce_only = b.pop("announce_only", False)
        if not isinstance(announce_only, bool):
            raise UsageError("announce_only must be true or false")
        if announce_only:
            return _JSON(ok(**runs.preview(bid, b)))
        run = runs.start(bid, b)
        return _JSON(ok(board_id=bid, run=run.public()))

    @ctx.api.delete("/boards/{bid:path}/checks")
    def checks_stop(bid: str) -> _JSON:
        run = runs.stop(bid)
        return _JSON(ok(board_id=bid, run=run.public()))
