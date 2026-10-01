"""``harness-manager board``: the Linux harness's SSH claim and its SSH reach (lane LINUX-CLAIM).

Verbs::

    harness-manager board claim        TARGET [--key PUB] [--adopt] [--replace-host-key] [--yes]
    harness-manager board claim-status TARGET        ask the board now (through the hub too)
    harness-manager board ssh          TARGET [--print] [-c CMD]
    harness-manager board identity     TARGET [--from-hub | --label/--ip/--mac | --clear]

**claim** gives your SSH key root on a Linux harness that is still unclaimed (TOFU: the
first key wins, and the board refuses every later one), then pins the board's host key in
boards.toml ``boards.<b>.ssh.host_key``. It asks first (``--yes`` skips the question), needs
the lease on a board behind a hub, and never happens by itself. ``--adopt`` pins a board you
already claimed elsewhere (pyverify ``claim``, the B1 runbook) after proving your key logs
in. ``--replace-host-key`` is for a board that was re-provisioned (a new card or image): a
host key that differs from the pin is otherwise refused, loudly.

Once claimed, the board refuses slot changes from anyone but itself (S12); Harness Manager
reaches it as ``ssh -J HUB root@BOARD`` with the pinned key, and ``board ssh`` is that
command. Bare metal has no SSH: ``claim`` exits 12.

Exit codes: 0 done; 4 not the lease holder; 7 the board or the hub did not answer; 8 already
claimed (never taken over); 12 no SSH to claim (bare metal); 15 not confirmed, the host key
changed, or your key does not log in.

``cli/main.py`` registers it with ``cmd_claim.register(sub)``.
"""

from __future__ import annotations

import argparse
import os
import shlex
import subprocess
import sys
from pathlib import Path
from typing import Any

from harness_manager.core.errors import ExitCode, UnavailableError, UsageError

from .context import VIA_HELP, VIA_METAVAR, Ctx
from .output import TSV_COLUMNS, Result

CLAIM_TSV: dict[str, tuple[str, ...]] = {
    "board claim": ("BOARD_ID", "STATE", "BY", "KEY_FP", "AT", "HOST_KEY", "PINNED", "ROUTE",
                    "ACTION"),
    "board ssh": ("BOARD_ID", "ARGV"),
}
TARGET_HELP = "shell address host[:port] (the board's harness)"
CAPABILITY = "ssh_claim"


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
    p.add_argument("--via", metavar=VIA_METAVAR, default=argparse.SUPPRESS, help=VIA_HELP)
    return p


def register(subparsers: Any) -> argparse.ArgumentParser:
    """Add ``board`` and its actions to the top-level subparsers. Returns the parser."""
    for layout, cols in CLAIM_TSV.items():
        TSV_COLUMNS.setdefault(layout, cols)
    fmt, board = _fmt(), _board()
    vp = subparsers.add_parser(
        "board", help="the board's SSH and identity: claim, claim-status, ssh, identity "
                      "(label, IP and MAC against its hub entry, and the fix)",
        description="The Linux harness's SSH claim (TOFU) and its SSH reach, and the board's "
                    "identity. A claim gives your key root on the board, pins its host key, "
                    "and from then on the board takes slot changes only over that SSH.",
        parents=[fmt])
    sub = vp.add_subparsers(dest="board_cmd", required=True, metavar="ACTION")
    cols = " ".join(CLAIM_TSV["board claim"])
    ap = sub.add_parser("claim", help="claim an unclaimed Linux harness with your SSH key "
                                      "and pin its host key (asks first; needs the lease)",
                        parents=[fmt, board], epilog=f"--tsv columns: {cols}")
    ap.add_argument("--key", default=None, metavar="PUB",
                    help="the PUBLIC key to claim with (default: boards.<b>.ssh.key + .pub, "
                         "else ~/.ssh/id_ed25519.pub)")
    ap.add_argument("--adopt", action="store_true",
                    help="the board is already claimed with your key (pyverify claim, another "
                         "machine): prove it logs in and pin the host key")
    ap.add_argument("--replace-host-key", action="store_true",
                    help="the board was re-provisioned: accept a host key that differs from "
                         "the pinned one (still checked against the board's identify)")
    ap.add_argument("--yes", action="store_true", help="do not ask")
    sub.add_parser("claim-status", help="is the board claimed, and by this Harness Manager's "
                                        "key? (asks the board now, through the hub if needed)",
                   parents=[fmt, board], epilog=f"--tsv columns: {cols}")
    sp = sub.add_parser("ssh", help="ssh into the Linux harness as root, with the pinned host "
                                    "key (through the hub when the board is behind one)",
                        parents=[fmt, board],
                        epilog=f"--tsv columns: {' '.join(CLAIM_TSV['board ssh'])}")
    sp.add_argument("--print", dest="print_only", action="store_true",
                    help="print the ssh command line, do not run it")
    sp.add_argument("-c", "--command", default="", metavar="CMD",
                    help="run this on the board instead of a shell, as ONE command line for "
                         "the board's shell, the way `ssh host 'CMD'` sends it (board ssh "
                         "TARGET -c 'uptime; logread | grep harnessd')")
    from . import cmd_identity  # BOARD-ID: label/IP/MAC and the fix

    cmd_identity.add_parser(sub, [fmt, board])
    TSV_COLUMNS.setdefault("board identity", cmd_identity.TSV)
    vp.set_defaults(fn=cmd_board)
    return vp


