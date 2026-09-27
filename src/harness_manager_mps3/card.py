"""The MPS3's user microSD (D13 overlay store) for Harness Manager (lane LINUX-SLOTS).

``make_card_adapter(session)`` is the ``pack.py`` hook (``BoardSession.card``, CCR LS-1).
Over pyverify's ``usd`` verb and the v0.13 re-push ``commit`` (net-protocol.md "User
microSD"; docs/planning/HANDOVER_USD_OVERLAY_STORE.md):

- ``status``: card present?, the store's state, its capacity, the DEFAULT overlay the
  board loads at power-on (rm, static, A/B store slot), the power-on decision, and, on a
  Linux harness, the OS boot slots on the same card (``BoardSession.os_slots``);
- ``commit``: make the RUNNING pair the power-on default. The shell never holds a whole
  partial (they stream straight into the ICAP), so persisting is a RE-PUSH of the same
  clearing + partial from this host's overlay store into the card's inactive store slot
  (``SwapOrchestrator.commit``: ``commit`` parks 6900, the pair goes over 6910, the shell
  reads it back and only then flips the header). Only what is running can be committed:
  the running ``rm_id`` and the shell's own ``static_id``;
- ``clear``: invalidate the default, so the next power-on boots the greybox.

**david's hard rule: no card -> the boot is unchanged.** With no card every mutation is
refused before anything is sent (``usd`` status is read, nothing else), and the error
says the board keeps booting as it always has.

**Which harness.** A harness that does not report the ``usd`` feature has no store
(today's fielded bare metal): ``card_reason()`` says so and nothing is sent. D13 runs on
both engines, so a bare-metal image built with it has a card too.

**One reader, one pusher** (L1-CARD, merged first): the card is read with
``Mps3Deploy.card_status`` and a commit sends its pair with ``Mps3Deploy.commit_pusher``,
the same as the deploy's "Keep on the card".

**The claim lock** (HM_ANSWERS S6, lane CLAIMED-LOCK). On a claimed Linux board the store's
mutations (``usd`` format/clear/rescan, the re-push ``commit``) are refused for any peer but
the board itself: ``usd locked: board claimed (use ssh)`` / ``commit locked: ...``, code
``locked``; ``usd`` status stays open. ``clear`` and ``commit`` ask the session's claim first
(``claim.lock_route``): a board this Harness Manager claimed is changed through the session's
board-SSH forward (6900 and 6910 on the board's 127.0.0.1); one claimed by another key, or
with no pin here, is ``ClaimLockedError`` before anything is sent; otherwise today's path,
and a lock refusal met there is asked about again (sent through the board's SSH when the
claim is ours, else ``ClaimLockedError`` with the claim hint).
"""

from __future__ import annotations

import logging
from dataclasses import replace
from typing import Any

from pyverify import rm_id as rmid
from pyverify.swap import SwapOrchestrator

from harness_manager.core.errors import (
    AbsentError,
    ActionFailedError,
    HarnessError,
    HeldError,
    IncompatibleError,
    RefusedError,
    UnavailableError,
)
from harness_manager.core.pack import CardStatus, Progress

from . import slot_words
from .constants import IMPL_LINUX

log = logging.getLogger(__name__)

CAPABILITY = "user microSD"
#: The control connection parks for the whole commit (write + read-back of ~3 MB on the
#: card): at least this, more for a bigger pair (SLOT-TIMING: the card writes at ~70 KB/s and
#: reads back at 14-135 KB/s on silicon, so ~3 MB can take ~4.5 min; ``os_slots.budget_s``).
COMMIT_TIMEOUT_S = 300.0
NO_CARD_HINT = ("insert a card in the board's user microSD slot; without one the board boots "
                "exactly as it always has")


def _hex32(value: Any) -> str:
    try:
        return rmid.format_rm_id(value)
    except (TypeError, ValueError):
        return str(value or "")


def _same(a: Any, b: Any) -> bool:
    try:
        return rmid.parse_rm_id(a) == rmid.parse_rm_id(b)
    except (TypeError, ValueError):
        return False


