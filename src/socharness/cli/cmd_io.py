"""Consoles (``engine.consoles``) and debug sessions (``engine.debug``)."""

from __future__ import annotations

import sys

from socharness.core.errors import AbsentError, ActionFailedError, ExitCode, UsageError
from socharness.core.services import DebugStatus

from .context import Ctx, Stopper, hold, hold_note
from .output import Result, tsv_line, with_data

EXPORT_HOST = "127.0.0.1"
READ_SLICE_S = 0.2


# --- console ------------------------------------------------------------------------------


def _write_raw(chunk: bytes) -> None:
    buf = getattr(sys.stdout, "buffer", None)
    if buf is not None:
        sys.stdout.flush()
        buf.write(chunk)
        buf.flush()
    else:
        sys.stdout.write(chunk.decode("utf-8", "replace"))
        sys.stdout.flush()


def cmd_console(ctx: Ctx) -> int:
    a = ctx.args
    if ctx.fmt == "json" and a.export is None and a.for_s is None:
        raise UsageError("--json collects the console into one object, so it needs --for SECONDS",
                         hint="e.g. socharness --json console TARGET uart0 --for 5")
    what = f"console {a.name}" + (" --export" if a.export is not None else "")
    with ctx.board(note=hold_note(what)) as (cand, session):
        broker = ctx.engine.consoles
        names = list(broker.names(session))
        if a.name not in names:
            raise AbsentError(f"{cand.board_id} has no console named {a.name!r}",
                              hint=f"consoles: {', '.join(names) or 'none'}")
        if a.export is not None:
            return _export(ctx, cand.board_id, session)
        stream = broker.subscribe(session, a.name)
        try:
            text = _pump(ctx, stream)
        finally:
            stream.close()
    if ctx.fmt == "json":
        ctx.emit(Result("console", {"board_id": cand.board_id, "name": a.name, "text": text,
                                    "bytes": len(text.encode())}))
    return ExitCode.OK


def _pump(ctx: Ctx, stream) -> str:
    """Stream the console until Ctrl-C/SIGTERM or ``--for`` elapses. Returns what was read."""
    name = ctx.args.name
    collected = bytearray()
    pending = b""
    with Stopper(ctx.args.for_s) as stopper:
        while not stopper.done:
            left = stopper.remaining()
            chunk = stream.read(timeout=READ_SLICE_S if left is None else min(READ_SLICE_S, left))
            if not chunk:
                continue
            collected += chunk
            if ctx.fmt == "human":
                _write_raw(chunk)
            elif ctx.fmt == "tsv":
                pending += chunk
                *lines, pending = pending.split(b"\n")
                for line in lines:
                    text = line.decode("utf-8", "replace").rstrip("\r")
                    sys.stdout.write(tsv_line("console", [name, text]) + "\n")
                sys.stdout.flush()
    if ctx.fmt == "tsv" and pending:
        sys.stdout.write(tsv_line("console", [name, pending.decode("utf-8", "replace")]) + "\n")
        sys.stdout.flush()
    return collected.decode("utf-8", "replace")


def _export(ctx: Ctx, board_id: str, session) -> int:
    a = ctx.args
    broker = ctx.engine.consoles
    port = broker.export_tcp(session, a.name, a.export)
    try:
        ctx.emit(Result("console --export", {
            "board_id": board_id, "name": a.name, "host": EXPORT_HOST, "port": port,
        }, rows=[[board_id, a.name, EXPORT_HOST, port]], human=[str(port)]))
        if a.for_s is None:
            ctx.note(f"console {a.name} of {board_id} is on {EXPORT_HOST}:{port}; "
                     "Ctrl-C stops the export")
        hold(a.for_s)
    finally:
        broker.close_all(board_id)
    return ExitCode.OK


# --- debug --------------------------------------------------------------------------------


def _status_row(board_id: str, st: DebugStatus) -> list:
    return [board_id, st.state, st.gdb_port or "", st.telnet_port or "", st.tcl_port or "",
            st.pid or "", list(st.config), st.detail]


def _status_human(board_id: str, st: DebugStatus) -> list[str]:
    lines = [f"debug      {board_id}: {st.state}"]
    if st.gdb_port:
        lines.append(f"gdb        {EXPORT_HOST}:{st.gdb_port}")
    if st.telnet_port:
        lines.append(f"telnet     {EXPORT_HOST}:{st.telnet_port}")
    if st.tcl_port:
        lines.append(f"tcl        {EXPORT_HOST}:{st.tcl_port}")
    if st.config:
        lines.append(f"config     {' '.join(st.config)}")
    if st.pid:
        lines.append(f"pid        {st.pid}")
    if st.detail:
        lines.append(f"detail     {st.detail}")
    return lines


def cmd_debug(ctx: Ctx) -> int:
    a = ctx.args
    action = a.debug_cmd
    note = hold_note("debug up") if action == "up" else ""
    with ctx.board(note=note) as (cand, session):
        svc = ctx.engine.debug
        if action == "detect":
            idcode = svc.detect(session)
            ctx.emit(Result("debug detect", {"board_id": cand.board_id, "idcode": idcode},
                            rows=[[cand.board_id, idcode]],
                            human=[f"idcode     {idcode}"]))
            return ExitCode.OK
        st = {"up": svc.up, "down": svc.down, "status": svc.status}[action](session)
        if action == "up" and st.state == "failed":
            raise with_data(ActionFailedError(
                f"the debug server for {cand.board_id} failed to start: "
                f"{st.detail or 'no detail given'}",
                hint="`socharness debug status TARGET` shows the last state"), status=st)
        ctx.emit(Result("debug up|down|status", {"board_id": cand.board_id, "status": st},
                        rows=[_status_row(cand.board_id, st)],
                        human=_status_human(cand.board_id, st)))
        if action == "up":
            # The engine stops a board's debug server when its session closes, so this
            # process IS the server's owner: hold until Ctrl-C, `detach`, or --for.
            if a.for_s is None:
                ctx.note(f"debug server up for {cand.board_id}; Ctrl-C or "
                         f"`socharness detach {a.target}` stops it")
            hold(a.for_s)
            svc.down(session)
    return ExitCode.OK
