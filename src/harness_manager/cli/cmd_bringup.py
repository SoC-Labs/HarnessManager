"""``harness-manager bringup - --serial PORT --volume PATH (--bundle DIR|ZIP | --version V
--source S)``: the app's bring-up wizard on the command line (lane BRINGUP-USB), the same plan.

1. the source: a bundle folder or zip, checked (``services.bringup.check_bundle``: refused
   for an ``.ebf``, an MCC command file, anything outside the config-SD tree, no bitstream),
   or a signed release (the harness catalogue's plan, ``--door usb``; refused while no
   signing key exists, as ``harness install`` is);
2. the backup of the configuration SD (mandatory; ``--backup-dir``);
3. the write: the storage install over the Debug USB (asks first), or ``--card-reader
   DEVICE_ID`` (lane SD-FLASH's writer; refused here with the reason while it is off or not in
   this build);
4. the MCC reboot (asks first), then the witness: the harness answers at ``--host`` (default
   192.168.10.101) within ``--wait`` seconds, or stage0 RESCUE does (a Linux harness with no
   bootable OS slot: write the user microSD with a whole-card image).

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
TSV_COLUMNS.setdefault(LAYOUT, ("BOARD_ID", "STEP", "RESULT", "DETAIL"))

HELP_TAB = """\
bringup - --serial PORT --volume PATH (--bundle DIR|ZIP | --version V [--source S])
  Bring a NEW board up over its Debug USB, as the app's Add > Over USB does: check the
  bundle (or plan the signed release), back up the configuration SD (mandatory), write it
  (asks first; never an .ebf), reboot through the MCC (asks first), then wait for the
  harness at --host (192.168.10.101) for --wait seconds. A Linux harness with no bootable
  OS slot answers in stage0 RESCUE: write its user microSD with a whole-card image.
  --card-reader DEVICE_ID writes the configuration SD in this PC's card reader instead
  (lane SD-FLASH; bringup.sd_flash on). --yes answers the questions; never a re-key.
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
                         "the Debug USB (lane SD-FLASH)")
    ap.add_argument("--host", default=bringup.DEFAULT_HOST,
                    help="where the harness answers after the reboot (default 192.168.10.101)")
    ap.add_argument("--wait", type=float, default=None, metavar="S",
                    help="how long to wait for it (default 180 s bare metal, 300 s Linux)")
    ap.add_argument("--yes", action="store_true", help="do not ask (never implies a re-key)")
    ap.set_defaults(fn=cmd_bringup)
    return ap


def _card_reader(ctx: Any) -> None:
    """``--card-reader``: SD-FLASH's writer, or the reason it cannot be used here."""
    sw = bringup.sd_flash(getattr(ctx.engine, "state_dir", None))
    if not sw["enabled"]:
        raise UnavailableError("card reader", f"SD card in this PC's card reader is {sw['reason']}")
    raise UnavailableError("card reader", "this build has no card-reader writer yet (lane "
                                          "SD-FLASH): write over the Debug USB (leave out "
                                          "--card-reader)")


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
    if a.card_reader:
        _card_reader(ctx)
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
    steps: list[list[Any]] = []
    data: dict[str, Any] = {}
    with ctx.board(note="bringup") as (cand, session):
        bid = cand.board_id
        storage = ctx.require(session, "storage", C.STORAGE_INSTALL)
        ctl = ctx.require(session, "controller", C.REBOOT_BOARD)
        if chk is not None:
            bit = chk.base_bit or {}
            steps.append([bid, "source", "checked",
                          f"{len(chk.files)} files, {chk.total_bytes} B; base {bit.get('path', '?')} "
                          f"({bit.get('size', '?')} B, sha256 {bit.get('sha256', '?')[:16]}…)"])
            data["check"] = chk.as_dict()
        dest = Path(a.backup_dir).expanduser() if a.backup_dir else state / "backups"
        dest.mkdir(parents=True, exist_ok=True)
        rec = storage.backup(dest)
        steps.append([bid, "backup", "taken", f"{rec.path} (sha256 {rec.sha256[:16]}…)"])
        data["backup"] = rec
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
                    exc.message, hint=f"restore the backup: harness-manager sd - --volume "
                                      f"VOLUME restore {rec.path}"), steps=steps) from exc
            steps.append([bid, "write", "written", f"{len(files)} files over the Debug USB, read "
                                                   "back"])
            ovl = _overlays(ctx, chk, state)
            if ovl is not None:
                steps.append([bid, "overlays", "added" if ovl.get("added") else "kept",
                              f"{ovl.get('path', '')} in mps3.overlay_dirs"])
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
    wait = a.wait or (bringup.LINUX_WITNESS_S if linux else bringup.DEFAULT_WITNESS_S)
    ctx.note(f"waiting up to {wait:.0f} s for the harness at {a.host} ({bringup.PC_ADDRESS_HINT})")
    try:
        seen = bringup.witness(ctx.engine, a.host, wait_s=wait)
    except ActionFailedError as exc:
        raise with_data(ActionFailedError(
            exc.message, hint=f"{exc.hint}: harness-manager sd - --volume VOLUME restore "
                              f"{data['backup'].path}"), steps=steps, **exc.data) from exc
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
