"""``harness-manager lease`` and ``harness-manager share``: the lab hub from a terminal (lanes L1, LR-C).

::

    harness-manager lease show TARGET
    harness-manager lease acquire TARGET [--ttl S] [--holder NAME] [--timeout S]
    harness-manager lease release TARGET
    harness-manager lease request TARGET [--message M] [--ttl S]
    harness-manager lease requests TARGET
    harness-manager lease respond TARGET ID --release | --keep MINUTES [--message M]
    harness-manager lease force TARGET [--yes] [--confirm-board NAME]
    harness-manager lease leave TARGET
    harness-manager lease dismiss TARGET          # forget the last forced release
    harness-manager share list TARGET
    harness-manager share start TARGET NAME      # a lane share name from boards.toml or a /dev path (never tty_00)

TARGET is the board, as every other verb takes it (``192.168.10.101``); its
boards.toml ``hub`` table names the hub and the fpgahub target. These verbs
talk to the hub, never to the board, so they work while the Harness Manager
service has the board open: the service and the CLI keep the lease token in
the same place (``<state_dir>/leases``), and the service heartbeats a lease the
CLI took as soon as the board is open there.

Which hub name is shown (LEASE-BOARD, docs/HUB_MODE.md "Boards and targets"): the
lease is taken on the fpgahub TARGET (``mps3_01_pl``, pyverify's name, so HM and
the platform's scripts share one lease and one queue), and the output names the
physical BOARD it belongs to (``mps3_01``) with the target as a detail:
``mps3-01 (mps3_01 on HUB, target mps3_01_pl)``. JSON adds ``board`` beside
``target``; the ``lease`` TSV appends a BOARD column.

``lease acquire`` blocks until the hub grants the lease (it may queue; the
queue place is kept). Ctrl-C while queued removes the queue entry. There is no
``share stop``: fpgahub stops EVERY share on the board with it.

Lease requests (docs/LEASE_REQUESTS.md, lane LR-C): ``lease request`` joins the
queue AND asks the holder's session to give the board up, then waits, showing
the 2:00 countdown and the answer. A "keep" answer does not end the wait (D1): it
prints the answer and counts down to when force-release can reopen. With no
answer by the deadline (or a keep that ran out), and at the head of the queue,
``lease force`` revokes the holder's lease (it asks first; without a terminal it
needs ``--yes``). When no Harness Manager session answered the request, the holder may
be a script (a soak or runner; docs/LEASE_REQUESTS.md D12): then it asks for the
board's name to be typed instead, and a script passes ``--confirm-board NAME`` (``--yes``
is not enough). ``lease leave`` leaves the queue and withdraws the request; Ctrl-C during
``lease request`` does the same.

Exit codes: 0 when the verb did what it says (``request``/``force``: the board is
yours); 6 ACTION_FAILED when a waiting ``request`` ends without the board (it left
the queue from elsewhere); 8 ALREADY when the lease is already yours; 12
UNAVAILABLE when force-release is not open yet (the holder still has time to
answer); 15 REFUSED when it is not available at all, or the prompt was not
confirmed.

The lead wires ``register(sub)`` into ``cli/main.py`` (CCR L1-2).
"""

from __future__ import annotations

import argparse
import re
import sys
import threading
import time
from datetime import datetime, timezone
from typing import Any

from harness_manager import naming
from harness_manager.core.errors import (
    AbsentError,
    ActionFailedError,
    AlreadyError,
    ExitCode,
    HarnessError,
    RefusedError,
    UnavailableError,
    UsageError,
)
from harness_manager.services.lease import (
    DEFAULT_REQUEST_TTL_S,
    DEFAULT_TTL_S,
    HOLDER_HM,
    LeaseService,
    confirm_board_error,
    default_holder,
    hub_holder,
    lease_name,
    typed_names,
    view_confirm_error,
)

from .context import Ctx
from .output import TSV_COLUMNS, Result

#: The TSV layouts are in ``output.TSV_COLUMNS`` (append-only). ``lease``: L1's six
#: columns, then the queue length, my place in it, my request, its answer (``keep:15`` /
#: ``release``), whether force-release is open, the incoming requests (for my lease),
#: who force-released my lease last, and (LEASE-BOARD) the physical board, "" when unknown.
LEASE_COLUMNS = TSV_COLUMNS["lease"]

# --- lease-request rules shared with the daemon (daemon/hub_api.py imports these) ----------

#: docs/LEASE_REQUESTS.md: the holder has this long to answer.
REQUEST_WINDOW_S = 120
#: The "keep it" answers, in minutes.
KEEP_MINUTES = (5, 15, 30, 60)
#: A request or answer message. The note it rides in is at most 4 KiB on the hub.
MAX_MESSAGE = 500
#: Request ids go into hub file names (``req-<id>.json``): this alphabet only.
REQUEST_ID = re.compile(r"[A-Za-z0-9_.\-]{1,64}")
_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f]+")


def clean_message(value: Any, field: str = "message") -> str:
    """A request or answer message: a string of at most ``MAX_MESSAGE`` characters.
    Control characters (a pasted newline) become one space. None is ``""``."""
    if value is None:
        return ""
    if not isinstance(value, str):
        raise UsageError(f"{field} must be a string, not {value!r}")
    text = _CONTROL.sub(" ", value).strip()
    if len(text) > MAX_MESSAGE:
        raise UsageError(f"{field} is {len(text)} characters; the most is {MAX_MESSAGE}",
                         hint="it is shown to the other person in one line: keep it short")
    return text


