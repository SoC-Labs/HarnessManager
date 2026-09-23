"""``harness-manager update``: check, install and roll back harness and app updates. Team T7.

Verbs::

    harness-manager update check   [TARGET]           read-only: the channel, and the plan for a board
    harness-manager update harness TARGET             install the channel's harness (asks first)
    harness-manager update app                        download, stage and switch to the new app
    harness-manager update rollback TARGET            restore the config SD backup, reboot, confirm
    harness-manager update rollback --app             switch back to the previous app version

Common options: ``--channel stable|beta|dev`` (default ``$HARNESS_MANAGER_UPDATE_CHANNEL`` or
stable) and ``--source URL|DIR`` (a mirror; default ``$HARNESS_MANAGER_UPDATE_SOURCE`` or the
GitHub dist repo). Private (Arm-IP) components need ``$HARNESS_MANAGER_GITHUB_TOKEN``.

Consent: ``update harness`` and ``update app`` ask ``[y/N]`` unless ``--yes``. A
re-key ALSO needs the typed phrase the plan prints (``--consent "REKEY 0x…"``, or
typed at the prompt): ``--yes`` alone never re-keys a board.

Exit codes: 0 done; 8 nothing to do; 6 written but the board does not run it
(the restore command is in the message); 14 a bundle that does not match its
identity; 15 a safety rail (signature, rollback serial, no consent, …); 4 busy.

The lead wires this module into ``cli/main.py`` with ``cmd_update.register(sub)``.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

from harness_manager.core.errors import (
    ActionFailedError,
    AlreadyError,
    ExitCode,
    RefusedError,
    UsageError,
)
from harness_manager.core.events import Event

from .context import Ctx
from .output import TSV_COLUMNS, Result, StderrProgress, with_data

#: TSV layouts for the update verbs (append-only). Registered into the shared table at
#: ``register()`` time until the lead folds them into ``output.TSV_COLUMNS`` (CCR T7-4).
UPDATE_TSV: dict[str, tuple[str, ...]] = {
    "update check": ("CHANNEL", "SERIAL", "HARNESS_CURRENT", "APP_CURRENT", "APP_UPDATE",
                     "BOARD_ID", "RUNNING", "MODE", "REKEY", "BLOCKERS"),
    "update harness": ("BOARD_ID", "VERSION", "RESULT", "BACKUP", "DETAIL"),
    "update app": ("VERSION", "STAGED", "SWITCHED", "CURRENT", "PREVIOUS"),
    "update rollback": ("TARGET", "RESULT", "VERSION", "DETAIL"),
}

TARGET_HELP = "shell address host[:port], or - for a USB-only board (with --serial/--volume)"


def _parents() -> list[argparse.ArgumentParser]:
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
    src.add_argument("--channel", default=None, metavar="NAME",
                     help="stable (default), beta or dev")
    src.add_argument("--source", default=None, metavar="URL|DIR",
                     help="where channel.json is: a URL (may contain {channel}) or a mirror dir")
    return [fmt, usb, src]


def register(subparsers: Any) -> argparse.ArgumentParser:
    """Add ``update`` and its actions to the top-level subparsers. Returns the parser."""
    for layout, cols in UPDATE_TSV.items():
        TSV_COLUMNS.setdefault(layout, cols)
    fmt, usb, src = _parents()
    vp = subparsers.add_parser("update", help="check, install and roll back updates",
                               description="Updates from the signed channel: the harness "
                                           "(config SD + overlays) and the app itself.",
                               parents=[fmt])
    sub = vp.add_subparsers(dest="update_cmd", required=True, metavar="ACTION")

    def epilog(layout: str) -> str:
        return f"--tsv columns: {' '.join(UPDATE_TSV[layout])}"

    ap = sub.add_parser("check", help="read-only: what the channel offers (and a board's plan)",
                        parents=[fmt, usb, src], epilog=epilog("update check"))
    ap.add_argument("target", nargs="?", default=None, metavar="TARGET", help=TARGET_HELP)
    ap.add_argument("--version", dest="want_version", default=None, metavar="V",
                    help="plan for this harness version instead of the current one")

    ap = sub.add_parser("harness", help="install the channel's harness on a board",
                        parents=[fmt, usb, src], epilog=epilog("update harness"))
    ap.add_argument("target", metavar="TARGET", help=TARGET_HELP)
    ap.add_argument("--version", dest="want_version", default=None, metavar="V",
                    help="install this version (an older one is a rollback from signed history)")
    ap.add_argument("--overlays-only", action="store_true",
                    help="only store the release's overlays; no SD write, no reboot")
    ap.add_argument("--consent", default="", metavar="PHRASE",
                    help='for a re-key: the exact phrase the plan prints ("REKEY 0x…")')
    ap.add_argument("--yes", action="store_true", help="do not ask for confirmation")

    ap = sub.add_parser("app", help="update this app: download, stage side by side, switch",
                        parents=[fmt, src], epilog=epilog("update app"))
    ap.add_argument("--version", dest="want_version", default=None, metavar="V",
                    help="stage this version instead of the channel's current one")
    ap.add_argument("--stage-only", action="store_true",
                    help="build the new version but do not switch to it")
    ap.add_argument("--yes", action="store_true", help="do not ask for confirmation")

    ap = sub.add_parser("rollback", help="undo: restore a board's SD backup, or the previous app",
                        parents=[fmt, usb], epilog=epilog("update rollback"))
    ap.add_argument("target", nargs="?", default=None, metavar="TARGET", help=TARGET_HELP)
    ap.add_argument("--app", action="store_true", help="switch back to the previous app version")
    ap.add_argument("--backup", default=None, metavar="ZIP",
                    help="the backup to restore (default: the one the last update took)")
    ap.add_argument("--wait", type=float, default=None, metavar="S",
                    help="how long to wait for the board to come back (default: 120 s "
                         "bare-metal, 180 s Linux)")
    ap.add_argument("--yes", action="store_true", help="do not ask for confirmation")

    vp.set_defaults(fn=cmd_update)
    return vp


# --- plumbing ---------------------------------------------------------------------------


def service(ctx: Ctx) -> Any:
    """The engine's update service (``engine.update``, CCR T7-1), else one built on it."""
    svc = getattr(ctx.engine, "update", None)
    if svc is not None and getattr(svc, "reason", None) is None:
        return svc
    from harness_manager.services.update import UpdateService

    return UpdateService(ctx.engine)


