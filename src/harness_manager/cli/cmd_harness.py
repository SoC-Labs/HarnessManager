"""``harness-manager harness``: the harness versions catalogue (HARNESS-CAT; HARNESS-DIST §8.1).

Verbs::

    harness list     [TARGET] [--channel C | --all]        the releases, with verdicts for TARGET
    harness show     VERSION [TARGET]                       one release: parts, notes, what changes
    harness fetch    VERSION [--kit]                        download + verify into the cache now
    harness install  TARGET [VERSION] [--consent PHRASE] [--yes] [--overlays-only]
    harness pin      TARGET VERSION                         never offer TARGET a newer release
    harness unpin    TARGET
    harness history  TARGET [--limit N]                     the board's last installs
    harness rollback TARGET [--to previous|VERSION] [--backup ZIP]
    harness mirror   DIR [--channel C ... | --all] [--versions V,V] [--include-private]

``update check|harness|rollback`` stay as T7 wrote them (an alias of the same core);
``update app`` is the app's own update.

Verdicts: ``fits``, ``re-key`` (the typed ``REKEY <static_id>`` consent), ``needs Debug USB
or hub``, ``incompatible``, each with its reason. Marks: ``running`` (the board reports the
release's identity), ``installed`` (HM's last install on the board), ``pinned``,
``current``, ``offered`` (what ``install`` with no VERSION gives), ``past-pin``.

Consent is the planner's: ``install`` and ``rollback`` ask ``[y/N]`` unless ``--yes``, and a
re-key ALSO needs the typed phrase (``--consent "REKEY 0x…"`` or at the prompt). A board
behind a hub is installed on only by the lease holder (exit 4 otherwise).

Exit codes are T7's: 8 nothing to do; 6 written but the board does not run it; 14 a bundle
that does not match its identity; 15 a safety rail (signature, serial, consent, …); 4 held
(the hub lease is someone else's, or nobody's).
"""

from __future__ import annotations

import argparse
import time
from pathlib import Path
from typing import Any

from harness_manager.core.errors import (
    AbsentError,
    ActionFailedError,
    AlreadyError,
    ExitCode,
    RefusedError,
    UsageError,
)

from . import cmd_update
from .context import Ctx
from .output import Result, with_data

TARGET_HELP = cmd_update.TARGET_HELP
CHANNELS = ("stable", "beta", "dev")


def _parents() -> tuple[argparse.ArgumentParser, ...]:
    fmt = argparse.ArgumentParser(add_help=False)
    g = fmt.add_mutually_exclusive_group()
    g.add_argument("--json", action="store_true", default=argparse.SUPPRESS,
                   help="one JSON object on stdout")
    g.add_argument("--tsv", action="store_true", default=argparse.SUPPRESS,
                   help="tab-separated rows, append-only columns")
    usb = argparse.ArgumentParser(add_help=False)
    usb.add_argument("--serial", action="append", metavar="URL", default=argparse.SUPPRESS,
                     help="add the board controller's USB serial link")
    usb.add_argument("--volume", action="append", metavar="PATH", default=argparse.SUPPRESS,
                     help="add the configuration SD volume (the mounted V2M-MPS3 drive)")
    src = argparse.ArgumentParser(add_help=False)
    src.add_argument("--source", default=None, metavar="URL|DIR",
                     help="where the channels are: a URL (may contain {channel}), a mirror dir, "
                          "or github:OWNER/REPO")
    one = argparse.ArgumentParser(add_help=False)
    one.add_argument("--channel", default=None, metavar="NAME",
                     help="stable (default), beta or dev")
    return fmt, usb, src, one


def _door_args(ap: argparse.ArgumentParser) -> None:
    """HUB-SD: the install door and its consent (HARNESS-DIST §8.1)."""
    ap.add_argument("--door", choices=("hub", "usb"), default=None,
                    help="where the config SD is written: hub (fpgahub on the lab hub) or "
                         "usb (the Debug USB here); default: the planner picks")
    ap.add_argument("--board-phrase", default="", metavar="PHRASE",
                    help='through the hub: the exact phrase the plan prints ("INSTALL … HELD '
                         'BY … N QUEUED"); never implied by --yes')
    ap.add_argument("--no-auto-revert", action="store_true",
                    help="through the hub: do not write the previous base back if the board "
                         "stays dark after the REBOOT (armed by default)")


