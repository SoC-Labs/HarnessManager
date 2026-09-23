"""``harness-manager lease`` and ``harness-manager share``: the lab hub from a terminal (lane L1).

::

    harness-manager lease show TARGET
    harness-manager lease acquire TARGET [--ttl S] [--holder NAME] [--timeout S]
    harness-manager lease release TARGET
    harness-manager share list TARGET
    harness-manager share start TARGET NAME      # a share name from boards.toml (mcc, ...) or a /dev path

TARGET is the board, as every other verb takes it (``192.168.10.101``); its
boards.toml ``hub`` table names the hub and the fpgahub target. These verbs
talk to the hub, never to the board, so they work while the Harness Manager
service has the board open: the service and the CLI keep the lease token in
the same place (``<state_dir>/leases``), and the service heartbeats a lease the
CLI took as soon as the board is open there.

``lease acquire`` blocks until the hub grants the lease (it may queue; the
queue place is kept). Ctrl-C while queued removes the queue entry. There is no
``share stop``: fpgahub stops EVERY share on the board with it.

The lead wires ``register(sub)`` into ``cli/main.py`` (CCR L1-2).
"""

from __future__ import annotations

import argparse
import sys
from typing import Any

from harness_manager.core.errors import AbsentError, ExitCode, UsageError
from harness_manager.services.lease import DEFAULT_TTL_S, LeaseService, default_holder

from .context import Ctx
from .output import TSV_COLUMNS, Result, tsv_field

LEASE_COLUMNS = ("TARGET", "HUB", "STATE", "HOLDER", "EXPIRES", "MINE")
SHARE_COLUMNS = ("TARGET", "HUB", "TTY", "TCP", "WRITER", "READERS", "RUNNING")

#: Append-only TSV layouts. They belong in ``output.TSV_COLUMNS`` (CCR L1-2; T5 owns that
#: table and its golden test). Until they are there, ``_emit`` prints the rows itself
#: with the same field rules, as ``cmd_daemon`` does.
LAYOUTS = {"lease": LEASE_COLUMNS, "share": SHARE_COLUMNS}


def _fmt_parent() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(add_help=False)
    g = p.add_mutually_exclusive_group()
    g.add_argument("--json", action="store_true", default=argparse.SUPPRESS,
                   help="one JSON object on stdout")
    g.add_argument("--tsv", action="store_true", default=argparse.SUPPRESS,
                   help="tab-separated rows, append-only columns")
    return p


def register(subparsers: argparse._SubParsersAction) -> None:
    """Add ``lease`` and ``share`` to the CLI's verbs."""
    fmt = _fmt_parent()
    target_help = "the board (192.168.10.101); its boards.toml hub table names the hub"

    vp = subparsers.add_parser(
        "lease", help="the board's hub lease: show, acquire (may queue), release",
        description="The hub lease that keeps two people off one shared board. The Harness "
                    "Manager service heartbeats it while the board is open there.",
        parents=[fmt], epilog=f"--tsv columns: {' '.join(LEASE_COLUMNS)}")
    lsub = vp.add_subparsers(dest="lease_cmd", required=True, metavar="ACTION")
    sp = lsub.add_parser("show", help="who holds it, until when", parents=[fmt])
    sp.add_argument("target", metavar="TARGET", help=target_help)
    sp = lsub.add_parser("acquire", help="take it; waits in the queue if someone holds it",
                         parents=[fmt])
    sp.add_argument("target", metavar="TARGET", help=target_help)
    sp.add_argument("--ttl", type=int, default=DEFAULT_TTL_S, metavar="S",
                    help=f"lease length in seconds (default {DEFAULT_TTL_S}); the service "
                         "extends it while the board is open")
    sp.add_argument("--holder", default=None, metavar="NAME",
                    help=f"holder name (default {default_holder()})")
    sp.add_argument("--timeout", type=float, default=3600.0, metavar="S",
                    help="give up waiting in the queue after this long (the entry is removed)")
    sp = lsub.add_parser("release", help="give it back (only a lease this Harness Manager took)",
                         parents=[fmt])
    sp.add_argument("target", metavar="TARGET", help=target_help)
    vp.set_defaults(fn=cmd_lease)

    vp = subparsers.add_parser(
        "share", help="the hub's TTY shares for the board: list, start (never stop)",
        description="fpgahub serves a board's USB serial ports (the MCC on tty_00, the FPGA "
                    "UART lanes) as TCP streams. There is no stop: it stops every share.",
        parents=[fmt], epilog=f"--tsv columns: {' '.join(SHARE_COLUMNS)}")
    ssub = vp.add_subparsers(dest="share_cmd", required=True, metavar="ACTION")
    sp = ssub.add_parser("list", help="running shares", parents=[fmt])
    sp.add_argument("target", metavar="TARGET", help=target_help)
    sp = ssub.add_parser("start", help="start a share (returns the existing one if running)",
                         parents=[fmt])
    sp.add_argument("target", metavar="TARGET", help=target_help)
    sp.add_argument("name", metavar="NAME",
                    help="a share name from boards.toml (mcc, fpga_uart2, ...) or a /dev/... path")
    vp.set_defaults(fn=cmd_share)


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


