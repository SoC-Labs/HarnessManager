"""``harness-manager hub``: fpgahub hubs as named settings (lane SET-HUBS).

Verbs::

    harness-manager hub list                           the hubs (yours and the machine's), and
                                                       the boards with an inline hub table
    harness-manager hub add NAME --ssh HOST [--group G] [--jump J] [--holder H]
    harness-manager hub add NAME --url URL [--ca-file F] [--cert-file F --key-file F]
        [--lease-ttl D] [--request-ttl D] [--queue-timeout D] [--token-stdin | --token-ref R]
        [--update]                                     change an existing hub's keys
    harness-manager hub token NAME (--stdin | --ref REF | --clear)
                                                       your own token (a machine hub's too)
    harness-manager hub test NAME [--target T]...      config, reach, auth, group, targets,
                                                       target; never takes a lease
    harness-manager hub targets NAME [--add TARGET [--board KEY] [--name N] [--match IP]]
                                                       what the hub offers; --add writes a
                                                       boards.toml entry that uses the hub
    harness-manager hub adopt BOARD [--as NAME]        "Make this a hub": the board's inline
                                                       hub table becomes [hubs.NAME] + hub.use
    harness-manager hub remove NAME [--force]

A token is never taken from the command line: ``--token-stdin`` / ``token --stdin`` read one
line from stdin (a prompt without echo on a terminal). ``--token-ref`` names where it is
instead: ``file:PATH`` (only you may read it), ``env:VAR`` or ``fpgahub-login``.

The verb edits ``settings.toml`` and ``boards.toml`` in the config directory directly;
``harness-manager config`` (lane SET-API) is the general settings verb, and will write
through the service when one runs. ``"hub"`` is in ``cli/main.py``'s ``NO_ENGINE``: no board
is opened.

Exit codes: 0 ok; 2 a bad value; 3 no such hub, board or target; 7 the test could not reach
or log in; 15 a machine hub's definition (the administrator's).
"""

from __future__ import annotations

import argparse
import getpass
import sys
from typing import Any

from harness_manager.core.errors import ExitCode, UsageError

from .context import Ctx
from .output import TSV_COLUMNS, Result, with_data

HUB_TSV: dict[str, tuple[str, ...]] = {
    "hub": ("NAME", "TRANSPORT", "WHERE", "MACHINE", "TOKEN", "BOARDS"),
    "hub test": ("HUB", "STEP", "OK", "DETAIL", "HINT"),
    "hub targets": ("HUB", "BOARD", "TARGET", "ROLE", "USED_BY"),
    "hub change": ("HUB", "ACTION", "BOARD", "DETAIL"),
}


def _fmt() -> argparse.ArgumentParser:
    fmt = argparse.ArgumentParser(add_help=False)
    g = fmt.add_mutually_exclusive_group()
    g.add_argument("--json", action="store_true", default=argparse.SUPPRESS,
                   help="one JSON object on stdout")
    g.add_argument("--tsv", action="store_true", default=argparse.SUPPRESS,
                   help="tab-separated rows, append-only columns")
    return fmt


