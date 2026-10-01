"""After a cold boot, check the reported design against the board's debug port (FIX-PACK-6
item 3).

Silicon, H1 r5-r8 (board 1, Linux v2.0.0, 1 Oct): after an MCC REBOOT with nanosoc kept on the
card, harnessd reported ``rm_id 0x01000001`` (nanosoc) while the greybox was resident: the
card's power-on load had not finished (``power-on failed:timeout``: the card reads at ~14 KB/s
and the load has 30 s), and ``debug detect`` found nothing on the JTAG chain. Linux fixes the
cause (1e50499, Linux v2.1); Harness Manager no longer trusts a reported design after a cold
boot without asking the fabric.

``after_cold_boot(engine, session, after=...)``, run once after ``mcc reboot`` and ``power
cycle`` (the CLI's in-process verbs, and the service's ``reboot`` / ``power_cycle`` jobs):

- it applies only to a Linux harness whose reported design has a debug port (the pack's
  ``session.debug.openocd_config()`` names one); anything else returns None and nothing is
  read;
- it reads ONE IDCODE, non-intrusively, exactly as ``debug detect`` does
  (``engine.debug.detect``: OpenOCD ``init; scan_chain; shutdown`` with the core deferred, so
  nothing halts), and says:

  - ``verified``: the debug port answers ``0x6ba00477`` (the SoC-400 SWJ-DP);
  - ``unverified``: nothing answers ("the board reports <design> but no debug port answers:
    greybox is probably resident (known issue, Linux v2.0.0)"), or another IDCODE does;
  - ``skipped``: no OpenOCD here, or the read could not be made (the harness not up yet after
    a power cycle, the JTAG server held...), with the reason;

- it NEVER fails the reboot and never changes the board: the record goes into the result
  (``design_check``), on the bus as ``design.check`` (the app's Activity row), and the service
  keeps the board's last one (``DesignChecks``: ``GET /boards/{bid}`` ``design_check``) until
  a deploy proves its own design (``deploy.done``) or the board is closed.

It lives apart from ``services/debug.py`` on purpose: it only calls ``DebugService.detect``.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from typing import Any

from harness_manager.core.errors import (
    HarnessError,
    NothingOnTargetError,
    UnavailableError,
)
from harness_manager.core.events import Event

log = logging.getLogger(__name__)

#: The IDCODE the platform's DAP answers with (the SoC-400 SWJ-DP; ``debug detect`` on nanosoc).
EXPECTED_IDCODE = "0x6ba00477"
TOPIC = "design.check"
VERIFIED = "verified"
UNVERIFIED = "unverified"
SKIPPED = "skipped"
KNOWN_ISSUE = "known issue, Linux v2.0.0"
#: The ``after`` words of the two cold boots.
AFTER_MCC_REBOOT = "mcc reboot"
AFTER_POWER_CYCLE = "power cycle"


def _design(rm_name: str, rm_id: str) -> str:
    return f"{rm_name or 'the design'} ({rm_id})" if rm_id else (rm_name or "the design")


def _record(state: str, ident: Any, text: str, *, idcode: str = "", reason: str = "",
            after: str = "") -> dict[str, Any]:
    return {"state": state, "rm_id": str(getattr(ident, "rm_id", "") or ""),
            "rm_name": str(getattr(ident, "rm_name", "") or ""), "idcode": idcode,
            "expected_idcode": EXPECTED_IDCODE, "text": text, "reason": reason,
            "after": after, "at": time.time()}


def _was_linux(engine: Any, session: Any) -> bool:
    """Whether the board ran the Linux harness at its last ``info`` (a read of what the engine
    kept, never of the board): a power cycle's harness is still booting."""
    last = getattr(engine, "last_identity", None)
    try:
        prev = last(session.candidate.board_id) if callable(last) else None
    except Exception:  # noqa: BLE001 - unknown is "no"
        return False
    return str(getattr(prev, "harness_impl", "") or "") == "linux"


