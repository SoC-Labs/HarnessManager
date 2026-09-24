"""XDC export over the daemon API (T10). The lead adds ``"xdc_api"`` to ``app.EXTENSIONS``.

Routes (bearer auth and the error envelope as everywhere; docs/XDC_EXPORT.md):

| Method and path | Returns |
|---|---|
| ``GET /xdc?pack=mps3`` | the catalogue: the model summary (board, status, shells, sources), the kits, the built-in designs and the check codes |
| ``POST /xdc/export`` ``{pack?, kit, design?, static_id?, preview?, format?}`` | the kit |
| ``GET /boards/{bid}/xdc`` | the catalogue for the board's pack, plus ``board: {static_id, model_static_id, matches, reason}`` |
| ``POST /boards/{bid}/xdc/export`` ``{kit, design?, preview?, format?}`` | the kit, checked against the static the board runs |

``kit`` is ``rm-kit`` or ``board``. ``design`` is a built-in design's name or an inline
design object (docs/XDC_EXPORT.md); a file path is not accepted over the API (the
daemon's filesystem is not the caller's).

- ``preview: true`` answers 200 with ``{kit, design, ok, files: {name: text}, checks,
  facts}`` even when checks fail, so a front end can show the files next to the failures.
- Otherwise a failed check is 409 REFUSED with ``error.data.checks`` (every finding).
- ``format: "zip"`` answers ``application/zip`` (the files plus ``manifest.json``).

None of these routes touches the board: the board routes read only the static id the
board reported when it was probed (``candidate.identity``), so they take no board gate
and are never 409 HELD.
"""

from __future__ import annotations

from typing import Any

from fastapi.responses import Response

from harness_manager.core.errors import UsageError
from harness_manager.services import xdc
from harness_manager.services.xdc.kits import Finding, same_static

from .app import _JSON, JsonBody, RouteContext, _obj, ok

FORMATS = ("json", "zip")


def _request(body: dict[str, Any]) -> tuple[str, Any, bool, str, str | None]:
    kit = body.get("kit")
    if kit not in xdc.KITS:
        raise UsageError(f"kit must be one of {', '.join(xdc.KITS)}, not {kit!r}")
    design = body.get("design")
    if design is not None and not isinstance(design, (str, dict)):
        raise UsageError("design must be a built-in design's name or a design object")
    if isinstance(design, str) and ("/" in design or "\\" in design or design.endswith(".json")):
        raise UsageError("design files are not read over the API",
                         hint="send the design document itself as the design object")
    preview = body.get("preview", False)
    if not isinstance(preview, bool):
        raise UsageError("preview must be true or false")
    fmt = body.get("format", "json")
    if fmt not in FORMATS:
        raise UsageError(f"format must be one of {', '.join(FORMATS)}")
    sid = body.get("static_id")
    if sid is not None and not isinstance(sid, str):
        raise UsageError("static_id must be a string such as 0x72BB0A36")
    return kit, design, preview, fmt, sid


def _answer(kit: xdc.Kit, preview: bool, fmt: str) -> Any:
    if not preview:
        kit.require_ok()                   # 409 REFUSED with error.data.checks
    if fmt == "zip":
        name = f"{kit.design.get('name')}_{kit.kind}.zip"
        return Response(kit.zip_bytes(), media_type="application/zip",
                        headers={"Content-Disposition": f'attachment; filename="{name}"',
                                 "X-Xdc-Ok": "1" if kit.ok else "0"})
    return _JSON(ok(**kit.to_json()))


def board_static(session: Any, model: xdc.PinModel) -> dict[str, Any]:
    """What static the board runs, as it said when probed, against the model's shell."""
    cand = getattr(session, "candidate", None)
    ident = getattr(cand, "identity", None)
    running = str(getattr(ident, "shell_id", "") or "")
    target = model.default_shell
    if not running:
        return {"static_id": None, "model_static_id": target, "matches": None,
                "reason": "the board did not report its static when it was probed"}
    match = same_static(running, target)
    return {"static_id": running, "model_static_id": target, "matches": match,
            "reason": "" if match else
            f"the board runs {running}; the pin model describes {target}, so an RM kit from "
            "it is for a different static"}


def register(ctx: RouteContext) -> None:
    api = ctx.api

    @api.get("/xdc")
    def xdc_catalogue(pack: str = "mps3") -> Any:
        return _JSON(ok(**xdc.catalogue(pack)))

    @api.post("/xdc/export")
    def xdc_export(body: JsonBody = None) -> Any:
        body = _obj(body)
        kit_name, design, preview, fmt, sid = _request(body)
        pack = body.get("pack") or "mps3"
        if not isinstance(pack, str):
            raise UsageError("pack must be a pack name")
        kit = xdc.export(pack, kit_name, design, static_id=sid)
        return _answer(kit, preview, fmt)

    def pack_of(session: Any) -> str:
        cand = getattr(session, "candidate", None)
        return str(getattr(cand, "pack", "") or "mps3")

    @api.get("/boards/{bid:path}/xdc")
    def board_xdc(bid: str) -> Any:
        s = ctx.board(bid)
        pins = xdc.load_pack_pins(pack_of(s))
        cat = xdc.catalogue(pins.pack, pins=pins)
        return _JSON(ok(**cat, board=board_static(s, pins.model), board_id=bid))

    @api.post("/boards/{bid:path}/xdc/export")
    def board_xdc_export(bid: str, body: JsonBody = None) -> Any:
        s = ctx.board(bid)
        body = _obj(body)
        kit_name, design, preview, fmt, sid = _request(body)
        pins = xdc.load_pack_pins(pack_of(s))
        kit = xdc.export(pins.pack, kit_name, design, static_id=sid, pins=pins)
        st = board_static(s, pins.model)
        if kit_name == "rm-kit" and st["matches"] is False and \
                same_static(kit.design.get("static_id", ""), pins.model.default_shell):
            kit.findings.append(Finding(
                "static_id", st["static_id"], st["reason"],
                hint="update the board to the fielded shell, or export without a board "
                     "(`harness-manager xdc rm-kit`) if the RM is meant for the model's static"))
        elif kit_name == "rm-kit" and st["matches"] is None:
            kit.findings.append(Finding("static_id", bid, st["reason"], severity="note"))
        return _answer(kit, preview, fmt)