def register(subparsers: Any) -> argparse.ArgumentParser:
    """Add ``hub`` and its actions to the top-level subparsers. Returns the parser."""
    for layout, cols in HUB_TSV.items():
        TSV_COLUMNS.setdefault(layout, cols)
    fmt = _fmt()
    vp = subparsers.add_parser(
        "hub", help="fpgahub hubs: list, add, test, targets, adopt, remove",
        description="fpgahub hubs as named settings: a board refers to one with "
                    "hub = { use = NAME, target = ... } in boards.toml. No hub at all is "
                    "the default: boards on your desk or your network need none.",
        parents=[fmt])
    sub = vp.add_subparsers(dest="hub_cmd", required=True, metavar="ACTION")

    def ep(layout: str) -> str:
        return f"--tsv columns: {' '.join(HUB_TSV[layout])}"

    sub.add_parser("list", help="the hubs, and the boards with an inline hub table",
                   parents=[fmt], epilog=ep("hub"))

    ap = sub.add_parser("add", help="add a hub (--ssh HOST or --url URL)", parents=[fmt],
                        epilog=ep("hub"))
    ap.add_argument("name", metavar="NAME", help="the name boards use (hub.use)")
    how = ap.add_mutually_exclusive_group()
    how.add_argument("--ssh", metavar="HOST", default=None,
                     help='an SSH hub: your lab account on HOST ("local" on the hub itself)')
    how.add_argument("--url", metavar="URL", default=None,
                     help="a REST hub: https://HUB:7246 (a token; 7245 is mTLS)")
    ap.add_argument("--host", metavar="HOST", default=None,
                    help="with --url: the SSH host for the data plane's tunnel fallback")
    ap.add_argument("--group", metavar="G", default=None,
                    help='SSH: the fpgahub socket\'s group (default fpga; "" for none)')
    ap.add_argument("--jump", metavar="HOST", default=None, help="SSH: a jump host (ssh -J)")
    ap.add_argument("--holder", metavar="NAME", default=None,
                    help="SSH: the lease holder (default harness-manager-<user>@<host>)")
    for flag in ("--ca-file", "--cert-file", "--key-file"):
        ap.add_argument(flag, metavar="PATH", default=None, help=f"REST: {flag[2:]}")
    ap.add_argument("--insecure", action="store_true", default=None,
                    help="REST: skip TLS hostname verification (unsafe)")
    ap.add_argument("--direct", choices=("auto", "never", "always"), default=None,
                    help="REST: the data plane's route (default auto)")
    ap.add_argument("--timeout", metavar="S", default=None, help="REST: call timeout (30s)")
    for flag, what in (("--lease-ttl", "the lease time asked for (1h)"),
                       ("--request-ttl", "the lease time asked for with a request (2h)"),
                       ("--queue-timeout", "how long an acquire waits in the queue (1h)")):
        ap.add_argument(flag, metavar="D", default=None, help=f"{what}: 3600, 90m, 1h")
    tok = ap.add_mutually_exclusive_group()
    tok.add_argument("--token-stdin", action="store_true",
                     help="REST: read your token from stdin into the secret store")
    tok.add_argument("--token-ref", metavar="REF", default=None,
                     help="REST: where your token is: file:PATH, env:VAR or fpgahub-login")
    ap.add_argument("--update", action="store_true", help="change an existing hub's keys")

    tp = sub.add_parser("token", help="set, point at or clear your token for a hub",
                        parents=[fmt], epilog=ep("hub"))
    tp.add_argument("name", metavar="NAME")
    which = tp.add_mutually_exclusive_group(required=True)
    which.add_argument("--stdin", action="store_true",
                       help="read it from stdin (a prompt without echo on a terminal)")
    which.add_argument("--ref", metavar="REF", default=None,
                       help="file:PATH, env:VAR or fpgahub-login")
    which.add_argument("--clear", action="store_true", help="forget it")

    xp = sub.add_parser("test", help="test the connection: never takes a lease",
                        parents=[fmt], epilog=ep("hub test"))
    xp.add_argument("name", metavar="NAME")
    xp.add_argument("--target", action="append", default=None, metavar="T",
                    help="a target to look for (default: those of the boards using the hub)")

    gp = sub.add_parser("targets", help="what the hub offers; --add writes a board for one",
                        parents=[fmt], epilog=ep("hub targets"))
    gp.add_argument("name", metavar="NAME")
    gp.add_argument("--add", metavar="TARGET", default=None,
                    help="write a boards.toml entry for TARGET that uses this hub")
    gp.add_argument("--board", metavar="KEY", default=None,
                    help="with --add: the board's key in boards.toml (default: the target)")
    gp.add_argument("--name", dest="label", metavar="N", default=None,
                    help="with --add: its display name (default: the hub's description)")
    gp.add_argument("--match", action="append", default=None, metavar="ADDR",
                    help="with --add: its address (default: the hub's board_ip)")

    dp = sub.add_parser("adopt", help='"Make this a hub": an inline hub table becomes a hub',
                        parents=[fmt], epilog=ep("hub change"))
    dp.add_argument("board", metavar="BOARD", help="the board's key in boards.toml")
    dp.add_argument("--as", dest="as_name", metavar="NAME", default=None,
                    help="the hub's name (default: the host's first label)")

    rp = sub.add_parser("remove", help="remove a hub and your stored token for it",
                        parents=[fmt], epilog=ep("hub change"))
    rp.add_argument("name", metavar="NAME")
    rp.add_argument("--force", action="store_true", help="even while boards use it")
    vp.set_defaults(fn=cmd_hub)
    return vp


