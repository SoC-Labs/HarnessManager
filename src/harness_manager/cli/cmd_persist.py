"""``harness-manager card backup|backups|restore`` (lane L3 PERSIST BACKUP).

Verbs, added to the ``card`` verb ``cmd_slots`` makes::

    harness-manager card backup   TARGET             save the board's card contents (/persist:
                                                     claim keys, identity, calibration, kept
                                                     state) to this PC over the board's SSH
    harness-manager card backups  TARGET             the backups kept for this board
    harness-manager card restore  TARGET [--backup ID] [--yes]
                                                     put a backup back and compare the files

Re-making the card wipes /persist: back up first, restore after. The board must be claimed by
this PC (`harness-manager board claim TARGET`). ``backup`` and ``backups`` change nothing on the
board; ``restore`` writes to it, asks first (``--yes`` skips) and needs the board's lease behind
a hub. File contents are never shown: names, sizes and the archive's sha256 only.

``cli/main.py`` registers it after ``cmd_slots`` with ``cmd_persist.register(sub, verbs)``,
``verbs`` being what ``cmd_slots.register`` returned.
"""

from __future__ import annotations

import argparse
from typing import Any

from harness_manager.core.errors import ExitCode, UsageError

from .cmd_slots import _board, _fmt
from .context import Ctx
from .output import TSV_COLUMNS, Result, StderrProgress

PERSIST_TSV = ("BOARD_ID", "BACKUP_ID", "TIME_UTC", "SHELL_ID", "BYTES", "FILES", "SHA256")
VERBS = ("backup", "backups", "restore")


def register(subparsers: Any, verbs: dict[str, argparse.ArgumentParser]) -> None:
    """Add the three verbs to the ``card`` parser in ``verbs`` (``cmd_slots.register``'s
    result). ``subparsers`` is unused: ``card`` already exists."""
    TSV_COLUMNS.setdefault("persist", PERSIST_TSV)
    fmt, board = _fmt(), _board()
    yes = argparse.ArgumentParser(add_help=False)
    yes.add_argument("--yes", action="store_true", help="do not ask for confirmation")
    cp = verbs["card"]
    action = next(a for a in cp._actions if isinstance(a, argparse._SubParsersAction))
    epilog = f"--tsv columns: {' '.join(PERSIST_TSV)}"
    action.add_parser("backup", help="save the board's card contents (/persist) to this PC",
                      description="Copy /persist (claim keys, identity, sensor calibration, "
                                  "kept state) from the board to this PC over the board's SSH. "
                                  "Do this before re-making the card.",
                      parents=[fmt, board], epilog=epilog)
    action.add_parser("backups", help="the saved card contents kept for this board",
                      parents=[fmt, board], epilog=epilog)
    rp = action.add_parser("restore", help="put saved card contents back on the board",
                           description="Upload a backup into /persist, then list the board's "
                                       "files again and compare with the backup.",
                           parents=[fmt, board, yes], epilog=epilog)
    rp.add_argument("--backup", default=None, metavar="ID",
                    help="which backup (default: the newest)")
    inner = cp.get_default("fn")

    def card(ctx: Ctx) -> int:
        act = ctx.args.card_cmd
        if act == "backup":
            return cmd_backup(ctx)
        if act == "backups":
            return cmd_backups(ctx)
        if act == "restore":
            return cmd_restore(ctx)
        return int(inner(ctx))

    cp.set_defaults(fn=card)


def _service(ctx: Ctx) -> Any:
    svc = getattr(ctx.engine, "persist_service", None)
    if svc is not None:
        return svc
    from harness_manager.services.persist_backup import PersistBackupService

    return PersistBackupService()


def _row(m: dict[str, Any]) -> list[Any]:
    return [m["board_id"], m["id"], m["created_utc"], m.get("shell_id", ""), m["size"],
            m["file_count"], m["sha256"]]


def _line(m: dict[str, Any]) -> str:
    return (f"{m['id']}  {m['created_utc']}  {m['size']} bytes  {m['file_count']} file(s)"
            + (f"  shell {m['shell_id']}" if m.get("shell_id") else ""))


def cmd_backup(ctx: Ctx) -> int:
    svc = _service(ctx)
    with ctx.board(note="cli card backup") as (cand, session):
        m = svc.backup(session, progress=StderrProgress("backup", ctx.err))
    line = f"saved {m['file_count']} file(s), {m['size']} bytes, as backup {m['id']}"
    ctx.emit(Result("persist", {"board_id": cand.board_id, "backup": _summary(m),
                                "result": line}, rows=[_row(m)],
                    human=[f"done       {line}", f"sha256     {m['sha256']}"]))
    return ExitCode.OK


def cmd_backups(ctx: Ctx) -> int:
    svc = _service(ctx)
    cand = ctx.candidate()
    backups = svc.list(cand.board_id)
    human = [f"backups    {cand.board_id}: " + ("none yet" if not backups else
                                              f"{len(backups)} saved on this PC")]
    human += [f"  {_line(m)}" for m in backups]
    ctx.emit(Result("persist", {"board_id": cand.board_id,
                                "backups": [_summary(m) for m in backups],
                                "recent": svc.has_recent_backup(cand.board_id)},
                    rows=[_row(m) for m in backups], human=human))
    return ExitCode.OK


def cmd_restore(ctx: Ctx) -> int:
    svc = _service(ctx)
    cand = ctx.candidate()
    backups = svc.list(cand.board_id)
    if not backups:
        raise UsageError(f"no saved card contents for {cand.board_id}",
                         hint="`harness-manager card backup TARGET` makes one")
    want = ctx.args.backup or backups[0]["id"]
    m = svc.get(cand.board_id, want)
    ctx.confirm(f"put backup {m['id']} ({m['file_count']} file(s)) back into the card contents "
                f"of {cand.board_id}? Files with the same names are overwritten.")
    from harness_manager.services.slots import SlotService, lease_check_for

    with ctx.board(note="cli card restore") as (_cand, session):
        SlotService(lease_check=lease_check_for(ctx.engine)).check_lease(
            session, "restore the card contents")
        out = svc.restore(session, m["id"], progress=StderrProgress("restore", ctx.err))
    ctx.emit(Result("persist", {**out, "result": out["note"]}, rows=[_row(m)],
                    human=[f"done       {out['note']}"]))
    return ExitCode.OK if out["verified"] else ExitCode.ACTION_FAILED


def _summary(m: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in m.items() if k != "files"}