def card_json(st: CardStatus) -> dict[str, Any]:
    """What the CLI and the API show of a ``CardStatus`` (``services.slots``)."""
    from harness_manager.services.slots import card_status_json

    return card_status_json(st)


class Mps3Card:
    """``core.pack.CardAdapter`` for an MPS3 session (module docstring).

    The card is read with L1-CARD's reader (``Mps3Deploy.card_status``: ``version`` then
    ``usd``, nothing written), and a commit sends its pair with the same commit pusher
    "Keep on the card" uses (``Mps3Deploy.commit_pusher``)."""

    def __init__(self, session: Any, *, commit_timeout_s: float | None = None) -> None:
        self._session = session
        #: None: ``commit_budget(pair bytes)``; a number: fixed (a test seam).
        self.commit_timeout_s = commit_timeout_s
        #: The pusher the last commit built (tests read its transport).
        self.last_pusher: Any = None

    # -- plumbing -------------------------------------------------------------------------------

    def _shell(self) -> Any:
        shell = getattr(self._session, "shell", None)
        if shell is None:
            raise UnavailableError(CAPABILITY, "no Ethernet link to the harness")
        return shell

    def _deploy(self) -> Any:
        deploy = getattr(self._session, "deploy", None)
        if deploy is None or not callable(getattr(deploy, "card_status", None)):
            raise UnavailableError(CAPABILITY, "this session has no deploy adapter to read the "
                                               "card with")
        return deploy

    def _catalogue(self) -> Any:
        cat = getattr(getattr(self._session, "deploy", None), "catalogue", None)
        if cat is None:
            from .overlays import default_catalogue

            cat = default_catalogue()
        return cat

    def _rm_name(self, rm_id: Any) -> str:
        from .overlays import resolve_rm_name

        try:
            return resolve_rm_name(rm_id, self._catalogue())
        except Exception:  # noqa: BLE001 - a name is never worth failing a status for
            return ""

    def _read(self) -> CardStatus:
        """L1-CARD's read; a harness with no store is ``UnavailableError``."""
        st = self._deploy().card_status()
        if not st.store:
            raise UnavailableError(CAPABILITY, st.reason or "this harness has no microSD store")
        return st

    def card_reason(self) -> str:
        try:
            self._read()
        except UnavailableError as exc:
            return exc.reason
        except HarnessError as exc:
            return f"the harness did not answer: {exc.message}"
        return ""

    # -- status ---------------------------------------------------------------------------------

    def status(self) -> CardStatus:
        return self.annotate(self._read())

    def annotate(self, st: CardStatus) -> CardStatus:
        """L1-CARD's read, completed: the default's RM name, notes, and (Linux) the OS slots
        on the same card. The daemon's ``GET /card`` calls it on the read it already made."""
        notes: list[str] = []
        default = dict(st.default) if st.default else None
        if default is not None:
            default = {"rm_id": _hex32(default.get("rm_id")),
                       "rm_name": self._rm_name(default.get("rm_id")),
                       "static_id": _hex32(default["static_id"]) if default.get("static_id")
                       else "", "slot": default.get("slot", "")}
        if st.state == "stale" and default is not None:
            notes.append(f"the default was committed for static {default['static_id'] or '?'}, "
                         "not this shell's: it is skipped at power-on (commit one built for "
                         "this shell)")
        if st.state == "foreign":
            notes.append("the card holds no harness store (a PC card?): it is never written; "
                         "`pyverify usd format` makes it a harness card")
        if not st.present:
            notes.append("no card: the board boots exactly as it always has")
        os_st = None
        slots = getattr(self._session, "os_slots", None)
        if st.present and slots is not None:
            try:
                live = self._shell().live()
                if live.impl == IMPL_LINUX and not slots.slots_reason():
                    os_st = slots.status()
            except HarnessError as exc:
                notes.append(f"the OS slots could not be read: {exc.message}")
        return replace(st, default=default, os_slots=os_st, notes=tuple(notes))

    # -- clear ----------------------------------------------------------------------------------

    def clear(self) -> CardStatus:
        st = self._read()
        if not st.present:
            raise RefusedError("no card in the user microSD slot: nothing was changed",
                               hint=NO_CARD_HINT)
        if st.state == "foreign":
            raise RefusedError("the card holds no harness store (foreign): it is never "
                               "written, and has no default to clear")
        impl = self._shell().live().impl
        if self._route("card clear", impl) == "board-ssh":
            resp = self._clear_via_board()
        else:
            resp = self._shell().call(lambda c: c.usd_clear())
            if not resp.ok and slot_words.is_claim_lock(str(resp.err or "")):
                self._route("card clear", impl, claimed=True)   # not ours: the hint
                resp = self._clear_via_board()
        if not resp.ok:
            raise _usd_error("card clear", resp.err)
        return self.status()

    # -- the claim lock (CLAIMED-LOCK) -----------------------------------------------------------

    def _route(self, what: str, impl: str, claimed: bool | None = None) -> str:
        """The claim's route for a store mutation ("" direct, "board-ssh"); a board this
        Harness Manager cannot enter is ``ClaimLockedError``, before anything is sent."""
        claim = getattr(self._session, "claim", None)
        if claim is None or not callable(getattr(claim, "lock_route", None)):
            return "board-ssh" if claimed else ""
        return claim.lock_route(what, impl=impl, claimed=claimed)

    def _clear_via_board(self) -> Any:
        from .os_slots import claim_forward
        from .shell import Mps3Shell

        with claim_forward(self._session, "card", "the card clear") as (host, ctl, _push):
            return Mps3Shell(host, ctl).call(lambda c: c.usd_clear())

    # -- commit ---------------------------------------------------------------------------------

    def commit(self, progress: Progress | None = None) -> dict[str, Any]:
        report: Progress = progress or (lambda p, d, t: None)
        st = self._read()
        if not st.present:
            raise RefusedError("no card in the user microSD slot: nothing was written",
                               hint=NO_CARD_HINT)
        if not st.committable:
            hint = ("a card with no harness store: format it first (`pyverify usd format`)"
                    if st.state == "foreign" else "`harness-manager card status TARGET` says why")
            raise RefusedError(f"the card cannot take a commit now (state {st.state!r}: "
                               f"{st.reason or st.text or 'no text'})", hint=hint)
        shell = self._shell()
        live = shell.live()
        if not live.rm_id or rmid.is_greybox(live.rm_id):
            raise RefusedError("nothing to persist: the partition runs the greybox",
                               hint="deploy an overlay first; `harness-manager card clear "
                                    "TARGET` makes the greybox the power-on default")
        entry = self._entry(live.rm_id, live.shell_id)
        name = entry.ref.name
        total = entry.overlay.manifest.clearing.len + entry.overlay.manifest.partial.len
        sent: dict[Any, int] = {}

        def on_frame(kind: Any, n: int) -> None:
            sent[kind] = n
            report("commit", sum(sent.values()), total)

        budget = commit_budget(total)
        from .os_slots import claim_forward
        from .shell import Mps3Shell

        wait_s = self.commit_timeout_s if self.commit_timeout_s is not None else budget.job_s

        def send(host: str, ctl: int, push: int | None) -> Any:
            pusher = self._deploy().commit_pusher(windowed="windowed" in live.features,
                                                  impl=live.impl, on_frame=on_frame,
                                                  stall_s=budget.push_stall_s, host=host,
                                                  port=push)
            self.last_pusher = pusher
            report("commit", 0, total)
            parked = Mps3Shell(host, ctl, timeout=wait_s)
            return parked.call(lambda c: SwapOrchestrator(c, pusher, commit_pusher=pusher).commit(
                entry.overlay, rm_id=rmid.parse_rm_id(live.rm_id),
                static_id=rmid.parse_rm_id(live.shell_id), features=tuple(live.features)))

        what = f"the card commit of {name}"
        if self._route(what, live.impl) == "board-ssh":
            with claim_forward(self._session, "card", what) as (host, ctl, push):
                reply = send(host, ctl, push)
        else:
            reply = send(shell.host, shell.port, None)
            if not reply.ok and slot_words.is_claim_lock(str(reply.err or "")):
                self._route(what, live.impl, claimed=True)     # not ours: the hint
                with claim_forward(self._session, "card", what) as (host, ctl, push):
                    reply = send(host, ctl, push)
        if not reply.ok:
            raise _usd_error(f"card commit of {name}", reply.err)
        report("commit", total, total)
        return {"slot": reply.slot, "rm_name": name, "rm_id": _hex32(live.rm_id),
                "static_id": _hex32(live.shell_id), "bytes": total}

    def _entry(self, rm_id: str, shell_id: str) -> Any:
        """The running pair in this host's overlay store: same rm_id, keyed to this shell."""
        cat = self._catalogue()
        for e in cat.entries():
            m = e.overlay.manifest
            if _same(m.rm_id, rm_id) and _same(m.static_id, shell_id):
                return e
        name = self._rm_name(rm_id) or "the running RM"
        raise AbsentError(
            f"{name} ({_hex32(rm_id)} for shell {_hex32(shell_id)}) is not in this host's "
            "overlay store, so its pair cannot be re-pushed to the card",
            hint="import or deploy it from Harness Manager first (the card commit re-pushes "
                 "the running pair; the shell keeps no copy)")


