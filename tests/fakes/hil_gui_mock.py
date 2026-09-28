"""HIL-GUI's routes (docs/API.md "HIL checks", ``hil_api.py``) in the T14 mock.

The mock serves the routes so the page's contract holds over it too: the Checks section
lists the plans and the defaults and has no past runs. Runs are the real service's
(``tests/web/test_hil_gui_browser.py`` drives them over the real daemon): here a Start is
422 UNAVAILABLE, saying so, and nothing is ever sent to a board.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Any

from fastapi import Body, FastAPI

from harness_manager.core.errors import AbsentError, UnavailableError
from harness_manager.services.hil_runs import CAPABILITY, HilRuns

API = "/api/v1"
JsonBody = Annotated[Any, Body()]


def register(app: FastAPI, state: Any, ok: Any) -> None:
    def status(bid: str) -> dict[str, Any]:
        state.session(bid)
        runs = HilRuns(state.engine, Path("/nonexistent"))
        return ok(board_id=bid, run=None, last=None, runs=[], plans=runs.plans(),
                  defaults=runs.defaults(), open=True)

    @app.get(f"{API}/boards/{{bid}}/checks/{{run}}/report")
    def report(bid: str, run: str) -> dict[str, Any]:
        state.session(bid)
        raise AbsentError(f"no checks run {run} on {bid} (the T14 mock runs no checks)")

    @app.get(f"{API}/boards/{{bid}}/checks")
    def checks(bid: str) -> dict[str, Any]:
        return status(bid)

    @app.post(f"{API}/boards/{{bid}}/checks")
    def start(bid: str, body: JsonBody = None) -> dict[str, Any]:
        state.session(bid)
        raise UnavailableError(CAPABILITY, "the T14 mock runs no checks: the real Harness "
                                           "Manager service does")

    @app.delete(f"{API}/boards/{{bid}}/checks")
    def stop(bid: str) -> dict[str, Any]:
        state.session(bid)
        raise AbsentError(f"no checks run is active on {bid}")