def register(subparsers: Any) -> argparse.ArgumentParser:
    """Add ``harness`` and its verbs to the top-level subparsers. Returns the parser."""
    from .output import TSV_COLUMNS

    fmt, usb, src, one = _parents()
    vp = subparsers.add_parser(
        "harness", parents=[fmt],
        help="harness versions: list, show, fetch, install, pin, history, roll back, mirror",
        description="The harness versions catalogue: every signed release of this board "
                    "pack's harness, with what it would do to a board.")
    sub = vp.add_subparsers(dest="harness_cmd", required=True, metavar="ACTION")

    def epilog(layout: str) -> str:
        return f"--tsv columns: {' '.join(TSV_COLUMNS[layout])}"

    ap = sub.add_parser("list", help="the releases, with a verdict for TARGET",
                        parents=[fmt, usb, src, one], epilog=epilog("harness list"))
    ap.add_argument("target", nargs="?", default=None, metavar="TARGET", help=TARGET_HELP)
    ap.add_argument("--all", action="store_true", help="every channel: stable, beta and dev")

    ap = sub.add_parser("show", help="one release: identity, parts, notes, what changes",
                        parents=[fmt, usb, src, one], epilog=epilog("harness show"))
    ap.add_argument("version", metavar="VERSION")
    ap.add_argument("target", nargs="?", default=None, metavar="TARGET", help=TARGET_HELP)

    ap = sub.add_parser("fetch", help="download and verify a release into the cache now",
                        parents=[fmt, src, one], epilog=epilog("harness fetch"))
    ap.add_argument("version", metavar="VERSION")
    ap.add_argument("--kit", action="store_true", help="also the DUT build kit (10-40 MB)")

    ap = sub.add_parser("install", help="install a release on a board (asks first)",
                        parents=[fmt, usb, src, one], epilog=epilog("harness install"))
    ap.add_argument("target", metavar="TARGET", help=TARGET_HELP)
    ap.add_argument("version", nargs="?", default=None, metavar="VERSION",
                    help="default: what the board is offered (its pin, else current)")
    ap.add_argument("--overlays-only", action="store_true",
                    help="only store the release's overlays; no SD write, no reboot")
    ap.add_argument("--consent", default="", metavar="PHRASE",
                    help='for a re-key: the exact phrase the plan prints ("REKEY 0x…")')
    ap.add_argument("--yes", action="store_true", help="do not ask for confirmation")
    _door_args(ap)

    ap = sub.add_parser("pin", help="pin a board to a release (none newer is offered)",
                        parents=[fmt, usb, src, one], epilog=epilog("harness pin"))
    ap.add_argument("target", metavar="TARGET", help=TARGET_HELP)
    ap.add_argument("version", metavar="VERSION")
    ap.add_argument("--no-check", action="store_true",
                    help="do not look the version up in the channels (offline)")

    ap = sub.add_parser("unpin", help="remove a board's pin", parents=[fmt, usb],
                        epilog=epilog("harness unpin"))
    ap.add_argument("target", metavar="TARGET", help=TARGET_HELP)

    ap = sub.add_parser("history", help="a board's last installs, newest first",
                        parents=[fmt, usb], epilog=epilog("harness history"))
    ap.add_argument("target", metavar="TARGET", help=TARGET_HELP)
    ap.add_argument("--limit", type=int, default=None, metavar="N")

    ap = sub.add_parser("rollback", help="re-install the previous release (or restore a backup)",
                        parents=[fmt, usb, src, one], epilog=epilog("harness rollback"))
    ap.add_argument("target", metavar="TARGET", help=TARGET_HELP)
    ap.add_argument("--to", default="previous", metavar="previous|VERSION",
                    help="the release to go back to (default: the one the last install replaced)")
    ap.add_argument("--backup", default=None, metavar="ZIP",
                    help="restore this config-SD backup instead (T7's rollback)")
    ap.add_argument("--wait", type=float, default=None, metavar="S",
                    help="with --backup: how long to wait for the board to come back")
    ap.add_argument("--consent", default="", metavar="PHRASE",
                    help='for a re-key: the exact phrase the plan prints ("REKEY 0x…")')
    ap.add_argument("--yes", action="store_true", help="do not ask for confirmation")
    _door_args(ap)

    ap = sub.add_parser("mirror", help="write an offline mirror: channels + blobs/<sha256>",
                        parents=[fmt, src], epilog=epilog("harness mirror"))
    ap.add_argument("dir", metavar="DIR")
    ap.add_argument("--channel", action="append", default=None, metavar="NAME",
                    help="a channel to mirror (repeatable; default stable)")
    ap.add_argument("--all", action="store_true", help="stable, beta and dev")
    ap.add_argument("--versions", default=None, metavar="V[,V...]",
                    help="only these releases (default: every listed one)")
    ap.add_argument("--include-private", action="store_true",
                    help="also the Arm-IP (github-token) parts: a mirror is often readable "
                         "by more people than the private repo")

    vp.set_defaults(fn=cmd_harness)
    return vp