def request_id(value: Any) -> str:
    if not isinstance(value, str) or not REQUEST_ID.fullmatch(value):
        raise UsageError(f"id must be a request id ({REQUEST_ID.pattern}), not {value!r}",
                         hint="`harness-manager lease requests TARGET` lists them")
    return value


def keep_minutes(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value not in KEEP_MINUTES:
        raise UsageError(f"minutes must be one of {', '.join(map(str, KEEP_MINUTES))}, "
                         f"not {value!r}", hint="\"keep\" says how long you still need the board")
    return value


def parse_iso(value: Any) -> float | None:
    """An ISO 8601 time from a hub note (``...Z`` or ``+00:00``) as epoch seconds, or None."""
    if not isinstance(value, str) or not value:
        return None
    text = value.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        stamp = datetime.fromisoformat(text)
    except ValueError:
        return None
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)     # the notes are UTC
    return stamp.timestamp()


def iso_utc(epoch: float) -> str:
    """Epoch seconds as ISO 8601 UTC with ``+00:00`` (D8)."""
    return datetime.fromtimestamp(epoch, timezone.utc).isoformat(timespec="seconds")


def fmt_left(seconds: float) -> str:
    """``95`` -> ``1:35``; never negative."""
    s = max(0, int(seconds + 0.999))
    return f"{s // 60}:{s % 60:02d}"


def request_refusal(view: dict[str, Any], target: str) -> HarnessError | None:
    """Why a request makes no sense from this lease view, or None.

    ``mine`` is by principal (docs/LEASE_REQUESTS.md "Who am I"), so it is also true when
    ANOTHER session of the same person holds the board. fpgahub keys leases on the
    principal and would hand the lease back instead of queueing (CCR-A2): refuse, before
    any queue entry or note is written.
    """
    lease = view.get("lease") or None
    if lease and lease.get("mine"):
        return AlreadyError(f"you already hold {target} (this or another Harness Manager "
                            "session of yours)",
                            hint="use it, or release it there first: `harness-manager lease "
                                 "release TARGET` on the machine that took it")
    return None


def force_refusal(view: dict[str, Any], now: float, target: str) -> HarnessError | None:
    """Why force-release is not available from this lease view, or None.

    The view's ``request.force_available`` is the lease service's answer. A view may be
    up to 10 s old (``lease show`` is cached), and availability changes by time alone at
    the deadline, so the time-based rules are checked here against ``now``: once they
    all pass, the service's ``force`` decides (it checks again before any revoke).

    - nothing to force: the lease is already mine (ALREADY), nobody holds it, or I have
      no request (REFUSED);
    - an answer: "release" (the board is on its way) or a "keep" whose minutes have not
      run out (REFUSED, with the time);
    - not at the head of the queue: a revoke hands the board to the head (REFUSED);
    - the deadline has not passed: 422 UNAVAILABLE with the time left.
    """
    lease = view.get("lease") or None
    req = view.get("request") or None
    if lease and lease.get("mine"):
        return AlreadyError(f"{target} is already yours; there is nothing to force",
                            hint="`harness-manager lease release TARGET` gives it back")
    if not req:
        return RefusedError(f"you have no request for {target}, so there is nothing to force",
                            hint="`harness-manager lease request TARGET` asks the holder first; "
                                 "force-release follows a request nobody answered")
    rid = req.get("id", "")
    if lease is None:
        return RefusedError(f"nobody holds {target}; your request gets it without a force",
                            hint="wait for `lease request` to finish")
    if req.get("force_available"):
        return None
    holder = lease.get("holder") or "the holder"
    service_reason = req.get("force_reason") or ""
    ans = req.get("answer") or None
    if ans:
        if ans.get("answer") == "release":
            return RefusedError(f"{holder} released {target}; it is on its way to you",
                                hint="wait for `lease request` to finish")
        minutes = ans.get("minutes") or 0
        at = parse_iso(ans.get("at"))
        until = None if at is None else at + 60 * minutes
        if until is None or now < until:
            said = f": {ans['message']!r}" if ans.get("message") else ""
            when = (f"; force-release opens in {fmt_left(until - now)}" if until is not None
                    else "")
            err = RefusedError(f"{holder} answered: keep {target} for {minutes} min{said}{when}",
                               hint="ask again later, or `harness-manager lease leave TARGET`")
            err.data = {"request_id": rid, "answer": ans,  # type: ignore[attr-defined]
                        "force_reason": service_reason,
                        "time_left_s": None if until is None else round(until - now)}
            return err
    pos = req.get("position")
    if isinstance(pos, bool) or not isinstance(pos, int) or pos < 1:
        return RefusedError(f"you are not in the queue for {target}",
                            hint="`harness-manager lease request TARGET` joins it again")
    if pos > 1:
        err = RefusedError(f"you are at position {pos} in the queue for {target}; only the head "
                           "can force-release it (a revoke hands the board to the head)",
                           hint="wait for the people ahead of you, or ask them")
        err.data = {"request_id": rid, "position": pos,  # type: ignore[attr-defined]
                    "force_reason": service_reason}
        return err
    deadline = parse_iso(req.get("deadline_at"))
    if deadline is None:
        err = RefusedError(f"force-release of {target} is not available: "
                           f"{service_reason or 'the request has no deadline'}")
        err.data = {"request_id": rid, "force_reason": service_reason}  # type: ignore[attr-defined]
        return err
    if now < deadline:
        err = UnavailableError("lease_force", f"{holder} has {fmt_left(deadline - now)} left to "
                                              f"answer the request (until {req['deadline_at']})")
        err.hint = ("force-release opens at the deadline if there is still no answer and "
                    "you are at the head of the queue")
        err.data = {"request_id": rid, "deadline_at": req["deadline_at"],  # type: ignore[attr-defined]
                    "time_left_s": round(deadline - now)}
        return err
    return None        # a view from before the deadline: the service decides