def commit_budget(nbytes: int) -> Any:
    """A card commit's budget (``os_slots.SlotTimeouts``): the parked control connection
    waits ``max(COMMIT_TIMEOUT_S, the pair written and read back at the card's budget
    rates)``; the pair's per-chunk stall limit is the OS-slot push's
    (``mps3.slot.push_timeout_s``, the no-progress limit): the same card, written the same way."""
    from .os_slots import budget_s, slot_timeouts

    t = slot_timeouts(nbytes)
    return replace(t, job_s=max(COMMIT_TIMEOUT_S, budget_s(nbytes)), setting_s=COMMIT_TIMEOUT_S)


def _usd_error(what: str, err: str) -> HarnessError:
    """A ``usd``/``commit`` refusal (contract error NAMES) -> the error class it means."""
    e = (err or "").strip()
    if e.startswith("unknown op"):
        return UnavailableError(CAPABILITY, f"this harness does not have the usd verb ({e})")
    if e in ("no card", "no sd card"):
        return RefusedError(f"{what}: no card in the user microSD slot; nothing was written",
                            hint=NO_CARD_HINT)
    if e == "no hw":
        return UnavailableError(CAPABILITY, "the fabric has no user-microSD controller (usd_spi)")
    if e == "foreign":
        return RefusedError(f"{what}: the card holds no harness store (foreign); it is never "
                            "written", hint="`pyverify usd format` makes it a harness card")
    if e == "stale key":
        return IncompatibleError(f"{what}: the pair is not keyed to this shell (stale key)")
    if e == "rm mismatch":
        return IncompatibleError(f"{what}: that is not the RM running now (rm mismatch)",
                                 hint="only the running pair can be committed")
    if e == "store busy":
        return HeldError(f"{what}: the card store is busy", hint="try again in a moment")
    if slot_words.is_claim_lock(e):
        # S6: `usd locked: board claimed (use ssh)` / `commit locked: ...` (the claim lock)
        from harness_manager.services.claim import LOCK_HINT

        return slot_words.ClaimLockedError(f"{what}: {e}", hint=LOCK_HINT)
    lock = slot_words.identity_lock_error(e, what)
    if lock is not None:
        # The same words as a swap or a slot act refused by it (LINUX-ANSWERS): mismatch
        # (push, commit, reboot) or unknown (which side cannot be read), never "give up".
        return lock
    if e == "unavailable":
        return UnavailableError(CAPABILITY, "the harness's card store is unavailable")
    return ActionFailedError(f"{what} failed: {e or 'no reason given'}",
                             hint="the card keeps its previous default")


def make_card_adapter(session: Any) -> Mps3Card | None:
    """The pack hook (CCR LS-1): an adapter for any session with an Ethernet shell. Whether
    the harness has a store is ``card_reason()``."""
    if getattr(session, "shell", None) is None:
        return None
    return Mps3Card(session)
