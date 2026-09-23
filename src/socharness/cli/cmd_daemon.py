"""``socharness daemon start|stop|status`` and ``socharness ui``.

``register(subparsers)`` adds both verbs to the CLI's parser; ``cli/main.py``
calls it (the lead wires it in). The verbs follow the CLI's output contract:
one JSON object with ``--json``, append-only TSV columns with ``--tsv``,
errors as ``ExitCode``s.

- ``daemon start [--port N] [--listen ADDR] [--foreground]``: start socharnessd
  detached (logging to ``<state_dir>/daemon.log``), or run it in this process.
- ``daemon stop [--force] [--timeout S]``: stop it; refused while a job runs
  unless ``--force``.
- ``daemon status``: running / unresponsive / stale / stopped.
- ``ui [--no-browser] [--port N] [--listen ADDR]``: start the daemon if it is
  not running, print the UI's URL (the token rides in the ``#`` fragment) and
  open a browser at it unless ``--no-browser`` (e.g. over ``ssh -L``).
"""

from __future__ import annotations

import argparse
import os
import sys
import webbrowser
from pathlib import Path

from socharness.core.errors import ExitCode, UsageError

from .context import Ctx
from .output import TSV_COLUMNS, Result, tsv_field

DAEMON_COLUMNS = ("STATE", "PID", "PORT", "URL", "STATE_DIR")
UI_COLUMNS = ("URL", "PORT", "PID", "STARTED")

#: Append-only TSV layouts of these verbs. They belong in ``output.TSV_COLUMNS``
#: (a contract change request; T5 owns that table). Until they are there,
#: ``_emit`` prints the TSV rows itself with the same field rules.
LAYOUTS = {"daemon": DAEMON_COLUMNS, "ui": UI_COLUMNS}

NON_LOOPBACK_WARNING = ("socharnessd listens on {addr}, which is not loopback: anyone who can "
                        "reach it AND has the token controls your boards")


def _fmt_parent() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(add_help=False)
    g = p.add_mutually_exclusive_group()
    g.add_argument("--json", action="store_true", default=argparse.SUPPRESS,
                   help="one JSON object on stdout")
    g.add_argument("--tsv", action="store_true", default=argparse.SUPPRESS,
                   help="tab-separated rows, append-only columns")
    return p


def _demo_parent() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(add_help=False)
    p.add_argument("--demo", action="store_true",
                   help="the demo daemon: scripted boards, no hardware (its own state dir)")
    return p


def _listen_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--port", type=int, default=0, metavar="N",
                   help="TCP port for the daemon (default: any free port)")
    p.add_argument("--listen", default="127.0.0.1", metavar="ADDR",
                   help="address to bind (default 127.0.0.1; anything else prints a warning)")


def register(subparsers: argparse._SubParsersAction) -> None:
    """Add ``daemon`` and ``ui`` to the CLI's verbs."""
    fmt = _fmt_parent()
    demo = _demo_parent()
    vp = subparsers.add_parser(
        "daemon", help="the local engine service (socharnessd): start, stop, status",
        description="socharnessd owns the engine so the CLI, the web UI and long-lived "
                    "sessions share one board session.",
        parents=[fmt], epilog=f"--tsv columns: {' '.join(DAEMON_COLUMNS)}")
    dsub = vp.add_subparsers(dest="daemon_cmd", required=True, metavar="ACTION")
    sp = dsub.add_parser("start", help="start socharnessd (detached)", parents=[fmt, demo])
    _listen_args(sp)
    sp.add_argument("--foreground", action="store_true",
                    help="run in this process until Ctrl-C (for service managers)")
    sp = dsub.add_parser("stop", help="stop socharnessd", parents=[fmt, demo])
    sp.add_argument("--force", action="store_true",
                    help="stop even while a job runs (the job is abandoned)")
    sp.add_argument("--timeout", type=float, default=10.0, metavar="S",
                    help="how long to wait for it to exit")
    dsub.add_parser("status", help="is socharnessd running, and where", parents=[fmt, demo])
    vp.set_defaults(fn=cmd_daemon)

    up = subparsers.add_parser(
        "ui", help="open the web UI (starts socharnessd if needed)",
        description="Start socharnessd if it is not running, print the UI's URL and open "
                    "a browser at it.",
        parents=[fmt, demo], epilog=f"--tsv columns: {' '.join(UI_COLUMNS)}")
    up.add_argument("--no-browser", action="store_true",
                    help="only print the URL (e.g. to open it through `ssh -L N:127.0.0.1:N`)")
    _listen_args(up)
    up.set_defaults(fn=cmd_ui)