# --- the verbs ----------------------------------------------------------------------------


def _fmt_parent() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(add_help=False)
    g = p.add_mutually_exclusive_group()
    g.add_argument("--json", action="store_true", default=argparse.SUPPRESS,
                   help="one JSON object on stdout")
    g.add_argument("--tsv", action="store_true", default=argparse.SUPPRESS,
                   help="tab-separated rows, append-only columns")
    return p


def _cols(layout: str) -> str:
    return f"--tsv columns: {' '.join(TSV_COLUMNS[layout])}"


def register(subparsers: argparse._SubParsersAction) -> None:
    """Add ``lease`` and ``share`` to the CLI's verbs."""
    fmt = _fmt_parent()
    target_help = "the board (192.168.10.101); its boards.toml hub table names the hub"

    vp = subparsers.add_parser(
        "lease", help="the board's hub lease: show, acquire, release, request, requests, respond, "
                 "force, leave, dismiss",
        description="The hub lease that keeps two people off one shared board. It needs a hub "
                    "(`harness-manager hub`), and nothing takes it for you: `acquire` here, or "
                    "Acquire lease in the app. The Harness "
                    "Manager service heartbeats it while the board is open there. `request` "
                    "asks the holder to give it up; with no answer in 2:00, `force` takes it.",
        parents=[fmt], epilog=_cols("lease"))
    lsub = vp.add_subparsers(dest="lease_cmd", required=True, metavar="ACTION")
    sp = lsub.add_parser("show", help="who holds it, until when; the queue and the requests",
                         parents=[fmt], epilog=_cols("lease"))
    sp.add_argument("target", metavar="TARGET", help=target_help)
    sp = lsub.add_parser("acquire", help="take it; waits in the queue if someone holds it",
                         parents=[fmt], epilog=_cols("lease"))
    sp.add_argument("target", metavar="TARGET", help=target_help)
    sp.add_argument("--ttl", type=int, default=None, metavar="S",
                    help=f"lease length in seconds (default: the hub's lease_ttl, else "
                         f"{DEFAULT_TTL_S}); the service extends it while the board is open")
    sp.add_argument("--holder", default=None, metavar="NAME",
                    help=f"holder name (default: the hub's holder, else {default_holder()})")
    sp.add_argument("--timeout", type=float, default=None, metavar="S",
                    help="give up waiting in the queue after this long (the entry is removed; "
                         "default: the hub's queue_timeout, else 3600)")
    sp = lsub.add_parser("release", help="give it back (only a lease this Harness Manager took)",
                         parents=[fmt], epilog=_cols("lease"))
    sp.add_argument("target", metavar="TARGET", help=target_help)

    sp = lsub.add_parser(
        "request", help="ask the holder to give it up; waits, with the 2:00 countdown",
        description="Join the queue and ask the holder's Harness Manager to release the board. "
                    "Waits until it is yours (exit 0) or the holder answers 'keep' (exit 4). "
                    "With no answer in 2:00 and you at the head of the queue, "
                    "`harness-manager lease force TARGET` takes it. Ctrl-C leaves the queue.",
        parents=[fmt], epilog=_cols("lease"))
    sp.add_argument("target", metavar="TARGET", help=target_help)
    sp.add_argument("--message", default="", metavar="M",
                    help=f"why you need it, shown to the holder (at most {MAX_MESSAGE} characters)")
    sp.add_argument("--ttl", type=int, default=None, metavar="S",
                    help="lease length in seconds once it is yours (default: the hub's "
                         f"request_ttl, else {DEFAULT_REQUEST_TTL_S})")

    sp = lsub.add_parser("requests", help="the requests waiting for your answer",
                         parents=[fmt], epilog=_cols("lease requests"))
    sp.add_argument("target", metavar="TARGET", help=target_help)

    sp = lsub.add_parser("respond", help="answer a request: release now, or keep it N minutes",
                         parents=[fmt], epilog=_cols("lease respond"))
    sp.add_argument("target", metavar="TARGET", help=target_help)
    sp.add_argument("id", metavar="ID", help="the request id (`lease requests TARGET`)")
    g = sp.add_mutually_exclusive_group(required=True)
    g.add_argument("--release", action="store_true", help="release the board to them now")
    g.add_argument("--keep", type=int, choices=KEEP_MINUTES, metavar="MINUTES",
                   help=f"keep it this long ({', '.join(map(str, KEEP_MINUTES))} minutes)")
    sp.add_argument("--message", default="", metavar="M", help="a note for the requester")

    sp = lsub.add_parser(
        "force", help="force-release it: no answer to your request in 2:00 (asks first)",
        description="Revoke the holder's lease. Only after your request's 2:00 deadline, with "
                    "no answer (or a 'keep' that ran out), and you at the head of the queue. "
                    "It kicks the holder off the board now. Asks first; without a terminal "
                    "it needs --yes. When no Harness Manager session answered, the holder may "
                    "be a script (a soak or runner): it asks you to type the board's name, and "
                    "without a terminal it needs --confirm-board NAME (--yes is not enough).",
        parents=[fmt], epilog=_cols("lease"))
    sp.add_argument("target", metavar="TARGET", help=target_help)
    sp.add_argument("--yes", action="store_true", help="do not ask for confirmation")
    sp.add_argument("--confirm-board", metavar="NAME", default=None,
                    help="the board's name (mps3-01), for scripts: confirms a force-release "
                         "whose holder may be a script; replaces the prompt")

    sp = lsub.add_parser("leave", help="leave the queue and withdraw your request",
                         parents=[fmt], epilog=_cols("lease leave"))
    sp.add_argument("target", metavar="TARGET", help=target_help)

    sp = lsub.add_parser("dismiss", help="forget the last forced release of your lease",
                         description="`lease show` reports the last time someone force-released "
                                     "your lease until you dismiss it. Local only: the hub is "
                                     "not asked, and the next forced release is reported again.",
                         parents=[fmt], epilog=_cols("lease dismiss"))
    sp.add_argument("target", metavar="TARGET", help=target_help)
    vp.set_defaults(fn=cmd_lease)

    vp = subparsers.add_parser(
        "share", help="the hub's TTY shares for the board: list, start (never stop)",
        description="fpgahub serves a board's USB serial ports (the FPGA UART lanes) as TCP "
                    "streams. Never the MCC's tty_00: Harness Manager runs the MCC on the hub. "
                    "There is no stop: it stops every share.",
        parents=[fmt], epilog=_cols("share"))
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
    ctx.emit(result)


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


