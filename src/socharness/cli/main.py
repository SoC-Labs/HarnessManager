"""``socharness``: CLI entry point.

Output contract (inherited from the HAPS helper contract):

- ``--json`` prints one JSON object on stdout; ``--tsv`` prints tab-separated
  rows whose columns are append-only (``output.TSV_COLUMNS``); human output
  otherwise. Both flags work before or after the verb;
- the exit code comes from ``socharness.core.errors.ExitCode`` via a raised
  ``HarnessError``, never from an ad-hoc number;
- errors go to stderr as ``socharness: <message> — <next action>``;
- progress, prompts and remarks go to stderr; stdout carries only the result.

Every verb runs through the ``Engine`` protocol (``cli/engine.py``). The verbs
live in ``cmd_*.py``; ``socharness help --tabs`` prints the help sections the
GUI renders (``helptext.py``).
"""

from __future__ import annotations

import argparse
import functools
import logging
import os
import sys
import traceback
from collections.abc import Sequence
from typing import Any, NoReturn

from socharness.core.errors import ActionFailedError, ExitCode, HarnessError, UsageError

from . import cmd_board, cmd_io, cmd_lab, cmd_program, cmd_system, helptext
from .context import Ctx
from .engine import get_engine
from .output import TSV_COLUMNS, Result, report_error

log = logging.getLogger(__name__)

NO_ENGINE = {"help", "version"}


class _Parser(argparse.ArgumentParser):
    """argparse that raises ``UsageError`` (exit 2, our error format) instead of exiting."""

    def error(self, message: str) -> NoReturn:
        raise UsageError(f"{self.prog}: {message}", hint=f"run `{self.prog} --help`")


def _fmt_parent() -> argparse.ArgumentParser:
    # SUPPRESS defaults: a leaf that does not see the flag must not reset what the
    # top-level parser already set.
    p = argparse.ArgumentParser(add_help=False)
    g = p.add_mutually_exclusive_group()
    g.add_argument("--json", action="store_true", default=argparse.SUPPRESS,
                   help="one JSON object on stdout")
    g.add_argument("--tsv", action="store_true", default=argparse.SUPPRESS,
                   help="tab-separated rows, append-only columns")
    return p


def _usb_parent() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(add_help=False)
    p.add_argument("--serial", action="append", metavar="URL", default=argparse.SUPPRESS,
                   help="add the board controller's USB serial link (serial:///dev/ttyUSB0, "
                        "COM7, /dev/ttyUSB0)")
    p.add_argument("--volume", action="append", metavar="PATH", default=argparse.SUPPRESS,
                   help="add the configuration SD volume (the mounted V2M-MPS3 drive)")
    return p


TARGET_HELP = "shell address host[:port], or - for a USB-only board (with --serial/--volume)"


def _epilog(layout: str) -> str:
    return f"--tsv columns: {' '.join(TSV_COLUMNS[layout])}"


def _for_arg(p: argparse.ArgumentParser, what: str) -> None:
    p.add_argument("--for", dest="for_s", type=float, default=None, metavar="SECONDS",
                   help=f"{what} for this long, then stop (default: until Ctrl-C)")


