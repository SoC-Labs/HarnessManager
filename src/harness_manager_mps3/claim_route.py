"""CLAIMROUTE: one place that sends a claim-locked control verb the right way.

From board image v2.2 a CLAIMED board refuses, to any peer that is not the board itself, every
state-changing 6900 verb (swap, reset, set_clk, link, macgen, dutrx, display except ``query``,
touch_cal set/default, reboot), every design push on 6910 and every TFTP push, with ONE line:
``{"ok":false,"err":"<verb> locked: board claimed (use ssh)","code":"locked"}``. Reads stay open.

So a verb that changes the board takes the same decision the card, slot, identity and XVC
paths take (``claim.lock_route``):

- unclaimed, bare metal, or not known to be claimed: DIRECT (today's path);
- claimed by THIS Harness Manager (its pin is the board's key): through the session's ONE claim
  forward (``os_slots.claim_forward``), a loopback peer from the board's point of view;
- claimed by anyone else: refused BEFORE anything is sent, in plain words
  (``ClaimLockedError`` with the claim hint).

A direct call that still comes back ``code:"locked"`` (the decision thought the board unclaimed:
it was claimed since, or by a restart) is retried ONCE through the forward when this Harness
Manager holds the claim, else refused in the claim's words (``claim.refusal_error``). Nothing is
ever sent twice to a board that did the thing: a locked reply did nothing.

Use ``routed_call(session, what, shell, fn)`` for a verb; ``routed_shell`` when the caller needs
the forward's ports too (the design push); ``locked_call`` to read a locked reply off a shell.
"""

from __future__ import annotations

import contextlib
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import Any, TypeVar

from harness_manager.core.errors import ClaimLockedError, HarnessError

from . import slot_words

T = TypeVar("T")

ROUTE_DIRECT = ""
ROUTE_BOARD_SSH = "board-ssh"
#: The claim forward's holder name for these verbs (``claim.hold_forward(user)``).
USER = "route"


class LockedReply(ClaimLockedError):
    """The board answered a verb with the claim lock's line (``err``, ``code``). ``routed_op``
    catches it for the retry; one that reaches a caller with no session (nothing to route
    with) is the claim refusal as it stands."""

    def __init__(self, err: str, code: str = "") -> None:
        super().__init__(f"the board is claimed and refused this from here [harness: {err}]",
                         hint="`harness-manager board claim-status TARGET`; only the claiming "
                              "key's owner can do this now, over SSH")
        self.err = err
        self.code = code
        self.from_reply = True
        self.claim_lost = slot_words.is_claim_lost(err, code)


@dataclass(frozen=True)
class Routed:
    """Where a verb goes: ``shell`` (6900), ``push`` (6910; None direct: the pack's own)."""

    shell: Any
    push: int | None
    route: str

    @property
    def via_board(self) -> bool:
        return self.route == ROUTE_BOARD_SSH


# --- the decision --------------------------------------------------------------------------------


def route_of(session: Any, what: str, impl: str | None = None,
             claimed: bool | None = None) -> str:
    """``""`` (direct) or ``"board-ssh"``; a board this Harness Manager cannot enter is
    ``ClaimLockedError`` before anything is sent. ``claimed``: what the caller just learned (a
    lock refusal); with no claim adapter (bare metal) that means the forward, which then fails
    in its own words."""
    claim = getattr(session, "claim", None)
    if claim is None or not callable(getattr(claim, "lock_route", None)):
        return ROUTE_BOARD_SSH if claimed else ROUTE_DIRECT
    return claim.lock_route(what, impl=impl, claimed=claimed)


def is_locked(err: str, code: str = "") -> bool:
    return slot_words.is_claim_lock(err, code)


def locked_error(err: str, what: str, session: Any = None) -> HarnessError:
    """The claim lock's refusal as the error Harness Manager shows (the claim's plain words);
    ``from_reply`` marks it as the BOARD's answer (so a direct attempt may be retried), not a
    decision made before sending."""
    claim = getattr(session, "claim", None)
    exc: HarnessError | None = None
    if slot_words.is_claim_lost(err):
        # the claim is gone (card-less board, warm restart): never the tunnel's to fix
        if claim is not None and callable(getattr(claim, "note_claim_lost", None)):
            claim.note_claim_lost()
        exc = slot_words.lost_claim_error(err, what)
        exc.from_reply = False                     # type: ignore[attr-defined]  # final: no retry
        return exc
    if claim is not None and callable(getattr(claim, "refusal_error", None)):
        exc = claim.refusal_error(err, what)
    else:
        from .claim import refusal_error

        exc = refusal_error(err, what)
    if exc is None:
        exc = ClaimLockedError(f"{what} was refused: the board is claimed [harness: {err}]",
                               hint="`harness-manager board claim-status TARGET`")
    exc.from_reply = True                          # type: ignore[attr-defined]
    return exc