def _now() -> float:
    return time.time()


def _stderr_is_tty() -> bool:
    try:
        return sys.stderr.isatty()
    except (AttributeError, ValueError):
        return False


def _stdin_is_tty() -> bool:
    try:
        return sys.stdin.isatty()
    except (AttributeError, ValueError):
        return False


def full_view(view: dict[str, Any]) -> dict[str, Any]:
    """A lease view with every key docs/LEASE_REQUESTS.md lists, whatever the service gave."""
    out = dict(view)
    out.setdefault("lease", None)
    out.setdefault("hub", None)
    for key, empty in (("queue", list), ("request", lambda: None), ("incoming", list),
                       ("taken", lambda: None)):
        if out.get(key) is None:
            out[key] = empty()
    return out


def _row(target: str, hub: str, view: dict[str, Any]) -> list[Any]:
    view = full_view(view)
    lease = view["lease"]
    head = ([target, hub, "free", "", "", False] if lease is None else
            [target, hub, "held", lease["holder"], lease.get("expires_at", ""), lease["mine"]])
    queue = view["queue"]
    mine = next((q.get("position") for q in queue if q.get("mine")), None)
    req = view["request"]
    answer = _answer_field((req or {}).get("answer"))
    taken = view["taken"]
    # LEASE-BOARD appends BOARD (the physical board, "" when unknown): TSV layouts only grow.
    return head + [len(queue), mine if mine is not None else (req or {}).get("position"),
                   (req or {}).get("id", ""), answer,
                   "" if req is None else bool(req.get("force_available")),
                   len(view["incoming"]), (taken or {}).get("by", ""), view.get("board") or ""]


def _clock(iso: Any) -> str:
    """``2026-09-24T12:02:00+00:00`` -> ``12:02:00`` (the UTC time a person reads)."""
    stamp = parse_iso(iso)
    if stamp is None:
        return str(iso or "?")
    return datetime.fromtimestamp(stamp, timezone.utc).strftime("%H:%M:%S UTC")


def _human_view(where: str, target: str, view: dict[str, Any], now: float) -> list[str]:
    view = full_view(view)
    lease = view["lease"]
    lines = [f"{where}: not leased"] if lease is None else [
        f"{where}: held by {lease['holder']} (user {lease.get('user') or '?'}, "
        f"expires {lease.get('expires_at') or '?'})" + (" — yours" if lease["mine"] else "")]
    if view["queue"]:
        lines.append("queue: " + "  ".join(
            f"{q.get('position', '?')}. {q.get('holder', '?')}" + (" (you)" if q.get("mine") else "")
            for q in view["queue"]))
    req = view["request"]
    if req:
        ans = req.get("answer") or None
        line = f"your request {req.get('id', '?')}: asked at {_clock(req.get('created_at'))}"
        if ans:
            said = f": {ans['message']!r}" if ans.get("message") else ""
            line += (f"; answered keep for {ans.get('minutes')} min{said}"
                     if ans.get("answer") == "keep" else f"; answered {ans.get('answer')}{said}")
        else:
            deadline = parse_iso(req.get("deadline_at"))
            left = "" if deadline is None else (f" ({fmt_left(deadline - now)} left)"
                                                if now < deadline else " (passed)")
            line += f"; answer due {_clock(req.get('deadline_at'))}{left}"
        lines.append(line)
        if req.get("force_available"):
            lines.append(f"  force-release is available: `harness-manager lease force {target}`")
        elif req.get("force_reason"):
            lines.append(f"  force-release: {req['force_reason']}")
    lines += _incoming_lines(target, view["incoming"], now)
    taken = view["taken"]
    if taken:
        lines.append(f"your lease was force-released by {taken.get('by', '?')} at "
                     f"{_clock(taken.get('at'))}: {taken.get('reason', '')}")
    return lines


def _answer_field(ans: Any) -> str:
    """``keep:15`` / ``release`` / ``""``: an answer in one TSV field."""
    if not isinstance(ans, dict) or not ans.get("answer"):
        return ""
    return f"keep:{ans.get('minutes')}" if ans["answer"] == "keep" else str(ans["answer"])


