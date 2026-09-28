"""``harness-manager board identity``: the board's label, IP and MAC against its hub entry and
the other boards, and the fix (lane BOARD-ID; docs/design/BOARD_IDENTITY.md).

Verbs::

    harness-manager board identity TARGET                  what the board reports, the hub's
                                                            record, the differences and clashes
    harness-manager board identity TARGET --from-hub       make the board match its hub entry
    harness-manager board identity TARGET --label L [--ip A/N] [--mac M] [--hostname H]
    harness-manager board identity TARGET --clear          back to the stage0 bake

A change needs the typed phrase (the new label, else ``IDENTITY <board_id>``; ``--consent``
gives it; ``--yes`` never does), the lease (behind a hub), the board claimed by this Harness
Manager, and no card job running. It is set, the harness restarts WARM (its ``reboot`` verb;
never an MCC REBOOT), and the identity is read back. A netbooted board (no user microSD) and
bare metal are refused with the reason.

Exit codes: 0 read or done; 4 the lease, or a card job; 6 set but not verified; 12 bare metal,
or an image without the identity verbs; 15 refused (the phrase, the claim, a netboot).

``cmd_claim.register`` adds it to the ``board`` group.
"""

from __future__ import annotations

import argparse
import sys
from typing import Any

from harness_manager.core.errors import ExitCode, RefusedError, UnavailableError
from harness_manager.services import board_identity as BI

from .context import Ctx
from .output import Result

TSV = ("BOARD_ID", "STATUS", "LABEL", "IP", "MAC", "HUB_TARGET", "HUB_LABEL", "HUB_IP", "HUB_MAC",
       "FINDINGS", "ACTION")


def add_parser(sub: Any, parents: list[argparse.ArgumentParser]) -> argparse.ArgumentParser:
    ap = sub.add_parser(
        "identity", parents=parents,
        help="the board's label, IP and MAC against its hub entry and the other boards; "
             "--from-hub (or --label/--ip/--mac) fixes it (asks for a typed phrase)",
        description="Show what the board says it is (label, IP, MAC), its hub record, the "
                    "differences and any clash with another board. With --from-hub, "
                    "--label/--ip/--mac/--hostname or --clear, change it: the typed phrase, "
                    "then set, a warm restart of the harness, and a read-back.",
        epilog=f"--tsv columns: {' '.join(TSV)}")
    g = ap.add_argument_group("change it")
    g.add_argument("--from-hub", action="store_true",
                   help="make the board match its hub entry (label from the hub's board, IP, "
                        "and MAC unless the hub's looks like its own adapter)")
    g.add_argument("--label", default=None, metavar="LABEL",
                   help="the LCD label, e.g. MPS3-02 (at most 23 characters)")
    g.add_argument("--ip", default=None, metavar="A.B.C.D[/N]",
                   help="the board's address, e.g. 192.168.11.101/24 (/24 when no prefix)")
    g.add_argument("--mac", default=None, metavar="MAC",
                   help="the board's MAC, unicast and non-zero, e.g. 02:00:00:00:02:fe")
    g.add_argument("--hostname", default=None, metavar="NAME",
                   help="the board's host name (by default it follows the label)")
    g.add_argument("--clear", action="store_true",
                   help="drop the board's own setting: back to the stage0 bake, else the "
                        "image default")
    ap.add_argument("--consent", default="", metavar="PHRASE",
                    help="the typed phrase, given here instead of at the prompt (the new "
                         "label, or IDENTITY <board_id>); --yes never implies it")
    ap.add_argument("--wait", type=float, default=None, metavar="S",
                    help="how long to wait for the harness to restart (default 180 s)")
    return ap


def _service(ctx: Ctx) -> Any:
    from harness_manager.services._unavailable import is_unavailable

    svc = getattr(ctx.engine, "board_identity", None)
    if svc is None or is_unavailable(svc):
        raise UnavailableError(BI.CAPABILITY, "this engine has no board identity service "
                                              "(update harness-manager-daemon, or set "
                                              "HARNESS_MANAGER_NO_DAEMON=1)")
    return svc


def _want(a: argparse.Namespace) -> dict[str, str]:
    return {k: getattr(a, k) for k in BI.FIELDS if getattr(a, k, None)}


def _fmt(rep: dict[str, Any] | None) -> str:
    if not rep:
        return "not read"
    src = rep.get("source") or {}
    bits = [f"label {rep.get('label') or '?'}", f"ip {rep.get('ip') or '?'}",
            f"mac {rep.get('mac') or '?'}"]
    if rep.get("hostname"):
        bits.append(f"hostname {rep['hostname']}")
    tail = f"via {rep.get('via') or '?'}"
    if src:
        tail += "; source " + ", ".join(f"{k} {v}" for k, v in sorted(src.items()))
    if rep.get("persist") is False:
        tail += "; no persistent store (netboot)"
    if rep.get("last_check"):
        tail += f"; last check {rep.get('at')}"
    return "  ".join(bits) + f"  ({tail})"


