"""``harness-manager bringup - --serial PORT --volume PATH (--bundle DIR|ZIP | --version V
--source S)``: the app's bring-up wizard on the command line (lane BRINGUP-USB), the same plan.

1. the source: a bundle folder or zip, checked (``services.bringup.check_bundle``: refused
   for an ``.ebf``, an MCC command file, anything outside the config-SD tree, no bitstream),
   or a signed release (the harness catalogue's plan, ``--door usb``; refused while no
   signing key exists, as ``harness install`` is);
2. the backup of the configuration SD (mandatory; ``--backup-dir``);
3. the write: the storage install over the Debug USB (asks first), or ``--card-reader
   DEVICE_ID``: the card writer's ``files`` kind (``services/cardwriter.py``, as the wizard's
   card-reader door and ``flash write --kind files``): the card in this PC's reader is backed
   up, the typed ``WRITE <model> <size>`` (``--confirm``; ``--yes`` never implies it), the
   pack's MBBIOS rule, written and read back. Refused with the reason while
   ``bringup.sd_flash`` is off;
4. the MCC reboot (asks first), then the witness: the harness answers at ``--host`` (default
   192.168.10.101) within ``--wait`` seconds, or stage0 RESCUE does (a Linux harness with no
   bootable OS slot: write the user microSD with a whole-card image). With the card reader
   there is NO MCC reboot: the user puts the card back and powers the board on (asks first),
   then the witness.

A bare-metal release over USB only ends ``written-not-running`` BY DESIGN (nothing confirms the
harness without Ethernet): the witness decides. Exit codes are the CLI's: 15 a refusal, 6 a
write or a witness that failed (the hint names the backup to restore).

Wiring: ``register(sub)`` from ``cli/main.py`` ``make_parser`` and ``HELP_TAB`` in
``helptext`` (CCR BRINGUP-3: those files are the integrator's).
"""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

from harness_manager.cli.output import TSV_COLUMNS, Result, with_data
from harness_manager.core import capabilities as C
from harness_manager.core.errors import (
    ActionFailedError,
    ExitCode,
    RefusedError,
    UnavailableError,
    UsageError,
)
from harness_manager.services import bringup

LAYOUT = "bringup"
#: The card reader door has no MCC reboot (the wizard's step 4 says the same).
PUT_BACK = ("no MCC reboot with the card reader: put the card back in the board's "
            "configuration SD slot and power the board on")
READER_RELEASE = ("a signed release goes through the Debug USB (its own install); for the card "
                  "reader, use a bundle folder or zip")
TSV_COLUMNS.setdefault(LAYOUT, ("BOARD_ID", "STEP", "RESULT", "DETAIL"))

HELP_TAB = """\
bringup - --serial PORT --volume PATH (--bundle DIR|ZIP | --version V [--source S])
  Bring a NEW board up over its Debug USB, as the app's Add > Over USB does: check the
  bundle (or plan the signed release), back up the configuration SD (mandatory), write it
  (asks first; never an .ebf), reboot through the MCC (asks first), then wait for the
  harness at --host (192.168.10.101) for --wait seconds. A Linux harness with no bootable
  OS slot answers in stage0 RESCUE: write its user microSD with a whole-card image.
  --card-reader DEVICE_ID writes the configuration SD in this PC's card reader instead
  (bringup.sd_flash on; `flash devices` lists the ids): typed WRITE <model> <size>
  (--confirm), no MCC reboot: put the card back and power the board on. --yes answers the
  questions; never a re-key, never a typed phrase.
"""


