"""Board-level verbs over session adapters: reset, clock, mcc (controller), sd (storage)."""

from __future__ import annotations

import hashlib
import re
import time
import zipfile
from pathlib import Path

from harness_manager.core import capabilities as C
from harness_manager.core.errors import AbsentError, ExitCode, RefusedError, UsageError
from harness_manager.core.pack import BackupRecord
from harness_manager.services import reset_guard

from .context import Ctx
from .guard import guard_consent, is_reboot_line
from .output import TSV_COLUMNS, Result, StderrProgress, reading_human, reading_json, reading_row

# --- reset ---------------------------------------------------------------------------------


def reset_on(ctx: Ctx, session, board_id: str, what: str) -> None:
    """Reset ``what`` on an open session: the ``reset`` verb, and the console's Ctrl-] r
    (FIX-PACK-5), so both refuse the same way."""
    resets = ctx.require(session, "resets", C.RESET_DUT)
    targets = list(resets.reset_targets())
    if what not in targets:
        raise UsageError(f"{board_id} cannot reset {what!r}",
                         hint=f"reset targets on this board: {', '.join(targets) or 'none'}")
    resets.reset(what)


def cmd_reset(ctx: Ctx) -> int:
    what = ctx.args.what
    with ctx.board() as (cand, session):
        reset_on(ctx, session, cand.board_id, what)
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
            if is_reboot_line(a.line):             # SLOT-TIMING: a REBOOT is a reset
                force, consent = guard_consent(ctx, session, reset_guard.ACTION_MCC_REBOOT)
                with reset_guard.guarded(session, reset_guard.ACTION_MCC_REBOOT, force=force,
                                         consent=consent):
                    reply = ctl.command(a.line)
            else:
                reply = ctl.command(a.line)        # the allowlist lives in the adapter
            ctx.emit(Result("mcc cmd", {"board_id": cand.board_id, "command": a.line,
                                        "reply": reply},
                            rows=[[cand.board_id, a.line, reply]],
                            human=reply.splitlines() or ["(no reply text)"]))
            return ExitCode.OK
        # reboot. SLOT-TIMING: never while the board's card job writes or reads back.
        if a.wait is not None and a.wait <= 0:
            raise UsageError(f"--wait {a.wait:g} is not a wait", hint="give seconds, more than 0")
        force, consent = guard_consent(ctx, session, reset_guard.ACTION_MCC_REBOOT)
        ctx.confirm(f"reboot {cand.board_id}? The board reloads from its SD and the running "
                    "design is lost")
        progress = StderrProgress("reboot", ctx.err)
        # --wait unset = None: the pack's own budget for the running harness (MCC-FIX).
        with reset_guard.guarded(session, reset_guard.ACTION_MCC_REBOOT, force=force,
                                 consent=consent):
            evidence = ctl.reboot(progress=progress, wait_s=a.wait)
        # FIX-PACK-6 item 3: never trust the reported design after a cold boot: one IDCODE
        # read (the service's job made it; in-process, here). It never fails the reboot.
        from harness_manager.services import design_check

        check = design_check.from_result(ctx.engine, session, evidence,
                                         after=design_check.AFTER_MCC_REBOOT,
                                         bus=getattr(ctx.engine, "bus", None))
    ev = evidence if isinstance(evidence, dict) else {}
    ev.pop("design_check", None)
    fpga_file = str(ev.get("fpga_file") or "")
    human = [f"rebooted   {cand.board_id} (seen: {', '.join(progress.phases) or '-'})"]
    if fpga_file:
        human.append(f"MCC loaded {fpga_file}")
    human += design_check.human(check)
    ctx.emit(Result("mcc reboot", {"board_id": cand.board_id, "result": "rebooted",
                                   "phases": progress.phases, "fpga_file": fpga_file,
                                   "mcc_firmware": str(ev.get("mcc_firmware") or ""),
                                   "evidence": ev, "design_check": check},
                    rows=[[cand.board_id, "rebooted", progress.phases, fpga_file]],
                    human=human))
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
                          hint="make one with `harness-manager sd TARGET backup DIR`")
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


