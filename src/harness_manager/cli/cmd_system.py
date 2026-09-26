"""System verbs: version, packs, probe, info, attach, detach, telemetry."""

from __future__ import annotations

import os
import signal
import socket
import time

from harness_manager import __version__, naming
from harness_manager.core.errors import (
    AbsentError,
    ActionFailedError,
    AlreadyError,
    ExitCode,
    HeldError,
)
from harness_manager.core.pack import ProbeHints
from harness_manager.core.session import LockOwner

from .context import Ctx, hold, hold_note, is_mine, is_my_hold, serial_url
from .engine import describe_engine
from .output import Result, reading_human, reading_json, reading_row, with_data


def cmd_version(ctx: Ctx) -> int:
    engine = describe_engine()
    ctx.emit(Result("version", {"version": __version__, "engine": engine},
                    rows=[[__version__, engine]], human=[__version__]))
    return ExitCode.OK


def cmd_packs(ctx: Ctx) -> int:
    packs = ctx.engine.packs()
    items = sorted((name, pack.title) for name, pack in packs.items())
    ctx.emit(Result("packs", {"packs": dict(items)}, rows=[list(i) for i in items],
                    human=[f"{n}\t{t}" for n, t in items]))
    return ExitCode.OK


# --- probe -------------------------------------------------------------------------------


def cmd_probe(ctx: Ctx) -> int:
    a = ctx.args
    scan = not a.no_scan
    hints = ProbeHints(
        hosts=tuple(a.host),
        serial_ports=tuple(serial_url(s) for s in (getattr(a, "serial", None) or ())),
        volumes=tuple(getattr(a, "volume", None) or ()),
        scan_usb=scan,
        scan_network=scan,
        timeout_s=a.timeout,
        via=getattr(a, "via", "") or "",
    )
    found = ctx.engine.probe(hints)
    if getattr(a, "pack", None):
        found = [c for c in found if c.pack == a.pack]
    if not found:
        where = ", ".join(list(hints.hosts) + list(hints.serial_ports) + list(hints.volumes))
        raise with_data(AbsentError(
            f"no board answered{f' at {where}' if where else ''}",
            hint="check power and cabling, or pass --host ADDR / --serial URL / --volume PATH"),
            candidates=[])
    rows, human = [], []
    for c in found:
        links = [f"{lk.kind.value}={lk.address}" for lk in c.links]
        rows.append([c.board_id, c.pack, c.label, c.evidence, links, c.name])
        named = f"{c.name}\t" if c.name else ""
        human.append(f"{named}{c.board_id}\t{c.label}\t{c.evidence}")
    ctx.emit(Result("probe", {"candidates": found}, rows=rows, human=human))
    return ExitCode.OK


# --- info --------------------------------------------------------------------------------


def cmd_info(ctx: Ctx) -> int:
    with ctx.board() as (cand, _session):
        info = ctx.engine.info(cand.board_id)
    ident = info.identity
    cand = info.candidate
    row = [cand.board_id, ident.board_type, ident.shell_id, ident.rm_id,
           ident.rm_name, ident.harness_version, ident.build_check.value,
           info.health.control_channel, cand.name]
    human = [f"name       {cand.name} ({naming.describe_source(cand)})"] if cand.name else []
    human += [
        f"board      {cand.label}",
        f"shell      {ident.shell_id}   harness {ident.harness_version or '?'}"
        f" ({ident.firmware_sha or '?'}{' dirty' if ident.firmware_dirty else ''})",
        f"design     {ident.rm_name or '?'} ({ident.rm_id})",
        f"build      firmware/fabric check: {ident.build_check.value}",
        f"features   {', '.join(ident.features) or '-'}",
        f"control    {info.health.control_channel}",
    ]
    human += [f"note       {n}" for n in info.health.notes]
    claim = getattr(info, "claim", None)
    if claim is not None:                    # LINUX-CLAIM: a Linux harness's SSH claim
        from .cmd_claim import describe

        human.append(f"ssh claim  {describe(claim)}")
    human.append(f"can        {', '.join(sorted(info.capabilities))}")
    human += [f"cannot     {cap}: {why}" for cap, why in sorted(info.unavailable.items())]
    # The JSON object IS the BoardInfo (candidate/identity/health/capabilities/unavailable,
    # and ``claim`` on a Linux harness only).
    data = {"candidate": info.candidate, "identity": info.identity, "health": info.health,
            "capabilities": info.capabilities, "unavailable": info.unavailable}
    if claim is not None:
        data["claim"] = claim
    ctx.emit(Result("info", data, rows=[row], human=human))
    return ExitCode.OK


