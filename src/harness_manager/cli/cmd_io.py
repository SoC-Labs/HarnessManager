"""Consoles (``engine.consoles``) and debug sessions (``engine.debug``).

Lane L2 adds the promoted console path, ``harness-manager pty TARGET NAME``: the
console's PTY path and the ``screen`` command to attach with, from any terminal,
while the GUI shows the same console. And ``harness-manager baud TARGET NAME
[RATE]``: each console's rate, and a change where the console allows one. The
``console`` verb stays (a console in this terminal, or ``--export``).
"""

from __future__ import annotations

import os
import select
import sys
import threading
import time
from collections.abc import Callable
from typing import Any

from harness_manager.core.errors import (
    AbsentError,
    ActionFailedError,
    ExitCode,
    HarnessError,
    UnavailableError,
    UnreachableError,
    UsageError,
)
from harness_manager.core.services import DebugStatus

from .context import Ctx, Stopper, hold, hold_note
from .output import TSV_COLUMNS, Result, tsv_field, tsv_line, with_data

EXPORT_HOST = "127.0.0.1"
READ_SLICE_S = 0.2
ESCAPE = b"\x1d"          # Ctrl-], as in telnet and miniterm

#: FIX-PACK-5: the interactive console resets the DUT itself, because while it runs it
#: holds the board's session lock and a `reset` from another terminal is refused (one
#: process owns a board). Ctrl-] then r arms it; r or y confirms, any other key cancels.
#: Ctrl-] then any other key exits at once; Ctrl-] alone exits after ESCAPE_WAIT_S.
RESET_KEY = b"r"
CONFIRM_KEYS = (b"r", b"y")
ESCAPE_WAIT_S = 2.0
CONFIRM_WAIT_S = 10.0
RESET_HELP = "Ctrl-] then r, r resets the DUT"


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
                         hint="e.g. harness-manager --json console TARGET uart0 --for 5")
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
                text = _terminal(ctx, stream, cand.board_id, _key_reader(),
                                 reset=_dut_reset(ctx, session, cand.board_id))
            else:
                text = _pump(ctx, stream)
        finally:
            stream.close()
    if ctx.fmt == "json":
        ctx.emit(Result("console", {"board_id": cand.board_id, "name": a.name, "text": text,
                                    "bytes": len(text.encode())}))
    return ExitCode.OK


#: A console that is not up this long after the start gets a note on stderr (Q2).
NOT_UP_NOTE_S = 3.0


def _pump(ctx: Ctx, stream) -> str:
    """Stream the console until Ctrl-C/SIGTERM or ``--for`` elapses. Returns what was read.

    A console that never connected is an error, not an empty success: ``--json console
    X uart0 --for 5`` against a board whose console port refused printed ``ok: true``
    with ``text: ""``, which reads as "the DUT said nothing" (Q2, 2026-09-24).
    """
    name = ctx.args.name
    collected = bytearray()
    pending = b""
    state = getattr(stream, "state", "up")
    ever_up = state == "up"
    noted = False
    started = time.monotonic()
    with Stopper(ctx.args.for_s) as stopper:
        while not stopper.done:
            state = getattr(stream, "state", "up")
            ever_up = ever_up or state == "up"
            if not ever_up and not noted and time.monotonic() - started >= NOT_UP_NOTE_S:
                ctx.note(f"console {name}: {state}; not connected yet (Ctrl-C stops)")
                noted = True
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
    if not ever_up and not collected:
        raise UnreachableError(
            f"console {name} never connected (it stayed {state})",
            hint="is the board up and its harness serving the console? "
                 "`harness-manager info TARGET` says")
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


def _dut_reset(ctx: Ctx, session: Any, board_id: str) -> Callable[[], None] | None:
    """The console's Ctrl-] r action (FIX-PACK-5): ``reset dut`` on the console's own
    session, the same call the ``reset`` verb makes. None when the session has no reset
    adapter: then Ctrl-] exits at once, as it always did."""
    if getattr(session, "resets", None) is None:
        return None
    from .cmd_board import reset_on

    return lambda: reset_on(ctx, session, board_id, "dut")


def _say(text: str) -> None:
    """One line from harness-manager inside the raw-mode console."""
    _write_raw(f"\r\n[harness-manager: {text}]\r\n".encode())