def cmd_hub(ctx: Ctx) -> int:
    from harness_manager.core.services import EngineConfig
    from harness_manager.engine import Engine
    from harness_manager.settings import hubs

    # An in-process engine for its settings resolver (its config dir, every pack's rows):
    # it opens no board and never talks to a running service ("hub" is in NO_ENGINE).
    r = hubs.load_resolver(engine=ctx.engine or Engine(EngineConfig()))
    return {"list": _list, "add": _add, "token": _token, "test": _test, "targets": _targets,
            "adopt": _adopt, "remove": _remove}[ctx.args.hub_cmd](ctx, r)


# --- list --------------------------------------------------------------------------------------


def _where(h: Any) -> str:
    if h.transport == "rest":
        return h.url
    return h.host + (f" via {h.jump}" if h.jump else "")


def _token_text(h: Any) -> str:
    if h.transport != "rest":
        return "-"
    t = h.token or {}
    if not t.get("set"):
        return "not set" if h.token_ref == "store" else f"from {h.token_ref}"
    return f"in {t.get('where')}" + ("" if t.get("reachable", True) else " (unreachable here)")


def _list(ctx: Ctx, r: Any) -> int:
    from harness_manager.settings import hubs

    rows, human, views = [], [], []
    found = hubs.list_hubs(r)
    for h in found:
        users = hubs.boards_using(r, h.name)
        v = {**h.view(), "boards": users}
        views.append(v)
        rows.append([h.name, h.transport, _where(h), "yes" if h.machine else "no",
                     _token_text(h), ",".join(users)])
        tag = f"  (machine hub: {h.policy})" if h.machine else ""
        human.append(f"{h.name:<14} {h.transport.upper():<5} {_where(h)}{tag}")
        detail = [f"group {h.group or '(none)'}" if h.transport == "ssh" else
                  f"token {_token_text(h)}", f"lease {h.lease_ttl // 60} min",
                  f"boards {', '.join(users) or '-'}"]
        human.append("               " + "; ".join(detail))
        for p in v["problems"]:
            human.append(f"               ! {p}")
    inline = hubs.inline_hub_boards(r)
    if not found:
        human.append("No hub. Harness Manager talks to boards on your desk or your network "
                     "directly. Add a hub when your boards live in a lab: "
                     "`harness-manager hub add NAME --ssh HOST` (or --url URL).")
    for b in inline:
        human.append(f"boards.{b} has an inline hub table: `harness-manager hub adopt {b}` "
                     "makes it a named hub")
    ctx.emit(Result("hub", {"hubs": views, "inline_boards": inline,
                            "settings": str(r.files.settings_path) if r.files else "",
                            "policy": r.policy.path},
                    rows=rows, human=human))
    return ExitCode.OK


# --- add / token / remove ------------------------------------------------------------------------


def _read_token(ctx: Ctx) -> str:
    if sys.stdin.isatty():
        value = getpass.getpass("fpgahub token (not shown): ")
    else:
        value = sys.stdin.readline()
    value = value.strip()
    if not value:
        raise UsageError("no token on stdin", hint="pipe it in: `printf %s TOKEN | ...`")
    return value


