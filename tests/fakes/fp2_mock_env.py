"""FIX-PACK-2 item 6's route (docs/API.md "The service's own environment", ``env_api.py``) in
the T14 mock: the real ``service_env.describe`` over a scripted environment
(``app.state.service_env``, empty by default), so a browser test can show the app's warning
without touching the test process's own environment.
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI

from harness_manager.daemon import service_env

API = "/api/v1"


def register(app: FastAPI, ok: Any) -> None:
    app.state.service_env = {}

    @app.get(f"{API}/daemon/env")
    def daemon_env() -> dict[str, Any]:
        return ok(**service_env.describe(dict(app.state.service_env)))