def _terminal(ctx: Ctx, stream, board_id: str, keys: Any,
              reset: Callable[[], None] | None = None) -> str:
    """Board output to this terminal, keystrokes to the board, until Ctrl-] (or SIGTERM).

    The raw mode sends every key, Ctrl-C included, to the board: a MicroPython
    REPL on the DUT needs Ctrl-C and Ctrl-D. The broker paces the bytes when the
    board pack asks it to (the MPS3 DUT UARTs).

    With ``reset`` (FIX-PACK-5), Ctrl-] then r arms a DUT reset and r or y confirms it;
    the console stays open and shows the boot. Ctrl-] then any other key exits at once,
    and Ctrl-] alone exits after ``ESCAPE_WAIT_S``. Keys read for the escape are never
    sent to the board.
    """
    name = ctx.args.name
    extra = f"; {RESET_HELP}" if reset is not None else ""
    ctx.note(f"console {name} of {board_id}: typing goes to the board, "
             f"Ctrl-] exits (Ctrl-C is sent to the board){extra}")
    collected = bytearray()
    done = threading.Event()

    def send(data: bytes) -> None:
        try:
            stream.write(data)
        except HarnessError as exc:
            _write_raw(f"\r\n[harness-manager: {exc.message}]\r\n".encode())

    def next_key(rest: bytes, wait_s: float) -> tuple[bytes, bytes]:
        """The next key and the bytes after it: from ``rest`` first, else typed within
        ``wait_s``; (b"", b"") when none came."""
        if rest:
            return rest[:1], rest[1:]
        end = time.monotonic() + wait_s
        while not done.is_set():
            left = end - time.monotonic()
            if left <= 0:
                break
            data = keys.read(min(READ_SLICE_S, left))
            if data:
                return data[:1], data[1:]
        return b"", b""

    def escape(rest: bytes) -> bytes | None:
        """After Ctrl-]: None to exit, else the bytes still to send to the board."""
        if reset is None:
            return None
        _say(f"r resets the DUT; any other key exits (or wait {ESCAPE_WAIT_S:g} s)")
        key, rest = next_key(rest, ESCAPE_WAIT_S)
        if key.lower() != RESET_KEY:
            return None
        _say(f"reset the DUT of {board_id}? r or y resets it; any other key cancels")
        key, rest = next_key(rest, CONFIRM_WAIT_S)
        if key.lower() not in CONFIRM_KEYS:
            _say("not reset; back to the console")
            return rest
        try:
            reset()
        except HarnessError as exc:
            _say(f"the DUT was not reset: {exc.message}"
                 + (f"; {exc.hint}" if exc.hint else ""))
        except Exception as exc:  # noqa: BLE001 - the console must outlive its own action
            _say(f"the DUT was not reset: {type(exc).__name__}: {exc}")
        else:
            _say(f"reset the DUT of {board_id}: done")
        return rest

    def forward() -> None:
        pending = b""
        while not done.is_set():
            if pending:
                data, pending = pending, b""
            else:
                try:
                    data = keys.read(READ_SLICE_S)
                except OSError:
                    done.set()
                    return
            if not data:
                continue
            if ESCAPE in data:
                head, _, rest = data.partition(ESCAPE)
                if head:
                    send(head)
                try:
                    left = escape(rest)
                except OSError:
                    left = None
                if left is None:
                    done.set()
                    return
                pending = left
                continue
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


