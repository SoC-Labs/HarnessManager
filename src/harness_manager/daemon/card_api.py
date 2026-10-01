"""The Linux harness's OS slots and the user microSD over the daemon API (lanes LINUX-SLOTS,
UI2-API-BUILD G6).

docs/API.md "User microSD and OS slots (LINUX-SLOTS, ``card_api.py``)" and "OS slots and the
card: roll back, commit, clear (UI2-API-BUILD G6, ``card_api.py``)":

- ``GET /boards/{bid}/slots`` -> ``{available, reason, slots}``: the OS slots A/B (running,
  default, where a push goes, the card job);
- ``POST /boards/{bid}/slots/rollback`` ``{confirm, reboot?, wait_s?}`` -> 202 job
  ``slot_rollback`` (``harness-manager slot rollback``);
- ``POST /boards/{bid}/card/commit`` ``{confirm}`` -> 202 job ``card_commit`` (``card commit``:
  the running overlay becomes the card's power-on default);
- ``POST /boards/{bid}/card/clear`` ``{confirm}`` -> 202 job ``card_clear`` (``card clear``);
- FIX-PACK-6, so the CLI's other slot changes work while the app holds the board (H1 Z1):
  ``POST /boards/{bid}/slots/push`` ``{confirm, image?, bundle?, static_id?, version?,
  rollback_first?}`` -> 202 job ``slot_push`` (``slot push``; absolute paths on this host),
  ``POST /boards/{bid}/slots/commit`` ``{confirm, slot?}`` -> 202 job ``slot_commit`` and
  ``POST /boards/{bid}/slots/verify`` ``{slot?}`` -> 202 job ``slot_verify`` (a read: no
  confirm, no lease, as the CLI).

The card itself is L1-CARD's ``GET /boards/{bid}/card`` (core routes), which LINUX-SLOTS
extends additively (the default's RM, the OS slots, the tile's ``line``). A harness
without OS slots is not an error for the read: ``available`` is false and ``reason`` says
why; a change is 422 UNAVAILABLE with that reason, before any job.

The changes keep Harness Manager's rules (``services.slots``, as the CLI): the front-end's
confirm (``confirm: true``), the board's hub lease when it is behind one (409 HELD naming the
holder, checked before the job and again inside it), and a claimed board is changed only
through its own SSH (the pack's claim route). The reset guard (SLOT-TIMING) is the pack's:
a rollback's reboot while the card job writes or reads back fails the job HELD. Nothing here
writes an OS slot image: a push is the harness install's (``POST /boards/{bid}/harness/install``).

LINUX-ANSWERS adds to ``slots`` (additive, ``services.slot_health``): ``fell_back`` (the
default slot that failed to boot: stage0 went back to the other one), ``committed_unbooted``,
``confirmed`` and ``claimed`` (the board's words when it sends them, else null), ``notes``,
and per slot ``boot`` ("booted (not yet confirmed)": ``verified: boot`` is never a confirm).
The read only asks ``slot status``: it never starts a ``verify`` (minutes of card time).
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from harness_manager.core.errors import (
    AbsentError,
    HarnessError,
    RefusedError,
    UnavailableError,
    UsageError,
)
from harness_manager.services import slot_health
from harness_manager.services.slot_health import extend_json
from harness_manager.services.slots import (
    REBOOT_WAIT_S,
    SlotService,
    card_line,
    card_status_json,
    lease_check_for,
    slot_status_json,
)

from .app import _JSON, JsonBody, RouteContext, _bool, _number, _obj, ok

#: The longest a rollback's reboot may be waited for (the Linux budget is 300 s).
MAX_WAIT_S = 1800.0


def slots_json(st: Any) -> dict[str, Any]:
    """A ``SlotStatus`` as ``GET /slots`` shows it (with LINUX-ANSWERS' keys)."""
    return extend_json(slot_status_json(st), st)


def card_json(st: Any) -> dict[str, Any]:
    """A ``CardStatus`` as ``GET /card`` shows it (its OS slots with LINUX-ANSWERS' keys)."""
    doc = card_status_json(st)
    if st.os_slots is not None and isinstance(doc.get("os_slots"), dict):
        extend_json(doc["os_slots"], st.os_slots)
    return doc


def confirmed(b: dict[str, Any], what: str) -> None:
    """The front-end's confirm: ``confirm: true``, else 400 (nothing is sent)."""
    if b.get("confirm") is not True:
        raise UsageError(f"to {what}, send confirm: true",
                         hint="the page asks first; the CLI asks, or takes --yes")


def register(ctx: RouteContext) -> None:
    d = ctx.daemon
    api = ctx.api
    reader = SlotService()

    def service() -> SlotService:
        """The changes' service: the daemon's lease cache when hub_api has one (so "mine" is
        the same view the page shows), the engine's bus and store."""
        return SlotService(lease_check=lease_check_for(d.engine, getattr(d, "leases", None)),
                           bus=getattr(d, "bus", None),
                           store=getattr(d.engine, "store", None))

    def still_open(bid: str, s: Any, what: str) -> None:
        try:
            current = ctx.board(bid)
        except HarnessError:
            current = None
        if current is not s:
            raise AbsentError(f"{bid} was closed before the {what} started; nothing was sent",
                              hint="open the board again, then retry")

    def precheck(bid: str, s: Any, svc: SlotService, adapter: Callable[[Any], Any],
                 what: str, *, card: bool = False) -> None:
        """Before the 202: the adapter can be used (422 with the reason), a card change has a
        card to change (409 REFUSED, as the CLI's exit 15), and the lease is this client's
        (409 HELD naming the holder). Under the board gate (409 while a job runs)."""
        with d.gates.op(bid):
            adapter(s)
            if card and not svc.card_status(s).present:
                raise RefusedError(f"no card in the USER microSD slot: nothing to {what}",
                                   hint="the board boots exactly as it always has without "
                                        "one; insert a card first")
            svc.check_lease(s, what)

    @api.get("/boards/{bid:path}/slots")
    def slots_read(bid: str) -> Any:
        s = ctx.board(bid)
        with d.gates.op(bid):
            try:
                st = reader.status(s)
            except UnavailableError as exc:
                return _JSON(ok(board_id=bid, available=False, reason=exc.reason, slots=None))
        return _JSON(ok(board_id=bid, available=True, reason="", slots=slots_json(st)))

    # -- UI2-API-BUILD G6: the changes (Board > Versions) ---------------------------------------

    @api.post("/boards/{bid:path}/slots/rollback")
    def slots_rollback(bid: str, body: JsonBody = None) -> Any:
        s = ctx.board(bid)
        b = _obj(body)
        confirmed(b, "roll the OS slot back")
        reboot = _bool(b, "reboot", True)
        wait_s = _number(b, "wait_s", REBOOT_WAIT_S)
        if not 0 < wait_s <= MAX_WAIT_S:
            raise UsageError(f"wait_s must be 1-{MAX_WAIT_S:.0f} seconds, not {wait_s:g}")
        svc = service()
        what = "roll the OS slot back"
        precheck(bid, s, svc, svc.slots, what)

        def run(progress: Callable[..., None]) -> Any:
            still_open(bid, s, "slot rollback")
            before = svc.status(s)
            fell = slot_health.fell_back(before)
            progress("rollback", 0, 0)
            out = svc.rollback(s, reboot=reboot, wait_s=wait_s, progress=progress)
            if fell:
                out["note"] = (f"slot {out['slot']} is the default again (slot {fell} failed "
                               f"to boot); slot {fell} is free for a push")
            note = out.get("note") or (f"slot {out['slot']} runs again (rebooted)"
                                       if out["rebooted"] else f"slot {out['slot']} is the default")
            result = {"board_id": bid, "act": "rollback", "slot": out["slot"],
                      "rebooted": out["rebooted"], "fell_back": fell or None, "note": note,
                      "slots": slots_json(out["status"])}
            if "evidence" in out:
                result["evidence"] = out["evidence"]
            return result

        return ctx.accepted(d.jobs.submit("slot_rollback", bid, run))

    # -- FIX-PACK-6: the CLI's other slot changes, so they work while the app holds the board --

    @api.post("/boards/{bid:path}/slots/push")
    def slots_push(bid: str, body: JsonBody = None) -> Any:
        from pathlib import Path

        from harness_manager.services.slots import push_source

        s = ctx.board(bid)
        b = _obj(body)
        confirmed(b, "push an OS image to the board's card")

        def path_of(key: str) -> Path | None:
            raw = b.get(key)
            if raw in (None, ""):
                return None
            if not isinstance(raw, str) or not Path(raw).is_absolute():
                raise UsageError(f"{key} must be an absolute path on the service's host",
                                 hint="the CLI sends its paths made absolute")
            return Path(raw)

        source = push_source(path_of("image"), bundle=path_of("bundle"),
                             static_id=str(b.get("static_id") or ""),
                             version=str(b.get("version") or ""))
        rollback_first = _bool(b, "rollback_first", False)
        svc = service()
        precheck(bid, s, svc, svc.slots, "push an OS image to the board's card")

        def run(progress: Callable[..., None]) -> Any:
            still_open(bid, s, "slot push")
            progress("push", 0, 0)
            out = svc.push(s, source, rollback_first=rollback_first, progress=progress)
            return {"board_id": bid, "act": "push", "slot": out["slot"],
                    "rolled_back_first": out["rolled_back_first"],
                    "image": source.image.name, "static_id": source.static_id,
                    "slots": slots_json(out["status"])}

        return ctx.accepted(d.jobs.submit("slot_push", bid, run))

    @api.post("/boards/{bid:path}/slots/commit")
    def slots_commit(bid: str, body: JsonBody = None) -> Any:
        s = ctx.board(bid)
        b = _obj(body)
        confirmed(b, "commit an OS slot")
        slot = b.get("slot")
        if slot not in (None, "A", "B"):
            raise UsageError(f"slot must be A or B, not {slot!r}")
        svc = service()
        precheck(bid, s, svc, svc.slots, "commit an OS slot")

        def run(progress: Callable[..., None]) -> Any:
            still_open(bid, s, "slot commit")
            progress("commit", 0, 0)
            out = svc.commit(s, slot)
            return {"board_id": bid, "act": "commit", "slot": out["slot"], "note": out["note"],
                    "slots": slots_json(out["status"])}

        return ctx.accepted(d.jobs.submit("slot_commit", bid, run))

    @api.post("/boards/{bid:path}/slots/verify")
    def slots_verify(bid: str, body: JsonBody = None) -> Any:
        s = ctx.board(bid)
        b = _obj(body)
        slot = b.get("slot")
        if slot not in (None, "A", "B"):
            raise UsageError(f"slot must be A or B, not {slot!r}")
        svc = service()
        with d.gates.op(bid):                    # a read: open to anyone, no lease, no confirm
            svc.slots(s)

        def run(progress: Callable[..., None]) -> Any:
            still_open(bid, s, "slot verify")
            progress("verify", 0, 0)
            out = svc.verify(s, slot, progress=progress)
            return {"board_id": bid, "act": "verify", "slot": out["slot"],
                    "slots": slots_json(out["status"])}

        return ctx.accepted(d.jobs.submit("slot_verify", bid, run))

    @api.post("/boards/{bid:path}/card/commit")
    def card_commit(bid: str, body: JsonBody = None) -> Any:
        s = ctx.board(bid)
        confirmed(_obj(body), "write the running overlay to the card as its power-on default")
        svc = service()
        precheck(bid, s, svc, svc.card, "commit the running overlay to the card", card=True)

        def run(progress: Callable[..., None]) -> Any:
            still_open(bid, s, "card commit")
            progress("commit", 0, 0)
            out = svc.card_commit(s, progress=progress)
            st = svc.card_status(s)
            return {"board_id": bid, "committed": out, "card": card_json(st),
                    "line": card_line(st),
                    "note": f"{out.get('rm_name') or out.get('rm_id') or 'the overlay'} is the "
                            f"power-on default (store slot {out.get('slot') or '?'})"}

        return ctx.accepted(d.jobs.submit("card_commit", bid, run))

    @api.post("/boards/{bid:path}/card/clear")
    def card_clear(bid: str, body: JsonBody = None) -> Any:
        s = ctx.board(bid)
        confirmed(_obj(body), "clear the card's power-on default")
        svc = service()
        precheck(bid, s, svc, svc.card, "clear the card's power-on default", card=True)

        def run(progress: Callable[..., None]) -> Any:
            still_open(bid, s, "card clear")
            progress("clear", 0, 0)
            st = svc.card_clear(s)
            return {"board_id": bid, "card": card_json(st), "line": card_line(st),
                    "note": "no power-on default: the greybox loads at the next power-on"}

        return ctx.accepted(d.jobs.submit("card_clear", bid, run))