class _Progress:
    """``update.progress`` events -> throttled lines on stderr (one bar per phase)."""

    def __init__(self, ctx: Ctx, board_id: str) -> None:
        self.ctx = ctx
        self.board_id = board_id
        self.bars: dict[str, StderrProgress] = {}

    def __call__(self, ev: Event) -> None:
        if ev.board_id and self.board_id and ev.board_id != self.board_id:
            return
        d = ev.data
        if ev.topic == "update.progress":
            phase = str(d.get("phase", ""))
            head = phase.split(":", 1)[0]
            bar = self.bars.setdefault(head, StderrProgress(f"update {head}", self.ctx.err))
            bar(phase.split(":", 1)[-1], int(d.get("bytes", 0) or 0), int(d.get("total", 0) or 0))
        elif ev.topic == "update.started":
            self.ctx.note(f"update: started {d.get('version', '')} ({d.get('mode', '')})")
        elif ev.topic == "update.failed":
            self.ctx.note(f"update: failed: {d.get('reason', '')}")


def _watch(ctx: Ctx, board_id: str):
    bus = getattr(ctx.engine, "bus", None)
    if bus is None:
        return lambda: None
    return bus.subscribe("update.*", _Progress(ctx, board_id))


def _ask_phrase(ctx: Ctx, phrase: str) -> str:
    """Typed consent: the user must type ``phrase`` exactly (never implied by --yes)."""
    stream = ctx.err or sys.stderr
    stream.write(f"This RE-KEYS the board. To go ahead, type exactly: {phrase}\n> ")
    stream.flush()
    try:
        answer = sys.stdin.readline()
    except (OSError, ValueError):
        answer = ""
    return answer.strip()