def check_design(engine: Any, session: Any, *,
                 after: str = AFTER_MCC_REBOOT) -> dict[str, Any] | None:
    """The cross-check itself (module docstring); None when it does not apply. Never raises."""
    try:
        ident = session.identity()
    except HarnessError as exc:
        if after != AFTER_POWER_CYCLE or not _was_linux(engine, session):
            return None                      # no identity, no claim to check
        return _record(SKIPPED, None, "not cross-checked: the harness has not answered since "
                       "the power cycle (a Linux cold boot takes ~3 min); `harness-manager "
                       "debug detect TARGET` checks the design it reports once `info` answers",
                       reason=str(exc), after=after)
    except Exception:  # noqa: BLE001 - never fails the reboot
        log.exception("the design check could not read the identity")
        return None
    if str(getattr(ident, "harness_impl", "") or "") != "linux":
        return None
    design = _design(ident.rm_name, ident.rm_id)
    adapter = getattr(session, "debug", None)
    if adapter is None:
        return None
    try:
        adapter.openocd_config()             # the pack's word: does this design have a DAP?
    except NothingOnTargetError:
        return None                          # greybox and friends: nothing to cross-check
    except UnavailableError:
        return None                          # not decidable here (a proxy: the service checks)
    except Exception as exc:  # noqa: BLE001 - never fails the reboot
        return _record(SKIPPED, ident, f"not cross-checked: could not tell whether {design} "
                       f"has a debug port ({exc})", reason=str(exc), after=after)
    debug = getattr(engine, "debug", None)
    if debug is None or not callable(getattr(debug, "detect", None)):
        return _record(SKIPPED, ident, "not cross-checked: this engine has no OpenOCD "
                       f"service; the board reports {design}", after=after)
    try:
        idcode = str(debug.detect(session) or "").lower()
    except NothingOnTargetError as exc:
        return _record(UNVERIFIED, ident, f"the board reports {design} but no debug port "
                       f"answers: greybox is probably resident ({KNOWN_ISSUE})",
                       reason=str(exc), after=after)
    except UnavailableError as exc:
        return _record(SKIPPED, ident, f"not cross-checked: no OpenOCD here ({exc.reason}); "
                       f"the board reports {design}", reason=str(exc), after=after)
    except HarnessError as exc:
        return _record(SKIPPED, ident, "not cross-checked: the IDCODE read did not finish "
                       f"({exc}); the board reports {design}", reason=str(exc), after=after)
    except Exception as exc:  # noqa: BLE001 - never fails the reboot
        log.exception("the design check's IDCODE read failed")
        return _record(SKIPPED, ident, f"not cross-checked: {type(exc).__name__}: {exc}; "
                       f"the board reports {design}", reason=str(exc), after=after)
    if idcode == EXPECTED_IDCODE:
        return _record(VERIFIED, ident, f"the board reports {design} and its debug port "
                       f"answers (IDCODE {idcode})", idcode=idcode, after=after)
    return _record(UNVERIFIED, ident, f"the board reports {design} but its debug port answers "
                   f"IDCODE {idcode or '?'}, not {EXPECTED_IDCODE}: another design is "
                   "probably resident", idcode=idcode, after=after)


def after_cold_boot(engine: Any, session: Any, *, after: str = AFTER_MCC_REBOOT,
                    bus: Any = None) -> dict[str, Any] | None:
    """``check_design``, then the ``design.check`` event (when it applied). Never raises."""
    rec = check_design(engine, session, after=after)
    if rec is not None and bus is not None:
        try:
            bid = session.candidate.board_id
            bus.publish(Event(TOPIC, bid, dict(rec)))
        except Exception:  # noqa: BLE001 - the record is the result's either way
            log.exception("publishing the design check failed")
    return rec


def attach(evidence: Any, rec: dict[str, Any] | None) -> Any:
    """A reboot's (or power cycle's) result with its check: ``design_check`` is set on a dict
    result whether or not the check applied (null then), so a client knows it was asked."""
    if isinstance(evidence, dict):
        return {**evidence, "design_check": rec}
    return evidence if rec is None else {"design_check": rec}


def from_result(engine: Any, session: Any, evidence: Any, *, after: str = AFTER_MCC_REBOOT,
                bus: Any = None) -> dict[str, Any] | None:
    """The CLI's check: the service's (its job's result carries ``design_check``), else one
    made here (the in-process engine). Never raises."""
    if isinstance(evidence, dict) and "design_check" in evidence:
        rec = evidence.get("design_check")
        return dict(rec) if isinstance(rec, dict) else None
    return after_cold_boot(engine, session, after=after, bus=bus)


def human(rec: dict[str, Any] | None) -> list[str]:
    """The CLI's line for a check (none when it did not apply)."""
    if not rec:
        return []
    head = {VERIFIED: "verified: ", UNVERIFIED: "UNVERIFIED: "}.get(rec.get("state", ""), "")
    return [f"design     {head}{rec.get('text', '')}"]


class DesignChecks:
    """The service's last check per board (``GET /boards/{bid}`` ``design_check``): kept from
    the ``design.check`` event until a deploy proves its own design (``deploy.done``) or the
    board is closed (``session.closed``)."""

    def __init__(self, bus: Any) -> None:
        self._mu = threading.Lock()
        self._last: dict[str, dict[str, Any]] = {}
        self._unsub: list[Callable[[], None]] = []
        if bus is not None:
            self._unsub = [bus.subscribe(TOPIC, self._on_check),
                           bus.subscribe("deploy.done", self._forget),
                           bus.subscribe("session.closed", self._forget)]

    def _on_check(self, ev: Event) -> None:
        if ev.board_id:
            with self._mu:
                self._last[ev.board_id] = dict(ev.data or {})

    def _forget(self, ev: Event) -> None:
        with self._mu:
            self._last.pop(ev.board_id, None)

    def last(self, board_id: str) -> dict[str, Any] | None:
        with self._mu:
            rec = self._last.get(board_id)
            return dict(rec) if rec is not None else None

    def close(self) -> None:
        for unsub in self._unsub:
            unsub()
        self._unsub = []