def _gdb_command(gdb_port: int) -> str:
    """``services.debug.gdb_command`` (imported here: the other verbs never load it)."""
    from harness_manager.services.debug import gdb_command

    return gdb_command(gdb_port, EXPORT_HOST)


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
    if st.gdb_port:
        lines.append(f"attach     {_gdb_command(st.gdb_port)}")
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
    if action in ("up", "detect"):
        # DEBUG-OCD: an OpenOCD without the board's adapter is refused before the board
        # (and its SSH tunnel) is opened. Local engines only: the daemon checks its own.
        check = getattr(ctx.engine.debug, "openocd", None)
        if callable(check):
            check()
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
                hint="`harness-manager debug status TARGET` shows the last state"), status=st)
        data: dict[str, Any] = {"board_id": cand.board_id, "status": st}
        if st.gdb_port:                                 # FIX-PACK-5: the line to paste
            data["gdb_command"] = _gdb_command(st.gdb_port)
        human = _status_human(cand.board_id, st)
        report = getattr(svc, "openocd_report", None)
        ocd = report(session) if action == "status" and callable(report) else None
        if ocd:                                         # the adapter verdict (DEBUG-OCD)
            data["openocd"] = ocd
            human.append(f"openocd    {ocd.get('detail', '')}")
            if ocd.get("hint"):
                human.append(f"           fix: {ocd['hint']}")
        ctx.emit(Result("debug up|down|status", data,
                        rows=[_status_row(cand.board_id, st)], human=human))
        if action == "up":
            # The engine stops a board's debug server when its session closes, so this
            # process IS the server's owner: hold until Ctrl-C, `detach`, or --for.
            if a.for_s is None:
                ctx.note(f"debug server up for {cand.board_id}: run gdb in another terminal "
                         f"(the attach line); this one holds the server until Ctrl-C or "
                         f"`harness-manager detach {a.target}`")
            hold(a.for_s)
            svc.down(session)
    return ExitCode.OK


# --- pty and baud (lane L2) -------------------------------------------------------------------

PTY_COLUMNS = ("BOARD_ID", "NAME", "PATH", "DEVICE", "COMMAND", "CLIENTS", "HELD_BY")
BAUD_COLUMNS = ("BOARD_ID", "NAME", "KIND", "BAUD", "SETTABLE", "SOURCE", "REASON")

#: Append-only TSV layouts of these verbs. They belong in ``output.TSV_COLUMNS``
#: (a contract change request; the table is not this lane's). Until they are
#: there, ``_emit`` prints the TSV rows itself with the same field rules.
LAYOUTS = {"pty": PTY_COLUMNS, "baud": BAUD_COLUMNS}


def _emit(ctx: Ctx, result: Result) -> None:
    if ctx.fmt != "tsv" or result.layout in TSV_COLUMNS:
        ctx.emit(result)
        return
    cols = LAYOUTS[result.layout]
    for row in result.rows:
        if len(row) != len(cols):     # a bug in the verb, never the user's fault
            raise AssertionError(f"tsv layout {result.layout!r} has {len(cols)} columns")
        sys.stdout.write("\t".join(tsv_field(v) for v in row) + "\n")
    sys.stdout.flush()


def _need(broker: Any, name: str) -> Any:
    fn = getattr(broker, name, None)
    if not callable(fn):
        reason = getattr(broker, "reason", "") or f"this build's console service has no {name}()"
        raise UnavailableError("console_dut", reason)
    return fn


def _check_name(broker: Any, session: Any, board_id: str, name: str) -> None:
    names = list(broker.names(session))
    if name not in names:
        raise AbsentError(f"{board_id} has no console named {name!r}",
                          hint=f"consoles: {', '.join(names) or 'none'}")


def cmd_pty(ctx: Ctx) -> int:
    """The console's PTY for ``screen``. In-process, this command holds it (Ctrl-C closes it);
    when harness-manager-daemon already has the board open, the PTY is the daemon's and
    this command prints it and returns."""
    a = ctx.args
    with ctx.board(note=hold_note(f"pty {a.name}")) as (cand, session):
        broker = ctx.engine.consoles
        _check_name(broker, session, cand.board_id, a.name)
        if a.baud is not None:
            _need(broker, "set_baud")(session, a.name, a.baud)
        info = _need(broker, "pty")(session, a.name)
        daemon_holds = getattr(session, "owned", None) is False
        held_by = "harness-manager-daemon" if getattr(session, "owned", None) is not None \
            else "this command"
        _emit(ctx, Result("pty", {"board_id": cand.board_id, **info, "held_by": held_by,
                                  "held_here": not daemon_holds},
                          rows=[[cand.board_id, info.get("name", a.name), info["path"],
                                 info.get("device", ""), info["command"], info.get("clients"),
                                 held_by]],
                          human=[info["path"], info["command"]]))
        if daemon_holds:
            ctx.note("the PTY is harness-manager-daemon's: it stays while the board is open "
                     "there (the web UI, or `harness-manager ui`)")
            return ExitCode.OK
        if a.for_s is None:
            ctx.note(f"console {info.get('name', a.name)} of {cand.board_id}: run "
                     f"`{info['command']}` in any terminal; Ctrl-C here closes the PTY")
        hold(a.for_s)
    return ExitCode.OK