def make_parser() -> argparse.ArgumentParser:
    fmt, usb = _fmt_parent(), _usb_parent()
    p = _Parser(prog="socharness", description="SoC Labs Harness Manager",
                epilog="`socharness help --tabs` prints the full help, section by section.")
    p.add_argument("--pack", default=None, help="board pack (default: mps3)")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--json", action="store_true", default=False,
                   help="one JSON object on stdout")
    g.add_argument("--tsv", action="store_true", default=False,
                   help="tab-separated rows, append-only columns")
    sub = p.add_subparsers(dest="cmd", required=True, metavar="VERB")
    verbs: dict[str, argparse.ArgumentParser] = {}

    def verb(name: str, help_: str, *, layout: str | None = None, parents=(fmt,),
             **kw: Any) -> argparse.ArgumentParser:
        vp = sub.add_parser(name, help=help_, description=help_, parents=list(parents),
                            epilog=_epilog(layout) if layout else None, **kw)
        verbs[name] = vp
        return vp

    def target(vp: argparse.ArgumentParser) -> None:
        vp.add_argument("target", metavar="TARGET", help=TARGET_HELP)

    # -- system ----------------------------------------------------------------------
    verb("version", "print the Harness Manager version", layout="version") \
        .set_defaults(fn=cmd_system.cmd_version)
    verb("packs", "list installed board packs", layout="packs") \
        .set_defaults(fn=cmd_system.cmd_packs)

    vp = verb("probe", "look for boards", layout="probe")
    vp.add_argument("--host", action="append", default=[], metavar="ADDR",
                    help="shell address to try (repeatable)")
    vp.add_argument("--serial", action="append", default=[], metavar="URL",
                    help="serial port to try (repeatable)")
    vp.add_argument("--volume", action="append", default=[], metavar="PATH",
                    help="mounted volume to try (repeatable)")
    vp.add_argument("--timeout", type=float, default=2.0, metavar="S")
    vp.add_argument("--no-scan", action="store_true",
                    help="look only at the addresses given; no default address, no USB scan")
    vp.set_defaults(fn=cmd_system.cmd_probe)

    vp = verb("info", "identity, health and capabilities of one board", layout="info",
              parents=(fmt, usb))
    target(vp)
    vp.set_defaults(fn=cmd_system.cmd_info)

    vp = verb("attach", "take the board's session lock and hold it (foreground)",
              layout="attach", parents=(fmt, usb))
    target(vp)
    vp.add_argument("--note", default="", help="shown to anyone the lock holds off")
    _for_arg(vp, "hold the lock")
    vp.set_defaults(fn=cmd_system.cmd_attach)

    vp = verb("detach", "release the lock your own attach (or console/debug up) holds",
              layout="detach", parents=(fmt, usb))
    target(vp)
    vp.add_argument("--timeout", type=float, default=5.0, metavar="S",
                    help="how long to wait for the holder to let go")
    vp.set_defaults(fn=cmd_system.cmd_detach)

    vp = verb("telemetry", "every reading, with its source", layout="telemetry",
              parents=(fmt, usb))
    target(vp)
    vp.set_defaults(fn=cmd_system.cmd_telemetry)

    # -- program -------------------------------------------------------------------
    ovl = argparse.ArgumentParser(add_help=False)
    ovl.add_argument("--overlay-dir", action="append", default=[], metavar="DIR",
                     help="look for overlays here first (repeatable); also "
                          "$SOCHARNESS_MPS3_OVERLAY_DIRS")

    vp = verb("overlays", "overlays that load on this board, and the ones that do not",
              layout="overlays", parents=(fmt, usb, ovl))
    target(vp)
    vp.set_defaults(fn=cmd_program.cmd_overlays)

    vp = verb("program", "program a partition (preflight, confirm, deploy, verify)",
              layout="program", parents=(fmt, usb, ovl))
    target(vp)
    vp.add_argument("rm", metavar="RM", help="overlay name (nanosoc) or rm_id (0x01000001)")
    vp.add_argument("--yes", action="store_true", help="do not ask for confirmation")
    vp.set_defaults(fn=cmd_program.cmd_program)

    vp = verb("restore", "load the baseline design and confirm it", layout="restore",
              parents=(fmt, usb, ovl))
    target(vp)
    vp.set_defaults(fn=cmd_program.cmd_restore)

    # -- consoles and debug --------------------------------------------------------
    vp = verb("console", "stream a console to stdout, or re-export it on a local port",
              layout="console", parents=(fmt, usb))
    target(vp)
    vp.add_argument("name", metavar="NAME", help="console name: uart0, uart1, swo, ...")
    vp.add_argument("--export", type=int, default=None, metavar="PORT",
                    help="re-export on 127.0.0.1:PORT (0 = any free port); prints the port")
    _for_arg(vp, "stream or export")
    vp.set_defaults(fn=cmd_io.cmd_console)

    vp = verb("debug", "OpenOCD debug server for the loaded design", parents=(fmt,))
    dsub = vp.add_subparsers(dest="debug_cmd", required=True, metavar="ACTION")
    for action, help_, layout in (
        ("up", "start the server and hold it until Ctrl-C", "debug up|down|status"),
        ("down", "stop the server", "debug up|down|status"),
        ("status", "state, ports, config, pid", "debug up|down|status"),
        ("detect", "non-intrusive: the TAP IDCODE", "debug detect"),
    ):
        ap = dsub.add_parser(action, help=help_, description=help_, parents=[fmt, usb],
                             epilog=_epilog(layout))
        target(ap)
        if action == "up":
            _for_arg(ap, "keep the server up")
    vp.set_defaults(fn=cmd_io.cmd_debug)

    # -- reset and clocks ----------------------------------------------------------
    vp = verb("reset", "reset part of the board (default: the DUT)", layout="reset",
              parents=(fmt, usb))
    target(vp)
    vp.add_argument("what", metavar="WHAT", nargs="?", default="dut",
                    help="what to reset (default: dut)")
    vp.set_defaults(fn=cmd_board.cmd_reset)

    vp = verb("clock", "list the clocks, or set the DUT clock", layout="clock",
              parents=(fmt, usb))
    target(vp)
    cg = vp.add_mutually_exclusive_group()
    cg.add_argument("--dut-mhz", type=float, default=None, metavar="N",
                    help="set the DUT clock to N MHz")
    cg.add_argument("--preset", default=None, metavar="NAME",
                    help="set the DUT clock by preset name (25mhz, 50mhz, 100mhz)")
    vp.set_defaults(fn=cmd_board.cmd_clock)

    # -- lab -------------------------------------------------------------------------
    vp = verb("lab", "lab tools on the shell: link, display, macgen, dutrx", parents=(fmt, usb))
    target(vp)
    lsub = vp.add_subparsers(dest="lab_cmd", required=True, metavar="TOOL")
    lp = lsub.add_parser("link", help="virtual-PHY link event", parents=[fmt, usb],
                         epilog=_epilog("lab link"))
    lp.add_argument("event", choices=("up", "down", "pulse"))
    lp = lsub.add_parser("display", help="who drives the CLCD panel", parents=[fmt, usb],
                         epilog=_epilog("lab display"))
    lp.add_argument("owner", choices=("harness", "dut", "toggle", "query"))
    lp.add_argument("--timeout", type=float, default=cmd_lab.DISPLAY_TIMEOUT_S, metavar="S",
                    help="how long a flip may take to land")
    lp = lsub.add_parser("macgen", help="MAC traffic generator/checker", parents=[fmt, usb],
                         epilog=_epilog("lab macgen"))
    lp.add_argument("--gen", action=argparse.BooleanOptionalAction, default=True,
                    help="generator on (default) or off")
    lp.add_argument("--chk", action=argparse.BooleanOptionalAction, default=True,
                    help="checker on (default) or off")
    lp.add_argument("--inject", default="none", metavar="FAULT",
                    help="arm a fault on the next frame (none, bad_fcs, runt, giant, ...)")
    lp = lsub.add_parser("dutrx", help="frames the DUT transmitted", parents=[fmt, usb],
                         epilog=_epilog("lab dutrx"))
    lp.add_argument("--frames", type=int, default=1, metavar="N", help="read up to N frames")
    vp.set_defaults(fn=cmd_lab.cmd_lab)

    # -- board controller and SD -------------------------------------------------------
    vp = verb("mcc", "the board controller (MCC): temp, osc, reboot, cmd",
              parents=(fmt, usb))
    target(vp)
    msub = vp.add_subparsers(dest="mcc_cmd", required=True, metavar="ACTION")
    msub.add_parser("temp", help="controller temperatures", parents=[fmt, usb],
                    epilog=_epilog("mcc temp|osc"))
    msub.add_parser("osc", help="oscillator set-points", parents=[fmt, usb],
                    epilog=_epilog("mcc temp|osc"))
    mp = msub.add_parser("reboot", help="reboot and prove it (down, then up)",
                         parents=[fmt, usb], epilog=_epilog("mcc reboot"))
    mp.add_argument("--yes", action="store_true", help="do not ask for confirmation")
    mp.add_argument("--wait", type=float, default=120.0, metavar="S",
                    help="how long to wait for the board to come back")
    mp = msub.add_parser("cmd", help="one allowlisted controller command",
                         parents=[fmt, usb], epilog=_epilog("mcc cmd"))
    mp.add_argument("words", nargs="+", metavar="LINE")
    vp.set_defaults(fn=cmd_board.cmd_mcc)

    vp = verb("sd", "the configuration SD: backup, install, restore", parents=(fmt, usb))
    target(vp)
    ssub = vp.add_subparsers(dest="sd_cmd", required=True, metavar="ACTION")
    sp = ssub.add_parser("backup", help="back up the SD into a zip in DIR", parents=[fmt, usb],
                         epilog=_epilog("sd backup"))
    sp.add_argument("dir", metavar="DIR")
    sp = ssub.add_parser("install", help="write a harness bundle (backup mandatory)",
                         parents=[fmt, usb], epilog=_epilog("sd install"))
    sp.add_argument("bundle", metavar="BUNDLE_DIR")
    sp.add_argument("--backup", required=True, metavar="ZIP",
                    help="the backup `sd backup` made of this SD")
    sp.add_argument("--yes", action="store_true", help="do not ask for confirmation")
    sp = ssub.add_parser("restore", help="put a backup back", parents=[fmt, usb],
                         epilog=_epilog("sd restore"))
    sp.add_argument("zip", metavar="ZIP")
    sp.add_argument("--yes", action="store_true", help="do not ask for confirmation")
    vp.set_defaults(fn=cmd_board.cmd_sd)

    # -- help ------------------------------------------------------------------------
    vp = verb("help", "help: the verbs, one verb, or the GUI's help sections",
              layout="help")
    vp.add_argument("topic", nargs="?", metavar="VERB|TAB",
                    help="a verb, or with --tabs a help tab name")
    vp.add_argument("--tabs", action="store_true",
                    help="print the help sections as '## <tab>' blocks (the GUI renders these)")
    vp.add_argument("--list", action="store_true", help="list the help tab names")
    vp.set_defaults(fn=functools.partial(cmd_help, parser=p, verbs=verbs))
    return p