# --- plumbing ---------------------------------------------------------------------------


def catalog(ctx: Ctx) -> Any:
    from harness_manager.services.harness_catalog import HarnessCatalog

    return HarnessCatalog(cmd_update.service(ctx))


def _channels(a: argparse.Namespace) -> list[str]:
    if getattr(a, "all", False):
        return list(CHANNELS)
    ch = getattr(a, "channel", None)
    if isinstance(ch, list):
        return ch or ["stable"]
    return [ch] if ch else ["stable"]


def _join(items: Any) -> str:
    return ",".join(str(i) for i in items) if items else "-"


def _row_line(r: dict[str, Any]) -> str:
    marks = _join(r["marks"])
    verdict = r["verdict_text"] or "-"
    return (f"{r['version']:<9} {_join(r['channels']):<12} {r['static_id']:<11} "
            f"{r['impl'] or '?':<10} {verdict:<23} {marks:<22} {r['why']}")


def _board_lines(board: dict[str, Any] | None) -> list[str]:
    if not board:
        return ["board      none given: verdicts need a TARGET"]
    run = board["running"] or {}
    lease = board["lease"]
    lease_txt = ("not needed (no hub)" if not lease["required"] else
                 f"held by you ({lease['holder']})" if lease["mine"] else lease["reason"])
    last = board.get("installed") or {}
    return [f"board      {board['board_id']}: runs {board['running_release'] or 'unrecorded'} "
            f"(shell {run.get('shell_id') or '?'}, fw {run.get('firmware_sha') or '?'}, "
            f"{run.get('impl') or 'impl ?'})",
            f"installed  {last.get('version') or '-'}"
            + (f" ({last.get('result')})" if last else "")
            + f"   pinned {board['pinned'] or '-'}   lease {lease_txt}"]


def _plan_lines(s: dict[str, Any]) -> list[str]:
    return cmd_update._plan_lines(s)


# --- the verbs ------------------------------------------------------------------------------


def cmd_harness(ctx: Ctx) -> int:
    return {"list": _list, "show": _show, "fetch": _fetch, "install": _install, "pin": _pin,
            "unpin": _unpin, "history": _history, "rollback": _rollback,
            "mirror": _mirror}[ctx.args.harness_cmd](ctx)


def _list(ctx: Ctx) -> int:
    a = ctx.args
    cat = catalog(ctx)
    if a.target is None:
        listing = cat.list(None, channels=_channels(a), source=a.source, pack=ctx.pack)
    else:
        with ctx.board(note="harness list") as (_cand, session):
            listing = cat.list(session, channels=_channels(a), source=a.source)
    data = listing.as_dict()
    human = [f"catalogue  {listing.catalog}: " + "; ".join(
        f"{c['channel']} serial {c['serial']} (signed by {c['signed_by']}, {c['key_role']})"
        for c in listing.channels)]
    human += _board_lines(listing.board)
    human.append(f"offered    {listing.offer or '-'}")
    human.append(f"{'VERSION':<9} {'CHANNELS':<12} {'STATIC':<11} {'IMPL':<10} {'VERDICT':<23} "
                 f"{'MARKS':<22} WHY")
    human += [_row_line(r) for r in listing.releases]
    human += [f"rollback   {c['version']} ({c['source']}: {c['why']})"
              + ("" if c["installable"] else f": {c['reason']}") for c in listing.rollback]
    human += [f"warning    {w}" for w in listing.warnings]
    rows = [[r["version"], r["channels"], r["status"], r["static_id"], r["impl"], r["fw_sha"],
             r["verdict"], r["marks"], r["mode"], r["size"], r["cached"], r["why"]]
            for r in listing.releases]
    ctx.emit(Result("harness list", data, rows=rows, human=human))
    return ExitCode.OK


