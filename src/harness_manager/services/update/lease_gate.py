"""The hub lease as a gate on harness installs (HARNESS-CAT; HARNESS-DIST §2 and §6(b)).

A shared lab board sits behind a hub, and its lease is who may drive it. Writing a new
harness to it, or rebooting it into one, interrupts whoever holds the lease, so an
install (and a rollback) runs only for the lease holder:

- a board with no hub (``session.hub`` None: the Debug USB on this machine, a standalone
  board) has no lease: the gate is open (``required: false``);
- behind a hub, the lease must be held by this client (``LeaseService.view``: ``mine``);
  nobody holding it, someone else holding it, or a hub that cannot be asked all refuse
  with ``HeldError`` (exit 4, HTTP 409) naming the holder and the next step.

The same rule as XVC (``services/xvc.py`` ``check_lease``). ``leases`` is anything with
``view(hub)`` (the daemon shares its ``LeaseService``; tests pass a stand-in).
"""

from __future__ import annotations

from typing import Any

from harness_manager.core.errors import HarnessError, HeldError


def lease_state(session: Any, leases: Any) -> dict[str, Any]:
    """``{required, mine, holder, target, reason}`` for the board's hub lease (never raises).

    ``reason`` is "" when an install may go ahead, else why not.
    """
    hub = getattr(session, "hub", None) if session is not None else None
    if hub is None:
        return {"required": False, "mine": False, "holder": "", "target": "",
                "reason": ""}
    target = str(getattr(hub, "target", "") or "the board")
    out: dict[str, Any] = {"required": True, "mine": False, "holder": "", "target": target,
                           "reason": ""}
    forget = getattr(leases, "forget", None)
    if callable(forget):
        try:
            forget(hub)                   # a fresh answer: a lease taken a moment ago counts
        except Exception:  # noqa: BLE001 - a cache drop must never stop the check
            pass
    try:
        view = leases.view(hub) if leases is not None else None
    except HarnessError as exc:
        out["holder"] = "unknown (the hub did not answer)"
        out["reason"] = f"cannot confirm you hold the lease on {target}: {exc.message}"
        return out
    lease = (view or {}).get("lease")
    if not lease:
        out["holder"] = "nobody"
        out["reason"] = f"nobody holds the lease on {target}"
        return out
    out["holder"] = str(lease.get("holder") or "someone else")
    if lease.get("mine"):
        out["mine"] = True
        return out
    out["reason"] = f"{out['holder']} holds the lease on {target}"
    return out


def require_lease(session: Any, leases: Any, what: str) -> dict[str, Any]:
    """The lease state when ``what`` may go ahead; ``HeldError`` otherwise."""
    st = lease_state(session, leases)
    if not st["required"] or st["mine"]:
        return st
    if st["holder"] == "nobody":
        hint = "take the lease first: `harness-manager lease acquire TARGET`"
    elif st["holder"].startswith("unknown"):
        hint = ("an install runs for the lease holder only; retry when the hub answers "
                "(`harness-manager lease show TARGET`)")
    else:
        hint = "ask for the board: `harness-manager lease request TARGET`"
    raise HeldError(f"cannot {what}: it is for the lease holder only, and {st['reason']}",
                    holder=st["holder"], hint=hint)
