"""Consoles (``engine.consoles``) and debug sessions (``engine.debug``)."""

from __future__ import annotations

import os
import select
import sys
import threading
import time
from typing import Any

from socharness.core.errors import (
    AbsentError,
    ActionFailedError,
    ExitCode,
    HarnessError,
    UsageError,
)
from socharness.core.services import DebugStatus

from .context import Ctx, Stopper, hold, hold_note
from .output import Result, tsv_line, with_data

EXPORT_HOST = "127.0.0.1"
READ_SLICE_S = 0.2
ESCAPE = b"\x1d"          # Ctrl-], as in telnet and miniterm


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
            if _interactive(ctx):
                text = _terminal(ctx, stream, cand.board_id, _key_reader())
            else:
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


def _interactive(ctx: Ctx) -> bool:
    """A terminal on both ends: typing goes to the board (``--read-only`` turns it off)."""
    a = ctx.args
    if ctx.fmt != "human" or a.for_s is not None or getattr(a, "read_only", False):
        return False
    try:
        return sys.stdin.isatty() and sys.stdout.isatty()
    except (AttributeError, ValueError):
        return False


class _PosixKeys:
    """Raw keystrokes from the controlling terminal; the mode is restored on exit."""

    def __init__(self) -> None:
        self.fd = sys.stdin.fileno()
        self._saved: Any = None

    def __enter__(self) -> _PosixKeys:
        import termios
        import tty

        self._saved = termios.tcgetattr(self.fd)
        tty.setraw(self.fd)          # Ctrl-C, Ctrl-D, Ctrl-Z reach the board as bytes
        return self

    def __exit__(self, *exc: object) -> None:
        import termios

        if self._saved is not None:
            termios.tcsetattr(self.fd, termios.TCSADRAIN, self._saved)

    def read(self, timeout: float) -> bytes:
        ready, _, _ = select.select([self.fd], [], [], timeout)
        return os.read(self.fd, 1024) if ready else b""


class _WindowsKeys:
    """Keystrokes from the Windows console; arrow and edit keys become ANSI sequences."""

    KEYS = {"H": b"\x1b[A", "P": b"\x1b[B", "M": b"\x1b[C", "K": b"\x1b[D",
            "G": b"\x1b[H", "O": b"\x1b[F", "S": b"\x1b[3~"}

    def __enter__(self) -> _WindowsKeys:
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def read(self, timeout: float) -> bytes:
        import msvcrt  # type: ignore[import-not-found]

        out = bytearray()
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            while msvcrt.kbhit():
                ch = msvcrt.getwch()
                out += self.KEYS.get(msvcrt.getwch(), b"") if ch in ("\x00", "\xe0") \
                    else ch.encode("utf-8")
            if out:
                break
            time.sleep(0.01)
        return bytes(out)


def _key_reader() -> Any:
    return _WindowsKeys() if os.name == "nt" else _PosixKeys()


def _terminal(ctx: Ctx, stream, board_id: str, keys: Any) -> str:
    """Board output to this terminal, keystrokes to the board, until Ctrl-] (or SIGTERM).

    The raw mode sends every key, Ctrl-C included, to the board: a MicroPython
    REPL on the DUT needs Ctrl-C and Ctrl-D. The broker paces the bytes when the
    board pack asks it to (the MPS3 DUT UARTs).
    """
    name = ctx.args.name
    ctx.note(f"console {name} of {board_id}: typing goes to the board, "
             "Ctrl-] exits (Ctrl-C is sent to the board)")
    collected = bytearray()
    done = threading.Event()

    def send(data: bytes) -> None:
        try:
            stream.write(data)
        except HarnessError as exc:
            _write_raw(f"\r\n[socharness: {exc.message}]\r\n".encode())

    def forward() -> None:
        while not done.is_set():
            try:
                data = keys.read(READ_SLICE_S)
            except OSError:
                done.set()
                return
            if not data:
                continue
            if ESCAPE in data:
                head = data.split(ESCAPE, 1)[0]
                if head:
                    send(head)
                done.set()
                return
            send(data)

    with Stopper(None) as stopper, keys:
        typer = threading.Thread(target=forward, daemon=True, name=f"console-{name}-keys")
        typer.start()
        while not done.is_set() and not stopper.done:
            chunk = stream.read(timeout=READ_SLICE_S)
            if chunk:
                collected += chunk
                _write_raw(chunk)
            elif getattr(stream, "closed", False):
                break
        done.set()
        typer.join(timeout=1.0)
    _write_raw(b"\r\n")
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
