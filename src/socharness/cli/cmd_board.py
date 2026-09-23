"""Board-level verbs over session adapters: reset, clock, mcc (controller), sd (storage)."""

from __future__ import annotations

import hashlib
import re
import time
import zipfile
from pathlib import Path

from socharness.core import capabilities as C
from socharness.core.errors import AbsentError, ExitCode, RefusedError, UsageError
from socharness.core.pack import BackupRecord

from .context import Ctx
from .output import Result, StderrProgress, reading_human, reading_json, reading_row

# --- reset ---------------------------------------------------------------------------------


def cmd_reset(ctx: Ctx) -> int:
    what = ctx.args.what
    with ctx.board() as (cand, session):
        resets = ctx.require(session, "resets", C.RESET_DUT)
        targets = list(resets.reset_targets())
        if what not in targets:
            raise UsageError(f"{cand.board_id} cannot reset {what!r}",
                             hint=f"reset targets on this board: {', '.join(targets) or 'none'}")
        resets.reset(what)
    ctx.emit(Result("reset", {"board_id": cand.board_id, "target": what, "result": "done"},
                    rows=[[cand.board_id, what, "done"]],
                    human=[f"reset      {what} on {cand.board_id}: done"]))
    return ExitCode.OK


# --- clock ---------------------------------------------------------------------------------

_PRESET = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*(?:mhz)?\s*$", re.IGNORECASE)


def preset_mhz(name: str) -> float:
    """``25mhz`` / ``25MHz`` / ``25`` -> 25.0. Anything else is a usage error."""
    m = _PRESET.match(name)
    if not m:
        raise UsageError(f"clock preset {name!r} is not a frequency",
                         hint="presets are written like 25mhz, 50mhz, 100mhz")
    return float(m.group(1))


def cmd_clock(ctx: Ctx) -> int:
    a = ctx.args
    mhz = a.dut_mhz if a.dut_mhz is not None else (preset_mhz(a.preset) if a.preset else None)
    if mhz is not None and mhz <= 0:
        raise UsageError(f"{mhz:g} MHz is not a clock frequency", hint="give a positive MHz")
    with ctx.board() as (cand, session):
        clocks = ctx.require(session, "clocks", C.CLOCK_DUT)
        readings = [clocks.set_clock("dut", mhz)] if mhz is not None else list(clocks.clocks())
    now = time.time()
    ctx.emit(Result("clock", {
        "board_id": cand.board_id, "set_mhz": mhz,
        "readings": [reading_json(r, now) for r in readings],
    }, rows=[reading_row(cand.board_id, r, now) for r in readings],
        human=[reading_human(r) for r in readings] or ["no clocks reported"]))
    return ExitCode.OK


# --- mcc (the board controller) --------------------------------------------------------------

_MCC_CAPABILITY = {
    "temp": C.CONSOLE_CONTROLLER,
    "osc": C.CLOCK_BOARD,
    "reboot": C.REBOOT_BOARD,
    "cmd": C.CONSOLE_CONTROLLER,
}


def cmd_mcc(ctx: Ctx) -> int:
    a = ctx.args
    action = a.mcc_cmd
    with ctx.board() as (cand, session):
        ctl = ctx.require(session, "controller", _MCC_CAPABILITY[action])
        if action in ("temp", "osc"):
            readings = list(ctl.temperatures() if action == "temp" else ctl.oscillators())
            now = time.time()
            ctx.emit(Result("mcc temp|osc", {
                "board_id": cand.board_id, "readings": [reading_json(r, now) for r in readings],
            }, rows=[reading_row(cand.board_id, r, now) for r in readings],
                human=[reading_human(r) for r in readings] or ["no readings"]))
            return ExitCode.OK
        if action == "cmd":
            reply = ctl.command(a.line)            # the allowlist lives in the adapter
            ctx.emit(Result("mcc cmd", {"board_id": cand.board_id, "command": a.line,
                                        "reply": reply},
                            rows=[[cand.board_id, a.line, reply]],
                            human=reply.splitlines() or ["(no reply text)"]))
            return ExitCode.OK
        # reboot
        ctx.confirm(f"reboot {cand.board_id}? The board reloads from its SD and the running "
                    "design is lost")
        progress = StderrProgress("reboot", ctx.err)
        ctl.reboot(progress=progress, wait_s=a.wait)
    ctx.emit(Result("mcc reboot", {"board_id": cand.board_id, "result": "rebooted",
                                   "phases": progress.phases},
                    rows=[[cand.board_id, "rebooted", progress.phases]],
                    human=[f"rebooted   {cand.board_id} (seen: {', '.join(progress.phases) or '-'})"]))
    return ExitCode.OK


