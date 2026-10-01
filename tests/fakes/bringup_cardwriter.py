"""Lane SD-FLASH's card-reader routes, faked to the BRING-UP contract, for BRINGUP-USB's tests.

``GET /api/v1/cardwriter/devices`` -> ``{enabled, reason?, devices: [{id, path, model,
size_bytes, removable, mounted, fs, writable, why_not?}]}``; ``POST /api/v1/cardwriter/write``
``{device_id, kind: "files"|"card", source, confirm: "WRITE <model> <size>"}`` -> 202 job
``cardwriter_write`` (progress unmount/write/verify; result ``{verified, sha256}``, or
``{needs_privilege, privileged_command, verify}``); a refusal is 409/422 with the reason.

Nothing is written anywhere: the "device" is a name, and each write is recorded in
``writes``. Attached to the real daemon app in front of its catch-all route.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Body, FastAPI
from fastapi.responses import JSONResponse

from harness_manager.core.errors import RefusedError, UsageError

JsonBody = Annotated[Any, Body()]
DEVICE = {"id": "usb-Generic_STORAGE_DEVICE-0:0", "path": "/dev/fake-sdx", "model": "STORAGE DEVICE",
          "size_bytes": 31914983424, "removable": True, "mounted": ["/media/me/BOOT"],
          "fs": "vfat", "writable": True}
KINDS = ("files", "card")


class FakeCardwriter:
    def __init__(self, *, enabled: bool = True, reason: str = "", privilege: bool = False,
                 devices: list[dict[str, Any]] | None = None) -> None:
        self.enabled, self.reason, self.privilege = enabled, reason, privilege
        self.devices = [dict(DEVICE)] if devices is None else devices
        self.writes: list[dict[str, Any]] = []

    def attach(self, app: FastAPI) -> FakeCardwriter:
        d = app.state.daemon
        r = APIRouter(prefix="/api/v1")

        @r.get("/cardwriter/devices")
        def devices() -> Any:
            return {"ok": True, "enabled": self.enabled, "reason": self.reason,
                    "devices": self.devices}

        @r.post("/cardwriter/write")
        def write(body: JsonBody = None) -> Any:
            b = body if isinstance(body, dict) else {}
            if b.get("kind") not in KINDS:
                raise UsageError(f"kind must be one of {', '.join(KINDS)}, not {b.get('kind')!r}")
            dev = next((x for x in self.devices if x["id"] == b.get("device_id")), None)
            if dev is None:
                raise RefusedError(f"no card reader {b.get('device_id')!r} is listed")
            want = f"WRITE {dev['model']} {dev['size_bytes']}"
            if b.get("confirm") != want:
                raise RefusedError(f"the confirm phrase is not {want!r}", hint="nothing was written")
            self.writes.append(dict(b))

            def run(progress: Any) -> Any:
                for phase in ("unmount", "write", "verify"):
                    progress(phase, 1, 1)
                if self.privilege:
                    return {"needs_privilege": True,
                            "privileged_command": f"sudo dd if={b['source']} of={dev['path']} bs=4M",
                            "verify": f"sudo cmp {b['source']} {dev['path']}"}
                return {"verified": True, "sha256": "ab" * 32}

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
