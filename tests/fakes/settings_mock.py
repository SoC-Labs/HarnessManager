"""The settings routes (docs/API.md "Settings", ``settings_api.py``) in the T14 mock daemon.

Settings are board-free, so the mock runs the REAL routes: ``daemon/settings_api.register``
with a ``RouteContext`` whose daemon side is the mock's (its engine, bus and jobs), over a
real resolver whose files live in a temporary directory of the mock's own. It never reads
or writes the user's settings, policy or keyring: the policy is a file in that directory
(absent until a test writes it: ``app.state.settings.policy_path``), and the secret store
has no keyring (the 0600 file only). The directory goes when the app does.
"""

from __future__ import annotations

import shutil
import tempfile
import weakref
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from fastapi import APIRouter, FastAPI

from harness_manager.daemon import settings_api
from harness_manager.daemon.app import RouteContext
from harness_manager.settings.ops import SettingsContext

API = "/api/v1"


def register(app: FastAPI, state: Any, accepted: Any) -> SettingsContext:
    """Add the settings routes to the mock app (``accepted`` is the mock's 202 helper).
    Returns the settings context (its ``state_dir``, ``policy_path`` and ``env`` are the
    knobs a test turns)."""
    root = Path(tempfile.mkdtemp(prefix="hm-mock-settings-"))
    weakref.finalize(app, shutil.rmtree, str(root), True)
    sctx = SettingsContext(state_dir=root / "state", env={"HARNESS_MANAGER_KEYRING": "off"},
                           policy_path=root / "policy.toml", keyrings=[],
                           packs=getattr(state.engine, "packs", None))
    daemon = SimpleNamespace(
        engine=state.engine, bus=state.jobs.bus, state_dir=root / "state", settings=sctx,
        jobs=SimpleNamespace(submit=lambda kind, board_id, fn: state.jobs.start(board_id, kind,
                                                                                fn)))
    router = APIRouter(prefix=API)
    settings_api.register(RouteContext(daemon=daemon, api=router, wsr=None, board=state.session,
                                       require=None, accepted=accepted))
    app.include_router(router)
    return sctx