def _baud_human(row: dict[str, Any]) -> list[str]:
    baud = row.get("baud")
    head = f"{row.get('name', '')}  {baud if baud else '-'} baud  ({row.get('kind', '?')}, " \
           f"{row.get('source', 'unknown')})  {'settable' if row.get('settable') else 'fixed'}"
    lines = [head]
    if row.get("settable") and row.get("choices"):
        lines.append("choices  " + " ".join(str(c) for c in row["choices"]))
    if row.get("reason"):
        lines.append(f"why      {row['reason']}")
    return lines


def cmd_baud(ctx: Ctx) -> int:
    """A console's rate; with RATE, change it (0 goes back to the console's default)."""
    a = ctx.args
    with ctx.board(note=f"cli baud {a.name}") as (cand, session):
        broker = ctx.engine.consoles
        _check_name(broker, session, cand.board_id, a.name)
        changed: dict[str, Any] | None = None
        if a.rate is not None:
            changed = _need(broker, "set_baud")(session, a.name, a.rate)
        row = _need(broker, "baud")(session, a.name)
        remote = getattr(session, "owned", None) is not None
        if changed is not None and row.get("kind") == "serial" and not remote:
            ctx.note("a serial console's rate lives in the process that holds its port, and this "
                     "one exits now: set it through harness-manager-daemon (`harness-manager "
                     "daemon start`), or with `harness-manager pty TARGET NAME --baud RATE`")
        data = {"board_id": cand.board_id, **row}
        if changed is not None:
            data["changed"] = changed
        _emit(ctx, Result("baud", data,
                          rows=[[cand.board_id, row.get("name", a.name), row.get("kind"),
                                 row.get("baud"), row.get("settable"), row.get("source"),
                                 row.get("reason")]],
                          human=_baud_human(row)))
    return ExitCode.OK


def register(subparsers: Any) -> dict[str, Any]:
    """Add ``pty`` and ``baud`` to the CLI's verbs; returns ``{verb: parser}`` for ``help``."""
    from .main import TARGET_HELP, _fmt_parent, _usb_parent

    def _epilog_for(cols: tuple[str, ...]) -> str:
        return f"--tsv columns: {' '.join(cols)}"

    fmt, usb = _fmt_parent(), _usb_parent()
    out: dict[str, Any] = {}
    help_ = ("a console's PTY for `screen`: prints the path and the screen command "
             "(the GUI shows the same console)")
    vp = subparsers.add_parser("pty", help=help_, description=help_, parents=[fmt, usb],
                               epilog=_epilog_for(PTY_COLUMNS))
    vp.add_argument("target", metavar="TARGET", help=TARGET_HELP)
    vp.add_argument("name", metavar="NAME", help="console name: uart0, uart1, swo, fpga_uart0, ...")
    vp.add_argument("--baud", type=int, default=None, metavar="RATE",
                    help="set a serial console's rate first (0 = its default)")
    vp.add_argument("--for", dest="for_s", type=float, default=None, metavar="SECONDS",
                    help="hold the PTY this long, then close it (default: until Ctrl-C)")
    vp.set_defaults(fn=cmd_pty)
    out["pty"] = vp

    help_ = "a console's baud rate; with RATE, change it where the console allows (0 = default)"
    vp = subparsers.add_parser("baud", help=help_, description=help_, parents=[fmt, usb],
                               epilog=_epilog_for(BAUD_COLUMNS))
    vp.add_argument("target", metavar="TARGET", help=TARGET_HELP)
    vp.add_argument("name", metavar="NAME", help="console name: uart0, uart1, swo, fpga_uart0, ...")
    vp.add_argument("rate", nargs="?", type=int, default=None, metavar="RATE",
                    help="the new rate in baud (0 goes back to the console's default)")
    vp.set_defaults(fn=cmd_baud)
    out["baud"] = vp
    return out