def _add(ctx: Ctx, r: Any) -> int:
    from harness_manager.settings import hubs

    a = ctx.args
    values: dict[str, Any] = {}
    if a.ssh is not None:
        values.update(transport="ssh", host=a.ssh)
    elif a.url is not None:
        values.update(transport="rest", url=a.url)
    elif not a.update:
        raise UsageError("a hub needs --ssh HOST or --url URL")
    for key, attr in (("host", "host"), ("group", "group"), ("jump", "jump"),
                      ("holder", "holder"), ("ca_file", "ca_file"), ("cert_file", "cert_file"),
                      ("key_file", "key_file"), ("direct", "direct"),
                      ("timeout_s", "timeout"), ("lease_ttl", "lease_ttl"),
                      ("request_ttl", "request_ttl"), ("queue_timeout", "queue_timeout")):
        value = getattr(a, attr, None)
        if value is not None:
            values[key] = value
    if a.insecure:
        values["insecure"] = "true"
    token = _read_token(ctx) if a.token_stdin else None
    if (token is not None or a.token_ref) and values.get("transport", "") == "ssh":
        raise UsageError("an SSH hub uses your SSH key, not a token")
    hub = hubs.add_hub(a.name, values, r, update=a.update)
    status: dict[str, Any] = {}
    if token is not None:
        status = hubs.set_hub_token(a.name, r, value=token)
    elif a.token_ref:
        status = hubs.set_hub_token(a.name, r, ref=a.token_ref)
    hub = hubs.resolve_hub(a.name, r)
    human = [f"{'changed' if a.update else 'added'} hub {hub.name}: {hub.transport.upper()} "
             f"{_where(hub)} in {r.files.settings_path if r.files else 'settings.toml'}"]
    if hub.transport == "rest":
        human.append(f"token: {_token_text(hub)}" + ("" if (hub.token or {}).get("set") or
                                                     hub.token_ref != "store" else
                                                     f" (`harness-manager hub token {hub.name} "
                                                     "--stdin`)"))
    human.append(f"next: `harness-manager hub test {hub.name}`")
    ctx.emit(Result("hub", {"hub": hub.view(), "token": status},
                    rows=[[hub.name, hub.transport, _where(hub), "no", _token_text(hub), ""]],
                    human=human))
    return ExitCode.OK


def _token(ctx: Ctx, r: Any) -> int:
    from harness_manager.settings import hubs

    a = ctx.args
    if a.stdin:
        status = hubs.set_hub_token(a.name, r, value=_read_token(ctx))
    elif a.ref is not None:
        status = hubs.set_hub_token(a.name, r, ref=a.ref)
    else:
        status = hubs.set_hub_token(a.name, r, clear=True)
    hub = hubs.resolve_hub(a.name, r)
    said = _token_text(hub)
    ctx.emit(Result("hub", {"hub": a.name, "token": status, "token_ref": hub.token_ref},
                    rows=[[hub.name, hub.transport, _where(hub),
                           "yes" if hub.machine else "no", said, ""]],
                    human=[f"hub {a.name}: token {said}"
                           + (f" ({status.get('why')})" if status.get("why") else "")]))
    return ExitCode.OK


def _remove(ctx: Ctx, r: Any) -> int:
    from harness_manager.settings import hubs

    out = hubs.remove_hub(ctx.args.name, r, force=ctx.args.force)
    human = [f"removed hub {out['removed']}"]
    if out["boards"]:
        human.append(f"these boards still name it, and will not open until changed: "
                     f"{', '.join(out['boards'])}")
    ctx.emit(Result("hub change", out, rows=[[out["removed"], "removed", b, ""]
                                             for b in out["boards"] or [""]], human=human))
    return ExitCode.OK


# --- test / targets ------------------------------------------------------------------------------


def _step_line(s: dict[str, Any]) -> str:
    mark = "ok  " if s["ok"] else "FAIL"
    line = f"  {mark} {s['step']:<8} {s['detail']}"
    return line + (f"  [{s['ms']} ms]" if s.get("ms") else "")


