"""``harness-manager slot`` and ``harness-manager card`` (lane LINUX-SLOTS; HARNESS-DIST H12, D13).

Verbs::

    harness-manager slot status   TARGET                       the OS slots A/B: running, default,
                                                               where a push goes, the card job
    harness-manager slot push     TARGET [IMAGE] (--bundle DIR|linux_bundle.json | --static-id ID)
                                  [--version V] [--rollback-first] [--yes]
                                                               push into the free slot, read back
    harness-manager slot commit   TARGET [--slot A|B] [--yes]  the pushed slot boots next
    harness-manager slot rollback TARGET [--no-reboot] [--wait S] [--yes]
                                                               the other slot boots again
    harness-manager slot verify   TARGET [--slot A|B]          read a slot back off the card
    harness-manager card status   TARGET                       the user microSD: present, store,
                                                               the power-on default, OS slots
    harness-manager card commit   TARGET [--yes]               the RUNNING overlay becomes the
                                                               power-on default (a re-push)
    harness-manager card clear    TARGET [--yes]               no default: greybox at power-on

The OS slots are the Linux harness's (``version.impl == "linux"``); the card store needs the
``usd`` feature. On any other harness every verb stops with exit 12 before sending anything.
**No card: the board boots exactly as it always has**, and every card change is refused.

Every change needs the board's lease (behind a hub) and a confirm (``--yes`` skips it); the
status verbs need neither. Both are Harness Manager's own rules: the board has no lease and
no confirm on slot acts (an unclaimed board takes them from anyone; a claimed one only from
itself, through its SSH, which Harness Manager uses). A push names the static the image was
provisioned for (from ``--bundle``, or ``--static-id``), never the board's own. After a
``slot commit`` and before the reboot no slot is free (rule 1): ``slot rollback`` first, or
``slot push --rollback-first``. The same holds after a FALLBACK (the default slot failed to
boot and stage0 went back to the other one): ``slot status`` says so, and ``slot rollback``
makes the running slot the default again.

``verified: boot`` means stage0 booted the slot, not that harnessd confirmed the boot:
status says "booted (not yet confirmed)" until the harness reports ``confirmed``. ``slot
verify`` reads a whole slot back and holds the board's card for minutes (other card jobs
get EBUSY meanwhile): it runs only when asked, never from a status read.

These verbs always run in this process (like ``update``): they need the board pack's own
slot and card adapters.

Exit codes: 0 done; 2 usage; 3 the running overlay is not in the local store (card commit);
4 not the lease holder, or the card is busy; 6 the board refused or a job failed; 7 the
board did not answer; 12 no OS slots / no card store on this harness; 14 the image is for
another static; 15 refused (no card, rule 1, not confirmed, a bad image).

``cli/main.py`` registers it with ``cmd_slots.register(sub)``.
"""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

from harness_manager.core.errors import ExitCode
from harness_manager.core.pack import CardStatus, SlotStatus

from .context import Ctx
from .output import TSV_COLUMNS, Result, StderrProgress

SLOT_TSV: dict[str, tuple[str, ...]] = {
    "slot": ("BOARD_ID", "SLOT", "STATE", "RUNNING", "DEFAULT", "TARGET", "VERIFIED", "HDR_CRC",
             "LEN", "VERSION", "JOB", "DETAIL"),
    "card": ("BOARD_ID", "PRESENT", "STATE", "CARD_MB", "DEFAULT_RM", "DEFAULT_SLOT",
             "DEFAULT_STATIC", "BOOT", "OS_RUNNING", "OS_DEFAULT", "DETAIL"),
}
TARGET_HELP = "shell address host[:port] (the board's harness)"


def _fmt() -> argparse.ArgumentParser:
    fmt = argparse.ArgumentParser(add_help=False)
    g = fmt.add_mutually_exclusive_group()
    g.add_argument("--json", action="store_true", default=argparse.SUPPRESS,
                   help="one JSON object on stdout")
    g.add_argument("--tsv", action="store_true", default=argparse.SUPPRESS,
                   help="tab-separated rows, append-only columns")
    return fmt


def _board() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(add_help=False)
    p.add_argument("target", metavar="TARGET", help=TARGET_HELP)
    # The same metavar and help as context.VIA_METAVAR/VIA_HELP (a test keeps them equal).
    p.add_argument("--via", metavar="ssh:HOST|hub", default=argparse.SUPPRESS,
                   help="reach the shell through an SSH tunnel on HOST (ssh:HOST), or through "
                        "the hub the board's boards.toml hub table names (hub); without --via, "
                        "the board's boards.toml via does the same")
    return p


