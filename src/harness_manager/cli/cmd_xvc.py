"""``harness-manager xvc``: fabric debug over the harness's own XVC server (lane XVC-CORE, X3).

Verbs::

    harness-manager xvc open   TARGET [--byo] [--for S]   take the board's XVC slot behind a relay,
                                                          start HM's hw_server (unless --byo)
    harness-manager xvc close  TARGET                     kick, stop hw_server, free the slot
    harness-manager xvc status TARGET                     state, the Vivado URL, who is attached
    harness-manager xvc tcl    TARGET [--byo]             the Vivado Tcl snippet for what is loaded
    harness-manager xvc ltx    TARGET [--rm|--static|--full] [-o FILE]   the probes file

**Scope.** XVC here is the reconfigurable partition's debug chain (the harness's Debug
Bridge and the loaded design's ILAs), never whole-device JTAG: every status says so.

**Who may open it.** On a board behind a hub, the lease holder only (409 HELD /
exit 4 names the holder). The bare-metal harness's XVC is unauthenticated: the status
carries that warning until the Linux cutover.

**Where it lives.** Through harness-manager-daemon (the web UI's session), ``open``
returns at once when the daemon already had the board open; the session stays there
until ``xvc close`` (or a closed board). On the in-process engine, or when this
command opened the board itself, ``open`` holds the session in the foreground until
Ctrl-C, ``--for`` or ``harness-manager detach``, as ``debug up`` does. A partition swap
closes the session and reopens it on the new design (a fresh hw_server): re-run the
probes lines of ``xvc tcl``.

Exit codes: 0 done; 3 no probes file; 4 not the lease holder, or the board's XVC slot
is held by another client; 5 a local port is taken; 7 the board did not answer; 8
already open; 12 no XVC on this board or no hw_server (use --byo).

``cli/main.py`` registers it with ``cmd_xvc.register(sub)``.
"""

from __future__ import annotations

import argparse
import shutil
from pathlib import Path
from typing import Any

from harness_manager.core.capabilities import DEBUG_FABRIC
from harness_manager.core.errors import ExitCode, UnavailableError

from .context import Ctx, hold, hold_note
from .output import TSV_COLUMNS, Result

#: TSV layouts (append-only). ``output.TSV_COLUMNS`` carries them; kept here too so the
#: verb registers them if the shared table predates them.
XVC_TSV: dict[str, tuple[str, ...]] = {
    "xvc": ("BOARD_ID", "STATE", "MODE", "URL", "RELAY_PORT", "HW_SERVER_PORT",
            "HW_SERVER_PID", "BOARD_SLOT", "REACH", "ATTACHED", "LTX", "WARNINGS", "DETAIL"),
    "xvc tcl": ("BOARD_ID", "LINE", "TEXT"),
    "xvc ltx": ("BOARD_ID", "WHICH", "NAME", "PATH", "CRC_OK", "WRITTEN"),
}
TARGET_HELP = "shell address host[:port] (the board's harness)"
SCOPE_HELP = ("XVC here reaches the reconfigurable partition's debug chain only (the harness's "
              "Debug Bridge and the loaded design's ILAs), never whole-device JTAG")


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
    p.add_argument("--via", metavar="ssh:HOST", default=argparse.SUPPRESS,
                   help="reach the shell through an SSH tunnel on HOST (the lab hub)")
    return p