#: FIX-PACK-7 (G8): `sd install`, `harness install`, `update harness`.
ALLOW_MCC_UPDATE_HELP = ("write the bundle's MBBIOS line even when the card has none and has "
                         "the .ebf it names (the MCC then updates itself to it at its next "
                         "boot); refused without this")


def install_notes(storage: object) -> list[str]:
    """What the config SD's last install said beside its files ("MBBIOS kept: …")."""
    return [str(n) for n in (getattr(storage, "install_notes", None) or [])]


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
            allow = {"allow_mcc_update": True} if getattr(a, "allow_mcc_update", False) else {}
            storage.install(files, backup=rec, progress=progress, **allow)
            notes = install_notes(storage)              # FIX-PACK-7: "MBBIOS kept: …"
            ctx.emit(Result("sd install", {"board_id": cand.board_id, "files": sorted(files),
                                           "backup": rec, "notes": notes},
                            rows=[[cand.board_id, len(files), rec.sha256]],
                            human=[f"written    {len(files)} files to the SD of {cand.board_id}",
                                   *notes,
                                   "not running yet: reboot (`harness-manager mcc TARGET reboot`), "
                                   "then check `harness-manager info TARGET`"]))
            return ExitCode.OK
        # restore
        rec = backup_record(storage, Path(a.zip))
        ctx.confirm(f"overwrite the SD of {cand.board_id} with {rec.path}?")
        storage.restore(rec, progress=progress)
    ctx.emit(Result("sd restore", {"board_id": cand.board_id, "backup": rec},
                    rows=[[cand.board_id, rec.path, rec.sha256]],
                    human=[f"restored   {cand.board_id} SD from {rec.path}"]))
    return ExitCode.OK


# --- board keys: who else may log in (ADOPT-GUI) -------------------------------------------------
#
#   harness-manager board keys list   TARGET
#   harness-manager board keys add    TARGET (--key "ssh-ed25519 AAAA..." | --key-file FILE.pub)
#                                            [--label NAME] [--yes]
#   harness-manager board keys remove TARGET --fingerprint SHA256:... [--yes]
#
# ``cmd_claim.register`` adds the group to ``board`` with ``add_keys_parser`` and routes
# ``board_cmd == "keys"`` to ``cmd_keys``. Output shows fingerprints only, never a key.

KEYS_TSV: tuple[str, ...] = ("BOARD_ID", "FINGERPRINT", "TYPE", "LABEL", "MINE", "ACTION")


def add_keys_parser(sub, parents, action_parents=None) -> None:
    """Add ``keys`` (list, add, remove) to the ``board`` group's subparsers. ``parents`` give
    ``keys`` its TARGET and --via; the actions take only ``action_parents`` (the output format),
    so TARGET is asked once: ``board keys TARGET list`` (16 Oct: it was asked twice)."""
    action_parents = list(parents[:1] if action_parents is None else action_parents)
    TSV_COLUMNS.setdefault("board keys", KEYS_TSV)
    kp = sub.add_parser(
        "keys", help="who may log in to a claimed board: list, add or remove a public key "
                     "(over the claim's SSH; add and remove ask first and need the lease)",
        description="The public keys a claimed Linux harness accepts. Add a colleague's key "
                    "(one line from their .pub file) or a second PC's; remove one by its "
                    "fingerprint. Never the key this PC uses. Private keys are refused.",
        parents=list(parents), epilog=f"--tsv columns: {' '.join(KEYS_TSV)}")
    ks = kp.add_subparsers(dest="keys_cmd", required=True, metavar="ACTION")
    pa = ks.add_parser("list", help="the keys the board accepts, by fingerprint and label",
                       parents=action_parents)
    pa.set_defaults(keys_cmd="list")
    ad = ks.add_parser("add", help="add a public key (asks first, shows its fingerprint)",
                       parents=action_parents)
    ad.add_argument("--key", default=None, metavar="LINE",
                    help="the PUBLIC key line to add (ssh-ed25519 AAAA... comment)")
    ad.add_argument("--key-file", default=None, metavar="FILE",
                    help="a .pub file holding ONE public key line")
    ad.add_argument("--label", default="", metavar="NAME",
                    help="who or what this key is (default: the key's own comment)")
    ad.add_argument("--yes", action="store_true", help="do not ask")
    rm = ks.add_parser("remove", help="remove a key by fingerprint (asks first; never this PC's "
                                      "own key)", parents=action_parents)
    rm.add_argument("--fingerprint", required=True, metavar="SHA256:...",
                    help="the key to remove, as `board keys list` shows it")
    rm.add_argument("--yes", action="store_true", help="do not ask")


