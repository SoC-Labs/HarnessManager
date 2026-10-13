"""A board's card contents (/persist) saved to this PC, over the daemon API (lane L3).

- ``GET /boards/{bid}/persist/backups`` -> ``{board_id, backups, recent, recent_days}``: the
  backups of this board kept on this PC (newest first: id, time, size, file count, sha256, no
  file list), and whether one is recent enough for a card re-make;
- ``POST /boards/{bid}/persist/backup`` -> 202 job ``persist_backup``: ``tar`` the card contents
  over the board's own SSH into a file on this PC with a record beside it;
- ``POST /boards/{bid}/persist/restore`` ``{confirm, backup_id}`` -> 202 job ``persist_restore``:
  put a backup back, then list the board's card contents again and compare.

The board must be claimed by this PC (its SSH is the way in); otherwise 409 REFUSED before the
job starts. A backup only reads the board, so it needs no confirm; a restore writes, so it needs
``confirm: true`` and the board's hub lease when it is behind one. File contents are never read
or logged. Registered in ``app.EXTENSIONS`` as ``persist_api``.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from harness_manager.core.errors import AbsentError, HarnessError, UsageError
from harness_manager.services.persist_backup import (
    RECENT_S,
    PersistBackupService,
    summary,
)
from harness_manager.services.slots import SlotService, lease_check_for

from .app import _JSON, JsonBody, RouteContext, _obj, ok
from .card_api import confirmed


def register(ctx: RouteContext) -> None:
    d = ctx.daemon
    api = ctx.api
    svc = getattr(d, "persist_backup", None) or PersistBackupService()

    def still_open(bid: str, s: Any, what: str) -> None:
        try:
            current = ctx.board(bid)
        except HarnessError:
            current = None
        if current is not s:
            raise AbsentError(f"{bid} was closed before the {what} started; nothing was sent",
                              hint="open the board again, then retry")

    def check_ready(bid: str, s: Any, what: str) -> None:
        """Before the 202: the board can be entered over its own SSH (409 REFUSED if not)."""
        with d.gates.op(bid):
            svc._adapter(s, what)

    @api.get("/boards/{bid:path}/persist/backups")
    def persist_list(bid: str) -> Any:
        ctx.board(bid)
        backups = svc.list(bid)
        return _JSON(ok(board_id=bid, backups=[summary(m) for m in backups],
                        recent=svc.has_recent_backup(bid),
                        recent_days=int(RECENT_S // 86400)))

    @api.post("/boards/{bid:path}/persist/backup")
    def persist_backup(bid: str, body: JsonBody = None) -> Any:
        s = ctx.board(bid)
        _obj(body)
        check_ready(bid, s, "backing up the card contents")

        def run(progress: Callable[..., None]) -> Any:
            still_open(bid, s, "card contents backup")
            m = svc.backup(s, progress=progress)
            return {"board_id": bid, "backup": summary(m),
                    "note": f"saved {m['file_count']} file(s), {m['size']} bytes, on this PC"}

        return ctx.accepted(d.jobs.submit("persist_backup", bid, run))

    @api.post("/boards/{bid:path}/persist/restore")
    def persist_restore(bid: str, body: JsonBody = None) -> Any:
        s = ctx.board(bid)
        b = _obj(body)
        confirmed(b, "put the saved card contents back on the board")
        backup_id = b.get("backup_id")
        if not isinstance(backup_id, str) or not backup_id:
            raise UsageError("backup_id must name one of the board's backups")
        svc.get(bid, backup_id)                    # 404 before the job
        check_ready(bid, s, "restoring the card contents")
        SlotService(lease_check=lease_check_for(d.engine, getattr(d, "leases", None)),
                    bus=getattr(d, "bus", None)).check_lease(s, "restore the card contents")

        def run(progress: Callable[..., None]) -> Any:
            still_open(bid, s, "card contents restore")
            return svc.restore(s, backup_id, progress=progress)

        return ctx.accepted(d.jobs.submit("persist_restore", bid, run))