def _hub(ctx: Ctx) -> tuple[Any, Any]:
    """(candidate, the board's hub adapter) from TARGET and boards.toml, without opening it."""
    from harness_manager.core.registry import load_packs

    cand = ctx.candidate()
    pack = load_packs().get(cand.pack)          # the installed pack (a daemon engine's are remote)
    hub_for = getattr(pack, "hub_for", None)
    hub = hub_for(cand) if callable(hub_for) else None
    if hub is None:
        raise AbsentError(f"{cand.board_id} is not behind a hub",
                          hint="add a hub table for it to boards.toml (docs/HIL_B0.md step 0.2)")
    return cand, hub


def _service() -> LeaseService:
    from harness_manager.engine import resolve_state_dir

    return LeaseService(resolve_state_dir())


def _row(target: str, hub: str, view: dict[str, Any]) -> list[Any]:
    lease = view.get("lease")
    if lease is None:
        return [target, hub, "free", "", "", False]
    return [target, hub, "held", lease["holder"], lease.get("expires_at", ""), lease["mine"]]


def cmd_lease(ctx: Ctx) -> int:
    a = ctx.args
    cand, hub = _hub(ctx)
    svc = _service()
    if a.lease_cmd == "show":
        view = svc.view(hub)
        lease = view["lease"]
        human = [f"{hub.target} on {hub.host}: not leased"] if lease is None else [
            f"{hub.target} on {hub.host}: held by {lease['holder']} (user {lease.get('user') or '?'}, "
            f"expires {lease.get('expires_at') or '?'})" + (" — yours" if lease["mine"] else "")]
        _emit(ctx, Result("lease", {"board_id": cand.board_id, **view},
                        rows=[_row(hub.target, hub.host, view)], human=human))
        return ExitCode.OK
    if a.lease_cmd == "acquire":
        holder = a.holder or default_holder()

        def progress(phase: str, done: int, _total: int) -> None:
            if phase == "queued":
                where = f" at position {done}" if done else ""
                ctx.note(f"queued{where} for {hub.target} as {holder}; waiting (Ctrl-C leaves the queue)")
            elif phase == "acquire":
                ctx.note(f"asking {hub.host} for {hub.target} as {holder} ...")

        try:
            out = svc.acquire(hub, board_id=cand.board_id, ttl_s=a.ttl, holder=holder,
                              progress=progress, timeout_s=a.timeout, heartbeat=False)
        except KeyboardInterrupt:
            removed = hub.client.lease_cancel(holder)
            ctx.note("left the queue" if removed else "not queued")
            raise
        lease = out["lease"]
        view = {"lease": lease, "hub": hub.host}
        human = [f"{hub.target} on {hub.host}: held by {lease['holder']}"
                 f" until {lease.get('expires_at') or '?'}" + (" (already yours)" if out.get("already")
                                                                else ""),
                 "the Harness Manager service extends it while the board is open there"]
        _emit(ctx, Result("lease", {"board_id": cand.board_id, **view},
                        rows=[_row(hub.target, hub.host, view)], human=human))
        return ExitCode.OK
    if a.lease_cmd == "release":
        out = svc.release(hub, board_id=cand.board_id)
        view = {"lease": None, "hub": hub.host}
        _emit(ctx, Result("lease", {"board_id": cand.board_id, **view, "released": out.get("released")},
                        rows=[_row(hub.target, hub.host, view)],
                        human=[f"{hub.target} on {hub.host}: released"]))
        return ExitCode.OK
    raise UsageError(f"unknown lease action {a.lease_cmd!r}")


def cmd_share(ctx: Ctx) -> int:
    a = ctx.args
    cand, hub = _hub(ctx)
    if a.share_cmd == "list":
        shares = hub.client.share_list()
    elif a.share_cmd == "start":
        tty = a.name if a.name.startswith("/dev/") else hub.config.shares.get(a.name)
        if not tty:
            raise AbsentError(f"no share named {a.name!r} for {cand.board_id}",
                              hint="configured: " + (", ".join(sorted(hub.config.shares)) or "none")
                                   + "; or give the /dev/... path")
        shares = [hub.client.share_start(tty, hub.config.baud)]
    else:
        raise UsageError(f"unknown share action {a.share_cmd!r}")
    rows = [[hub.target, hub.host, s.tty, f"{s.host}:{s.port}", s.writer, s.readers, s.running]
            for s in shares]
    human = [f"{s.tty} → {s.host}:{s.port}  writer {s.writer or '-'}  clients {s.readers}"
             for s in shares] or [f"no shares running for {hub.target} on {hub.host}"]
    data = {"board_id": cand.board_id, "hub": hub.host, "target": hub.target,
            "shares": [{"tty": s.tty, "host": s.host, "port": s.port, "writer": s.writer,
                        "readers": s.readers, "running": s.running} for s in shares]}
    _emit(ctx, Result("share", data, rows=rows, human=human))
    return ExitCode.OK