def _show(ctx: Ctx) -> int:
    a = ctx.args
    cat = catalog(ctx)
    chans = [a.channel] if a.channel else None
    if a.target is None:
        out = cat.show(a.version, None, channels=chans, source=a.source, pack=ctx.pack)
    else:
        with ctx.board(note="harness show") as (_cand, session):
            out = cat.show(a.version, session, channels=chans, source=a.source)
    ident = out["identity"]
    human = [f"harness    {out['version']} ({out['status']}) on {out['channel']}"
             + (f", released {out['released_at']}" if out["released_at"] else ""),
             f"identity   static {ident['static_id']}  usercode {ident['usercode'] or '?'}  "
             f"fw {ident['fw_sha'] or '?'}  {ident['impl'] or 'impl ?'}  "
             f"proto {ident['proto'] or '?'}  vivado {out['vivado'] or '?'}"]
    if out["verdict"]:
        human.append(f"verdict    {out['verdict_text']}: {out['why']}")
    if out["marks"]:
        human.append(f"marks      {_join(out['marks'])}")
    human += [f"part       {c['name']:<16} {c['target']:<10} {c['kind']:<9} {c['size']:>10} B"
              + ("  cached" if c["cached"] else "")
              + (f"  ({c['skipped']})" if c["skipped"] else "") for c in out["component_list"]]
    human += [f"changes    {line}" for line in out["changes"]["summary"]]
    for line in (out["notes"] or "").splitlines():
        human.append(f"notes      {line}")
    if out["notes_url"]:
        human.append(f"notes url  {out['notes_url']} (a link; HM never fetches it)")
    if out["plan"]:
        human += _plan_lines(out["plan"])
    rows: list[list[Any]] = [[out["version"], "identity", k, v] for k, v in ident.items()]
    rows += [[out["version"], "component", c["name"],
              f"{c['target']} {c['kind']} {c['size']} {c['sha256']}"]
             for c in out["component_list"]]
    rows += [[out["version"], "verdict", out["verdict"] or "-", out["why"]]]
    rows += [[out["version"], "changes", str(i), line]
             for i, line in enumerate(out["changes"]["summary"], 1)]
    ctx.emit(Result("harness show", out, rows=rows, human=human))
    return ExitCode.OK


def _fetch(ctx: Ctx) -> int:
    a = ctx.args
    cat = catalog(ctx)
    out = cat.fetch(a.version, channel=a.channel, source=a.source, pack=ctx.pack, kit=a.kit)
    human = [f"{c['result']:<9} {c['name']:<16} {c['size']:>10} B"
             + (f"  ({c['why']})" if c["why"] else "") for c in out["components"]]
    human.append(f"harness {out['version']}: {out['fetched']} fetched, {out['cached']} already "
                 f"cached, {len(out['skipped'])} skipped")
    rows = [[out["version"], c["name"], c["size"], c["sha256"], c["result"], c["path"]]
            for c in out["components"]]
    ctx.emit(Result("harness fetch", out, rows=rows, human=human))
    return ExitCode.OK


def _approve(ctx: Ctx, plan: Any, what: str) -> Any:
    """The user's consent: the typed phrase for a re-key (never implied by --yes), and for
    an install through the hub the phrase naming the board, its lease holder and queue."""
    a = ctx.args
    door: dict[str, Any] = {}
    if getattr(plan, "board_phrase", ""):
        ctx.note((plan.hub or {}).get("consent_text") or plan.board_phrase)
        bp = getattr(a, "board_phrase", "") or ("" if a.yes else _ask(ctx, plan.board_phrase))
        door = {"board_phrase": bp,
                "auto_revert": False if getattr(a, "no_auto_revert", False) else None}
    if plan.rekey:
        consent = a.consent or ("" if a.yes else cmd_update._ask_phrase(ctx, plan.consent_phrase))
        return plan.approve(consent=consent, **door)
    if not door:
        ctx.confirm(what)
    return plan.approve(**door)


def _ask(ctx: Ctx, phrase: str) -> str:
    """The typed phrase of a remote install (HUB-SD): the user types it, exactly."""
    import sys

    stream = ctx.err or sys.stderr
    stream.write(f"This goes through the hub. To go ahead, type exactly: {phrase}\n> ")
    stream.flush()
    try:
        return sys.stdin.readline().strip()
    except (OSError, ValueError):
        return ""