def register(subparsers: Any, *, parents: tuple[argparse.ArgumentParser, ...] = ()
             ) -> argparse.ArgumentParser:
    ap = subparsers.add_parser(
        "bringup", parents=list(parents),
        help="bring a new board up over its Debug USB: back up, write, reboot, witness",
        description=HELP_TAB.splitlines()[1].strip(),
        epilog=f"--tsv columns: {' '.join(TSV_COLUMNS[LAYOUT])}")
    ap.add_argument("target", metavar="TARGET",
                    help="- for the board on this PC's Debug USB (with --serial and --volume)")
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--bundle", metavar="DIR|ZIP",
                     help="a bundle folder or zip on this PC: the config-SD tree (config.txt and "
                          "MB/), or a release bundle with sd/")
    src.add_argument("--version", metavar="V", help="a signed harness release to install")
    ap.add_argument("--source", metavar="S", default=None,
                    help="where the release comes from: github:OWNER/REPO, a URL or a mirror "
                         "folder (default: the catalogue)")
    ap.add_argument("--channel", default=None, help="the release's channel (default: stable)")
    ap.add_argument("--consent", default="",
                    help="the typed re-key phrase the plan prints (REKEY 0x...); --yes never "
                         "implies it")
    ap.add_argument("--backup-dir", metavar="DIR", default=None,
                    help="where the backup zip goes (default: the state dir's backups/)")
    ap.add_argument("--card-reader", metavar="DEVICE_ID", default=None,
                    help="write the configuration SD in this PC's card reader instead of over "
                         "the Debug USB (the id `harness-manager flash devices` prints; the "
                         "card is out of the board; no --serial or --volume)")
    ap.add_argument("--confirm", default=None, metavar="PHRASE",
                    help='--card-reader: the typed phrase, exactly "WRITE <model> <size>" as '
                         "`flash devices` shows it; --yes never implies it")
    ap.add_argument("--host", default=bringup.DEFAULT_HOST,
                    help="where the harness answers after the reboot (default 192.168.10.101)")
    ap.add_argument("--wait", type=float, default=None, metavar="S",
                    help="how long to wait for it (default 180 s bare metal, 300 s Linux)")
    ap.add_argument("--yes", action="store_true", help="do not ask (never implies a re-key)")
    ap.set_defaults(fn=cmd_bringup)
    return ap


def _card_writer(ctx: Any) -> Any:
    """``--card-reader``: the card writer ``flash write`` uses (its test seam), refused with
    the reason while ``bringup.sd_flash`` is off or this OS has no writer."""
    from harness_manager.services import cardwriter as cw

    from . import cmd_flash

    w = cmd_flash.WRITER if cmd_flash.WRITER is not None else \
        cw.CardWriter(state_dir=getattr(ctx.engine, "state_dir", None))
    err = w.refusal()
    if err is not None:
        raise UnavailableError("card reader", err.reason, hint=err.hint or "")
    return w


def _steps_out(ctx: Any, bid: str, steps: list[list[Any]], data: dict[str, Any]) -> int:
    human = [f"{s:<9} {r:<10} {d}" for _b, s, r, d in steps]
    ctx.emit(Result(LAYOUT, {"board_id": bid, "steps": [
        {"step": s, "result": r, "detail": d} for _b, s, r, d in steps], **data},
        rows=steps, human=human))
    return ExitCode.OK