def register(subparsers: Any) -> argparse.ArgumentParser:
    """Add ``xvc`` and its actions to the top-level subparsers. Returns the parser."""
    for layout, cols in XVC_TSV.items():
        TSV_COLUMNS.setdefault(layout, cols)
    fmt, board = _fmt(), _board()
    vp = subparsers.add_parser(
        "xvc", help="debug the partition's ILAs in Vivado over the harness's XVC "
                    "(partition-scoped, never whole-device JTAG)",
        description="Fabric debug over XVC. " + SCOPE_HELP + ". Harness Manager holds the "
                    "board's one XVC slot behind a local relay, runs its own hw_server, and "
                    "reopens the session after every partition swap.",
        parents=[fmt])
    sub = vp.add_subparsers(dest="xvc_cmd", required=True, metavar="ACTION")

    def epilog(layout: str) -> str:
        return f"{SCOPE_HELP}.  --tsv columns: {' '.join(XVC_TSV[layout])}"

    ap = sub.add_parser("open", help="take the board's XVC slot and start HM's hw_server",
                        parents=[fmt, board], epilog=epilog("xvc"))
    ap.add_argument("--byo", action="store_true",
                    help="bring your own hw_server: HM runs only the relay (Vivado: "
                         "open_hw_target -xvc_url 127.0.0.1:R)")
    ap.add_argument("--for", dest="for_s", type=float, default=None, metavar="SECONDS",
                    help="hold the session this long when this command owns it "
                         "(default: until Ctrl-C)")
    sub.add_parser("close", help="kick the client, stop hw_server, free the board's slot",
                   parents=[fmt, board], epilog=epilog("xvc"))
    sub.add_parser("status", help="state, the Vivado URL, who is attached, the probes files",
                   parents=[fmt, board], epilog=epilog("xvc"))
    ap = sub.add_parser("tcl", help="the Vivado Tcl snippet for the loaded design",
                        parents=[fmt, board], epilog=epilog("xvc tcl"))
    ap.add_argument("--byo", action="store_true", default=None,
                    help="the snippet for your own hw_server (open_hw_target -xvc_url)")
    ap = sub.add_parser("ltx", help="the probes file (.ltx) for the loaded design",
                        parents=[fmt, board], epilog=epilog("xvc ltx"))
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--rm", dest="which", action="store_const", const="rm",
                   help="the RM's own probes file (its ILAs)")
    g.add_argument("--static", dest="which", action="store_const", const="static",
                   help="the static's probes file (the MIG calibration view; Linux)")
    g.add_argument("--full", dest="which", action="store_const", const="full",
                   help="the full-design probes file the mint staged (static + RM)")
    ap.add_argument("-o", "--out", default=None, metavar="FILE",
                    help="write the probes file here (default: print where it is)")
    vp.set_defaults(fn=cmd_xvc)
    return vp


def _service(ctx: Ctx) -> Any:
    svc = getattr(ctx.engine, "xvc", None)
    if svc is None:
        raise UnavailableError(DEBUG_FABRIC, "this engine has no XVC service (update "
                                             "harness-manager-daemon, or set "
                                             "HARNESS_MANAGER_NO_DAEMON=1)")
    return svc


def _get(st: Any, key: str, default: Any = None) -> Any:
    return st.get(key, default) if isinstance(st, dict) else getattr(st, key, default)


def _status_json(board_id: str, st: Any) -> dict[str, Any]:
    data = st.to_json() if hasattr(st, "to_json") else dict(st)
    return {"board_id": board_id, **data}


def _ltx_name(st: Any) -> str:
    ltx = _get(st, "ltx") or {}
    key = ltx.get("preferred")
    item = ltx.get(key) if key else None
    return f"{item.get('name')} ({key})" if item else ""


def _attached(st: Any) -> str:
    att = _get(st, "attached")
    if not att:
        return ""
    who = f"pid {att.get('pid')}" if att.get("pid") else att.get("peer", "")
    cmd = (att.get("command") or "").split(" ")[0]
    return f"{who} {Path(cmd).name}".strip() if cmd else who


def _status_row(board_id: str, st: Any) -> list[Any]:
    return [board_id, _get(st, "state"), _get(st, "mode"), _get(st, "url"),
            _get(st, "relay_port") or "", _get(st, "hw_server_port") or "",
            _get(st, "hw_server_pid") or "", _get(st, "board_slot"), _get(st, "reach"),
            _attached(st), _ltx_name(st), " | ".join(_get(st, "warnings") or ()),
            _get(st, "detail") or _get(st, "reason") or ""]


def _status_human(board_id: str, st: Any, target: str) -> list[str]:
    state, mode = _get(st, "state"), _get(st, "mode")
    lines = [f"xvc        {board_id}: {state}{f' ({mode})' if mode else ''}"]
    if _get(st, "open"):
        if mode == "byo":
            lines.append(f"vivado     open_hw_target -xvc_url {_get(st, 'url')}   "
                         f"(`harness-manager xvc tcl {target} --byo`)")
        else:
            lines.append(f"vivado     connect_hw_server -url {_get(st, 'url')}   "
                         f"(`harness-manager xvc tcl {target}`)")
        lines.append(f"relay      127.0.0.1:{_get(st, 'relay_port')}   board slot: "
                     f"{_get(st, 'board_slot')}   reach: {_get(st, 'reach') or '-'}")
        if _get(st, "hw_server_pid"):
            lines.append(f"hw_server  {_get(st, 'hw_server')}  pid {_get(st, 'hw_server_pid')}")
        lines.append(f"attached   {_attached(st) or '-'}")
        lines.append(f"probes     {_ltx_name(st) or 'none for the loaded design'}")
    elif _get(st, "reason"):
        lines.append(f"cannot     {_get(st, 'reason')}")
    if _get(st, "detail"):
        lines.append(f"detail     {_get(st, 'detail')}")
    lines.append(f"scope      {_get(st, 'scope') or SCOPE_HELP}")
    for w in _get(st, "warnings") or ():
        lines.append(f"warning    {w}")
    return lines