def _run_plan(ctx: Ctx, cat: Any, cand: Any, session: Any, plan: Any, verified: Any,
              verb: str) -> Any:
    """Refuse on blockers, settle a no-op, check the lease, ask, install."""
    for line in _plan_lines(plan.summary()):
        ctx.note(line)
    if plan.blockers:
        raise with_data(RefusedError(f"cannot install on {cand.board_id}: "
                                     f"{'; '.join(plan.blockers)}",
                                     hint="fix the blockers, then run it again"),
                        plan=plan.summary())
    if plan.up_to_date:
        # Nothing to install, but let the installer settle a journal a crashed run left.
        cat.update.install_harness(session, plan, plan.approve(), verified)
        raise with_data(AlreadyError(f"{cand.board_id} already runs harness {plan.version}",
                                     hint="nothing to do"), plan=plan.summary())
    cat.check_lease(session, plan)                   # before the question (HeldError: exit 4)
    what = ("store the overlays of" if plan.mode == "overlays" else verb)
    approval = _approve(ctx, plan, f"{what} harness {plan.version} on {cand.board_id}?")
    unsubscribe = cmd_update._watch(ctx, cand.board_id)
    try:
        return cat.install(session, plan, approval, verified)
    finally:
        unsubscribe()


def _outcome(ctx: Ctx, out: Any, layout: str, row: list[Any]) -> int:
    data = out.as_dict()
    if out.result in ("dark", "auto-reverted", "auto-revert-failed"):     # HUB-SD (U10)
        raise with_data(ActionFailedError(out.detail, hint=out.restore_hint.replace(
            "TARGET", ctx.args.target) or "check the board"), outcome=data)
    if out.result == "written-not-running":
        raise with_data(ActionFailedError(out.detail, hint=out.restore_hint.replace(
            "TARGET", ctx.args.target) or "check the board"), outcome=data)
    backup = (out.backup or {}).get("path", "")
    human = [f"{out.result:<10} {out.detail}"]
    if backup:
        human.append(f"backup     {backup}")
    human += [f"stored     {s}" for s in out.stored]
    human += [f"skipped    {k}: {v}" for k, v in out.skipped.items()]
    ctx.emit(Result(layout, data, rows=[row], human=human))
    return ExitCode.OK


def _install(ctx: Ctx) -> int:
    a = ctx.args
    cat = catalog(ctx)
    with ctx.board(note="harness install") as (cand, session):
        verified = cat.locate(a.version, channel=a.channel, source=a.source,
                              pack=cand.pack) if a.version else None
        plan, verified = cat.plan(session, a.version, channel=a.channel, source=a.source,
                                  overlays_only=a.overlays_only, verified=verified,
                                  via=getattr(a, "door", None))
        out = _run_plan(ctx, cat, cand, session, plan, verified, "install")
    backup = (out.backup or {}).get("path", "")
    return _outcome(ctx, out, "harness install",
                    [out.board_id, out.version, out.result, plan.running_release, backup,
                     out.detail])


def _pin(ctx: Ctx) -> int:
    a = ctx.args
    cat = catalog(ctx)
    cand = ctx.candidate()
    verified = None
    if not a.no_check:
        verified, _ = cat.channels([a.channel] if a.channel else CHANNELS, source=a.source,
                                   pack=cand.pack)
    out = cat.pin(cand.board_id, a.version, pack=cand.pack, verified=verified)
    ctx.emit(Result("harness pin", out, rows=[[out["board_id"], out["pinned"], out["previous"]]],
                    human=[f"pinned     {cand.board_id} to harness {out['pinned']}"
                           + (f" (was {out['previous']})" if out["previous"] else "")
                           + ": no newer release is offered to it"]))
    return ExitCode.OK


def _unpin(ctx: Ctx) -> int:
    cat = catalog(ctx)
    cand = ctx.candidate()
    out = cat.unpin(cand.board_id)
    human = [f"unpinned   {cand.board_id} (was {out['previous']})" if out["previous"]
             else f"unpinned   {cand.board_id} had no pin"]
    ctx.emit(Result("harness unpin", out, rows=[[out["board_id"], out["pinned"],
                                                 out["previous"]]], human=human))
    return ExitCode.OK