def cmd_bringup(ctx: Any) -> int:
    a = ctx.args
    if a.target != "-":
        raise UsageError("bringup is for a board on this PC's Debug USB: TARGET is -",
                         hint="harness-manager bringup - --serial PORT --volume PATH --bundle DIR")
    writer = None
    if a.card_reader:
        if a.version:
            raise UsageError(READER_RELEASE, hint="harness-manager bringup - --bundle DIR|ZIP "
                                                  "--card-reader DEVICE_ID")
        writer = _card_writer(ctx)
    elif a.confirm is not None:
        raise UsageError("--confirm is the card reader's typed phrase: it needs --card-reader",
                         hint="over the Debug USB, --yes answers the questions")
    if a.wait is not None and a.wait <= 0:
        raise UsageError(f"--wait {a.wait:g} is not a wait", hint="give seconds, more than 0")
    state = Path(getattr(ctx.engine, "state_dir", None) or Path.home() / ".config" /
                 "harness-manager")
    chk = None
    if a.bundle:
        chk = bringup.check_bundle(Path(a.bundle).expanduser().resolve(),
                                   state / "bringup" / "bundles")
        if chk.refused:
            raise with_data(RefusedError(f"refusing {chk.path}: {'; '.join(chk.problems)}",
                                         hint="fix the bundle; nothing was written"),
                            check=chk.as_dict())
    if writer is not None:
        assert chk is not None
        return _card_reader(ctx, writer, chk, state)
    steps: list[list[Any]] = []
    data: dict[str, Any] = {}
    with ctx.board(note="bringup") as (cand, session):
        bid = cand.board_id
        storage = ctx.require(session, "storage", C.STORAGE_INSTALL)
        ctl = ctx.require(session, "controller", C.REBOOT_BOARD)
        if chk is not None:
            steps.append([bid, "source", "checked", _checked(chk)])
            data["check"] = chk.as_dict()
        dest = Path(a.backup_dir).expanduser() if a.backup_dir else state / "backups"
        dest.mkdir(parents=True, exist_ok=True)
        rec = storage.backup(dest)
        steps.append([bid, "backup", "taken", f"{rec.path} (sha256 {rec.sha256[:16]}…)"])
        data["backup"] = rec
        restore = f"harness-manager sd - --volume VOLUME restore {rec.path}"
        linux = bool(chk is not None and chk.impl == "linux")
        if chk is not None:
            ctx.confirm(f"write {len(chk.files)} files to the configuration SD of {bid}? A USB write "
                        "can take 5 minutes: do not unplug, power off or start a second write. "
                        f"(backup {rec.path})")
            files = {k: Path(v) for k, v in chk.install_files.items()}
            try:
                storage.install(files, backup=rec)
            except ActionFailedError as exc:
                raise with_data(ActionFailedError(
                    exc.message, hint=f"restore the backup: {restore}"), steps=steps) from exc
            steps.append([bid, "write", "written", f"{len(files)} files over the Debug USB, read "
                                                   "back"])
            _overlay_step(ctx, chk, state, bid, steps)
            ctx.confirm(f"reboot {bid} through the MCC? It reloads from the SD")
            from harness_manager.services import reset_guard

            with reset_guard.guarded(session, reset_guard.ACTION_MCC_REBOOT):
                ev = ctl.reboot(wait_s=None)
            steps.append([bid, "reboot", "witnessed", str((ev or {}).get("summary", "rebooted"))])
            data["reboot"] = ev
        else:
            from . import cmd_harness

            cat = cmd_harness.catalog(ctx)
            verified = cat.locate(a.version, channel=a.channel, source=a.source, pack=cand.pack)
            plan, verified = cat.plan(session, a.version, channel=a.channel, source=a.source,
                                      verified=verified, via="usb")
            out = cmd_harness._run_plan(ctx, cat, cand, session, plan, verified, "install")
            linux = plan.reboot_wait_s is not None
            data["install"] = out.as_dict()
            if out.result not in ("installed", "written-not-running"):
                raise with_data(ActionFailedError(out.detail, hint=out.restore_hint or
                                                  "check the board"), outcome=out.as_dict())
            steps.append([bid, "install", out.result, out.detail])
    return _witness(ctx, bid, steps, data, linux, restore)


def _checked(chk: Any) -> str:
    bit = chk.base_bit or {}
    return (f"{len(chk.files)} files, {chk.total_bytes} B; base {bit.get('path', '?')} "
            f"({bit.get('size', '?')} B, sha256 {bit.get('sha256', '?')[:16]}…)")


def _overlay_step(ctx: Any, chk: Any, state: Path, bid: str, steps: list[list[Any]]) -> None:
    ovl = _overlays(ctx, chk, state)
    if ovl is not None:
        steps.append([bid, "overlays", "added" if ovl.get("added") else "kept",
                      f"{ovl.get('path', '')} in mps3.overlay_dirs"])


def _witness(ctx: Any, bid: str, steps: list[list[Any]], data: dict[str, Any], linux: bool,
             restore: str) -> int:
    """Wait for the harness at ``--host``; a dark board names the backup to restore."""
    a = ctx.args
    wait = a.wait or (bringup.LINUX_WITNESS_S if linux else bringup.DEFAULT_WITNESS_S)
    ctx.note(f"waiting up to {wait:.0f} s for the harness at {a.host} ({bringup.PC_ADDRESS_HINT})")
    try:
        seen = bringup.witness(ctx.engine, a.host, wait_s=wait)
    except ActionFailedError as exc:
        raise with_data(ActionFailedError(
            exc.message, hint=f"{exc.hint}: {restore}"), steps=steps, **exc.data) from exc
    steps.append([bid, "witness", seen["state"], seen["text"]])
    data["witness"] = seen
    if seen["state"] == "rescue":
        steps.append([bid, "os", "needed", "stage0 RESCUE: write the board's user microSD with "
                                           f"{bringup.CARD_IMAGE_HINT}, or skip if it is "
                                           "prepared; over the network from rescue "
                                           f"{bringup.RESCUE_NETWORK_REASON}"])
    else:
        steps.append([bid, "next", "access", f"harness-manager claim {a.host} (Linux), then "
                                             f"harness-manager board identity {a.host}"])
    return _steps_out(ctx, bid, steps, data)