def _plan_lines(s: dict[str, Any]) -> list[str]:
    """Human lines for a plan summary (``Plan.summary()``)."""
    run = s["running"] or {}
    lines = [
        f"board      {s['board_id']}",
        f"running    shell {run.get('shell_id') or '?'}  harness {run.get('harness') or '?'}"
        + (f"  (channel release {s['running_release']})" if s["running_release"] else
           "  (not a release this channel lists)"),
        f"offered    harness {s['version'] or '-'} on {s['channel']} (serial {s['serial']})",
        f"mode       {s['mode']}" + ("  RE-KEY" if s["rekey"] else ""),
    ]
    lines += [f"step       {st['action']}: {st['detail']}" for st in s["steps"]]
    lines += [f"unusable   {u}" for u in s["unusable"]]
    lines += [f"warning    {w}" for w in s["warnings"]]
    lines += [f"blocked    {b}" for b in s["blockers"]]
    if s["rekey"]:
        lines.append(f"consent    type: {s['consent_phrase']}")
    return lines


# --- the verbs --------------------------------------------------------------------------


def cmd_update(ctx: Ctx) -> int:
    action = ctx.args.update_cmd
    return {"check": _check, "harness": _harness, "app": _app, "rollback": _rollback}[action](ctx)


def _check(ctx: Ctx) -> int:
    a = ctx.args
    svc = service(ctx)
    if a.target is None:
        report = svc.check(channel=a.channel, source=a.source)
    else:
        with ctx.board(note="update check") as (_cand, session):
            report = svc.check(channel=a.channel, source=a.source, session=session)
            if a.want_version:
                plan, _ = svc.plan_harness(session, channel=a.channel, source=a.source,
                                           version=a.want_version)
                report["plan"] = plan.summary()
    plan = report.get("plan") or {}
    run = plan.get("running") or {}
    row = [report["channel"], report["serial"], report["harness_current"], report["app_current"],
           report["app_update"], plan.get("board_id", ""), run.get("harness", ""),
           plan.get("mode", ""), plan.get("rekey", ""), plan.get("blockers", [])]
    human = [f"channel    {report['channel']} serial {report['serial']}, signed by "
             f"{report['signed_by']} ({report['key_role']})",
             f"harness    current {report['harness_current'] or '-'}",
             f"app        current {report['app_current'] or '-'}, running {report['app_running']}"
             + (f": UPDATE to {report['app_update']} (`harness-manager update app`)"
                if report["app_update"] else "")]
    human += [f"warning    {w}" for w in report["warnings"]]
    if plan:
        human += _plan_lines(plan)
    ctx.emit(Result("update check", report, rows=[row], human=human))
    return ExitCode.OK


