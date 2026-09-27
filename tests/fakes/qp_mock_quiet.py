"""QUIET-POLL's routes (docs/API.md "Background reads", ``quiet_api.py``) in the T14 mock.

The mock serves the demo's boards, whose gate allows every background read
(``services.quiet.BackgroundGate(enabled=False)``, as ``DemoEngine.fake_boards`` makes the
real service's): a page's viewer is counted and nothing is ever held back.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import Body, FastAPI

from harness_manager.services.quiet import BackgroundGate

API = "/api/v1"
JsonBody = Annotated[Any, Body()]


def register(app: FastAPI, state: Any, ok: Any) -> BackgroundGate:
    gate = BackgroundGate(enabled=False)

    @app.put(f"{API}/boards/{{bid}}/viewers/{{vid}}")
    def view(bid: str, vid: str, body: JsonBody = None) -> dict[str, Any]:
        state.session(bid)
        ttl = (body or {}).get("ttl_s") if isinstance(body, dict) else None
        gate.view(bid, vid, ttl)
        return ok(board_id=bid, viewer=vid, background=gate.state(bid))

    @app.delete(f"{API}/boards/{{bid}}/viewers/{{vid}}")
    def unview(bid: str, vid: str) -> dict[str, Any]:
        return ok(board_id=bid, viewer=vid, removed=gate.unview(bid, vid))

    @app.get(f"{API}/boards/{{bid}}/background")
    def background(bid: str) -> dict[str, Any]:
        state.session(bid)
        return ok(board_id=bid, background=gate.state(bid))

    return gate