def _test(ctx: Ctx, r: Any) -> int:
    from harness_manager.settings import hubtest

    a = ctx.args
    report = hubtest.test_hub(a.name, resolver=r, targets=a.target)
    v = report.view()
    rows = [[a.name, s["step"], "yes" if s["ok"] else "no", s["detail"], s["hint"]]
            for s in v["steps"]]
    if report.ok:
        human = [f"hub {a.name} ({v['transport'].upper()}): every step passed"]
        human += [_step_line(s) for s in v["steps"]]
        if v["targets"]:
            human.append("  offers: " + ", ".join(t["target"] for t in v["targets"]))
        ctx.emit(Result("hub test", v, rows=rows, human=human))
        return ExitCode.OK
    if ctx.fmt == "human":
        # A failure has no result on stdout (the output contract): the steps are remarks on
        # stderr, before the error line; --json carries them in error.data.report.
        ctx.note(f"hub {a.name} ({(v['transport'] or '?').upper()}): stopped at {v['failed']}")
        for s in v["steps"]:
            ctx.note(_step_line(s))
    raise with_data(report.error(), report=v)


def _targets(ctx: Ctx, r: Any) -> int:
    from harness_manager.settings import hubs, hubtest
    from harness_manager.settings.schema import join_key

    a = ctx.args
    report = hubtest.test_hub(a.name, resolver=r, targets=[a.add] if a.add else [])
    if not report.ok:
        raise with_data(report.error(), report=report.view())
    used: dict[str, list[str]] = {}
    for board in hubs.boards_using(r, a.name):
        t = r.layer.values.get(join_key(("boards", board, "hub", "target")), "mps3_01_pl")
        used.setdefault(t, []).append(board)
    if not a.add:
        rows = [[a.name, t["board"], t["target"], t["role"] or "", ",".join(used.get(
            t["target"], []))] for t in report.targets]
        human = [f"hub {a.name} offers {len(report.targets)} targets:"]
        for t in report.targets:
            by = used.get(t["target"])
            human.append(f"  {t['target']:<16} board {t['board']:<12}"
                         + (f" used by {', '.join(by)}" if by else
                            f" `harness-manager hub targets {a.name} --add {t['target']}`"))
        ctx.emit(Result("hub targets", {"hub": a.name, "targets": report.targets,
                                        "used": used}, rows=rows, human=human))
        return ExitCode.OK
    details: dict[str, Any] = {}
    if a.label is None or a.match is None:
        try:
            details = hubtest.target_details(a.name, a.add, resolver=r)
        except UsageError:
            raise
        except Exception as exc:  # noqa: BLE001 - a board can be written without the facts
            ctx.note(f"the hub did not say {a.add}'s address and description ({exc}); "
                     "writing the board without them")
    out = hubs.add_board_for_target(a.name, a.add, r, board_key=a.board, name=a.label,
                                    match=a.match, details=details)
    human = [f"added boards.{out['board']} in {out['path']}: {a.add} on hub {a.name}"
             + (f", {out['name']}" if out["name"] else "")
             + (f", at {', '.join(out['match'])}" if out["match"] else "")]
    human += [f"note: {n}" for n in out["notes"]]
    ctx.emit(Result("hub change", out, rows=[[a.name, "added", out["board"], a.add]],
                    human=human))
    return ExitCode.OK


# --- adopt -----------------------------------------------------------------------------------------


def _adopt(ctx: Ctx, r: Any) -> int:
    from harness_manager.settings import hubs

    a = ctx.args
    out = hubs.adopt_inline_hub(a.board, r, as_name=a.as_name)
    if not out["changed"]:
        human = [f"boards.{a.board} already uses the hub {out['hub']}: nothing to do"]
    else:
        human = [f"boards.{a.board} now uses the hub {out['hub']} "
                 f"({'new' if out['created'] else 'the same definition, reused'})"]
        if out["via"] == "hub":
            human.append(f"boards.{a.board}.via is now \"hub\"")
        human.append(f"the old boards.toml is kept as {out['backup']}")
    human += [f"note: {n}" for n in out["notes"]]
    ctx.emit(Result("hub change", out,
                    rows=[[out["hub"], "adopted" if out["changed"] else "unchanged", a.board,
                           out["via"]]], human=human))
    return ExitCode.OK