def _service(ctx: Ctx) -> Any:
    svc = getattr(ctx.engine, "board_claim", None)
    if svc is None:
        raise UnavailableError(CAPABILITY, "this engine has no SSH claim service (update "
                                           "harness-manager-daemon, or set "
                                           "HARNESS_MANAGER_NO_DAEMON=1)")
    return svc


def cmd_board(ctx: Ctx) -> int:
    from .cmd_identity import cmd_identity

    return {"claim": _claim, "claim-status": _claim_status, "ssh": _ssh,
            "identity": cmd_identity}[ctx.args.board_cmd](ctx)


# --- the view ---------------------------------------------------------------------------------


def describe(st: dict[str, Any] | None) -> str:
    """One line: what ``info`` and ``board claim-status`` say about the claim."""
    if not st:
        return "no SSH to claim (bare metal)"
    state = st.get("state")
    claimed = st.get("claimed") or {}
    checked = "" if st.get("live") else f" [{st.get('source') or 'not checked'}, {st.get('checked_at')}]"
    if state == "mine":
        bits = [b for b in (claimed.get("key_fp"), claimed.get("by"), claimed.get("at")) if b]
        return f"claimed by you ({', '.join(bits)}){checked}"
    if state == "other":
        return f"claimed by another key (not this Harness Manager's){checked}"
    if state == "unclaimed":
        return f"unclaimed: `harness-manager board claim TARGET` claims it with your key{checked}"
    return f"unknown{checked}"


def claim_row(board_id: str, st: dict[str, Any] | None) -> list[Any]:
    st = st or {}
    claimed = st.get("claimed") or {}
    hk = st.get("host_key") or {}
    return [board_id, st.get("state") or "none", claimed.get("by") or "",
            claimed.get("key_fp") or "", claimed.get("at") or "", hk.get("reported") or "",
            hk.get("pinned") or "", st.get("route") or "", st.get("action") or ""]


def claim_human(board_id: str, st: dict[str, Any] | None) -> list[str]:
    lines = [f"claim      {board_id}: {describe(st)}"]
    if not st:
        return lines
    hk = st.get("host_key") or {}
    pinned, reported = hk.get("pinned"), hk.get("reported")
    if pinned and hk.get("match") is False and hk.get("seen_before"):
        lines.append(f"host key   changed back: pinned {pinned}, the board reports {reported}, "
                     f"a key pinned here before (last seen {str(hk['seen_before'])[:10]})")
    elif pinned and hk.get("match") is False:
        lines.append(f"host key   CHANGED: pinned {pinned}, the board reports {reported}")
    elif pinned:
        lines.append(f"host key   {pinned} pinned"
                     + (f" (boards.toml boards.{st['table']}.ssh.host_key)" if st.get("table") else ""))
    elif reported:
        lines.append(f"host key   {reported} (the board's; not pinned)")
    if st.get("claim_key"):                  # identify ssh.key_sha256 (C1), when published
        lines.append(f"claim key  {st['claim_key']} (the board's first claimed key)")
    lines.append(f"route      {st.get('route')}  user {st.get('user')}")
    lines += [f"note       {n}" for n in st.get("notes") or ()]
    return lines


