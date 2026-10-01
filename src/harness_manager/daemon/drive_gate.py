"""The hub lease as a gate on the routes that drive a board (lane UI2-API-HUB, UI v2 gap G7).

Until UI v2, only the page checked the lease before Program, Restore, Reset, Reboot, Restart
shell, Power-cycle, Clock and Debug (``JS/actions.js`` ``gateReason`` with ``holder``, the
``week.js`` ``holderOnly`` rule): the CLI through the service, a script, or a page that forgot
the check could drive a board someone else holds. Now the service refuses too, with the ONE
rule every lease gate uses (FIX-PACK-4; ``services.update.lease_gate``, as harness installs,
XVC and the SSH claim do):

- a board with no hub (``session.hub`` None) has no lease: never refused for it;
- behind a hub, the lease must be held HERE (this Harness Manager has the token). Nobody
  holding it, your hub name in another session, someone else, or a lease the hub did not
  confirm (not known is not free) is 409 HELD, before anything reaches the board, naming the
  holder, with ``error.data: {reason: "LEASE", lease: {required, mine, here, holder,
  target}}``;
- the escape is today's reset escape: ``force: true`` with ``consent: "RESET <board_id>"``
  (``services.reset_guard.force_phrase``) goes ahead anyway, and the daemon log says so.
  ``force`` with another phrase is 409 REFUSED, naming the phrase.

The check asks the hub once (``lease_state`` drops the cached view first, so a lease taken a
moment ago counts); a job or a read afterwards does not ask again.
"""

from __future__ import annotations

import logging
from typing import Any

from harness_manager.core.errors import HeldError, RefusedError, UsageError

log = logging.getLogger(__name__)

REASON = "LEASE"
#: What each gated route does, as the refusal says it.
WHAT = {"deploy": "program a design", "restore": "restore the baseline",
        "reset": "reset the board", "clocks": "set a clock", "reboot": "reboot the board",
        "power_cycle": "power-cycle the board", "debug_up": "start a debug session",
        "command": "send a REBOOT to the board controller"}


def escape(body: dict[str, Any] | None) -> tuple[bool, str]:
    """``(force, consent)`` from a request body (400 USAGE for a bad type)."""
    b = body or {}
    force = b.get("force", False)
    if not isinstance(force, bool):
        raise UsageError(f"force must be true or false, not {force!r}")
    consent = b.get("consent", "")
    if not isinstance(consent, str):
        raise UsageError("consent must be a string (type exactly: RESET <board_id>)")
    return force, consent


def require_holder(d: Any, bid: str, session: Any, kind: str,
                   body: dict[str, Any] | None = None) -> dict[str, Any] | None:
    """Refuse (409 HELD) a route of ``kind`` that drives ``bid`` unless its hub lease is held
    here, or the reset escape is given. Returns the lease state (None: no hub, or a service
    with no lease service)."""
    leases = getattr(d, "leases", None)
    if getattr(session, "hub", None) is None or leases is None:
        return None
    from harness_manager.services.update.lease_gate import lease_state

    st = lease_state(session, leases)
    if not st["required"] or st["here"]:
        return st
    what = WHAT.get(kind, kind)
    force, consent = escape(body)
    if force:
        from harness_manager.services.reset_guard import force_phrase

        want = force_phrase(bid)
        if consent.strip() != want:
            raise RefusedError(f"cannot {what} without the hub lease: force needs the typed "
                               f"phrase ({st['reason']})", hint=f"type exactly: {want}")
        log.warning("%s on %s WITHOUT the hub lease (forced with consent): %s", kind, bid,
                    st["reason"])
        return st
    if st["holder"] == "nobody":
        hint = "take the lease first (Acquire, or `harness-manager lease acquire TARGET`)"
    elif st["holder"].startswith("unknown"):
        hint = "retry when the hub answers; not known is not free"
    elif st["mine"]:
        hint = "use the session that holds the lease, or release it there first"
    else:
        hint = "ask for the board (Request, or `harness-manager lease request TARGET`)"
    err = HeldError(f"cannot {what}: on a board behind a hub it is for the lease holder only, "
                    f"and {st['reason']}", holder=st["holder"],
                    hint=f"{hint}; or force it: force true with consent \"RESET {bid}\"")
    err.data = {"reason": REASON, "lease": {k: st[k] for k in (   # type: ignore[attr-defined]
        "required", "mine", "here", "holder", "target")}}
    raise err