def _harness(ctx: Ctx) -> int:
    a = ctx.args
    svc = service(ctx)
    with ctx.board(note="update harness") as (cand, session):
        plan, verified = svc.plan_harness(session, channel=a.channel, source=a.source,
                                          version=a.want_version, overlays_only=a.overlays_only)
        for line in _plan_lines(plan.summary()):
            ctx.note(line)
        if plan.blockers:
            raise with_data(RefusedError(f"cannot update {cand.board_id}: "
                                         f"{'; '.join(plan.blockers)}",
                                         hint="fix the blockers, then run it again"),
                            plan=plan.summary())
        if plan.up_to_date:
            # Nothing to install, but let the installer settle a journal a crashed run left.
            svc.install_harness(session, plan, plan.approve(), verified)
            raise with_data(AlreadyError(f"{cand.board_id} already runs harness {plan.version}",
                                         hint="nothing to do"), plan=plan.summary())
        if plan.rekey:
            consent = a.consent or ("" if a.yes else _ask_phrase(ctx, plan.consent_phrase))
            approval = plan.approve(consent=consent)
        else:
            what = ("store the overlays of" if plan.mode == "overlays" else "install")
            ctx.confirm(f"{what} harness {plan.version} on {cand.board_id}?")
            approval = plan.approve()
        unsubscribe = _watch(ctx, cand.board_id)
        try:
            out = svc.install_harness(session, plan, approval, verified)
        finally:
            unsubscribe()
    data = out.as_dict()
    backup = (out.backup or {}).get("path", "")
    if out.result == "written-not-running":
        raise with_data(ActionFailedError(out.detail, hint=out.restore_hint.replace(
            "TARGET", a.target) or "check the board"), outcome=data)
    human = [f"{out.result:<10} {out.detail}"]
    if backup:
        human.append(f"backup     {backup}")
    human += [f"stored     {s}" for s in out.stored]
    human += [f"skipped    {k}: {v}" for k, v in out.skipped.items()]
    ctx.emit(Result("update harness", data,
                    rows=[[out.board_id, out.version, out.result, backup, out.detail]],
                    human=human))
    return ExitCode.OK


def _app(ctx: Ctx) -> int:
    a = ctx.args
    svc = service(ctx)
    verified = svc.fetch_channel(a.channel, a.source)
    rel = verified.channel.app_release(a.want_version)
    if rel is None:
        raise RefusedError(f"the {verified.channel.channel!r} channel has no app release "
                           f"{a.want_version or '(no current release)'}")
    ctx.confirm(f"download harness-manager {rel.version} and "
                f"{'stage it' if a.stage_only else 'switch to it'}?")
    out = svc.update_app(verified=verified, version=a.want_version, switch=not a.stage_only)
    pointer = out.get("pointer") or svc.app().state()
    human = [f"staged     harness-manager {out['version']}",
             f"switched   {'yes: it runs from the next start' if out['switched'] else 'no'}"]
    if out.get("warning"):
        human.append(f"warning    {out['warning']}")
    ctx.emit(Result("update app", out,
                    rows=[[out["version"], True, out["switched"], pointer.get("current", ""),
                           pointer.get("previous", "")]], human=human))
    return ExitCode.OK


def _rollback(ctx: Ctx) -> int:
    a = ctx.args
    svc = service(ctx)
    if a.app:
        if a.target is not None:
            raise UsageError("give either TARGET or --app, not both")
        st = svc.app().state()
        ctx.confirm(f"switch back from harness-manager {st['current'] or '?'} to {st['previous'] or '?'}?")
        st = svc.app().rollback()
        ctx.emit(Result("update rollback", {"target": "app", "result": "switched", "state": st},
                        rows=[["app", "switched", st["current"], f"previous {st['previous']}"]],
                        human=[f"switched   harness-manager {st['current']} is current again "
                               "(from the next start)"]))
        return ExitCode.OK
    if a.target is None:
        raise UsageError("update rollback needs a TARGET (a board) or --app")
    with ctx.board(note="update rollback") as (cand, session):
        ctx.confirm(f"restore the config SD of {cand.board_id} from "
                    f"{a.backup or 'the backup the last update took'} and reboot it?")
        unsubscribe = _watch(ctx, cand.board_id)
        try:
            out = svc.rollback_harness(session, backup_path=Path(a.backup) if a.backup else None,
                                       wait_s=a.wait)
        finally:
            unsubscribe()
    if out.result != "restored":
        raise with_data(ActionFailedError(out.detail, hint=f"check `harness-manager info {a.target}`; "
                                                           "power-cycle the board if it is dark"),
                        outcome=out.as_dict())
    ctx.emit(Result("update rollback", out.as_dict(),
                    rows=[[cand.board_id, out.result, out.identity_after.get("harness", ""),
                           out.detail]],
                    human=[f"{out.result:<10} {out.detail}"]))
    return ExitCode.OK
