"""SD-FLASH's card-reader routes as a Windows laptop's service answers them (lane WINDOWS),
for the wizard's browser tests: ``GET /cardwriter/devices`` with ``platform: "win32"`` and a
USB reader's card (``\\\\.\\PhysicalDrive2``, needs_privilege); ``POST /cardwriter/write`` of
kind ``card`` ends ``needs_privilege`` with the REAL Windows steps
(``cardwriter.privileged_commands`` for that disk). The unsigned and confirm phrases are
checked as the product checks them. Nothing is written anywhere."""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Body, FastAPI
from fastapi.responses import JSONResponse

from harness_manager.core.errors import RefusedError
from harness_manager.services import cardwriter as cw

JsonBody = Annotated[Any, Body()]
DISK = cw.Disk("PhysicalDrive2", cw.windows_disk_path(2), 15_931_539_456,
               model="Generic- MicroSD/M2 USB Device", transport="usb", removable=True,
               platform="win32", number=2)
DEVICE = {"id": "PhysicalDrive2-0a1b2c3d4e", "path": DISK.path, "model": DISK.model,
          "size_bytes": DISK.size, "size": "15.9 GB", "removable": True, "mounted": ["F:\\"],
          "fs": "vfat", "writable": True, "needs_privilege": True,
          "confirm": "WRITE Generic- MicroSD/M2 USB Device 15.9 GB",
          "kinds": {"files": {"ok": False, "why_not": "not a V2M-MPS3 card"},
                    "card": {"ok": True, "why_not": ""}}}


class WinCardwriter:
    def __init__(self) -> None:
        self.writes: list[dict[str, Any]] = []

    def attach(self, app: FastAPI) -> WinCardwriter:
        d = app.state.daemon
        r = APIRouter(prefix="/api/v1")

        @r.get("/cardwriter/devices")
        def devices() -> Any:
            return {"ok": True, "enabled": True, "supported": True, "platform": "win32",
                    "devices": [DEVICE], "excluded": []}

        @r.post("/cardwriter/write")
        def write(body: JsonBody = None) -> Any:
            b = body if isinstance(body, dict) else {}
            if b.get("device_id") != DEVICE["id"]:
                raise RefusedError(f"no card reader {b.get('device_id')!r} is listed")
            from harness_manager.services import unsigned

            info = unsigned.of_path(str(b.get("source") or ""))
            unsigned.require(info, b.get("confirm_unsigned"))
            if b.get("confirm") != DEVICE["confirm"]:
                raise RefusedError(f"the confirm phrase is not {DEVICE['confirm']!r}")
            self.writes.append(dict(b))

            def run(progress: Any) -> Any:
                out = cw.privileged_commands(DISK, str(b["source"]), 536_870_912, info.sha256)
                return {"outcome": "needs_privilege", "needs_privilege": True,
                        "verified": False, **out}

            job = d.jobs.submit("cardwriter_write", "", run)
            return JSONResponse({"ok": True, "job": job.id}, status_code=202)

        routes = app.router.routes
        before = list(routes)
        app.include_router(r)
        mine = [x for x in routes if not any(x is y for y in before)]
        for x in mine:
            routes.remove(x)
        routes[0:0] = mine                     # ahead of the daemon's catch-all 404
        return self