def _keys_service(ctx: Ctx):
    from harness_manager.core.errors import UnavailableError

    svc = getattr(ctx.engine, "board_claim", None)
    if svc is None:
        raise UnavailableError("ssh_claim", "this engine has no SSH claim service (update "
                                            "harness-manager-daemon, or set "
                                            "HARNESS_MANAGER_NO_DAEMON=1)")
    return svc


def _keys_rows(board_id: str, keys: list[dict], action: str = "") -> list[list]:
    return [[board_id, k["fingerprint"], k["type"], k["label"], "yes" if k.get("mine") else "no",
             action] for k in keys]


def _keys_human(keys: list[dict]) -> list[str]:
    return [f"{k['fingerprint']}  {k['label'] or '(no label)'}{'  (this PC)' if k.get('mine') else ''}"
            for k in keys] or ["no keys"]


def cmd_keys(ctx: Ctx) -> int:
    a = ctx.args
    action = a.keys_cmd
    with ctx.board(note="board keys") as (cand, session):
        svc = _keys_service(ctx)
        bid = cand.board_id
        if action == "list":
            keys = svc.keys_list(session)
            out, rows, human = {"keys": keys}, _keys_rows(bid, keys), _keys_human(keys)
        elif action == "add":
            if bool(a.key) == bool(a.key_file):
                raise UsageError("give the key with --key LINE or --key-file FILE (one of them)")
            if a.key_file:
                try:
                    text = Path(a.key_file).expanduser().read_text("utf-8", "replace")
                except OSError as exc:
                    raise UsageError(f"cannot read {a.key_file}: {exc.strerror or exc}") from exc
            else:
                text = a.key
            lines = [ln for ln in text.splitlines() if ln.strip() and not ln.startswith("#")]
            if a.key_file and len(lines) > 1 and "PRIVATE" not in text and "-----" not in text:
                text = lines[0]                      # a .pub file's first key; a private key
                                                     # file goes whole to the check and is refused
            info = svc.keys_preview(session, text, a.label)    # private/bad keys stop here
            svc.check_claimable(session)
            svc.check_lease(session, what="adding a key")
            ctx.confirm(f"add key {info['fingerprint']} ({info['label']}) to {bid}? That key "
                        "gets root on the board")
            res = svc.keys_add(session, confirm=True, key=text, label=a.label,
                               expect_fingerprint=info["fingerprint"], progress=ctx.note)
            keys = res["keys"]
            out = {"key": {k: v for k, v in res.items() if k != "keys"}, "keys": keys}
            word = "already there" if res["action"] == "already_there" else "added"
            rows = _keys_rows(bid, [k for k in keys if k["fingerprint"] == res["fingerprint"]],
                              res["action"])
            human = [f"{word}  {res['fingerprint']}  {res['label']}", *_keys_human(keys)]
        else:
            svc.check_claimable(session)
            svc.check_lease(session, what="removing a key")
            ctx.confirm(f"remove key {a.fingerprint} from {bid}? Its owner loses access")
            res = svc.keys_remove(session, confirm=True, fingerprint=a.fingerprint,
                                  progress=ctx.note)
            keys = res["keys"]
            out = {"key": {k: v for k, v in res.items() if k != "keys"}, "keys": keys}
            rows = [[bid, res["fingerprint"], "", "", "", "removed"]]
            human = [f"removed  {res['fingerprint']}", *_keys_human(keys)]
    ctx.emit(Result("board keys", {"board_id": bid, **out}, rows=rows, human=human))
    return ExitCode.OK