def register(subparsers: Any) -> dict[str, argparse.ArgumentParser]:
    """Add ``slot`` and ``card``. Returns ``{name: parser}``."""
    for layout, cols in SLOT_TSV.items():
        TSV_COLUMNS.setdefault(layout, cols)
    fmt, board = _fmt(), _board()
    yes = argparse.ArgumentParser(add_help=False)
    yes.add_argument("--yes", action="store_true", help="do not ask for confirmation")

    def epilog(layout: str) -> str:
        return f"--tsv columns: {' '.join(SLOT_TSV[layout])}"

    sp = subparsers.add_parser(
        "slot", help="the Linux harness's OS slots A/B on the board's card: status, push, "
                     "commit, rollback, verify",
        description="The Linux harness boots from OS slot A or B on the board's user microSD. "
                    "A push goes to the slot that is neither running nor the default, is read "
                    "back by the board, and boots only after `slot commit` and a reboot.",
        parents=[fmt])
    ssub = sp.add_subparsers(dest="slot_cmd", required=True, metavar="ACTION")
    ssub.add_parser("status", help="running, default, where a push goes, the card job",
                    parents=[fmt, board], epilog=epilog("slot"))
    ap = ssub.add_parser("push", help="push a boot image into the free slot and read it back",
                         description="Push a boot image into the free OS slot and read it "
                                     "back; it is not committed. --bundle or --static-id is "
                                     "required: it names the static the image was provisioned "
                                     "for, never the board's own. Given both, --static-id must "
                                     "match the bundle's provisioned static.",
                         parents=[fmt, board, yes], epilog=epilog("slot"))
    ap.add_argument("image", nargs="?", default=None, metavar="IMAGE",
                    help="the S0LB boot image (default: linux_slot.img beside --bundle)")
    ap.add_argument("--bundle", default=None, metavar="PATH",
                    help="the release's linux_bundle.json (or its directory): the provisioned "
                         "static, the image's sha256 and frames (this or --static-id is "
                         "required)")
    ap.add_argument("--static-id", default="", metavar="ID",
                    help="the static the image was provisioned for (this or --bundle is "
                         "required)")
    ap.add_argument("--version", dest="image_version", default="", metavar="V",
                    help="the release this image is (shown by slot status)")
    ap.add_argument("--rollback-first", action="store_true",
                    help="if a commit is waiting for a reboot (no free slot), undo it first")
    ap = ssub.add_parser("commit", help="make the pushed slot the one that boots next",
                         parents=[fmt, board, yes], epilog=epilog("slot"))
    ap.add_argument("--slot", choices=("A", "B"), default=None,
                    help="refuse unless this is the slot the commit would pick")
    ap = ssub.add_parser("rollback", help="make the other slot the default again (and boot it)",
                         parents=[fmt, board, yes], epilog=epilog("slot"))
    ap.add_argument("--no-reboot", action="store_true",
                    help="do not reboot into it now (it boots at the next reboot)")
    ap.add_argument("--wait", type=float, default=180.0, metavar="S",
                    help="how long the reboot may take (default 180)")
    ap = ssub.add_parser("verify", help="read a slot back off the card (open to anyone; holds "
                                        "the card for minutes, other card jobs get EBUSY)",
                         parents=[fmt, board], epilog=epilog("slot"))
    ap.add_argument("--slot", choices=("A", "B"), default=None,
                    help="which slot (default: the one that is not the default)")
    sp.set_defaults(fn=cmd_slot)

    cp = subparsers.add_parser(
        "card", help="the board's user microSD: status, commit the running overlay as the "
                     "power-on default, clear it",
        description="The user microSD holds the overlay the board loads at power-on (and, on "
                    "the Linux harness, its OS slots). No card: the board boots exactly as it "
                    "always has.",
        parents=[fmt])
    csub = cp.add_subparsers(dest="card_cmd", required=True, metavar="ACTION")
    csub.add_parser("status", help="present, the store, the power-on default, the OS slots",
                    parents=[fmt, board], epilog=epilog("card"))
    csub.add_parser("commit", help="the RUNNING overlay becomes the power-on default",
                    parents=[fmt, board, yes], epilog=epilog("card"))
    csub.add_parser("clear", help="no power-on default: the greybox loads at power-on",
                    parents=[fmt, board, yes], epilog=epilog("card"))
    cp.set_defaults(fn=cmd_card)
    return {"slot": sp, "card": cp}


def _service(ctx: Ctx) -> Any:
    from harness_manager.services.slots import SlotService, lease_check_for

    store = getattr(ctx.engine, "store", None)
    return SlotService(lease_check=lease_check_for(ctx.engine),
                       bus=getattr(ctx.engine, "bus", None), store=store)


# --- output ---------------------------------------------------------------------------------------


def slot_json(board_id: str, st: SlotStatus) -> dict[str, Any]:
    from harness_manager.services.slot_health import extend_json
    from harness_manager.services.slots import slot_status_json

    return {"board_id": board_id, **extend_json(slot_status_json(st), st)}


