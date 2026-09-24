"""The kit routes (docs/API.md "DUT build kits and the build guide", ``kit_api.py``) in the
T14 mock daemon.

The kit service is board-free and deterministic, so the mock runs the REAL routes: it
calls ``daemon/kit_api.register`` with a ``RouteContext`` whose daemon side is the mock's
(its sessions, its jobs, its board gate). The mock and the daemon therefore cannot
disagree on a kit route. The kits live in the state dir's content store
(``$HARNESS_MANAGER_STATE_DIR``: the tests point it at a temporary directory).
"""

from __future__ import annotations

from contextlib import contextmanager
from types import SimpleNamespace
from typing import Any

from fastapi import APIRouter, FastAPI

from harness_manager.daemon import kit_api
from harness_manager.daemon.app import RouteContext

API = "/api/v1"


def register(app: FastAPI, state: Any, accepted: Any) -> None:
    """Add the kit routes to the mock app (``accepted`` is the mock's 202 helper)."""

    @contextmanager
    def op(bid: str):
        state.jobs.gate(bid)                  # 409 HELD while a job runs on the board
        yield

    daemon = SimpleNamespace(
        engine=state.engine, bus=state.jobs.bus,
        gates=SimpleNamespace(op=op),
        jobs=SimpleNamespace(submit=lambda kind, board_id, fn: state.jobs.start(board_id, kind,
                                                                                fn)))
    router = APIRouter(prefix=API)
    kit_api.register(RouteContext(daemon=daemon, api=router, wsr=None, board=state.session,
                                  require=None, accepted=accepted))
    app.include_router(router)