def cmd_help(ctx: Ctx, *, parser: argparse.ArgumentParser,
             verbs: dict[str, argparse.ArgumentParser]) -> int:
    a = ctx.args
    names = helptext.tab_names()
    if a.list:
        ctx.emit(Result("help", {"tabs": names}, rows=[[n, "", ""] for n in names],
                        human=names))
        return ExitCode.OK
    if a.tabs:
        sections = helptext.tabs(a.topic) if a.topic else helptext.tabs()
        if not sections:
            raise UsageError(f"no help tab named {a.topic!r}", hint=f"tabs: {', '.join(names)}")
        rows = [[n, i, line] for n, text in sections
                for i, line in enumerate(text.splitlines(), 1)]
        ctx.emit(Result("help", {"tabs": [{"name": n, "text": t} for n, t in sections]},
                        rows=rows, human=helptext.render_tabs(sections).splitlines()))
        return ExitCode.OK
    if a.topic:
        vp = verbs.get(a.topic)
        if vp is None:
            raise UsageError(f"no verb named {a.topic!r}",
                             hint=f"verbs: {', '.join(sorted(verbs))}")
        text = vp.format_help()
    else:
        text = parser.format_help()
    name = a.topic or "socharness"
    ctx.emit(Result("help", {"verb": name, "text": text},
                    rows=[[name, i, line] for i, line in enumerate(text.splitlines(), 1)],
                    human=text.rstrip("\n").splitlines()))
    return ExitCode.OK


