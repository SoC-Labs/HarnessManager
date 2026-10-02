"""SD-FLASH's routes (docs/API.md "SD cards in this PC's card reader", ``cardwriter_api.py``) in
the T14 mock daemon: the REAL routes, over ``--demo``'s simulated card readers (the mock's
engine is a ``DemoEngine``: ``fake_boards``), so nothing lists or writes a real device.

The setting ``bringup.sd_flash`` is read from the mock's own settings directory (the one the
mock's real settings routes write: ``app.state.settings.state_dir``), so a browser test turns
it on through Settings, ``PUT /settings``, or by writing that directory's settings.toml.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from fastapi import APIRouter, FastAPI

from harness_manager.daemon import cardwriter_api
from harness_manager.daemon.app import RouteContext

API = "/api/v1"


def register(app: FastAPI, state: Any, accepted: Any, settings: Any) -> None:
    """Add the card-writer routes (after the settings routes: ``settings`` is their context)."""
    daemon = SimpleNamespace(
        engine=state.engine, bus=state.jobs.bus, state_dir=settings.state_dir,
        gates=SimpleNamespace(busy=lambda board_id: next(iter(state.jobs.running(board_id)),
                                                         None)),
        jobs=SimpleNamespace(submit=lambda kind, board_id, fn: state.jobs.start(board_id, kind,
                                                                                fn)))
    router = APIRouter(prefix=API)
    cardwriter_api.register(RouteContext(daemon=daemon, api=router, wsr=None,
                                         board=state.session, require=None, accepted=accepted))
    app.include_router(router)