def _emit(ctx: Ctx, layout: str, board_id: str, st: dict[str, Any] | None) -> None:
    ctx.emit(Result(layout, {"board_id": board_id, "claim": st},
                    rows=[claim_row(board_id, st)], human=claim_human(board_id, st)))


# --- the actions -------------------------------------------------------------------------------


def _key_fingerprint(path: str) -> str:
    """The claim key's fingerprint, for the question (read locally; the adapter re-checks)."""
    import base64
    import hashlib

    try:
        first = next(ln for ln in Path(os.path.expanduser(path)).read_text("ascii").splitlines()
                     if ln.strip() and not ln.lstrip().startswith("#"))
        blob = base64.b64decode(first.split()[1], validate=True)
    except (OSError, StopIteration, IndexError, ValueError):
        return ""
    return "SHA256:" + base64.b64encode(hashlib.sha256(blob).digest()).decode().rstrip("=")


def _claim(ctx: Ctx) -> int:
    a = ctx.args
    with ctx.board(note="board claim") as (cand, session):
        svc = _service(ctx)
        # Before the question: a refusal after "yes" is rude (the daemon's route re-checks).
        for name in ("check_claimable", "check_lease"):
            check = getattr(svc, name, None)
            if callable(check):
                check(session)
        if a.adopt:
            question = (f"adopt the SSH claim of {cand.board_id}? Harness Manager logs in with "
                        "your key, checks the board's host key against its identify and pins "
                        "it in boards.toml")
        else:
            fp = _key_fingerprint(a.key) if a.key else ""
            question = (f"claim {cand.board_id} with {'key ' + fp if fp else 'your SSH key'}"
                        f"{' (' + a.key + ')' if a.key else ''}? That key gets root on the "
                        "board, the board refuses every later claim, and its slot changes "
                        "then need that key (undo: mps3-unclaim on the serial console)")
        ctx.confirm(question)
        st = svc.claim(session, confirm=True, key=a.key, adopt=bool(a.adopt),
                       replace_host_key=bool(a.replace_host_key), progress=ctx.note)
    _emit(ctx, "board claim", cand.board_id, st)
    return ExitCode.OK


def _claim_status(ctx: Ctx) -> int:
    with ctx.board(note="board claim-status") as (cand, session):
        st = _service(ctx).refresh(session)
    _emit(ctx, "board claim", cand.board_id, st)
    return ExitCode.OK


def remote_command(command: str) -> list[str]:
    """``-c CMD`` as ssh's command: ONE argument, the whole line, as ``ssh host 'CMD'`` sends
    it (FIX-PACK-6, H1). ssh joins its command arguments with spaces and the board's shell
    parses the result, so a line split into words here lost its quoting there:
    ``-c "logread | grep -E 'a|b'"`` reached the board as ``logread | grep -E a|b``. One
    argument reaches the board's shell exactly as typed, on POSIX and on Windows (where
    ``subprocess`` quotes it back into one argument of ssh.exe's command line)."""
    return [command] if command else []


def command_line(argv: list[str]) -> str:
    """``argv`` as one line for this host's shell (``cmd.exe`` rules on Windows)."""
    return subprocess.list2cmdline(argv) if os.name == "nt" else shlex.join(argv)


def run_ssh(argv: list[str]) -> int:
    """Run ssh in the foreground (a seam: tests record the argv instead)."""
    return subprocess.call(argv, stdin=sys.stdin, stdout=sys.stdout, stderr=sys.stderr)


def _ssh(ctx: Ctx) -> int:
    a = ctx.args
    command = remote_command(a.command)
    with ctx.board(note="board ssh") as (cand, session):
        argv = _service(ctx).ssh_argv(session, command, tty=not command and not a.print_only)
        text = command_line(argv)
        if a.print_only:
            ctx.emit(Result("board ssh", {"board_id": cand.board_id, "argv": argv},
                            rows=[[cand.board_id, text]], human=[text]))
            return ExitCode.OK
        if ctx.fmt != "human":
            raise UsageError("board ssh runs an interactive ssh; --json/--tsv go with --print")
        ctx.note(f"ssh: {text}")
        # The board stays open (its hub tunnel and lock) for as long as ssh runs.
        rc = run_ssh(argv)
    return ExitCode.OK if rc == 0 else ExitCode.ACTION_FAILED