# --- sd (the configuration storage) ----------------------------------------------------------


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def backup_record(storage: object, path: Path) -> BackupRecord:
    """The ``BackupRecord`` for a backup archive given on the command line.

    The storage adapter's own reader is used when it offers one (``load_backup``,
    see the hand-back's contract change request); otherwise the record is built
    from the archive itself and the adapter re-verifies it.
    """
    if not path.is_file():
        raise AbsentError(f"no backup archive at {path}",
                          hint="make one with `socharness sd TARGET backup DIR`")
    loader = getattr(storage, "load_backup", None)
    if callable(loader):
        return loader(path)
    try:
        with zipfile.ZipFile(path) as zf:
            files = sum(1 for i in zf.infolist() if not i.is_dir())
    except zipfile.BadZipFile as exc:
        raise UsageError(f"{path} is not a backup archive (not a zip)",
                         hint="give the .zip that `sd backup` wrote") from exc
    return BackupRecord(path=str(path), sha256=_sha256(path), created_at=path.stat().st_mtime,
                        files=files, volume_label="")


def bundle_files(bundle: Path) -> dict[str, Path]:
    """SD-relative POSIX path -> source file, for every file under ``bundle``."""
    if not bundle.is_dir():
        raise AbsentError(f"no bundle directory at {bundle}", hint="give the unpacked bundle dir")
    files = {p.relative_to(bundle).as_posix(): p for p in sorted(bundle.rglob("*")) if p.is_file()}
    if not files:
        raise AbsentError(f"bundle {bundle} has no files", hint="give the unpacked bundle dir")
    ebf = sorted(k for k in files if k.lower().endswith(".ebf"))
    if ebf:
        raise RefusedError(f"bundle {bundle} contains board-controller firmware ({', '.join(ebf)})",
                           hint=".ebf files are never written to the SD; remove them")
    return files


def cmd_sd(ctx: Ctx) -> int:
    a = ctx.args
    action = a.sd_cmd
    cap = C.STORAGE_BACKUP if action == "backup" else C.STORAGE_INSTALL
    progress = StderrProgress(f"sd {action}", ctx.err)
    with ctx.board() as (cand, session):
        storage = ctx.require(session, "storage", cap)
        if action == "backup":
            dest = Path(a.dir)
            if dest.exists() and not dest.is_dir():
                raise UsageError(f"{dest} exists and is not a directory",
                                 hint="give a directory for the backup archive")
            dest.mkdir(parents=True, exist_ok=True)
            rec = storage.backup(dest, progress=progress)
            ctx.emit(Result("sd backup", {"board_id": cand.board_id, "backup": rec},
                            rows=[[cand.board_id, rec.path, rec.sha256, rec.files,
                                   rec.volume_label]],
                            human=[f"backed up  {rec.files} files from {rec.volume_label or '?'} "
                                   f"to {rec.path}", f"sha256     {rec.sha256}"]))
            return ExitCode.OK
        if action == "install":
            files = bundle_files(Path(a.bundle))
            rec = backup_record(storage, Path(a.backup))
            ctx.confirm(f"write {len(files)} files to the SD of {cand.board_id}? "
                        f"(backup {rec.path})")
            storage.install(files, backup=rec, progress=progress)
            ctx.emit(Result("sd install", {"board_id": cand.board_id, "files": sorted(files),
                                           "backup": rec},
                            rows=[[cand.board_id, len(files), rec.sha256]],
                            human=[f"written    {len(files)} files to the SD of {cand.board_id}",
                                   "not running yet: reboot (`socharness mcc TARGET reboot`), "
                                   "then check `socharness info TARGET`"]))
            return ExitCode.OK
        # restore
        rec = backup_record(storage, Path(a.zip))
        ctx.confirm(f"overwrite the SD of {cand.board_id} with {rec.path}?")
        storage.restore(rec, progress=progress)
    ctx.emit(Result("sd restore", {"board_id": cand.board_id, "backup": rec},
                    rows=[[cand.board_id, rec.path, rec.sha256]],
                    human=[f"restored   {cand.board_id} SD from {rec.path}"]))
    return ExitCode.OK
