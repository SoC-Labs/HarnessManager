"""Bring a new board up over its Debug USB (lane BRINGUP-USB; docs/API.md "Bring-up").

| Route | Does |
|---|---|
| ``GET /bringup`` | the wizard's switches: the default address, ``bringup.sd_flash``, the network OS door (not yet), the signing keys, whether this service has the card-reader routes |
| ``POST /bringup/scan`` ``{host?, ask_mcc?, timeout_s?}`` | the MPS3 Debug USBs this PC sees (the pack's USB probe), each with its MCC port, its V2M-MPS3 drive and what it holds, what the MCC answers (``ask_mcc``), and whether a harness answers at ``host`` |
| ``POST /bringup/bundle`` ``{path}`` | check a bundle folder or zip: the files, the base ``.bit`` (size, sha256, part, USERID); 409 REFUSED with ``error.data.check`` for an ``.ebf``, an MCC command file, a file outside the config-SD tree, no bitstream |
| ``POST /boards/{bid}/bringup/install`` ``{bundle, backup_path}`` | 202 job ``sd_install``: the bundle checked again, then written to the board's config SD by its storage adapter (the backup is mandatory; never an ``.ebf``); a release bundle's ``overlays/open`` then joins ``mps3.overlay_dirs`` (Program and Restore find them) |
| ``POST /boards/{bid}/bringup/witness`` ``{host?, wait_s?, poll_s?}`` | 202 job ``bringup_witness``: wait for the harness to answer at ``host`` after the reboot; ``state`` ``running`` or ``rescue``; a timeout fails the job with ``error.data.timeout`` |

Composed, not new executors: the backup, the reboot and the restore are the existing
``storage/backup``, ``controller/reboot`` and ``storage/restore`` jobs; a signed release
installs through ``harness/install``; the card reader is SD-FLASH's ``/cardwriter`` routes.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from harness_manager.cli.output import with_data
from harness_manager.core import capabilities as C
from harness_manager.core.errors import HarnessError, HeldError, RefusedError, UsageError
from harness_manager.services import bringup

from .app import _JSON, JsonBody, RouteContext, _abs_path, _bool, _number, _obj, ok

WITNESS_JOB = "bringup_witness"
INSTALL_JOB = "sd_install"           # the storage install's own kind: the UI knows it
CARDWRITER_ROUTE = "/cardwriter/devices"


def _host(b: dict[str, Any]) -> str:
    host = b.get("host", bringup.DEFAULT_HOST)
    if not isinstance(host, str) or not host.strip() or any(c.isspace() for c in host.strip()):
        raise UsageError(f"host must be an address, not {host!r}", hint="e.g. 192.168.10.101")
    return host.strip()


def register(ctx: RouteContext) -> None:
    d = ctx.daemon
    api = ctx.api

    def work_dir() -> Any:
        return d.state_dir / "bringup" / "bundles"

    def cardwriter_served() -> bool:
        return any(getattr(r, "path", "").endswith(CARDWRITER_ROUTE) for r in api.routes)

    def gate(bid: str) -> Any:
        return d.gates.op(bid)

    def no_board_job(what: str) -> None:
        running = [j for j in d.jobs.running() if j.board_id]
        if running:
            job = running[0]
            err = HeldError(f"{job.describe()} is running on {job.board_id}; {what} now could "
                            "take that board's ports", holder=f"harness-manager-daemon "
                                                              f"{job.describe()}",
                            hint="scan again when the job finishes")
            err.data = {"job": job.id, "kind": job.kind,  # type: ignore[attr-defined]
                        "board_id": job.board_id}
            raise err

    @api.get("/bringup")
    def bringup_status() -> Any:
        out = bringup.status(d.engine, d.state_dir)
        out["sd_flash"]["routes"] = cardwriter_served()
        return _JSON(ok(**out))

    @api.post("/bringup/scan")
    def bringup_scan(body: JsonBody = None) -> Any:
        b = _obj(body)
        ask = _bool(b, "ask_mcc", False)
        timeout = _number(b, "timeout_s", 1.5)
        if timeout <= 0:
            raise UsageError("timeout_s must be positive")
        no_board_job("a scan")
        out = bringup.scan(d.engine, host=_host(b), ask=ask, gate=gate, timeout_s=timeout)
        d.remember(out.pop("candidates"))          # GET /boards lists them (source "probe")
        return _JSON(ok(**out))

    @api.post("/bringup/bundle")
    def bringup_bundle(body: JsonBody = None) -> Any:
        b = _obj(body)
        path = b.get("path")
        if not isinstance(path, str):
            raise UsageError("the request needs 'path': the bundle's folder or .zip")
        chk = bringup.check_bundle(path, work_dir())
        bringup.clear_work(work_dir())
        if chk.refused:
            raise with_data(RefusedError(f"refusing {chk.path}: {chk.problems[0]}",
                                         hint="fix the bundle (error.data.check lists every "
                                              "problem); nothing was written"),
                            check=chk.as_dict())
        return _JSON(ok(check=chk.as_dict()))

    @api.post("/boards/{bid:path}/bringup/install")
    def bringup_install(bid: str, body: JsonBody = None) -> Any:
        from harness_manager.cli.cmd_board import backup_record

        from .drive_gate import require_holder

        s = ctx.board(bid)
        b = _obj(body)
        bundle = b.get("bundle")
        if not isinstance(bundle, str) or not bundle:
            raise UsageError("the request needs 'bundle': the folder or .zip checked by "
                             "POST /bringup/bundle")
        if not b.get("backup_path"):
            raise RefusedError("writing the configuration SD needs a backup of it first",
                               hint="take one (POST .../storage/backup), then pass its "
                                    "backup_path; nothing was written")
        backup_path = _abs_path(b.get("backup_path"), "backup_path")
        chk = bringup.check_bundle(bundle, work_dir())
        if chk.refused:
            raise with_data(RefusedError(f"refusing {chk.path}: {chk.problems[0]}",
                                         hint="nothing was written"), check=chk.as_dict())
        require_holder(d, bid, s, "write the configuration SD", b)   # behind a hub: the lease
        files = {dest: _abs_path(src, "file") for dest, src in chk.install_files.items()}
        with d.gates.op(bid):
            storage = ctx.require(s, "storage", C.STORAGE_INSTALL)
            record = backup_record(storage, backup_path)

        def run(progress: Callable[[str, int, int], None]) -> Any:
            storage.install(files, backup=record, progress=progress)
            return {"files": sorted(files), "backup": record, "bundle": chk.path,
                    "base_bit": chk.base_bit, "impl": chk.impl, "overlays": overlays()}

        def overlays() -> dict[str, Any] | None:
            """The bundle's overlays/open joins mps3.overlay_dirs, so Program and Restore find
            them. Never fails the job: the SD is written by now."""
            if not chk.overlays:
                return None
            from harness_manager.core.events import Event
            from harness_manager.settings import ops

            from .settings_api import settings_context

            try:
                kept = bringup.keep_overlays(chk, d.state_dir / "bringup" / "overlays")
                if kept is None:
                    return None
                got = bringup.add_overlay_dir(settings_context(d), kept)
            except (HarnessError, OSError) as exc:
                return {"added": False, "error": str(exc), **chk.overlays}
            result = got.pop("result", None)
            if result is not None:
                d.bus.publish(Event("settings.changed", "", ops.changed_event(result)))
            return {**got, "count": chk.overlays["count"], "names": chk.overlays["names"]}

        return ctx.accepted(d.jobs.submit(INSTALL_JOB, bid, run))

    @api.post("/boards/{bid:path}/bringup/witness")
    def bringup_witness(bid: str, body: JsonBody = None) -> Any:
        ctx.board(bid)
        b = _obj(body)
        host = _host(b)
        wait_s = _number(b, "wait_s", bringup.DEFAULT_WITNESS_S)
        poll_s = _number(b, "poll_s", 3.0)
        if wait_s <= 0 or poll_s <= 0:
            raise UsageError("wait_s and poll_s must be positive")
        engine = d.engine

        def run(progress: Callable[[str, int, int], None]) -> Any:
            return bringup.witness(engine, host, wait_s=wait_s, poll_s=poll_s,
                                   progress=progress)

        return ctx.accepted(d.jobs.submit(WITNESS_JOB, bid, run))