def _job(st: SlotStatus) -> str:
    j = st.job
    if j.act == "none":
        return j.state
    return f"{j.act} {j.slot or '-'} {j.state}" + (f" ({j.err})" if j.err else "")


def slot_rows(board_id: str, st: SlotStatus, detail: str = "") -> list[list[Any]]:
    from harness_manager.services import slot_health

    fell = slot_health.fell_back(st)
    return [[board_id, n, s.state, "yes" if n == st.running else "no",
             "yes" if n == st.default else "no", "yes" if n == st.target else "no",
             s.verified, s.hdr_crc, s.length or "", s.version, _job(st),
             detail or s.err or ("failed to boot (stage0 fell back)" if n == fell else "")]
            for n, s in sorted(st.slots.items())] or \
        [[board_id, "", "", st.running, st.default, st.target, "", "", "", "", _job(st),
          detail or ("no card" if not st.card else "")]]


def slot_human(board_id: str, st: SlotStatus) -> list[str]:
    from harness_manager.services import slot_health

    if not st.card:
        return [f"slots      {board_id}: no user microSD card (running {st.running})"]
    fell = slot_health.fell_back(st)
    nowhere = ("no slot (roll back first: the default failed to boot)" if fell else
               "no slot (roll back the pending commit first)")
    lines = [f"slots      {board_id}: running {st.running}, default {st.default or '?'}, "
             f"a push goes to {st.target or nowhere}"]
    for n, s in sorted(st.slots.items()):
        marks = [m for m, on in (("running", n == st.running), ("default", n == st.default),
                                 ("staged", n == st.staged), ("target", n == st.target),
                                 ("FAILED TO BOOT", n == fell)) if on]
        what = s.state + (f" hdr_crc {s.hdr_crc} {s.length} B" if s.valid else "")
        what += f", {slot_health.boot_words(st, n)}" if s.valid else ""
        what += f", release {s.version}" if s.version else ""
        what += f" ({s.err})" if s.err else ""
        lines.append(f"  {n}        {what}" + (f"  [{', '.join(marks)}]" if marks else ""))
    lines.append(f"job        {_job(st)}")
    unbooted = slot_health.committed_unbooted(st)
    if unbooted:
        lines.append(f"note       slot {unbooted} is committed and boots at the next "
                     "reboot; no slot is free until then (rule 1)")
    lines += [f"note       {n}" for n in slot_health.notes(st)]
    return lines


def card_json(board_id: str, st: CardStatus) -> dict[str, Any]:
    from harness_manager.services.slot_health import extend_json
    from harness_manager.services.slots import card_status_json

    doc = card_status_json(st)
    if st.os_slots is not None and isinstance(doc.get("os_slots"), dict):
        extend_json(doc["os_slots"], st.os_slots)
    return {"board_id": board_id, **doc}


def card_rows(board_id: str, st: CardStatus) -> list[list[Any]]:
    d = st.default or {}
    os_st = st.os_slots
    return [[board_id, "yes" if st.present else "no", st.state, st.card_mb or "",
             d.get("rm_name") or d.get("rm_id", ""), d.get("slot", ""), d.get("static_id", ""),
             st.boot, os_st.running if os_st else "", os_st.default if os_st else "",
             " | ".join(st.notes)]]


def card_human(board_id: str, st: CardStatus) -> list[str]:
    if not st.present:
        return [f"card       {board_id}: no card (the board boots exactly as it always has)"]
    lines = [f"card       {board_id}: {st.state}" + (f", {st.card_mb} MB" if st.card_mb else "")
             + (f"  (CLCD: {st.text})" if st.text else "")]
    d = st.default
    if d:
        lines.append(f"default    {d.get('rm_name') or '?'} {d.get('rm_id')} for static "
                     f"{d.get('static_id') or '?'}, store slot {d.get('slot') or '?'}")
    else:
        lines.append("default    none: the greybox loads at power-on")
    if st.boot:
        lines.append(f"power-on   {st.boot}")
    if st.os_slots is not None:
        lines += [ln.replace("slots      ", "os slots   ", 1) for ln in
                  slot_human(board_id, st.os_slots)]
    lines += [f"note       {n}" for n in st.notes if "no card" not in n]
    return lines


# --- slot -----------------------------------------------------------------------------------------


