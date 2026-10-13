"""Stop, step and restart the DUT's clock (lane L5c COSIM; net-protocol v0.20 ``cosim``).

A board whose harness reports the engine feature ``cosim`` has a clock gate in front of the
DUT. This service reads its cycle count and, for a claimed board, halts the clock, runs it for
exactly N cycles, or lets it run again. Gate on the feature NAME ``cosim``, never a version.

- ``cycles`` is open to anyone who can reach the board.
- ``halt``, ``step`` and ``run`` are claim-locked: a stopped clock stops the DUT, its console
  and its debug path. They go through the session's claim forward (the board's own SSH), like
  the identity change does: directly while the board is unclaimed, through the SSH once the
  board says ``locked`` and the claim is this Harness Manager's.
- ``step`` replies when the step has finished (a step of up to 100,000,000 cycles takes at
  most a few seconds); the board gives up after 5 s and leaves the clock as it is.
- A swap, ``reset dut`` and ``reset rp`` put the clock back to running.

**Known limitation.** Inputs that are not tied to the DUT clock (UART receive, the virtual
network PHY's receive) are not held while the clock is stopped, so a stopped or stepped run is
exact for the DUT's inside and its AHB/APB buses, not for those pins (``LIMITATION``).

Every reply carries ``mode`` (run | halt | step), ``running``, ``cycles`` (a whole number
below 2**53) and ``remaining``; ``step`` adds ``n``.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from harness_manager.core.errors import (
    ActionFailedError,
    ClaimLockedError,
    HarnessError,
    UnavailableError,
    UsageError,
)

FEATURE = "cosim"
CAPABILITY = "mps3.cosim"
ACTS = ("cycles", "halt", "step", "run")
LOCKED_ACTS = ("halt", "step", "run")
STEP_MIN, STEP_MAX = 1, 100_000_000
STEP_TIMEOUT_S = 5

#: The sentences (the board's own codes, in plain words).
NOT_SUPPORTED = "This board's image has no clock control"
NO_HW = "This board's design has no clock control block, or it did not answer"
LOCKED = "Claim the board to stop its clock"
TIMEOUT = "The step did not finish in 5 seconds; the clock was left as it is"
NEEDS_CLAIM_HINT = "claim the board in Harness Manager (Board > Access), then try again"
LIMITATION = ("Serial input and the network receive are not held while the clock is stopped, "
              "so a stopped or stepped run is exact inside the DUT and on its buses, but not "
              "for those pins.")


def _unavailable(reason: str) -> UnavailableError:
    """UnavailableError whose shown message is the plain sentence, not "mps3.cosim is
    unavailable: ..."."""
    exc = UnavailableError(CAPABILITY, reason)
    exc.message = reason
    return exc


def check_steps(n: Any) -> int:
    """``n`` as a whole number of cycles, 1 to 100,000,000, else ``UsageError``."""
    if isinstance(n, bool) or not isinstance(n, int) or not STEP_MIN <= n <= STEP_MAX:
        raise UsageError(f"The step must be a whole number of cycles from {STEP_MIN} to "
                         f"{STEP_MAX:,}", hint="for example 10000")
    return n


def refusal(reply: Mapping[str, Any], act: str = "") -> HarnessError:
    """A refused ``cosim`` reply as the error Harness Manager shows."""
    code = str(reply.get("code") or "")
    err = str(reply.get("err") or "")
    if code == "not_supported":
        return _unavailable(NOT_SUPPORTED)
    if code == "no_hw":
        return _unavailable(NO_HW)
    if code == "locked":
        return ClaimLockedError(LOCKED, hint=NEEDS_CLAIM_HINT)
    if code == "invalid":
        return UsageError(f"The board refused that: {err or 'invalid'}",
                          hint=f"steps are whole numbers from {STEP_MIN} to {STEP_MAX:,}")
    if code == "timeout":
        return ActionFailedError(TIMEOUT)
    what = f" {act}" if act else ""
    return ActionFailedError(f"The board refused the clock{what}: {err or code or 'no reason given'}")