def _incoming_lines(target: str, incoming: list[dict[str, Any]], now: float) -> list[str]:
    lines = []
    for inc in incoming:
        said = f": {inc['message']!r}" if inc.get("message") else ""
        ans = inc.get("answer") or None
        if ans:                                   # D5: what I answered (it survives a reload)
            what = (f"keep for {ans.get('minutes')} min" if ans.get("answer") == "keep"
                    else str(ans.get("answer")))
            lines.append(f"request {inc.get('id', '?')} from {inc.get('by', '?')}{said}; "
                         f"you answered {what} at {_clock(ans.get('at'))}")
            continue
        deadline = parse_iso(inc.get("deadline_at"))
        left = "" if deadline is None or now >= deadline else f" ({fmt_left(deadline - now)} left)"
        lines.append(f"request {inc.get('id', '?')} from {inc.get('by', '?')}{said}; answer by "
                     f"{_clock(inc.get('deadline_at'))}{left}: `harness-manager lease respond "
                     f"{target} {inc.get('id', 'ID')} --release | --keep MINUTES`")
    return lines


class _Countdown:
    """What ``lease request`` shows on stderr while it waits, on its own thread.

    ``arm(mode)`` is called from the service's progress callback; the thread then
    reads the lease view (one ``view`` call, so the service's ``request`` is never
    re-entered) and shows:

    - ``notified``: the holder's 2:00, from the note's ``deadline_at`` (not our clock);
    - ``answered``: the answer; for a "keep", the time until force-release can reopen
      (``answer.at`` + minutes). The wait goes on (D1);
    - ``force``: that force-release is open, and the command for it.

    On a terminal the time left is one line redrawn each second; otherwise each state
    is printed once.
    """

    def __init__(self, ctx: Ctx, svc: Any, cand: Any, hub: Any) -> None:
        self.ctx, self.svc, self.cand, self.hub = ctx, svc, cand, hub
        self.stop = threading.Event()
        self.wake = threading.Event()
        self._pending: list[str] = []          # modes to show, in order (none is dropped)
        self._mu = threading.Lock()
        self._drawn = False
        self._thread: threading.Thread | None = None

    def arm(self, mode: str) -> None:
        with self._mu:
            self._pending.append(mode)
        self.wake.set()
        if self._thread is None:
            self._thread = threading.Thread(target=self._run, name="lease-countdown", daemon=True)
            self._thread.start()

    def _stream(self) -> Any:
        return self.ctx.err or sys.stderr

    def _view(self) -> dict[str, Any] | None:
        try:
            return full_view(self.svc.view(self.hub))
        except HarnessError:
            return None

    def _plan(self, mode: str) -> tuple[float | None, str]:
        """(until when to count down, or None; the redrawn line's text with ``{left}``)."""
        view = self._view() or full_view({})
        holder = (view["lease"] or {}).get("holder") or "the holder"
        req = view["request"] or {}
        board = _board_name(self.cand, self.hub)
        target = getattr(self.ctx.args, "target", "TARGET")
        if mode == "notified":
            deadline = parse_iso(req.get("deadline_at"))
            about = ""
            if deadline is None:
                deadline, about = _now() + REQUEST_WINDOW_S, " (about)"
            self.note(f"asked {holder} to release {board}; the answer is due by "
                      f"{_clock(iso_utc(deadline))}{about} ({fmt_left(deadline - _now())})")
            return deadline, f"waiting for {holder} to answer: {{left}}{about} left " \
                             "(Ctrl-C leaves the queue)"
        if mode == "answered":
            ans = req.get("answer") or {}
            said = f": {ans['message']!r}" if ans.get("message") else ""
            if ans.get("answer") == "keep":
                minutes = ans.get("minutes") or 0
                at = parse_iso(ans.get("at"))
                until = None if at is None else at + 60 * minutes
                when = "" if until is None else f"; force-release can reopen at {_clock(iso_utc(until))}"
                self.note(f"{holder} keeps {board} for {minutes} more min{said}{when}. You stay "
                          f"in the queue (position {req.get('position') or '?'}); still "
                          "waiting (Ctrl-C leaves the queue)")
                return until, f"{holder} keeps it: {{left}} until force-release can reopen " \
                              "(Ctrl-C leaves the queue)"
            if ans.get("answer") == "release":
                self.note(f"{holder} released {board}{said}; it is on its way to you")
                return None, ""
            self.note("the holder answered; still waiting (Ctrl-C leaves the queue)")
            return None, ""
        if mode == "force":
            self.note(f"force-release is open: you are at the head of the queue and "
                      f"{holder} has not given {board} up. `harness-manager lease force "
                      f"{target}` takes it now (it kicks {holder} off; it asks first)")
        return None, ""

    def _run(self) -> None:
        until: float | None = None
        text = ""
        while not self.stop.is_set():
            with self._mu:
                modes, self._pending = self._pending, []
            for mode in modes:
                if self.stop.is_set():
                    return
                until, text = self._plan(mode)
            if until is not None and _stderr_is_tty() and until > _now():
                with self._mu:
                    if self.stop.is_set():
                        return
                    self._stream().write("\r" + text.format(left=fmt_left(until - _now()))
                                         + "\x1b[K")
                    self._stream().flush()
                    self._drawn = True
                self.wake.wait(1.0)
            else:
                self.clear()
                until = None
                self.wake.wait()
            self.wake.clear()

    def clear(self) -> None:
        with self._mu:
            if self._drawn:
                self._stream().write("\r\x1b[K")
                self._stream().flush()
                self._drawn = False

    def note(self, text: str) -> None:
        """A line on stderr that does not tangle with the countdown line."""
        self.clear()
        with self._mu:
            self.ctx.note(text)

    def close(self) -> None:
        self.stop.set()
        self.wake.set()
        t = self._thread
        if t is not None and t is not threading.current_thread():
            t.join(timeout=2.0)
        self.clear()