# --- attach / detach ----------------------------------------------------------------------


def _owner_json(owner: LockOwner | None) -> dict | None:
    if owner is None:
        return None
    return {"user": owner.user, "host": owner.host, "pid": owner.pid, "since": owner.since,
            "note": owner.note, "text": owner.describe()}


def cmd_attach(ctx: Ctx) -> int:
    a = ctx.args
    note = hold_note("attach" + (f": {a.note}" if a.note else ""))
    with ctx.board(note=note) as (cand, _session):
        lock = ctx.lock(cand.board_id)
        owner = ctx.owner(cand.board_id)
        holder = owner.describe() if owner else "this process"
        ctx.emit(Result("attach", {
            "board_id": cand.board_id, "state": "attached", "holder": _owner_json(owner),
            "lock": str(lock.path), "hold_s": a.for_s,
        }, rows=[[cand.board_id, "attached", holder, str(lock.path)]], human=[
            f"attached   {cand.board_id}",
            f"holder     {holder}",
        ]))
        if a.for_s is None:
            ctx.note(f"holding {cand.board_id}; Ctrl-C or `harness-manager detach {a.target}` "
                     "releases it")
        why = hold(a.for_s)
    ctx.note(f"released {cand.board_id} ({why})")
    return ExitCode.OK


def _take_over_stale(ctx: Ctx, board_id: str) -> bool:
    """Take a stale lock over and release it (SessionLock's own rule). False if its
    holder is still alive."""
    lock = ctx.lock(board_id)
    try:
        lock.acquire()
    except HeldError:
        return False
    lock.release()
    return True


def _wait_released(ctx: Ctx, board_id: str, pid: int, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        owner = ctx.owner(board_id)
        if owner is None or owner.pid != pid:
            return True
        # Windows ends the holder without running its handler: the lock goes stale.
        if _take_over_stale(ctx, board_id):
            return True
        time.sleep(0.05)
    return False


def cmd_detach(ctx: Ctx) -> int:
    """Release the hold of your own foreground ``harness-manager`` verb (attach, console,
    debug up) by signalling it, or clear a stale lock. Never anyone else's lock."""
    cand = ctx.candidate()
    owner = ctx.owner(cand.board_id)
    if owner is None:
        raise AlreadyError(f"{cand.board_id} is not attached", hint="nothing to release")
    if owner.pid == os.getpid() and owner.host == socket.gethostname():
        raise AlreadyError("this process holds the lock itself", hint="nothing to release")
    if is_my_hold(owner):
        try:
            os.kill(owner.pid, signal.SIGTERM)
        except OSError:
            pass                  # already gone: the wait below clears the stale lock
        if not _wait_released(ctx, cand.board_id, owner.pid, ctx.args.timeout):
            raise ActionFailedError(
                f"`harness-manager` (pid {owner.pid}) did not release {cand.board_id} "
                f"within {ctx.args.timeout:g}s",
                hint=f"stop pid {owner.pid} by hand, then run detach again")
        state = "released"
    elif _take_over_stale(ctx, cand.board_id):
        state = "stale-removed"
    else:
        why = ("it is held by another of your programs, not a foreground `harness-manager` verb"
               if is_mine(owner) else "only the holder can release it")
        raise HeldError(f"{cand.board_id} is held by {owner.describe()}; {why}",
                        holder=owner.describe(),
                        hint="close it in that program" if is_mine(owner) else "ask the holder")
    ctx.emit(Result("detach", {"board_id": cand.board_id, "state": state,
                               "holder": _owner_json(owner)},
                    rows=[[cand.board_id, state, owner.describe()]],
                    human=[f"{state:<10} {cand.board_id} (was {owner.describe()})"]))
    return ExitCode.OK


# --- telemetry ----------------------------------------------------------------------------


def cmd_telemetry(ctx: Ctx) -> int:
    with ctx.board() as (cand, session):
        readings = list(ctx.engine.telemetry.readings(session))
    now = time.time()
    ctx.emit(Result("telemetry", {
        "board_id": cand.board_id, "readings": [reading_json(r, now) for r in readings],
    }, rows=[reading_row(cand.board_id, r, now) for r in readings],
        human=[reading_human(r) for r in readings] or ["no readings"]))
    return ExitCode.OK