def cmd_xvc(ctx: Ctx) -> int:
    return {"open": _open, "close": _close, "status": _status, "tcl": _tcl,
            "ltx": _ltx}[ctx.args.xvc_cmd](ctx)


def _emit_status(ctx: Ctx, board_id: str, st: Any) -> None:
    ctx.emit(Result("xvc", _status_json(board_id, st), rows=[_status_row(board_id, st)],
                    human=_status_human(board_id, st, ctx.args.target)))


def _open(ctx: Ctx) -> int:
    a = ctx.args
    with ctx.board(note=hold_note("xvc open")) as (cand, session):
        svc = _service(ctx)
        st = svc.open(session, byo=bool(a.byo))
        _emit_status(ctx, cand.board_id, st)
        if getattr(session, "owned", None) is False:
            ctx.note(f"the XVC session stays with harness-manager-daemon; "
                     f"`harness-manager xvc close {a.target}` ends it")
            return ExitCode.OK
        # This process (or the board this command opened in the daemon) owns the session:
        # hold it until Ctrl-C, `detach`, or --for.
        if a.for_s is None:
            ctx.note(f"XVC open for {cand.board_id}; Ctrl-C or "
                     f"`harness-manager detach {a.target}` closes it")
        hold(a.for_s)
        svc.close(session, reason="closed by the command that opened it")
    return ExitCode.OK


def _close(ctx: Ctx) -> int:
    with ctx.board(note="xvc close") as (cand, session):
        st = _service(ctx).close(session, reason="closed")
        _emit_status(ctx, cand.board_id, st)
    return ExitCode.OK


def _status(ctx: Ctx) -> int:
    with ctx.board(note="xvc status") as (cand, session):
        # This command holds the board, so it may read its identity; the daemon's proxy
        # ignores refresh (the daemon reads it under the board gate when it must).
        _emit_status(ctx, cand.board_id, _service(ctx).status(session, refresh=True))
    return ExitCode.OK


def _tcl(ctx: Ctx) -> int:
    with ctx.board(note="xvc tcl") as (cand, session):
        out = dict(_service(ctx).tcl(session, byo=ctx.args.byo, refresh=True))
    lines = out["tcl"].rstrip("\n").splitlines()
    human = list(lines)
    if not out.get("open"):
        human.append(f"# (no XVC session is open: `harness-manager xvc open {ctx.args.target}` "
                     "first; the ports are filled in then)")
    ctx.emit(Result("xvc tcl", {"board_id": cand.board_id, **out},
                    rows=[[cand.board_id, i, line] for i, line in enumerate(lines, 1)],
                    human=human))
    return ExitCode.OK


def _ltx(ctx: Ctx) -> int:
    a = ctx.args
    which = a.which or "auto"
    with ctx.board(note="xvc ltx") as (cand, session):
        item = dict(_service(ctx).ltx(session, which, refresh=True))
    src = Path(str(item["path"]))
    written = ""
    if a.out:
        dest = Path(a.out)
        if dest.is_dir():
            dest = dest / src.name
        shutil.copyfile(src, dest)
        written = str(dest.resolve())
    item = {k: (str(v) if isinstance(v, Path) else v) for k, v in item.items()}
    crc = item.get("crc_ok")
    human = [f"probes     {item.get('name')} ({item.get('which')}"
             f"{', crc ok' if crc else ', CRC MISMATCH' if crc is False else ''})",
             f"path       {item.get('path')}"]
    if written:
        human.append(f"written    {written}")
    ctx.emit(Result("xvc ltx", {"board_id": cand.board_id, **item, "written": written or None},
                    rows=[[cand.board_id, item.get("which"), item.get("name"), item.get("path"),
                           crc, written]], human=human))
    return ExitCode.OK