def _lease_result(cand: Any, hub: Any, lease: dict[str, Any], human: list[str],
                  board: str = "", **extra: Any) -> Result:
    """LEASE-BOARD: ``board`` (the physical board, null when unknown) beside ``lease.target``,
    at the top as ``lease show`` has it, and in the lease object."""
    lease = {**lease, "board": lease.get("board") or board or None}
    view = {"lease": lease, "hub": hub.host, "board": board or None}
    return Result("lease", {"board_id": cand.board_id, "name": _name(cand), **view, **extra},
                  rows=[_row(hub.target, hub.host, view)], human=human)


def _request(ctx: Ctx, cand: Any, hub: Any, svc: Any) -> int:
    a = ctx.args
    message = clean_message(a.message)
    if a.ttl is not None and not 60 <= a.ttl <= 86400:     # the daemon route's rule (hub_api._ttl)
        raise UsageError(f"--ttl must be whole seconds from 60 to 86400, not {a.ttl}")
    refusal = request_refusal(full_view(svc.view(hub)), _board_name(cand, hub))
    if refusal is not None:
        raise refusal
    countdown = _Countdown(ctx, svc, cand, hub)
    board = svc.board_of(hub, ask=False)          # the view above asked the hub already

    def progress(phase: str, done: int, _total: int) -> None:
        if phase == "queued":
            where = f" at position {done}" if done else ""
            countdown.note(f"queued{where} for {_where(cand, hub, board)} (Ctrl-C leaves the queue)")
        elif phase in ("notified", "answered"):
            countdown.arm(phase)
        elif phase == "force-available":
            countdown.arm("force")
        elif phase == "held":
            countdown.close()

    try:
        out = svc.request(cand.board_id, hub, message=message, ttl_s=a.ttl, progress=progress)
    except KeyboardInterrupt:
        countdown.close()
        left = svc.leave(cand.board_id, hub).get("left")
        ctx.note("left the queue; the request is withdrawn" if left else "not in the queue")
        raise
    finally:
        countdown.close()
    if out.get("lease"):
        lease = out["lease"]
        human = [f"{_where(cand, hub, board)}: yours, held by {lease['holder']} until "
                 f"{lease.get('expires_at') or '?'}"
                 + (" (already yours)" if out.get("already") else ""),
                 "the Harness Manager service extends it while the board is open there"]
        _emit(ctx, _lease_result(cand, hub, lease, human, board,
                                 **{k: v for k, v in out.items() if k not in ("lease", "ok")}))
        return ExitCode.OK
    if out.get("left"):
        # D7: the request was withdrawn (lease leave, the UI's Leave queue): no board.
        raise ActionFailedError(f"the request for {_where(cand, hub, board)} ended: you left "
                                "the queue",
                                hint=f"`harness-manager lease request {a.target}` asks again")
    raise ActionFailedError(f"the request for {lease_name(board, hub.target)} ended without the "
                            f"board ({out})",
                            hint=f"`harness-manager lease show {a.target}`")


def _force(ctx: Ctx, cand: Any, hub: Any, svc: Any) -> int:
    a = ctx.args
    view = full_view(svc.view(hub))
    hub_board = view.get("board") or ""
    refusal = force_refusal(view, _now(), lease_name(hub_board, hub.target))
    if refusal is not None:
        raise refusal
    lease = view["lease"] or {}
    holder = lease.get("holder") or "the holder"
    board = _revoked_board(cand, hub, view)
    # D12: the names that confirm it, the one to ask for first (what the prompt calls it).
    names = (_board_name(cand, hub), naming.address_of(cand))
    typed = typed_names(names[0], view.get("board"), hub.target, *names[1:])
    confirm_board = getattr(a, "confirm_board", None)
    if confirm_board is not None:
        # A typed name confirms it whoever holds it (a script needs no --yes as well).
        err = confirm_board_error("unknown", _holder_why(lease), confirm_board, typed, hub.target)
        if err is not None:
            raise err
    elif lease.get("holder_kind") != HOLDER_HM:
        confirm_board = _ask_board_name(ctx, holder, typed[0], lease)
        err = view_confirm_error(view, confirm_board, typed, hub.target)
        if err is not None:
            raise err
    else:
        warning = (f"Are you sure? This kicks {holder} off {board} now; anything they are "
                   "running on the board is interrupted.")
        if not a.yes and not _stdin_is_tty():
            raise RefusedError(f"force-release asks first, and there is no terminal to ask on "
                               f"(it kicks {holder} off {board} now)",
                               hint="re-run with --yes if you mean it")
        ctx.confirm(warning)
    out = svc.force(cand.board_id, hub, confirm=True, confirm_board=confirm_board,
                    board_names=names)
    lease = out.get("lease") or {}
    human = [f"{_where(cand, hub, hub_board)}: force-released; yours, held by "
             f"{lease.get('holder', '?')} until {lease.get('expires_at') or '?'}",
             f"{holder} is told who took it and why"]
    _emit(ctx, _lease_result(cand, hub, lease, human, hub_board,
                             **{k: v for k, v in out.items() if k not in ("lease", "ok")}))
    return ExitCode.OK


def _holder_why(lease: dict[str, Any]) -> str:
    """Why the view could not say a Harness Manager session holds it (D12)."""
    return lease.get("holder_kind_reason") or "the holder may be a script (a soak or runner)"