def human(board_id: str, st: dict[str, Any] | None) -> list[str]:
    if not st:
        return [f"identity   {board_id}: no Ethernet harness to ask"]
    lines = [f"identity   {board_id}: {str(st.get('status') or 'unknown').upper()}",
             f"reported   {_fmt(st.get('reported'))}"]
    hub = st.get("hub")
    if hub:
        lines.append(f"hub        {hub.get('target')}: label {hub.get('label') or '?'}  ip "
                     f"{hub.get('board_ip') or '?'}  mac {hub.get('board_mac') or '?'}  "
                     f"hostname {hub.get('hostname') or '?'}")
    else:
        lines.append("hub        no hub record (no hub, or not read yet)")
    lines += [f"{f['kind']:<10} {f['text']}" for f in st.get("findings") or ()]
    lines += [f"note       {n}" for n in (st.get("notes") or ())
              if n not in {f["text"] for f in st.get("findings") or ()}]
    fix = st.get("fix") or {}
    changes = fix.get("changes") or []
    if changes:
        what = ", ".join(f"{c['field']} {c['from'] or '-'} -> {c['to']}" for c in changes)
        ref = fix.get("refusal")
        lines.append(f"fix        {what}: " + (f"cannot: {ref['message']}" if ref else
                                                "`harness-manager board identity TARGET "
                                                "--from-hub` (asks for the typed phrase)"))
    return lines


def row(board_id: str, st: dict[str, Any] | None, action: str = "") -> list[Any]:
    st = st or {}
    rep = st.get("reported") or {}
    hub = st.get("hub") or {}
    return [board_id, st.get("status") or "unknown", rep.get("label", ""), rep.get("ip", ""),
            rep.get("mac", ""), hub.get("target", ""), hub.get("label", ""),
            hub.get("board_ip", ""), hub.get("board_mac", ""),
            ";".join(f["kind"] for f in st.get("findings") or ()), action]


def _phrase(ctx: Ctx, phrase: str, question: str) -> str:
    given = str(getattr(ctx.args, "consent", "") or "")
    if not given:
        stream = ctx.err or sys.stderr
        stream.write(f"{question}\nTo go ahead, type exactly: {phrase}\n> ")
        stream.flush()
        try:
            given = sys.stdin.readline()
        except (OSError, ValueError):
            given = ""
    if given.strip() != phrase:
        raise RefusedError("the identity change was not confirmed: nothing was changed",
                           hint=f"type exactly: {phrase} (--consent PHRASE; --yes never implies it)")
    return given.strip()


def cmd_identity(ctx: Ctx) -> int:
    a = ctx.args
    want = _want(a)
    change = bool(a.from_hub or want or a.clear)
    if a.clear and (a.from_hub or want):
        from harness_manager.core.errors import UsageError

        raise UsageError("--clear goes alone", hint="clear first, then set what you want")
    with ctx.board(note="board identity") as (cand, session):
        svc = _service(ctx)
        st = svc.status(session, refresh=True)
        if not change:
            ctx.emit(Result("board identity", {"board_id": cand.board_id, "identity": st},
                            rows=[row(cand.board_id, st)], human=human(cand.board_id, st)))
            return ExitCode.OK
        if st is None:
            raise UnavailableError(BI.CAPABILITY, BI.NO_ADAPTER)
        plan = BI.plan_fix(cand.board_id, st.get("reported"), st.get("hub"), want=want,
                           from_hub=a.from_hub, clear=a.clear)
        if not plan["changes"] or (not a.clear and not plan["want"]):
            ctx.emit(Result("board identity", {"board_id": cand.board_id, "identity": st,
                                               "action": "none", "notes": plan["notes"]},
                            rows=[row(cand.board_id, st, "none")],
                            human=[*human(cand.board_id, st),
                                   "result     the board already matches: nothing to change",
                                   *(f"note       {n}" for n in plan["notes"])]))
            return ExitCode.OK
        ref = (st.get("fix") or {}).get("refusal")
        if ref:                                   # before the question: a refusal after it is rude
            raise BI.refusal_error(ref["name"], ref["message"], ref.get("hint") or "")
        what = ", ".join(f"{c['field']} {c['from'] or '-'} -> {c['to']}" for c in plan["changes"])
        consent = _phrase(ctx, plan["phrase"],
                          f"change the identity of {cand.board_id}: {what}? The harness then "
                          "restarts (its reboot verb, warm: the FPGA is not reloaded) and the "
                          "identity is read back.")
        out = svc.fix(session, confirm=consent, want=want or None, from_hub=a.from_hub,
                      clear=a.clear, wait_s=a.wait, progress=ctx.note)
    after = out.get("identity")
    ctx.emit(Result("board identity", {"board_id": cand.board_id, **out},
                    rows=[row(cand.board_id, after, out.get("action", ""))],
                    human=[f"changed    {what}",
                           f"reboot     {(out.get('reboot') or {}).get('summary') or 'done'}",
                           f"verified   {'yes' if out.get('verified') else 'NO'}",
                           *human(cand.board_id, after)[1:],
                           *(f"note       {n}" for n in out.get("notes") or ())]))
    return ExitCode.OK