# --- reading a locked reply ------------------------------------------------------------------------


def locked_call(shell: Any, fn: Callable[[Any], T]) -> T:
    """``shell.call(fn)``; ``LockedReply`` when the board's last reply was the claim lock's line
    (``Mps3Shell.call_raw`` keeps it in ``tap.last``). A shell without ``call_raw`` is a plain
    ``call``; its reply's own ``ok``/``err`` are read instead."""
    raw = getattr(shell, "call_raw", None)
    if not callable(raw):
        resp = shell.call(fn)
        _check(getattr(resp, "ok", True), getattr(resp, "err", ""), "")
        return resp
    resp, last = raw(lambda c, tap: (fn(c), dict(getattr(tap, "last", None) or {})))
    _check(last.get("ok"), str(last.get("err") or ""), slot_words.reply_code(last))
    return resp


def _check(ok: Any, err: Any, code: str) -> None:
    if ok is False and isinstance(err, str) and (is_locked(err, code)
                                                 or slot_words.is_claim_lost(err, code)):
        raise LockedReply(err, code)


# --- the forward -----------------------------------------------------------------------------------


def _forward_shell(session: Any, what: str, direct: Any, user: str) -> Iterator[Routed]:
    from .card import _forwarded, _gate_key
    from .os_slots import claim_forward
    from .shell import Mps3Shell

    with claim_forward(session, user, what) as (host, ctl, push):
        yield Routed(_forwarded(Mps3Shell(host, ctl, timeout=float(getattr(direct, "timeout", 3.0)),
                                          gate_key=_gate_key(direct))), push, ROUTE_BOARD_SSH)


@contextlib.contextmanager
def routed_shell(session: Any, what: str, direct: Any, *, impl: str | None = None,
                 claimed: bool | None = None, user: str = USER) -> Iterator[Routed]:
    """The shell (and push port) this claim-locked verb goes to, held for the ``with``: the
    session's own ``direct`` shell, or one on the claim forward."""
    if route_of(session, what, impl, claimed) == ROUTE_BOARD_SSH:
        yield from _forward_shell(session, what, direct, user)
    else:
        yield Routed(direct, None, ROUTE_DIRECT)


def escalate(session: Any, what: str, err: str, *, impl: str | None = None) -> None:
    """A direct call was answered with the lock: return when this Harness Manager holds the
    claim (go through the forward), else raise the refusal (``ClaimLockedError``)."""
    if slot_words.is_claim_lost(err):                      # the claim is gone: no tunnel helps
        raise locked_error(err, what, session)
    route_of(session, what, impl, claimed=True)            # not ours: ClaimLockedError, the hint
    claim = getattr(session, "claim", None)
    if claim is None or not callable(getattr(claim, "hold_forward", None)):
        raise locked_error(err, what, session)


def routed_call(session: Any, what: str, direct: Any, fn: Callable[[Any], T], *,
                impl: str | None = None, user: str = USER) -> T:
    """Run ``fn(client)`` on the board the claim-aware way (module docstring)."""
    return routed_op(session, what, direct, lambda sh: locked_call(sh, fn), impl=impl, user=user)


def routed_op(session: Any, what: str, direct: Any, op: Callable[[Any], T], *,
              impl: str | None = None, user: str = USER) -> T:
    """``op(shell)`` on the shell the claim says; ``op`` raises ``LockedReply`` (``locked_call``,
    or ``Mps3Shell.reset``) when the board answers with the lock. Direct and locked: once more
    through the forward when the claim is ours, else the refusal."""
    via_board = False
    try:
        with routed_shell(session, what, direct, impl=impl, user=user) as r:
            via_board = r.via_board
            return op(r.shell)
    except LockedReply as exc:
        if via_board or exc.claim_lost:      # locked even from the board itself / claim gone
            raise locked_error(exc.err, what, session) from exc
        err = exc.err
    escalate(session, what, err, impl=impl)
    try:
        with routed_shell(session, what, direct, impl=impl, claimed=True, user=user) as r:
            return op(r.shell)
    except LockedReply as again:
        raise locked_error(again.err, what, session) from again