def _ask_board_name(ctx: Ctx, holder: str, name: str, lease: dict[str, Any]) -> str:
    """D12: the holder may be a script. Ask for the board's name on the terminal; without
    one, the caller must pass ``--confirm-board`` (USAGE, as the API's missing name)."""
    if not _stdin_is_tty():
        raise UsageError(f"no Harness Manager session is known to hold {name} "
                         f"({_holder_why(lease)}); force-release needs the board's name typed, "
                         "and there is no terminal to type it on",
                         hint=f"re-run with --confirm-board {name} if you mean it "
                              "(--yes is not enough)")
    stream = ctx.err or sys.stderr
    stream.write(f"No Harness Manager session is known to hold {name}; it may be a script (a soak or "
                 f"runner). Force-releasing kicks {holder} off it now, and anything running on "
                 f"the board is interrupted.\nType {name} to force-release: ")
    stream.flush()
    try:
        answer = sys.stdin.readline()
    except (OSError, ValueError, EOFError):
        answer = ""
    if not answer.endswith("\n"):
        stream.write("\n")
        stream.flush()
    if not answer.strip():
        raise RefusedError("not confirmed: no board name was typed",
                           hint=f"type {name} to force-release it")
    return answer.strip()


def _board_name(cand: Any, hub: Any) -> str:
    """What people call the board (N1: ``mps3-01``): the candidate's name, else the hub's
    board that owns the target (one hub call at most), else the target."""
    name = _name(cand)
    if name:
        return name
    getter = getattr(getattr(hub, "client", None), "board_id", None)
    if callable(getter):
        try:
            board = getter()
            if isinstance(board, str) and board:
                return naming.hub_display(board)
        except HarnessError:
            pass
    return hub.target


def _revoked_board(cand: Any, hub: Any, view: dict[str, Any]) -> str:
    """The board a force revokes, for the prompt (D4): the view's ``board`` (``mps3_01``)
    as people write it, with the N1 name first when that differs."""
    board = view.get("board")
    if not isinstance(board, str) or not board:
        return _board_name(cand, hub)
    shown = naming.hub_display(board)
    name = _name(cand)
    return f"{name} (hub board {board})" if name and name != shown else shown


def _name(cand: Any) -> str:
    return getattr(cand, "name", "") or ""


def _where(cand: Any, hub: Any, board: str = "") -> str:
    """Where a lease is, in words. LEASE-BOARD: the hub's physical board (``mps3_01``) when it
    is known, with the target it is leased as (``mps3_01_pl``) as a detail::

        mps3-01 (mps3_01 on HUB, target mps3_01_pl)     the board has a name (N1)
        mps3_01 on HUB (target mps3_01_pl)              it has none

    With the board unknown (or the same as the target) it is what it always was:
    ``mps3-01 (mps3_01_pl on HUB)``, ``mps3_01_pl on HUB``. The name comes with the candidate
    (boards.toml, the hub table); ``board`` from ``LeaseService.board_of``."""
    name = _name(cand)
    label = lease_name(board, hub.target)
    detail = f"target {hub.target}" if label != hub.target else ""
    if name:
        return f"{name} ({label} on {hub.host}{', ' + detail if detail else ''})"
    return f"{label} on {hub.host}" + (f" ({detail})" if detail else "")


def _principal(hub: Any) -> str:
    """This client's principal on the hub (``david@mapstone-dev``), or ``""`` if it cannot say."""
    getter = getattr(getattr(hub, "client", None), "principal", None)
    if callable(getter):
        try:
            who = getter()
            return who if isinstance(who, str) else ""
        except HarnessError:
            return ""
    return ""


