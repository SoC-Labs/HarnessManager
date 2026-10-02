"""SD cards in THIS PC's card reader over the daemon API (lane SD-FLASH; docs/API.md "SD
cards in this PC's card reader").

| Route | Does |
|---|---|
| ``GET /cardwriter/devices`` | ``{enabled, reason?, supported, platform, simulated, cap_bytes?, devices, excluded}`` |
| ``POST /cardwriter/write`` ``{device_id, kind, source, confirm, backup_path?, backup_dir?, allow_mcc_update?}`` | 202 job ``cardwriter_write`` |

The service is ``services.cardwriter`` (the CLI's ``flash`` verb runs the same code
in-process). The rules:

- **Off by default** (``bringup.sd_flash``): ``devices`` answers ``enabled: false`` with the
  reason and lists nothing (nothing is run); ``write`` is 422 UNAVAILABLE with that reason.
  Windows answers the same way with "not supported on Windows yet".
- **Everything is checked before the 202**: the device is listed again and must still have
  the id it was listed with (a swapped card has another: 409 REFUSED), the kind must suit it,
  ``confirm`` must be exactly its ``WRITE <model> <size>`` (409 REFUSED, ``error.data.confirm``
  says the phrase); ``kind: "card"``: a whole-card image (an MBR and a bootable stage0 slot;
  ``linux_slot.img`` alone is refused) that fits; ``kind: "files"``: a bundle with no ``.ebf``
  that keeps the card's MCC firmware selection (``MBBIOS``; ``allow_mcc_update`` is the
  explicit override for the one case HM refuses).
- **A ``files`` write backs the card up first**: into ``backup_dir`` (default: this
  service's ``backups/``), or from ``backup_path`` (a backup of this card, verified).
- **The job** (``cardwriter_write``, engine-wide like a harness fetch: one at a time) lists
  the device again, writes, reads back. Events: ``cardwriter.progress {device_id, kind,
  phase: "backup"|"unmount"|"write"|"verify", bytes, total}`` and ``cardwriter.done
  {device_id, kind, outcome, verified, sha256, ...}``. ``outcome``: ``written``,
  ``needs_privilege`` (the job is DONE, nothing written: ``privileged_command``,
  ``verify_command``, ``verify_expect``) or ``verify_failed`` (the job FAILS, ACTION_FAILED).
- ``--demo``: simulated card readers (temp files in the demo's state dir), never a real one.
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any

from harness_manager.core.errors import HeldError, UsageError
from harness_manager.core.events import Event
from harness_manager.services import cardwriter as cw

from .app import _JSON, JsonBody, RouteContext, _abs_path, _bool, _obj, _str, ok

ENGINE = ""
JOB_KIND = "cardwriter_write"


def register(ctx: RouteContext) -> None:
    d = ctx.daemon
    api = ctx.api
    mu = threading.Lock()
    state: dict[str, Any] = {"writer": None}

    def publish(topic: str, data: dict[str, Any]) -> None:
        d.bus.publish(Event(topic, ENGINE, data))

    def writer() -> cw.CardWriter:
        with mu:
            if state["writer"] is None:
                # The settings are read where settings_api writes them (d.state_dir).
                if getattr(d.engine, "fake_boards", False):          # --demo: never a real card
                    state["writer"] = cw.demo_writer(Path(d.state_dir), publish=publish)
                else:
                    state["writer"] = cw.CardWriter(state_dir=d.state_dir, publish=publish)
            return state["writer"]

    d.card_writer = writer          # the bring-up's card-reader door writes with the same one

    @api.get("/cardwriter/devices")
    def devices() -> _JSON:
        return _JSON(ok(**writer().devices_json()))

    @api.post("/cardwriter/write")
    def write(body: JsonBody = None) -> _JSON:
        b = _obj(body)
        w = writer()
        w.require()                                         # 422: off, or not on this OS
        kind = _str(b, "kind")
        if kind not in cw.KINDS:
            raise UsageError(f"kind must be files or card, not {kind!r}")
        source = _abs_path(b.get("source"), "source")
        confirm = b.get("confirm", "")
        if not isinstance(confirm, str):
            raise UsageError("confirm must be the typed phrase (WRITE <model> <size>)")
        backup_path = _abs_path(b["backup_path"], "backup_path") if b.get("backup_path") else None
        backup_dir = _abs_path(b["backup_dir"], "backup_dir") if b.get("backup_dir") else None
        if kind == "files" and backup_path is None and backup_dir is None:
            backup_dir = Path(d.state_dir) / "backups"
        other = d.gates.busy(ENGINE)
        if other is not None:
            err = HeldError(f"{other.describe()} is running in the service",
                            holder=f"harness-manager-daemon {other.describe()}",
                            hint=f"wait for it to finish (GET /api/v1/jobs/{other.id})")
            err.data = {"job": other.id, "kind": other.kind, "board_id": ENGINE}  # type: ignore[attr-defined]
            raise err
        plan = w.prepare(_str(b, "device_id"), kind, source, confirm, backup_path=backup_path,
                         backup_dir=backup_dir,
                         allow_mcc_update=_bool(b, "allow_mcc_update", False))
        return ctx.accepted(d.jobs.submit(JOB_KIND, ENGINE,
                                          lambda progress: w.run(plan, progress)))