def _card_reader(ctx: Any, w: Any, chk: Any, state: Path) -> int:
    """``--card-reader``: the bundle's config-SD tree onto the card in this PC's reader (the
    card writer's ``files`` kind: a backup of the card first, the pack's MBBIOS rule, read
    back), then no MCC reboot: the card goes back in the board, which is powered on, and the
    witness waits for it."""
    from harness_manager.services import cardwriter as cw

    from . import cmd_flash
    from .output import StderrProgress

    a = ctx.args
    bid = "-"
    if getattr(a, "serial", None) or getattr(a, "volume", None):
        ctx.note("--card-reader writes the card in this PC's reader: --serial and --volume are "
                 "not used (no MCC reboot)")
    dest = Path(a.backup_dir).expanduser() if a.backup_dir else state / "backups"
    plan = w.plan(a.card_reader, "files", Path(chk.sd_root), backup_dir=dest)
    disk = plan.device.disk
    steps: list[list[Any]] = [[bid, "source", "checked", _checked(chk)]]
    data: dict[str, Any] = {"check": chk.as_dict(), "card_reader": plan.summary()}
    ctx.note(f"device   {disk.path}  {disk.display_model}  {cw.human_size(disk.size)}  "
             f"({disk.transport})")
    ctx.note(f"writes   {len(plan.files)} files from {chk.sd_root} onto "
             f"{plan.device.files_root}")
    ctx.note(f"backup   a new one in {dest}")
    for d in plan.mbbios:
        if d["note"]:
            ctx.note(d["note"])
    if a.confirm is not None:
        typed = a.confirm
    elif a.yes:
        raise with_data(RefusedError(f"not confirmed: --yes never types the phrase; give it with "
                                     f"--confirm {plan.confirm!r}", hint="nothing was written"),
                        confirm=plan.confirm, device_id=plan.device.id)
    else:
        typed = cmd_flash._ask(ctx, plan.confirm)
    w.check_confirm(plan, typed)
    out = w.run(plan, progress=StderrProgress("bringup card reader", ctx.err))
    if out.get("outcome") != "written":
        raise with_data(ActionFailedError(f"the card in {disk.path} was not written: "
                                          f"{out.get('outcome')}",
                                          hint=out.get("privileged_command") or
                                          "check the card reader"), steps=steps, write=out)
    bk = out.get("backup") or {}
    steps.append([bid, "backup", "taken", f"{bk.get('path', '?')} (sha256 "
                                          f"{str(bk.get('sha256', '?'))[:16]}…)"])
    steps.append([bid, "write", "written", f"{len(out.get('files') or [])} files onto the card "
                                           f"in {disk.path} ({plan.device.files_root}), read "
                                           "back"])
    for d in out.get("mbbios") or []:
        if d.get("note"):
            steps.append([bid, "mbbios", d.get("action", ""), d["note"]])
    data["write"] = out
    data["backup"] = bk
    _overlay_step(ctx, chk, state, bid, steps)
    steps.append([bid, "reboot", "by-hand", PUT_BACK])
    restore = (f"take the card out again, put it in this PC's reader, then: harness-manager sd - "
               f"--volume {plan.device.files_root} restore {bk.get('path', 'BACKUP')}")
    try:
        ctx.confirm(f"{PUT_BACK}. Is it back in, and on?")
    except RefusedError as exc:
        raise with_data(RefusedError(
            "the card is written; nothing waited for the harness",
            hint=f"{PUT_BACK}, then look for it: harness-manager probe --host {a.host} --no-scan"),
            steps=steps, **{k: v for k, v in data.items() if k != "check"}) from exc
    return _witness(ctx, bid, steps, data, chk.impl == "linux", restore)


def _overlays(ctx: Any, chk: Any, state: Path) -> dict[str, Any] | None:
    if not chk.overlays:
        return None
    from harness_manager.settings import ops

    try:
        kept = bringup.keep_overlays(chk, state / "bringup" / "overlays")
        if kept is None:
            return None
        got = bringup.add_overlay_dir(ops.SettingsContext(engine=ctx.engine), kept)
    except Exception as exc:  # noqa: BLE001 - the SD is written; say it, never fail for it
        ctx.note(f"the bundle's overlays were not added to mps3.overlay_dirs: {exc}")
        return None
    got.pop("result", None)
    return got
