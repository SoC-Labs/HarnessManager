"""Lab verbs: vPHY link events, CLCD owner flip, MAC traffic generator, DUT frames.

These are MPS3-shell verbs with no board-agnostic service yet, so they run
pyverify ``ShellClient`` verbs through the session's shell handle
(``session.shell.call(fn)``, which opens 6900, runs ``fn``, closes, and maps
failures to exit-coded errors). This is the only CLI module that touches the
shell or reads anything from pyverify (its published closed sets).

A shell that declines a verb because its bitstream lacks the block
(``clcd_kvm not present``, ``dut_egress not present``) is an UNAVAILABLE
capability (exit 12), never an empty-looking success.
"""

from __future__ import annotations

from typing import Any

from harness_manager.core.errors import ActionFailedError, ExitCode, UnavailableError, UsageError

from .context import Ctx
from .output import Result, with_data

# Pack-prefixed capability names (core/capabilities.py: packs add their own as "<pack>.<name>").
CAP_SHELL = "shell"
CAP_DISPLAY = "display_flip"
CAP_DUT_EGRESS = "dut_egress"
DISPLAY_TIMEOUT_S = 2.0


def _cap(ctx: Ctx, name: str) -> str:
    return f"{ctx.pack}.{name}"


def _shell(ctx: Ctx, session: Any) -> Any:
    shell = getattr(session, "shell", None)
    if shell is not None and callable(getattr(shell, "call", None)):
        return shell
    if hasattr(session, "shell"):
        reason = "needs the Ethernet link to the shell (this TARGET is USB-only)"
    else:
        reason = f"the {session.candidate.pack} pack has no shell control channel"
    raise UnavailableError(_cap(ctx, CAP_SHELL), reason)


def _declined(capability: str, err: str, what: str) -> Exception:
    if "not present" in err:
        return UnavailableError(capability, f"the loaded shell bitstream has no {what} ({err})")
    return ActionFailedError(f"the shell declined: {err or 'no reason given'}",
                             hint="check `harness-manager info TARGET`")


def _known(name: str) -> tuple[str, ...] | None:
    """A closed set published by the shell codec (pyverify), or None if it has none."""
    try:
        from pyverify import client
    except ImportError:
        return None
    value = getattr(client, name, None)
    return tuple(value) if value else None


def cmd_lab(ctx: Ctx) -> int:
    action = ctx.args.lab_cmd
    with ctx.board() as (cand, session):
        shell = _shell(ctx, session)
        return {"link": _link, "display": _display, "macgen": _macgen, "dutrx": _dutrx}[action](
            ctx, cand.board_id, shell)


def _link(ctx: Ctx, board_id: str, shell: Any) -> int:
    event = ctx.args.event
    resp = shell.call(lambda c: c.link(event))
    if not resp.ok:
        raise ActionFailedError(f"the shell refused link event {event!r}",
                                hint="events: up, down, pulse")
    ctx.emit(Result("lab link", {"board_id": board_id, "event": event, "result": "done"},
                    rows=[[board_id, event, "done"]],
                    human=[f"link       {event} on {board_id}: done"]))
    return ExitCode.OK


def _display(ctx: Ctx, board_id: str, shell: Any) -> int:
    owner = ctx.args.owner
    if owner == "query":
        resp = shell.call(lambda c: c.display_owner())
        if not resp.ok:
            raise _declined(_cap(ctx, CAP_DISPLAY), resp.err, "CLCD KVM")
        data = {"board_id": board_id, "requested": "query", "owner": resp.owner,
                "landed": None}
        ctx.emit(Result("lab display", data, rows=[[board_id, "query", resp.owner, None]],
                        human=[f"display    owned by {resp.owner}"]))
        return ExitCode.OK
    timeout = ctx.args.timeout
    resp = shell.call(lambda c: c.display_settled(owner, timeout=timeout))
    if not resp.ok:
        raise _declined(_cap(ctx, CAP_DISPLAY), resp.err, "CLCD KVM")
    data = {"board_id": board_id, "requested": resp.requested, "owner": resp.owner,
            "landed": resp.landed, "polls": resp.polls, "waited_s": round(resp.waited_s, 3)}
    if not resp.landed:
        raise with_data(ActionFailedError(
            f"INCONCLUSIVE: asked for {resp.requested}, but the panel was still owned by "
            f"{resp.owner or '?'} after {resp.waited_s:.2f}s",
            hint="re-check with `harness-manager lab TARGET display query`"), **data)
    ctx.emit(Result("lab display", data,
                    rows=[[board_id, resp.requested, resp.owner, resp.landed]],
                    human=[f"display    now owned by {resp.owner} ({resp.polls} polls, "
                           f"{resp.waited_s:.2f}s)"]))
    return ExitCode.OK


def _macgen(ctx: Ctx, board_id: str, shell: Any) -> int:
    a = ctx.args
    known = _known("MACGEN_INJECTS")
    if known is not None and a.inject not in known:
        raise UsageError(f"unknown inject {a.inject!r}", hint=f"known: {', '.join(known)}")
    resp = shell.call(lambda c: c.macgen(gen=a.gen, chk=a.chk, inject=a.inject))
    if not resp.ok:
        raise ActionFailedError("the shell refused the macgen request",
                                hint="check `harness-manager info TARGET` (the gen/checker block)")
    data = {"board_id": board_id, "gen": a.gen, "chk": a.chk, "inject": a.inject,
            "tx": resp.tx, "rx": resp.rx, "err": resp.err}
    ctx.emit(Result("lab macgen", data, rows=[[board_id, resp.tx, resp.rx, resp.err]],
                    human=[f"macgen     tx {resp.tx}  rx {resp.rx}  err {resp.err}"]))
    return ExitCode.OK


def _dutrx(ctx: Ctx, board_id: str, shell: Any) -> int:
    want = ctx.args.frames
    if want < 1:
        raise UsageError("--frames must be at least 1", hint="e.g. --frames 4")

    def read(c: Any) -> list:
        out = []
        for _ in range(want):
            frame, last = c.read_dut_frame()
            out.append((frame, last))
            if frame is None:
                break
        return out

    got = shell.call(read)
    last = got[-1][1]
    if not last.ok:
        raise _declined(_cap(ctx, CAP_DUT_EGRESS), last.err, "DUT egress capture")
    frames = [f for f, _ in got if f is not None]
    counters = {"frames_waiting": last.frames, "rx": last.rx, "drop_full": last.drop_full,
                "drop_giant": last.drop_giant, "ovf": last.ovf, "desync": last.desync}
    data = {"board_id": board_id, "frames": [{"len": len(f), "data": f.hex()} for f in frames],
            **counters}

    def row(f: bytes | None) -> list:
        return [board_id, len(f) if f else 0, last.frames, last.rx, last.drop_full,
                last.drop_giant, last.ovf, last.desync, f.hex() if f else ""]

    human = [f"frame      {len(f)} bytes: {f.hex()}" for f in frames] or ["no frame waiting"]
    human.append(f"counters   rx {last.rx}  drop_full {last.drop_full}  "
                 f"drop_giant {last.drop_giant}  waiting {last.frames}"
                 f"{'  OVF' if last.ovf else ''}{'  DESYNC' if last.desync else ''}")
    ctx.emit(Result("lab dutrx", data, rows=[row(f) for f in frames] or [row(None)],
                    human=human))
    return ExitCode.OK