def parse(reply: Mapping[str, Any]) -> dict[str, Any]:
    """The five fields of an ok reply, in the contract's order."""
    out: dict[str, Any] = {"mode": str(reply.get("mode") or ""),
                           "running": bool(reply.get("running")),
                           "cycles": int(reply.get("cycles") or 0),
                           "remaining": int(reply.get("remaining") or 0)}
    if "n" in reply:
        out["n"] = int(reply["n"])
    return out


def _shell(session: Any) -> Any:
    shell = getattr(session, "shell", None)
    if shell is None:
        raise _unavailable("There is no network link to this board's harness")
    return shell


def offered(session: Any) -> tuple[bool, str]:
    """``(True, "")`` when the board's harness reports ``cosim``, else ``(False, why)``.
    One ping and version on the control port; a session that cannot say is asked anyway."""
    live = getattr(_shell(session), "live", None)
    if not callable(live):
        return True, ""
    features = tuple(live().features)
    return (True, "") if FEATURE in features else (False, NOT_SUPPORTED)


def _require(session: Any) -> None:
    ok, why = offered(session)
    if not ok:
        raise _unavailable(why)


def _send(shell: Any, act: str, n: int | None) -> dict[str, Any]:
    def ask(c: Any, *_tap: Any) -> dict[str, Any]:
        if act == "cycles":
            return dict(c.cosim_cycles())
        if act == "halt":
            return dict(c.cosim_halt())
        if act == "run":
            return dict(c.cosim_run())
        return dict(c.cosim_step(n))

    call = getattr(shell, "call_raw", None)
    if callable(call):
        return call(lambda c, _t: ask(c))
    return shell.call(ask)


def _via_board(session: Any, act: str, n: int | None) -> dict[str, Any]:
    """The same act over the claim forward: the board's own SSH to its 127.0.0.1."""
    from harness_manager_mps3.card import _forwarded, _gate_key
    from harness_manager_mps3.os_slots import claim_forward
    from harness_manager_mps3.shell import Mps3Shell

    with claim_forward(session, "cosim", "stopping the board's clock") as (host, ctl, _push):
        shell = _forwarded(Mps3Shell(host, ctl, gate_key=_gate_key(_shell(session))))
        return _send(shell, act, n)


def _route(session: Any, claimed: bool | None = None) -> str:
    claim = getattr(session, "claim", None)
    if claim is None or not callable(getattr(claim, "lock_route", None)):
        return "board-ssh" if claimed else ""
    try:
        return claim.lock_route("Stopping the board's clock", impl="linux", claimed=claimed)
    except ClaimLockedError as exc:
        raise ClaimLockedError(LOCKED, hint=exc.hint or NEEDS_CLAIM_HINT) from exc


def _is_locked(reply: Mapping[str, Any]) -> bool:
    return str(reply.get("code") or "") == "locked" or "board claimed" in str(reply.get("err") or "")


def _do(session: Any, act: str, n: int | None = None) -> dict[str, Any]:
    if act in LOCKED_ACTS:
        route = _route(session)
        if route == "board-ssh":
            reply = _via_board(session, act, n)
        else:
            reply = _send(_shell(session), act, n)
            if not reply.get("ok") and _is_locked(reply):
                if getattr(session, "claim", None) is None:
                    raise refusal({"code": "locked"}, act)
                _route(session, claimed=True)
                reply = _via_board(session, act, n)
    else:
        reply = _send(_shell(session), act, n)
    if not reply.get("ok"):
        raise refusal(reply, act)
    return parse(reply)


def cycles(session: Any) -> dict[str, Any]:
    _require(session)
    return _do(session, "cycles")


def halt(session: Any) -> dict[str, Any]:
    _require(session)
    return _do(session, "halt")


def run(session: Any) -> dict[str, Any]:
    _require(session)
    return _do(session, "run")


def step(session: Any, n: Any) -> dict[str, Any]:
    count = check_steps(n)          # a bad count never reaches the board
    _require(session)
    return _do(session, "step", count)


ACTIONS: dict[str, Callable[..., dict[str, Any]]] = {
    "cycles": cycles, "halt": halt, "run": run, "step": step}