def cmd_lease(ctx: Ctx) -> int:
    a = ctx.args
    cand, hub = _hub(ctx)
    svc = _service()
    if a.lease_cmd == "show":
        view = full_view(svc.view(hub))
        if view.get("lease"):
            view["lease"].setdefault("board", view.get("board"))     # an older service's view
        _emit(ctx, Result("lease", {"board_id": cand.board_id, "name": _name(cand), **view},
                          rows=[_row(hub.target, hub.host, view)],
                          human=_human_view(_where(cand, hub, view.get("board") or ""),
                                            hub.target, view, _now())))
        return ExitCode.OK
    if a.lease_cmd == "acquire":
        holder = a.holder or hub_holder(hub)             # SET-HUB-3: the hub's holder

        def progress(phase: str, done: int, _total: int) -> None:
            # LEASE-BOARD: name the board. Asked only once the hub has answered (queued), so
            # an unreachable hub costs one timeout, not two; cached after the first ask.
            if phase == "queued":
                where = f" at position {done}" if done else ""
                ctx.note(f"queued{where} for {_where(cand, hub, svc.board_of(hub))} as {holder}; "
                         "waiting (Ctrl-C leaves the queue)")
            elif phase == "acquire":
                ctx.note(f"asking {hub.host} for {_where(cand, hub, svc.board_of(hub, ask=False))}"
                         f" as {holder} ...")

        try:
            out = svc.acquire(hub, board_id=cand.board_id, ttl_s=a.ttl, holder=holder,
                              progress=progress, timeout_s=a.timeout, heartbeat=False)
        except KeyboardInterrupt:
            # fpgahub takes an admin's --holder literally and queued us under our principal,
            # so cancel with the principal (docs/LEASE_REQUESTS.md, CCR-A3).
            removed = hub.client.lease_cancel(_principal(hub) or holder)
            ctx.note("left the queue" if removed else "not queued")
            raise
        lease = out["lease"]
        board = svc.board_of(hub)                 # after the grant: the hub answers
        human = [f"{_where(cand, hub, board)}: held by {lease['holder']}"
                 f" until {lease.get('expires_at') or '?'}" + (" (already yours)" if out.get("already")
                                                                else ""),
                 "the Harness Manager service extends it while the board is open there"]
        _emit(ctx, _lease_result(cand, hub, lease, human, board))
        return ExitCode.OK
    if a.lease_cmd == "release":
        out = svc.release(hub, board_id=cand.board_id)
        board = svc.board_of(hub)                 # after the release: the hub answered
        released = out.get("released")
        if isinstance(released, dict):
            released = {**released, "board": released.get("board") or board or None}
        view = {"lease": None, "hub": hub.host, "board": board or None}
        _emit(ctx, Result("lease", {"board_id": cand.board_id, **view, "released": released},
                          rows=[_row(hub.target, hub.host, view)],
                          human=[f"{_where(cand, hub, board)}: released"]))
        return ExitCode.OK
    if a.lease_cmd == "request":
        return _request(ctx, cand, hub, svc)
    if a.lease_cmd == "requests":
        view = full_view(svc.view(hub))
        incoming = view["incoming"]
        rows = [[hub.target, r.get("id"), r.get("by"), r.get("user"), r.get("host"),
                 r.get("message"), r.get("created_at"), r.get("deadline_at"),
                 _answer_field(r.get("answer"))] for r in incoming]
        lines = _incoming_lines(hub.target, incoming, _now())
        if not incoming:
            mine = (view["lease"] or {}).get("mine")
            lines = [f"no requests for {_where(cand, hub, view.get('board') or '')}"
                     + ("" if mine else " (you do not hold it; requests go to the holder)")]
        _emit(ctx, Result("lease requests", {"board_id": cand.board_id, "name": _name(cand),
                                             "hub": hub.host, "target": hub.target,
                                             "board": view.get("board") or None,
                                             "incoming": incoming},
                          rows=rows, human=lines))
        return ExitCode.OK
    if a.lease_cmd == "respond":
        rid = request_id(a.id)
        message = clean_message(a.message)
        answer, minutes = ("release", 0) if a.release else ("keep", keep_minutes(a.keep))
        out = svc.respond(cand.board_id, hub, rid, answer, minutes=minutes, message=message)
        board = svc.board_of(hub)
        human = ([f"released {_where(cand, hub, board)}: the requester gets it next"]
                 if answer == "release" else
                 [f"told the requester you keep {_where(cand, hub, board)} for {minutes} more min"])
        data = {"board_id": cand.board_id, "name": _name(cand), "target": hub.target,
                "board": board or None, "id": rid,
                "answer": answer,
                "minutes": minutes, "message": message,
                **{k: v for k, v in out.items() if k != "ok"}}
        _emit(ctx, Result("lease respond", data,
                          rows=[[hub.target, rid, answer, minutes, message]], human=human))
        return ExitCode.OK
    if a.lease_cmd == "force":
        return _force(ctx, cand, hub, svc)
    if a.lease_cmd == "leave":
        out = svc.leave(cand.board_id, hub)
        left = bool(out.get("left"))
        board = svc.board_of(hub)
        _emit(ctx, Result("lease leave", {"board_id": cand.board_id, "name": _name(cand),
                                          "hub": hub.host, "target": hub.target,
                                          "board": board or None, "left": left},
                          rows=[[hub.target, hub.host, left]],
                          human=[f"left the queue for {_where(cand, hub, board)}; the request is "
                                 "withdrawn" if left else
                                 f"not in the queue for {_where(cand, hub, board)}"]))
        return ExitCode.OK
    if a.lease_cmd == "dismiss":
        # D11: local (the service's own record); like `leave`, running it twice is fine.
        dismissed = bool(svc.dismiss_taken(hub))
        board = svc.board_of(hub, ask=False)      # local: boards.toml hub.board, no hub call
        _emit(ctx, Result("lease dismiss", {"board_id": cand.board_id, "name": _name(cand),
                                            "hub": hub.host, "target": hub.target,
                                            "board": board or None, "dismissed": dismissed},
                          rows=[[hub.target, hub.host, dismissed]],
                          human=[f"dismissed the forced release of {_where(cand, hub, board)}"
                                 if dismissed else
                                 f"no forced release of {_where(cand, hub, board)} to dismiss"]))
        return ExitCode.OK
    raise UsageError(f"unknown lease action {a.lease_cmd!r}")


def cmd_share(ctx: Ctx) -> int:
    a = ctx.args
    cand, hub = _hub(ctx)
    if a.share_cmd == "list":
        shares = hub.client.share_list()
    elif a.share_cmd == "start":
        tty = a.name if a.name.startswith("/dev/") else hub.config.shares.get(a.name)
        refuse = getattr(hub, "refuse_share", None)       # MCC-FIX: never tty_00 (the pack says)
        if tty and callable(refuse):
            refuse(a.name, tty)
        if not tty:
            raise AbsentError(f"no share named {a.name!r} for {cand.board_id}",
                              hint="configured: " + (", ".join(sorted(hub.config.shares)) or "none")
                                   + "; or give the /dev/... path")
        shares = [hub.client.share_start(tty, hub.config.baud)]
    else:
        raise UsageError(f"unknown share action {a.share_cmd!r}")
    rows = [[hub.target, hub.host, s.tty, f"{s.host}:{s.port}", s.writer, s.readers, s.running]
            for s in shares]
    human = [f"{s.tty} -> {s.host}:{s.port}  writer {s.writer or '-'}  clients {s.readers}"
             for s in shares] or [f"no shares running for {hub.target} on {hub.host}"]
    data = {"board_id": cand.board_id, "hub": hub.host, "target": hub.target,
            "shares": [{"tty": s.tty, "host": s.host, "port": s.port, "writer": s.writer,
                        "readers": s.readers, "running": s.running} for s in shares]}
    _emit(ctx, Result("share", data, rows=rows, human=human))
    return ExitCode.OK