def state_dir(demo: bool = False) -> Path:
    """The daemon's state dir. The demo daemon has its own, so it never holds real boards."""
    from socharness.daemon.state import default_state_dir

    return default_state_dir() / "demo" if demo else default_state_dir()


def _check_listen(ctx: Ctx, listen: str, port: int) -> None:
    from socharness.daemon.state import is_loopback

    if port < 0 or port > 65535:
        raise UsageError(f"--port {port} is not a TCP port", hint="0..65535 (0: any free port)")
    if not is_loopback(listen):
        ctx.note("socharness: warning: " + NON_LOOPBACK_WARNING.format(addr=listen))


def cmd_daemon(ctx: Ctx) -> int:
    from socharness.daemon import control

    a = ctx.args
    sdir = state_dir(getattr(a, "demo", False))
    action = a.daemon_cmd
    if action == "start":
        _check_listen(ctx, a.listen, a.port)
        if a.foreground:
            from socharness.daemon.server import run_daemon

            ctx.note(f"socharnessd running in the foreground for {sdir}; Ctrl-C stops it")
            return run_daemon(sdir, port=a.port, listen=a.listen, demo=a.demo)
        info = control.start(sdir, port=a.port, listen=a.listen, demo=a.demo)
        data = {"state": "running", "pid": info.pid, "port": info.port, "url": info.base_url,
                "state_dir": str(sdir), "started": True}
        _emit(ctx, Result("daemon", data, rows=[_row(data)],
                        human=[f"started    socharnessd (pid {info.pid}) at {info.base_url}",
                               "ui         `socharness ui` opens the web UI"]))
        return ExitCode.OK
    if action == "stop":
        from socharness.daemon.state import read_info

        before = read_info(sdir)
        outcome = control.stop(sdir, force=a.force, timeout=a.timeout)
        data = {"state": outcome, "pid": before.pid if before else None,
                "port": before.port if before else None,
                "url": before.base_url if before else None, "state_dir": str(sdir)}
        _emit(ctx, Result("daemon", data, rows=[_row(data)],
                        human=[f"{outcome:<10} socharnessd ({sdir})"]))
        return ExitCode.OK
    st = control.status(sdir)
    human = [f"state      {st['state']}", f"state dir  {sdir}"]
    if st.get("pid"):
        human.append(f"pid        {st['pid']}")
    if st.get("url"):
        human.append(f"url        {st['url']}")
    if st.get("boards_open") is not None:
        human.append(f"boards     {st['boards_open']} open")
    if st.get("detail"):
        human.append(f"detail     {st['detail']}")
    _emit(ctx, Result("daemon", st, rows=[_row(st)], human=human))
    return ExitCode.OK


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


def can_open_browser() -> bool:
    """False on a Linux/BSD box with no display (e.g. over ssh): there ``webbrowser``
    would start a console browser (lynx, w3m) in this terminal. ``$BROWSER`` wins."""
    if os.environ.get("BROWSER"):
        return True
    if sys.platform in ("win32", "darwin"):
        return True
    return bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))


def _row(data: dict) -> list:
    return [data.get("state"), data.get("pid"), data.get("port"), data.get("url"),
            data.get("state_dir")]


def cmd_ui(ctx: Ctx) -> int:
    from socharness.daemon import control

    a = ctx.args
    sdir = state_dir(getattr(a, "demo", False))
    _check_listen(ctx, a.listen, a.port)
    info, started = control.ensure_running(sdir, port=a.port, listen=a.listen, demo=a.demo)
    if a.port and info.port != a.port:
        raise UsageError(f"socharnessd already runs on port {info.port}, not {a.port}",
                         hint="use that port (e.g. `ssh -L "
                              f"{info.port}:127.0.0.1:{info.port}`), or `socharness daemon stop` "
                              "and run `socharness ui` again")
    url = info.ui_url
    opened = False
    headless = not a.no_browser and not can_open_browser()
    if not a.no_browser and not headless:
        try:
            opened = bool(webbrowser.open(url))
        except webbrowser.Error:
            opened = False
    data = {"url": url, "port": info.port, "pid": info.pid, "started": started,
            "browser": opened}
    _emit(ctx, Result("ui", data, rows=[[url, info.port, info.pid, started]], human=[url]))
    how = "started" if started else "running"
    stop = "`socharness daemon stop --demo`" if a.demo else "`socharness daemon stop`"
    ctx.note(f"socharnessd {'(demo) ' if a.demo else ''}{how} (pid {info.pid}); {stop} stops it")
    if headless:
        ctx.note("no display here: open the URL above in a browser (over ssh: "
                 f"`ssh -L {info.port}:127.0.0.1:{info.port} HOST`, then open it locally)")
    elif not a.no_browser and not opened:
        ctx.note("no browser could be opened here; open the URL above")
    return ExitCode.OK