def _when(t: Any) -> str:
    try:
        return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(float(t)))
    except (TypeError, ValueError):
        return "?"


def _history(ctx: Ctx) -> int:
    a = ctx.args
    if a.limit is not None and a.limit < 1:
        raise UsageError("--limit must be at least 1")
    cat = catalog(ctx)
    cand = ctx.candidate()
    hist = cat.history(cand.board_id, a.limit)
    pin = cat.update.pins().get(cand.board_id)
    human = [f"{_when(h.get('recorded_at'))}  {h.get('kind') or 'install':<9} "
             f"{h.get('version') or '?':<8} {h.get('result', ''):<22} "
             f"from {h.get('from_version') or '?':<8} {h.get('static_id') or ''}"
             for h in hist] or [f"no installs are recorded for {cand.board_id}"]
    if pin:
        human.append(f"pinned     {pin['version']}")
    rows = [[cand.board_id, h.get("recorded_at", ""), h.get("kind") or "install",
             h.get("version", ""), h.get("result", ""), h.get("from_version", ""),
             h.get("static_id", ""), h.get("fw_sha", ""), h.get("doors") or [],
             (h.get("backup") or {}).get("path", "")] for h in hist]
    ctx.emit(Result("harness history", {"board_id": cand.board_id, "history": hist,
                                        "pinned": pin["version"] if pin else ""},
                    rows=rows, human=human))
    return ExitCode.OK


def _rollback(ctx: Ctx) -> int:
    a = ctx.args
    cat = catalog(ctx)
    if a.backup is not None:
        if a.to != "previous":
            raise UsageError("give either --to or --backup, not both")
        with ctx.board(note="harness rollback") as (cand, session):
            ctx.confirm(f"restore the config SD of {cand.board_id} from {a.backup} and reboot it?")
            unsubscribe = cmd_update._watch(ctx, cand.board_id)
            try:
                out = cat.update.rollback_harness(session, backup_path=Path(a.backup),
                                                  wait_s=a.wait, via=getattr(a, "door", None))
            finally:
                unsubscribe()
        if out.result != "restored":
            raise with_data(ActionFailedError(out.detail, hint="power-cycle the board if it is "
                                                               "dark"), outcome=out.as_dict())
        version = str((cat.history(cand.board_id, 1) or [{}])[0].get("version", ""))
        ctx.emit(Result("harness rollback", {**out.as_dict(), "how": "restore",
                                             "restored_version": version},
                        rows=[[cand.board_id, version, out.result, "restore", out.detail]],
                        human=[f"{out.result:<10} {out.detail}"]))
        return ExitCode.OK
    with ctx.board(note="harness rollback") as (cand, session):
        listing = cat.list(session, channels=[a.channel] if a.channel else CHANNELS,
                           source=a.source)
        target = cat.rollback_target(cand.board_id, a.to, listing)
        name = listing.channel_of(target)
        if not name:
            raise AbsentError(f"no channel lists harness {target}",
                              hint="restore an SD backup instead (--backup ZIP)")
        ctx.note(f"rollback   {cand.board_id}: re-install harness {target} (from {name})")
        plan, verified = cat.plan(session, target, verified=listing.verified[name],
                                  via=getattr(a, "door", None))
        out = _run_plan(ctx, cat, cand, session, plan, verified, "roll back to")
    data_row = [out.board_id, out.version, out.result, "re-install", out.detail]
    return _outcome(ctx, out, "harness rollback", data_row)


def _mirror(ctx: Ctx) -> int:
    a = ctx.args
    cat = catalog(ctx)
    versions = [v.strip() for v in a.versions.split(",") if v.strip()] if a.versions else None
    reports = cat.mirror(Path(a.dir).expanduser(), channels=_channels(a), source=a.source,
                         pack=ctx.pack, versions=versions, include_private=a.include_private)
    data = {"root": str(Path(a.dir).expanduser()), "include_private": a.include_private,
            "channels": [r.summary() for r in reports]}
    human = [f"mirrored   {r.channel_dir}: {len(r.blobs)} blob(s)" for r in reports]
    human += [f"skipped    {k}: {v}" for r in reports for k, v in r.skipped.items()]
    rows = [[r.channel_dir.name, r.root, r.channel_dir, len(r.blobs), list(r.skipped)]
            for r in reports]
    ctx.emit(Result("harness mirror", data, rows=rows, human=human))
    return ExitCode.OK