def cmd_slot(ctx: Ctx) -> int:
    act = ctx.args.slot_cmd
    svc = _service(ctx)
    with ctx.board(note=f"cli slot {act}") as (cand, session):
        bid = cand.board_id
        if act == "status":
            st = svc.status(session)
            ctx.emit(Result("slot", slot_json(bid, st), rows=slot_rows(bid, st),
                            human=slot_human(bid, st)))
            return ExitCode.OK
        if act == "verify":
            out = svc.verify(session, ctx.args.slot, progress=StderrProgress("verify", ctx.err))
            return _slot_result(ctx, bid, out, f"slot {out['slot']} read back: "
                                               f"verified {_verified(out['status'], out['slot'])}")
        if act == "push":
            from harness_manager.services.slots import push_source

            a = ctx.args
            source = push_source(Path(a.image) if a.image else None,
                                 bundle=Path(a.bundle) if a.bundle else None,
                                 static_id=a.static_id, version=a.image_version)
            svc.slots(session)                    # unavailable: stop before the prompt
            ctx.confirm(f"push {source.image.name} (for static {source.static_id}) into the free "
                        f"OS slot of {bid}? It is read back and NOT committed.")
            out = svc.push(session, source, rollback_first=a.rollback_first,
                           progress=StderrProgress("push", ctx.err))
            undone = (f" (the pending commit of slot {out['rolled_back_first']} was rolled "
                      "back first)" if out["rolled_back_first"] else "")
            return _slot_result(ctx, bid, out, f"slot {out['slot']} holds {source.image.name}, "
                                               f"read back{undone}; `harness-manager slot commit "
                                               f"{ctx.args.target}` makes it boot next")
        if act == "commit":
            svc.slots(session)
            ctx.confirm(f"make the pushed OS slot the one {bid} boots next?")
            out = svc.commit(session, ctx.args.slot)
            return _slot_result(ctx, bid, out, out["note"])
        if act == "rollback":
            from harness_manager.services import slot_health

            before = svc.status(session)
            fell = slot_health.fell_back(before)
            if fell:
                # A fallback: the running slot becomes the default again; nothing reboots.
                ctx.confirm(f"slot {fell} of {bid} failed to boot and {before.running} is "
                            f"running: make {before.running} the default again?")
            else:
                ctx.confirm(f"roll {bid}'s OS slot back"
                            + ("" if ctx.args.no_reboot else " and reboot it into the other slot")
                            + "?")
            out = svc.rollback(session, reboot=not ctx.args.no_reboot, wait_s=ctx.args.wait,
                               progress=StderrProgress("rollback", ctx.err))
            if fell:
                out["note"] = (f"slot {out['slot']} is the default again (slot {fell} failed to "
                               f"boot); slot {fell} is free for a push")
            note = out.get("note") or (f"slot {out['slot']} runs again (rebooted)"
                                       if out["rebooted"] else f"slot {out['slot']} is the default")
            return _slot_result(ctx, bid, out, note)
    return ExitCode.USAGE


def _verified(st: SlotStatus, slot: str) -> str:
    info = st.slots.get(slot)
    return info.verified if info else "?"


def _slot_result(ctx: Ctx, bid: str, out: dict[str, Any], line: str) -> int:
    st: SlotStatus = out["status"]
    data = {**slot_json(bid, st), **{k: v for k, v in out.items() if k != "status"},
            "result": line}
    ctx.emit(Result("slot", data, rows=slot_rows(bid, st, line),
                    human=[f"done       {line}", *slot_human(bid, st)]))
    return ExitCode.OK


# --- card -----------------------------------------------------------------------------------------


def cmd_card(ctx: Ctx) -> int:
    act = ctx.args.card_cmd
    svc = _service(ctx)
    with ctx.board(note=f"cli card {act}") as (cand, session):
        bid = cand.board_id
        if act == "status":
            st = svc.card_status(session)
            ctx.emit(Result("card", card_json(bid, st), rows=card_rows(bid, st),
                            human=card_human(bid, st)))
            return ExitCode.OK
        if act == "commit":
            svc.card(session)
            ctx.confirm(f"write the overlay running on {bid} to its card as the power-on "
                        "default?")
            out = svc.card_commit(session, progress=StderrProgress("commit", ctx.err))
            st = svc.card_status(session)
            line = (f"{out['rm_name']} ({out['rm_id']}) is the power-on default, store slot "
                    f"{out['slot']}")
            ctx.emit(Result("card", {**card_json(bid, st), "committed": out, "result": line},
                            rows=card_rows(bid, st),
                            human=[f"done       {line}", *card_human(bid, st)]))
            return ExitCode.OK
        if act == "clear":
            svc.card(session)
            ctx.confirm(f"clear {bid}'s power-on default? The greybox loads at the next "
                        "power-on.")
            st = svc.card_clear(session)
            line = "no power-on default: the greybox loads at the next power-on"
            ctx.emit(Result("card", {**card_json(bid, st), "result": line},
                            rows=card_rows(bid, st),
                            human=[f"done       {line}", *card_human(bid, st)]))
            return ExitCode.OK
    return ExitCode.USAGE
