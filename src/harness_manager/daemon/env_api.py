"""``GET /api/v1/daemon/env``: the service's own tool variables (FIX-PACK-2 item 6).

What the service started with (``service_env.capture`` in ``server.run_daemon``), which of
those variables set a setting (``in_effect``) or hide the user's own value (``hides_yours``),
and the one-line ``warning`` the app shows when a Tools variable hides the user's setting.
Bearer auth like every /api/v1 route (never ``/health``: values are paths on this machine).
A secret's value is never in the answer.
"""

from __future__ import annotations

from typing import Any

from . import service_env


def register(ctx: Any) -> None:
    from .app import _JSON, ok

    d = ctx.daemon

    @ctx.api.get("/daemon/env")
    def daemon_env() -> Any:
        snap = getattr(d, "env_at_start", None)
        if snap is None:                     # an app made without run_daemon (tests, embeds)
            snap = service_env.capture()
        return _JSON(ok(**service_env.describe(snap)))
