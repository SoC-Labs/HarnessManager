"""The XDC routes (docs/API.md "XDC export", ``xdc_api.py``) in the T14 mock daemon.

The XDC service is board-free and deterministic, so the mock runs the real one over the
real pin model: only the board side is the mock's (its sessions, its job gate). The
request parsing and the board-static rule are ``daemon/xdc_api.py``'s own helpers
(``_request``, ``board_static``, ``check_board_static``), so the mock and the daemon cannot
disagree on them. The mock does not check that ``pack`` is a string (the daemon does).
"""

from __future__ import annotations

from typing import Any

from fastapi import Body, FastAPI
from fastapi.responses import Response

from harness_manager.daemon.xdc_api import (
    _request,
    board_static,
    check_board_static,
    running_static,
)
from harness_manager.services import xdc

API = "/api/v1"


def register(app: FastAPI, state: Any, ok: Any) -> None:
    """Add the four XDC routes to the mock app (``ok`` is the mock's envelope helper)."""

    def answer(kit: xdc.Kit, preview: bool, fmt: str) -> Any:
        if not preview:
            kit.require_ok()                  # 409 REFUSED with error.data.checks
        if fmt == "zip":
            return Response(kit.zip_bytes(), media_type="application/zip",
                            headers={"Content-Disposition":
                                     f'attachment; filename="{kit.design.get("name")}_{kit.kind}.zip"',
                                     "X-Xdc-Ok": "1" if kit.ok else "0"})
        return ok(**kit.to_json())

    def static_of(bid: str, s: Any, model: xdc.PinModel) -> dict[str, Any]:
        def read() -> Any:
            state.jobs.gate(bid)
            return s.identity()
        return board_static(*running_static(s, read), model)

    def pack_of(s: Any) -> str:
        return str(getattr(getattr(s, "candidate", None), "pack", "") or "mps3")

    @app.get(f"{API}/xdc")
    def xdc_catalogue(pack: str = "mps3") -> dict[str, Any]:
        return ok(**xdc.catalogue(pack))

    @app.post(f"{API}/xdc/export")
    def xdc_export(body: dict[str, Any] = Body(default_factory=dict)) -> Any:  # noqa: B008
        kit_name, design, preview, fmt, sid = _request(body)
        kit = xdc.export(str(body.get("pack") or "mps3"), kit_name, design, static_id=sid)
        return answer(kit, preview, fmt)

    @app.get(f"{API}/boards/{{bid}}/xdc")
    def board_xdc(bid: str) -> dict[str, Any]:
        s = state.session(bid)
        pins = xdc.load_pack_pins(pack_of(s))
        return ok(**xdc.catalogue(pins.pack, pins=pins), board=static_of(bid, s, pins.model),
                  board_id=bid)

    @app.post(f"{API}/boards/{{bid}}/xdc/export")
    def board_xdc_export(bid: str, body: dict[str, Any] = Body(default_factory=dict)) -> Any:  # noqa: B008
        s = state.session(bid)
        kit_name, design, preview, fmt, sid = _request(body)
        pins = xdc.load_pack_pins(pack_of(s))
        kit = xdc.export(pins.pack, kit_name, design, static_id=sid, pins=pins)
        check_board_static(kit, static_of(bid, s, pins.model), pins.model, bid)
        return answer(kit, preview, fmt)
