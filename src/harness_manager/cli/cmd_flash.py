"""``harness-manager flash``: SD cards in THIS PC's card reader (``services/cardwriter.py``).

    harness-manager flash devices [--all]
    harness-manager flash write DEVICE_ID SOURCE --kind files|card [--confirm PHRASE]
                                [--confirm-unsigned PHRASE]
                                [--backup ZIP | --backup-dir DIR] [--allow-mcc-update] [--yes]

Runs in this process (no board, no service): the same code as the app's
``/cardwriter`` routes, under the same setting (``bringup.sd_flash``, off by default) and
the same one-write-per-device lock. Every check runs before the questions; then the typed
phrases: what is written is unsigned (a bundle folder or zip, a whole-card image), so first
``INSTALL UNSIGNED <first 8 hex of its sha256>`` (``--confirm-unsigned``; the banner and the
sha256 and how it is made are printed), then ``WRITE <model> <size>`` (``--confirm``).
``--yes`` never asks, so both must then come with their flags. Without the rights to write the device it prints
the sudo commands and exits 12 (Harness Manager never asks for root).
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

from harness_manager.core.errors import ExitCode, RefusedError, UnavailableError, UsageError
from harness_manager.services import cardwriter as cw

from .context import Ctx
from .output import TSV_COLUMNS, Result, StderrProgress, with_data

WHAT = "SD cards in this PC's card reader: devices, write"

# --tsv columns (append-only), kept beside the verb that prints them.
DEVICE_COLUMNS = ("ID", "PATH", "MODEL", "SIZE_BYTES", "SIZE", "MOUNTED", "FS", "FILES",
                  "CARD", "CONFIRM", "NEEDS_PRIVILEGE")
WRITE_COLUMNS = ("DEVICE_ID", "PATH", "KIND", "OUTCOME", "VERIFIED", "SHA256",
                 "PRIVILEGED_COMMAND")
TSV_COLUMNS.setdefault("flash devices", DEVICE_COLUMNS)
TSV_COLUMNS.setdefault("flash write", WRITE_COLUMNS)

#: A test seam: the CardWriter the verb uses (None: the real one, in the user's state dir).
WRITER: cw.CardWriter | None = None


def _fmt() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(add_help=False)
    g = p.add_mutually_exclusive_group()
    g.add_argument("--json", action="store_true", default=argparse.SUPPRESS,
                   help="one JSON object on stdout")
    g.add_argument("--tsv", action="store_true", default=argparse.SUPPRESS,
                   help="tab-separated rows, append-only columns")
    return p


def register(subparsers: Any) -> argparse.ArgumentParser:
    """Add ``flash`` to the top-level subparsers. Returns its parser."""
    fmt = _fmt()
    vp = subparsers.add_parser(
        "flash", help=WHAT, parents=[fmt],
        description="Write SD cards in this computer's card reader: the board's configuration "
                    "SD (files) or the Linux harness's user microSD (card). Off by default: "
                    "harness-manager config set bringup.sd_flash on. Only card readers are "
                    "listed: never the system disk, never the board's own MCC drive.")
    sub = vp.add_subparsers(dest="flash_cmd", required=True, metavar="ACTION")
    ap = sub.add_parser("devices", help="the card readers' cards, with the phrase a write needs",
                        description="The cards in this computer's card readers: id, path, "
                                    "model, size, what each can take, and the phrase a write "
                                    "needs. --all also lists every other disk and why it is "
                                    "not offered.",
                        parents=[fmt], epilog=f"--tsv columns: {' '.join(DEVICE_COLUMNS)}")
    ap.add_argument("--all", action="store_true",
                    help="also list the disks that are not offered, each with why")
    ap = sub.add_parser("write", help="write one card (asks for its typed phrase)",
                        description="Write one card. files: a harness bundle folder or .zip "
                                    "onto the configuration SD's mounted FAT volume (a backup "
                                    "first, never an .ebf, the card's MBBIOS line kept). card: a "
                                    "whole-card image (stage0_mkcard.py card --card-img) onto the "
                                    "whole device; linux_slot.img alone is refused. Both are "
                                    "unsigned: type INSTALL UNSIGNED <first 8 hex of its sha256> "
                                    "as well as WRITE <model> <size>. Read back and compared "
                                    "before it says written.",
                        parents=[fmt], epilog=f"--tsv columns: {' '.join(WRITE_COLUMNS)}")
    ap.add_argument("device_id", metavar="DEVICE_ID",
                    help="the id `harness-manager flash devices` printed")
    ap.add_argument("source", metavar="SOURCE",
                    help="files: the bundle folder or .zip; card: the whole-card image file")
    ap.add_argument("--kind", required=True, choices=cw.KINDS,
                    help="files (the configuration SD) or card (a whole-card image)")
    ap.add_argument("--confirm", default=None, metavar="PHRASE",
                    help='the typed phrase, given here instead of at the prompt: exactly '
                         '"WRITE <model> <size>" as `flash devices` shows it')
    ap.add_argument("--confirm-unsigned", default=None, metavar="PHRASE",
                    help='the unsigned phrase, given here instead of at the prompt: exactly '
                         '"INSTALL UNSIGNED <first 8 hex of its sha256>" as the write prints it; '
                         "--yes never implies it")
    ap.add_argument("--backup", default=None, metavar="ZIP",
                    help="files: a backup of this card as it is now (`sd backup` made it); "
                         "without it a new backup is taken first")
    ap.add_argument("--backup-dir", default=None, metavar="DIR",
                    help="files: where the new backup goes (default: backups/ in Harness "
                         "Manager's state directory)")
    ap.add_argument("--allow-mcc-update", action="store_true",
                    help="files: write the bundle's MBBIOS line even though the .ebf it names "
                         "is on the card (the MCC then updates itself at its next boot)")
    ap.add_argument("--yes", action="store_true",
                    help="never ask (a script): the phrases must then come with --confirm and "
                         "--confirm-unsigned")
    vp.set_defaults(fn=cmd_flash)
    return vp


def writer() -> cw.CardWriter:
    return WRITER if WRITER is not None else cw.CardWriter()


def cmd_flash(ctx: Ctx) -> int:
    if ctx.args.flash_cmd == "devices":
        return _devices(ctx)
    return _write(ctx)


def _devices(ctx: Ctx) -> int:
    doc = writer().devices_json()
    rows = [[d["id"], d["path"], d["model"], d["size_bytes"], d["size"], d["mounted"],
             d.get("fs", ""), _can(d, "files"), _can(d, "card"), d["confirm"],
             d["needs_privilege"]] for d in doc["devices"]]
    if not doc["enabled"]:
        human = [doc["reason"]]
        if doc.get("supported", True):
            human.append("turn it on: harness-manager config set bringup.sd_flash on")
    elif not doc["devices"]:
        human = ["no card in a card reader (insert one; `flash devices --all` says why each "
                 "other disk is not offered)"]
    else:
        human = []
        for d in doc["devices"]:
            human.append(f"{d['id']:<22} {d['path']:<14} {d['model']:<20} {d['size']:>9}  "
                         f"{', '.join(d['labels']) or '-'}")
            for kind in cw.KINDS:
                why = d["kinds"][kind]["why_not"]
                human.append(f"    {kind:<6} {'yes' if not why else 'no: ' + why}")
            human.append(f"    type   {d['confirm']}"
                         + ("   (needs root: the write prints the sudo command)"
                            if d["needs_privilege"] else ""))
    if getattr(ctx.args, "all", False) and doc["excluded"]:
        human.append("not offered:")
        human += [f"    {e['path']:<16} {(e['model'] or '-')[:24]:<24} {e['size']:>9}  {e['why']}"
                  for e in doc["excluded"]]
    ctx.emit(Result("flash devices", doc, rows=rows, human=human))
    return ExitCode.OK


def _can(d: dict[str, Any], kind: str) -> str:
    return "yes" if d["kinds"][kind]["ok"] else "no"


def _ask(ctx: Ctx, phrase: str, what: str = "To write it, type exactly") -> str:
    stream = ctx.err or sys.stderr
    stream.write(f"{what}: {phrase}\n> ")
    stream.flush()
    try:
        return sys.stdin.readline().strip()
    except (OSError, ValueError):
        return ""


def _unsigned(ctx: Ctx, w: cw.CardWriter, plan: cw.WritePlan) -> None:
    """The red banner, the sha256 and how it is made, then INSTALL UNSIGNED <sha8>:
    ``--confirm-unsigned``, else asked; ``--yes`` never types it."""
    from harness_manager.services import unsigned

    a = ctx.args
    info = w.unsigned_of(plan.source)
    ctx.note(unsigned.BANNER)
    of = {"zip": "the zip", "file": "the image"}.get(info.of, f"its manifest of {info.files} files")
    ctx.note(f"sha256   {info.sha256}  ({of}: {info.how})")
    if a.confirm_unsigned is not None:
        typed = a.confirm_unsigned
    elif a.yes:
        raise with_data(RefusedError(f"not confirmed: --yes never types the phrase; give it with "
                                     f"--confirm-unsigned {info.phrase!r}",
                                     hint=f"{unsigned.BANNER} Nothing was written"),
                        unsigned=info.as_dict())
    else:
        typed = _ask(ctx, info.phrase, f"To install this unsigned {info.noun}, type exactly")
    w.check_unsigned(plan, typed)


def _write(ctx: Ctx) -> int:
    a = ctx.args
    w = writer()
    w.require()
    source = Path(a.source).expanduser().resolve()
    backup_path = Path(a.backup).expanduser().resolve() if a.backup else None
    backup_dir = Path(a.backup_dir).expanduser().resolve() if a.backup_dir else None
    if a.kind == "files" and backup_path is None and backup_dir is None:
        from harness_manager.settings.files import config_dir

        backup_dir = Path(config_dir(None)) / "backups"
    if a.kind == "card" and (backup_path or backup_dir or a.allow_mcc_update):
        raise UsageError("--backup, --backup-dir and --allow-mcc-update are for --kind files")
    plan = w.plan(a.device_id, a.kind, source, backup_path=backup_path, backup_dir=backup_dir,
                  allow_mcc_update=a.allow_mcc_update)
    disk = plan.device.disk
    ctx.note(f"device   {disk.path}  {disk.display_model}  {cw.human_size(disk.size)}  "
             f"({disk.transport})")
    if plan.card is not None:
        ctx.note(f"writes   {source} ({cw.human_size(plan.card.size)}): {plan.card.describe()}")
        ctx.note("         the whole card is overwritten from its first byte")
    else:
        ctx.note(f"writes   {len(plan.files)} files from {source} onto {plan.device.files_root}")
        ctx.note("backup   " + (str(backup_path) if backup_path else f"a new one in {backup_dir}"))
        for d in plan.mbbios:
            if d["note"]:
                ctx.note(d["note"])
    _unsigned(ctx, w, plan)
    if a.confirm is not None:
        typed = a.confirm
    elif a.yes:
        raise with_data(RefusedError(f"not confirmed: --yes never asks; give the phrase with "
                                     f"--confirm {plan.confirm!r}",
                                     hint="nothing was written"),
                        confirm=plan.confirm, device_id=plan.device.id)
    else:
        typed = _ask(ctx, plan.confirm)
    w.check_confirm(plan, typed)
    out = w.run(plan, progress=StderrProgress(f"flash {a.kind}", ctx.err))
    if out["outcome"] == "needs_privilege":
        for line in privileged_text(out, disk.io_path):
            ctx.note(line)
        hint = (f"run: {out['privileged_command']}" if not out.get("privileged_how") else
                "run the Administrator PowerShell steps above, or write it with "
                f"{out.get('imager', {}).get('name', 'an imager')}")
        err = UnavailableError(cw.CAPABILITY, f"this user may not write {disk.io_path}",
                               hint=hint)
        raise with_data(err, **{k: out[k] for k in (
            "outcome", "privileged_command", "privileged_steps", "verify_command",
            "verify_expect", "image", "bytes", "sha256", "privileged_shell",
            "privileged_how", "verify_how", "imager", "disk_number") if k in out})
    human = [f"written  {disk.path}: {out['outcome']}, read back and verified "
             f"(sha256 {out['sha256'][:16]}…)"]
    human += [d["note"] for d in out.get("mbbios", []) if d["note"]]
    if out.get("backup"):
        human.append(f"backup   {out['backup']['path']}")
    human.append(out.get("note", ""))
    ctx.emit(Result("flash write", out,
                    rows=[[plan.device.id, disk.path, a.kind, out["outcome"], out["verified"],
                           out["sha256"], ""]],
                    human=[h for h in human if h]))
    return ExitCode.OK


def privileged_text(out: dict, path: str) -> list[str]:
    """What the CLI prints when the write needs privileges HM never takes: each step, then
    how to verify (Windows: the Administrator PowerShell steps and the imager)."""
    if not out.get("privileged_how"):
        return [f"Harness Manager may not write {path} (it never asks for root). Run this "
                f"yourself:", f"    {out['privileged_command']}", "then check it:",
                f"    {out['verify_command']}     ({out['verify_expect']})"]
    lines = [f"Harness Manager never writes a whole disk on Windows ({path}): "
             f"{out['privileged_how']}:"]
    lines += [f"  {i}. {step}" for i, step in enumerate(out["privileged_steps"], 1)]
    lines += [f"then check it ({out.get('verify_how') or 'the same PowerShell'}; it "
              f"{out['verify_expect']}):", f"    {out['verify_command']}"]
    imager = out.get("imager") or {}
    if imager:
        lines.append(f"Or with {imager['name']} ({imager['url']}):")
        lines += [f"  - {step}" for step in imager.get("steps", [])]
    return lines