def _format(args: argparse.Namespace) -> str:
    want_json, want_tsv = bool(getattr(args, "json", False)), bool(getattr(args, "tsv", False))
    if want_json and want_tsv:
        raise UsageError("--json and --tsv cannot be combined", hint="pick one")
    return "json" if want_json else "tsv" if want_tsv else "human"


def _normalise(args: argparse.Namespace) -> None:
    if getattr(args, "cmd", "") == "mcc" and getattr(args, "mcc_cmd", "") == "cmd":
        args.line = " ".join(args.words)


def main(argv: Sequence[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    flags = {"--json", "--tsv"} & set(argv)
    guess = "human" if len(flags) != 1 else flags.pop().lstrip("-")
    parser = make_parser()
    try:
        args = parser.parse_args(argv)
        fmt = _format(args)
    except SystemExit as exc:                        # --help
        return ExitCode.OK if not exc.code else ExitCode.USAGE
    except HarnessError as exc:
        return report_error(guess, exc)
    _normalise(args)
    engine = None
    try:
        if args.cmd not in NO_ENGINE:
            engine = get_engine(args)
        return int(args.fn(Ctx(args, engine, fmt)))
    except HarnessError as exc:
        return report_error(fmt, exc)
    except KeyboardInterrupt:
        return report_error(fmt, ActionFailedError(
            "interrupted", hint="check the board with `socharness info TARGET`"))
    except BrokenPipeError:
        # The reader went away (`| head`): that ends a stream normally.
        try:
            devnull = os.open(os.devnull, os.O_WRONLY)
            os.dup2(devnull, sys.stdout.fileno())
        except (OSError, ValueError):
            pass
        return ExitCode.OK
    except Exception as exc:  # noqa: BLE001 - a bug: say so, with the exit code for bugs
        log.debug("internal error", exc_info=True)
        if os.environ.get("SOCHARNESS_DEBUG"):
            traceback.print_exc()
        return report_error(fmt, HarnessError(f"internal error: {type(exc).__name__}: {exc}"))
    finally:
        if engine is not None:
            try:
                engine.close_all()
            except Exception:  # noqa: BLE001 - never mask the verb's own result
                log.exception("closing the engine failed")


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
